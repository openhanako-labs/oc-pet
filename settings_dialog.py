"""配置面板 - GUI 设置对话框

从 config.json 读取配置，修改后保存。
右键菜单 -> "⚙️ 设置" 打开。

可配置项：
  - TTS：开关、音量
  - 行为模式：静默/正常/活跃/黏人
  - 主动对话：开关、冷却时间
  - 屏幕感知：开关、截屏间隔
  - 久坐提醒：开关、空闲阈值
  - 语音输入：开关
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QCheckBox, QSlider, QSpinBox, QComboBox,
    QPushButton, QLabel, QGroupBox, QTabWidget, QWidget
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont


STYLE = """
QDialog { background: #1a1820; color: #d4cec4; }
QGroupBox {
    border: 1px solid #3a3450; border-radius: 8px;
    margin-top: 12px; padding-top: 16px;
    color: #8888aa; font-weight: bold;
}
QGroupBox::title { left: 12px; padding: 0 6px; }
QLabel { color: #d4cec4; }
QCheckBox { color: #d4cec4; }
QComboBox {
    background: #252330; color: #d4cec4;
    border: 1px solid #3a3450; border-radius: 4px; padding: 4px 8px;
}
QComboBox::drop-down { border: none; }
QComboBox QAbstractItemView {
    background: #252330; color: #d4cec4; selection-background-color: #3a3458;
}
QSlider::groove:horizontal { height: 4px; background: #3a3450; border-radius: 2px; }
QSlider::handle:horizontal {
    background: #88aacc; width: 14px; height: 14px;
    margin: -5px 0; border-radius: 7px;
}
QPushButton {
    background: #3a3458; color: #d4cec4; border: none;
    border-radius: 4px; padding: 8px 24px; font-size: 13px;
}
QPushButton:hover { background: #4a4478; }
QPushButton#save { background: #3a5844; }
QPushButton#save:hover { background: #4a7844; }
"""


class SettingsDialog(QDialog):
    """配置面板"""

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self._config = config
        self._result = None
        self.setWindowTitle("设置")
        self.setMinimumSize(420, 520)
        self.setStyleSheet(STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        # ── TTS 语音 ──
        tts_group = QGroupBox("语音输出")
        tts_layout = QFormLayout(tts_group)

        self.tts_enabled = QCheckBox("启用 TTS 语音")
        self.tts_enabled.setChecked(config.get("tts", {}).get("enabled", True))
        tts_layout.addRow(self.tts_enabled)

        self.tts_volume = QSlider(Qt.Horizontal)
        self.tts_volume.setRange(0, 100)
        self.tts_volume.setValue(int(config.get("tts", {}).get("volume", 0.8) * 100))
        self.tts_vol_label = QLabel(f"{self.tts_volume.value()}%")
        self.tts_volume.valueChanged.connect(lambda v: self.tts_vol_label.setText(f"{v}%"))
        vol_row = QHBoxLayout()
        vol_row.addWidget(self.tts_volume)
        vol_row.addWidget(self.tts_vol_label)
        tts_layout.addRow("音量", vol_row)

        layout.addWidget(tts_group)

        # ── 行为模式 ──
        beh_group = QGroupBox("行为模式")
        beh_layout = QFormLayout(beh_group)

        self.behavior = QComboBox()
        self.behavior.addItems(["静默 (quiet)", "正常 (normal)", "活跃 (active)", "黏人 (cling)"])
        beh_map = {"quiet": 0, "normal": 1, "active": 2, "cling": 3}
        self.behavior.setCurrentIndex(beh_map.get(config.get("behavior", "normal"), 1))
        beh_layout.addRow("模式", self.behavior)

        layout.addWidget(beh_group)

        # ── 主动对话 ──
        pro_group = QGroupBox("主动对话")
        pro_layout = QFormLayout(pro_group)

        self.pro_enabled = QCheckBox("启用主动搭话")
        self.pro_enabled.setChecked(config.get("proactive", {}).get("enabled", True))
        pro_layout.addRow(self.pro_enabled)

        self.pro_cooldown = QSpinBox()
        self.pro_cooldown.setRange(1, 120)
        self.pro_cooldown.setSuffix(" 分钟")
        self.pro_cooldown.setValue(config.get("proactive", {}).get("cooldown_minutes", 10))
        pro_layout.addRow("冷却时间", self.pro_cooldown)

        layout.addWidget(pro_group)

        # ── 久坐提醒 ──
        br_group = QGroupBox("久坐提醒")
        br_layout = QFormLayout(br_group)

        self.br_enabled = QCheckBox("启用久坐提醒")
        self.br_enabled.setChecked(config.get("break_reminder", {}).get("enabled", True))
        br_layout.addRow(self.br_enabled)

        self.br_idle = QSpinBox()
        self.br_idle.setRange(5, 120)
        self.br_idle.setSuffix(" 分钟")
        self.br_idle.setValue(config.get("break_reminder", {}).get("idle_minutes", 15))
        br_layout.addRow("触发空闲", self.br_idle)

        self.br_cooldown = QSpinBox()
        self.br_cooldown.setRange(5, 120)
        self.br_cooldown.setSuffix(" 分钟")
        self.br_cooldown.setValue(config.get("break_reminder", {}).get("cooldown_minutes", 30))
        br_layout.addRow("提醒冷却", self.br_cooldown)

        layout.addWidget(br_group)

        # ── 屏幕感知 ──
        screen_group = QGroupBox("屏幕感知")
        screen_layout = QFormLayout(screen_group)

        self.screen_enabled = QCheckBox("启用屏幕截屏分析")
        self.screen_enabled.setChecked(config.get("screen", {}).get("enabled", True))
        screen_layout.addRow(self.screen_enabled)

        self.screen_interval = QSpinBox()
        self.screen_interval.setRange(30, 600)
        self.screen_interval.setSuffix(" 秒")
        self.screen_interval.setValue(config.get("screen", {}).get("interval", 120))
        screen_layout.addRow("截屏间隔", self.screen_interval)

        layout.addWidget(screen_group)

        layout.addStretch()

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

    def _save(self):
        """收集所有设置写入 config"""
        c = self._config

        # TTS
        c.setdefault("tts", {})["enabled"] = self.tts_enabled.isChecked()
        c["tts"]["volume"] = self.tts_volume.value() / 100

        # 行为
        beh_idx = self.behavior.currentIndex()
        c["behavior"] = ["quiet", "normal", "active", "cling"][beh_idx]

        # 主动对话
        c.setdefault("proactive", {})["enabled"] = self.pro_enabled.isChecked()
        c["proactive"]["cooldown_minutes"] = self.pro_cooldown.value()

        # 久坐提醒
        c.setdefault("break_reminder", {})["enabled"] = self.br_enabled.isChecked()
        c["break_reminder"]["idle_minutes"] = self.br_idle.value()
        c["break_reminder"]["cooldown_minutes"] = self.br_cooldown.value()

        # 屏幕感知
        c.setdefault("screen", {})["enabled"] = self.screen_enabled.isChecked()
        c["screen"]["interval"] = self.screen_interval.value()

        self.accept()

    def get_config(self) -> dict:
        """获取修改后的 config"""
        return self._config
