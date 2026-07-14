"""物理引擎 - 拖拽惯性 / 弹跳 / 巡逻行走

从 pet.py 提取的运动物理逻辑。接收一个 window 引用（QWidget），
操作其 x/y/width/height/move 等方法。

用法:
    physics = PhysicsEngine(window, callbacks)
    physics.start_walk(target_x, facing_right)
    physics.start_bounce(vx, vy)
    physics.tick()  # 在 30ms 定时器中调用
"""
from __future__ import annotations

import logging
import math
import random
from typing import Callable

from .behavior import BehaviorParams, PHYSICS_INTERVAL, INERTIA_FACTOR, INTENT_FACTOR
from .behavior import WALK_SPEED_BASE, ARRIVAL_DISTANCE
from .behavior import BOUNCE_GRAVITY, BOUNCE_FRICTION, BOUNCE_ELASTICITY, BOUNCE_MIN_SPEED

logger = logging.getLogger(__name__)


class PhysicsCallbacks:
    """物理引擎回调接口 - pet.py 实现这些方法"""

    def get_screen_geometry(self):
        """返回当前屏幕的 QScreen.geometry()"""
        raise NotImplementedError

    def get_pos(self):
        """返回窗口当前位置 (x, y)"""
        raise NotImplementedError

    def get_size(self):
        """返回窗口尺寸 (width, height)"""
        raise NotImplementedError

    def move_to(self, x: int, y: int):
        """移动窗口"""
        raise NotImplementedError

    def on_walk_finished(self):
        """行走完成回调"""
        pass

    def on_bounce_finished(self, x: int, y: int):
        """弹跳结束回调（传回最终位置）"""
        pass

    def on_facing_change(self, facing_right: bool):
        """朝向变化回调"""
        pass

    def set_anim(self, anim: str):
        """切换动画序列"""
        pass


class PhysicsEngine:
    """物理引擎 - 管理行走惯性和弹跳物理

    状态：
      _is_walking: 是否在行走
      _bounce_active: 是否在弹跳
      _vx/_vy: 速度向量
      _target_x: 行走目标 X 坐标
    """

    def __init__(self, callbacks: PhysicsCallbacks):
        self._cb = callbacks
        self._is_walking = False
        self._bounce_active = False
        self._vx: float = 0.0
        self._vy: float = 0.0
        self._target_x: int = 0

    @property
    def is_walking(self) -> bool:
        return self._is_walking

    @property
    def is_bouncing(self) -> bool:
        return self._bounce_active

    @property
    def is_active(self) -> bool:
        return self._is_walking or self._bounce_active

    def stop(self):
        """停止所有运动"""
        self._is_walking = False
        self._bounce_active = False
        self._vx = 0.0
        self._vy = 0.0

    def start_walk(self, target_x: int, facing_right: bool):
        """开始走向目标"""
        self._target_x = target_x
        self._vx = 0.0
        self._facing_right = facing_right
        self._is_walking = True
        self._cb.on_facing_change(facing_right)
        self._cb.set_anim('walk')

    def start_bounce(self, vx: float, vy: float):
        """开始弹跳"""
        self._vx = vx
        self._vy = vy
        self._bounce_active = True

    def tick(self, params: BehaviorParams):
        """物理 tick (30ms) - 行走惯性或弹跳"""
        if self._bounce_active:
            self._tick_bounce()
        elif self._is_walking:
            self._tick_walk(params)

    def _tick_bounce(self):
        """弹跳物理"""
        sg = self._cb.get_screen_geometry()
        w, h = self._cb.get_size()
        x, y = self._cb.get_pos()

        # 重力
        self._vy += BOUNCE_GRAVITY
        # 摩擦
        self._vx *= BOUNCE_FRICTION
        self._vy *= BOUNCE_FRICTION

        new_x = x + self._vx
        new_y = y + self._vy

        # 左右弹跳
        if new_x < 0:
            new_x = 0
            self._vx = abs(self._vx) * BOUNCE_ELASTICITY
        elif new_x > sg.width() - w:
            new_x = sg.width() - w
            self._vx = -abs(self._vx) * BOUNCE_ELASTICITY

        # 上下弹跳
        if new_y < 0:
            new_y = 0
            self._vy = abs(self._vy) * BOUNCE_ELASTICITY
        elif new_y > sg.height() - h:
            new_y = sg.height() - h
            self._vy = -abs(self._vy) * BOUNCE_ELASTICITY
            self._vx *= 0.85  # 地面摩擦

        self._cb.move_to(int(new_x), int(new_y))

        # 停止
        speed = math.sqrt(self._vx ** 2 + self._vy ** 2)
        if speed < BOUNCE_MIN_SPEED:
            self._bounce_active = False
            self._vx = 0.0
            self._vy = 0.0
            self._cb.set_anim('idle')
            self._cb.on_bounce_finished(int(new_x), int(new_y))

    def _tick_walk(self, params: BehaviorParams):
        """行走惯性"""
        x, _ = self._cb.get_pos()
        sg = self._cb.get_screen_geometry()
        w, _ = self._cb.get_size()

        dx = self._target_x - x
        if abs(dx) <= ARRIVAL_DISTANCE:
            self._is_walking = False
            self._cb.on_walk_finished()
            return

        max_speed = WALK_SPEED_BASE * params.speed_mul
        desired_vx = dx * 0.12
        desired_vx = max(-max_speed, min(max_speed, desired_vx))

        # 惯性
        self._vx = self._vx * INERTIA_FACTOR + desired_vx * INTENT_FACTOR

        # 朝向 - 只有当速度足够大且方向明确时才更新
        if abs(self._vx) > 1.0 and abs(dx) > 5:
            facing = (dx > 0)  # 用目标方向而不是速度方向
            self._cb.on_facing_change(facing)

        # 死区
        if abs(self._vx) < 0.2:
            self._vx = 0.3 if self._vx >= 0 else -0.3

        new_x = x + self._vx
        new_x = max(10, min(new_x, sg.width() - w - 10))

        self._cb.move_to(int(new_x), self._cb.get_pos()[1])


