"""AnimationMixin — 桌宠动画（呼吸浮动 / 视线跟随 / 动画序列切换）。

由 PetWindow 多重继承。访问 self._renderer / self._physics / self._transition /
self._bob_frame / self._bob_offset / self._is_dragging / self._emotion_bob_factor /
self.char_label 等，均由 PetWindow 提供（鸭子类型，无需 import pet）。

拆分自 pet.py 的动画区块（原 838-970 行），降低 PetWindow 体积。

2026-09-20：PhysicsCallbacks 的四个动画相关回调（on_walk_finished /
on_bounce_finished / on_facing_change / set_anim）也从 pet.py 搬入这里——
它们的职责全是「把物理层的意图落到动画上」，本就属于动画域。
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
        # 2026-09-21：`extra` / 空 是上游约定的「**无动作**」哨兵
        # （`capability_registry.ToolResult.anim` 的默认值就是 "extra"，
        # a2a_capability / conversation_engine 也用同一个值），它**不是动作名**。
        # 以前原样丢给渲染器 → 每次两条「未知动作 'extra'」警告（53 分钟 51 次）。
        if seq_name in (None, "", "extra"):
            logger.debug("_set_anim_seq: %r 是「无动作」哨兵，跳过", seq_name)
            return True
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

    # ── PhysicsCallbacks：物理层意图 → 动画（2026-09-20 自 pet.py 搬入）──

    def on_walk_finished(self):
        """散步到达终点：回 idle、存位置、进休息状态。"""
        from config import async_config_saver

        self._is_walking = False
        self._idle_if_not_already()
        self._store_label_pos()
        pos = self.pos()
        self.config.setdefault("window", {})["x"] = pos.x()
        self.config.setdefault("window", {})["y"] = pos.y()
        # 异步防抖保存：散步每次到达都写盘会周期性卡顿，改走后台，
        # 且只交本窗口拥有的切片（整份快照会盖掉设置面板刚改的键）。
        async_config_saver.schedule({"window": {"x": pos.x(), "y": pos.y()}})
        if self._on_position_change:
            self._on_position_change(pos.x(), pos.y())
        params = self._get_behavior_params()
        self._motion._start_rest(params)
        # 散步到达后张望一下，更有生气（追逐中不抢戏）
        if not (getattr(self, '_chasing', False) or getattr(self, '_is_dragging', False)
                or self._is_thinking):
            self._do_look_around()

    def on_bounce_finished(self, x: int, y: int):
        """弹跳结束：复位运动状态并保存落点。"""
        from config import async_config_saver

        self._motion_state = "idle"
        self._bounce_active = False
        self.config.setdefault("window", {})["x"] = x
        self.config.setdefault("window", {})["y"] = y
        async_config_saver.schedule({"window": {"x": x, "y": y}})

    def on_facing_change(self, facing_right: bool):
        """朝向变化：同步给渲染器（atlas 方向动画靠它决定左右）。"""
        self._facing_right = facing_right
        self._renderer.set_facing(facing_right)

    def _idle_if_not_already(self):
        """回 idle，但**已经在 idle 就不重播**。

        2026-09-21。背景：没有 walk motion 的模型（miku 这类），多个**状态驱动**
        的入口"让桌宠走路/走完了"时都会要一次 idle —— 而每次触发都是真重播：
        把 idle 循环从头打断，还打一行 INFO。实测：53 分钟 761 行，占日志 25%。

        为什么必须收成一个方法：第一次我只堵了 `set_anim` 那一扇门，
        漏了 `on_walk_finished`（它也直接调 `_set_anim_seq('idle')`）——
        探针里 5/12 条就从那扇门上来。**同一个形状的入口有多少个，
        就得有多少个待修的洞；所以改成从源头定义一次。**

        只认「当前序列」不认模型名：有 walk motion 的模型此刻的序列是 'walk'，
        不会被拦，正常回 idle。
        """
        if getattr(self, "_anim_seq", None) == "idle":
            return
        self._set_anim_seq("idle")

    def set_anim(self, anim: str):
        """物理/行为层的动作请求入口（MotionStateMachine 的 set_anim 回调）。

        ## 2026-09-20 修正：atlas 判断依据错了对象

        原实现（在 pet.py）：

            if anim == 'walk':
                if 'running-right' in self._renderer._frames:
                    anim = 'running-right' if self._facing_right else 'running-left'
            self._set_anim_seq(anim)

        `_frames` 是 **sprite/atlas 渲染器**的帧表（`{序列名: [QPixmap]}`）。
        Live2D 渲染器也有 `_frames`，但它是**空 dict**（`live2d_renderer.py:578`）
        ——于是判断恒为 False，`'walk'` 原样传给 `_set_anim_seq`。

        而 miku 的 motion 只有 idle/happy/waving/angry/sad/thinking/touch，
        **没有 walk** → `play_anim('walk')` 报「未知动作」→ 每 12-17 秒一条警告。

        实测证据（`logs/oc_pet.log`）：

            walk  194 次（23:01-23:56，中位间隔 12s）
            extra  63 次
            紧邻后一行 100% 是「_set_anim_seq: 'walk' 不是可用动作」

        触发链：`MotionStateMachine.tick`（500ms）→ `walk_chance` 命中
        → `_start_walk` → `physics.start_walk` → `cb.set_anim('walk')` → 这里。

        ## 修法

        按**渲染器能力**判断，而不是猜它有没有某个帧名：
        - sprite/atlas：有 `running-right` 帧 → 按朝向换名（原行为不变）
        - Live2D：没有 walking 类 motion → 降级到 `idle`
          （走路的**位移**仍由 physics 驱动，只是没有走路动画可播；
            这比每 12 秒报一次“未知动作”干净得多）

        降级而非报错，是因为「模型没有走路动画」是**正常情况**，不是错误。
        """
        if anim == 'walk':
            frames = getattr(self._renderer, '_frames', None) or {}
            if 'running-right' in frames:
                # atlas/sprite：按朝向选左右帧（原行为）
                anim = 'running-right' if self._facing_right else 'running-left'
            elif hasattr(self._renderer, '_motion_files'):
                # Live2D：没有 walking 类 motion 就降级 idle，不报“未知动作”。
                # 有对应 motion 的模型（如 lafei 的 main_2）仍走原路径。
                motion_files = self._renderer._motion_files or []
                if not any('walk' in str(f).lower() for f in motion_files):
                    anim = 'idle'
        # 2026-09-21：状态驱动的"回 idle"统一走 `_idle_if_not_already` ——
        # 已经在 idle 就不再触发（物理层每步都会回调 set_anim('walk')）。
        # 注意：**故意重播**（菜单手动点同一个动作）不走这条路，
        # 它们带 emotion/style 或走后端 force_restart。
        if anim == "idle":
            self._idle_if_not_already()
            return
        self._set_anim_seq(anim)
