"""眼睛跟随光标 — 瞳孔 overlay 追逐鼠标位置

在角色上方叠加两个瞳孔圆点，偏移方向跟随光标。
每 50ms 更新一次，点击穿透。
"""
from __future__ import annotations

import math
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QTimer, QPoint, QRect
from PySide6.QtGui import QPainter, QColor, QCursor, QBrush


# 瞳孔配置
PUPIL_RADIUS = 3           # 瞳孔半径 (px)
PUPIL_COLOR = QColor(40, 40, 50, 220)      # 深灰蓝
PUPIL_HIGHLIGHT = QColor(255, 255, 255, 120)  # 高光白

# 眼睛相对位置（相对于角色标签的百分比）
LEFT_EYE_PCT  = (0.38, 0.25)
RIGHT_EYE_PCT = (0.62, 0.25)

# 瞳孔最大偏移
MAX_OFFSET = 4  # px


class EyeOverlay(QWidget):
    """瞳孔覆盖物 — 透明、点击穿透、跟踪鼠标。

    用法：
        overlay = EyeOverlay(parent_widget)
        overlay.set_geometry(0, 0, 200, 360)  # 与角色标签对齐
        overlay.timer.start(50)                # 开始跟踪
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("background: transparent;")

        self._left_pupil_offset = QPoint(0, 0)
        self._right_pupil_offset = QPoint(0, 0)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_pupils)
        self._started = False

    def start(self):
        self._started = True
        self.timer.start(50)
        self.show()
        self.raise_()

    def stop(self):
        self._started = False
        self.timer.stop()
        self.hide()

    def _update_pupils(self):
        if not self._started or not self.isVisible():
            return

        # 获取光标相对于本 overlay 的位置
        global_cursor = QCursor.pos()
        local = self.mapFromGlobal(global_cursor)

        w = self.width()
        h = self.height()

        # 计算光标相对于两个眼睛的偏移方向
        left_center = QPoint(int(w * LEFT_EYE_PCT[0]), int(h * LEFT_EYE_PCT[1]))
        right_center = QPoint(int(w * RIGHT_EYE_PCT[0]), int(h * RIGHT_EYE_PCT[1]))

        for eye_pos, attr in [
            (left_center, "_left_pupil_offset"),
            (right_center, "_right_pupil_offset"),
        ]:
            dx = local.x() - eye_pos.x()
            dy = local.y() - eye_pos.y()
            dist = math.sqrt(dx * dx + dy * dy)
            if dist > 0.5:
                # 归一化并缩放到 MAX_OFFSET 内
                scale = min(dist, w * 2) / (w * 2)  # 0~1，越近越敏感
                offset_x = dx / dist * MAX_OFFSET * scale
                offset_y = dy / dist * MAX_OFFSET * scale
            else:
                offset_x = 0.0
                offset_y = 0.0
            setattr(self, attr, QPoint(int(offset_x), int(offset_y)))

        self.update()

    def paintEvent(self, event):
        if not self._started:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()

        left_center = QPoint(int(w * LEFT_EYE_PCT[0]), int(h * LEFT_EYE_PCT[1]))
        right_center = QPoint(int(w * RIGHT_EYE_PCT[0]), int(h * RIGHT_EYE_PCT[1]))

        for eye_center, offset in [
            (left_center, self._left_pupil_offset),
            (right_center, self._right_pupil_offset),
        ]:
            cx = eye_center.x() + offset.x()
            cy = eye_center.y() + offset.y()

            # 瞳孔
            painter.setBrush(QBrush(PUPIL_COLOR))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(cx - PUPIL_RADIUS, cy - PUPIL_RADIUS,
                                PUPIL_RADIUS * 2, PUPIL_RADIUS * 2)

            # 高光
            painter.setBrush(QBrush(PUPIL_HIGHLIGHT))
            hl_x = cx - PUPIL_RADIUS // 3
            hl_y = cy - PUPIL_RADIUS // 2
            hl_r = max(1, PUPIL_RADIUS // 2)
            painter.drawEllipse(hl_x, hl_y, hl_r, hl_r)

        painter.end()

    def resize_for_character(self, char_width: int, char_height: int):
        """跟随角色标签尺寸"""
        self.setFixedSize(char_width, char_height)
        self.update()