class MotionStateMachine:
    """运动状态机 - idle/wander/rest 转换

    在 500ms 定时器中调用 tick()，根据行为参数决定是否走动或休息。
    """

    def __init__(self, physics: PhysicsEngine, callbacks: PhysicsCallbacks):
        self._physics = physics
        self._cb = callbacks
        self._state = "idle"
        self._rest_counter = 0

    @property
    def state(self) -> str:
        return self._state

    def reset(self):
        self._state = "idle"
        self._rest_counter = 0
        self._cb.set_anim('idle')

    def tick(self, params: BehaviorParams):
        """500ms tick - 决定状态转换"""
        if self._physics.is_active:
            return
        if params.walk_chance <= 0:
            if self._state != "idle":
                self._state = "idle"
                self._cb.set_anim('idle')
            return

        # 休息倒计时
        if self._state == "rest":
            self._rest_counter -= 500
            if self._rest_counter <= 0:
                self._state = "idle"
            return

        # idle -> 决定
        if self._state == "idle":
            if random.random() < params.walk_chance:
                self._start_walk(params)
            else:
                self._start_rest(params)

    def _start_walk(self, params: BehaviorParams):
        from PySide6.QtGui import QCursor
        sg = self._cb.get_screen_geometry()
        x, _ = self._cb.get_pos()

        if params.direction_to_mouse:
            cursor = QCursor.pos()
            diff = cursor.x() - x
            if abs(diff) > 30 and 0 < cursor.x() < sg.width():
                direction = 1 if diff > 0 else -1
            else:
                direction = random.choice([-1, 1])
        else:
            direction = random.choice([-1, 1])

        distance = random.randint(params.min_dist, params.max_dist)
        target = x + direction * distance
        target = max(10, min(target, sg.width() - self._cb.get_size()[0] - 10))

        self._state = "wander"
        self._physics.start_walk(target, facing_right=(direction > 0))

    def _start_rest(self, params: BehaviorParams):
        self._state = "rest"
        self._cb.set_anim('idle')
        self._rest_counter = random.randint(
            max(params.min_pause, 1500),
            max(params.max_pause, 4000)
        )
