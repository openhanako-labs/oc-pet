"""桌面宠物主窗口"""
import os
import json
import math
import random
import time
import logging
from pathlib import Path
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QMenu, QDialog, QFormLayout,
    QSystemTrayIcon, QSlider, QStyle
)
from PySide6.QtCore import (
    Qt, QTimer, QPoint, QRect, QEvent,
    QPropertyAnimation, QEasingCurve, Signal
)
from PySide6.QtGui import (
    QPixmap, QPainter, QFont, QColor, QPen, QPainterPath,
    QFontMetrics, QAction, QIcon, QTransform, QImage,
    QCursor
)
from config import CHARACTER_INFO, EXPRESSION_MAP, load_config, save_config
from core.hanako_monitor import HanakoMonitor, compact_bubble_text

from motion.behavior import BehaviorParams, BEHAVIOR_MODES
from motion.behavior import MOUSE_REACTIONS, MouseReactionParams
from motion.behavior import (
    PHYSICS_INTERVAL, INERTIA_FACTOR, INTENT_FACTOR,
    ARRIVAL_DISTANCE, WALK_SPEED_BASE,
    BOUNCE_ELASTICITY, BOUNCE_FRICTION, BOUNCE_GRAVITY, BOUNCE_MIN_SPEED
)
from ui.bubble import ChatBubble

from motion.action_linker import ActionLinker
from motion.foreground_watcher import ForegroundWatcher
from ui.tts_player import TTSTtsPlayer
from ui.startup_screen import StartupScreen
from core.perception import PerceptionController, ProactiveScheduler
from core.pet_audio_bridge import PetAudioBridge, PetAudioCallbacks, AudioType
from motion.physics import PhysicsEngine, MotionStateMachine, PhysicsCallbacks
from avatar.sprite_renderer import SpriteRenderer

from core.conversation_engine import ConversationEngine
from core.narrative_engine import NarrativeEngine
from motion.mouse_tracker import MouseTracker
from core.window_interaction import WindowInteraction

logger = logging.getLogger(__name__)

# 延迟导入语音输入（依赖 sounddevice + whisper）
try:
    from voice_input import VoiceInput, preload_whisper
    _voice_available = True
except ImportError:
    _voice_available = False
    logger.info("VoiceInput not available (install sounddevice + whisper)")

# ─── 设置对话框 ─────────────────────────────────────────

