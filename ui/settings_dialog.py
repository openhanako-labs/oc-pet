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
    QCheckBox, QSlider, QSpinBox, QComboBox,
    QPushButton, QLabel, QGroupBox, QTabWidget, QWidget,
    QLineEdit, QListWidget, QListWidgetItem, QAbstractItemView,
    QMessageBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from config import load_config


STYLE = """
QDialog { background: #f7f6f3; color: #2c2c2c; }
QTabWidget::pane { border: 1px solid #e5e2db; background: #ffffff; }
QTabBar::tab {
    background: #f7f6f3; color: #7a7a7a; border: 1px solid #e5e2db;
    padding: 8px 16px; margin-right: 2px; border-top-left-radius: 4px; border-top-right-radius: 4px;
}
QTabBar::tab:selected { background: #ffffff; color: #2c2c2c; border-bottom: 2px solid #4a90d9; }
QTabBar::tab:hover { background: #ffffff; color: #2c2c2c; }
QGroupBox {
    border: 1px solid #e5e2db; border-radius: 8px;
    margin-top: 16px; padding-top: 20px;
    color: #7a7a7a; font-weight: bold;
    background: #ffffff;
}
QGroupBox::title { left: 12px; padding: 0 6px; }
QLabel { color: #2c2c2c; }
QCheckBox { color: #2c2c2c; }
QComboBox {
    background: #ffffff; color: #2c2c2c;
    border: 1px solid #e5e2db; border-radius: 4px; padding: 4px 8px;
    min-height: 22px;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 20px;
    border: none;
}
QComboBox::down-arrow {
    image: none;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid #7a7a7a;
    margin-right: 6px;
}
QComboBox QAbstractItemView {
    background: #ffffff; color: #2c2c2c; selection-background-color: #e8f0fc;
    border: 1px solid #e5e2db;
}
QSlider::groove:horizontal { height: 4px; background: #e5e2db; border-radius: 2px; }
QSlider::handle:horizontal {
    background: #4a90d9; width: 14px; height: 14px;
    margin: -5px 0; border-radius: 7px;
}
QPushButton {
    background: #4a90d9; color: #ffffff; border: none;
    border-radius: 4px; padding: 8px 24px; font-size: 13px;
}
QPushButton:hover { background: #5fa0e9; }
QPushButton#save { background: #34c759; }
QPushButton#save:hover { background: #44d769; }
QPushButton#danger { background: #ff3b30; }
QPushButton#danger:hover { background: #ff4b40; }
QListWidget {
    background: #ffffff; color: #2c2c2c;
    border: 1px solid #e5e2db; border-radius: 4px;
}
QListWidget::item { padding: 4px 8px; }
QListWidget::item:selected { background: #e8f0fc; }
"""


