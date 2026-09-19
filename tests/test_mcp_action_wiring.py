# -*- coding: utf-8 -*-
"""MCP 表现类工具的**接线**验收。

为什么要这个文件：2026-09-19 实测发现，MCP 的 5 个写操作
（say / play_anim / expression / set_emotion / celebrate）**全部静默失效**，
而工具照样回"已派发"：

```
22:29:49  Processing request of type CallToolRequest     ← 请求收到了
（全日志没有一行 "MCP 动作已应用"）                        ← 动作没执行
```

根因：`EventBus.emit` **同步**调用订阅者，所以 `_on_mcp_action` 跑在 HTTP/MCP
线程里；而它用 `QTimer.singleShot(0, ...)` 想回主线程——**在没有 Qt 事件循环的
线程里，这个定时器永远不触发**。

修法：改走 Qt 信号（跨线程自动排队）。以下测试把这条线钉住。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIXIN = ROOT / "pet_mixins" / "interface_mixin.py"
PETPY = ROOT / "pet.py"


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── 静态接线（防止有人改回去） ────────────────────────────


def test_mcp_action_goes_through_qt_signal():
    """派发侧必须用信号；**只查 `_on_mcp_action` 的函数体**，不被注释或
    其它路径里的同名调用绊倒。"""
    import ast

    src = _src(MIXIN)
    assert "mcp_action_signal.emit" in src, "MCP 动作应经 Qt 信号回主线程"
    i = src.find("def _on_mcp_action")
    assert i != -1, "找不到 _on_mcp_action"
    j = src.find("self._mcp_action_handler", i)
    body = src[i:j] if j > i else src[i:]
    calls = [
        n for n in ast.walk(ast.parse(_dedent(body)))
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "singleShot"
    ]
    assert not calls, (
        "QTimer.singleShot 在 HTTP 线程里不会触发——就是它让 5 个工具全哑了"
    )


def _dedent(block: str) -> str:
    """把嵌在函数里的片段去缩进，让它能被 ast.parse。"""
    import textwrap

    return textwrap.dedent(block)


def test_docstring_states_the_reason():
    """把原因写在旁边，否则后人会"优化"回 QTimer。"""
    src = _src(MIXIN)
    assert "事件循环" in src


def test_pet_defines_the_signal():
    src = _src(PETPY)
    assert "mcp_action_signal = Signal(" in src, "信号必须定义在 QWidget 子类上"


def test_signal_is_connected_inside_init_mcp_server():
    """连接必须在 `_init_mcp_server` 里、且**早于订阅**。

    从 `__init__` 的连接区接太晚了：服务先起来，那个窗口里来的动作会静静丢掉。
    """
    src = _src(MIXIN)
    i_start = src.find("def _init_mcp_server")
    i_next = src.find("def _mcp_capabilities", i_start)
    body = src[i_start:i_next] if i_start != -1 and i_next > i_start else ""
    assert body, "找不到 _init_mcp_server 方法体"
    i_conn = body.find("self.mcp_action_signal.connect(self._apply_mcp_action)")
    i_on = body.find('EventBus.on("mcp_action"')
    assert i_conn != -1, "信号必须在 _init_mcp_server 里连接"
    assert i_on != -1, "找不到订阅"
    assert i_conn < i_on, "连接必须早于订阅，否则先来的动作会丢"


def test_no_qtimer_singleshot_anywhere_in_mixin():
    """**整个文件里都不许再有 `QTimer.singleShot`**。

    2026-09-19 同一个错误出现了**两处**：MCP 动作（5 个工具全哑）和
    外部触发（手机活动那条路）。所以断言整个文件，不只某个函数。
    """
    import ast

    calls = [
        n for n in ast.walk(ast.parse(_src(MIXIN)))
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "singleShot"
    ]
    assert not calls, (
        "QTimer.singleShot 在 HTTP 线程里不会触发——它已经一前一后坑了两条路"
    )


def test_both_paths_use_signals():
    src = _src(MIXIN)
    assert "mcp_action_signal.emit" in src
    assert "external_trigger_signal.emit" in src


def test_external_trigger_signal_is_connected():
    src = _src(MIXIN)
    assert "self.external_trigger_signal.connect(self._apply_external_trigger)" in src
    assert "external_trigger_signal = Signal(str, str, str, str)" in _src(PETPY)


# ── 应用侧：每个动作真的落到该落的地方 ───────────────────


class _FakePet:
    """只带 InterfaceMixin 的壳，记录动作被派到了哪里。"""

    def __init__(self) -> None:
        from pet_mixins.interface_mixin import InterfaceMixin

        self._renderer = _FakeRenderer()
        self.bubbles: list = []
        self.anims: list = []
        self.surface: list = []
        self.celebrated = 0

        class _P(InterfaceMixin):
            pass

        self._impl = _P()

    # 被 _apply_mcp_action 调用的钩子
    def _show_bubble(self, text, *a, **k):
        self.bubbles.append(text)

    def _set_anim_seq(self, name, *a, **k):
        self.anims.append(name)

    def _set_surface_emotion(self, emo, **k):
        self.surface.append(emo)

    def _celebrate(self, *a, **k):
        self.celebrated += 1


class _FakeRenderer:
    def __init__(self) -> None:
        self.emotions: list = []
        self.expressions: list = []

    def set_emotion(self, emo, intensity=1.0):
        self.emotions.append((emo, intensity))

    def _apply_expression(self, name):
        self.expressions.append(name)

    def set_emotion_expression_only(self, emo):
        self.emotions.append((emo, None))


def _apply(pet: _FakePet, action: str, params: dict):
    """把 mixin 的方法绑到壳上执行（模拟主线程应用）。"""
    import types

    from pet_mixins.interface_mixin import InterfaceMixin

    fn = types.MethodType(InterfaceMixin._apply_mcp_action, pet)
    fn(action, params)


def test_say_shows_bubble():
    pet = _FakePet()
    _apply(pet, "say", {"text": "你好"})
    assert pet.bubbles == ["你好"]


def test_play_anim_sets_anim_seq():
    pet = _FakePet()
    _apply(pet, "play_anim", {"anim": "waving"})
    assert pet.anims == ["waving"]


def test_set_emotion_hits_renderer():
    pet = _FakePet()
    _apply(pet, "set_emotion", {"emotion": "happy", "intensity": 0.8})
    assert ("happy", 0.8) in pet._renderer.emotions


def test_expression_hits_renderer():
    pet = _FakePet()
    _apply(pet, "expression", {"name": "脸红"})
    assert pet._renderer.expressions == ["脸红"]


def test_celebrate_calls_hook():
    pet = _FakePet()
    _apply(pet, "celebrate", {})
    assert pet.celebrated == 1


def test_idle_resets():
    pet = _FakePet()
    _apply(pet, "idle", {})
    assert "idle" in pet.anims


def test_unknown_action_does_not_raise():
    pet = _FakePet()
    _apply(pet, "not_a_real_action", {})
    assert pet.bubbles == [] and pet.anims == []


def test_bad_intensity_falls_back_to_one():
    pet = _FakePet()
    _apply(pet, "set_emotion", {"emotion": "happy", "intensity": "不是数字"})
    assert ("happy", 1.0) in pet._renderer.emotions
