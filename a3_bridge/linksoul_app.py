import argparse
import logging
import os
import re
import signal
import threading
import time

from .bridge import HomeBridge
from .config import load_config
from .qwen_home_agent import QwenApiError, QwenHomeAgent, QwenStreamClient


def merge_short_asr_fragment(prefix: str, text: str) -> str:
    """Merge an isolated one-character action split by streaming ASR."""
    prefix = str(prefix or "").strip()
    text = str(text or "").strip()
    if prefix == "开":
        return "打开" + text
    if prefix == "关":
        return "关闭" + text
    if prefix == "打" and text.startswith("开"):
        return "打" + text
    return prefix + text


def normalize_asr_text(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?]", "", str(text or "").strip().lower())


def is_wake_word_only(text: str) -> bool:
    normalized = normalize_asr_text(text)
    return normalized in ("远征", "远征远征", "hiluka", "luka")


def is_incomplete_asr_fragment(text: str) -> bool:
    """Recognize fragments that should wait briefly for the next ASR event."""
    normalized = normalize_asr_text(text)
    if normalized in ("开", "关", "打"):
        return True
    if normalized.endswith(("调成", "换成", "设置成", "设成", "改成", "变成", "调到")):
        return True
    targets = ("灯", "灯光", "风扇", "报警器", "模式")
    actions = ("打开", "开启", "关闭", "关掉", "设置", "调成", "换成")
    colors = ("白色", "红色", "绿色", "蓝色", "暖色", "黄色")
    if normalized.startswith("把") and any(target in normalized for target in targets):
        if not any(action in normalized for action in actions) and not any(
            color in normalized for color in colors
        ):
            return True
    if normalized.startswith(("打开", "开启", "关闭", "关掉")) and not any(
        target in normalized for target in targets
    ):
        return True
    return False


