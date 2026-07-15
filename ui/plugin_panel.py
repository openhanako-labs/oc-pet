"""插件面板 - 浏览 Hanako 插件 + 快捷调用

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
    QDialog, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
    QPushButton, QLabel, QLineEdit, QHeaderView, QSplitter, QTextEdit
)
from PySide6.QtCore import Qt

logger = logging.getLogger(__name__)

HANAKO_PLUGINS = Path.home() / ".hanako" / "plugins"

STYLE = """
QDialog { background: #f5f5f7; color: #1d1d1f; }
QTreeWidget {
    background: #ffffff; color: #1d1d1f;
    border: 1px solid #d2d2d7; border-radius: 8px;
    font-size: 13px;
}
QTreeWidget::item { padding: 6px 10px; border-radius: 4px; margin: 2px 4px; }
QTreeWidget::item:selected { background: #e8e8ed; }
QTreeWidget::item:hover { background: #f5f5f7; }
QLabel { color: #86868b; font-size: 11px; }
QLineEdit {
    background: #ffffff; color: #1d1d1f;
    border: 1px solid #d2d2d7; border-radius: 8px; padding: 8px 12px;
    min-height: 32px; font-size: 13px;
}
QLineEdit:focus { border: 2px solid #0071e3; }
QTextEdit {
    background: #ffffff; color: #1d1d1f;
    border: 1px solid #d2d2d7; border-radius: 8px; padding: 10px;
    font-size: 13px;
}
QPushButton {
    background: #0071e3; color: #ffffff; border: none;
    border-radius: 8px; padding: 10px 24px; font-size: 13px; font-weight: bold;
}
QPushButton:hover { background: #0077ed; }
QPushButton:pressed { background: #006edb; }
QPushButton#send { background: #34c759; }
QPushButton#send:hover { background: #30d158; }
QPushButton#send:pressed { background: #2db84e; }
QPushButton:disabled { background: #e8e8ed; color: #86868b; }
"""


class PluginPanel(QDialog):
    """插件浏览面板"""

    def __init__(self, on_send_command=None, parent=None):
        super().__init__(parent)
        self._on_send = on_send_command or (lambda text: None)
        self.setWindowTitle("插件")
        self.setMinimumSize(560, 480)
        self.setStyleSheet(STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # ── 搜索 ──
        search_row = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索插件...")
        self._search.textChanged.connect(self._filter)
        search_row.addWidget(self._search)
        layout.addLayout(search_row)

        # ── 插件列表 ──
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["插件", "工具数", "描述"])
        self._tree.header().resizeSection(0, 160)
        self._tree.header().resizeSection(1, 60)
        self._tree.header().setSectionResizeMode(2, QHeaderView.Stretch)
        self._tree.itemClicked.connect(self._on_select)
        layout.addWidget(self._tree, stretch=1)

        # ── 详情 ──
        self._detail = QTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setMaximumHeight(120)
        layout.addWidget(self._detail)

        # ── 指令输入 ──
        cmd_row = QHBoxLayout()
        self._cmd_input = QLineEdit()
        self._cmd_input.setPlaceholderText("输入指令让桌宠调用插件（如：播放一首音乐）")
        self._cmd_input.returnPressed.connect(self._send_command)
        cmd_row.addWidget(self._cmd_input)

        send_btn = QPushButton("发送")
        send_btn.setObjectName("send")
        send_btn.clicked.connect(self._send_command)
        cmd_row.addWidget(send_btn)

        layout.addLayout(cmd_row)

        # 加载数据
        self._plugins = self._scan_plugins()
        self._populate_tree()

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
                            pass
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
                f"<span style='color:#86868b'>{t['source']}</span><br>"
                f"<span style='color:#1d1d1f'>{t['desc']}</span>"
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
                f"<span style='color:#86868b'>{p['desc']}</span><br><br>"
                f"<b>工具 ({len(p['tools'])})：</b><br>{tools_list}"
            )

    def _send_command(self):
        """发送指令到对话引擎"""
        text = self._cmd_input.text().strip()
        if not text:
            return
        self._on_send(text)
        self._cmd_input.clear()
        self.accept()
