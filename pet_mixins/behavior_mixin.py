"""BehaviorMixin — 桌宠行为（用户交互标记 / 空闲/自言自语 / 前台 / 鼠标反应 / 屏幕感知）。

由 PetWindow 多重继承。访问 self._engine / self._perception / self._renderer /
self._physics / self._mouse_tracker / self._hanako_monitor / self._emotion_face /
self._state_mgr / self._pet_revert_timer / self.bubble / self._mouse_reaction_params /
self._show_bubble / self._set_anim_seq / self._set_surface_emotion / self._reposition_bubble
等，均由 PetWindow 提供（鸭子类型，无需 import pet）。

依赖方法（同样由 PetWindow / 其他 mixin 提供）：
  _mark_user_interaction / _show_bubble / _set_anim_seq / _set_surface_emotion /
  _pet_play_happy / _reposition_bubble / get_pos / _current_screen_geometry

拆分自 pet.py 的行为/感知区块（原 1965-2370 行），降低 PetWindow 体积。
"""
import logging
import random
import time

from PySide6.QtCore import QTimer

from config import EXPRESSION_MAP, get_transition_style
from core.event_bus import EventBus
from core.game.session import emit_session_events
from core.perception.scenarios import get_bubble_emotion_for_prompt

logger = logging.getLogger(__name__)


