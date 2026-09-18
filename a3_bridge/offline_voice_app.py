"""Offline A3 voice-to-Xiaohong-SE bridge using the on-device Vosk engine.

The robot must be in ``only_voice`` mode. A3 supplies denoised, VAD-labelled
16 kHz S16LE mono PCM; this node recognizes each utterance, calls HomeBridge,
and optionally plays a short prerecorded reply through the A3 speaker.
"""

import argparse
import logging
import queue
import threading
import time
import uuid
import wave
from pathlib import Path
from typing import Optional

import rclpy
from audio_msgs.msg import AudioData, AudioInfo, AudioPlayback
from audio_msgs.srv import AbandonAudioFocus, RequestAudioFocus
from rclpy.node import Node
from rclpy.qos import (
    QoSDurabilityPolicy,
    QoSHistoryPolicy,
    QoSProfile,
    QoSReliabilityPolicy,
)
from ros2_plugin_proto.msg import RosMsgWrapper
from vosk import KaldiRecognizer, Model, SetLogLevel

from aimdk.protocol.agent.agent_data_pb2 import (
    AudioVADState,
    ProcessedAudioOutput,
    WakeUpResult,
)
from aimdk.protocol.agent.asr_result_pb2 import AsrResult

from .bridge import HomeBridge
from .config import load_config
from .voice_utils import parse_vosk_result, remove_wake_word, reply_audio_key


AUDIO_TOPIC = "/agent/process_audio_output/pb_3Aaimdk_2Eprotocol_2EProcessedAudioOutput"
WAKEUP_TOPIC = "/agent/wakeup/pb_3Aaimdk_2Eprotocol_2EWakeUpResult"
ASR_TOPIC = "/AsrResult/pb_3Aaimdk_2Eprotocol_2EAsrResult"
PLAYBACK_TOPIC = "/audiohal/audio/playback"
REQUEST_FOCUS_SERVICE = "/audio_5Fmsgs/srv/RequestAudioFocus"
ABANDON_FOCUS_SERVICE = "/audio_5Fmsgs/srv/AbandonAudioFocus"
SAMPLE_RATE = 16000
MAX_AUDIO_BYTES = SAMPLE_RATE * 2 * 30
MIN_AUDIO_BYTES = SAMPLE_RATE * 2 // 5
PKG_NAME = "a3_home_voice"


