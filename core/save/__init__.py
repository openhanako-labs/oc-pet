"""养成数据存档子系统

VPet 第 8 节存档系统设计落地：
- PetSave Pydantic 模型：怎么存
- PetSaveManager：怎么变 + 持久化 + 模式判定
- 挂起池（StoreXxx）：一次性大额改动拆成多个 tick 慢慢回流
"""

from .pet_save import (
    SAVE_DIR,
    IMPLICIT_MAX,
    ModeType,
    PetSave,
    PetSaveManager,
)

__all__ = [
    "SAVE_DIR",
    "IMPLICIT_MAX",
    "ModeType",
    "PetSave",
    "PetSaveManager",
]
