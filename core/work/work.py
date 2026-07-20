"""桌宠工作 / 学习 / 玩耍与经济结算系统。

设计参考 VPet v1.10：工作定义、等级解锁、状态效率、三段收益公式，
以及基于 ``threading.Timer`` 的无 Qt 异步结算。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from enum import Enum
from typing import Callable, Optional

from pydantic import BaseModel

from core.pet_state import PetStateManager
from core.save.pet_save import PetSaveManager

logger = logging.getLogger(__name__)


class WorkType(str, Enum):
    """活动类型。"""

    WORK = "work"       # 工作（赚金币）
    STUDY = "study"     # 学习（赚经验）
    PLAY = "play"       # 玩耍（加心情，减体力）


class Work(BaseModel):
    """工作定义。"""

    id: str
    name: str
    work_type: WorkType
    description: str = ""

    # 经济参数
    money_base: float = 10.0
    finish_bonus: float = 1.0
    level_limit: int = 0
    duration_minutes: float = 5.0

    # 消耗（每分钟）
    cost_hunger: float = 0.5
    cost_thirst: float = 0.8
    cost_stamina: float = 1.0
    cost_mood: float = 0.2

    # 动画
    working_graph: str = "review"
    complete_graph: str = "waving"

    # 图标
    icon: str = "💼"


DEFAULT_WORKS: list[Work] = [
    Work(
        id="desk_work", name="伏案工作", work_type=WorkType.WORK,
        money_base=10, finish_bonus=1.0, duration_minutes=5,
        icon="📝", working_graph="review",
    ),
    Work(
        id="coding", name="写代码", work_type=WorkType.WORK,
        money_base=15, finish_bonus=1.5, level_limit=3, duration_minutes=10,
        icon="💻", working_graph="review",
    ),
    Work(
        id="study_book", name="看书学习", work_type=WorkType.STUDY,
        money_base=8, finish_bonus=2.0, duration_minutes=8,
        icon="📖", working_graph="waiting",
    ),
    Work(
        id="play_ball", name="玩毛线球", work_type=WorkType.PLAY,
        money_base=0, finish_bonus=0, duration_minutes=3,
        cost_hunger=0.3, cost_stamina=0.5, cost_mood=-1.0,
        icon="🧶", working_graph="jumping",
    ),
    Work(
        id="cleaning", name="打扫卫生", work_type=WorkType.WORK,
        money_base=12, finish_bonus=0.8, level_limit=1, duration_minutes=7,
        icon="🧹", working_graph="running",
    ),
]


def calc_work_reward(
    work: Work,
    level: int,
    efficiency: float,
) -> tuple[float, float]:
    """计算工作收益并返回 ``(money_reward, exp_reward)``。

    ``efficiency`` 为 0-1，由饱腹、口渴和心情决定。计算顺序忠实保留
    VPet v1.10 的三段公式：等级基数 → 完成奖励 → 1.25 次幂。
    """
    level_factor = 1.1 * level + 10

    money_base = (
        level_factor
        if work.work_type == WorkType.WORK
        else level_factor * 10
    )
    money_reward = (
        money_base * (1 + work.finish_bonus / 2) + 1
    ) ** 1.25
    money_reward *= efficiency

    exp_reward = (money_reward / 10) ** 1.25
    if work.work_type == WorkType.STUDY:
        exp_reward *= 2

    return round(money_reward, 2), round(exp_reward, 2)


def calc_efficiency(hunger: float, thirst: float, mood: float) -> float:
    """计算工作效率（0-1）。"""
    h_factor = max(0.0, min(1.0, hunger / 60))
    t_factor = max(0.0, min(1.0, thirst / 60))
    m_factor = max(0.0, min(1.0, mood / 50))
    return round((h_factor + t_factor + m_factor) / 3, 3)


def _signed_power(value: float, exponent: float) -> float:
    """对负消耗保留符号，避免 ``negative ** 1.5`` 产生复数。"""
    return math.copysign(abs(value) ** exponent, value)


def validate_work_balance(work: Work, level: int) -> bool:
    """防超模检测：折算后的收益/消耗比不应超过 VPet 阈值 1.5。

    ``money_base`` 是工作定义的产出档位。为了让金币、经验和属性点能在
    同一量纲比较，金币按 ``money_base`` 折算，学习经验再按 VPet 的
    10 倍经验基数折算。玩耍不直接产出金币或经验，不参与经济超模检查。
    """
    efficiency = 1.0
    money, exp = calc_work_reward(work, level, efficiency)
    cost = (
        _signed_power(work.cost_hunger, 1.5) / 3
        + _signed_power(work.cost_thirst, 1.5) / 4
        + _signed_power(work.cost_mood, 1.5) / 4
        + work.level_limit / 10
    ) * work.duration_minutes * 3

    if work.work_type == WorkType.PLAY or cost <= 0:
        return True

    if work.work_type == WorkType.STUDY:
        reward = exp
        reward_scale = max(abs(work.money_base) * 10, 1.0)
    else:
        reward = money
        reward_scale = max(abs(work.money_base), 1.0)

    ratio = reward / (cost * reward_scale)
    return ratio <= 1.5


class FinishWorkInfo:
    """工作完成信息。"""

    def __init__(
        self,
        work: Work,
        money: float,
        exp: float,
        duration: float,
        reason: str,
    ):
        self.work = work
        self.money = money
        self.exp = exp
        self.duration = duration
        self.reason = reason


class WorkTimer:
    """工作计时器——使用后台定时器异步结算。"""

    def __init__(
        self,
        save_manager: PetSaveManager,
        state_manager: PetStateManager,
        on_start: Optional[Callable[[Work], None]] = None,
        on_finish: Optional[Callable[[FinishWorkInfo], None]] = None,
        on_progress: Optional[Callable[[Work, float], None]] = None,
    ):
        self._save_mgr = save_manager
        self._state_mgr = state_manager
        self._on_start = on_start
        self._on_finish = on_finish
        self._on_progress = on_progress

        self._current_work: Optional[Work] = None
        self._start_time: float = 0.0
        self._accumulated: float = 0.0
        self._running = False
        self._lock = threading.RLock()
        self._timer: Optional[threading.Timer] = None
        # 让已取消但恰好开始执行的旧 Timer 无法影响下一次工作。
        self._run_id = 0

    @property
    def is_working(self) -> bool:
        with self._lock:
            return self._running

    @property
    def current_work(self) -> Optional[Work]:
        with self._lock:
            return self._current_work

    @property
    def progress(self) -> float:
        with self._lock:
            return self._accumulated

    @staticmethod
    def _is_ill(mode: object) -> bool:
        return getattr(mode, "value", mode) == "ill"

    @staticmethod
    def _run_callback(callback: Optional[Callable], *args: object) -> None:
        """在计时器锁外执行回调，异常只记日志，不杀死后台线程。"""
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            logger.exception("WorkTimer callback raised")

    def start_work(self, work: Work) -> bool:
        """开始工作；已在工作、等级不足或生病时返回 ``False``。"""
        with self._lock:
            if self._running:
                logger.warning("Already working")
                return False

            save = self._save_mgr.save
            if save.level < work.level_limit:
                logger.warning(
                    "Level too low: %d < %d", save.level, work.level_limit,
                )
                return False

            if self._is_ill(save.mode):
                logger.warning("Too sick to work")
                return False

            self._run_id += 1
            run_id = self._run_id
            self._current_work = work
            self._start_time = time.time()
            self._accumulated = 0.0
            self._running = True
            self._timer = None

            # 与内部状态放在同一临界区，避免 stop_work 在两次写入之间插入，
            # 留下“计时器已停但 PetState 仍标记 working”的竞态。
            try:
                self._state_mgr.set_working(True)
            except Exception:
                logger.exception("Failed to mark pet as working")
                self._running = False
                self._current_work = None
                self._accumulated = 0.0
                self._run_id += 1
                return False

        self._run_callback(self._on_start, work)
        self._schedule_tick(run_id)
        return True

    def stop_work(
        self,
        reason: str = "manual_stop",
    ) -> Optional[FinishWorkInfo]:
        """停止工作，按当前完成比例结算并返回完成信息。"""
        with self._lock:
            if not self._running or self._current_work is None:
                return None

            work = self._current_work
            progress = self._accumulated
            start_time = self._start_time
            timer = self._timer

            self._running = False
            self._timer = None
            self._run_id += 1

            if timer is not None:
                timer.cancel()

            save = self._save_mgr.save
            efficiency = calc_efficiency(save.hunger, save.thirst, save.mood)
            money, exp = calc_work_reward(work, save.level, efficiency)
            money *= progress
            exp *= progress

            if work.work_type == WorkType.WORK:
                save.money += money
            elif work.work_type == WorkType.STUDY:
                save.exp += exp
            elif work.work_type == WorkType.PLAY:
                save.pending_mood += 20 * progress

            info = FinishWorkInfo(
                work=work,
                money=money,
                exp=exp,
                duration=max(0.0, time.time() - start_time),
                reason=reason,
            )

            self._current_work = None
            self._accumulated = 0.0

            # 必须在 WorkTimer 锁内清除状态，否则新工作可能先启动、再被
            # 旧 stop_work 的 False 覆盖。
            try:
                self._state_mgr.set_working(False)
            except Exception:
                logger.exception("Failed to clear pet working state")

        self._run_callback(self._on_finish, info)
        return info

    def _schedule_tick(self, run_id: Optional[int] = None) -> None:
        """更新进度、应用一秒消耗，并安排下一次检查。"""
        finish_reason: Optional[str] = None
        progress_event: Optional[tuple[Work, float]] = None

        with self._lock:
            active_run_id = self._run_id if run_id is None else run_id
            if active_run_id != self._run_id or not self._running:
                return

            work = self._current_work
            if work is None:
                return

            elapsed = time.time() - self._start_time
            duration_sec = work.duration_minutes * 60
            if duration_sec <= 0:
                self._accumulated = 1.0
            else:
                self._accumulated = min(1.0, max(0.0, elapsed / duration_sec))

            if self._accumulated >= 1.0:
                finish_reason = "complete"
            elif self._is_ill(self._save_mgr.save.mode):
                finish_reason = "state_fail"
            else:
                # 每秒应用一次按分钟配置的消耗。
                dt = 1.0
                save = self._save_mgr.save
                save.hunger -= work.cost_hunger * dt / 60
                save.thirst -= work.cost_thirst * dt / 60
                save.stamina -= work.cost_stamina * dt / 60
                save.mood -= work.cost_mood * dt / 60
                progress_event = (work, self._accumulated)

        if finish_reason is not None:
            self.stop_work(reason=finish_reason)
            return

        if progress_event is not None:
            self._run_callback(self._on_progress, *progress_event)

        with self._lock:
            if active_run_id != self._run_id or not self._running:
                return
            timer = threading.Timer(1.0, self._schedule_tick, args=(active_run_id,))
            timer.daemon = True
            self._timer = timer

        timer.start()


class WorkRegistry:
    """线程安全的工作注册表。"""

    def __init__(self):
        self._works: dict[str, Work] = {}
        self._lock = threading.RLock()
        self._load_defaults()

    def _load_defaults(self) -> None:
        with self._lock:
            for work in DEFAULT_WORKS:
                if not validate_work_balance(work, max(1, work.level_limit)):
                    logger.warning("Default work %s may be unbalanced", work.id)
                self._works[work.id] = work

    def get(self, work_id: str) -> Optional[Work]:
        with self._lock:
            return self._works.get(work_id)

    def all(self) -> list[Work]:
        """返回所有已注册工作。"""
        with self._lock:
            return list(self._works.values())

    def available(self, level: int) -> list[Work]:
        """返回当前等级可用的工作。"""
        with self._lock:
            return [
                work for work in self._works.values()
                if work.level_limit <= level
            ]

    def register(self, work: Work) -> None:
        if not validate_work_balance(work, 1):
            logger.warning("Work %s may be unbalanced", work.id)
        with self._lock:
            self._works[work.id] = work


__all__ = [
    "DEFAULT_WORKS",
    "FinishWorkInfo",
    "Work",
    "WorkRegistry",
    "WorkTimer",
    "WorkType",
    "calc_efficiency",
    "calc_work_reward",
    "validate_work_balance",
]
