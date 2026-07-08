"""SpriteRenderer - 2D 帧精灵渲染器

从 pet.py 提取的帧精灵渲染逻辑，实现 AvatarRenderer 接口。
管理帧加载、动画定时器、瞳孔 overlay、朝向翻转。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QPoint, QRect
from PySide6.QtGui import QPixmap, QPainter, QTransform, QColor, QImage
from PySide6.QtWidgets import QLabel, QWidget

from avatar.base import AvatarRenderer
from eye_overlay import EyeOverlay

logger = logging.getLogger(__name__)

# ── 帧扫描工具 ──

def scan_top_y(pixmap: QPixmap) -> int:
    """扫描 pixmap 中第一个非透明像素的 Y 坐标（用于气泡定位）"""
    if pixmap.isNull():
        return 0
    img = pixmap.toImage()
    w = img.width()
    h = img.height()
    # 采样而非逐像素（性能）
    step = max(1, w // 40)
    for y in range(h):
        for x in range(0, w, step):
            if img.pixelColor(x, y).alpha() > 10:
                return y
    return 0


class SpriteRenderer(AvatarRenderer):
    """2D 帧精灵渲染器。

    管理：
        - 帧序列加载（characters/<id>/frames/<anim>/_N.png）
        - 动画定时器（idle ~3fps, walk ~4fps）
        - 帧区间约束（情绪映射到 extra 帧子范围）
        - 瞳孔 overlay（鼠标跟随）
        - 朝向翻转（左右镜像）
        - 缩放

    用法：
        renderer = SpriteRenderer(parent_widget)
        renderer.load("ophelia")
        renderer.play_anim("idle")
    """

    def __init__(self, parent: QWidget):
        super().__init__()
        self._parent = parent

        # 角色图片 Label
        self.char_label = QLabel(parent)
        self.char_label.setAlignment(Qt.AlignCenter)
        self.char_label.setFixedSize(180, 250)
        self.char_label.move(10, 70)
        self.char_label.lower()
        self.char_label.installEventFilter(parent)

        # 瞳孔 overlay
        self._eye_overlay = EyeOverlay(parent)
        self._eye_overlay.setFixedSize(180, 250)
        self._eye_overlay.move(10, 70)
        self._eye_overlay.hide()
        self._eye_overlay.raise_()

        # 帧数据
        self._frames: dict[str, list[QPixmap]] = {}
        self._frame_tops: dict[str, list[int]] = {}
        self._anim_seq: str = 'idle'
        self._anim_idx: int = 0
        self._anim_range: tuple[Optional[int], Optional[int]] = (None, None)

        # 动画定时器
        self._anim_timer = QTimer(parent)
        self._anim_timer.timeout.connect(self._anim_tick)
        self._anim_timer.start(200)

        # 朝向
        self._facing_right: bool = True

        # 缩放
        self._scale: float = 1.0

    # ── 生命周期 ──

    def load(self, character_id: str) -> bool:
        """从 characters/<id>/frames/ 加载帧序列"""
        self._character_id = character_id
        self._frames = {}
        self._frame_tops = {}

        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        char_dir = os.path.join(base_dir, "characters", character_id, "frames")

        if not os.path.isdir(char_dir):
            logger.warning("Character directory not found: %s", char_dir)
            self._fallback_load(character_id)
            return False

        for seq_name in ("idle", "walk", "extra"):
            seq_dir = os.path.join(char_dir, seq_name)
            if not os.path.isdir(seq_dir):
                continue
            files = sorted(
                f for f in os.listdir(seq_dir)
                if f.endswith(".png")
            )
            if not files:
                continue

            frames = []
            tops = []
            for f in files:
                path = os.path.join(seq_dir, f)
                pix = QPixmap(path)
                if pix.isNull():
                    logger.warning("Failed to load frame: %s", path)
                    continue
                frames.append(pix)
                tops.append(scan_top_y(pix))

            if frames:
                self._frames[seq_name] = frames
                self._frame_tops[seq_name] = tops
                logger.info("Loaded %s: %d frames", seq_name, len(frames))

        if not self._frames:
            self._fallback_load(character_id)
            return False

        self._anim_seq = 'idle'
        self._anim_idx = 0
        self._anim_range = (None, None)
        self._show_frame()
        return True

    def _fallback_load(self, character_id: str):
        """加载失败时显示占位"""
        self._frames = {'idle': [self._make_placeholder(character_id)]}
        self._frame_tops = {'idle': [0]}
        self.char_label.setStyleSheet("color: #e6e6f0; font-size: 16px;")
        self.char_label.setText(f"[{character_id}]")
        self._show_frame()

    def _make_placeholder(self, name: str) -> QPixmap:
        """生成占位图"""
        px = QPixmap(180, 250)
        px.fill(Qt.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(60, 50, 80, 200))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(20, 30, 140, 190, 20, 20)
        p.setPen(QColor(200, 200, 220))
        p.setFont(self.char_label.font())
        p.drawText(QRect(20, 80, 140, 100), Qt.AlignCenter, name)
        p.end()
        return px

    def cleanup(self):
        """释放资源"""
        self._anim_timer.stop()
        self._eye_overlay.stop()
        self._frames.clear()
        self._frame_tops.clear()

    # ── 动画控制 ──

    def play_anim(
        self,
        anim: str,
        emotion: str = "",
        frame_range: Optional[tuple[int, int]] = None,
    ) -> None:
        """切换动画序列"""
        # 如果有 frame_range，直接使用
        if frame_range is not None:
            start, end = frame_range
            if anim in self._frames:
                self._anim_seq = anim
                self._anim_range = (start, end)
                self._anim_idx = start if start is not None else 0
                speed = 330 if anim == 'idle' else 250
                self._anim_timer.setInterval(speed)
                self._show_frame()
                self._current_anim = anim
                self._current_emotion = emotion
                return

        # emotion -> EXPRESSION_MAP 映射
        if emotion and emotion in EXPRESSION_MAP:
            mapped = EXPRESSION_MAP[emotion]
            if isinstance(mapped, tuple) and len(mapped) == 3:
                seq, start, end = mapped
                if seq in self._frames:
                    self._anim_seq = seq
                    self._anim_range = (start, end)
                    self._anim_idx = start if start is not None else 0
                    speed = 330 if seq == 'idle' else 250
                    self._anim_timer.setInterval(speed)
                    self._show_frame()
                    self._current_anim = seq
                    self._current_emotion = emotion
                    return

        # 无 emotion = 旧行为
        if anim != self._anim_seq and anim in self._frames:
            self._anim_seq = anim
            self._anim_range = (None, None)
            self._anim_idx = 0
            speed = 330 if anim == 'idle' else 250
            self._anim_timer.setInterval(speed)
            self._show_frame()
            self._current_anim = anim

    def set_emotion(self, emotion: str, intensity: float = 1.0) -> None:
        """设置情绪（触发帧区间切换）"""
        self._current_emotion = emotion
        # 通过 play_anim 实现
        self.play_anim(self._anim_seq, emotion=emotion)

    # ── 内部动画 ──

    def _anim_tick(self):
        """推进到下一帧"""
        frames = self._frames.get(self._anim_seq, [])
        if len(frames) > 1:
            start, end = self._anim_range
            if start is not None and end is not None:
                n = end - start + 1
                self._anim_idx = start + ((self._anim_idx - start + 1) % n)
            else:
                self._anim_idx = (self._anim_idx + 1) % len(frames)
            self._show_frame()

    def _show_frame(self):
        """渲染当前帧到 char_label"""
        frames = self._frames.get(self._anim_seq, [])
        if not frames:
            return
        pix = frames[self._anim_idx % len(frames)]
        ls = self.char_label.size()
        if ls.width() > 0 and ls.height() > 0:
            pix = pix.scaled(ls.width(), ls.height(),
                             Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if not self._facing_right:
            pix = pix.transformed(QTransform().scale(-1, 1))
        self.char_label.setPixmap(pix)

    # ── 视线 ──

    def look_at(self, x: int, y: int) -> None:
        """瞳孔 overlay 跟随坐标"""
        # EyeOverlay 自己定时获取光标位置，这里不需要主动调用
        pass

    def get_char_top_y(self) -> int:
        """获取角色头顶 Y 坐标（用于气泡定位）"""
        tops = self._frame_tops.get(self._anim_seq, [])
        if tops:
            idx = self._anim_idx % len(tops)
            return self.char_label.y() + int(tops[idx] * self._scale)
        return self.char_label.y()

    # ── 变换 ──

    def set_position(self, x: int, y: int) -> None:
        """角色 Label 位置不变（由窗口控制），只更新 overlay"""
        # char_label 的位置是相对窗口的，不随窗口移动
        pass

    def get_size(self) -> tuple[int, int]:
        """获取角色渲染尺寸"""
        return (self.char_label.width(), self.char_label.height())

    def set_scale(self, scale: float) -> None:
        """缩放角色和 overlay"""
        self._scale = scale
        cw = max(180, int(180 * scale))
        ch = max(250, int(250 * scale))
        self.char_label.setFixedSize(cw, ch)
        self.char_label.move(10, int(70 * scale))
        self._eye_overlay.setFixedSize(cw, ch)
        self._eye_overlay.move(10, int(70 * scale))
        self._eye_overlay.resize_for_character(cw, ch)
        self._show_frame()

    def get_scale(self) -> float:
        return self._scale

    def recalc_geometry(self, window_w: int, window_h: int):
        """窗口尺寸变化时重算角色尺寸"""
        cw = max(180, int(180 * self._scale))
        ch = max(250, int(250 * self._scale))
        self.char_label.setFixedSize(cw, ch)
        self.char_label.move(10, int(70 * self._scale))
        self._eye_overlay.setFixedSize(cw, ch)
        self._eye_overlay.move(10, int(70 * self._scale))
        self._eye_overlay.resize_for_character(cw, ch)
        self._show_frame()

    # ── 朝向 ──

    def set_facing(self, right: bool) -> None:
        self._facing_right = right
        self._show_frame()

    def get_facing(self) -> bool:
        return self._facing_right

    # ── 瞳孔 overlay 控制 ──

    def show_eyes(self):
        self._eye_overlay.start()

    def hide_eyes(self):
        self._eye_overlay.stop()

    @property
    def eye_overlay(self):
        return self._eye_overlay

    @property
    def label(self):
        return self.char_label


# 延迟导入 EXPRESSION_MAP（避免循环引用）
def _get_expression_map():
    try:
        from config import EXPRESSION_MAP
        return EXPRESSION_MAP
    except ImportError:
        return {}

# 模块级缓存
EXPRESSION_MAP = {}
try:
    from config import EXPRESSION_MAP
except ImportError:
    pass
