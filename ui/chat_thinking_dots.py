"""思考点动画 — 「…」三点脉冲（P0-7）

参考 N.E.K.O. React ``styles.css`` 的 ``focus-thinking-dots`` /
``@keyframes focus-thinking-dot-pulse``：三点依次放大+上浮再回落，
周期约 1.8s（CSS 原版 1.25s，P0-7 验收要求 ~1.8s）。

实现：单个 ``QVariantAnimation``（0→1，1800ms，无限循环）驱动相位，
``paintEvent`` 按每个点的相位偏移（0.11 周期 ≈ 0.2s 交错）绘制圆点。
纯 QWidget 绘制，无 QWebEngine、无外部资源。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QEasingCurve, QVariantAnimation
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from ui.theme.neko_palette import glow_rgb

DOT_PERIOD_MS = 1800        # 周期 ~1.8s
DOT_COUNT = 3               # 三个点
DOT_RADIUS = 3              # 单点半径 (px)
DOT_GAP = 5                 # 点间距 (px)
DOT_STAGGER = 0.11          # 相位交错（占周期比例）≈ 0.2s
_DOT_PEAK = 0.35            # 峰值位置（对齐 CSS 35% 处 opacity=1）
_DOT_FLOOR_ALPHA = 0.30     # 静止透明度（对齐 CSS opacity: 0.3）
_DOT_PEAK_LIFT = 2.0        # 峰值上浮 (px)


class ChatThinkingDots(QWidget):
    """三点点脉冲思考动画。

    用法：``dots = ChatThinkingDots(theme="light")`` → ``dots.start()`` /
    ``dots.stop()``。尺寸固定为三点一行（宽 = 3*2r + 2*gap，高 = 2r + lift）。
    """

    def __init__(self, parent=None, theme: str = "light"):
        super().__init__(parent)
        self._theme = theme if theme in ("light", "dark") else "light"
        self._phase: float = 0.0
        self._running: bool = False

        w = DOT_COUNT * (DOT_RADIUS * 2) + (DOT_COUNT - 1) * DOT_GAP
        h = DOT_RADIUS * 2 + int(_DOT_PEAK_LIFT) + 4
        self.setFixedSize(w, h)

        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(DOT_PERIOD_MS)
        self._anim.setLoopCount(-1)
        self._anim.setEasingCurve(QEasingCurve.Linear)
        self._anim.valueChanged.connect(self._on_phase)

    # ── 公共 API ──

    def start(self) -> None:
        """开始脉冲动画（幂等）。"""
        if not self._running:
            self._running = True
            self._anim.start()

    def stop(self) -> None:
        """停止动画并复位为静止态（幂等）。"""
        if self._running:
            self._anim.stop()
            self._running = False
        self._phase = 0.0
        self.update()

    def is_running(self) -> bool:
        return self._running

    def set_theme(self, theme: str) -> None:
        """切换主题（重绘取色）。"""
        if theme in ("light", "dark"):
            self._theme = theme
            self.update()

    @property
    def theme(self) -> str:
        return self._theme

    # ── 动画 / 绘制 ──

    def _on_phase(self, value: object) -> None:
        self._phase = float(value) % 1.0
        self.update()

    @staticmethod
    def _dot_alpha(phase: float) -> float:
        """单点透明度曲线：0→0.35 升到 1.0，0.35→0.7 回落到 0.3，之后保持。"""
        if phase < _DOT_PEAK:
            return _DOT_FLOOR_ALPHA + (1.0 - _DOT_FLOOR_ALPHA) * (phase / _DOT_PEAK)
        if phase < _DOT_PEAK * 2:
            return 1.0 - (1.0 - _DOT_FLOOR_ALPHA) * ((phase - _DOT_PEAK) / _DOT_PEAK)
        return _DOT_FLOOR_ALPHA

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        base = QColor(*glow_rgb(self._theme))
        base.setAlpha(255)
        diameter = DOT_RADIUS * 2
        y_base = float(self.height() - diameter) / 2.0 + _DOT_PEAK_LIFT
        for i in range(DOT_COUNT):
            phase = (self._phase - i * DOT_STAGGER) % 1.0
            alpha = self._dot_alpha(phase)
            lift = _DOT_PEAK_LIFT * ((alpha - _DOT_FLOOR_ALPHA) / (1.0 - _DOT_FLOOR_ALPHA))
            color = QColor(base)
            color.setAlphaF(alpha)
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            x = i * (diameter + DOT_GAP)
            y = y_base - lift
            p.drawEllipse(int(x), int(y), diameter, diameter)
        p.end()
