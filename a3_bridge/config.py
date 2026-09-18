import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict


@dataclass(frozen=True)
class DiscoveryConfig:
    enabled: bool = False
    auto_directed_broadcasts: bool = True
    unicast_scan_fallback: bool = True
    unicast_scan_max_hosts: int = 512
    unicast_scan_timeout_seconds: float = 3.0
    broadcast_address: str = "255.255.255.255"
    port: int = 37020
    timeout_seconds: float = 3.0
    attempts: int = 3
    interval_seconds: float = 0.5
    device_id: str = "xiaohong-se-01"
    service: str = "xiaohong-se-home-gateway"


@dataclass(frozen=True)
class GatewayConfig:
    base_url: str
    bearer_token: str
    connect_timeout_seconds: float = 1.0
    read_timeout_seconds: float = 5.0
    scene_read_timeout_seconds: float = 10.0
    retry_count: int = 1
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)


@dataclass(frozen=True)
class DanceConfig:
    enabled: bool = False
    duration_seconds: int = 18
    lighting_interval_ms: int = 600
    skill_package_path: str = "/agibot/data/resources/default/skill/零帧起手"
    skill_rpc_url: str = (
        "http://10.42.10.12:52893/rpc/aimdk.protocol.SkillPilotService/SkillPackage"
    )
    rpc_timeout_seconds: float = 5.0


@dataclass(frozen=True)
class AppConfig:
    gateway: GatewayConfig
    default_room: str
    rooms: Dict[str, Dict[str, Any]]
    sensors: Dict[str, str]
    device_names: Dict[str, str]
    scene_phrases: Dict[str, list]
    mode_state_file: str = "state/desired_mode.json"
    dance: DanceConfig = field(default_factory=DanceConfig)


