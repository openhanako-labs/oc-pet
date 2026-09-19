# -*- coding: utf-8 -*-
"""事件合并的**真实接线**验收（真类 + 真线程 + 真定时器）。

单元测试只能证明"判定逻辑对"，证明不了"screen.py 真的按它接上了"。
这里用真的 ScreenPerception，只把"打视觉 API"那一步换成记录器，
然后模拟一串快速切窗口，看它到底打了几次。

（不需要真的切窗口，所以稳定、不花 API。）
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from core.perception.screen import ScreenPerception


def _make():
    sp = ScreenPerception(interval=120)
    sp._running = True                       # _flush_burst 只在运行中才冲
    sp.set_burst(enabled=True, quiet_gap_s=0.2, min_age_s=0.1,
                 max_age_s=5.0, max_size=10)
    calls = []

    def fake_capture(mode="timer", app="", title="", burst=None):
        calls.append({"mode": mode, "app": app, "title": title, "burst": burst})
        return None

    sp._capture_and_analyze = fake_capture
    return sp, calls


def _wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_rapid_switches_merge_into_one_api_call():
    sp, calls = _make()
    for app, title in (("Edge", "博客"), ("Code", "pet.py"),
                       ("Terminal", "pwsh"), ("Edge", "博客文章")):
        sp.on_foreground_change(app, "browser", title)

    # 去抖：动手之前不该有任何调用
    assert calls == [], "还没等到安静就打了 API"
    assert _wait_for(lambda: len(calls) == 1), "等安静后应该只打一次"
    time.sleep(0.4)
    assert len(calls) == 1, f"应该只合并成一次，实际 {len(calls)} 次"

    got = calls[0]
    assert got["mode"] == "event"
    # 落在最后停下的那个窗口上（用户真正在看的），不是第一个
    assert got["app"] == "Edge"
    assert got["burst"] is not None and got["burst"].size == 4
    assert got["burst"].window_chain() == "Edge → Code → Terminal → Edge"


def test_lone_event_still_produces_exactly_one_call():
    """不密集时也不能丢——每个事件最终都要被看到。"""
    sp, calls = _make()
    sp.on_foreground_change("Edge", "browser", "A")
    assert _wait_for(lambda: len(calls) == 1)
    time.sleep(0.3)
    assert len(calls) == 1


def test_event_after_long_pause_is_its_own_call():
    sp, calls = _make()
    sp.on_foreground_change("Edge", "browser", "A")
    assert _wait_for(lambda: len(calls) == 1)
    time.sleep(0.3)
    sp.on_foreground_change("Code", "editor", "B")
    assert _wait_for(lambda: len(calls) == 2)
    assert calls[1]["app"] == "Code"
    assert calls[1]["burst"].size == 1


def test_disabled_burst_falls_back_to_old_behaviour():
    """关掉合并要能回退旧行为（立刻分析），不能哑掉。"""
    sp = ScreenPerception(interval=120)
    sp._running = True
    sp.set_burst(enabled=False)
    calls = []
    sp._capture_and_analyze = lambda mode="timer", app="", title="", burst=None: calls.append(
        {"app": app, "burst": burst})
    sp.on_foreground_change("Edge", "browser", "A")
    assert len(calls) == 1 and calls[0]["app"] == "Edge"
    assert calls[0]["burst"] is None, "旧路径不该带爆发"


def test_blacklist_still_short_circuits():
    sp, calls = _make()
    sp._blacklist_enabled = True
    try:
        sp.on_foreground_change("KeePassXC", "tool", "密码库")
    except Exception:
        pass
    time.sleep(0.4)
    assert calls == [], "黑名单窗口不该进合并队列"


def test_stop_discards_pending_burst():
    """停了就不该再冒出一条 API。"""
    sp, calls = _make()
    sp.on_foreground_change("Edge", "browser", "A")
    sp.cancel_pending_burst()
    sp._running = False
    time.sleep(0.5)
    assert calls == []


def test_capture_hint_is_available_for_the_prompt():
    """合并顺带产出的一句人话——下一步接进提示词要用它。"""
    sp, calls = _make()
    for app in ("Edge", "Code", "Chrome"):
        sp.on_foreground_change(app, "x", "t")
    assert _wait_for(lambda: len(calls) == 1)
    hint = calls[0]["burst"].hint()
    assert "Edge" in hint and "Chrome" in hint


def test_screen_records_last_burst():
    """实例上留得住最近一次爆发（走**真方法**的前置部分，不打 API）。

    不能把 _capture_and_analyze 整个换成假函数——_last_burst 是在真方法里设的，
    换了假函数就测不到接线。改用 _vision_disabled 短路它后面的 API 步骤。
    """
    sp = ScreenPerception(interval=120)
    sp._running = True
    sp._vision_disabled = True
    sp.set_burst(enabled=True, quiet_gap_s=0.2, min_age_s=0.1)
    sp.on_foreground_change("Edge", "browser", "A")
    assert _wait_for(lambda: getattr(sp, "_last_burst", None) is not None)
    assert sp._last_burst.size == 1
    assert sp._last_burst.window_chain() == "Edge"


# ── 另一半：把"他刚从哪过来"喂进视觉提问 ──────────────────


def test_vision_prompt_unchanged_without_any_hint():
    """三个参数全空时必须**原样**返回——不能顺手改掉旧行为。"""
    from core.perception.screen import VISION_PROMPT, build_vision_prompt

    assert build_vision_prompt() == VISION_PROMPT
    assert build_vision_prompt("", "") == VISION_PROMPT


def test_vision_prompt_keeps_the_window_hint():
    from core.perception.screen import build_vision_prompt

    p = build_vision_prompt("Code", "pet.py - oc-pet")
    assert "[当前窗口]" in p and "Code" in p
    assert "[最近窗口切换]" not in p, "没有爆发序列时不该出现这一段"


def test_vision_prompt_carries_the_burst_sequence():
    from core.perception.screen import build_vision_prompt

    p = build_vision_prompt("Code", "pet.py", recent="这 1.2s 里切过 2 次窗口：Edge → Code（最后停在 Code）")
    assert "[最近窗口切换]" in p
    assert "Edge → Code" in p


def test_vision_prompt_warns_not_to_confuse_sequence_with_screen():
    """必须明说"别把序列当本屏内容"——否则模型会把窗口名写进描述，比不说还糟。"""
    from core.perception.screen import build_vision_prompt

    p = build_vision_prompt("Code", "t", recent="Edge → Code")
    assert "不要" in p and "截图" in p


def test_vision_prompt_has_both_sections():
    from core.perception.screen import build_vision_prompt

    p = build_vision_prompt("Code", "t", recent="Edge → Code")
    assert p.index("[当前窗口]") < p.index("[最近窗口切换]"), "顺序：先说当前，再说刚切过"


def test_capture_passes_the_hint_into_the_prompt():
    """源码护栏：_capture_and_analyze 必须把 burst.hint() 传进 build_vision_prompt。"""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1].joinpath(
        "core", "perception", "screen.py").read_text(encoding="utf-8")
    body = src[src.index("def _capture_and_analyze"):]
    assert "burst.hint()" in body
    assert "recent=" in body


def test_live_burst_hint_is_what_gets_fed():
    """端到端：合并出来的 hint 就是将要被喂进提问的那句（真类、真定时器）。"""
    sp, calls = _make()
    for app in ("Edge", "Code", "Chrome"):
        sp.on_foreground_change(app, "x", "t")
    assert _wait_for(lambda: len(calls) == 1)
    from core.perception.screen import build_vision_prompt

    prompt = build_vision_prompt(calls[0]["app"], calls[0]["title"],
                                 recent=calls[0]["burst"].hint())
    assert "[最近窗口切换]" in prompt
    assert "Edge" in prompt and "Chrome" in prompt
