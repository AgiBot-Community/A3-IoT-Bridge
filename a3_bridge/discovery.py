import json
import ipaddress
import logging
import select
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import DiscoveryConfig


class DiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiscoveryResult:
    host: str
    port: int
    announcement: Dict[str, Any]

    @property
    def base_url(self) -> str:
        return "http://%s:%d" % (self.host, self.port)


@dataclass(frozen=True)
class BroadcastTarget:
    interface: str
    local_ip: str
    broadcast_ip: str
    prefix_length: int = 24
    is_default_route: bool = False


_IGNORED_INTERFACE_PREFIXES = (
    "lo",
    "docker",
    "veth",
    "virbr",
    "vmnet",
    "vbox",
    "br-",
    "tun",
    "tap",
    "wg",
    "tailscale",
    "zt",
    "cni",
    "flannel",
    "podman",
)


def enumerate_directed_broadcasts() -> List[BroadcastTarget]:
    """Return broadcast-capable global IPv4 addresses on a Linux HDU.

    A socket is later bound to ``local_ip`` so Linux cannot route the packet
    through A3's higher-priority internal Ethernet or 5G interface.
    """
    try:
        completed = subprocess.run(
            ["ip", "-j", "-4", "address", "show", "up", "scope", "global"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        interfaces = json.loads(completed.stdout)
        routes_completed = subprocess.run(
            ["ip", "-j", "-4", "route", "show", "default"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        routes = json.loads(routes_completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TypeError):
        logging.warning("无法枚举活动 IPv4 网卡，将使用兼容广播地址")
        return []

    default_sources = {
        (str(route.get("dev", "")), str(route.get("prefsrc") or route.get("src") or ""))
        for route in routes if isinstance(routes, list) and isinstance(route, dict)
    }
    default_devices = {dev for dev, _ in default_sources if dev}
    results: List[BroadcastTarget] = []
    seen = set()
    for interface in interfaces if isinstance(interfaces, list) else []:
        if not isinstance(interface, dict):
            continue
        name = str(interface.get("ifname", ""))
        lowered = name.lower()
        if not name or lowered.startswith(_IGNORED_INTERFACE_PREFIXES):
            continue
        flags = {str(flag).upper() for flag in interface.get("flags", [])}
        if "POINTOPOINT" in flags or (flags and "BROADCAST" not in flags):
            continue
        for address in interface.get("addr_info", []):
            if not isinstance(address, dict) or address.get("family") != "inet":
                continue
            if address.get("scope") not in (None, "global"):
                continue
            local_ip = str(address.get("local", ""))
            try:
                parsed_ip = ipaddress.IPv4Address(local_ip)
                prefix_length = int(address.get("prefixlen"))
                if parsed_ip.is_loopback or parsed_ip.is_link_local or not 0 <= prefix_length <= 30:
                    continue
                broadcast_ip = str(
                    address.get("broadcast")
                    or ipaddress.IPv4Interface("%s/%d" % (local_ip, prefix_length)).network.broadcast_address
                )
                ipaddress.IPv4Address(broadcast_ip)
            except (
                ipaddress.AddressValueError,
                ipaddress.NetmaskValueError,
                TypeError,
                ValueError,
                KeyError,
            ):
                continue
            key = (local_ip, broadcast_ip)
            if key not in seen:
                seen.add(key)
                is_default = (name, local_ip) in default_sources or (
                    name in default_devices and not any(src for dev, src in default_sources if dev == name)
                )
                results.append(
                    BroadcastTarget(name, local_ip, broadcast_ip, prefix_length, is_default)
                )
    return sorted(results, key=lambda item: (not item.is_default_route, item.interface, item.local_ip))


def _open_discovery_sockets(
    config: DiscoveryConfig,
) -> List[Tuple[socket.socket, BroadcastTarget, Tuple[str, ...]]]:
    targets = enumerate_directed_broadcasts() if config.auto_directed_broadcasts else []
    opened: List[Tuple[socket.socket, BroadcastTarget, Tuple[str, ...]]] = []

    for target in targets:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((target.local_ip, 0))
        except OSError as exc:
            sock.close()
            logging.warning(
                "跳过无法绑定的发现网卡 interface=%s local=%s error=%s",
                target.interface,
                target.local_ip,
                exc,
            )
            continue
        destinations = [target.broadcast_ip]
        if config.broadcast_address and config.broadcast_address not in destinations:
            destinations.append(config.broadcast_address)
        opened.append((sock, target, tuple(destinations)))

    if opened:
        return opened

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", 0))
    fallback = BroadcastTarget("route-default", "0.0.0.0", config.broadcast_address, 24, True)
    return [(sock, fallback, (config.broadcast_address,))]


def discover_gateway(config: DiscoveryConfig) -> DiscoveryResult:
    request_id = "discover-%s" % uuid.uuid4()
    payload = json.dumps(
        {"type": "xhse_discover", "version": 1, "request_id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")
    deadline = time.monotonic() + config.timeout_seconds

    opened = _open_discovery_sockets(config)
    try:
        for attempt in range(config.attempts):
            sent = 0
            for sock, target, destinations in opened:
                for destination in destinations:
                    try:
                        sock.sendto(payload, (destination, config.port))
                        sent += 1
                        logging.info(
                            "发送小鸿 SE 发现广播 attempt=%d/%d interface=%s local=%s target=%s request_id=%s",
                            attempt + 1,
                            config.attempts,
                            target.interface,
                            target.local_ip,
                            destination,
                            request_id,
                        )
                    except OSError as exc:
                        logging.warning(
                            "发现广播发送失败 interface=%s target=%s error=%s",
                            target.interface,
                            destination,
                            exc,
                        )
            if sent == 0:
                raise DiscoveryError("所有活动网卡均无法发送小鸿 SE 发现广播")

            receive_until = min(deadline, time.monotonic() + config.interval_seconds)
            result = _receive_valid_any(
                [item[0] for item in opened], receive_until, request_id, config
            )
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                break

        result = _receive_valid_any([item[0] for item in opened], deadline, request_id, config)
        if result is not None:
            return result
        if config.auto_directed_broadcasts and config.unicast_scan_fallback:
            result = _scan_local_subnets(opened, payload, request_id, config)
            if result is not None:
                return result
    finally:
        for sock, _, _ in opened:
            sock.close()

    raise DiscoveryError("未发现小鸿 SE，请确认设备已联网且局域网未开启客户端隔离")


def _scan_local_subnets(
    opened: Sequence[Tuple[socket.socket, BroadcastTarget, Tuple[str, ...]]],
    payload: bytes,
    request_id: str,
    config: DiscoveryConfig,
) -> Optional[DiscoveryResult]:
    """Last-resort discovery for hotspots that do not forward broadcasts."""
    candidates = [item for item in opened if item[1].local_ip != "0.0.0.0"]
    default_candidates = [item for item in candidates if item[1].is_default_route]
    if default_candidates:
        candidates = default_candidates

    deadline = time.monotonic() + config.unicast_scan_timeout_seconds
    for sock, target, _ in candidates:
        try:
            network = ipaddress.IPv4Network(
                "%s/%d" % (target.local_ip, target.prefix_length), strict=False
            )
        except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError):
            continue
        host_count = max(0, int(network.num_addresses) - 2)
        if host_count > config.unicast_scan_max_hosts:
            logging.warning(
                "跳过过大的单播发现网段 interface=%s network=%s hosts=%d limit=%d",
                target.interface,
                network,
                host_count,
                config.unicast_scan_max_hosts,
            )
            continue
        hosts = [str(host) for host in network.hosts() if str(host) != target.local_ip]
        logging.info(
            "广播未响应，开始受限单播发现 interface=%s network=%s hosts=%d request_id=%s",
            target.interface,
            network,
            len(hosts),
            request_id,
        )
        sent = 0
        for host in hosts:
            if time.monotonic() >= deadline:
                break
            try:
                sock.sendto(payload, (host, config.port))
                sent += 1
            except OSError:
                continue
            if sent % 32 == 0:
                result = _receive_valid_any(
                    [sock], min(deadline, time.monotonic() + 0.05), request_id, config
                )
                if result is not None:
                    return result
        result = _receive_valid_any([sock], deadline, request_id, config)
        if result is not None:
            return result
    return None


def _receive_valid_any(
    sockets: Sequence[socket.socket],
    deadline: float,
    request_id: str,
    config: DiscoveryConfig,
) -> Optional[DiscoveryResult]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            readable, _, _ = select.select(list(sockets), [], [], remaining)
        except (OSError, ValueError) as exc:
            raise DiscoveryError("等待小鸿 SE 发现响应失败") from exc
        if not readable:
            return None
        try:
            data, peer = readable[0].recvfrom(2048)
        except OSError as exc:
            raise DiscoveryError("接收小鸿 SE 发现响应失败") from exc

        message = _parse_announcement(data, request_id, config)
        if message is None:
            continue

        peer_host = peer[0]
        announced_ip = message.get("ip")
        if announced_ip != peer_host:
            logging.warning(
                "小鸿 SE 响应地址与来源地址不一致 announced=%s peer=%s，使用来源地址",
                announced_ip,
                peer_host,
            )
        result = DiscoveryResult(peer_host, message["http_port"], message)
        logging.info(
            "发现小鸿 SE device_id=%s endpoint=%s",
            message.get("device_id"),
            result.base_url,
        )
        return result


def _parse_announcement(
    data: bytes,
    request_id: str,
    config: DiscoveryConfig,
) -> Optional[Dict[str, Any]]:
    try:
        message = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(message, dict):
        return None
    port = message.get("http_port")
    if (
        message.get("type") != "xhse_announce"
        or message.get("version") != 1
        or message.get("request_id") != request_id
        or message.get("service") != config.service
        or not isinstance(message.get("ip"), str)
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
    ):
        return None
    if config.device_id and message.get("device_id") != config.device_id:
        return None
    return message
