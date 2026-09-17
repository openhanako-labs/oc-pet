"""插件面板 - 浏览 Hanako 插件 + 快捷调用

UI重构: 继承 PanelWindow 基类，统一标题栏、刷新按钮、关闭按钮

扫描 ~/.hanako/plugins/ 目录，列出所有已安装插件及其工具。
用户可以从桌宠右键菜单 -> "🔌 插件" 打开。

可以点击插件发送指令到对话引擎，让 LLM 以角色口吻调用。
"""
from __future__ import annotations

import json
import os
import logging
import re
from pathlib import Path

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
    QPushButton, QLabel, QLineEdit, QHeaderView, QSplitter, QTextEdit,
    QWidget, QApplication
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from ui.theme import get_default, rgb, rgba, THEME_COLORS
from ui.panel_window import PanelWindow

from hanako_home import hanako_home

logger = logging.getLogger(__name__)

HANAKO_PLUGINS = hanako_home() / "plugins"
HANAKO_APPS = hanako_home() / "apps"


def _build_style(theme: str) -> str:
    """按主题从共享调色板生成插件面板 QSS（深/浅色都覆盖）

    注意：QDialog 本身透明，可见的玻璃卡是 #glassPanel（全局设计系统层）。
    """
    return f"""
QDialog {{ background: transparent; color: rgb({rgb(theme, 'dlg_text')}); }}
QTreeWidget {{
    background: rgb({rgb(theme, 'input_bg')}); color: rgb({rgb(theme, 'dlg_text')});
    border: 1px solid rgb({rgb(theme, 'input_border')}); border-radius: 4px;
    font-size: 13px;
}}
QTreeWidget::item {{ padding: 4px 8px; }}
QTreeWidget::item:selected {{ background: rgba({rgba(theme, 'tree_selected')}); }}
QTreeWidget::item:hover {{ background: rgb({rgb(theme, 'tree_hover')}); }}
QLabel {{ color: rgb({rgb(theme, 'dlg_muted')}); font-size: 11px; }}
QLineEdit {{
    background: rgb({rgb(theme, 'input_bg')}); color: rgb({rgb(theme, 'dlg_text')});
    border: 1px solid rgb({rgb(theme, 'input_border')}); border-radius: 4px; padding: 6px 10px;
}}
QTextEdit {{
    background: rgb({rgb(theme, 'input_bg')}); color: rgb({rgb(theme, 'dlg_text')});
    border: 1px solid rgb({rgb(theme, 'input_border')}); border-radius: 4px; padding: 8px;
    font-size: 12px;
}}
QPushButton {{
    background: rgb({rgb(theme, 'btn_primary')}); color: #ffffff; border: none;
    border-radius: 4px; padding: 8px 20px; font-size: 13px;
}}
QPushButton:hover {{ background: rgb({rgb(theme, 'btn_primary_hover')}); }}
QPushButton#send {{ background: rgb({rgb(theme, 'btn_send')}); }}
QPushButton#send:hover {{ background: rgb({rgb(theme, 'btn_send_hover')}); }}
QPushButton:disabled {{ background: rgb({rgb(theme, 'btn_disabled_bg')}); color: rgb({rgb(theme, 'dlg_muted')}); }}
"""


