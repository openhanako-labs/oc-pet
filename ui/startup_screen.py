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

from ui.theme import get_default, rgba, rgb, THEME_COLORS

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
                logger.debug("startup_screen: 非致命异常(已静默吞掉)", exc_info=True)

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
        screen.show_for_character("yuexinmiao")
        # 自动渐入→停留→渐出
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent;")

        mgr = get_default()
        self._ui_theme = mgr.current if mgr else "dark"

        # 玻璃卡（居中浮于桌宠之上，替代全窗平涂遮罩）
        from ui.theme.design_system import apply_glass_shadow
        self._card = QWidget(self)
        self._card.setObjectName("glassPanel")
        apply_glass_shadow(self._card, self._ui_theme)

        self._label = QLabel()
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setWordWrap(True)
        self._apply_styles()

        cl = QVBoxLayout(self._card)
        cl.setContentsMargins(28, 22, 28, 22)
        cl.setAlignment(Qt.AlignCenter)
        cl.addWidget(self._label)
        self._card.setMinimumWidth(300)
        self._card.adjustSize()

        if mgr is not None:
            mgr.theme_changed.connect(self.set_theme)

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
        self._card.adjustSize()

        # 覆盖父窗口
        if self.parent():
            self.setFixedSize(self.parent().size())
            self.move(0, 0)
        self.raise_()

        # 居中玻璃卡
        cw, ch = self._card.width(), self._card.height()
        self._card.move((self.width() - cw) // 2, (self.height() - ch) // 2)

        # 透明窗口 + 居中玻璃卡
        self.setStyleSheet("background: transparent;")
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

    def _apply_styles(self):
        """按主题设置启动文字颜色"""
        t = self._ui_theme
        self._label.setStyleSheet(f"""
            QLabel {{
                color: rgb({rgb(t, 'text_primary')});
                font-size: 13px;
                font-family: "Microsoft YaHei", sans-serif;
                background: transparent;
                padding: 24px;
            }}
        """)

    def set_theme(self, theme: str):
        """主题切换 — 由 ThemeManager.theme_changed 信号触发"""
        if theme not in THEME_COLORS:
            return
        if theme == self._ui_theme:
            return
        self._ui_theme = theme
        self._apply_styles()
        if self.isVisible():
            self.setStyleSheet("background: transparent;")

    def mousePressEvent(self, event):
        """点击任意处跳过"""
        self._hold_timer.stop()
        self._start_fadeout()
