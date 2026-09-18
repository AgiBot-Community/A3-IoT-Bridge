import threading
import json
import logging
import time
import zlib
from pathlib import Path
from typing import Any, Dict

from .config import AppConfig
from .dance_controller import DanceController
from .intent_parser import IntentParser
from .models import BridgeReply, ParsedIntent
from .xiaohong_client import GatewayConnectionError, GatewayProtocolError, XiaohongSEClient


class HomeBridge:
    BUSY_RETRY_DELAYS = (0.35, 0.7, 1.0)
    MODE_SCENES = {
        "normal_mode": "normal",
        "home_mode": "home",
        "away_mode": "away",
    }
    SCENE_DEVICE_TARGETS = {
        "all_lights_on": ("light_1", "light_2", "light_3"),
        "all_lights_off": ("light_1", "light_2", "light_3"),
        "bright_mode": ("light_1", "light_2", "light_3"),
        "ambient_mode": ("light_1", "light_2", "light_3"),
        "all_fans_on": ("fan_1", "fan_2"),
        "all_fans_off": ("fan_1", "fan_2"),
        "ventilation_mode": ("fan_1", "fan_2"),
    }

    def __init__(
        self,
        config: AppConfig,
        client: XiaohongSEClient = None,
        dance_controller: DanceController = None,
    ):
        self.config = config
        self.client = client or XiaohongSEClient(config.gateway)
        self.parser = IntentParser(config)
        self.dance = dance_controller or DanceController(config.dance, self.client)
        self._control_lock = threading.Lock()
        self._last_undo = None

    def handle_text(self, text: str) -> BridgeReply:
        intent = self.parser.parse(text)
        if intent.kind in ("unsupported", "clarify"):
            return BridgeReply(intent.message or "请换一种说法。", intent)

        try:
            with self._control_lock:
                if intent.kind == "device_control":
                    undo = self._capture_device_states((intent.device_id,))
                    result = self._control_device_resilient(
                        intent.device_id,
                        intent.action,
                        params=intent.params,
                    )
                    if self._execution_succeeded(result):
                        self._last_undo = undo
                    return BridgeReply(self._control_reply(intent, result), intent, result)
                if intent.kind == "scene_control":
                    if intent.scene_id in self.MODE_SCENES and self.dance.is_active:
                        stopped = self.dance.stop(reason="mode_switch")
                        if stopped.get("execution_state") != "succeeded":
                            return BridgeReply(
                                "跳舞模式还没有安全停止，请稍后再切换家居模式。",
                                intent,
                                stopped,
                            )
                    if intent.scene_id == "home_mode":
                        undo = self._capture_home_arrival_undo()
                        result = self._run_home_arrival()
                    else:
                        undo = self._capture_scene_undo(intent.scene_id)
                        result = self._control_scene_resilient(intent.scene_id)
                    if self._execution_succeeded(result):
                        self._last_undo = undo
                    return BridgeReply(self._scene_reply(intent, result), intent, result)
                if intent.kind == "state_query":
                    result = self._get_device_state_resilient(intent.device_id)
                    return BridgeReply(self._state_reply(intent, result), intent, result)
                if intent.kind == "mode_query":
                    result = self._retry_busy(self.client.get_mode, "mode query")
                    return BridgeReply(self._mode_reply(result), intent, result)
                if intent.kind == "home_summary":
                    result = self._collect_home_summary()
                    return BridgeReply(result["message"], intent, result)
                if intent.kind == "bedtime_control":
                    result = self._run_bedtime_mode()
                    return BridgeReply(result["message"], intent, result)
                if intent.kind == "undo":
                    result = self._undo_last_control()
                    return BridgeReply(result["message"], intent, result)
                if intent.kind == "dance_control":
                    if intent.action == "start":
                        result = self.dance.start()
                    elif intent.action == "stop":
                        result = self.dance.stop()
                    else:
                        result = self.dance.status()
                    return BridgeReply(self._dance_reply(intent, result), intent, result)
        except GatewayConnectionError as exc:
            logging.warning("连接小鸿 SE 失败 intent=%s error=%s", intent, exc)
            return BridgeReply("现在连不上小鸿SE，设备没有执行。", intent)
        except GatewayProtocolError as exc:
            logging.warning("小鸿 SE 协议异常 intent=%s error=%s", intent, exc)
            return BridgeReply("小鸿SE回复异常，设备没有执行。", intent)
        except ValueError as exc:
            logging.warning("家居参数异常 intent=%s error=%s", intent, exc)
            return BridgeReply("控制参数有问题，设备没有执行。", intent)

        return BridgeReply("这个指令暂时无法执行。", intent)

    @staticmethod
    def _execution_succeeded(result: Dict[str, Any]) -> bool:
        return result.get("code") == 0 and result.get("execution_state") == "succeeded"

    def _retry_busy(self, operation, description: str) -> Dict[str, Any]:
        result = operation()
        for delay in self.BUSY_RETRY_DELAYS:
            if result.get("code") != 1008 and result.get("_http_status") != 429:
                return result
            logging.warning(
                "小鸿 SE 繁忙，延迟重试 operation=%s delay=%.2fs",
                description,
                delay,
            )
            time.sleep(delay)
            result = operation()
        return result

    def _get_device_state_resilient(self, device_id: str) -> Dict[str, Any]:
        return self._retry_busy(
            lambda: self.client.get_device_state(device_id),
            "get state %s" % device_id,
        )

    def _control_device_resilient(
        self, device_id: str, action: str, params=None
    ) -> Dict[str, Any]:
        request_id = self.client.new_request_id("a3-device")
        return self._retry_busy(
            lambda: self.client.control_device(
                device_id,
                action,
                params=params,
                request_id=request_id,
            ),
            "control %s %s" % (device_id, action),
        )

    def _control_scene_resilient(self, scene_id: str) -> Dict[str, Any]:
        request_id = self.client.new_request_id("a3-scene")
        return self._retry_busy(
            lambda: self.client.control_scene(scene_id, request_id=request_id),
            "scene %s" % scene_id,
        )

    def _capture_device_states(self, device_ids) -> Dict[str, Any]:
        states = {}
        for device_id in device_ids:
            if not device_id:
                continue
            try:
                result = self._get_device_state_resilient(device_id)
            except (GatewayConnectionError, GatewayProtocolError):
                logging.warning("保存撤销状态失败 device_id=%s", device_id)
                continue
            if result.get("online") and isinstance(result.get("state"), dict):
                states[device_id] = dict(result["state"])
        return {"type": "devices", "states": states} if states else None

    def _capture_scene_undo(self, scene_id: str) -> Dict[str, Any]:
        if scene_id in self.MODE_SCENES:
            try:
                mode = self._retry_busy(self.client.get_mode, "capture mode").get("mode")
            except (GatewayConnectionError, GatewayProtocolError):
                mode = None
            return {"type": "mode", "mode": mode} if mode else None
        targets = self.SCENE_DEVICE_TARGETS.get(scene_id, ())
        return self._capture_device_states(targets)

    def _capture_home_arrival_undo(self) -> Dict[str, Any]:
        mode_undo = self._capture_scene_undo("home_mode") or {}
        light_undo = self._capture_device_states(("light_1",)) or {}
        return {
            "type": "home_arrival",
            "mode": mode_undo.get("mode"),
            "states": light_undo.get("states", {}),
        }

    def _run_home_arrival(self) -> Dict[str, Any]:
        """Enter home mode, then turn on the living-room light after its ACK."""
        mode_result = self._control_scene_resilient("home_mode")
        if not self._execution_succeeded(mode_result):
            return {
                "code": mode_result.get("code", 1005),
                "execution_state": mode_result.get("execution_state", "failed"),
                "mode_result": mode_result,
                "light_result": None,
            }
        light_result = self._control_device_resilient("light_1", "turn_on")
        succeeded = self._execution_succeeded(light_result)
        return {
            "code": 0 if succeeded else light_result.get("code", 1005),
            "execution_state": "succeeded" if succeeded else "failed",
            "mode_result": mode_result,
            "light_result": light_result,
        }

    def _restore_device_state(self, device_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
        if device_id.startswith("light_"):
            if state.get("power") != "on":
                return self._control_device_resilient(device_id, "turn_off")
            rgb = state.get("rgb")
            if isinstance(rgb, list) and len(rgb) == 3:
                return self._control_device_resilient(
                    device_id,
                    "set_color",
                    params={
                        "r": int(rgb[0]),
                        "g": int(rgb[1]),
                        "b": int(rgb[2]),
                        "brightness": int(state.get("brightness", 255)),
                    },
                )
            return self._control_device_resilient(device_id, "turn_on")
        if device_id.startswith("fan_"):
            action = "turn_on" if state.get("power") == "on" else "turn_off"
            return self._control_device_resilient(device_id, action)
        if device_id == "alarm":
            mode_name = state.get("mode_name")
            if state.get("power") != "on" or mode_name == "off":
                return self._control_device_resilient(device_id, "turn_off")
            if mode_name:
                return self._control_device_resilient(
                    device_id, "set_mode", params={"mode": mode_name}
                )
            return self._control_device_resilient(device_id, "turn_on")
        return {"code": 1002, "execution_state": "failed"}

    def _undo_last_control(self) -> Dict[str, Any]:
        undo = self._last_undo
        if not undo:
            return {
                "code": 1002,
                "execution_state": "failed",
                "message": "目前没有可以撤销的家居操作。",
            }
        succeeded = 0
        targets = 0
        if undo.get("type") == "devices":
            for device_id, state in undo.get("states", {}).items():
                targets += 1
                if self._execution_succeeded(self._restore_device_state(device_id, state)):
                    succeeded += 1
        elif undo.get("type") == "mode":
            targets = 1
            scene_id = {
                "normal": "normal_mode",
                "home": "home_mode",
                "away": "away_mode",
            }.get(undo.get("mode"))
            if scene_id:
                result = self._control_scene_resilient(scene_id)
                if self._execution_succeeded(result):
                    self._save_desired_mode(scene_id)
                    succeeded = 1
        elif undo.get("type") == "home_arrival":
            scene_id = {
                "normal": "normal_mode",
                "home": "home_mode",
                "away": "away_mode",
            }.get(undo.get("mode"))
            if scene_id:
                targets += 1
                result = self._control_scene_resilient(scene_id)
                if self._execution_succeeded(result):
                    self._save_desired_mode(scene_id)
                    succeeded += 1
            for device_id, state in undo.get("states", {}).items():
                targets += 1
                if self._execution_succeeded(self._restore_device_state(device_id, state)):
                    succeeded += 1
        if targets and succeeded == targets:
            self._last_undo = None
            return {
                "code": 0,
                "execution_state": "succeeded",
                "restored": succeeded,
                "message": "已经恢复到刚才的状态了。",
            }
        return {
            "code": 1005,
            "execution_state": "failed",
            "restored": succeeded,
            "targets": targets,
            "message": "只恢复了部分设备，请检查没有响应的模组。",
        }

    def _run_bedtime_mode(self) -> Dict[str, Any]:
        if self.dance.is_active:
            stopped = self.dance.stop(reason="bedtime")
            if not self._execution_succeeded(stopped):
                return {
                    "code": 1005,
                    "execution_state": "failed",
                    "message": "跳舞模式还没有安全停止，晚安模式暂未执行。",
                }
        undo = self._capture_device_states(
            ("light_1", "light_2", "light_3", "fan_1", "fan_2")
        )
        lights = self._control_scene_resilient("all_lights_off")
        fans = self._control_scene_resilient("all_fans_off")
        succeeded = self._execution_succeeded(lights) and self._execution_succeeded(fans)
        result = {
            "code": 0 if succeeded else 1005,
            "execution_state": "succeeded" if succeeded else "failed",
            "lights": lights,
            "fans": fans,
            "message": (
                "灯和风扇都关好了，晚安，早点休息。"
                if succeeded
                else "晚安模式只完成了一部分，请检查没有响应的设备。"
            ),
        }
        if succeeded:
            self._last_undo = undo
        return result

    def _collect_home_summary(self) -> Dict[str, Any]:
        health = self.client.health()
        mode = self._retry_busy(self.client.get_mode, "summary mode")
        sensor_id = self.config.sensors.get("temperature_humidity")
        sensor = self._get_device_state_resilient(sensor_id) if sensor_id else {}
        device_ids = []
        for room in self.config.rooms.values():
            for device_type in ("light", "fan"):
                device_id = room.get(device_type)
                if device_id and device_id not in device_ids:
                    device_ids.append(device_id)
        states = {
            device_id: self._get_device_state_resilient(device_id)
            for device_id in device_ids
        }
        sensor_state = sensor.get("state") or {}
        temperature = sensor_state.get("temperature_c")
        humidity = sensor_state.get("humidity_percent")
        mode_name = {
            "normal": "普通模式",
            "home": "居家模式",
            "away": "离家模式",
        }.get(mode.get("mode"), "未知模式")
        light_ids = [item for item in device_ids if item.startswith("light_")]
        fan_ids = [item for item in device_ids if item.startswith("fan_")]
        lights_on = sum(
            1 for item in light_ids if (states[item].get("state") or {}).get("power") == "on"
        )
        fans_on = sum(
            1 for item in fan_ids if (states[item].get("state") or {}).get("power") == "on"
        )
        parts = []
        if temperature is not None and humidity is not None:
            parts.append("屋里%.1f度，湿度%.1f%%" % (temperature, humidity))
        parts.append("当前是%s" % mode_name)
        parts.append("%d盏灯开着" % lights_on if lights_on else "灯都关着")
        parts.append("%d台风扇开着" % fans_on if fans_on else "风扇都关着")
        module_count = health.get("connected_module_count")
        if isinstance(module_count, int):
            parts.append("%d个家居模组在线" % module_count)
        parts.append("报警已触发" if mode.get("alarm_active") else "没有触发报警")
        return {
            "code": 0,
            "execution_state": "succeeded",
            "message": "，".join(parts) + "。",
            "health": health,
            "mode": mode,
            "states": states,
        }

    def health(self) -> Dict[str, Any]:
        return self.client.health()

    def close(self) -> None:
        self.dance.close()

    def reconcile_desired_mode(self) -> Dict[str, Any]:
        """Restore a saved user mode after Xiaohong SE has restarted."""
        desired_scene = self._load_desired_mode()
        if desired_scene not in self.MODE_SCENES:
            return {"status": "no_saved_mode"}
        current = self.client.get_mode()
        if current.get("code") != 0:
            return {"status": "mode_query_failed", "result": current}
        desired_mode = self.MODE_SCENES[desired_scene]
        if current.get("mode") == desired_mode:
            return {"status": "already_current", "mode": desired_mode}
        result = self.client.control_scene(desired_scene)
        if result.get("code") == 0 and result.get("execution_state") == "succeeded":
            logging.info("已恢复小鸿 SE 期望模式 scene_id=%s", desired_scene)
            return {"status": "restored", "mode": desired_mode, "result": result}
        return {"status": "restore_failed", "result": result}

    def _device_name(self, device_id: str) -> str:
        return self.config.device_names.get(device_id, "设备")

    def _control_reply(self, intent: ParsedIntent, result: Dict[str, Any]) -> str:
        state = result.get("execution_state")
        code = result.get("code")
        name = self._device_name(intent.device_id)
        if state == "succeeded" and code == 0:
            if intent.action == "turn_on":
                if "灯" in name:
                    return self._pick_reply(
                        result,
                        (
                            "好，%s打开了。" % name,
                            "收到，%s亮起来了。" % name,
                            "没问题，%s现在亮着。" % name,
                            "搞定，%s已经点亮了。" % name,
                        ),
                    )
                if "风扇" in name:
                    return self._pick_reply(
                        result,
                        (
                            "好，%s打开了。" % name,
                            "收到，%s转起来了。" % name,
                            "没问题，%s已经开始工作。" % name,
                            "搞定，%s现在开着。" % name,
                        ),
                    )
                return self._pick_reply(
                    result,
                    ("好，%s打开了。" % name, "收到，%s已经开启。" % name),
                )
            if intent.action == "turn_off":
                if "灯" in name:
                    return self._pick_reply(
                        result,
                        (
                            "好，%s关掉了。" % name,
                            "收到，%s已经熄灭。" % name,
                            "没问题，%s已经关了。" % name,
                            "搞定，%s现在关闭了。" % name,
                        ),
                    )
                if "风扇" in name:
                    return self._pick_reply(
                        result,
                        (
                            "好，%s关掉了。" % name,
                            "收到，%s已经停下来了。" % name,
                            "没问题，%s已经关闭。" % name,
                            "搞定，%s现在安静了。" % name,
                        ),
                    )
                return self._pick_reply(
                    result,
                    ("好，%s关掉了。" % name, "收到，%s已经关闭。" % name),
                )
            if intent.action == "set_color":
                return "好，%s颜色调好了。" % name
            if intent.action == "set_mode" and intent.device_id == "alarm":
                mode_name = (result.get("device_state") or {}).get("mode_name")
                mode_text = {
                    "off": "已关闭",
                    "light_on": "已设为灯光常亮",
                    "light_blink": "已设为只闪不响",
                    "sound_only": "已设为只响不闪",
                    "sound_light": "紧急声光报警已启动，请注意安全",
                    "short_beep": "已短鸣提示",
                }.get(mode_name, "模式已设置")
                if mode_name == "sound_light":
                    return mode_text + "。"
                return "%s%s。" % (name, mode_text)
            return "%s操作已完成。" % name
        if code == 1003:
            return "%s没在线，这次没有执行。" % name
        if state == "timeout" or code == 1005:
            return "%s没有响应，这次没有执行。" % name
        if code == 1008:
            return "控制请求较多，请稍后再试。"
        return "%s没有执行成功，可以再试一次。" % name

    @staticmethod
    def _pick_reply(result: Dict[str, Any], choices: tuple) -> str:
        """Vary short ACK replies without adding another model call or extra latency."""
        request_id = str(result.get("request_id") or "home")
        index = zlib.crc32(request_id.encode("utf-8")) % len(choices)
        return choices[index]

    def _scene_reply(self, intent: ParsedIntent, result: Dict[str, Any]) -> str:
        if intent.scene_id == "home_mode" and result.get("mode_result") is not None:
            mode_ok = self._execution_succeeded(result["mode_result"])
            light_ok = self._execution_succeeded(result.get("light_result") or {})
            if mode_ok:
                self._save_desired_mode(intent.scene_id)
            if mode_ok and light_ok:
                return "欢迎回家，已经进入居家模式，客厅灯也打开了。"
            if mode_ok:
                return "已经进入居家模式，但客厅灯没有响应。"
            return "居家模式切换失败，客厅灯没有执行。"
        if result.get("execution_state") == "succeeded" and result.get("code") == 0:
            if intent.scene_id in self.MODE_SCENES:
                self._save_desired_mode(intent.scene_id)
            return {
                "normal_mode": "好，已经恢复普通模式。",
                "home_mode": "好，已经进入居家模式。",
                "away_mode": "好，已经进入离家模式。",
                "bright_mode": "好，明亮模式打开了。",
                "ambient_mode": "好，氛围模式打开了。",
                "ventilation_mode": "好，通风模式打开了。",
                "all_lights_on": "好，所有灯都打开了。",
                "all_lights_off": "好，所有灯都关掉了。",
                "all_fans_on": "好，所有风扇都打开了。",
                "all_fans_off": "好，所有风扇都关掉了。",
            }.get(intent.scene_id, "好，场景执行完成。")
        scene_state = result.get("scene_state") or {}
        succeeded = scene_state.get("succeeded")
        targets = scene_state.get("targets")
        if isinstance(succeeded, int) and isinstance(targets, int) and targets > 0:
            return "场景只完成了%d个设备，共%d个，请检查离线设备。" % (succeeded, targets)
        if result.get("execution_state") == "timeout":
            return "场景中的部分设备暂时没有响应。"
        return "场景执行失败，请稍后重试。"

    @staticmethod
    def _dance_reply(intent: ParsedIntent, result: Dict[str, Any]) -> str:
        if result.get("reason") == "safety_locked":
            return "跳舞功能已经准备好，目前处于安全锁定状态。"
        if intent.action == "status":
            if result.get("execution_state") != "succeeded":
                return result.get("message") or "暂时无法查询跳舞模式。"
            if result.get("active"):
                remaining_ms = result.get("remaining_ms")
                if isinstance(remaining_ms, (int, float)) and remaining_ms >= 0:
                    return "现在正在跳舞，大约还剩%d秒。" % max(
                        1, round(remaining_ms / 1000)
                    )
                return "现在正在跳舞。"
            return "现在没有运行跳舞模式。"
        if result.get("execution_state") == "succeeded":
            if intent.action == "start":
                if result.get("reason") == "already_running":
                    return "跳舞模式已经在运行了。"
                return "好，跳舞模式开始。"
            return "好，跳舞模式已经停止。"
        if result.get("reason") == "lighting_start_failed":
            lighting = result.get("lighting") or {}
            scene_state = lighting.get("scene_state") or {}
            if scene_state.get("online_light_count") == 0:
                return "灯光模组暂时离线，跳舞模式没有启动。"
        return result.get("message") or "跳舞模式启动失败，请稍后再试。"

    def _state_reply(self, intent: ParsedIntent, result: Dict[str, Any]) -> str:
        name = self._device_name(intent.device_id)
        if not result.get("online") or not isinstance(result.get("state"), dict):
            return "%s当前离线或没有可用数据。" % name
        state = result["state"]
        if intent.device_id == self.config.sensors.get("temperature_humidity"):
            temperature = state.get("temperature_c")
            humidity = state.get("humidity_percent")
            if temperature is not None and humidity is not None:
                return "屋里现在%.1f度，湿度%.1f%%。" % (temperature, humidity)
        if intent.device_id == self.config.sensors.get("illuminance"):
            illuminance = state.get("illuminance_lux", state.get("lux"))
            if illuminance is not None:
                return "现在室内光照度是%s勒克斯。" % illuminance
        power = state.get("power")
        if power == "on":
            return "%s当前已打开。" % name
        if power == "off":
            return "%s当前已关闭。" % name
        return "%s状态已获取。" % name

    def _mode_reply(self, result: Dict[str, Any]) -> str:
        if result.get("code") != 0:
            return "暂时无法查询当前模式。"
        mode_text = {
            "normal": "普通模式",
            "home": "居家模式",
            "away": "离家模式",
        }.get(result.get("mode"))
        if not mode_text:
            return "当前模式状态异常。"
        parts = ["当前处于%s" % mode_text]
        if result.get("alarm_active"):
            reasons = self._alarm_reasons(result.get("cause_mask", 0))
            parts.append("报警已触发" + ("，原因是" + "、".join(reasons) if reasons else ""))
        elif result.get("fan_auto_inhibit"):
            parts.append("高温自动开风扇目前已被手动关闭抑制")
        else:
            parts.append("自动化运行正常")
        return "，".join(parts) + "。"

    @staticmethod
    def _alarm_reasons(cause_mask: Any) -> list:
        try:
            value = int(cause_mask)
        except (TypeError, ValueError):
            return []
        definitions = (
            (0x01, "温度过高"),
            (0x02, "湿度过高"),
            (0x04, "烟雾报警"),
            (0x08, "检测到人体存在"),
            (0x10, "自动动作执行失败"),
            (0x20, "温度上升过快"),
            (0x40, "湿度上升过快"),
        )
        return [text for bit, text in definitions if value & bit]

    def _save_desired_mode(self, scene_id: str) -> None:
        path = Path(self.config.mode_state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps({"scene_id": scene_id}, ensure_ascii=False),
            encoding="utf-8",
        )
        temp_path.replace(path)

    def _load_desired_mode(self) -> str:
        path = Path(self.config.mode_state_file)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return ""
        return str(data.get("scene_id", ""))
