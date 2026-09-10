# -*- coding: utf-8 -*-
"""BugFix #1 单元测试：HanakoSessionManager 静默 turn 快速恢复。

背景：用户消息经 `chat_via_hanako → send_and_wait` 后，Hanako WS 回复事件没有
匹配到桌宠 turn（会话身份不匹配 / 事件丢失），`send_and_wait` 死等满
reply_timeout(180s)，引擎线程冻结 → 无回复气泡、无 TTS、UI 停在"思考中..."。

修复：`send_and_wait` 增加静默 turn 快速恢复——send 后 `silent_turn_grace`
（默认 20s）内一条 WS 事件都没收到（last_event_ts 未动），主动从会话历史
恢复最终回复（云端其实已生成，Hanako 主窗口可见）；历史暂无回复则不打断
turn，继续等 WS 事件或走正常超时（绝不提前判死仍在处理的 turn）。
"""
from __future__ import annotations

import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.hanako_session_manager import (
    HanakoSessionManager,
    HistoryPage,
    SessionRef,
    TurnAccumulator,
)


class _Sub:
    def __init__(self, fn, event_types=None):
        self.fn = fn
        self.event_types = event_types

    def close(self):
        pass


class _FakeWS:
    """最小 WS 客户端：永远就绪、可发 prompt、不推送任何事件（静默 turn）。"""

    def __init__(self):
        self.sent_prompts: list[dict] = []
        self.ready = True

    @property
    def is_ready(self):
        return self.ready

    def start(self):
        pass

    def wait_until_ready(self, timeout):
        return self.ready

    def subscribe(self, callback, event_types=None, session_id=None, session_path=None):
        return _Sub(callback, event_types)

    def subscribe_state(self, callback):
        return _Sub(callback, None)

    def send_prompt(self, **kwargs):
        self.sent_prompts.append(kwargs)

    def resume_stream(self, cursor):
        pass

    def abort_stream(self, cursor, reason="user_abort"):
        pass


def _make_sm(grace: float = 0.05, reply_timeout: float = 180.0):
    sm = HanakoSessionManager(
        ws_client=_FakeWS(),
        base_url="https://hanako.invalid",
        token="",
        reply_timeout=reply_timeout,
        activity_timeout=60.0,
        silent_turn_grace=grace,
    )
    return sm


def _history_with_reply(session, limit=30):
    return HistoryPage(
        session=session,
        messages=(
            {"role": "user", "displayText": "你好", "content": "你好"},
            {"role": "assistant", "content": "晚上好呀，月曦夜。", "thinking": ""},
        ),
        content_blocks=(),
        has_more=False,
    )


def _history_without_reply(session, limit=30):
    return HistoryPage(
        session=session,
        messages=(
            {"role": "user", "displayText": "你好", "content": "你好"},
        ),
        content_blocks=(),
        has_more=False,
    )


# ── 1. 静默 turn：历史有最终回复 → 快速恢复，不等满 reply_timeout ─────────


def test_silent_turn_recovers_reply_from_history_before_reply_timeout():
    """WS 事件一条没到，但云端历史已有回复 → send_and_wait 提前返回回复。"""
    sm = _make_sm(grace=0.05, reply_timeout=180.0)
    sm.get_history = _history_with_reply

    session = SessionRef("sess-1", "/agents/aimis/sessions/sess-1", "aimis")
    t0 = time.monotonic()
    result = sm.send_and_wait(session, "你好", display_text="你好", timeout=180.0)
    elapsed = time.monotonic() - t0

    assert elapsed < 5.0, "静默恢复应远早于 reply_timeout(180s) 返回"
    assert result.error is None
    assert "晚上好呀" in result.text
    # prompt 确实已发出（消息进了 Hanako，只是事件没回来）
    assert len(sm.ws_client.sent_prompts) == 1
    assert sm.ws_client.sent_prompts[0]["text"] == "你好"


def test_silent_recovery_preserves_recovered_text():
    """历史恢复应带回完整回复文本（含情绪标签解析留给上层）。"""
    sm = _make_sm(grace=0.05)
    sm.get_history = _history_with_reply
    session = SessionRef("sess-1", "/agents/aimis/sessions/sess-1", "aimis")
    result = sm.send_and_wait(session, "你好", display_text="你好", timeout=180.0)
    assert result.error is None
    assert result.text == "晚上好呀，月曦夜。"


