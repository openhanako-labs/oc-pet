"""设置对话框"""
import json
import os
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QComboBox, QSlider, QCheckBox, QPushButton, QLabel, QGroupBox, QTabWidget, QWidget,
    QLineEdit, QSpinBox, QDoubleSpinBox, QListWidget, QListWidgetItem, QInputDialog,
    QMessageBox, QFileDialog, QScrollArea
)
from PySide6.QtCore import Qt
from config import load_config, save_config, DEFAULT_CONFIG

# ── Apple-style 亮色主题 ──
STYLE = """
QDialog { background: #f5f5f7; color: #1d1d1f; }
QScrollArea { background: #f5f5f7; border: none; }
QTabWidget::pane { border: 1px solid #d2d2d7; background: #ffffff; border-radius: 8px; }
QTabBar::tab {
    background: #e8e8ed; color: #86868b; border: 1px solid #d2d2d7;
    padding: 10px 20px; margin-right: 4px; border-top-left-radius: 8px; border-top-right-radius: 8px;
    font-size: 13px;
}
QTabBar::tab:selected { background: #ffffff; color: #1d1d1f; border-bottom: 2px solid #0071e3; font-weight: bold; }
QTabBar::tab:hover { background: #f5f5f7; color: #1d1d1f; }
QGroupBox {
    border: 1px solid #d2d2d7; border-radius: 12px;
    margin-top: 20px; padding-top: 24px;
    color: #86868b; font-weight: bold; font-size: 13px;
    background: #ffffff;
}
QGroupBox::title { left: 16px; padding: 0 10px; }
QLabel { color: #1d1d1f; font-size: 13px; }
QCheckBox { color: #1d1d1f; spacing: 10px; font-size: 13px; }
QComboBox {
    background: #ffffff; color: #1d1d1f;
    border: 1px solid #d2d2d7; border-radius: 8px; padding: 8px 14px;
    min-height: 32px; font-size: 13px;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 28px;
    border: none;
}
QComboBox::down-arrow {
    image: none;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid #86868b;
    margin-right: 10px;
}
QComboBox QAbstractItemView {
    background: #ffffff; color: #1d1d1f; selection-background-color: #e8e8ed;
    border: 1px solid #d2d2d7; border-radius: 8px;
}
QSlider::groove:horizontal { height: 6px; background: #d2d2d7; border-radius: 3px; }
QSlider::handle:horizontal {
    background: #0071e3; width: 20px; height: 20px;
    margin: -7px 0; border-radius: 10px;
}
QPushButton {
    background: #0071e3; color: #ffffff; border: none;
    border-radius: 8px; padding: 10px 28px; font-size: 13px; font-weight: bold;
}
QPushButton:hover { background: #0077ed; }
QPushButton:pressed { background: #006edb; }
QPushButton#save { background: #34c759; }
QPushButton#save:hover { background: #30d158; }
QPushButton#save:pressed { background: #2db84e; }
QPushButton#danger { background: #ff3b30; }
QPushButton#danger:hover { background: #ff453a; }
QPushButton#danger:pressed { background: #e0342a; }
QSpinBox, QDoubleSpinBox {
    background: #ffffff; color: #1d1d1f;
    border: 1px solid #d2d2d7; border-radius: 8px; padding: 8px 14px;
    min-height: 32px; font-size: 13px;
}
QLineEdit {
    background: #ffffff; color: #1d1d1f;
    border: 1px solid #d2d2d7; border-radius: 8px; padding: 8px 14px;
    min-height: 32px; font-size: 13px;
}
QLineEdit:focus { border: 2px solid #0071e3; }
QListWidget {
    background: #ffffff; color: #1d1d1f;
    border: 1px solid #d2d2d7; border-radius: 8px; font-size: 13px;
}
QListWidget::item { padding: 8px 12px; border-radius: 6px; margin: 2px 4px; }
QListWidget::item:selected { background: #e8e8ed; }
QListWidget::item:hover { background: #f5f5f7; }
"""


