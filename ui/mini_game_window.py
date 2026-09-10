"""小游戏窗口 — 猜数字 / 石头剪刀布 / 快速反应（P2-3）

PySide6 原生窗口，风格对齐 neko_palette：
- ``MiniGameWindow`` 基类：无边框工具窗 + 标题栏 + 关闭 + 结果行
- ``GuessNumberWindow``：1~100 猜数字，提示大了/小了
- ``RockPaperScissorsWindow``：✊✋✌️ 三局两胜
- ``ReactionWindow``：变绿瞬间点击测反应速度

游戏逻辑在 ``core/play/games.py``（纯 Python），窗口只做渲染/输入转发。
对局结束统一发 ``game_finished(dict)``（GameResult.to_dict()），由接线层
写事件日志（复用 EventStream）。
"""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from core.play.games import (
    GAME_CATALOG,
    GuessNumberGame,
    ReactionGame,
    RockPaperScissorsGame,
)
from ui.theme.neko_palette import palette

logger = logging.getLogger(__name__)

_WINDOW_QSS = """
MiniGameWindow {{
    background: {bg};
    border: 1px solid {border};
    border-radius: 16px;
}}
MiniGameWindow QLabel#mgTitle {{
    color: {title};
    font-size: 14px;
    font-weight: 700;
}}
MiniGameWindow QLabel#mgHint {{
    color: {meta};
    font-size: 12px;
}}
MiniGameWindow QLabel#mgResult {{
    color: {text};
    font-size: 13px;
    font-weight: 600;
}}
MiniGameWindow QLabel#mgScore {{
    color: {meta};
    font-size: 12px;
}}
MiniGameWindow QPushButton#mgClose {{
    border: none;
    border-radius: 12px;
    background: rgba(131,148,175,0.12);
    color: {meta};
    font-size: 12px;
}}
MiniGameWindow QPushButton#mgClose:hover {{
    background: rgba(131,148,175,0.24);
}}
MiniGameWindow QPushButton#mgAction {{
    border: 1px solid rgba(47,104,223,0.35);
    border-radius: 999px;
    background: rgba(47,104,223,0.08);
    color: #2f68df;
    padding: 6px 16px;
    font-size: 13px;
}}
MiniGameWindow QPushButton#mgAction:hover {{
    background: rgba(47,104,223,0.16);
    border-color: rgba(47,104,223,0.6);
}}
MiniGameWindow QPushButton#mgAction:disabled {{
    color: {meta};
    background: rgba(131,148,175,0.10);
    border-color: rgba(131,148,175,0.2);
}}
MiniGameWindow QPushButton#mgBig {{
    border: 2px solid rgba(47,104,223,0.4);
    border-radius: 18px;
    background: rgba(47,104,223,0.10);
    color: #2f68df;
    font-size: 26px;
    min-height: 72px;
}}
MiniGameWindow QPushButton#mgBig:hover {{
    background: rgba(47,104,223,0.18);
}}
MiniGameWindow QLineEdit#mgInput {{
    border: 1.5px solid rgba(127,144,170,0.25);
    border-radius: 12px;
    background: {bg};
    color: {text};
    padding: 6px 10px;
    font-size: 13px;
}}
"""


