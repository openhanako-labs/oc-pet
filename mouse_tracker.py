"""鼠标交互追踪器 — 感知鼠标状态，驱动桌宠反应

追踪鼠标相对于角色的位置、速度、静止时长，
产生状态信号供 pet.py 处理行为反应。

状态:
  idle      — 鼠标在远处，无特殊行为
  nearby    — 鼠标在角色附近（< NEARBY_RADIUS px）
  hovering  — 鼠标在角色附近且静止（> HOVER_TIME 秒）
  chasing   — 鼠标长时间不动，桌宠走过去
  startled  — 鼠标快速掠过（速度 > STARTLE_SPEED px/s）

用法:
    tracker = MouseTracker(get_window_rect)
    tracker.on_nearby = lambda: ...
    tracker.on_startled = lambda speed: ...
    tracker.on_hover = lambda: ...
    tracker.on_chase = lambda target_x: ...
    tracker.on_leave = lambda: ...
    tracker.tick()  # 每 200ms 调用
"""
from __future__ import annotations

import math
import time
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtGui import QCursor

logger = logging.getLogger(__name__)


# ── 阈值配置 ──

NEARBY_RADIUS = 120      # "附近"判定半径 (px)
HOVER_TIME = 8.0         # 静止多久算 hover (秒)
CHASE_TIME = 15.0        # 静止多久触发追逐 (秒)
STARTLE_SPEED = 2500     # 快速掠过阈值 (px/s)
STARTLE_COOLDOWN = 8.0   # 惊吓冷却 (秒)
LEAVE_GRACE = 1.0        # 离开判定延迟 (秒)
MOUSE_STILL_THRESHOLD = 8  # 鼠标"静止"判定 (px/帧)


@dataclass
class MouseState:
    """鼠标状态快照"""
    x: int = 0
    y: int = 0
    speed: float = 0.0        # px/s
    distance_to_pet: float = 0  # 到角色中心的距离
    is_nearby: bool = False
    is_still: bool = False
    still_duration: float = 0.0  # 持续静止时长 (秒)


class MouseTracker:
    """鼠标交互追踪器

    Args:
        get_window_rect: 返回角色窗口 (x, y, w, h) 的 callable
    """

    def __init__(self, get_window_rect: Callable):
        self._get_window_rect = get_window_rect

        # 状态
        self._state = MouseState()
        self._scene: str = "idle"  # idle / nearby / hovering / chasing / startled
        self._last_pos: tuple[int, int] = (0, 0)
        self._last_move_time: float = time.time()
        self._last_startle_time: float = 0.0
        self._nearby_since: float = 0.0
        self._leave_timer: float = 0.0

        # 回调（由 pet.py 设置）
        self.on_nearby: Optional[Callable] = None       # 鼠标进入附近
        self.on_hover: Optional[Callable] = None        # 鼠标悬停
        self.on_chase: Optional[Callable[[int], None]] = None  # 追逐目标 x
        self.on_startled: Optional[Callable[[float], None]] = None  # 惊吓 (speed)
        self.on_leave: Optional[Callable] = None        # 鼠标离开附近

    @property
    def scene(self) -> str:
        return self._scene

    @property
    def state(self) -> MouseState:
        return self._state

    @property
    def is_nearby(self) -> bool:
        return self._scene in ("nearby", "hovering", "chasing")

    def tick(self):
        """每 200ms 调用，更新鼠标状态并触发回调"""
        now = time.time()
        cursor = QCursor.pos()
        cx, cy = cursor.x(), cursor.y()

        # 速度计算
        dx = cx - self._last_pos[0]
        dy = cy - self._last_pos[1]
        dist_moved = math.sqrt(dx * dx + dy * dy)
        dt = 0.2  # tick 间隔
        speed = dist_moved / dt if dt > 0 else 0

        # 鼠标是否静止
        is_still = dist_moved < MOUSE_STILL_THRESHOLD
        if not is_still:
            self._last_move_time = now
        still_duration = now - self._last_move_time

        # 到角色中心的距离
        rect = self._get_window_rect()
        if rect:
            pet_cx = rect[0] + rect[2] // 2
            pet_cy = rect[1] + rect[3] // 2
            dist_to_pet = math.sqrt((cx - pet_cx) ** 2 + (cy - pet_cy) ** 2)
        else:
            dist_to_pet = 9999

        is_nearby = dist_to_pet < NEARBY_RADIUS

        # 更新状态
        self._state = MouseState(
            x=cx, y=cy, speed=speed,
            distance_to_pet=dist_to_pet,
            is_nearby=is_nearby,
            is_still=is_still,
            still_duration=still_duration,
        )

        self._last_pos = (cx, cy)

        # ── 状态机 ──
        old_scene = self._scene
        new_scene = self._scene

        # 惊吓检测（优先级最高，瞬间触发）
        if speed > STARTLE_SPEED and (now - self._last_startle_time) > STARTLE_COOLDOWN:
            if old_scene != "startled":
                new_scene = "startled"
                self._last_startle_time = now
                if self.on_startled:
                    self.on_startled(speed)
                # 惊吓后自动回到 idle
                self._scene = "idle"
                return

        if new_scene == "startled":
            new_scene = "idle"

        # 状态转移
        if is_nearby:
            self._leave_timer = 0
            if still_duration >= CHASE_TIME and old_scene != "chasing":
                new_scene = "chasing"
                if self.on_chase:
                    self.on_chase(cx)
            elif still_duration >= HOVER_TIME and old_scene not in ("hovering", "chasing"):
                new_scene = "hovering"
                if self.on_hover:
                    self.on_hover()
            elif old_scene == "idle":
                new_scene = "nearby"
                self._nearby_since = now
                if self.on_nearby:
                    self.on_nearby()
        else:
            # 鼠标在远处
            if old_scene in ("nearby", "hovering", "chasing"):
                self._leave_timer += dt
                if self._leave_timer >= LEAVE_GRACE:
                    new_scene = "idle"
                    if self.on_leave:
                        self.on_leave()
            else:
                new_scene = "idle"

        self._scene = new_scene
