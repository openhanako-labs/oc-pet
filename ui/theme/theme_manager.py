"""主题管理器 — 跟随系统时间切换 light/dark

QSS 样式文件来自视觉灵感学习模块（05-视觉灵感学习/tokens.yaml）。
"""
from __future__ import annotations

import datetime
import logging
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

logger = logging.getLogger(__name__)

THEME_DIR = Path(__file__).parent
LIGHT_QSS_PATH = THEME_DIR / "light.qss"
DARK_QSS_PATH = THEME_DIR / "dark.qss"

# 时间边界（24h 制）
LIGHT_START = datetime.time(6, 0)   # 6:00 起为 light
DARK_START = datetime.time(18, 0)   # 18:00 起为 dark


def _is_dark_now() -> bool:
    """根据当前时间判断是否 dark

    dark = [18:00, 24:00) ∪ [00:00, 06:00)
    light = [06:00, 18:00)
    """
    now = datetime.datetime.now().time()
    return now < LIGHT_START or now >= DARK_START


class ThemeManager(QObject):
    """主题管理器 — 启动时 + 每分钟检查切换

    用法：
        app = QApplication([])
        theme_mgr = ThemeManager(app)
        theme_mgr.apply_initial()       # 启动时调用一次
        # 组件订阅：theme_mgr.theme_changed.connect(on_change)
    """

    theme_changed = Signal(str)  # 'light' or 'dark'

    def __init__(self, app: QApplication):
        super().__init__()
        self._app = app
        self._current = self._detect_initial()

        # 每分钟检查一次（避免错过 6:00 / 18:00 边界）
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._check_switch)
        self._timer.start(60_000)  # 60s

        logger.info("ThemeManager 初始化，当前主题：%s", self._current)

    def _detect_initial(self) -> str:
        return "dark" if _is_dark_now() else "light"

    def _load_qss(self, theme: str) -> str:
        path = DARK_QSS_PATH if theme == "dark" else LIGHT_QSS_PATH
        if not path.exists():
            logger.warning("QSS 文件不存在：%s", path)
            return ""
        return path.read_text(encoding="utf-8")

    def _check_switch(self):
        new = self._detect_initial()
        if new != self._current:
            logger.info("主题切换：%s → %s", self._current, new)
            self._current = new
            self._apply()
            self.theme_changed.emit(new)

    def _apply(self):
        qss = self._load_qss(self._current)
        self._app.setStyleSheet(qss)

    def apply_initial(self):
        """启动时调用一次"""
        self._apply()

    def force_switch(self, theme: str):
        """手动切换主题（测试 / 用户手动覆盖用）

        注意：这不会改变时间判断逻辑，60s 后如果时间到了边界会自动切回去
        """
        if theme not in ("light", "dark"):
            return
        if theme != self._current:
            logger.info("手动切换：%s → %s", self._current, theme)
            self._current = theme
            self._apply()
            self.theme_changed.emit(theme)

    @property
    def current(self) -> str:
        return self._current


# 全局单例
_default: "ThemeManager | None" = None


def init_default(app: QApplication) -> "ThemeManager":
    """初始化全局 ThemeManager（main.py 启动时调用一次）"""
    global _default
    if _default is None:
        _default = ThemeManager(app)
    return _default


def get_default() -> "ThemeManager | None":
    """获取全局 ThemeManager（UI 组件订阅用）"""
    return _default