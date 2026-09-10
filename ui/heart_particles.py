"""爱心粒子叠层 — 抚摸/喂食时从宠物头顶冒出的爱心。

设计为桌宠窗口的子控件：透明、不拦截鼠标事件（不挡抚摸手势）。
burst(count, x, y) 在窗口局部坐标 (x, y) 处冒出 count 颗爱心，
边上升边淡出，约 1.2s 后自动销毁。
"""
from __future__ import annotations

import random

from PySide6.QtCore import Qt, QPropertyAnimation, QPoint
from PySide6.QtWidgets import QWidget, QLabel
import logging
logger = logging.getLogger(__name__)



HEART_GLYPHS = ["💗", "💖", "💕", "♥", "🐾"]


class HeartBurst(QWidget):
    """头顶爱心粒子层（透明、穿透鼠标）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowFlags(Qt.FramelessWindowHint)
        # 覆盖整个父窗口，便于用窗口局部坐标定位粒子
        if parent is not None:
            self.setGeometry(0, 0, parent.width(), parent.height())

    def resize_to_parent(self):
        if self.parent() is not None:
            # 给一个最小覆盖尺寸，避免粒子落在窗口矩形外被裁剪
            w = max(self.parent().width(), 360)
            h = max(self.parent().height(), 360)
            self.setGeometry(0, 0, w, h)

    def burst(self, count: int = 3, x: int = None, y: int = None):
        """在 (x, y)（窗口局部坐标）冒出 count 颗爱心"""
        if x is None:
            x = (self.width() or 100) // 2
        if y is None:
            y = (self.height() or 100) // 2
        for _ in range(max(1, int(count))):
            self._spawn_one(x, y)

    def _spawn_one(self, x: int, y: int):
        try:
            lab = QLabel(random.choice(HEART_GLYPHS), self)
            lab.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            lab.setStyleSheet("background: transparent;")
            font = lab.font()
            font.setPointSize(random.randint(14, 24))
            lab.setFont(font)
            lab.adjustSize()
            ox = x + random.randint(-16, 16)
            oy = y + random.randint(-8, 8)
            lab.move(ox, oy)
            lab.show()

            dur = random.randint(900, 1500)
            dx = random.randint(-22, 22)
            dy = -random.randint(34, 64)

            pos_anim = QPropertyAnimation(lab, b"pos", self)
            pos_anim.setDuration(dur)
            pos_anim.setStartValue(QPoint(ox, oy))
            pos_anim.setEndValue(QPoint(ox + dx, oy + dy))

            op_anim = QPropertyAnimation(lab, b"windowOpacity", self)
            op_anim.setDuration(dur)
            op_anim.setStartValue(1.0)
            op_anim.setEndValue(0.0)

            pos_anim.finished.connect(lab.deleteLater)
            pos_anim.start()
            op_anim.start()
        except Exception:
            # 单颗失败不影响整体
            logger.debug("heart_particles: 非致命异常(已静默吞掉)", exc_info=True)
