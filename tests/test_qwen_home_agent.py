import json
import unittest
from types import SimpleNamespace

from a3_bridge.models import BridgeReply, ParsedIntent
from a3_bridge.qwen_home_agent import QwenHomeAgent, SYSTEM_PROMPT


class FakeBridge:
    def __init__(self):
        self.commands = []
        self.parser = FakeParser()
        self.config = SimpleNamespace(
            rooms={
                "living_room": {
                    "display_name": "客厅",
                    "aliases": ["客厅", "大厅"],
                    "fan": "fan_1",
                },
                "master_bedroom": {
                    "display_name": "主卧",
                    "aliases": ["主卧", "主卧室"],
                    "fan": "fan_2",
                },
            }
        )

    def handle_text(self, text):
        self.commands.append(text)
        return BridgeReply(
            "客厅灯已打开。",
            ParsedIntent("device_control", text, device_id="light_1", action="turn_on"),
            {"execution_state": "succeeded", "code": 0},
        )

    @staticmethod
    def _device_name(device_id):
        return {"light_1": "客厅灯", "fan_2": "主卧风扇"}.get(device_id, device_id)


class FakeParser:
    def __init__(self, intent=None):
        self.intent = intent

    def parse(self, text):
        return self.intent or ParsedIntent("unsupported", text)

    def correct(self, text):
        return text.strip()

    @staticmethod
    def _normalize(text):
        return text.strip().lower()


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, messages, tools=None, on_text_delta=None):
        self.requests.append((messages, tools, on_text_delta))
        response = self.responses.pop(0)
        if on_text_delta and response.get("content"):
            on_text_delta(response["content"])
        return response


class QwenHomeAgentTest(unittest.TestCase):
    def test_system_prompt_positions_robot_housekeeper(self):
        self.assertIn("远征机器人管家", SYSTEM_PROMPT)
        self.assertIn("不要称呼用户为“主人”", SYSTEM_PROMPT)

    def test_chat_without_tool(self):
        bridge = FakeBridge()
        client = FakeClient([{"content": "你好呀。", "tool_calls": []}])
        agent = QwenHomeAgent(bridge, client)
        self.assertEqual(agent.handle("a3", "你好"), "你好呀。")
        self.assertEqual(bridge.commands, [])

    def test_plain_chat_streams_text(self):
        bridge = FakeBridge()
        client = FakeClient([{"content": "今天天气不错。", "tool_calls": []}])
        agent = QwenHomeAgent(bridge, client)
        chunks = []
        answer = agent.handle("a3", "今天天气不错", on_chat_delta=chunks.append)
        self.assertEqual(answer, "今天天气不错。")
        self.assertEqual(chunks, ["今天天气不错。"])
        self.assertIsNone(client.requests[0][1])

    def test_tool_result_is_sent_back_for_natural_reply(self):
        call = {
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "execute_home_command",
                "arguments": json.dumps({"command": "打开客厅灯"}, ensure_ascii=False),
            },
        }
        bridge = FakeBridge()
        client = FakeClient(
            [
                {"content": "", "tool_calls": [call]},
            ]
        )
        agent = QwenHomeAgent(bridge, client)
        answer = agent.handle("a3", "屋里有点暗")
        self.assertEqual(answer, "客厅灯已打开。")
        self.assertEqual(bridge.commands, ["打开客厅灯"])
        self.assertEqual(len(client.requests), 1)
        self.assertIsNotNone(client.requests[0][1])

    def test_known_home_command_bypasses_qwen(self):
        bridge = FakeBridge()
        bridge.parser = FakeParser(
            ParsedIntent("device_control", "打开客厅灯", device_id="light_1", action="turn_on")
        )
        client = FakeClient([])
        agent = QwenHomeAgent(bridge, client)
        self.assertEqual(agent.handle("a3", "打开客厅灯"), "客厅灯已打开。")
        self.assertEqual(bridge.commands, ["打开客厅灯"])
        self.assertEqual(client.requests, [])

    def test_asr_correction_reaches_deterministic_parser(self):
        bridge = FakeBridge()
        bridge.parser = FakeParser(
            ParsedIntent("device_control", "打开客厅灯", device_id="light_1", action="turn_on")
        )
        bridge.parser.correct = lambda text: text.replace("客庭", "客厅")
        client = FakeClient([])
        agent = QwenHomeAgent(bridge, client)
        self.assertEqual(agent.handle("a3", "打开客庭灯"), "客厅灯已打开。")
        self.assertEqual(bridge.commands, ["打开客厅灯"])

    def test_short_pronoun_follow_up_uses_last_device(self):
        bridge = FakeBridge()
        bridge.parser = FakeParser(
            ParsedIntent("device_control", "打开客厅灯", device_id="light_1", action="turn_on")
        )
        client = FakeClient([])
        agent = QwenHomeAgent(bridge, client)
        self.assertEqual(agent.handle("a3", "打开客厅灯"), "客厅灯已打开。")
        bridge.parser.intent = ParsedIntent(
            "device_control", "关闭客厅灯", device_id="light_1", action="turn_off"
        )
        self.assertEqual(agent.handle("a3", "把它关掉"), "客厅灯已打开。")
        self.assertEqual(bridge.commands[-1], "关闭客厅灯")

    def test_explicit_target_never_uses_previous_context(self):
        bridge = FakeBridge()
        bridge.parser = FakeParser(
            ParsedIntent("device_control", "打开客厅灯", device_id="light_1", action="turn_on")
        )
        agent = QwenHomeAgent(bridge, FakeClient([]))
        agent.handle("a3", "打开客厅灯")
        self.assertEqual(
            agent._resolve_contextual_command("a3", "关闭主卧风扇"),
            "关闭主卧风扇",
        )

    def test_room_answer_completes_pending_fan_request(self):
        bridge = FakeBridge()
        clarify = ParsedIntent(
            "clarify",
            "我有点热",
            params={"clarification": "fan_room", "action": "turn_on"},
            message="你现在在客厅还是主卧？",
        )
        bridge.parser = FakeParser(clarify)
        agent = QwenHomeAgent(bridge, FakeClient([]))
        agent._remember_pending_clarification("a3", clarify)
        self.assertEqual(
            agent._resolve_pending_clarification("a3", "主卧"),
            "打开主卧风扇",
        )


if __name__ == "__main__":
    unittest.main()
