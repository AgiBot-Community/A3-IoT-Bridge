from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ParsedIntent:
    kind: str
    original_text: str
    device_id: Optional[str] = None
    action: Optional[str] = None
    scene_id: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    message: Optional[str] = None


@dataclass(frozen=True)
class BridgeReply:
    text: str
    intent: ParsedIntent
    raw_result: Optional[Dict[str, Any]] = None
