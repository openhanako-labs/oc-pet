"""VRMRenderer - 3D VRM 角色渲染器（占位 / 规划中）

注意：VRM 与本项目 Q6 帧精灵格式【无关】。
    - Q6 是本桌宠的原生 2D 帧精灵格式（精灵图 + 网格 pet.json），由 SpriteRenderer 渲染。
    - 本文件是【独立的未来 3D 扩展钩子】，仅当角色 pet.json 声明 "format": "vrm"
      且目录含 .vrm 时由工厂路由到这里。当前未实现真实渲染。

设计目标（尚未实现）：
    用 QWebEngineView 承载一个 three.js 场景，加载 .vrm 模型，通过 QWebChannel 与
    Python 通信（TTS 口型 / 情绪 / 视线）。内存占用 150~300MB，比 Live2D 重，但能拿到
    真正的 3D 骨骼与物理（弹簧骨骼晃动）。

当前状态：
    仅实现 AvatarRenderer 接口骨架，load() 返回 False 并给出清晰提示，使渲染工厂能识别
    .vrm 格式但不崩溃。实际渲染待 Live2D 路线验证后推进。
"""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import QLabel, QWidget

from avatar.base import AvatarRenderer

logger = logging.getLogger(__name__)


class VRMRenderer(AvatarRenderer):
    """3D VRM 渲染器（占位）。"""

    def __init__(self, parent: QWidget):
        super().__init__()
        self._parent = parent
        # 占位 label（未来替换为 QWebEngineView 承载 three.js）
        self.char_label = QLabel(parent)
        self.char_label.setAlignment(Qt.AlignCenter)
        self.char_label.setFixedSize(192, 208)
        self.char_label.move(10, 0)
        self.char_label.lower()
        self._scale = 1.0
        self._facing_right = True
        self._base_label_pos = QPoint(10, 0)
        self._gaze_offset_x = 0.0
        self._gaze_offset_y = 0.0
        # 兼容属性（避免 pet.py 直接访问崩溃）
        self._frames: dict = {}
        self._frame_tops: dict = {}
        self._anim_timer = None
        self._anim_seq = "idle"
        self._anim_idx = 0
        self._anim_range = (None, None)
        self._opacity_effect: Optional[object] = None

    # ── 生命周期 ──

    def load(self, character_id: str, sprite_dir: str = None) -> bool:
        logger.warning(
            "VRMRenderer.load('%s'): VRM 渲染尚未实现（规划走 QWebEngineView + three.js）。"
            "请在设置中改用 Sprite 或 Live2D 角色。", character_id)
        self._character_id = character_id
        self.unsupported = True
        self.unsupported_reason = "VRM 渲染尚未实现，请改用 Q6 / Live2D 角色"
        self.char_label.setText(f"[VRM 未实现]\n{character_id}")
        self.char_label.setStyleSheet("color: #e6e6f0; font-size: 14px;")
        return False

    def cleanup(self) -> None:
        pass

    # ── 动画 / 情绪 / 视线 ──

    def play_anim(self, anim: str, emotion: str = "", frame_range=None) -> None:
        self._current_anim = anim

    def set_emotion(self, emotion: str, intensity: float = 1.0) -> None:
        self._current_emotion = emotion

    def apply_action_intent(self, intent: dict) -> None:
        """应用结构化动作意图（VRM：gesture 名 → play_anim；params 忽略）。

        VRM 渲染器仅实现基础播放，无直接参数概念，故 params 字典忽略；
        gesture 名直接作为动画名尝试播放，未识别时安全忽略。intent 非法安全返回。
        """
        if not isinstance(intent, dict):
            return
        gesture = intent.get("gesture")
        if gesture and isinstance(gesture, str):
            try:
                self.play_anim(gesture)
            except Exception:
                logger.debug("vrm_renderer: 非致命异常(已静默吞掉)", exc_info=True)

    def look_at(self, x: int, y: int) -> None:
        pass

    def set_gaze_enabled(self, enabled: bool) -> None:
        pass

    def update_gaze(self) -> None:
        pass

    def get_char_top_y(self) -> int:
        return self.char_label.y()

    # ── 变换 ──

    def set_position(self, x: int, y: int) -> None:
        pass

    def get_size(self) -> tuple[int, int]:
        return (self.char_label.width(), self.char_label.height())

    def set_scale(self, scale: float) -> None:
        self._scale = scale
        self.char_label.setFixedSize(int(192 * scale), int(208 * scale))

    def get_scale(self) -> float:
        return self._scale

    def recalc_geometry(self, window_w: int, window_h: int) -> None:
        self.char_label.setFixedSize(int(192 * self._scale), int(208 * self._scale))

    def set_facing(self, right: bool) -> None:
        self._facing_right = right

    def get_facing(self) -> bool:
        return self._facing_right

    def set_label_base_pos(self, pos: QPoint) -> None:
        self._base_label_pos = pos

    # ── 透明度 ──

    def set_alpha(self, alpha: float) -> None:
        alpha = max(0.0, min(1.0, alpha))
        self.char_label.setWindowOpacity(alpha)

    def get_alpha(self) -> float:
        return self.char_label.windowOpacity()

    # ── 兼容接口 ──

    @property
    def label(self):
        return self.char_label

    @property
    def eye_overlay(self):
        return None

    def show_eyes(self):
        pass

    def hide_eyes(self):
        pass
