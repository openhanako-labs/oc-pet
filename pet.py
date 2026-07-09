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
    QPropertyAnimation, QEasingCurve
)
from PySide6.QtGui import (
    QPixmap, QPainter, QFont, QColor, QPen, QPainterPath,
    QFontMetrics, QAction, QIcon, QTransform, QImage,
    QCursor
)
from config import CHARACTER_INFO, EXPRESSION_MAP, load_config, save_config
from hanako_monitor import HanakoMonitor, compact_bubble_text
from memory_store import MemoryStore
from behavior import BehaviorParams, BEHAVIOR_MODES
from behavior import (
    PHYSICS_INTERVAL, INERTIA_FACTOR, INTENT_FACTOR,
    ARRIVAL_DISTANCE, WALK_SPEED_BASE,
    BOUNCE_ELASTICITY, BOUNCE_FRICTION, BOUNCE_GRAVITY, BOUNCE_MIN_SPEED
)
from bubble import ChatBubble
from break_notifier import BreakNotifier
from action_linker import ActionLinker
from foreground_watcher import ForegroundWatcher
from tts_player import TTSTtsPlayer
from eye_overlay import EyeOverlay
from startup_screen import StartupScreen
from character_editor import CharacterEditor
from proactive_scheduler import ProactiveScheduler
from avatar.sprite_renderer import SpriteRenderer
from perception import PerceptionController
from conversation_engine import ConversationEngine

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

    def __init__(self):
        super().__init__()
        self.config = load_config()
        # _is_penetrable 已移除(死代码),统一使用 _mousePassthrough

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

        self._current_char = self.config.get("character", "yuexiye")
        self._is_thinking = False

        self._pet_scale = self.config.get("scale", 1.0)
        self._pet_opacity = self.config.get("opacity", 1.0)
        self._behavior_mode = self.config.get("behavior", "normal")

        # ── 动画状态 ──
        self._bob_frame = 0
        self._label_base_pos = QPoint(0, 0)
        self._target_x = 0
        self._vx = 0.0
        self._physics_timer = QTimer(self)
        self._physics_timer.timeout.connect(self._physics_tick)
        self._motion_state = "idle"   # idle / wander / rest
        self._rest_counter = 0
        self._motion_timer = QTimer(self)
        self._motion_timer.timeout.connect(self._motion_tick)
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

        # ── 关怀提醒 ──
        br_cfg = self.config.get("break_reminder", {})
        self._break_notifier = BreakNotifier(
            character_id=self._current_char,
            idle_minutes=br_cfg.get("idle_minutes", 15),
            gradual=br_cfg.get("gradual", True),
            cooldown_minutes=br_cfg.get("cooldown_minutes", 30),
        )
        if not br_cfg.get("enabled", True):
            self._break_notifier.disable()
        self._break_notifier.on_remind = self._on_break_remind
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
            character_id=self._current_char,
            foreground_watcher=self._foreground_watcher,
            on_proactive=self._on_proactive_trigger,
        )
        self._proactive.load_config(proactive_cfg)

        # ── 感知控制器(P2: 时间 + 情绪状态机 + 日程)──
        self._perception = PerceptionController(self._current_char)

        # ── 对话引擎（合并 bridge，单进程）──
        self._engine = ConversationEngine(self._current_char)
        self._engine.on_reply = self._on_engine_reply
        self._engine.on_status = self._on_engine_status
        self._engine.on_tts_ready = lambda: logger.info("Engine TTS ready")
        self._engine.start()

        # ── 语音输入（ASR）──
        self._voice_input = None
        self._voice_recording = False
        if _voice_available:
            self._voice_input = VoiceInput()
            self._voice_input._on_status = self._on_voice_status
            # 后台预加载 Whisper 模型
            preload_whisper()

        # ── TTS 播放器 ──
        tts_cfg = self.config.get("tts", {})
        self._tts_player = TTSTtsPlayer()
        self._tts_player.set_volume(tts_cfg.get("volume", 0.8))
        if not tts_cfg.get("enabled", True):
            self._tts_player.disable()

        # ── 帧动画状态(在 _setup_ui 后初始化)──
        self._anim_seq = 'idle'
        self._anim_idx = 0
        self._anim_range = (None, None)
        self._facing_right = True  # 当前朝向

        # 状态
        self._visible = True
        self._mousePassthrough = True

        self._setup_window()
        self._setup_ui()
        # ── 渲染器就绪后,同步动画状态别名 ──
        self._anim_frames = self._renderer._frames
        self._anim_frame_tops = self._renderer._frame_tops
        self._anim_timer = self._renderer._anim_timer
        self._anim_timer.timeout.connect(self._anim_tick)
        self._setup_animation()
        # ── 记忆存储 ──
        self._mem_store = MemoryStore(self._current_char)

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

        win_cfg = self.config.get("window", {})
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

    def _adjust_scale(self):
        """缩放 +0.2,钳制 0.3~2.0"""
        self._pet_scale = min(2.0, self._pet_scale + 0.2)
        self._recalc_geometry()

    def _zoom_in(self):
        """放大"""
        self._adjust_scale()

    def _zoom_out(self):
        """缩小"""
        self._pet_scale = max(0.3, self._pet_scale - 0.2)
        self._recalc_geometry()

    def _open_character_editor(self):
        """打开角色设定编辑器"""
        editor = CharacterEditor(self._current_char, self)
        editor.exec()

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
        self._eye_overlay = self._renderer.eye_overlay

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
        # 呼吸浮动
        self._bob_timer = QTimer(self)
        self._bob_timer.timeout.connect(self._bob_tick)
        self._bob_timer.start(30)

    def _bob_tick(self):
        self._bob_frame += 1
        offset = int(math.sin(self._bob_frame * 0.06) * 2.5)
        if not self._is_dragging:
            self.char_label.move(
                self._label_base_pos.x(),
                self._label_base_pos.y() + offset
            )

    def _set_anim_seq(self, seq_name, emotion=None):
        """切换动画序列 - 委托给 SpriteRenderer"""
        self._renderer.play_anim(seq_name, emotion=emotion)
        self._anim_seq = self._renderer._anim_seq
        self._anim_idx = self._renderer._anim_idx
        self._anim_range = self._renderer._anim_range

    def _anim_tick(self):
        """帧推进 - 委托给 SpriteRenderer"""
        self._renderer._anim_tick()
        self._anim_idx = self._renderer._anim_idx

    def _show_anim_frame(self):
        """渲染当前帧 - 委托给 SpriteRenderer"""
        self._renderer._show_frame()

    def _get_char_top_y(self):
        """获取角色头顶 Y 坐标 - 委托给 SpriteRenderer"""
        return self._renderer.get_char_top_y()

    def _reposition_bubble(self):
        """气泡置于角色头顶上方,根据实际角色内容定位"""
        top_y = self._get_char_top_y()
        bw = self.bubble.width()
        bh = self.bubble.height()
        bx = (self.width() - bw) // 2
        by = top_y - bh - 4  # 头顶上方 4px
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

        # 缩放
        self._menu.addAction("🔍 放大", self._zoom_in)
        self._menu.addAction("🔍 缩小", self._zoom_out)

        # 角色 / 穿透 / 设置
        self._menu.addAction("🎨 角色", self._open_character_editor)
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
                    # 写入 outbox（同 _send_message 逻辑）
                    basedir = Path(__file__).parent / "data"
                    try:
                        basedir.mkdir(parents=True, exist_ok=True)
                        msg = {"text": text, "character": self._current_char, "time": time.time()}
                        outbox = basedir / "outbox.json"
                        msgs = json.loads(outbox.read_text("utf-8")) if outbox.exists() else []
                        msgs.append(msg)
                        outbox.write_text(json.dumps(msgs, ensure_ascii=False), "utf-8")
                        (basedir / ".pending").write_text("1", "utf-8")
                        logger.info("Voice input sent: %s", text[:30])
                    except Exception as e:
                        logger.warning("Voice outbox error: %s", e)

                    # 显示气泡提示
                    self.bubble.set_text(f"🎤 {text[:30]}")
                    self._reposition_bubble()
                    self.bubble.show()
                    QTimer.singleShot(3000, self._auto_hide_bubble)

                    # 截停 TTS
                    self._tts_player.stop()
                    self._break_notifier.reset()
                    self._is_thinking = True
                else:
                    self._show_bubble("没听清...", emotion="neutral")

            t = threading.Thread(target=_do_asr, daemon=True)
            t.start()

    def _on_voice_status(self, msg: str):
        """语音输入状态回调"""
        if msg:
            self._show_bubble(msg, emotion="thinking")
        else:
            try:
                self.bubble.hide_bubble()
            except Exception:
                pass

    def _open_character_editor(self):
        """打开角色编辑器"""
        from character_editor import CharacterEditor
        editor = CharacterEditor(self._current_char, parent=self)
        editor.exec()

    def _open_settings(self):
        """打开配置面板"""
        from settings_dialog import SettingsDialog
        dialog = SettingsDialog(self.config, parent=self)
        if dialog.exec():
            self.config = dialog.get_config()
            save_config(self.config)
            logger.info("配置已保存")
            # 应用即时生效的设置
            self._apply_settings()

    def _open_plugin_panel(self):
        """打开插件面板"""
        from plugin_panel import PluginPanel
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
        # TTS
        tts_cfg = self.config.get("tts", {})
        if tts_cfg.get("enabled", True):
            self._tts_player.enable()
        else:
            self._tts_player.disable()
        self._tts_player.set_volume(tts_cfg.get("volume", 0.8))

        # 行为模式
        self._switch_behavior_mode(self.config.get("behavior", "normal"))

        # 久坐提醒
        br_cfg = self.config.get("break_reminder", {})
        if br_cfg.get("enabled", True):
            self._break_notifier.enable()
        else:
            self._break_notifier.disable()

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
        info = CHARACTER_INFO.get(char_id)
        if not info:
            print(f"Unknown character: {char_id}")
            return

        self._current_char = char_id
        self.config["character"] = char_id
        save_config(self.config)

        # 委托给渲染器加载帧序列
        self._renderer.load(char_id)
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

    # ── 事件过滤器:统一处理点按/拖拽 ──

    def eventFilter(self, obj, event):
        if obj is self.char_label:
            t = event.type()

            if t == QEvent.Enter:
                self._eye_overlay.start()
                return True
            if t == QEvent.Leave:
                self._eye_overlay.stop()
                return True

            if t == QEvent.MouseButtonPress:
                if event.button() == Qt.LeftButton:
                    self._drag_start_cursor = QCursor.pos()
                    self._drag_start_window = self.pos()
                    self._is_dragging = False
                    self._was_click = True
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
                            self._vx = dx / dt * 0.02    # 缩放到物理帧单位
                            self._vy = dy / dt * 0.02
                            speed = math.sqrt(self._vx ** 2 + self._vy ** 2)
                            if speed > 1.5:
                                self._bounce_active = True
                                self._is_walking = False
                                self._motion_state = "bounce"
                                self._set_anim_seq('walk')  # 弹跳时用 walk 动画
                                self._physics_timer.start(PHYSICS_INTERVAL)
                            else:
                                self._vx = 0.0
                                self._vy = 0.0
                                self._bounce_active = False
                        else:
                            self._vx = 0.0
                            self._vy = 0.0

                        pos = self.pos()
                        self.config.setdefault("window", {})["x"] = pos.x()
                        self.config.setdefault("window", {})["y"] = pos.y()
                        save_config(self.config)
                    elif self._was_click:
                        self._toggle_chat()
                        self._motion_state = "idle"
                    self._was_click = False
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

    # ── 聊天交互 ──

    def _stop_walking(self):
        self._is_walking = False
        self._bounce_active = False
        self._vy = 0.0
        self._physics_timer.stop()
        self._motion_state = "idle"
        self._rest_counter = 0
        self._set_anim_seq('idle')

    def _motion_tick(self):
        """运动状态机主循环 (500ms/tick) - idle→wander/rest 转换"""
        if self._is_dragging or self.input_widget.isVisible() or self._is_walking or self._bounce_active:
            return

        params = self._get_behavior_params()
        if params.walk_chance <= 0:
            if self._motion_state != "idle":
                self._motion_state = "idle"
                self._set_anim_seq('idle')
            return

        # 休息倒计时
        if self._motion_state == "rest":
            self._rest_counter -= 500
            if self._rest_counter <= 0:
                self._motion_state = "idle"
            return

        # idle → 决定下一步
        if self._motion_state == "idle":
            if random.random() < params.walk_chance:
                self._start_walk(params)
            else:
                self._start_rest(params)

    def _start_walk(self, params: BehaviorParams):
        """开始走动 - 惯性物理驱动"""
        sg = self._current_screen_geometry()
        current_x = self.x()

        # 方向决策
        if params.direction_to_mouse:
            cursor = QCursor.pos()
            diff = cursor.x() - current_x
            if abs(diff) > 30 and 0 < cursor.x() < sg.width():
                direction = 1 if diff > 0 else -1
            else:
                direction = random.choice([-1, 1])
        else:
            direction = random.choice([-1, 1])

        distance = random.randint(params.min_dist, params.max_dist)
        self._target_x = current_x + direction * distance
        self._target_x = max(10, min(self._target_x, sg.width() - self.width() - 10))

        self._vx = 0.0
        self._facing_right = (direction > 0)
        self._motion_state = "wander"
        self._set_anim_seq('walk')
        self._is_walking = True
        self._physics_timer.start(PHYSICS_INTERVAL)

    def _physics_tick(self):
        """物理引擎 - 惯性运动 / 弹性弹跳 (30ms/tick)"""
        sg = self._current_screen_geometry()

        # ── 弹跳模式 ──
        if self._bounce_active:
            # 重力
            self._vy += BOUNCE_GRAVITY

            # 摩擦衰减
            self._vx *= BOUNCE_FRICTION
            self._vy *= BOUNCE_FRICTION

            new_x = self.x() + self._vx
            new_y = self.y() + self._vy

            # 左右边缘弹跳
            left = 0
            right = sg.width() - self.width()
            if new_x < left:
                new_x = left
                self._vx = abs(self._vx) * BOUNCE_ELASTICITY
            elif new_x > right:
                new_x = right
                self._vx = -abs(self._vx) * BOUNCE_ELASTICITY

            # 上下边缘弹跳
            top = 0
            bottom = sg.height() - self.height()
            if new_y < top:
                new_y = top
                self._vy = abs(self._vy) * BOUNCE_ELASTICITY
            elif new_y > bottom:
                new_y = bottom
                self._vy = -abs(self._vy) * BOUNCE_ELASTICITY
                # 落地时水平速度也多衰减一点(模拟地面摩擦)
                self._vx *= 0.85

            self.move(int(new_x), int(new_y))

            # 速度降到很低 → 停止弹跳
            speed = math.sqrt(self._vx ** 2 + self._vy ** 2)
            if speed < BOUNCE_MIN_SPEED:
                self._bounce_active = False
                self._vx = 0.0
                self._vy = 0.0
                self._motion_state = "idle"
                self._physics_timer.stop()
                self._set_anim_seq('idle')
                # 保存最终位置
                pos = self.pos()
                self.config.setdefault("window", {})["x"] = pos.x()
                self.config.setdefault("window", {})["y"] = pos.y()
                save_config(self.config)
            return

        # ── 原有:步行模式 ──
        if not self._is_walking:
            self._physics_timer.stop()
            return

        dx = self._target_x - self.x()
        if abs(dx) <= ARRIVAL_DISTANCE:
            self._physics_timer.stop()
            self._on_walk_finished()
            return

        # 比例控制 + 速度上限
        params = self._get_behavior_params()
        max_speed = WALK_SPEED_BASE * params.speed_mul
        desired_vx = dx * 0.12  # 比例增益
        desired_vx = max(-max_speed, min(max_speed, desired_vx))

        # 惯性公式
        self._vx = self._vx * INERTIA_FACTOR + desired_vx * INTENT_FACTOR

        # 根据速度更新朝向
        if abs(self._vx) > 0.5:
            self._facing_right = (self._vx > 0)

        # 防止抖动的死区
        if abs(self._vx) < 0.2:
            self._vx = 0.3 if self._vx >= 0 else -0.3

        new_x = self.x() + self._vx

        # 屏幕边界
        sg = self._current_screen_geometry()
        new_x = max(10, min(new_x, sg.width() - self.width() - 10))

        self.move(int(new_x), self.y())

    def _start_rest(self, params: BehaviorParams):
        """开始休息(不动 + 倒计时)"""
        self._motion_state = "rest"
        self._set_anim_seq('idle')
        self._rest_counter = random.randint(
            max(params.min_pause, 1500),
            max(params.max_pause, 4000)
        )

    def _on_walk_finished(self):
        self._is_walking = False
        self._set_anim_seq('idle')
        self._store_label_pos()
        pos = self.pos()
        self.config.setdefault("window", {})["x"] = pos.x()
        self.config.setdefault("window", {})["y"] = pos.y()
        save_config(self.config)
        # 走完自动进入休息
        params = self._get_behavior_params()
        self._start_rest(params)

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

        # ── 关怀提醒重置 ──
        self._break_notifier.reset()

        # P2: 用户交互 -> 重置情绪状态机
        try:
            self._perception.reset_emotion()
        except Exception:
            pass

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

    def _auto_hide_bubble(self):
        """发送中气泡超时隐藏"""
        self._is_thinking = False
        self._bubble_message = ""
        if hasattr(self, 'bubble'):
            try:
                self.bubble.hide_bubble()
            except Exception:
                pass

    def _clear_hanako_bubble(self):
        """清除气泡（超时回调）"""
        if hasattr(self, 'bubble'):
            try:
                self.bubble.hide_bubble()
            except Exception:
                pass
            self._bubble_message = ""

    def _on_engine_reply(self, reply: str, emotion: str, anim: str, audio_path: str):
        """对话引擎回复回调 - 在主线程中执行"""
        # 截停旧 TTS
        self._tts_player.stop()

        # 显示气泡
        if reply:
            try:
                compact = compact_bubble_text(reply)
            except Exception:
                compact = reply
            self._show_bubble(compact or reply, emotion=emotion)

        # 播放音频
        if audio_path and os.path.exists(audio_path):
            tts_cfg = self.config.get("tts", {})
            if tts_cfg.get("enabled", True):
                logger.info("Playing TTS: %s", audio_path)
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

        # 记忆写入
        if reply and self._pending_chat and self._pending_user_msg:
            try:
                self._mem_store.add(
                    user_msg=self._pending_user_msg,
                    bot_reply=reply,
                    emotion=emotion,
                    confidence=0.85,
                    source="dialogue",
                )
            except Exception as e:
                logger.warning("Memory store failed: %s", e)
            self._pending_user_msg = ""
            self._pending_chat = False

        # 重置 idle
        self._is_thinking = False
        self._idle_stage = None
        self._last_interaction = time.time()

    def _on_engine_status(self, msg: str):
        """引擎状态提示"""
        if msg:
            self._show_bubble(msg, emotion="thinking")
        else:
            try:
                self.bubble.hide_bubble()
            except Exception:
                pass

    # ── 右键菜单 ──

    # ── Hanako 状态回调 ──

    def _reposition_status_label(self):
        """将状态指示器放在窗口右下角"""
        sw = self._status_label.width()
        sh = self._status_label.height()
        self._status_label.move(self.width() - sw - 6, self.height() - sh - 6)

    def _restore_status_label(self):
        """穿透提示后恢复为状态指示"""
        self._update_status_indicator(self._hanako_monitor.current_state_name)

    def _update_status_indicator(self, state_name: str):
        """更新持久化状态指示器"""
        from hanako_monitor import STATE_LABELS
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
        """每 30 秒检查:关怀提醒 + idle 超时"""
        now = time.time()
        idle_secs = now - self._last_interaction

        # Idle 超时递进(只在无 Agent 互动时触发)
        if self._idle_stage is None and idle_secs >= 300:
            self._idle_stage = "cute"
            self._show_break_bubble("怎么不理我呀~", emotion="cute")
        elif self._idle_stage == "cute" and idle_secs >= 900:
            self._idle_stage = "sad"
            self._show_break_bubble("好无聊......", emotion="sad")
        elif self._idle_stage == "sad" and idle_secs >= 1800:
            self._idle_stage = "missing"
            self._show_break_bubble("主人去哪了......", emotion="missing")

        # 关怀提醒(BreakNotifier)
        try:
            self._break_notifier.check()
        except Exception:
            pass

        # Proactive 主动对话(与 BreakNotifier 同频)
        try:
            self._proactive.tick()
        except Exception:
            pass

        # 感知系统 tick(情绪衰减 + 日程刷新)
        try:
            self._perception.tick_emotion()
            self._perception.tick_schedule()
        except Exception:
            pass

    def _foreground_tick(self):
        """每 2 秒检测前台窗口"""
        try:
            self._foreground_watcher.tick()
        except Exception:
            pass

    def _on_foreground_change(self, app_name: str, app_category: str):
        """前台窗口变化 → 重置 idle 计时器"""
        going = self._idle_stage
        self._last_interaction = time.time()
        self._idle_stage = None
        if going is not None:
            self._show_break_bubble("你回来啦~", emotion="happy")

    def _on_proactive_trigger(self, prompt_text: str):
        """Proactive 调度器触发 -> 通过引擎发送"""
        self._break_notifier.reset()
        if self._engine:
            self._engine.send(prompt_text, character=self._current_char)
            logger.info("Proactive message sent: %s", prompt_text)

    def _show_break_bubble(self, text: str, emotion: str = "neutral"):
        """显示关怀/闲置提醒气泡"""
        self._show_bubble(text, emotion=emotion)

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

        # 4. 对话记忆写入(当收到 Agent 回复时)
        if state == "speaking" and message and self._pending_chat and self._pending_user_msg:
            try:
                compact_reply = compact_bubble_text(message) if message else message
                self._mem_store.add(
                    user_msg=self._pending_user_msg,
                    bot_reply=compact_reply or message,
                    emotion=emotion,
                    confidence=0.85,
                    source="dialogue",
                )
                logger.debug("Memory stored: %d entries", self._mem_store.count())
            except Exception as e:
                logger.warning("Memory store failed: %s", e)
            # 重置跟踪
            self._pending_user_msg = ""
            self._pending_emotion = "neutral"
            self._pending_chat = False

        # 5. idle 超时重置
        self._idle_stage = None
        self._last_interaction = time.time()


    def _on_break_remind(self, stage: str, msg: str):
        """关怀提醒回调"""
        self._show_break_bubble(msg, emotion="cute")

    # ── 窗口关闭清理 ──

    def closeEvent(self, event):
        """关闭窗口时停止所有定时器 + 清理资源"""
        timers = [
            '_physics_timer', '_motion_timer', '_bob_timer',
            '_anim_timer', '_drag_poll_timer', '_hanako_poll_timer',
            '_break_timer', '_foreground_timer', '_bubble_timer',
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


        if hasattr(self, '_tray_icon'):
            try:
                self._tray_icon.hide()
            except Exception:
                pass

        if hasattr(self, '_mem_store'):
            try:
                self._mem_store.close()
            except Exception:
                pass

        if hasattr(self, '_engine'):
            try:
                self._engine.stop()
            except Exception:
                pass

        super().closeEvent(event)