class BehaviorMixin:
    """行为逻辑：用户交互标记、空闲/自言自语、前台切换、鼠标反应、屏幕感知。"""

    # 鼠标反应冷却
    _mouse_reaction_cooldown: float = 0.0

    # ── 专注模式联动（P0-5：专注时降低 proactive 频率）──

    def _get_focus_manager(self):
        """返回注入的 FocusStateMachine 实例（T05 注入；未注入返回 None）。

        注意：方法名避免与注入属性 ``self._focus_manager``（FocusStateMachine
        实例）同名——否则实例属性会覆盖绑定方法，调用会得到
        ``'FocusStateMachine' object is not callable``（2026-08-20 启动崩溃）。
        鸭子类型访问：``self._focus_manager`` 由 PetWindow 注入。缺失时
        一律按"非专注"处理 → 零行为（满足 focus.enabled=false 零行为）。
        """
        return getattr(self, "_focus_manager", None)

    def _is_focus_active(self) -> bool:
        """专注模式是否处于 active 且已启用（线程安全由状态机保证）。

        ``FocusStateMachine.active`` 为 True 且 ``enabled`` 为 True 才算；
        两者任一不满足（默认关）都返回 False → 不抑制任何行为。
        """
        fm = self._get_focus_manager()
        if fm is None:
            return False
        try:
            return bool(fm.enabled and fm.active)
        except Exception:
            return False

    def _focus_suppresses_proactive(self) -> bool:
        """专注模式下是否抑制主动搭话/自言自语（默认抑制）。

        可由 ``config.focus.suppress_proactive=false`` 显式关闭抑制
        （保留专注视觉但允许搭话）；默认缺省为 True。
        """
        if not self._is_focus_active():
            return False
        try:
            cfg = getattr(self, "config", None) or {}
            return bool((cfg.get("focus", {}) or {}).get("suppress_proactive", True))
        except Exception:
            return True

    # ── 用户交互标记 ──

    def _mark_user_interaction(self):
        """记录用户活动，并让未送达的自言自语失效。"""
        self._last_interaction = time.time()
        self._last_user_interaction_mono = time.monotonic()
        self._idle_stage = None
        idle_chatter = getattr(self, "_idle_chatter", None)
        if idle_chatter:
            idle_chatter.reset()
        # 轻存在感：用户交互（拖拽/点击/对话/右键）重置空闲计时
        presence = getattr(self, "_presence", None)
        if presence is not None:
            try:
                presence.mark_interaction()
            except Exception:
                logger.debug("behavior_mixin: 非致命异常(已静默吞掉)", exc_info=True)

    # ── 空闲自言自语 ──

    def _can_idle_chatter(self) -> bool:
        """仅在桌宠和对话链都空闲时允许生成自言自语。"""
        # P0-5 专注模式：安静优先，抑制自言自语（主动搭话频率下降）
        if self._focus_suppresses_proactive():
            return False
        idle_chatter = getattr(self, "_idle_chatter", None)
        if not idle_chatter or not idle_chatter.enabled:
            return False
        if time.monotonic() - self._last_user_interaction_mono < idle_chatter.min_interval_sec:
            return False
        if not self.isVisible() or self._is_thinking or self._pending_chat:
            return False
        if getattr(self, "_voice_recording", False):
            return False
        if hasattr(self, "input_widget") and self.input_widget.isVisible():
            return False
        if hasattr(self, "_tts_player") and self._tts_player.is_playing():
            return False
        hanako_state = getattr(self._hanako_monitor, "current_state_name", "idle")
        if hanako_state in {"listening", "thinking", "working", "speaking"}:
            return False
        return True

    def _do_idle_chatter(self, text: str, emotion: str):
        """在 Qt 主线程显示自言自语，并应用情绪动画。"""
        if not text or not self._can_idle_chatter():
            logger.debug("Discarded stale idle chatter")
            return

        # P2: 解析 [emotion:xxx] 和 [action:{...}] 标签
        parsed_text, parsed_emotion = self._parse_emotion_tag(text)
        action_intent = self._parse_action_tag(text)
        
        emotion = parsed_emotion or emotion or "neutral"
        self._show_bubble(parsed_text, emotion=emotion)
        
        # P2: 触发动作（如果有 action 标签）
        if action_intent is not None:
            r = getattr(self, "_renderer", None)
            if r is not None and hasattr(r, "apply_action_intent"):
                try:
                    r.apply_action_intent(action_intent)
                except Exception as e:
                    logger.debug("Idle chatter action failed: %s", e)
        
        anim = EXPRESSION_MAP.get(emotion, EXPRESSION_MAP["neutral"])[0]
        # 收窄：surprised/angry 不切瞪眼帧，避免空闲自言自语高频瞪眼
        if emotion in ("surprised", "angry"):
            anim = "idle"
        self._set_anim_seq(anim, emotion=emotion, style=get_transition_style(emotion))

        self._current_emotion = emotion
        if emotion != "neutral":
            self._emotion_expiry_timer.start(3000)
        else:
            self._emotion_expiry_timer.stop()
        logger.info("Idle chatter: %s [emotion:%s]", text, emotion)
    
    @staticmethod
    def _parse_emotion_tag(text: str) -> tuple:
        """P2: 从文本解析 [emotion:xxx] 标签，返回 (cleaned_text, emotion)"""
        import re
        m = re.search(r'\[emotion:([a-z_]+)\]', text, re.IGNORECASE)
        if m:
            emotion = m.group(1).lower()
            cleaned = re.sub(r'\[emotion:[a-z_]+\]', '', text).strip()
            return cleaned, emotion
        return text, None
    
    @staticmethod
    def _parse_action_tag(text: str) -> dict | None:
        """P2: 从文本解析 [action:{...}] 标签，返回 action_intent 字典"""
        import re
        import json
        m = re.search(r'\[action:(\{.*?\})\]', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                logger.debug("behavior_mixin: 非致命异常(已静默吞掉)", exc_info=True)
        return None

    # ── 闲置检测 + 关怀提醒 ──

    def _break_check(self):
        """每 30 秒检查: idle 感知 + proactive 主动对话"""
        logger.debug("_break_check called")
        try:
            self._break_check_inner()
        except Exception as e:
            logger.error("_break_check error: %s", e)

    def _break_check_inner(self):
        now = time.time()
        idle_secs = now - self._last_interaction

        # idle 回归检测（用户回来时打招呼）
        if self._idle_stage is not None and idle_secs < 10:
            going = self._idle_stage
            self._idle_stage = None
            if going is not None:
                self._show_bubble("你回来啦~", emotion="happy")
        elif self._idle_stage is None and idle_secs >= 300:
            self._idle_stage = "idle"

        # Proactive 主动对话（P0-5：专注模式下降低频率——跳过 tick，让冷却自然拉长）
        if not self._focus_suppresses_proactive():
            try:
                if time.time() > self._proactive_grace:
                    # 2026-09-06: 接入统一调度器（DialogueScheduler）
                    if hasattr(self, '_scheduler') and self._scheduler is not None:
                        # 检查调度器是否允许触发
                        now = time.time()
                        if self._scheduler.request("proactive", "low", {}):
                            self._proactive.tick()
                    else:
                        self._proactive.tick()
            except Exception:
                logger.debug("behavior_mixin: 非致命异常(已静默吞掉)", exc_info=True)

        # 感知系统 tick(情绪衰减 + 主动对话 + 日程刷新)
        try:
            self._perception.tick()
        except Exception:
            logger.debug("behavior_mixin: 非致命异常(已静默吞掉)", exc_info=True)

        # P2-5 休息提醒（联动专注模式）：连续工作计时 + 深夜降频（防御式）
        try:
            tick = getattr(self, "_work_reminder_tick", None)
            if tick is not None:
                tick()
        except Exception:
            logger.debug("behavior_mixin: 非致命异常(已静默吞掉)", exc_info=True)

    # ── 前台窗口 ──

    def _foreground_tick(self):
        """每 2 秒检测前台窗口 + 活动感知"""
        logger.debug("_foreground_tick called")
        try:
            self._foreground_watcher.tick()
        except Exception as e:
            logger.error("_foreground_tick error: %s", e)
        # P0-1 陪玩：游戏不在前台时，顺便看它是不是已经关了（退出判定）
        gw = getattr(self, "_game_watch", None)
        if gw is not None:
            try:
                emit_session_events(gw.poll())
            except Exception:
                logger.debug("behavior_mixin: 非致命异常(已静默吞掉)", exc_info=True)
        # 活动感知：打字/划水/空闲（零成本，喂给 ProactiveScheduler）
        tracker = getattr(self, "_activity_tracker", None)
        if tracker is not None:
            try:
                tracker.tick()
            except Exception:
                logger.debug("behavior_mixin: 非致命异常(已静默吞掉)", exc_info=True)

    def _on_foreground_change(self, app_name: str, app_category: str, title: str):
        """前台窗口变化 → 重置 idle 计时器 + 窗口互动 + 事件触发截图"""
        going = self._idle_stage
        self._mark_user_interaction()
        if going is not None:
            self._show_bubble("你回来啦~", emotion="happy")

        # 窗口互动：桌宠靠近当前窗口（带冷却）——仅当显式开启 auto_walk 时触发。
        # 默认关闭：用户不希望在每次切换前台窗口时桌宠自动跳过去（位置漂移、
        # 像“偏左偏右”的困扰来源）。要恢复旧行为：设置里开“自动跟随窗口”。
        if hasattr(self, '_window_interaction'):
            wi_config = self.config.get('window_interaction', {})
            if wi_config.get('enabled', True) and wi_config.get('auto_walk', False):
                cooldown = wi_config.get('cooldown_seconds', 600)
                now = time.time()
                if not hasattr(self, '_last_move_near'):
                    self._last_move_near = 0
                if now - self._last_move_near >= cooldown:
                    try:
                        self._window_interaction.move_near_window()
                        self._last_move_near = now
                        EventBus.emit("window_interacted", target="window")
                    except Exception as e:
                        logger.debug("Window interaction failed: %s", e)

        # 事件触发截图：前台切换时触发一次屏幕感知（后台线程执行，不阻塞主线程）
        try:
            if hasattr(self, '_perception') and self._perception._screen:
                import threading as _threading
                _threading.Thread(
                    target=self._perception._screen.on_foreground_change,
                    args=(app_name, app_category, title),
                    daemon=True
                ).start()
        except Exception as e:
            logger.debug("Foreground screenshot trigger failed: %s", e)

    # ── 主动对话 ──

    def _on_proactive_trigger(self, prompt_text: str):
        """Proactive 调度器触发 -> 直接弹场景文案气泡 + 动作（P5 即时化）。

        P5 优化：proactive 文案本身就是桌宠要说的话，直接显示即可获得"即时感"，
        不再走 engine.send 等 2-5 秒 LLM 往返，也不把"[主动对话触发]…"指令包装
        以 user 身份写进 Hanako 会话历史（长期污染记忆）。LLM 不再参与本路径。
        """
        logger.info("Proactive trigger: %s", prompt_text)
        EventBus.emit("proactive_triggered", target="scheduler")

        # 场景文案 → 气泡情绪（不依赖 LLM 返回的 emotion 标签）
        emotion = get_bubble_emotion_for_prompt(prompt_text)

        # 直接显示场景文案气泡（prompt_text 即要显示的话，无需 LLM）
        # 2026-09-06: 添加触发来源标签（调试用）
        self._show_bubble(prompt_text, emotion=emotion, source="proactive")

        # 记录对话空闲计时：主动对话也算一次"对话"，让 proactive 冷却正常
        # （user_reply=False：proactive 自身触发后的记录，非用户回应，不重置
        #   _user_replied_since_last，也不触发冷却减半奖励）
        try:
            if getattr(self, "_perception", None) is not None:
                proactive = getattr(self._perception, "proactive", None)
                if proactive is not None:
                    proactive.mark_conversation(user_reply=False)
        except Exception:
            logger.debug("behavior_mixin: 非致命异常(已静默吞掉)", exc_info=True)

        # 触发动画：主动动作是用户的明确意图（proactive 调度器判定后触发），
        # 必须【无视 emotion 冷却】强制播放挥手/比心——否则屏幕感知反复推 happy
        # 进入冷却后，proactive 触发只会"闪过思考气泡"但角色继续 idle 摇摆，
        # 用户感受"没动作"（实际是手势被冷却抑制了）。
        # 做法：先 force_idle 清掉可能占用优先级的旧 motion，再用 NORMAL 优先级播放 waving。
        try:
            renderer = getattr(self, "_renderer", None)
            if renderer is not None:
                # T08: 通过 MotionMixer 提交 user_initiated 层请求，
                # 替代旧版「清空冷却表 + 伪造时间戳」的私有状态越界 bypass。
                if hasattr(renderer, "submit_motion_request"):
                    from avatar.motion_mixer import MotionRequest, Layer
                    # 2026-09-11：必须给 duration。原先缺省 0.0 = “永不过期”，
                    # mixer 会永久认为本动作在播（层=USER_INITIATED=4），
                    # 使 is_idle() 永远为 False、并让后续重播判定失真。
                    # 取值与帧管线的卡手势超时一致：非 idle motion 最多播这么久。
                    renderer.submit_motion_request(
                        MotionRequest(
                            layer=Layer.USER_INITIATED,
                            motion_group="waving",
                            can_interrupt=True,
                            duration=float(getattr(renderer, "GESTURE_TIMEOUT", 5.0)),
                            name="proactive_waving",
                        ),
                        fallback_motion="happy",
                    )
                elif hasattr(renderer, "_play_motion_kw"):
                    if not renderer._play_motion_kw("waving"):
                        renderer._play_motion_kw("happy")
                if hasattr(renderer, "set_emotion_expression_only"):
                    renderer.set_emotion_expression_only(emotion)
        except Exception as e:
            logger.debug("Proactive 主动动作触发失败: %s", e)
        # 同时记录 sprite 路径的 _anim_seq（保持向后兼容；动作沿用 waving）
        self._set_anim_seq("waving", emotion=emotion, style=get_transition_style(emotion))

    # ── 鼠标交互反应 ──

    def _get_window_rect(self) -> tuple[int, int, int, int] | None:
        """返回角色窗口 (x, y, w, h)，供 MouseTracker 使用"""
        p = self.pos()
        s = self.size()
        return (p.x(), p.y(), s.width(), s.height())

    def _check_reaction_cooldown(self) -> bool:
        """检查是否在反应冷却中（3 秒内不重复）"""
        now = time.time()
        if now - self._mouse_reaction_cooldown < 5.0:
            return True  # 冷却中
        self._mouse_reaction_cooldown = now
        return False

    def _on_mouse_nearby(self):
        """鼠标进入角色附近 - 只切动画，不弹气泡（用温和的好奇/开心，避免频繁惊讶）"""
        params = self._mouse_reaction_params
        if not params.react_nearby:
            return
        if self._is_thinking or self._check_reaction_cooldown():
            return
        self._set_anim_seq(params.nearby_anim, emotion="happy", style=get_transition_style("happy"))

    def _on_mouse_hover(self):
        """鼠标在角色附近静止（1.5s）→ 转向 + 气泡回应（“在看我吗~”之类）"""
        params = self._mouse_reaction_params
        if not params.react_hover:
            return
        if self._is_thinking or self._check_reaction_cooldown():
            return
        try:
            import random as _random
            lines = [
                "嗯？", "在看我吗~", "有什么事吗？", "摸我一下试试？",
                "发什么呆呢~", "要陪我玩吗？", "在看什么呢？",
            ]
            self._show_bubble(_random.choice(lines), emotion="thinking")
        except Exception:
            logger.debug("behavior_mixin: 非致命异常(已静默吞掉)", exc_info=True)
        self._set_anim_seq("idle", emotion="thinking", style=get_transition_style("thinking"))

    def _on_mouse_chase(self, target_x: int):
        """鼠标长时间不动，走过去并持续跟随光标"""
        params = self._mouse_reaction_params
        if not params.chase_enabled:
            return
        if self._is_thinking or self._physics.is_active:
            return
        x, _ = self.get_pos()
        sg = self._current_screen_geometry()
        target = max(10, min(target_x, sg.width() - self.width() - 10))
        self._motion_state = "chase"
        self._chasing = True
        self._chase_last_target = target
        self._physics.start_walk(target, facing_right=(target > x))
        # _unified_timer 已在初始化时启动，_update_chase 负责持续跟随

    # ── 待机微动作 / 随机散步活力 ──

    def _tick_idle_life(self):
        """待机时偶发微动作 + 有性格的表情节奏。"""
        # P0-5 专注模式：视觉安静——不做随机微动作/张望/伸展/Live2D 小动作
        if self._is_focus_active():
            return
        if getattr(self, '_is_dragging', False) or self._is_thinking:
            return
        if getattr(self, '_chasing', False) or self._physics.is_active:
            return
        if getattr(self, '_pet_cuddle', False):
            return
        if getattr(self, '_motion_state', 'idle') not in ('idle', 'rest'):
            return
        if getattr(self, 'bubble', None) and self.bubble.isVisible() and self.bubble.is_typing():
            return

        ef = getattr(self, "_emotion_face", None)
        # 表情脸静默冷却（秒），避免连续高频弹出
        self._idle_face_cd = getattr(self, "_idle_face_cd", 0.0) - 1.0

        self._idle_action_cd -= 1.0
        if self._idle_action_cd > 0:
            # 动作冷却中：极低概率自发一个微表情，让安静也「有生命」
            if ef is not None and self._idle_face_cd <= 0 and random.random() < 0.03:
                ef.flash("blink", 900)
                self._idle_face_cd = random.uniform(12, 22)
            return
        self._idle_action_cd = random.uniform(8, 20)  # 待机微动作节奏：约 8-20 秒一次

        # 低精力时偶尔打盹（先于其他微动作）
        emgr = getattr(self, '_state_mgr', None)
        energy = 100.0
        if emgr is not None:
            try:
                energy = float(emgr.save.energy)
            except Exception:
                energy = 100.0
        if energy < 25 and random.random() < 0.5:
            self._set_anim_seq("sleep", emotion="neutral", style="snap")
            self._pet_revert_timer.stop()
            self._pet_revert_timer.start(2600)
            return

        roll = random.random()
        if roll < 0.45:
            self._do_look_around()
            if ef is not None and self._idle_face_cd <= 0 and random.random() < 0.3:
                ef.flash("blink", 1000)
                self._idle_face_cd = random.uniform(10, 18)
        elif roll < 0.8:
            self._do_stretch()
            if ef is not None and self._idle_face_cd <= 0 and random.random() < 0.25:
                ef.flash("heh", 1100)  # 伸懒腰后的小得意
                self._idle_face_cd = random.uniform(10, 18)
        elif roll < 0.88:
            # Live2D 微动作：待机时偶发播放低优先级 motion（比心/挥手/唱歌等）
            self._do_live2d_mini_action()
        else:
            mood = 50
            mgr = getattr(self, '_state_mgr', None)
            if mgr is not None:
                try:
                    mood = float(mgr.save.mood)
                except Exception:
                    mood = 50
            if mood >= 55:
                # 心情好：摇摇但不弹机械 happy，改用害羞脸（更有人格）
                self._pet_play_happy(big=False, revert=1100, surface=False)
                if ef is not None and self._idle_face_cd <= 0:
                    ef.flash("shy", 1300)
                    self._idle_face_cd = random.uniform(14, 24)
            else:
                self._do_look_around()

    def _do_live2d_mini_action(self):
        """Live2D 待机微动作：偶发播放弱优先级 motion（比心/挥手/唱歌/思考）。

        合理性约束（避免“神经质”）:
        - 仅 idle/rest 且非说话/非思考/非拖拽时触发
        - 行为模式调制概率: quiet 0.4 / normal 0.7 / active 1.0 / cling 1.0
        - 触发后由 revert 定时器在动作结束后回 idle
        """
        if getattr(self, '_is_dragging', False) or self._is_thinking:
            return
        if getattr(self, '_chasing', False) or self._physics.is_active:
            return
        if getattr(self, '_motion_state', 'idle') not in ('idle', 'rest'):
            return
        # 说话/思考中不播（避免打断口型与气泡）
        if getattr(self, '_tts_player', None) is not None:
            try:
                if self._tts_player.is_playing():
                    return
            except Exception:
                logger.debug("behavior_mixin: 非致命异常(已静默吞掉)", exc_info=True)
        if getattr(self, 'bubble', None) and self.bubble.isVisible():
            return

        mode = getattr(self, '_behavior_mode', 'normal')
        mode_mult = {"quiet": 0.4, "normal": 0.7, "active": 1.0, "cling": 1.0}.get(mode, 0.7)
        if random.random() > mode_mult:
            return

        # 候选微动作（对应 renderer._ANIM_TO_MOTION_KW 的键；happy/touch 在 miku
        # 语义名模型下都有效，mail/complete/special 只在 lafei 风格模型有）
        acts = ["waving", "thinking", "happy", "touch", "mail", "special"]
        act = random.choice(acts)
        renderer = getattr(self, '_renderer', None)
        if renderer is not None and hasattr(renderer, 'play_anim'):
            try:
                renderer.play_anim(act, emotion="")
                self._pet_revert_timer.stop()
                self._pet_revert_timer.start(2200)
                logger.debug("Live2D 微动作: %s", act)
            except Exception:
                logger.debug("behavior_mixin: 非致命异常(已静默吞掉)", exc_info=True)

    def _do_look_around(self):
        """张望：先左后右再回正（复用视线平滑）"""
        if getattr(self, '_chasing', False):
            return
        renderer = getattr(self, '_renderer', None)
        if renderer is None or not getattr(renderer, '_gaze_enabled', False):
            return
        if self._looking_around:
            return
        self._looking_around = True
        petx, pety = self.get_pos()
        try:
            renderer.look_at(petx - 280, pety)
            QTimer.singleShot(450, lambda: renderer.look_at(petx + 280, pety))
            QTimer.singleShot(950, self._end_look_around)
        except Exception:
            self._looking_around = False

    def _end_look_around(self):
        self._looking_around = False
        renderer = getattr(self, '_renderer', None)
        if renderer is not None:
            try:
                renderer.reset_gaze()
            except Exception:
                logger.debug("behavior_mixin: 非致命异常(已静默吞掉)", exc_info=True)

    def _do_stretch(self):
        """伸懒腰：临时增强呼吸 bob 幅度"""
        self._stretch_until = time.time() + 1.4

    def _update_chase(self):
        """追逐中：持续跟随光标 X；贴脸或光标跑开则结束"""
        if self._is_thinking or getattr(self, '_is_dragging', False):
            self._chasing = False
            return
        tracker = self._mouse_tracker
        petx, _ = self.get_pos()
        # 光标跑远或快速移动 -> 放弃追逐
        if not tracker.is_nearby or tracker.state.speed > 1700:
            self._end_chase(happy=False)
            return
        cx = tracker.state.x
        if abs(cx - petx) <= 38:
            if not self._physics.is_active:
                self._end_chase(happy=True)
            return
        if abs(cx - self._chase_last_target) > 14:
            self._chase_last_target = cx
            self._physics.start_walk(cx, facing_right=(cx > petx))

    def _end_chase(self, happy=False):
        self._chasing = False
        self._motion_state = "idle"  # 复位，避免永久阻塞待机微动作
        if happy and not getattr(self, '_is_dragging', False):
            self._set_anim_seq('idle', emotion='happy', style='spring')
            self._set_surface_emotion('happy', duration_ms=900)
            self._show_bubble('找到你啦~', emotion='happy')
            self._pet_revert_timer.stop()
            self._pet_revert_timer.start(900)

    def _show_sticker(self, emoji: str, caption: str = ""):
        """显示大表情贴图（如摸头大反应的 💕）"""
        if not hasattr(self, 'bubble'):
            return
        self._is_thinking = False
        self._bubble_message = f"__sticker__{emoji}{caption}"
        try:
            self.bubble.set_sticker(emoji, caption)
            self._reposition_bubble()
            self.bubble.show()
            self.bubble.raise_()
            self._bubble_timer.start(6000)
        except Exception:
            logger.debug("behavior_mixin: 非致命异常(已静默吞掉)", exc_info=True)

    def _on_mouse_startled(self, speed: float):
        """鼠标快速掠过 - 只切动画"""
        params = self._mouse_reaction_params
        if not params.react_startle:
            return
        if self._is_thinking or self._check_reaction_cooldown():
            return
        self._set_anim_seq(params.startle_anim, emotion="surprised", style=get_transition_style("surprised"))

    def _on_mouse_leave(self):
        """鼠标离开角色附近"""
        self._renderer.reset_gaze()

    # ── 屏幕感知 ──

    def _on_screen_emotion(self, emotion: str, intensity: float):
        """屏幕内容触发的情绪（从后台线程调用，通过信号转主线程）"""
        self.screen_emotion_signal.emit(emotion, intensity)

    def _on_screen_proactive(self, prompt: str):
        """屏幕内容触发主动对话（从后台线程调用，通过信号转主线程）"""
        self.screen_proactive_signal.emit(prompt)

    def _do_screen_emotion(self, emotion: str, intensity: float):
        """在主线程处理屏幕情绪（带应用层冷却）"""
        try:
            now = time.time()
            if now - self._last_screen_emotion_at < self._screen_emotion_cooldown:
                return
            self._last_screen_emotion_at = now

            self._perception.trigger_emotion(emotion, intensity)
            EventBus.emit("screen_analyzed", emotion=emotion, intensity=intensity)
            # 统一从 config.EXPRESSION_MAP 取动画（权威映射，避免分叉）
            mapped = EXPRESSION_MAP.get(emotion)
            anim = mapped[0] if mapped else 'idle'
            # 收窄：surprised/angry 不切瞪眼帧，避免高频瞪眼
            if emotion in ('surprised', 'angry'):
                anim = 'idle'
            # P: Live2D 的 _frames 恒为空 dict（仅兼容占位），`anim in _frames` 恒为
            # False → 整块死代码，屏幕情绪对 Live2D 永远不生效。
            # 按渲染器类型判断 anim 是否支持：
            #   - Live2D（有 _model）：anim/emotion 命中 _ANIM_TO_MOTION_KW 或 anim 命中 _motion_groups
            #   - Sprite：anim 在 _frames 帧序列中才算支持
            renderer = getattr(self, "_renderer", None)
            if renderer is not None:
                if hasattr(renderer, "_model"):
                    kw_map = getattr(renderer, "_ANIM_TO_MOTION_KW", {})
                    supported = (
                        anim in kw_map
                        or emotion in kw_map
                        or anim in getattr(renderer, "_motion_groups", {})
                    )
                else:
                    supported = anim in getattr(renderer, "_frames", {})
                if supported:
                    self._set_anim_seq(anim, emotion=emotion, style=get_transition_style(emotion))
                    self._set_surface_emotion(emotion, duration_ms=3000, source="screen")
        except Exception:
            logger.debug("behavior_mixin: 非致命异常(已静默吞掉)", exc_info=True)

    def _do_screen_proactive(self, prompt: str):
        """在主线程处理屏幕内容主动对话

        显示思考状态，然后发送给对话引擎生成回复（会触发 TTS）。
        使用较短的超时（30 秒），超时后显示默认回复。
        """
        try:
            # 不显示原始提示词（那是内部 prompt，不是给用户看的）
            # 只显示思考状态
            self._show_bubble("🔍 正在观察...", emotion="thinking")
            self._is_thinking = True
            
            # 发送给对话引擎生成回复（会触发 TTS）
            if hasattr(self, '_engine') and self._engine:
                # 使用较短的超时（30 秒），避免长时间等待
                try:
                    self._engine.send(prompt, source="proactive")
                except Exception as e:
                    logger.error("Screen proactive LLM 调用失败: %s", e)
                    # 显示默认回复
                    self._show_bubble("你看了个有趣的视频啊～", emotion="happy")
            elif hasattr(self, '_conversation_engine') and self._conversation_engine:
                try:
                    self._conversation_engine.send(prompt)
                except Exception as e:
                    logger.error("Screen proactive LLM 调用失败: %s", e)
                    self._show_bubble("你看了个有趣的视频啊～", emotion="happy")
            
            # 记录日志
            logger.info("Screen proactive: %s", prompt[:80])
            
            # 触发动画：主动动作是用户的明确意图，必须【无视 emotion 冷却】强制播放挥手
            try:
                renderer = getattr(self, "_renderer", None)
                if renderer is not None:
                    if hasattr(renderer, "submit_motion_request"):
                        from avatar.motion_mixer import MotionRequest, Layer
                        # 2026-09-11：同 _trigger_proactive_waving，必须给 duration
                        # 否则 mixer 永久 stale（见那里的注释）。
                        renderer.submit_motion_request(
                            MotionRequest(
                                layer=Layer.USER_INITIATED,
                                motion_group="waving",
                                can_interrupt=True,
                                duration=float(getattr(renderer, "GESTURE_TIMEOUT", 5.0)),
                                name="screen_proactive_waving",
                            ),
                            fallback_motion="happy",
                        )
                    elif hasattr(renderer, "_play_motion_kw"):
                        if not renderer._play_motion_kw("waving"):
                            renderer._play_motion_kw("happy")
            except Exception as e:
                logger.debug("Screen proactive 主动动作触发失败: %s", e)
        except Exception as e:
            logger.error("Screen proactive failed: %s", e)
            self._show_bubble("你看了个有趣的视频啊～", emotion="happy")

    def _on_screen_update(self, description: str):
        """屏幕分析结果更新（后台线程回调，通过信号绕回主线程）"""
        self.screen_update_signal.emit(description)

    def _do_screen_update(self, description: str):
        """在主线程处理屏幕更新（记录日志等）"""
        logger.debug("Screen update: %s", description[:50])
