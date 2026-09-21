# -*- coding: utf-8 -*-
"""庆祝时机回归锁定：**只在对话回合结束庆祝，工具结束不庆祝**（2026-09-21）。

## 起因（真机现象）

桌宠一直在播报「完成啦！」——实测 19:28–19:30 三分钟内播了 6 次。

根因：庆祝挂在 `tool_end success` 上，而一次对话里几十个工具调用 = 几十次
庆祝（撒花 + happy + 气泡 + **语音**）。修复生效（监视归属绑对键）之后，
助手自己的主对话第一次真正进入监视范围，这条链路才第一次被真实流量压到。

## 不变量

1. `tool_end` success **不再**产生 celebrating（工具结束只是中间步骤）
2. `tool_end` failure 仍然报「遇到问题」——那是真信号，不静音
3. 庆祝只在 `turn_end`，且**本回合真的跑过工具**时（避免每句闲聊都撒花）
4. 两次庆祝之间有最小间隔（防 WS 重放/镜像带来的成串 turn_end）
5. `turn_end` 不庆祝时，仍然要回 idle（watchdog 语义不能被破坏）
"""
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.hanako_monitor import (  # noqa: E402
    CELEBRATE_MIN_INTERVAL, HanakoMonitor, map_event_to_mood,
)
from pet_mixins.bubble_mixin import BubbleMixin  # noqa: E402


def _mon():
    calls = []

    def rec(anim, msg, emotion="neutral", state="idle", audio_path=""):
        calls.append({"anim": anim, "msg": msg, "emotion": emotion, "state": state})

    return HanakoMonitor(on_state_change=rec), calls


# ── map_event_to_mood（纯函数）────────────────────────────

def test_tool_success_does_not_celebrate():
    mood, msg, _emo = map_event_to_mood({"type": "tool_end", "name": "bash",
                                         "success": True})
    assert mood != "celebrating", f"工具成功不该庆祝：{mood}"
    assert "完成" not in (msg or "")


def test_tool_failure_still_reports():
    """失败是真信号，用户没要求静音它。"""
    mood, msg, emo = map_event_to_mood({"type": "tool_end", "success": False})
    assert mood == "error" and msg == "遇到问题" and emo == "angry"


def test_turn_end_maps_to_celebrating():
    mood, msg, emo = map_event_to_mood({"type": "turn_end"})
    assert mood == "celebrating"
    assert emo == "happy"
    assert msg == "", "message 必须留空——庆祝分支自己决定气泡，别覆盖对话回复"


# ── push_event（真实路径）────────────────────────────────

def test_turn_end_after_tool_celebrates():
    mon, calls = _mon()
    mon.push_event({"type": "tool_start", "name": "bash"})
    mon.push_event({"type": "turn_end"})
    assert calls and calls[-1]["state"] == "celebrating", calls


def test_turn_end_without_tool_returns_idle():
    """没干活的回合（纯闲聊）不该撒花播报。"""
    mon, calls = _mon()
    mon.push_event({"type": "turn_end"})
    assert calls and calls[-1]["state"] == "idle", calls


def test_turn_end_still_resets_to_idle_when_not_celebrating():
    """watchdog 语义不能被庆祝改造破坏：不庆祝也要回 idle。"""
    mon, calls = _mon()
    mon.push_event({"type": "turn_end"})
    assert calls[-1]["state"] == "idle"
    assert mon._current_state_name == "idle"


def test_tool_end_success_never_celebrates_in_real_path():
    mon, calls = _mon()
    for _ in range(5):
        mon.push_event({"type": "tool_end", "name": "bash", "success": True})
    assert all(c["state"] != "celebrating" for c in calls), calls


def test_celebration_throttled_within_interval():
    mon, calls = _mon()
    mon.push_event({"type": "tool_start"})
    mon.push_event({"type": "turn_end"})
    assert calls[-1]["state"] == "celebrating"
    # 紧接着再来一轮 → 间隔不够，只回 idle
    mon.push_event({"type": "tool_start"})
    mon.push_event({"type": "turn_end"})
    assert calls[-1]["state"] == "idle", f"没有按 {CELEBRATE_MIN_INTERVAL}s 节流"


def test_celebration_allowed_after_interval():
    mon, calls = _mon()
    mon.push_event({"type": "tool_start"})
    mon.push_event({"type": "turn_end"})
    assert calls[-1]["state"] == "celebrating"
    mon._celebrate_last_push = time.time() - CELEBRATE_MIN_INTERVAL - 1
    mon.push_event({"type": "tool_start"})
    mon.push_event({"type": "turn_end"})
    assert calls[-1]["state"] == "celebrating", "过了间隔还不庆祝"


def test_tool_flag_resets_every_turn():
    mon, calls = _mon()
    mon.push_event({"type": "tool_start"})
    mon.push_event({"type": "turn_end"})
    assert mon._turn_had_tool is False, "工具标记必须每回合清零"
    mon._celebrate_last_push = 0.0
    mon.push_event({"type": "turn_end"})          # 这一轮没工具
    assert calls[-1]["state"] == "idle"


def test_tool_progress_also_counts_as_tool():
    mon, calls = _mon()
    mon.push_event({"type": "tool_progress", "name": "bash"})
    mon.push_event({"type": "turn_end"})
    assert calls[-1]["state"] == "celebrating"


# ── 气泡不盖回复 ──────────────────────────────────────────

class _FakeTimer:
    def stop(self):
        pass

    def start(self, ms):
        pass


def _fake():
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
    fake._show_bubble = lambda text, emotion="neutral", priority=0: \
        fake._bubble_texts.append(text)
    fake._synth_celebration_tts = MagicMock()
    return fake


def _call(fake, **kw):
    import PySide6.QtCore as QtCore

    with patch.object(QtCore, "QTimer",
                      SimpleNamespace(singleShot=staticmethod(lambda ms, cb: None))):
        BubbleMixin._do_celebrating(fake, **kw)


def test_no_fallback_bubble_when_disabled():
    """fallback_bubble=False + 无摘要 → 不出气泡（否则会盖掉助手回复）。"""
    fake = _fake()
    _call(fake, summary="", fallback_bubble=False)
    assert fake._bubble_texts == [], fake._bubble_texts


def test_summary_bubble_still_shows():
    fake = _fake()
    _call(fake, summary="共修复 3 个文件", fallback_bubble=False)
    assert fake._bubble_texts == ["共修复 3 个文件"]


def test_default_keeps_legacy_bubble():
    """老调用点（不传 fallback_bubble）行为不变——BugFix #5-E 的既有契约。"""
    fake = _fake()
    _call(fake, summary="")
    assert fake._bubble_texts == ["完成啦！"]