class OfflineVoiceApp(Node):
    def __init__(
        self,
        bridge: HomeBridge,
        model_path: Path,
        reply_audio_dir: Optional[Path],
        always_listen: bool,
        wake_window_seconds: float,
        once: bool,
    ) -> None:
        super().__init__("a3_home_offline_voice")
        SetLogLevel(-1)
        self.bridge = bridge
        self.model = Model(str(model_path))
        self.reply_audio_dir = reply_audio_dir
        self.always_listen = always_listen
        self.wake_window_seconds = wake_window_seconds
        self.once = once
        self.complete = False
        self.recording = False
        self.speaking = False
        self.buffer = bytearray()
        self.wake_deadline = 0.0
        self.work_queue = queue.Queue(maxsize=2)
        self.stop_event = threading.Event()

        audio_qos = QoSProfile(
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
        )
        wake_qos = QoSProfile(
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=QoSReliabilityPolicy.RELIABLE,
            durability=QoSDurabilityPolicy.VOLATILE,
        )
        self.create_subscription(RosMsgWrapper, AUDIO_TOPIC, self._on_audio, audio_qos)
        self.create_subscription(RosMsgWrapper, ASR_TOPIC, self._on_asr, audio_qos)
        self.create_subscription(RosMsgWrapper, WAKEUP_TOPIC, self._on_wakeup, wake_qos)
        self.playback_publisher = self.create_publisher(AudioPlayback, PLAYBACK_TOPIC, 10)
        self.request_focus_client = self.create_client(RequestAudioFocus, REQUEST_FOCUS_SERVICE)
        self.abandon_focus_client = self.create_client(AbandonAudioFocus, ABANDON_FOCUS_SERVICE)

        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()
        mode = "免唤醒调试" if always_listen else "唤醒后识别"
        self.get_logger().info(f"Vosk 已加载，运行模式：{mode}")

    @staticmethod
    def _payload(message: RosMsgWrapper) -> bytes:
        return b"".join(message.data)

    def _on_wakeup(self, message: RosMsgWrapper) -> None:
        if message.serialization_type != "pb" or self.speaking:
            return
        result = WakeUpResult()
        result.ParseFromString(self._payload(message))
        if result.is_success:
            self.wake_deadline = time.monotonic() + self.wake_window_seconds
            self.get_logger().info(
                f"已唤醒 keyword={result.keyword} confidence={result.confidence:.3f}"
            )

    def _on_asr(self, message: RosMsgWrapper) -> None:
        """Use A3's final ASR text when raw processed audio is unavailable."""
        if message.serialization_type != "pb" or self.speaking:
            return
        result = AsrResult()
        result.ParseFromString(self._payload(message))
        text = remove_wake_word(result.text.strip())
        if not result.is_final or not text:
            return
        self.get_logger().info(f"A3 ASR最终结果：{text}")
        try:
            self.work_queue.put_nowait(text)
        except queue.Full:
            self.get_logger().warning("上一条语音仍在处理，本句话已忽略")

    def _on_audio(self, message: RosMsgWrapper) -> None:
        if message.serialization_type != "pb" or self.speaking:
            return
        audio = ProcessedAudioOutput()
        audio.ParseFromString(self._payload(message))
        if audio.stream_id != 1:
            return

        if audio.vad_state == AudioVADState.AUDIO_VAD_STATE_BEGIN:
            self.buffer.clear()
            self.recording = True
            self._append(audio.audio_data)
        elif audio.vad_state == AudioVADState.AUDIO_VAD_STATE_PROCESSING:
            self._append(audio.audio_data)
        elif audio.vad_state == AudioVADState.AUDIO_VAD_STATE_END:
            self._append(audio.audio_data)
            self._finish_utterance()
        elif audio.vad_state == AudioVADState.AUDIO_VAD_STATE_NONE:
            self.recording = False
            self.buffer.clear()

    def _append(self, data: bytes) -> None:
        if not self.recording or not data:
            return
        remaining = MAX_AUDIO_BYTES - len(self.buffer)
        if remaining > 0:
            self.buffer.extend(data[:remaining])

    def _finish_utterance(self) -> None:
        self.recording = False
        if len(self.buffer) < MIN_AUDIO_BYTES:
            self.buffer.clear()
            return
        pcm = bytes(self.buffer)
        self.buffer.clear()
        try:
            self.work_queue.put_nowait(pcm)
        except queue.Full:
            self.get_logger().warning("上一条语音仍在处理，本句话已忽略")

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                work_item = self.work_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if isinstance(work_item, str):
                    self._process_text(work_item)
                else:
                    self._process_utterance(work_item)
            except Exception as exc:  # keep the voice loop alive during field tests
                self.get_logger().error(f"处理语音失败：{exc}")
            finally:
                self.work_queue.task_done()

    def _process_utterance(self, pcm: bytes) -> None:
        recognizer = KaldiRecognizer(self.model, SAMPLE_RATE)
        recognizer.AcceptWaveform(pcm)
        text = remove_wake_word(parse_vosk_result(recognizer.FinalResult()))
        if not text:
            self.get_logger().info("未识别到有效文本")
            return
        self.get_logger().info(f"识别结果：{text}")

        self._process_text(text)

    def _process_text(self, text: str) -> None:

        if not self.always_listen and time.monotonic() > self.wake_deadline:
            self.get_logger().info("未处于唤醒窗口，忽略本句话")
            return

        reply = self.bridge.handle_text(text)
        self.get_logger().info(f"控制回复：{reply.text}")
        if self.reply_audio_dir is not None:
            self._play_reply(reply_audio_key(reply.text))
        self.wake_deadline = 0.0
        if self.once:
            self.complete = True

    def _wait_future(self, future, timeout_seconds: float):
        deadline = time.monotonic() + timeout_seconds
        while not future.done() and time.monotonic() < deadline and not self.stop_event.is_set():
            time.sleep(0.02)
        return future.result() if future.done() else None

    def _request_focus(self) -> bool:
        if not self.request_focus_client.wait_for_service(timeout_sec=2.0):
            self.get_logger().warning("音频焦点服务不可用")
            return False
        request = RequestAudioFocus.Request()
        request.focus_requester.pkg_name = PKG_NAME
        request.focus_requester.priority = 6
        request.focus_requester.priority_weight = 1
        response = self._wait_future(self.request_focus_client.call_async(request), 3.0)
        return bool(response and response.focus_response.focus_gain)

    def _abandon_focus(self) -> None:
        if not self.abandon_focus_client.wait_for_service(timeout_sec=1.0):
            return
        request = AbandonAudioFocus.Request()
        request.focus_requester.pkg_name = PKG_NAME
        request.focus_requester.priority = 6
        request.focus_requester.priority_weight = 1
        self._wait_future(self.abandon_focus_client.call_async(request), 2.0)

    def _play_reply(self, key: str) -> None:
        wav_path = self.reply_audio_dir / (key + ".wav")
        if not wav_path.is_file():
            self.get_logger().warning(f"缺少播报文件：{wav_path}")
            return
        with wave.open(str(wav_path), "rb") as wav_file:
            if (
                wav_file.getnchannels() != 1
                or wav_file.getsampwidth() != 2
                or wav_file.getframerate() != SAMPLE_RATE
                or wav_file.getcomptype() != "NONE"
            ):
                self.get_logger().error(f"播报 WAV 必须是 16kHz/16bit/单声道 PCM：{wav_path}")
                return
            pcm = wav_file.readframes(wav_file.getnframes())

        self.speaking = True
        try:
            if not self._request_focus():
                self.get_logger().warning("未取得音频焦点，跳过播报")
                return
            token_id = str(uuid.uuid4())
            chunk_bytes = SAMPLE_RATE * 2 // 10
            for offset in range(0, len(pcm), chunk_bytes):
                message = AudioPlayback()
                message.stamps = self.get_clock().now().to_msg()
                message.info = AudioInfo(
                    channels=1,
                    sample_rate=SAMPLE_RATE,
                    sample_format="S16LE",
                    coding_format="pcm",
                )
                message.data = AudioData(data=list(pcm[offset : offset + chunk_bytes]))
                message.pkg_name = PKG_NAME
                message.token_id = token_id
                self.playback_publisher.publish(message)
                time.sleep(0.09)
            time.sleep(0.2)
        finally:
            self._abandon_focus()
            self.speaking = False

    def close(self) -> None:
        self.stop_event.set()
        self.worker.join(timeout=3.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="A3 本地 Vosk 语音控制小鸿 SE")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--model", default="models/vosk-model-small-cn-0.22")
    parser.add_argument("--reply-audio-dir", default="assets/replies")
    parser.add_argument("--no-playback", action="store_true")
    parser.add_argument("--always-listen", action="store_true", help="调试模式：不要求唤醒事件")
    parser.add_argument("--wake-window", type=float, default=12.0)
    parser.add_argument("--once", action="store_true", help="执行第一条有效指令后退出")
    args = parser.parse_args()

    model_path = Path(args.model)
    if not model_path.is_dir():
        parser.error("Vosk 模型目录不存在：%s" % model_path)
    reply_audio_dir = None if args.no_playback else Path(args.reply_audio_dir)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    rclpy.init()
    node = OfflineVoiceApp(
        HomeBridge(load_config(args.config)),
        model_path,
        reply_audio_dir,
        args.always_listen,
        max(1.0, args.wake_window),
        args.once,
    )
    try:
        while rclpy.ok() and not node.complete:
            rclpy.spin_once(node, timeout_sec=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        node.close()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
