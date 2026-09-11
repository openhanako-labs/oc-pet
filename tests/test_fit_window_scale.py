"""窗口贴合的尺寸语义 —— 缩放只能乘一次。

## 修之前是什么样

渲染器测出的 w/h 是**当前 _pet_scale 的视口里**量的，也就是「窗口像素」。
而 `_base_w/_base_h` 是「未缩放基准」，窗口 = 基准 × _pet_scale。

旧代码把测量值直接当基准 → 缩放被乘了两次 → **窗口尺寸与 scale 成平方关系**：

    S=1.0 → 451x833     （看不出问题，所以一直没被发现）
    S=1.8 → 1458x2698   （屏幕 2048x1152，窗口高出 2.3 倍）

代价不只是「太大」：那张比屏幕还高的透明窗口会吞掉鼠标滚轮。
实测：用户的 scale 被这样从 1.8 滚到了 0.45。

## 锁住的不变量

    **基准尺寸必须与 scale 无关**（同一个模型，无论 S 取多少，
      量出来除回去之后都该是同一个基准）。

这一条正是旧代码违反的：它的基准随 S 线性增长。
"""
from __future__ import annotations

import types

import pytest

pet_mod = pytest.importorskip("pet")


# ── 轻量替身：只提供 fit_window_to_model 用到的那几样 ──

class _Point:
    def __init__(self, x=0, y=0):
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y


