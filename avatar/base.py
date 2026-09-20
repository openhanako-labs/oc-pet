"""AvatarRenderer 抽象接口 - 所有渲染形态的统一基类

后端业务逻辑（对话、行为、感知）只跟此接口交互，
不关心底层是帧精灵、Live2D 还是 VRM。

子类需实现所有 abstract 方法。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

# T08: MotionMixer 抽象层类型
from avatar.motion_mixer import Layer, MotionRequest, MotionMixer


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

    @abstractmethod
    def apply_action_intent(self, intent: dict) -> None:
        """应用结构化动作意图（任意动作；向后兼容 [emotion:xxx] 标签路径）。

        intent 形如::

            {"gesture": "wave", "intensity": 0.8,
             "params": {"ParamAngleX": 15, "ParamMouthOpenY": 0.6}}

        - ``gesture``: 语义动作名（映射 pet.json 的 motion/expression 或渲染器内置）。
        - ``intensity``: 0.0~1.0，动作幅度 / 参数强度系数。
        - ``params``: 可选，直接 Live2D 参数目标字典（精灵图等无参数概念的实现忽略）。

        实现要求：
        - Live2D：把 params 作为目标值，复用程序化表情层的每帧平滑插值
          过渡到目标（不要瞬间跳变）；gesture 名也可触发对应 motion/expression。
        - 精灵图：gesture 名 → 映射 ``frames/<anim>/`` 序列播放；params 字典忽略。
        - 参数为空 / intent 非法时必须安全忽略，不抛异常。
        """
        ...

    def set_master_emotion(self, emotion: str) -> None:
        """P2-6: 推送当前主导情绪（master emotion）到渲染器。

        默认实现只更新状态（不触发动作/手势），Live2DRenderer 重写为
        同步程序化表情层。精灵/VRM 渲染器继承本默认实现即可。
        """
        self._current_emotion = emotion or "neutral"

    def set_va_target(self, valence: float, arousal: float,
                      hold_sec: float = 3.0) -> bool:
        """推送**连续 VA 坐标**到渲染器（2026-09-20）。

        与 ``set_master_emotion``（离散情绪名）的区别：这个收连续值，
        由渲染器自己的插值层平滑过渡到目标参数。

        何时用：已有可靠连续情绪信号时（如 ``core/emotion_classifier.py``
        从文本分类出的 VAD）——比把离散情绪名查表成坐标更准。

        Args:
            valence: 效价 ∈[-1,1]（-1 很消极，+1 很积极）。
            arousal: 唤起 ∈[-1,1]（-1 很平静，+1 很兴奋）。
            hold_sec: 保持期内不被离散情绪覆盖（默认 3s）。

        Returns:
            是否采纳。默认实现返回 False（该渲染器不支持连续 VA）。
        """
        return False

    def set_procedural_smoothing(self, seconds: float) -> None:
        """P2-6: 配置程序化表情层插值时间常数（秒）。

        默认实现为空操作；Live2DRenderer 使用该值做面部参数平滑过渡。
        """
        return None

    # ── T08: MotionMixer 统一混流接口 ──

    def submit_motion_request(self, req: "MotionRequest") -> bool:
        """T08: 提交动作请求（经层优先级仲裁）。

        默认实现为 no-op（精灵/VRM 渲染器继承即可）；
        Live2DRenderer 重写为 mixer 仲裁 + motion/expression 播放。
        """
        return True

    def force_idle(self) -> None:
        """T08: 强制重置到 idle（替代外部直调 _force_idle）。

        默认实现为 no-op；Live2DRenderer 重写为 mixer.force_reset + _force_idle。
        """
        return None

    def play_emote_sequence(self, preset_name: str) -> bool:
        """T09: 播放 emote 预设序列。

        默认实现为 no-op（返回 False）；Live2DRenderer 和 SpriteRenderer 各自重写。
        """
        return False

    def get_motion_layer(self) -> "Layer":
        """T08: 获取当前活跃动作层。默认返回 IDLE。"""
        return Layer.IDLE

    def is_motion_idle(self) -> bool:
        """T08: 是否处于 idle 层（无高优先级动作在播）。默认返回 True。"""
        return True

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
