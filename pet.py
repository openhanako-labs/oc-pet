"""桌面宠物主窗口"""
import os
import json
import math
import random
import time
import logging
import threading
import contextlib
from pathlib import Path
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QMenu, QDialog, QFormLayout,
    QSystemTrayIcon, QSlider, QStyle
)
from PySide6.QtCore import (
    Qt, QTimer, QPoint, QRect, QEvent, QThread,
    QPropertyAnimation, QEasingCurve, Signal
)
from PySide6.QtGui import (
    QPixmap, QPainter, QFont, QColor, QPen, QPainterPath,
    QFontMetrics, QAction, QIcon, QTransform, QImage,
    QCursor, QWheelEvent
)
from config import (CHARACTER_INFO, EXPRESSION_MAP, get_transition_style,
                    load_config, save_config, async_config_saver)
from core.hanako_monitor import (HanakoMonitor, clean_bubble_text,
                                 compact_bubble_text, looks_like_keyword_salad)
from core.idle_chatter import IdleChatter

from motion.behavior import BehaviorParams, BEHAVIOR_MODES
from motion.behavior import MOUSE_REACTIONS, MouseReactionParams
from motion.behavior import (
    PHYSICS_INTERVAL, INERTIA_FACTOR, INTENT_FACTOR,
    ARRIVAL_DISTANCE, WALK_SPEED_BASE,
    BOUNCE_ELASTICITY, BOUNCE_FRICTION, BOUNCE_GRAVITY, BOUNCE_MIN_SPEED
)
from ui.bubble import ChatBubble
from ui.activity_feed import ActivityFeed
from ui.heart_particles import HeartBurst
from ui.theme.palette import rgb, rgba
from ui.theme import get_default, rgb, rgba

from motion.action_linker import ActionLinker
from core.game.session import GameSessionWatcher, emit_session_events
from motion.foreground_watcher import ForegroundWatcher
from ui.tts_player import TTSTtsPlayer
from ui.streaming_pcm_player import StreamingPcmPlayer
from ui.startup_screen import StartupScreen
from core.perception import PerceptionController, ProactiveScheduler
from core.pet_audio_bridge import PetAudioBridge, PetAudioCallbacks, AudioType
from core.emotion_transitions import TransitionEngine
from motion.physics import PhysicsEngine, MotionStateMachine, PhysicsCallbacks
from avatar.factory import create_renderer
from avatar.decision_trace import trace
from ui.sfx import play as sfx_play

from core.conversation_engine import ConversationEngine
from motion.mouse_tracker import MouseTracker
from core.window_interaction import WindowInteraction
from pet_mixins.audio_mixin import AudioMixin
from pet_mixins.animation_mixin import AnimationMixin
from pet_mixins.interaction_mixin import InteractionMixin
from pet_mixins.chat_mixin import ChatMixin
from pet_mixins.behavior_mixin import BehaviorMixin
from pet_mixins.voice_provider_mixin import VoiceProviderMixin
from pet_mixins.play_mixin import PlayMixin
from pet_mixins.bubble_mixin import BubbleMixin
from pet_mixins.interface_mixin import InterfaceMixin
from pet_mixins.perception_mixin import PerceptionMixin
from pet_mixins.panels_mixin import PanelsMixin

logger = logging.getLogger(__name__)

# 延迟导入语音输入（依赖 sounddevice + whisper）
try:
    from voice_input import VoiceInput, preload_whisper
    _voice_available = True
except ImportError:
    _voice_available = False
    logger.info("VoiceInput not available (install sounddevice + whisper)")

# ─── 设置对话框 ─────────────────────────────────────────

