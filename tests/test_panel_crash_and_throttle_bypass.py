# -*- coding: utf-8 -*-
"""回归：两个实测崩溃/失效（2026-09-17 14:31 日志）。

## 一、插件面板一点就崩（CRITICAL）

    File "ui/plugin_panel.py", line 151, in mousePressEvent
        header_local = self._header.mapFromGlobal(event.globalPos())
    AttributeError: 'PluginPanel' object has no attribute '_header'

根因：`55a8a70 refactor: 插件面板继承 PanelWindow` 之后，`PluginPanel`
里那份 `mousePressEvent/mouseMoveEvent/mouseReleaseEvent` 是**未删除的
重复代码**，引用 `self._header` / `self._close`——而这两个名字在
`PanelWindow` 里是 `_create_header()` 的局部变量，从未存成实例属性。
鼠标一按就 AttributeError，冒泡成 CRITICAL。

拖拽已由 `PanelWindow` 基类完整提供，故删除子类重复实现。

## 二、气泡节流被绕过

实测（日志 14:32:30→14:32:31）：tool_call 与「正在思考」在 1 秒内
连续上屏——`_do_tool_progress` 直接调 `_show_bubble`，
**绕过了 `_do_engine_status` 的节流**。

修法：工具进度统一走 `_do_engine_status`；失败提示仍立即上屏。
"""
from __future__ import annotations

import inspect
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── 一、插件面板不再引用不存在的属性 ────────────────────────────────────────


def _code_only(func) -> str:
    """取函数源码，只留**可执行语句**（剔注释与 docstring）。

    否则「解释为何删掉 self._header」这类注释/文档会被误判为代码引用。
    实现：先剥三引号块（docstring），再剔 # 开头的行。
    """
    import re

    src = inspect.getsource(func)
    # 剥三引号块（含 ''' 与 """）
    src = re.sub(r'"""[\s\S]*?"""', '', src)
    src = re.sub(r"'''[\s\S]*?'''", '', src)
    # 剔注释行
    return "\n".join(
        ln for ln in src.splitlines() if not ln.strip().startswith("#")
    )


def test_plugin_panel_has_no_orphan_drag_handlers():
    """PluginPanel 不应再定义拖拽事件处理（基类已提供）。

    这是崩溃的直接原因：子类版本引用 `self._header`（不存在）。
    """
    from ui.plugin_panel import PluginPanel
    from ui.panel_window import PanelWindow

    for name in ("mousePressEvent", "mouseMoveEvent", "mouseReleaseEvent"):
        assert not hasattr(PluginPanel, name) or (
            getattr(PluginPanel, name) is getattr(PanelWindow, name)
        ), f"PluginPanel 不应覆盖 {name}（拖拽由基类提供）"


def test_plugin_panel_source_has_no_header_ref():
    """**代码**层面不得再出现 self._header / self._close 引用。

    注释里提到它们是合理的（解释为何删掉）。
    """
    import io
    import re

    from ui import plugin_panel

    path = plugin_panel.__file__
    raw = io.open(path, encoding="utf-8").read()
    # 去注释行
    code_lines = [
        ln for ln in raw.splitlines()
        if not ln.strip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert not re.search(r"self\._header\b", code), "代码里仍有 self._header"
    assert not re.search(r"self\._close\b", code), "代码里仍有 self._close"


def test_panel_window_provides_drag():
    """基类必须提供拖拽实现（子类删除后仍能拖）。"""
    from ui.panel_window import PanelWindow

    assert hasattr(PanelWindow, "mousePressEvent")
    assert hasattr(PanelWindow, "mouseMoveEvent")
    assert hasattr(PanelWindow, "mouseReleaseEvent")
    src = inspect.getsource(PanelWindow)
    assert "_drag_pos" in src, "基类应用 _drag_pos 记录拖拽起点"


# ── 二、工具进度接入节流 ────────────────────────────────────────────────────


def test_tool_progress_uses_throttle_path():
    """`_do_tool_progress` 必须走 `_do_engine_status`（节流入口），
    而不是直接调 `_show_bubble`。

    只看代码，不看注释——注释里会提到 _show_bubble 以说明修改理由。
    """
    from pet import PetWindow

    code = _code_only(PetWindow._do_tool_progress)
    assert "_do_engine_status" in code, "工具进度应走节流入口"
    assert "_show_bubble" not in code, "不应直接调 _show_bubble（绕过节流）"


def test_status_signal_carries_emotion():
    """状态信号需携带情绪（工具失败要显示 sad，不能一律 thinking）。"""
    from pet import PetWindow

    # 槽签名应有 emotion 参数
    sig_params = inspect.signature(PetWindow._do_engine_status).parameters
    assert "emotion" in sig_params, "_do_engine_status 应接受 emotion"
    # 信号声明为两个 str
    import io

    src = io.open(PetWindow.__module__ and
                  __import__("pet").__file__, encoding="utf-8").read()
    assert "engine_status_signal = Signal(str, str)" in src, (
        "信号应声明为 Signal(str, str)"
    )


def test_tool_failure_passes_sad():
    """工具失败应传 sad 情绪。"""
    from pet import PetWindow

    code = _code_only(PetWindow._do_tool_progress)
    assert '"sad"' in code or "'sad'" in code, "工具失败应传 sad"