# ── 2. 静默 turn：历史还没有回复 → 不提前判死，继续等超时墙 ──────────────


def test_silent_turn_not_killed_when_history_has_no_reply_yet():
    """服务端仍在处理（历史暂无回复）→ 静默恢复不得提前判死 turn。"""
    sm = _make_sm(grace=0.05, reply_timeout=180.0)
    sm.get_history = _history_without_reply

    session = SessionRef("sess-1", "/agents/aimis/sessions/sess-1", "aimis")
    t0 = time.monotonic()
    # 调用方 timeout=0.3：deadline 墙下限 1.0s → 应在 ~1s 走"历史无可恢复→判死"，
    # 而不是 0.05s 静默宽限一到就把 turn 杀掉。
    result = sm.send_and_wait(session, "你好", display_text="你好", timeout=0.3)
    elapsed = time.monotonic() - t0

    assert elapsed >= 0.5, "历史无回复时不应在静默宽限(0.05s)就提前判死 turn"
    assert result.error is not None
    assert "Stream ended before reply history was available" in result.error


# ── 3. 正常 WS 事件流：收到事件后不触发静默恢复 ───────────────────────────


def test_normal_events_prevent_silent_recovery():
    """turn 正常收到 WS 事件（last_event_ts 被刷新）→ 静默恢复不触发，正常完成。"""
    sm = _make_sm(grace=5.0, reply_timeout=180.0)  # 宽限设大，正常事件先到
    sm.get_history = _history_without_reply  # 即使历史没回复也不该被静默恢复

    session = SessionRef("sess-1", "/agents/aimis/sessions/sess-1", "aimis")
    # 先 send 拿到 turn，再模拟服务端正常推送 turn_end（绕过静默恢复路径）
    future = sm.send_message(session, "你好", display_text="你好")
    turn = sm._pending_by_session[session.session_id]
    # 模拟事件流：thinking_start(带 seq+streamId) → text_delta → turn_end
    for event in (
        {"type": "thinking_start", "sessionId": session.session_id,
         "sessionPath": session.session_path, "streamId": "s1", "seq": 1},
        {"type": "text_delta", "sessionId": session.session_id,
         "sessionPath": session.session_path, "streamId": "s1", "seq": 2, "delta": "晚上好呀"},
        {"type": "turn_end", "sessionId": session.session_id,
         "sessionPath": session.session_path, "streamId": "s1", "seq": 3},
    ):
        sm._handle_event(event)

    result = future.result(timeout=5.0)
    assert result.error is None
    assert "晚上好呀" in result.text
    assert not getattr(turn, "_silent_recovery_tried", False), "正常事件流不应触发静默恢复"


# ── 4. BugFix #2：显式 received_any_event（不依赖 monotonic 严格递增）──────


def test_accept_event_sets_received_any_event_flag():
    """accept_event 命中本 turn 事件 → received_any_event 置位（显式布尔）。"""
    session = SessionRef("sess-1", "/agents/aimis/sessions/sess-1", "aimis")
    turn = TurnAccumulator(session=session, client_message_id="ocpet_x", origin="oc_pet")
    assert turn.received_any_event is False
    # 模拟 monotonic 同 tick：时间戳没有严格增大（旧判断 last>created 会误判静默）
    turn.last_event_ts = turn.created_at
    assert turn.accept_event({"seq": 1, "streamId": "s1"}) is True
    assert turn.received_any_event is True, "收到事件必须置位显式布尔，不依赖时间戳"


def test_same_tick_event_not_treated_as_silent():
    """同 tick 收到事件（last_event_ts==created_at）→ 按流式文本完成，
    绝不被历史旧回复提前完成/截断（Fix 2 回归点）。"""
    sm = _make_sm(grace=0.05)
    sm.get_history = _history_with_reply  # 若误判静默会用旧回复提前完成（回归点）
    session = SessionRef("sess-1", "/agents/aimis/sessions/sess-1", "aimis")

    future = sm.send_message(session, "你好", display_text="你好")
    turn = sm._pending_by_session[session.session_id]
    # 模拟同 tick：收到事件后 last_event_ts 与 created_at 相等
    turn.received_any_event = True
    turn.last_event_ts = turn.created_at
    # 正常流式继续（text_delta → turn_end）
    for event in (
        {"type": "text_delta", "sessionId": session.session_id,
         "sessionPath": session.session_path, "streamId": "s1", "seq": 1, "delta": "实时回复"},
        {"type": "turn_end", "sessionId": session.session_id,
         "sessionPath": session.session_path, "streamId": "s1", "seq": 2},
    ):
        sm._handle_event(event)

    result = future.result(timeout=5.0)
    assert result.error is None
    assert "实时回复" in result.text
    assert "晚上好呀" not in result.text, "同 tick 收到事件的 turn 不得被历史旧回复截断"


