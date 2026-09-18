import re
from typing import Dict, Optional, Tuple

from .asr_correction import DomainTextCorrector
from .config import AppConfig
from .models import ParsedIntent


class IntentParser:
    """将常见中文自然语言转换为小鸿 SE 的封闭式命令。"""

    ON_WORDS = ("打开", "开启", "启动", "开一下", "开开")
    OFF_WORDS = ("关闭", "关掉", "关上", "关一下", "停止")
    STATE_WORDS = ("状态", "开着吗", "打开了吗", "关了吗", "有没有开")

    COLORS: Dict[str, Tuple[int, int, int]] = {
        "白色": (255, 255, 255),
        "白光": (255, 255, 255),
        "红色": (255, 0, 0),
        "红光": (255, 0, 0),
        "绿色": (0, 255, 0),
        "绿光": (0, 255, 0),
        "蓝色": (0, 0, 255),
        "蓝光": (0, 0, 255),
        "暖色": (255, 180, 80),
        "暖光": (255, 180, 80),
        "黄色": (255, 200, 0),
        "黄光": (255, 200, 0),
    }

    def __init__(self, config: AppConfig):
        self.config = config
        self.corrector = DomainTextCorrector()

    def correct(self, text: str) -> str:
        return self.corrector.correct(text)

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"[\s，。！？、,.!?]", "", text.strip().lower())

    def _find_room(self, text: str) -> Optional[str]:
        explicit = self._find_explicit_room(text)
        return explicit or self.config.default_room or None

    def _find_explicit_room(self, text: str) -> Optional[str]:
        for room_id, room in self.config.rooms.items():
            for alias in room.get("aliases", []):
                if self._normalize(alias) in text:
                    return room_id
        return None

    def _room_device(self, room_id: Optional[str], device_type: str) -> Optional[str]:
        if not room_id:
            return None
        return self.config.rooms.get(room_id, {}).get(device_type)

    def _match_scene(self, text: str) -> Optional[str]:
        for scene_id, phrases in self.config.scene_phrases.items():
            if any(self._normalize(phrase) in text for phrase in phrases):
                return scene_id

        all_words = ("所有", "全部", "都", "全屋")
        if any(word in text for word in all_words):
            if "灯" in text:
                if any(word in text for word in self.OFF_WORDS):
                    return "all_lights_off"
                if any(word in text for word in self.ON_WORDS):
                    return "all_lights_on"
            if "风扇" in text:
                if any(word in text for word in self.OFF_WORDS):
                    return "all_fans_off"
                if any(word in text for word in self.ON_WORDS):
                    return "all_fans_on"
        return None

    def parse(self, utterance: str) -> ParsedIntent:
        corrected = self.correct(utterance)
        text = self._normalize(corrected)
        if not text:
            return ParsedIntent("unsupported", utterance, message="我没有听清，请再说一次。")

        if any(
            phrase in text
            for phrase in (
                "撤销刚才的操作",
                "撤销刚才操作",
                "撤销上一步",
                "恢复刚才的状态",
                "恢复刚才状态",
                "回到刚才的状态",
                "回到刚才状态",
            )
        ):
            return ParsedIntent("undo", utterance)

        if any(
            phrase in text
            for phrase in (
                "我要睡觉了",
                "准备睡觉了",
                "该睡觉了",
                "开启晚安模式",
                "进入晚安模式",
                "晚安模式",
            )
        ):
            return ParsedIntent("bedtime_control", utterance, action="start")

        if any(
            phrase in text
            for phrase in (
                "家里现在怎么样",
                "屋里现在怎么样",
                "家里现在什么情况",
                "家里情况怎么样",
                "全屋状态",
                "家里安全吗",
                "家里都好吗",
            )
        ):
            return ParsedIntent("home_summary", utterance)

        dance_words = ("跳舞", "舞蹈", "跳舞灯光", "派对模式")
        if any(word in text for word in dance_words):
            if any(word in text for word in ("状态", "还在", "结束了吗", "停止了吗")):
                return ParsedIntent("dance_control", utterance, action="status")
            if any(word in text for word in self.OFF_WORDS) or "结束" in text or "退出" in text:
                return ParsedIntent("dance_control", utterance, action="stop")
            if any(word in text for word in self.ON_WORDS + ("开始",)) or any(
                phrase in text for phrase in ("来一段", "来段", "跳一个", "跳一段", "派对模式")
            ):
                return ParsedIntent("dance_control", utterance, action="start")

        # 退出类表达必须优先于场景词匹配，否则“关闭通风模式”会因为包含
        # “通风模式”而被错误地当成开启通风。
        if any(word in text for word in self.OFF_WORDS) or "退出" in text:
            if any(word in text for word in ("离家模式", "居家模式", "自动模式")):
                return ParsedIntent("scene_control", utterance, scene_id="normal_mode")
            if "通风模式" in text:
                return ParsedIntent("scene_control", utterance, scene_id="all_fans_off")
            if any(word in text for word in ("明亮模式", "氛围模式")):
                return ParsedIntent("scene_control", utterance, scene_id="all_lights_off")

        if any(
            phrase in text
            for phrase in (
                "现在是什么模式",
                "当前是什么模式",
                "现在处于什么模式",
                "当前处于什么模式",
                "查询当前模式",
                "自动模式状态",
                "安防状态",
            )
        ):
            return ParsedIntent("mode_query", utterance)

        scene_id = self._match_scene(text)
        if scene_id:
            return ParsedIntent("scene_control", utterance, scene_id=scene_id)

        if "模式" in text:
            return ParsedIntent(
                "clarify",
                utterance,
                message="请说进入居家模式、离家模式，或者恢复普通模式。",
            )

        if any(word in text for word in ("温度", "湿度", "多少度", "冷热")):
            device_id = self.config.sensors.get("temperature_humidity")
            return ParsedIntent("state_query", utterance, device_id=device_id)

        if any(word in text for word in ("光照", "照度")) or "现在亮不亮" in text:
            device_id = self.config.sensors.get("illuminance")
            return ParsedIntent("state_query", utterance, device_id=device_id)

        explicit_room_id = self._find_explicit_room(text)
        room_id = explicit_room_id or self.config.default_room or None

        if "报警" in text or "警报" in text:
            if any(
                phrase in text
                for phrase in (
                    "紧急情况报警",
                    "紧急报警",
                    "立即报警",
                    "马上报警",
                    "快报警",
                    "发出警报",
                    "拉响警报",
                    "启动报警",
                    "启动警报",
                    "开启报警",
                    "开启警报",
                    "有危险报警",
                    "发生危险报警",
                )
            ):
                return ParsedIntent(
                    "device_control",
                    utterance,
                    device_id="alarm",
                    action="set_mode",
                    params={"mode": "sound_light"},
                )
            if any(phrase in text for phrase in ("解除警报", "解除报警", "取消警报", "取消报警")):
                return ParsedIntent(
                    "device_control", utterance, device_id="alarm", action="turn_off"
                )
            action = self._find_action(text)
            if action == "turn_off":
                return ParsedIntent(
                    "device_control", utterance, device_id="alarm", action="turn_off"
                )
            alarm_mode = self._find_alarm_mode(text)
            if alarm_mode:
                return ParsedIntent(
                    "device_control",
                    utterance,
                    device_id="alarm",
                    action="set_mode",
                    params={"mode": alarm_mode},
                )
            if not action:
                return ParsedIntent("clarify", utterance, message="需要打开还是关闭声光报警器？")
            return ParsedIntent("device_control", utterance, device_id="alarm", action=action)

        fan_natural_words = (
            "有点热",
            "好热",
            "太热",
            "有点闷",
            "太闷",
            "透透气",
            "吹点风",
            "有点冷",
            "好冷",
            "太冷",
            "不用吹",
            "别吹",
            "风太大",
        )
        if "风扇" in text or any(word in text for word in fan_natural_words):
            if any(
                word in text
                for word in ("低档", "中档", "高档", "一档", "二档", "三档", "风速", "调速")
            ):
                return ParsedIntent(
                    "clarify",
                    utterance,
                    message="当前风扇只支持打开和关闭，不能调节风速。",
                )
            natural_off = any(
                word in text
                for word in ("有点冷", "好冷", "太冷", "不用吹", "别吹", "风太大")
            )
            natural_on = any(
                word in text
                for word in ("有点热", "好热", "太热", "有点闷", "太闷", "透透气", "吹点风")
            )
            if natural_off:
                action = "turn_off"
            elif natural_on:
                action = "turn_on"
            else:
                action = self._find_action(text)
            if any(word in text for word in self.STATE_WORDS):
                if not explicit_room_id:
                    return ParsedIntent(
                        "clarify",
                        utterance,
                        params={
                            "clarification": "fan_room",
                            "action": "query",
                        },
                        message="你想查询客厅还是主卧的风扇？",
                    )
                device_id = self._room_device(explicit_room_id, "fan")
                return ParsedIntent("state_query", utterance, device_id=device_id)
            if action and not explicit_room_id:
                action_text = "打开" if action == "turn_on" else "关闭"
                return ParsedIntent(
                    "clarify",
                    utterance,
                    params={
                        "clarification": "fan_room",
                        "action": action,
                    },
                    message="你现在在客厅还是主卧？我帮你%s风扇。" % action_text,
                )
            device_id = self._room_device(explicit_room_id, "fan")
            if not device_id:
                return ParsedIntent(
                    "clarify",
                    utterance,
                    message="你想打开还是关闭哪个房间的风扇？",
                )
            if not action:
                return ParsedIntent(
                    "clarify",
                    utterance,
                    message="你想打开还是关闭%s？" % self.config.device_names.get(
                        device_id, "风扇"
                    ),
                )
            return ParsedIntent("device_control", utterance, device_id=device_id, action=action)

        if "灯" in text or any(word in text for word in ("有点暗", "太暗", "看不清")):
            device_id = self._room_device(room_id, "light")
            if not device_id:
                return ParsedIntent("clarify", utterance, message="需要控制哪个房间的灯？")
            if any(word in text for word in self.STATE_WORDS):
                return ParsedIntent("state_query", utterance, device_id=device_id)
            color = self._find_color(text)
            if color:
                r, g, b = color
                return ParsedIntent(
                    "device_control",
                    utterance,
                    device_id=device_id,
                    action="set_color",
                    params={"r": r, "g": g, "b": b, "brightness": 255},
                )
            action = self._find_action(text)
            if not action and any(word in text for word in ("有点暗", "太暗", "看不清")):
                action = "turn_on"
            if not action:
                return ParsedIntent("clarify", utterance, message="需要打开还是关闭这盏灯？")
            return ParsedIntent("device_control", utterance, device_id=device_id, action=action)

        return ParsedIntent(
            "unsupported",
            utterance,
            message="这个指令暂时不在智能家居控制范围内。",
        )

    def _find_action(self, text: str) -> Optional[str]:
        if any(word in text for word in self.OFF_WORDS):
            return "turn_off"
        if any(word in text for word in self.ON_WORDS):
            return "turn_on"
        return None

    def _find_color(self, text: str) -> Optional[Tuple[int, int, int]]:
        for name, rgb in self.COLORS.items():
            if name in text:
                return rgb
        return None

    @staticmethod
    def _find_alarm_mode(text: str) -> Optional[str]:
        if any(word in text for word in ("短鸣", "响一声", "提示音")):
            return "short_beep"
        if any(word in text for word in ("只响不闪", "仅声音", "只响", "只鸣", "蜂鸣")):
            return "sound_only"
        if any(word in text for word in ("只闪不响", "仅闪烁", "只闪", "闪烁")):
            return "light_blink"
        if any(word in text for word in ("灯常亮", "常亮")):
            return "light_on"
        if any(word in text for word in ("声光同时", "又响又闪", "声光报警")):
            return "sound_light"
        return None