def should_suppress_post_reply_fragment(
    text: str, previous_answer: str, elapsed_seconds: float
) -> bool:
    """Drop likely loudspeaker echo/noise without blocking useful follow-ups."""
    normalized = normalize_asr_text(text)
    answer = normalize_asr_text(previous_answer)
    if not normalized:
        return True
    home_terms = (
        "灯",
        "风扇",
        "模式",
        "报警",
        "警报",
        "温度",
        "湿度",
        "光照",
        "客厅",
        "主卧",
        "次卧",
        "打开",
        "开启",
        "关闭",
        "关掉",
        "撤销",
        "睡觉",
        "蓝色",
        "红色",
        "绿色",
        "暖色",
        "白色",
        "黄色",
    )
    if 0 <= elapsed_seconds <= 8.0 and len(normalized) >= 3 and normalized in answer:
        return True
    if any(term in normalized for term in home_terms):
        return False
    if 0 <= elapsed_seconds <= 1.2 and len(normalized) <= 6:
        return True
    return False


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    # SDK 的状态 Topic 日志量很大，保留连接和业务日志即可。
    logging.getLogger("linksoul_agentsdk.passive").setLevel(logging.WARNING)
    logging.getLogger("linksoul_agentsdk.client").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="LinkSoul ASR 回调到小鸿 SE 的 A3 本体桥接服务")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()

    try:
        from linksoul_agentsdk import AgentAuthCallback, AgentParam, AgentSdk, IdGenerator
        from linksoul_agentsdk.passive import Asr2LlmCallback, Asr2LlmResponse
    except ImportError as exc:
        raise SystemExit("请先执行 pip install -r requirements.txt") from exc

    app_id = os.getenv("LINKSOUL_APP_ID", "").strip()
    app_key = os.getenv("LINKSOUL_APP_KEY", "").strip()
    app_secret = os.getenv("LINKSOUL_APP_SECRET", "").strip()
    if not all((app_id, app_key, app_secret)):
        raise SystemExit("请设置 LINKSOUL_APP_ID、LINKSOUL_APP_KEY、LINKSOUL_APP_SECRET")

    bridge = HomeBridge(load_config(args.config))
    try:
        qwen_agent = QwenHomeAgent(bridge, QwenStreamClient.from_env())
        logging.info("已启用千问自然对话和家居工具调用")
    except QwenApiError:
        qwen_agent = None
        logging.warning("未配置千问，将使用本地家居指令解析")

    pending_fragments = {}
    pending_fragments_lock = threading.Lock()
    processed_events = {}
    recent_replies = {}

    class AuthCallback(AgentAuthCallback):
        def on_auth_state(self, current_app_id: str, code: int, msg: str) -> None:
            if code == 0:
                logging.info("LinkSoul 认证成功 app_id=%s", current_app_id)
            else:
                logging.error("LinkSoul 认证失败 code=%s msg=%s", code, msg)

    class HomeCallback(Asr2LlmCallback):
        def on_request(
            self,
            agent_id: str,
            event_id: str,
            text: str,
            param: AgentParam,
            response: Asr2LlmResponse,
        ) -> None:
            if not text:
                return
            started = time.monotonic()
            raw_text = text.strip()
            with pending_fragments_lock:
                previous_event_time = processed_events.get(event_id)
                if previous_event_time and started - previous_event_time <= 120:
                    logging.warning("忽略重复ASR事件 event_id=%s text=%s", event_id, raw_text)
                    response.on_interrupt(event_id, "chat", None)
                    response.on_llm_done(event_id)
                    return
                processed_events[event_id] = started
                if len(processed_events) > 256:
                    cutoff = started - 120
                    for old_event_id, event_time in list(processed_events.items()):
                        if event_time < cutoff:
                            processed_events.pop(old_event_id, None)
                recent_reply = recent_replies.get(agent_id)
                if recent_reply and should_suppress_post_reply_fragment(
                    raw_text,
                    recent_reply[0],
                    started - recent_reply[1],
                ):
                    logging.info(
                        "抑制播报后回声/短杂音 agent_id=%s event_id=%s text=%s elapsed_ms=%d",
                        agent_id,
                        event_id,
                        raw_text,
                        int((started - recent_reply[1]) * 1000),
                    )
                    response.on_interrupt(event_id, "chat", None)
                    response.on_llm_done(event_id)
                    return
                pending = pending_fragments.pop(agent_id, None)
                if pending and started - pending[1] <= 3.0:
                    raw_text = merge_short_asr_fragment(pending[0], raw_text)
                    logging.info(
                        "合并ASR短分段 agent_id=%s prefix=%s merged=%s",
                        agent_id,
                        pending[0],
                        raw_text,
                    )
                if is_wake_word_only(raw_text):
                    logging.info(
                        "忽略纯唤醒词 agent_id=%s event_id=%s text=%s",
                        agent_id,
                        event_id,
                        raw_text,
                    )
                    response.on_interrupt(event_id, "chat", None)
                    response.on_llm_done(event_id)
                    return
                if is_incomplete_asr_fragment(raw_text):
                    pending_fragments[agent_id] = (raw_text, started)
                    logging.info(
                        "暂存ASR未完成分段 agent_id=%s event_id=%s text=%s",
                        agent_id,
                        event_id,
                        raw_text,
                    )
                    response.on_interrupt(event_id, "chat", None)
                    response.on_llm_done(event_id)
                    return
            corrected_text = bridge.parser.correct(raw_text)
            logging.info(
                "收到语音文本 agent_id=%s event_id=%s text=%s corrected=%s",
                agent_id,
                event_id,
                raw_text,
                corrected_text,
            )
            item_id = IdGenerator.generate_item_id()
            response.on_interrupt(event_id, "chat", None)
            streamed = False

            def emit_chat_delta(chunk: str) -> None:
                nonlocal streamed
                if chunk:
                    response.on_llm_item_delta(event_id, item_id, chunk)
                    streamed = True

            if qwen_agent is not None:
                try:
                    answer = qwen_agent.handle(
                        agent_id,
                        corrected_text,
                        on_chat_delta=emit_chat_delta,
                    )
                except Exception as exc:
                    logging.exception("千问处理失败，回退到本地解析: %s", exc)
                    answer = bridge.handle_text(corrected_text).text
            else:
                answer = bridge.handle_text(corrected_text).text
            request_intent = bridge.parser.parse(corrected_text)
            # 内置舞蹈技能包自带开场语、音乐和表情。成功启动后不再让
            # LinkSoul 额外播报一句，避免两个音频源重叠抢占扬声器。
            suppress_start_reply = bool(
                request_intent.kind == "dance_control"
                and request_intent.action == "start"
                and bridge.dance.is_active
            )
            if not streamed and not suppress_start_reply:
                response.on_llm_item_delta(event_id, item_id, answer)
            response.on_llm_item_done(event_id, item_id)
            response.on_llm_done(event_id)
            if not suppress_start_reply and answer:
                with pending_fragments_lock:
                    recent_replies[agent_id] = (answer, time.monotonic())
            logging.info(
                "语音请求处理完成 event_id=%s streamed=%s suppress_start_reply=%s elapsed_ms=%d",
                event_id,
                streamed,
                suppress_start_reply,
                int((time.monotonic() - started) * 1000),
            )

    sdk = AgentSdk.create(
        url="wss://open.agibot.com/api/V1/open-portal/app/wss/agent-sdk",
        app_id=app_id,
        app_key=app_key,
        app_secret=app_secret,
    )
    sdk.register_auth(AuthCallback())
    sdk.register_asr2_llm(HomeCallback(sdk))
    sdk.initialize()

    # v1.4.0 SDK 的 WebSocket 接收线程是 daemon；主线程必须保持运行。
    stopped = threading.Event()

    def warm_gateway() -> None:
        try:
            health = bridge.health()
            logging.info(
                "小鸿 SE 预连接成功 status=%s modules=%s mode=%s endpoint=%s",
                health.get("status"),
                health.get("connected_module_count"),
                health.get("current_mode"),
                bridge.client.current_base_url,
            )
            result = bridge.reconcile_desired_mode()
            if result.get("status") not in ("no_saved_mode", "already_current"):
                logging.info("启动模式恢复检查 result=%s", result)
        except Exception as exc:
            logging.warning("小鸿 SE 启动预连接失败，将在首次请求时重试: %s", exc)

    threading.Thread(target=warm_gateway, daemon=True).start()

    def reconcile_mode() -> None:
        # 小鸿 SE 的模式掉电不保存。周期核对只负责恢复用户选择的模式，
        # 不在 A3 重复实现任何温湿度、人体或烟雾规则。
        while not stopped.wait(30.0):
            try:
                result = bridge.reconcile_desired_mode()
                if result.get("status") in ("restored", "restore_failed", "mode_query_failed"):
                    logging.info("模式恢复检查 result=%s", result)
            except Exception as exc:
                logging.warning("模式恢复检查失败: %s", exc)

    reconcile_thread = threading.Thread(target=reconcile_mode, daemon=True)
    reconcile_thread.start()

    def stop_service(signum=None, frame=None) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop_service)
    signal.signal(signal.SIGTERM, stop_service)
    logging.info("A3 智能家居桥接服务已启动")
    try:
        while not stopped.wait(1.0):
            pass
    finally:
        stopped.set()
        reconcile_thread.join(timeout=2.0)
        bridge.close()
        sdk.release()
        logging.info("A3 智能家居桥接服务已停止")


if __name__ == "__main__":
    main()
