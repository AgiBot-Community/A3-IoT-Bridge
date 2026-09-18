import argparse
import json
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs, urlsplit


class MockState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.devices: Dict[str, Dict[str, Any]] = {
            "light_1": {"power": "off", "rgb": [255, 255, 255], "brightness": 255},
            "light_2": {"power": "off", "rgb": [255, 255, 255], "brightness": 255},
            "light_3": {"power": "off", "rgb": [255, 255, 255], "brightness": 255},
            "fan_1": {"power": "off", "level": 0},
            "fan_2": {"power": "off", "level": 0},
            "alarm": {"power": "off", "mode": 0},
            "temperature_humidity": {"temperature_c": 27.5, "humidity_percent": 48.2, "age_ms": 120},
            "illuminance": {"illuminance_lux": 320, "age_ms": 120},
        }
        self.cache: "OrderedDict[str, Tuple[str, Dict[str, Any]]]" = OrderedDict()
        self.mode = "normal"
        self.dance_lighting = {
            "active": False,
            "duration_sec": 18,
            "elapsed_ms": 0,
            "remaining_ms": 0,
            "interval_ms": 600,
            "online_light_count": 3,
            "reason": "idle",
        }


class MockHandler(BaseHTTPRequestHandler):
    server_version = "XiaohongSEMock/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> MockState:
        return self.server.mock_state  # type: ignore[attr-defined]

    @property
    def token(self) -> str:
        return self.server.mock_token  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == "Bearer " + self.token

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _read_payload(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        if not self._authorized():
            self._json(401, {"code": 1006, "execution_state": "failed", "message": "unauthorized"})
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/api/v1/health":
            self._json(200, {
                "service": "xiaohong-se-home-gateway",
                "status": "ready",
                "firmware_version": "mock-1.0",
                "wifi_connected": True,
                "hub_ready": True,
                "connected_module_count": 8,
                "connected_modules": ["0x01", "0x03", "0x04", "0x06", "0x07", "0x08", "0x09", "0x0A"],
                "current_mode": self.state.mode,
                "dance_lighting_active": self.state.dance_lighting["active"],
            })
            return
        if parsed.path == "/api/v1/dance-lighting/status":
            self._json(200, {"code": 0, "message": "ok", **self.state.dance_lighting})
            return
        if parsed.path == "/api/v1/mode":
            self._json(200, {
                "code": 0,
                "message": "ok",
                "mode": self.state.mode,
                "mode_value": {"normal": 0, "home": 1, "away": 2}[self.state.mode],
                "preset": 0,
                "cause_mask": 0,
                "alarm_active": False,
                "fan_auto_inhibit": False,
                "automation_location": "xiaohong_se",
                "trend": {"valid": False, "window_seconds": 60},
                "thresholds": {},
            })
            return
        if parsed.path == "/api/v1/device/state":
            device_id = parse_qs(parsed.query).get("device_id", [""])[0]
            state = self.state.devices.get(device_id)
            if state is None:
                self._json(404, {"code": 1002, "message": "unknown device"})
            else:
                self._json(200, {"code": 0, "message": "ok", "device_id": device_id, "online": True, "state": state})
            return
        self._json(404, {"code": 1002, "message": "unknown path"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._json(401, {"code": 1006, "execution_state": "failed", "message": "unauthorized"})
            return
        try:
            payload = self._read_payload()
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"code": 1001, "execution_state": "failed", "message": "bad json"})
            return
        if self.path == "/api/v1/device/control":
            self._control_device(payload)
            return
        if self.path == "/api/v1/scene/control":
            self._control_scene(payload)
            return
        self._json(404, {"code": 1002, "execution_state": "failed", "message": "unknown path"})

    def _cache_result(self, request_id: str, payload: Dict[str, Any], result: Dict[str, Any]) -> None:
        signature = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.state.lock:
            previous = self.state.cache.get(request_id)
            if previous and previous[0] != signature:
                raise ValueError("request id conflict")
            self.state.cache[request_id] = (signature, result)
            while len(self.state.cache) > 64:
                self.state.cache.popitem(last=False)

    def _cached(self, request_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        signature = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.state.lock:
            previous = self.state.cache.get(request_id)
        if not previous:
            return None
        if previous[0] != signature:
            return {"request_id": request_id, "code": 1007, "execution_state": "failed", "message": "request_id conflict"}
        return previous[1]

    def _control_device(self, payload: Dict[str, Any]) -> None:
        request_id = payload.get("request_id", "")
        cached = self._cached(request_id, payload)
        if cached:
            self._json(409 if cached.get("code") == 1007 else 200, cached)
            return
        device_id = payload.get("device_id")
        action = payload.get("action")
        state = self.state.devices.get(device_id)
        if state is None:
            self._json(404, {"request_id": request_id, "code": 1002, "execution_state": "failed", "message": "unknown device"})
            return
        if device_id.startswith("light_") and action in ("turn_on", "turn_off", "set_color"):
            if action == "set_color":
                params = payload.get("params") or {}
                state.update({"power": "on", "rgb": [params.get("r"), params.get("g"), params.get("b")], "brightness": params.get("brightness")})
            else:
                state["power"] = "on" if action == "turn_on" else "off"
        elif device_id.startswith("fan_") and action in ("turn_on", "turn_off"):
            state.update({"power": "on" if action == "turn_on" else "off", "level": 1 if action == "turn_on" else 0})
        elif device_id == "alarm" and action in ("turn_on", "turn_off", "set_mode"):
            mode_names = {
                "off": 0,
                "light_on": 1,
                "light_blink": 2,
                "sound_only": 3,
                "sound_light": 4,
                "short_beep": 5,
            }
            if action == "set_mode":
                mode_name = (payload.get("params") or {}).get("mode")
                if mode_name not in mode_names:
                    self._json(400, {"request_id": request_id, "code": 1001, "execution_state": "failed", "message": "bad alarm mode"})
                    return
            else:
                mode_name = "sound_light" if action == "turn_on" else "off"
            state.update({
                "power": "off" if mode_name == "off" else "on",
                "alarm": mode_name != "off",
                "mode": mode_names[mode_name],
                "mode_name": mode_name,
            })
        else:
            self._json(404, {"request_id": request_id, "code": 1002, "execution_state": "failed", "message": "unsupported action"})
            return
        result = {"request_id": request_id, "code": 0, "message": "ok", "execution_state": "succeeded", "device_state": dict(state)}
        self._cache_result(request_id, payload, result)
        self._json(200, result)

    def _control_scene(self, payload: Dict[str, Any]) -> None:
        request_id = payload.get("request_id", "")
        cached = self._cached(request_id, payload)
        if cached:
            self._json(409 if cached.get("code") == 1007 else 200, cached)
            return
        scene_id = payload.get("scene_id")
        if scene_id == "dance_lighting":
            duration = max(15, min(20, int(payload.get("duration_sec", 18))))
            interval = max(400, min(1000, int(payload.get("interval_ms", 600))))
            self.state.dance_lighting.update({
                "active": True,
                "duration_sec": duration,
                "elapsed_ms": 0,
                "remaining_ms": duration * 1000,
                "interval_ms": interval,
                "reason": "running",
            })
            result = {
                "request_id": request_id,
                "code": 0,
                "message": "dance lighting started",
                "execution_state": "succeeded",
                "scene_state": {
                    "scene_id": scene_id,
                    "active": True,
                    "duration_sec": duration,
                    "interval_ms": interval,
                    "online_light_count": 3,
                    "reason": "running",
                    "targets": 3,
                    "succeeded": 3,
                    "failed": 0,
                    "timed_out": 0,
                },
            }
            self._cache_result(request_id, payload, result)
            self._json(200, result)
            return
        if scene_id == "dance_lighting_stop":
            self.state.dance_lighting.update({
                "active": False,
                "elapsed_ms": 0,
                "remaining_ms": 0,
                "reason": "manual_stop",
            })
            result = {
                "request_id": request_id,
                "code": 0,
                "message": "dance lighting stopped",
                "execution_state": "succeeded",
                "scene_state": {
                    "scene_id": "dance_lighting",
                    "active": False,
                    "reason": "manual_stop",
                },
            }
            self._cache_result(request_id, payload, result)
            self._json(200, result)
            return
        scenes = {
            "all_lights_on": [("light_1", "on"), ("light_2", "on"), ("light_3", "on")],
            "all_lights_off": [("light_1", "off"), ("light_2", "off"), ("light_3", "off")],
            "all_fans_on": [("fan_1", "on"), ("fan_2", "on")],
            "all_fans_off": [("fan_1", "off"), ("fan_2", "off")],
            "bright_mode": [("light_1", "on"), ("light_2", "on"), ("light_3", "on")],
            "ambient_mode": [("light_1", "on"), ("light_2", "on"), ("light_3", "on")],
            "ventilation_mode": [("fan_1", "on"), ("fan_2", "on")],
            "normal_mode": [],
            "home_mode": [],
            "away_mode": [
                ("light_1", "off"), ("light_2", "off"), ("light_3", "off"),
                ("fan_1", "off"), ("fan_2", "off"), ("alarm", "off"),
            ],
        }
        targets = scenes.get(scene_id)
        if targets is None:
            self._json(404, {"request_id": request_id, "code": 1002, "execution_state": "failed", "message": "unknown scene"})
            return
        for device_id, power in targets:
            self.state.devices[device_id]["power"] = power
        if scene_id in ("normal_mode", "home_mode", "away_mode"):
            self.state.mode = scene_id.removesuffix("_mode")
        result = {
            "request_id": request_id,
            "code": 0,
            "message": "ok",
            "execution_state": "succeeded",
            "scene_state": {"scene_id": scene_id, "targets": len(targets), "succeeded": len(targets), "failed": 0, "timed_out": 0},
        }
        self._cache_result(request_id, payload, result)
        self._json(200, result)


def make_server(host: str, port: int, token: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), MockHandler)
    server.mock_state = MockState()  # type: ignore[attr-defined]
    server.mock_token = token  # type: ignore[attr-defined]
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="小鸿 SE HTTP 接口模拟器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--token", default="dev-token")
    args = parser.parse_args()
    server = make_server(args.host, args.port, args.token)
    print("mock server: http://%s:%d" % (args.host, server.server_port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
