"""AvatarRenderer 抽象接口 - 所有渲染形态的统一基类

后端业务逻辑（对话、行为、感知）只跟此接口交互，
不关心底层是帧精灵、Live2D 还是 VRM。

子类需实现所有 abstract 方法。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class AvatarRenderer(ABC):
    """Avatar 渲染器抽象基类。

    生命周期：
        load(character_id) -> play_anim / look_at / set_emotion -> cleanup

    状态：
        _character_id: 当前角色 ID
        _current_anim: 当前动画名（idle/walk/extra）
        _current_emotion: 当前情绪
        _scale: 缩放倍率
    """

    def __init__(self):
        self._character_id: str = ""
        self._current_anim: str = "idle"
        self._current_emotion: str = "neutral"
        self._scale: float = 1.0

    # ── 生命周期 ──

    @abstractmethod
    def load(self, character_id: str) -> bool:
        """加载角色资源。

        Args:
            character_id: 角色 ID（如 "yuexinmiao"、"phoebe"）

        Returns:
            True 如果加载成功
        """
        ...

    @abstractmethod
    def cleanup(self) -> None:
        """释放资源（窗口、定时器、模型等）"""
        ...

    # ── 动画控制 ──

    @abstractmethod
    def play_anim(
        self,
        anim: str,
        emotion: str = "",
        frame_range: Optional[tuple[int, int]] = None,
    ) -> None:
        """播放动画序列。

        Args:
            anim: 动画名（idle/walk/extra）
            emotion: 可选情绪名，用于子帧区间映射
            frame_range: 可选帧区间 (start, end)，None 表示全序列
        """
        ...

    @abstractmethod
    def set_emotion(self, emotion: str, intensity: float = 1.0) -> None:
        """设置情绪表情。

        Args:
            emotion: 情绪名（happy/angry/sad/surprised/thinking/neutral）
            intensity: 情绪强度 0.0~1.0（用于未来帧混合）
        """
        ...

    # ── 视线 ──

    @abstractmethod
    def look_at(self, x: int, y: int) -> None:
        """视线跟随（瞳孔/头部朝向目标坐标）。

        Args:
            x, y: 屏幕全局坐标
        """
        ...

    # ── 变换 ──

    @abstractmethod
    def set_position(self, x: int, y: int) -> None:
        """设置角色位置（窗口位置）"""
        ...

    @abstractmethod
    def get_size(self) -> tuple[int, int]:
        """获取角色渲染尺寸 (width, height)"""
        ...

    @abstractmethod
    def set_scale(self, scale: float) -> None:
        """缩放"""
        ...

    @abstractmethod
    def get_scale(self) -> float:
        """获取当前缩放"""
        ...

    # ── 朝向 ──

    @abstractmethod
    def set_facing(self, right: bool) -> None:
        """设置朝向（True=右，False=左）"""
        ...

    @abstractmethod
    def get_facing(self) -> bool:
        """获取当前朝向"""
        ...

    # ── 状态查询 ──

    @property
    def character_id(self) -> str:
        return self._character_id

    @property
    def current_anim(self) -> str:
        return self._current_anim

    @property
    def current_emotion(self) -> str:
        return self._current_emotion
