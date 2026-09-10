"""聊天面板 UI — PySide6 原生重绘 N.E.K.O. 设计语言（P0-6）

参考 N.E.K.O. React ``MessageList.tsx`` / ``MessageBubble.tsx`` /
``styles.css`` 重绘：
- 消息列表（user/assistant/system 三类气泡、头像、时间戳）
- 思考点动画（ChatThinkingDots 尾部行）
- 自动滚动到最新（用户上翻时保持位置，不强制拉回）
- 细滚动条（QSS 浮动细条，对齐 .message-list-scroll-thumb）
- light/dark 主题切换（data-theme 动态属性 + neko.qss）
- 专注辉光层（FocusOverlay 叠在面板上，P0-7）

纯 QWidget + QSS + QPropertyAnimation，无 QWebEngine。
入口/接线（打开面板、新消息注入、focus 状态联动）由 T05 在 pet 层完成。
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from ui.chat_message import ChatMessage, ROLE_USER, ROLE_ASSISTANT, ROLE_SYSTEM
from ui.focus_overlay import FocusOverlay
from ui.interaction_card import InteractionCard
from ui.theme.neko_palette import palette

logger = logging.getLogger(__name__)

NEKO_QSS_PATH = Path(__file__).parent / "theme" / "neko.qss"

_SCROLL_BOTTOM_TOLERANCE = 24  # 距底部 24px 内视为"贴底"

# P2-1: 流式打字机参数（可配）
TYPING_CHAR_MS = 30        # 默认每字间隔（ms）
TYPING_PUNCT_MS = 140      # 标点后停顿（ms，比普通字长）
TYPING_PUNCT_CHARS = "。！？；：,.!?;:…—~～\n"
# 长文本加速：超过阈值每 tick 追加更多字，控制总时长 + 减少重绘次数
_TYPING_LONG_THRESHOLD_1 = 700
_TYPING_LONG_THRESHOLD_2 = 1500


class ChatPanel(QWidget):
    """聊天面板：消息列表 + 自动滚动 + 输入行 + 专注辉光。

    信号：
        message_submitted(str) — 输入行回车/发送按钮
        close_requested()      — 右上角关闭按钮
        card_action(str, str)  — 互动卡片动作（kind, action_id，P2 互动层）
    """

    message_submitted = Signal(str)
    close_requested = Signal()
    card_action = Signal(str, str)

    def __init__(self, theme: str = "light", agent_name: str = "",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme if theme in ("light", "dark") else "light"
        self._agent_name = agent_name or ""
        self._messages: list[ChatMessage] = []
        self._cards: list[InteractionCard] = []
        self._thinking_msg: ChatMessage | None = None
        self._stick_to_bottom: bool = True
        self._theme_mgr = None

        # P2-1: 流式打字机状态（同一时刻至多一条 assistant 消息在流式显示）
        self._stream_msg: ChatMessage | None = None
        self._stream_full: str = ""
        self._stream_pos: int = 0
        self._stream_interval: int = TYPING_CHAR_MS
        self._stream_chars_per_tick: int = 1
        self._typewriter_timer = QTimer(self)
        self._typewriter_timer.timeout.connect(self._stream_tick)

        self.setObjectName("chatPanel")
        self.setProperty("data-theme", self._theme)

        self._build_ui()
        self._apply_qss()
        self._connect_theme_manager()

    # ── UI 构建 ──

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 顶栏
        header = QFrame(self)
        header.setObjectName("chatHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 8, 14, 8)
        header_layout.setSpacing(8)
        self._title_label = QLabel(self._title_text(), header)
        self._title_label.setObjectName("chatTitle")
        self._theme_button = QPushButton("🌓 主题", header)
        self._theme_button.setObjectName("headerButton")
        self._theme_button.setCursor(Qt.PointingHandCursor)
        self._theme_button.clicked.connect(self._toggle_theme)
        self._close_button = QPushButton("✕", header)
        self._close_button.setObjectName("headerButton")
        self._close_button.setFixedWidth(30)
        self._close_button.setCursor(Qt.PointingHandCursor)
        self._close_button.clicked.connect(self.close_requested.emit)
        header_layout.addWidget(self._title_label)
        header_layout.addStretch(1)
        header_layout.addWidget(self._theme_button)
        header_layout.addWidget(self._close_button)
        root.addWidget(header)

        # 消息列表
        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("messageScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        container = QWidget(self._scroll)
        container.setObjectName("messageListContainer")
        self._list_layout = QVBoxLayout(container)
        self._list_layout.setContentsMargins(12, 12, 12, 12)
        self._list_layout.setSpacing(12)
        self._list_layout.addStretch(1)  # 消息从顶部排布，新消息追加到底部前
        self._scroll.setWidget(container)

        # 空状态（初始提示）
        self._empty_label = QLabel("还没有消息，和我说说话吧～", container)
        self._empty_label.setObjectName("messageEmpty")
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setVisible(False)

        root.addWidget(self._scroll, stretch=1)

        # 输入行
        composer = QFrame(self)
        composer.setObjectName("chatComposer")
        composer_layout = QHBoxLayout(composer)
        composer_layout.setContentsMargins(12, 8, 12, 10)
        composer_layout.setSpacing(8)
        self._input = QLineEdit(composer)
        self._input.setObjectName("chatInput")
        self._input.setPlaceholderText("说点什么…")
        self._input.returnPressed.connect(self._submit)
        self._send_button = QPushButton("发送", composer)
        self._send_button.setObjectName("sendButton")
        self._send_button.setCursor(Qt.PointingHandCursor)
        self._send_button.clicked.connect(self._submit)
        composer_layout.addWidget(self._input, stretch=1)
        composer_layout.addWidget(self._send_button)
        root.addWidget(composer)

        # 专注辉光层（叠在面板上，透明、不抢鼠标）
        self.focus_overlay = FocusOverlay(self, theme=self._theme)

        # 滚动跟踪
        self._scroll.verticalScrollBar().valueChanged.connect(self._on_scroll_value_changed)

    def _title_text(self) -> str:
        return f"与 {self._agent_name} 聊天" if self._agent_name else "聊天"

    def _apply_qss(self) -> None:
        """应用 neko.qss 到面板子树（只影响本面板，不污染全局）。"""
        try:
            if NEKO_QSS_PATH.exists():
                self.setStyleSheet(NEKO_QSS_PATH.read_text(encoding="utf-8"))
            else:
                logger.warning("neko.qss 不存在，聊天面板使用默认样式")
        except Exception as exc:
            logger.warning("neko.qss 应用失败: %s", exc)

    def _connect_theme_manager(self) -> None:
        """订阅全局 ThemeManager（若已初始化；否则等 T05 注入）。"""
        try:
            from ui.theme import get_default
            mgr = get_default()
            if mgr is not None:
                self._theme_mgr = mgr
                self.set_theme(mgr.current)
                mgr.theme_changed.connect(self.set_theme)
        except Exception as exc:
            logger.debug("ThemeManager 未初始化，聊天面板保持默认主题: %s", exc)

    # ── 消息 API ──

    def add_message(self, role: str, text: str, author: str = "",
                    timestamp: float | None = None, thinking: bool = False,
                    animate: bool = True) -> ChatMessage:
        """追加一条消息（自动滚动到最新；贴底时才强制滚动）。

        ``animate=False``：跳过入场高度动画（流式打字机消息用，避免
        先收缩到空文本高度再被文本撑开导致跳动）。
        """
        msg = ChatMessage(role=role, text=text, author=author,
                          timestamp=timestamp, theme=self._theme,
                          thinking=thinking, parent=self)
        self._messages.append(msg)
        # 插到底部 stretch 之前
        self._list_layout.insertWidget(self._list_layout.count() - 1, msg)
        if animate:
            msg.play_enter_animation()
        self._update_empty_state()
        QTimer.singleShot(0, self._scroll_to_bottom)
        return msg

    def append_user(self, text: str, timestamp: float | None = None) -> ChatMessage:
        return self.add_message(ROLE_USER, text, timestamp=timestamp)

    def append_assistant(self, text: str, author: str = "",
                         timestamp: float | None = None) -> ChatMessage:
        return self.add_message(ROLE_ASSISTANT, text, author=author or self._agent_name,
                                timestamp=timestamp)

    def append_system(self, text: str, timestamp: float | None = None) -> ChatMessage:
        return self.add_message(ROLE_SYSTEM, text, timestamp=timestamp)

    # ── 互动卡片（P2 互动层）──

    def add_card(self, kind: str, title: str, body: str,
                 actions: list[tuple[str, str]] | None = None) -> InteractionCard:
        """插入一张可点互动卡片（游戏邀请/音乐推荐/休息建议）。

        Args:
            kind: 卡片类型（game/music/rest），用于样式与 action 透传。
            title: 卡片标题。
            body: 卡片正文。
            actions: [(按钮文字, action_id), ...]；点击经 ``card_action`` 发出。

        Returns:
            创建的 InteractionCard（调用方可持有引用）。
        """
        card = InteractionCard(kind=kind, title=title, body=body,
                               actions=actions, theme=self._theme, parent=self)
        card.action_clicked.connect(lambda k, a: self.card_action.emit(k, a))
        card.dismiss_requested.connect(self._remove_card)
        self._cards.append(card)
        self._list_layout.insertWidget(self._list_layout.count() - 1, card)
        card.setMaximumWidth(340)
        self._update_empty_state()
        QTimer.singleShot(0, self._scroll_to_bottom)
        return card

    def _remove_card(self, kind: str) -> None:
        """移除一张已关闭的卡片（按 kind 取最近一张）。"""
        for card in reversed(self._cards):
            if card.kind == kind or not kind:
                self._cards.remove(card)
                self._list_layout.removeWidget(card)
                card.deleteLater()
                self._update_empty_state()
                break

    @property
    def card_count(self) -> int:
        return len(self._cards)

    # ── P2-1 流式打字机 ──

    def start_assistant_stream(self, text: str, author: str = "",
                               timestamp: float | None = None,
                               char_interval_ms: int = TYPING_CHAR_MS) -> ChatMessage:
        """以打字机效果流式显示一条助手消息（N.E.K.O. 流式渲染的 PySide6 原生实现）。

        - 文本逐字追加：默认 ~30ms/字，标点后停顿更长（可配）。
        - 点击气泡立即显示全文（跳过），见 ChatMessage.typewriter_clicked。
        - 与思考点衔接：调用前应先 set_thinking(False)；本方法内部也会
          关闭思考点，保证"思考点显示期间无文本，开始输出文本时思考点消失"。
        - 同一时刻只允许一条流式消息；若上一条仍在流式，先立刻收尾。
        - 长文本自动加速（每 tick 多字），控制总时长与重绘次数。

        Args:
            text: 完整回复文本
            author: 助手名（缺省用面板 agent_name）
            timestamp: 时间戳（None 自动）
            char_interval_ms: 每字间隔 ms（>=5）

        Returns:
            新建的 ChatMessage（可监听 typewriter_clicked / 读取 text）。
        """
        text = text or ""
        # 新流式开始前，把仍在进行的旧流式立刻收尾（避免两个定时器交错）
        if self._stream_msg is not None:
            self.skip_stream()
        # 思考点衔接：开始输出文本时思考点消失
        self.set_thinking(False)

        msg = self.add_message(ROLE_ASSISTANT, "", author=author or self._agent_name,
                               timestamp=timestamp, animate=False)
        msg.typewriter_clicked.connect(self.skip_stream)

        interval = max(5, int(char_interval_ms))
        length = len(text)
        if length > _TYPING_LONG_THRESHOLD_2:
            cpt = 3
        elif length > _TYPING_LONG_THRESHOLD_1:
            cpt = 2
        else:
            cpt = 1

        self._stream_msg = msg
        self._stream_full = text
        self._stream_pos = 0
        self._stream_interval = interval
        self._stream_chars_per_tick = cpt

        if not text:
            self._finish_stream()
            return msg

        msg.set_typewriter_active(True)
        # 首个 tick 立即推进一步（不等满一个间隔，观感更跟手）
        QTimer.singleShot(0, self._stream_tick)
        return msg

    def _stream_tick(self) -> None:
        """打字机推进：追加若干字符 → 更新气泡文本 → 安排下一次。"""
        if self._stream_msg is None or self._stream_pos >= len(self._stream_full):
            self._finish_stream()
            return
        step = self._stream_chars_per_tick
        # 若下一字符是标点，本次只追加 1 字（保证标点停顿可感知）
        if step > 1 and self._stream_pos < len(self._stream_full) \
                and self._stream_full[self._stream_pos] in TYPING_PUNCT_CHARS:
            step = 1
        self._stream_pos = min(len(self._stream_full), self._stream_pos + step)
        try:
            self._stream_msg.set_text(self._stream_full[:self._stream_pos])
        except Exception:
            self._finish_stream()
            return
        # 流式期间保持贴底（尊重用户上翻）
        self.scroll_to_bottom()
        if self._stream_pos >= len(self._stream_full):
            self._finish_stream()
            return
        last_ch = self._stream_full[self._stream_pos - 1]
        self._typewriter_timer.start(self._next_stream_delay(last_ch))

    def _next_stream_delay(self, last_char: str) -> int:
        """计算下一次 tick 的间隔：标点/换行后停顿更长。"""
        if last_char in TYPING_PUNCT_CHARS:
            return max(self._stream_interval, TYPING_PUNCT_MS)
        return self._stream_interval

    def skip_stream(self) -> None:
        """跳过流式：立即显示全文并停止定时器（点击气泡触发）。"""
        if self._stream_msg is None:
            return
        msg = self._stream_msg
        full = self._stream_full if self._stream_full else msg.text
        self._stream_full = full
        self._stream_pos = len(full)
        try:
            msg.set_text(full)
        except Exception:
            logger.debug("chat_panel: 非致命异常(已静默吞掉)", exc_info=True)
        self._finish_stream()

    def _finish_stream(self) -> None:
        """流式结束/被跳过：停表、复位状态、恢复光标。"""
        self._typewriter_timer.stop()
        msg = self._stream_msg
        self._stream_msg = None
        self._stream_full = ""
        self._stream_pos = 0
        if msg is not None:
            try:
                msg.set_typewriter_active(False)
            except Exception:
                logger.debug("chat_panel: 非致命异常(已静默吞掉)", exc_info=True)
        self.scroll_to_bottom()

    def set_typewriter_speed(self, ms_per_char: int) -> None:
        """配置流式打字机每字间隔（ms，>=5）。"""
        self._stream_interval = max(5, int(ms_per_char))

    @property
    def is_streaming(self) -> bool:
        """是否正在流式输出。"""
        return self._stream_msg is not None

    def set_thinking(self, thinking: bool, author: str = "") -> None:
        """开启/关闭尾部"思考点"行（assistant 气泡内三点动画）。"""
        if thinking:
            if self._thinking_msg is None:
                self._thinking_msg = self.add_message(
                    ROLE_ASSISTANT, "", author=author or self._agent_name, thinking=True,
                )
        else:
            if self._thinking_msg is not None:
                msg = self._thinking_msg
                self._thinking_msg = None
                self._list_layout.removeWidget(msg)
                self._messages.remove(msg)
                msg.deleteLater()
                self._update_empty_state()

    def clear(self) -> None:
        """清空全部消息/卡片（流式/思考点一并停止）。"""
        self.skip_stream()
        self.set_thinking(False)
        for msg in list(self._messages):
            self._list_layout.removeWidget(msg)
            self._messages.remove(msg)
            msg.deleteLater()
        for card in list(self._cards):
            self._list_layout.removeWidget(card)
            self._cards.remove(card)
            card.deleteLater()
        self._update_empty_state()

    @property
    def message_count(self) -> int:
        return len(self._messages)

    @property
    def messages(self) -> list[ChatMessage]:
        return list(self._messages)

    def _update_empty_state(self) -> None:
        has = len(self._messages) > 0 or len(self._cards) > 0
        if hasattr(self, "_empty_label"):
            self._empty_label.setVisible(not has)

    # ── 滚动 ──

    def _on_scroll_value_changed(self, value: int) -> None:
        sb = self._scroll.verticalScrollBar()
        self._stick_to_bottom = (sb.maximum() - value) <= _SCROLL_BOTTOM_TOLERANCE

    def scroll_to_bottom(self, force: bool = False) -> None:
        """滚动到最新消息。``force=True`` 忽略用户上翻状态。"""
        if force or self._stick_to_bottom:
            self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        try:
            sb = self._scroll.verticalScrollBar()
            sb.setValue(sb.maximum())
        except Exception:
            logger.debug("chat_panel: 非致命异常(已静默吞掉)", exc_info=True)

    @property
    def stick_to_bottom(self) -> bool:
        return self._stick_to_bottom

    # ── 输入 ──

    def _submit(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        self._input.clear()
        self.message_submitted.emit(text)

    def set_input_text(self, text: str) -> None:
        self._input.setText(text)
        self._input.setFocus()

    def input_widget(self) -> QLineEdit:
        return self._input

    # ── 主题 ──

    def set_theme(self, theme: str) -> None:
        """切换主题（同步所有子消息/辉光/思考点）。"""
        if theme not in ("light", "dark"):
            return
        if theme == self._theme:
            return
        self._theme = theme
        self.setProperty("data-theme", theme)
        for msg in self._messages:
            msg.set_theme(theme)
        for card in self._cards:
            card.set_theme(theme)
        self.focus_overlay.set_theme(theme)
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.update()

    def _toggle_theme(self) -> None:
        self.set_theme("dark" if self._theme == "light" else "light")

    @property
    def theme(self) -> str:
        return self._theme

    def set_agent_name(self, name: str) -> None:
        self._agent_name = name or ""
        self._title_label.setText(self._title_text())

    # ── 专注辉光（P0-7 接线）──

    def set_focus_active(self, active: bool, strength: float | None = None) -> None:
        """专注模式视觉开关：委托给 FocusOverlay（辉光呼吸/消失）。"""
        self.focus_overlay.set_active(active, strength)

    # ── 布局 ──

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        self.focus_overlay.setGeometry(self.rect())
        # 输入行聚焦保持；resize 后重新贴底
        self._scroll_to_bottom()
