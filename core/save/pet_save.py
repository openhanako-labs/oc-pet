"""桌宠养成数据存档

设计参考：
- VPet 第 8 节存档系统（IGameSave + 自动保存 + 迁移）
- VPet 挂起池（StoreXxx）——一次性大额属性变化拆成多个 tick 慢慢回流，
  避免"一口吃撑"导致动画/状态机来不及响应。

路径约定：~/.oc-pet/saves/<agent_id>.json
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


# ============ 路径常量 ============

# 用户级存档目录
SAVE_DIR: Path = Path.home() / ".oc-pet" / "saves"

# 隐式属性上限（hunger/thirst/energy 没有 _max 字段，按此上限夹紧）
IMPLICIT_MAX: float = 100.0


# ============ 枚举 ============

class ModeType(str, Enum):
    """综合健康/情绪模式（VPet 的 Happy/Nomal/PoorCondition/Ill）"""
    HAPPY = "happy"
    NORMAL = "normal"
    POOR = "poor"
    ILL = "ill"


# ============ 数据模型 ============

class PetSave(BaseModel):
    """桌宠养成数据存档
    
    字段顺序即 JSON 顺序——Pydantic v2 按字段定义顺序序列化。
    """
    # ---- 基本信息 ----
    name: str = "月薪喵"
    host_name: str = "主人"

    # ---- 经济 ----
    money: float = 0.0
    exp: float = 0.0
    level: int = 1

    # ---- 7 属性（0-100）----
    health: float = 80.0       # 健康
    stamina: float = 100.0     # 体力
    hunger: float = 80.0       # 饱腹（高=饱）
    thirst: float = 80.0       # 口渴（高=不渴）
    mood: float = 70.0         # 心情
    likability: float = 50.0   # 好感度
    energy: float = 100.0      # 精力

    # ---- 属性上限（随等级提升）----
    health_max: float = 100.0
    stamina_max: float = 100.0
    mood_max: float = 100.0
    likability_max: float = 100.0

    # ---- 挂起池（VPet StoreXxx：避免一口吃撑）----
    pending_health: float = 0.0
    pending_stamina: float = 0.0
    pending_hunger: float = 0.0
    pending_thirst: float = 0.0
    pending_mood: float = 0.0
    pending_likability: float = 0.0

    # ---- 元数据 ----
    created_at: float = Field(default_factory=time.time)
    last_save_at: float = Field(default_factory=time.time)
    total_play_time: float = 0.0  # 秒
    save_version: int = 1

    # ---- 当前模式（由 cal_mode() 写入）----
    mode: ModeType = ModeType.NORMAL


# 挂起池字段映射：(pending_field, main_field, max_field_or_None)
# 集中维护，避免 store_take 里散落字符串
_PENDING_TRANSFERS = (
    ("pending_health", "health", "health_max"),
    ("pending_stamina", "stamina", "stamina_max"),
    ("pending_hunger", "hunger", None),
    ("pending_thirst", "thirst", None),
    ("pending_mood", "mood", "mood_max"),
    ("pending_likability", "likability", "likability_max"),
)


# ============ 管理器 ============

class PetSaveManager:
    """桌宠养成数据存档管理器
    
    使用方式：
        mgr = PetSaveManager.from_agent_id("yuexinmiao")
        mgr.load()
        mgr.save.tick_decay(dt_seconds=60.0)
        mgr.auto_save()
    
    路径约定：~/.oc-pet/saves/<agent_id>.json
    """

    def __init__(self, save_path: str):
        self.save_path: Path = Path(save_path)
        self.save: PetSave = PetSave()
        # 自动保存节流
        self._last_auto_save: float = 0.0
        self._auto_save_interval: float = 60.0  # 默认 60 秒

    # ---------- 工厂 ----------

    @classmethod
    def from_agent_id(cls, agent_id: str = "default") -> "PetSaveManager":
        """从 agent_id 生成默认路径并确保目录存在

        Args:
            agent_id: 桌宠实例标识（如 "yuexinmiao"），用于多开隔离
        """
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        return cls(str(SAVE_DIR / f"{agent_id}.json"))

    # ---------- 持久化 ----------

    def load(self) -> PetSave:
        """从文件加载，不存在则用默认值

        存档损坏时备份原文件并退回默认值，避免反复 crash 覆盖证据。
        """
        if not self.save_path.exists():
            self.save = PetSave()
            return self.save

        try:
            with open(self.save_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            self._backup_corrupted()
            self.save = PetSave()
            return self.save

        try:
            # 未来版本迁移入口：按 data.get("save_version", 1) 分流
            self.save = PetSave(**data)
        except (TypeError, ValueError):
            # 字段不匹配（升级导致结构变化）也走备份兜底
            self._backup_corrupted()
            self.save = PetSave()
        return self.save

    def save_to_disk(self) -> None:
        """写入 JSON，更新 last_save_at（原子写入）

        写入流程：tempfile → flush → os.replace，掉电也不会留半截文件。
        """
        self.save_path.parent.mkdir(parents=True, exist_ok=True)
        self.save.last_save_at = time.time()

        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(self.save_path.parent),
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(
                    self.save.model_dump(mode="json"),
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.save_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def auto_save(self) -> bool:
        """自动保存（由定时器调用）

        Returns:
            True 表示本次执行了写入；False 表示被节流跳过。
        """
        now = time.time()
        if now - self._last_auto_save < self._auto_save_interval:
            return False
        self._last_auto_save = now
        self.save_to_disk()
        return True

    def set_auto_save_interval(self, seconds: float) -> None:
        """调整自动保存间隔（调试/测试用）"""
        self._auto_save_interval = max(0.0, float(seconds))

    def _backup_corrupted(self) -> None:
        """备份损坏的存档文件，便于事后排查"""
        try:
            stamp = int(time.time())
            backup = self.save_path.with_suffix(f".corrupted.{stamp}.json")
            self.save_path.rename(backup)
        except OSError:
            pass

    # ---------- 模式判定 ----------

    def cal_mode(self) -> ModeType:
        """根据属性阈值计算当前模式

        优先级（重要）：ILL > POOR > HAPPY > NORMAL
        必须先判 ILL，否则 health<30 同时 hunger<30 时会被 POOR 误判。

        阈值：
        - health<30 or stamina<20 → ILL
        - hunger<30 or thirst<30 or mood<30 → POOR
        - mood>80 and health>70 → HAPPY
        - 其余 → NORMAL

        Returns:
            ModeType 枚举值；同步写入 self.save.mode。
        """
        s = self.save

        if s.health < 30 or s.stamina < 20:
            mode = ModeType.ILL
        elif s.hunger < 30 or s.thirst < 30 or s.mood < 30:
            mode = ModeType.POOR
        elif s.mood > 80 and s.health > 70:
            mode = ModeType.HAPPY
        else:
            mode = ModeType.NORMAL

        s.mode = mode
        return mode

    # ---------- 挂起池 ----------

    def store_take(self, ratio: float = 0.1) -> None:
        """挂起池回流——每个 tick 把 pending 的一部分加到主属性

        VPet 核心设计：投喂/工作结算等场景可能一次性给出大量数值，
        直接加会让属性瞬间爆表，动画/状态机来不及响应。
        所以先堆到 pending_xxx，每个 tick 按 ratio 转正到主属性。

        Args:
            ratio: 每次回流比例，默认 0.1（即每 tick 转 10%）。
                   设 1.0 等价于"立刻全部转正"。
        """
        if ratio <= 0:
            return

        for pending_field, main_field, max_field in _PENDING_TRANSFERS:
            pending = getattr(self.save, pending_field)
            if pending <= 0:
                continue

            # ratio=1.0 时一次性清空 pending
            if ratio >= 1.0:
                delta = pending
            else:
                delta = pending * ratio

            setattr(self.save, pending_field, pending - delta)

            current = getattr(self.save, main_field) + delta
            cap = getattr(self.save, max_field) if max_field else IMPLICIT_MAX
            # 夹紧到 [0, cap]，防止溢出
            if current > cap:
                current = cap
            elif current < 0:
                current = 0
            setattr(self.save, main_field, current)

    def add_pending(
        self,
        field: str,
        amount: float,
    ) -> None:
        """往挂起池加值（外部喂食/工作结算入口）

        支持字段名（不含 pending_ 前缀）：health/stamina/hunger/thirst/mood/likability
        也可以直接传完整字段名：pending_xxx。
        """
        if amount == 0:
            return

        # 允许传入短名自动转 pending_xxx
        if not field.startswith("pending_"):
            field = f"pending_{field}"

        valid = {p[0] for p in _PENDING_TRANSFERS}
        if field not in valid:
            raise ValueError(
                f"未知挂起池字段: {field}（可选: {sorted(valid)}）"
            )

        current = getattr(self.save, field)
        # pending 可正可负（治疗用正，伤害用负），不做上限夹紧
        setattr(self.save, field, current + amount)

    # ---------- 升级 ----------

    def add_exp(self, amount: float) -> bool:
        """加经验，触发升级检查（支持连续升级）

        Args:
            amount: 经验增量，<=0 直接返回 False

        Returns:
            True 表示触发了至少一次升级。
        """
        if amount <= 0:
            return False

        self.save.exp += amount
        leveled = False

        # 溢出经验自动滚入下一级（防止大额经验被截断）
        threshold = self._exp_threshold()
        while self.save.exp >= threshold:
            self.save.exp -= threshold
            self.level_up()
            leveled = True
            threshold = self._exp_threshold()

        return leveled

    def _exp_threshold(self) -> float:
        """升级所需经验：level * 100（线性递增）

        Lv1→Lv2 要 100，Lv2→Lv3 要 200...Lvn→Lvn+1 要 n*100。
        简单可预测，方便外部做收益规划。
        """
        return float(self.save.level * 100)

    def level_up(self) -> None:
        """升级：提升属性上限 + 好感度上限 + 回部分状态

        - 4 个有 _max 的属性上限各 +5
        - likability_max +10（升级直接体现"亲密度提升"）
        - 回血/回体力/回心情（避免满级残血）
        """
        s = self.save
        s.level += 1
        s.health_max += 5.0
        s.stamina_max += 5.0
        s.mood_max += 5.0
        s.likability_max += 10.0

        # 升级奖励：按上限回一部分
        s.health = min(s.health + 20.0, s.health_max)
        s.stamina = min(s.stamina + 20.0, s.stamina_max)
        s.mood = min(s.mood + 10.0, s.mood_max)

    # ---------- 自然衰减 ----------

    def tick_decay(
        self,
        dt_seconds: float,
        working: bool = False,
    ) -> None:
        """属性自然衰减（每分钟调一次，或按 dt_seconds 按比例缩放）

        衰减速率（每分钟）：
            hunger  -= 0.5
            thirst  -= 0.8
            energy  -= 0.3
            stamina -= 0.2（工作时额外 -0.5）
            mood    -= 0.1
            health  -= 0.2（仅当 hunger<30 or thirst<30 时）

        Args:
            dt_seconds: 自上次 tick 起的实际秒数
            working: 是否处于工作状态（工作时 stamina/energy 衰减更快）
        """
        if dt_seconds <= 0:
            return

        s = self.save
        minutes = dt_seconds / 60.0

        # 基础衰减
        s.hunger = max(0.0, s.hunger - 0.5 * minutes)
        s.thirst = max(0.0, s.thirst - 0.8 * minutes)
        s.energy = max(0.0, s.energy - 0.3 * minutes)
        s.stamina = max(0.0, s.stamina - 0.2 * minutes)
        s.mood = max(0.0, s.mood - 0.1 * minutes)

        # 工作额外衰减
        if working:
            s.stamina = max(0.0, s.stamina - 0.5 * minutes)
            s.energy = max(0.0, s.energy - 0.3 * minutes)

        # 长期饥饿/口渴过低时扣健康
        if s.hunger < 30 or s.thirst < 30:
            s.health = max(0.0, s.health - 0.2 * minutes)

        # 累加游戏时长
        s.total_play_time += dt_seconds

        # 衰减后重新计算模式（属性可能跌破阈值）
        self.cal_mode()


# ============ 自检入口 ============

if __name__ == "__main__":  # pragma: no cover
    # 简易自检：加载 → 加经验 → 衰减 → 保存
    mgr = PetSaveManager.from_agent_id("_selftest")
    mgr.load()
    print(f"[init] level={mgr.save.level} mode={mgr.save.mode}")

    mgr.add_exp(150)
    print(f"[add_exp +150] level={mgr.save.level} exp={mgr.save.exp}")

    mgr.tick_decay(dt_seconds=60.0, working=True)
    print(f"[tick_decay 60s] stamina={mgr.save.stamina:.1f} mode={mgr.save.mode}")

    mgr.add_pending("health", 50.0)
    mgr.store_take()
    print(f"[pending+take] pending={mgr.save.pending_health:.1f} "
          f"health={mgr.save.health:.1f}")

    mgr.save_to_disk()
    print(f"[saved] {mgr.save_path}")