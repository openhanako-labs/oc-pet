"""情绪表情 — 悬浮在宠物头顶的小圆脸，直观反映当前情绪。

轮询桌宠的 _current_emotion 更新：😊开心 / 😢难过 / 🤔思考 / 😲惊讶 / 😐平静。
情绪变化时做一次轻微"弹出"动画，让宠物真的"有情绪"。
"""
from __future__ import annotations

import time
from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve, Property, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget
import logging
logger = logging.getLogger(__name__)



EMOTION_EMOJI = {
    "happy": "😊",
    "sad": "😢",
    "thinking": "🤔",
    "surprised": "😲",
    "angry": "😠",
    "neutral": "😐",
    # 自发微表情（体现性格，非情绪态驱动）
    "blink": "😉",
    "heh": "😝",
    "love": "🥰",
    "shy": "☺️",
}

# 情绪中文文案（HUD 底部“当前情绪”使用）
EMOTION_LABEL = {
    "happy": "开心",
    "sad": "难过",
    "thinking": "思考中",
    "surprised": "惊讶",
    "angry": "生气",
    "neutral": "平静",
}


class EmotionFace(QWidget):
    """头顶情绪小圆脸"""

    SIZE = 34

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setFixedSize(self.SIZE, self.SIZE)

        self._theme = "dark"
        self._emotion = "neutral"
        self._scale = 1.0
        self._last_pop_time = 0.0
        self._flash_until = 0.0  # 自发微表情(flash)到期时间戳

        # 初始隐藏，只在非 neutral 情绪时短暂显示
        self.hide()
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._do_hide)

        try:
            from ui.theme import get_default
            mgr = get_default()
            if mgr is not None:
                self._theme = mgr.current
                mgr.theme_changed.connect(self._on_theme)
        except Exception:
            logger.debug("emotion_face: 非致命异常(已静默吞掉)", exc_info=True)

    def _on_theme(self, theme: str):
        self._theme = theme
        self.update()

    # Qt 属性：供 QPropertyAnimation 驱动弹出动画
    def _get_scale(self) -> float:
        return self._scale

    def _set_scale(self, v: float):
        self._scale = v
        self.update()

    popScale = Property(float, _get_scale, _set_scale)

    def set_emotion(self, emotion: str) -> None:
        if emotion not in EMOTION_EMOJI:
            emotion = "neutral"
        now = time.time()
        # 自发微表情(flash)进行中：真实情绪变化可打断接管，平静轮询忽略
        if now < self._flash_until:
            if emotion == "neutral":
                return
            self._flash_until = 0.0
        if emotion == self._emotion:
            return
        self._emotion = emotion

        if emotion == "neutral":
            # 回到平静后延迟 800ms 再隐藏，避免情绪切换时闪烁
            self._hide_timer.stop()
            self._hide_timer.start(800)
            self.update()
            return

        # 非平静情绪：显示并弹出
        self._hide_timer.stop()
        if not self.isVisible():
            self.show()
            self.raise_()
        self.update()
        self._pop()

    def _do_hide(self):
        if self._emotion == "neutral":
            self.hide()

    def flash(self, emotion: str, ms: int = 1300) -> None:
        """自发/情境微表情：临时显示并自动隐藏，不持久改写情绪态。

        用于「有性格的随机反应」——例如安静时偶尔眨眼、被夸时害羞。
        期间宠物真实情绪变化(set_emotion 非 neutral)会打断并接管显示。
        """
        if emotion not in EMOTION_EMOJI or emotion == "neutral":
            return
        self._flash_until = time.time() + ms / 1000.0
        self._emotion = emotion
        if not self.isVisible():
            self.show()
            self.raise_()
        self.update()
        self._pop()
        QTimer.singleShot(ms, self._end_flash)

    def _end_flash(self):
        if time.time() >= self._flash_until:
            self._emotion = "neutral"
            self.hide()

    def _pop(self):
        """情绪变化时轻微弹出；1 秒内不重复弹出，避免情绪高频抖动"""
        try:
            now = time.time()
            if now - self._last_pop_time < 1.0:
                return
            self._last_pop_time = now
            self._scale = 0.6
            self.update()
            # 存 self 引用，防止动画运行期间被 GC 提前回收
            self._pop_anim = QPropertyAnimation(self, b"popScale", self)
            self._pop_anim.setDuration(220)
            self._pop_anim.setEasingCurve(QEasingCurve.OutBack)
            self._pop_anim.setStartValue(0.6)
            self._pop_anim.setEndValue(1.0)
            self._pop_anim.start()
        except Exception:
            logger.debug("emotion_face: 非致命异常(已静默吞掉)", exc_info=True)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        dark = self._theme == "dark"
        s = self.SIZE
        cx, cy = s // 2, s // 2
        r = (s // 2 - 2) * self._scale

        # 玻璃圆底
        bg = QColor(30, 34, 58, 220) if dark else QColor(255, 250, 242, 235)
        border = QColor(140, 160, 220, 140) if dark else QColor(190, 160, 130, 150)
        circle = QPainterPath()
        circle.addEllipse(cx - r, cy - r, r * 2, r * 2)
        p.fillPath(circle, bg)
        p.setPen(border)
        p.setBrush(Qt.NoBrush)
        p.drawPath(circle)

        # 表情
        p.setPen(Qt.NoPen)
        font = QFont("Segoe UI Emoji", int(r * 1.1))
        p.setFont(font)
        p.drawText(cx - r, cy - r, int(r * 2), int(r * 2),
                   Qt.AlignCenter, EMOTION_EMOJI.get(self._emotion, "😐"))
        p.end()
