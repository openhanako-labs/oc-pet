"""养成状态管理器——挂起池回流 + 自然衰减 + 模式计算

桥接层：PetSave 存数据，PetState 管逻辑。

设计要点：
- 挂起池（pending_*）：物品/动作一次给的数值不直接加满，而是分 tick 慢慢回流。
  避免"一口吃撑"——VPet 的 StoreXxx 思路。
- 自然衰减：每秒根据速率扣减 hunger/thirst/energy/stamina/mood。
- 模式计算：属性阈值决定 ill/poor/normal/happy 之一，回调通知。

依赖：
- core.save.pet_save.PetSaveManager（前置模块，由其他任务落地）

线程安全：
- 所有写操作加锁。
- 模式变更回调在锁外触发，避免回调内访问 self.save 死锁。

不要：
- 不要在这里做持久化——save_manager 负责落盘。
- 不要在这里做 UI 输出——mode 变化由回调层接管。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from core.save.pet_save import PetSaveManager

logger = logging.getLogger(__name__)


class PetStateManager:
    """养成状态管理器——挂起池回流 + 自然衰减 + 模式计算"""

    # -------------------------------------------------------------------------
    # 可配置参数（子类可覆盖或运行时改实例属性）
    # -------------------------------------------------------------------------

    # 衰减速率（每秒）
    DECAY_RATES: dict[str, float] = {
        "hunger": 0.008,      # ~0.5/min
        "thirst": 0.013,      # ~0.8/min
        "energy": 0.005,      # ~0.3/min
        "stamina": 0.003,     # ~0.2/min（工作时加倍）
        "mood": 0.002,        # ~0.1/min
    }

    # 饥饿/口渴长期过低时，对 health 的附加伤害（每秒）
    HEALTH_DAMAGE_RATE = 0.005  # ~0.3/min

    # 挂起池回流比例（每 tick 消化多少比例的挂起值）
    STORE_DRAIN_RATE = 0.1

    # 模式阈值
    ILL_HEALTH_THRESHOLD = 30.0
    ILL_STAMINA_THRESHOLD = 20.0
    POOR_HUNGER_THRESHOLD = 30.0
    POOR_THIRST_THRESHOLD = 30.0
    POOR_MOOD_THRESHOLD = 30.0
    HAPPY_MOOD_THRESHOLD = 80.0
    HAPPY_HEALTH_THRESHOLD = 70.0

    def __init__(
        self,
        save_manager: "PetSaveManager",
        on_mode_change: Optional[Callable[[str, str], None]] = None,
    ):
        """
        Args:
            save_manager: PetSaveManager 实例（前置依赖）
            on_mode_change: 模式变化回调 (old_mode, new_mode) -> None
        """
        self._save_mgr = save_manager
        self._on_mode_change = on_mode_change
        self._last_tick = time.time()
        self._lock = threading.Lock()
        self._working = False  # 是否在工作中（影响 stamina 衰减）

    # -------------------------------------------------------------------------
    # 公共属性 / 控制
    # -------------------------------------------------------------------------

    @property
    def save(self):
        return self._save_mgr.save

    def set_working(self, working: bool):
        """设置工作状态——影响 stamina 衰减速度（×2）"""
        with self._lock:
            self._working = working

    @property
    def working(self) -> bool:
        with self._lock:
            return self._working

    def apply_item_effect(self, item_effects: dict):
        """应用物品效果到挂起池（不直接加属性，慢慢回流）

        Args:
            item_effects: {attr_name: value}，例如 {"hunger": 30, "mood": 10}
                          负值也支持（惩罚性物品）。
        """
        with self._lock:
            s = self.save
            for attr, value in item_effects.items():
                pending_attr = f"pending_{attr}"
                if not hasattr(s, pending_attr):
                    logger.debug("ignore unknown attr: %s", attr)
                    continue
                current = getattr(s, pending_attr, 0) or 0.0
                setattr(s, pending_attr, current + value)

    def is_pending_empty(self) -> bool:
        """挂起池是否已排空（所有 pending_* 趋近 0）"""
        with self._lock:
            s = self.save
            for attr in ("health", "stamina", "hunger", "thirst", "mood", "likability"):
                pending = getattr(s, f"pending_{attr}", 0) or 0.0
                if abs(pending) > 0.01:
                    return False
            return True

    # -------------------------------------------------------------------------
    # 主循环
    # -------------------------------------------------------------------------

    def tick(self, dt_seconds: Optional[float] = None):
        """每帧/每秒调用一次——衰减 + 挂起池回流 + 钳制 + 模式重算

        Args:
            dt_seconds: 距上次 tick 的秒数。None 则按 wall clock 算。
        """
        now = time.time()
        if dt_seconds is None:
            dt_seconds = now - self._last_tick
        self._last_tick = now
        # dt 为负或过大的兜底
        if dt_seconds < 0:
            dt_seconds = 0.0
        if dt_seconds > 60.0:
            # 避免长挂起后一次性扣干
            dt_seconds = 60.0

        mode_changed = False
        old_mode = "normal"
        new_mode = "normal"

        with self._lock:
            self._apply_decay(dt_seconds)
            self._drain_pending()
            self._clamp_attributes()

            old_mode = self.save.mode
            new_mode = self._cal_mode()
            if new_mode != old_mode:
                self.save.mode = new_mode
                mode_changed = True

        # 回调放锁外——回调里再摸 self.save 不会死锁
        if mode_changed and self._on_mode_change is not None:
            try:
                self._on_mode_change(old_mode, new_mode)
            except Exception:
                logger.exception("on_mode_change callback raised: old=%s new=%s", old_mode, new_mode)

    # -------------------------------------------------------------------------
    # 内部步骤
    # -------------------------------------------------------------------------

    def _apply_decay(self, dt: float):
        """自然衰减

        working 时 stamina 衰减倍率 2.0。
        hunger/thirst 长期 < 20 时附加 health 伤害。
        """
        s = self.save
        for attr, rate in self.DECAY_RATES.items():
            current = getattr(s, attr, None)
            if current is None:
                continue
            multiplier = 2.0 if (self._working and attr == "stamina") else 1.0
            setattr(s, attr, current - rate * dt * multiplier)

        if (s.hunger is not None and s.hunger < 20) or \
           (s.thirst is not None and s.thirst < 20):
            s.health = s.health - self.HEALTH_DAMAGE_RATE * dt

    def _drain_pending(self):
        """挂起池回流——每 tick 消化 STORE_DRAIN_RATE 比例的挂起值

        处理时把挂起值加到当前属性，再从挂起池扣掉。
        阈值过滤：|pending| < 0.01 直接清零，避免浮点残留。
        """
        s = self.save
        for attr in ("health", "stamina", "hunger", "thirst", "mood", "likability"):
            pending = getattr(s, f"pending_{attr}", 0) or 0.0
            if abs(pending) > 0.01:
                drain = pending * self.STORE_DRAIN_RATE
                current = getattr(s, attr, 0) or 0.0
                setattr(s, attr, current + drain)
                setattr(s, f"pending_{attr}", pending - drain)
            else:
                setattr(s, f"pending_{attr}", 0.0)

    def _clamp_attributes(self):
        """属性钳制到合法范围"""
        s = self.save
        s.health = max(0.0, min(s.health, s.health_max))
        s.stamina = max(0.0, min(s.stamina, s.stamina_max))
        s.hunger = max(0.0, min(s.hunger, 100.0))
        s.thirst = max(0.0, min(s.thirst, 100.0))
        s.mood = max(0.0, min(s.mood, s.mood_max))
        s.likability = max(0.0, min(s.likability, s.likability_max))
        s.energy = max(0.0, min(s.energy, 100.0))

    def _cal_mode(self) -> str:
        """根据属性阈值计算当前模式

        优先级：ill > poor > happy > normal
        - ill：health 或 stamina 严重不足
        - poor：饥饿/口渴/心情之一过低
        - happy：心情 + 健康都高
        - normal：其他情况
        """
        s = self.save
        if s.health < self.ILL_HEALTH_THRESHOLD or s.stamina < self.ILL_STAMINA_THRESHOLD:
            return "ill"
        if (s.hunger < self.POOR_HUNGER_THRESHOLD or
                s.thirst < self.POOR_THIRST_THRESHOLD or
                s.mood < self.POOR_MOOD_THRESHOLD):
            return "poor"
        if s.mood > self.HAPPY_MOOD_THRESHOLD and s.health > self.HAPPY_HEALTH_THRESHOLD:
            return "happy"
        return "normal"


__all__ = ["PetStateManager"]