class MiniGameWindow(QWidget):
    """小游戏窗口基类（无边框工具窗，父窗口可为 None）。"""

    game_finished = Signal(dict)   # GameResult.to_dict()
    close_requested = Signal()

    def __init__(self, game_id: str, theme: str = "light",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._game_id = game_id
        self._theme = theme if theme in ("light", "dark") else "light"
        self._info = GAME_CATALOG.get(game_id, {})
        self._result_label: QLabel | None = None

        self.setObjectName("miniGameWindow")
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setFixedWidth(360)

        self._build_ui()
        self._apply_qss()

    # ── UI ──

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 14)
        root.setSpacing(10)

        head = QHBoxLayout()
        self._title_label = QLabel(self._info.get("name", "小游戏"), self)
        self._title_label.setObjectName("mgTitle")
        self._close_button = QPushButton("✕", self)
        self._close_button.setObjectName("mgClose")
        self._close_button.setFixedWidth(24)
        self._close_button.setCursor(Qt.PointingHandCursor)
        self._close_button.clicked.connect(self.close_requested.emit)
        head.addWidget(self._title_label)
        head.addStretch(1)
        head.addWidget(self._close_button)
        root.addLayout(head)

        self._content = QVBoxLayout()
        self._content.setSpacing(10)
        root.addLayout(self._content)

        self._hint_label = QLabel(self._info.get("desc", ""), self)
        self._hint_label.setObjectName("mgHint")
        self._hint_label.setWordWrap(True)
        root.addWidget(self._hint_label)

        self._result_label = QLabel("", self)
        self._result_label.setObjectName("mgResult")
        self._result_label.setWordWrap(True)
        root.addWidget(self._result_label)

    def set_result_text(self, text: str) -> None:
        if self._result_label is not None:
            self._result_label.setText(text or "")

    def _apply_qss(self) -> None:
        try:
            p = palette(self._theme)
            pd = palette("dark")
            bg = p["card_bg"] if self._theme == "light" else pd["card_bg"]
            border = "rgba(53,72,104,0.10)" if self._theme == "light" else "rgba(148,166,196,0.16)"
            title = p["card_text"] if self._theme == "light" else pd["card_text"]
            text = p["card_text"] if self._theme == "light" else pd["card_text"]
            meta = p["card_meta"] if self._theme == "light" else pd["card_meta"]
            self.setStyleSheet(_WINDOW_QSS.format(
                bg=bg, border=border, title=title, text=text, meta=meta,
            ))
        except Exception as exc:
            logger.debug("MiniGameWindow QSS 失败: %s", exc)

    def set_theme(self, theme: str) -> None:
        if theme not in ("light", "dark") or theme == self._theme:
            return
        self._theme = theme
        self._apply_qss()

    @property
    def game_id(self) -> str:
        return self._game_id

    def _finish(self, result) -> None:
        """对局结束：更新结果 + 发信号（由接线层记录事件）。"""
        try:
            self.set_result_text(result.detail)
        except Exception:
            logger.debug("mini_game_window: 非致命异常(已静默吞掉)", exc_info=True)
        try:
            self.game_finished.emit(result.to_dict())
        except Exception as exc:
            logger.debug("game_finished emit 失败: %s", exc)

    def _center_on_screen(self) -> None:
        try:
            from PySide6.QtGui import QGuiApplication
            screen = QGuiApplication.primaryScreen()
            if screen is not None:
                geo = screen.availableGeometry()
                self.move(geo.center().x() - self.width() // 2,
                          geo.center().y() - self.height() // 2)
        except Exception:
            logger.debug("mini_game_window: 非致命异常(已静默吞掉)", exc_info=True)


# ── 猜数字 ───────────────────────────────────────────────


class GuessNumberWindow(MiniGameWindow):
    """猜数字窗口：输入数字 → 大了/小了/猜中。"""

    def __init__(self, theme: str = "light", parent: QWidget | None = None):
        super().__init__("guess_number", theme=theme, parent=parent)
        self._game = GuessNumberGame()

        row = QHBoxLayout()
        row.setSpacing(8)
        self._input = QLineEdit(self)
        self._input.setObjectName("mgInput")
        self._input.setPlaceholderText(f"{self._game.low}~{self._game.high}")
        self._input.returnPressed.connect(self._on_guess)
        self._guess_button = QPushButton("猜！", self)
        self._guess_button.setObjectName("mgAction")
        self._guess_button.setCursor(Qt.PointingHandCursor)
        self._guess_button.clicked.connect(self._on_guess)
        row.addWidget(self._input, stretch=1)
        row.addWidget(self._guess_button)
        self._content.addLayout(row)

        self._attempts_label = QLabel(self._attempts_text(), self)
        self._attempts_label.setObjectName("mgScore")
        self._content.addWidget(self._attempts_label)

        self._input.setFocus()

    def _attempts_text(self) -> str:
        return f"剩余机会：{self._game.attempts_left} / {self._game.max_attempts}"

    def _on_guess(self) -> None:
        raw = self._input.text().strip()
        self._input.clear()
        if not raw:
            return
        try:
            n = int(raw)
        except ValueError:
            self.set_result_text("请输入数字哦～")
            return
        out = self._game.guess(n)
        if out.get("status") == "invalid":
            self.set_result_text(f"要猜 {self._game.low}~{self._game.high} 之间的整数哦")
            return
        if out.get("status") == "win":
            self._guess_button.setEnabled(False)
            self._input.setEnabled(False)
            self._attempts_label.setText(self._attempts_text())
            self._finish(self._game.result())
            return
        if out.get("status") == "lose":
            self._guess_button.setEnabled(False)
            self._input.setEnabled(False)
            self._attempts_label.setText(self._attempts_text())
            self._finish(self._game.result())
            return
        hint = "大啦，往小猜" if out.get("hint") == "lower" else "小啦，往大猜"
        self.set_result_text(f"{hint}（已猜 {out.get('attempts')} 次）")
        self._attempts_label.setText(self._attempts_text())
        self._input.setFocus()


# ── 石头剪刀布 ───────────────────────────────────────────


class RockPaperScissorsWindow(MiniGameWindow):
    """石头剪刀布窗口：✊✋✌️ 三局两胜。"""

    def __init__(self, theme: str = "light", parent: QWidget | None = None):
        super().__init__("rps", theme=theme, parent=parent)
        self._game = RockPaperScissorsGame()

        row = QHBoxLayout()
        row.setSpacing(12)
        self._buttons: dict[str, QPushButton] = {}
        for choice, emoji in (("rock", "✊"), ("paper", "✋"), ("scissors", "✌️")):
            btn = QPushButton(emoji, self)
            btn.setObjectName("mgBig")
            btn.setFixedHeight(72)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(
                lambda checked=False, c=choice: self._on_play(c)
            )
            row.addWidget(btn, stretch=1)
            self._buttons[choice] = btn
        self._content.addLayout(row)

        self._score_label = QLabel(self._score_text(), self)
        self._score_label.setObjectName("mgScore")
        self._content.addWidget(self._score_label)

    def _score_text(self) -> str:
        return (f"你 {self._game.player_score} : {self._game.pet_score} 我"
                f"（平 {self._game.draw_score}，胜 {self._game.wins_needed} 局）")

    def _on_play(self, choice: str) -> None:
        out = self._game.play(choice)
        if out.get("result") == "invalid":
            self.set_result_text("出招无效，重新选一个～")
            return
        result_text = {
            "win": "你赢啦！🎉",
            "lose": "我赢啦～😼",
            "draw": "平局！再来～",
        }.get(out.get("result"), "")
        pet_emoji = out.get("pet_emoji", "")
        self.set_result_text(f"我出 {pet_emoji}，{result_text}")
        self._score_label.setText(self._score_text())
        if out.get("finished"):
            for btn in self._buttons.values():
                btn.setEnabled(False)
            self._finish(self._game.result())


# ── 快速反应 ─────────────────────────────────────────────


class ReactionWindow(MiniGameWindow):
    """快速反应窗口：准备 → 变绿瞬间点击 → 测 ms。"""

    _READY_TEXT = "点「开始」等信号"
    _WAIT_TEXT = "等待变绿…"
    _GO_TEXT = "点我！⚡"

    def __init__(self, theme: str = "light", parent: QWidget | None = None):
        super().__init__("reaction", theme=theme, parent=parent)
        self._game = ReactionGame()
        self._state = "idle"  # idle / waiting / armed / done

        self._big_button = QPushButton(self._READY_TEXT, self)
        self._big_button.setObjectName("mgBig")
        self._big_button.setCursor(Qt.PointingHandCursor)
        self._big_button.setMinimumHeight(96)
        self._big_button.clicked.connect(self._on_click)
        self._content.addWidget(self._big_button)

        self._delay_timer = QTimer(self)
        self._delay_timer.setSingleShot(True)
        self._delay_timer.timeout.connect(self._on_go)
        self._round_label = QLabel("", self)
        self._round_label.setObjectName("mgScore")
        self._content.addWidget(self._round_label)

    def _on_click(self) -> None:
        if self._state == "idle":
            self._start_round()
            return
        if self._state == "waiting":
            # 抢跑：立即判负
            out = self._game.record(time.monotonic())
            self._delay_timer.stop()
            self._state = "done"
            self._big_button.setText(self._READY_TEXT)
            self._big_button.setEnabled(True)
            self._finish(self._game.result())
            return
        if self._state == "armed":
            out = self._game.record(time.monotonic())
            self._state = "done"
            self._big_button.setText(self._READY_TEXT)
            self._big_button.setEnabled(True)
            self._finish(self._game.result())
            return

    def _start_round(self) -> None:
        self._game.reset()
        self.set_result_text("")
        self._state = "waiting"
        self._big_button.setText(self._WAIT_TEXT)
        self._big_button.setEnabled(False)
        delay_ms = 1200 + (time.monotonic() * 1000 % 2200)  # 1.2~3.4s
        self._game.arm(time.monotonic(), delay_sec=delay_ms / 1000.0)
        self._delay_timer.start(int(delay_ms))

    def _on_go(self) -> None:
        if self._state != "waiting":
            return
        self._state = "armed"
        self._big_button.setText(self._GO_TEXT)
        self._big_button.setEnabled(True)


def create_game_window(kind: str, theme: str = "light",
                       parent: QWidget | None = None) -> MiniGameWindow | None:
    """按游戏 id 创建窗口；未知 id 返回 None。"""
    kind = str(kind or "").strip().lower()
    if kind == "guess_number":
        return GuessNumberWindow(theme=theme, parent=parent)
    if kind == "rps":
        return RockPaperScissorsWindow(theme=theme, parent=parent)
    if kind == "reaction":
        return ReactionWindow(theme=theme, parent=parent)
    return None


__all__ = [
    "GuessNumberWindow",
    "MiniGameWindow",
    "ReactionWindow",
    "RockPaperScissorsWindow",
    "create_game_window",
]
