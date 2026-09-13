"""配置面板 - GUI 设置对话框

可配置项：
  - Agent 管理：启用/禁用桌宠、新增/移除
  - TTS：开关、音量、引擎
  - 行为模式：静默/正常/活跃/黏人
  - 鼠标交互：开关
  - 主动对话：开关、冷却时间
  - 屏幕感知：开关、截屏间隔
  - 语音输入：引擎选择
  - 记忆注入：预算模式、上限
  - 窗口：透明度、缩放
  - 久坐提醒：开关、间隔
  - API 配置：LLM/TTS/ASR
  - 角色包管理 (M5)
"""
from __future__ import annotations

import logging
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QCheckBox, QSlider, QSpinBox, QDoubleSpinBox, QComboBox,
    QPushButton, QLabel, QGroupBox, QTabWidget, QWidget,
    QLineEdit, QListWidget, QListWidgetItem, QAbstractItemView,
    QScrollArea, QMessageBox
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from config import load_config, save_config
from ui.theme.palette import rgb, rgba
from ui.theme.theme_manager import get_default
logger = logging.getLogger(__name__)



class SettingsDialog(QDialog):
    """配置面板"""

    def __init__(self, config: dict = None, pet_manager=None, parent=None):
        super().__init__(parent)
        self._config = config or load_config()
        self._pet_manager = pet_manager
        self.setWindowTitle("设置")
        self.setMinimumSize(380, 400)  # P2: 调小最小尺寸，减少空余空间
        self.setMaximumHeight(800)  # 限制最大高度，避免超出屏幕
        # UI优化: 允许纵向缩放（QDialog 默认可缩放）
        mgr = get_default()
        self._ui_theme = mgr.current if mgr else "dark"
        if mgr is not None:
            mgr.theme_changed.connect(self._on_dialog_theme_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        self._main_tabs = QTabWidget()
        self._main_tabs.setStyleSheet(self._tab_qss())
        
        # UI优化: 添加搜索过滤框
        search_layout = QHBoxLayout()
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("🔍 搜索设置...")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.textChanged.connect(self._filter_settings)
        search_layout.addWidget(self._search_input)
        layout.addLayout(search_layout)
        
        # 滚动区域：内容过多时可滚动，避免窗口超出屏幕
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._main_tabs)
        layout.addWidget(scroll)

        # ── Tab 1: 基础设置 ──
        basic_tab = QWidget()
        basic_layout = QVBoxLayout(basic_tab)
        basic_layout.setContentsMargins(8, 8, 8, 8)
        basic_layout.setSpacing(14)

        # 页面引导说明（产品级：明确页面定位，建立标题层级）
        page_hint = QLabel("调整桌宠的行为、外观与反馈。改动保存后立即生效。")
        page_hint.setWordWrap(True)
        page_hint.setStyleSheet("color: rgb(%s); font-size: 11px; margin-bottom: 2px;" % rgb(self._ui_theme, "text_muted"))
        basic_layout.addWidget(page_hint)

        # 行为模式
        beh_group = QGroupBox("行为模式")
        beh_layout = QFormLayout(beh_group)

        self.behavior = QComboBox()
        self.behavior.addItems(["静默 (quiet)", "正常 (normal)", "活跃 (active)", "黏人 (cling)"])
        beh_map = {"quiet": 0, "normal": 1, "active": 2, "cling": 3}
        self.behavior.setCurrentIndex(beh_map.get(self._config.get("behavior", "normal"), 1))
        beh_layout.addRow("模式", self.behavior)

        basic_layout.addWidget(beh_group)

        # 窗口
        win_group = QGroupBox("窗口")
        win_layout = QFormLayout(win_group)

        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(20, 100)
        self.opacity.setValue(int(self._config.get("opacity", 1.0) * 100))
        self._opacity_label = QLabel(f"{self.opacity.value()}%")
        self.opacity.valueChanged.connect(lambda v: self._opacity_label.setText(f"{v}%"))
        op_row = QHBoxLayout()
        op_row.addWidget(self.opacity)
        op_row.addWidget(self._opacity_label)
        win_layout.addRow("透明度", op_row)

        self.scale = QSlider(Qt.Horizontal)
        # 下限 30%（0.3），与滚轮/快捷键缩放下限一致；上限保持 200%
        self.scale.setRange(30, 200)
        self.scale.setValue(int(self._config.get("scale", 1.0) * 100))
        self._scale_label = QLabel(f"{self.scale.value()}%")
        self.scale.valueChanged.connect(lambda v: self._scale_label.setText(f"{v}%"))
        sc_row = QHBoxLayout()
        sc_row.addWidget(self.scale)
        sc_row.addWidget(self._scale_label)
        win_layout.addRow("缩放", sc_row)

        self.mouse_interaction = QCheckBox("鼠标交互（视线跟随 + 反应）")
        self.mouse_interaction.setChecked(self._config.get("mouse_interaction", True))
        win_layout.addRow(self.mouse_interaction)

        basic_layout.addWidget(win_group)

        # 音效
        sfx_group = QGroupBox("音效")
        sfx_layout = QFormLayout(sfx_group)

        self.sfx_enabled = QCheckBox("启用交互音效")
        self.sfx_enabled.setChecked(self._config.get("sfx", {}).get("enabled", True))
        sfx_layout.addRow(self.sfx_enabled)

        self.sfx_volume = QSlider(Qt.Horizontal)
        self.sfx_volume.setRange(0, 100)
        self.sfx_volume.setValue(int(self._config.get("sfx", {}).get("volume", 0.5) * 100))
        self._sfx_vol_label = QLabel(f"{self.sfx_volume.value()}%")
        self.sfx_volume.valueChanged.connect(lambda v: self._sfx_vol_label.setText(f"{v}%"))
        sfx_vol_row = QHBoxLayout()
        sfx_vol_row.addWidget(self.sfx_volume)
        sfx_vol_row.addWidget(self._sfx_vol_label)
        sfx_layout.addRow("音量", sfx_vol_row)

        basic_layout.addWidget(sfx_group)
        
        # ── 渲染格式（自动检测，无需用户指定）──
        # 已移除：渲染格式完全自动检测，不需要用户手动指定
        # 角色有 live2d 资源 → live2d，否则 → sprite
        # 参见 avatar/factory.py 的 create_renderer() 和 detect_format()

        # ── 桌宠独立配置（per-pet：每个桌宠自己的 TTS 引擎/音色/助手） ──
        per_pet_group = QGroupBox("桌宠独立配置")
        per_pet_layout = QVBoxLayout(per_pet_group)
        per_pet_hint = QLabel(
            "每个桌宠可单独指定 TTS 引擎/音色与对话助手（不配置则沿用全局默认）。"
            "保存后对当前桌宠立即生效，其他桌宠不受影响。"
        )
        per_pet_hint.setWordWrap(True)
        per_pet_hint.setStyleSheet("color: rgb(%s); font-size: 10px;" % rgb(self._ui_theme, "text_muted"))
        per_pet_layout.addWidget(per_pet_hint)

        # 逐 agent 配置行：<agent_id> [TTS引擎] [音色] [助手]
        self._per_pet_rows: dict[str, tuple] = {}
        try:
            from avatar.factory import detect_format  # noqa: F401
            from core.character_package import CharacterPackageManager
            pkg_mgr = CharacterPackageManager()
            agents = self._config.get("agents", [])
            # 若配置里无 agents（老用户/默认），至少展示当前角色
            agent_ids = [a.get("id", "") for a in agents if a.get("id")]
            cur = self._config.get("character", "")
            if cur and cur not in agent_ids:
                agent_ids.append(cur)
            if not agent_ids:
                agent_ids = ["yuexinmiao"]

            for aid in agent_ids:
                row = QHBoxLayout()
                row.setSpacing(6)
                row.addWidget(QLabel(f"{aid}:"))

                # TTS 引擎下拉（agent 级覆盖）
                eng = QComboBox()
                eng.addItems(["沿用全局", "CosyVoice 本地", "微软 Edge (免费)", "MIMO TTS", "API 调用", "Qwen3-TTS 本地"])
                eng_map = {"": 0, "cosyvoice": 1, "edge": 2, "mimo": 3, "api": 4, "qwen": 5}
                ac = next((a for a in agents if a.get("id") == aid), {})
                agent_tts = ac.get("tts", {}) if isinstance(ac.get("tts"), dict) else {}
                cur_eng = agent_tts.get("provider", "")
                eng.setCurrentIndex(eng_map.get(cur_eng, 0))

                # 音色下拉（Edge 音色 + CosyVoice/Qwen 参考音色）
                voice = QComboBox()
                voice.setEditable(True)
                voice.setMinimumWidth(150)
                try:
                    from tts_provider.edge_tts import EDGE_VOICES
                    edge_voices = EDGE_VOICES
                except Exception:
                    edge_voices = ["zh-CN-XiaoxiaoNeural"]
                # 第一项：沿用全局
                voice.addItem("沿用全局")
                for item in ([f"edge|{v}" for v in edge_voices] +
                             ["cosy|ophelia", "cosy|luoqixi", "cosy|aimis", "cosy|alice", "cosy|glados", "cosy|rebecca",
                              "qwen|ophelia", "qwen|luoqixi", "qwen|aimis", "qwen|alice", "qwen|glados",
                              "qwen|rebecca", "qwen|lo", "qwen|vivian", "qwen|ryan", "qwen|aiden"]):
                    voice.addItem(item)
                cur_voice = agent_tts.get("voice", "") or agent_tts.get("edge_voice", "")
                if cur_eng:
                    # 独立引擎：选中保存的音色，启用下拉框
                    voice.setEnabled(True)
                    if cur_voice:
                        tag = f"{cur_eng}|{cur_voice}"
                        v_idx = voice.findText(tag)
                        if v_idx >= 0:
                            voice.setCurrentIndex(v_idx)
                        else:
                            voice.setEditText(cur_voice)
                else:
                    # 沿用全局：选中第一项，禁用下拉框
                    voice.setCurrentIndex(0)
                    voice.setEnabled(False)

                # 引擎切换时联动音色状态
                def _on_engine_changed(idx, voice_cb=voice):
                    if idx == 0:
                        voice_cb.setCurrentIndex(0)
                        voice_cb.setEnabled(False)
                    else:
                        voice_cb.setEnabled(True)
                eng.currentIndexChanged.connect(_on_engine_changed)

                # 助手下拉（服务端可用 agent）
                ag = QComboBox()
                ag.addItem("沿用全局", "")
                try:
                    from pathlib import Path
                    discovered = []
                    agents_dir = Path.home() / ".hanako" / "agents"
                    if agents_dir.is_dir():
                        discovered = sorted(d.name for d in agents_dir.iterdir() if d.is_dir())
                    for dname in discovered:
                        ag.addItem(dname, dname)
                    agent_dialog = (ac.get("dialog", {}) or {}).get("agent_id", "")
                    d_idx = ag.findData(agent_dialog)
                    ag.setCurrentIndex(d_idx if d_idx >= 0 else 0)
                except Exception:
                    logger.debug("settings_dialog: 非致命异常(已静默吞掉)", exc_info=True)

                row.addWidget(eng, 1)
                row.addWidget(voice, 2)
                row.addWidget(ag, 1)
                per_pet_layout.addLayout(row)
                self._per_pet_rows[aid] = (eng, voice, ag)
        except Exception as e:
            logger.warning("per-pet 配置区构建失败: %s", e)

        basic_layout.addWidget(per_pet_group)

        basic_layout.addStretch()
        self._main_tabs.addTab(basic_tab, "🎨 基础")

        # ── Tab 2: 功能设置 ──
        func_tab = QWidget()
        func_layout = QVBoxLayout(func_tab)
        func_layout.setContentsMargins(16, 16, 16, 16)
        func_layout.setSpacing(16)

        # 功能页子标签
        self.func_sub_tabs = QTabWidget()
        self.func_sub_tabs.setStyleSheet(self._tab_qss())

        # ── 子标签 1: 语音 ──
        voice_tab = QWidget()
        voice_layout = QVBoxLayout(voice_tab)
        voice_layout.setContentsMargins(12, 12, 12, 12)
        voice_layout.setSpacing(16)

        # TTS
        tts_group = QGroupBox("语音输出")
        tts_layout = QFormLayout(tts_group)
        tts_layout.setSpacing(10)

        self.tts_enabled = QCheckBox("启用 TTS 语音")
        self.tts_enabled.setChecked(self._config.get("tts", {}).get("enabled", True))
        tts_layout.addRow(self.tts_enabled)

        self.tts_provider = QComboBox()
        self.tts_provider.addItems(["本地 CosyVoice", "MIMO TTS", "API 调用", "微软 Edge (免费)", "Qwen3-TTS 本地"])
        tts_prov_map = {"cosyvoice": 0, "mimo": 1, "api": 2, "edge": 3, "qwen": 4}
        self.tts_provider.setCurrentIndex(tts_prov_map.get(self._config.get("tts", {}).get("provider", "cosyvoice"), 0))
        tts_layout.addRow("TTS 引擎", self.tts_provider)

        # 微软 Edge 音色选择（仅当选中 Edge 引擎时显示）
        self.tts_edge_voice = QComboBox()
        try:
            from tts_provider.edge_tts import EDGE_VOICES, DEFAULT_VOICE
        except Exception:
            EDGE_VOICES, DEFAULT_VOICE = ["zh-CN-XiaoxiaoNeural"], "zh-CN-XiaoxiaoNeural"
        self.tts_edge_voice.addItems(EDGE_VOICES)
        cur_voice = self._config.get("tts", {}).get("edge_voice", DEFAULT_VOICE)
        idx = self.tts_edge_voice.findText(cur_voice)
        self.tts_edge_voice.setCurrentIndex(idx if idx >= 0 else 0)
        tts_layout.addRow("Edge 音色", self.tts_edge_voice)

        def _toggle_edge_voice():
            is_edge = self.tts_provider.currentIndex() == 3
            self.tts_edge_voice.setVisible(is_edge)
            self.tts_edge_voice.setEnabled(is_edge)
            # 重新布局以收起空行
            ok = self.tts_edge_voice.isVisibleTo(self)
            label = tts_layout.labelForField(self.tts_edge_voice)
            if label:
                label.setVisible(ok)

        self.tts_provider.currentIndexChanged.connect(_toggle_edge_voice)
        _toggle_edge_voice()

        self.tts_volume = QSlider(Qt.Horizontal)
        self.tts_volume.setRange(0, 100)
        self.tts_volume.setValue(int(self._config.get("tts", {}).get("volume", 0.8) * 100))
        self.tts_vol_label = QLabel(f"{self.tts_volume.value()}%")
        self.tts_volume.valueChanged.connect(lambda v: self.tts_vol_label.setText(f"{v}%"))
        vol_row = QHBoxLayout()
        vol_row.addWidget(self.tts_volume)
        vol_row.addWidget(self.tts_vol_label)
        tts_layout.addRow("音量", vol_row)

        voice_layout.addWidget(tts_group)

        # ASR
        asr_group = QGroupBox("语音输入")
        asr_layout = QFormLayout(asr_group)
        asr_layout.setSpacing(10)

        self.asr_provider = QComboBox()
        self.asr_provider.addItems(["本地 Whisper", "MIMO ASR", "API 调用"])
        asr_prov_map = {"whisper_local": 0, "mimo": 1, "api": 2}
        self.asr_provider.setCurrentIndex(asr_prov_map.get(self._config.get("asr", {}).get("provider", "whisper_local"), 0))
        asr_layout.addRow("ASR 引擎", self.asr_provider)

        # 本地 Whisper 后端（faster-whisper 需用户手动 pip install，不自动下载）
        self.asr_backend = QComboBox()
        self.asr_backend.addItems(["whisper (默认)", "faster-whisper (需手动安装)"])
        asr_backend_map = {"whisper": 0, "faster_whisper": 1}
        self.asr_backend.setCurrentIndex(
            asr_backend_map.get(self._config.get("asr", {}).get("backend", "whisper"), 0)
        )
        asr_layout.addRow("本地后端", self.asr_backend)

        # 语言：中文优化 / 自动检测（中英混合）
        self.asr_lang = QComboBox()
        self.asr_lang.addItems(["中文优化", "自动检测 (中英混合)"])
        asr_lang_map = {"zh": 0, "auto": 1}
        self.asr_lang.setCurrentIndex(
            asr_lang_map.get(self._config.get("asr", {}).get("language", "auto"), 1)
        )
        asr_layout.addRow("识别语言", self.asr_lang)

        # 麦克风设备选择（2026-08-22 新增：解决默认设备不是麦克风导致录不到人声）
        self.asr_device = QComboBox()
        self.asr_device.setMinimumWidth(220)
        try:
            import sounddevice as sd
            _devs = sd.query_devices()
            _default_in = None
            try:
                _default_in = sd.default.device[0]
            except Exception:
                logger.debug("settings_dialog: 非致命异常(已静默吞掉)", exc_info=True)
            self._asr_device_names = []  # (display, value)
            self.asr_device.addItem("系统默认设备", "")
            for _di, _d in enumerate(_devs):
                if _d.get("max_input_channels", 0) > 0:
                    _nm = str(_d.get("name", f"设备 {_di}"))
                    self._asr_device_names.append((_nm, _di))
                    self.asr_device.addItem(f"{_di}: {_nm}", str(_di))
            # 当前配置值
            _cur_dev = self._config.get("asr", {}).get("device", "")
            _found = False
            if _cur_dev:
                for _disp, _val in self._asr_device_names:
                    if str(_cur_dev) in (_nm := _disp) or str(_cur_dev) == str(_val):
                        _idx = self.asr_device.findData(str(_val))
                        if _idx >= 0:
                            self.asr_device.setCurrentIndex(_idx)
                            _found = True
                            break
            if not _found and _cur_dev:
                self.asr_device.addItem(f"{_cur_dev} (未找到)", str(_cur_dev))
                self.asr_device.setCurrentIndex(self.asr_device.count() - 1)
        except Exception as _e:
            # sounddevice 不可用时仍允许手动填写设备
            self.asr_device.setEditable(True)
            self.asr_device.setCurrentText(self._config.get("asr", {}).get("device", ""))
            logger.warning("麦克风设备枚举失败: %s", _e)
        asr_layout.addRow("麦克风设备", self.asr_device)

        voice_layout.addWidget(asr_group)
        voice_layout.addStretch()

        self.func_sub_tabs.addTab(voice_tab, "语音")

        # ── 子标签 2: 交互 ──
        interact_tab = QWidget()
        interact_layout = QVBoxLayout(interact_tab)
        interact_layout.setContentsMargins(12, 12, 12, 12)
        interact_layout.setSpacing(16)

        # 主动对话
        pro_group = QGroupBox("主动对话")
        pro_layout = QFormLayout(pro_group)
        pro_layout.setSpacing(10)

        self.pro_enabled = QCheckBox("启用主动搭话")
        self.pro_enabled.setChecked(self._config.get("proactive", {}).get("enabled", True))
        pro_layout.addRow(self.pro_enabled)

        self.pro_cooldown = QSpinBox()
        self.pro_cooldown.setRange(1, 120)
        self.pro_cooldown.setSuffix(" 分钟")
        self.pro_cooldown.setValue(self._config.get("proactive", {}).get("cooldown_minutes", 10))
        pro_layout.addRow("冷却时间", self.pro_cooldown)

        # 全屏检测：游戏/视频全屏时不打扰（阈值可调 + 可关）
        self.pro_fullscreen_suppress = QCheckBox("全屏时不主动搭话（游戏/视频）")
        self.pro_fullscreen_suppress.setChecked(
            self._config.get("proactive", {}).get("fullscreen_suppress", True)
        )
        pro_layout.addRow(self.pro_fullscreen_suppress)

        self.pro_fullscreen_threshold = QDoubleSpinBox()
        self.pro_fullscreen_threshold.setRange(0.5, 1.0)
        self.pro_fullscreen_threshold.setSingleStep(0.05)
        self.pro_fullscreen_threshold.setDecimals(2)
        self.pro_fullscreen_threshold.setSuffix(" 倍屏")
        self.pro_fullscreen_threshold.setValue(
            self._config.get("proactive", {}).get("fullscreen_threshold", 0.95)
        )
        pro_layout.addRow("全屏判定阈值", self.pro_fullscreen_threshold)

        interact_layout.addWidget(pro_group)

        # 屏幕感知
        screen_group = QGroupBox("屏幕感知")
        screen_layout = QFormLayout(screen_group)
        screen_layout.setSpacing(10)

        self.screen_enabled = QCheckBox("启用屏幕截屏分析")
        self.screen_enabled.setChecked(self._config.get("screen", {}).get("enabled", True))
        screen_layout.addRow(self.screen_enabled)

        self.screen_blur = QCheckBox("截图高斯模糊（降低文字可读性）")
        self.screen_blur.setChecked(self._config.get("screen", {}).get("blur", False))
        screen_layout.addRow(self.screen_blur)

        self.screen_blacklist = QCheckBox("敏感窗口黑名单（密码管理器/登录页自动跳过）")
        self.screen_blacklist.setChecked(self._config.get("screen", {}).get("blacklist", False))
        screen_layout.addRow(self.screen_blacklist)

        self.screen_compress = QCheckBox("截图缩放压缩（缩小4倍+50%质量，省流量）")
        self.screen_compress.setChecked(self._config.get("screen", {}).get("compress", True))
        screen_layout.addRow(self.screen_compress)

        self.screen_interval = QSpinBox()
        self.screen_interval.setRange(30, 600)
        self.screen_interval.setSuffix(" 秒")
        self.screen_interval.setValue(self._config.get("screen", {}).get("interval", 120))
        screen_layout.addRow("截屏间隔", self.screen_interval)

        # 随机间隔范围（可选）：勾选后每次截屏在下限~上限间随机，避免固定节奏
        self.screen_rand = QCheckBox("随机截屏间隔（更自然）")
        self.screen_rand.setChecked(bool(
            self._config.get("screen", {}).get("interval_min")
            and self._config.get("screen", {}).get("interval_max")
        ))
        screen_layout.addRow(self.screen_rand)
        self.screen_interval_min = QSpinBox()
        self.screen_interval_min.setRange(30, 600)
        self.screen_interval_min.setSuffix(" 秒")
        self.screen_interval_min.setValue(
            self._config.get("screen", {}).get("interval_min")
            or max(30, int(self._config.get("screen", {}).get("interval", 120) * 0.7))
        )
        self.screen_interval_max = QSpinBox()
        self.screen_interval_max.setRange(30, 600)
        self.screen_interval_max.setSuffix(" 秒")
        self.screen_interval_max.setValue(
            self._config.get("screen", {}).get("interval_max")
            or max(31, int(self._config.get("screen", {}).get("interval", 120) * 1.3))
        )
        rand_row = QHBoxLayout()
        rand_row.addWidget(QLabel("范围:"))
        rand_row.addWidget(self.screen_interval_min)
        rand_row.addWidget(QLabel(" 至 "))
        rand_row.addWidget(self.screen_interval_max)
        # 随机范围仅在勾选时启用
        self.screen_interval_min.setEnabled(self.screen_rand.isChecked())
        self.screen_interval_max.setEnabled(self.screen_rand.isChecked())
        self.screen_rand.toggled.connect(self.screen_interval_min.setEnabled)
        self.screen_rand.toggled.connect(self.screen_interval_max.setEnabled)
        screen_layout.addRow("", rand_row)

        interact_layout.addWidget(screen_group)

        # 窗口互动
        wi_group = QGroupBox("窗口互动")
        wi_layout = QFormLayout(wi_group)
        wi_layout.setSpacing(10)

        self.wi_enabled = QCheckBox("启用窗口互动")
        self.wi_enabled.setChecked(self._config.get("window_interaction", {}).get("enabled", True))
        wi_layout.addRow(self.wi_enabled)

        self.wi_auto_walk = QCheckBox("自动跟随窗口（切换窗口时桌宠走到窗口旁）")
        self.wi_auto_walk.setChecked(self._config.get("window_interaction", {}).get("auto_walk", False))
        wi_layout.addRow(self.wi_auto_walk)

        self.wi_cooldown = QSpinBox()
        self.wi_cooldown.setRange(5, 1800)
        self.wi_cooldown.setSuffix(" 秒")
        self.wi_cooldown.setValue(self._config.get("window_interaction", {}).get("cooldown_seconds", 600))
        wi_layout.addRow("冷却时间", self.wi_cooldown)

        interact_layout.addWidget(wi_group)

        # 久坐提醒
        break_group = QGroupBox("久坐提醒")
        break_layout = QFormLayout(break_group)
        break_layout.setSpacing(10)

        self.break_enabled = QCheckBox("启用久坐提醒")
        self.break_enabled.setChecked(self._config.get("break_reminder", {}).get("enabled", True))
        break_layout.addRow(self.break_enabled)

        self.break_idle = QSpinBox()
        self.break_idle.setRange(5, 120)
        self.break_idle.setSuffix(" 分钟")
        self.break_idle.setValue(self._config.get("break_reminder", {}).get("idle_minutes", 15))
        break_layout.addRow("空闲阈值", self.break_idle)

        self.break_cooldown = QSpinBox()
        self.break_cooldown.setRange(5, 120)
        self.break_cooldown.setSuffix(" 分钟")
        self.break_cooldown.setValue(self._config.get("break_reminder", {}).get("cooldown_minutes", 30))
        break_layout.addRow("提醒间隔", self.break_cooldown)

        interact_layout.addWidget(break_group)

        # 工作提醒
        work_group = QGroupBox("工作提醒")
        work_layout = QFormLayout(work_group)
        work_layout.setSpacing(10)

        self.work_enabled = QCheckBox("启用工作提醒")
        self.work_enabled.setChecked(self._config.get("work_reminder", {}).get("enabled", True))
        work_layout.addRow(self.work_enabled)

        self.work_after = QSpinBox()
        self.work_after.setRange(30, 240)
        self.work_after.setSuffix(" 分钟")
        self.work_after.setValue(self._config.get("work_reminder", {}).get("after_minutes", 90))
        work_layout.addRow("连续工作", self.work_after)

        self.work_late_night = QCheckBox("深夜加倍（22:00-06:00）")
        self.work_late_night.setChecked(True)
        work_layout.addRow(self.work_late_night)

        self.work_late_night_multiplier = QDoubleSpinBox()
        self.work_late_night_multiplier.setRange(1.0, 5.0)
        self.work_late_night_multiplier.setSuffix("x")
        self.work_late_night_multiplier.setSingleStep(0.5)
        self.work_late_night_multiplier.setValue(self._config.get("work_reminder", {}).get("late_night_multiplier", 3.0))
        work_layout.addRow("深夜倍数", self.work_late_night_multiplier)

        self.work_cooldown = QSpinBox()
        self.work_cooldown.setRange(10, 120)
        self.work_cooldown.setSuffix(" 分钟")
        self.work_cooldown.setValue(self._config.get("work_reminder", {}).get("cooldown_minutes", 60))
        work_layout.addRow("提醒间隔", self.work_cooldown)

        self.work_snooze = QSpinBox()
        self.work_snooze.setRange(5, 60)
        self.work_snooze.setSuffix(" 分钟")
        self.work_snooze.setValue(self._config.get("work_reminder", {}).get("snooze_minutes", 10))
        work_layout.addRow("稍后提醒", self.work_snooze)

        self.work_tts = QCheckBox("语音提醒")
        self.work_tts.setChecked(self._config.get("work_reminder", {}).get("tts_enabled", False))
        work_layout.addRow(self.work_tts)

        interact_layout.addWidget(work_group)
        interact_layout.addStretch()

        self.func_sub_tabs.addTab(interact_tab, "交互")

        # ── 子标签 3: 记忆 ──
        memory_tab = QWidget()
        memory_layout = QVBoxLayout(memory_tab)
        memory_layout.setContentsMargins(12, 12, 12, 12)
        memory_layout.setSpacing(16)

        # 记忆注入
        mem_group = QGroupBox("记忆注入")
        mem_layout = QFormLayout(mem_group)
        mem_layout.setSpacing(10)

        mem_mode = self._config.get("memory", {}).get("budget_mode", "auto")
        self.mem_mode = QComboBox()
        self.mem_mode.addItems(["自动（按模型上下文 1%）", "手动指定"])
        self.mem_mode.setCurrentIndex(0 if mem_mode == "auto" else 1)
        mem_layout.addRow("预算模式", self.mem_mode)

        self.mem_budget = QSpinBox()
        self.mem_budget.setRange(200, 20000)
        self.mem_budget.setSuffix(" 字符")
        self.mem_budget.setSingleStep(200)
        self.mem_budget.setValue(self._config.get("memory", {}).get("budget_chars", 3000))
        self.mem_budget.setEnabled(mem_mode != "auto")
        self.mem_mode.currentIndexChanged.connect(
            lambda idx: self.mem_budget.setEnabled(idx == 1)
        )
        mem_layout.addRow("记忆上限", self.mem_budget)

        self.mem_hint = QLabel("agnes-2.0-flash (1M tokens) → 自动预算 6000 字符")
        self.mem_hint.setStyleSheet("color: rgb(%s); font-size: 10px;" % rgb(self._ui_theme, "text_muted"))
        mem_layout.addRow(self.mem_hint)

        memory_layout.addWidget(mem_group)
        
        # 2026-09-07: 会话记忆开关（新开对话是否保存记忆）
        sess_group = QGroupBox("会话记忆")
        sess_layout = QFormLayout(sess_group)
        sess_layout.setSpacing(10)
        
        self.sess_memory_enabled = QCheckBox("新开对话保存记忆（默认关闭，避免污染主会话）")
        self.sess_memory_enabled.setChecked(self._config.get("session_memory_enabled", False))
        sess_layout.addRow(self.sess_memory_enabled)
        
        sess_hint = QLabel("关闭时，桌宠新开的 Hana 对话不会保存记忆，只在配置显式启用时才保存。")
        sess_hint.setWordWrap(True)
        sess_hint.setStyleSheet("color: rgb(%s); font-size: 10px;" % rgb(self._ui_theme, "text_muted"))
        sess_layout.addRow(sess_hint)
        
        memory_layout.addWidget(sess_group)
        memory_layout.addStretch()

        self.func_sub_tabs.addTab(memory_tab, "记忆")

        func_layout.addWidget(self.func_sub_tabs)
        self._main_tabs.addTab(func_tab, "⚙️ 功能")
        
        # ── Tab 2.1: 快捷键设置 ──
        shortcuts_tab = QWidget()
        shortcuts_layout = QVBoxLayout(shortcuts_tab)
        shortcuts_layout.setContentsMargins(12, 12, 12, 12)
        shortcuts_layout.setSpacing(16)
        
        # 页面引导说明
        shortcuts_hint = QLabel("配置右键菜单的键盘快捷键。禁用后快捷键不会响应。")
        shortcuts_hint.setWordWrap(True)
        shortcuts_hint.setStyleSheet("color: rgb(%s); font-size: 11px; margin-bottom: 2px;" % rgb(self._ui_theme, "text_muted"))
        shortcuts_layout.addWidget(shortcuts_hint)
        
        # 快捷键开关
        shortcuts_group = QGroupBox("快捷键")
        shortcuts_group_layout = QVBoxLayout(shortcuts_group)
        
        self.shortcuts_enabled = QCheckBox("启用快捷键")
        self.shortcuts_enabled.setChecked(self._config.get("shortcuts", {}).get("enabled", True))
        shortcuts_group_layout.addWidget(self.shortcuts_enabled)
        
        # 快捷键列表
        self._shortcuts_list = []
        shortcuts_defaults = [
            ("对话", "Ctrl+L", "toggle_input"),
            ("说话", "Ctrl+D", "toggle_voice"),
            ("持续监听", "Ctrl+Shift+D", "toggle_voice_continuous"),
            ("穿透", "Ctrl+P", "toggle_passthrough"),
            ("活动流", "Ctrl+H", "open_activity_feed"),
            ("设置", "Ctrl+S", "open_settings"),
            ("插件", "Ctrl+Shift+P", "open_plugin_panel"),
        ]
        
        shortcuts_table = QFormLayout()
        shortcuts_table.setSpacing(10)
        
        shortcuts_cfg = self._config.get("shortcuts", {}).get("keys", {})
        for label, default_key, action_id in shortcuts_defaults:
            # 加载当前配置或使用默认值
            current_key = shortcuts_cfg.get(action_id, default_key)
            # 创建快捷键输入框
            key_input = QLineEdit(current_key)
            key_input.setPlaceholderText(default_key)
            key_input.setMinimumWidth(120)
            # 连接信号
            def _on_shortcut_changed(text, aid=action_id):
                self._config.setdefault("shortcuts", {}).setdefault("keys", {})[aid] = text
            key_input.textEdited.connect(_on_shortcut_changed)
            shortcuts_table.addRow(f"{label}", key_input)
            self._shortcuts_list.append((key_input, action_id))
        
        shortcuts_group_layout.addLayout(shortcuts_table)
        shortcuts_layout.addWidget(shortcuts_group)
        
        shortcuts_layout.addStretch()
        self._main_tabs.addTab(shortcuts_tab, "⌨️ 快捷键")

        # ── Tab 2.5: 角色包管理 (M5) ──
        pkg_tab = QWidget()
        pkg_layout = QVBoxLayout(pkg_tab)
        pkg_layout.setContentsMargins(12, 12, 12, 12)
        pkg_layout.setSpacing(16)

        try:
            from core.character_package import CharacterPackageManager
            self._pkg_mgr = CharacterPackageManager()
        except Exception as e:
            self._pkg_mgr = None
            logger = __import__('logging').getLogger(__name__)
            logger.warning("CharacterPackageManager not available: %s", e)

        pkg_group = QGroupBox("角色包管理 (M5)")
        pkg_group_layout = QVBoxLayout(pkg_group)

        # 已安装列表
        self._pkg_list = QListWidget()
        self._pkg_list.setMinimumHeight(100)
        pkg_group_layout.addWidget(self._pkg_list)

        # 操作按钮行
        pkg_btns_row1 = QHBoxLayout()

        self._import_pkg_btn = QPushButton("📦 导入 .pet/.zip")
        self._import_pkg_btn.clicked.connect(self._import_package)
        pkg_btns_row1.addWidget(self._import_pkg_btn)

        self._export_pkg_btn = QPushButton("💾 导出选中")
        self._export_pkg_btn.clicked.connect(self._export_package)
        self._export_pkg_btn.setEnabled(False)
        self._pkg_list.currentRowChanged.connect(lambda r: self._export_pkg_btn.setEnabled(r >= 0))
        pkg_btns_row1.addWidget(self._export_pkg_btn)

        pkg_group_layout.addLayout(pkg_btns_row1)

        pkg_btns_row2 = QHBoxLayout()

        self._uninstall_pkg_btn = QPushButton("🗑️ 卸载选中")
        self._uninstall_pkg_btn.setObjectName("danger")
        self._uninstall_pkg_btn.clicked.connect(self._uninstall_package)
        self._uninstall_pkg_btn.setEnabled(False)
        self._pkg_list.currentRowChanged.connect(lambda r: self._uninstall_pkg_btn.setEnabled(r >= 0))
        pkg_btns_row2.addWidget(self._uninstall_pkg_btn)

        self._refresh_pkg_btn = QPushButton("🔄 刷新列表")
        self._refresh_pkg_btn.clicked.connect(self._refresh_package_list)
        pkg_btns_row2.addWidget(self._refresh_pkg_btn)

        pkg_group_layout.addLayout(pkg_btns_row2)

        # 切换桌宠按钮
        self._switch_pet_btn = QPushButton("🔄 切换选中桌宠")
        self._switch_pet_btn.clicked.connect(self._switch_pet)
        self._switch_pet_btn.setEnabled(False)
        self._pkg_list.currentRowChanged.connect(lambda r: self._switch_pet_btn.setEnabled(r >= 0))
        pkg_group_layout.addWidget(self._switch_pet_btn)

        # 状态标签
        self._pkg_status_label = QLabel("就绪")
        self._pkg_status_label.setStyleSheet("color: rgb(%s); font-size: 10px;" % rgb(self._ui_theme, "text_muted"))
        pkg_group_layout.addWidget(self._pkg_status_label)

        # 刷新列表推迟到 showEvent，避免构造时阻塞

        pkg_layout.addWidget(pkg_group)
        pkg_layout.addStretch()
        self._main_tabs.addTab(pkg_tab, "📦 角色包")

        # 2026-09-06: P2 主动对话配置
        proactive_tab = QWidget()
        proactive_layout = QVBoxLayout(proactive_tab)
        proactive_layout.setContentsMargins(16, 16, 16, 16)
        proactive_layout.setSpacing(16)

        proactive_hint = QLabel("配置桌宠的主动对话行为。改动保存后下次启动生效。")
        proactive_hint.setWordWrap(True)
        proactive_hint.setStyleSheet("color: rgb(%s); font-size: 11px; margin-bottom: 2px;" % rgb(self._ui_theme, "text_muted"))
        proactive_layout.addWidget(proactive_hint)

        # 主动对话总开关
        proactive_group = QGroupBox("主动对话")
        proactive_form = QFormLayout(proactive_group)
        proactive_form.setSpacing(10)

        self.proactive_enabled = QCheckBox("启用主动对话")
        self.proactive_enabled.setChecked(self._config.get("proactive", {}).get("enabled", True))
        proactive_form.addRow(self.proactive_enabled)

        # 每日上限
        self.proactive_daily_limit = QSpinBox()
        self.proactive_daily_limit.setRange(0, 20)
        self.proactive_daily_limit.setValue(self._config.get("proactive", {}).get("daily_budget", {}).get("daily_limit", 6))
        proactive_form.addRow("每日上限", self.proactive_daily_limit)

        # 免打扰时段
        self.proactive_dnd_start = QSpinBox()
        self.proactive_dnd_start.setRange(0, 23)
        self.proactive_dnd_start.setValue(self._config.get("proactive", {}).get("dnd", {}).get("late_night_start", 0))
        proactive_form.addRow("免打扰开始", self.proactive_dnd_start)

        self.proactive_dnd_end = QSpinBox()
        self.proactive_dnd_end.setRange(0, 23)
        self.proactive_dnd_end.setValue(self._config.get("proactive", {}).get("dnd", {}).get("late_night_end", 8))
        proactive_form.addRow("免打扰结束", self.proactive_dnd_end)

        proactive_layout.addWidget(proactive_group)
        proactive_layout.addStretch()
        self._main_tabs.addTab(proactive_tab, "💬 主动对话")

        # ── Tab 3: API 配置 ──
        api_tab = QWidget()
        api_layout = QVBoxLayout(api_tab)
        api_layout.setContentsMargins(16, 16, 16, 16)
        api_layout.setSpacing(16)

        api_group = QGroupBox("API 配置（留空 = 用 Hanako 默认）")
        api_form = QFormLayout(api_group)
        api_form.setSpacing(10)

        # 读取 provider catalog 获取可用模型
        self._catalog_models = self._load_catalog_models()

        # LLM Provider 快速选择
        self.llm_provider_select = QComboBox()
        self.llm_provider_select.addItem("手动填写", "")
        for pid in self._catalog_models.get("providers", []):
            self.llm_provider_select.addItem(pid, pid)
        self.llm_provider_select.currentIndexChanged.connect(self._on_llm_provider_select)
        api_form.addRow("LLM Provider", self.llm_provider_select)

        self.llm_url = QLineEdit()
        self.llm_url.setPlaceholderText("留空用 Hanako")
        api_form.addRow("LLM 地址", self.llm_url)

        self.llm_key = QLineEdit()
        self.llm_key.setEchoMode(QLineEdit.Password)
        self.llm_key.setPlaceholderText("留空用 Hanako")
        api_form.addRow("LLM Key", self.llm_key)

        self.llm_model = QComboBox()
        self.llm_model.setEditable(True)
        self.llm_model.addItems(self._catalog_models.get("llm", []))
        self.llm_model.setCurrentText("")
        self.llm_model.lineEdit().setPlaceholderText("留空用 Hanako")
        api_form.addRow("LLM 模型", self.llm_model)

        # TTS API provider 快速选择
        self.tts_provider_select = QComboBox()
        self.tts_provider_select.addItem("手动填写", "")
        for pid in self._catalog_models.get("providers", []):
            self.tts_provider_select.addItem(pid, pid)
        self.tts_provider_select.currentIndexChanged.connect(self._on_tts_provider_select)
        api_form.addRow("TTS Provider", self.tts_provider_select)

        self.tts_url = QLineEdit()
        self.tts_url.setPlaceholderText("TTS API 地址")
        api_form.addRow("TTS 地址", self.tts_url)

        self.tts_key = QLineEdit()
        self.tts_key.setEchoMode(QLineEdit.Password)
        self.tts_key.setPlaceholderText("TTS Key")
        api_form.addRow("TTS Key", self.tts_key)

        self.tts_model = QComboBox()
        self.tts_model.setEditable(True)
        self.tts_model.lineEdit().setPlaceholderText("tts-1（OpenAI 默认）")
        api_form.addRow("TTS 模型", self.tts_model)

        self.tts_voice = QComboBox()
        self.tts_voice.setEditable(True)
        # MIMO + 通用音色
        mimo_voices = ["mimo_default", "冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"]
        openai_voices = ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]
        self.tts_voice.addItems(mimo_voices + ["─── OpenAI ───"] + openai_voices)
        self.tts_voice.lineEdit().setPlaceholderText("选择或输入音色")
        api_form.addRow("TTS 音色", self.tts_voice)

        # ASR API provider 快速选择
        self.asr_provider_select = QComboBox()
        self.asr_provider_select.addItem("手动填写", "")
        for pid in self._catalog_models.get("providers", []):
            self.asr_provider_select.addItem(pid, pid)
        self.asr_provider_select.currentIndexChanged.connect(self._on_asr_provider_select)
        api_form.addRow("ASR Provider", self.asr_provider_select)

        self.asr_url = QLineEdit()
        self.asr_url.setPlaceholderText("ASR API 地址")
        api_form.addRow("ASR 地址", self.asr_url)

        self.asr_key = QLineEdit()
        self.asr_key.setEchoMode(QLineEdit.Password)
        self.asr_key.setPlaceholderText("ASR Key")
        api_form.addRow("ASR Key", self.asr_key)

        self.asr_model = QComboBox()
        self.asr_model.setEditable(True)
        self.asr_model.lineEdit().setPlaceholderText("whisper-1（OpenAI 默认）")
        api_form.addRow("ASR 模型", self.asr_model)

        # ── 视觉模型配置（M2 屏幕感知专用）──
        self._vision_separator = QLabel("─── 视觉模型（屏幕感知专用）───")
        self._vision_separator.setStyleSheet("color: rgb(%s); font-weight: bold; margin-top: 10px;" % rgb(self._ui_theme, "text_muted"))
        api_form.addRow(self._vision_separator)

        self._vision_hint = QLabel("留空则使用 LLM 配置。建议使用支持图片的模型（如 agnes-2.0-flash、GPT-4V 等）")
        self._vision_hint.setWordWrap(True)
        self._vision_hint.setStyleSheet("color: rgb(%s); font-size: 11px;" % rgb(self._ui_theme, "text_muted"))
        api_form.addRow(self._vision_hint)

        # 视觉 Provider 快速选择
        self.vision_provider_select = QComboBox()
        self.vision_provider_select.addItem("手动填写", "")
        for pid in self._catalog_models.get("providers", []):
            self.vision_provider_select.addItem(pid, pid)
        self.vision_provider_select.currentIndexChanged.connect(self._on_vision_provider_select)
        api_form.addRow("视觉 Provider", self.vision_provider_select)

        self.vision_url = QLineEdit()
        self.vision_url.setPlaceholderText("视觉 API 地址（留空用 LLM 配置）")
        api_form.addRow("视觉地址", self.vision_url)

        self.vision_key = QLineEdit()
        self.vision_key.setEchoMode(QLineEdit.Password)
        self.vision_key.setPlaceholderText("视觉 API Key（留空用 LLM 配置）")
        api_form.addRow("视觉 Key", self.vision_key)

        self.vision_model = QComboBox()
        self.vision_model.setEditable(True)
        # 推荐的视觉模型
        vision_models = ["agnes-2.0-flash", "gpt-4o", "gpt-4-vision-preview", "claude-3-opus", "claude-3-sonnet"]
        self.vision_model.addItems(vision_models)
        self.vision_model.setCurrentText("")
        self.vision_model.lineEdit().setPlaceholderText("留空用 LLM 模型")
        api_form.addRow("视觉模型", self.vision_model)

        api_layout.addWidget(api_group)
        api_layout.addStretch()
        self._main_tabs.addTab(api_tab, "🔌 API")

        # ── Tab: MCP（游戏 MCP 桥接分类：Minecraft + Skyrim 两个子标签）──
        self._main_tabs.addTab(self._build_mcp_tab(), "🔗 MCP")

        # ── Tab: QQ/微信（需求③：Hanako 只读桥接）──
        self._main_tabs.addTab(self._build_hb_tab(), "💬 QQ/微信")

        # .env / agent / 角色包列表在 showEvent 中异步加载

        # ── 按钮 ──
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("保存")
        save_btn.setObjectName("save")
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        layout.addLayout(btn_row)

    def showEvent(self, event):
        """对话框显示后再异步加载可能阻塞的列表，避免卡死"""
        super().showEvent(event)
        QTimer.singleShot(0, self._load_deferred_data)

    def _load_deferred_data(self):
        """延迟加载 角色包/环境变量，失败也不阻塞 UI"""
        try:
            self._load_env_to_ui()
        except Exception:
            logger.debug("settings_dialog: 非致命异常(已静默吞掉)", exc_info=True)
        try:
            self._refresh_package_list()
        except Exception:
            logger.debug("settings_dialog: 非致命异常(已静默吞掉)", exc_info=True)

    # ── M5: 角色包管理 ──

    def _refresh_package_list(self):
        """刷新角色包列表"""
        if not hasattr(self, '_pkg_list') or not self._pkg_mgr:
            return
        self._pkg_list.clear()
        try:
            packages = self._pkg_mgr.list_installed_packages()
            for pkg in packages:
                version_tag = f" v{pkg.version}" if pkg.version and pkg.version != "?" else ""
                desc_tag = f" - {pkg.description}" if pkg.description and pkg.description != "(无 manifest)" else ""
                display_text = f"{pkg.name}{version_tag}{desc_tag}"
                # 标注缺少模型资源的角色（如 shizuku 模型未下载），提示用户无法切换
                try:
                    from avatar.factory import resource_available
                    _ok, _reason = resource_available(pkg.agent_id)
                    if not _ok:
                        display_text += "（需下载模型）"
                except Exception:
                    logger.debug("settings_dialog: 非致命异常(已静默吞掉)", exc_info=True)
                item = QListWidgetItem(display_text)
                item.setData(Qt.UserRole, pkg.agent_id)  # 存储 agent_id
                self._pkg_list.addItem(item)
            self._pkg_status_label.setText(f"共 {len(packages)} 个已安装角色")
        except Exception as e:
            logger = __import__('logging').getLogger(__name__)
            logger.warning("刷新角色包列表失败: %s", e)
            self._pkg_status_label.setText(f"加载失败: {e}")

    def _import_package(self):
        """导入 .pet 文件"""
        if not self._pkg_mgr:
            QMessageBox.warning(self, "提示", "角色包管理器不可用")
            return

        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "导入角色包", "",
            "角色包 (*.pet *.zip);;所有文件 (*)"
        )
        if not path:
            return

        try:
            result = self._pkg_mgr.install_package(path, overwrite=False)
            self._pkg_status_label.setText(f"导入成功: {result}")
            self._refresh_package_list()
            QMessageBox.information(self, "成功", f"角色包安装成功！\n{result}")
        except Exception as e:
            QMessageBox.critical(self, "导入失败", str(e))
            self._pkg_status_label.setText(f"导入失败: {e}")

    def _export_package(self):
        """导出选中的角色为 .pet 文件"""
        if not self._pkg_mgr:
            return
        row = self._pkg_list.currentRow()
        if row < 0:
            return

        # 从 UserRole 数据获取 agent_id
        item = self._pkg_list.item(row)
        agent_id = item.data(Qt.UserRole)
        if not agent_id:
            # 兜底：从文本中提取
            item_text = item.text()
            agent_id = item_text.split(" ")[0]

        from PySide6.QtWidgets import QFileDialog
        out_path, _ = QFileDialog.getSaveFileName(
            self, "导出角色包", f"{agent_id}.pet", "角色包 (*.pet)"
        )
        if not out_path:
            return

        try:
            result_path = self._pkg_mgr.create_package(agent_id, output_path=out_path)
            self._pkg_status_label.setText(f"导出成功: {result_path}")
            QMessageBox.information(self, "成功", f"角色包已导出到:\n{result_path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))
            self._pkg_status_label.setText(f"导出失败: {e}")

    def _uninstall_package(self):
        """卸载选中的角色"""
        if not self._pkg_mgr:
            return
        row = self._pkg_list.currentRow()
        if row < 0:
            return

        # 从 UserRole 数据获取 agent_id
        item = self._pkg_list.item(row)
        agent_id = item.data(Qt.UserRole)
        if not agent_id:
            # 兜底：从文本中提取
            item_text = item.text()
            agent_id = item_text.split(" ")[0]

        reply = QMessageBox.question(
            self, "确认卸载",
            f"确定要卸载角色 '{agent_id}' 吗？\n（精灵文件和配置将被删除）",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            success = self._pkg_mgr.uninstall_package(agent_id)
            if success:
                self._pkg_status_label.setText(f"已卸载: {agent_id}")
                self._refresh_package_list()
                QMessageBox.information(self, "成功", f"角色 '{agent_id}' 已卸载")
            else:
                QMessageBox.warning(self, "提示", f"角色 '{agent_id}' 不存在")
        except Exception as e:
            QMessageBox.critical(self, "卸载失败", str(e))

    def _switch_pet(self):
        """切换到选中的桌宠"""
        row = self._pkg_list.currentRow()
        if row < 0:
            return

        item = self._pkg_list.item(row)
        agent_id = item.data(Qt.UserRole)
        if not agent_id:
            item_text = item.text()
            agent_id = item_text.split(" ")[0]

        # 预校验：角色是否具备可加载资源（如 shizuku 声明 live2d 但模型未下载）。
        # 缺资源则不切换，避免重启后白屏 / 静默加载失败。
        try:
            from avatar.factory import resource_available
            _ok, _reason = resource_available(agent_id)
        except Exception as _e:
            logger.warning("资源预校验失败（放行）：%s", _e)
            _ok, _reason = True, ""
        if not _ok:
            QMessageBox.warning(
                self, "无法切换",
                f"角色 '{agent_id}' 缺少可加载的模型资源：\n{_reason}\n\n"
                f"请先放置模型文件后再切换。",
            )
            return

        # 统一应用切换：agents[].enabled + character + character_package
        self._apply_package_selection(agent_id)
        
        # P2 Fix: 刷新防抖写盘 pending，确保旧配置不会覆盖本次切换结果。
        # 原顺序（save → schedule）有竞态：若后台线程正在写旧角色配置，
        # schedule 的新配置会被覆盖。改为先 flush 旧配置，再保存新配置。
        try:
            from config import async_config_saver
            async_config_saver.shutdown()  # 立即写盘 pending（若有）
            async_config_saver.schedule(self._config)  # 登记新配置
        except Exception:
            logger.debug("settings_dialog: 非致命异常(已静默吞掉)", exc_info=True)
        
        save_config(self._config)
        self._pkg_status_label.setText(f"已切换到: {agent_id}")
        QMessageBox.information(self, "切换成功", f"桌宠已切换为 '{agent_id}'，重启后生效")

    def _current_active_agent_id(self):
        """返回当前启用中的桌宠 agent_id（agents[].enabled=True 的项）。

        多个同时启用时返回 None（多宠模式，下拉框显示"默认"不强制单一角色）。
        """
        try:
            enabled = [a.get("id") for a in self._config.get("agents", []) if a.get("enabled")]
        except Exception:
            return None
        if len(enabled) == 1:
            return enabled[0]
        return None

    def _sync_pkg_select(self, agent_id):
        """把基础 tab 的角色包下拉框同步到指定 agent_id（找不到则不动）。

        BugFix #4：基础 tab 的角色包下拉框已删除，本方法保留为兼容空操作
        （M5 角色包管理 tab 切换时不再需要同步下拉框）。
        """
        if not hasattr(self, "_pkg_select"):
            return
        idx = self._pkg_select.findData(agent_id)
        if idx >= 0:
            self._pkg_select.setCurrentIndex(idx)

    def _apply_package_selection(self, agent_id):
        """统一应用"切换桌宠"语义（不落盘，由调用方决定保存时机）。

        任务 #3 修复：设置面板原来有两套互不相通的机制——
          - 角色包管理 tab _switch_pet 写 config["character"] + agents[].enabled（启动生效）
          - 基础 tab 下拉框 _save 写 config["character_package"]（启动无人读取，死字段）
        统一后：agents[].enabled 是唯一启动真相源，character/character_package
        同步为展示字段，两处切换走同一套逻辑。

        BugFix #4：基础 tab 的"角色包"下拉框已删除（用户意图：只保留角色包管理
        tab 的切换入口），本函数现在只由 M5「切换选中桌宠」调用。
        """
        if not agent_id or agent_id == "default":
            return
        agents = self._config.setdefault("agents", [])
        found = False
        for a in agents:
            if a.get("id") == agent_id:
                a["enabled"] = True
                found = True
            else:
                a["enabled"] = False
        if not found:
            is_builtin = False
            try:
                pkg_mgr = getattr(self, "_pkg_mgr", None)
                if pkg_mgr is not None and (pkg_mgr.characters_dir / agent_id).exists():
                    is_builtin = True
            except Exception:
                logger.debug("settings_dialog: 非致命异常(已静默吞掉)", exc_info=True)
            agents.append({
                "id": agent_id,
                "enabled": True,
                "position": {"x": -1, "y": -1},
                "scale": 1.0,
                "builtin": is_builtin,
            })
        self._config["character"] = agent_id
        self._config["character_package"] = agent_id
        
        # 2026-09-08: 切换桌宠后自动保存配置（避免用户需要手动保存）
        try:
            from config import save_config
            save_config(self._config)
            self._pkg_status_label.setText(f"已切换为: {agent_id}（下次重启生效）")
        except Exception as e:
            self._pkg_status_label.setText(f"切换成功但保存失败: {e}")

    # ── Provider Catalog ──

    @staticmethod
    def _load_catalog_models() -> dict:
        """从 provider-catalog.json 读取所有可用模型

        Returns:
            {"llm": [...], "providers": [...], "provider_map": {...},
             "provider_configs": {"prov_id": {base_url, api_key, models}}}
        """
        import json
        from pathlib import Path
        catalog_path = Path.home() / ".hanako" / "provider-catalog.json"
        llm_models = []
        provider_map = {}
        provider_configs = {}
        try:
            if catalog_path.exists():
                data = json.loads(catalog_path.read_text("utf-8"))
                for prov_id, prov_cfg in data.get("providers", {}).items():
                    provider_configs[prov_id] = {
                        "base_url": prov_cfg.get("base_url", ""),
                        "api_key": prov_cfg.get("api_key", ""),
                    }
                    for m in prov_cfg.get("models", []):
                        if isinstance(m, dict):
                            mid = m.get("id", "")
                            if mid:
                                label = f"{mid}  [{prov_id}]"
                                llm_models.append(label)
                                provider_map[label] = prov_id
                        elif isinstance(m, str) and m:
                            label = f"{m}  [{prov_id}]"
                            llm_models.append(label)
                            provider_map[label] = prov_id
        except Exception:
            logger.debug("settings_dialog: 非致命异常(已静默吞掉)", exc_info=True)
        return {
            "llm": sorted(set(llm_models)),
            "providers": sorted(provider_configs.keys()),
            "provider_map": provider_map,
            "provider_configs": provider_configs,
        }

    def _on_llm_provider_select(self, idx: int):
        """LLM provider 下拉选择 → 自动填充 URL、Key、模型列表"""
        prov_id = self.llm_provider_select.itemData(idx)
        if not prov_id:
            return
        cfg = self._catalog_models.get("provider_configs", {}).get(prov_id, {})
        if cfg.get("base_url"):
            self.llm_url.setText(cfg["base_url"])
        if cfg.get("api_key"):
            self.llm_key.setText(cfg["api_key"])
        self.llm_model.clear()
        models = []
        try:
            from pathlib import Path
            import json
            catalog_path = Path.home() / ".hanako" / "provider-catalog.json"
            data = json.loads(catalog_path.read_text("utf-8"))
            prov_models = data.get("providers", {}).get(prov_id, {}).get("models", [])
            for m in prov_models:
                if isinstance(m, dict):
                    models.append(m.get("id", ""))
                elif isinstance(m, str):
                    models.append(m)
        except Exception:
            logger.debug("settings_dialog: 非致命异常(已静默吞掉)", exc_info=True)
        self.llm_model.addItems([m for m in models if m])

    def _on_tts_provider_select(self, idx: int):
        """TTS provider 下拉选择 → 自动填充 URL、Key、模型列表"""
        prov_id = self.tts_provider_select.itemData(idx)
        if not prov_id:
            return
        
        # 只在 TTS 引擎是 "本地 CosyVoice"（索引 0）时才联动
        # 如果用户已经手动选择了 "MIMO TTS" 或 "API 调用"，则不覆盖
        if self.tts_provider.currentIndex() == 0:
            # TTS 引擎下拉框选项：["本地 CosyVoice", "MIMO TTS", "API 调用"]，索引 2 是 "API 调用"
            self.tts_provider.setCurrentIndex(2)
        
        cfg = self._catalog_models.get("provider_configs", {}).get(prov_id, {})
        if cfg.get("base_url"):
            self.tts_url.setText(cfg["base_url"])
        if cfg.get("api_key"):
            self.tts_key.setText(cfg["api_key"])
        # 填充该 provider 的模型列表
        self.tts_model.clear()
        models = []
        try:
            from pathlib import Path
            import json
            catalog_path = Path.home() / ".hanako" / "provider-catalog.json"
            data = json.loads(catalog_path.read_text("utf-8"))
            prov_models = data.get("providers", {}).get(prov_id, {}).get("models", [])
            for m in prov_models:
                if isinstance(m, dict):
                    models.append(m.get("id", ""))
                elif isinstance(m, str):
                    models.append(m)
        except Exception:
            logger.debug("settings_dialog: 非致命异常(已静默吞掉)", exc_info=True)
        self.tts_model.addItems([m for m in models if m])

    def _on_asr_provider_select(self, idx: int):
        """ASR provider 下拉选择 → 自动填充 URL、Key、模型列表"""
        prov_id = self.asr_provider_select.itemData(idx)
        if not prov_id:
            return
        
        # 只在 ASR 引擎是 "本地 Whisper"（索引 0）时才联动
        # 如果用户已经手动选择了 "MIMO ASR" 或 "API 调用"，则不覆盖
        if self.asr_provider.currentIndex() == 0:
            # ASR 引擎下拉框选项：["本地 Whisper", "MIMO ASR", "API 调用"]，索引 2 是 "API 调用"
            self.asr_provider.setCurrentIndex(2)
        
        cfg = self._catalog_models.get("provider_configs", {}).get(prov_id, {})
        if cfg.get("base_url"):
            self.asr_url.setText(cfg["base_url"])
        if cfg.get("api_key"):
            self.asr_key.setText(cfg["api_key"])
        self.asr_model.clear()
        models = []
        try:
            from pathlib import Path
            import json
            catalog_path = Path.home() / ".hanako" / "provider-catalog.json"
            data = json.loads(catalog_path.read_text("utf-8"))
            prov_models = data.get("providers", {}).get(prov_id, {}).get("models", [])
            for m in prov_models:
                if isinstance(m, dict):
                    models.append(m.get("id", ""))
                elif isinstance(m, str):
                    models.append(m)
        except Exception:
            logger.debug("settings_dialog: 非致命异常(已静默吞掉)", exc_info=True)
        self.asr_model.addItems([m for m in models if m])

    def _on_vision_provider_select(self, idx: int):
        """视觉 provider 下拉选择 → 自动填充 URL、Key、模型列表"""
        prov_id = self.vision_provider_select.itemData(idx)
        if not prov_id:
            return
        cfg = self._catalog_models.get("provider_configs", {}).get(prov_id, {})
        if cfg.get("base_url"):
            self.vision_url.setText(cfg["base_url"])
        if cfg.get("api_key"):
            self.vision_key.setText(cfg["api_key"])
        # 保留推荐模型，追加该 provider 的模型列表
        current_models = ["agnes-2.0-flash", "gpt-4o", "gpt-4-vision-preview", "claude-3-opus", "claude-3-sonnet"]
        try:
            from pathlib import Path
            import json
            catalog_path = Path.home() / ".hanako" / "provider-catalog.json"
            data = json.loads(catalog_path.read_text("utf-8"))
            prov_models = data.get("providers", {}).get(prov_id, {}).get("models", [])
            for m in prov_models:
                if isinstance(m, dict):
                    model_id = m.get("id", "")
                elif isinstance(m, str):
                    model_id = m
                else:
                    continue
                if model_id and model_id not in current_models:
                    current_models.append(model_id)
        except Exception:
            logger.debug("settings_dialog: 非致命异常(已静默吞掉)", exc_info=True)
        self.vision_model.clear()
        self.vision_model.addItems(current_models)

    # ── .env 读写 ──

    def _load_env_to_ui(self):
        from env_config import ENV_PATH
        if not ENV_PATH.exists():
            return
        try:
            for line in ENV_PATH.read_text("utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                mapping = {
                    "LLM_BASE_URL": self.llm_url,
                    "LLM_API_KEY": self.llm_key,
                    "TTS_BASE_URL": self.tts_url,
                    "TTS_API_KEY": self.tts_key,
                    "ASR_BASE_URL": self.asr_url,
                    "ASR_API_KEY": self.asr_key,
                }
                if key in mapping:
                    mapping[key].setText(val)
                elif key == "LLM_PROVIDER" and val:
                    for i in range(self.llm_provider_select.count()):
                        if self.llm_provider_select.itemData(i) == val:
                            self.llm_provider_select.setCurrentIndex(i)
                            break
                elif key == "TTS_PROVIDER" and val:
                    for i in range(self.tts_provider_select.count()):
                        if self.tts_provider_select.itemData(i) == val:
                            self.tts_provider_select.setCurrentIndex(i)
                            break
                elif key == "ASR_PROVIDER" and val:
                    for i in range(self.asr_provider_select.count()):
                        if self.asr_provider_select.itemData(i) == val:
                            self.asr_provider_select.setCurrentIndex(i)
                            break
                elif key == "LLM_MODEL" and val:
                    # 先精确匹配，再按 model_id 前缀匹配
                    idx = self.llm_model.findText(val)
                    if idx < 0:
                        for i in range(self.llm_model.count()):
                            if self.llm_model.itemText(i).startswith(val):
                                idx = i
                                break
                    if idx >= 0:
                        self.llm_model.setCurrentIndex(idx)
                    else:
                        self.llm_model.setEditText(val)
                elif key == "TTS_MODEL" and val:
                    idx = self.tts_model.findText(val)
                    if idx >= 0:
                        self.tts_model.setCurrentIndex(idx)
                    else:
                        self.tts_model.setEditText(val)
                elif key == "TTS_VOICE" and val:
                    idx = self.tts_voice.findText(val)
                    if idx >= 0:
                        self.tts_voice.setCurrentIndex(idx)
                    else:
                        self.tts_voice.setEditText(val)
                elif key == "ASR_MODEL" and val:
                    idx = self.asr_model.findText(val)
                    if idx >= 0:
                        self.asr_model.setCurrentIndex(idx)
                    else:
                        self.asr_model.setEditText(val)
                # 视觉模型配置
                elif key == "VISION_PROVIDER" and val:
                    for i in range(self.vision_provider_select.count()):
                        if self.vision_provider_select.itemData(i) == val:
                            self.vision_provider_select.setCurrentIndex(i)
                            # 触发模型列表刷新，避免只保留预设硬编码列表
                            self._on_vision_provider_select(i)
                            break
                elif key == "VISION_BASE_URL" and val:
                    self.vision_url.setText(val)
                elif key == "VISION_API_KEY" and val:
                    self.vision_key.setText(val)
                elif key == "VISION_MODEL" and val:
                    idx = self.vision_model.findText(val)
                    if idx >= 0:
                        self.vision_model.setCurrentIndex(idx)
                    else:
                        self.vision_model.setEditText(val)
        except Exception:
            logger.debug("settings_dialog: 非致命异常(已静默吞掉)", exc_info=True)

    @staticmethod
    def _strip_provider_suffix(text: str) -> str:
        """去掉 'model_id  [provider]' 后缀，返回纯 model_id"""
        import re
        return re.sub(r"\s{2,}\[[^\]]+\]\s*$", "", text).strip()

    def _save_env(self):
        # 合并式写入：保留 .env 中未在对话框里的键（HANAKO_*/PHONE_*/OC_PET_* 等），
        # 只更新已知字段——整文件覆写会丢掉 PHONE_AUTH_TOKEN（认证降级）和
        # OC_PET_COSYVOICE_DIR（cosyvoice 回退硬编码路径）等关键配置。
        from env_config import update_env
        update_env({
            "LLM_PROVIDER": self.llm_provider_select.currentData() or "",
            "LLM_BASE_URL": self.llm_url.text().strip(),
            "LLM_API_KEY": self.llm_key.text().strip(),
            "LLM_MODEL": self._strip_provider_suffix(self.llm_model.currentText()),
            "TTS_PROVIDER": self.tts_provider_select.currentData() or "",
            "TTS_BASE_URL": self.tts_url.text().strip(),
            "TTS_API_KEY": self.tts_key.text().strip(),
            "TTS_MODEL": self.tts_model.currentText().strip(),
            "TTS_VOICE": self.tts_voice.currentText().strip(),
            "ASR_PROVIDER": self.asr_provider_select.currentData() or "",
            "ASR_BASE_URL": self.asr_url.text().strip(),
            "ASR_API_KEY": self.asr_key.text().strip(),
            "ASR_MODEL": self.asr_model.currentText().strip(),
            "VISION_PROVIDER": self.vision_provider_select.currentData() or "",
            "VISION_BASE_URL": self.vision_url.text().strip(),
            "VISION_API_KEY": self.vision_key.text().strip(),
            "VISION_MODEL": self.vision_model.currentText().strip(),
        })

    # ── 保存 ──

    def _save(self):
        c = self._config

        # 行为
        beh_idx = self.behavior.currentIndex()
        c["behavior"] = ["quiet", "normal", "active", "cling"][beh_idx]

        # 窗口
        c["opacity"] = self.opacity.value() / 100
        c["scale"] = self.scale.value() / 100
        c["mouse_interaction"] = self.mouse_interaction.isChecked()

        # TTS
        c.setdefault("tts", {})["enabled"] = self.tts_enabled.isChecked()
        c["tts"]["provider"] = ["cosyvoice", "mimo", "api", "edge", "qwen"][self.tts_provider.currentIndex()]
        c["tts"]["volume"] = self.tts_volume.value() / 100
        if hasattr(self, "tts_edge_voice"):
            c["tts"]["edge_voice"] = self.tts_edge_voice.currentText()

        # SFX
        c.setdefault("sfx", {})["enabled"] = self.sfx_enabled.isChecked()
        c["sfx"]["volume"] = self.sfx_volume.value() / 100

        # 主动对话
        c.setdefault("proactive", {})["enabled"] = self.pro_enabled.isChecked()
        c["proactive"]["cooldown_minutes"] = self.pro_cooldown.value()
        c["proactive"]["fullscreen_suppress"] = self.pro_fullscreen_suppress.isChecked()
        c["proactive"]["fullscreen_threshold"] = round(self.pro_fullscreen_threshold.value(), 2)
        
        # 2026-09-06: P2 主动对话配置（每日上限、免打扰时段）
        c["proactive"]["daily_budget"] = {
            "enabled": True,
            "daily_limit": self.proactive_daily_limit.value(),
            "period_limits": {
                "morning": 2,
                "afternoon": 2,
                "evening": 2,
                "night": 0
            }
        }
        c["proactive"]["dnd"] = {
            "enabled": True,
            "late_night_start": self.proactive_dnd_start.value(),
            "late_night_end": self.proactive_dnd_end.value()
        }

        # 屏幕感知
        c.setdefault("screen", {})["enabled"] = self.screen_enabled.isChecked()
        c["screen"]["interval"] = self.screen_interval.value()
        c["screen"]["blur"] = self.screen_blur.isChecked()
        c["screen"]["blacklist"] = self.screen_blacklist.isChecked()
        c["screen"]["compress"] = self.screen_compress.isChecked()
        # 随机截屏间隔：勾选才写范围，不勾则清掉（回退到基准±30%）
        if self.screen_rand.isChecked():
            lo, hi = self.screen_interval_min.value(), self.screen_interval_max.value()
            c["screen"]["interval_min"] = min(lo, hi)
            c["screen"]["interval_max"] = max(lo, hi)
        else:
            c["screen"].pop("interval_min", None)
            c["screen"].pop("interval_max", None)

        # 窗口互动
        c.setdefault("window_interaction", {})["enabled"] = self.wi_enabled.isChecked()
        c["window_interaction"]["auto_walk"] = self.wi_auto_walk.isChecked()
        c["window_interaction"]["cooldown_seconds"] = self.wi_cooldown.value()

        # 久坐提醒
        c.setdefault("break_reminder", {})["enabled"] = self.break_enabled.isChecked()
        c["break_reminder"]["idle_minutes"] = self.break_idle.value()
        c["break_reminder"]["cooldown_minutes"] = self.break_cooldown.value()

        # 工作提醒
        c.setdefault("work_reminder", {})["enabled"] = self.work_enabled.isChecked()
        c["work_reminder"]["after_minutes"] = self.work_after.value()
        c["work_reminder"]["late_night_multiplier"] = self.work_late_night_multiplier.value()
        c["work_reminder"]["cooldown_minutes"] = self.work_cooldown.value()
        c["work_reminder"]["snooze_minutes"] = self.work_snooze.value()
        c["work_reminder"]["tts_enabled"] = self.work_tts.isChecked()

        # ASR
        c.setdefault("asr", {})["provider"] = ["whisper_local", "mimo", "api"][self.asr_provider.currentIndex()]
        if hasattr(self, "asr_backend"):
            c["asr"]["backend"] = ["whisper", "faster_whisper"][self.asr_backend.currentIndex()]
        if hasattr(self, "asr_lang"):
            c["asr"]["language"] = ["zh", "auto"][self.asr_lang.currentIndex()]
        # 麦克风设备（2026-08-22 新增）
        if hasattr(self, "asr_device"):
            c["asr"]["device"] = self.asr_device.currentData() or ""

        # 记忆注入
        c.setdefault("memory", {})["budget_mode"] = "auto" if self.mem_mode.currentIndex() == 0 else "manual"
        c["memory"]["budget_chars"] = self.mem_budget.value()
        
        # 2026-09-07: 会话记忆开关
        if hasattr(self, "sess_memory_enabled"):
            c["session_memory_enabled"] = self.sess_memory_enabled.isChecked()

        # 快捷键
        c.setdefault("shortcuts", {})["enabled"] = self.shortcuts_enabled.isChecked()
        shortcuts_keys = c["shortcuts"].setdefault("keys", {})
        for key_input, action_id in self._shortcuts_list:
            shortcuts_keys[action_id] = key_input.text()

        # API .env
        self._save_env()

        # 渲染格式切换
        if hasattr(self, 'render_format_select'):
            fmt_data = self.render_format_select.currentData()
            if fmt_data and fmt_data != "auto":
                c["render_format"] = fmt_data
            else:
                c.pop("render_format", None)

        # 桌宠独立配置（per-pet）：写回 agents[].tts / agents[].dialog.agent_id
        if getattr(self, '_per_pet_rows', None):
            # 用 currentText 解析，不依赖索引顺序
            eng_text_map = {
                "沿用全局": "",
                "CosyVoice 本地": "cosyvoice",
                "微软 Edge (免费)": "edge",
                "MIMO TTS": "mimo",
                "API 调用": "api",
                "Qwen3-TTS 本地": "qwen",
            }
            agents_out = c.setdefault("agents", [])
            for aid, (eng_cb, voice_cb, agent_cb) in self._per_pet_rows.items():
                ac = next((a for a in agents_out if a.get("id") == aid), None)
                if ac is None:
                    ac = {"id": aid, "enabled": False}
                    agents_out.append(ac)
                eng = eng_text_map.get(eng_cb.currentText(), "")
                # 解析音色："edge|xxx" 或 "cosy|xxx"；空则移除该字段
                vtxt = voice_cb.currentText()
                vtag, _, vname = vtxt.partition("|")
                if not eng:
                    # 沿用全局：清掉 agent 级 tts（仅移除我们管理的键，保留其他）
                    if isinstance(ac.get("tts"), dict):
                        ac["tts"].pop("provider", None)
                        ac["tts"].pop("voice", None)
                        ac["tts"].pop("edge_voice", None)
                        if not ac["tts"]:
                            ac.pop("tts", None)
                else:
                    ac.setdefault("tts", {})["provider"] = eng
                    if eng == "edge":
                        if vtag == "edge" and vname:
                            ac["tts"]["edge_voice"] = vname
                        else:
                            if isinstance(ac["tts"], dict):
                                ac["tts"].pop("edge_voice", None)
                        ac["tts"].pop("voice", None) if isinstance(ac["tts"], dict) else None
                    else:
                        # cosy / qwen 等通过 voice_profile 映射到音色的引擎，统一走 voice 字段
                        if vtag in ("cosy", "qwen") and vname:
                            ac["tts"]["voice"] = vname
                        else:
                            # 音色选"沿用全局"或非标准格式，清掉 voice
                            if isinstance(ac["tts"], dict):
                                ac["tts"].pop("voice", None)
                        ac["tts"].pop("edge_voice", None)
                # 助手绑定：空 = 沿用全局
                selected_agent = agent_cb.currentData()
                if selected_agent:
                    ac.setdefault("dialog", {})["agent_id"] = selected_agent
                else:
                    if isinstance(ac.get("dialog"), dict):
                        ac["dialog"].pop("agent_id", None)
                        if not ac["dialog"]:
                            ac.pop("dialog", None)

        # Minecraft（需求② P4）
        if hasattr(self, "mc_enabled"):
            mc = c.setdefault("mc", {})
            mc["enabled"] = self.mc_enabled.isChecked()
            mc["transport"] = "ws" if self.mc_transport.currentIndex() == 1 else "http"
            mc["http_url"] = self.mc_http_url.text().strip()
            mc["ws_url"] = self.mc_ws_url.text().strip()
            mc["token"] = self.mc_token.text().strip()
            mc["timeout"] = self.mc_timeout.value()
            mc["http_timeout_ms"] = self.mc_http_timeout.value()
            gr = mc.setdefault("guardrails", {})
            gr["allow_remote"] = self.mc_allow_remote.isChecked()
            gr["require_token"] = self.mc_require_token.isChecked()
            gr["block_destructive"] = self.mc_block_destructive.isChecked()
            gr["allowed_methods"] = [
                x.strip() for x in self.mc_allowed_methods.text().replace("，", ",").split(",") if x.strip()
            ]

        # Skyrim（需求⑤：MCP 桥接）
        if hasattr(self, "skyrim_enabled"):
            sc = c.setdefault("skyrim", {})
            sc["enabled"] = self.skyrim_enabled.isChecked()
            sc["server_type"] = "skylink" if self.skyrim_type.currentIndex() == 0 else "skyrimnet"
            sc["skylink_dll"] = self.skyrim_dll.text().strip()
            sc["dotnet_path"] = self.skyrim_dotnet.text().strip() or "dotnet"
            sc["skynet_url"] = self.skyrim_url.text().strip() or "http://localhost:8889/sse"
            sc["skynet_transport"] = "streamable_http" if self.skyrim_transport.currentIndex() == 1 else "sse"
            sc["allow_remote"] = self.skyrim_allow_remote.isChecked()
            try:
                sc["timeout"] = int(self.skyrim_timeout.value())
            except Exception:  # noqa: BLE001
                sc["timeout"] = 30

        # QQ/微信（需求③：只读桥接）
        if hasattr(self, "hb_enabled"):
            hbc = c.setdefault("hanako_bridge", {})
            hbc["enabled"] = self.hb_enabled.isChecked()
            hbc["agent_id"] = self.hb_agent_id.currentText().strip()
            hbc["platforms"] = [p for p, cb in (("qq", self.hb_qq), ("wechat", self.hb_wechat))
                                if cb.isChecked()]
            hbc["owner_only"] = self.hb_owner_only.isChecked()
            hbc["max_per_hour"] = self.hb_max_per_hour.value()
            hbc["poll_interval"] = self.hb_poll_interval.value()
            hbc["localhost_only"] = self.hb_localhost_only.isChecked()

        # 落盘：将内存改动持久化到 config.json（原子写），否则关闭后配置丢失。
        save_config(self._config)

        self.accept()

    # ── MCP 父分类标签页（Minecraft + Skyrim 两个 MCP 游戏桥接的子标签）──
    def _build_mcp_tab(self) -> QWidget:
        """🔗 MCP 页：把 Minecraft / Skyrim 两个 MCP 游戏桥接收进同一个分类下。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)

        self.mcp_sub_tabs = QTabWidget()
        self.mcp_sub_tabs.setStyleSheet(self._tab_qss())
        self.mcp_sub_tabs.addTab(self._build_mc_tab(), "🎮 Minecraft")
        self.mcp_sub_tabs.addTab(self._build_skyrim_tab(), "⚔️ Skyrim")
        layout.addWidget(self.mcp_sub_tabs)
        return page

    # ── Minecraft 标签页（需求② P4：开关 / transport / HTTP 护栏）──

    def _build_mc_tab(self) -> QWidget:
        """🎮 Minecraft 页：启用开关、transport、连接参数、安全护栏。"""
        mc = self._config.get("mc") or {}
        g = mc.get("guardrails") or {}
        muted = "color: rgb(%s); font-size: 11px;" % rgb(self._ui_theme, "text_muted")

        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(14)

        hint = QLabel(
            "接入 Minecraft：http 方式让桌宠调用具体游戏方法，ws 方式让桌宠派任务和看 bot 玩。\n"
            "默认关闭。两种方式都需要你先运行对应的外部桥接程序，保存后立即生效。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(muted)
        lay.addWidget(hint)

        # ── 连接 ──
        conn = QGroupBox("连接")
        form = QFormLayout(conn)

        self.mc_enabled = QCheckBox("启用 Minecraft 接入")
        self.mc_enabled.setChecked(bool(mc.get("enabled", False)))
        form.addRow(self.mc_enabled)

        self.mc_transport = QComboBox()
        self.mc_transport.addItems([
            "http — minecraft-mcp（JSON-RPC 方法桥，需令牌）",
            "ws — mc-agent-neko（派高层任务 + 实时画面）",
        ])
        self.mc_transport.setCurrentIndex(1 if str(mc.get("transport", "http")).lower() == "ws" else 0)
        form.addRow("方式", self.mc_transport)

        self.mc_http_url = QLineEdit(str(mc.get("http_url", "http://127.0.0.1:8765")))
        self.mc_http_url.setPlaceholderText("http://127.0.0.1:8765")
        form.addRow("HTTP 地址", self.mc_http_url)

        self.mc_ws_url = QLineEdit(str(mc.get("ws_url", "ws://127.0.0.1:48909")))
        self.mc_ws_url.setPlaceholderText("ws://127.0.0.1:48909")
        form.addRow("WS 地址", self.mc_ws_url)

        self.mc_token = QLineEdit(str(mc.get("token", "") or ""))
        self.mc_token.setEchoMode(QLineEdit.Password)
        self.mc_token.setPlaceholderText("minecraft-mcp 首次启动生成的令牌")
        form.addRow("令牌 (token)", self.mc_token)

        tok_hint = QLabel("令牌存在 config.json（该文件已被 .gitignore 忽略，不会入库）；http 方式必需。")
        tok_hint.setWordWrap(True)
        tok_hint.setStyleSheet(muted)
        form.addRow("", tok_hint)

        self.mc_timeout = QSpinBox()
        self.mc_timeout.setRange(10, 3600)
        self.mc_timeout.setValue(int(mc.get("timeout", 120)))
        self.mc_timeout.setSuffix(" 秒")
        form.addRow("任务最长等待", self.mc_timeout)

        self.mc_http_timeout = QSpinBox()
        self.mc_http_timeout.setRange(1000, 60000)
        self.mc_http_timeout.setSingleStep(1000)
        self.mc_http_timeout.setValue(int(mc.get("http_timeout_ms", 10000)))
        self.mc_http_timeout.setSuffix(" 毫秒")
        form.addRow("单次调用超时", self.mc_http_timeout)

        lay.addWidget(conn)

        # ── 护栏 ──
        guard = QGroupBox("安全护栏（HTTP 方式）")
        gform = QFormLayout(guard)

        self.mc_allow_remote = QCheckBox("允许连接非本机地址")
        self.mc_allow_remote.setChecked(bool(g.get("allow_remote", False)))
        gform.addRow(self.mc_allow_remote)

        self.mc_require_token = QCheckBox("必须配置令牌才允许调用")
        self.mc_require_token.setChecked(bool(g.get("require_token", True)))
        gform.addRow(self.mc_require_token)

        self.mc_block_destructive = QCheckBox("拦截高危方法（op / ban / give / fill / summon / execute…）")
        self.mc_block_destructive.setChecked(bool(g.get("block_destructive", True)))
        gform.addRow(self.mc_block_destructive)

        am = g.get("allowed_methods") or []
        am_text = ", ".join(str(x) for x in am) if isinstance(am, (list, tuple)) else str(am)
        self.mc_allowed_methods = QLineEdit(am_text)
        self.mc_allowed_methods.setPlaceholderText(
            "留空 = 放行除高危外的全部方法；例：getInventory, getPosition, setBlock"
        )
        gform.addRow("方法白名单", self.mc_allowed_methods)

        g_hint = QLabel("默认只连本机、必须有令牌、拦截高危方法——三项都是刻意收紧的，放宽前先想清楚后果。")
        g_hint.setWordWrap(True)
        g_hint.setStyleSheet(muted)
        gform.addRow("", g_hint)

        lay.addWidget(guard)

        # ── 自检 ──
        test_row = QHBoxLayout()
        test_row.addStretch()
        self.mc_test_btn = QPushButton("测试连接")
        self.mc_test_btn.clicked.connect(self._mc_test_connection)
        test_row.addWidget(self.mc_test_btn)
        lay.addLayout(test_row)
        lay.addStretch()
        return tab

    def _mc_test_connection(self):
        """就地探一下 HTTP 桥是否可达（用界面当前值，不落盘）。"""
        if self.mc_transport.currentIndex() == 1:
            QMessageBox.information(
                self, "测试连接",
                "ws 方式没有健康检查接口。\n\n"
                "确认 mc-agent-neko 已运行后，保存重启，再让桌宠派一个任务即可验证是否连通。",
            )
            return

        url = (self.mc_http_url.text() or "").strip().rstrip("/")
        token = (self.mc_token.text() or "").strip()

        if not self.mc_allow_remote.isChecked():
            try:
                from core.mc_bridge import _is_loopback_host
                if not _is_loopback_host(url):
                    QMessageBox.critical(
                        self, "测试连接",
                        "护栏拦截：地址不是本机，而「允许连接非本机地址」未勾选。\n\n"
                        "这是默认行为，防止误连远程机器或把令牌发出去。",
                    )
                    return
            except Exception:  # noqa: BLE001
                logger.debug("settings_dialog: 非致命异常(已静默吞掉)", exc_info=True)

        if self.mc_require_token.isChecked() and not token:
            QMessageBox.critical(self, "测试连接", "缺少令牌，而「必须配置令牌才允许调用」已勾选。")
            return

        try:
            import requests
            r = requests.get(f"{url}/health", timeout=3)
            if r.status_code == 200:
                QMessageBox.information(self, "测试连接", f"已连通：{url}\n\n{r.text[:200]}")
            else:
                QMessageBox.warning(self, "测试连接", f"已连上但返回异常：HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(
                self, "测试连接",
                f"连接失败：{e}\n\n请先在游戏里运行 minecraft-mcp，再回来试。",
            )

    # ── Skyrim 标签页（需求⑤：MCP 桥接整合 SkyLink AI / SkyrimNet）──

    def _build_skyrim_tab(self) -> QWidget:
        """⚔️ Skyrim 页：选服务类型（SkyLink AI / SkyrimNet）、连接参数、测试连接。"""
        sky = self._config.get("skyrim") or {}
        muted = "color: rgb(%s); font-size: 11px;" % rgb(self._ui_theme, "text_muted")

        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(14)

        hint = QLabel(
            "接入 Skyrim 的 MCP server：SkyLink AI（74 工具，stdio）或 SkyrimNet（56 工具，HTTP@8889）。\n"
            "启用后桌宠能「列出工具 / 调用工具」读写游戏状态。保存后立即生效。\n"
            "前提：游戏里装好对应 SKSE 插件并运行（SkyLink 还需 .NET 10 Runtime）。\n"
            "实测：SkyrimNet 的 MCP 走 SSE 传输，端点要用 http://localhost:8889/sse；\n"
            "只填 host:port 也能连（桌宠会自动补 /sse，并在 localhost / 127.0.0.1 间自动重试）。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(muted)
        lay.addWidget(hint)

        # ── 连接 ──
        conn = QGroupBox("连接")
        form = QFormLayout(conn)

        self.skyrim_enabled = QCheckBox("启用 Skyrim MCP 接入")
        self.skyrim_enabled.setChecked(bool(sky.get("enabled", False)))
        form.addRow(self.skyrim_enabled)

        self.skyrim_type = QComboBox()
        self.skyrim_type.addItems([
            "SkyLink AI — stdio（dotnet SkyrimMCP.dll，需 .NET 10）",
            "SkyrimNet — HTTP（游戏内 SKSE 插件 @ localhost:8889）",
        ])
        self.skyrim_type.setCurrentIndex(
            0 if str(sky.get("server_type", "skyrimnet")).lower() == "skylink" else 1
        )
        form.addRow("服务类型", self.skyrim_type)

        self.skyrim_dll = QLineEdit(str(sky.get("skylink_dll", "") or ""))
        self.skyrim_dll.setPlaceholderText(r"D:\Games\Skyrim\Data\SKSE\Plugins\SkyLinkAI_Server\SkyrimMCP.dll")
        form.addRow("SkyrimMCP.dll 路径", self.skyrim_dll)

        self.skyrim_dotnet = QLineEdit(str(sky.get("dotnet_path", "dotnet") or "dotnet"))
        self.skyrim_dotnet.setPlaceholderText("dotnet（需 .NET 10 Runtime）")
        form.addRow("dotnet 路径", self.skyrim_dotnet)

        self.skyrim_url = QLineEdit(
            str(sky.get("skynet_url", "http://localhost:8889/sse") or "http://localhost:8889/sse")
        )
        self.skyrim_url.setPlaceholderText("http://localhost:8889/sse")
        form.addRow("SkyrimNet 地址", self.skyrim_url)

        self.skyrim_transport = QComboBox()
        self.skyrim_transport.addItems([
            "sse — 旧式 SSE 传输（多数 MCP over HTTP 用这个）",
            "streamable_http — 2024-11 新标准 Streamable HTTP",
        ])
        self.skyrim_transport.setCurrentIndex(
            1 if str(sky.get("skynet_transport", "sse")).lower() == "streamable_http" else 0
        )
        form.addRow("传输方式", self.skyrim_transport)

        self.skyrim_allow_remote = QCheckBox("允许连接非本机地址")
        self.skyrim_allow_remote.setChecked(bool(sky.get("allow_remote", False)))
        form.addRow(self.skyrim_allow_remote)

        self.skyrim_timeout = QSpinBox()
        self.skyrim_timeout.setRange(5, 300)
        self.skyrim_timeout.setValue(int(sky.get("timeout", 30)))
        self.skyrim_timeout.setSuffix(" 秒")
        form.addRow("连接/调用超时", self.skyrim_timeout)

        lay.addWidget(conn)

        g_hint = QLabel(
            "默认只连本机（防误连远程）。SkyLink 的 stdio 子进程由桌宠拉起；"
            "SkyrimNet 的 8889 由游戏内插件提供，桌宠只连不启。"
        )
        g_hint.setWordWrap(True)
        g_hint.setStyleSheet(muted)
        lay.addWidget(g_hint)

        # ── 自检 ──
        test_row = QHBoxLayout()
        test_row.addStretch()
        self.skyrim_test_btn = QPushButton("测试连接")
        self.skyrim_test_btn.clicked.connect(self._skyrim_test_connection)
        test_row.addWidget(self.skyrim_test_btn)
        lay.addLayout(test_row)
        lay.addStretch()
        return tab

    def _skyrim_test_connection(self):
        """就地探一下 MCP server 是否可达（用界面当前值，不落盘）。"""
        try:
            from core.skyrim_bridge import SkyrimBridge
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "测试连接", f"导入 skyrim_bridge 失败：{e}")
            return

        cfg = {
            "server_type": "skylink" if self.skyrim_type.currentIndex() == 0 else "skyrimnet",
            "skylink_dll": self.skyrim_dll.text().strip(),
            "dotnet_path": self.skyrim_dotnet.text().strip() or "dotnet",
            "skynet_url": self.skyrim_url.text().strip() or "http://localhost:8889/sse",
            "skynet_transport": "streamable_http" if self.skyrim_transport.currentIndex() == 1 else "sse",
            "allow_remote": self.skyrim_allow_remote.isChecked(),
            "timeout": max(5, int(self.skyrim_timeout.value())),
        }
        # 护栏先本地校验，省得 spawn 子进程/连网
        if cfg["server_type"] == "skyrimnet" and not cfg["allow_remote"]:
            try:
                from core.skyrim_bridge import _is_loopback_host
                if not _is_loopback_host(cfg["skynet_url"]):
                    QMessageBox.critical(
                        self, "测试连接",
                        "护栏拦截：地址不是本机，而「允许连接非本机地址」未勾选。\n"
                        "这是默认行为，防止误连远程机器。",
                    )
                    return
            except Exception:  # noqa: BLE001
                logger.debug("settings_dialog: 非致命异常(已静默吞掉)", exc_info=True)

        bridge = SkyrimBridge(cfg)
        res = bridge.connect()
        bridge.close()
        if res.ok:
            tools = (res.value or {}).get("tools", []) if isinstance(res.value, dict) else []
            QMessageBox.information(
                self, "测试连接",
                f"已连通（{cfg['server_type']}）！\n可用工具 {len(tools)} 个。",
            )
        else:
            QMessageBox.critical(self, "测试连接", f"连接失败：\n{res.error}")

    # ── QQ/微信标签页（需求③：Hanako 只读桥接）──

    @staticmethod
    def _list_hanako_agents() -> list:
        """列出 ~/.hanako/agents/ 下的 agent 名（本地目录读取，安全）。"""
        try:
            import os
            base = os.path.join(os.path.expanduser("~"), ".hanako", "agents")
            return sorted(d for d in os.listdir(base)
                          if os.path.isdir(os.path.join(base, d)))
        except Exception:  # noqa: BLE001
            return []

    def _build_hb_tab(self) -> QWidget:
        """💬 QQ/微信页：只读桥接的开关、目标 agent、平台过滤、节流。"""
        hb = self._config.get("hanako_bridge") or {}
        muted = "color: rgb(%s); font-size: 11px;" % rgb(self._ui_theme, "text_muted")

        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(14)

        hint = QLabel(
            "订阅 Hanako 已接入的 QQ / 微信消息：来消息时桌宠会提醒你。\n"
            "⚠️ 只读——Hanako 没有对外开放给第三方的纯文本发送口，所以本功能刻意不能代你回复，"
            "回复仍请在 Hanako 侧进行。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(muted)
        lay.addWidget(hint)

        conn = QGroupBox("连接")
        form = QFormLayout(conn)

        self.hb_enabled = QCheckBox("启用 QQ/微信 消息提醒")
        self.hb_enabled.setChecked(bool(hb.get("enabled", False)))
        form.addRow(self.hb_enabled)

        self.hb_agent_id = QComboBox()
        self.hb_agent_id.setEditable(True)
        agents = self._list_hanako_agents()
        if agents:
            self.hb_agent_id.addItems(agents)
        cur = str(hb.get("agent_id", "") or "")
        if cur:
            if cur not in agents:
                self.hb_agent_id.addItem(cur)
            self.hb_agent_id.setCurrentText(cur)
        self.hb_agent_id.setPlaceholderText("必填：选一个 agent（如 ophelia）")
        form.addRow("目标 agent", self.hb_agent_id)

        agent_hint = QLabel("必须显式指定：不会自动替你猜，避免读到别人的会话。"
                            "该 agent 需要在 Hanako 里已经开好 QQ / 微信接入。")
        agent_hint.setWordWrap(True)
        agent_hint.setStyleSheet(muted)
        form.addRow("", agent_hint)

        lay.addWidget(conn)

        scope = QGroupBox("范围")
        sform = QFormLayout(scope)

        plat_row = QHBoxLayout()
        plats = [str(x).lower() for x in (hb.get("platforms") or ["qq", "wechat"])]
        self.hb_qq = QCheckBox("QQ")
        self.hb_qq.setChecked("qq" in plats)
        self.hb_wechat = QCheckBox("微信")
        self.hb_wechat.setChecked("wechat" in plats)
        plat_row.addWidget(self.hb_qq)
        plat_row.addWidget(self.hb_wechat)
        plat_row.addStretch()
        sform.addRow("平台", plat_row)

        self.hb_owner_only = QCheckBox("只看你自己的会话（建议保持勾选）")
        self.hb_owner_only.setChecked(bool(hb.get("owner_only", True)))
        sform.addRow(self.hb_owner_only)

        self.hb_max_per_hour = QSpinBox()
        self.hb_max_per_hour.setRange(1, 60)
        self.hb_max_per_hour.setValue(int(hb.get("max_per_hour", 10)))
        self.hb_max_per_hour.setSuffix(" 次/小时")
        sform.addRow("提醒上限", self.hb_max_per_hour)

        self.hb_poll_interval = QSpinBox()
        self.hb_poll_interval.setRange(10, 600)
        self.hb_poll_interval.setSingleStep(10)
        self.hb_poll_interval.setValue(int(hb.get("poll_interval", 30)))
        self.hb_poll_interval.setSuffix(" 秒")
        sform.addRow("轮询间隔", self.hb_poll_interval)

        self.hb_localhost_only = QCheckBox("只允许连本机 127.0.0.1（建议保持勾选）")
        self.hb_localhost_only.setChecked(bool(hb.get("localhost_only", True)))
        sform.addRow(self.hb_localhost_only)

        s_hint = QLabel("Hanako server 默认监听 0.0.0.0 且未开 TLS；强制走本机可以避免把令牌发到内网。"
                        "取消勾选将使用 server 广告的内网地址，风险自负。")
        s_hint.setWordWrap(True)
        s_hint.setStyleSheet(muted)
        sform.addRow("", s_hint)

        lay.addWidget(scope)

        test_row = QHBoxLayout()
        test_row.addStretch()
        self.hb_test_btn = QPushButton("测试连接")
        self.hb_test_btn.clicked.connect(self._hb_test)
        test_row.addWidget(self.hb_test_btn)
        lay.addLayout(test_row)
        lay.addStretch()
        return tab

    def _hb_test(self):
        """就地探一下目标 agent 的 QQ/微信接入状态（用界面当前值，不落盘）。"""
        aid = self.hb_agent_id.currentText().strip()
        if not aid:
            QMessageBox.warning(self, "测试连接", "请先选一个 agent。")
            return
        try:
            from core.hanako_bridge import BridgeClient, ServerInfoLoader
            client = BridgeClient(
                ServerInfoLoader(),
                localhost_only=self.hb_localhost_only.isChecked(),
            )
            res = client.status(aid)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "测试连接", f"加载桥接模块失败：{e}")
            return

        if not res:
            QMessageBox.critical(
                self, "测试连接",
                f"连不上 Hanako server：{res.error}\n\n请确认 Hanako 桌面端正在运行。",
            )
            return

        st = res.value or {}
        lines = [f"agent: {st.get('agentId', aid)}"]
        for key, label in (("qq", "QQ"), ("wechat", "微信")):
            d = st.get(key) or {}
            if not d.get("configured"):
                state = "未配置"
            elif not d.get("enabled"):
                state = "已配置但未启用"
            else:
                state = {"connected": "已连接"}.get(d.get("status"), str(d.get("status")))
            lines.append(f"{label}: {state}")
        lines.append(f"bridgeReady: {st.get('bridgeReady')}")
        QMessageBox.information(self, "测试连接", "\n".join(lines))

    def _filter_settings(self, text: str):
        """UI优化: 根据搜索文本过滤设置项（隐藏不匹配的标签页）"""
        text = text.lower().strip()
        
        # 标签页名称映射
        tab_keywords = {
            0: ["基础", "behavior", "window", "sfx", "render", "per_pet"],
            1: ["功能", "voice", "tts", "asr", "interaction", "memory", "active", "screen", "break"],
            2: ["快捷键", "shortcut", "hotkey", "keys"],
            3: ["角色包", "package", "pkg", "m5"],
            4: ["主动对话", "proactive", "cooldown", "dnd"],
            5: ["api", "llm", "tts", "asr", "endpoint"],
            6: ["mcp", "minecraft", "mc", "我的世界", "skyrim", "老滚", "天际",
                "上古卷轴", "bot", "bridge", "token", "护栏", "skylink", "skyrimnet"],
            7: ["qq", "微信", "wechat", "消息提醒", "hanako", "桥接", "agent"],
        }
        
        for i in range(self._main_tabs.count()):
            tab_text = self._main_tabs.tabText(i).lower()
            keywords = tab_keywords.get(i, [])
            
            # 检查标签页名称或关键词是否匹配
            if text == "" or text in tab_text or any(text in kw for kw in keywords):
                self._main_tabs.setTabVisible(i, True)
            else:
                self._main_tabs.setTabVisible(i, False)
    
    def get_config(self) -> dict:
        return self._config

    # ── 主题化（颜色统一来自 ui/theme/palette，随主题切换刷新）──

    def _tab_qss(self, theme=None):
        t = theme or getattr(self, "_ui_theme", "dark")
        return f"""
            QTabWidget::pane {{ border: 1px solid rgb({rgb(t, 'panel_border')}); background: rgba({rgba(t, 'panel_bg')}); }}
            QTabBar::tab {{ background: rgba({rgba(t, 'tab_bg')}); color: rgb({rgb(t, 'text_secondary')}); padding: 6px 12px; }}
            QTabBar::tab:selected {{ background: rgba({rgba(t, 'panel_bg')}); color: rgb({rgb(t, 'text_primary')}); border-bottom: 2px solid rgb({rgb(t, 'btn_primary')}); }}
        """

    def _on_dialog_theme_changed(self, theme: str):
        self._ui_theme = theme
        if hasattr(self, "_main_tabs"):
            self._main_tabs.setStyleSheet(self._tab_qss(theme))
        if hasattr(self, "func_sub_tabs"):
            self.func_sub_tabs.setStyleSheet(self._tab_qss(theme))
        if hasattr(self, "mcp_sub_tabs"):
            self.mcp_sub_tabs.setStyleSheet(self._tab_qss(theme))
        if hasattr(self, "mem_hint"):
            self.mem_hint.setStyleSheet("color: rgb(%s); font-size: 10px;" % rgb(theme, "text_muted"))
        if hasattr(self, "_pkg_status_label"):
            self._pkg_status_label.setStyleSheet("color: rgb(%s); font-size: 10px;" % rgb(theme, "text_muted"))
        if hasattr(self, "_vision_separator"):
            self._vision_separator.setStyleSheet("color: rgb(%s); font-weight: bold; margin-top: 10px;" % rgb(theme, "text_muted"))
        if hasattr(self, "_vision_hint"):
            self._vision_hint.setStyleSheet("color: rgb(%s); font-size: 11px;" % rgb(theme, "text_muted"))
