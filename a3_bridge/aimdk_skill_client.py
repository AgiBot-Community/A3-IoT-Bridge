"""Minimal HTTP client for the AimDK SkillPilot skill-package RPC."""

import http.client
import json
import socket
from typing import Any, Dict, Tuple
from urllib.parse import urlsplit


class AimDKSkillError(RuntimeError):
    pass


class AimDKSkillClient:
    def __init__(self, rpc_url: str, timeout_seconds: float = 5.0):
        parsed = urlsplit(rpc_url)
        if parsed.scheme != "http" or not parsed.hostname or not parsed.path:
            raise ValueError("AimDK SkillPackage RPC 地址无效")
        self.endpoint: Tuple[str, int, str] = (
            parsed.hostname,
            parsed.port or 80,
            parsed.path,
        )
        self.timeout_seconds = timeout_seconds

    def start_skill(self, path: str, session_id: str) -> Dict[str, Any]:
        return self._call(
            {
                "header": self._header(session_id),
                "source": "custom",
                "command": "Start",
                "path": path,
                "session_id": session_id,
            }
        )

    def stop_skill(self, session_id: str) -> Dict[str, Any]:
        return self._call(
            {
                "header": self._header(session_id),
                "source": "custom",
                "command": "Stop",
                "session_id": session_id,
            }
        )

    @staticmethod
    def _header(trace_id: str) -> Dict[str, Any]:
        return {
            "timestamp": {
                "seconds": "0",
                "nanos": 0,
                "ms_since_epoch": "0",
            },
            "trace_id": trace_id,
            "domin": "a3_home_bridge",
        }

    def _call(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        host, port, path = self.endpoint
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        conn = http.client.HTTPConnection(host, port, timeout=self.timeout_seconds)
        try:
            conn.request(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Content-Length": str(len(body)),
                    "Connection": "close",
                },
            )
            response = conn.getresponse()
            raw = response.read()
        except (socket.timeout, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise AimDKSkillError("AimDK 舞蹈接口无响应：%s" % exc) from exc
        finally:
            conn.close()

        try:
            result = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AimDKSkillError("AimDK 舞蹈接口返回非 JSON 数据") from exc
        if not isinstance(result, dict):
            raise AimDKSkillError("AimDK 舞蹈接口返回格式异常")
        result.setdefault("_http_status", response.status)
        header = result.get("header") or {}
        if response.status != 200 or header.get("code", 0) not in (0, "0"):
            raise AimDKSkillError(
                "AimDK 舞蹈接口执行失败：%s"
                % (header.get("msg") or result.get("message") or response.status)
            )
        return result