class PetWindow(QWidget):
    """透明桌面宠物窗口"""

    # 跨线程信号：后台线程 -> 主线程
    engine_reply_signal = Signal(str, str, str, str)  # reply, emotion, anim, audio_path
    engine_status_signal = Signal(str)  # status message
    voice_status_signal = Signal(str)  # voice input status
    screen_emotion_signal = Signal(str, float)  # emotion, intensity
    screen_proactive_signal = Signal(str)  # prompt

    def __init__(self, agent_id: str = "ophelia", sprite_dir: str = None,
                 position: dict = None, scale: float = 1.0,
                 on_position_change: callable = None,
                 pet_manager=None):
        super().__init__()
        self.config = load_config()
        self._agent_id = agent_id
        self._sprite_dir = sprite_dir  # None = 用默认 characters/ 目录
        self._on_position_change = on_position_change  # 位置变化回调
        self._pet_manager = pet_manager  # 多桌宠管理器引用
        self._init_position = position  # 初始位置（供 _setup_window 使用）

        # ── 交互状态 ──
        self._drag_start_cursor = QPoint()
        self._drag_start_window = QPoint()
        self._is_dragging = False
        self._was_click = False
        self._drag_poll_timer = QTimer(self)
        self._drag_poll_timer.timeout.connect(self._drag_poll_tick)
        self._drag_last_pos = QPoint()   # 上一帧拖拽位置,用于速度计算
        self._drag_last_time = 0.0
        self._vy = 0.0                   # 垂直速度(弹跳用)
        self._bounce_active = False      # 弹跳模式中
        self._is_sitting = False         # 是否坐在窗口边缘
        self._sitting_edge = ""           # 坐在哪条边: top/bottom/left/right

        self._current_char = agent_id
        self._is_thinking = False

        self._pet_scale = scale
        self._pet_opacity = self.config.get("opacity", 1.0)
        self._behavior_mode = self.config.get("behavior", "normal")

        # ── 动画状态 ──
        self._bob_frame = 0
        self._bob_offset = 0
        self._label_base_pos = QPoint(0, 0)
        self._target_x = 0
        self._vx = 0.0
        # 合并 physics + bob + gaze 为单个 30ms 定时器（减少事件循环压力）
        self._unified_timer = QTimer(self)
        self._unified_timer.timeout.connect(self._unified_tick)
        self._unified_timer.start(PHYSICS_INTERVAL)  # 30ms, ~33Hz
        self._motion_state = "idle"   # idle / wander / rest
        self._rest_counter = 0
        self._motion_timer = QTimer(self)
        self._motion_timer.timeout.connect(lambda: self._motion.tick(self._get_behavior_params()))
        self._motion_timer.start(500)
        self._is_walking = False

        # ── Hanako 状态监控 ──
        self._hanako_monitor = HanakoMonitor(on_state_change=self._on_hanako_state)

        # Hanako 状态轮询(文件桥接模式)
        self._hanako_poll_timer = QTimer(self)
        self._hanako_poll_timer.timeout.connect(self._hanako_monitor.tick)
        self._hanako_poll_timer.start(800)
        self._bubble_message = ""    # 当前气泡文字,用于超时隐藏
        self._bubble_timer = QTimer(self)
        self._bubble_timer.timeout.connect(self._clear_hanako_bubble)
        self._bubble_timer.setSingleShot(True)

        # ── 空闲检查定时器 ──
        self._break_timer = QTimer(self)
        self._break_timer.timeout.connect(self._break_check)

        # ── 动作联动 ──
        al_cfg = self.config.get("action_linker", {})
        self._action_linker = ActionLinker(
            character_id=self._current_char,
            highlight_duration=al_cfg.get("highlight_duration", 30),
            enabled=al_cfg.get("enabled", True),
        )

        # ── 前景窗口检测 ──
        self._foreground_watcher = ForegroundWatcher()
        self._foreground_watcher.on_change = self._on_foreground_change
        self._foreground_watcher.start()
        self._foreground_timer = QTimer(self)
        self._foreground_timer.timeout.connect(self._foreground_tick)

        # ── Proactive 主动对话调度器(P1)──
        proactive_cfg = self.config.get("proactive", {})
        self._proactive = ProactiveScheduler(
            foreground_watcher=self._foreground_watcher,
            on_proactive=self._on_proactive_trigger,
        )
        self._proactive.load_config(proactive_cfg)
        self._proactive_grace = time.time() + 120  # 启动后 2 分钟内不触发主动对话

        # ── 感知控制器(P2: 时间 + 情绪状态机 + 日程)──
        self._perception = PerceptionController(self._current_char)
        # 屏幕内容→情绪回调
        self._perception.screen.on_emotion = self._on_screen_emotion
        self._perception.screen.on_screen_proactive = self._on_screen_proactive
        
        # ── 屏幕感知开关（从配置读取）──
        screen_cfg = self.config.get("screen", {})
        if not screen_cfg.get("enabled", True):
            self._perception.screen.disable()
            logger.info("Screen perception disabled by config")
        # 截图模糊开关
        if not screen_cfg.get("blur", True):
            self._perception.screen._blur_enabled = False
            logger.info("Screen blur disabled by config")

        # ── 鼠标交互追踪器 ──
        self._mouse_tracker = MouseTracker(self._get_window_rect)
        self._mouse_reaction_params = MOUSE_REACTIONS.get(
            self._behavior_mode, MOUSE_REACTIONS["normal"]
        )
        self._mouse_tracker.on_nearby = self._on_mouse_nearby
        self._mouse_tracker.on_hover = self._on_mouse_hover
        self._mouse_tracker.on_chase = self._on_mouse_chase
        self._mouse_tracker.on_startled = self._on_mouse_startled
        self._mouse_tracker.on_leave = self._on_mouse_leave
        self._mouse_last_scene = "idle"  # 用于去重
        self._mouse_tracker_timer = QTimer(self)
        self._mouse_tracker_timer.timeout.connect(self._mouse_tracker.tick)
        self._mouse_tracker_timer.start(200)
        # 视线跟随由 unified_timer 驱动，不再单独开定时器

        # ── TTS provider ──
        tts_provider = self._create_tts_provider()

        # ── 检测是否是内置角色 ──
        is_builtin = False
        if self._pet_manager:
            for agent in self._pet_manager.agents:
                if agent["id"] == self._current_char:
                    is_builtin = agent.get("builtin", False)
                    break

        # ── 对话引擎（合并 bridge，单进程）──
        self._engine = ConversationEngine(
            self._current_char, perception=self._perception,
            tts_provider=tts_provider, builtin=is_builtin
        )
        self._engine.on_reply = self._on_engine_reply
        self._engine.on_status = self._on_engine_status
        self._engine.on_tts_ready = lambda: logger.info("Engine TTS ready")

        # ── M1: 叙事引擎 ──
        self._narrative_enabled = True  # 可通过配置开关
        narrative_cfg = self.config.get("narrative", {})
        self._narrative_enabled = narrative_cfg.get("enabled", True)
        self._narrative = NarrativeEngine(
            character_id=self._current_char,
            perception=self._perception,
            adapter=self._engine._adapter if self._engine else None,
            cooldown_minutes=narrative_cfg.get("cooldown_minutes", 15),
        )
        # 将叙事事件注入气泡显示流程
        self._narrative.on_event = self._on_narrative_event
        # 启动后台叙事循环（每 10 分钟尝试一次，实际受冷却控制）
        if self._narrative_enabled:
            try:
                self._narrative.start_background_loop(interval_seconds=600)
                logger.info("NarrativeEngine started for %s", self._current_char)
            except Exception as e:
                logger.warning("Failed to start NarrativeEngine: %s", e)
        # 连接跨线程信号
        self.engine_reply_signal.connect(self._do_engine_reply)
        self.engine_status_signal.connect(self._do_engine_status)
        self.voice_status_signal.connect(self._do_voice_status)
        self.screen_emotion_signal.connect(self._do_screen_emotion)
        self.screen_proactive_signal.connect(self._do_screen_proactive)
        self._engine.start()

        # ── 语音输入（ASR）──
        asr_provider = self._create_asr_provider()
        self._voice_input = None
        self._voice_recording = False
        if _voice_available:
            self._voice_input = VoiceInput(asr_provider=asr_provider)
            self._voice_input._on_status = self._on_voice_status
            # 后台预加载 Whisper 模型
            preload_whisper()

        # ── TTS 播放器 ──
        tts_cfg = self.config.get("tts", {})
        self._tts_player = TTSTtsPlayer()
        self._tts_player.set_volume(tts_cfg.get("volume", 0.8))
        if not tts_cfg.get("enabled", True):
            self._tts_player.disable()

        # TTS 口型回调
        self._tts_player.on_start = self._on_tts_start
        self._tts_player.on_end = self._on_tts_end
        self._tts_player.on_error = lambda msg: self._on_tts_end()

        # ── AUDIO-07: 桌宠音频事件桥接器 ──
        self._audio_bridge = PetAudioBridge(self)
        try:
            self._audio_bridge.connect()
            logger.info("AUDIO-07: PetAudioBridge connected")
        except Exception as e:
            logger.warning("AUDIO-07: Failed to connect bridge: %s", e)

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
        self._anim_timer.timeout.connect(self._anim_tick)
        self._setup_animation()

        # ── 物理引擎（委托）──
        self._physics = PhysicsEngine(self)
        self._motion = MotionStateMachine(self._physics, self)

        # ── 窗口互动模块 ──
        self._window_interaction = WindowInteraction(self)

        self._setup_menu()
        self._setup_tray()
        self.load_character(self._current_char)
        self._startup_screen.raise_()  # 确保启动画面在角色立绘之上
        self._break_timer.start(30000)  # 每 30 秒检查一次空闲
        self._foreground_timer.start(2000)  # 每 2 秒检测前台窗口

        # ── proactive 默认启用(由 config 控制)──
        if not proactive_cfg.get("enabled", True):
            self._proactive.disable()

        # ── 空闲时间追踪 ──
        self._current_anim = "idle"
        self._last_interaction = time.time()
        self._idle_stage = None

        # ── 恢复窗口状态 ──
        self._recalc_geometry()
        self.setWindowOpacity(self._pet_opacity)
        # ── 对话记忆跟踪 ──
        self._pending_user_msg = ""  # 等待配对的用户消息
        self._pending_emotion = "neutral"  # 等待配对的 emotion
        self._pending_chat = False  # 是否正在等待 Agent 回复


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
        self.setFixedSize(200, 360)

        win_cfg = self._init_position or self.config.get("window", {})
        if win_cfg.get("x", -1) >= 0 and win_cfg.get("y", -1) >= 0:
            self.move(win_cfg["x"], win_cfg["y"])
        else:
            sg = self._current_screen_geometry()
            self.move(sg.width() - 250, sg.height() - 350)

    def _apply_penetration(self):
        """应用当前鼠标穿透状态"""
        self.setAttribute(Qt.WA_TransparentForMouseEvents, self._mousePassthrough)
        if hasattr(self, 'char_label') and self.char_label:
            self.char_label.setAttribute(Qt.WA_TransparentForMouseEvents, self._mousePassthrough)
        if hasattr(self, 'status_label') and self.status_label:
            self.status_label.setAttribute(Qt.WA_TransparentForMouseEvents, self._mousePassthrough)

    def _toggle_passthrough(self):
        """切换鼠标穿透"""
        self._mousePassthrough = not self._mousePassthrough
        self._apply_penetration()
        if self._mousePassthrough:
            self.input_widget.hide()
            self.bubble.hide_bubble()
            # 状态栏提示穿透已启用(3s后恢复)
            self._status_label.setText("🖱️ 穿透中")
            self._status_label.setStyleSheet("""
                QLabel {
                    background: rgba(60, 40, 80, 220);
                    color: #cc88ff;
                    border: 1px solid #cc88ff60;
                    border-radius: 10px;
                    font-size: 9px;
                    padding: 2px 6px;
                }
            """)
            self._status_label.show()
            self._reposition_status_label()
            QTimer.singleShot(3000, self._restore_status_label)

    # ── 系统托盘 ──

    def _setup_tray(self):
        """初始化系统托盘图标"""
        self._tray = QSystemTrayIcon(self)
        # 用角色首帧做托盘图标
        px = self._make_tray_icon()
        self._tray.setIcon(QIcon(px))
        self._tray.setToolTip("OC Desktop Pet")
        tray_menu = QMenu()
        tray_menu.setStyleSheet("""
            QMenu { background: rgba(25,25,35,230); color: #e6e6f0;
                     border: 1px solid rgba(80,80,120,100); border-radius: 8px; padding: 4px; }
            QMenu::item { padding: 6px 20px; border-radius: 4px; }
            QMenu::item:selected { background: rgba(70,90,160,150); }
        """)
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
        if self.isVisible():
            self.hide()
        else:
            self.show()

    def _trigger_action(self, action_id: str):
        """用户点击动作联动项"""
        basedir = Path(__file__).parent / "data"
        self._action_linker.trigger_action(basedir, action_id)
        self._show_bubble(f"{action_id}!", emotion="happy")

    def _recalc_geometry(self):
        """缩放后重算窗口和角色图片尺寸(不改变窗口位置)"""
        w = max(200, int(200 * self._pet_scale))
        h = max(360, int(360 * self._pet_scale))
        self.setFixedSize(w, h)
        # 委托给 SpriteRenderer 处理角色尺寸
        self._renderer.set_scale(self._pet_scale)
        self._renderer.recalc_geometry(w, h)
        QTimer.singleShot(50, self._store_label_pos)
        QTimer.singleShot(50, self._reposition_status_label)
        QTimer.singleShot(50, self._reposition_bubble)

    def _apply_scale(self):
        """应用缩放设置"""
        self._recalc_geometry()

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

        # 角色渲染器(帧精灵 / 未来 Live2D / VRM)
        self._renderer = SpriteRenderer(self)
        # 兼容别名(供 pet.py 其他部分使用)
        self.char_label = self._renderer.label
        self.char_label.installEventFilter(self)

        # 启动画面
        self._startup_screen = StartupScreen(self)
        self._startup_screen.show_for_character(self._current_char)

        # 气泡(顶层)
        self.bubble = ChatBubble(self)
        self.bubble.move(0, 0)
        self.bubble.raise_()

        # 状态指示器(左下角悬浮)
        self._status_label = QLabel(self)
        self._status_label.setAlignment(Qt.AlignCenter)
        self._status_label.setFixedSize(68, 20)
        self._status_label.setStyleSheet("""
            QLabel {
                background: rgba(30, 30, 50, 200);
                color: #aaaacc;
                border: 1px solid rgba(80, 80, 120, 80);
                border-radius: 10px;
                font-size: 9px;
                padding: 2px 6px;
            }
        """)
        self._status_label.setText("⚪ 空闲")
        self._status_label.hide()

        # 底部输入区
        self.input_widget = QWidget(self)
        self.input_widget.setFixedSize(200, 40)
        self.input_widget.setStyleSheet("background: transparent;")
        input_layout = QHBoxLayout(self.input_widget)
        input_layout.setContentsMargins(4, 2, 4, 2)
        input_layout.setSpacing(4)

        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("说点什么...")
        self.input_field.setStyleSheet("""
            QLineEdit {
                background: rgba(20, 20, 30, 180);
                color: #e6e6f0;
                border: 1px solid rgba(100, 100, 140, 100);
                border-radius: 12px;
                padding: 6px 12px;
                font-size: 11px;
            }
            QLineEdit:focus {
                border-color: rgba(140, 160, 255, 150);
            }
        """)
        self.input_field.returnPressed.connect(self._send_message)

        self.send_btn = QPushButton("➤")
        self.send_btn.setFixedSize(30, 30)
        self.send_btn.setStyleSheet("""
            QPushButton {
                background: rgba(60, 70, 120, 180);
                color: #e6e6f0;
                border: none;
                border-radius: 15px;
                font-size: 14px;
            }
            QPushButton:hover {
                background: rgba(80, 100, 180, 200);
            }
        """)
        self.send_btn.clicked.connect(self._send_message)

        input_layout.addWidget(self.input_field)
        input_layout.addWidget(self.send_btn)
        self.input_widget.hide()

        self.main_layout.addStretch()
        self.main_layout.addWidget(self.input_widget, 0, Qt.AlignCenter)

        # 状态指示器移到右下角
        QTimer.singleShot(100, self._reposition_status_label)

    # ── 动画 ──

    def _setup_animation(self):
        # 帧动画时钟(idle 默认 4fps,walk 6fps)
        self._anim_timer.start(200)
        # 呼吸浮动由 unified_timer 驱动，不再单独开定时器

    def _bob_tick(self):
        self._bob_frame += 1
        self._bob_offset = int(math.sin(self._bob_frame * 0.06) * 2.5)
        if not self._is_dragging:
            # 视线偏移 + 呼吸浮动叠加
            ox = self._renderer._base_label_pos.x() + int(self._renderer._gaze_offset_x)
            oy = self._renderer._base_label_pos.y() + int(self._renderer._gaze_offset_y) + self._bob_offset
            self.char_label.move(ox, oy)

    def _gaze_tick(self):
        """视线跟随平滑更新"""
        self._renderer.update_gaze()

    def _unified_tick(self):
        """统一高频定时器回调（30ms）— 合并 physics + bob + gaze"""
        # 1. 物理模拟
        self._physics.tick(self._get_behavior_params())
        # 2. 呼吸浮动
        self._bob_tick()
        # 3. 视线跟随（每 2 帧更新一次，~15fps 足够）
        if self._bob_frame % 2 == 0:
            self._gaze_tick()

    def _set_anim_seq(self, seq_name, emotion=None):
        """切换动画序列 - 委托给 SpriteRenderer"""
        self._renderer.play_anim(seq_name, emotion=emotion)
        self._anim_seq = self._renderer._anim_seq
        self._anim_idx = self._renderer._anim_idx
        self._anim_range = self._renderer._anim_range

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

    # ── TTS 口型 ──

    # ── PetAudioCallbacks 实现（AUDIO-07）──

    def on_tts_start(self, emotion: str = "neutral") -> None:
        """AUDIO-07 回调：TTS 开始 → PET-02 口型"""
        frames = self._renderer._frames
        if emotion in ('happy', 'angry', 'surprised'):
            speak_seq = 'speak_open'
        else:
            speak_seq = 'speak_half'
        for seq in (speak_seq, 'speak_open', 'speak_half', 'speak_closed'):
            if seq in frames:
                self._renderer.play_anim(seq)
                self._anim_seq = seq
                logger.debug("AUDIO-07 TTS mouth: %s (emotion=%s)", seq, emotion)
                return
        logger.debug("AUDIO-07 TTS mouth: no speak frames, skip")

    def on_tts_end(self) -> None:
        """AUDIO-07 回调：TTS 结束 → 恢复 idle"""
        self._set_anim_seq('idle')
        logger.debug("AUDIO-07 TTS mouth: restored idle")

    def on_music_start(self, track_name: str = "") -> None:
        """AUDIO-07 回调：音乐开始 → 强制闭嘴"""
        self._set_anim_seq('idle')  # 确保不处于说话状态
        logger.info("AUDIO-07 music start: %s (mouth closed)", track_name)

    def on_music_end(self) -> None:
        """AUDIO-07 回调：音乐结束 → 恢复正常"""
        logger.info("AUDIO-07 music end")

    def on_notification_play(self) -> None:
        """AUDIO-07 回调：提示音 → 瞬时反应"""
        # 可选：触发 extra 帧 blink/jump
        logger.debug("AUDIO-07 notification played")

    def on_volume_change(self, volume: float) -> None:
        """AUDIO-07 回调：音量变化"""
        pass

    def on_pause(self, audio_type) -> None:
        """AUDIO-07 回调：播放暂停"""
        logger.debug("AUDIO-07 pause: %s", audio_type.value if hasattr(audio_type, 'value') else audio_type)

    def on_resume(self, audio_type) -> None:
        """AUDIO-07 回调：播放恢复"""
        logger.debug("AUDIO-07 resume: %s", audio_type.value if hasattr(audio_type, 'value') else audio_type)

    # ── 旧接口兼容（直接由 TTSTtsPlayer 调用）──

    def _on_tts_start(self):
        """兼容 TTSTtsPlayer.on_start → 转发给桥接器"""
        self.on_tts_start(getattr(self, '_last_tts_emotion', 'neutral'))

    def _on_tts_end(self):
        """兼容 TTSTtsPlayer.on_end → 转发给桥接器"""
        self.on_tts_end()

    def _reposition_bubble(self):
        """气泡置于角色头顶上方,根据实际角色内容定位"""
        top_y = self._get_char_top_y()
        bw = self.bubble.width()
        bh = self.bubble.height()
        bx = (self.width() - bw) // 2
        by = top_y - bh - 20  # 头顶上方 20px，避免遮挡
        self.bubble.move(max(bx, 2), max(by, 2))

    # ── 右键菜单 ──

    def _setup_menu(self):
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        # 构建右键菜单
        self._menu = QMenu(self)
        self._menu.setStyleSheet("""
            QMenu { background: rgba(25,25,35,230); color: #e6e6f0; border: 1px solid rgba(80,90,140,120); border-radius: 8px; }
            QMenu::item { padding: 6px 20px; border-radius: 4px; }
            QMenu::item:selected { background: rgba(70,90,160,150); }
            QMenu::indicator { width: 0; }
        """)

        # 基础操作
        self._menu.addAction("💬 对话", self._toggle_input)
        self._voice_action = self._menu.addAction("🎤 说话", self._toggle_voice)

        # 行为模式子菜单
        self._behavior_submenu = self._menu.addMenu("🚶 行为")
        self._behavior_actions = {}
        for mode in ["quiet", "normal", "active", "cling"]:
            labels = {"quiet": "静默", "normal": "正常", "active": "活跃", "cling": "黏人"}
            a = self._behavior_submenu.addAction(labels.get(mode, mode))
            a.setCheckable(True)
            a.setChecked(mode == self._behavior_mode)
            a.triggered.connect(lambda checked, m=mode: self._switch_behavior_mode(m))
            self._behavior_actions[mode] = a

        # 动作联动(动态高亮)
        self._menu.addSeparator()
        self._action_menu_items = {}  # action_id -> QAction
        for action in self._action_linker.actions:
            a = self._menu.addAction(f"{action.emoji} {action.label}", lambda a_id=action.id: self._trigger_action(a_id))
            a.setVisible(False)  # 默认隐藏,匹配时高亮
            self._action_menu_items[action.id] = a

        self._menu.addSeparator()

        # 穿透 / 设置
        self._passthrough_action = self._menu.addAction("🔍 穿透", self._toggle_passthrough)
        self._passthrough_action.setCheckable(True)
        self._passthrough_action.setChecked(self._mousePassthrough)
        self._menu.addAction("⚙️ 设置", self._open_settings)
        self._menu.addAction("🔌 插件", self._open_plugin_panel)

        self._menu.addSeparator()
        self._menu.addAction("❌ 退出", self.close)

    def _toggle_input(self):
        """切换输入框显示"""
        if self.input_widget.isVisible():
            self.input_widget.hide()
        else:
            self.input_widget.show()
            self.input_field.setFocus()

    def _toggle_voice(self):
        """切换语音录音"""
        if not self._voice_input:
            self._show_bubble("语音输入不可用", emotion="neutral")
            return

        if not self._voice_recording:
            # 开始录音
            if self._voice_input.start():
                self._voice_recording = True
                self._voice_action.setText("⏹ 停止")
            else:
                self._show_bubble("录音启动失败", emotion="neutral")
        else:
            # 停止录音 -> 识别 -> 发送
            self._voice_action.setText("🎤 说话")
            self._voice_recording = False

            # 在后台线程识别，避免阻塞 UI
            import threading
            def _do_asr():
                text = self._voice_input.stop()
                if text:
                    # 通过引擎发送
                    self._engine.send(text, character=self._current_char)
                    logger.info("Voice input sent: %s", text[:30])
                    # 显示提示 + 思考状态
                    self.voice_status_signal.emit(f"🎤 {text[:30]}")
                    # 截停 TTS
                    self._tts_player.stop()
                    self._is_thinking = True
                    self._pending_chat = True
                    self._pending_user_msg = text
                else:
                    self.voice_status_signal.emit("没听清...")

            t = threading.Thread(target=_do_asr, daemon=True)
            t.start()

    def _on_voice_status(self, msg: str):
        """语音输入状态 - 从后台线程，通过信号转主线程"""
        self.voice_status_signal.emit(msg)

    def _do_voice_status(self, msg: str):
        """在主线程处理语音状态"""
        if msg:
            self._show_bubble(msg, emotion="thinking")
        else:
            try:
                self.bubble.hide_bubble()
            except Exception:
                pass

    def _create_tts_provider(self):
        """根据配置创建 TTS provider，失败返回 None"""
        provider = self.config.get("tts", {}).get("provider", "cosyvoice")
        try:
            if provider == "mimo":
                from tts_provider.mimo_tts import MimoTtsProvider
                from env_config import get_tts_api_config
                mimo = MimoTtsProvider()
                cfg = get_tts_api_config()
                mimo.configure(
                    base_url=cfg.get("base_url", ""),
                    api_key=cfg.get("api_key", ""),
                    model=cfg.get("model", ""),
                    voice=cfg.get("voice", "default_zh"),
                )
                return mimo
            elif provider == "api":
                from tts_provider.api_tts import ApiTtsProvider
                return ApiTtsProvider()
            else:
                from tts_provider.cosyvoice import CosyVoiceProvider
                return CosyVoiceProvider()
        except Exception as e:
            logger.warning("TTS provider 创建失败 (%s): %s", provider, e)
            return None

    def _create_asr_provider(self):
        """根据配置创建 ASR provider，失败返回 None"""
        provider = self.config.get("asr", {}).get("provider", "whisper_local")
        try:
            if provider == "mimo":
                from asr_provider.mimo_asr import MimoAsrProvider
                from env_config import get_asr_api_config
                mimo = MimoAsrProvider()
                cfg = get_asr_api_config()
                mimo.configure(
                    base_url=cfg.get("base_url", ""),
                    api_key=cfg.get("api_key", ""),
                    model=cfg.get("model", ""),
                )
                return mimo
            elif provider == "api":
                from asr_provider.api_asr import ApiAsrProvider
                return ApiAsrProvider()
            else:
                from asr_provider.whisper_local import WhisperLocalProvider
                return WhisperLocalProvider()
        except Exception as e:
            logger.warning("ASR provider 创建失败 (%s): %s", provider, e)
            return None

    def _open_settings(self):
        """打开配置面板"""
        from ui.settings_dialog import SettingsDialog
        dialog = SettingsDialog(parent=self, pet_manager=self._pet_manager)
        if dialog.exec():
            self.config = dialog.get_config()
            save_config(self.config)
            logger.info("配置已保存")
            # 应用即时生效的设置
            self._apply_settings()

    def _open_plugin_panel(self):
        """打开插件面板"""
        from ui.plugin_panel import PluginPanel
        panel = PluginPanel(on_send_command=self._send_plugin_command, parent=self)
        panel.exec()

    def _send_plugin_command(self, text: str):
        """从插件面板发送指令到对话引擎"""
        if self._engine:
            self._engine.send(text, character=self._current_char)
            self._tts_player.stop()
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
        if self._engine:
            new_provider = self._create_tts_provider()
            self._engine._tts = new_provider
            if new_provider:
                import threading
                def _reload_tts():
                    new_provider.preload()
                    self._engine._tts_ready = new_provider.is_ready
                    logger.info("TTS provider 已切换: %s (ready=%s)",
                                new_provider.name, new_provider.is_ready)
                threading.Thread(target=_reload_tts, daemon=True).start()

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

    # ── 角色加载 ──

    def load_character(self, char_id: str):
        """加载角色 - 委托给 SpriteRenderer"""
        self._current_char = char_id

        # 委托给渲染器加载帧序列，优先使用 sprite_dir
        self._renderer.load(char_id, sprite_dir=self._sprite_dir)
        # 同步状态别名
        self._anim_frames = self._renderer._frames
        self._anim_frame_tops = self._renderer._frame_tops

        # 更新托盘图标
        self._tray.setIcon(QIcon(self._make_tray_icon()))

        # 启动画面
        self._startup_screen.show_for_character(char_id)

        # 重新定位气泡
        self._reposition_bubble()

        QTimer.singleShot(50, self._store_label_pos)

    def _store_label_pos(self):
        self._label_base_pos = self.char_label.pos()
        self._renderer.set_label_base_pos(self._label_base_pos)

    # ── 事件过滤器:统一处理点按/拖拽 ──

    def eventFilter(self, obj, event):
        if obj is self.char_label:
            t = event.type()
            import time as _time
            _t0 = _time.perf_counter()

            if t == QEvent.MouseButtonPress:
                if event.button() == Qt.LeftButton:
                    # 退出坐下状态
                    if self._is_sitting:
                        self._exit_sitting()
                    self._drag_start_cursor = QCursor.pos()
                    self._drag_start_window = self.pos()
                    self._is_dragging = False
                    self._was_click = True
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
                        self._was_click = False
                        self.char_label.setCursor(QCursor(Qt.ClosedHandCursor))
                        self._drag_poll_timer.start(16)
                    if self._is_dragging:
                        self.move(self._drag_start_window + delta)
                return True

            elif t == QEvent.MouseButtonRelease:
                if event.button() == Qt.LeftButton:
                    self._drag_poll_timer.stop()
                    if self._is_dragging:
                        self.char_label.setCursor(QCursor(Qt.ArrowCursor))
                        self._is_dragging = False
                        self._store_label_pos()

                        # ── 弹跳:释放时计算拖拽速度 ──
                        now = time.time()
                        cursor = QCursor.pos()
                        dt = now - self._drag_last_time
                        if dt > 0 and dt < 0.2:
                            dx = cursor.x() - self._drag_last_pos.x()
                            dy = cursor.y() - self._drag_last_pos.y()
                            vx = dx / dt * 0.02
                            vy = dy / dt * 0.02
                            speed = math.sqrt(vx ** 2 + vy ** 2)
                            if speed > 1.5:
                                self._bounce_active = True
                                self._is_walking = False
                                self._motion_state = "bounce"
                                self._set_anim_seq('walk')
                                self._physics.start_bounce(vx, vy)
                            else:
                                self._bounce_active = False
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
                        save_config(self.config)
                        if self._on_position_change:
                            self._on_position_change(pos.x(), pos.y())
                    elif self._was_click:
                        self._toggle_chat()
                        self._motion_state = "idle"
                    self._was_click = False
                _elapsed = (_time.perf_counter() - _t0) * 1000
                if _elapsed > 16:  # 超过一帧的时间才告警
                    logger.warning("eventFilter[release] slow: %.1fms", _elapsed)
                return True

            elif t == QEvent.MouseButtonDblClick:
                # 双击可留作扩展
                return True

        return super().eventFilter(obj, event)

    # ── 拖拽轮询 ──

    def _drag_poll_tick(self):
        """拖拽时每 16ms 轮询鼠标位置(不掉事件)"""
        if self._is_dragging:
            cursor = QCursor.pos()
            delta = cursor - self._drag_start_cursor
            self.move(self._drag_start_window + delta)
            # 记录用于释放后速度估算
            self._drag_last_pos = cursor
            self._drag_last_time = time.time()

    # ── 窗口边缘吸附坐下 ──

    SIT_THRESHOLD = 30   # 距离边缘多少 px 触发吸附
    SIT_ROTATE = 12      # 坐下时旋转角度

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
        save_config(self.config)
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
        # 恢复帧渲染
        self._renderer._show_frame()

        logger.info("Stopped sitting")

    def _apply_sitting_rotation(self, edge: str):
        """坐下时应用视觉旋转效果"""
        from PySide6.QtWidgets import QGraphicsRotation, QGraphicsProxyWidget
        # 简单方案：用 transform 旋转 char_label 的 pixmap
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

    def _toggle_chat(self):
        self._stop_walking()
        if self.input_widget.isVisible():
            self.input_widget.hide()
            self.input_field.clear()
        else:
            self.input_widget.show()
            self.input_widget.raise_()
            self.input_field.setFocus()

    def _send_message(self):
        text = self.input_field.text().strip()
        if not text or self._is_thinking:
            return

        self.input_field.clear()
        self.input_widget.hide()

        # P2: 用户交互 -> 重置情绪状态机
        try:
            self._perception.reset_emotion()
        except Exception:
            pass

        # 标记对话时间（主动对话用）
        if self._perception.proactive:
            self._perception.proactive.mark_conversation()

        # ── 用户发新消息 → 立即截停旧 TTS(P2 可中断管线)──
        self._tts_player.stop()

        # 通过对话引擎发送（异步）
        if self._engine:
            self._engine.send(text, character=self._current_char)

        self.bubble.set_text("⏳ 思考中...")
        self._reposition_bubble()
        self.bubble.show()
        self.bubble.raise_()
        self._is_thinking = True
        self._pending_user_msg = text
        self._pending_emotion = "neutral"
        self._pending_chat = True

        # 立即切换到思考动画（视觉反馈）
        try:
            self._set_anim_seq("working", emotion="thinking")
        except Exception:
            pass

        # 超时保护：30 秒无回复自动恢复
        if not hasattr(self, '_think_timeout'):
            from PySide6.QtCore import QTimer as _QTimer
            self._think_timeout = _QTimer()
            self._think_timeout.setSingleShot(True)
            self._think_timeout.timeout.connect(self._on_think_timeout)
        self._think_timeout.start(30000)

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
                pass

    def _on_think_timeout(self):
        """LLM 超时：自动恢复 idle 状态"""
        if self._is_thinking:
            logger.warning("LLM response timeout (30s), resetting to idle")
            self._is_thinking = False
            self._pending_chat = False
            self.bubble.hide_bubble()
            self._set_anim_seq('idle')
            self._show_bubble("…信号不太好", emotion="sad")

    def _clear_hanako_bubble(self):
        """清除气泡（超时回调）"""
        if hasattr(self, 'bubble'):
            try:
                self.bubble.hide_bubble()
            except Exception:
                pass
            self._bubble_message = ""

    def _on_engine_reply(self, reply: str, emotion: str, anim: str, audio_path: str):
        """对话引擎回复回调 - 从后台线程调用，通过信号转到主线程"""
        # 从 Python threading.Thread 调 QTimer.singleShot 不可靠
        # 用 Signal 发射，Qt 会自动跨线程投递到主线程
        self.engine_reply_signal.emit(reply, emotion, anim, audio_path)

    def _do_engine_reply(self, reply: str, emotion: str, anim: str, audio_path: str):
        """在主线程中处理引擎回复"""
        # 取消超时计时器
        if hasattr(self, '_think_timeout'):
            self._think_timeout.stop()

        # 截停旧 TTS
        self._tts_player.stop()

        # 显示气泡
        if reply and reply.strip() and reply.strip() not in ("\u2026", "..."):
            try:
                compact = compact_bubble_text(reply)
            except Exception:
                compact = reply
            self._show_bubble(compact or reply, emotion=emotion)
        else:
            # 空回复也要清除"思考中"气泡
            try:
                self.bubble.hide_bubble()
            except Exception:
                pass

        # 播放音频（和文字一起）
        if audio_path and os.path.exists(audio_path):
            tts_cfg = self.config.get("tts", {})
            if tts_cfg.get("enabled", True):
                logger.info("Playing TTS: %s", audio_path)
                self._last_tts_emotion = emotion or "neutral"
                self._tts_player.play(audio_path)

        # 动画
        try:
            self._set_anim_seq(anim, emotion=emotion)
        except Exception:
            pass

        # 触发情绪状态机
        if emotion and emotion != "neutral":
            try:
                self._perception.trigger_emotion(emotion)
            except Exception:
                pass

        # 重置状态
        if self._pending_chat:
            self._pending_user_msg = ""
            self._pending_chat = False

        # 重置 idle
        self._is_thinking = False
        self._idle_stage = None
        self._last_interaction = time.time()

    def _on_engine_status(self, msg: str):
        """引擎状态提示 - 从后台线程调用，通过信号转到主线程"""
        self.engine_status_signal.emit(msg)

    def _do_engine_status(self, msg: str):
        """在主线程中处理引擎状态"""
        if msg:
            self._show_bubble(msg, emotion="thinking")
        else:
            try:
                self.bubble.hide_bubble()
            except Exception:
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
        save_config(self.config)
        if self._on_position_change:
            self._on_position_change(pos.x(), pos.y())
        params = self._get_behavior_params()
        self._motion._start_rest(params)

    def on_bounce_finished(self, x: int, y: int):
        self._motion_state = "idle"
        self._bounce_active = False
        self.config.setdefault("window", {})["x"] = x
        self.config.setdefault("window", {})["y"] = y
        save_config(self.config)

    def on_facing_change(self, facing_right: bool):
        self._facing_right = facing_right

    def set_anim(self, anim: str):
        # atlas 格式：walk → running-right/left（根据朝向）
        if anim == 'walk':
            if 'running-right' in self._renderer._frames:
                anim = 'running-right' if self._facing_right else 'running-left'
        self._set_anim_seq(anim)

    # ── Hanako 状态回调 ──

    def _reposition_status_label(self):
        """将状态指示器放在窗口右下角"""
        sw = self._status_label.width()
        sh = self._status_label.height()
        self._status_label.move(self.width() - sw - 6, self.height() - sh - 6)

    def _restore_status_label(self):
        """穿透提示后恢复为状态指示，然后淡出隐藏"""
        self._update_status_indicator(self._hanako_monitor.current_state_name)
        # 3 秒后淡出隐藏
        QTimer.singleShot(3000, self._status_label.hide)

    def _update_status_indicator(self, state_name: str):
        """更新持久化状态指示器"""
        from core.hanako_monitor import STATE_LABELS
        label = STATE_LABELS.get(state_name, f"⚪ {state_name}")
        self._status_label.setText(label)

        # 状态颜色映射
        colors = {
            "idle": ("#aaaacc", "rgba(30,30,50,200)"),
            "listening": ("#88dd88", "rgba(30,60,30,200)"),
            "thinking": ("#ddcc66", "rgba(60,50,20,200)"),
            "working": ("#6699ff", "rgba(20,40,80,200)"),
            "speaking": ("#88bbff", "rgba(20,50,80,200)"),
        }
        tc, bg = colors.get(state_name, colors["idle"])
        self._status_label.setStyleSheet(f"""
            QLabel {{
                background: {bg};
                color: {tc};
                border: 1px solid {tc}40;
                border-radius: 10px;
                font-size: 9px;
                padding: 2px 6px;
            }}
        """)
        self._status_label.show()
        self._reposition_status_label()

    # ── 空闲时间追踪(idle 超时递进)──


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

        # Proactive 主动对话
        try:
            if time.time() > self._proactive_grace:
                self._proactive.tick()
        except Exception:
            pass

        # 感知系统 tick(情绪衰减 + 主动对话 + 日程刷新)
        try:
            self._perception.tick()
        except Exception:
            pass

    def _foreground_tick(self):
        """每 2 秒检测前台窗口"""
        logger.debug("_foreground_tick called")
        try:
            self._foreground_watcher.tick()
        except Exception as e:
            logger.error("_foreground_tick error: %s", e)

    def _on_foreground_change(self, app_name: str, app_category: str, title: str):
        """前台窗口变化 → 重置 idle 计时器 + 窗口互动 + 事件触发截图"""
        going = self._idle_stage
        self._last_interaction = time.time()
        self._idle_stage = None
        if going is not None:
            self._show_bubble("你回来啦~", emotion="happy")
        
        # 窗口互动：桌宠靠近当前窗口（带冷却）
        if hasattr(self, '_window_interaction'):
            wi_config = self.config.get('window_interaction', {})
            if wi_config.get('enabled', True):
                cooldown = wi_config.get('cooldown_seconds', 30)
                now = time.time()
                if not hasattr(self, '_last_move_near'):
                    self._last_move_near = 0
                if now - self._last_move_near >= cooldown:
                    try:
                        self._window_interaction.move_near_window()
                        self._last_move_near = now
                    except Exception as e:
                        logger.debug("Window interaction failed: %s", e)

        # 事件触发截图：前台切换时触发一次屏幕感知
        try:
            if hasattr(self, '_perception') and self._perception._screen:
                self._perception._screen.on_foreground_change(app_name, app_category, title)
        except Exception as e:
            logger.debug("Foreground screenshot trigger failed: %s", e)

    def _on_proactive_trigger(self, prompt_text: str):
        """Proactive 调度器触发 -> 发送给模型生成回复 + TTS"""
        logger.info("Proactive trigger: %s", prompt_text)
        
        # 将触发条件发送给对话引擎，让模型生成符合人格的回复
        if self._engine:
            # 包装成用户输入，让模型生成回复
            proactive_prompt = f"[主动对话触发] {prompt_text}\n\n请根据你的人格设定，生成一段简短的、有个性的回应。不要复述触发消息，而是自然地表达你的想法。"
            self._engine.send(proactive_prompt, character=self._current_char)
            
            # 显示思考中气泡
            self._show_bubble("⏳ 思考中...", emotion="thinking")
            self._is_thinking = True
        
        # 触发动画
        self._set_anim_seq("waving", emotion="happy")

    # ── M1: 叙事事件回调 ──

    def _on_narrative_event(self, event):
        """叙事引擎回调：将 NarrativeEvent 注入 UI 展示
        
        在后台线程调用，通过 _do_narrative_event 转主线程。
        """
        from PySide6.QtCore import QTimer
        # 切回主线程执行
        QTimer.singleShot(0, lambda: self._do_narrative_event(event))

    def _do_narrative_event(self, event):
        """在主线程处理叙事事件"""
        try:
            # 显示气泡（无 TTS，纯文字）
            if event.content and event.content.strip():
                self._show_bubble(event.content, emotion=event.emotion)
                # 动画
                anim = event.animation or "idle"
                self._set_anim_seq(anim, emotion=event.emotion)
                logger.info("Narrative displayed: [%s] %s | anim=%s", event.event_type, event.content[:30], anim)
        except Exception as e:
            logger.error("Narrative display failed: %s", e)

    # ── 鼠标交互反应 ──

    _mouse_reaction_cooldown: float = 0.0  # 上次反应时间

    def _get_window_rect(self) -> tuple[int, int, int, int] | None:
        """返回角色窗口 (x, y, w, h)，供 MouseTracker 使用"""
        p = self.pos()
        s = self.size()
        return (p.x(), p.y(), s.width(), s.height())

    def _gaze_tick(self):
        """每 50ms 更新视线跟随（平滑偏移）"""
        if not hasattr(self, '_renderer'):
            return
        params = self._mouse_reaction_params
        if params.gaze_enabled and self._mouse_tracker.is_nearby:
            state = self._mouse_tracker.state
            self._renderer.look_at(state.x, state.y)
        else:
            self._renderer.update_gaze()

    def _check_reaction_cooldown(self) -> bool:
        """检查是否在反应冷却中（3 秒内不重复）"""
        now = time.time()
        if now - self._mouse_reaction_cooldown < 5.0:
            return True  # 冷却中
        self._mouse_reaction_cooldown = now
        return False

    def _on_mouse_nearby(self):
        """鼠标进入角色附近 - 只切动画，不弹气泡"""
        params = self._mouse_reaction_params
        if not params.react_nearby:
            return
        if self._is_thinking or self._check_reaction_cooldown():
            return
        self._set_anim_seq(params.nearby_anim, emotion="surprised")

    def _on_mouse_hover(self):
        """鼠标在角色附近静止 - 只切动画"""
        params = self._mouse_reaction_params
        if not params.react_hover:
            return
        if self._is_thinking:
            return
        self._set_anim_seq("idle", emotion="thinking")

    def _on_mouse_chase(self, target_x: int):
        """鼠标长时间不动，走过去看看"""
        params = self._mouse_reaction_params
        if not params.chase_enabled:
            return
        if self._is_thinking or self._physics.is_active:
            return
        x, _ = self.get_pos()
        direction = 1 if target_x > x else -1
        distance = min(abs(target_x - x), 200)
        target = x + direction * distance
        sg = self._current_screen_geometry()
        target = max(10, min(target, sg.width() - self.width() - 10))
        self._motion_state = "chase"
        self._physics.start_walk(target, facing_right=(direction > 0))
        # _unified_timer 已在初始化时启动

    def _on_mouse_startled(self, speed: float):
        """鼠标快速掠过 - 只切动画"""
        params = self._mouse_reaction_params
        if not params.react_startle:
            return
        if self._is_thinking or self._check_reaction_cooldown():
            return
        self._set_anim_seq(params.startle_anim, emotion="surprised")

    def _on_mouse_leave(self):
        """鼠标离开角色附近"""
        self._renderer.reset_gaze()

    def _on_screen_emotion(self, emotion: str, intensity: float):
        """屏幕内容触发的情绪（从后台线程调用，通过信号转主线程）"""
        self.screen_emotion_signal.emit(emotion, intensity)

    def _on_screen_proactive(self, prompt: str):
        """屏幕内容触发主动对话（从后台线程调用，通过信号转主线程）"""
        self.screen_proactive_signal.emit(prompt)

    def _do_screen_emotion(self, emotion: str, intensity: float):
        """在主线程处理屏幕情绪"""
        try:
            self._perception.trigger_emotion(emotion, intensity)
            anim_map = {
                'happy': 'waving', 'surprised': 'jumping',
                'thinking': 'running', 'sad': 'failed',
            }
            anim = anim_map.get(emotion, 'idle')
            if anim in self._renderer._frames:
                self._set_anim_seq(anim, emotion=emotion)
        except Exception:
            pass

    def _do_screen_proactive(self, prompt: str):
        """在主线程处理屏幕内容主动对话"""
        try:
            # 显示气泡
            self._show_bubble(prompt, emotion="thinking")
            # 发送给对话引擎生成回复（会触发 TTS）
            if hasattr(self, '_engine') and self._engine:
                self._engine.send(prompt)
            elif hasattr(self, '_conversation_engine') and self._conversation_engine:
                self._conversation_engine.send(prompt)
        except Exception as e:
            logger.debug("Screen proactive failed: %s", e)

    def _show_bubble(self, text: str, emotion: str = "neutral"):
        """显示消息气泡"""
        if not text or not hasattr(self, 'bubble'):
            return
        try:
            self._is_thinking = False
            self._bubble_message = text
            self.bubble.set_text(text, bright=(emotion == "happy"))
            self._reposition_bubble()
            self.bubble.show()
            self.bubble.raise_()
            self._bubble_timer.start(6000)
        except Exception:
            pass

    def _show_context_menu(self, pos):
        """右键菜单"""
        if not hasattr(self, '_menu'):
            return
        # 更新动态部分
        if hasattr(self, '_behavior_actions'):
            for mode, a in self._behavior_actions.items():
                a.setChecked(mode == self._behavior_mode)
        if hasattr(self, '_action_menu_items') and hasattr(self, '_action_linker'):
            highlighted = self._action_linker.highlighted_actions
            for aid, a in self._action_menu_items.items():
                a.setVisible(aid in highlighted)
        if hasattr(self, '_passthrough_action'):
            self._passthrough_action.setChecked(self._mousePassthrough)
        try:
            self._menu.popup(self.mapToGlobal(pos))
        except Exception:
            pass



    def _on_hanako_state(self, anim_name: str, message: str, emotion: str = "neutral", state: str = "idle", audio_path: str = ""):
        """Hanako 状态变化时的回调 - 增强：支持情绪映射 + 状态指示 + 动作联动 + 记忆写入 + 错误隔离"""
        # 总是更新状态指示器
        try:
            self._update_status_indicator(state)
        except Exception:
            pass

        # P2: 触发情绪状态机
        if emotion and emotion != "neutral":
            try:
                self._perception.trigger_emotion(emotion)
            except Exception:
                pass

        # 1. 消息气泡
        show_text = message.strip()
        if show_text:
            try:
                tts_cfg = self.config.get("tts", {})
                if tts_cfg.get("enabled", True) and audio_path:
                    if os.path.exists(audio_path):
                        logger.info("Playing TTS: %s", audio_path)
                        self._last_tts_emotion = emotion or "neutral"
                        self._tts_player.play(audio_path)
                    else:
                        logger.warning("TTS audio not found: %s", audio_path)
                else:
                    if not audio_path:
                        logger.debug("No audio_path in response")
                self._show_bubble(show_text, emotion=emotion)
            except Exception as e:
                logger.warning("TTS/bubble error: %s", e)

        # 2. 动画(P3: 传递 emotion,支持帧区间)
        try:
            if anim_name != self._current_anim:
                safe_anims = ['idle', 'walk', 'extra']
                if anim_name not in safe_anims:
                    anim_name = 'idle'
                self._current_anim = anim_name
                self._set_anim_seq(anim_name, emotion=emotion)
        except Exception:
            pass

        # 3. 动作联动
        if state in ("working", "listening") and self._action_linker.enabled:
            try:
                self._action_linker.check()
            except Exception:
                pass

        # 4. 重置状态(当收到 Agent 回复时)
        if state == "speaking" and message and self._pending_chat:
            # 重置跟踪
            self._pending_user_msg = ""
            self._pending_emotion = "neutral"
            self._pending_chat = False

        # 5. idle 超时重置
        self._idle_stage = None
        self._last_interaction = time.time()


    # ── 窗口关闭清理 ──

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
                    pass

        if hasattr(self, '_foreground_watcher'):
            try:
                self._foreground_watcher.stop()
            except Exception:
                pass

        if hasattr(self, '_tts_player'):
            try:
                self._tts_player.stop()
            except Exception:
                pass

        # ── AUDIO-07: 断开桥接器 ──
        if hasattr(self, '_audio_bridge'):
            try:
                self._audio_bridge.disconnect()
            except Exception:
                pass


        if hasattr(self, '_tray'):
            try:
                self._tray.hide()
            except Exception:
                pass

        if hasattr(self, '_engine'):
            try:
                self._engine.stop()
                # 等后台线程退出，避免 TTS 文件被截断
                if self._engine._thread and self._engine._thread.is_alive():
                    self._engine._thread.join(timeout=3)
            except Exception:
                pass

        # ── M1: 停止叙事引擎 ──
        if hasattr(self, '_narrative'):
            try:
                self._narrative.stop_background_loop()
            except Exception:
                pass

        super().closeEvent(event)



