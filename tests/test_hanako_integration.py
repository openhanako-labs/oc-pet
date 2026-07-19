"""Hanako WS 集成冒烟测试 — 验证核心链路

不需要 PySide6，不启动 GUI，直接测底层模块。
运行: python tests/test_hanako_integration.py
"""
import sys
import time
import logging
from pathlib import Path

# 添加项目根目录到 path
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("test")

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} — {detail}")


def test_rest_api():
    """测试 REST API 连通性"""
    print("\n── REST API ──")
    from env_config import get_hanako_config
    cfg = get_hanako_config()
    check("get_hanako_config 返回", cfg is not None, str(cfg))
    check("transport_mode 合法", cfg["transport_mode"] in ("direct", "prefer_hanako", "hanako_only"))

    from core.hanako_ws_client import HanakoWSClient
    from core.hanako_session_manager import HanakoSessionManager
    _ws = HanakoWSClient(cfg["base_url"], cfg["api_token"])
    mgr = HanakoSessionManager(_ws, cfg["base_url"], cfg["api_token"])

    # health
    try:
        h = mgr.health()
        check("health() 调用", True)
        check("health 返回 agentId", "agentId" in h, str(h)[:200])
    except Exception as e:
        check("health() 调用", False, str(e))
        return mgr, None

    # list sessions
    try:
        sessions = mgr.list_sessions()
        check("list_sessions() 调用", True)
        check("返回列表", isinstance(sessions, list) and len(sessions) > 0,
              f"got {type(sessions)} len={len(sessions) if isinstance(sessions, list) else '?'}")
        if sessions:
            s = sessions[0]
            check("SessionSummary 有 session_id", hasattr(s, 'session_id') and s.session_id)
            check("SessionSummary 有 session_path", hasattr(s, 'session_path') and s.session_path)
            logger.info("First session: id=%s title=%s", s.session_id, s.title)
    except Exception as e:
        check("list_sessions()", False, str(e))
        sessions = []

    return mgr, sessions


def test_ws_client():
    """测试 WS 客户端连接"""
    print("\n── WS Client ──")
    from env_config import get_hanako_config
    from core.hanako_ws_client import HanakoWSClient, ConnectionState
    cfg = get_hanako_config()

    client = HanakoWSClient(cfg["base_url"], cfg["api_token"])
    check("ConnectionState 初始为 STOPPED", client.state == ConnectionState.STOPPED)

    # 记录状态变化
    state_log = []
    state_sub = client.subscribe_state(lambda s, e: state_log.append(s))

    # 启动
    client.start()
    check("start() 不抛异常", True)

    # 等待连接
    ready = client.wait_until_ready(timeout=10)
    check("wait_until_ready 成功", ready, f"state={client.state}")
    check("state 变为 READY", client.state == ConnectionState.READY, f"actual={client.state}")
    check("状态变化记录", len(state_log) >= 1, f"log={state_log}")

    # 事件订阅
    events = []
    evt_sub = client.subscribe(lambda e: events.append(e), event_types={"text_delta", "turn_end"})
    check("subscribe 不抛异常", True)

    state_sub.close()
    evt_sub.close()
    client.stop(timeout=3)
    check("stop() 不抛异常", True)

    return client


