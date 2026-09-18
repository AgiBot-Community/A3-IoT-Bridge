import unittest

from a3_bridge.linksoul_app import (
    is_incomplete_asr_fragment,
    is_wake_word_only,
    merge_short_asr_fragment,
    should_suppress_post_reply_fragment,
)

from a3_bridge.voice_utils import parse_vosk_result, remove_wake_word, reply_audio_key


class VoiceUtilsTests(unittest.TestCase):
    def test_merges_short_asr_action_fragment(self):
        self.assertEqual(merge_short_asr_fragment("开", "客厅风扇"), "打开客厅风扇")
        self.assertEqual(merge_short_asr_fragment("关", "主卧灯"), "关闭主卧灯")

    def test_filters_wake_word_only(self):
        self.assertTrue(is_wake_word_only("远征远征。"))
        self.assertFalse(is_wake_word_only("远征远征，打开客厅灯"))

    def test_detects_incomplete_domain_fragment(self):
        self.assertTrue(is_incomplete_asr_fragment("把主卧灯光调成"))
        self.assertTrue(is_incomplete_asr_fragment("把主卧灯光"))
        self.assertTrue(is_incomplete_asr_fragment("关闭全部"))
        self.assertFalse(is_incomplete_asr_fragment("把主卧灯光调成蓝色"))
        self.assertFalse(is_incomplete_asr_fragment("关闭全部灯光"))
        self.assertFalse(is_incomplete_asr_fragment("打开明亮模式"))
        self.assertFalse(is_incomplete_asr_fragment("开启氛围模式"))
        self.assertFalse(is_incomplete_asr_fragment("开启通风模式"))

    def test_suppresses_post_reply_noise_but_keeps_home_follow_up(self):
        answer = "收到，声光报警器已经开启。"
        self.assertTrue(should_suppress_post_reply_fragment("嗯这个", answer, 0.36))
        self.assertTrue(
            should_suppress_post_reply_fragment("声光报警器已经开启", answer, 2.0)
        )
        self.assertFalse(should_suppress_post_reply_fragment("主卧", answer, 0.5))
        self.assertFalse(should_suppress_post_reply_fragment("把它关掉", answer, 0.5))
        self.assertFalse(should_suppress_post_reply_fragment("蓝色", answer, 0.5))

    def test_parses_and_compacts_chinese_result(self):
        self.assertEqual(parse_vosk_result('{"text": "打开 客厅 灯"}'), "打开客厅灯")

    def test_removes_wake_word(self):
        self.assertEqual(remove_wake_word("远征远征，打开客厅灯"), "打开客厅灯")

    def test_keeps_command_without_wake_word(self):
        self.assertEqual(remove_wake_word("我有点热"), "我有点热")

    def test_classifies_dynamic_reply(self):
        self.assertEqual(reply_audio_key("客厅灯已打开。"), "success")
        self.assertEqual(reply_audio_key("客厅灯当前离线。"), "offline")
        self.assertEqual(reply_audio_key("暂时无法连接智能家居中控。"), "gateway")


if __name__ == "__main__":
    unittest.main()
