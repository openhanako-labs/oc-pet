"""桌宠物品系统 — 食物/饮料/礼物/药品/玩具/零食"""
from .item import (
    Item,
    ItemRegistry,
    ItemType,
    DEFAULT_ITEMS,
    use_item,
)

__all__ = [
    "Item",
    "ItemRegistry",
    "ItemType",
    "DEFAULT_ITEMS",
    "use_item",
]