# ── 5. BugFix #1：静默恢复期间 get_history 异常不判死 turn ─────────────────


def test_silent_recovery_ignores_get_history_exception():
    """静默恢复路径 get_history 瞬时异常（REST 抖动）→ 不提前判死/不 abort，
    turn 保持存活，继续等至 deadline 墙。"""
    sm = _make_sm(grace=0.05, reply_timeout=180.0)

    def boom(session, limit=30):
        raise RuntimeError("REST 抖动")

    sm.get_history = boom
    session = SessionRef("sess-1", "/agents/aimis/sessions/sess-1", "aimis")
    t0 = time.monotonic()
    result = sm.send_and_wait(session, "你好", display_text="你好", timeout=0.3)
    elapsed = time.monotonic() - t0

    # 不应在静默宽限(0.05s)处因 get_history 异常提前判死；应走 deadline 墙(≥1s)
    assert elapsed >= 0.5, "get_history 瞬时异常不得在静默宽限就判死 turn"
    assert result.error is not None
    assert "Stream recovery failed" in result.error


# ── 6. BugFix #3：同 session 其他消息事件不污染本 turn ─────────────────────


def test_foreign_message_events_rejected_for_pending_turn():
    """用户在 Hanako 主窗口同时发消息（同 session、不同 clientMessageId）→
    其事件不得绑定 stream / 刷新 last_seq / 积累文本污染本 turn。"""
    sm = _make_sm(grace=0.05)
    sm.get_history = _history_without_reply
    session = SessionRef("sess-1", "/agents/aimis/sessions/sess-1", "aimis")

    future = sm.send_message(session, "你好", display_text="你好")
    turn = sm._pending_by_session[session.session_id]

    # 外国消息事件：clientMessageId 不匹配 → 必须被拒绝
    sm._handle_event({
        "type": "text_delta", "sessionId": session.session_id,
        "sessionPath": session.session_path, "streamId": "foreign-stream",
        "seq": 1, "delta": "这是别人的回复", "clientMessageId": "other_client",
    })
    assert turn.stream_id is None, "外国事件不得绑定 stream"
    assert turn.text_parts == [], "外国事件不得积累文本"
    assert not turn.received_any_event, "外国事件不得标记本 turn 为活跃"
    assert turn.last_seq == 0, "外国事件不得推进 last_seq"

    # 本 turn 自己的事件仍正常完成
    for event in (
        {"type": "text_delta", "sessionId": session.session_id,
         "sessionPath": session.session_path, "streamId": "own-stream",
         "seq": 1, "delta": "本 turn 回复"},
        {"type": "turn_end", "sessionId": session.session_id,
         "sessionPath": session.session_path, "streamId": "own-stream", "seq": 2},
    ):
        sm._handle_event(event)

    result = future.result(timeout=5.0)
    assert result.error is None
    assert "本 turn 回复" in result.text
    assert "别人的回复" not in result.text


# ── 7. BugFix #4（任务 #2）：只收到 tool 事件、无最终文本事件 → 工具静默恢复 ──


