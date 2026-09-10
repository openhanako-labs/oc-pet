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
from pathlib import Path

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
    QPushButton, QLabel, QLineEdit, QHeaderView, QSplitter, QTextEdit,
    QWidget
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from ui.theme import get_default, rgb, rgba, THEME_COLORS
from ui.panel_window import PanelWindow

logger = logging.getLogger(__name__)

HANAKO_PLUGINS = Path.home() / ".hanako" / "plugins"


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

    def mousePressEvent(self, event):
        """从头部拖拽（避开关闭按钮）"""
        if event.button() == Qt.LeftButton:
            header_local = self._header.mapFromGlobal(event.globalPos())
            if self._header.rect().contains(header_local):
                close_local = self._close.mapFromGlobal(event.globalPos())
                if not self._close.rect().contains(close_local):
                    self._drag = event.globalPos() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if getattr(self, "_drag", None) is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPos() - self._drag)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag = None
        super().mouseReleaseEvent(event)

    def _scan_plugins(self) -> list[dict]:
        """扫描 Hanako 插件目录"""
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
                    # 尝试读取工具的 name/description
                    tool_file = d / src
                    tool_name = os.path.splitext(src)[0].split("/")[-1]
                    tool_desc = ""
                    if tool_file.exists():
                        try:
                            content = tool_file.read_text("utf-8")
                            # 简单提取 name 和 description
                            for line in content.split("\n")[:20]:
                                if "export const name" in line:
                                    tool_name = line.split("=")[-1].strip().strip("';\"")
                                if "export const description" in line:
                                    tool_desc = line.split("=")[-1].strip().strip("';\"")[:60]
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
