"""Amadeus 风格 HUD —— monospace + 圆点状态灯（N.E.K.O. 设计语言重上色，P1-8）。

功能保持原样（只换肤）：
  - 玻璃质感面板（半透明底 + 顶部内高光 + 发光描边）
  - 文字磷光辉光
  - 状态点带呼吸脉冲，整体「活」起来

P1-8：配色/圆角/间距全部取自 ``ui/theme/neko_palette.py`` 设计 token
（hud_* 段），替换原 cyberpunk 绿硬编码；主题感知（跟随 ThemeManager）。
"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, QRect, QPoint, QTimer
from PySide6.QtGui import QPainter, QColor, QFont, QFontMetrics
from PySide6.QtWidgets import QWidget

from ui.crt_effects import glass_panel, phosphor_glow_text, phosphor_glow_ellipse
from ui.theme.neko_palette import NEKO_LAYOUT, neko_qcolor
import logging
logger = logging.getLogger(__name__)



class AmadeusHUD(QWidget):
    """右上角 Amadeus 风格 HUD。

    调用 set_counts(running, need_you, active) 更新数字。
    数字 0 = 灰色，>0 = 主题色（蓝 / 黄 / 青）。状态点带呼吸脉冲。
    """

    DOT_RUNNING = 0
    DOT_NEEDYOU = 1
    DOT_ACTIVE = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self._running = 0
        self._need_you = 0
        self._active = 0
        self._pulse = 0.0
        self._theme = "dark"

        # 布局 token 来自 neko_palette（P1-8 去硬编码）
        self._pad_x = NEKO_LAYOUT["amadeus_pad_x"]
        self._pad_y = NEKO_LAYOUT["amadeus_pad_y"]
        self._dot_r = NEKO_LAYOUT["amadeus_dot_r"]
        self._gap = NEKO_LAYOUT["amadeus_gap"]
        self._radius = NEKO_LAYOUT["amadeus_radius"]
        self._pulse_ms = NEKO_LAYOUT["amadeus_pulse_ms"]

        self._font = QFont("Consolas", 9)
        self._font.setStyleHint(QFont.Monospace)
        self.setFont(self._font)

        self._update_size()

        # 状态点呼吸脉冲
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._tick_pulse)
        self._pulse_timer.start(self._pulse_ms)

        try:
            from ui.theme import get_default
            mgr = get_default()
            if mgr is not None:
                self._theme = mgr.current
                mgr.theme_changed.connect(self._on_theme)
        except Exception:
            logger.debug("amadeus_hud: 非致命异常(已静默吞掉)", exc_info=True)

    def set_counts(self, running: int, need_you: int, active: int) -> None:
        self._running = max(0, int(running))
        self._need_you = max(0, int(need_you))
        self._active = max(0, int(active))
        self.update()

    def _on_theme(self, theme: str) -> None:
        self._theme = theme
        self.update()

    def _tick_pulse(self) -> None:
        self._pulse = (self._pulse + 0.4) % (2 * math.pi)
        self.update()

    def _update_size(self) -> None:
        fm = QFontMetrics(self._font)
        sample = "0 RUNNING · 0 NEED YOU · 0 ACTIVE"
        text_w = fm.horizontalAdvance(sample)
        text_h = fm.height()
        w = text_w + self._pad_x * 2 + self._dot_r * 2 * 3 + self._gap * 3
        h = text_h + self._pad_y * 2
        self.setFixedSize(w, h)

    def _colors(self, theme: str | None = None) -> dict[str, QColor]:
        """当前主题的全部 HUD 颜色（取自 neko_palette hud_* token）。"""
        t = (theme or self._theme)
        t = t if t in ("light", "dark") else "dark"
        return {
            "bg": neko_qcolor(t, "hud_panel_bg"),
            "border": neko_qcolor(t, "hud_panel_border"),
            "border_glow": neko_qcolor(t, "hud_border_glow"),
            "text_dim": neko_qcolor(t, "hud_text_secondary"),
            "text_bright": neko_qcolor(t, "hud_glow_text"),
            "dot_running": neko_qcolor(t, "hud_dot_running"),
            "dot_needyou": neko_qcolor(t, "hud_dot_needyou"),
            "dot_active": neko_qcolor(t, "hud_dot_active"),
            "dot_idle": neko_qcolor(t, "hud_dot_idle"),
        }

    @property
    def DOT_COLORS(self) -> tuple[QColor, QColor, QColor]:
        """兼容旧接口：当前主题的三个状态点颜色。"""
        cols = self._colors()
        return (cols["dot_running"], cols["dot_needyou"], cols["dot_active"])

    @property
    def theme(self) -> str:
        return self._theme

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cols = self._colors()

        # 玻璃面板
        glass_panel(
            p, QRect(0, 0, w, h), self._radius,
            cols["bg"], cols["border"], cols["border_glow"], top_highlight=True,
        )

        # 三组 [圆点 + 数字 + 文字]
        p.setFont(self._font)
        fm = p.fontMetrics()
        text_y = (h - fm.height()) // 2

        sections = (
            (self._running, "RUNNING", self.DOT_RUNNING),
            (self._need_you, "NEED YOU", self.DOT_NEEDYOU),
            (self._active, "ACTIVE", self.DOT_ACTIVE),
        )
        dot_colors = (cols["dot_running"], cols["dot_needyou"], cols["dot_active"])

        x = self._pad_x
        pulse_a = 0.75 + 0.25 * (0.5 + 0.5 * math.sin(self._pulse))
        for idx, (count, label, dot_kind) in enumerate(sections):
            active = count > 0
            dot_color = dot_colors[dot_kind] if active else cols["dot_idle"]
            cy = h // 2

            if active:
                # 呼吸脉冲辉光
                glow = QColor(dot_color)
                glow.setAlpha(int(160 * pulse_a))
                phosphor_glow_ellipse(p, x + self._dot_r, cy, self._dot_r,
                                      dot_color, glow, passes=3, spread=2.0)
            else:
                p.setBrush(dot_color)
                p.setPen(Qt.NoPen)
                p.drawEllipse(QPoint(x + self._dot_r, cy), self._dot_r, self._dot_r)

            x += self._dot_r * 2 + 4

            seg = f"{count} {label}"
            text_color = cols["text_bright"] if active else cols["text_dim"]
            # 数字用更亮的主题色做辉光，标签用本色
            phosphor_glow_text(
                p, seg, x, text_y,
                self._font, text_color, cols["text_bright"],
                align=Qt.AlignLeft, glow_passes=2, glow_spread=0.45,
            )
            x += fm.horizontalAdvance(seg)

            if idx < len(sections) - 1:
                sep = " · "
                p.setPen(cols["text_dim"])
                p.drawText(x, text_y, sep)
                x += fm.horizontalAdvance(sep)

        p.end()
