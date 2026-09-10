"""InteractionMixin — 桌宠交互（鼠标事件 / 拖拽 / 抚摸 / 边缘坐下）。

由 PetWindow 多重继承。访问 self._renderer / self._physics / self._motion /
self.config / self.char_label / self._heart_overlay / self._bubble 等，均由
PetWindow 提供（鸭子类型，无需 import pet）。

依赖方法（同样由 PetWindow / 其他 mixin 提供）：
  _mark_user_interaction / _set_anim_seq / _show_bubble / _set_surface_emotion /
  _flash_status_hud / _toggle_chat / _current_screen_geometry / _get_char_top_y

拆分自 pet.py 的交互区块（原 1686-2060 行），降低 PetWindow 体积。
"""
import logging
import math
import os
import time

from PySide6.QtCore import QEvent, Qt, QPoint
from PySide6.QtGui import QCursor, QTransform
from PySide6.QtWidgets import QGraphicsRotation, QGraphicsProxyWidget

from config import async_config_saver
from ui.sfx import play as sfx_play

logger = logging.getLogger(__name__)


class InteractionMixin:
    """交互逻辑：鼠标事件过滤、拖拽、抚摸/连击、边缘坐下、快速喂食。"""

    # 边缘吸附阈值 / 坐下旋转角
    SIT_THRESHOLD = 30
    SIT_ROTATE = 12

    # ── 鼠标事件过滤 ──

    def eventFilter(self, obj, event):
        if obj is self.char_label:
            t = event.type()
            import time as _time
            _t0 = _time.perf_counter()

            if t == QEvent.MouseButtonPress:
                if event.button() == Qt.LeftButton:
                    self._mark_user_interaction()
                    # 退出坐下状态
                    if self._is_sitting:
                        self._exit_sitting()
                    self._drag_start_cursor = QCursor.pos()
                    self._drag_start_window = self.pos()
                    self._is_dragging = False
                    self._was_click = True
                    # 抚摸手势：记录按下时刻与位置
                    self._pet_press_time = _time.perf_counter()
                    self._pet_press_pos = QCursor.pos()
                    self._pet_cuddle = False
                    self._pet_stroke_count = 0
                _elapsed = (_time.perf_counter() - _t0) * 1000
                if _elapsed > 16:
                    logger.warning("eventFilter[press] slow: %.1fms", _elapsed)
                return True

            elif t == QEvent.MouseMove:
                if (event.buttons() & Qt.LeftButton) and self._was_click:
                    self._stop_walking()
                    cursor = QCursor.pos()
                    delta = cursor - self._drag_start_cursor
                    if delta.manhattanLength() > 5 and not self._is_dragging:
                        self._is_dragging = True
                        sfx_play("pickup")
                        self._was_click = False
                        self._cancel_pending_click()
                        self.char_label.setCursor(QCursor(Qt.ClosedHandCursor))
                        self._drag_poll_timer.start(16)
                    if self._is_dragging:
                        self.move(self._drag_start_window + delta)
                    elif not self._is_dragging:
                        # 抚摸：按住不动 → 进入撸猫模式，小幅移动记一次抚摸
                        held = _time.perf_counter() - self._pet_press_time
                        moved = (cursor - self._pet_press_pos).manhattanLength()
                        if held > 0.3 and moved < 16:
                            if not self._pet_cuddle:
                                self._pet_cuddle = True
                                self._cancel_pending_click()
                            now = _time.perf_counter()
                            if now - self._pet_last_stroke > 0.22:
                                self._pet_last_stroke = now
                                self._on_pet_stroke()
                return True

            elif t == QEvent.MouseButtonRelease:
                if event.button() == Qt.LeftButton:
                    self._drag_poll_timer.stop()
                    if self._is_dragging:
                        self.char_label.setCursor(QCursor(Qt.ArrowCursor))
                        self._is_dragging = False
                        self._store_label_pos()

                        # ── 弹跳:释放时用滑动平均速度估算（平滑甩动） ──
                        if self._drag_vel_hist:
                            n = len(self._drag_vel_hist)
                            avg_vx = sum(v[0] for v in self._drag_vel_hist) / n
                            avg_vy = sum(v[1] for v in self._drag_vel_hist) / n
                            self._drag_vel_hist.clear()
                            vx = avg_vx * 0.02
                            vy = avg_vy * 0.02
                            speed = math.sqrt(vx ** 2 + vy ** 2)
                            if speed > 1.2:
                                self._bounce_active = True
                                self._is_walking = False
                                self._motion_state = "bounce"
                                self._set_anim_seq('walk')
                                self._physics.start_bounce(vx, vy)
                                sfx_play("bounce")
                            else:
                                self._bounce_active = False
                                sfx_play("drop")
                        else:
                            self._bounce_active = False

                        # ── 边缘吸附坐下 ──
                        edge = self._check_edge_sitting()
                        if edge and not self._bounce_active:
                            self._enter_sitting(edge)
                            self._was_click = False
                            return True

                        # 如果不在边缘，退出坐下状态
                        if self._is_sitting:
                            self._exit_sitting()

                        pos = self.pos()
                        self.config.setdefault("window", {})["x"] = pos.x()
                        self.config.setdefault("window", {})["y"] = pos.y()
                        # 异步防抖保存：释放瞬间不阻塞 GUI 线程（避免拖拽卡顿）
                        async_config_saver.schedule(self.config)
                        if self._on_position_change:
                            self._on_position_change(pos.x(), pos.y())
                    elif self._pet_cuddle:
                        # 撸猫会话结束（抚摸已在移动中累计）
                        self._pet_cuddle = False
                        self._cancel_pending_click()
                    elif self._was_click:
                        # 延迟触发聊天，留给双击(抚摸)取消
                        self._schedule_pending_click()
                        self._motion_state = "idle"
                    self._was_click = False
                _elapsed = (_time.perf_counter() - _t0) * 1000
                if _elapsed > 16:  # 超过一帧的时间才告警
                    logger.warning("eventFilter[release] slow: %.1fms", _elapsed)
                return True

            elif t == QEvent.MouseButtonDblClick:
                # 双击 = 抚摸（摸一下）；取消尚未触发的单击聊天
                self._cancel_pending_click()
                # P2: hit detection — 根据点击部位触发不同反应
                hit_area = None
                if hasattr(self, '_renderer') and hasattr(self._renderer, 'hit_detect'):
                    try:
                        # 获取点击位置（相对 char_label）
                        pos = event.globalPos() - self.char_label.mapToGlobal(QPoint(0, 0))
                        hit_area = self._renderer.hit_detect(pos.x(), pos.y())
                    except Exception:
                        logger.debug("interaction_mixin: 非致命异常(已静默吞掉)", exc_info=True)
                self._on_pet_pat(hit_area=hit_area)
                return True

        return super().eventFilter(obj, event)

    # ── 拖拽轮询 ──

    def _drag_poll_tick(self):
        """拖拽时每 16ms 轮询鼠标位置(不掉事件)"""
        if self._is_dragging:
            cursor = QCursor.pos()
            delta = cursor - self._drag_start_cursor
            self.move(self._drag_start_window + delta)
            # 记录用于释放后速度估算（用滑动平均，平滑甩动）
            now = time.time()
            dt = now - self._drag_last_time
            if dt > 0:
                vx = (cursor.x() - self._drag_last_pos.x()) / dt
                vy = (cursor.y() - self._drag_last_pos.y()) / dt
                self._drag_vel_hist.append((vx, vy))
                if len(self._drag_vel_hist) > self._drag_vel_hist_max:
                    self._drag_vel_hist.pop(0)
            self._drag_last_pos = cursor
            self._drag_last_time = now

    # ── 窗口边缘吸附坐下 ──

    def _check_edge_sitting(self) -> str | None:
        """检查是否靠近屏幕边缘，返回边缘方向或 None"""
        sg = self._current_screen_geometry()
        pos = self.pos()
        w, h = self.width(), self.height()
        x, y = pos.x(), pos.y()

        # 检查四条边
        if y <= sg.top() + self.SIT_THRESHOLD:
            return "top"
        if y + h >= sg.bottom() - self.SIT_THRESHOLD:
            return "bottom"
        if x <= sg.left() + self.SIT_THRESHOLD:
            return "left"
        if x + w >= sg.right() - self.SIT_THRESHOLD:
            return "right"
        return None

    def _enter_sitting(self, edge: str):
        """吸附到窗口边缘并进入坐下状态"""
        sg = self._current_screen_geometry()
        pos = self.pos()
        w, h = self.width(), self.height()
        x, y = pos.x(), pos.y()

        # 吸附到对应边缘
        if edge == "bottom":
            y = sg.bottom() - h
        elif edge == "top":
            y = sg.top()
        elif edge == "left":
            x = sg.left()
        elif edge == "right":
            x = sg.right() - w

        self.move(x, y)
        self._is_sitting = True
        self._sitting_edge = edge
        self._stop_walking()
        self._motion_state = "sitting"

        # 应用旋转效果（朝边缘方向倾斜）
        self._apply_sitting_rotation(edge)

        # 保存位置
        self.config.setdefault("window", {})["x"] = x
        self.config.setdefault("window", {})["y"] = y
        # 异步防抖保存（避免通用写盘阻塞）
        async_config_saver.schedule(self.config)
        if self._on_position_change:
            self._on_position_change(x, y)

        logger.info("Sitting on %s edge", edge)

    def _exit_sitting(self):
        """退出坐下状态"""
        if not self._is_sitting:
            return
        self._is_sitting = False
        self._sitting_edge = ""
        self._motion_state = "idle"

        # 移除旋转
        self.char_label.setGraphicsEffect(None)
        # 恢复帧渲染（仅 sprite renderer 有 _show_frame；Live2D 走自己的 draw loop，不需要）
        if hasattr(self._renderer, "_show_frame"):
            try:
                self._renderer._show_frame()
            except Exception:
                logger.debug("interaction_mixin: 非致命异常(已静默吞掉)", exc_info=True)

        logger.info("Stopped sitting")

    def _apply_sitting_rotation(self, edge: str):
        """坐下时应用视觉旋转效果"""
        from PySide6.QtWidgets import QGraphicsRotation, QGraphicsProxyWidget
        frames = self._renderer._frames.get(self._renderer._anim_seq, [])
        if not frames:
            return
        pix = frames[self._renderer._anim_idx % len(frames)]
        ls = self.char_label.size()
        if ls.width() > 0 and ls.height() > 0:
            pix = pix.scaled(ls.width(), ls.height(),
                             Qt.KeepAspectRatio, Qt.SmoothTransformation)

        # 根据边缘方向旋转
        angle = {
            "bottom": self.SIT_ROTATE,     # 底部：向右倾
            "top": -self.SIT_ROTATE,       # 顶部：向左倾
            "left": self.SIT_ROTATE,       # 左边：向右倾
            "right": -self.SIT_ROTATE,     # 右边：向左倾
        }.get(edge, 0)

        transform = QTransform()
        cx = pix.width() // 2
        cy = pix.height() // 2
        transform.translate(cx, cy)
        transform.rotate(angle)
        transform.translate(-cx, -cy)
        rotated = pix.transformed(transform, Qt.SmoothTransformation)
        if not self._renderer._facing_right:
            rotated = rotated.transformed(QTransform().scale(-1, 1))
        self.char_label.setPixmap(rotated)

    # ── 聊天交互 ──

    def _stop_walking(self):
        self._is_walking = False
        self._bounce_active = False
        self._physics.stop()
        # _unified_timer 保持运行（idle 时 tick 直接 return）
        self._motion.reset()
        self._set_anim_seq('idle')

    def _head_local_point(self) -> QPoint:
        """宠物头顶的窗口局部坐标（爱心/表情的原点）"""
        top = self._get_char_top_y()
        return QPoint(self.width() // 2, top)

    def _reposition_overlays(self):
        """把 HUD / 情绪脸定位到头顶，避免在屏幕边缘重叠"""
        if not hasattr(self, '_status_hud'):
            return
        top = self._get_char_top_y()
        cx = self.width() // 2
        face_h = self._emotion_face.height()
        hud_h = self._status_hud.height()

        # 情绪脸：头顶正上方
        face_y = max(top - face_h - 6, 2)
        self._emotion_face.move(cx - self._emotion_face.width() // 2, face_y)

        # 状态 HUD：
        #   - pinned（用户右键勾选常驻）→ 放头顶，方便随时查看
        #   - 临时闪现（喂食/抚摸）→ 放窗口右下角，不遮挡桌宠主体
        if getattr(self, '_hud_pinned', False):
            hud_y = top - hud_h - face_h - 12
            if hud_y <= face_h + 8:
                hud_x = cx + 10
                hud_y = max(top + 8, 2)
                if hud_x + self._status_hud.width() > self.width():
                    hud_x = max(cx - self._status_hud.width() // 2, 2)
            else:
                hud_x = cx - self._status_hud.width() // 2
            self._status_hud.move(hud_x, max(hud_y, 2))
        else:
            hud_x = max(self.width() - self._status_hud.width() - 6, 2)
            hud_y = max(self.height() - self._status_hud.height() - 6, 2)
            self._status_hud.move(hud_x, hud_y)

    # ── 抚摸 / 喂食 ──

    def _schedule_pending_click(self):
        """延迟触发单击=聊天，给双击(抚摸)留取消窗口"""
        self._pending_click = True
        self._click_timer.start(250)

    def _fire_pending_click(self):
        if self._pending_click:
            self._pending_click = False
            # T1-3: 触摸分层冷却 — 单击冷却 5 秒，避免频繁触发 LLM 往返
            now = time.perf_counter()
            if hasattr(self, '_last_click_time') and now - self._last_click_time < 5.0:
                logger.debug("触摸冷却中，跳过单击聊天: %.1fs", now - self._last_click_time)
                return
            self._last_click_time = now
            self._toggle_chat()
            self._motion_state = "idle"

    def _cancel_pending_click(self):
        if self._pending_click:
            self._pending_click = False
            self._click_timer.stop()

    # P3 交互反馈文案池（深化：按连击场景分层，避免千篇一律）
    _PAT_BUBBLES = {
        1: ["喵~", "呼噜呼噜~", "好舒服~", "摸头好开心~"],
        2: ["再摸摸嘛~", "头顶有点痒~", "嘿嘿…"],
        3: ["喵呜…要被摸成毛毯了", "这里这里，往上一点~"],
        4: ["就差一下了！", "快破纪录啦~"],
    }
    _PAT_BIG_TEXT = "最喜欢主人了！"
    _STROKE_BUBBLES = ["呜喵…", "咕噜咕噜…", "好困…像被按了关机键", "这里也要摸~"]

    def _on_pet_pat(self, hit_area: str | None = None):
        """双击 = 摸一下：开心反应 + 连击累计

        P2: hit_area 为点击部位（head/body/hair/eye/hand/foot），
        不同部位触发不同动作和气泡。
        """
        self._mark_user_interaction()
        sfx_play("pat")
        self._pet_combo += 1
        self._pet_combo_timer.start(2500)  # 2.5s 内继续摸算连击
        big = self._pet_combo >= 5
        if hasattr(self, '_state_mgr') and self._state_mgr:
            if big:
                self._state_mgr.apply_item_effect({"mood": 6, "likability": 3})
            else:
                self._state_mgr.apply_item_effect({"mood": 2, "likability": 1})
        # P2: 根据点击部位选择动作
        anim = self._pat_anim_for_area(hit_area)
        try:
            if hasattr(self._renderer, "play_anim"):
                self._renderer.play_anim(anim)
        except Exception:
            logger.debug("interaction_mixin: 非致命异常(已静默吞掉)", exc_info=True)
        self._pet_play_happy(big=big)
        head = self._head_local_point()
        self._heart_overlay.burst(count=6 if big else 2, x=head.x(), y=head.y())
        if big:
            self._show_sticker("💕", self._PAT_BIG_TEXT)
        else:
            import random
            pool = self._PAT_BUBBLES.get(min(self._pet_combo, 4), self._PAT_BUBBLES[1])
            # P2: 部位专属气泡
            area_bubble = self._area_bubble_for(hit_area)
            bubble_text = area_bubble or random.choice(pool)
            self._show_bubble(bubble_text, emotion="happy")

    def _pat_anim_for_area(self, hit_area: str | None) -> str:
        """P2: 点击部位 → 动画名映射"""
        if hit_area == 'eye':
            return 'surprised'  # 戳眼睛 → 惊讶
        elif hit_area == 'hand':
            return 'waving'  # 戳手 → 挥手
        elif hit_area == 'foot':
            return 'surprised'  # 戳脚 → 惊讶
        elif hit_area == 'body':
            return 'happy'  # 戳身体 → 开心
        elif hit_area == 'hair':
            return 'touch'  # 戳头发 → 摸头
        return 'touch'  # head 或默认 → 摸头

    _AREA_BUBBLES = {
        'eye': ["哎哟~ 眼睛不能戳!", "这里要温柔一点~"],
        'hand': ["抓到手啦~", "嗯? 摸到手了"],
        'foot': ["嘿嘿~ 痒痒的", "这里有敏感区哦"],
        'body': ["好舒服~", "喜欢这样~"],
        'hair': ["呼噜呼噜~", "头发要顺毛~"],
    }

    def _area_bubble_for(self, hit_area: str | None):
        """P2: 点击部位 → 专属气泡文案"""
        if hit_area and hit_area in self._AREA_BUBBLES:
            import random
            return random.choice(self._AREA_BUBBLES[hit_area])
        return None

    def _on_pet_stroke(self):
        """按住不动 = 连续撸：涓流心情 + 单颗爱心"""
        if hasattr(self, '_state_mgr') and self._state_mgr:
            self._state_mgr.apply_item_effect({"mood": 1, "likability": 0.5})
        self._pet_stroke_count += 1
        sfx_play("pet")
        self._pet_play_happy(big=False, revert=400, style="snap")
        head = self._head_local_point()
        self._heart_overlay.burst(count=1, x=head.x(), y=head.y())
        # P3：抚摸低频气泡（每 3 次一次），避免刷屏但增加反馈层次
        if self._pet_stroke_count % 3 == 0:
            import random
            self._show_bubble(random.choice(self._STROKE_BUBBLES), emotion="happy")

    def _pet_play_happy(self, big=False, revert=650, seq=None, style="spring", surface=True):
        if seq is None:
            seq = "jumping" if big else "waving"
        self._set_anim_seq(seq, emotion="happy", style=style)
        # happy 比常规交互更突出，使用相对增益，母线音量由设置页 sfx 滑块控制
        sfx_play("happy", gain=0.6 if big else 0.42)
        # surface=False：只做动画肢体反应，不弹情绪脸（用于 idle 自发摇摇，避免机械感）
        if surface:
            self._set_surface_emotion("happy", duration_ms=revert)
        self._pet_revert_timer.stop()
        self._pet_revert_timer.start(revert)

    def _pet_revert(self):
        self._set_anim_seq("idle", emotion="neutral", style="snap")

    def _reset_pet_combo(self):
        self._pet_combo = 0

    def _quick_feed(self):
        """免费基础投喂（已弃用）。

        养成系统移除后，该方法保留空壳以避免旧调用方报错。
        """
        logger.debug("_quick_feed is deprecated and does nothing")