class PluginPanel(PanelWindow):
    """插件浏览面板
    
    继承 PanelWindow，统一标题栏、刷新按钮、关闭按钮。
    """

    def __init__(self, on_send_command=None, parent=None):
        super().__init__("插件", parent, show_refresh=True, min_size=(560, 480), max_size=(800, 800))
        self._on_send = on_send_command or (lambda text: None)
        
        # 填充内容区域
        self._build_content()
        
        # 加载数据
        self._plugins = self._scan_plugins()
        self._populate_tree()
        
        # 刷新按钮连接
        self.refresh_requested.connect(self.refresh)
    
    def _build_content(self):
        """构建内容区域（搜索 + 列表 + 详情 + 指令输入）"""
        # 搜索
        search_row = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索插件...")
        self._search.textChanged.connect(self._filter)
        search_row.addWidget(self._search)
        self.content_layout.addLayout(search_row)
        
        # 插件列表
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["插件", "工具数", "描述"])
        self._tree.header().resizeSection(0, 160)
        self._tree.header().resizeSection(1, 60)
        self._tree.header().setSectionResizeMode(2, QHeaderView.Stretch)
        self._tree.itemClicked.connect(self._on_select)
        self.content_layout.addWidget(self._tree, stretch=1)
        
        # 详情
        self._detail = QTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setMaximumHeight(120)
        self.content_layout.addWidget(self._detail)
        
        # 指令输入
        cmd_row = QHBoxLayout()
        self._cmd_input = QLineEdit()
        self._cmd_input.setPlaceholderText("输入指令让桌宠调用插件（如：播放一首音乐）")
        self._cmd_input.returnPressed.connect(self._send_command)
        cmd_row.addWidget(self._cmd_input)
        
        send_btn = QPushButton("发送")
        send_btn.setObjectName("send")
        send_btn.clicked.connect(self._send_command)
        cmd_row.addWidget(send_btn)
        
        self.content_layout.addLayout(cmd_row)

    def set_theme(self, theme: str):
        """主题切换 — 由 ThemeManager.theme_changed 信号触发"""
        if theme not in THEME_COLORS:
            return
        if theme == self._ui_theme:
            return
        self._ui_theme = theme
        self.setStyleSheet(_build_style(theme))

    def _center_on_screen(self):
        """无边框对话框需手动居中"""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(geo.x() + (geo.width() - self.width()) // 2,
                  geo.y() + (geo.height() - self.height()) // 2)

    # 注（2026-09-17）：原先这里有一份 mousePressEvent/mouseMoveEvent/
    # mouseReleaseEvent 的拖拽实现，是 `55a8a70 refactor: 插件面板继承
    # PanelWindow` 之后**未删除的重复代码**。它引用 `self._header` /
    # `self._close`，而这两个名字在 PanelWindow 里是 `_create_header()`
    # 的局部变量（从未存成实例属性）——于是鼠标一按就 AttributeError，
    # 冒泡成 CRITICAL 崩溃（实测 2026-09-17 14:31:11）。
    # 拖拽已由 PanelWindow 基类完整提供（`_drag_pos` + 三个事件方法），
    # 故删除。

    def _scan_plugins(self) -> list[dict]:
        """扫描 Hanako 插件：V1 plugins + V2 apps（2026-09-17 补后者）。

        V2 的发现机制与 V1 不同——不能读 `contributes.tools`（官方 schema
        不承认该字段），而是**直接枚举 tools/ 目录**。见 tool_registry
        的 _scan_apps_dir 同源说明。
        """
        return self._scan_v1_plugins() + self._scan_v2_apps()

    def _scan_v1_plugins(self) -> list[dict]:
        """扫描 V1 插件（~/.hanako/plugins，读 contributes.tools）。"""
        plugins = []
        if not HANAKO_PLUGINS.exists():
            return plugins

        for d in sorted(HANAKO_PLUGINS.iterdir()):
            if not d.is_dir():
                continue
            manifest = d / "manifest.json"
            if not manifest.exists():
                continue
            try:
                m = json.loads(manifest.read_text("utf-8"))
                # 安全获取 tools
                contributes = m.get("contributes", {})
                if not isinstance(contributes, dict):
                    contributes = {}
                tools_raw = contributes.get("tools", [])
                if not isinstance(tools_raw, list):
                    tools_raw = []
                tools = []
                for t in tools_raw:
                    if isinstance(t, str):
                        # 工具是字符串（只有 ID）
                        tools.append({"name": t, "desc": "", "source": ""})
                        continue
                    if not isinstance(t, dict):
                        continue
                    src = t.get("source", "")
                    # 2026-09-17：解析逻辑抽到 core/js_tool_parser.py（单一实现）
                    tool_file = d / src
                    tool_name = os.path.splitext(src)[0].split("/")[-1]
                    tool_desc = ""
                    if tool_file.exists():
                        try:
                            from core.js_tool_parser import parse_tool_summary
                            _s = parse_tool_summary(tool_file)
                            tool_name = _s["name"]
                            tool_desc = _s["description"][:60]
                        except Exception:
                            logger.debug("plugin_panel: 非致命异常(已静默吞掉)", exc_info=True)
                    tools.append({"name": tool_name, "desc": tool_desc, "source": src})

                plugins.append({
                    "id": m.get("id", d.name),
                    "name": m.get("name", d.name),
                    "desc": m.get("description", ""),
                    "tools": tools,
                    "path": str(d),
                })
            except Exception as e:
                logger.warning("Failed to parse plugin %s: %s", d.name, e)

        return plugins

    def _scan_v2_apps(self) -> list[dict]:
        """扫描 V2 Apps（~/.hanako/apps）——**直接枚举 tools/ 目录**。

        为什么不能读 manifest：V2 官方 schema 不承认 `contributes.tools`
        （实测 V2 manifest 的 contributes 只有 cards/settings/ui），
        Hana 靠「tools/ 目录存在」发现工具。

        容错：无 manifest / 非 v2 / 无 tools 目录 → 跳过。
        """
        apps = []
        if not HANAKO_APPS.exists():
            return apps
        for d in sorted(HANAKO_APPS.iterdir()):
            if not d.is_dir():
                continue
            manifest = d / "manifest.json"
            if not manifest.exists():
                continue
            try:
                m = json.loads(manifest.read_text("utf-8"))
                if m.get("manifestVersion") != 2:
                    continue
                tools_dir = d / "tools"
                if not tools_dir.is_dir():
                    continue
                tools = []
                from core.js_tool_parser import iter_tool_files, parse_tool_summary
                for f in iter_tool_files(tools_dir):
                    s = parse_tool_summary(f)
                    tools.append({"name": s["name"], "desc": s["description"][:60],
                                  "source": f"tools/{f.name}"})
                if not tools:
                    continue
                apps.append({
                    "id": m.get("id", d.name),
                    "name": m.get("name", d.name),
                    "desc": m.get("description", ""),
                    "tools": tools,
                    "path": str(d),
                    "kind": "app",
                })
            except Exception as e:
                logger.warning("Failed to parse app %s: %s", d.name, e)
        return apps

    def _populate_tree(self):
        """填充插件树"""
        self._tree.clear()
        for p in self._plugins:
            item = QTreeWidgetItem([
                p["name"],
                str(len(p["tools"])),
                p["desc"][:50],
            ])
            item.setData(0, Qt.UserRole, p)

            # 子节点：工具
            for t in p["tools"]:
                child = QTreeWidgetItem([f"  {t['name']}", "", t["desc"][:40]])
                child.setData(0, Qt.UserRole, {"tool": t, "plugin": p["id"]})
                item.addChild(child)

            self._tree.addTopLevelItem(item)

    def _filter(self, text: str):
        """搜索过滤"""
        text = text.strip().lower()
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            p = item.data(0, Qt.UserRole)
            if not p:
                continue
            match = not text or text in p["name"].lower() or text in p["desc"].lower()
            item.setHidden(not match)

    def _on_select(self, item: QTreeWidgetItem, column: int):
        """选中插件/工具时显示详情"""
        data = item.data(0, Qt.UserRole)
        if not data:
            return

        if "tool" in data:
            # 工具节点
            t = data["tool"]
            p_id = data["plugin"]
            self._detail.setHtml(
                f"<b>{t['name']}</b> ({p_id})<br>"
                f"<span style='color:rgb({rgb(self._ui_theme, 'dlg_muted')})'>{t['source']}</span><br>"
                f"<span style='color:rgb({rgb(self._ui_theme, 'dlg_text')})'>{t['desc']}</span>"
            )
            # 预填指令
            self._cmd_input.setText(f"帮我用{p_id}的{t['name']}功能")
            self._cmd_input.setFocus()
        else:
            # 插件节点
            p = data
            tools_list = "<br>".join(
                f"• {t['name']}: {t['desc'][:40]}" for t in p["tools"]
            )
            self._detail.setHtml(
                f"<b>{p['name']}</b> ({p['id']})<br>"
                f"<span style='color:rgb({rgb(self._ui_theme, 'dlg_muted')})'>{p['desc']}</span><br><br>"
                f"<b>工具 ({len(p['tools'])}):</b><br>{tools_list}"
            )

    def _send_command(self):
        """发送指令到对话引擎"""
        text = self._cmd_input.text().strip()
        if not text:
            return
        self._on_send(text)
        self._cmd_input.clear()
        self.accept()
