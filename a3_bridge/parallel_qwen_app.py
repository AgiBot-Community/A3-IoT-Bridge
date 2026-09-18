"""Parallel A3 ASR -> Qwen -> Xiaohong SE -> interrupting A3 TTS bridge.

This is a field-test workaround for regular A3 firmware that reports
``only_voice`` but still runs its built-in conversation pipeline.
"""

import argparse
import json
import logging
import queue
import threading
import urllib.error
import urllib.request
import uuid

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from ros2_plugin_proto.msg import RosMsgWrapper

from aimdk.protocol.agent.asr_result_pb2 import AsrResult
from aimdk.protocol.interaction.tts_service_pb2 import (
    TTSStatus,
    TTSStatusType_Begin,
    TTSStatusType_InQue,
    TTSStatusType_Playing,
)

from .bridge import HomeBridge
from .config import load_config
from .qwen_home_agent import QwenHomeAgent, QwenStreamClient
from .voice_utils import remove_wake_word


ASR_TOPIC = "/AsrResult/pb_3Aaimdk_2Eprotocol_2EAsrResult"
TTS_STATUS_TOPIC = "/interaction/tts_status/pb_3Aaimdk_2Eprotocol_2ETTSStatus"
TTS_RPC_ROOT = "http://10.42.10.10:59301/rpc/aimdk.protocol.TTSService"


class A3TTS:
    def __init__(self, timeout_seconds: float = 8.0):
        self.timeout_seconds = timeout_seconds

    def _post(self, method: str, payload: dict) -> dict:
        request = urllib.request.Request(
            TTS_RPC_ROOT + "/" + method,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def interrupt_and_play(self, text: str) -> dict:
        # Stop any native fallback that has already begun, then stay one point above
        # the native interaction priority (600) so a late fallback cannot interrupt us.
        try:
            self._post("StopTTS", {})
        except Exception as exc:
            logging.warning("StopTTS failed before replacement reply: %s", exc)
        return self._post(
            "PlayTTS",
            {
                "text": text,
                "priority_level": "INTERACTION_L6",
                "priority_weight": 1,
                "domain": "a3_home_qwen",
                "trace_id": "a3-home-" + str(uuid.uuid4()),
                "is_interrupted": True,
            },
        )

    def stop_all(self) -> None:
        self._post("StopTTS", {})

    def stop_trace(self, trace_id: str) -> None:
        if trace_id:
            self._post("StopTTSTraceId", {"trace_id": trace_id})


class ParallelQwenApp(Node):
    def __init__(self, home_agent: QwenHomeAgent, tts: A3TTS):
        super().__init__("a3_home_parallel_qwen")
        self.home_agent = home_agent
        self.tts = tts
        self.work_queue = queue.Queue(maxsize=2)
        self.stop_event = threading.Event()
        self.seen_events = set()
        self.suppressed_tts_traces = set()
        qos = QoSProfile(
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
        )
        self.subscription = self.create_subscription(
            RosMsgWrapper, ASR_TOPIC, self._on_asr, qos
        )
        self.tts_status_subscription = self.create_subscription(
            RosMsgWrapper, TTS_STATUS_TOPIC, self._on_tts_status, qos
        )
        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()
        self.get_logger().info("千问旁路语音接管已启动")

    @staticmethod
    def _payload(message: RosMsgWrapper) -> bytes:
        return b"".join(message.data)

    def _on_asr(self, message: RosMsgWrapper) -> None:
        if message.serialization_type != "pb":
            return
        result = AsrResult()
        result.ParseFromString(self._payload(message))
        text = remove_wake_word(result.text.strip())
        event_id = result.event_id or str(uuid.uuid4())
        if not result.is_final or not text or event_id in self.seen_events:
            return
        self.seen_events.add(event_id)
        if len(self.seen_events) > 100:
            self.seen_events.clear()
            self.seen_events.add(event_id)
        self.get_logger().info(f"A3 ASR最终结果：{text}")
        try:
            self.work_queue.put_nowait((event_id, text))
        except queue.Full:
            self.get_logger().warning("上一条对话仍在处理，本句已忽略")

    def _on_tts_status(self, message: RosMsgWrapper) -> None:
        if message.serialization_type != "pb":
            return
        status = TTSStatus()
        status.ParseFromString(self._payload(message))
        if status.domain != "omnissdk":
            return
        if status.tts_status not in (
            TTSStatusType_Begin,
            TTSStatusType_Playing,
            TTSStatusType_InQue,
        ):
            return
        trace_id = status.trace_id
        if not trace_id or trace_id in self.suppressed_tts_traces:
            return
        self.suppressed_tts_traces.add(trace_id)
        if len(self.suppressed_tts_traces) > 100:
            self.suppressed_tts_traces.clear()
            self.suppressed_tts_traces.add(trace_id)
        self.get_logger().info(
            f"拦截原生TTS trace_id={trace_id} status={status.tts_status}"
        )
        threading.Thread(
            target=self._stop_native_trace,
            args=(trace_id,),
            daemon=True,
        ).start()

    def _stop_native_trace(self, trace_id: str) -> None:
        try:
            self.tts.stop_trace(trace_id)
        except Exception as exc:
            self.get_logger().warning(f"停止原生TTS失败 trace_id={trace_id}: {exc}")

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                event_id, text = self.work_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                # Clear any native fallback already queued for the same utterance.
                try:
                    self.tts.stop_all()
                except Exception as exc:
                    self.get_logger().warning(f"预停止原生TTS失败：{exc}")
                answer = self.home_agent.handle("a3-robot", text)
                self.get_logger().info(f"千问最终回复：{answer}")
                result = self.tts.interrupt_and_play(answer)
                success = result.get("is_sucess", result.get("is_success", False))
                self.get_logger().info(f"A3 TTS替换播报 success={success}")
            except Exception as exc:
                self.get_logger().error(f"千问对话或播报失败：{exc}")
            finally:
                self.work_queue.task_done()

    def close(self) -> None:
        self.stop_event.set()
        self.worker.join(timeout=3.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="A3千问旁路自然对话与家居控制")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    bridge = HomeBridge(load_config(args.config))
    agent = QwenHomeAgent(bridge, QwenStreamClient.from_env())
    rclpy.init()
    node = ParallelQwenApp(agent, A3TTS())
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.close()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
