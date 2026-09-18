"""A3 full-takeover microphone and wake-up probe.

Run this only after the robot has been switched to ``only_voice`` and rebooted.
It records one VAD-delimited utterance from the built-in microphone as raw
16 kHz, 16-bit, mono, little-endian PCM.
"""

import argparse
import datetime as dt
import logging
from pathlib import Path

import rclpy
from rclpy.node import Node
from rclpy.qos import (
    QoSDurabilityPolicy,
    QoSHistoryPolicy,
    QoSProfile,
    QoSReliabilityPolicy,
)
from ros2_plugin_proto.msg import RosMsgWrapper

from aimdk.protocol.agent.agent_data_pb2 import (
    AudioVADState,
    ProcessedAudioOutput,
    WakeUpResult,
)


AUDIO_TOPIC = "/agent/process_audio_output/pb_3Aaimdk_2Eprotocol_2EProcessedAudioOutput"
WAKEUP_TOPIC = "/agent/wakeup/pb_3Aaimdk_2Eprotocol_2EWakeUpResult"
MAX_AUDIO_BYTES = 16000 * 2 * 30


class AudioProbe(Node):
    def __init__(self, output_dir: Path, once: bool) -> None:
        super().__init__("a3_home_audio_probe")
        self.output_dir = output_dir
        self.once = once
        self.complete = False
        self.buffer = bytearray()
        self.recording = False
        self.active_stream_id = None
        self.seen_stream_ids = set()

        audio_qos = QoSProfile(
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
        )
        wakeup_qos = QoSProfile(
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=QoSReliabilityPolicy.RELIABLE,
            durability=QoSDurabilityPolicy.VOLATILE,
        )
        self.create_subscription(RosMsgWrapper, AUDIO_TOPIC, self._on_audio, audio_qos)
        self.create_subscription(RosMsgWrapper, WAKEUP_TOPIC, self._on_wakeup, wakeup_qos)
        self.get_logger().info("等待 A3 内置麦克风降噪音频和唤醒事件")

    @staticmethod
    def _payload(message: RosMsgWrapper) -> bytes:
        return b"".join(message.data)

    def _on_wakeup(self, message: RosMsgWrapper) -> None:
        if message.serialization_type != "pb":
            return
        result = WakeUpResult()
        result.ParseFromString(self._payload(message))
        self.get_logger().info(
            f"收到唤醒事件 keyword={result.keyword} "
            f"confidence={result.confidence:.3f} success={result.is_success}"
        )

    def _on_audio(self, message: RosMsgWrapper) -> None:
        if message.serialization_type != "pb":
            return
        audio = ProcessedAudioOutput()
        audio.ParseFromString(self._payload(message))
        if audio.stream_id not in self.seen_stream_ids:
            self.seen_stream_ids.add(audio.stream_id)
            self.get_logger().info(
                f"检测到麦克风音频流 stream_id={audio.stream_id}，"
                f"首帧状态={audio.vad_state}，数据={len(audio.audio_data)} bytes"
            )

        # A3 文档定义内置麦为 1，但部分麦克风阵列会以上游外置流 2 输出。
        # 完全接管验证阶段同时接受两者，以实际 VAD 流为准。
        if audio.stream_id not in (1, 2):
            return

        if audio.vad_state == AudioVADState.AUDIO_VAD_STATE_BEGIN:
            self.buffer.clear()
            self.recording = True
            self.active_stream_id = audio.stream_id
            self.get_logger().info(f"检测到语音开始 stream_id={audio.stream_id}")
        elif audio.vad_state == AudioVADState.AUDIO_VAD_STATE_PROCESSING:
            if self.active_stream_id == audio.stream_id:
                self._append(audio.audio_data)
        elif audio.vad_state == AudioVADState.AUDIO_VAD_STATE_END:
            if self.active_stream_id == audio.stream_id:
                self._append(audio.audio_data)
                if self.recording and self.buffer:
                    self._save(audio.stream_id)
                self.recording = False
                self.active_stream_id = None
        elif audio.vad_state == AudioVADState.AUDIO_VAD_STATE_NONE:
            self.recording = False
            self.buffer.clear()

    def _append(self, data: bytes) -> None:
        if not self.recording or not data:
            return
        remaining = MAX_AUDIO_BYTES - len(self.buffer)
        if remaining > 0:
            self.buffer.extend(data[:remaining])

    def _save(self, stream_id: int) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = self.output_dir / (f"mic_{stream_id}_{timestamp}.pcm")
        path.write_bytes(bytes(self.buffer))
        duration = len(self.buffer) / 32000.0
        self.get_logger().info(f"已保存 {path}，时长约 {duration:.2f} 秒")
        self.complete = self.once


def main() -> None:
    parser = argparse.ArgumentParser(description="验证 A3 only_voice 音频和唤醒 Topic")
    parser.add_argument("--output-dir", default="logs/audio_probe")
    parser.add_argument("--once", action="store_true", help="保存第一句话后退出")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    rclpy.init()
    node = AudioProbe(Path(args.output_dir), args.once)
    try:
        while rclpy.ok() and not node.complete:
            rclpy.spin_once(node, timeout_sec=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
