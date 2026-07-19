"""桌宠工作、学习、玩耍与经济结算系统。"""
from .work import (
    DEFAULT_WORKS,
    FinishWorkInfo,
    Work,
    WorkRegistry,
    WorkTimer,
    WorkType,
    calc_efficiency,
    calc_work_reward,
    validate_work_balance,
)

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
