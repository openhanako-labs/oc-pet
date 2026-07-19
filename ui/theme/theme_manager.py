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
        self._mode = "auto"  # "auto" | "light" | "dark"
        self._current = self._detect_initial()

        # 每分钟检查一次（避免错过 6:00 / 18:00 边界）
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._check_switch)
        self._timer.start(60_000)  # 60s

        logger.info("ThemeManager 初始化，当前主题：%s，模式：%s", self._current, self._mode)

    def _detect_initial(self) -> str:
        return "dark" if _is_dark_now() else "light"

    def _load_qss(self, theme: str) -> str:
        path = DARK_QSS_PATH if theme == "dark" else LIGHT_QSS_PATH
        if not path.exists():
            logger.warning("QSS 文件不存在：%s", path)
            return ""
        return path.read_text(encoding="utf-8")

    def _check_switch(self):
        if self._mode != "auto":
            return  # 手动模式，禁用自动检查
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
        """手动切换主题（兼容老 API，会自动禁用自动模式）

        注意：调用后会禁用自动切换，不会自动切回时间判断的主题
        """
        if theme not in ("light", "dark"):
            return
        self.set_mode(theme)

    def set_mode(self, mode: str):
        """设置主题模式

        - "auto": 跟随时间自动切换（默认）
        - "light": 强制浅色（禁用自动检查）
        - "dark": 强制深色（禁用自动检查）

        子菜单、设置面板都可以调用这个接口。
        """
        if mode not in ("auto", "light", "dark"):
            logger.warning("未知主题模式：%s", mode)
            return
        old_mode = self._mode
        old_theme = self._current
        self._mode = mode
        if mode == "auto":
            self._current = self._detect_initial()
        else:
            self._current = mode
        if old_theme != self._current:
            self._apply()
            self.theme_changed.emit(self._current)
        logger.info("主题模式：%s → %s（主题：%s）", old_mode, mode, self._current)

    @property
    def current(self) -> str:
        return self._current

    @property
    def mode(self) -> str:
        """当前模式（auto / light / dark）"""
        return self._mode


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