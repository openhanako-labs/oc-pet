"""角色设定编辑器 — 直接在桌宠中编辑 SKILL.md

右键菜单 → "编辑角色设定" 打开 Qt 对话框。
加载/保存 skills/public/<角色>/SKILL.md，保留 YAML front matter。

用法:
    editor = CharacterEditor(parent_pet, character_id="ophelia")
    editor.exec()
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTextEdit,
    QPushButton, QLabel, QMessageBox,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

logger = logging.getLogger(__name__)

# 编辑器样式
EDITOR_STYLE = """
    QTextEdit {
        background: #1a1820;
        color: #d4cec4;
        border: 1px solid #3a3450;
        border-radius: 6px;
        padding: 10px;
        font-size: 13px;
        font-family: "Consolas", "Microsoft YaHei", monospace;
    }
    QPushButton {
        background: #3a3458;
        color: #d4cec4;
        border: none;
        border-radius: 4px;
        padding: 6px 18px;
        font-size: 12px;
    }
    QPushButton:hover {
        background: #4a4478;
    }
    QPushButton#save_btn {
        background: #3a5844;
    }
    QPushButton#save_btn:hover {
        background: #4a7844;
    }
    QLabel {
        color: #8888aa;
        font-size: 11px;
    }
"""


class CharacterEditor(QDialog):
    """角色设定编辑器对话框"""

    def __init__(self, character_id: str, parent=None):
        super().__init__(parent)
        self._character_id = character_id
        self._skill_path = self._resolve_skill_path()
        self._setup_ui()
        self._load()

    def _resolve_skill_path(self) -> Path:
        """找到角色的 SKILL.md 文件"""
        base = Path(__file__).parent
        path = base / "skills" / "public" / self._character_id / "SKILL.md"
        return path if path.exists() else None

    def _setup_ui(self):
        self.setWindowTitle(f"编辑角色设定 — {self._character_id}")
        self.setMinimumSize(500, 420)
        self.setStyleSheet("""
            QDialog {
                background: #1a1820;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)

        # 提示标签
        hint = QLabel(
            "编辑 SKILL.md。YAML front matter（--- 之间的部分）请谨慎修改。\n"
            "保存后角色会立即使用新设定。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # 文本框
        self._editor = QTextEdit()
        self._editor.setStyleSheet(EDITOR_STYLE)
        font = QFont("Consolas", 11)
        font.setStyleHint(QFont.Monospace)
        self._editor.setFont(font)
        layout.addWidget(self._editor, stretch=1)

        # 按钮栏
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        save_btn = QPushButton("保存")
        save_btn.setObjectName("save_btn")
        save_btn.clicked.connect(self._save)
        btn_layout.addWidget(save_btn)

        layout.addLayout(btn_layout)

    def _load(self):
        """加载 SKILL.md 内容到编辑器"""
        if not self._skill_path or not self._skill_path.exists():
            self._editor.setPlainText(
                f"---\nname: {self._character_id}\ndescription: ...\n---\n\n"
                f"# {self._character_id} — 角色设定\n\n## 身份\n\n## 外观\n\n## 性格\n\n"
            )
            return
        try:
            content = self._skill_path.read_text("utf-8")
            self._editor.setPlainText(content)
        except Exception as e:
            logger.warning("Failed to load SKILL.md: %s", e)
            QMessageBox.warning(self, "加载失败", f"无法读取角色设定文件：{e}")

    def _save(self):
        """保存编辑内容到 SKILL.md"""
        content = self._editor.toPlainText()

        if not self._skill_path:
            # 新建
            base = Path(__file__).parent
            dir_path = base / "skills" / "public" / self._character_id
            dir_path.mkdir(parents=True, exist_ok=True)
            self._skill_path = dir_path / "SKILL.md"

        try:
            self._skill_path.write_text(content, "utf-8")
            logger.info("Saved character settings: %s", self._skill_path)
            self.accept()
        except Exception as e:
            logger.warning("Failed to save SKILL.md: %s", e)
            QMessageBox.warning(self, "保存失败", f"无法保存角色设定：{e}")