class SettingsDialog(QDialog):
    """设置对话框"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚙️ 设置")
        self.setMinimumWidth(520)
        self._config = load_config()
        self.setStyleSheet(STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # 创建滚动区域
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setContentsMargins(4, 4, 4, 4)
        scroll_layout.setSpacing(8)

        tabs = QTabWidget()
        scroll_layout.addWidget(tabs)

        # ── Tab 1: 基础设置 ──
        basic_tab = QWidget()
        basic_layout = QVBoxLayout(basic_tab)
        basic_layout.setContentsMargins(12, 12, 12, 12)
        basic_layout.setSpacing(10)

        # 行为
        behavior_group = QGroupBox("行为模式")
        behavior_layout = QFormLayout(behavior_group)
        behavior_layout.setSpacing(12)
        behavior_layout.setContentsMargins(16, 20, 16, 16)

        self.behavior = QComboBox()
        self.behavior.addItems(["安静", "普通", "活跃", "粘人"])
        mode_map = {"quiet": 0, "normal": 1, "active": 2, "cling": 3}
        self.behavior.setCurrentIndex(mode_map.get(self._config.get("behavior", "normal"), 1))
        behavior_layout.addRow("模式", self.behavior)

        basic_layout.addWidget(behavior_group)

        # 窗口
        window_group = QGroupBox("窗口")
        window_layout = QFormLayout(window_group)
        window_layout.setSpacing(12)
        window_layout.setContentsMargins(16, 20, 16, 16)

        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(30, 100)
        self.opacity.setValue(int(self._config.get("opacity", 1.0) * 100))
        self.opacity_label = QLabel(f"{self.opacity.value()}%")
        self.opacity.valueChanged.connect(lambda v: self.opacity_label.setText(f"{v}%"))
        op_row = QHBoxLayout()
        op_row.addWidget(self.opacity)
        op_row.addWidget(self.opacity_label)
        window_layout.addRow("透明度", op_row)

        self.scale = QSlider(Qt.Horizontal)
        self.scale.setRange(50, 200)
        self.scale.setValue(int(self._config.get("scale", 1.0) * 100))
        self.scale_label = QLabel(f"{self.scale.value()}%")
        self.scale.valueChanged.connect(lambda v: self.scale_label.setText(f"{v}%"))
        sc_row = QHBoxLayout()
        sc_row.addWidget(self.scale)
        sc_row.addWidget(self.scale_label)
        window_layout.addRow("缩放", sc_row)

        self.mouse_interaction = QCheckBox("鼠标交互 (视线跟随 + 反应)")
        self.mouse_interaction.setChecked(self._config.get("mouse_interaction", True))
        window_layout.addRow(self.mouse_interaction)

        basic_layout.addWidget(window_group)

        # 角色选择
        char_group = QGroupBox("角色")
        char_layout = QFormLayout(char_group)
        char_layout.setSpacing(12)
        char_layout.setContentsMargins(16, 20, 16, 16)

        self.character = QComboBox()
        from config import CHARACTER_INFO
        for cid, info in CHARACTER_INFO.items():
            self.character.addItem(info["name"], cid)
        current_char = self._config.get("character", "yuexinmiao")
        for i in range(self.character.count()):
            if self.character.itemData(i) == current_char:
                self.character.setCurrentIndex(i)
                break
        char_layout.addRow("当前角色", self.character)

        basic_layout.addWidget(char_group)
        basic_layout.addStretch()

        tabs.addTab(basic_tab, "基础")

        # ── Tab 2: 功能设置 ──
        func_tab = QWidget()
        func_layout = QVBoxLayout(func_tab)
        func_layout.setContentsMargins(12, 12, 12, 12)
        func_layout.setSpacing(10)

        config = self._config

        # TTS
        tts_group = QGroupBox("语音输出")
        tts_layout = QFormLayout(tts_group)
        tts_layout.setSpacing(12)
        tts_layout.setContentsMargins(16, 20, 16, 16)

        self.tts_enabled = QCheckBox("启用 TTS 语音")
        self.tts_enabled.setChecked(config.get("tts", {}).get("enabled", True))
        tts_layout.addRow(self.tts_enabled)

        self.tts_provider = QComboBox()
        self.tts_provider.addItems(["本地 CosyVoice", "MIMO TTS", "API 调用"])
        tts_prov_map = {"cosyvoice": 0, "mimo": 1, "api": 2}
        self.tts_provider.setCurrentIndex(tts_prov_map.get(config.get("tts", {}).get("provider", "cosyvoice"), 0))
        tts_layout.addRow("TTS 引擎", self.tts_provider)

        self.tts_volume = QSlider(Qt.Horizontal)
        self.tts_volume.setRange(0, 100)
        self.tts_volume.setValue(int(config.get("tts", {}).get("volume", 0.8) * 100))
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
        pro_layout.setSpacing(12)
        pro_layout.setContentsMargins(16, 20, 16, 16)

        self.pro_enabled = QCheckBox("启用主动搭话")
        self.pro_enabled.setChecked(config.get("proactive", {}).get("enabled", True))
        pro_layout.addRow(self.pro_enabled)

        self.pro_cooldown = QSpinBox()
        self.pro_cooldown.setRange(1, 120)
        self.pro_cooldown.setSuffix(" 分钟")
        self.pro_cooldown.setValue(config.get("proactive", {}).get("cooldown_minutes", 10))
        pro_layout.addRow("冷却时间", self.pro_cooldown)

        func_layout.addWidget(pro_group)

        # 屏幕感知
        screen_group = QGroupBox("屏幕感知")
        screen_layout = QFormLayout(screen_group)
        screen_layout.setSpacing(12)
        screen_layout.setContentsMargins(16, 20, 16, 16)

        self.screen_enabled = QCheckBox("启用屏幕截屏分析")
        self.screen_enabled.setChecked(config.get("screen", {}).get("enabled", True))
        screen_layout.addRow(self.screen_enabled)

        self.screen_blur = QCheckBox("截图模糊（隐私保护）")
        self.screen_blur.setChecked(config.get("screen", {}).get("blur", True))
        screen_layout.addRow(self.screen_blur)

        self.screen_interval = QSpinBox()
        self.screen_interval.setRange(30, 600)
        self.screen_interval.setSuffix(" 秒")
        self.screen_interval.setValue(config.get("screen", {}).get("interval", 120))
        screen_layout.addRow("截屏间隔", self.screen_interval)

        func_layout.addWidget(screen_group)

        # 窗口互动
        wi_group = QGroupBox("窗口互动")
        wi_layout = QFormLayout(wi_group)
        wi_layout.setSpacing(12)
        wi_layout.setContentsMargins(16, 20, 16, 16)

        self.wi_enabled = QCheckBox("启用窗口互动")
        self.wi_enabled.setChecked(config.get("window_interaction", {}).get("enabled", True))
        wi_layout.addRow(self.wi_enabled)

        self.wi_cooldown = QSpinBox()
        self.wi_cooldown.setRange(5, 300)
        self.wi_cooldown.setSuffix(" 秒")
        self.wi_cooldown.setValue(config.get("window_interaction", {}).get("cooldown_seconds", 30))
        wi_layout.addRow("冷却时间", self.wi_cooldown)

        func_layout.addWidget(wi_group)

        # 久坐提醒
        break_group = QGroupBox("久坐提醒")
        break_layout = QFormLayout(break_group)
        break_layout.setSpacing(12)
        break_layout.setContentsMargins(16, 20, 16, 16)

        self.break_enabled = QCheckBox("启用久坐提醒")
        self.break_enabled.setChecked(config.get("break_reminder", {}).get("enabled", True))
        break_layout.addRow(self.break_enabled)

        self.break_idle = QSpinBox()
        self.break_idle.setRange(5, 120)
        self.break_idle.setSuffix(" 分钟")
        self.break_idle.setValue(config.get("break_reminder", {}).get("idle_minutes", 15))
        break_layout.addRow("空闲阈值", self.break_idle)

        self.break_cooldown = QSpinBox()
        self.break_cooldown.setRange(5, 120)
        self.break_cooldown.setSuffix(" 分钟")
        self.break_cooldown.setValue(config.get("break_reminder", {}).get("cooldown_minutes", 30))
        break_layout.addRow("提醒间隔", self.break_cooldown)

        func_layout.addWidget(break_group)

        # ASR
        asr_group = QGroupBox("语音输入")
        asr_layout = QFormLayout(asr_group)
        asr_layout.setSpacing(12)
        asr_layout.setContentsMargins(16, 20, 16, 16)

        self.asr_provider = QComboBox()
        self.asr_provider.addItems(["本地 Whisper", "MIMO ASR", "API 调用"])
        asr_prov_map = {"whisper_local": 0, "mimo": 1, "api": 2}
        self.asr_provider.setCurrentIndex(asr_prov_map.get(config.get("asr", {}).get("provider", "whisper_local"), 0))
        asr_layout.addRow("ASR 引擎", self.asr_provider)

        func_layout.addWidget(asr_group)

        # 记忆注入
        mem_group = QGroupBox("记忆注入")
        mem_layout = QFormLayout(mem_group)
        mem_layout.setSpacing(12)
        mem_layout.setContentsMargins(16, 20, 16, 16)

        mem_mode = config.get("memory", {}).get("budget_mode", "auto")
        self.mem_mode = QComboBox()
        self.mem_mode.addItems(["自动（按模型上下文 1%）", "手动指定"])
        self.mem_mode.setCurrentIndex(0 if mem_mode == "auto" else 1)
        mem_layout.addRow("预算模式", self.mem_mode)

        self.mem_budget = QSpinBox()
        self.mem_budget.setRange(200, 20000)
        self.mem_budget.setSuffix(" 字符")
        self.mem_budget.setSingleStep(200)
        self.mem_budget.setValue(config.get("memory", {}).get("budget_chars", 3000))
        self.mem_budget.setEnabled(mem_mode != "auto")
        self.mem_mode.currentIndexChanged.connect(
            lambda idx: self.mem_budget.setEnabled(idx == 1)
        )
        mem_layout.addRow("记忆上限", self.mem_budget)

        self.mem_hint = QLabel("agnes-2.0-flash (1M tokens) → 自动预算 6000 字符")
        self.mem_hint.setStyleSheet("color: #86868b; font-size: 11px;")
        mem_layout.addRow(self.mem_hint)

        func_layout.addWidget(mem_group)

        func_layout.addStretch()

        tabs.addTab(func_tab, "功能")

        # ── Tab 2.5: 角色包管理 (M5) ──
        pkg_tab = QWidget()
        pkg_layout = QVBoxLayout(pkg_tab)
        pkg_layout.setContentsMargins(12, 12, 12, 12)
        pkg_layout.setSpacing(10)

        pkg_group = QGroupBox("角色包")
        pkg_inner = QVBoxLayout(pkg_group)
        pkg_inner.setSpacing(12)
        pkg_inner.setContentsMargins(16, 20, 16, 16)

        # 当前角色包选择
        pkg_form = QFormLayout()
        pkg_form.setSpacing(12)
        self._pkg_select = QComboBox()
        self._load_packages()
        pkg_form.addRow("当前角色包", self._pkg_select)
        pkg_inner.addLayout(pkg_form)

        # 角色包列表
        self._pkg_list = QListWidget()
        self._pkg_list.setMinimumHeight(120)
        self._refresh_pkg_list()
        pkg_inner.addWidget(self._pkg_list)

        # 操作按钮
        pkg_btns = QHBoxLayout()
        pkg_btns.setSpacing(10)
        btn_install = QPushButton("📦 安装角色包")
        btn_install.clicked.connect(self._install_package)
        btn_remove = QPushButton("🗑️ 移除选中")
        btn_remove.setObjectName("danger")
        btn_remove.clicked.connect(self._remove_package)
        btn_refresh = QPushButton("🔄 刷新")
        btn_refresh.clicked.connect(self._refresh_pkg_list)
        pkg_btns.addWidget(btn_install)
        pkg_btns.addWidget(btn_remove)
        pkg_btns.addWidget(btn_refresh)
        pkg_inner.addLayout(pkg_btns)

        self._pkg_status_label = QLabel("")
        self._pkg_status_label.setStyleSheet("color: #86868b; font-size: 11px;")
        pkg_inner.addWidget(self._pkg_status_label)

        pkg_layout.addWidget(pkg_group)
        pkg_layout.addStretch()

        tabs.addTab(pkg_tab, "角色包")

        # ── Tab 3: API 配置 ──
        api_tab = QWidget()
        api_layout = QVBoxLayout(api_tab)
        api_layout.setContentsMargins(12, 12, 12, 12)
        api_layout.setSpacing(10)

        # LLM
        llm_group = QGroupBox("LLM 大语言模型")
        llm_layout = QFormLayout(llm_group)
        llm_layout.setSpacing(12)
        llm_layout.setContentsMargins(16, 20, 16, 16)

        self.llm_provider = QComboBox()
        self.llm_provider.addItems(["deepseek", "openai", "openrouter", "siliconflow", "自定义"])
        llm_layout.addRow("Provider", self.llm_provider)

        self.llm_base_url = QLineEdit()
        self.llm_base_url.setPlaceholderText("https://api.deepseek.com")
        llm_layout.addRow("Base URL", self.llm_base_url)

        self.llm_api_key = QLineEdit()
        self.llm_api_key.setEchoMode(QLineEdit.Password)
        self.llm_api_key.setPlaceholderText("sk-xxx")
        llm_layout.addRow("API Key", self.llm_api_key)

        self.llm_model = QComboBox()
        self.llm_model.setEditable(True)
        llm_layout.addRow("Model", self.llm_model)

        self._load_env_to_ui()
        self.llm_provider.currentIndexChanged.connect(self._on_provider_change)

        api_layout.addWidget(llm_group)

        # TTS API
        tts_api_group = QGroupBox("TTS API (可选)")
        tts_api_layout = QFormLayout(tts_api_group)
        tts_api_layout.setSpacing(12)
        tts_api_layout.setContentsMargins(16, 20, 16, 16)

        self.tts_base_url = QLineEdit()
        self.tts_base_url.setPlaceholderText("留空使用 Provider 默认")
        tts_api_layout.addRow("Base URL", self.tts_base_url)

        self.tts_api_key = QLineEdit()
        self.tts_api_key.setEchoMode(QLineEdit.Password)
        tts_api_layout.addRow("API Key", self.tts_api_key)

        self.tts_model = QComboBox()
        self.tts_model.setEditable(True)
        tts_api_layout.addRow("Model", self.tts_model)

        self.tts_voice = QComboBox()
        self.tts_voice.setEditable(True)
        tts_api_layout.addRow("Voice", self.tts_voice)

        api_layout.addWidget(tts_api_group)

        # ASR API
        asr_api_group = QGroupBox("ASR API (可选)")
        asr_api_layout = QFormLayout(asr_api_group)
        asr_api_layout.setSpacing(12)
        asr_api_layout.setContentsMargins(16, 20, 16, 16)

        self.asr_base_url = QLineEdit()
        self.asr_base_url.setPlaceholderText("留空使用 Provider 默认")
        asr_api_layout.addRow("Base URL", self.asr_base_url)

        self.asr_api_key = QLineEdit()
        self.asr_api_key.setEchoMode(QLineEdit.Password)
        asr_api_layout.addRow("API Key", self.asr_api_key)

        self.asr_model = QComboBox()
        self.asr_model.setEditable(True)
        asr_api_layout.addRow("Model", self.asr_model)

        api_layout.addWidget(asr_api_group)

        # Vision API
        vision_group = QGroupBox("Vision API (屏幕感知)")
        vision_layout = QFormLayout(vision_group)
        vision_layout.setSpacing(12)
        vision_layout.setContentsMargins(16, 20, 16, 16)

        self.vision_base_url = QLineEdit()
        self.vision_base_url.setPlaceholderText("https://api.siliconflow.cn")
        vision_layout.addRow("Base URL", self.vision_base_url)

        self.vision_api_key = QLineEdit()
        self.vision_api_key.setEchoMode(QLineEdit.Password)
        vision_layout.addRow("API Key", self.vision_api_key)

        self.vision_model = QComboBox()
        self.vision_model.setEditable(True)
        vision_layout.addRow("Model", self.vision_model)

        vision_hint = QLabel("留空则回退到 LLM 配置")
        vision_hint.setStyleSheet("color: #86868b; font-size: 11px;")
        vision_layout.addRow(vision_hint)

        api_layout.addWidget(vision_group)
        api_layout.addStretch()

        tabs.addTab(api_tab, "API")

        # 设置滚动区域
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)
        save_btn = QPushButton("💾 保存")
        save_btn.setObjectName("save")
        save_btn.clicked.connect(self._save)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

    def _load_packages(self):
        """扫描并加载可用的角色包"""
        import sys
        sys.path.insert(0, os.path.dirname(__file__))
        try:
            from core.character_package import CharacterPackageManager
            mgr = CharacterPackageManager()
            pkgs = mgr.list_installed()

            self._pkg_select.clear()
            self._pkg_select.addItem("默认", "default")
            for pkg in pkgs:
                self._pkg_select.addItem(pkg.get("name", pkg.get("id", "?")), pkg.get("id"))

            # 选中当前
            current = self._config.get("character_package", "default")
            for i in range(self._pkg_select.count()):
                if self._pkg_select.itemData(i) == current:
                    self._pkg_select.setCurrentIndex(i)
                    break
        except Exception:
            pass

    def _refresh_pkg_list(self):
        """刷新角色包列表"""
        import sys
        sys.path.insert(0, os.path.dirname(__file__))
        self._pkg_list.clear()
        try:
            from core.character_package import CharacterPackageManager
            mgr = CharacterPackageManager()
            pkgs = mgr.list_installed()
            for pkg in pkgs:
                item = QListWidgetItem(f"🎭 {pkg.get('name', '?')} ({pkg.get('id', '?')})")
                item.setData(Qt.UserRole, pkg.get("id"))
                self._pkg_list.addItem(item)
            self._pkg_status_label.setText(f"共 {len(pkgs)} 个已安装角色包")
        except Exception as e:
            self._pkg_status_label.setText(f"扫描失败: {e}")

    def _install_package(self):
        """安装角色包（zip 文件）"""
        path, _ = QFileDialog.getOpenFileName(self, "选择角色包", "", "ZIP 文件 (*.zip)")
        if not path:
            return
        import sys
        sys.path.insert(0, os.path.dirname(__file__))
        try:
            from core.character_package import CharacterPackageManager
            mgr = CharacterPackageManager()
            result = mgr.install(path)
            self._pkg_status_label.setText(f"✅ 安装成功: {result.get('name', '?')}")
            self._refresh_pkg_list()
            self._load_packages()
        except Exception as e:
            self._pkg_status_label.setText(f"❌ 安装失败: {e}")

    def _remove_package(self):
        """移除选中的角色包"""
        item = self._pkg_list.currentItem()
        if not item:
            return
        pkg_id = item.data(Qt.UserRole)
        if not pkg_id:
            return
        reply = QMessageBox.question(self, "确认移除", f"确定要移除角色包 {pkg_id} 吗？",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        import sys
        sys.path.insert(0, os.path.dirname(__file__))
        try:
            from core.character_package import CharacterPackageManager
            mgr = CharacterPackageManager()
            mgr.remove(pkg_id)
            self._pkg_status_label.setText(f"✅ 已移除: {pkg_id}")
            self._refresh_pkg_list()
            self._load_packages()
        except Exception as e:
            self._pkg_status_label.setText(f"❌ 移除失败: {e}")

    def _load_env_to_ui(self):
        """从 .env 加载 API 配置到 UI"""
        from env_config import get_llm_config, get_tts_api_config, get_asr_api_config, get_vision_config
        llm = get_llm_config()
        if llm:
            self.llm_base_url.setText(llm.get("base_url", ""))
            self.llm_api_key.setText(llm.get("api_key", ""))
            model = llm.get("model", "")
            if model:
                self.llm_model.setCurrentText(model)

        tts = get_tts_api_config()
        if tts:
            self.tts_base_url.setText(tts.get("base_url", ""))
            self.tts_api_key.setText(tts.get("api_key", ""))
            self.tts_model.setCurrentText(tts.get("model", ""))
            self.tts_voice.setCurrentText(tts.get("voice", ""))

        asr = get_asr_api_config()
        if asr:
            self.asr_base_url.setText(asr.get("base_url", ""))
            self.asr_api_key.setText(asr.get("api_key", ""))
            self.asr_model.setCurrentText(asr.get("model", ""))

        vision = get_vision_config()
        if vision:
            self.vision_base_url.setText(vision.get("base_url", ""))
            self.vision_api_key.setText(vision.get("api_key", ""))
            self.vision_model.setCurrentText(vision.get("model", ""))

    def _on_provider_change(self, idx):
        """Provider 切换时自动填充 Base URL"""
        urls = {
            0: "https://api.deepseek.com",
            1: "https://api.openai.com",
            2: "https://openrouter.ai/api",
            3: "https://api.siliconflow.cn",
        }
        if idx in urls:
            self.llm_base_url.setText(urls[idx])

    def _save_env(self):
        """保存 API 配置到 .env"""
        from env_config import save_env
        save_env(
            llm_provider=self.llm_provider.currentText(),
            llm_base_url=self.llm_base_url.text().strip(),
            llm_api_key=self.llm_api_key.text().strip(),
            llm_model=self.llm_model.currentText().strip(),
            tts_base_url=self.tts_base_url.text().strip(),
            tts_api_key=self.tts_api_key.text().strip(),
            tts_model=self.tts_model.currentText().strip(),
            tts_voice=self.tts_voice.currentText().strip(),
            asr_base_url=self.asr_base_url.text().strip(),
            asr_api_key=self.asr_api_key.text().strip(),
            asr_model=self.asr_model.currentText().strip(),
            vision_base_url=self.vision_base_url.text().strip(),
            vision_api_key=self.vision_api_key.text().strip(),
            vision_model=self.vision_model.currentText().strip(),
        )

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
        c["memory"]["budget_chars"] = self.mem_budget.value() if self.mem_mode.currentIndex() == 1 else 0
        c["memory"]["budget_percent"] = 1.0  # 默认1%

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
