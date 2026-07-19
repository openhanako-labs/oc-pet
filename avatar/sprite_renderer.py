"""SpriteRenderer - 2D 帧精灵渲染器

从 pet.py 提取的帧精灵渲染逻辑，实现 AvatarRenderer 接口。
管理帧加载、动画定时器、视线跟随（精灵偏移）、朝向翻转。

视线跟随：根据鼠标位置微微偏移精灵，模拟"注视"效果。
"""
from __future__ import annotations

import logging
import math
import os
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QPoint, QRect
from PySide6.QtGui import QPixmap, QPainter, QTransform, QColor, QImage
from PySide6.QtWidgets import QLabel, QWidget, QGraphicsOpacityEffect

from avatar.base import AvatarRenderer

logger = logging.getLogger(__name__)

# ── 视线跟随参数 ──

GAZE_MAX_OFFSET_X = 4   # 水平最大偏移 (px)
GAZE_MAX_OFFSET_Y = 3   # 垂直最大偏移 (px)
GAZE_SMOOTHING = 0.15    # 平滑系数 (0-1, 越小越平滑)
GAZE_FLIP_THRESHOLD = 80  # 鼠标超过角色中心多远时自动翻转朝向 (px)


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
        - 视线跟随（精灵偏移模拟注视）
        - 朝向翻转（左右镜像）
        - 缩放

    用法：
        renderer = SpriteRenderer(parent_widget)
        renderer.load("yuexinmiao")
        renderer.play_anim("idle")
        renderer.look_at(mouse_x, mouse_y)  # 视线跟随
    """

    def __init__(self, parent: QWidget):
        super().__init__()
        self._parent = parent

        # 角色图片 Label
        self.char_label = QLabel(parent)
        self.char_label.setAlignment(Qt.AlignCenter)
        self.char_label.setFixedSize(192, 208)  # atlas 默认格子尺寸
        self.char_label.move(10, 0)
        self.char_label.lower()
        self.char_label.installEventFilter(parent)

        # 帧数据
        self._frames: dict[str, list[QPixmap]] = {}
        self._frame_tops: dict[str, list[int]] = {}
        self._seq_fps: dict[str, int] = {}
        self._emotion_ranges: dict[str, tuple[int, int]] = {}
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

        # 视线跟随状态
        self._gaze_offset_x: float = 0.0  # 当前平滑后的偏移
        self._gaze_offset_y: float = 0.0
        self._gaze_target_x: float = 0.0  # 目标偏移
        self._gaze_target_y: float = 0.0
        self._gaze_enabled: bool = True
        self._base_label_pos: QPoint = QPoint(10, 70)  # 角色 label 的基准位置

        # 透明度（情绪过渡：TransitionEngine 调用 set_alpha）
        # 用 QGraphicsOpacityEffect：不影响 _show_frame 的渲染逻辑
        self._opacity_effect: Optional[QGraphicsOpacityEffect] = None

    # ── 生命周期 ──

    def load(self, character_id: str, sprite_dir: str = None) -> bool:
        """加载角色 - 优先用 sprite_dir，回退到 characters/ 目录"""
        self._character_id = character_id
        self._frames = {}
        self._frame_tops = {}

        # 优先使用传入的 sprite_dir
        if sprite_dir and os.path.isdir(sprite_dir):
            char_dir = sprite_dir
        else:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            char_dir = os.path.join(base_dir, "characters", character_id)

        # 优先：pet.json
        pet_json_path = os.path.join(char_dir, "pet.json")
        if os.path.exists(pet_json_path):
            import json
            meta = json.loads(open(pet_json_path, encoding='utf-8').read())
            # 检测 atlas 格式（8x9 网格）
            if 'atlas' in meta:
                if self._load_from_atlas(char_dir, meta):
                    return True
            # 回退到旧的 spritesheet 格式
            if self._load_from_pet_json(char_dir, pet_json_path):
                return True
            logger.warning("pet.json load failed, falling back to frames/")

        # 回退：分帧 PNG 文件夹模式
        frames_dir = os.path.join(char_dir, "frames")
        if os.path.isdir(frames_dir):
            return self._load_from_frames_dir(frames_dir)

        logger.warning("Character directory not found: %s", char_dir)
        self._fallback_load(character_id)
        return False

    def _load_from_pet_json(self, char_dir: str, json_path: str) -> bool:
        """从 pet.json 加载 spritesheet"""
        try:
            import json
            meta = json.loads(open(json_path, encoding='utf-8').read())

            ss = meta.get('spritesheet', {})
            src = ss.get('src', 'spritesheet.png')
            frame_w = ss.get('frameWidth', 0)
            frame_h = ss.get('frameHeight', 0)
            self._scale = ss.get('scale', 1.0)

            sheet_path = os.path.join(char_dir, src)
            if not os.path.exists(sheet_path):
                logger.warning("Spritesheet not found: %s", sheet_path)
                return False

            sheet = QPixmap(sheet_path)
            if sheet.isNull():
                logger.warning("Failed to load spritesheet: %s", sheet_path)
                return False

            cols = sheet.width() // frame_w if frame_w > 0 else 1
            rows = sheet.height() // frame_h if frame_h > 0 else 1
            total = cols * rows

            # 切割所有帧
            all_frames = []
            all_tops = []
            for idx in range(total):
                col = idx % cols
                row = idx // cols
                x = col * frame_w
                y = row * frame_h
                pix = sheet.copy(x, y, frame_w, frame_h)
                if not pix.isNull():
                    all_frames.append(pix)
                    all_tops.append(scan_top_y(pix))

            if not all_frames:
                return False

            # 映射动画序列
            anims = meta.get('animations', {})
            for seq_name, seq_cfg in anims.items():
                start = seq_cfg.get('start', 0)
                count = seq_cfg.get('count', 0)
                if count > 0:
                    self._frames[seq_name] = all_frames[start:start + count]
                    self._frame_tops[seq_name] = all_tops[start:start + count]
                    fps = seq_cfg.get('fps', 3)
                    self._seq_fps[seq_name] = fps
                    logger.info("Loaded %s: %d frames (from spritesheet)", seq_name, count)

            # 情绪帧映射
            emotions = meta.get('emotions', {})
            self._emotion_ranges = {}
            for emo, emo_cfg in emotions.items():
                start = emo_cfg.get('start', 0)
                count = emo_cfg.get('count', 0)
                if count > 0:
                    self._emotion_ranges[emo] = (start, start + count - 1)

            if not self._frames:
                return False

            self._anim_seq = 'idle'
            self._anim_idx = 0
            self._anim_range = (None, None)
            self._show_frame()
            return True

        except Exception as e:
            logger.warning("pet.json load error: %s", e)
            return False

    def _load_from_atlas(self, char_dir: str, meta: dict) -> bool:
        """从 atlas 格式加载（8×9 网格，兼容 Codex hatch-pet）"""
        try:
            atlas_cfg = meta.get('atlas', {})
            src = atlas_cfg.get('src', 'atlas.png')
            columns = atlas_cfg.get('columns', 8)
            rows = atlas_cfg.get('rows', 9)
            cell_w = atlas_cfg.get('cellWidth', 192)
            cell_h = atlas_cfg.get('cellHeight', 208)
            self._scale = meta.get('scale', 1.0)

            atlas_path = os.path.join(char_dir, src)
            if not os.path.exists(atlas_path):
                logger.warning("Atlas not found: %s", atlas_path)
                return False

            sheet = QPixmap(atlas_path)
            if sheet.isNull():
                logger.warning("Failed to load atlas: %s", atlas_path)
                return False

            # 切割 atlas 为行×列
            all_rows = []
            for row in range(rows):
                row_frames = []
                for col in range(columns):
                    x = col * cell_w
                    y = row * cell_h
                    cell = sheet.copy(x, y, cell_w, cell_h)
                    if not cell.isNull():
                        row_frames.append(cell)
                all_rows.append(row_frames)

            # 映射动画序列
            anims = meta.get('animations', {})
            for anim_name, anim_cfg in anims.items():
                row_idx = anim_cfg.get('row', 0)
                frame_count = anim_cfg.get('frames', 6)
                fps = anim_cfg.get('fps', 3)
                if 0 <= row_idx < len(all_rows):
                    frames = all_rows[row_idx][:frame_count]
                    if frames:
                        self._frames[anim_name] = frames
                        self._frame_tops[anim_name] = [scan_top_y(f) for f in frames]
                        self._seq_fps[anim_name] = fps
                        logger.info("Loaded %s: %d frames (from atlas row %d)",
                                    anim_name, len(frames), row_idx)

            # 情绪映射
            emotions = meta.get('emotions', {})
            for emo, emo_cfg in emotions.items():
                anim_ref = emo_cfg.get('anim', '')
                if anim_ref:
                    self._emotion_ranges[emo] = anim_ref

            if not self._frames:
                return False

            self._anim_seq = 'idle'
            self._anim_idx = 0
            self._anim_range = (None, None)
            self._show_frame()
            logger.info("Atlas loaded: %d animations from %s", len(self._frames), src)
            return True

        except Exception as e:
            logger.warning("Atlas load error: %s", e)
            return False

    def _load_from_frames_dir(self, frames_dir: str) -> bool:
        """从 characters/<id>/frames/ 加载帧序列

        自动扫描所有子目录，不再硬编码序列名。
        用户只需在 frames/ 下创建 <序列名>/ 目录并放入 PNG，
        系统自动发现并加载。
        """
        for seq_name in sorted(os.listdir(frames_dir)):
            seq_dir = os.path.join(frames_dir, seq_name)
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

        # 确保 idle 存在作为默认序列
        if "idle" not in self._frames and self._frames:
            first = next(iter(self._frames))
            logger.info("No idle sequence, using '%s' as default", first)

        if not self._frames:
            self._fallback_load(self._character_id)
            return False

        self._anim_seq = 'idle' if 'idle' in self._frames else next(iter(self._frames))
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
        self._frames.clear()
        self._frame_tops.clear()

    # ── 透明度（供 TransitionEngine 调用） ──

    def set_alpha(self, alpha: float) -> None:
        """设置角色透明度 alpha ∈ [0,1]。

        供 core.emotion_transitions.TransitionEngine 回调。
        使用 QGraphicsOpacityEffect，不改 _show_frame 渲染逻辑。
        首次调用时懒构造 effect 并挂到 char_label。

        Args:
            alpha: 0=完全透明，1=完全不透明
        """
        if alpha < 0.0:
            alpha = 0.0
        elif alpha > 1.0:
            alpha = 1.0

        if self._opacity_effect is None:
            self._opacity_effect = QGraphicsOpacityEffect(self.char_label)
            self.char_label.setGraphicsEffect(self._opacity_effect)
        self._opacity_effect.setOpacity(alpha)

    def get_alpha(self) -> float:
        """查询当前透明度（无 effect 时返回 1.0）。"""
        if self._opacity_effect is None:
            return 1.0
        return self._opacity_effect.opacity()

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

        # emotion -> atlas 映射（字符串动画名）
        if emotion and hasattr(self, '_emotion_ranges') and emotion in self._emotion_ranges:
            ref = self._emotion_ranges[emotion]
            if isinstance(ref, str) and ref in self._frames:
                self._anim_seq = ref
                self._anim_range = (None, None)
                self._anim_idx = 0
                fps = self._seq_fps.get(ref, 3)
                self._anim_timer.setInterval(int(1000 / fps))
                self._show_frame()
                self._current_anim = ref
                self._current_emotion = emotion
                return

        # emotion -> EXPRESSION_MAP 映射（元组帧范围）
        if emotion and emotion in EXPRESSION_MAP:
            mapped = EXPRESSION_MAP[emotion]
            if isinstance(mapped, tuple) and len(mapped) == 3:
                seq, start, end = mapped
                if seq in self._frames:
                    # 安全检查：确保帧索引不越界
                    max_idx = len(self._frames[seq]) - 1
                    if start is not None and start > max_idx:
                        start = max_idx
                    if end is not None and end > max_idx:
                        end = max_idx
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
        """渲染当前帧到 char_label，应用视线偏移"""
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

    # ── 视线跟随 ──

    def look_at(self, x: int, y: int) -> None:
        """设置视线目标（全局坐标），驱动精灵偏移和朝向翻转

        在 200ms 定时器或鼠标事件中调用。
        """
        if not self._gaze_enabled:
            return

        # 计算角色中心（全局坐标）
        label_pos = self.char_label.mapToGlobal(QPoint(0, 0))
        cx = label_pos.x() + self.char_label.width() // 2
        cy = label_pos.y() + self.char_label.height() // 2

        dx = x - cx
        dy = y - cy
        dist = math.sqrt(dx * dx + dy * dy)

        if dist < 1:
            self._gaze_target_x = 0
            self._gaze_target_y = 0
        else:
            # 归一化后缩放到最大偏移
            norm_x = dx / max(dist, 1)
            norm_y = dy / max(dist, 1)
            # 距离越远偏移越大，但有上限
            strength = min(1.0, dist / 400.0)
            self._gaze_target_x = norm_x * GAZE_MAX_OFFSET_X * strength
            self._gaze_target_y = norm_y * GAZE_MAX_OFFSET_Y * strength

        # 平滑插值
        self._gaze_offset_x += (self._gaze_target_x - self._gaze_offset_x) * GAZE_SMOOTHING
        self._gaze_offset_y += (self._gaze_target_y - self._gaze_offset_y) * GAZE_SMOOTHING

        # 应用偏移到 label 位置
        ox = self._base_label_pos.x() + int(self._gaze_offset_x)
        oy = self._base_label_pos.y() + int(self._gaze_offset_y)
        self.char_label.move(ox, oy)

        # 自动翻转朝向（鼠标在另一侧且距离够远）
        # 行走中不翻转，避免与物理引擎的朝向冲突
        is_walking = getattr(self._parent, '_is_walking', False) or \
                     getattr(getattr(self._parent, '_physics', None), 'is_walking', False)
        if not is_walking and abs(dx) > GAZE_FLIP_THRESHOLD:
            should_face_right = dx > 0
            if should_face_right != self._facing_right:
                self._facing_right = should_face_right
                self._show_frame()

    def reset_gaze(self):
        """重置视线（鼠标离开时调用）"""
        self._gaze_target_x = 0
        self._gaze_target_y = 0
        # 平滑归零
        self._gaze_offset_x *= 0.5
        self._gaze_offset_y *= 0.5
        self.char_label.move(
            self._base_label_pos.x() + int(self._gaze_offset_x),
            self._base_label_pos.y() + int(self._gaze_offset_y),
        )

    def set_gaze_enabled(self, enabled: bool):
        """开关视线跟随"""
        self._gaze_enabled = enabled
        if not enabled:
            self.reset_gaze()

    def update_gaze(self):
        """每帧调用，平滑更新偏移（用于无新鼠标事件时的渐变归零）"""
        if abs(self._gaze_offset_x - self._gaze_target_x) > 0.1 or \
           abs(self._gaze_offset_y - self._gaze_target_y) > 0.1:
            self._gaze_offset_x += (self._gaze_target_x - self._gaze_offset_x) * GAZE_SMOOTHING
            self._gaze_offset_y += (self._gaze_target_y - self._gaze_offset_y) * GAZE_SMOOTHING
            ox = self._base_label_pos.x() + int(self._gaze_offset_x)
            oy = self._base_label_pos.y() + int(self._gaze_offset_y)
            self.char_label.move(ox, oy)

    def get_char_top_y(self) -> int:
        """获取角色头顶 Y 坐标（用于气泡定位）"""
        tops = self._frame_tops.get(self._anim_seq, [])
        if tops:
            idx = self._anim_idx % len(tops)
            return self.char_label.y() + int(tops[idx] * self._scale)
        return self.char_label.y()

    # ── 变换 ──

    def set_label_base_pos(self, pos: QPoint):
        """设置角色 label 基准位置（由 pet.py 在布局变化时调用）"""
        self._base_label_pos = pos

    def set_position(self, x: int, y: int) -> None:
        """角色 Label 位置不变（由窗口控制），只更新 overlay"""
        # char_label 的位置是相对窗口的，不随窗口移动
        pass

    def get_size(self) -> tuple[int, int]:
        """获取角色渲染尺寸"""
        return (self.char_label.width(), self.char_label.height())

    def set_scale(self, scale: float) -> None:
        """缩放角色 — 尺寸从实际帧数据计算"""
        self._scale = scale
        base_w, base_h = self._get_frame_size()
        cw = int(base_w * scale)
        ch = int(base_h * scale)
        self.char_label.setFixedSize(cw, ch)
        # 垂直居中偏移
        self.char_label.move(10, 0)
        self._base_label_pos = QPoint(10, 0)
        self._show_frame()

    def _get_frame_size(self) -> tuple[int, int]:
        """获取当前角色的帧原始尺寸"""
        if self._frames:
            first_seq = next(iter(self._frames))
            first_frame = self._frames[first_seq][0]
            return first_frame.width(), first_frame.height()
        return 192, 208  # atlas 默认格子尺寸

    def get_scale(self) -> float:
        return self._scale

    def recalc_geometry(self, window_w: int, window_h: int):
        """窗口尺寸变化时重算角色尺寸"""
        base_w, base_h = self._get_frame_size()
        cw = int(base_w * self._scale)
        ch = int(base_h * self._scale)
        self.char_label.setFixedSize(cw, ch)
        self.char_label.move(10, 0)
        self._base_label_pos = QPoint(10, 0)
        self._show_frame()

    # ── 朝向 ──

    def set_facing(self, right: bool) -> None:
        self._facing_right = right
        self._show_frame()

    def get_facing(self) -> bool:
        return self._facing_right

    # ── 兼容接口（已弃用 eye overlay）──

    @property
    def eye_overlay(self):
        """兼容旧代码，返回 None"""
        return None

    @property
    def label(self):
        return self.char_label

    def show_eyes(self):
        """兼容旧代码，现在由 look_at 驱动"""
        pass

    def hide_eyes(self):
        """兼容旧代码"""
        self.reset_gaze()


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
