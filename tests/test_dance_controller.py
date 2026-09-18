import unittest

from a3_bridge.config import DanceConfig
from a3_bridge.dance_controller import DanceController


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.active = False

    def control_scene(self, scene_id, request_id=None, params=None):
        self.calls.append((scene_id, request_id, params))
        self.active = scene_id == "dance_lighting"
        return {
            "code": 0,
            "execution_state": "succeeded",
            "scene_state": {
                "scene_id": scene_id,
                "active": self.active,
                "online_light_count": 3,
            },
        }

    def get_dance_lighting_status(self):
        return {
            "code": 0,
            "active": self.active,
            "reason": "running" if self.active else "manual_stop",
            "remaining_ms": 12000 if self.active else 0,
        }


class FakeSkillClient:
    def __init__(self):
        self.started = []
        self.stopped = []

    def start_skill(self, path, session_id):
        self.started.append((path, session_id))
        return {"header": {"code": 0}, "session_id": session_id}

    def stop_skill(self, session_id):
        self.stopped.append(session_id)
        return {"header": {"code": 0}, "session_id": session_id}


class DanceControllerTest(unittest.TestCase):
    def test_disabled_controller_never_calls_hardware(self):
        gateway = FakeGateway()
        skill = FakeSkillClient()
        controller = DanceController(DanceConfig(enabled=False), gateway, skill)
        result = controller.start()
        self.assertEqual(result["reason"], "safety_locked")
        self.assertEqual(gateway.calls, [])
        self.assertEqual(skill.started, [])

    def test_start_and_stop_coordinate_lighting_and_skill(self):
        gateway = FakeGateway()
        skill = FakeSkillClient()
        controller = DanceController(DanceConfig(enabled=True), gateway, skill)
        started = controller.start()
        self.assertEqual(started["execution_state"], "succeeded")
        self.assertTrue(controller.is_active)
        self.assertEqual(gateway.calls[0][0], "dance_lighting")
        self.assertEqual(gateway.calls[0][2]["duration_sec"], 18)
        self.assertEqual(len(skill.started), 1)
        stopped = controller.stop()
        self.assertEqual(stopped["execution_state"], "succeeded")
        self.assertFalse(controller.is_active)
        self.assertEqual(gateway.calls[-1][0], "dance_lighting_stop")
        self.assertEqual(skill.stopped, [skill.started[0][1]])

    def test_status_combines_local_and_se_state(self):
        gateway = FakeGateway()
        skill = FakeSkillClient()
        controller = DanceController(DanceConfig(enabled=True), gateway, skill)
        controller.start()
        status = controller.status()
        self.assertTrue(status["active"])
        self.assertEqual(status["remaining_ms"], 12000)
        controller.stop()


if __name__ == "__main__":
    unittest.main()
