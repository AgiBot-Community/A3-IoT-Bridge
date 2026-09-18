import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from a3_bridge.bridge import HomeBridge
from a3_bridge.config import DanceConfig, DiscoveryConfig, load_config
from a3_bridge.dance_controller import DanceController
from a3_bridge.discovery import BroadcastTarget, discover_gateway, enumerate_directed_broadcasts
from a3_bridge.intent_parser import IntentParser
from a3_bridge.mock_server import make_server
from a3_bridge.xiaohong_client import XiaohongSEClient


ROOT = Path(__file__).resolve().parents[1]


def make_config(base_url: str):
    raw = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    raw["gateway"]["base_url"] = base_url
    raw["gateway"]["bearer_token"] = "dev-token"
    raw["gateway"]["discovery"]["enabled"] = False
    temp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    raw["mode_state_file"] = temp.name + ".mode-state"
    json.dump(raw, temp, ensure_ascii=False)
    temp.close()
    return load_config(temp.name)


class DiscoveryResponder:
    def __init__(self, http_port: int):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.http_port = http_port
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.sock.close()
        self.thread.join(timeout=2)

    def _serve(self):
        try:
            data, peer = self.sock.recvfrom(2048)
            request = json.loads(data.decode("utf-8"))
            response = {
                "type": "xhse_announce",
                "version": 1,
                "request_id": request["request_id"],
                "service": "xiaohong-se-home-gateway",
                "device_id": "xiaohong-se-01",
                "hostname": "xiaohong-se",
                "ip": "192.0.2.123",
                "http_port": self.http_port,
            }
            self.sock.sendto(json.dumps(response).encode("utf-8"), peer)
        except OSError:
            pass


class IntentParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = make_config("http://127.0.0.1:18080")
        cls.parser = IntentParser(cls.config)

    def test_explicit_room_fan(self):
        intent = self.parser.parse("帮我打开主卧风扇")
        self.assertEqual((intent.kind, intent.device_id, intent.action), ("device_control", "fan_2", "turn_on"))

    def test_conservative_asr_corrections(self):
        intent = self.parser.parse("关比全部登光")
        self.assertEqual((intent.kind, intent.scene_id), ("scene_control", "all_lights_off"))
        intent = self.parser.parse("打开痛风模式")
        self.assertEqual((intent.kind, intent.scene_id), ("scene_control", "ventilation_mode"))
        intent = self.parser.parse("屋里温室度多少")
        self.assertEqual((intent.kind, intent.device_id), ("state_query", "temperature_humidity"))

    def test_natural_heat_asks_for_room(self):
        intent = self.parser.parse("我有点热")
        self.assertEqual(intent.kind, "clarify")
        self.assertEqual(intent.params["clarification"], "fan_room")
        self.assertEqual(intent.params["action"], "turn_on")

    def test_natural_heat_with_room_executes_directly(self):
        intent = self.parser.parse("主卧有点热")
        self.assertEqual((intent.device_id, intent.action), ("fan_2", "turn_on"))

    def test_natural_cold_stops_default_room_fan(self):
        intent = self.parser.parse("主卧有点冷，别吹了")
        self.assertEqual((intent.device_id, intent.action), ("fan_2", "turn_off"))

    def test_bare_fan_name_requires_action(self):
        intent = self.parser.parse("客厅风扇")
        self.assertEqual(intent.kind, "clarify")
        self.assertIn("打开还是关闭", intent.message)

    def test_dark_room_turns_on_light(self):
        intent = self.parser.parse("主卧有点暗")
        self.assertEqual((intent.device_id, intent.action), ("light_2", "turn_on"))

    def test_all_lights_scene(self):
        intent = self.parser.parse("把灯都关掉")
        self.assertEqual((intent.kind, intent.scene_id), ("scene_control", "all_lights_off"))

    def test_color(self):
        intent = self.parser.parse("把次卧灯调成蓝色")
        self.assertEqual(intent.action, "set_color")
        self.assertEqual(intent.params["b"], 255)

    def test_sensor_query(self):
        intent = self.parser.parse("现在温度和湿度是多少")
        self.assertEqual((intent.kind, intent.device_id), ("state_query", "temperature_humidity"))

    def test_mode_scenes_and_query(self):
        self.assertEqual(self.parser.parse("我回来了").scene_id, "home_mode")
        self.assertEqual(self.parser.parse("我回家了").scene_id, "home_mode")
        self.assertEqual(self.parser.parse("回家了").scene_id, "home_mode")
        self.assertEqual(self.parser.parse("我到家了").scene_id, "home_mode")
        self.assertEqual(self.parser.parse("我要出门了").scene_id, "away_mode")
        self.assertEqual(self.parser.parse("退出自动模式").scene_id, "normal_mode")
        self.assertEqual(self.parser.parse("现在是什么模式").kind, "mode_query")

    def test_scene_exit_phrases_do_not_reopen_scene(self):
        self.assertEqual(self.parser.parse("关闭通风模式").scene_id, "all_fans_off")
        self.assertEqual(self.parser.parse("退出离家模式").scene_id, "normal_mode")
        self.assertEqual(self.parser.parse("关闭氛围模式").scene_id, "all_lights_off")
        incomplete = self.parser.parse("模式")
        self.assertEqual(incomplete.kind, "clarify")
        self.assertIn("居家模式", incomplete.message)

    def test_alarm_submodes_and_fan_speed_limit(self):
        alarm = self.parser.parse("报警器只响不闪")
        self.assertEqual((alarm.action, alarm.params["mode"]), ("set_mode", "sound_only"))
        alarm_off = self.parser.parse("关闭声光报警器")
        self.assertEqual(alarm_off.action, "turn_off")
        fan = self.parser.parse("把客厅风扇调到高档")
        self.assertEqual(fan.kind, "clarify")
        self.assertIn("不能调节风速", fan.message)

    def test_natural_emergency_alarm_and_cancel(self):
        emergency = self.parser.parse("屋内有紧急情况，立即报警")
        self.assertEqual(
            (emergency.kind, emergency.device_id, emergency.action),
            ("device_control", "alarm", "set_mode"),
        )
        self.assertEqual(emergency.params["mode"], "sound_light")
        cancelled = self.parser.parse("解除警报")
        self.assertEqual((cancelled.device_id, cancelled.action), ("alarm", "turn_off"))

    def test_dance_start_stop_and_status_intents(self):
        start = self.parser.parse("来一段舞蹈")
        self.assertEqual((start.kind, start.action), ("dance_control", "start"))
        stop = self.parser.parse("结束派对模式")
        self.assertEqual((stop.kind, stop.action), ("dance_control", "stop"))
        status = self.parser.parse("现在还在跳舞吗")
        self.assertEqual((status.kind, status.action), ("dance_control", "status"))

    def test_housekeeper_summary_bedtime_and_undo_intents(self):
        self.assertEqual(self.parser.parse("家里现在怎么样").kind, "home_summary")
        self.assertEqual(self.parser.parse("我要睡觉了").kind, "bedtime_control")
        self.assertEqual(self.parser.parse("撤销刚才的操作").kind, "undo")


class ClientAndBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0, "dev-token")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.config = make_config("http://127.0.0.1:%d" % cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_health(self):
        result = XiaohongSEClient(self.config.gateway).health()
        self.assertEqual(result["status"], "ready")

    def test_busy_response_is_retried(self):
        bridge = HomeBridge(self.config)
        responses = [
            {"code": 1008, "_http_status": 429, "execution_state": "failed"},
            {"code": 0, "execution_state": "succeeded"},
        ]
        calls = []

        def operation():
            calls.append(True)
            return responses.pop(0)

        with patch("a3_bridge.bridge.time.sleep") as sleep:
            result = bridge._retry_busy(operation, "test")
        self.assertEqual(result["code"], 0)
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(0.35)

    def test_control_and_state(self):
        client = XiaohongSEClient(self.config.gateway)
        result = client.control_device("light_1", "turn_on", request_id="test-light-1")
        self.assertEqual(result["execution_state"], "succeeded")
        state = client.get_device_state("light_1")
        self.assertEqual(state["state"]["power"], "on")

    def test_idempotent_retry(self):
        client = XiaohongSEClient(self.config.gateway)
        first = client.control_device("fan_1", "turn_on", request_id="same-request")
        second = client.control_device("fan_1", "turn_on", request_id="same-request")
        self.assertEqual(first["device_state"], second["device_state"])

    def test_bridge_natural_language(self):
        bridge = HomeBridge(self.config)
        reply = bridge.handle_text("客厅有点热")
        self.assertEqual(reply.raw_result["execution_state"], "succeeded")
        self.assertIn("客厅风扇", reply.text)

    def test_bridge_temperature_reply(self):
        bridge = HomeBridge(self.config)
        reply = bridge.handle_text("现在多少度")
        self.assertIn("27.5度", reply.text)

    def test_mode_control_query_and_alarm_mode(self):
        bridge = HomeBridge(self.config)
        changed = bridge.handle_text("我要出门了")
        self.assertEqual(changed.raw_result["execution_state"], "succeeded")
        self.assertEqual(changed.text, "好，已经进入离家模式。")
        queried = bridge.handle_text("现在是什么模式")
        self.assertIn("离家模式", queried.text)
        alarm = bridge.handle_text("报警器只响不闪")
        self.assertEqual(alarm.raw_result["device_state"]["mode_name"], "sound_only")
        self.assertIn("只响不闪", alarm.text)
        emergency = bridge.handle_text("屋内有紧急情况报警")
        self.assertEqual(emergency.raw_result["device_state"]["mode_name"], "sound_light")
        self.assertIn("请注意安全", emergency.text)

    def test_home_arrival_enters_mode_and_turns_on_living_room_light(self):
        bridge = HomeBridge(self.config)
        bridge.handle_text("我要出门了")
        arrived = bridge.handle_text("我回家了")
        self.assertEqual(arrived.raw_result["execution_state"], "succeeded")
        self.assertEqual(arrived.raw_result["mode_result"]["execution_state"], "succeeded")
        self.assertEqual(arrived.raw_result["light_result"]["execution_state"], "succeeded")
        self.assertIn("客厅灯也打开了", arrived.text)
        self.assertEqual(bridge.client.get_mode()["mode"], "home")
        self.assertEqual(
            bridge.client.get_device_state("light_1")["state"]["power"], "on"
        )

    def test_mode_cause_mask_reply(self):
        bridge = HomeBridge(self.config)
        reply = bridge._mode_reply({
            "code": 0,
            "mode": "away",
            "alarm_active": True,
            "cause_mask": 0x21,
        })
        self.assertIn("温度过高", reply)
        self.assertIn("温度上升过快", reply)

    def test_home_summary(self):
        bridge = HomeBridge(self.config)
        reply = bridge.handle_text("家里现在怎么样")
        self.assertEqual(reply.raw_result["code"], 0)
        self.assertIn("27.5度", reply.text)
        self.assertIn("8个家居模组在线", reply.text)

    def test_bedtime_mode_and_undo(self):
        bridge = HomeBridge(self.config)
        bridge.handle_text("打开客厅灯")
        bridge.handle_text("打开主卧风扇")
        bedtime = bridge.handle_text("我要睡觉了")
        self.assertEqual(bedtime.raw_result["execution_state"], "succeeded")
        self.assertEqual(
            bridge.client.get_device_state("light_1")["state"]["power"], "off"
        )
        self.assertEqual(
            bridge.client.get_device_state("fan_2")["state"]["power"], "off"
        )
        undone = bridge.handle_text("撤销刚才的操作")
        self.assertEqual(undone.raw_result["execution_state"], "succeeded")
        self.assertEqual(
            bridge.client.get_device_state("light_1")["state"]["power"], "on"
        )
        self.assertEqual(
            bridge.client.get_device_state("fan_2")["state"]["power"], "on"
        )

    def test_undo_single_device_control(self):
        bridge = HomeBridge(self.config)
        bridge.handle_text("关闭客厅灯")
        bridge.handle_text("打开客厅灯")
        undone = bridge.handle_text("恢复刚才状态")
        self.assertEqual(undone.raw_result["execution_state"], "succeeded")
        state = bridge.client.get_device_state("light_1")
        self.assertEqual(state["state"]["power"], "off")

    def test_dance_is_safely_locked_by_default(self):
        bridge = HomeBridge(self.config)
        reply = bridge.handle_text("开始跳舞")
        self.assertEqual(reply.raw_result["reason"], "safety_locked")
        self.assertIn("安全锁定", reply.text)

    def test_dance_lighting_client_methods(self):
        client = XiaohongSEClient(self.config.gateway)
        started = client.control_scene(
            "dance_lighting",
            request_id="test-dance-start",
            params={"duration_sec": 18, "interval_ms": 600},
        )
        self.assertEqual(started["scene_state"]["online_light_count"], 3)
        status = client.get_dance_lighting_status()
        self.assertTrue(status["active"])
        stopped = client.control_scene(
            "dance_lighting_stop",
            request_id="test-dance-stop",
        )
        self.assertEqual(stopped["execution_state"], "succeeded")
        self.assertFalse(client.get_dance_lighting_status()["active"])

    def test_udp_discovery_uses_packet_source_ip(self):
        responder = DiscoveryResponder(self.server.server_port)
        responder.start()
        try:
            discovery = DiscoveryConfig(
                enabled=True,
                auto_directed_broadcasts=False,
                broadcast_address="127.0.0.1",
                port=responder.port,
                timeout_seconds=1.0,
                attempts=1,
                interval_seconds=0.5,
            )
            result = discover_gateway(discovery)
            self.assertEqual(result.host, "127.0.0.1")
            self.assertEqual(result.port, self.server.server_port)
        finally:
            responder.close()

    def test_client_discovers_and_verifies_health(self):
        responder = DiscoveryResponder(self.server.server_port)
        responder.start()
        raw = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        raw["gateway"]["bearer_token"] = "dev-token"
        raw["gateway"]["discovery"].update(
            {
                "broadcast_address": "127.0.0.1",
                "auto_directed_broadcasts": False,
                "port": responder.port,
                "timeout_seconds": 1.0,
                "attempts": 1,
            }
        )
        temp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(raw, temp, ensure_ascii=False)
        temp.close()
        try:
            client = XiaohongSEClient(load_config(temp.name).gateway)
            result = client.health()
            self.assertEqual(result["status"], "ready")
            self.assertEqual(client.current_base_url, "http://127.0.0.1:%d" % self.server.server_port)
        finally:
            responder.close()

    def test_enumerates_linux_directed_broadcasts_and_ignores_virtual_interfaces(self):
        addresses = [
            {
                "ifname": "wifi_hdu",
                "flags": ["BROADCAST", "UP"],
                "addr_info": [
                    {
                        "family": "inet",
                        "local": "192.0.2.251",
                        "prefixlen": 24,
                        "broadcast": "192.0.2.255",
                        "scope": "global",
                    }
                ],
            },
            {
                "ifname": "docker0",
                "flags": ["BROADCAST", "UP"],
                "addr_info": [
                    {
                        "family": "inet",
                        "local": "172.17.0.1",
                        "prefixlen": 16,
                        "broadcast": "172.17.255.255",
                        "scope": "global",
                    }
                ],
            },
            {
                "ifname": "rmnet_mhi0.1",
                "flags": ["POINTOPOINT", "UP"],
                "addr_info": [
                    {
                        "family": "inet",
                        "local": "10.33.90.214",
                        "prefixlen": 30,
                        "scope": "global",
                    }
                ],
            },
        ]
        addresses_completed = type("Completed", (), {"stdout": json.dumps(addresses)})()
        routes_completed = type(
            "Completed",
            (),
            {"stdout": json.dumps([{"dst": "default", "dev": "wifi_hdu", "prefsrc": "192.0.2.251"}])},
        )()
        with patch(
            "a3_bridge.discovery.subprocess.run",
            side_effect=[addresses_completed, routes_completed],
        ):
            self.assertEqual(
                enumerate_directed_broadcasts(),
                [BroadcastTarget("wifi_hdu", "192.0.2.251", "192.0.2.255", 24, True)],
            )


if __name__ == "__main__":
    unittest.main()