class _Rect:
    def __init__(self, x=0, y=0, w=0, h=0):
        self._x, self._y, self._w, self._h = x, y, w, h

    def center(self):
        return _Point(self._x + self._w // 2, self._y + self._h // 2)

    def bottom(self):
        return self._y + self._h - 1


class _Screen:
    """可用区域（已经扣掉任务栏）—— 与 QRect 同形。"""

    def __init__(self, w=2048, h=1104):
        self._w, self._h = w, h

    def top(self):
        return 0

    def left(self):
        return 0

    def right(self):
        return self._w - 1

    def bottom(self):
        return self._h - 1

    def height(self):
        return self._h

    def width(self):
        return self._w


class _FakePet:
    def __init__(self, scale, visible=True, geo=None, screen=None):
        self._pet_scale = scale
        self._base_w = 0
        self._base_h = 0
        self.final = None
        self._visible = visible
        self._geo = geo or _Rect(700, 200, 450, 833)
        # 默认给一个「够大」的屏幕，让尺寸用例只考察缩放语义；
        # 屏幕约束由显式传 screen 的用例考察。
        self._screen = screen or _Screen(10000, 10000)
        self.moved_to = None
        self._renderer = types.SimpleNamespace(
            recalc_geometry=lambda w, h: None)
        # 绑真实实现：这样 fit 路径会一路跑到真正的尺寸/定位逻辑
        self._resize_keeping_visible = types.MethodType(
            pet_mod.PetWindow._resize_keeping_visible, self)
        self._clamp_size_to_screen = types.MethodType(
            pet_mod.PetWindow._clamp_size_to_screen, self)

    # fit_window_to_model 末尾会用 QTimer.singleShot 延后调这两个；
    # Python 会先求值 self._store_label_pos，所以替身必须有同名方法。
    def _store_label_pos(self):
        pass

    def _reposition_bubble(self):
        pass

    def setFixedSize(self, w, h):
        self.final = (w, h)

    def isVisible(self):
        return self._visible

    def frameGeometry(self):
        return self._geo

    def move(self, x, y):
        self.moved_to = (x, y)

    def _current_screen_geometry(self):
        return self._screen


@pytest.fixture(autouse=True)
def _no_qt_timers(monkeypatch):
    monkeypatch.setattr(pet_mod.QTimer, "singleShot",
                        staticmethod(lambda *a, **k: None))


def _fit(scale, w, h):
    p = _FakePet(scale)
    pet_mod.PetWindow.fit_window_to_model(p, w, h)
    return p


# ══════════════════════════════════════════════════════════════
#  核心：基准与 scale 无关
# ══════════════════════════════════════════════════════════════

# 同一个模型在「无缩放基准」下的贴合尺寸（由扫描测得后归一化而来）
UNIT_W, UNIT_H = 451, 833


@pytest.mark.parametrize("scale", [0.3, 0.45, 1.0, 1.8, 3.0])
def test_base_is_independent_of_scale(scale):
    """渲染器报的是当前缩放下的像素，除回去必须得到同一个基准。

    旧代码在这里会得到 基准 = 451*S（随 S 线性增长），
    进而 窗口 = 451*S²。
    """
    measured = (int(round(UNIT_W * scale)), int(round(UNIT_H * scale)))

    p = _fit(scale, *measured)

    assert (p._base_w, p._base_h) == pytest.approx(
        (UNIT_W, UNIT_H), abs=1), (
        f"S={scale} 的基准应恒为 {UNIT_W}x{UNIT_H}，实得 "
        f"{p._base_w}x{p._base_h} —— 基准随缩放漂移就是多重缩放"
    )


@pytest.mark.parametrize("scale", [0.3, 0.45, 1.0, 1.8, 3.0])
def test_window_equals_measured(scale):
    """窗口尺寸 = 渲染器量到的模型实际大小（docstring 的承诺）。

    屏幕够大时不得有任何额外缩放。屏幕装不下时会另行等比压缩，
    那是 _clamp_size_to_screen 的职责（另有用例）。
    """
    measured = (int(round(UNIT_W * scale)), int(round(UNIT_H * scale)))

    p = _fit(scale, *measured)

    assert p.final == pytest.approx(measured, abs=1), (
        f"S={scale} 时窗口 {p.final} 不等于测量值 {measured}"
    )


def test_old_behaviour_would_be_quadratic():
    """把旧公式摆在这里，说明它错在哪（防回归时有人"修回去"）。

    同一模型：S=1.0 时窗口 451x833；若窗口 ∝ S²，S=1.8 就该是 1461x2699。
    实测旧代码正是 1458x2698。
    """
    w1 = UNIT_W * 1.0
    w_buggy_18 = UNIT_W * 1.8 * 1.8
    assert w_buggy_18 == pytest.approx(1461, abs=2)
    assert w1 == pytest.approx(451, abs=1)


# ══════════════════════════════════════════════════════════════
#  缩小必须真的能缩小
# ══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("scale", [0.3, 0.5, 0.9])
def test_scale_below_one_actually_shrinks(scale):
    """旧式 max(base_w, base_w*scale) 会把窗口钉在基准上，
    于是 _pet_scale < 1 在启动路径上完全无效（另一处两条路语义不一致）。
    """
    measured = (int(round(UNIT_W * scale)), int(round(UNIT_H * scale)))

    p = _fit(scale, *measured)

    assert p.final[0] <= measured[0] + 1
    assert p.final[1] <= measured[1] + 1


def test_never_collapses_to_degenerate_window():
    """极端缩放下窗口不能变成 0/负数（setFixedSize 会炸）。"""
    p = _fit(0.3, 1, 1)
    assert p.final[0] >= 60 and p.final[1] >= 60


def test_survives_zero_scale():
    """脏配置（scale=0）不得让贴合除零崩溃。"""
    p = _fit(0.0, 800, 1400)
    assert p.final[0] >= 60 and p.final[1] >= 60


# ══════════════════════════════════════════════════════════════
#  与滚轮路径一致
# ══════════════════════════════════════════════════════════════

def test_consistent_with_recalc_geometry_formula():
    """两条路必须用同一个公式：窗口 = 基准 × scale（下限 60）。

    启动走 fit_window_to_model，滚轮走 _recalc_geometry。
    以前这两条路的缩放语义不一致，用户感受是「滚轮和重启后大小对不上」。
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "pet.py").read_text(encoding="utf-8")

    fit_body = src[src.index("def fit_window_to_model"):
                   src.index("def _recalc_geometry")]
    recalc_body = src[src.index("def _recalc_geometry"):
                      src.index("def _apply_scale")]

    for name, body in (("fit", fit_body), ("recalc", recalc_body)):
        assert "int(base_w * self._pet_scale)" in body, (
            f"{name} 路径的窗口宽度公式不是 基准×scale"
        )
        assert "max(60," in body, f"{name} 路径缺 60px 下限"


# ══════════════════════════════════════════════════════════════
#  贴合/缩放后不能把自己挤出屏幕
# ══════════════════════════════════════════════════════════════

# 实测事故：scale=1.8 时窗口从 833 长到 1499，保持「中心」不动
# → 上移 333px → Win32 报回 (1470,-325)-(2483,1549)，
#   而屏幕只有 1152 高，桌宠顶部被顶出了屏幕。

def test_keeps_bottom_edge_fixed():
    """桌宠是「站在桌面上」的：变高时底边不该动，只向上长。"""
    screen = _Screen(2048, 2160)
    # 站在屏幕底部（默认位置就是底部居中）
    old = _Rect(700, screen.bottom() - 833 + 1, 450, 833)
    p = _FakePet(1.0, geo=old, screen=screen)

    pet_mod.PetWindow._resize_keeping_visible(p, 810, 1499)

    x, y = p.moved_to
    assert y + 1499 - 1 == old.bottom(), "底边应保持不动（脚踩同一条线）"
    assert y >= 0, "同时不能顶出屏幕"


def test_never_pushed_above_screen():
    """即使保持底边会顶出屏幕，顶部也必须被钳回屏幕内。"""
    # 底边在 1032，窗口高 1499 → 顶部会算成 -467
    p = _FakePet(1.0, geo=_Rect(700, 200, 450, 833))

    pet_mod.PetWindow._resize_keeping_visible(p, 810, 1499)

    assert p.moved_to[1] >= 0, (
        f"窗口顶边跑到屏幕上方了: y={p.moved_to[1]}（实测事故是 -325）"
    )


def test_window_taller_than_screen_is_shrunk_first():
    """窗口比屏幕还高时，先等比压进屏幕（保证全身可见）。

    Live2D 的 SetScale 是「相对窗口」的：窗口多大角色就多大。
    所以窗口超过屏幕 = 角色有一部分在屏幕外（实测脚被推到屏幕下方）。
    """
    p = _FakePet(1.0, geo=_Rect(700, 200, 450, 833),
                 screen=_Screen(2048, 1104))

    pet_mod.PetWindow._resize_keeping_visible(p, 810, 1600)

    w, h = p.final
    assert h <= 1104, f"窗口未被压进屏幕: {w}x{h}"
    assert abs(w / h - 810 / 1600) < 0.02, "等比缩小不得改变宽高比（会拉变形）"


def test_clamp_is_a_noop_when_it_fits():
    p = _FakePet(1.0, screen=_Screen(2048, 1104))

    assert pet_mod.PetWindow._clamp_size_to_screen(p, 800, 1000) == (800, 1000)


def test_clamp_shrinks_both_axes_together():
    p = _FakePet(1.0, screen=_Screen(1000, 1000))

    w, h = pet_mod.PetWindow._clamp_size_to_screen(p, 2000, 1000)

    assert w <= 1000 and h <= 1000
    assert w / h == pytest.approx(2.0, abs=0.05), "宽高比应保持"


def test_clamp_never_returns_degenerate():
    p = _FakePet(1.0, screen=_Screen(2048, 1104))

    w, h = pet_mod.PetWindow._clamp_size_to_screen(p, 1, 999999)

    assert w >= 60 and h >= 60


def test_small_window_stays_within_screen():
    """窗口变小且原本贴底时，不该被留到屏幕外面。"""
    p = _FakePet(1.0, geo=_Rect(700, 900, 450, 200),
                 screen=_Screen(2048, 1104))

    pet_mod.PetWindow._resize_keeping_visible(p, 200, 300)

    x, y = p.moved_to
    assert y >= 0 and y + 300 <= 1104, f"窗口越界: y={y}"
    assert x >= 0 and x + 200 <= 2048, f"窗口越界: x={x}"


def test_horizontal_center_preserved():
    old = _Rect(700, 200, 450, 833)          # center x = 925
    p = _FakePet(1.0, geo=old, screen=_Screen(2048, 2160))

    pet_mod.PetWindow._resize_keeping_visible(p, 810, 600)

    x, _ = p.moved_to
    assert x + 810 // 2 == old.center().x(), "水平中心应保持"


def test_horizontal_clamped_into_screen():
    """窗口比屏幕宽时不能左右越界。"""
    p = _FakePet(1.0, geo=_Rect(1900, 200, 450, 400),
                 screen=_Screen(2048, 1104))

    pet_mod.PetWindow._resize_keeping_visible(p, 1900, 400)

    x, _ = p.moved_to
    assert x >= 0 and x + 1900 <= 2048, f"窗口越界: x={x}"


def test_invisible_window_is_not_moved():
    """还没 show() 时 move() 无意义（且可能触发额外事件）。"""
    p = _FakePet(1.0, visible=False)

    pet_mod.PetWindow._resize_keeping_visible(p, 810, 400)

    assert p.final == (810, 400)
    assert p.moved_to is None


def test_both_paths_share_the_placement_rule():
    """源码级守卫：保持中心的老写法不得再出现。

    以前两处各自 frameGeometry().center() + move(...) ——
    修了一处漏另一处，就会变成「滚轮缩放会把桌宠顶出屏幕，重启不会」。
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "pet.py").read_text(encoding="utf-8")

    assert src.count("_resize_keeping_visible(") >= 3, (
        "定义 1 处 + 调用至少 2 处（fit 与 recalc）"
    )
    assert "_center.x() - w_final // 2" not in src
    assert "_center.x() - w // 2" not in src
