"""游戏会话状态机（陪玩方向 P0-1）。

回答一个问题：**「现在在玩哪个游戏、玩了多久、中途离开过没有」**。

## 关键设计：切走 ≠ 退出

`ForegroundWatcher` 只看得见**前台窗口**。如果拿"前台不是游戏"当"游戏退出了"，
那用户 alt-tab 查个攻略，会话就断了——下次切回来又从 0 开始算，陪玩节奏全乱。

所以这里把两件事分开：

| 情况 | 事件 | 会话 |
|---|---|---|
| 前台首次命中游戏 | ``game_started`` | 开始 |
| 从游戏切走，**进程还在** | ``game_background`` | 继续（只是不在前台） |
| 切回游戏 | ``game_foreground`` | 继续 |
| **进程没了** | ``game_exited`` | 结束 |

"进程还在吗"由 ``is_running`` 探针回答，三种取值：

- ``True``  ：在跑 → 后台，不断会话
- ``False`` ：没了 → 退出
- ``None``  ：**探不到**（探测整体失败 / 白名单外通用条目的进程模式为空）
  → **保守当作还在跑**，不断会话。宁可让会话多留一会儿，
  也不要误报"游戏退出了"去触发一句不该说的话。

例外：白名单外的通用条目（``generic=True``，进程模式为空）**没法探测**，
此时退而求其次——**离开前台即视为结束**（文档已注明该限制）。

## 事件形状

``{"type": str, "game": dict | None, "ts": float, "duration": float}``
``duration`` 是本次会话已持续的秒数（``game_exited`` 时为总时长）。

P0-2（状态感知）/ P0-5（节奏控制）会订阅这些事件，P0-1 只负责识别与发事件。
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from core.game.registry import GameEntry, generic_game, load_games, match_game

logger = logging.getLogger(__name__)

EV_STARTED = "game_started"
EV_FOREGROUND = "game_foreground"
EV_BACKGROUND = "game_background"
EV_EXITED = "game_exited"

# EventBus 事件名：所有游戏会话事件都走它，data 里带 ``payload=<事件 dict>``
# ⚠️ 不能叫 ``event=``：EventBus.emit 的第一个位置参数就叫 event，会撞名报 TypeError。
EVENT_BUS_NAME = "game_session"


def emit_session_events(events) -> int:
    """把会话事件转发到 EventBus（``game_session``）。

    P0-2（状态感知）/ P0-5（节奏控制）以后订阅这个事件就行，
    P0-1 只负责把“认出了游戏”变成总线上可订阅的事实。

    Returns:
        实际发出的条数（无订阅者时 emit 是 O(1) 空操作，无副作用）。
    """
    events = list(events or [])
    if not events:
        return 0
    try:
        from core.event_bus import EventBus
    except Exception:  # noqa: BLE001 — 总线不可用不该拖垮识别
        return 0
    for ev in events:
        EventBus.emit(EVENT_BUS_NAME, payload=ev)
    return len(events)


def _default_is_running(entry: GameEntry) -> Optional[bool]:
    """默认探针：按进程名模式找窗口，找到 = 还在跑。

    返回 None 表示"探不到"，交给调用方保守处理。
    """
    if not entry.process:
        return None
    try:
        from motion.foreground_watcher import find_process_windows
        wins = find_process_windows(entry.process)
    except Exception as e:  # noqa: BLE001 — 探测失败绝不拖垮主循环
        logger.debug("game: 进程探测失败（当作未知）: %s", e)
        return None
    if wins is None:
        return None          # 枚举整体失败 → 未知
    return len(wins) > 0


class GameSessionWatcher:
    """游戏会话状态机。

    Args:
        games: 白名单；留空则用 ``load_games(None)``（内置表）。
        is_running: ``(GameEntry) -> bool | None`` 进程存活探针（可注入以便测试）。
        enabled: False 时所有方法都直接返回空列表（等于关掉，零开销）。
        now: 取时间函数（可注入，测试用）。
    """

    def __init__(
        self,
        games: Optional[list[GameEntry]] = None,
        is_running: Optional[Callable[[GameEntry], Optional[bool]]] = None,
        enabled: bool = True,
        now: Callable[[], float] = time.time,
    ):
        self._games: list[GameEntry] = list(games) if games is not None else load_games(None)
        self._is_running = is_running or _default_is_running
        self._enabled = bool(enabled)
        self._now = now

        self._game: Optional[GameEntry] = None
        self._since: float = 0.0
        self._in_fg: bool = False

    # ── 查询 ──────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def current(self) -> Optional[GameEntry]:
        """当前会话的游戏（没有会话时为 None）。"""
        return self._game

    @property
    def in_foreground(self) -> bool:
        """游戏是否正在前台。"""
        return self._in_fg

    @property
    def duration(self) -> float:
        """当前会话已持续秒数（无会话为 0）。"""
        if self._game is None:
            return 0.0
        return max(0.0, self._now() - self._since)

    def session(self) -> Optional[dict]:
        """当前会话快照（给 MCP / 状态口 / 日志用）。"""
        if self._game is None:
            return None
        return {
            "game": self._game.to_dict(),
            "duration": round(self.duration, 1),
            "in_foreground": self._in_fg,
            "since": self._since,
        }

    def set_games(self, games: list[GameEntry]) -> None:
        """热替换白名单（配置重载用）。不改进行中的会话。"""
        self._games = list(games)

    # ── 驱动 ──────────────────────────────────────────────

    def on_foreground(self, process: str, title: str, category: str = "") -> list[dict]:
        """前台窗口变化时调用（由 ForegroundWatcher.on_change 驱动）。

        Args:
            process: 前台进程名。
            title: 窗口标题。
            category: ``classify_app`` 的分类；为 ``"gaming"`` 时启用通用兜底。

        Returns:
            本次产生的事件列表（通常 0~2 个，顺序有意义）。
        """
        if not self._enabled:
            return []

        entry = match_game(process, title, self._games)
        if entry is None and str(category or "").lower() == "gaming":
            entry = generic_game(title, process)

        if entry is None:
            return self._leave_game()

        return self._enter_game(entry)

    def poll(self) -> list[dict]:
        """周期性检查（和前台 tick 同频调用）：游戏不在前台时，看它是不是没了。

        Returns:
            退出事件（0~1 个）。
        """
        if not self._enabled or self._game is None or self._in_fg:
            return []
        if self._gone(self._game):
            return [self._exit_event()]
        return []

    # ── 内部 ──────────────────────────────────────────────

    def _enter_game(self, entry: GameEntry) -> list[dict]:
        events: list[dict] = []
        if self._game is None or self._game.id != entry.id:
            # 换游戏：前一个若确实没了，先报退出
            if self._game is not None and self._gone(self._game):
                events.append(self._exit_event())
            self._game = entry
            self._since = self._now()
            self._in_fg = True
            events.append(self._event(EV_STARTED))
            logger.info("游戏启动：%s（类型 %s）", entry.name, entry.type)
            return events

        if not self._in_fg:
            self._in_fg = True
            events.append(self._event(EV_FOREGROUND))
            logger.debug("切回游戏：%s", entry.name)
        return events

    def _leave_game(self) -> list[dict]:
        if self._game is None or not self._in_fg:
            return []
        self._in_fg = False
        if self._gone(self._game):
            return [self._exit_event()]
        logger.debug("游戏切到后台（仍在运行）：%s", self._game.name)
        return [self._event(EV_BACKGROUND)]

    def _gone(self, entry: GameEntry) -> bool:
        """这个游戏的会话该不该结束。

        - 能探测且探到 → 按探测结果；
        - 探不到（None）→ 通用条目按"离开前台即结束"处理，其余保守续命。
        """
        if not entry.process:
            return True   # 通用条目：无法探测进程，以离开前台为结束
        return self._is_running(entry) is False

    def _event(self, kind: str) -> dict:
        return {
            "type": kind,
            "game": self._game.to_dict() if self._game else None,
            "ts": self._now(),
            "duration": round(self.duration, 1),
        }

    def _exit_event(self) -> dict:
        ev = self._event(EV_EXITED)
        name = self._game.name if self._game else ""
        logger.info("游戏退出：%s（本次 %.0f 秒）", name, ev["duration"])
        self._game = None
        self._in_fg = False
        self._since = 0.0
        return ev
