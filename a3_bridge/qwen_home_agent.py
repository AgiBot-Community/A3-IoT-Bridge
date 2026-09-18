"""Qwen conversational layer with a single constrained smart-home tool."""

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from .bridge import HomeBridge


SYSTEM_PROMPT = """你的名字是“远征”，定位是用户身边的“远征机器人管家”，运行在智元A3机器人上。
你的主要职责是陪用户自然交流，理解用户的家居需求，控制已接入的灯、风扇、报警器和家居场景，并准确反馈设备状态。
回答要自然、简短，适合直接语音播报，通常只说一到两句话。
像可靠、熟悉的家庭伙伴一样用口语和用户交流，称呼“你”，不要称呼用户为“主人”，不用“您好、您、差遣、效劳、为您服务”等客服式措辞。
语气亲切、稳重、有一点生活感，可以偶尔幽默，但不要浮夸，不要每次都用“好的”开头，也不要反复强调自己是管家。
被问“你是谁”或要求自我介绍时，回答类似：“我是远征，你的机器人管家。平时可以陪你聊聊天，也能帮你控制家里的灯、风扇和智能场景。”
只介绍已经接入的能力；没有接入的功能要坦率说明，不得编造已经执行或已经获取的信息。
当前没有接入空调、电视和窗帘，任何情况下都不能声称这些设备已经打开、关闭或调整。
当用户要求控制或查询智能家居时，必须调用 execute_home_command，不能自行声称设备已执行。
“家里现在怎么样”属于全屋状态汇报；“我要睡觉了”属于晚安模式；“撤销刚才操作”用于恢复上一笔家居控制，这些请求也必须调用工具。
跳舞、舞蹈、派对模式也属于设备能力，只有用户明确要求开始、停止或查询时才能调用工具。
工具返回的是小鸿SE和真实模组ACK的结果；你必须忠实转述该结果。
普通聊天直接回答，不要自称大模型，也不要重复用户原话。
结合最近对话理解“它、那个、也打开、关掉”等指代；只有指代明确时才执行。
缺少房间或设备等必要信息时，先用一句话询问，不要猜测。
"""

