"""Minecraft 实时画面窗（需求② · P3）

独立 PySide6 工具窗，风格对齐 ``MiniGameWindow``（neko_palette），用于
「看 bot 玩 MC」体验：把 mc_bridge 经 WS 推来的截图帧渲染出来，并显示任务状态。

线程安全：截图帧来自 mc_bridge 的 WS 后台线程。引擎侧把 ``bridge.on_frame``
接到本窗的 ``frame_received`` 信号（Qt 跨线程 emit 自动排队），
槽 ``_on_frame`` 跑在 Qt 主线程，安全更新 UI。

画面数据来自 ``mc_bridge.WsTransport._decode_screenshot``，统一为
``{"bytes": <原始图像字节>, "meta": {...}}``。
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

try:
    from ui.theme.neko_palette import palette
except Exception:  # noqa: BLE001
    palette = None  # 主题缺失时退化为纯色

logger = logging.getLogger(__name__)

_WINDOW_QSS = """
MCStreamWindow {{
    background: {bg};
    border: 1px solid {border};
    border-radius: 16px;
}}
MCStreamWindow QLabel#mcTitle {{
    color: {title};
    font-size: 14px;
    font-weight: 700;
}}
MCStreamWindow QLabel#mcStatus {{
    color: {meta};
    font-size: 12px;
}}
MCStreamWindow QLabel#mcScreen {{
    background: rgba(0,0,0,0.35);
    border-radius: 10px;
}}
MCStreamWindow QPushButton#mcClose {{
    border: none;
    border-radius: 12px;
    background: rgba(131,148,175,0.12);
    color: {meta};
    font-size: 12px;
}}
MCStreamWindow QPushButton#mcClose:hover {{
    background: rgba(131,148,175,0.24);
}}
"""


class MCStreamWindow(QWidget):
    """MC bot 实时画面窗（无边框工具窗）。"""

    frame_received = Signal(dict)   # {"bytes":..,"meta":..} —— 跨线程安全投递
    status_received = Signal(str)   # 任务状态文本 —— 跨线程安全投递
    close_requested = Signal()

    def __init__(self, theme: str = "light", parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme if theme in ("light", "dark") else "light"
        self._last_meta: dict = {}

        self.setObjectName("mcStreamWindow")
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setFixedWidth(340)

        self.frame_received.connect(self._on_frame)
        self.status_received.connect(self.set_status)
        self._build_ui()
        self._apply_qss()

    # ── UI ──
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 14)
        root.setSpacing(8)

        head = QHBoxLayout()
        self._title_label = QLabel("Minecraft · 看 bot 玩", self)
        self._title_label.setObjectName("mcTitle")
        self._close_button = self._make_close()
        head.addWidget(self._title_label)
        head.addStretch(1)
        head.addWidget(self._close_button)
        root.addLayout(head)

        self._screen_label = QLabel(self)
        self._screen_label.setObjectName("mcScreen")
        self._screen_label.setMinimumHeight(180)
        self._screen_label.setAlignment(Qt.AlignCenter)
        self._screen_label.setText("等待 bot 画面…")
        root.addWidget(self._screen_label)

        self._status_label = QLabel("", self)
        self._status_label.setObjectName("mcStatus")
        self._status_label.setWordWrap(True)
        root.addWidget(self._status_label)

    def _make_close(self) -> QLabel:
        from PySide6.QtWidgets import QPushButton
        btn = QPushButton("✕", self)
        btn.setObjectName("mcClose")
        btn.setFixedWidth(24)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self.close_requested.emit)
        return btn

    def _apply_qss(self) -> None:
        if palette is None:
            self.setStyleSheet("MCStreamWindow{background:#fbfdff;border:1px solid #d7e0ee;border-radius:16px;}")
            return
        try:
            p = palette(self._theme)
            pd = palette("dark")
            bg = p["card_bg"] if self._theme == "light" else pd["card_bg"]
            border = "rgba(53,72,104,0.10)" if self._theme == "light" else "rgba(148,166,196,0.16)"
            title = p["card_text"] if self._theme == "light" else pd["card_text"]
            meta = p["card_meta"] if self._theme == "light" else pd["card_meta"]
            self.setStyleSheet(_WINDOW_QSS.format(bg=bg, border=border, title=title, meta=meta))
        except Exception as e:  # noqa: BLE001
            logger.warning("[mc_stream_window] qss apply failed: %s", e)

    # ── 数据入口 ──
    @property
    def last_meta(self) -> dict:
        return self._last_meta

    def _on_frame(self, payload: dict) -> None:
        """主线程槽：把截图字节渲染到画面区。"""
        self._last_meta = payload.get("meta", {}) or {}
        if not self.isVisible():
            self.show()
        raw = payload.get("bytes")
        if not raw:
            return
        pix = QPixmap()
        if not pix.loadFromData(QByteArray(raw)):
            self._screen_label.setText("画面解码失败")
            return
        # 等比缩放进画面区宽度
        max_w = self.width() - 32
        scaled = pix.scaledToWidth(min(max_w, pix.width()), Qt.SmoothTransformation) if pix.width() > max_w else pix
        self._screen_label.setPixmap(scaled)

    def set_status(self, text: str) -> None:
        self._status_label.setText(text or "")
