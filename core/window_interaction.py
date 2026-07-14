"""窗口互动模块 - 桌宠与任意应用窗口互动

功能：
1. 桌宠靠近窗口 - 桌宠移动到目标窗口旁边
2. 虚拟物件显示在窗口上方 - 在窗口上方显示 emoji 物件
3. 桌宠在窗口边缘走动 - 桌宠沿着窗口边缘移动

使用方式：
    from core.window_interaction import WindowInteraction
    
    interaction = WindowInteraction(pet_window)
    interaction.move_near_window()  # 桌宠靠近当前窗口
    interaction.show_object_on_window("☕", "一杯咖啡")  # 在窗口上方显示物件
    interaction.walk_along_edge()  # 桌宠沿着窗口边缘走动
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QTimer, QPoint, QRect

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════
#  数据结构
# ════════════════════════════════════════════════════════════

@dataclass
class WindowRect:
    """窗口矩形"""
    x: int
    y: int
    width: int
    height: int
    
    @property
    def center_x(self) -> int:
        return self.x + self.width // 2
    
    @property
    def center_y(self) -> int:
        return self.y + self.height // 2
    
    @property
    def right(self) -> int:
        return self.x + self.width
    
    @property
    def bottom(self) -> int:
        return self.y + self.height
    
    @property
    def left(self) -> int:
        return self.x
    
    @property
    def top(self) -> int:
        return self.y


# ════════════════════════════════════════════════════════════
#  窗口互动管理器
# ════════════════════════════════════════════════════════════

class WindowInteraction:
    """窗口互动管理器
    
    职责：
    1. 获取前台窗口位置
    2. 计算桌宠应该移动到的位置
    3. 移动桌宠到目标位置
    4. 在窗口上方显示虚拟物件
    5. 让桌宠沿着窗口边缘移动
    """
    
    def __init__(self, pet_window):
        self._pet = pet_window
        self._current_window: Optional[WindowRect] = None
        self._walk_timer: Optional[QTimer] = None
        self._walk_path: list[tuple[int, int]] = []
        self._walk_index: int = 0
        
        logger.info("WindowInteraction initialized")
    
    def get_current_window(self) -> Optional[WindowRect]:
        """获取当前前台窗口的位置和大小
        
        Returns:
            WindowRect 或 None
        """
        try:
            from motion.foreground_watcher import _get_foreground_window_rect
            rect = _get_foreground_window_rect()
            if rect:
                self._current_window = WindowRect(
                    x=rect[0],
                    y=rect[1],
                    width=rect[2],
                    height=rect[3]
                )
                return self._current_window
        except Exception as e:
            logger.debug("Failed to get window rect: %s", e)
        
        return None
    
    def move_near_window(self, offset_x: int = 10, offset_y: int = 0) -> bool:
        """桌宠移动到当前窗口旁边
        
        Args:
            offset_x: 水平偏移（正值向右）
            offset_y: 垂直偏移（正值向下）
        
        Returns:
            是否成功移动
        """
        window = self.get_current_window()
        if not window:
            logger.debug("No window found")
            return False
        
        # 计算目标位置（窗口右侧）
        target_x = window.right + offset_x
        target_y = window.y + offset_y
        
        # 确保不超出屏幕
        screen = self._pet.screen()
        if screen:
            screen_rect = screen.availableGeometry()
            target_x = min(target_x, screen_rect.width() - self._pet.width())
            target_y = min(target_y, screen_rect.height() - self._pet.height())
            target_x = max(target_x, 0)
            target_y = max(target_y, 0)
        
        # 使用物理引擎平滑移动（不是瞬移）
        current_x = self._pet.pos().x()
        direction = 1 if target_x > current_x else -1
        self._pet._physics.start_walk(target_x, facing_right=(direction > 0))
        logger.info("Walking pet near window: (%d, %d)", target_x, target_y)
        
        return True
    
    def show_object_on_window(self, emoji: str, label: str, duration: int = 10) -> bool:
        """在当前窗口上方显示虚拟物件
        
        Args:
            emoji: 物件 emoji
            label: 物件描述
            duration: 显示时长（秒）
        
        Returns:
            是否成功显示
        """
        window = self.get_current_window()
        if not window:
            logger.debug("No window found")
            return False
        
        # 计算物件位置（窗口上方）
        obj_x = window.center_x
        obj_y = window.top - 50  # 窗口上方 50px
        
        # 确保不超出屏幕
        screen = self._pet.screen()
        if screen:
            screen_rect = screen.availableGeometry()
            obj_y = max(obj_y, 0)
        
        # 显示物件
        if hasattr(self._pet, '_virtual_object_overlay'):
            from ui.virtual_object_overlay import VirtualObject
            obj = VirtualObject(emoji=emoji, label=label, duration=duration)
            self._pet._virtual_object_overlay.show_object(obj, position=(obj_x, obj_y))
            logger.info("Showed object on window: %s %s at (%d, %d)", emoji, label, obj_x, obj_y)
            return True
        
        logger.debug("No virtual object overlay found")
        return False
    
    def walk_along_edge(self, speed: int = 2, steps: int = 50) -> bool:
        """桌宠沿着当前窗口边缘走动
        
        Args:
            speed: 移动速度（像素/步）
            steps: 步数
        
        Returns:
            是否开始走动
        """
        window = self.get_current_window()
        if not window:
            logger.debug("No window found")
            return False
        
        # 生成沿着窗口边缘的路径
        self._walk_path = self._generate_edge_path(window, steps)
        self._walk_index = 0
        
        if not self._walk_path:
            return False
        
        # 启动走动定时器
        if self._walk_timer:
            self._walk_timer.stop()
        
        self._walk_timer = QTimer(self._pet)
        self._walk_timer.timeout.connect(lambda: self._walk_step(speed))
        self._walk_timer.start(50)  # 50ms 一步
        
        logger.info("Started walking along window edge: %d steps", steps)
        return True
    
    def stop_walking(self):
        """停止走动"""
        if self._walk_timer:
            self._walk_timer.stop()
            self._walk_timer = None
        
        self._walk_path = []
        self._walk_index = 0
        
        # 恢复待机动画
        if hasattr(self._pet, '_set_anim_seq'):
            self._pet._set_anim_seq('idle')
        
        logger.info("Stopped walking")
    
    def _generate_edge_path(self, window: WindowRect, steps: int) -> list[tuple[int, int]]:
        """生成沿着窗口边缘的路径
        
        Args:
            window: 窗口矩形
            steps: 步数
        
        Returns:
            路径点列表 [(x, y), ...]
        """
        path = []
        
        # 从窗口右侧开始，顺时针走
        # 右侧
        for i in range(steps // 4):
            x = window.right + 10
            y = window.y + (window.height * i) // (steps // 4)
            path.append((x, y))
        
        # 底部
        for i in range(steps // 4):
            x = window.right - (window.width * i) // (steps // 4)
            y = window.bottom + 10
            path.append((x, y))
        
        # 左侧
        for i in range(steps // 4):
            x = window.x - 10
            y = window.bottom - (window.height * i) // (steps // 4)
            path.append((x, y))
        
        # 顶部
        for i in range(steps // 4):
            x = window.x + (window.width * i) // (steps // 4)
            y = window.y - 10
            path.append((x, y))
        
        return path
    
    def _walk_step(self, speed: int):
        """走动一步"""
        if self._walk_index >= len(self._walk_path):
            self.stop_walking()
            return
        
        # 获取目标位置
        target_x, target_y = self._walk_path[self._walk_index]
        
        # 移动桌宠
        self._pet.move_to(target_x, target_y)
        
        # 更新动画
        if hasattr(self._pet, '_set_anim_seq'):
            self._pet._set_anim_seq('walk')
        
        self._walk_index += 1


# ════════════════════════════════════════════════════════════
#  便捷函数
# ════════════════════════════════════════════════════════════

_global_interaction: Optional[WindowInteraction] = None

def get_window_interaction(pet_window) -> WindowInteraction:
    """获取全局 WindowInteraction 实例"""
    global _global_interaction
    if _global_interaction is None:
        _global_interaction = WindowInteraction(pet_window)
    return _global_interaction
