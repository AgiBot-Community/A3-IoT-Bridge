import http.client
import json
import logging
import re
import socket
import threading
import time
import uuid
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode, urlsplit

from .config import GatewayConfig
from .discovery import DiscoveryError, discover_gateway


class GatewayError(RuntimeError):
    pass


class GatewayConnectionError(GatewayError):
    pass


class GatewayProtocolError(GatewayError):
    pass


class GatewayDiscoveryError(GatewayConnectionError):
    pass


class XiaohongSEClient:
    REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

    def __init__(self, config: GatewayConfig):
        self.config = config
        self._endpoint_lock = threading.Lock()
        self._fallback_endpoint = self._parse_endpoint(config.base_url) if config.base_url else None
        self._endpoint = None if config.discovery.enabled else self._fallback_endpoint

    @staticmethod
    def _parse_endpoint(base_url: str) -> Tuple[str, int, str]:
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError("小鸿 SE 当前只支持合法的 http:// 地址")
        return parsed.hostname, parsed.port or 80, parsed.path.rstrip("/")

    @property
    def current_base_url(self) -> Optional[str]:
        endpoint = self._endpoint
        if endpoint is None:
            return None
        host, port, base_path = endpoint
        return "http://%s:%d%s" % (host, port, base_path)

    @staticmethod
    def new_request_id(prefix: str = "a3") -> str:
        return "%s-%s" % (prefix, uuid.uuid4())

    def health(self) -> Dict[str, Any]:
        return self._request_json("GET", "/api/v1/health", retries=1)

    def get_device_state(self, device_id: str) -> Dict[str, Any]:
        query = urlencode({"device_id": device_id})
        return self._request_json("GET", "/api/v1/device/state?" + query, retries=1)

    def get_mode(self) -> Dict[str, Any]:
        return self._request_json("GET", "/api/v1/mode", retries=1)

    def get_dance_lighting_status(self) -> Dict[str, Any]:
        return self._request_json(
            "GET",
            "/api/v1/dance-lighting/status",
            retries=1,
        )

    def control_device(
        self,
        device_id: str,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        request_id = request_id or self.new_request_id("a3-device")
        self._validate_request_id(request_id)
        payload = {
            "request_id": request_id,
            "device_id": device_id,
            "action": action,
            "params": params or {},
            "source": "agibot_a3",
        }
        return self._request_json(
            "POST",
            "/api/v1/device/control",
            payload=payload,
            retries=self.config.retry_count,
        )

    def control_scene(
        self,
        scene_id: str,
        request_id: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        request_id = request_id or self.new_request_id("a3-scene")
        self._validate_request_id(request_id)
        payload = {
            "request_id": request_id,
            "scene_id": scene_id,
            "source": "agibot_a3",
        }
        for key, value in (params or {}).items():
            if key in ("request_id", "scene_id", "source"):
                raise ValueError("场景参数不能覆盖保留字段 %s" % key)
            payload[key] = value
        return self._request_json(
            "POST",
            "/api/v1/scene/control",
            payload=payload,
            read_timeout=self.config.scene_read_timeout_seconds,
            retries=self.config.retry_count,
        )

    def _validate_request_id(self, request_id: str) -> None:
        if not self.REQUEST_ID_RE.fullmatch(request_id):
            raise ValueError("request_id 不符合小鸿 SE 的格式约束")

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        read_timeout: Optional[float] = None,
        retries: int = 0,
    ) -> Dict[str, Any]:
        body = b""
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(body) > 2048:
                raise ValueError("请求体超过小鸿 SE 的 2 KiB 限制")

        last_error = None
        for attempt in range(retries + 1):
            try:
                self._ensure_endpoint()
                return self._send_once(method, path, body, read_timeout)
            except GatewayConnectionError as exc:
                last_error = exc
                if self.config.discovery.enabled:
                    self._invalidate_endpoint()
                if attempt >= retries:
                    raise
                time.sleep(0.15 * (attempt + 1))
        raise last_error or GatewayConnectionError("请求失败")

    def _ensure_endpoint(self) -> None:
        if self._endpoint is not None:
            return
        with self._endpoint_lock:
            if self._endpoint is not None:
                return
            try:
                discovered = discover_gateway(self.config.discovery)
                self._endpoint = (discovered.host, discovered.port, "")
                health = self._send_once(
                    "GET",
                    "/api/v1/health",
                    b"",
                    self.config.read_timeout_seconds,
                )
                if (
                    health.get("_http_status") != 200
                    or health.get("service") != self.config.discovery.service
                ):
                    raise GatewayProtocolError("自动发现地址未通过小鸿 SE 健康检查")
                logging.info("小鸿 SE 自动发现和健康验证成功 endpoint=%s", self.current_base_url)
            except (DiscoveryError, GatewayConnectionError, GatewayProtocolError) as exc:
                self._endpoint = None
                if self._fallback_endpoint is not None:
                    logging.warning("自动发现失败，尝试备用地址: %s", exc)
                    self._endpoint = self._fallback_endpoint
                    return
                if isinstance(exc, GatewayProtocolError):
                    raise
                raise GatewayDiscoveryError(str(exc)) from exc

    def _invalidate_endpoint(self) -> None:
        with self._endpoint_lock:
            self._endpoint = None

    def _send_once(
        self,
        method: str,
        path: str,
        body: bytes,
        read_timeout: Optional[float],
    ) -> Dict[str, Any]:
        endpoint = self._endpoint
        if endpoint is None:
            raise GatewayConnectionError("尚未获得小鸿 SE 地址")
        host, port, base_path = endpoint
        conn = http.client.HTTPConnection(
            host,
            port,
            timeout=self.config.connect_timeout_seconds,
        )
        headers = {
            "Authorization": "Bearer " + self.config.bearer_token,
            "Accept": "application/json",
            "Connection": "close",
        }
        if body:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))

        try:
            conn.connect()
            if conn.sock is not None:
                conn.sock.settimeout(read_timeout or self.config.read_timeout_seconds)
            conn.request(method, base_path + path, body=body or None, headers=headers)
            response = conn.getresponse()
            raw = response.read()
        except (socket.timeout, TimeoutError) as exc:
            raise GatewayConnectionError("连接或等待小鸿 SE 响应超时") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise GatewayConnectionError("无法连接小鸿 SE：%s" % exc) from exc
        finally:
            conn.close()

        try:
            result = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayProtocolError("小鸿 SE 返回了非 JSON 响应，HTTP %s" % response.status) from exc

        if not isinstance(result, dict):
            raise GatewayProtocolError("小鸿 SE JSON 顶层必须是对象")
        result.setdefault("_http_status", response.status)
        return result
