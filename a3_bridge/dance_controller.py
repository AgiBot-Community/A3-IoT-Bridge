"""Coordinate an AimDK built-in dance skill with Xiaohong SE lighting."""

import logging
import threading
import uuid
from typing import Any, Dict, Optional

from .aimdk_skill_client import AimDKSkillClient, AimDKSkillError
from .config import DanceConfig
from .xiaohong_client import GatewayError, XiaohongSEClient


class DanceController:
    def __init__(
        self,
        config: DanceConfig,
        gateway: XiaohongSEClient,
        skill_client: Optional[AimDKSkillClient] = None,
    ):
        self.config = config
        self.gateway = gateway
        self.skill_client = skill_client or AimDKSkillClient(
            config.skill_rpc_url,
            config.rpc_timeout_seconds,
        )
        self._lock = threading.RLock()
        self._active = False
        self._session_id: Optional[str] = None
        self._timer: Optional[threading.Timer] = None

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def start(self) -> Dict[str, Any]:
        with self._lock:
            if not self.config.enabled:
                return self._locked_result()
            if self._active:
                return {
                    "code": 0,
                    "execution_state": "succeeded",
                    "active": True,
                    "reason": "already_running",
                    "duration_sec": self.config.duration_seconds,
                }

            request_id = "a3-dance-%s" % uuid.uuid4()
            try:
                lighting = self.gateway.control_scene(
                    "dance_lighting",
                    request_id=request_id,
                    params={
                        "duration_sec": self.config.duration_seconds,
                        "interval_ms": self.config.lighting_interval_ms,
                    },
                )
            except GatewayError as exc:
                return self._failed_result("小鸿 SE 暂时没有响应。", str(exc))
            if not self._scene_succeeded(lighting):
                return {
                    "code": lighting.get("code", 1),
                    "execution_state": lighting.get("execution_state", "failed"),
                    "active": False,
                    "reason": "lighting_start_failed",
                    "lighting": lighting,
                }

            session_id = "a3-dance-skill-%s" % uuid.uuid4()
            try:
                skill = self.skill_client.start_skill(
                    self.config.skill_package_path,
                    session_id,
                )
            except AimDKSkillError as exc:
                self._stop_lighting_after_start_failure()
                return self._failed_result("机器人舞蹈动作启动失败。", str(exc))

            returned_session = str(skill.get("session_id") or session_id).strip()
            self._session_id = returned_session
            self._active = True
            self._timer = threading.Timer(
                self.config.duration_seconds,
                self._finish_after_timeout,
            )
            self._timer.daemon = True
            self._timer.start()
            logging.info(
                "跳舞模式已启动 duration=%ss interval=%sms skill=%s session_id=%s",
                self.config.duration_seconds,
                self.config.lighting_interval_ms,
                self.config.skill_package_path,
                returned_session,
            )
            return {
                "code": 0,
                "execution_state": "succeeded",
                "active": True,
                "reason": "running",
                "duration_sec": self.config.duration_seconds,
                "lighting": lighting,
                "skill": skill,
            }

    def stop(self, reason: str = "manual_stop") -> Dict[str, Any]:
        with self._lock:
            if not self.config.enabled:
                return self._locked_result()
            timer = self._timer
            self._timer = None
            if timer is not None:
                timer.cancel()
            session_id = self._session_id
            was_active = self._active
            self._active = False
            self._session_id = None

            skill_result: Dict[str, Any] = {"skipped": not bool(session_id)}
            skill_error = None
            if session_id:
                try:
                    skill_result = self.skill_client.stop_skill(session_id)
                except AimDKSkillError as exc:
                    skill_error = str(exc)

            lighting_result: Dict[str, Any] = {}
            lighting_error = None
            try:
                lighting_result = self.gateway.control_scene(
                    "dance_lighting_stop",
                    request_id="a3-dance-stop-%s" % uuid.uuid4(),
                )
            except GatewayError as exc:
                lighting_error = str(exc)

            succeeded = skill_error is None and self._scene_succeeded(lighting_result)
            logging.info(
                "跳舞模式停止 reason=%s was_active=%s skill_error=%s lighting_error=%s",
                reason,
                was_active,
                skill_error,
                lighting_error,
            )
            return {
                "code": 0 if succeeded else 1,
                "execution_state": "succeeded" if succeeded else "failed",
                "active": False,
                "reason": reason,
                "was_active": was_active,
                "skill": skill_result,
                "lighting": lighting_result,
                "skill_error": skill_error,
                "lighting_error": lighting_error,
            }

    def status(self) -> Dict[str, Any]:
        with self._lock:
            if not self.config.enabled:
                return self._locked_result()
            local_active = self._active
            session_id = self._session_id
        try:
            lighting = self.gateway.get_dance_lighting_status()
        except GatewayError as exc:
            return self._failed_result("暂时无法查询跳舞模式。", str(exc))
        return {
            "code": lighting.get("code", 0),
            "execution_state": "succeeded" if lighting.get("code", 0) == 0 else "failed",
            "active": bool(local_active or lighting.get("active")),
            "reason": lighting.get("reason", "running" if local_active else "idle"),
            "duration_sec": lighting.get("duration_sec", self.config.duration_seconds),
            "remaining_ms": lighting.get("remaining_ms"),
            "session_id": session_id,
            "lighting": lighting,
        }

    def close(self) -> None:
        if self.config.enabled and self.is_active:
            self.stop(reason="service_shutdown")

    def _finish_after_timeout(self) -> None:
        try:
            self.stop(reason="completed")
        except Exception:
            logging.exception("跳舞模式定时停止失败")

    def _stop_lighting_after_start_failure(self) -> None:
        try:
            self.gateway.control_scene(
                "dance_lighting_stop",
                request_id="a3-dance-rollback-%s" % uuid.uuid4(),
            )
        except GatewayError:
            logging.exception("舞蹈启动失败后，灯效回滚请求失败")

    @staticmethod
    def _scene_succeeded(result: Dict[str, Any]) -> bool:
        return bool(
            result.get("code") == 0 and result.get("execution_state") == "succeeded"
        )

    @staticmethod
    def _failed_result(message: str, detail: str) -> Dict[str, Any]:
        return {
            "code": 1,
            "execution_state": "failed",
            "active": False,
            "reason": "error",
            "message": message,
            "detail": detail,
        }

    @staticmethod
    def _locked_result() -> Dict[str, Any]:
        return {
            "code": 2001,
            "execution_state": "blocked",
            "active": False,
            "reason": "safety_locked",
            "message": "跳舞功能已经准备好，目前处于安全锁定状态。",
        }