class PetWindow(AudioMixin, AnimationMixin, InteractionMixin, ChatMixin, BehaviorMixin, VoiceProviderMixin, PlayMixin, BubbleMixin, InterfaceMixin, PerceptionMixin, PanelsMixin, QWidget):
    """透明桌面宠物窗口"""

    # 跨线程信号：后台线程 -> 主线程
    engine_reply_signal = Signal(str, str, str, str, object)  # reply, emotion, anim, audio_path, action_intent
    engine_status_signal = Signal(str, str)  # status message, emotion
    # P1: 流式 chunk 信号（边生成边显示气泡）
    engine_chunk_signal = Signal(str, str, str, int)  # chunk, accumulated, emotion, gen
    voice_status_signal = Signal(str)  # voice input status
    # 跨线程截停 TTS：ASR 后台线程不得直调 _tts_player（QMediaPlayer 是 COM 组件，
    # 跨线程调用会触发 RPC_E_SERVERCALL_RETRYLATER 0x8001010D），必须经信号绕回主线程。
    tts_stop_signal = Signal()  # 请求在主线程停止 TTS 播放
    # 2026-09-07: TTS 音频播放信号（供 BubbleMixin 的 speak() 回调使用）
    tts_audio_signal = Signal(str)  # audio_path
    # 2026-09-13: 流式 TTS 信号（Qwen3-TTS local 边合成边播）
    # kind: "begin" | "chunk" | "end"；payload: chunk 时为 PCM bytes
    tts_stream_signal = Signal(str, object, int)  # kind, payload, gen
    # 语音识别完成后（后台线程）的状态写入：_is_thinking/_pending_* 与
    # _record_topic 一并经信号挪到主线程执行，避免主线程同帧读到中间态（B3-2）。
    chat_state_signal = Signal(str)  # 语音输入文本 -> 主线程更新聊天状态
    screen_emotion_signal = Signal(str, float)  # emotion, intensity
    screen_proactive_signal = Signal(str)  # prompt
    screen_update_signal = Signal(str)  # screen analysis update (description)
    hanako_state_signal = Signal(str, str, str, str, str)  # anim, msg, emotion, state, audio_path
    idle_chatter_signal = Signal(str, str)  # text, emotion
    # M4: 工具进度（Hanako WS 模式下从 SessionManager.on_tool 转发过来）
    tool_progress_signal = Signal(str, str, str, object)  # tool_name, phase, display_text, success
    # 气泡：MultiPetBridge dispatcher / mission_tracker 等后台线程也会调 _show_bubble，
    # 而它内部要 start QTimer——必须先绕回主线程，否则 Qt 拒绝启动定时器。
    bubble_signal = Signal(str, str, int)  # text, emotion, priority
    # G celebrating 完工音：TTS 合成在后台线程完成，播放必须经信号回主线程
    # （QMediaPlayer 是 COM 组件，跨线程调用会触发 RPC_E_SERVERCALL_RETRYLATER 0x8001010D）。
    tts_celebration_signal = Signal(str)  # audio_path
    # F 本地状态口写转发：HTTP 线程只 emit 事件，经信号转主线程再驱动（不直连渲染线程）。
    pet_set_mode_signal = Signal(str)  # mode
    # T05 专注状态 → 主线程 UI（FocusStateMachine listener 可能在后台线程触发）
    focus_ui_signal = Signal(bool, float, object)  # active, charge, signals

    def __init__(self, agent_id: str = "yuexinmiao", sprite_dir: str = None,
                 position: dict = None, scale: float = 1.0,
                 on_position_change: callable = None,
                 agent_config: dict = None,
                 pet_manager=None):
        super().__init__()
        self.config = load_config()
        self._agent_config = agent_config or {}  # agent 级覆盖（含 tts/dialog，per-pet 独立配置）
        # per-pet 合并：agent 级 tts/dialog 覆盖全局（每个桌宠独立引擎+助手）
        try:
            self._merge_agent_config()
        except Exception as e:
            logger.warning("agent_config 合并失败: %s", e)
        self._agent_id = agent_id
        self._sprite_dir = sprite_dir  # None = 用默认 characters/ 目录
        self._on_position_change = on_position_change  # 位置变化回调
        self._pet_manager = pet_manager  # 多桌宠管理器引用
        self._init_position = position  # 初始位置（供 _setup_window 使用）
        self._pet_scale = scale
        # 屏幕对尺寸的钳制比例（1.0 = 未被限制）。
        # 滚轮气泡靠它报出**实际生效**的百分比，而不是屏幕上做不到的那个。
        self._screen_clamp = 1.0
        self._current_char = agent_id

        # ── 接线初始化（按语义分块，顺序与原 __init__ 完全一致，行为零变化）──
        self._init_diag_switches()
        self._init_states()
        self._init_schedulers()
        self._init_interaction()
        self._init_engine()
        self._init_voice_audio()
        self._init_mcp_server()
        self._init_visual_startup()
        # T05：N.E.K.O. 移植四线成果接入主循环（focus/chat_panel/memory_panel/proactive generator）
        self._init_neko_t05()
        # P2：互动层（小游戏邀请 / 音乐推荐 / 休息提醒）——防御式，失败不影响主功能
        self._init_play_layer()

    def _init_diag_switches(self):
        """P0 调试开关：环境变量禁用各模块（二分法定位 0x8001010d）。

        用法：OC_DISABLE_TRAY=1 / OC_DISABLE_PERCEPTION=1 / OC_DISABLE_LIVE2D=1
        让你能"禁托盘→禁感知→禁 Live2D，各跑一天"隔离崩溃根因。
        """
        self._diag_disable_tray = os.environ.get("OC_DISABLE_TRAY", "") == "1"
        self._diag_disable_perception = os.environ.get("OC_DISABLE_PERCEPTION", "") == "1"
        self._diag_disable_live2d = os.environ.get("OC_DISABLE_LIVE2D", "") == "1"
        if any((self._diag_disable_tray, self._diag_disable_perception, self._diag_disable_live2d)):
            logger.warning(
                "P0 调试开关生效: tray=%s perception=%s live2d=%s",
                self._diag_disable_tray, self._diag_disable_perception, self._diag_disable_live2d,
            )

    def _init_states(self):
        """交互/动画/Hanako/情绪等基础状态（与 __init__ 原顺序一致）。"""
        # ── 交互状态 ──
        self._drag_start_cursor = QPoint()
        self._drag_start_window = QPoint()
        self._is_dragging = False
        self._was_click = False
        self._drag_poll_timer = QTimer(self)
        self._drag_poll_timer.timeout.connect(self._drag_poll_tick)
        self._drag_last_pos = QPoint()   # 上一帧拖拽位置,用于速度计算
        self._drag_last_time = 0.0
        # 速度滑动平均窗口（拖拽甩动平滑）：保留最近 N 帧的速度，
        # 释放时取平均替代单帧，避免快速甩动时的初速度抖动。
        self._drag_vel_hist = []         # [(vx, vy), ...]
        self._drag_vel_hist_max = 5      # 窗口大小
        self._vy = 0.0                   # 垂直速度(弹跳用)
        self._bounce_active = False      # 弹跳模式中
        self._is_sitting = False         # 是否坐在窗口边缘
        self._sitting_edge = ""           # 坐在哪条边: top/bottom/left/right

        self._is_thinking = False

        self._pet_opacity = self.config.get("opacity", 1.0)
        self._behavior_mode = self.config.get("behavior", "normal")

        # ── 动画状态 ──
        self._bob_frame = 0
        self._bob_offset = 0
        self._label_base_pos = QPoint(0, 0)
        self._target_x = 0
        self._vx = 0.0
        # 合并 physics + bob + gaze 为单个定时器（减少事件循环压力）
        self._unified_timer = QTimer(self)
        self._unified_timer.timeout.connect(self._unified_tick)
        self._unified_timer.start(50)  # 50ms = 20fps，平衡流畅与性能
        self._motion_state = "idle"   # idle / wander / rest
        self._rest_counter = 0
        self._motion_timer = QTimer(self)
        # 守卫：与 _unified_tick 同因，MotionStateMachine 还没注入前 lambda 一调就崩。
        # 短路的 and 在 None 时直接返回 False，tick 不会执行。
        self._motion_timer.timeout.connect(
            lambda: getattr(self, '_motion', None) is not None
            and self._motion.tick(self._get_behavior_params())
        )
        self._motion_timer.start(500)
        self._is_walking = False

        # ── Hanako 状态监控 ──
        self._hanako_monitor = HanakoMonitor(on_state_change=self._on_hanako_state)

        # Hanako 状态轮询(文件桥接模式)
        self._hanako_poll_timer = QTimer(self)
        self._hanako_poll_timer.timeout.connect(self._hanako_monitor.tick)
        self._hanako_poll_timer.start(800)
        self._bubble_message = ""    # 当前气泡文字,用于超时隐藏
        self._bubble_priority = 0    # 当前气泡优先级（0=通知 1=对话回复）
        self._pending_bubbles = []   # 被高优先级顶掉的低优先级气泡队列
        self._bubble_timer = QTimer(self)
        self._bubble_timer.timeout.connect(self._clear_hanako_bubble)
        self._bubble_timer.setSingleShot(True)

        # ── 情绪过期定时器（A2: 3秒无新情绪回 idle）──
        self._emotion_expiry_timer = QTimer(self)
        self._emotion_expiry_timer.timeout.connect(self._on_emotion_expired)
        self._emotion_expiry_timer.setSingleShot(True)
        self._current_emotion = "neutral"
        self._emotion_source = "neutral"   # 当前情绪来源（缺陷①优先级）
        self._emotion_entered_at = 0.0      # 当前情绪进入时刻（time.monotonic 秒），供驻留迟滞判断
        # 屏幕情绪二次冷却，避免视觉模型反复输出同类关键词导致表情高频跳动
        self._screen_emotion_cooldown = 30.0  # 秒
        self._last_screen_emotion_at = 0.0

        # ── 空闲检查定时器 ──
        self._break_timer = QTimer(self)
        self._break_timer.timeout.connect(self._break_check)

    def _init_engine(self):
        """TTS provider / 对话引擎 / 信号连接 / 开场问候（与 __init__ 原顺序一致）。"""
        # ── TTS provider ──
        tts_provider = self._create_tts_provider()
        # 记下初始签名：设置页保存时若 TTS 配置未变，就不再重建 provider
        # （重建 cosyvoice 会在主线程拉起 torch/wetext 整条链，必须避免）
        self._tts_reload_gen = 0
        try:
            self._tts_provider_sig = self._tts_provider_signature()
        except Exception:
            self._tts_provider_sig = None

        # ── 检测是否是内置角色 ──
        is_builtin = False
        if self._pet_manager:
            for agent in self._pet_manager.agents:
                if agent["id"] == self._current_char:
                    is_builtin = agent.get("builtin", False)
                    break

        # ── 对话引擎（合并 bridge，单进程）──
        # F2/F5: 对话后端 agent 从配置 dialog.agent_id 读取（零硬编码）。
        # 未绑定（空）时回退到显示角色名（向后兼容），引导选择后再切换。
        _dlg_agent = ""
        try:
            # 顶部已全局 import load_config，这里直接用（避免遮蔽）
            _dlg_agent = (load_config().get("dialog", {}) or {}).get("agent_id", "") or ""
        except Exception:
            _dlg_agent = ""
        if not _dlg_agent:
            _dlg_agent = self._current_char
        self._engine = ConversationEngine(
            self._current_char, perception=self._perception,
            tts_provider=tts_provider, builtin=is_builtin,
            agent_id=_dlg_agent,
        )
        # P2-7: 挂接音色解析器（角色音色/情绪音色 → 每句合成前选音色；多桌宠各自独立）
        self._wire_voice_resolver()
        self._engine.on_reply = self._on_engine_reply
        self._engine.on_status = self._on_engine_status
        # BugFix #4：LLM 慢（工具链/tool-silent/慢模型）时心跳推送"还在想..."，
        # 复用 status 通道在主线程显示 thinking 气泡，避免 27s 静默无反馈。
        self._engine.on_progress = self._on_engine_status
        self._engine.on_tts_ready = lambda: logger.info("Engine TTS ready")
        # 2026-09-13: 流式 TTS 回调（Qwen3-TTS local 边合成边播）
        self._engine.on_tts_stream = self._on_engine_tts_stream
        # P1: 流式 chunk 回调（边生成边显示气泡）
        self._engine.on_llm_chunk = self._on_engine_chunk
        # M4: 桥接 on_tool_progress -> Qt Signal
        self._engine.on_tool_progress = lambda tool_name, phase, display_text, success: \
            self.tool_progress_signal.emit(tool_name, phase, display_text, success)
        self.tool_progress_signal.connect(self._do_tool_progress)
        # 后台线程调 _show_bubble 时经此信号绕回主线程（queued connection）
        self.bubble_signal.connect(self._show_bubble_impl)

        # 连接跨线程信号
        self.engine_reply_signal.connect(self._do_engine_reply)
        self.engine_status_signal.connect(self._do_engine_status)
        # P1: 流式 chunk 信号连接
        self.engine_chunk_signal.connect(self._do_engine_chunk)
        self.voice_status_signal.connect(self._do_voice_status)
        self.tts_stop_signal.connect(self._do_tts_stop)
        # 2026-09-07: TTS 音频播放信号（供 BubbleMixin 的 speak() 回调使用）
        self.tts_audio_signal.connect(self._do_play_tts_audio)
        # 2026-09-13: 流式 TTS 信号
        self.tts_stream_signal.connect(self._do_engine_tts_stream)
        self.chat_state_signal.connect(self._do_chat_state)
        self.screen_emotion_signal.connect(self._do_screen_emotion)
        self.screen_proactive_signal.connect(self._do_screen_proactive)
        self.screen_update_signal.connect(self._do_screen_update)
        self.hanako_state_signal.connect(self._do_hanako_state)
        # G：完工音播放（后台合成 → 信号回主线程；绝不直接碰 Qt/渲染）
        self.tts_celebration_signal.connect(self._do_tts_celebration)
        # F：本地状态口写转发（HTTP 线程 → 主线程驱动）
        self.pet_set_mode_signal.connect(self._do_pet_set_mode)
        self._engine.start()

        # ── 零配置开场问候（首次启动且未配 LLM 时，本地问候引导配置）──
        # 检测到无 LLM 配置时主动自我介绍，让新用户第一眼看到桌宠“活”。
        # 幂等：maybe_greet 内部检测标记文件，已问候过不再打扰。
        try:
            from core.greeting import maybe_greet, default_marker_path
            from config import CHARACTER_INFO
            _gname = self._current_char
            try:
                _ginfo = CHARACTER_INFO.get(self._current_char, {}) or {}
                _gname = _ginfo.get("name") or self._current_char
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
            _marker = default_marker_path(self._agent_id)
            _greet = maybe_greet(self.config, _marker, _gname)
            if _greet:
                _gtext, _gemotion = _greet
                # 等窗口稳定后弹气泡（主线程延迟）
                QTimer.singleShot(
                    1500,
                    lambda t=_gtext, e=_gemotion: self._show_bubble(t, emotion=e),
                )
                logger.info("开场问候（零配置引导）→ %s", _gtext[:36])
        except Exception as e:
            logger.debug("开场问候跳过: %s", e)

    def _init_voice_audio(self):
        """语音输入(ASR) / TTS 播放器 / 音频事件桥接（与 __init__ 原顺序一致）。"""
        # ── 语音输入（ASR）──
        asr_provider = self._create_asr_provider()
        self._voice_input = None
        self._voice_recording = False
        self._voice_continuous = False          # 持续监听模式
        self._voice_continuous_buffer = []      # 持续监听下的语音段缓存
        self._voice_continuous_silence = 0      # 连续静音帧计数
        self._voice_continuous_started = False  # 是否已检测到语音开始
        if _voice_available:
            # 传入 config.asr.device（默认空=系统默认麦克风；2026-08-22 新增）
            _asr_cfg = self.config.get("asr", {}) or {}
            self._voice_input = VoiceInput(
                asr_provider=asr_provider,
                device=_asr_cfg.get("device", ""),
            )
            self._voice_input._on_status = self._on_voice_status
            # 持续监听：VAD 回调（每帧音频数据到达时触发）
            self._voice_input.set_vad_callback(self._on_voice_vad)
            # 仅当 ASR 使用本地模型时才预加载；远程(mimo/api)走 API，
            # 不需要本地大模型，避免无意义地加载 torch/whisper 及下游依赖
            # （funasr/wetext 等），造成启动卡顿与运行时下载。
            # R1（2026-09-14）：sensevoice 也是本地模型，同样需预加载；
            # 但它 import 链实测 13.5s，**必须后台线程**（preload_whisper 已如此）。
            _asr_name = getattr(asr_provider, "name", "")
            if _asr_name == "whisper_local":
                preload_whisper()
            elif _asr_name == "sensevoice":
                try:
                    import threading as _threading
                    _threading.Thread(
                        target=asr_provider.preload,
                        name="SenseVoicePreload", daemon=True,
                    ).start()
                    logger.info("SenseVoice 预加载已入后台线程（import 链较重）")
                except Exception as e:
                    logger.warning("SenseVoice 预加载启动失败（非致命）: %s", e)

        # ── TTS 播放器 ──
        tts_cfg = self.config.get("tts", {})
        self._tts_player = TTSTtsPlayer()
        self._tts_player.set_volume(tts_cfg.get("volume", 0.8))
        if not tts_cfg.get("enabled", True):
            self._tts_player.disable()

        # ── 流式 PCM 播放器（Qwen3-TTS local 边合成边播）──
        self._stream_player = StreamingPcmPlayer(sample_rate=24000)
        self._stream_player.set_volume(tts_cfg.get("volume", 0.8))
        # 2026-09-14 预缓冲：实测合成速率仅 0.36x 实时（低于播放所需 1.0x），
        # 首块一到就开声卡会立即抽干缓冲 → 用户听到"两个字一卡"。
        #
        # 默认走**智能预缓冲**（由 _setup_lip_timeline 传入预期时长，
        # 算"撑到合成结束所需的量"——数学上永不断粮）。
        # 固定档（prebuffer_seconds）仅作为无文本信息时的降级手段，默认 0。
        _stream_cfg = tts_cfg.get("stream", {}) or {}
        try:
            self._stream_player.set_prebuffer(
                _stream_cfg.get("prebuffer_seconds", 0),
                _stream_cfg.get("prebuffer_max_wait_seconds", 12.0),
            )
        except Exception as e:
            logger.warning("预缓冲配置失败（用默认）: %s", e)
        if not tts_cfg.get("enabled", True):
            self._stream_player.disable()

        # TTS 口型回调
        self._tts_player.on_start = self._on_tts_start
        self._tts_player.on_end = self._on_tts_end
        self._tts_player.on_error = lambda msg: self._on_tts_end()
        self._stream_player.on_start = self._on_tts_start
        self._stream_player.on_end = self._on_tts_end
        self._stream_player.on_error = lambda msg: self._on_tts_end()

        # ── AUDIO-07: 桌宠音频事件桥接器 ──
        self._audio_bridge = PetAudioBridge(self)
        try:
            self._audio_bridge.connect()
            logger.info("AUDIO-07: PetAudioBridge connected")
        except Exception as e:
            logger.warning("AUDIO-07: Failed to connect bridge: %s", e)


    def _wire_mouth_level_source(self) -> None:
        """把流式播放器的实时音频电平接到渲染器口型（Live2D 专用）。

        2026-09-14：电平从"纯正弦开合"升级为"跟真实音量"。

        ⚠️ **必须用 _mouth_level_probe 而非 sp.current_level 直连**：
        `current_level()` 在未播放时返回 **0.0**（而非 None），
        而 `_update_mouth` 把 0.0 当作"真实静音"。
        后果：Edge / CosyVoice 等**文件式 TTS** 不走流式播放器，
        电平恒为 0.0 → 嘴全程锁在 0.08（几乎闭合，看着像不说话）。
        probe 在未播放时返回 None，让渲染器回退到正弦包络。
        """
        r = getattr(self, "_renderer", None)
        sp = getattr(self, "_stream_player", None)
        if r is None or sp is None or not hasattr(r, "set_mouth_level_source"):
            logger.debug("口型电平接线跳过（渲染器或播放器不支持）")
            return
        try:
            r.set_mouth_level_source(self._mouth_level_probe)
            logger.info("口型电平已接线（振幅驱动，非流式时回退正弦）")
        except Exception as e:
            logger.warning("口型电平接线失败（回退正弦）: %s", e)

    def _mouth_level_probe(self):
        """口型电平探测（2026-09-14 完善）：

        优先级：
          1. 流式播放器在播 → 返回真实 RMS（Qwen 流式 TTS，最准）
          2. 文件式播放器在播 → 返回位置包络（Edge/CosyVoice/MiMo/API，粗略但能动）
          3. 都没在播 → None（渲染器闭嘴或正弦兑底）

        为什么不能用 `is_playing()` 以外的判据：
        - 流式 `current_level()` 未播放时返回 0.0，与"真实静音"无法区分
        - 文件式 `current_level()` 未播放时返回 **None**（不是 0.0），语义正确
        """
        sp = getattr(self, "_stream_player", None)
        if sp is not None:
            try:
                if sp.is_playing():
                    return sp.current_level()
            except Exception:
                logger.debug("流式口型电平探测失败", exc_info=True)
        # 回落：文件式 TTS（Edge/CosyVoice/MiMo/API）——位置包络，比纯正弦更贴合音频起止
        fp = getattr(self, "_tts_player", None)
        if fp is not None and hasattr(fp, "current_level"):
            try:
                lvl = fp.current_level()
                if lvl is not None:
                    return lvl
            except Exception:
                logger.debug("文件式口型电平探测失败", exc_info=True)
        return None

    def _init_visual_startup(self):
        """渲染器/物理/UI/托盘/启动收尾（与 __init__ 原顺序一致）。"""
        # ── 帧动画状态(在 _setup_ui 后初始化)──
        self._anim_seq = 'idle'
        self._anim_idx = 0
        self._anim_range = (None, None)
        self._facing_right = True  # 当前朝向

        # 状态
        self._visible = True
        self._mousePassthrough = False

        self._setup_window()
        self._setup_ui()
        # ── 渲染器就绪后,同步动画状态别名 ──
        self._anim_frames = self._renderer._frames
        self._anim_frame_tops = self._renderer._frame_tops
        self._anim_timer = self._renderer._anim_timer
        # 守卫：部分渲染器（如 Live2D）不挂 QTimer 帧动画器，_anim_timer 为 None。
        # 直接 .timeout 会 AttributeError，这里与 _unified_tick/_motion_timer 同理短路。
        if self._anim_timer is not None:
            self._anim_timer.timeout.connect(self._anim_tick)
        self._setup_animation()

        # ── 情绪过渡引擎（依赖渲染器、不依赖动画帧动画计时器）──
        # 由 _unified_tick() 驱动 tick()，不新开 QTimer
        self._transition = TransitionEngine(self._renderer.set_alpha)

        # ── 物理引擎（委托）──
        self._physics = PhysicsEngine(self)
        self._motion = MotionStateMachine(self._physics, self)

        # ── 窗口互动模块 ──
        self._window_interaction = WindowInteraction(self)

        self._setup_menu()
        if self._diag_disable_tray:
            logger.warning("系统托盘 DISABLED via OC_DISABLE_TRAY=1")
        else:
            self._setup_tray()
        self.load_character(self._current_char)
        self._startup_screen.raise_()  # 确保启动画面在角色立绘之上
        self._break_timer.start(30000)  # 每 30 秒检查一次空闲
        self._foreground_timer.start(2000)  # 每 2 秒检测前台窗口

        # ── proactive 默认启用(由 config 控制)──
        # P2 拆分 fix：proactive_cfg 已在 _init_schedulers 存为 self._proactive_cfg
        if not self._proactive_cfg.get("enabled", True):
            self._proactive.disable()

        # ── 空闲时间追踪 ──
        self._current_anim = "idle"
        self._last_interaction = time.time()
        self._last_user_interaction_mono = time.monotonic()
        self._idle_stage = None

        # ── 恢复窗口状态 ──
        self._recalc_geometry()
        self.setWindowOpacity(self._pet_opacity)
        # ── 对话记忆跟踪 ──
        self._pending_user_msg = ""  # 等待配对的用户消息
        self._pending_emotion = "neutral"  # 等待配对的 emotion
        self._pending_chat = False  # 是否正在等待 Agent 回复
        # 2026-09-14：延后显示的气泡文本（等 TTS 开播时再显示，避免重复气泡）
        self._pending_bubble_text = ""
        self._pending_bubble_emotion = "neutral"

        # ── 空闲自言自语 ──
        self.idle_chatter_signal.connect(self._do_idle_chatter)
        self._idle_chatter = IdleChatter(
            llm_adapter=getattr(self._engine, "_adapter", None),
            on_chatter=lambda text, emotion: self.idle_chatter_signal.emit(text, emotion),
            min_interval_sec=120,
            max_interval_sec=600,
            character_id=self._current_char,
        )
        # 注入 agent 身份（异步，不阻塞启动）
        self._inject_agent_identity()
        # F5: 首次启动引导——若 dialog.agent_id 未绑定且服务端无同名 agent，
        # 自动绑定到第一个可用 agent（零硬编码，不静默乱落）
        self._ensure_dialog_agent()

        # ── P2 关系：陪伴记忆 + 每日首启问候 ──
        self._init_companion_memory()

        # ── P3 表现：多宠打招呼（订阅 pet_enter，另一只宠上线时响应）──
        self._init_multi_pet_greeting()

    # ────────────────────────────────────────────────────────────
    # T05：N.E.K.O. 移植四线成果接入主循环（P0-1/P0-2/P0-5/P0-6/P0-7/P0-8）
    # ────────────────────────────────────────────────────────────

    def _init_neko_t05(self):
        """把 T02/T03/T04 四线成果接进 PetWindow 主循环。

        职责（全部防御式接线，任何一条失败都不影响既有功能）：
          1. T02 P0-1：ProactiveScheduler 注入 ProactiveGenerator（Hanako 通道）
          2. T04 P0-5：创建 FocusScorer+FocusStateMachine → self._focus_manager
             （BehaviorMixin._focus_suppresses_proactive 自动读取；专注降频生效）
          3. T04 P0-6/P0-7：ChatPanel 窗口 + message_submitted/close_requested 接线
             + set_thinking/set_focus_active 联动
          4. T04 P0-8：MemoryPanel 窗口挂到右键菜单「管理」组
          5. 专注信号 → ChatPanel 辉光：focus 状态变化经 listener 回主线程更新

        线程约束：FocusStateMachine 的 listener 可能在后台线程触发（对话引擎回调），
        因此 focus → UI 的更新经 Qt Signal（focus_ui_signal）绕回主线程。
        """
        # 5. 专注 → ChatPanel 辉光信号（后台线程回调 → 主线程更新 UI）
        self.focus_ui_signal.connect(self._on_focus_ui_changed)

        # 2. 专注核心（FocusScorer + FocusStateMachine）
        try:
            from core.perception.focus import create_focus_core
            self._focus_scorer, self._focus_manager = create_focus_core()
            self._focus_manager.add_listener(self._on_focus_state_changed)
            logger.info("T05 focus core ready | enabled=%s", self._focus_manager.enabled)
        except Exception as e:
            logger.warning("T05 focus core 初始化失败（专注模式禁用）: %s", e)
            self._focus_scorer = None
            self._focus_manager = None

        # 1. ProactiveGenerator 注入（复用引擎的 HanakoPetAdapter，source="proactive"）
        self._inject_proactive_generator()

        # 3+4. ChatPanel / MemoryPanel（延迟到 UI 初始化完成后创建）
        self._init_neko_panels()

        # P1 集成：反重复 / 屏幕感知升级 / 事实库 / 反思引擎 / 向量嵌入确认
        # （全部防御式，任何一线失败不影响既有功能）
        self._init_neko_p1()

    def _inject_proactive_generator(self):
        """T02 P0-1：把 LLM 生成器注入 ProactiveScheduler。

        生成器复用 ConversationEngine 创建的 HanakoPetAdapter（source="proactive"
        走 chat_direct 直连，不写 Hanako 会话历史）。config proactive.llm_generation
        在 scheduler.load_config 已读取；未启用 / 无适配器 / 未配置 LLM 时静默跳过
        （回退模板池——避免把"(模型未配置)"当生成结果投递）。
        """
        try:
            from core.perception.proactive_generation import ProactiveGenerator
            adapter = getattr(getattr(self, "_engine", None), "_adapter", None)
            if adapter is None:
                logger.info("T05 proactive generator: 无 LLM 适配器，保持模板池")
                return
            if not getattr(self, "_proactive", None):
                return
            # 仅当适配器已配置 LLM（base_url + api_key 齐备）才注入；
            # 否则 chat_direct 返回的"(模型未配置)"会被当成生成结果。
            _base = (getattr(adapter, "_base_url", "") or "").strip()
            _key = (getattr(adapter, "_api_key", "") or "").strip()
            if not _base or not _key:
                logger.info("T05 proactive generator: LLM 未配置，保持模板池")
                return
            gen = ProactiveGenerator(adapter=adapter, use_qt_bridge=True)
            self._proactive.set_generator(gen)
            logger.info("T05 proactive generator 注入成功（llm_generation=%s）",
                        self._proactive._llm_generation)
        except Exception as e:
            logger.warning("T05 proactive generator 注入失败（回退模板池）: %s", e)

    def _on_fact_store_changed(self, result: dict):
        """主线程：事实库变化通知（日志；后续可接记忆面板刷新）。"""
        try:
            added = int(result.get("added", 0) or 0)
            if added:
                logger.info("[FactStore] added=%d facts", added)
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

    def _record_conversation_facts(self, text: str) -> None:
        """对话记忆写入点：engine.send 后把用户文本交给 FactStore 抽取事实。

        后台线程 LLM 抽取（FactStore.record_text 自带 Qt 信号回主线程），
        任何失败静默跳过，绝不阻塞对话链路。
        """
        store = getattr(self, "_fact_store", None)
        if store is None or not text or not str(text).strip():
            return
        try:
            store.record_text(
                str(text).strip(),
                extra_context="对话",
                evidence=[{"source": "conversation", "ts": time.time()}],
            )
        except Exception as exc:
            logger.debug("P1 对话事实记录跳过: %s", exc)

    def _on_reflection_changed(self, result: dict):
        """主线程：反思引擎变化通知（日志；后续可接记忆面板刷新）。"""
        try:
            added = int(result.get("added", 0) or 0)
            if added:
                logger.info("[Reflection] added=%d insights", added)
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

    def _maybe_reflect(self):
        """主线程慢 tick（60s）：按周期触发反思。

        ``schedule_reflect`` 把 LLM 工作放后台线程，结果经 Qt 信号回主线程，
        不阻塞主线程（0x8001010D 约束）。

        2026-09-14 避让：**对话进行中不发起反思**。
        实测反思一次吃 29s / 12623 token（日志：`source=memory_reflect`），
        与用户回复抢同一条 API——用户感知为"回复变慢"。
        反思是 24h 周期任务，晚一分钟无所谓。
        """
        engine = getattr(self, "_reflection_engine", None)
        if engine is None:
            return
        # 用 getattr 兜底：调用方可能未继承完整 mixin（如测试桩），
        # 拿不到避让判据时按“不忙”处理（宁可多跑一次反思，也不要崩）。
        busy_fn = getattr(self, "_is_conversation_busy", None)
        if callable(busy_fn):
            try:
                if busy_fn():
                    logger.debug("反思避让：对话进行中，跳过本轮")
                    return
            except Exception:
                logger.debug("对话忙判断失败，按不忙处理", exc_info=True)
        try:
            engine.schedule_reflect()
        except Exception as exc:
            logger.debug("P1 反思调度失败（非致命）: %s", exc)

    def _is_conversation_busy(self) -> bool:
        """对话是否正在进行（供后台 LLM 任务避让）。

        判据（任一为真即算忙）：
        - 正在等 Agent 回复（`_pending_chat`）
        - 正在流式/普通播 TTS

        后台任务（反思/屏幕感知）与用户回复抢同一条 API 是实测的延迟来源，
        这里给它们一个统一的"让路"信号。
        """
        if getattr(self, "_pending_chat", False):
            return True
        try:
            sp = getattr(self, "_stream_player", None)
            if sp is not None and sp.is_playing():
                return True
        except Exception:
            logger.debug("判断流式播放状态失败", exc_info=True)
        try:
            tp = getattr(self, "_tts_player", None)
            if tp is not None and tp.is_playing():
                return True
        except Exception:
            logger.debug("判断普通 TTS 状态失败", exc_info=True)
        return False

    def _on_focus_state_changed(self, active: bool, charge: float, signals: dict):
        """FocusStateMachine 状态变化回调（可能后台线程）→ 经信号回主线程更新 UI。"""
        try:
            self.focus_ui_signal.emit(active, charge, signals)
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

    def _on_focus_ui_changed(self, active: bool, charge: float, signals: dict):
        """主线程处理专注状态变化：更新 ChatPanel 辉光强度。"""
        try:
            if self._chat_panel is not None:
                strength = 0.3
                try:
                    strength = float((self.config.get("focus", {}) or {}).get("glow_strength", 0.3))
                except Exception:
                    strength = 0.3
                self._chat_panel.set_focus_active(bool(active), strength if active else 0.0)
            logger.info("T05 focus=%s charge=%.3f", "on" if active else "off", charge)
        except Exception as e:
            logger.debug("T05 focus UI 更新失败: %s", e)

    def _feed_focus_score(self, user_text: str):
        """T04 P0-5：用户消息 → FocusScorer 打分 → FocusStateMachine 积分（主线程）。"""
        try:
            if self._focus_scorer is None or self._focus_manager is None:
                return
            if not self._focus_manager.enabled:
                return
            from core.perception.focus import emotion_reading_from_state
            reading = emotion_reading_from_state(getattr(self._perception, "emotion", None))
            score = self._focus_scorer.score(user_text=user_text, emotion_reading=reading)
            self._focus_manager.update(score, spoke=False)
        except Exception as e:
            logger.debug("T05 focus score feed 失败: %s", e)

    # ── ChatPanel 接线（P0-6）──

    def _on_chat_panel_submit(self, text: str):
        """ChatPanel 输入提交 → 对话引擎发送（与 _send_message 一致语义）。"""
        if not text or not text.strip():
            return
        text = text.strip()
        self._mark_user_interaction()
        try:
            self._perception.reset_emotion()
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
        # 记录话题 + 打断旧回复 + 发送
        try:
            self._record_topic(text)
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
        if self._engine:
            # P1: 用户输入即时反应 — 分析内容，立刻触发动作（不等 AI 回复）
            try:
                if self._action_linker and self._action_linker.enabled:
                    reactions = self._action_linker.check_user_input(text)
                    for r in reactions:
                        self._apply_immediate_reaction(r)
            except Exception as e:
                logger.debug("即时反应失败: %s", e)
            
            try:
                self._engine.interrupt(reason="new_message")
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
            self._engine.send(text, character=self._current_char)
        # P1-2：对话事实写入点（engine.send 成功后记录事实；后台 LLM 抽取）
        try:
            self._record_conversation_facts(text)
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
        # 聊天面板：用户消息回显 + 思考点
        if self._chat_panel is not None:
            self._chat_panel.append_user(text)
            self._chat_panel.set_thinking(True)
        # 专注打分（P0-5）
        self._feed_focus_score(text)
        # P2 互动层：聊天关键词 → 小游戏/音乐/休息卡片（防御式）
        try:
            self._dispatch_chat_interaction(text)
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

    def _toggle_chat_panel(self):
        """打开/关闭聊天面板（右键菜单入口）。"""
        if self._chat_panel is None:
            return
        if self._chat_panel.isVisible():
            self._chat_panel.hide()
            return
        pet_geo = self.geometry()
        self._chat_panel.move(pet_geo.right() + 16, max(8, pet_geo.top() - 8))
        self._chat_panel.show()
        self._chat_panel.raise_()
        self._chat_panel.input_widget().setFocus()

    def _close_chat_panel(self):
        """ChatPanel 关闭按钮 → 隐藏窗口（不销毁，保留会话）。"""
        if self._chat_panel is not None:
            self._chat_panel.hide()

    def _toggle_memory_panel(self):
        """打开/关闭记忆面板（右键菜单入口，P0-8）。"""
        if self._memory_panel is None:
            return
        if self._memory_panel.isVisible():
            self._memory_panel.hide()
            return
        # 切换 agent 时刷新数据源
        try:
            if getattr(self._memory_panel, "set_agent", None) is not None:
                self._memory_panel.set_agent(self._agent_id)
            else:
                self._memory_panel.reload()
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
        pet_geo = self.geometry()
        self._memory_panel.move(pet_geo.right() + 16, max(8, pet_geo.top() - 8))
        self._memory_panel.show()
        self._memory_panel.raise_()

    def _toggle_character_card(self):
        """打开/关闭角色卡（右键菜单入口，P1-7）。"""
        if self._character_card is None:
            return
        if self._character_card.isVisible():
            self._character_card.hide()
            return
        # 切换角色/agent 时刷新数据源
        try:
            if getattr(self._character_card, "set_agent", None) is not None:
                self._character_card.set_agent(self._agent_id, self._current_char)
            else:
                self._character_card.reload()
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
        pet_geo = self.geometry()
        self._character_card.move(pet_geo.right() + 16, max(8, pet_geo.top() - 8))
        self._character_card.show()
        self._character_card.raise_()

    def _on_other_pet_enter(self, event):
        """另一只桌宠上线 → 弹打招呼气泡 + 挥手动作（仅响应非自己的 enter）。"""
        try:
            payload = getattr(event, "payload", None) or {}
            new_pet_id = payload.get("agent_id", "")
            if not new_pet_id or new_pet_id == self._agent_id:
                return
            # 礼貌打招呼（随机文案 + 挥手动作）
            import random as _r
            lines = [
                f"咦，{new_pet_id} 也来啦？打个招呼～",
                f"欢迎 {new_pet_id}！",
                f"{new_pet_id} 来了，今天一起玩吧！",
            ]
            text = _r.choice(lines)
            # 弹气泡（主线程安全：bridge 分发在后台线程，用信号/延时回主线程）
            try:
                from PySide6.QtCore import QTimer as _QT
                _QT.singleShot(0, lambda t=text: self._show_bubble(t, emotion="happy"))
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
            # 挥手动作（Live2D: waving motion；sprite: waving 序列）
            try:
                if hasattr(self, "_set_anim_seq"):
                    from pet_mixins.behavior_mixin import get_transition_style
                    self._set_anim_seq("waving", emotion="happy",
                                       style=get_transition_style("happy"))
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
            logger.info("P3 多宠打招呼 → %s", text[:40])
        except Exception as e:
            logger.warning("P3 多宠打招呼处理失败: %s", e)

    def _init_companion_memory(self):
        """初始化陪伴记忆（CompanionMemory）+ 记忆层 A-G 接线。

        - 记忆文件：~/.oc-pet/memory/<agent_id>.json（与养成存档同目录）
        - A/B：注入 EventStream + emotion_provider（事件流 + 情绪标签）
        - C/D/E：注入 SceneMemory 到感知控制器与 proactive（回忆/联想）
        - G：初始化 PetStatusMapper（状态语义层）
        - F：按 config.state_http.enabled 启动本地状态口（默认关）
        - 跨天启动时弹"接得上昨天"的问候；非跨天不打扰。
        """
        try:
            from core.companion_memory import CompanionMemory
            from core.event_stream import EventStream
            self._companion_memory = CompanionMemory(self._agent_id)
            # A：事件流（与旧 <agent_id>.json 平级的新文件，互不破坏）
            try:
                self._event_stream = EventStream(self._agent_id)
                self._companion_memory.set_event_stream(self._event_stream)
            except Exception as e:
                logger.warning("A 事件流初始化失败（非致命）: %s", e)
                self._event_stream = None
            # B：情绪快照 provider（读 EmotionStateMachine.current，纯内存读，无新增线程）
            try:
                if self._perception is not None:
                    self._companion_memory.set_emotion_provider(
                        lambda: self._perception.emotion.current
                    )
            except Exception as e:
                logger.debug("B 情绪 provider 注入失败: %s", e)
            # C/D/E：场景记忆（SceneMemory）
            try:
                from core.scene_memory import SceneMemory
                self._scene_memory = SceneMemory(self._agent_id)
                # 透传给感知控制器（closeEvent 收盘聚类）
                if self._perception is not None:
                    self._perception.set_scene_memory(self._scene_memory)
                # 注入 PetWindow 实际驱动的 proactive（D 回忆 + E 联想检索端）+ 记忆层配置
                proactive = getattr(self, "_proactive", None)
                if proactive is not None:
                    proactive.set_scene_memory(self._scene_memory)
                    proactive.load_memory_config(self.config.get("memory", {}))
            except Exception as e:
                logger.warning("C/D/E 场景记忆初始化失败（非致命）: %s", e)
                self._scene_memory = None
            # G：状态语义层（6 态映射；celebrating 用）
            try:
                from core.pet_status import PetStatusMapper
                self._status_mapper = PetStatusMapper()
            except Exception as e:
                logger.warning("G 状态语义层初始化失败（非致命）: %s", e)
                self._status_mapper = None
            # A：订阅活动事件（screen.py append 后 emit → 写事件流）
            try:
                from core.event_bus import EventBus
                EventBus.on("activity_event", self._on_activity_event)
                self._activity_event_subscribed = True
            except Exception as e:
                logger.warning("A 活动事件订阅失败（非致命）: %s", e)
                self._activity_event_subscribed = False
            # F：订阅本地状态口写转发（HTTP 线程 → 信号 → 主线程）
            try:
                from core.event_bus import EventBus
                EventBus.on("pet_set_mode", self._on_pet_set_mode)
                self._pet_set_mode_subscribed = True
            except Exception as e:
                logger.warning("F pet_set_mode 订阅失败（非致命）: %s", e)
                self._pet_set_mode_subscribed = False
            # F：启动本地状态口（默认关）
            self._status_http = None
            self._init_status_http()
            # P4：启动通用外部触发入口（默认关）
            self._external_trigger = None
            self._init_external_trigger()
            # 前台分类活动 → 记忆（常做的事）
            if hasattr(self, '_foreground_watcher'):
                self._foreground_watcher.on_change = self._on_foreground_change_with_memory
            # P0-1 陪玩：游戏窗口识别（只识别 + 发事件，不做用户可见动作）
            self._init_game_watch()
            # O1-P2：初始化后台 LLM 全局闸门（并发上限 + 每源预算 + 429 全局冷却）
            self._init_llm_gate()
            # O3：口型开口上限（默认不压，只留旋钮）
            self._init_lip_sync()
            # 跨天首启问候（延迟到窗口稳定后弹气泡）
            from core.companion_hooks import build_morning_greeting
            greet = build_morning_greeting(self._companion_memory)
            if greet:
                QTimer.singleShot(2000, lambda g=greet: self._show_bubble(g, emotion="happy"))
                logger.info("P2 每日首启问候 → %s", greet[:40])
            else:
                logger.debug("P2 非跨天启动，不弹首启问候")
        except Exception as e:
            logger.warning("P2 陪伴记忆初始化失败（非致命）: %s", e)
            self._companion_memory = None

    def _init_lip_sync(self):
        """按 ``config.lip_sync`` 配置口型。

        起音/收音（40ms/90ms）是常量，本身不需配置；这里只接**开口上限**：
        ``mouth_peak`` 默认 1.0 = 不压（保持现行为）。

        为何不给默认值：AgentAtelierR 用 55% 避免“每次都张满嘴”，但 oc-pet 的
        ``/a/`` 是自己调到 0.95 的（miku 模型实测值）——压多少是**看脸决定**的事，
        我看不见成品，不替你定。想试就把 ``lip_sync.mouth_peak`` 改成 0.75 或 0.55。
        """
        try:
            from core.lip_sync import set_mouth_peak

            cfg = (self.config.get("lip_sync", {}) if hasattr(self, "config") else {}) or {}
            set_mouth_peak(cfg.get("mouth_peak", 1.0))
        except Exception as e:
            logger.warning("口型配置失败（非致命）: %s", e)

    def _init_a2a(self, session_manager):
        """G2/A4：把活派给 Hana 的 agent（**启动路径**）。

        这里只负责把会话管理器存下来，真正的装卸在 :meth:`_apply_a2a_config`。
        """
        self._a2a_session_manager = session_manager
        return self._apply_a2a_config()

    def _apply_a2a_config(self):
        """按**当前** ``self.config`` 装/卸派活能力。

        启动（`_init_a2a`）和「设置面板保存后热重载」走**同一条路**
        （``core.a2a_capability.apply_config``）——只有一条路，
        就不会出现"启动时对、重载时不对"。所以改完设置**不用重启**。

        默认**关**（``config.a2a.enabled``），护栏全在 ``core/a2a.py``。
        结果**不自动念**——(b) 方案：只响一声门铃，他问才讲。
        """
        try:
            from core.a2a_capability import apply_config

            sm = getattr(self, "_a2a_session_manager", None)
            if sm is None:
                logger.info("A2A: 无会话管理器，跳过")
                return None

            def _create(agent_id):
                # create_session 的 agent_id 是**关键字参数**
                return sm.create_session(agent_id=agent_id)

            def _send(session, text, timeout):
                # send_and_wait 的 timeout 也是关键字参数，
                # 且返回 ReplyResult 而不是 str——两处都跟默认假设不一样
                r = sm.send_and_wait(session, text, timeout=timeout)
                return getattr(r, "text", "") or ""

            d = apply_config(
                getattr(self, "config", {}) or {}, _create, _send,
                on_result=self._on_a2a_result,
            )
            self._a2a = d
            logger.info("A2A %s：%s",
                        "已生效" if (d is not None and d.enabled) else "未启用",
                        d.stats() if d is not None else "无会话管理器")
            return d
        except Exception as e:
            logger.warning("A2A 应用配置失败（非致命）: %s", e)
            return None

    def _on_a2a_result(self, result):
        """(b) 方案：**只响门铃，不念结论**。

        讲不讲、什么时候讲由他决定；结论在 ``Delegator.results`` 里等他问。
        **失败也响**（内容不同）——不响也不存就等于替他吞掉了失败。

        但"失败"要分两种（用户 2026-09-19 追问"思考时间过长没回复也算吗"）：
        会话建起来了 = 活已经交出去了，只是还没回话，**不能替它宣布失败**。

        本方法在派活的**后台线程**里被调，所以只走线程安全的气泡入口。
        """
        try:
            from core.a2a_capability import label_of

            label = label_of(getattr(result, "agent_id", ""))
            if getattr(result, "ok", False):
                text, emotion = f"{label} 那边有结果了，想听就说一声～", "happy"
            elif getattr(result, "delivered", False):
                text, emotion = f"{label} 那边还没回话…要我看看吗？", "thinking"
            else:
                text, emotion = f"{label} 那边没交出去…要我细说吗？", "sad"
            self._show_bubble(text, emotion=emotion, source="a2a", duration_ms=8000)
        except Exception as e:
            logger.debug("A2A 门铃失败（忽略）: %s", e)

    def _init_llm_gate(self):
        """O1-P2：按 ``config.llm_gate`` 配置全局闸门（启动时一次）。

        它管的是后台那几条**互相不知情**的 LLM 调用（屏幕视觉 / 语义增强 /
        主动对话）：并发上限、每源小时预算、以及撞到 429 后全体一起收手。
        """
        try:
            from core.llm_gate import configure_gate

            g = configure_gate(getattr(self, "config", {}) or {})
            logger.info("O1-P2 全局闸门就绪：%s", g.stats())
        except Exception as e:
            logger.warning("O1-P2 全局闸门配置失败（非致命）: %s", e)

    def _init_game_watch(self):
        """P0-1（陪玩）：按 ``config.game`` 建游戏会话状态机。

        默认**开**，但这一步**不做任何用户可见动作**（不弹气泡、不说话）——
        只把“现在在玩哪个游戏、玩了多久”变成可观测的事实 + 一条 EventBus 事件
        （``game_session``）。说话是 P0-3/P0-4 的事，所以这一步可以放心常开。

        白名单来自内置表（零配置可用）+ ``config.game.games`` 覆盖，
        ``config.game.disabled`` 可剔除条目，全部关闭用 ``enabled=false``。
        """
        self._game_watch = None
        try:
            cfg = (self.config.get("game", {}) if hasattr(self, "config") else {}) or {}
            if not cfg.get("enabled", True):
                logger.info("陪玩 P0-1：游戏识别未启用（config game.enabled=false）")
                return
            from core.game.registry import load_games

            games = load_games(cfg)
            self._game_watch = GameSessionWatcher(games=games)
            logger.info("陪玩 P0-1：游戏识别就绪，白名单 %d 款（%s）",
                        len(games), "、".join(g.name for g in games[:5]))
        except Exception as e:
            logger.warning("陪玩 P0-1 初始化失败（非致命）: %s", e)
            self._game_watch = None

    def _on_foreground_change_with_memory(self, app_name: str, app_category: str, title: str):
        """前台变化：记录活动到陪伴记忆 + A 事件流 + 原有回调。"""
        try:
            mem = getattr(self, "_companion_memory", None)
            if mem is not None:
                mem.record_activity(app_category)
                # A：事件流追加（foreground 段；emotion 由 provider 自动填）
                now = time.time()
                start_ts = getattr(self, "_last_fg_start_ts", 0.0) or now
                mem.record_event(category=app_category, scenario="", intent="",
                                 start_ts=start_ts, end_ts=now, source="foreground")
            self._last_fg_start_ts = time.time()
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
        try:
            self._on_foreground_change(app_name, app_category, title)
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
        # P0-1 陪玩：游戏会话识别——只发 EventBus 事件（game_session），
        # 不弹气泡不说话；说什么是 P0-3/P0-4 的事。
        try:
            gw = getattr(self, "_game_watch", None)
            if gw is not None:
                emit_session_events(gw.on_foreground(app_name, title, app_category))
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

    def _on_activity_event(self, event=None):
        """A 活动事件订阅：视觉活动 → 事件流（source="vision"）。

        隐私约束：只记 category/时间，summary/detail 文本不进流。
        该回调在 screen 分析线程执行，record_event 内部线程安全。
        """
        try:
            mem = getattr(self, "_companion_memory", None)
            if mem is None or event is None:
                return
            now = time.time()
            start_ts = getattr(event, "start_time", 0.0) or now
            end_ts = getattr(event, "end_time", 0.0) or now
            mem.record_event(category=getattr(event, "category", "") or "",
                             scenario="", intent="",
                             start_ts=start_ts, end_ts=end_ts, source="vision")
        except Exception as e:
            logger.debug("A activity_event 写流失败: %s", e)

    # ── F：本地状态口（默认关，复用 phone_receiver 范式）──

    def trigger(self, text: str, action: str = "custom", emotion: str = ""):
        """公共入口：从任意线程发起外部触发（work/任务/内部事件均可调）。

        用途：内部模块（如 work 完成）可直接调用 window.trigger(...) 而不需要
        持有 HTTP 句柄。内部经 EventBus 转发到统一处理逻辑。
        """
        try:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: self._apply_external_trigger(action, text, emotion or "neutral"))
        except Exception as e:
            logger.warning("window.trigger 调度失败: %s", e)

    def _renderer_format(self) -> str:
        """按鸭子类型识别渲染器格式（sprite|live2d|vrm|unknown）。"""
        try:
            r = getattr(self, "_renderer", None)
            if r is None:
                return "unknown"
            if hasattr(r, "_model"):
                return "live2d"
            if hasattr(r, "_frames"):
                return "sprite"
            return "vrm"
        except Exception:
            return "unknown"

    def _on_pet_set_mode(self, mode: str = ""):
        """F 写转发：HTTP 线程 → Qt 信号 → 主线程驱动（不直连渲染线程）。"""
        try:
            self.pet_set_mode_signal.emit(str(mode or ""))
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

    def set_hanako_ws(self, ws_client, session_manager):
        """注入共享 Hanako WS 客户端（由 PetManager 调用）"""
        try:
            # 注入到对话引擎
            if hasattr(self, '_engine') and self._engine:
                if hasattr(self._engine, 'set_session_manager'):
                    self._engine.set_session_manager(session_manager)
            # 注入到 HanakoMonitor（共享 WS 订阅）
            if hasattr(self, '_hanako_monitor') and self._hanako_monitor:
                if hasattr(self._hanako_monitor, 'set_ws_client'):
                    self._hanako_monitor.set_ws_client(ws_client)
                # 绑定本桌宠对应的助手：只观测该助手的会话，
                # 不转播其他 agent 的活动（一个桌宠对应一个助手）
                if hasattr(self._hanako_monitor, 'set_agent_context'):
                    self._hanako_monitor.set_agent_context(self._agent_id, session_manager)
            # G2/A4：会话管理器到手，顺势把派活通道接上（默认关）
            self._init_a2a(session_manager)
            logger.info("Hanako WS injected into PetWindow")
        except Exception as e:
            logger.warning("set_hanako_ws failed: %s", e)

    def set_nurturing(
        self,
        save_mgr,
        state_mgr,
        work_timer,
        item_registry,
        work_registry,
    ):
        """注入 P0 养成系统（已弃用）。

        保留空方法以兼容旧调用方；状态 / 喂食 / 工作 / 任务玩法已移除。
        """
        logger.debug("set_nurturing is deprecated and does nothing")


    def _inject_agent_identity(self):
        """异步读取 agent 身份，注入给 idle_chatter 和 screen perception"""
        def _load():
            try:
                from core.hanako_context import HanakoContext
                ctx = HanakoContext(self._current_char)
                identity = ctx.read_identity() or ctx.read_description() or ""
                if identity:
                    if hasattr(self, '_idle_chatter') and self._idle_chatter:
                        self._idle_chatter.set_agent_identity(identity)
                    if hasattr(self, '_perception') and self._perception:
                        screen = getattr(self._perception, '_screen', None)
                        if screen and hasattr(screen, 'set_agent_identity'):
                            screen.set_agent_identity(identity)
                    logger.info("Agent identity injected (%d chars)", len(identity))
            except Exception as e:
                logger.debug("Agent identity injection skipped: %s", e)
        threading.Thread(target=_load, daemon=True).start()

    def _ensure_dialog_agent(self):
        """F5: 确保对话后端 agent 已绑定（首次启动引导）。

        规则（零硬编码）：
          - 若 dialog.agent_id 已绑定 → 不动
          - 否则看服务端是否有与本地角色同名的 agent → 有则绑定它
          - 否则绑定第一个可用 agent（不静默乱落，记录日志）
          - 若服务端无任何 agent → 保持未绑定（回退本地直接对话）
        """
        # 只在引擎就绪后做，避免竞态
        if not (hasattr(self, '_engine') and self._engine):
            return
        try:
            from config import load_config, save_config
            cfg = load_config()
            bound = (cfg.get("dialog", {}) or {}).get("agent_id", "") or ""
            if bound:
                return  # 已绑定
            agents = self._available_agents()
            if not agents:
                logger.info("F5: 服务端无可用 agent，回退本地对话（未绑定）")
                return
            # 优先同名角色
            target = next((a for a in agents if a.get("id") == self._current_char), None)
            if target is None:
                target = agents[0]
            tid = target.get("id", "")
            if not tid:
                return
            cfg.setdefault("dialog", {})["agent_id"] = tid
            save_config(cfg)
            # 同步 self.config 快照，避免退出时旧值覆盖
            try:
                self.config = cfg
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
            # 应用到引擎
            try:
                if hasattr(self._engine, 'switch_agent'):
                    self._engine.switch_agent(tid)
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
            logger.info("F5: 首次启动自动绑定对话 assistant=%s (角色=%s)", tid, self._current_char)
        except Exception as e:
            logger.warning("F5: 引导失败: %s", e)

    # ── 轻存在感（Presence） ──

    def _on_presence_action(self, action: str, bubble: str):
        """轻存在感回调：切动画 + 可选轻气泡（不打断对话）。

        action 已是真实帧序列名（waiting / sleep / waving）；
        bubble 默认空串（纯动作不打扰），少数动作带一句无害轻提示。
        """
        try:
            self._set_anim_seq(action, emotion="neutral", style="fade")
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
        if bubble:
            try:
                self._show_bubble(bubble, emotion="neutral", priority=0)
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

    def _presence_tick(self):
        """QTimer 每 60s 驱动一次存在感检查（主线程，无需跨线程处理）。

        P1-3：顺带触发反思引擎周期判断（schedule_reflect 后台线程 LLM，
        经 Qt 信号回主线程，不阻塞）。
        """
        try:
            if self._presence:
                self._presence.tick()
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
        try:
            self._maybe_reflect()
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

    # ── 多宠总览 ──

    def _open_pet_overview(self):
        """打开多宠总览面板（依赖 pet_manager 注入）。"""
        pm = getattr(self, "_pet_manager", None)
        if pm is None:
            self._show_bubble("多宠总览需要从主管理器启动", emotion="neutral")
            return
        try:
            from ui.pet_overview import PetOverviewDialog
            dlg = PetOverviewDialog(pm, self)
            dlg.exec_()
        except Exception as e:
            logger.warning("打开桌宠总览失败: %s", e)
            self._show_bubble("总览面板打开失败", emotion="sad")

    def _ban_last_proactive_topic(self):
        """禁言最近一次主动说话的话题类型（24h）。

        消费 core/usage_memory.py：写入 explicit_ban 后，
        ProactiveScheduler._speak 会在同一内容键上拦截 24h。
        """
        sched = getattr(self, "_proactive", None)
        if sched is None:
            self._show_bubble("主动对话未启用", emotion="neutral")
            return
        try:
            key = sched.ban_last_content(hours=24.0)
        except Exception as e:
            logger.warning("内容禁言失败: %s", e)
            self._show_bubble("禁言没成功", emotion="sad")
            return
        if key:
            self._show_bubble("好，这类话题我 24 小时内不提了", emotion="neutral")
        else:
            self._show_bubble("还没说过什么，先记下这条规则也行", emotion="neutral")

    # ── 屏幕查询 ──

    def _current_screen_geometry(self):
        """获取当前窗口所在屏幕的可用区域(支持多显示器)"""
        screen = self.screen()
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return QRect(0, 0, 1920, 1080)
        return screen.availableGeometry()

    # ── 窗口设置 ──

    def _setup_window(self):
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._apply_penetration()
        # 窗口基准适配 Live2D 角色：优先读 config.json 的 window 宽高（用户可配），
        # 默认 458x520（角色正方形画布按高度缩放后渲染宽 = 高×0.882）
        self._base_w = int(self.config.get("window", {}).get("width", 458))
        self._base_h = int(self.config.get("window", {}).get("height", 520))
        # 防脏数据：<250/300 的尺寸视为误存（config 默认 200x300 是过小的演示值，
        # 设置面板也可能把小值写进来），用默认值兜底——否则窗口比角色还小，
        # 模型被压扁+裁脚。
        if self._base_w < 250 or self._base_h < 300:
            logger.warning("config.window 尺寸异常 (%dx%d)，使用默认 458x520",
                           self._base_w, self._base_h)
            self._base_w, self._base_h = 458, 520
        self._base_w = max(40, self._base_w)
        self._base_h = max(40, self._base_h)
        self.setFixedSize(self._base_w, self._base_h)

        win_cfg = self._init_position or self.config.get("window", {})
        sg = self._current_screen_geometry()
        if win_cfg.get("x", -1) >= 0 and win_cfg.get("y", -1) >= 0:
            tx, ty = int(win_cfg["x"]), int(win_cfg["y"])
        else:
            # 默认：屏幕底部居中（之前是右下角，用户反馈“偏右”）。
            tx, ty = (sg.width() - 250) // 2, sg.height() - 350
        # 边界约束：config 里可能存了越界坐标（如窗口尺寸变化前的旧 x），
        # 让窗口始终完整落在屏幕可用区域内，避免“一启动就贴边/出屏幕”。
        try:
            tw = max(60, int(self._base_w * getattr(self, "_pet_scale", 1.0)))
            th = max(60, int(self._base_h * getattr(self, "_pet_scale", 1.0)))
            tx = max(sg.left(), min(tx, sg.right() - tw + 1))
            ty = max(sg.top(), min(ty, sg.bottom() - th + 1))
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
        self.move(tx, ty)

    def _apply_penetration(self):
        """应用当前鼠标穿透状态。

        WA_TransparentForMouseEvents 在 Windows 上对【顶层窗口】经常不生效
        （Qt 已知坑：该属性对子控件有效、对顶层窗口依赖平台实现）。
        用户实测"开启后无法互动桌宠后面的内容"就是没穿透成功。
        这里改用 Windows 原生 WS_EX_TRANSPARENT 扩展样式（GWL_EXSTYLE），
        对顶层窗口稳定生效；同时保留 attribute 设置作为跨平台补充。
        """
        self.setAttribute(Qt.WA_TransparentForMouseEvents, self._mousePassthrough)
        if hasattr(self, 'char_label') and self.char_label:
            self.char_label.setAttribute(Qt.WA_TransparentForMouseEvents, self._mousePassthrough)
        if hasattr(self, 'status_label') and self.status_label:
            self.status_label.setAttribute(Qt.WA_TransparentForMouseEvents, self._mousePassthrough)

        # ── Windows 原生穿透（关键）──
        # WS_EX_TRANSPARENT(0x20)：鼠标事件直接穿透到下层窗口；
        # WS_EX_LAYERED(0x80000)：配合透明窗口使用（FramelessWindowHint 的 Qt 窗口
        #   已经是 layered，设置透明样式即可）。
        # 注意：必须拿到真实的 winId()，且窗口 show 之后才能改（setWindowFlags 会重建窗口）。
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = int(self.winId())
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_LAYERED = 0x00080000
            WS_EX_APPWINDOW = 0x00040000
            user32 = ctypes.windll.user32
            cur = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if self._mousePassthrough:
                new_style = cur | WS_EX_TRANSPARENT
            else:
                new_style = cur & ~WS_EX_TRANSPARENT
            # 保证 layered（透明窗口必需）与 appwindow（避免被任务栏/Alt+Tab 吞掉）
            new_style |= WS_EX_LAYERED | WS_EX_APPWINDOW
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
            logger.info("鼠标穿透: %s (exstyle=0x%x → 0x%x)", "开启" if self._mousePassthrough else "关闭", cur, new_style)
        except Exception as e:
            logger.warning("Windows 原生穿透设置失败（降级 attribute）: %s", e)

    def _toggle_passthrough(self):
        """切换鼠标穿透"""
        self._mousePassthrough = not self._mousePassthrough
        self._apply_penetration()
        if self._mousePassthrough:
            self.input_widget.hide()
            self.bubble.hide_bubble()
        else:
            # 退出穿透：恢复输入区
            self.input_widget.show()

    # ── 系统托盘 ──

    def _setup_tray(self):
        """初始化系统托盘图标"""
        self._tray = QSystemTrayIcon(self)
        # 用角色首帧做托盘图标
        px = self._make_tray_icon()
        self._tray.setIcon(QIcon(px))
        self._tray.setToolTip("OC Desktop Pet")
        tray_menu = QMenu()
        tray_menu.setStyleSheet(self._menu_qss())
        vis = tray_menu.addAction("显示/隐藏")
        vis.triggered.connect(self._toggle_visibility)
        passthrough = tray_menu.addAction("鼠标穿透")
        passthrough.setCheckable(True)
        passthrough.setChecked(self._mousePassthrough)
        passthrough.triggered.connect(self._toggle_passthrough)
        tray_menu.addSeparator()
        quit_a = tray_menu.addAction("退出")
        quit_a.triggered.connect(self.close)
        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()
        self._tray_menu = tray_menu

    # ── 主题化样式（颜色统一来自 ui/theme/palette，随主题切换刷新）──

    def _menu_qss(self, theme=None):
        """右键菜单 / 托盘菜单 / 喂食·工作子菜单共用的主题化样式"""
        t = theme or getattr(self, "_ui_theme", "dark")
        return f"""
            QMenu {{ background: rgba({rgba(t, 'panel_bg')}); color: rgb({rgb(t, 'text_primary')});
                     border: 1px solid rgba({rgba(t, 'panel_border')}); border-radius: 8px; padding: 4px; }}
            QMenu::item {{ padding: 6px 20px; border-radius: 4px; }}
            QMenu::item:selected {{ background: rgba({rgba(t, 'tab_checked')}); }}
            QMenu::indicator {{ width: 0; }}
        """

    def _input_qss(self, theme=None):
        t = theme or getattr(self, "_ui_theme", "dark")
        return f"""
            QLineEdit {{
                background: rgba({rgba(t, 'input_bg')});
                color: rgb({rgb(t, 'text_primary')});
                border: 1px solid rgba({rgba(t, 'input_border')});
                border-radius: 12px; padding: 6px 12px; font-size: 11px;
            }}
            QLineEdit:focus {{ border-color: rgba({rgba(t, 'tab_checked')}); }}
        """

    def _send_btn_qss(self, theme=None):
        t = theme or getattr(self, "_ui_theme", "dark")
        return f"""
            QPushButton {{
                background: rgba({rgba(t, 'btn_primary')});
                color: rgb({rgb(t, 'text_primary')});
                border: none; border-radius: 15px; font-size: 14px;
            }}
            QPushButton:hover {{ background: rgba({rgba(t, 'btn_primary_hover')}); }}
        """

    def _passthrough_qss(self, theme=None):
        t = theme or getattr(self, "_ui_theme", "dark")
        return f"""
            QLabel {{
                background: rgba({rgba(t, 'panel_bg')});
                color: rgb({rgb(t, 'accent')});
                border: 1px solid rgba({rgba(t, 'accent_soft')});
                border-radius: 10px; font-size: 9px; padding: 2px 6px;
            }}
        """

    def _refresh_window_theme(self, theme: str):
        """主题切换时刷新主窗体所有写死样式（菜单 / 输入 / 发送键 / 穿透态）"""
        self._ui_theme = theme
        self.input_field.setStyleSheet(self._input_qss(theme))
        self.send_btn.setStyleSheet(self._send_btn_qss(theme))
        self._menu.setStyleSheet(self._menu_qss(theme))
        if hasattr(self, "_tray_menu"):
            self._tray_menu.setStyleSheet(self._menu_qss(theme))

    def _maybe_show_onboarding(self):
        """首次启动弹出轻量引导；看过则不再现"""
        ui_cfg = self.config.setdefault("ui", {})
        if ui_cfg.get("onboarded"):
            return
        try:
            from ui.onboarding import OnboardingOverlay

            def _mark_done():
                self.config.setdefault("ui", {})["onboarded"] = True
                try:
                    save_config(self.config)
                except Exception:
                    logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

            ov = OnboardingOverlay(self, on_close=_mark_done)
            ov.show_relative()
        except Exception:
            logger.exception("onboarding failed")

    # ── 行为模式(占位) ──
    def _switch_behavior_mode(self, mode):
        """切换行为模式 - 通过 BehaviorParams 完全参数化"""
        self._behavior_mode = mode
        self.config["behavior"] = mode
        save_config(self.config)
        self._stop_walking()
        self._motion_state = "idle"
        self._rest_counter = 0
        # 更新鼠标交互参数
        self._mouse_reaction_params = MOUSE_REACTIONS.get(mode, MOUSE_REACTIONS["normal"])
        self._renderer.set_gaze_enabled(self._mouse_reaction_params.gaze_enabled)

    def _get_behavior_params(self) -> BehaviorParams:
        """获取当前行为模式的参数"""
        return BEHAVIOR_MODES.get(self._behavior_mode, BEHAVIOR_MODES["normal"])

    def _make_tray_icon(self):
        px = QPixmap(16, 16)
        px.fill(Qt.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(140, 100, 200, 220))
        p.setPen(Qt.NoPen)
        p.drawEllipse(2, 2, 12, 12)
        p.end()
        return px

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self._toggle_visibility()

    def _toggle_visibility(self):
        """切换显示/隐藏"""
        self._mark_user_interaction()
        if self.isVisible():
            self.hide()
        else:
            self.show()

    def _trigger_action(self, action_id: str):
        """用户点击动作联动项 — 直接触发 renderer.apply_action_intent（P0 优化：去掉 outbox 绕路）"""
        self._mark_user_interaction()
        
        # P2: 动作冷却检查（连续触发合并成一次）
        now = time.time()
        last_trigger = self._action_cooldowns.get(action_id, 0)
        if now - last_trigger < self._action_cooldown_sec:
            logger.debug("Action cooldown: %s (last=%.1fs ago)", action_id, now - last_trigger)
            return  # 冷却中，跳过
        self._action_cooldowns[action_id] = now
        
        # P0: 直接触发 renderer，不经过 outbox.json → Agent 绕路
        try:
            renderer = getattr(self, '_renderer', None)
            if renderer and hasattr(renderer, 'apply_action_intent'):
                renderer.apply_action_intent({"gesture": action_id, "intensity": 0.7})
                self._show_bubble(f"{action_id}!", emotion="happy")
                return
        except Exception as e:
            logger.warning("直接触发动作失败，回退 outbox: %s", e)
        # 回退：outbox 绕路（兼容旧逻辑）
        basedir = Path(__file__).parent / "data"
        self._action_linker.trigger_action(basedir, action_id)
        self._show_bubble(f"{action_id}!", emotion="happy")

    def _apply_immediate_reaction(self, reaction: dict):
        """P1: 用户输入即时反应 — 直接应用情绪+动作到渲染器。

        与 _trigger_action 不同：
        - 不需要显示气泡（静默反应）
        - 同时设置情绪和动作
        - 强度可调

        Args:
            reaction: {action, emotion, intensity}
        """
        try:
            renderer = getattr(self, '_renderer', None)
            if not renderer:
                return
            
            action_id = reaction.get("action", "touch")
            emotion = reaction.get("emotion", "happy")
            intensity = reaction.get("intensity", 0.7)
            
            # 设置情绪（优先级仲裁，不打断当前对话）
            self._set_surface_emotion(emotion)
            
            # 触发动作
            if hasattr(renderer, 'apply_action_intent'):
                renderer.apply_action_intent({
                    "gesture": action_id,
                    "intensity": intensity,
                })
            
            logger.debug("即时反应: %s (emotion=%s, intensity=%.1f)", 
                        action_id, emotion, intensity)
        except Exception as e:
            logger.debug("即时反应失败: %s", e)

    def _clamp_size_to_screen(self, w: int, h: int) -> tuple:
        """把窗口尺寸压进当前屏幕可用区域（等比，不裁切角色）。

        为何必须做：Live2D 的 SetScale 是「**相对窗口**」的语义——
        窗口多大，角色就多大。所以窗口一旦超过屏幕，角色必然有一部分
        落在屏幕外。

        实测（2026-09-11，用户 scale=1.8）：
            窗口 810x1499，而可用区只有 2048x1104 → 脚被推到屏幕下方
            （Win32 报回 rect=(2568,0)-(3581,1874)，屏幕只有 1600 高）

        为何等比缩而不是裁切：SetScale 是把画布压进窗口的，
        窗口宽高比一变，角色就会被拉变形。

        代价：屏幕装不下时，桌宠实际显示会小于用户设定的 scale。
        但「完整可见」比「够大但缺脚」重要；想要更大只能换更大的屏或降低缩放。
        这里只记日志，**不把新尺寸写回配置**——用户的 scale 是他的意图，
        屏幕装不下是当前环境的事实，两者不该互相污染。
        """
        sg = self._current_screen_geometry()
        aw, ah = sg.width(), sg.height()
        if aw <= 0 or ah <= 0:
            return w, h
        if w <= aw and h <= ah:
            self._screen_clamp = 1.0
            return w, h
        k = min(aw / w, ah / h)
        cw, ch = max(60, int(w * k)), max(60, int(h * k))
        # 记下被压的比例：滚轮气泡靠它说实话
        # （否则窗口已被压到 1112 高，用户继续上滚时气泡还报 165%，而桌宠一像素不变）
        self._screen_clamp = k
        logger.info(
            "PetWindow: 窗口 %dx%d 超出屏幕 %dx%d，等比压到 %dx%d（缩放被屏幕限制）",
            w, h, aw, ah, cw, ch,
        )
        return cw, ch

    def _zoom_label(self, requested: float) -> str:
        """缩放气泡的文案。屏幕装不下时报**实际生效**的百分比。"""
        k = float(getattr(self, "_screen_clamp", 1.0) or 1.0)
        eff = requested * k
        if eff < requested - 0.005:
            return f"🔍 {int(eff * 100)}%（屏幕上限）"
        return f"🔍 {int(requested * 100)}%"

    def _resize_keeping_visible(self, w: int, h: int) -> None:
        """setFixedSize + 重新定位，保证窗口不被挤出屏幕。

        桌宠在屏幕上是「站在那里」的，所以贴边/缩放时：
          · 水平：中心不动（左右摆动幅度小，居中观感稳定）
          · 垂直：**底边不动**（脚踩在同一条线上）

        旧实现在两处都保持中心，于是窗口变高时会向上顶出屏幕：
        实测 scale=1.8 时窗口从 833 长到 1499，上移 333px，
        Win32 报回的真实矩形是 (1470,-325)-(2483,1549) ——
        屏幕只有 1152 高，桌宠顶部被推出了屏幕。

        位置属于「运行时可调、用户可改」的状态，所以这里只做钳制，
        不把新位置当配置写回（与 launch_all 的「运行时启发式不得修改
        持久配置」同一原则）。
        """
        geo = self.frameGeometry()
        x_center = geo.center().x()
        bottom = geo.bottom()
        # 唯一的 resize 咽喉：尺寸与定位的约束都在这里生效，
        # 避免 fit / 滚轮两条路各自漏一个约束（本项目反复踩过的坑）。
        w, h = self._clamp_size_to_screen(w, h)
        self.setFixedSize(w, h)
        if not self.isVisible():
            return

        x = x_center - w // 2
        y = bottom - h + 1
        sg = self._current_screen_geometry()
        # 比屏幕还高时，优先保证「顶部可见」（头不能被裁）
        if h >= sg.height():
            y = sg.top()
        else:
            y = max(sg.top(), min(y, sg.bottom() - h + 1))
        x = max(sg.left(), min(x, sg.right() - w + 1))
        self.move(x, y)

    def fit_window_to_model(self, w: int, h: int):
        """窗口贴合到模型实际大小（Live2D 渲染器测量后回调）。

        更新基准尺寸并 setFixedSize，同时让渲染器按新尺寸重算 fit。
        保留用户缩放（_pet_scale）语义：贴合后仍可滚轮缩放。
        """
        try:
            # ── 2026-09-11 修：缩放被乘了两次 ──
            #
            # 渲染器传进来的 w/h 是在**当前 _pet_scale 的视口里**量的，也就是
            # 已经是「窗口像素」。而 _base_w/_base_h 是「未缩放基准」，
            # 实际窗口 = 基准 × _pet_scale。
            #
            # 旧代码直接 基准 = 测量值 → 窗口 = 测量值 × S = 真实大小 × S。
            # 于是窗口尺寸与 scale 成**平方**关系：
            #   S=1.0 → 451x833      （看不出来，所以一直没人发现）
            #   S=1.8 → 1458x2698    （屏幕只有 2048x1152，窗口高出 2.3 倍）
            # 代价不只是“太大”：那张比屏幕还高的透明窗口会吞掉鼠标滚轮，
            # 实测因此把用户的 scale 从 1.8 误滚到 0.45。
            #
            # 除回去之后 窗口 = 测量值 = 模型实际大小，与 docstring 一致，
            # 且尺寸与 scale 恢复线性（与滚轮路径 _recalc_geometry 同义）。
            _s = max(0.05, float(getattr(self, "_pet_scale", 1.0) or 1.0))
            self._base_w = max(40, int(round(w / _s)))
            self._base_h = max(40, int(round(h / _s)))
            base_w = self._base_w
            base_h = self._base_h
            # 下限用 60（与 _recalc_geometry 一致）。
            # 旧式 max(base_w, ...) 在 _pet_scale < 1 时会把窗口钉在基准上，
            # 于是「缩小」在启动路径上完全无效——又一处两条路语义不一致。
            w_final = max(60, int(base_w * self._pet_scale))
            h_final = max(60, int(base_h * self._pet_scale))
            # setFixedSize 默认左上角不动、向右下扩展——贴合后模型会跟着往右/往下漂
            # （用户反馈“更右了”）。_resize_keeping_visible 保持水平中心 + 底边，
            # 并把窗口钳制在屏幕内。
            self._resize_keeping_visible(w_final, h_final)
            # P0-2: 一次性调用 recalc_geometry，避免 set_scale 和 recalc_geometry 分别触发 _recompute_fit
            if hasattr(self._renderer, "recalc_geometry"):
                self._renderer.recalc_geometry(w_final, h_final)
            logger.info("PetWindow: 窗口贴合模型 %dx%d (缩放 %.2f → %dx%d)",
                        self._base_w, self._base_h, self._pet_scale, w_final, h_final)
            QTimer.singleShot(50, self._store_label_pos)
            QTimer.singleShot(50, self._reposition_bubble)
        except Exception as e:
            logger.warning("PetWindow: 窗口贴合失败: %s", e)

    def _recalc_geometry(self):
        """缩放后重算窗口和角色图片尺寸(保持窗口中心不变)"""
        # P7: 精灵图模式下用 calc_ideal_window_size 算窗口尺寸，而非 458x520 默认
        # （精灵图帧尺寸+15%边距，比默认窗口小得多，避免窗口远大于精灵图的问题）
        if hasattr(self._renderer, "calc_ideal_window_size"):
            base_w, base_h = self._renderer.calc_ideal_window_size()
        else:
            base_w, base_h = self._base_w, self._base_h
        w = max(60, int(base_w * self._pet_scale))
        h = max(60, int(base_h * self._pet_scale))
        # 与 fit_window_to_model 走同一个定位规则（保持底边 + 钳制屏幕内），
        # 否则「滚轮放大」会把桌宠顶出屏幕，而「重启后贴合」不会——
        # 又一处两条路语义不一致。
        self._resize_keeping_visible(w, h)
        # 委托给 SpriteRenderer 处理角色尺寸
        self._renderer.set_scale(self._pet_scale)
        self._renderer.recalc_geometry(w, h)
        QTimer.singleShot(50, self._store_label_pos)
        QTimer.singleShot(50, self._reposition_bubble)

    def _apply_scale(self):
        """应用缩放设置"""
        self._recalc_geometry()

    def wheelEvent(self, event: QWheelEvent):
        """滚轮缩放桌宠：上滚放大，下滚缩小。

        范围 0.3~3.0，实时生效并持久化到 config.scale。
        按住 Ctrl + 滚轮同样触发（避免与悬浮窗滚动冲突）。
        """
        try:
            delta = event.angleDelta().y()
            if delta == 0:
                event.ignore()
                return
            step = 0.1 if abs(delta) < 120 else 0.15  # 高分辨率滚轮每格更精细
            new_scale = self._pet_scale + (step if delta > 0 else -step)
            # 下限 0.3（允许缩小到 30%）；0.5 会让用户觉得"缩不小"
            new_scale = round(max(0.3, min(3.0, new_scale)), 2)
            if new_scale != self._pet_scale:
                self._pet_scale = new_scale
                self._apply_scale()
                # 持久化到 config
                try:
                    self.config["scale"] = new_scale
                    from config import save_config, async_config_saver
                    async_config_saver.schedule(self.config)
                except Exception:
                    logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
                # 缩放反馈：立即更新气泡（缩放是用户主动行为，优先级 ≥ 已显示的对对话气泡）。
                # 旧逻辑：仅当 bubble 不可见才弹 → 10 秒对话气泡存在期间任何缩放都不会更新。
                # 用户反馈"滚轮缩放一直显示 65%"就是这个 bug。
                # 仍保留 1.2s 节流防快速重复刷屏（同一缩放操作只弹一次）。
                # 缩放提示是轻量反馈，1500ms 就消失（用户反馈"提示存在太久"）。
                _now = time.time()
                if (_now - getattr(self, "_last_zoom_bubble_ts", 0) > 1.2
                        and not getattr(self, "_is_thinking", False)):
                    self._show_bubble(self._zoom_label(new_scale), emotion="neutral", priority=0, duration_ms=1500)
                    self._last_zoom_bubble_ts = _now
            event.accept()
        except Exception as e:
            logger.debug("wheelEvent 缩放异常: %s", e)
            event.ignore()

    def _zoom_pet(self, factor: float):
        """按系数缩放桌宠（供菜单/快捷键调用）"""
        # 与 wheelEvent 保持同一下限 0.3，否则菜单/快捷键缩放缩不到 30%
        new_scale = round(max(0.3, min(3.0, self._pet_scale * factor)), 2)
        if new_scale != self._pet_scale:
            self._pet_scale = new_scale
            self._apply_scale()
            try:
                self.config["scale"] = new_scale
                from config import async_config_saver
                async_config_saver.schedule(self.config)
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
        return self._pet_scale

    def _rescale_current_frame(self):
        """把当前帧缩放到 char_label 大小"""
        frames = self._anim_frames.get(self._anim_seq, [])
        if not frames:
            return
        pix = frames[self._anim_idx % len(frames)]
        ls = self.char_label.size()
        if ls.width() > 0 and ls.height() > 0:
            pix = pix.scaled(ls.width(), ls.height(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if not self._facing_right:
            pix = pix.transformed(QTransform().scale(-1, 1))
        self.char_label.setPixmap(pix)

    def _adjust_opacity(self):
        """降低透明度 0.1,钳制 0.2~1.0"""
        self._pet_opacity = max(0.2, self._pet_opacity - 0.1)
        self.setWindowOpacity(self._pet_opacity)

    def _opacity_up(self):
        """增加透明度"""
        self._pet_opacity = min(1.0, self._pet_opacity + 0.1)
        self.setWindowOpacity(self._pet_opacity)

    def _opacity_down(self):
        """降低透明度"""
        self._adjust_opacity()

    # ── UI ──

    def _setup_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        self.main_layout.setAlignment(Qt.AlignCenter)

        # 角色渲染器(帧精灵 / Live2D / 未来 VRM) — 按角色目录格式自动选择
        self._renderer = create_renderer(self._current_char, self, override_format=self.config.get("render_format"))
        # 口型电平接线：把流式播放器的实时 RMS 接到渲染器。
        # 必须在 renderer 创建后（_init_voice_audio 早于 _setup_ui，那时还没 renderer）。
        self._wire_mouth_level_source()
        # 兼容别名(供 pet.py 其他部分使用)
        self.char_label = self._renderer.label
        self.char_label.installEventFilter(self)
        # 关键：必须把角色 widget 加进布局，否则 QOpenGLWidget 不显示（画了看不见）。
        # 透明测试已验证 addWidget 后 Live2D 能正常显示。精灵渲染用绝对定位 move()，
        # 但 QOpenGLWidget 需要进布局才能触发正确的显示/绘制。
        self.main_layout.addWidget(self.char_label, 0, Qt.AlignCenter)
        self.char_label.raise_()

        # 启动画面
        self._startup_screen = StartupScreen(self)
        self._startup_screen.show_for_character(self._current_char)

        # 气泡(顶层)
        self.bubble = ChatBubble(self)
        self.bubble.move(0, 0)
        self.bubble.raise_()

        # 宠物互动叠层：爱心粒子
        self._heart_overlay = HeartBurst(self)
        self._heart_overlay.resize_to_parent()
        self._heart_overlay.raise_()

        # 主题状态（用于菜单/输入框样式跟随）
        mgr = get_default()
        self._ui_theme = mgr.current if mgr else "dark"
        if mgr is not None:
            mgr.theme_changed.connect(self._refresh_window_theme)

        # 底部输入区
        self.input_widget = QWidget(self)
        self.input_widget.setFixedSize(280, 40)
        self.input_widget.setStyleSheet("background: transparent;")
        input_layout = QHBoxLayout(self.input_widget)
        input_layout.setContentsMargins(4, 2, 4, 2)
        input_layout.setSpacing(4)

        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("说点什么...")
        self.input_field.setStyleSheet(self._input_qss())
        self.input_field.returnPressed.connect(self._send_message)

        self.send_btn = QPushButton("➤")
        self.send_btn.setFixedSize(30, 30)
        self.send_btn.setStyleSheet(self._send_btn_qss())
        self.send_btn.clicked.connect(self._send_message)

        input_layout.addWidget(self.input_field)
        input_layout.addWidget(self.send_btn)
        self.input_widget.hide()

        self.main_layout.addStretch()
        self.main_layout.addWidget(self.input_widget, 0, Qt.AlignCenter)

        # 首次启动：延迟弹出引导（不挡启动流程）
        QTimer.singleShot(900, self._maybe_show_onboarding)

    # ── 动画 ──

    def _setup_animation(self):
        # 帧动画时钟(idle 默认 4fps,walk 6fps)
        # 渲染器不提供 QTimer 动画器（Live2D 自管循环）时跳过，避免 None.start()
        if self._anim_timer is not None:
            self._anim_timer.start(200)
        # 呼吸浮动由 unified_timer 驱动，不再单独开定时器

    def _unified_tick(self):
        """统一高频定时器回调（30ms）— 合并 physics + bob + gaze + transition"""
        # 守卫：渲染器/物理引擎尚未初始化完成时（PetWindow.__init__ 中段抛出）
        # 定时器已在更早的 start(50) 里启动，若不短路就会 AttributeError。
        if getattr(self, '_physics', None) is None:
            return
        # 1. 物理模拟
        self._physics.tick(self._get_behavior_params())
        # 2. 呼吸浮动
        self._bob_tick()
        # 3. 视线跟随（每 2 帧更新一次，~15fps 足够）
        if self._bob_frame % 2 == 0:
            self._gaze_tick()
        # 4. 空闲自言自语（每秒检查一次，实际触发间隔至少 120 秒）
        if self._bob_frame % 20 == 0:
            idle_chatter = getattr(self, "_idle_chatter", None)
            if idle_chatter and self._can_idle_chatter():
                idle_chatter.tick()
        # 5. 情绪过渡（无进行中过渡时 tick 内部直接 return，开销可忽略）
        self._transition.tick()
        # 5.5 情绪驱动本体动画（每秒一次；仅平静状态下，避免打断行走/追逐/对话）
        if self._bob_frame % 20 == 0:
            emo = getattr(self, '_current_emotion', 'neutral')
            # 情绪驱动本体动画（仅平静状态下，避免打断行走/追逐/对话）
            # 收窄：surprised/angry 不切瞪眼帧（用户反馈高频瞪眼），只保留表情脸表达
            if not hasattr(self, '_last_body_emotion'):
                self._last_body_emotion = 'neutral'
            body_emo = emo
            if body_emo in ('surprised', 'angry'):
                body_emo = 'neutral'  # 驱动本体动画时降级为中性，避免瞪眼
            if body_emo != self._last_body_emotion and hasattr(self, '_renderer'):
                calm = (not getattr(self, '_physics', None) or not self._physics.is_active) \
                    and not getattr(self, '_chasing', False) \
                    and not getattr(self, '_is_thinking', False) \
                    and not getattr(self, '_is_dragging', False)
                if calm:
                    self._renderer.set_emotion(body_emo)
                elif body_emo == "neutral":
                    # P2-9: 非 calm 状态下回 neutral，至少清表情（不播 motion，不打断动作）
                    try:
                        self._renderer.set_emotion_expression_only("neutral")
                    except Exception:
                        logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
                self._last_body_emotion = body_emo
        # 6. 待机微动作（约每秒一次）
        if self._bob_frame % 33 == 0:
            self._tick_idle_life()
        # 7. 鼠标追逐：持续跟随光标
        if getattr(self, '_chasing', False):
            self._update_chase()

    # ── TTS 口型 ──

    # ── PetAudioCallbacks 实现（AUDIO-07）──

    # ── 音频回调已迁移至 pet_mixins/audio_mixin.py（AudioMixin）──

    def _reposition_bubble(self):
        """气泡置于角色头顶上方,根据实际角色内容定位。

        边界防御（气泡不显示的常见根因）：
        - ChatBubble 是 PetWindow 的子 widget，坐标是父窗口的局部坐标。
        - 若 top_y - bh - 20 为负（角色头顶太靠上或气泡太高），直接塞到 y=2
          会把气泡压进 char_label 区域，可能被 QOpenGLWidget 盖住。
        - 若 bx 越出父窗口宽度，气泡一半在窗口外被 Qt 裁掉。
        因此这里先 clamp 到 [2, parent_bottom - bh - 2]，并在明显越界时打 warning。
        """
        top_y = self._get_char_top_y()
        bw = self.bubble.width()
        bh = self.bubble.height()
        pw = self.width()
        ph = self.height()

        # 水平居中，越界则贴左/右边界
        bx = (pw - bw) // 2
        if bx < 2:
            bx = 2
        if bx + bw > pw - 2:
            bx = pw - bw - 2

        # 垂直：头顶上方 20px；若被 clamp 到顶部，优先保证 y >= 2
        by = top_y - bh - 20
        clamped_top = False
        if by < 2:
            by = 2
            clamped_top = True
        if by + bh > ph - 2:
            by = max(2, ph - bh - 2)

        if clamped_top:
            try:
                logger.debug(
                    "_reposition_bubble: top_y=%d bh=%d → y 被 clamp 到 2（角色头顶贴顶）",
                    top_y, bh,
                )
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

        self.bubble.move(max(bx, 0), max(by, 0))
        self._reposition_overlays()

    # ── 右键菜单 ──

    def _setup_menu(self):
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        # 构建右键菜单（分层：互动 / 玩法 / 管理 三组，退出置底）
        self._menu = QMenu(self)
        self._menu.setStyleSheet(self._menu_qss())
        
        # UI优化: 添加键盘快捷键（从配置读取）
        from PySide6.QtGui import QKeySequence
        
        # 加载快捷键配置
        shortcuts_cfg = self.config.get("shortcuts", {})
        shortcuts_enabled = shortcuts_cfg.get("enabled", True)
        shortcuts_keys = shortcuts_cfg.get("keys", {})
        
        # 默认快捷键
        default_shortcuts = {
            "toggle_input": "Ctrl+L",
            "toggle_voice": "Ctrl+D",
            "toggle_voice_continuous": "Ctrl+Shift+D",
            "toggle_passthrough": "Ctrl+P",
            "open_activity_feed": "Ctrl+H",
            "open_settings": "Ctrl+S",
            "open_plugin_panel": "Ctrl+Shift+P",
        }
        
        def _get_shortcut(action_id: str) -> QKeySequence:
            """获取快捷键（如果启用）"""
            if not shortcuts_enabled:
                return QKeySequence()
            key = shortcuts_keys.get(action_id, default_shortcuts.get(action_id, ""))
            return QKeySequence(key) if key else QKeySequence()
        
        # ── 互动组 ──
        self._interact_menu = self._menu.addMenu("🍙 互动")
        self._interact_menu.setStyleSheet(self._menu_qss())
        a_chat = self._interact_menu.addAction("💬 对话", self._toggle_input)
        a_chat.setShortcut(_get_shortcut("toggle_input"))
        self._voice_action = self._interact_menu.addAction("🎤 说话", self._toggle_voice)
        self._voice_action.setShortcut(_get_shortcut("toggle_voice"))
        self._voice_continuous_action = self._interact_menu.addAction("🎤 持续监听", self._toggle_voice_continuous)
        self._voice_continuous_action.setShortcut(_get_shortcut("toggle_voice_continuous"))
        self._voice_continuous_action.setCheckable(True)
        self._voice_continuous_action.setChecked(False)

        # 行为模式子菜单
        self._behavior_submenu = self._interact_menu.addMenu("🚶 行为")
        self._behavior_submenu.setStyleSheet(self._menu_qss())
        self._behavior_actions = {}
        for mode, icon in [("quiet", "🤫"), ("normal", "🚶"), ("active", "🏃"), ("cling", "🤝")]:
            labels = {"quiet": "静默", "normal": "正常", "active": "活跃", "cling": "黏人"}
            a = self._behavior_submenu.addAction(f"{icon} {labels.get(mode, mode)}")
            a.setCheckable(True)
            a.setChecked(mode == self._behavior_mode)
            a.triggered.connect(lambda checked, m=mode: self._switch_behavior_mode(m))
            self._behavior_actions[mode] = a

        # 动作联动(动态高亮,默认隐藏,匹配时显示)
        self._interact_menu.addSeparator()
        self._action_menu_items = {}  # action_id -> QAction
        for action in self._action_linker.actions:
            a = self._interact_menu.addAction(f"{action.emoji} {action.label}", lambda a_id=action.id: self._trigger_action(a_id))
            a.setVisible(False)  # 默认隐藏,匹配时高亮
            self._action_menu_items[action.id] = a

        # ── 管理组 ──
        self._manage_menu = self._menu.addMenu("⚙️ 管理")
        self._manage_menu.setStyleSheet(self._menu_qss())

        # 穿透 / 活动流 / 新建对话
        self._passthrough_action = self._manage_menu.addAction("🔍 穿透", self._toggle_passthrough)
        self._passthrough_action.setShortcut(_get_shortcut("toggle_passthrough"))
        self._passthrough_action.setCheckable(True)
        self._passthrough_action.setChecked(self._mousePassthrough)
        a_activity = self._manage_menu.addAction("📜 活动流", self._open_activity_feed)
        a_activity.setShortcut(_get_shortcut("open_activity_feed"))
        # 多宠总览（依赖 pet_manager 注入；独立启动时隐藏）
        if getattr(self, "_pet_manager", None) is not None:
            self._manage_menu.addAction("🐾 桌宠总览", self._open_pet_overview)
        # M4: 新建对话入口（仅在 Hanako WS 模式下有意义）
        self._new_session_action = self._manage_menu.addAction("🔄 新对话", self._create_new_session)
        # 2026-09-10: 内容禁言——禁掉最近一次主动说话的话题类型（24h）
        # 消费 core/usage_memory.py；不注入时 ProactiveScheduler 自理惰性单例
        self._ban_topic_action = self._manage_menu.addAction("🙊 别再说这类", self._ban_last_proactive_topic)
        # 注：不再提供全局“切换助手”菜单——助手绑定已 per-pet 化
        # （每个桌宠在设置面板独立配置，见“桌宠独立配置”组）
        # 全局切换会破坏各桌宠自己的绑定，故移除。

        # 主题子菜单
        self._theme_submenu = self._manage_menu.addMenu("🎨 主题")
        self._theme_submenu.setStyleSheet(self._menu_qss())
        self._theme_actions = {}
        for label, mode, icon in [("自动（跟随时间）", "auto", "🌗"), ("浅色", "light", "☀️"), ("深色", "dark", "🌙")]:
            a = self._theme_submenu.addAction(f"{icon} {label}")
            a.setCheckable(True)
            a.triggered.connect(lambda checked, m=mode: self._set_theme_mode(m))
            self._theme_actions[mode] = a
        self._refresh_theme_menu()

        self._manage_menu.addAction("⚙️ 设置", self._open_settings)
        a_settings = self._manage_menu.actions()[-1]
        a_settings.setShortcut(_get_shortcut("open_settings"))
        a_plugin = self._manage_menu.addAction("🔌 插件", self._open_plugin_panel)
        a_plugin.setShortcut(_get_shortcut("open_plugin_panel"))

        # FrameBaker 集成（管理组内）
        try:
            from ui.framebaker import get_framebaker_menu_items
            for label, callback in get_framebaker_menu_items():
                self._manage_menu.addAction(label, callback)
        except Exception as e:
            logger.debug("FrameBaker 菜单加载失败: %s", e)

        # ── 退出（顶层置底,始终可见）──
        self._menu.addSeparator()
        self._menu.addAction("❌ 退出", self.close)

    # ── 养成接入（set_nurturing 后才填充） ──

    def _refresh_theme_menu(self):
        """根据 ThemeManager 当前 mode 同步右键菜单选中状态"""
        from ui.theme import get_default
        mgr = get_default()
        if mgr is None:
            return
        current_mode = mgr.mode
        for mode, action in self._theme_actions.items():
            action.setChecked(mode == current_mode)

    def _set_theme_mode(self, mode: str):
        """切换主题模式（auto / light / dark），持久化到 config.json"""
        from ui.theme import get_default
        mgr = get_default()
        if mgr is None:
            return
        mgr.set_mode(mode)
        self._refresh_theme_menu()
        mode_label = {"auto": "自动（跟随时间）", "light": "浅色", "dark": "深色"}.get(mode, mode)
        logger.info("主题模式切换：%s", mode_label)

        # 持久化到 config.json
        try:
            from config import load_config, save_config
            cfg = load_config()
            cfg["theme_mode"] = mode
            save_config(cfg)
        except Exception as e:
            logger.warning("保存主题模式到 config 失败：%s", e)

        # 轻量提示（在气泡显示）
        self._show_bubble(f"🎨 主题：{mode_label}", "neutral")

    def _open_activity_feed(self):
        """打开活动流窗口（浮动窗口，跟随宠宠位置）

        数据源：PerceptionController.get_recent_activity_events()
        主题感知：跟随 ThemeManager
        """
        events = []
        if hasattr(self, "_perception") and self._perception:
            try:
                events = self._perception.get_recent_activity_events(minutes=60)
            except Exception as e:
                logger.warning("获取活动事件失败: %s", e)

        if not hasattr(self, "_activity_feed") or self._activity_feed is None:
            self._activity_feed = ActivityFeed(events=events, parent=None)
            self._activity_feed.setWindowFlags(
                self._activity_feed.windowFlags() | Qt.Tool
            )
            # 注入定时刷新源——从 perception 拉最新事件
            self._activity_feed.set_refresh_source(
                lambda: self._perception.get_recent_activity_events(minutes=60)
                if self._perception else []
            )
        else:
            self._activity_feed.set_events(events)

        # 定位：宠宠右上角偏移 16px
        pet_geo = self.geometry()
        self._activity_feed.move(
            pet_geo.right() + 16,
            max(8, pet_geo.top() - 8)
        )
        self._activity_feed.show()
        self._activity_feed.raise_()

    def _open_settings(self):
        """打开配置面板"""
        from ui.settings_dialog import SettingsDialog
        # parent=None：不挂在 PetWindow 下。PetWindow 是 WS_EX_LAYERED/
        # WS_EX_TRANSPARENT 的特殊透明窗口（且带鼠标穿透），以此为 parent 的
        # 对话框会被约束/穿透，导致"exec 在跑但窗口不显示"或位置异常。独立窗口最干净。
        dialog = SettingsDialog(parent=None, config=self.config, pet_manager=self._pet_manager)
        if dialog.exec():
            self.config = dialog.get_config()
            save_config(self.config)
            # 热重载派活能力：设置里改了开关/白名单/配额**立刻生效**，不用重启
            # （启动与这里走同一条路 core.a2a_capability.apply_config）
            self._apply_a2a_config()
            # 刷新防抖写盘 pending：避免退出时 async_config_saver 用旧 config 引用
            # 把设置面板刚保存的切换结果覆盖回原角色。
            try:
                from config import async_config_saver
                async_config_saver.schedule(self.config)
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
            logger.info("配置已保存")
            # 应用即时生效的设置
            self._apply_settings()

    def _open_plugin_panel(self):
        """打开插件面板"""
        # 无条件日志：确认菜单项是否真的触发到本函数（R3-4 诊断）
        logger.info("打开插件面板请求被触发")
        try:
            from ui.plugin_panel import PluginPanel
            logger.info("PluginPanel 构造中...")
            # parent=None：不挂在 PetWindow 下。PetWindow 是 WS_EX_LAYERED/
            # WS_EX_TRANSPARENT 的特殊透明窗口（且带鼠标穿透），以此为 parent 的
            # 对话框会被约束/穿透，导致“exec 在跑但窗口不显示”。独立窗口最干净。
            panel = PluginPanel(on_send_command=self._send_plugin_command)
            logger.info("PluginPanel 构造成功，显式显示")
            panel.show()
            panel.raise_()
            panel.activateWindow()
            logger.info("开始 exec")
            panel.exec()
            logger.info("PluginPanel 已关闭")
        except Exception as e:
            # 异常若被 Qt 信号处理器吞掉，用户看到“点了没反应”且日志无记录。
            # 这里兜底：记录完整堆栈 + 气泡提示，让问题可见。
            logger.exception("打开插件面板失败: %s", e)
            try:
                self._show_bubble("插件面板打开失败", emotion="sad")
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

    def _send_plugin_command(self, text: str):
        """从插件面板发送指令到对话引擎"""
        self._mark_user_interaction()
        if self._engine:
            self._engine.send(text, character=self._current_char)
            # P1-2：对话事实写入点
            try:
                self._record_conversation_facts(text)
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
            self._stop_all_tts()
            self.bubble.set_text("⏳ 思考中...")
            self._reposition_bubble()
            self.bubble.show()
            self.bubble.raise_()
            self._is_thinking = True
            self._pending_chat = True

    def _apply_settings(self):
        """应用配置变更"""
        # 窗口透明度和缩放
        new_opacity = self.config.get("opacity", 1.0)
        if hasattr(self, '_pet_opacity') and self._pet_opacity != new_opacity:
            self._pet_opacity = new_opacity
            self.setWindowOpacity(new_opacity)
        
        new_scale = self.config.get("scale", 1.0)
        if hasattr(self, '_pet_scale') and self._pet_scale != new_scale:
            self._pet_scale = new_scale
            self._apply_scale()
        
        # TTS
        tts_cfg = self.config.get("tts", {})
        if tts_cfg.get("enabled", True):
            self._tts_player.enable()
        else:
            self._tts_player.disable()
        self._tts_player.set_volume(tts_cfg.get("volume", 0.8))

        # TTS 引擎切换（重建 provider）
        # 关键：CosyVoiceProvider 的**构造函数**会同步 import
        # torch / lightning / diffusers / onnxruntime，并可能触发 ModelScope
        # 联网下载 wetext 模型，耗时数十秒。旧实现把构造留在主线程、只把
        # preload() 丢后台，等于没防住——每点一次「保存」就冻结一次 UI。
        # 现改为：① 签名未变直接跳过；② 构造+preload 整体后台化；
        #        ③ 代际号丢弃过期结果，防连点产生并发实例。
        self._maybe_reload_tts_provider()

        # 鼠标交互
        self._mouse_reaction_params = MOUSE_REACTIONS.get(
            self.config.get("behavior", "normal"), MOUSE_REACTIONS["normal"]
        )
        if hasattr(self, '_renderer'):
            self._renderer.set_gaze_enabled(
                self.config.get("mouse_interaction", True)
                and self._mouse_reaction_params.gaze_enabled
            )

        # 行为模式
        self._switch_behavior_mode(self.config.get("behavior", "normal"))

        # 主动对话
        pro_cfg = self.config.get("proactive", {})
        if pro_cfg.get("enabled", True):
            self._proactive.enable()
        else:
            self._proactive.disable()
        self._proactive.load_config(pro_cfg)

        # 屏幕感知
        if hasattr(self, '_perception') and self._perception:
            screen = self._perception._screen
            screen_cfg = self.config.get("screen", {})
            if screen_cfg.get("enabled", True):
                screen.enable()
            else:
                screen.disable()
            screen.set_blur(screen_cfg.get("blur", False))
            screen.set_blacklist(screen_cfg.get("blacklist", False))
            screen.set_compress(screen_cfg.get("compress", True))
            # P1 感知哈希近邻去重（0=关）：像素变了但画面没变时不打视觉 API
            try:
                if hasattr(screen, "set_phash_threshold"):
                    screen.set_phash_threshold(
                        int(screen_cfg.get("phash_threshold", 0) or 0))
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
            # 截屏间隔（支持随机范围：interval_jitter_min/max，缺省用 interval±30%）
            try:
                iv = int(screen_cfg.get("interval", 120) or 120)
                lo = screen_cfg.get("interval_min")
                hi = screen_cfg.get("interval_max")
                if lo and hi:
                    screen.set_interval_range(int(lo), int(hi))
                else:
                    screen.set_interval(iv)
            except Exception:
                screen.set_interval(120)

        # Minecraft（需求② P4）：面板里改完开关/transport 即时重建桥接，不必重启桌宠
        try:
            _engine = getattr(self, "_engine", None)
            if _engine is not None and hasattr(_engine, "setup_mc_bridge"):
                _engine.setup_mc_bridge(self.config.get("mc"))
            # 需求③：QQ/微信桥接同理即时生效
            if _engine is not None and hasattr(_engine, "setup_hanako_bridge"):
                _engine.setup_hanako_bridge(self.config.get("hanako_bridge"))
            # 需求⑤：Skyrim MCP 桥接同理即时生效
            if _engine is not None and hasattr(_engine, "setup_skyrim_bridge"):
                _engine.setup_skyrim_bridge(self.config.get("skyrim"))
        except Exception as e:
            logger.warning("PetWindow: 应用 Minecraft/QQ微信/Skyrim 配置失败：%s", e)

    # ── 角色加载 ──

    def load_character(self, char_id: str):
        """加载角色 - 委托给 SpriteRenderer"""
        self._current_char = char_id

        # 委托给渲染器加载帧序列，优先使用 sprite_dir
        try:
            _loaded = self._renderer.load(char_id, sprite_dir=self._sprite_dir)
        except Exception as _e:
            logger.error("PetWindow: 渲染器加载异常 %s: %s", char_id, _e)
            _loaded = False
        if not _loaded:
            logger.error("PetWindow: 渲染器加载失败（缺少模型文件）%s", char_id)
            self._show_bubble(f"角色「{char_id}」缺少模型文件，无法加载", emotion="sad")
        # sprite 角色：帧尺寸已知，直接贴合窗口（Live2D 用 HitDrawable 测量，
        # 这里用帧尺寸×scale + 15% margin——窗口不再是 458x520 大热区包小图）
        if hasattr(self._renderer, "desired_window_size"):
            try:
                dw, dh = self._renderer.desired_window_size()
                if dw and dh:
                    logger.info("PetWindow: sprite fit 目标 %dx%d (帧 %s)",
                                dw, dh, self._renderer._get_frame_size())
                    self.fit_window_to_model(dw, dh)
            except Exception as e:
                logger.warning("PetWindow: sprite fit 失败: %s", e)
        # 渲染器不支持时明确提示用户（如 VRM 占位）
        if getattr(self._renderer, "unsupported", False):
            self._show_bubble(
                getattr(self._renderer, "unsupported_reason", "该角色格式暂不支持"),
                emotion="sad")
        # 同步状态别名
        self._anim_frames = self._renderer._frames
        self._anim_frame_tops = self._renderer._frame_tops

        # 更新托盘图标
        # OC_DISABLE_TRAY=1 时 _setup_tray 被跳过，_tray 不存在——必须判空，
        # 否则 load_character 启动即 AttributeError 崩溃。
        tray = getattr(self, "_tray", None)
        if tray is not None:
            tray.setIcon(QIcon(self._make_tray_icon()))

        # 启动画面
        self._startup_screen.show_for_character(char_id)

        # 重新定位气泡
        self._reposition_bubble()

        QTimer.singleShot(50, self._store_label_pos)

    def _store_label_pos(self):
        self._label_base_pos = self.char_label.pos()
        self._renderer.set_label_base_pos(self._label_base_pos)

    def _auto_hide_bubble(self):
        """发送中气泡超时隐藏"""
        self._is_thinking = False
        self._bubble_message = ""
        # 取消超时计时器
        if hasattr(self, '_think_timeout'):
            self._think_timeout.stop()
        if hasattr(self, 'bubble'):
            try:
                self.bubble.hide_bubble()
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

    def _on_think_timeout(self):
        """LLM 超时：自动恢复 idle 状态"""
        if self._is_thinking:
            logger.warning("LLM response timeout (30s), resetting to idle")
            self._is_thinking = False
            self._pending_chat = False
            self.bubble.hide_bubble()
            # P2-10 修复：带 emotion=neutral，确保表情被清除
            from config import get_transition_style
            self._set_anim_seq('idle', emotion='neutral', style=get_transition_style('neutral'))
            self._show_bubble("…信号不太好", emotion="sad")

    def _clear_hanako_bubble(self):
        """清除气泡（超时回调）；如有排队的低优先级通知，依次弹出"""
        if hasattr(self, 'bubble'):
            try:
                self.bubble.hide_bubble()
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
            self._bubble_message = ""
            self._bubble_priority = 0
            # 弹出最早排队的通知
            while self._pending_bubbles:
                text, emotion, priority = self._pending_bubbles.pop(0)
                if text:
                    self._show_bubble(text, emotion=emotion, priority=priority)
                    return

    def _on_emotion_expired(self):
        """A2: 情绪过期 — 3秒无新情绪后回到 idle
        
        E4 修复：同时复位 _emotion_source 和 _last_body_emotion，
        保证状态机一致性。旧实现只改 _current_emotion，
        导致 _last_body_emotion 陈旧，下一轮 set_emotion 判断失效。
        """
        if self._current_emotion != "neutral":
            logger.info("Emotion expired: %s -> neutral", self._current_emotion)
            self._current_emotion = "neutral"
            self._emotion_source = "neutral"  # E4: 复位来源
            try:
                self._set_anim_seq("idle", emotion="neutral", style=get_transition_style("neutral"))
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
            # P2-6：过期回 neutral 也同步程序化表情层（面部参数平滑回归）
            self._sync_renderer_master_emotion("neutral")
            # E4: 复位 _last_body_emotion，避免下一轮 set_emotion 判断错误
            self._last_body_emotion = "neutral"

    def _sync_renderer_master_emotion(self, emotion: str) -> None:
        """P2-6：把当前主导情绪（master emotion）同步到渲染器的程序化表情层。

        在情绪更新链路的每个写入点调用（_set_surface_emotion /
        _do_engine_reply_inner / _on_emotion_expired），渲染器据此做
        面部参数平滑插值。各渲染器缺省实现（avatar/base.py）只更新状态，
        Live2DRenderer 重写为同步表情 + 平滑过渡。全程 try/except 兜底。
        """
        renderer = getattr(self, "_renderer", None)
        if renderer is None:
            return
        setter = getattr(renderer, "set_master_emotion", None)
        if callable(setter):
            try:
                setter(emotion or "neutral")
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

    # 情绪来源优先级（缺陷①）：数值越大越高优，低优先不能覆盖高优先。
    # neutral 视为最低（0），任何来源都可覆盖回 neutral。
    _EMOTION_PRIORITY = {
        "dialog": 3,    # 对话回复情绪（LLM 明确表达）
        "screen": 2,    # 屏幕内容触发
        "timer": 1,     # 定时/主动对话/待机
        "neutral": 0,   # 回归默认
    }

    # 情绪最短驻留时间（迟滞 / hysteresis），单位毫秒。
    # 进入一个非中性情绪后，至少驻留这么久才允许被【同优先级或更低优先级】的
    # 另一个非中性情绪替换，挡住 happy→thinking→curious 式高频横跳。
    # 更高优先级来源（如用户直接对话 dialog）仍可立即打断；回到 neutral 始终允许。
    # 设计参照 mc-agent-neko 的 MIN_DWELL_MS。
    EMOTION_MIN_DWELL_MS = 8000

    def _set_surface_emotion(self, emotion: str, duration_ms: int = 3000, source: str = "dialog"):
        """统一设置当前情绪并同步到情绪脸，启动过期计时器。

        缺陷① 修复：带来源优先级。低优先级来源不能覆盖正显示的高优先级
        非 neutral 情绪（如屏幕情绪不能顶掉正在讲话的对话情绪）。
        """
        emotion = emotion or "neutral"
        new_prio = self._EMOTION_PRIORITY.get(source, 2)
        cur_prio = self._EMOTION_PRIORITY.get(getattr(self, "_emotion_source", "neutral"), 0)
        cur_emo = getattr(self, "_current_emotion", "neutral")
        # 迟滞：非中性情绪之间，dwell 窗口内且非更高优先级 → 忽略，挡住横跳
        if cur_emo != "neutral" and emotion != "neutral" and emotion != cur_emo:
            dwell_ms = (time.monotonic() - getattr(self, "_emotion_entered_at", 0.0)) * 1000
            if dwell_ms < self.EMOTION_MIN_DWELL_MS and new_prio <= cur_prio:
                logger.debug(
                    "情绪驻留中忽略切换: %s -> %s (dwell=%.0fms < %dms, prio %d<=%d)",
                    cur_emo, emotion, dwell_ms, self.EMOTION_MIN_DWELL_MS, new_prio, cur_prio,
                )
                trace.record("emotion", chosen=cur_emo, source=f"blocked:{source}",
                             rejected={emotion: f"dwell={dwell_ms:.0f}ms<{self.EMOTION_MIN_DWELL_MS}ms & prio {new_prio}<={cur_prio}"},
                             note="dwell-blocked")
                return
        # 低优先不能覆盖高优先的非 neutral 情绪
        if cur_emo != "neutral" and new_prio < cur_prio:
            logger.debug(
                "情绪被低优先级覆盖忽略: %s(%d) < 当前 %s(%d)",
                emotion, new_prio, cur_emo, cur_prio,
            )
            trace.record("emotion", chosen=cur_emo, source=f"blocked:{source}",
                         rejected={emotion: f"prio {new_prio}<{cur_prio}"},
                         note="low-prio-blocked")
            return
        switched = cur_emo != emotion
        self._current_emotion = emotion
        self._emotion_entered_at = time.monotonic()  # 刷新进入时刻，重启驻留窗口
        self._emotion_source = source
        trace.record("emotion", chosen=emotion, source=source,
                     rejected={cur_emo: "replaced"} if switched else {},
                     note="switch" if switched else "refresh")
        # P2-6：主导情绪同步到渲染器程序化表情层（面部参数平滑过渡）
        self._sync_renderer_master_emotion(self._current_emotion)
        if self._current_emotion != "neutral":
            self._emotion_expiry_timer.stop()
            self._emotion_expiry_timer.start(duration_ms)
        else:
            self._emotion_expiry_timer.stop()

    def _stop_all_tts(self) -> None:
        """停掉普通 TTS 与流式 TTS 播放（主线程）。"""
        try:
            self._tts_player.stop()
        except Exception:
            logger.debug("pet: 停 TTS 失败", exc_info=True)
        try:
            sp = getattr(self, "_stream_player", None)
            if sp is not None:
                sp.stop()
        except Exception:
            logger.debug("pet: 停流式 TTS 失败", exc_info=True)

    def _on_engine_reply(self, reply: str, emotion: str, anim: str, audio_path: str, action_intent=None):
        """对话引擎回复回调 - 从后台线程调用，通过信号转到主线程"""
        # 从 Python threading.Thread 调 QTimer.singleShot 不可靠
        # 用 Signal 发射，Qt 会自动跨线程投递到主线程
        self.engine_reply_signal.emit(reply, emotion, anim, audio_path, action_intent)

    def _on_engine_tts_stream(self, kind: str, payload, gen: int):
        """流式 TTS 回调（TTS 线程池调用）→ 信号转主线程。"""
        self.tts_stream_signal.emit(kind, payload, gen)

    def _smart_prebuffer_enabled(self) -> bool:
        """是否启用智能预缓冲（config `tts.stream.smart_prebuffer`）。

        默认 False。R=0.36x 时需攒到 ~74% 才能不断粮，首字会晚 10 秒左右——
        这是真实取舍，不替用户默认打开。
        """
        try:
            return bool(
                (self.config.get("tts", {}) or {})
                .get("stream", {}).get("smart_prebuffer", False)
            )
        except Exception:
            return False

    def _play_clock(self) -> float:
        """返回当前 TTS 已播放时长（秒）。供口型时间轴同步用。

        基于流式播放器实际吃掉的字节数（而非墙钟）——
        否则网络/合成卡顿时口型会跑到声音前面。
        """
        sp = getattr(self, "_stream_player", None)
        if sp is None:
            return 0.0
        try:
            return float(sp.played_seconds())
        except Exception:
            return 0.0

    def _setup_lip_timeline(self, text: str) -> None:
        """LIP-1：从文本生成口型时间轴并接到渲染器。

        在 TTS begin 时调用——那时文本已完整（无需等音频）。
        同时告知播放器**预期音频时长**，启用智能预缓冲。
        失败/文本为空/无 pypinyin → 清除时间轴，回退振幅驱动。
        """
        r = getattr(self, "_renderer", None)
        frames = []
        try:
            from core.lip_sync import build_timeline, total_duration
            frames = build_timeline(text or "")
        except Exception as e:
            logger.debug("口型时间轴生成失败（回退振幅）: %s", e)
            frames = []

        # 智能预缓冲：告知播放器预期总时长（口型时间轴就是按同一语速估的）
        sp = getattr(self, "_stream_player", None)
        if sp is not None and frames:
            try:
                from core.lip_sync import total_duration
                sp.set_expected_duration(
                    total_duration(frames),
                    enable=self._smart_prebuffer_enabled(),
                )
            except Exception as e:
                logger.debug("预期时长设置失败: %s", e)

        if r is None or not hasattr(r, "set_lip_timeline"):
            return
        if not frames:
            try:
                r.clear_lip_timeline()
            except Exception:
                pass
            return
        try:
            r.set_lip_timeline(frames, clock_fn=self._play_clock)
            logger.info("口型时间轴已接入：%d 片段，%.2fs",
                        len(frames), frames[-1].end)
        except Exception as e:
            logger.warning("口型时间轴接入失败（回退振幅）: %s", e)

    def _do_engine_tts_stream(self, kind: str, payload, gen: int):
        """主线程：流式 TTS 事件处理。"""
        try:
            if kind == "begin":
                # 新一句开始：停掉旧播放 + 重建流式缓冲
                self._stop_all_tts()
                emo = "neutral"
                text = ""
                if isinstance(payload, dict):
                    emo = payload.get("emotion") or "neutral"
                    text = payload.get("text") or ""
                self._last_tts_emotion = emo
                # LIP-1：先建时间轴（须在 prepare 前——prepare 后声卡可能立即拉数据）
                self._setup_lip_timeline(text)
                self._stream_player.prepare()
            elif kind == "chunk":
                self._stream_player.feed(payload)
            elif kind == "end":
                self._stream_player.finish()
        except Exception as e:
            logger.warning("流式 TTS 事件处理失败 (%s): %s", kind, e)

    def _on_engine_chunk(self, chunk: str, accumulated: str, emotion: str, gen: int):
        """P1: 流式 chunk 回调 - 从后台线程调用，通过信号转到主线程"""
        self.engine_chunk_signal.emit(chunk, accumulated, emotion, gen)

    def _do_engine_chunk(self, chunk: str, accumulated: str, emotion: str, gen: int):
        """在主线程中处理流式 chunk — 边生成边显示气泡"""
        try:
            # 开始流式气泡（第一次 chunk 时）
            if not getattr(self, '_is_streaming_bubble', False):
                self._show_bubble_stream(accumulated, emotion)
            else:
                # 追加文本到现有气泡
                self._append_bubble_text(chunk)
        except Exception:
            logger.debug("_do_engine_chunk failed: %s", chunk[:20])

    def _do_engine_reply(self, reply: str, emotion: str, anim: str, audio_path: str, action_intent=None):
        """在主线程中处理引擎回复"""
        try:
            self._do_engine_reply_inner(reply, emotion, anim, audio_path, action_intent=action_intent)
        except Exception:
            logger.exception("_do_engine_reply crashed")
            # 确保至少恢复基本状态
            self._is_thinking = False
            self._pending_chat = False

    def _do_engine_reply_inner(self, reply: str, emotion: str, anim: str, audio_path: str, action_intent=None):
        """在主线程中处理引擎回复（内部实现）。

        气泡时序（2026-09-14 改）：**只在 TTS 开始播放时显示一次**。

        旧行为会显示两次：
          1) 流式气泡结束时用完整回复替换一次（或文本回复完显示）
          2) TTS 合成完又显示一次
        用户反馈“重复”。现在改为：把待显示文本暂存，
        由 `on_tts_start`（真正开口那一刻）统一显示。

        无 TTS（或 TTS 关闭）时，不能把气泡吞了——那时直接显示。
        """
        # 取消超时计时器
        if hasattr(self, '_think_timeout'):
            self._think_timeout.stop()

        # 2026-09-17：取消待发的状态气泡。
        # 否则会出现「回复已上屏 → 1.5s 后一条迟到的『正在思考…』把它盖掉」。
        self._cancel_pending_status()

        # P1: 如果正在流式显示，结束流式气泡（但**不**立即重显）
        if getattr(self, '_is_streaming_bubble', False):
            self._finish_bubble_stream()

        # 截停旧 TTS
        self._stop_all_tts()

        # 计算要显示的文本
        #
        # 2026-09-17：不再无条件 `compact_bubble_text(reply) or reply`。
        # 清洗后为空有两种截然不同的原因：
        #   a) 回复里真的只有协议标签、没有正文（如模型只输出 `[feel:0.3,0.2]`）
        #   b) 清洗逻辑把正文也吃掉了（元信息正则越界，历史 bug）
        # 两种都不应该回退到原始文本——回退会把标签原样上屏，或留下 `[ 。` 残渣。
        # 判据用 clean_bubble_text：它返回空，就说明去掉标签/标点后确实没东西可显示。
        display_text = ""
        if reply and reply.strip() and reply.strip() not in ("\u2026", "..."):
            try:
                display_text = compact_bubble_text(reply)
            except Exception:
                display_text = reply
            if not display_text:
                try:
                    display_text = reply if clean_bubble_text(reply) else ""
                except Exception:
                    display_text = reply
            # 2026-09-17：关键词堆兜底——与来源无关。
            # 现象（日志 13:24:09）：屏幕主动评论返回的不是句子，而是词语罗列
            # （"桌宠 鼓励 用户 阅读 技术博客…"）。模板已瘦身，此处再加一道
            # 出口校验：不管哪条链路产生的，堆词一律不上气泡。
            if display_text and looks_like_keyword_salad(display_text):
                logger.warning("气泡文本疑似关键词堆，已拦截: %r", display_text[:60])
                display_text = ""

        # 是否有即将播放的音频（有则延到 on_tts_start 显示）
        will_play = bool(
            audio_path and os.path.exists(audio_path)
            and (self.config.get("tts", {}) or {}).get("enabled", True)
        )
        if will_play and display_text:
            # 暂存：等 TTS 真正开始播放时再显示（避免两次气泡）
            self._pending_bubble_text = display_text
            self._pending_bubble_emotion = emotion
            logger.debug("气泡延后到 TTS 开播时显示: %r", display_text[:30])
        elif display_text:
            # 无音频：直接显示（否则用户什么都看不到）
            self._pending_bubble_text = ""
            self._show_bubble(display_text, emotion=emotion, priority=1)
        else:
            # 空回复也要清除“思考中”气泡
            self._pending_bubble_text = ""
            try:
                self.bubble.hide_bubble()
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

        # 播放音频（和文字一起）
        if audio_path and os.path.exists(audio_path):
            tts_cfg = self.config.get("tts", {})
            if tts_cfg.get("enabled", True):
                logger.info("Playing TTS: %s", audio_path)
                self._last_tts_emotion = emotion or "neutral"
                self._tts_player.play(audio_path)

        # 动画（收窄：surprised/angry 不切瞪眼帧，避免对话时高频瞪眼）
        try:
            r = getattr(self, "_renderer", None)
            if action_intent is not None and r is not None and hasattr(r, "apply_action_intent"):
                # P2: 动作冷却检查（连续触发合并成一次）
                now = time.time()
                gesture = action_intent.get("gesture", "")
                last_trigger = self._action_cooldowns.get(gesture, 0)
                if now - last_trigger >= self._action_cooldown_sec or not gesture:
                    # 结构化动作意图（[action:{...}]）：由渲染器平滑驱动表情/动作，
                    # 复用 Live2D 每帧指数平滑或精灵图帧序列，避免瞬间跳变。
                    r.apply_action_intent(action_intent)
                    if gesture:
                        self._action_cooldowns[gesture] = now
            else:
                # 回退：emotion 标签路径（[emotion:xxx]）→ 身体动画
                body_anim = anim
                if emotion in ('surprised', 'angry'):
                    body_anim = 'idle'
                self._set_anim_seq(body_anim, emotion=emotion, style=get_transition_style(emotion))
            # 面部表情独立于身体动作：始终同步对话情绪（P2-10 修复，不依赖
            # play_anim 的 if emotion 守卫，强制清渲染器表情）
            if r is not None and hasattr(r, "set_emotion_expression_only"):
                r.set_emotion_expression_only(emotion or "neutral")
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

        # A2: 情绪过期 — 3秒无新情绪自动回 idle
        # 对话情绪是最高优先级（缺陷①），直接写入并标记来源，屏幕/定时情绪此后不得覆盖
        self._current_emotion = emotion or "neutral"
        self._emotion_source = "dialog"
        self._emotion_entered_at = time.monotonic()  # 对话情绪作为驻留窗口起点
        # P2-6：对话情绪同步到渲染器程序化表情层（面部参数平滑过渡）
        self._sync_renderer_master_emotion(self._current_emotion)
        if self._current_emotion != "neutral":
            self._emotion_expiry_timer.start(3000)
        else:
            self._emotion_expiry_timer.stop()

        # 触发情绪状态机
        if emotion and emotion != "neutral":
            try:
                self._perception.trigger_emotion(emotion)
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

        # 重置状态
        if self._pending_chat:
            self._pending_user_msg = ""
            self._pending_chat = False

        # T05 P0-6：ChatPanel 同步——关闭思考点 + 追加助手回复
        # P2-1：助手回复走流式打字机（逐字显示，点击气泡可跳过）
        try:
            if getattr(self, "_chat_panel", None) is not None:
                self._chat_panel.set_thinking(False)
                if reply and reply.strip() and reply.strip() not in ("\u2026", "..."):
                    start = getattr(self._chat_panel, "start_assistant_stream", None)
                    if callable(start):
                        start(reply.strip())
                    else:
                        self._chat_panel.append_assistant(reply.strip())
        except Exception as e:
            logger.debug("T05 chat panel reply sync failed: %s", e)

        # 重置 idle
        self._is_thinking = False
        self._mark_user_interaction()

    def _on_engine_status(self, msg: str, emotion: str = "thinking"):
        """引擎状态提示 - 从后台线程调用，通过信号转到主线程"""
        self.engine_status_signal.emit(msg, emotion or "thinking")

    # ── 状态气泡节流（2026-09-17）────────────────────────
    #
    # 问题：实测日志里 564 个气泡有 339 个（60%）是「正在思考…/正在回复…/
    # 正在使用 xxx…/工具执行完成…」这类占位文案。最密的一簇在 7 秒里弹了
    # 22 个气泡（工具链每次 tool_start/tool_end 都推一条），视觉上是抽搐，
    # 不是「活泼」。
    #
    # 策略：
    #   - 占位状态归为一个「处理中」槽，窗口内只显示最后一条
    #   - 窗口内不再重发时，到期后仍显示最后一条（不闪回空）
    #   - 真实回复（_do_engine_reply_inner）到达时立即取消待发状态，
    #     保证回复不被迟到的状态覆盖
    #
    # 注意：只节流**状态**气泡，不节流真实回复、工具失败、错误提示——
    # 后者是用户该知道的信息，吞掉它们是另一个错误。
    _STATUS_BUBBLE_THROTTLE_MS = 1500

    def _do_engine_status(self, msg: str, emotion: str = "thinking"):
        """在主线程中处理引擎状态（带节流）。

        emotion: 气泡情绪。工具失败等场景可传 "sad"。
        """
        if not msg:
            try:
                self.bubble.hide_bubble()
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
            return

        # 工具失败 / 错误提示不过滤——这些是用户该看到的信息
        if self._is_important_status(msg):
            self._flush_status_bubble(msg, emotion)
            return

        # 状态类：合并到窗口末尾只发最后一条
        now = time.monotonic() * 1000.0
        last_ms = getattr(self, "_status_bubble_last_ms", 0.0)
        if now - last_ms < self._STATUS_BUBBLE_THROTTLE_MS:
            # 窗口内：记下最新一条（含情绪），不立即上屏
            self._status_bubble_pending = msg
            self._status_bubble_pending_emotion = emotion
            timer = getattr(self, "_status_bubble_timer", None)
            if timer is not None:
                timer.stop()
            else:
                timer = QTimer(self)
                timer.setSingleShot(True)
                timer.timeout.connect(self._flush_status_bubble_pending)
                self._status_bubble_timer = timer
            timer.start(self._STATUS_BUBBLE_THROTTLE_MS)
            return
        self._flush_status_bubble(msg, emotion)

    @staticmethod
    def _is_important_status(msg: str) -> bool:
        """该状态是否必须立即展示（工具失败/错误）。"""
        return any(k in msg for k in ("失败", "错误", "⚠️", "出了问題", "异常"))

    def _flush_status_bubble(self, msg: str, emotion: str = "thinking"):
        """真正上屏一条状态气泡，并记录节流时间。"""
        self._status_bubble_pending = ""
        self._status_bubble_last_ms = time.monotonic() * 1000.0
        self._show_bubble(msg, emotion=emotion)

    def _flush_status_bubble_pending(self):
        """节流窗口到期：补发窗口内最后一条状态（含其情绪）。"""
        msg = getattr(self, "_status_bubble_pending", "")
        if msg:
            emo = getattr(self, "_status_bubble_pending_emotion", "thinking")
            self._flush_status_bubble(msg, emo)

    def _cancel_pending_status(self):
        """真实回复即将上屏——取消任何待发的状态气泡。

        否则会出现「回复已显示 → 1.5s 后一条迟到的『正在思考…』把它盖掉」。
        """
        self._status_bubble_pending = ""
        timer = getattr(self, "_status_bubble_timer", None)
        if timer is not None:
            timer.stop()
        self._status_bubble_last_ms = 0.0

    def _do_tts_stop(self):
        """主线程槽：截停 TTS 播放（由 tts_stop_signal 从后台线程绕回主线程）。

        QMediaPlayer（Windows 后端=Media Foundation，纯 COM）只能在创建它的
        主线程操作；任何后台线程（ASR 识别完成、持续监听）需要停 TTS 时一律
        通过 emit tts_stop_signal 走到这里。
        """
        try:
            player = getattr(self, "_tts_player", None)
            if player is not None:
                player.stop()
        except Exception as e:
            logger.warning("_do_tts_stop error: %s", e)

    def _do_play_tts_audio(self, audio_path: str):
        """主线程槽：播放 TTS 音频（由 tts_audio_signal 从后台线程绕回主线程）。

        2026-09-07: 供 BubbleMixin 的 speak() 回调使用，让 idle chatter、
        屏幕感知 proactive 等回复也有语音。
        """
        try:
            if not audio_path or not os.path.exists(audio_path):
                return
            player = getattr(self, "_tts_player", None)
            if player is not None and player.enabled:
                logger.info("Playing TTS (signal): %s", audio_path)
                player.play(audio_path)
        except Exception as e:
            logger.warning("_do_play_tts_audio error: %s", e)

    def _do_chat_state(self, text: str):
        """主线程槽：语音识别完成后更新聊天状态（由 chat_state_signal 绕回主线程）。

        原实现在 _do_asr 后台线程直写 _is_thinking/_pending_chat/_pending_user_msg
        并调 _record_topic（内部写文件/列表），主线程同帧可能读到中间态。
        这里统一在主线程更新状态 + 记录话题，语义与文字输入 _send_message 一致。
        """
        if not text:
            return
        self._is_thinking = True
        self._pending_chat = True
        self._pending_user_msg = text
        # P2 关系：记录语音话题到陪伴记忆（主线程执行，避免后台写文件/列表）
        try:
            self._record_topic(text)
        except Exception as e:
            logger.warning("_do_chat_state record_topic error: %s", e)

    # ── M4: 工具进度 + 新对话入口 ──

    def _do_tool_progress(self, tool_name: str, phase: str, display_text: str, success):
        """Hanako WS 工具进度回调 - 在主线程中显示气泡

        phase: "start" / "progress" / "end"
        success: None / True / False

        2026-09-17：接入状态气泡节流。
        实测（日志 14:32:30→14:32:31）：tool_call 与「正在思考」在 1 秒内
        连续上屏——因为本方法直接调 `_show_bubble`，**绕过了
        `_do_engine_status` 的节流**。现在统一走 `_do_engine_status`，
        与其它状态文案共享同一窗口；失败提示仍立即上屏（重要信息不节流）。
        """
        try:
            if phase == "start" or phase == "progress":
                self._do_engine_status(display_text or f"⏳ {tool_name}…", "thinking")
            elif phase == "end":
                if success is False:
                    # 失败是重要信息：直接上屏（节流器会识别并放行）
                    self._do_engine_status(f"⚠️ {display_text or tool_name} 出了问题", "sad")
                # 成功时不覆盖后续的最终回复气泡
        except Exception as e:
            logger.warning("_do_tool_progress error: %s", e)

    # ── per-pet 配置合并（每个桌宠独立引擎+助手） ──

    def _merge_agent_config(self):
        """把 agent 级配置合并进 self.config（agent 覆盖全局）。

        支持段：tts（引擎/音色）、dialog（助手绑定）。
        原则：agent 级字段存在则覆盖全局，缺省则沿用全局默认——
        老配置文件（无 agent 段）完全不受影响。
        """
        ac = self._agent_config or {}

        # tts 段：浅合并（只覆盖 agent 提供的键）
        at = ac.get("tts") if isinstance(ac.get("tts"), dict) else None
        if at:
            self.config.setdefault("tts", {}).update(at)
            logger.info(
                "per-pet tts: %s 覆盖 tts=%s", self._agent_id,
                {k: v for k, v in at.items() if k in ("provider", "edge_voice", "voice")},
            )

        # dialog 段：助手绑定（agent 指定则用，未指定保持全局/F5 引导）
        ad = ac.get("dialog") if isinstance(ac.get("dialog"), dict) else None
        if ad and ad.get("agent_id"):
            self.config.setdefault("dialog", {})["agent_id"] = ad["agent_id"]
            logger.info("per-pet dialog: %s 绑定 agent=%s", self._agent_id, ad["agent_id"])

    def _effective_tts(self) -> dict:
        """当前桌宠生效的 TTS 配置（= 全局 tts + agent 级覆盖）。"""
        return self.config.get("tts", {}) or {}

    # ── F4: 切换对话后端助手 ──

    def _available_agents(self) -> list[dict]:
        """动态发现服务端可用 agent（零硬编码）。

        优先用 PetManager 的 discover_agents（含名称/立绘），
        回退到扫描 ~/.hanako/agents/ 目录。
        """
        try:
            from pet_manager import PetManager
            mgr = getattr(self, '_pet_manager', None)
            if mgr is None:
                # 尝试从模块构建一个仅用于发现的实例
                mgr = PetManager()
            return mgr.discover_agents()
        except Exception as e:
            logger.warning("discover_agents 失败: %s", e)
        # 回退：直接扫目录
        try:
            from hanako_home import hanako_home
            home = hanako_home() / "agents"
            if home.exists():
                return [{"id": d.name, "name": d.name} for d in home.iterdir() if d.is_dir()]
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
        return []

    def _build_agent_submenu(self):
        """已废弃：助手绑定已 per-pet 化（设置面板逐桌宠配置），
        全局切换会破坏各自绑定，此入口不再提供。保留空实现防外部调用。"""
        pass

    def _switch_agent(self, agent_id: str):
        """已废弃：见 _build_agent_submenu。
        真正的绑定在设置面板“桌宠独立配置”或首次启动 F5 引导。"""
        logger.info("_switch_agent 已废弃，忽略（agent=%s）——请到设置面板配置桌宠独立绑定", agent_id)

    def _refresh_agent_menu(self):
        """已废弃：见 _build_agent_submenu。"""
        pass
    # ── 右键菜单 ──

    # ── PhysicsCallbacks 接口 ──

    def get_screen_geometry(self):
        return self._current_screen_geometry()

    def get_pos(self):
        return (self.x(), self.y())

    def get_size(self):
        return (self.width(), self.height())

    def move_to(self, x: int, y: int):
        self.move(x, y)

    def on_walk_finished(self):
        self._is_walking = False
        self._set_anim_seq('idle')
        self._store_label_pos()
        pos = self.pos()
        self.config.setdefault("window", {})["x"] = pos.x()
        self.config.setdefault("window", {})["y"] = pos.y()
        # 异步防抖保存：散步每次到达都写盘会周期性卡顿，改走后台
        async_config_saver.schedule(self.config)
        if self._on_position_change:
            self._on_position_change(pos.x(), pos.y())
        params = self._get_behavior_params()
        self._motion._start_rest(params)
        # 散步到达后张望一下，更有生气（追逐中不抢戏）
        if not (getattr(self, '_chasing', False) or getattr(self, '_is_dragging', False) or self._is_thinking):
            self._do_look_around()

    def on_bounce_finished(self, x: int, y: int):
        self._motion_state = "idle"
        self._bounce_active = False
        self.config.setdefault("window", {})["x"] = x
        self.config.setdefault("window", {})["y"] = y
        # 异步防抖保存
        async_config_saver.schedule(self.config)

    def on_facing_change(self, facing_right: bool):
        self._facing_right = facing_right
        # 同步渲染器朝向，确保 atlas 方向动画不被反向翻转
        self._renderer.set_facing(facing_right)

    def set_anim(self, anim: str):
        # atlas 格式：walk → running-right/left（根据朝向）
        if anim == 'walk':
            if 'running-right' in self._renderer._frames:
                anim = 'running-right' if self._facing_right else 'running-left'
        self._set_anim_seq(anim)

    # ── Hanako 状态回调 ──

    # ── 空闲时间追踪(idle 超时递进)──

    def closeEvent(self, event):
        """关闭窗口时停止所有定时器 + 清理资源"""
        timers = [
            '_unified_timer', '_motion_timer',
            '_anim_timer', '_drag_poll_timer', '_hanako_poll_timer',
            '_break_timer', '_foreground_timer', '_bubble_timer',
            '_mouse_tracker_timer',
        ]
        for tname in timers:
            t = getattr(self, tname, None)
            if t:
                try:
                    t.stop()
                except Exception:
                    logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

        if hasattr(self, '_idle_chatter'):
            try:
                self._idle_chatter.disable()
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

        if hasattr(self, '_foreground_watcher'):
            try:
                self._foreground_watcher.stop()
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

        if hasattr(self, '_tts_player'):
            try:
                self._tts_player.stop()
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
        if hasattr(self, '_stream_player'):
            try:
                self._stream_player.stop()
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

        # ── AUDIO-07: 断开桥接器 ──
        if hasattr(self, '_audio_bridge'):
            try:
                self._audio_bridge.disconnect()
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

        if hasattr(self, '_tray'):
            try:
                self._tray.hide()
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

        if hasattr(self, '_engine'):
            try:
                self._engine.stop()
                # 等后台线程退出，避免 TTS 文件被截断
                if self._engine._thread and self._engine._thread.is_alive():
                    self._engine._thread.join(timeout=3)
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

        # ── P2 关系：关电脑离别语 + 存档陪伴记忆 ──
        try:
            mem = getattr(self, "_companion_memory", None)
            if mem is not None:
                from core.companion_hooks import build_farewell
                farewell = build_farewell(mem)
                # 离别语延迟 300ms 弹出（等窗口还可见）
                QTimer.singleShot(300, lambda f=farewell: self._show_bubble(f, emotion="sad"))
                # 归档今日 + 保存（下次启动能接上）
                mem.close()
                logger.info("P2 离别语 → %s", farewell[:40])
        except Exception as e:
            logger.warning("P2 离别语失败（非致命）: %s", e)

        # ── C 记忆层收盘：事件流 → 场景聚类 + 裁剪 ──
        try:
            mem = getattr(self, "_companion_memory", None)
            scene_memory = getattr(self, "_scene_memory", None)
            if mem is not None:
                events = mem.read_events(days=30)
                if scene_memory is not None:
                    scene_memory.rebuild(events)
                    scene_memory.prune()
                mem.prune_events()
        except Exception as e:
            logger.warning("C 收盘聚类失败（非致命）: %s", e)

        # ── A/F 退订事件总线（防止全局注册表累积、多宠串扰、回调持有已销毁实例）──
        try:
            from core.event_bus import EventBus
            if getattr(self, "_activity_event_subscribed", False):
                EventBus.off("activity_event", self._on_activity_event)
                self._activity_event_subscribed = False
            if getattr(self, "_pet_set_mode_subscribed", False):
                EventBus.off("pet_set_mode", self._on_pet_set_mode)
                self._pet_set_mode_subscribed = False
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)

        # ── F 本地状态口停止（默认未启动则空操作）──
        try:
            if getattr(self, "_status_http", None) is not None:
                self._status_http.stop()
                self._status_http = None
        except Exception as e:
            logger.warning("F 状态口停止失败（非致命）: %s", e)

        super().closeEvent(event)


