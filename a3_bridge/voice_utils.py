"""Small, ROS-independent helpers for the offline voice application."""

import json
import re


WAKE_WORDS = (
    "远征远征",
    "远征",
    "嗨鹿卡",
    "嗨露卡",
    "hi luka",
    "hey luka",
)


def parse_vosk_result(payload: str) -> str:
    """Return normalized recognized text from a Vosk JSON result."""
    try:
        result = json.loads(payload)
    except (TypeError, ValueError):
        return ""
    text = str(result.get("text", "")).strip()
    return re.sub(r"\s+", "", text)


def remove_wake_word(text: str) -> str:
    """Remove a leading A3 wake phrase while preserving the command."""
    normalized = text.strip()
    lowered = normalized.lower()
    for wake_word in WAKE_WORDS:
        if lowered.startswith(wake_word):
            return normalized[len(wake_word) :].lstrip("，,。.!！?？ ")
    return normalized


def reply_audio_key(reply: str) -> str:
    """Map dynamic bridge replies to a compact set of prerecorded prompts."""
    if "没有听清" in reply:
        return "not_clear"
    if "哪个房间" in reply or "打开还是关闭" in reply:
        return "clarify"
    if "不在智能家居控制范围" in reply or "暂时无法执行" in reply:
        return "unsupported"
    if "无法连接智能家居中控" in reply:
        return "gateway"
    if "离线" in reply:
        return "offline"
    if "没有响应" in reply or "稍后再试" in reply:
        return "timeout"
    if "失败" in reply or "异常" in reply or "参数不正确" in reply:
        return "failed"
    return "success"