def load_config(path: str) -> AppConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    gateway_raw = dict(raw["gateway"])
    discovery_raw = dict(gateway_raw.get("discovery", {}))

    base_url = os.getenv("XHSE_BASE_URL", gateway_raw.get("base_url", "")).strip()
    token = os.getenv("XHSE_BEARER_TOKEN", gateway_raw.get("bearer_token", "")).strip()
    discovery_enabled_env = os.getenv("XHSE_DISCOVERY_ENABLED")
    if discovery_enabled_env is None:
        discovery_enabled = bool(discovery_raw.get("enabled", False))
    else:
        discovery_enabled = discovery_enabled_env.strip().lower() in ("1", "true", "yes", "on")

    if base_url and not base_url.startswith("http://"):
        raise ValueError("gateway.base_url 必须使用 http://，例如 http://192.168.1.100:8080")
    if not base_url and not discovery_enabled:
        raise ValueError("请配置 gateway.base_url，或启用 gateway.discovery.enabled")
    if not token or token.startswith("replace-"):
        raise ValueError("请在 config.json 或 XHSE_BEARER_TOKEN 中配置联调 Token")

    discovery = DiscoveryConfig(
        enabled=discovery_enabled,
        auto_directed_broadcasts=os.getenv(
            "XHSE_DISCOVERY_AUTO_INTERFACES",
            str(discovery_raw.get("auto_directed_broadcasts", True)),
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        unicast_scan_fallback=os.getenv(
            "XHSE_DISCOVERY_UNICAST_SCAN",
            str(discovery_raw.get("unicast_scan_fallback", True)),
        ).strip().lower()
        in ("1", "true", "yes", "on"),
        unicast_scan_max_hosts=max(1, int(discovery_raw.get("unicast_scan_max_hosts", 512))),
        unicast_scan_timeout_seconds=float(
            discovery_raw.get("unicast_scan_timeout_seconds", 3.0)
        ),
        broadcast_address=os.getenv(
            "XHSE_DISCOVERY_BROADCAST",
            str(discovery_raw.get("broadcast_address", "255.255.255.255")),
        ).strip(),
        port=int(discovery_raw.get("port", 37020)),
        timeout_seconds=float(discovery_raw.get("timeout_seconds", 3.0)),
        attempts=max(1, int(discovery_raw.get("attempts", 3))),
        interval_seconds=max(0.0, float(discovery_raw.get("interval_seconds", 0.5))),
        device_id=str(discovery_raw.get("device_id", "xiaohong-se-01")).strip(),
        service=str(discovery_raw.get("service", "xiaohong-se-home-gateway")).strip(),
    )
    if not 1 <= discovery.port <= 65535:
        raise ValueError("gateway.discovery.port 必须在 1～65535 之间")
    if discovery.timeout_seconds <= 0:
        raise ValueError("gateway.discovery.timeout_seconds 必须大于 0")
    if discovery.unicast_scan_timeout_seconds <= 0:
        raise ValueError("gateway.discovery.unicast_scan_timeout_seconds 必须大于 0")
    if not discovery.broadcast_address:
        raise ValueError("gateway.discovery.broadcast_address 不能为空")

    gateway = GatewayConfig(
        base_url=base_url.rstrip("/"),
        bearer_token=token,
        connect_timeout_seconds=float(gateway_raw.get("connect_timeout_seconds", 1.0)),
        read_timeout_seconds=float(gateway_raw.get("read_timeout_seconds", 5.0)),
        scene_read_timeout_seconds=float(gateway_raw.get("scene_read_timeout_seconds", 10.0)),
        retry_count=max(0, int(gateway_raw.get("retry_count", 1))),
        discovery=discovery,
    )

    rooms = raw.get("rooms", {})
    default_room = raw.get("default_room", "")
    if default_room and default_room not in rooms:
        raise ValueError("default_room 必须是 rooms 中已定义的房间")

    dance_raw = dict(raw.get("dance", {}))
    dance_enabled_env = os.getenv("A3_DANCE_ENABLED")
    if dance_enabled_env is None:
        dance_enabled = bool(dance_raw.get("enabled", False))
    else:
        dance_enabled = dance_enabled_env.strip().lower() in ("1", "true", "yes", "on")
    dance = DanceConfig(
        enabled=dance_enabled,
        duration_seconds=int(dance_raw.get("duration_seconds", 18)),
        lighting_interval_ms=int(dance_raw.get("lighting_interval_ms", 600)),
        skill_package_path=os.getenv(
            "A3_DANCE_SKILL_PATH",
            str(
                dance_raw.get(
                    "skill_package_path",
                    "/agibot/data/resources/default/skill/零帧起手",
                )
            ),
        ).strip(),
        skill_rpc_url=os.getenv(
            "A3_DANCE_SKILL_RPC_URL",
            str(
                dance_raw.get(
                    "skill_rpc_url",
                    "http://10.42.10.12:52893/rpc/aimdk.protocol.SkillPilotService/SkillPackage",
                )
            ),
        ).strip(),
        rpc_timeout_seconds=float(dance_raw.get("rpc_timeout_seconds", 5.0)),
    )
    if not 15 <= dance.duration_seconds <= 20:
        raise ValueError("dance.duration_seconds 必须在 15～20 秒之间")
    if not 400 <= dance.lighting_interval_ms <= 1000:
        raise ValueError("dance.lighting_interval_ms 必须在 400～1000 毫秒之间")
    if not dance.skill_package_path.startswith("/agibot/data/resources/default/skill/"):
        raise ValueError("dance.skill_package_path 必须指向机器人内置技能目录")
    if not dance.skill_rpc_url.startswith("http://"):
        raise ValueError("dance.skill_rpc_url 必须使用 http://")
    if dance.rpc_timeout_seconds <= 0:
        raise ValueError("dance.rpc_timeout_seconds 必须大于 0")

    return AppConfig(
        gateway=gateway,
        default_room=default_room,
        rooms=rooms,
        sensors=raw.get("sensors", {}),
        device_names=raw.get("device_names", {}),
        scene_phrases=raw.get("scene_phrases", {}),
        mode_state_file=str(raw.get("mode_state_file", "state/desired_mode.json")),
        dance=dance,
    )
