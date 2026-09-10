# -*- coding: utf-8 -*-
"""QA 独立回归（fresh eyes）：BugFix #2（工具气泡有、最终回复气泡/TTS 缺失）边界验证。

验证对象：commit 5ce2124 —— TurnAccumulator 新增 received_final_text_event，
send_and_wait 新增 tool-silent 恢复分支（received_any_event=True 且无最终文本
事件且工具链静默 >= silent_turn_grace → 从会话历史恢复，finish_on_missing=False
不判死，每隔 grace 重试，deadline 墙兜底）。

任务 #3（091263c）语义修正后复查：received_final_text_event 只在 turn_end 置位
（text_delta 是增量事件，不再置位——空/部分 delta 会误判"已拿到最终文本"并永久
屏蔽恢复），恢复条件改为 `not turn.done`。本文件 8 个用例断言在新语义下仍成立
（用例均在 turn_end 到达后断言标志，或断言无 turn_end 时标志为 False），已确认
全绿；仅更新注释以反映 #3 语义。

本文件独立验证（非重跑工程师测试），针对 team-lead 指定边界：
  a) 工具链 + 晚到的 text_delta 正常完成，不误触发 tool-silent 恢复（防误恢复）；
  b) tool-silent 恢复历史无回复 → 不判死、重试、最终 deadline 墙兜底；
  c) 只收到 tool 事件永不收到终态 → 恢复出回复（核心场景独立复现）；
  d) mood_text/其他携带文本的事件与 received_final_text_event 的置位语义。
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
)


class _Sub:
    def __init__(self, fn, event_types=None):
        self.fn = fn
        self.event_types = event_types

    def close(self):
        pass


class _FakeWS:
    """最小 WS 客户端：永远就绪、可发 prompt、记录 abort（验证 deadline 判死时 abort）。"""

    def __init__(self):
        self.sent_prompts: list[dict] = []
        self.ready = True
        self.aborts: list = []

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
        self.aborts.append((cursor, reason))


def _make_sm(grace: float = 0.05, reply_timeout: float = 180.0, activity_timeout: float = 60.0):
    sm = HanakoSessionManager(
        ws_client=_FakeWS(),
        base_url="https://hanako.invalid",
        token="",
        reply_timeout=reply_timeout,
        activity_timeout=activity_timeout,
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


_SESSION = SessionRef("sess-1", "/agents/aimis/sessions/sess-1", "aimis")


def _wait_for_turn(sm, session, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        turn = sm._pending_by_session.get(session.session_id)
        if turn is not None:
            return turn
        time.sleep(0.01)
    raise AssertionError("turn 未被索引，无法注入事件")


def _inject(sm, event_type, seq, *, delta=None, name=None, success=None):
    event = {
        "type": event_type,
        "sessionId": _SESSION.session_id,
        "sessionPath": _SESSION.session_path,
        "streamId": "tool-stream" if event_type.startswith("tool") else "own-stream",
        "seq": seq,
    }
    if delta is not None:
        event["delta"] = delta
    if name is not None:
        event["name"] = name
    if success is not None:
        event["success"] = success
    sm._handle_event(event)


# ── a) 防误恢复：工具链 + 晚到的 text_delta 正常完成 ────────────────────────


def test_tool_chain_then_text_within_grace_completes_without_recovery():
    """工具链事件后 text_delta/turn_end 在 grace 内到达 → 用流式文本正常完成，
    绝不触发 tool-silent 恢复（历史有旧回复时不得提前用旧文本截断实时回复）。"""
    sm = _make_sm(grace=0.2)
    sm.get_history = _history_with_reply  # 若误恢复会拿到旧回复（回归点）
    holder: dict = {}

    def run():
        holder["result"] = sm.send_and_wait(_SESSION, "你好", display_text="你好", timeout=180.0)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    turn = _wait_for_turn(sm, _SESSION)

    _inject(sm, "tool_start", 1, name="biaoqingbao_express")
    _inject(sm, "tool_end", 2, name="biaoqingbao_express", success=True)
    time.sleep(0.1)  # 文本稍晚（仍在 grace 内）
    _inject(sm, "text_delta", 3, delta="实时回复")
    _inject(sm, "turn_end", 4)
    t.join(timeout=5.0)

    assert not t.is_alive(), "send_and_wait 应在事件流完成后返回"
    result = holder["result"]
    assert result.error is None
    assert result.text == "实时回复", f"应用流式文本完成，实际={result.text!r}"
    assert "晚上好呀" not in result.text, "grace 内到达的 text_delta 不得被历史旧回复截断"
    assert turn.received_final_text_event is True
    assert float(getattr(turn, "_last_tool_silent_recovery_ts", 0.0)) == 0.0, (
        "grace 内收到最终文本事件 → tool-silent 恢复不应触发"
    )


def test_tool_silent_recovery_then_late_text_completes_not_killed():
    """工具链静默触发恢复但历史无回复（finish_on_missing=False 不判死），
    随后晚到的 text_delta/turn_end 仍能正常完成 turn（防误杀 + 防截断）。"""
    sm = _make_sm(grace=0.05, reply_timeout=180.0)
    sm.get_history = _history_without_reply  # 恢复拿不到回复 → 必须继续等
    holder: dict = {}

    def run():
        holder["result"] = sm.send_and_wait(_SESSION, "你好", display_text="你好", timeout=180.0)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    turn = _wait_for_turn(sm, _SESSION)

    _inject(sm, "tool_start", 1, name="biaoqingbao_express")
    _inject(sm, "tool_end", 2, name="biaoqingbao_express", success=True)
    # 越过首个 ~1s 轮询 + grace：tool-silent 恢复已尝试过但未完成 turn
    time.sleep(1.3)
    assert turn is not None and not turn.done, "历史无回复时 tool-silent 恢复不得判死 turn"
    assert float(getattr(turn, "_last_tool_silent_recovery_ts", 0.0)) > 0.0, (
        "应已触发过一次 tool-silent 恢复"
    )

    # 晚到的 WS 事件（服务端 1.3s 后才开始流式）仍能正常完成
    _inject(sm, "text_delta", 3, delta="晚上好呀（晚到）")
    _inject(sm, "turn_end", 4)
    t.join(timeout=3.0)

    assert not t.is_alive()
    result = holder["result"]
    assert result.error is None, f"晚到 WS 事件应正常完成 turn: {result.error}"
    assert result.text == "晚上好呀（晚到）"
    assert turn.done


def test_tool_silent_recovery_fires_before_late_text_when_history_has_reply():
    """【行为记录 / 风险】历史已有回复、text_delta 在工具链静默 grace 之后才到：
    tool-silent 恢复会先于晚到文本完成 turn（用历史文本）。
    若服务端历史是"流式中途的 partial 文本"，此处可能截断最终回复——需产品决策。
    本测试断言当前实际行为（非规格失败），供团队评估。"""
    sm = _make_sm(grace=0.05, reply_timeout=180.0)
    sm.get_history = _history_with_reply
    holder: dict = {}

    def run():
        holder["result"] = sm.send_and_wait(_SESSION, "你好", display_text="你好", timeout=180.0)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    turn = _wait_for_turn(sm, _SESSION)

    _inject(sm, "tool_start", 1, name="biaoqingbao_express")
    _inject(sm, "tool_end", 2, name="biaoqingbao_express", success=True)
    time.sleep(1.3)  # 越过 ~1s 轮询 + grace：恢复已用历史回复完成 turn
    # 晚到文本此时才到 → 已被 done 的 turn 忽略（作为风险记录，不判为规格失败）
    _inject(sm, "text_delta", 3, delta="实时回复（晚于 20s 静默）")
    t.join(timeout=3.0)

    assert not t.is_alive()
    result = holder["result"]
    assert result.error is None
    assert result.text == "晚上好呀，月曦夜。", (
        "当前行为：历史有回复时，工具链静默 >grace 后由恢复完成（晚到文本被丢弃）"
    )
    assert turn.received_final_text_event is False


def test_ongoing_tool_progress_prevents_premature_recovery():
    """长时间运行的工具链持续收到 tool_progress（每次间隔 < grace）→ turn 保持
    活跃、不触发 tool-silent 恢复；工具结束后 text_delta 正常完成。"""
    sm = _make_sm(grace=0.2)
    sm.get_history = _history_with_reply  # 若误恢复会拿到旧回复（回归点）
    holder: dict = {}

    def run():
        holder["result"] = sm.send_and_wait(_SESSION, "你好", display_text="你好", timeout=180.0)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    turn = _wait_for_turn(sm, _SESSION)

    _inject(sm, "tool_start", 1, name="play")
    # 工具链持续推送进度（每 0.1s < grace=0.2），直到越过首个 ~1s 轮询
    end = time.monotonic() + 1.5
    seq = 2
    while time.monotonic() < end:
        _inject(sm, "tool_progress", seq, name="play")
        seq += 1
        time.sleep(0.1)
    _inject(sm, "tool_end", seq, name="play", success=True)
    seq += 1
    time.sleep(0.05)
    _inject(sm, "text_delta", seq, delta="正在为你播放音乐")
    seq += 1
    _inject(sm, "turn_end", seq)
    t.join(timeout=5.0)

    assert not t.is_alive()
    result = holder["result"]
    assert result.error is None
    assert result.text == "正在为你播放音乐", f"持续活跃的工具链不应被历史截断: {result.text!r}"
    assert float(getattr(turn, "_last_tool_silent_recovery_ts", 0.0)) == 0.0, (
        "工具链持续活跃时不得触发 tool-silent 恢复"
    )


# ── b) tool-silent 恢复失败（历史始终无回复）→ 不判死、重试、deadline 墙兜底 ──


def test_tool_silent_deadline_wall_fallback_when_no_reply_ever():
    """工具事件到达但最终文本永远不来、历史也始终无回复：
    1) 不提前判死（多次 tool-silent 恢复均 finish_on_missing=False）；
    2) 每次轮询按 grace 重试（get_history 多次调用）；
    3) 最终走到 deadline 墙 → finish_on_missing=True 判死 + abort 服务端。"""
    sm = _make_sm(grace=0.05, reply_timeout=180.0, activity_timeout=0.4)
    calls = {"n": 0}

    def history(session, limit=30):
        calls["n"] += 1
        return _history_without_reply(session, limit)

    sm.get_history = history
    holder: dict = {}

    def run():
        holder["result"] = sm.send_and_wait(_SESSION, "你好", display_text="你好", timeout=3.0)

    t0 = time.monotonic()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    turn = _wait_for_turn(sm, _SESSION)

    _inject(sm, "tool_start", 1, name="biaoqingbao_express")
    _inject(sm, "tool_end", 2, name="biaoqingbao_express", success=True)
    t.join(timeout=8.0)
    elapsed = time.monotonic() - t0

    assert not t.is_alive()
    result = holder["result"]
    assert result.error is not None, "历史始终无回复 → deadline 墙应判死"
    assert "Stream ended before reply history was available" in result.error
    assert calls["n"] >= 3, f"应按 grace 重试多次（实际 {calls['n']} 次），不得卡死/提前判死"
    assert elapsed >= 2.0, f"不应在 grace(0.05s) 就判死，应走到 deadline 墙（实际 {elapsed:.2f}s）"
    assert len(sm.ws_client.aborts) == 1, "deadline 判死时应 abort 服务端 turn（P0 语义）"


# ── c) 核心场景独立复现：只收到 tool 事件、永不收到终态 → 恢复出回复 ────────


def test_tool_only_events_recover_from_history_core_scenario():
    """#2 核心场景独立复现：用户消息后只收到 tool_start/tool_end（无
    text_delta/turn_end），工具链静默超过 grace → 从会话历史恢复最终回复，
    远早于 activity_timeout(60s)/reply_timeout(180s)。"""
    sm = _make_sm(grace=0.05, reply_timeout=180.0)
    sm.get_history = _history_with_reply
    holder: dict = {}

    def run():
        holder["result"] = sm.send_and_wait(_SESSION, "你好", display_text="你好", timeout=180.0)

    t0 = time.monotonic()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    turn = _wait_for_turn(sm, _SESSION)

    _inject(sm, "tool_start", 1, name="biaoqingbao_express")
    _inject(sm, "tool_end", 2, name="biaoqingbao_express", success=True)
    t.join(timeout=10.0)
    elapsed = time.monotonic() - t0

    assert not t.is_alive(), "send_and_wait 应在工具链静默后从历史恢复并返回"
    result = holder["result"]
    assert result.error is None
    assert result.text == "晚上好呀，月曦夜。"
    assert elapsed < 10.0, f"应远早于 activity_timeout(60s) 恢复（实际 {elapsed:.2f}s）"
    assert turn.received_any_event is True, "tool 事件应算活跃"
    assert turn.received_final_text_event is False, "tool 事件不应算最终文本"
    assert not getattr(turn, "_silent_recovery_tried", False), (
        "仅 tool 事件应走 tool-silent 分支，而非原静默分支"
    )


# ── d) 携带文本的事件与 received_final_text_event 置位语义 ──────────────────


def test_mood_events_keep_turn_alive_but_do_not_count_as_final_text():
    """mood_start/mood_text/mood_end 是 <mood> 内省块（Vibe/Will…），不是最终回复
    文本 → 应刷新活跃（received_any_event=True、顺延 deadline）但**不得**置位
    received_final_text_event；随后 text_delta 才置位并正常完成。"""
    sm = _make_sm(grace=5.0)
    sm.get_history = _history_without_reply
    future = sm.send_message(_SESSION, "你好", display_text="你好")
    turn = sm._pending_by_session[_SESSION.session_id]

    _inject(sm, "mood_start", 1)
    _inject(sm, "mood_text", 2, delta="Vibe: 好奇 / Will: 回应")
    _inject(sm, "mood_end", 3)
    assert turn.received_any_event is True, "mood 事件应算活跃（turn 还活着）"
    assert turn.received_final_text_event is False, "mood 内省块不是最终回复文本"

    _inject(sm, "text_delta", 4, delta="晚上好呀")
    _inject(sm, "turn_end", 5)
    result = future.result(timeout=5.0)
    assert result.error is None
    assert result.text == "晚上好呀"
    assert turn.received_final_text_event is True, "turn_end 才应置位最终文本标志（#3 修正：text_delta 不置位）"


def test_mood_only_turn_recovers_via_tool_silent_branch():
    """只收到 mood 内省事件、无 text_delta/turn_end（服务端在别的身份上回推文本）→
    mood 事件算活跃但不算终态，工具链静默后应走 tool-silent 分支从历史恢复。
    验证 mood_text 不置位标志不会导致"永远等不到恢复"。"""
    sm = _make_sm(grace=0.05, reply_timeout=180.0)
    sm.get_history = _history_with_reply
    holder: dict = {}

    def run():
        holder["result"] = sm.send_and_wait(_SESSION, "你好", display_text="你好", timeout=180.0)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    turn = _wait_for_turn(sm, _SESSION)

    _inject(sm, "mood_start", 1)
    _inject(sm, "mood_text", 2, delta="Vibe: 开心")
    _inject(sm, "mood_end", 3)
    t.join(timeout=6.0)

    assert not t.is_alive(), "仅 mood 事件的 turn 也应从历史恢复并返回"
    result = holder["result"]
    assert result.error is None
    assert result.text == "晚上好呀，月曦夜。"
    assert turn.received_any_event is True
    assert turn.received_final_text_event is False