class SettingsDialog(QDialog):
    """配置面板"""

    def __init__(self, config: dict = None, pet_manager=None, parent=None):
        super().__init__(parent)
        self._config = config or load_config()
        self._pet_manager = pet_manager
        self.setWindowTitle("设置")
        self.setMinimumSize(460, 600)
        self.setStyleSheet(STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        tabs = QTabWidget()
        layout.addWidget(tabs)

        # ── Tab 1: 基础设置 ──
        basic_tab = QWidget()
        basic_layout = QVBoxLayout(basic_tab)
        basic_layout.setContentsMargins(8, 8, 8, 8)
        basic_layout.setSpacing(12)

        # Agent 管理
        if pet_manager:
            agent_group = QGroupBox("桌宠管理")
            agent_layout = QVBoxLayout(agent_group)

            self._agent_list = QListWidget()
            self._agent_list.setMinimumHeight(120)
            self._refresh_agent_list()
            agent_layout.addWidget(self._agent_list)

            agent_btns = QHBoxLayout()
            self._add_agent_btn = QPushButton("+ 添加")
            self._add_agent_btn.clicked.connect(self._add_agent)
            agent_btns.addWidget(self._add_agent_btn)

            self._remove_agent_btn = QPushButton("- 移除")
            self._remove_agent_btn.setObjectName("danger")
            self._remove_agent_btn.clicked.connect(self._remove_agent)
            agent_btns.addWidget(self._remove_agent_btn)

            self._toggle_agent_btn = QPushButton("启用/禁用")
            self._toggle_agent_btn.clicked.connect(self._toggle_agent)
            agent_btns.addWidget(self._toggle_agent_btn)

            agent_layout.addLayout(agent_btns)
            
            # ── 角色包选择（自由搭配）──
            pkg_select_layout = QHBoxLayout()
            pkg_select_layout.addWidget(QLabel("角色包:"))
            
            self._pkg_select = QComboBox()
            self._pkg_select.addItem("默认", "default")
            # 加载已安装的角色包
            try:
                from core.character_package import CharacterPackageManager
                pkg_mgr = CharacterPackageManager()
                installed = pkg_mgr.list_installed()
                for pkg in installed:
                    self._pkg_select.addItem(pkg.get("name", "未知"), pkg.get("id", ""))
            except Exception:
                pass
            
            # 设置当前选中
            current_pkg = self._config.get("character_package", "default")
            idx = self._pkg_select.findData(current_pkg)
            if idx >= 0:
                self._pkg_select.setCurrentIndex(idx)
            
            pkg_select_layout.addWidget(self._pkg_select)
            agent_layout.addLayout(pkg_select_layout)
            
            basic_layout.addWidget(agent_group)

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
        self.scale.setRange(50, 200)
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

        basic_layout.addStretch()
        tabs.addTab(basic_tab, "基础")

        # ── Tab 2: 功能设置 ──
        func_tab = QWidget()
        func_layout = QVBoxLayout(func_tab)
        func_layout.setContentsMargins(16, 16, 16, 16)
        func_layout.setSpacing(20)

        # TTS
        tts_group = QGroupBox("语音输出")
        tts_layout = QFormLayout(tts_group)

        self.tts_enabled = QCheckBox("启用 TTS 语音")
        self.tts_enabled.setChecked(self._config.get("tts", {}).get("enabled", True))
        tts_layout.addRow(self.tts_enabled)

        self.tts_provider = QComboBox()
        self.tts_provider.addItems(["本地 CosyVoice", "MIMO TTS", "API 调用"])
        tts_prov_map = {"cosyvoice": 0, "mimo": 1, "api": 2}
        self.tts_provider.setCurrentIndex(tts_prov_map.get(self._config.get("tts", {}).get("provider", "cosyvoice"), 0))
        tts_layout.addRow("TTS 引擎", self.tts_provider)

        self.tts_volume = QSlider(Qt.Horizontal)
        self.tts_volume.setRange(0, 100)
        self.tts_volume.setValue(int(self._config.get("tts", {}).get("volume", 0.8) * 100))
        self.tts_vol_label = QLabel(f"{self.tts_volume.value()}%")
        self.tts_volume.valueChanged.connect(lambda v: self.tts_vol_label.setText(f"{v}%"))
        vol_row = QHBoxLayout()
        vol_row.addWidget(self.tts_volume)
        vol_row.addWidget(self.tts_vol_label)
        tts_layout.addRow("音量", vol_row)

        func_layout.addWidget(tts_group)

        # 主动对话
        pro_group = QGroupBox("主动对话")
        pro_layout = QFormLayout(pro_group)

        self.pro_enabled = QCheckBox("启用主动搭话")
        self.pro_enabled.setChecked(self._config.get("proactive", {}).get("enabled", True))
        pro_layout.addRow(self.pro_enabled)

        self.pro_cooldown = QSpinBox()
        self.pro_cooldown.setRange(1, 120)
        self.pro_cooldown.setSuffix(" 分钟")
        self.pro_cooldown.setValue(self._config.get("proactive", {}).get("cooldown_minutes", 10))
        pro_layout.addRow("冷却时间", self.pro_cooldown)

        func_layout.addWidget(pro_group)

        # 屏幕感知
        screen_group = QGroupBox("屏幕感知")
        screen_layout = QFormLayout(screen_group)

        self.screen_enabled = QCheckBox("启用屏幕截屏分析")
        self.screen_enabled.setChecked(self._config.get("screen", {}).get("enabled", True))
        screen_layout.addRow(self.screen_enabled)

        self.screen_blur = QCheckBox("截图模糊（隐私保护）")
        self.screen_blur.setChecked(self._config.get("screen", {}).get("blur", True))
        screen_layout.addRow(self.screen_blur)

        self.screen_interval = QSpinBox()
        self.screen_interval.setRange(30, 600)
        self.screen_interval.setSuffix(" 秒")
        self.screen_interval.setValue(self._config.get("screen", {}).get("interval", 120))
        screen_layout.addRow("截屏间隔", self.screen_interval)

        func_layout.addWidget(screen_group)

        # 窗口互动
        wi_group = QGroupBox("窗口互动")
        wi_layout = QFormLayout(wi_group)

        self.wi_enabled = QCheckBox("启用窗口互动")
        self.wi_enabled.setChecked(self._config.get("window_interaction", {}).get("enabled", True))
        wi_layout.addRow(self.wi_enabled)

        self.wi_cooldown = QSpinBox()
        self.wi_cooldown.setRange(5, 300)
        self.wi_cooldown.setSuffix(" 秒")
        self.wi_cooldown.setValue(self._config.get("window_interaction", {}).get("cooldown_seconds", 30))
        wi_layout.addRow("冷却时间", self.wi_cooldown)

        func_layout.addWidget(wi_group)

        # 久坐提醒
        break_group = QGroupBox("久坐提醒")
        break_layout = QFormLayout(break_group)

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

        func_layout.addWidget(break_group)

        # ASR
        asr_group = QGroupBox("语音输入")
        asr_layout = QFormLayout(asr_group)

        self.asr_provider = QComboBox()
        self.asr_provider.addItems(["本地 Whisper", "MIMO ASR", "API 调用"])
        asr_prov_map = {"whisper_local": 0, "mimo": 1, "api": 2}
        self.asr_provider.setCurrentIndex(asr_prov_map.get(self._config.get("asr", {}).get("provider", "whisper_local"), 0))
        asr_layout.addRow("ASR 引擎", self.asr_provider)

        func_layout.addWidget(asr_group)

        # 记忆注入
        mem_group = QGroupBox("记忆注入")
        mem_layout = QFormLayout(mem_group)

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
        self.mem_hint.setStyleSheet("color: #666688; font-size: 10px;")
        mem_layout.addRow(self.mem_hint)

        func_layout.addWidget(mem_group)

        func_layout.addStretch()
        tabs.addTab(func_tab, "功能")

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

        self._import_pkg_btn = QPushButton("📦 导入 .pet")
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
        self._pkg_status_label.setStyleSheet("color: #666688; font-size: 10px;")
        pkg_group_layout.addWidget(self._pkg_status_label)

        # 刷新列表（在 _pkg_status_label 创建之后）
        self._refresh_package_list()

        pkg_layout.addWidget(pkg_group)
        pkg_layout.addStretch()
        tabs.addTab(pkg_tab, "角色包")

        # ── Tab 3: API 配置 ──
        api_tab = QWidget()
        api_layout = QVBoxLayout(api_tab)
        api_layout.setContentsMargins(8, 8, 8, 8)

        api_group = QGroupBox("API 配置（留空 = 用 Hanako 默认）")
        api_form = QFormLayout(api_group)

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
        vision_separator = QLabel("─── 视觉模型（屏幕感知专用）───")
        vision_separator.setStyleSheet("color: #888; font-weight: bold; margin-top: 10px;")
        api_form.addRow(vision_separator)

        vision_hint = QLabel("留空则使用 LLM 配置。建议使用支持图片的模型（如 agnes-2.0-flash、GPT-4V 等）")
        vision_hint.setWordWrap(True)
        vision_hint.setStyleSheet("color: #666; font-size: 11px;")
        api_form.addRow(vision_hint)

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
        tabs.addTab(api_tab, "API")

        # 加载已有 .env 值
        self._load_env_to_ui()

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

    # ── Agent 管理 ──

    def _refresh_agent_list(self):
        """刷新 agent 列表"""
        if not self._pet_manager:
            return
        self._agent_list.clear()
        for agent in self._pet_manager.agents:
            agent_id = agent["id"]
            enabled = agent.get("enabled", True)
            status = "✅" if enabled else "❌"
            has_sprites = self._pet_manager._has_sprites(agent_id)
            sprite_tag = "🎨" if has_sprites else "⬜"
            # 从 discovered 列表获取名称
            name = agent_id
            for d in self._pet_manager.discover_agents():
                if d["id"] == agent_id:
                    name = d["name"]
                    break
            self._agent_list.addItem(f"{status} {sprite_tag} {name} ({agent_id})")

    def _add_agent(self):
        """新增 agent"""
        if not self._pet_manager:
            return
        discovered = self._pet_manager.discover_agents()
        existing_ids = {a["id"] for a in self._pet_manager.agents}
        available = [d for d in discovered if d["id"] not in existing_ids]
        if not available:
            QMessageBox.information(self, "提示", "所有 agent 都已添加")
            return

        # 简单选择对话框
        from PySide6.QtWidgets import QInputDialog
        items = [f"{d['name']} ({d['id']})" for d in available]
        item, ok = QInputDialog.getItem(self, "添加桌宠", "选择 Agent:", items, 0, False)
        if ok and item:
            idx = items.index(item)
            agent_id = available[idx]["id"]
            self._pet_manager.add_agent(agent_id)
            self._refresh_agent_list()

    def _remove_agent(self):
        """移除选中的 agent"""
        if not self._pet_manager:
            return
        row = self._agent_list.currentRow()
        if row < 0:
            return
        agent = self._pet_manager.agents[row]
        reply = QMessageBox.question(
            self, "确认", f"移除 {agent['id']} 的桌宠？\n（不会删除 Hanako agent 本身）",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self._pet_manager.remove_agent(agent["id"])
            self._refresh_agent_list()

    def _toggle_agent(self):
        """切换 agent 启用状态"""
        if not self._pet_manager:
            return
        row = self._agent_list.currentRow()
        if row < 0:
            return
        agent = self._pet_manager.agents[row]
        new_state = not agent.get("enabled", True)
        self._pet_manager.set_enabled(agent["id"], new_state)
        self._refresh_agent_list()

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
                item = QListWidgetItem(display_text)
                item.setData(Qt.UserRole, pkg.agent_id)  # 存储 agent_id
                self._pkg_list.addItem(item)
            self._pkg_status_label.setText(f"共 {len(packages)} 个已安装角色")
        except Exception as e:
            self._pkg_status_label.setText(f"加载失败: {e}")

    def _import_package(self):
        """导入 .pet 文件"""
        if not self._pkg_mgr:
            QMessageBox.warning(self, "提示", "角色包管理器不可用")
            return

        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "导入角色包", "",
            "角色包 (*.pet);;所有文件 (*)"
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
        
        # 保存到配置
        self._config["character"] = agent_id
        self._pkg_status_label.setText(f"已切换到: {agent_id}")
        QMessageBox.information(self, "切换成功", f"桌宠已切换为 '{agent_id}'，重启后生效")

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
            pass
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
            pass
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
            pass
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
            pass
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
            pass
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
            pass

    @staticmethod
    def _strip_provider_suffix(text: str) -> str:
        """去掉 'model_id  [provider]' 后缀，返回纯 model_id"""
        import re
        return re.sub(r"\s{2,}\[[^\]]+\]\s*$", "", text).strip()

    def _save_env(self):
        from env_config import ENV_PATH
        lines = [
            "# OC Desktop Pet - API 配置",
            "# 留空则回退到 Hanako 的默认配置",
            "",
            "# LLM",
            f"LLM_PROVIDER={self.llm_provider_select.currentData() or ''}",
            f"LLM_BASE_URL={self.llm_url.text().strip()}",
            f"LLM_API_KEY={self.llm_key.text().strip()}",
            f"LLM_MODEL={self._strip_provider_suffix(self.llm_model.currentText())}",
            "",
            "# TTS API",
            f"TTS_PROVIDER={self.tts_provider_select.currentData() or ''}",
            f"TTS_BASE_URL={self.tts_url.text().strip()}",
            f"TTS_API_KEY={self.tts_key.text().strip()}",
            f"TTS_MODEL={self.tts_model.currentText().strip() or 'tts-1'}",
            f"TTS_VOICE={self.tts_voice.currentText().strip() or 'mimo_default'}",
            "",
            "# ASR API",
            f"ASR_PROVIDER={self.asr_provider_select.currentData() or ''}",
            f"ASR_BASE_URL={self.asr_url.text().strip()}",
            f"ASR_API_KEY={self.asr_key.text().strip()}",
            f"ASR_MODEL={self.asr_model.currentText().strip() or 'whisper-1'}",
            "",
            "# Vision API（屏幕感知专用，留空用 LLM 配置）",
            f"VISION_PROVIDER={self.vision_provider_select.currentData() or ''}",
            f"VISION_BASE_URL={self.vision_url.text().strip()}",
            f"VISION_API_KEY={self.vision_key.text().strip()}",
            f"VISION_MODEL={self.vision_model.currentText().strip()}",
        ]
        ENV_PATH.write_text("\n".join(lines) + "\n", "utf-8")

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
        c["tts"]["provider"] = ["cosyvoice", "mimo", "api"][self.tts_provider.currentIndex()]
        c["tts"]["volume"] = self.tts_volume.value() / 100

        # 主动对话
        c.setdefault("proactive", {})["enabled"] = self.pro_enabled.isChecked()
        c["proactive"]["cooldown_minutes"] = self.pro_cooldown.value()

        # 屏幕感知
        c.setdefault("screen", {})["enabled"] = self.screen_enabled.isChecked()
        c["screen"]["interval"] = self.screen_interval.value()
        c["screen"]["blur"] = self.screen_blur.isChecked()

        # 窗口互动
        c.setdefault("window_interaction", {})["enabled"] = self.wi_enabled.isChecked()
        c["window_interaction"]["cooldown_seconds"] = self.wi_cooldown.value()

        # 久坐提醒
        c.setdefault("break_reminder", {})["enabled"] = self.break_enabled.isChecked()
        c["break_reminder"]["idle_minutes"] = self.break_idle.value()
        c["break_reminder"]["cooldown_minutes"] = self.break_cooldown.value()

        # ASR
        c.setdefault("asr", {})["provider"] = ["whisper_local", "mimo", "api"][self.asr_provider.currentIndex()]

        # 记忆注入
        c.setdefault("memory", {})["budget_mode"] = "auto" if self.mem_mode.currentIndex() == 0 else "manual"
        c["memory"]["budget_chars"] = self.mem_budget.value()

        # API .env
        self._save_env()

        # 角色包选择
        if hasattr(self, '_pkg_select'):
            pkg_data = self._pkg_select.currentData()
            if pkg_data:
                c["character_package"] = pkg_data

        self.accept()

    def get_config(self) -> dict:
        return self._config