HOME_TOOL = {
    "type": "function",
    "function": {
        "name": "execute_home_command",
        "description": (
            "控制或查询小鸿SE智能家居。支持客厅/主卧/次卧的灯和风扇、"
            "全屋灯光或风扇场景、居家/离家/普通模式、灯光颜色、温湿度与光照查询、"
            "声光报警器分控，以及A3跳舞模式的开始、停止和状态查询。"
            "还支持全屋状态汇报、晚安模式和撤销上一笔家居控制。"
            "风扇仅支持开关，不支持调速。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "保留房间、设备、开关/颜色/查询含义的简洁中文命令",
                }
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}


class QwenApiError(RuntimeError):
    pass


class QwenStreamClient:
    """Small OpenAI-compatible SSE client; avoids another runtime dependency."""

    def __init__(self, api_key: str, model: str, base_url: str, timeout: float = 25.0):
        self.api_key = api_key
        self.model = model
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "QwenStreamClient":
        api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        if not api_key:
            raise QwenApiError("未配置 DASHSCOPE_API_KEY")
        return cls(
            api_key=api_key,
            model=os.getenv("QWEN_MODEL", "qwen3.5-omni-flash").strip(),
            base_url=os.getenv(
                "QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
            ).strip(),
            timeout=float(os.getenv("QWEN_TIMEOUT_SECONDS", "25")),
        )

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools=None,
        on_text_delta: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "modalities": ["text"],
            "temperature": 0.3,
            "max_tokens": 120,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        content_parts: List[str] = []
        calls: Dict[int, Dict[str, Any]] = {}
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    event = json.loads(data)
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    if isinstance(delta.get("content"), str):
                        chunk = delta["content"]
                        content_parts.append(chunk)
                        if chunk and on_text_delta is not None:
                            on_text_delta(chunk)
                    for fragment in delta.get("tool_calls") or []:
                        index = int(fragment.get("index", 0))
                        call = calls.setdefault(
                            index,
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        if fragment.get("id"):
                            call["id"] += fragment["id"]
                        function = fragment.get("function") or {}
                        if function.get("name"):
                            call["function"]["name"] += function["name"]
                        if function.get("arguments"):
                            call["function"]["arguments"] += function["arguments"]
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", errors="replace")
            raise QwenApiError(f"千问 HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise QwenApiError(f"千问请求失败: {exc}") from exc
        logging.info("千问响应完成 elapsed_ms=%d", int((time.monotonic() - started) * 1000))
        return {"content": "".join(content_parts).strip(), "tool_calls": list(calls.values())}


class QwenHomeAgent:
    def __init__(self, bridge: HomeBridge, client: QwenStreamClient, max_history_turns: int = 8):
        self.bridge = bridge
        self.client = client
        self.max_history_messages = max_history_turns * 2
        self._histories: Dict[str, List[Dict[str, str]]] = {}
        self._home_contexts: Dict[str, Dict[str, Any]] = {}
        self._pending_clarifications: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def handle(
        self,
        session_id: str,
        text: str,
        on_chat_delta: Optional[Callable[[str], None]] = None,
    ) -> str:
        with self._lock:
            corrected_text = self.bridge.parser.correct(text)
            if corrected_text != text.strip():
                logging.info("ASR领域纠错 original=%s corrected=%s", text, corrected_text)
            corrected_text = self._resolve_pending_clarification(session_id, corrected_text)
            resolved_text = self._resolve_contextual_command(session_id, corrected_text)
            if resolved_text != corrected_text:
                logging.info(
                    "家居上下文补全 session=%s original=%s resolved=%s",
                    session_id,
                    corrected_text,
                    resolved_text,
                )
                corrected_text = resolved_text
            # 已能由本地规则明确识别的家居请求直接执行。不能让大模型在
            # tool_choice=auto 时跳过工具、仅生成“已执行”的口头回复。
            intent = self.bridge.parser.parse(corrected_text)
            if intent.kind in (
                "device_control",
                "scene_control",
                "state_query",
                "mode_query",
                "dance_control",
                "home_summary",
                "bedtime_control",
                "undo",
                "clarify",
            ):
                if intent.kind == "clarify":
                    self._remember_pending_clarification(session_id, intent)
                reply = self.bridge.handle_text(corrected_text)
                logging.info(
                    "家居指令确定性执行 kind=%s device_id=%s scene_id=%s action=%s result=%s",
                    intent.kind,
                    intent.device_id,
                    intent.scene_id,
                    intent.action,
                    reply.raw_result,
                )
                answer = reply.text
                self._remember_home_context(session_id, reply)
                self._remember(session_id, corrected_text, answer)
                return answer

            history = list(self._histories.get(session_id, []))
            messages: List[Dict[str, Any]] = [
                {"role": "system", "content": SYSTEM_PROMPT},
                *history,
                {"role": "user", "content": corrected_text},
            ]
            home_related = self._looks_like_home_request(corrected_text)
            first = self.client.complete(
                messages,
                tools=[HOME_TOOL] if home_related else None,
                on_text_delta=None if home_related else on_chat_delta,
            )
            tool_calls = first.get("tool_calls") or []
            if tool_calls:
                outputs = []
                for call in tool_calls:
                    output = self._run_tool(session_id, call, corrected_text)
                    outputs.append(output)
                # 执行结果直接来自小鸿 SE 的真实 ACK。这里不再让大模型二次
                # 改写，避免失败结果被改写成“已经完成”。
                answer = "；".join(str(output.get("message") or "操作未完成。") for output in outputs)
            else:
                answer = first.get("content", "").strip()
            if not answer:
                answer = "抱歉，我刚才没有理解，请再说一次。"
            self._remember(session_id, corrected_text, answer)
            return answer

    def _run_tool(
        self, session_id: str, call: Dict[str, Any], original_text: str
    ) -> Dict[str, Any]:
        function = call.get("function") or {}
        if function.get("name") != "execute_home_command":
            return {"ok": False, "message": "不支持该工具。"}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        command = self.bridge.parser.correct(
            str(arguments.get("command") or original_text).strip()
        )
        reply = self.bridge.handle_text(command)
        succeeded = self._reply_succeeded(reply)
        self._remember_home_context(session_id, reply)
        logging.info(
            "千问工具执行 command=%s ok=%s result=%s",
            command,
            succeeded,
            reply.raw_result,
        )
        return {"ok": succeeded, "message": reply.text}

    def _remember_pending_clarification(self, session_id: str, intent) -> None:
        if intent.params.get("clarification") != "fan_room":
            return
        self._pending_clarifications[session_id] = {
            "action": intent.params.get("action"),
            "updated_at": time.monotonic(),
        }

    def _resolve_pending_clarification(self, session_id: str, text: str) -> str:
        pending = self._pending_clarifications.get(session_id)
        if not pending:
            return text
        if time.monotonic() - pending["updated_at"] > 30:
            self._pending_clarifications.pop(session_id, None)
            return text
        normalized = re.sub(r"[\s，。！？、,.!?]", "", text.strip().lower())
        room_id = None
        room_name = None
        for candidate_id, room in self.bridge.config.rooms.items():
            aliases = list(room.get("aliases", []))
            display_name = str(room.get("display_name") or "")
            if display_name:
                aliases.append(display_name)
            if any(
                self.bridge.parser._normalize(alias) in normalized
                for alias in aliases
                if alias
            ):
                room_id = candidate_id
                room_name = display_name or aliases[0]
                break
        if not room_id:
            return text
        if not self.bridge.config.rooms.get(room_id, {}).get("fan"):
            return text
        action = pending.get("action")
        self._pending_clarifications.pop(session_id, None)
        if action == "turn_on":
            resolved = "打开%s风扇" % room_name
        elif action == "turn_off":
            resolved = "关闭%s风扇" % room_name
        else:
            resolved = "查询%s风扇状态" % room_name
        logging.info(
            "补全风扇房间 session=%s answer=%s resolved=%s",
            session_id,
            text,
            resolved,
        )
        return resolved

    def _remember_home_context(self, session_id: str, reply) -> None:
        intent = reply.intent
        if intent.kind not in (
            "device_control",
            "scene_control",
            "state_query",
            "mode_query",
            "dance_control",
        ):
            return
        if not self._reply_succeeded(reply):
            return
        self._pending_clarifications.pop(session_id, None)
        self._home_contexts[session_id] = {
            "intent": intent,
            "updated_at": time.monotonic(),
        }

    def _resolve_contextual_command(self, session_id: str, text: str) -> str:
        """Resolve short, unambiguous follow-ups without asking Qwen to guess."""
        context = self._home_contexts.get(session_id)
        if not context or time.monotonic() - context["updated_at"] > 120:
            return text
        intent = context["intent"]
        normalized = re.sub(r"[\s，。！？、,.!?]", "", text.strip().lower())
        has_reference = any(
            word in normalized for word in ("它", "这个", "那个", "这盏", "那盏", "刚才的")
        )
        short_follow_up = len(normalized) <= 8
        has_explicit_target = any(
            word in normalized
            for word in (
                "客厅",
                "主卧",
                "次卧",
                "灯",
                "风扇",
                "报警器",
                "温度",
                "湿度",
                "光照",
                "模式",
                "跳舞",
            )
        )
        if not has_reference and (not short_follow_up or has_explicit_target):
            return text

        if intent.kind in ("device_control", "state_query") and intent.device_id:
            name = self.bridge._device_name(intent.device_id)
            if any(word in normalized for word in ("关掉", "关闭", "关上", "停掉")):
                return "关闭" + name
            if any(word in normalized for word in ("打开", "开启", "开开")):
                return "打开" + name
            if any(word in normalized for word in ("开着吗", "关了吗", "什么状态", "状态呢")):
                return "查询" + name + "状态"
            color = self.bridge.parser._find_color(normalized)
            if color and any(word in normalized for word in ("换成", "调成", "变成", "它", "这个")):
                color_name = next(
                    name for name, value in self.bridge.parser.COLORS.items() if value == color
                )
                return "把%s调成%s" % (name, color_name)

        if intent.kind == "scene_control" and any(
            word in normalized for word in ("关闭", "关掉", "退出", "停止")
        ):
            scene_name = {
                "home_mode": "居家模式",
                "away_mode": "离家模式",
                "normal_mode": "普通模式",
                "bright_mode": "明亮模式",
                "ambient_mode": "氛围模式",
                "ventilation_mode": "通风模式",
            }.get(intent.scene_id)
            if scene_name:
                return "关闭" + scene_name

        if intent.kind == "dance_control" and any(
            word in normalized for word in ("关闭", "关掉", "退出", "停止", "结束")
        ):
            return "停止跳舞"
        return text

    @staticmethod
    def _reply_succeeded(reply) -> bool:
        result = reply.raw_result or {}
        if reply.intent.kind in (
            "device_control",
            "scene_control",
            "dance_control",
            "bedtime_control",
            "undo",
        ):
            return bool(
                result.get("execution_state") == "succeeded" and result.get("code") == 0
            )
        if reply.intent.kind in ("state_query", "mode_query", "home_summary"):
            return bool(result.get("code") == 0)
        return False

    @staticmethod
    def _looks_like_home_request(text: str) -> bool:
        home_words = (
            "灯",
            "风扇",
            "温度",
            "湿度",
            "光照",
            "照度",
            "模式",
            "报警",
            "警报",
            "开门",
            "关门",
            "电视",
            "空调",
            "窗帘",
            "有点暗",
            "太暗",
            "看不清",
            "有点热",
            "太热",
            "好热",
            "有点冷",
            "太冷",
            "好冷",
            "有点闷",
            "透透气",
            "跳舞",
            "舞蹈",
            "派对",
            "热闹一下",
            "睡觉",
            "晚安模式",
            "撤销",
            "恢复刚才",
            "家里现在怎么样",
            "全屋状态",
            "回家",
            "到家",
            "回来",
            "出门",
            "外出",
        )
        if any(word in text for word in home_words):
            return True
        pronouns = ("它", "这个", "那个", "这盏", "那盏")
        actions = ("打开", "开启", "关闭", "关掉", "停止", "查询", "看看")
        return any(word in text for word in pronouns) and any(word in text for word in actions)

    def _remember(self, session_id: str, user_text: str, answer: str) -> None:
        history = self._histories.setdefault(session_id, [])
        history.extend(
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": answer},
            ]
        )
        del history[: max(0, len(history) - self.max_history_messages)]
