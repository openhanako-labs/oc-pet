# -*- coding: utf-8 -*-
"""栖息（站在窗口上）纯计算部分的验收。

不需要桌面、不需要 Qt——所以能把边界全试一遍：
夹取、上方没地方、全屏、太小、以及"什么时候该下来"。
"""
from __future__ import annotations

from motion.perch import (
    REASON_DRAGGING,
    REASON_MINIMIZED,
    REASON_TARGET_GONE,
    REASON_TIMEOUT,
    PerchPlan,
    Rect,
    plan_perch,
    release_reason,
)

SCREEN = Rect(0, 0, 1920, 1080)
PET = (440, 830)          # miku 实测尺寸
SMALL_PET = (200, 380)   # 真正的"站得上去"需要小形象（见文件末尾的约束测试）


def _plan(window, pet=SMALL_PET, **kw):
    return plan_perch(window, *pet, screen=SCREEN, **kw)


# ── 站哪儿 ────────────────────────────────────────────────


def test_no_target():
    p = _plan(None)
    assert p.ok is False and "没有可站" in p.reason


def test_invalid_rect():
    assert _plan(Rect(100, 100, 100, 500)).ok is False
    assert _plan(Rect(100, 100, 500, 100)).ok is False


def test_too_small_window_is_refused():
    """太小站上去就挡事了。"""
    p = _plan(Rect(0, 0, 120, 80))
    assert p.ok is False and "太小" in p.reason


def test_stands_on_top_edge_of_window():
    """脚踩在窗框上：宠物底边 = 窗口顶边 - 留白。"""
    win = Rect(200, 400, 1400, 1000)
    p = _plan(win, margin=6)
    assert p.ok and p.on_edge == "top"
    assert p.y + SMALL_PET[1] == win.top - 6
    # 横向居中
    assert p.x == win.left + (win.width - SMALL_PET[0]) // 2


def test_horizontally_centered_and_never_negative():
    win = Rect(0, 400, 1200, 900)
    p = _plan(win)
    assert p.ok and p.x >= 0


def test_clamped_into_screen_on_the_right():
    """窗口贴近右边缘时，宠物不能跑到屏幕外。"""
    win = Rect(1500, 400, 1915, 900)
    p = plan_perch(win, *SMALL_PET, screen=SCREEN, min_window_w=200)
    assert p.ok
    assert p.x + SMALL_PET[0] <= SCREEN.right


def test_falls_back_below_when_no_room_above():
    """窗口贴着屏幕顶端 → 改站到下沿。"""
    win = Rect(200, 100, 1400, 600)
    p = _plan(win)
    assert p.ok and p.on_edge == "bottom"
    assert p.y == win.bottom + 6


def test_refused_when_no_room_above_or_below():
    """窗口上下都顶到屏幕边，就没地方站了。"""
    win = Rect(200, 0, 1400, 1080)
    p = _plan(win)
    assert p.ok is False and "落脚" in p.reason


def test_fullscreen_window_has_no_frame():
    win = Rect(0, 0, 1920, 1080)
    p = _plan(win)
    assert p.ok is False and "全屏" in p.reason


def test_works_without_screen_rect():
    """没给屏幕信息时也要能用（只是不夹取）。"""
    win = Rect(300, 500, 1300, 900)
    p = plan_perch(win, *SMALL_PET, screen=None)
    assert p.ok and p.on_edge == "top" and p.y == 500 - SMALL_PET[1] - 6


def test_tiny_pet_size_is_safe():
    win = Rect(200, 400, 1400, 1000)
    p = plan_perch(win, 0, 0, screen=SCREEN)
    assert p.ok, "尺寸异常不能崩，按 1 处理"


def test_position_helper():
    win = Rect(200, 400, 1400, 1000)
    p = _plan(win)
    assert isinstance(p, PerchPlan)
    assert p.position() == (p.x, p.y)


# ── 现实约束（这一条最重要） ──────────────────────────────


def test_miku_current_size_cannot_stand_on_a_mid_screen_window():
    """**把现实约束固定下来**：miku 现在 444×827，1080 高的屏幕上
    要踩住窗框，窗口顶边得在 830px 以下——普通窗口都不满足。

    这不是 bug，是尺寸问题：Mate-Engine / Desktop Mate 的形象都小，
    正因为如此。所以真要能看到"站在窗口上"，要么栖息时缩小，要么不做。
    """
    mid_window = Rect(200, 400, 1400, 900)          # 很普通的中屏窗口
    p = plan_perch(mid_window, *PET, screen=SCREEN)
    assert p.ok is False, "830px 的形象在 1080 屏上站不住中屏窗口"

    # 同一个窗口，形象缩小到 0.45 倍高（≈372px）就能站
    p2 = plan_perch(mid_window, 200, 372, screen=SCREEN)
    assert p2.ok and p2.on_edge == "top"


def test_threshold_is_about_pet_height_not_window():
    """阈值取决于**形象高度**：窗口越低越容易站。"""
    low_window = Rect(200, 900, 1400, 1050)
    assert plan_perch(low_window, *PET, screen=SCREEN).ok is True


# ── 什么时候该下来 ────────────────────────────────────────


def test_keeps_standing_when_all_good():
    assert release_reason() == ""
    assert release_reason(following_for=100, ttl=0) == "", "ttl=0 = 不限时"


def test_dragging_wins_over_everything():
    """人的意图优先于环境变化。"""
    assert release_reason(dragging=True, target_seen=False,
                          minimized=True) == REASON_DRAGGING


def test_target_gone_beats_minimized():
    assert release_reason(target_seen=False, minimized=True) == REASON_TARGET_GONE


def test_minimized_releases():
    assert release_reason(minimized=True) == REASON_MINIMIZED


def test_timeout_lasts_respected():
    assert release_reason(following_for=9.9, ttl=10) == ""
    assert release_reason(following_for=10, ttl=10) == REASON_TIMEOUT
