# -*- coding: utf-8 -*-
"""BugFix #5-E 单测：长任务完成带摘要汇报（celebrating 分支）。

- HanakoMonitor 缓存最近一次成功 tool_end 事件；
- get_last_tool_end_summary()：
    - details（dict.text/content 或 str）或 summary 字段 → 清洗精简后返回；
    - 清洗后过短（< TOOL_END_SUMMARY_MIN_CHARS）且耗时短 → 空串（维持原动画）；
    - 无实质摘要但工具链耗时 >= 30s（长任务）→ 回退"长任务完成啦"；
    - success=False 的 tool_end 不缓存。
- BubbleMixin._do_celebrating(summary)：有摘要 → 摘要气泡；无摘要 → "完成啦！"。

运行: python -m pytest tests/test_bugfix5_celebration_summary.py -v
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.hanako_monitor import HanakoMonitor  # noqa: E402
from pet_mixins.bubble_mixin import BubbleMixin  # noqa: E402


def _push_tool_end(monitor: HanakoMonitor, **kw):
    ev = {"type": "tool_end", "name": "bash", "success": True}
    ev.update(kw)
    monitor.push_event(ev)
    return ev


# ── HanakoMonitor 摘要提取 ────────────────────────────────


def test_monitor_stores_tool_end_details_text():
    m = HanakoMonitor()
    _push_tool_end(m, details={"text": "签到完成，积分 +50，连续签到 7 天！"})
    s = m.get_last_tool_end_summary()
    assert "签到完成" in s


def test_monitor_stores_tool_end_details_content():
    m = HanakoMonitor()
    _push_tool_end(m, details={"content": "报告已生成，共 12 页，含 3 张图表"})
    s = m.get_last_tool_end_summary()
    assert "报告已生成" in s


def test_monitor_stores_tool_end_details_str():
    m = HanakoMonitor()
    _push_tool_end(m, details="已为你完成文件整理，共 42 个文件")
    s = m.get_last_tool_end_summary()
    assert "已为你完成文件整理" in s


def test_monitor_stores_tool_end_summary_field():
    m = HanakoMonitor()
    _push_tool_end(m, summary="已为你生成复盘报告，共 12 页")
    s = m.get_last_tool_end_summary()
    assert "已为你生成复盘报告" in s


def test_monitor_short_details_no_summary():
    """过短（< 8 字）且耗时短 → 无实质摘要。"""
    m = HanakoMonitor()
    _push_tool_end(m, details="ok")
    assert m.get_last_tool_end_summary() == ""


def test_monitor_long_duration_fallback():
    """无实质摘要但工具链耗时 >= 30s → 长任务完成汇报。"""
    m = HanakoMonitor()
    _push_tool_end(m, details="ok", duration=45)
    assert m.get_last_tool_end_summary() == "长任务完成啦"


def test_monitor_elapsed_ms_duration_fallback():
    m = HanakoMonitor()
    _push_tool_end(m, details="ok", elapsedMs=45_000)
    assert m.get_last_tool_end_summary() == "长任务完成啦"


def test_monitor_short_duration_no_summary():
    m = HanakoMonitor()
    _push_tool_end(m, details="ok", duration=5)
    assert m.get_last_tool_end_summary() == ""


def test_monitor_failed_tool_end_not_cached():
    m = HanakoMonitor()
    _push_tool_end(m, success=False, details="失败详情文本长度足够")
    assert m.get_last_tool_end_summary() == ""


# ── BubbleMixin._do_celebrating 分支 ──────────────────────


class _FakeTimer:
    def __init__(self):
        self.stopped = 0
        self.started = 0

    def stop(self):
        self.stopped += 1

    def start(self, ms):
        self.started += 1


def _make_celebrating_self():
    fake = SimpleNamespace(
        config={"celebrating": {"enabled": True, "tts_enabled": False},
                "tts": {"enabled": True}},
        _status_mapper=None,
        _renderer=object(),
        _celebration_in_progress=False,
        _last_celebrating_at=0.0,
        _pet_revert_timer=_FakeTimer(),
        _bubble_texts=[],
    )
    fake._set_surface_emotion = MagicMock()
    fake._show_bubble = lambda text, emotion="neutral", priority=0: fake._bubble_texts.append(text)
    fake._synth_celebration_tts = MagicMock()
    return fake


def _call_do_celebrating(fake, summary=""):
    """调用 _do_celebrating，并屏蔽 QTimer（离屏无事件循环）。"""
    import PySide6.QtCore as QtCore

    with patch.object(
        QtCore, "QTimer",
        SimpleNamespace(singleShot=staticmethod(lambda ms, cb: None)),
    ):
        BubbleMixin._do_celebrating(fake, summary=summary)


def test_do_celebrating_with_summary():
    """E：有摘要 → 气泡显示摘要（不只是"完成啦"）。"""
    fake = _make_celebrating_self()
    _call_do_celebrating(fake, summary="签到完成，积分 +50")
    assert fake._bubble_texts == ["签到完成，积分 +50"]


def test_do_celebrating_without_summary():
    """E：无摘要 → 维持原"完成啦！"庆祝动画。"""
    fake = _make_celebrating_self()
    _call_do_celebrating(fake, summary="")
    assert fake._bubble_texts == ["完成啦！"]


def test_get_celebration_summary_delegates():
    """E：_get_celebration_summary 从 HanakoMonitor 取摘要。"""
    monitor = HanakoMonitor()
    _push_tool_end(monitor, details={"text": "报告已生成，共 12 页"})
    fake = SimpleNamespace(_hanako_monitor=monitor)
    s = BubbleMixin._get_celebration_summary(fake)
    assert "报告已生成" in s


def test_get_celebration_summary_no_monitor():
    """E：无 monitor → 空串（维持原庆祝动画）。"""
    assert BubbleMixin._get_celebration_summary(SimpleNamespace(_hanako_monitor=None)) == ""
