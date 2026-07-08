"""行为模式参数化配置"""

from dataclasses import dataclass


@dataclass(frozen=True)
class BehaviorParams:
    """行为模式参数 — 每项对应一个模式的行为特性"""
    walk_chance: float         # 每次检测触发的概率 0-1
    min_dist: int              # 最小移动距离(px)
    max_dist: int              # 最大移动距离(px)
    speed_mul: float           # 移动速度倍率
    direction_to_mouse: bool   # 是否朝鼠标方向走
    min_pause: int             # 走完后的最小休息(ms)
    max_pause: int             # 走完后的最大休息(ms)


BEHAVIOR_MODES = {
    "quiet": BehaviorParams(
        walk_chance=0.0, min_dist=0, max_dist=0,
        speed_mul=0.3,
        direction_to_mouse=False, min_pause=0, max_pause=0
    ),
    "normal": BehaviorParams(
        walk_chance=0.3, min_dist=30, max_dist=100,
        speed_mul=1.0,
        direction_to_mouse=False, min_pause=2000, max_pause=5000
    ),
    "active": BehaviorParams(
        walk_chance=0.7, min_dist=60, max_dist=200,
        speed_mul=1.3,
        direction_to_mouse=False, min_pause=1000, max_pause=3000
    ),
    "cling": BehaviorParams(
        walk_chance=0.6, min_dist=30, max_dist=120,
        speed_mul=1.1,
        direction_to_mouse=True, min_pause=1500, max_pause=4000
    ),
}

# ─── 惯性运动常量 ───────────────────────────────────────

PHYSICS_INTERVAL = 30          # 物理更新间隔 (ms)，≈33fps
INERTIA_FACTOR = 0.90          # 惯性保持 (0-1, 越高越滑)
INTENT_FACTOR = 0.10           # 目标牵引力 (0-1)
ARRIVAL_DISTANCE = 6           # 到达判定距离 (px)
WALK_SPEED_BASE = 4.0          # 基础走路速度 (px/帧)

# ─── 弹跳物理常量 ───────────────────────────────────────

BOUNCE_ELASTICITY = 0.55       # 边缘反弹系数 (0-1)
BOUNCE_FRICTION = 0.92         # 每帧速度衰减
BOUNCE_GRAVITY = 0.15          # 向下加速度 (px/帧²)
BOUNCE_MIN_SPEED = 0.3         # 低于此速度停止弹跳
