# -*- coding: utf-8 -*-
"""QA 独立回归（fresh eyes）：BugFix #1（对话回复气泡/TTS 缺失）边界验证。

背景：commit 4aaa68d 修复"发『你好』后桌宠停在思考中、无回复无 TTS"。
根因是 Hanako WS 回复事件未匹配到桌宠 turn，send_and_wait 死等 reply_timeout(180s)。
修复：TurnAccumulator.created_at + send_and_wait ~1s 轮询 + 静默 turn（silent_turn_grace
默认 20s 内无任何 WS 事件）主动从会话历史恢复（finish_on_missing=False 不提前判死）。

本文件针对 team-lead 指定的边界场景独立验证（非重跑工程师测试）：
  a) 正常事件流（收到事件后）不触发静默恢复；
  b) 静默 turn 历史无回复 → 不提前判死、继续等，晚到的 WS 事件仍能正常完成；
  c) 活动 turn 超过 activity_timeout 仍走 Bug A 历史恢复路径（不误触发静默恢复）；
  d) 【疑似源码 Bug】静默恢复期间历史 REST 瞬时失败（异常路径）→ 按设计意图
     finish_on_missing=False 不应判死 turn，实际却 _finish_with_error 提前判死
     并 abort 服务端 turn；
  e) 【行为记录】同 session 的其他事件（带 seq+streamId，经 session fallback
     路由到静默 turn）会刷新 last_event_ts → 掩盖"静默" → 20s 快速恢复不触发。
"""
from __future__ import annotations

