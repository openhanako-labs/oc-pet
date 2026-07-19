"""对话气泡组件 — 主题感知 + 淡入动画 + 打字机效果

颜色从 ThemeManager 拉，不再硬编码。
"""
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QTimer, QRect, QPropertyAnimation, Signal
from PySide6.QtGui import QPainter, QFont, QColor, QPainterPath, QFontMetrics

from ui.theme import get_default


# 主题色字典（与 ui/theme/light.qss 和 dark.qss 对齐）
THEME_COLORS = {
    "light": {
        "bg": (255, 246, 235, 240),        # 米色透明（暖深底上的气泡）
        "text": (60, 40, 25, 255),          # 暖深棕字
        "bright_bg": (255, 220, 180, 245),  # 沙橙高亮
        "bright_text": (80, 40, 10, 255),   # 深橙字
        "shadow": (0, 0, 0, 40),
    },
    "dark": {
        "bg": (20, 24, 50, 230),            # 夜蓝紫透明
        "text": (232, 236, 245, 255),       # 月白
        "bright_bg": (233, 196, 106, 235),  # 金黄高亮
        "bright_text": (12, 14, 28, 255),   # 夜底字
        "shadow": (0, 0, 0, 80),
    },
}


class ChatBubble(QWidget):
    """头顶对话气泡 — 主题感知 + 淡入 + 打字机 + 三角指针"""

    theme_changed = Signal()  # 主题切换时通知外部（pet.py 重绘）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._full_text = ""
        self._typewriter_revealed = 0
        self._is_typing = False
        self._on_typing_done = None
        self._typewriter_speed = 28
        self._padding_h = 14
        self._padding_v = 10
        self._max_width = 200
        self._theme = "light"  # 默认 light
        self._bg_color = self._color("bg")
        self._text_color = self._color("text")
        self._shadow_color = self._color("shadow")
        self._font = QFont("Microsoft YaHei UI", 10)
        self.setFont(self._font)
        self.setMinimumSize(40, 30)

        # 连接全局 ThemeManager（如果已初始化）
        mgr = get_default()
        if mgr is not None:
            self._theme = mgr.current
            self._bg_color = self._color("bg")
            self._text_color = self._color("text")
            self._shadow_color = self._color("shadow")
            mgr.theme_changed.connect(self.set_theme)

        # 淡入动画
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self._fade_anim.setDuration(250)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)

        # 打字机时钟
        self._typewriter_timer = QTimer(self)
        self._typewriter_timer.timeout.connect(self._typewriter_tick)

        self._flash_timer = QTimer(self)
        self._flash_timer.timeout.connect(self._flash_tick)
        self._flash_count = 0
        self._bright = False
        self.hide()

    def _color(self, key: str) -> QColor:
        """取当前主题的颜色（QColor 形式）"""
        rgba = THEME_COLORS[self._theme][key]
        return QColor(*rgba)

    def set_theme(self, theme: str):
        """切换主题 — 由 ThemeManager.theme_changed 信号触发"""
        if theme not in THEME_COLORS:
            return
        if theme == self._theme:
            return
        self._theme = theme
        # 重置当前色（bright 状态可能不同）
        if self._bright:
            self._bg_color = self._color("bright_bg")
            self._text_color = self._color("bright_text")
        else:
            self._bg_color = self._color("bg")
            self._text_color = self._color("text")
        self._shadow_color = self._color("shadow")
        self.theme_changed.emit()
        self.update()

    @property
    def theme(self) -> str:
        return self._theme

    def set_text(self, text: str, bright: bool = False, on_typing_done=None):
        """设置文字并开始打字机效果

        Args:
            text: 完整文本
            bright: 高亮模式（emotion == "happy" 时）
            on_typing_done: 打字完成回调
        """
        # 停掉上一次
        self._typewriter_timer.stop()
        self._is_typing = False

        self._full_text = text
        self._text = text
        self._typewriter_revealed = 0
        self._on_typing_done = on_typing_done
        self._bright = bright

        # 根据 bright 和 theme 取色
        if bright:
            self._bg_color = self._color("bright_bg")
            self._text_color = self._color("bright_text")
        else:
            self._bg_color = self._color("bg")
            self._text_color = self._color("text")
        self._shadow_color = self._color("shadow")

        # 立即计算气泡尺寸（用全文）
        self._update_size()
        self.show()
        self.raise_()

        # 淡入
        self.setWindowOpacity(0.0)
        self._fade_anim.stop()
        self._fade_anim.start()

        # 连续速度公式
        length = len(text)
        if length <= 8:
            speed = 42
        elif length >= 80:
            speed = 10
        else:
            speed = 42 - (length - 8) * (42 - 10) / (80 - 8)
        self._typewriter_speed = int(speed)

        if length > 0:
            self._is_typing = True
            self._typewriter_revealed = 1
            self._typewriter_timer.start(speed)
        else:
            self._typewriter_revealed = 0

        self.update()

    def _typewriter_tick(self):
        """打字机进度推进一步"""
        if self._typewriter_revealed < len(self._full_text):
            self._typewriter_revealed += 1
            self.update()

        if self._typewriter_revealed >= len(self._full_text):
            self._typewriter_timer.stop()
            self._is_typing = False
            if self._on_typing_done:
                self._on_typing_done()
                self._on_typing_done = None
            return

        ch = self._full_text[self._typewriter_revealed - 1]
        if ch in "。！？；：":
            self._typewriter_timer.start(int(self._typewriter_speed * 2.5))
        else:
            self._typewriter_timer.start(self._typewriter_speed)

    def set_typewriter_speed(self, ms_per_char: int):
        self._typewriter_speed = max(5, ms_per_char)

    def is_typing(self) -> bool:
        return self._is_typing

    def skip_typing(self):
        self._typewriter_timer.stop()
        self._typewriter_revealed = len(self._full_text)
        self._is_typing = False
        if self._on_typing_done:
            cb = self._on_typing_done
            self._on_typing_done = None
            cb()
        self.update()

    def hide_bubble(self):
        self._typewriter_timer.stop()
        self._fade_anim.stop()
        self._is_typing = False
        self.hide()

    def _start_flash(self):
        self._flash_count = 0
        self._flash_timer.start(300)

    def _flash_tick(self):
        self._flash_count += 1
        if self._flash_count > 12:
            self._flash_timer.stop()
            self._flash_count = 0
        self.update()

    def _update_size(self):
        fm = QFontMetrics(self._font)
        text_w = self._max_width - self._padding_h * 2 - 4
        rect = fm.boundingRect(
            QRect(0, 0, text_w, 1000),
            Qt.AlignLeft | Qt.TextWordWrap,
            self._text
        )
        bw = rect.width() + self._padding_h * 2 + 16
        bh = rect.height() + self._padding_v * 2 + 8
        self.setFixedSize(max(bw, 50), max(bh, 36))

    def paintEvent(self, event):
        if not self._full_text:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()
        r = 14
        tri_h = 8

        flashing = self._flash_timer.isActive()
        is_on = (self._flash_count % 2 == 0) if flashing else True

        bg = self._bg_color if is_on else QColor(*THEME_COLORS[self._theme]["bg"][:3], 160)
        tc = self._text_color if is_on else QColor(150, 150, 160)

        body_h = h - tri_h

        # 阴影层
        shadow_path = QPainterPath()
        shadow_path.addRoundedRect(2, 2, w - 12, body_h + 2, r, r)
        p.fillPath(shadow_path, self._shadow_color)

        # 气泡主体 + 三角
        bubble_path = QPainterPath()
        bubble_path.addRoundedRect(0, 0, w - 12, body_h, r, r)
        cx = (w - 12) // 2
        bubble_path.moveTo(cx - 7, body_h)
        bubble_path.lineTo(cx, body_h + tri_h)
        bubble_path.lineTo(cx + 7, body_h)
        bubble_path.closeSubpath()
        p.fillPath(bubble_path, bg)

        # 打字机文本
        p.setPen(tc)
        reveal = self._typewriter_revealed if self._typewriter_revealed > 0 else len(self._full_text)
        display = self._full_text[:reveal]
        if self._is_typing and reveal < len(self._full_text):
            if (self._flash_count % 6) < 3:
                display += "▎"

        text_rect = QRect(
            self._padding_h, self._padding_v,
            w - self._padding_h * 2 - 14,
            body_h - self._padding_v * 2
        )
        p.drawText(text_rect, Qt.AlignLeft | Qt.TextWordWrap, display)
        p.end()