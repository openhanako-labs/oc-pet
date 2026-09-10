"""首屏引导 — 第一次打开桌宠时的轻量交互提示。

设计目标：
  - 不挡桌宠主体：卡片浮于桌宠右侧，半透明玻璃质感，可一键关闭。
  - 不唠叨：只讲 3 条核心交互（拖拽 / 抚摸 / 双击），记住已看过就不再现。
  - 跟随主题：玻璃卡样式来自全局 #glassCard（design_system），随主题切换自动更新。
  - 零打断：淡入淡出，关闭后才写入 config，不影响桌宠运行。

用法：
    ov = OnboardingOverlay(pet_window, on_close=mark_done)
    ov.show_relative()   # 相对桌宠定位并淡入
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, QEvent
from PySide6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QFrame,
    QGraphicsOpacityEffect
)
import logging
logger = logging.getLogger(__name__)



# 引导条目：手势提示 + 说明（沿用项目已有的 emoji 手势风格，非装饰性 icon）
TIPS = [
    ("🖱️  拖一拖", "按住桌宠可以拖动它，松手会轻轻弹一下"),
    ("✋  摸一摸", "按住不放它会舒服地眯眼，心情涨得飞快"),
    ("👆  双击头", "快速戳两下头顶，它会开心地摇一摇"),
]


class OnboardingOverlay(QWidget):
    """桌宠右侧的轻量引导卡片"""

    def __init__(self, parent=None, on_close=None):
        super().__init__(parent)
        self._on_close = on_close
        self._opacity = None

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # 不抢焦点：引导只是提示，别把用户正在打的字打断
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # 关键：必须是独立顶层窗口（Qt.Window），不能只给 Qt.SubWindow ——
        # 后者不含 Window 位，widget 会沦为被桌宠窗口裁剪的子部件：桌宠一缩
        # （滚轮缩放让窗口变窄），卡片就被裁到看不见（2026-09-10 用户实测，
        # 离屏探针证实 isWindow()=False、卡片落在父几何之外）。
        # 顶层窗口的 move() 走屏幕坐标，正好与 show_relative 的屏幕坐标计算一致。
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.Window | Qt.WindowStaysOnTopHint
        )
        self.setFixedWidth(264)

        # 玻璃卡（吃全局 #glassCard 主题样式）
        self._card = QFrame(self)
        self._card.setObjectName("glassCard")
        try:
            from ui.theme.design_system import apply_glass_shadow
            mgr = None
            try:
                from ui.theme import get_default
                mgr = get_default()
            except Exception:
                logger.debug("onboarding: 非致命异常(已静默吞掉)", exc_info=True)
            apply_glass_shadow(self._card, theme=(mgr.current if mgr else "dark"))
        except Exception:
            logger.debug("onboarding: 非致命异常(已静默吞掉)", exc_info=True)

        layout = QVBoxLayout(self._card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        # 标题
        title = QLabel("初次见面～")
        title.setStyleSheet("font-size: 15px; font-weight: 700; color: rgb(230, 230, 245);"
                            if (mgr and mgr.current == "dark") else
                            "font-size: 15px; font-weight: 700; color: rgb(40, 40, 50);")
        layout.addWidget(title)

        subtitle = QLabel("几个小动作就能让它活过来：")
        subtitle.setStyleSheet("font-size: 11px; color: rgb(150, 155, 180);"
                               if (mgr and mgr.current == "dark") else
                               "font-size: 11px; color: rgb(120, 120, 135);")
        layout.addWidget(subtitle)

        # 提示条目
        for gesture, desc in TIPS:
            row = QHBoxLayout()
            row.setSpacing(10)
            g = QLabel(gesture)
            g.setFixedWidth(74)
            g.setStyleSheet("font-size: 13px; color: rgb(180, 200, 255); font-weight: 600;"
                            if (mgr and mgr.current == "dark") else
                            "font-size: 13px; color: rgb(80, 110, 200); font-weight: 600;")
            d = QLabel(desc)
            d.setWordWrap(True)
            d.setStyleSheet("font-size: 11px; color: rgb(190, 195, 215);"
                            if (mgr and mgr.current == "dark") else
                            "font-size: 11px; color: rgb(90, 95, 110);")
            row.addWidget(g)
            row.addWidget(d, 1)
            layout.addLayout(row)

        layout.addStretch()

        # 关闭按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        got_it = QPushButton("知道啦")
        got_it.setObjectName("save")
        got_it.setFixedWidth(96)
        got_it.clicked.connect(self._dismiss)
        btn_row.addWidget(got_it)
        layout.addLayout(btn_row)

        # 外层透明容器布局（让卡片按内容自适应高度）
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._card)

        self._card.adjustSize()
        self.resize(self._card.width(), self._card.height())

        # 跟随桌宠：滚轮缩放/拖动会改变桌宠窗口几何，卡片需实时重新贴边，
        # 否则停在旧位置——缩小后看起来就像"消失了"。
        if parent is not None:
            parent.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() in (QEvent.Move, QEvent.Resize):
            if self.isVisible():
                self._place()
        return super().eventFilter(obj, event)

    def _place(self):
        """按桌宠窗口当前几何贴边定位；超出屏幕自动翻到左侧并夹回屏内。"""
        parent = self.parentWidget()
        if parent is None:
            return
        pg = parent.geometry()
        x = pg.x() + pg.width() + 18
        y = pg.y() + max(0, (pg.height() - self.height()) // 2)
        # 防止超出屏幕：右侧放不下翻到左侧；Y 夹进可视区
        screen = parent.screen() if hasattr(parent, "screen") else None
        if screen is not None:
            sr = screen.availableGeometry()
            if x + self.width() > sr.x() + sr.width():
                x = pg.x() - self.width() - 18
            x = max(sr.x(), min(x, sr.x() + sr.width() - self.width()))
            y = max(sr.y(), min(y, sr.y() + sr.height() - self.height()))
        self.move(x, y)

    def show_relative(self):
        """相对父窗体（桌宠）右侧居中定位并淡入"""
        self._place()
        self._fade_in()
        self.show()
        self.raise_()

    def _fade_in(self):
        try:
            eff = QGraphicsOpacityEffect(self._card)
            self._card.setGraphicsEffect(eff)
            # 存 self 引用，防止动画运行期间被 GC 提前回收
            self._fade_in_anim = QPropertyAnimation(eff, b"opacity", self)
            self._fade_in_anim.setDuration(280)
            self._fade_in_anim.setEasingCurve(QEasingCurve.OutCubic)
            self._fade_in_anim.setStartValue(0.0)
            self._fade_in_anim.setEndValue(1.0)
            self._fade_in_anim.start()
        except Exception:
            logger.debug("onboarding: 非致命异常(已静默吞掉)", exc_info=True)

    def _dismiss(self):
        # 先淡出再关闭
        try:
            eff = QGraphicsOpacityEffect(self._card)
            self._card.setGraphicsEffect(eff)
            # 存 self 引用，防止动画运行期间被 GC 提前回收
            self._dismiss_anim = QPropertyAnimation(eff, b"opacity", self)
            self._dismiss_anim.setDuration(200)
            self._dismiss_anim.setEasingCurve(QEasingCurve.InCubic)
            self._dismiss_anim.setStartValue(1.0)
            self._dismiss_anim.setEndValue(0.0)
            self._dismiss_anim.finished.connect(self._finish_dismiss)
            self._dismiss_anim.start()
        except Exception:
            self._finish_dismiss()

    def _finish_dismiss(self):
        try:
            if self._on_close is not None:
                self._on_close()
        except Exception:
            logger.debug("onboarding: 非致命异常(已静默吞掉)", exc_info=True)
        self.hide()
        self.deleteLater()
