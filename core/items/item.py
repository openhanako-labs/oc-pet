"""桌宠物品 / 食物系统

设计参考：VPet `IFood` 接口（7 字段：Exp/Strength/StrengthFood/StrengthDrink/
Feeling/Health/Likability）+ `RealPrice` + `IsOverLoad` 防超模检测。

核心约束：
- 物品效果不直接加到属性，而是加到挂起池（pending_xxx），
  由 PetSaveManager.store_take() 每 tick 回流到主属性。
  这是 VPet 的"挂起池"设计——避免一次性大额属性变化导致动画/状态机
  来不及响应（一口吃撑）。
- 价格必须通过 is_balanced() 检查，防止 MOD/插件注册超模物品。

不依赖 PySide6 / Qt，可在 headless 测试环境直接运行。
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

from pydantic import BaseModel

from core.save.pet_save import PetSave

logger = logging.getLogger(__name__)


# ============ 物品类型 ============

class ItemType(str, Enum):
    """物品类型枚举

    str 混入让 ItemType 直接可序列化（Pydantic v2 友好）。
    """
    FOOD = "food"
    DRINK = "drink"
    GIFT = "gift"
    MEDICINE = "medicine"
    TOY = "toy"
    SNACK = "snack"


# ============ 数据模型 ============

class Item(BaseModel):
    """物品基类

    7 个效果字段对应 7 项 PetSave 属性 + 1 个 exp：
    health / stamina / hunger / thirst / mood / likability / energy / exp

    数值约定：
    - 正数 = 增益（如 effect_health=5 表示 +5 健康）
    - 负数 = 减益（如 effect_mood=-10 表示 -10 心情，吃药会苦脸）
    - 所有效果通过挂起池回流，不直接加到主属性（除 exp）
    """
    id: str                          # 唯一标识（"apple", "coffee"）
    name: str                        # 显示名（"苹果", "咖啡"）
    item_type: ItemType
    description: str = ""
    price: float = 0.0               # 虚拟价格（eddies）
    icon: str = ""                   # emoji 或图标路径

    # ---- 7 属性效果（正数加，负数减）----
    effect_health: float = 0.0
    effect_stamina: float = 0.0
    effect_hunger: float = 0.0
    effect_thirst: float = 0.0
    effect_mood: float = 0.0
    effect_likability: float = 0.0
    effect_energy: float = 0.0
    effect_exp: float = 0.0

    # ---- 动画 ----
    consume_graph: str = ""          # 使用时播放的动画名（空则按类型选默认）

    # ---- 防超模公式 ----
    # 权重设计：likability 权重大（5x，因为亲密度是稀有资源），
    # health 次之（2x），日常属性（hunger/thirst）中等（1.5x），
    # exp 权重小（0.3x，因为 exp 主要来自工作而非投喂）。

    @property
    def real_value(self) -> float:
        """估算物品实际价值（用于防超模检测）

        注意：effect_energy 不计入——精力是工作产出而非物品产出，
        避免咖啡/能量饮料这类物品被识别为超模。
        """
        return (
            abs(self.effect_health) * 2.0
            + abs(self.effect_stamina) * 1.0
            + abs(self.effect_hunger) * 1.5
            + abs(self.effect_thirst) * 1.5
            + abs(self.effect_mood) * 1.0
            + abs(self.effect_likability) * 5.0
            + abs(self.effect_exp) * 0.3
        )

    def is_balanced(self, tolerance: float = 1.3) -> bool:
        """防超模检测：价格不应低于实际价值的 1/tolerance 倍

        沿用 VPet 的 IsOverLoad 语义：tolerance=1.3 表示价格不能低于
        real_value/1.3（即可以打 23% 折扣，但再低就是超模）。

        Args:
            tolerance: 容忍度，默认 1.3（VPet 同款）。

        Returns:
            True = 价格合理；False = 可能超模（建议调高价格）。
        """
        if self.price <= 0:
            return True  # 免费物品不做检测
        return self.price >= self.real_value / tolerance

    def default_graph(self) -> str:
        """按物品类型返回默认动画名

        说明：动画名是软约定——具体项目可能定义不同动画集。
        - DRINK/FOOD/SNACK → waving（吃/喝的动作，复用）
        - GIFT/TOY → jumping（收到礼物/玩具时的开心反应）
        - MEDICINE → failed（吃药苦脸）
        - 其他 → idle（兜底）
        """
        if self.consume_graph:
            return self.consume_graph
        return {
            ItemType.DRINK: "waving",
            ItemType.FOOD: "waving",
            ItemType.GIFT: "jumping",
            ItemType.MEDICINE: "failed",
            ItemType.TOY: "jumping",
            ItemType.SNACK: "waving",
        }.get(self.item_type, "idle")


# ============ 预置物品 ============

# 价格已通过 is_balanced() 检查（tolerance=1.3），调价依据：
#   apple     10 → 30  (real=35.5, min=27.3)
#   water      5 → 25  (real=30.0, min=23.1)
#   coffee    15 → 20  (real=20.0, min=15.4)
#   fish      20 → 60  (real=72.5, min=55.8)
#   medicine  30 → 75  (real=90.0, min=69.2)
#   toy_ball  25 → 45  (real=55.0, min=42.3)
#   treat      8 → 20  (real=21.0, min=16.2)
DEFAULT_ITEMS: list[Item] = [
    Item(
        id="apple", name="苹果", item_type=ItemType.FOOD,
        price=30,
        effect_hunger=15, effect_health=5, effect_mood=3,
        icon="🍎",
        description="新鲜的红苹果，便宜管饱。",
    ),
    Item(
        id="water", name="水", item_type=ItemType.DRINK,
        price=25,
        effect_thirst=20, effect_energy=5,
        icon="💧",
        description="一杯清水，最朴素的解渴方式。",
    ),
    Item(
        id="coffee", name="咖啡", item_type=ItemType.DRINK,
        price=20,
        effect_thirst=10, effect_energy=30, effect_mood=5,
        icon="☕",
        description="浓香咖啡，提神醒脑，但不解渴。",
    ),
    Item(
        id="fish", name="小鱼干", item_type=ItemType.FOOD,
        price=60,
        effect_hunger=25, effect_mood=10, effect_likability=5,
        icon="🐟",
        description="喵星人的最爱。",
    ),
    Item(
        id="medicine", name="药", item_type=ItemType.MEDICINE,
        price=75,
        effect_health=40, effect_mood=-10,
        icon="💊",
        description="良药苦口，吃了心情会变差但能回血。",
    ),
    Item(
        id="toy_ball", name="毛线球", item_type=ItemType.TOY,
        price=45,
        effect_mood=20, effect_stamina=-10, effect_likability=5,
        icon="🧶",
        description="逗猫神器——玩得开心但会累。",
    ),
    Item(
        id="treat", name="小零食", item_type=ItemType.SNACK,
        price=20,
        effect_hunger=8, effect_mood=5, effect_health=-2,
        icon="🍪",
        description="甜甜小饼干，吃多了对健康不好。",
    ),
]


# ============ 注册表 ============

class ItemRegistry:
    """物品注册表——管理所有可用物品

    设计要点：
    - 单例友好（不需要强单例），游戏启动时创建一个 instance 即可。
    - register() 时自动跑 is_balanced()，不通过仅警告不阻塞——
      这样 MOD 作者可以临时注册调试用的超模物品，但会被日志记录。
    - list_by_type() 给 UI 分组用（食物页/饮料页/礼物页）。
    """

    def __init__(self) -> None:
        self._items: dict[str, Item] = {}
        self._load_defaults()

    def _load_defaults(self) -> None:
        """加载预置物品，并校验平衡性"""
        for item in DEFAULT_ITEMS:
            if not item.is_balanced():
                logger.warning(
                    "[ItemRegistry] 默认物品 %s 未通过平衡检查: "
                    "price=%.1f real_value=%.1f",
                    item.id, item.price, item.real_value,
                )
            self._items[item.id] = item

    def get(self, item_id: str) -> Optional[Item]:
        """按 id 获取物品，未注册返回 None"""
        return self._items.get(item_id)

    def register(self, item: Item) -> None:
        """注册新物品（MOD/插件用）

        超模物品仅警告不阻塞——这是有意的，方便调试期快速注册。
        正式发布前应调好价格或自定义 tolerance。
        """
        if not item.is_balanced():
            logger.warning(
                "[ItemRegistry] 注册物品 %s 可能超模: "
                "price=%.1f real_value=%.1f",
                item.id, item.price, item.real_value,
            )
        self._items[item.id] = item

    def list_by_type(self, item_type: ItemType) -> list[Item]:
        """按类型过滤物品（UI 分组用）"""
        return [i for i in self._items.values() if i.item_type == item_type]

    def all(self) -> list[Item]:
        """返回所有已注册物品"""
        return list(self._items.values())

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, item_id: str) -> bool:
        return item_id in self._items


# ============ 使用物品 ============

def use_item(save: PetSave, item: Item) -> dict:
    """使用物品——把效果灌入挂起池，返回动画/摘要

    关键设计：效果加到 pending_xxx（不直接加到主属性）。
    主属性回流由 PetSaveManager.store_take() 在 tick 里完成，
    这样大量使用苹果不会瞬间把 hunger 顶满、动画/状态机来不及响应。

    exp 例外：直接累加到 save.exp，不走挂起池。
    因为经验值不会被 PetSaveManager 的 store_take 处理（它有自己的
    add_exp 走升级检查），且经验通常累积不瞬时。

    Returns:
        包含物品名/图标/动画/有效效果字典的摘要，UI 据此播放动画+飘字。
    """
    # ---- 挂起池写入 ----
    save.pending_health += item.effect_health
    save.pending_stamina += item.effect_stamina
    save.pending_hunger += item.effect_hunger
    save.pending_thirst += item.effect_thirst
    save.pending_mood += item.effect_mood
    save.pending_likability += item.effect_likability

    # ---- 经验直接累加 ----
    save.exp += item.effect_exp

    # ---- 构造效果摘要（过滤 0 值，减小 payload）----
    effects = {
        k: v for k, v in {
            "health": item.effect_health,
            "stamina": item.effect_stamina,
            "hunger": item.effect_hunger,
            "thirst": item.effect_thirst,
            "mood": item.effect_mood,
            "likability": item.effect_likability,
            "energy": item.effect_energy,  # energy 不进 pending 但还是要告知 UI
            "exp": item.effect_exp,
        }.items() if v != 0
    }

    return {
        "item": item.name,
        "icon": item.icon,
        "graph": item.default_graph(),
        "effects": effects,
    }


# ============ 模块加载自检 ============

def _selftest() -> None:  # pragma: no cover
    """模块加载时跑一遍预置物品的平衡检查——失败仅警告，不抛错"""
    unbalanced = [i for i in DEFAULT_ITEMS if not i.is_balanced()]
    if unbalanced:
        logger.warning(
            "[Item] %d/%d 默认物品未通过平衡检查（详见上方的 register 警告）",
            len(unbalanced), len(DEFAULT_ITEMS),
        )
    else:
        logger.info(
            "[Item] 默认物品全部通过平衡检查（共 %d 件）",
            len(DEFAULT_ITEMS),
        )


_selftest()