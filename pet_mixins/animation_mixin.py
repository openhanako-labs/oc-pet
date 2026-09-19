"""AnimationMixin — 桌宠动画（呼吸浮动 / 视线跟随 / 动画序列切换）。

由 PetWindow 多重继承。访问 self._renderer / self._physics / self._transition /
self._bob_frame / self._bob_offset / self._is_dragging / self._emotion_bob_factor /
self.char_label 等，均由 PetWindow 提供（鸭子类型，无需 import pet）。

拆分自 pet.py 的动画区块（原 838-970 行），降低 PetWindow 体积。
"""
import logging
import math
import time

logger = logging.getLogger(__name__)


class AnimationMixin:
    """动画逻辑：呼吸浮动、视线跟随、动画序列切换、帧推进。"""

    def _emotion_bob_factor(self) -> float:
        """按情绪调制呼吸浮动幅度（happy 更弹、sad 更稳）。"""
        return {
            "happy": 1.8,
            "surprised": 1.3,
            "sad": 0.3,
            "thinking": 1.0,
            "angry": 1.1,
            "neutral": 1.0,
        }.get(getattr(self, '_current_emotion', 'neutral'), 1.0)

    # ── 呼吸浮动 ──

    def _bob_tick(self):
        """呼吸浮动：角色上下轻微起伏（伸懒腰时增强）"""
        self._bob_frame += 1
        amp = 2.5 * self._emotion_bob_factor()
        if time.time() < getattr(self, '_stretch_until', 0.0):
            amp *= 2.2  # 伸懒腰：临时增强呼吸幅度
        self._bob_offset = int(math.sin(self._bob_frame * 0.06) * amp)
        if not self._is_dragging:
            ox = self._renderer._base_label_pos.x() + int(self._renderer._gaze_offset_x)
            oy = self._renderer._base_label_pos.y() + int(self._renderer._gaze_offset_y) + self._bob_offset
            # 只在位置变化时 move，避免不必要的重绘
            cur = self.char_label.pos()
            if cur.x() != ox or cur.y() != oy:
                self.char_label.move(ox, oy)

    # ── 视线跟随 ──

    def _gaze_tick(self):
        """视线跟随平滑更新（鼠标存在时跟随，否则回中）"""
        if not hasattr(self, '_renderer'):
            return
        params = self._mouse_reaction_params
        if params.gaze_enabled and self._mouse_tracker.is_nearby:
            state = self._mouse_tracker.state
            self._renderer.look_at(state.x, state.y)
        else:
            self._renderer.update_gaze()

    # ── 动画序列切换 ──

    def _set_anim_seq(self, seq_name, emotion=None, style="snap"):
        """切换动画序列，可选弹性/缓动过渡（去简陋感）。

        style:
            snap  - 瞬切（向后兼容，不透明度不变）
            fade  - ease-out 缓出淡入
            spring- 欠阻尼弹簧（惊讶/生气等弹一下）

        全程 try/except 兜底：过渡若异常，降级为 snap 瞬切，绝不崩溃。
        """
        try:
            # 2026-09-19：**必须看返回值**。这个契约渲染器早就写着
            # （“False 表示无匹配（调用方不得声称已触发）”），但没人守——
            # 切换失败时又继续把 _anim_seq/_anim_idx 抄成渲染器的值，状态就错开了。
            if not self._renderer.play_anim(seq_name, emotion=emotion):
                logger.warning("_set_anim_seq: %r 不是可用动作，已忽略（不切状态）", seq_name)
                return False
            self._anim_seq = self._renderer._anim_seq
            self._anim_idx = self._renderer._anim_idx
            self._anim_range = self._renderer._anim_range

            tr = getattr(self, '_transition', None)
            if tr is None or style == "snap":
                if tr is not None:
                    tr.reset(1.0)  # 确保全亮（snap 不做过渡）
                return True

            # fade / spring：先压暗再弹性淡入，表现“旧动作收尾、新动作登场”
            tr.reset(0.0)
            tr.go(1.0, style=style)
            return True
        except Exception:
            logger.exception("情绪过渡异常，降级 snap: %s", seq_name)
            try:
                self._renderer.play_anim(seq_name, emotion=emotion)
            except Exception:
                logger.debug("animation_mixin: 非致命异常(已静默吞掉)", exc_info=True)

    # ── 帧推进 / 渲染委托 ──

    def _anim_tick(self):
        """帧推进 - 委托给 SpriteRenderer"""
        logger.debug("_anim_tick called")
        self._renderer._anim_tick()
        self._anim_idx = self._renderer._anim_idx

    def _show_anim_frame(self):
        """渲染当前帧 - 委托给 SpriteRenderer"""
        self._renderer._show_frame()

    def _get_char_top_y(self):
        """获取角色头顶 Y 坐标 - 委托给 SpriteRenderer"""
        return self._renderer.get_char_top_y()
