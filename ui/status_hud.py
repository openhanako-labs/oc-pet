"""状态面板 HUD — 玻璃拟态需求条，悬浮在宠物头顶。

显示 5 项养成属性（饱腹/口渴/心情/精力/健康），颜色随数值高低变化
（高=青绿、中=琥珀、低=红），一眼看清宠物需求。主题感知（跟随 ThemeManager）。

设计为桌宠窗口子控件，透明、不拦截鼠标。

P1-8 重上色：配色/圆角/间距全部取自 ``ui/theme/neko_palette.py`` 设计 token
（N.E.K.O. 设计语言），不再散落硬编码色值；功能逻辑不变（只换肤）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

from ui.emotion_face import EMOTION_LABEL
from ui.theme.neko_palette import NEKO_LAYOUT, neko_qcolor, palette
import logging
logger = logging.getLogger(__name__)



# 行定义：(emoji, 名称, 取值键)
_ROWS = (
    ("🍖", "饱腹", "hunger"),
    ("💧", "口渴", "thirst"),
    ("😊", "心情", "mood"),
    ("⚡", "精力", "energy"),
    ("❤️", "健康", "health"),
)

# 情绪 → palette token 后缀（色值在 neko_palette 的 hud_emotion_* 定义）
_EMOTION_TOKEN = {
    "happy": "hud_emotion_happy",
    "sad": "hud_emotion_sad",
    "thinking": "hud_emotion_thinking",
    "surprised": "hud_emotion_surprised",
    "angry": "hud_emotion_angry",
    "neutral": "hud_emotion_neutral",
}


def _bar_color(ratio: float, dark: bool) -> QColor:
    """数值高低→颜色（绿→琥珀→红），色值取自 neko_palette hud_bar_* token。"""
    theme = "dark" if dark else "light"
    if ratio >= 0.6:
        return neko_qcolor(theme, "hud_bar_good")
    if ratio >= 0.3:
        return neko_qcolor(theme, "hud_bar_warn")
    return neko_qcolor(theme, "hud_bar_bad")


class StatusHUD(QWidget):
    """头顶需求面板（玻璃拟态）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setWindowFlags(Qt.FramelessWindowHint)

        self._theme = "dark"
        self._stats: dict[str, tuple[float, float]] = {}
        self._row_h = NEKO_LAYOUT["hud_row_h"]
        self._pad = NEKO_LAYOUT["hud_pad"]
        self._bar_h = NEKO_LAYOUT["hud_bar_h"]
        self._bar_radius = NEKO_LAYOUT["hud_bar_radius"]
        self._radius = NEKO_LAYOUT["hud_radius"]
        self._emo_row_h = NEKO_LAYOUT["hud_emo_row_h"]
        self._emotion = "neutral"
        self.setFixedSize(
            188,
            self._pad * 2 + 18 + len(_ROWS) * self._row_h + self._emo_row_h,
        )

        try:
            from ui.theme import get_default
            mgr = get_default()
            if mgr is not None:
                self._theme = mgr.current
                mgr.theme_changed.connect(self._on_theme)
        except Exception:
            logger.debug("status_hud: 非致命异常(已静默吞掉)", exc_info=True)

    def _on_theme(self, theme: str):
        self._theme = theme
        self.update()

    def set_stats(self, save) -> None:
        """从 PetSave 实例刷新数值"""
        try:
            self._stats = {
                "hunger": (float(save.hunger), 100.0),
                "thirst": (float(save.thirst), 100.0),
                "mood": (float(save.mood), float(save.mood_max)),
                "energy": (float(save.energy), 100.0),
                "health": (float(save.health), float(save.health_max)),
            }
            self.update()
        except Exception:
            logger.debug("status_hud: 非致命异常(已静默吞掉)", exc_info=True)

    def set_emotion(self, emotion: str) -> None:
        """设置当前情绪（与头顶情绪脸同步），底部显示文案"""
        if emotion not in EMOTION_LABEL:
            emotion = "neutral"
        if emotion == self._emotion:
            return
        self._emotion = emotion
        self.update()

    def paintEvent(self, event):
        if not self._stats:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        theme = "dark" if self._theme == "dark" else "light"

        w, h = self.width(), self.height()
        # 玻璃面板（颜色来自 neko_palette）
        bg = neko_qcolor(theme, "hud_panel_bg")
        border = neko_qcolor(theme, "hud_panel_border")
        path = QPainterPath()
        path.addRoundedRect(0, 0, w, h, self._radius, self._radius)
        p.fillPath(path, bg)
        p.setPen(border)
        p.drawPath(path)

        # 标题
        p.setPen(neko_qcolor(theme, "hud_title"))
        p.setFont(QFont("Microsoft YaHei UI", 10, QFont.Weight.Bold))
        p.drawText(self._pad, self._pad + 2, "状态")

        # 各属性条
        text_col = neko_qcolor(theme, "hud_text")
        track_col = neko_qcolor(theme, "hud_track")
        y = self._pad + 18
        for emoji, name, key in _ROWS:
            val, mx = self._stats.get(key, (0.0, 100.0))
            ratio = max(0.0, min(1.0, val / mx)) if mx > 0 else 0.0

            p.setPen(text_col)
            p.setFont(QFont("Microsoft YaHei UI", 9))
            p.drawText(self._pad, y + self._row_h - 7, f"{emoji} {name}")

            # 条 track
            bar_x = self._pad + 52
            bar_w = w - bar_x - self._pad - 34
            track_rect = (bar_x, y + self._row_h // 2 - self._bar_h // 2, bar_w, self._bar_h)
            p.setPen(Qt.NoPen)
            p.setBrush(track_col)
            p.drawRoundedRect(*track_rect, self._bar_radius, self._bar_radius)

            # 条 fill
            fill_w = max(2, int(bar_w * ratio))
            p.setBrush(_bar_color(ratio, self._theme == "dark"))
            p.drawRoundedRect(bar_x, track_rect[1], fill_w, self._bar_h,
                              self._bar_radius, self._bar_radius)

            # 数值
            p.setPen(text_col)
            p.setFont(QFont("Microsoft YaHei UI", 8))
            p.drawText(w - self._pad - 30, y + self._row_h - 7, f"{int(val)}")

            y += self._row_h

        # 当前情绪（底部文案，与头顶情绪脸同步）
        label = EMOTION_LABEL.get(self._emotion, "平静")
        p.setPen(text_col)
        p.setFont(QFont("Microsoft YaHei UI", 9, QFont.Weight.Bold))
        p.drawText(self._pad, self.height() - self._emo_row_h + 14, "当前情绪")
        # 情绪色点
        emo_token = _EMOTION_TOKEN.get(self._emotion, "hud_emotion_neutral")
        if emo_token not in palette(theme):
            emo_token = "hud_emotion_neutral"
        emo_dot = neko_qcolor(theme, emo_token)
        p.setBrush(emo_dot)
        p.setPen(Qt.NoPen)
        p.drawEllipse(self._pad + 64, self.height() - self._emo_row_h + 6, 9, 9)
        p.setPen(text_col)
        p.setFont(QFont("Microsoft YaHei UI", 9))
        p.drawText(self._pad + 78, self.height() - self._emo_row_h + 14, label)

        p.end()