def _wait_for_turn(sm, session, timeout=2.0):
    """等待 send_and_wait 的 turn 被索引（send_message 在子线程中执行）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        turn = sm._pending_by_session.get(session.session_id)
        if turn is not None:
            return turn
        time.sleep(0.01)
    raise AssertionError("turn 未被索引，无法注入事件")


def test_tool_only_events_trigger_tool_silent_recovery():
    """#2 端到端：用户消息后只有 tool_start/tool_end 事件（无 text_delta/turn_end），
    工具链静默超过 silent_turn_grace → send_and_wait 主动从历史恢复，不等
    activity_timeout(60s) —— 修复"工具气泡有、最终回复气泡/TTS 没有"。"""
    sm = _make_sm(grace=0.05, reply_timeout=180.0)
    sm.get_history = _history_with_reply
    session = SessionRef("sess-1", "/agents/aimis/sessions/sess-1", "aimis")

    holder: dict = {}
    def run():
        holder["result"] = sm.send_and_wait(session, "你好", display_text="你好", timeout=180.0)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    turn = _wait_for_turn(sm, session)

    # 模拟：tool 事件到达（不带 clientMessageId），但最终文本事件永远不来
    sm._handle_event({
        "type": "tool_start", "sessionId": session.session_id,
        "sessionPath": session.session_path, "streamId": "tool-stream",
        "seq": 1, "name": "biaoqingbao_express",
    })
    time.sleep(0.02)
    sm._handle_event({
        "type": "tool_end", "sessionId": session.session_id,
        "sessionPath": session.session_path, "streamId": "tool-stream",
        "seq": 2, "name": "biaoqingbao_express", "success": True,
    })
    t.join(timeout=6.0)

    assert not t.is_alive(), "send_and_wait 应在工具链静默后从历史恢复并返回"
    result = holder["result"]
    assert result.error is None
    assert "晚上好呀" in result.text
    assert turn.received_any_event is True, "tool 事件应算活跃"
    assert turn.received_final_text_event is False, "tool 事件不应算最终文本"


def test_tool_events_with_matching_client_message_id_still_recover():
    """tool 事件带匹配本 turn 的 clientMessageId → c5f2995 校验放行，
    但仍无最终文本事件 → 工具静默恢复生效。"""
    sm = _make_sm(grace=0.05, reply_timeout=180.0)
    sm.get_history = _history_with_reply
    session = SessionRef("sess-1", "/agents/aimis/sessions/sess-1", "aimis")

    holder: dict = {}
    def run():
        holder["result"] = sm.send_and_wait(session, "你好", display_text="你好", timeout=180.0)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    turn = _wait_for_turn(sm, session)
    client_id = turn.client_message_id  # 匹配本 turn

    sm._handle_event({
        "type": "tool_start", "sessionId": session.session_id,
        "sessionPath": session.session_path, "streamId": "tool-stream",
        "seq": 1, "name": "biaoqingbao_express", "clientMessageId": client_id,
    })
    sm._handle_event({
        "type": "tool_end", "sessionId": session.session_id,
        "sessionPath": session.session_path, "streamId": "tool-stream",
        "seq": 2, "name": "biaoqingbao_express", "success": True,
        "clientMessageId": client_id,
    })
    t.join(timeout=6.0)

    assert not t.is_alive()
    result = holder["result"]
    assert result.error is None
    assert "晚上好呀" in result.text


def test_foreign_tool_event_rejected_then_original_silent_recovery():
    """tool 事件带不匹配 clientMessageId（他人在同 session 发消息）→ BugFix #3
    拒收（不置位活跃、不绑定 stream），turn 仍按原静默恢复路径从历史取回复。"""
    sm = _make_sm(grace=0.05, reply_timeout=180.0)
    sm.get_history = _history_with_reply
    session = SessionRef("sess-1", "/agents/aimis/sessions/sess-1", "aimis")

    holder: dict = {}
    def run():
        holder["result"] = sm.send_and_wait(session, "你好", display_text="你好", timeout=180.0)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    turn = _wait_for_turn(sm, session)

    sm._handle_event({
        "type": "tool_start", "sessionId": session.session_id,
        "sessionPath": session.session_path, "streamId": "foreign-stream",
        "seq": 1, "name": "biaoqingbao_express", "clientMessageId": "someone_else",
    })
    t.join(timeout=6.0)

    assert not t.is_alive()
    result = holder["result"]
    assert result.error is None
    assert "晚上好呀" in result.text
    assert turn.received_any_event is False, "外来 tool 事件必须被拒收"
    assert turn.stream_id is None, "外来 tool 事件不得绑定 stream"


def test_tool_events_then_text_events_skip_tool_silent_recovery():
    """工具事件后最终文本事件（text_delta/turn_end）正常到达 → 不触发工具静默恢复，
    使用流式文本完成（防误用历史旧回复截断实时回复）。"""
    sm = _make_sm(grace=0.05, reply_timeout=180.0)
    sm.get_history = _history_with_reply  # 若误恢复会拿到旧回复（回归点）
    session = SessionRef("sess-1", "/agents/aimis/sessions/sess-1", "aimis")

    holder: dict = {}
    def run():
        holder["result"] = sm.send_and_wait(session, "你好", display_text="你好", timeout=180.0)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    turn = _wait_for_turn(sm, session)

    sm._handle_event({
        "type": "tool_start", "sessionId": session.session_id,
        "sessionPath": session.session_path, "streamId": "tool-stream",
        "seq": 1, "name": "biaoqingbao_express",
    })
    sm._handle_event({
        "type": "tool_end", "sessionId": session.session_id,
        "sessionPath": session.session_path, "streamId": "tool-stream",
        "seq": 2, "name": "biaoqingbao_express", "success": True,
    })
    sm._handle_event({
        "type": "text_delta", "sessionId": session.session_id,
        "sessionPath": session.session_path, "streamId": "own-stream",
        "seq": 3, "delta": "实时回复",
    })
    sm._handle_event({
        "type": "turn_end", "sessionId": session.session_id,
        "sessionPath": session.session_path, "streamId": "own-stream", "seq": 4,
    })
    t.join(timeout=6.0)

    assert not t.is_alive()
    result = holder["result"]
    assert result.error is None
    assert "实时回复" in result.text
    assert "晚上好呀" not in result.text, "收到最终文本事件的 turn 不得被历史旧回复截断"
    assert turn.received_final_text_event is True


def test_tool_silent_recovery_retries_until_reply_available():
    """首次工具静默恢复时历史还没有回复 → 不判死、不打断（finish_on_missing=False），
    静默 grace 后重试，历史出现回复后完成。"""
    sm = _make_sm(grace=0.05, reply_timeout=180.0)
    calls = {"n": 0}

    def history(session, limit=30):
        calls["n"] += 1
        if calls["n"] == 1:
            return _history_without_reply(session, limit)
        return _history_with_reply(session, limit)

    sm.get_history = history
    session = SessionRef("sess-1", "/agents/aimis/sessions/sess-1", "aimis")

    holder: dict = {}
    def run():
        holder["result"] = sm.send_and_wait(session, "你好", display_text="你好", timeout=180.0)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    turn = _wait_for_turn(sm, session)

    sm._handle_event({
        "type": "tool_start", "sessionId": session.session_id,
        "sessionPath": session.session_path, "streamId": "tool-stream",
        "seq": 1, "name": "biaoqingbao_express",
    })
    sm._handle_event({
        "type": "tool_end", "sessionId": session.session_id,
        "sessionPath": session.session_path, "streamId": "tool-stream",
        "seq": 2, "name": "biaoqingbao_express", "success": True,
    })
    t.join(timeout=8.0)

    assert not t.is_alive(), "工具静默恢复重试后应完成"
    result = holder["result"]
    assert result.error is None
    assert "晚上好呀" in result.text
    assert calls["n"] >= 2, "首次无回复后应重试，而不是提前判死"
    assert not getattr(turn, "_silent_recovery_tried", False), "仅 tool 事件不应走原静默分支"


def test_final_text_event_flag_set_only_by_turn_end():
    """received_final_text_event 只在 turn_end 置位；text_delta 是**增量**事件
    （可能为空/部分），置位会误判"已拿到最终文本"、永久屏蔽 tool-silent 恢复
    （任务 #3 修正：多 tool 事件 + 空 text_delta 场景实测复现卡死）。"""
    sm = _make_sm(grace=5.0)
    session = SessionRef("sess-1", "/agents/aimis/sessions/sess-1", "aimis")

    future = sm.send_message(session, "你好", display_text="你好")
    turn = sm._pending_by_session[session.session_id]

    sm._handle_event({
        "type": "tool_start", "sessionId": session.session_id,
        "sessionPath": session.session_path, "streamId": "s1", "seq": 1, "name": "x",
    })
    assert turn.received_any_event is True
    assert turn.received_final_text_event is False, "tool 事件不得置最终文本标志"

    sm._handle_event({
        "type": "text_delta", "sessionId": session.session_id,
        "sessionPath": session.session_path, "streamId": "s1", "seq": 2, "delta": "hi",
    })
    assert turn.received_final_text_event is False, (
        "text_delta 是增量事件，不得置最终文本标志（否则空 delta 会永久卡死恢复）"
    )

    sm._handle_event({
        "type": "turn_end", "sessionId": session.session_id,
        "sessionPath": session.session_path, "streamId": "s1", "seq": 3,
    })
    assert turn.received_final_text_event is True, "turn_end 是权威终态信号，应置位"
    result = future.result(timeout=5.0)
    assert result.error is None
    assert result.text == "hi"


