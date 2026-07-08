"""角色启动画面 — 首次/切换角色时显示角色设定文字

叠加在桌宠窗口上，半透明深色底 + 核心设定文字。
渐入→停留→渐出，3-5 秒自动消失，点击可跳过。
"""
from __future__ import annotations

import yaml
import logging
from pathlib import Path

from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout
from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QFont, QPalette, QColor

logger = logging.getLogger(__name__)


def _extract_lore(skill_md_path: Path) -> str:
    """从 SKILL.md 提取角色核心设定文字"""
    try:
        text = skill_md_path.read_text("utf-8")
    except Exception:
        return ""

    # 提取 YAML front matter
    front = {}
    if text.startswith("---"):
        rest = text[3:]
        end = rest.find("---")
        if end > 0:
            try:
                front = yaml.safe_load(rest[:end])
            except Exception:
                pass

    name = front.get("name", "").title()
    desc = front.get("description", "")

    # 组合成简短介绍
    lines = []
    if name:
        lines.append(f"「{name}」")
    if desc:
        lines.append(desc[:80])
    if not lines:
        lines.append("正在苏醒...")

    return "\n".join(lines)


class StartupScreen(QWidget):
    """半透明 overlay，启动或切换角色时展示。

    用法:
        screen = StartupScreen(parent_pet_window)
        screen.show_for_character("ophelia")
        # 自动渐入→停留→渐出
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent;")

        self._layout = QVBoxLayout(self)
        self._layout.setAlignment(Qt.AlignCenter)

        self._label = QLabel()
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setWordWrap(True)
        self._label.setStyleSheet("""
            QLabel {
                color: #e0d8cc;
                font-size: 13px;
                font-family: "Microsoft YaHei", sans-serif;
                background: transparent;
                padding: 24px;
            }
        """)

        self._layout.addWidget(self._label)
        self.hide()

        # 渐出计时器
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._start_fadeout)

        # 渐入动画
        self._fade_anim: QPropertyAnimation | None = None

    def show_for_character(self, character_id: str):
        """为指定角色显示启动画面"""
        # 确定 SKILL.md 路径
        base = Path(__file__).parent
        skill_path = base / "skills" / "public" / character_id / "SKILL.md"
        lore = _extract_lore(skill_path) if skill_path.exists() else f"「{character_id}」"

        self._label.setText(lore)

        # 覆盖父窗口
        if self.parent():
            self.setFixedSize(self.parent().size())
            self.move(0, 0)
        self.raise_()

        # 深色半透明遮罩
        self.setStyleSheet("background: rgba(20, 18, 24, 220);")
        self.show()

        # 渐入动画（简化：直接显示，hold 3 秒后渐出）
        self.setWindowOpacity(1.0)
        self._hold_timer.start(3000)

    def _start_fadeout(self):
        """3 秒停留后开始渐出"""
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self._fade_anim.setDuration(600)
        self._fade_anim.setStartValue(self.windowOpacity())
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.setEasingCurve(QEasingCurve.OutQuad)
        self._fade_anim.finished.connect(self._on_done)
        self._fade_anim.start()

    def _on_done(self):
        self.hide()
        self.setStyleSheet("background: transparent;")

    def mousePressEvent(self, event):
        """点击任意处跳过"""
        self._hold_timer.stop()
        self._start_fadeout()