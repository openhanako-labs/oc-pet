# -*- coding: utf-8 -*-
"""回归：V2 Apps 的工具发现（面板 + 工具注册表）。

## 背景（2026-09-17 核实）

用户发现插件面板只显示 V1 插件的工具，V2 App 的工具一个都没有。

实测：
    V1 plugins  23 个插件  86 个工具   ← 面板只读这里
    V2 apps      3 个应用  11 个工具   ← 完全没读

**根因不是"没加路径"，是发现机制不同**：
- V1 靠 `contributes.tools` 字段（manifest 里声明）
- V2 **不能读那个字段**——官方 schema 不承认它（实测 V2 manifest 的
  contributes 只有 cards/settings/ui），Hana 靠「tools/ 目录存在」发现工具

所以 V2 必须走独立扫描路径：直接枚举 `tools/*.js` 并解析导出声明。

## 修了什么

1. `core/tool_registry.py`：新增 `_scan_apps_dir`，工具数 93 → 104
2. `ui/plugin_panel.py`：新增 `_scan_v2_apps`，面板 23/86 → 26/97
3. 两处的 JS 解析都从 `split("=")` 换成正则（原实现遇到含 `=` 的行会截断）
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── 一、工具注册表读得到 V2 ─────────────────────────────────────────────────


def test_registry_discovers_v2_apps():
    """tool_registry.discover() 必须能读到 V2 apps 的工具。

    用真实环境（本机有 V2 app 才有意义）；无 app 则跳过。
    """
    from pathlib import Path

    apps_dir = Path.home() / ".hanako" / "apps"
    if not apps_dir.is_dir():
        import pytest

        pytest.skip("本机无 V2 apps 目录")

    from core.tool_registry import ToolRegistry

    reg = ToolRegistry()
    reg.discover()
    # V2 工具的 plugin_id 会带 v2 特征（如 bilibili-intake-v2 / hanako-mail）
    plugins = {t.plugin_id for t in reg._tools.values()}
    v2_like = {p for p in plugins if "mail" in p or "audio-player" in p or "intake" in p}
    assert v2_like, f"未发现任何 V2 app 的工具，plugins={sorted(plugins)[:20]}"


def test_scan_apps_dir_ignores_v1_manifests(tmp_path):
    """_scan_apps_dir 只处理 manifestVersion==2，避免与 _scan_dir 重复。"""
    from core.tool_registry import ToolRegistry

    apps = tmp_path / "apps"
    (apps / "v1-app").mkdir(parents=True)
    (apps / "v1-app" / "manifest.json").write_text(
        json.dumps({"id": "v1-app", "manifestVersion": 1}), encoding="utf-8"
    )
    (apps / "v1-app" / "tools").mkdir()
    (apps / "v1-app" / "tools" / "t.js").write_text(
        'export const name = "should_not_appear";\n', encoding="utf-8"
    )

    reg = ToolRegistry()
    reg._scan_apps_dir(apps)
    assert "should_not_appear" not in reg._tools, "v1 manifest 不应被 V2 扫描器处理"


def test_scan_apps_dir_discovers_without_contributes_tools(tmp_path):
    """关键：V2 工具能在**没有** contributes.tools 字段时被发现。

    这正是 V2 的真实形态——manifest 里只有 cards/settings。
    """
    from core.tool_registry import ToolRegistry

    apps = tmp_path / "apps"
    d = apps / "myapp"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(
        json.dumps({
            "id": "myapp",
            "manifestVersion": 2,
            "contributes": {"cards": ["x"]},  # 故意没有 tools
        }),
        encoding="utf-8",
    )
    (d / "tools").mkdir()
    (d / "tools" / "hello.js").write_text(
        'export const name = "hello_tool";\n'
        'export const description = "测试工具";\n'
        'export const parameters = { type: "object", properties: {} };\n',
        encoding="utf-8",
    )

    reg = ToolRegistry()
    reg._scan_apps_dir(apps)
    assert "hello_tool" in reg._tools, f"应发现 hello_tool，实得 {list(reg._tools)}"
    assert reg._tools["hello_tool"].plugin_id == "myapp"


def test_scan_apps_dir_skips_app_without_tools_dir(tmp_path):
    """无 tools/ 目录的 app 跳过（纯卡片型 app 没有工具）。"""
    from core.tool_registry import ToolRegistry

    apps = tmp_path / "apps"
    d = apps / "cardonly"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(
        json.dumps({"id": "cardonly", "manifestVersion": 2}), encoding="utf-8"
    )

    reg = ToolRegistry()
    reg._scan_apps_dir(apps)
    assert reg._tools == {}, "无 tools 目录不应产生工具"


# ── 二、面板读得到 V2 ───────────────────────────────────────────────────────


def test_panel_scan_includes_v2_apps():
    """插件面板的 _scan_plugins 必须同时含 V1 与 V2。"""
    from pathlib import Path

    apps_dir = Path.home() / ".hanako" / "apps"
    if not apps_dir.is_dir():
        import pytest

        pytest.skip("本机无 V2 apps 目录")

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])  # noqa: F841

    from ui.plugin_panel import PluginPanel

    p = PluginPanel.__new__(PluginPanel)
    v1 = p._scan_v1_plugins()
    v2 = p._scan_v2_apps()
    merged = p._scan_plugins()

    assert v2, "应扫到 V2 app（至少一个带工具）"
    assert len(merged) == len(v1) + len(v2), "合并结果应等于 V1 + V2"
    # V2 条目带 kind 标记，便于 UI 区分
    assert all(a.get("kind") == "app" for a in v2)


def test_panel_uses_shared_parser():
    """工具名解析走共享实现，不再内联正则。

    2026-09-17：解析逻辑抽到 `core/js_tool_parser.py`。
    本断言防止再次内联分叉（历史上三处各写一份，已出过行为不一致：
    `split("=")` 把 `mail_accounts` 解析成 `accounts`）。
    """
    import inspect

    from ui import plugin_panel

    src = inspect.getsource(plugin_panel.PluginPanel._scan_v2_apps)
    assert "js_tool_parser" in src, "应复用 core/js_tool_parser.py"
    assert 'split("=")[-1]' not in src, "不应再用 split('=') 解析"
