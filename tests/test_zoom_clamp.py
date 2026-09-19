# -*- coding: utf-8 -*-
"""缩放/屏幕钳制的验收（放大 bug）。

用户实测：“放大到一定程度桌宠会显示到设定窗口外面”。

根因：`_resize_keeping_visible` 把屏幕钳制结果**只留在局部变量**里，
调用方继续用**未钳制**的尺寸去调 `renderer.recalc_geometry(...)`；
而渲染器是拿这个尺寸设角色 label 的
（`char_label.setFixedSize(w,h)` → `_recompute_fit()`）。
于是：窗口被屏幕钳小、角色 label 按放大后的尺寸渲染 → 模型画到窗口外。

**两条调用路径都中招**（滚轮放大 `_recalc_geometry` / 启动贴合 `fit_window_to_model`），
且只在 `_screen_clamp < 1`（屏幕上限）时出现——与用户描述完全一致。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PET = ROOT / "pet.py"


def _src() -> str:
    return PET.read_text(encoding="utf-8")


# ── 屏幕几何桩（不依赖 QApplication） ─────────────────────


class _Rect:
    def __init__(self, l=0, t=0, w=2560, h=1440):
        self._l, self._t, self._w, self._h = l, t, w, h

    def width(self):
        return self._w

    def height(self):
        return self._h

    def top(self):
        return self._t

    def bottom(self):
        return self._t + self._h - 1

    def left(self):
        return self._l

    def right(self):
        return self._l + self._w - 1

    def center(self):
        class _C:
            def __init__(self, x):
                self._x = x

            def x(self):
                return self._x

        return _C(self._l + self._w // 2)


class _Geo:
    def __init__(self, rect):
        self._r = rect

    def center(self):
        return self._r.center()

    def bottom(self):
        return self._r.bottom()


class _FakeWindow:
    """只带 `_resize_keeping_visible` 需要的那几个钩子。

    注意：`_clamp_size_to_screen` 用**真实实现**绑上来（不假冒），
    否则测的就不是产品代码里的钐制逻辑了。
    """

    def __init__(self, rect=None, visible=True):
        import types

        import pet

        self._rect = rect or _Rect()
        self._visible = visible
        self._screen_clamp = 1.0
        self.size = None
        self.pos = None
        self._clamp_size_to_screen = types.MethodType(
            pet.PetWindow._clamp_size_to_screen, self
        )

    def _current_screen_geometry(self):
        return self._rect

    def frameGeometry(self):
        return _Geo(self._rect)

    def setFixedSize(self, w, h):
        self.size = (w, h)

    def isVisible(self):
        return self._visible

    def move(self, x, y):
        self.pos = (x, y)


def _call(name, win, *args):
    """把 PetWindow 的方法绑到桩上执行（避免实例化 QWidget）。"""
    import types

    import pet

    fn = types.MethodType(getattr(pet.PetWindow, name), win)
    return fn(*args)


# ── 钳制数学 ─────────────────────────────────────────────


def test_size_in_screen_is_untouched():
    win = _FakeWindow(_Rect(w=2560, h=1440))
    assert _call("_clamp_size_to_screen", win, 237, 523) == (237, 523)
    assert win._screen_clamp == 1.0


def test_oversize_is_scaled_proportionally():
    win = _FakeWindow(_Rect(w=1000, h=1000))
    w, h = _call("_clamp_size_to_screen", win, 2000, 1000)
    assert (w, h) == (1000, 500), "等比压进屏幕，不能裁切（裁切会把角色压变形）"
    assert win._screen_clamp == 0.5


def test_taller_than_screen_uses_height_ratio():
    win = _FakeWindow(_Rect(w=2560, h=1440))
    w, h = _call("_clamp_size_to_screen", win, 800, 2880)
    assert h == 1440 and w == 400
    assert win._screen_clamp == 0.5


# ── 契约：必须把钳制后的尺寸交回去 ───────────────────────


def test_resize_returns_clamped_size():
    """**这条是 bug 的核心**：返回值必须是钳制后的尺寸。"""
    win = _FakeWindow(_Rect(w=1000, h=1000))
    ret = _call("_resize_keeping_visible", win, 2000, 1000)
    assert ret == (1000, 500), "调用方要靠这个返回值去喂渲染器"
    assert win.size == (1000, 500), "窗口本身也用钳制后的尺寸"


def test_resize_returns_when_invisible():
    win = _FakeWindow(_Rect(w=1000, h=1000), visible=False)
    assert _call("_resize_keeping_visible", win, 2000, 1000) == (1000, 500)


def test_resize_never_expands():
    win = _FakeWindow(_Rect(w=2560, h=1440))
    assert _call("_resize_keeping_visible", win, 100, 200) == (100, 200)


def test_window_is_kept_inside_screen():
    win = _FakeWindow(_Rect(l=0, t=0, w=1000, h=1000))
    _call("_resize_keeping_visible", win, 2000, 1000)
    x, y = win.pos
    assert 0 <= x and x + win.size[0] <= 1000, "水平必须留在屏内"
    assert 0 <= y and y + win.size[1] <= 1000, "垂直必须留在屏内"


# ── 缩放气泡说实话 ───────────────────────────────────────


def test_zoom_label_marks_screen_limit():
    win = _FakeWindow()
    win._screen_clamp = 0.5
    assert "屏幕上限" in _call("_zoom_label", win, 2.0)
    assert "100%" in _call("_zoom_label", win, 2.0), "报实际生效的百分比"


def test_zoom_label_plain_when_not_clamped():
    win = _FakeWindow()
    win._screen_clamp = 1.0
    assert _call("_zoom_label", win, 1.2).endswith("120%")


# ── 源码护栏：两条路都必须用返回值 ───────────────────────


def test_both_paths_feed_clamped_size_to_renderer():
    """**两条调用路径**（滚轮 / 启动贴合）都必须把钳制后的尺寸交给渲染器。"""
    src = _src()
    assert "recalc_geometry(w_ok, h_ok)" in src, "应把钳制后的尺寸交给渲染器"
    for bad in ("recalc_geometry(w_final, h_final)", "recalc_geometry(w, h)"):
        assert bad not in src, f"{bad} 会让模型按未钳制尺寸渲染（放大 bug 的根因）"


def test_resize_declares_tuple_return():
    src = _src()
    assert "def _resize_keeping_visible(self, w: int, h: int) -> tuple:" in src, (
        "返回类型必须写明，否则调用方很容易忽略返回值"
    )


def test_clamp_result_is_not_written_back_to_config():
    """屏幕装不下是环境事实，不该污染用户的 scale 意图。"""
    src = _src()
    i = src.find("def _clamp_size_to_screen")
    body = src[i:i + 2600]
    assert 'config["scale"]' not in body and "config['scale']" not in body
