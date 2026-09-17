# -*- coding: utf-8 -*-
"""回归：状态气泡节流 —— 占位文案不得刷屏，真实回复不得被覆盖。

现象（实测日志，2026-09-12 11:53）：

    11:53:10  正在思考…
    11:53:12  正在回复…
    11:53:12  正在使用 tool_search…
    11:53:12  正在使用 extension_manager…
    11:53:12  工具执行完成…
    11:53:12  ⚠️ 工具执行失败… 出了问题
    …（7 秒内 22 个气泡）

全量统计：564 个气泡里 339 个（60%）是这类占位文案。

修复（2026-09-17）：
  - `_do_engine_status` 对状态类气泡做 1.5s 窗口节流，窗口内只发最后一条
  - 工具失败/错误提示**不过滤**（用户该知道的信息）
  - 真实回复到达时 `_cancel_pending_status()` 取消待发状态

本测试只锁行为契约，不依赖真实 Qt 事件循环（用 fake 替身）。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeBubble:
    def __init__(self):
        self.visible = False

    def isVisible(self):
        return self.visible

    def hide_bubble(self):
        self.visible = False


class _FakeTimer:
    def __init__(self):
        self.started = []
        self.stopped = 0

    def start(self, ms):
        self.started.append(ms)

    def stop(self):
        self.stopped += 1


class _Pet:
    """最小替身：只提供 _do_engine_status 依赖的属性与方法。"""

    from pet import PetWindow as _PW

    _STATUS_BUBBLE_THROTTLE_MS = _PW._STATUS_BUBBLE_THROTTLE_MS
    _is_important_status = staticmethod(_PW._is_important_status)
    _flush_status_bubble = _PW._flush_status_bubble
    _flush_status_bubble_pending = _PW._flush_status_bubble_pending
    _cancel_pending_status = _PW._cancel_pending_status
    _do_engine_status = _PW._do_engine_status

    def __init__(self):
        self.bubble = _FakeBubble()
        self.shown = []
        self._status_bubble_last_ms = 0.0
        self._status_bubble_pending = ""
        self._status_bubble_timer = _FakeTimer()

    def _show_bubble(self, text, emotion="neutral", priority=0, duration_ms=0, source=""):
        self.shown.append((text, emotion))


def _pet():
    return _Pet()


# ── 一、节流生效 ─────────────────────────────────────────────────────────────


def test_rapid_status_bubbles_collapse():
    """密集状态只上屏第一条，其余进待发槽（不逐条闪）。"""
    p = _pet()
    for msg in ("正在思考…", "正在回复…", "正在使用 tool_search…", "工具执行完成…"):
        p._do_engine_status(msg)

    assert len(p.shown) == 1, f"窗口内只应上屏 1 条，实得 {p.shown!r}"
    assert p.shown[0][0] == "正在思考…"
    assert p._status_bubble_pending == "工具执行完成…", "待发槽应保留最后一条"


def test_pending_flush_emits_last_one():
    """窗口到期 → 补发窗口内最后一条（而不是第一条）。"""
    p = _pet()
    p._do_engine_status("正在思考…")
    p._do_engine_status("正在回复…")
    p._do_engine_status("工具执行完成…")

    p._flush_status_bubble_pending()
    assert [t for t, _ in p.shown] == ["正在思考…", "工具执行完成…"]
    assert p._status_bubble_pending == "", "补发后待发槽应清空"


def test_window_expiry_allows_next_bubble():
    """窗口过去后，新状态正常上屏。"""
    p = _pet()
    p._do_engine_status("正在思考…")
    assert len(p.shown) == 1
    # 模拟窗口已过
    p._status_bubble_last_ms = 0.0
    p._do_engine_status("正在回复…")
    assert len(p.shown) == 2


# ── 二、重要信息不得被吞 ─────────────────────────────────────────────────────


def test_tool_failure_bypasses_throttle():
    """工具失败必须立即上屏——它是用户该知道的信息，不能被节流吞掉。"""
    p = _pet()
    p._do_engine_status("正在思考…")  # 占用节流窗口
    p._do_engine_status("⚠️ 工具执行失败… 出了问题")
    p._do_engine_status("工具执行失败…")

    texts = [t for t, _ in p.shown]
    assert "正在思考…" in texts
    assert any("失败" in t for t in texts), f"失败提示必须上屏: {texts!r}"


def test_important_status_detector():
    from pet import PetWindow

    assert PetWindow._is_important_status("⚠️ 工具执行失败… 出了问题")
    assert PetWindow._is_important_status("工具执行失败…")
    assert PetWindow._is_important_status("发生错误")
    assert PetWindow._is_important_status("异常")
    # 普通状态不算重要
    assert not PetWindow._is_important_status("正在思考…")
    assert not PetWindow._is_important_status("正在回复…")
    assert not PetWindow._is_important_status("工具执行完成…")


# ── 三、真实回复取消待发状态 ─────────────────────────────────────────────────


def test_cancel_pending_status_clears_queue():
    """真实回复上屏前取消待发状态，避免迟到的状态把回复盖掉。"""
    p = _pet()
    p._do_engine_status("正在思考…")
    p._do_engine_status("工具执行完成…")  # 进待发槽
    assert p._status_bubble_pending

    p._cancel_pending_status()
    assert p._status_bubble_pending == ""
    assert p._status_bubble_timer.stopped >= 1

    # 取消后再补发 → 什么都不发（回复不该被覆盖）
    before = len(p.shown)
    p._flush_status_bubble_pending()
    assert len(p.shown) == before, "取消后不得再补发状态气泡"


def test_empty_status_hides_bubble():
    """空状态 → 隐藏气泡（原有行为不能回退）。"""
    p = _pet()
    p.bubble.visible = True
    p._do_engine_status("")
    assert p.bubble.isVisible() is False