def test_multi_dense_tool_events_with_empty_text_delta_recovers():
    """任务 #3 核心回归：3 个 tool 事件密集到达 + 中间夹空 text_delta（无
    turn_end）→ 旧代码 received_final_text_event 被空 delta 误置位，tool-silent
    分支被永久屏蔽，turn 拖到 deadline 墙（~2-3min）才恢复，用户看到"工具气泡有、
    最终回复气泡/TTS 没有"。修复后：静默 grace 一过立即从历史恢复，且空 delta
    不再污染 text_parts（否则恢复后仍无正文）。"""
    sm = _make_sm(grace=0.05, reply_timeout=180.0)
    sm.get_history = _history_with_reply
    session = SessionRef("sess-1", "/agents/aimis/sessions/sess-1", "aimis")

    holder: dict = {}
    def run():
        holder["result"] = sm.send_and_wait(session, "你好", display_text="你好", timeout=180.0)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    turn = _wait_for_turn(sm, session)

    # 模拟真实日志事件流：3 个 tool_start 密集到达，中间夹一个空 text_delta
    for event in (
        {"type": "tool_start", "sessionId": session.session_id,
         "sessionPath": session.session_path, "streamId": "tool-stream",
         "seq": 1, "name": "biaoqingbao_search_stickers"},
        {"type": "tool_start", "sessionId": session.session_id,
         "sessionPath": session.session_path, "streamId": "tool-stream",
         "seq": 2, "name": "search_memory"},
        {"type": "text_delta", "sessionId": session.session_id,
         "sessionPath": session.session_path, "streamId": "tool-stream",
         "seq": 3, "delta": ""},
        {"type": "tool_start", "sessionId": session.session_id,
         "sessionPath": session.session_path, "streamId": "tool-stream",
         "seq": 4, "name": "biaoqingbao_express"},
    ):
        sm._handle_event(event)
        time.sleep(0.01)
    t.join(timeout=8.0)

    assert not t.is_alive(), "多 tool 事件 + 空 text_delta 也应从历史恢复并返回"
    result = holder["result"]
    assert result.error is None
    assert "晚上好呀" in result.text, f"空 delta 不得污染最终文本: {result.text!r}"
    assert turn.received_final_text_event is False, "空 text_delta 不得置最终文本标志"
    assert turn.received_any_event is True
    assert float(getattr(turn, "_last_tool_silent_recovery_ts", 0.0)) > 0.0, (
        "应已触发 tool-silent 恢复"
    )


def test_partial_text_delta_then_stream_death_still_recovers():
    """任务 #3 边界：text_delta 携带部分文本后流中断（无 turn_end）→ 旧代码
    received_final_text_event=True 永久屏蔽恢复。修复后：静默超过 grace 即从历史
    恢复权威最终文本（历史有完整回复时不展示残缺部分）。"""
    sm = _make_sm(grace=0.05, reply_timeout=180.0)
    sm.get_history = _history_with_reply
    session = SessionRef("sess-1", "/agents/aimis/sessions/sess-1", "aimis")

    holder: dict = {}
    def run():
        holder["result"] = sm.send_and_wait(session, "你好", display_text="你好", timeout=180.0)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    turn = _wait_for_turn(sm, session)

    sm._handle_event({
        "type": "text_delta", "sessionId": session.session_id,
        "sessionPath": session.session_path, "streamId": "s1", "seq": 1, "delta": "晚",
    })
    t.join(timeout=6.0)

    assert not t.is_alive(), "部分 text_delta 后流中断也应从历史恢复"
    result = holder["result"]
    assert result.error is None
    assert "晚上好呀" in result.text, f"应恢复历史完整回复，实际={result.text!r}"