def test_full_roundtrip():
    """端到端：发消息 → 收回复"""
    print("\n── Full Roundtrip ──")
    from env_config import get_hanako_config
    from core.hanako_ws_client import HanakoWSClient
    from core.hanako_session_manager import HanakoSessionManager
    cfg = get_hanako_config()

    # 建立连接
    client = HanakoWSClient(cfg["base_url"], cfg["api_token"])
    client.start()
    if not client.wait_until_ready(timeout=10):
        check("WS 连接建立", False, "timeout")
        client.stop()
        return

    mgr = HanakoSessionManager(client, cfg["base_url"], cfg["api_token"])

    # 创建新 session 避免与当前会话冲突
    try:
        session = mgr.create_session()
        logger.info("Created new session: %s", session.session_id)
    except Exception as e:
        check("create_session", False, str(e))
        client.stop()
        return

    check("Session 就绪", session is not None)

    # 发送消息
    try:
        future = mgr.send_message(session, "桌宠集成测试：请回复'测试通过'两个字")
        check("send_message 不抛异常", True)
        check("返回 Future", future is not None)
    except Exception as e:
        check("send_message", False, str(e))
        client.stop()
        return

    # 等待回复
    try:
        result = future.result(timeout=60)
        check("收到 ReplyResult", result is not None)
        check("ReplyResult.text 非空", bool(result.text), f"text='{result.text[:100]}'")
        check("无错误", result.error is None, f"error='{result.error}'")
        logger.info("Reply: %s", result.text[:200])
        if result.thinking:
            logger.info("Thinking: %s", result.thinking[:200])
        if result.tool_calls:
            logger.info("Tool calls: %d", len(result.tool_calls))
    except Exception as e:
        check("future.result()", False, str(e))

    client.stop(timeout=3)


def test_harness_adapter():
    """测试 HarnessAdapter 双路径"""
    print("\n── HarnessAdapter ──")
    from core.harness_adapter import HanakoPetAdapter, HanakoUnavailableBeforeSend

    try:
        adapter = HanakoPetAdapter(agent_id="ophelia", builtin=False)
        check("HanakoPetAdapter 初始化", True)
        check("transport_mode 已设置", hasattr(adapter, 'transport_mode'))
        logger.info("transport_mode=%s", adapter.transport_mode)
    except Exception as e:
        check("HanakoPetAdapter 初始化", False, str(e))
        return

    # parse_emotion
    text1, emo1 = adapter.parse_emotion("你好呀！[emotion:happy]")
    check("parse_emotion 单个", emo1 == "happy", f"got '{emo1}'")
    check("parse_emotion 剥离", "[emotion" not in text1, f"text='{text1}'")

    text2, emo2 = adapter.parse_emotion("[emotion:thinking]让我想想……[emotion:sad]")
    check("parse_emotion 多个取最后", emo2 == "sad", f"got '{emo2}'")

    text3, emo3 = adapter.parse_emotion("普通消息没有情绪标签")
    check("parse_emotion 无标签", emo3 == "neutral", f"got '{emo3}'")


def test_emotion_expiry():
    """测试情绪过期机制（不需要 GUI）"""
    print("\n── Emotion Expiry (config) ──")
    from config import EXPRESSION_MAP, ATLAS_STATE_MAP

    check("EXPRESSION_MAP 有 happy", "happy" in EXPRESSION_MAP)
    check("EXPRESSION_MAP 有 sad (非 idle)", EXPRESSION_MAP.get("sad") != ("idle", None, None))
    check("EXPRESSION_MAP 有 cute", "cute" in EXPRESSION_MAP)
    check("EXPRESSION_MAP 有 missing", "missing" in EXPRESSION_MAP)
    check("ATLAS_STATE_MAP 有 angry", "angry" in ATLAS_STATE_MAP)
    check("ATLAS_STATE_MAP 有 cute", "cute" in ATLAS_STATE_MAP)


if __name__ == "__main__":
    print("=" * 60)
    print("Hanako WS 集成冒烟测试")
    print("=" * 60)

    # 1. 配置和情绪
    test_emotion_expiry()
    test_harness_adapter()

    # 2. REST API
    mgr, sessions = test_rest_api()

    # 3. WS 连接
    try:
        client = test_ws_client()
    except Exception as e:
        print(f"  ❌ WS 测试异常: {e}")
        client = None

    # 4. 端到端（独立创建连接）
    test_full_roundtrip()

    # 汇总
    print(f"\n{'=' * 60}")
    print(f"结果: {PASS} 通过, {FAIL} 失败")
    print(f"{'=' * 60}")
    sys.exit(0 if FAIL == 0 else 1)
