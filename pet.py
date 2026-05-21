"""桌面宠物主窗口"""
import os
import json
import math
import random
import time
from pathlib import Path
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QMenu, QDialog, QFormLayout
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
from hanako_monitor import HanakoMonitor

# ─── 对话气泡 ───────────────────────────────────────────

class ChatBubble(QWidget):
    """头顶对话气泡 — 白底圆角阴影 + 淡入动画 + 三角指针"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._padding_h = 14
        self._padding_v = 10
        self._max_width = 200
        self._bg_color = QColor(255, 255, 255, 240)
        self._text_color = QColor(40, 40, 50)
        self._shadow_color = QColor(0, 0, 0, 40)
        self._font = QFont("Microsoft YaHei UI", 10)
        self.setFont(self._font)
        self.setMinimumSize(40, 30)

        # 淡入动画
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self._fade_anim.setDuration(250)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)

        self._flash_timer = QTimer(self)
        self._flash_timer.timeout.connect(self._flash_tick)
        self._flash_count = 0
        self._bright = False
        self.hide()

    def set_text(self, text: str, bright: bool = False):
        self._text = text
        self._bright = bright
        if bright:
            self._bg_color = QColor(255, 235, 200, 245)
            self._text_color = QColor(80, 40, 10)
        else:
            self._bg_color = QColor(255, 255, 255, 240)
            self._text_color = QColor(40, 40, 50)
        self._update_size()
        self.update()
        self.show()
        self.raise_()
        # 淡入
        self.setWindowOpacity(0.0)
        self._fade_anim.stop()
        self._fade_anim.start()
        if bright:
            self._start_flash()

    def hide_bubble(self):
        self._fade_anim.stop()
        self.hide()

    def _start_flash(self):
        self._flash_count = 0
        self._flash_timer.start(300)

    def _flash_tick(self):
        self._flash_count += 1
        if self._flash_count > 12:
            self._flash_timer.stop()
            self._flash_count = 0
        self.update()

    def _update_size(self):
        fm = QFontMetrics(self._font)
        text_w = self._max_width - self._padding_h * 2 - 4
        rect = fm.boundingRect(
            QRect(0, 0, text_w, 1000),
            Qt.AlignLeft | Qt.TextWordWrap,
            self._text
        )
        bw = rect.width() + self._padding_h * 2 + 16
        bh = rect.height() + self._padding_v * 2 + 8
        self.setFixedSize(max(bw, 50), max(bh, 36))

    def paintEvent(self, event):
        if not self._text:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()
        r = 14
        tri_h = 8

        flashing = self._flash_timer.isActive()
        is_on = (self._flash_count % 2 == 0) if flashing else True

        bg = self._bg_color if is_on else QColor(255, 255, 255, 160)
        tc = self._text_color if is_on else QColor(150, 150, 160)

        body_h = h - tri_h

        # 阴影层
        shadow_path = QPainterPath()
        shadow_path.addRoundedRect(2, 2, w - 12, body_h + 2, r, r)
        p.fillPath(shadow_path, self._shadow_color)

        # 气泡主体 + 三角
        bubble_path = QPainterPath()
        bubble_path.addRoundedRect(0, 0, w - 12, body_h, r, r)
        cx = (w - 12) // 2
        bubble_path.moveTo(cx - 7, body_h)
        bubble_path.lineTo(cx, body_h + tri_h)
        bubble_path.lineTo(cx + 7, body_h)
        bubble_path.closeSubpath()
        p.fillPath(bubble_path, bg)

        # 文字
        p.setPen(tc)
        text_rect = QRect(
            self._padding_h, self._padding_v,
            w - self._padding_h * 2 - 14,
            body_h - self._padding_v * 2
        )
        p.drawText(text_rect, Qt.AlignLeft | Qt.TextWordWrap, self._text)
        p.end()

# ─── 设置对话框 ─────────────────────────────────────────

class PetWindow(QWidget):
    """透明桌面宠物窗口"""

    def __init__(self):
        super().__init__()
        self.config = load_config()

        # ── 交互状态 ──
        self._drag_start_cursor = QPoint()
        self._drag_start_window = QPoint()
        self._is_dragging = False
        self._was_click = False
        self._drag_poll_timer = QTimer(self)
        self._drag_poll_timer.timeout.connect(self._drag_poll_tick)

        self._current_char = self.config.get("character", "yuexiye")
        self._is_thinking = False

        # ── 动画状态 ──
        self._bob_frame = 0
        self._label_base_pos = QPoint(0, 0)
        self._walk_anim = None
        self._walk_timer = QTimer(self)
        self._walk_timer.timeout.connect(self._maybe_walk)
        self._walk_timer.start(4000)
        self._is_walking = False

        # ── Hanako 状态监控 ──
        self._hanako_monitor = HanakoMonitor(on_state_change=self._on_hanako_state)
        self._hanako_poll_timer = QTimer(self)
        self._hanako_poll_timer.timeout.connect(self._hanako_monitor.tick)
        self._hanako_poll_timer.start(800)
        self._bubble_message = ""    # 当前气泡文字，用于超时隐藏
        self._bubble_timer = QTimer(self)
        self._bubble_timer.timeout.connect(self._clear_hanako_bubble)
        self._bubble_timer.setSingleShot(True)

        # ── 帧动画 ──
        self._anim_frames = {}    # {'idle': [QPixmap,...], 'walk': [QPixmap,...]}
        self._anim_frame_tops = {}  # {'idle': [top_y, ...], 'walk': [top_y, ...]}
        self._anim_seq = 'idle'
        self._anim_idx = 0
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._anim_tick)

        # 状态
        self._visible = True

        self._setup_window()
        self._setup_ui()
        self._setup_animation()
        self._setup_menu()
        self.load_character(self._current_char)

    # ── API ──

    # ── 窗口设置 ──

    def _setup_window(self):
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(200, 360)

        win_cfg = self.config.get("window", {})
        if win_cfg.get("x", -1) >= 0 and win_cfg.get("y", -1) >= 0:
            self.move(win_cfg["x"], win_cfg["y"])
        else:
            screen = QApplication.primaryScreen()
            if screen:
                sg = screen.availableGeometry()
                self.move(sg.width() - 250, sg.height() - 350)

    # ── UI ──

    def _setup_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        self.main_layout.setAlignment(Qt.AlignCenter)

        # 角色图片（底层）
        self.char_label = QLabel(self)
        self.char_label.setAlignment(Qt.AlignCenter)
        self.char_label.setFixedSize(200, 260)
        self.char_label.move(0, 60)  # 给头顶气泡留空间
        self.char_label.lower()
        self.char_label.installEventFilter(self)

        # 气泡（顶层）
        self.bubble = ChatBubble(self)
        self.bubble.move(0, 0)
        self.bubble.raise_()

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

    # ── 动画 ──

    def _setup_animation(self):
        # 帧动画时钟（idle 默认 4fps，walk 6fps）
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

    def _set_anim_seq(self, seq_name):
        """切换动画序列并重置帧索引"""
        if seq_name != self._anim_seq and seq_name in self._anim_frames:
            self._anim_seq = seq_name
            self._anim_idx = 0
            speed = 330 if seq_name == 'idle' else 250  # idle ~3fps, walk 4fps
            self._anim_timer.setInterval(speed)
            self._show_anim_frame()

    def _anim_tick(self):
        """推进到下一帧"""
        frames = self._anim_frames.get(self._anim_seq, [])
        if len(frames) > 1:
            self._anim_idx = (self._anim_idx + 1) % len(frames)
            self._show_anim_frame()

    def _show_anim_frame(self):
        frames = self._anim_frames.get(self._anim_seq, [])
        if not frames:
            return
        pix = frames[self._anim_idx % len(frames)]
        self.char_label.setPixmap(pix)

    def _get_char_top_y(self):
        """获取当前帧角色头顶 y 坐标（相对窗口），从预计算数据中查找"""
        tops = self._anim_frame_tops.get(self._anim_seq, [])
        if tops:
            idx = self._anim_idx % len(tops)
            return self.char_label.y() + tops[idx]
        return self.char_label.y()

    def _reposition_bubble(self):
        """气泡置于角色头顶上方，根据实际角色内容定位"""
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

    # ── 角色加载 ──

    def load_character(self, char_id: str):
        self._current_char = char_id
        info = CHARACTER_INFO.get(char_id)
        if not info:
            return

        char_dir = os.path.join(os.path.dirname(__file__), info["path"])
        self._anim_frames = {}
        self._anim_frame_tops = {}
        target_w, target_h = 180, 250

        def scan_top_y(qpx):
            """快速扫描 QPixmap 第一个非透明像素的行号"""
            img = qpx.toImage().convertToFormat(QImage.Format_ARGB32)
            w = img.width()
            h = img.height()
            stride = w * 4
            raw = bytes(img.constBits())
            for y in range(h):
                row_start = y * stride
                for x in range(w):
                    if raw[row_start + x * 4 + 3] > 30:
                        return y
            return 0

        for seq_name in ['idle', 'walk', 'extra']:
            seq_dir = os.path.join(char_dir, 'frames', seq_name)
            frames = []
            tops = []
            if os.path.isdir(seq_dir):
                fnames = sorted([f for f in os.listdir(seq_dir) if f.endswith('.png')])
                for fn in fnames:
                    px = QPixmap(os.path.join(seq_dir, fn))
                    if not px.isNull():
                        px = px.scaled(target_w, target_h,
                                       Qt.KeepAspectRatio,
                                       Qt.SmoothTransformation)
                        frames.append(px)
                        tops.append(scan_top_y(px))
            if frames:
                self._anim_frames[seq_name] = frames
                self._anim_frame_tops[seq_name] = tops

        # Fallback: 旧式单图
        if not self._anim_frames:
            for fn in ['idle.png', 'stand.png']:
                p = os.path.join(char_dir, fn)
                if os.path.exists(p):
                    px = QPixmap(p)
                    if not px.isNull():
                        px = px.scaled(target_w, target_h,
                                       Qt.KeepAspectRatio,
                                       Qt.SmoothTransformation)
                        self._anim_frames['idle'] = [px]
                        self._anim_frame_tops['idle'] = [scan_top_y(px)]
                        break

        self._anim_seq = 'idle'
        self._anim_idx = 0
        self._show_anim_frame()

        if 'idle' in self._anim_frames:
            self.char_label.setStyleSheet("")
        else:
            self.char_label.setText(f"[{info['name']}]")
            self.char_label.setStyleSheet("color: #e6e6f0; font-size: 16px;")

        QTimer.singleShot(50, self._store_label_pos)

        self.config["character"] = char_id
        save_config(self.config)
        self._is_thinking = False

    def _store_label_pos(self):
        self._label_base_pos = self.char_label.pos()

    # ── 事件过滤器：统一处理点按/拖拽 ──

    def eventFilter(self, obj, event):
        if obj is self.char_label:
            t = event.type()

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
                        pos = self.pos()
                        self.config.setdefault("window", {})["x"] = pos.x()
                        self.config.setdefault("window", {})["y"] = pos.y()
                        save_config(self.config)
                        # 拖完继续走动
                        self._walk_timer.start(4000)
                    elif self._was_click:
                        self._toggle_chat()
                        self._walk_timer.start(4000)
                    self._was_click = False
                return True

            elif t == QEvent.MouseButtonDblClick:
                # 双击可留作扩展
                return True

        return super().eventFilter(obj, event)

    # ── 拖拽轮询 ──

    def _drag_poll_tick(self):
        """拖拽时每 16ms 轮询鼠标位置（不掉事件）"""
        if self._is_dragging:
            cursor = QCursor.pos()
            delta = cursor - self._drag_start_cursor
            self.move(self._drag_start_window + delta)

    # ── 聊天交互 ──

    def _stop_walking(self):
        self._is_walking = False
        self._walk_timer.stop()
        if self._walk_anim and self._walk_anim.state() == QPropertyAnimation.Running:
            self._walk_anim.stop()
        self._set_anim_seq('idle')

    def _maybe_walk(self):
        """随机走动：左移/右移/停留"""
        if self._is_dragging or self._is_walking or self.input_widget.isVisible():
            return

        r = random.random()
        if r < 0.25:
            return

        screen = QApplication.primaryScreen()
        if not screen:
            return
        sg = screen.availableGeometry()
        win_w = self.width()
        current_x = self.x()

        direction = random.choice([-1, 1])
        distance = random.randint(30, 120)
        target_x = current_x + direction * distance
        target_x = max(10, min(target_x, sg.width() - win_w - 10))

        # 切换为走路动画
        self._set_anim_seq('walk')
        self._is_walking = True
        self._walk_anim = QPropertyAnimation(self, b"pos")
        self._walk_anim.setDuration(1500)
        self._walk_anim.setStartValue(self.pos())
        self._walk_anim.setEndValue(QPoint(target_x, self.y()))
        self._walk_anim.setEasingCurve(QEasingCurve.InOutSine)
        self._walk_anim.finished.connect(self._on_walk_finished)
        self._walk_anim.start()

    def _on_walk_finished(self):
        self._is_walking = False
        self._set_anim_seq('idle')
        self._store_label_pos()
        pos = self.pos()
        self.config.setdefault("window", {})["x"] = pos.x()
        self.config.setdefault("window", {})["y"] = pos.y()
        save_config(self.config)
        self._walk_timer.start(random.randint(2000, 6000))

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

        # 写到 outbox
        basedir = Path.home() / ".hanako/plugins/hanako-desktop-companion"
        try:
            basedir.mkdir(parents=True, exist_ok=True)
            msg = {"text": text, "character": self._current_char, "time": time.time()}
            outbox = basedir / "outbox.json"
            msgs = json.loads(outbox.read_text("utf-8")) if outbox.exists() else []
            msgs.append(msg)
            outbox.write_text(json.dumps(msgs, ensure_ascii=False), "utf-8")
            # 写待处理标记 → Agent 下次回复前检测到
            (basedir / ".pending").write_text("1", "utf-8")
        except Exception as e:
            print(f"Outbox error: {e}")

        self.bubble.set_text("⏳ 发送中...")
        self._reposition_bubble()
        self.bubble.show()
        self.bubble.raise_()
        self._is_thinking = True
        QTimer.singleShot(3000, self._auto_hide_bubble)

    def _auto_hide_bubble(self):
        if not self._is_thinking:
            self.bubble.hide()

    # ── 右键菜单 ──

    # ── Hanako 状态回调 ──

    def _on_hanako_state(self, anim_name: str, message: str, emotion: str = "neutral"):
        """Hanako 状态变化时的回调 — 增强：支持情绪映射"""
        if not message:
            if self._is_thinking or self._is_walking or self.input_widget.isVisible():
                return
            self._bubble_message = ""
            self._bubble_timer.stop()
            self.bubble.hide_bubble()
            return

        # 情绪 → 动画序列映射
        target_anim = EXPRESSION_MAP.get(emotion, anim_name)
        if target_anim in self._anim_frames:
            self._set_anim_seq(target_anim)
        elif anim_name in self._anim_frames:
            self._set_anim_seq(anim_name)
        else:
            self._set_anim_seq('idle')

        # 高亮气泡（开心时）
        bright = (emotion in ('happy', 'surprised'))

        self._is_thinking = False
        self._bubble_message = message
        self.bubble.set_text(message, bright=bright)
        self._reposition_bubble()
        self.bubble.show()
        self.bubble.raise_()
        self._bubble_timer.start(5000)

    def _clear_hanako_bubble(self):
        """超时隐藏气泡"""
        self.bubble.hide_bubble()
        self._bubble_message = ""

    def _show_context_menu(self, pos):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background: rgba(25, 25, 35, 230);
                color: #e6e6f0;
                border: 1px solid rgba(80, 80, 120, 100);
                border-radius: 8px;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 20px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background: rgba(70, 90, 160, 150);
            }
            QMenu::separator {
                height: 1px;
                background: rgba(80, 80, 120, 80);
                margin: 4px 8px;
            }
        """)

        char_menu = menu.addMenu("切换角色")
        for cid, info in CHARACTER_INFO.items():
            action = char_menu.addAction(info["name"])
            action.setCheckable(True)
            if cid == self._current_char:
                action.setChecked(True)
            action.triggered.connect(lambda checked, c=cid: self.load_character(c))

        menu.addSeparator()

        menu.addSeparator()

        vis_action = menu.addAction("隐藏桌宠" if self._visible else "显示桌宠")
        vis_action.triggered.connect(self._toggle_visibility)

        menu.addSeparator()
        quit_action = menu.addAction("退出")
        quit_action.triggered.connect(self.close)

        menu.exec(self.mapToGlobal(pos))

    def _toggle_visibility(self):
        self._visible = not self._visible
        if self._visible:
            self.show()
            self._walk_timer.start(4000)
        else:
            self._walk_timer.stop()
            self.hide()

    # ── 关闭 ──

    def closeEvent(self, event):
        self._hanako_monitor.force_idle()
        pos = self.pos()
        self.config.setdefault("window", {})["x"] = pos.x()
        self.config.setdefault("window", {})["y"] = pos.y()
        save_config(self.config)
        event.accept()
