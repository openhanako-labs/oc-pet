# -*- coding: utf-8 -*-
"""栖息接线验收（O1-P4）。

分两层：
  - **真机只读**：`window_target` 真去问一次 Windows（不改任何窗口）。
  - **源码护栏**：菜单入口、每帧 tick、缩小/恢复、以及"不假装检测拖动"。
纯几何计算在 test_perch.py。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from motion import window_target as wt


# ── 真机只读 ──────────────────────────────────────────────


def test_bogus_hwnd_is_reported_as_missing():
    """查不到就要老实返回 None/False——不能假装成功。"""
    assert wt.rect_of(0) is None
    assert wt.is_alive(0) is False
    assert wt.title_of(0) == ""


def test_own_pid_is_positive():
    assert wt.own_pid() > 0


def test_foreground_target_shape():
    t = wt.foreground_target()
    if t is None:
        pytest.skip("当前没有前台窗口")
    assert t["hwnd"] > 0
    assert len(t["rect"]) == 4
    assert t["rect"][2] > 0 and t["rect"][3] > 0
    assert isinstance(t["own"], bool)
    assert isinstance(t["title"], str)


def test_own_window_is_flagged():
    """桌宠自己被当成目标时要能认出来（否则它会站到自己身上）。"""
    pid = wt.own_pid()
    assert wt.pid_of_window(0) != pid, "0 不是我们的窗口"


def test_screen_rect_shape():
    s = wt.screen_rect()
    if s is None:
        pytest.skip("拿不到工作区矩形")
    assert s[2] > 0 and s[3] > 0


# ── 源码护栏 ──────────────────────────────────────────────


def _read(rel: str) -> str:
    return Path(__file__).resolve().parents[1].joinpath(rel).read_text(encoding="utf-8")


def _pet_src() -> str:
    return _read("pet.py")


def _mixin_src() -> str:
    return _read("pet_mixins/perch_mixin.py")


def test_menu_has_a_perch_entry():
    s = _pet_src()
    assert '"🪟 站在窗口上"' in s
    assert "self._perch_action.setCheckable(True)" in s


def test_tick_follows_the_perch():
    """每帧 tick 里必须真的跟（否则"站在窗口上"只站一帧）。"""
    s = _pet_src()
    idx = s.index("# 8. 栖息：站在窗口上")
    body = s[idx:idx + 260]
    assert "self._perch_tick()" in body


def test_mixin_is_actually_composed():
    """搬进 mixin 后**必须真的组装进 PetWindow**——否则等于功能被搬没了。"""
    code = _read("pet.py")
    assert "from pet_mixins.perch_mixin import PerchMixin" in code
    cls = code[code.index("class PetWindow("):]
    cls = cls[:cls.index("):")]
    assert "PerchMixin" in cls, "PerchMixin 没加进基类 = 功能消失"


def test_pet_py_stays_thin():
    """项目规矩：pet.py 不得继续膨胀（有专门的护栏测试守这条）。
    这里额外盯一次，因为我刚因为这事被护栏抓过。
    """
    n = len(_pet_src().splitlines())
    assert n < 3550, f"pet.py 已 {n} 行（上限 3550）——新行为应进 mixin"


def test_start_perch_shrinks_first():
    """miku 太大：不先缩小就根本站不住，所以缩小必须发生在选位之前。"""
    s = _mixin_src()
    body = s[s.index("def _start_perch"):]
    body = body[:body.index("def _perch_shrink_to_fit")]
    assert "_perch_shrink_to_fit(" in body
    assert body.index("_perch_shrink_to_fit(") < body.index("self._perch_step()")


def test_failed_perch_says_why_and_cleans_up():
    s = _mixin_src()
    body = s[s.index("def _start_perch"):]
    body = body[:body.index("def _perch_shrink_to_fit")]
    assert "silent=False" in body, "站不上去要让用户知道，不能默默不动"


def test_stop_perch_restores_scale_and_position():
    s = _mixin_src()
    body = s[s.index("def _stop_perch"):]
    assert "scale_before" in body and "self._apply_scale()" in body
    assert "pos_before" in body and "self.move(" in body
    assert "setChecked(False)" in body, "下车要取消菜单勾选，否则状态对不上"


def test_shrink_is_bounded():
    s = _mixin_src()
    assert "PERCH_MIN_SCALE" in s, "缩到看不清比站不住更糟"
    assert "max(self.PERCH_MIN_SCALE" in s


def test_no_pretend_drag_detection():
    """不装模作样地检测拖动：每帧都在移动它，判不准——这一点必须写在代码里。"""
    s = _mixin_src()
    assert "不做" in s and "拖动" in s, "要留下为什么不做拖动检测的说明"
    assert "dragging=False" in s
