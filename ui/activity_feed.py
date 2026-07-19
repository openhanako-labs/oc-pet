"""活动流组件 — 基于 P-04 模式卡

浮动窗口，渲染 ActivityEvent 列表。
P-04 模式卡四要素：操作者 + 动词 + 对象 + 时间戳。

数据源：core/perception.py 的 ActivityEvent。
- source=foreground → user（前台窗口）
- source=vision → agent（AI 视觉推断）
- 其他 → system

主题感知：颜色从 ThemeManager 拉取，跟随 light/dark。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea, QWidget,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter, QColor, QFont, QPainterPath, QFontMetrics, QPen

from ui.theme import get_default

# 避免循环导入（core.perception 也可能用 ui.theme）
# ActivityEvent 类型用 TYPE_CHECKING 即可，这里用 duck typing
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from core.perception import ActivityEvent


# 三类 actor 主题色（与 light.qss / dark.qss 对齐）
ACTOR_THEME_COLORS = {
    "light": {
        "user":    {"bg": "#2a201a", "line": "#c2410c", "dot": "#f4a261"},
        "agent":   {"bg": "#221a26", "line": "#9333ea", "dot": "#c084fc"},
        "system":  {"bg": "#2a2520", "line": "#4a3f37", "dot": "#a89580"},
    },
    "dark": {
        "user":    {"bg": "rgba(233,196,106,0.07)", "line": "#e9c46a", "dot": "#fde68a"},
        "agent":   {"bg": "rgba(125,211,252,0.07)", "line": "#38bdf8", "dot": "#7dd3fc"},
        "system":  {"bg": "rgba(100,116,139,0.08)", "line": "#64748b", "dot": "#94a3b8"},
    },
}


@dataclass
class FeedItem:
    """P-04 模式卡的四要素"""
    actor: str          # "user" / "agent" / "system"
    actor_name: str     # 显示名（如 "Chrome" / "瑞贝卡" / "MacroDroid"）
    verb: str           # 动词
    target: str         # 对象
    quote: str = ""     # 可选上下文
    timestamp: float = 0.0


def _qcolor(c) -> QColor:
    """统一接受 hex / rgba 字符串"""
    return QColor(c)


def _parse_color(c) -> tuple:
    """取 RGB 元组（用于 setBrush 等场景）"""
    qc = _qcolor(c) if isinstance(c, str) else c
    return (qc.red(), qc.green(), qc.blue(), qc.alpha())


def activity_to_feed(events) -> List[FeedItem]:
    """ActivityEvent 列表 → FeedItem 列表

    映射规则：
    - source=foreground → actor=user（用户前台窗口）
    - source=vision     → actor=agent（AI 推断）
    - 其他               → actor=system
    """
    items = []
    for ev in events:
        if ev.source == "foreground":
            actor = "user"
        elif ev.source == "vision":
            actor = "agent"
        else:
            actor = "system"

        # confidence 显示为 quote
        quote = ""
        if ev.confidence and ev.confidence < 0.8:
            quote = f"置信度 {ev.confidence:.0%}"

        items.append(FeedItem(
            actor=actor,
            actor_name=ev.app or "未知应用",
            verb=ev.activity or "进行中",
            target=ev.summary or ev.category,
            quote=quote,
            timestamp=ev.start_time or 0.0,
        ))
    # 时间倒序
    items.sort(key=lambda x: x.timestamp, reverse=True)
    return items


class _FeedRow(QFrame):
    """单条活动流条目"""

    def __init__(self, item: FeedItem, theme: str = "light", parent=None):
        super().__init__(parent)
        self._item = item
        self._theme = theme
        self.setMinimumHeight(56)
        self.setObjectName("feedRow")

    def set_theme(self, theme: str):
        self._theme = theme
        self.update()

    def _c(self, key: str) -> str:
        return ACTOR_THEME_COLORS[self._theme][self._item.actor][key]

    def _text_color(self) -> QColor:
        return QColor("#f5e9d3") if self._theme == "light" else QColor("#e8ecf5")

    def _mute_color(self) -> QColor:
        return QColor("#8a7a66") if self._theme == "light" else QColor("#6b7591")

    def _format_time(self, ts: float) -> str:
        if not ts:
            return ""
        try:
            dt = datetime.fromtimestamp(ts)
            now = datetime.now()
            delta = now - dt
            if delta.total_seconds() < 60:
                return "刚刚"
            elif delta.total_seconds() < 3600:
                return f"{int(delta.total_seconds() // 60)} min ago"
            elif delta.days == 0:
                return dt.strftime("%H:%M:%S")
            elif delta.days == 1:
                return "昨天"
            else:
                return dt.strftime("%m-%d %H:%M")
        except Exception:
            return ""

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        h = self.height()

        # 行背景渐变（左侧 actor 色 → 右侧透明）
        bg_color = _qcolor(self._c("bg"))
        grad = QPainterPath()
        grad.addRect(0, 0, int(w * 0.4), h)
        p.fillPath(grad, bg_color)

        # 左侧时间轴圆点 + 外环
        cx, cy = 24, h // 2
        ring_color = _qcolor(self._c("line"))
        p.setPen(QPen(ring_color, 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(cx - 18, cy - 18, 36, 36)
        # 内点
        p.setPen(Qt.NoPen)
        p.setBrush(_qcolor(self._c("dot")))
        p.drawEllipse(cx - 4, cy - 4, 8, 8)

        # actor name（带 actor 色）
        font = QFont("Microsoft YaHei UI", 10, QFont.DemiBold)
        p.setFont(font)
        p.setPen(ring_color)
        actor_text = self._item.actor_name
        if self._item.actor == "user":
            actor_text = f"@{self._item.actor_name}"
        p.drawText(48, 18, actor_text)

        # verb + target
        font.setWeight(QFont.Normal)
        p.setFont(font)
        text = f"{self._item.verb} → {self._item.target}"
        fm = QFontMetrics(font)
        elided = fm.elidedText(text, Qt.ElideRight, w - 48 - 100)
        p.setPen(self._text_color())
        p.drawText(48, 36, elided)

        # quote（小字、左侧 muted 色，可选）
        if self._item.quote:
            font.setPointSize(8)
            p.setFont(font)
            p.setPen(self._mute_color())
            p.drawText(48, 50, self._item.quote)

        # 时间戳（右上）
        ts_text = self._format_time(self._item.timestamp)
        if ts_text:
            font.setPointSize(8)
            p.setFont(font)
            p.setPen(self._mute_color())
            p.drawText(w - 100, 18, ts_text)

        p.end()


class ActivityFeed(QDialog):
    """活动流浮动窗口

    用法：
        feed = ActivityFeed(events, parent=None)
        feed.show()

    主题切换：自动跟随 ThemeManager（light/dark）
    """

    def __init__(self, events=None, parent=None):
        super().__init__(parent)
        self._events = events or []
        self._theme = "light"
        self._rows: List[_FeedRow] = []

        self.setWindowTitle("活动流")
        # FramelessWindowHint：去掉 Qt 默认 title bar，让 #feedHeader 作为唯一标题栏
        self.setWindowFlags(
            Qt.Dialog | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint
        )
        self.setMinimumSize(420, 320)
        self.resize(480, 480)

        # 主题感知
        mgr = get_default()
        if mgr is not None:
            self._theme = mgr.current
            mgr.theme_changed.connect(self._on_theme_changed)

        # 布局
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 标题栏
        header = QFrame()
        header.setObjectName("feedHeader")
        header.setFixedHeight(44)
        header_lay = QHBoxLayout(header)
        header_lay.setContentsMargins(16, 0, 8, 0)

        title = QLabel("活动流")
        title.setObjectName("feedTitle")
        font = QFont("Microsoft YaHei UI", 11, QFont.DemiBold)
        title.setFont(font)
        header_lay.addWidget(title)

        header_lay.addStretch()

        self._count_label = QLabel(f"· {len(self._events)} 条")
        self._count_label.setObjectName("feedCount")
        header_lay.addWidget(self._count_label)

        close_btn = QPushButton("×")
        close_btn.setObjectName("feedClose")
        close_btn.setFixedSize(28, 28)
        close_btn.clicked.connect(self.close)
        header_lay.addWidget(close_btn)

        root.addWidget(header)

        # 滚动列表
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setObjectName("feedScroll")

        self._list_widget = QWidget()
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(0, 8, 0, 8)
        self._list_layout.setSpacing(0)
        self._list_layout.addStretch()

        scroll.setWidget(self._list_widget)
        root.addWidget(scroll, 1)

        self._populate()

    def set_events(self, events):
        """更新事件列表（外部调用）"""
        self._events = events
        self._count_label.setText(f"· {len(self._events)} 条")
        self._populate()

    def _populate(self):
        """填充列表"""
        # 清空已有 rows（保留 stretch）
        for row in self._rows:
            row.deleteLater()
        self._rows.clear()

        items = activity_to_feed(self._events)
        for item in items:
            row = _FeedRow(item, self._theme, self._list_widget)
            self._rows.append(row)
            self._list_layout.insertWidget(self._list_layout.count() - 1, row)

    def _on_theme_changed(self, theme: str):
        self._theme = theme
        for row in self._rows:
            row.set_theme(theme)
        self.update()