import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.hanako_session_manager import (
    HanakoSessionError,
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
    """最小 WS 客户端：永远就绪、可发 prompt、不推送任何事件（静默 turn）。"""

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


def _make_sm(grace: float = 0.05, reply_timeout: float = 10.0, activity_timeout: float = 60.0):
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


# ── a) 正常事件流不触发静默恢复；活动 turn 超过 activity_timeout 走 Bug A ──


def test_active_turn_past_activity_timeout_recovers_via_bug_a_not_silent():
    """收到过事件的 turn（received_any_event=True）在事件停止后：
    1) 静默恢复绝不触发（_silent_recovery_tried 保持 False）；
    2) 超过 activity_timeout 后走 Bug A 历史恢复路径（deadline 墙），而不是判死。
    """
    sm = _make_sm(grace=0.05, reply_timeout=10.0, activity_timeout=0.2)
    sm.get_history = _history_with_reply

    holder: dict = {}

    def _run():
        holder["result"] = sm.send_and_wait(_SESSION, "你好", display_text="你好", timeout=1.0)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(0.05)
    turn = sm._pending_by_session[_SESSION.session_id]
    holder["turn"] = turn
    # 事件在静默宽限(0.05s)到期前到达 → turn 变成"活动 turn"
    sm._handle_event({
        "type": "text_delta", "sessionId": _SESSION.session_id,
        "sessionPath": _SESSION.session_path, "streamId": "s1",
        "seq": 1, "delta": "晚上好呀",
    })
    t.join(timeout=3.0)

    result = holder.get("result")
    assert result is not None, "send_and_wait 未返回"
    # 活动 turn：静默恢复不得触发（received_any_event=True 时静默分支被跳过）
    assert not getattr(holder["turn"], "_silent_recovery_tried", False), (
        "收到过事件的 turn 不应触发静默恢复"
    )
    assert result.error is None, f"活动 turn 超时应走 Bug A 历史恢复而非判死: {result.error}"
    assert "晚上好呀" in result.text


def test_normal_event_flow_completes_without_silent_recovery_flag():
    """正常事件流（thinking→text→turn_end）完成后，_silent_recovery_tried 必须为 False。"""
    sm = _make_sm(grace=0.05, reply_timeout=10.0)
    sm.get_history = _history_without_reply  # 即使历史没回复也不该被静默恢复
    future = sm.send_message(_SESSION, "你好", display_text="你好")
    turn = sm._pending_by_session[_SESSION.session_id]
    for event in (
        {"type": "thinking_start", "sessionId": _SESSION.session_id,
         "sessionPath": _SESSION.session_path, "streamId": "s1", "seq": 1},
        {"type": "text_delta", "sessionId": _SESSION.session_id,
         "sessionPath": _SESSION.session_path, "streamId": "s1", "seq": 2, "delta": "晚上好呀"},
        {"type": "turn_end", "sessionId": _SESSION.session_id,
         "sessionPath": _SESSION.session_path, "streamId": "s1", "seq": 3},
    ):
        sm._handle_event(event)
    result = future.result(timeout=5.0)
    assert result.error is None
    assert not getattr(turn, "_silent_recovery_tried", False)


# ── b) 静默 turn 历史无回复 → 不提前判死、继续等；晚到 WS 事件仍能完成 ─────


def test_silent_turn_no_reply_in_history_keeps_waiting_then_ws_completes():
    """静默恢复一次后（历史暂无回复）turn 必须保持 pending：
    随后晚到的 WS 事件仍能正常完成 turn（回复文本来自 WS，而非被提前判死）。"""
    sm = _make_sm(grace=0.05, reply_timeout=10.0)
    sm.get_history = _history_without_reply

    holder: dict = {}

    def _run():
        holder["result"] = sm.send_and_wait(_SESSION, "你好", display_text="你好", timeout=5.0)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(1.5)  # 越过 ~1s 轮询粒度 + 0.05s 宽限 → 静默恢复已尝试过

    turn = sm._pending_by_session[_SESSION.session_id]
    assert turn is not None and not turn.done, "历史无回复时静默恢复不得判死 turn"
    assert getattr(turn, "_silent_recovery_tried", False), "应已尝试过一次静默恢复"

    # 晚到的 WS 事件（服务端 1.5s 后才开始流式）仍能正常完成
    for event in (
        {"type": "thinking_start", "sessionId": _SESSION.session_id,
         "sessionPath": _SESSION.session_path, "streamId": "s1", "seq": 10},
        {"type": "text_delta", "sessionId": _SESSION.session_id,
         "sessionPath": _SESSION.session_path, "streamId": "s1", "seq": 11, "delta": "晚上好呀（晚到）"},
        {"type": "turn_end", "sessionId": _SESSION.session_id,
         "sessionPath": _SESSION.session_path, "streamId": "s1", "seq": 12},
    ):
        sm._handle_event(event)
    t.join(timeout=3.0)

    result = holder.get("result")
    assert result is not None
    assert result.error is None, f"晚到 WS 事件应正常完成 turn: {result.error}"
    assert result.text == "晚上好呀（晚到）"
    assert turn.done, "turn 应通过晚到的 WS 事件正常完成"


# ── c) 【疑似源码 Bug】静默恢复期间历史 REST 瞬时失败 → 不应判死 turn ──────


def test_silent_recovery_transient_history_error_must_not_kill_turn():
    """设计意图（commit 说明）：静默恢复 finish_on_missing=False —— 历史拿不到最终
    回复时"绝不提前打断仍可能正在处理的 turn"。
    若静默恢复期间 get_history 抛瞬时 REST 异常，_recover_from_history 的
    except 分支会无视 finish_on_missing=False 直接 _finish_with_error：
    1) turn 被提前判死（~1s 而非等回复/超时墙）；
    2) _finish_with_error 还会 abort 服务端 turn —— 可能取消仍在生成的回复。
    本测试断言：瞬时 REST 失败不得判死 turn（应继续等待，稍后 WS 事件仍可完成）。
    """
    sm = _make_sm(grace=0.05, reply_timeout=10.0)

    def _boom(session, limit=30):
        raise HanakoSessionError("history REST transient failure")

    sm.get_history = _boom

    holder: dict = {}

    def _run():
        holder["result"] = sm.send_and_wait(_SESSION, "你好", display_text="你好", timeout=5.0)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(1.5)  # 越过 ~1s 轮询 + 0.05s 宽限 → 静默恢复已触发

    turn = sm._pending_by_session.get(_SESSION.session_id)
    if turn is not None:
        assert not turn.done, "静默恢复期间历史 REST 瞬时失败不得判死 turn（应保持 pending）"
    # 若 turn 仍 pending：晚到的 WS 事件应能完成它
    if turn is not None and not turn.done:
        sm._handle_event({
            "type": "text_delta", "sessionId": _SESSION.session_id,
            "sessionPath": _SESSION.session_path, "streamId": "s1",
            "seq": 1, "delta": "晚上好呀（瞬时失败后仍在处理）",
        })
        sm._handle_event({
            "type": "turn_end", "sessionId": _SESSION.session_id,
            "sessionPath": _SESSION.session_path, "streamId": "s1", "seq": 2,
        })
    t.join(timeout=3.0)

    result = holder.get("result")
    assert result is not None
    assert result.error is None, (
        f"【源码 Bug 复现】静默恢复期间历史 REST 瞬时失败不应判死 turn，"
        f"实际 error={result.error!r}，且 abort 已发送 {len(sm.ws_client.aborts)} 次"
    )


# ── d) 【已修复】同 session 的其他事件不再被静默 turn accept ──────────────


def test_unrelated_session_event_rejected_for_silent_turn():
    """BugFix #3：同 session 的"别的消息"事件（不同 clientMessageId、带 seq+streamId、
    经 session fallback 路由到本 turn）必须被拒绝——不绑定 stream、不推进 last_seq、
    不置 received_any_event、不刷新 last_event_ts。

    旧行为（QA finding#e）：事件被静默 turn accept → 掩盖"静默" → 20s 快速恢复
    不触发（退回 Bug A 路径）→ 桌宠仍可能卡满 reply_timeout；且别的消息的
    text_delta 会污染本 turn 文本。按 team-lead 问题 3 评估已顺手修复
    （_handle_event 前置 clientMessageId 匹配校验，与 _handle_user_echo 同语义）。
    """
    sm = _make_sm(grace=5.0, reply_timeout=10.0)
    future = sm.send_message(_SESSION, "你好", display_text="你好")
    turn = sm._pending_by_session[_SESSION.session_id]

    # 同 session 的"别的消息"echo（不同 clientMessageId，带 seq+streamId）
    sm._handle_event({
        "type": "session_user_message",
        "sessionId": _SESSION.session_id,
        "sessionPath": _SESSION.session_path,
        "streamId": "ext-stream",
        "seq": 7,
        "clientMessageId": "other-client",
        "message": {"clientMessageId": "other-client", "text": "Hanako 窗口里的别的消息"},
    })

    # 外国事件被拒绝：不绑定 stream、不推进 last_seq、不置 received_any_event
    assert turn.last_seq == 0, "无关事件不得推进 last_seq（已修复：不再被静默 turn accept）"
    assert turn.stream_id is None, "无关事件不得绑定 stream"
    assert not turn.received_any_event, "无关事件不得标记本 turn 为活跃"
    assert turn.acked is False, "非本 turn 的 echo 不应置 acked"
    # 不误杀：turn 仍未完成（等真正的回复/超时）
    assert not turn.done
    assert future is not None
