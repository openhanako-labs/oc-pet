"""桌宠总览面板 — 多宠物并行时的统一控制入口

UI重构: 继承 PanelWindow 基类，统一标题栏、刷新按钮、关闭按钮
"""
from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QWidget,
    QPushButton, QLabel, QFrame,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from ui.theme import get_default, rgb, rgba
from ui.panel_window import PanelWindow
# 2026-09-15: 补漏 import——_on_theme_changed 调用 apply_glass_shadow，
# 但本文件从未导入它（切主题时 NameError 崩溃）。
from ui.theme.design_system import apply_glass_shadow

logger = logging.getLogger(__name__)


class PetOverviewDialog(PanelWindow):
    """多桌宠总览面板。

    继承 PanelWindow，统一标题栏、刷新按钮、关闭按钮。
    
    界面要点：
    - 每行一个 agent：左 agent_id + 状态徽标，右 4 个操作按钮
    - 底部：全部隐藏 / 全部显示
    """

    def __init__(self, overview_api, parent=None):
        super().__init__("桌宠总览", parent, show_refresh=True, min_size=(360, 400), max_size=(520, 720))
        self._api = overview_api
        self._row_widgets: list[QWidget] = []
        
        # 填充内容区域
        self._build_content()
        
        # 添加底部区域
        self._build_footer()
        
        self.refresh()
    
    def _build_content(self):
        """构建内容区域（列表）"""
        # 列表区域（带滚动）
        self._list_frame = QFrame()
        self._list_frame.setObjectName("listFrame")
        self._list_frame.setStyleSheet(
            f"background: rgba({rgba(self._ui_theme, 'input_bg')});"
            f" border: 1px solid rgba({rgba(self._ui_theme, 'input_border')});"
            f" border-radius: 6px;"
        )
        self._list_layout = QVBoxLayout(self._list_frame)
        self._list_layout.setContentsMargins(6, 6, 6, 6)
        self._list_layout.setSpacing(4)
        self.content_layout.addWidget(self._list_frame, stretch=1)
    
    def _build_footer(self):
        """构建底部区域（全部隐藏/显示按钮）"""
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        
        hide_all_btn = QPushButton("👁‍🗨 全部隐藏")
        hide_all_btn.clicked.connect(self._on_hide_all)
        hide_all_btn.setStyleSheet(self._btn_style("primary"))
        bottom.addWidget(hide_all_btn)
        
        show_all_btn = QPushButton("👁 全部显示")
        show_all_btn.clicked.connect(self._on_show_all)
        show_all_btn.setStyleSheet(self._btn_style("primary"))
        bottom.addWidget(show_all_btn)
        
        bottom.addStretch(1)
        
        footer_widget = QWidget()
        footer_widget.setLayout(bottom)
        self.add_footer(footer_widget)

    # ── 主题 ──

    def _build_qss(self) -> str:
        return (
            f"QDialog {{ background: transparent; color: rgb({rgb(self._ui_theme, 'dlg_text')}); }}"
            f"QLabel {{ color: rgb({rgb(self._ui_theme, 'dlg_text')}); }}"
            f"QPushButton {{ background: rgb({rgb(self._ui_theme, 'btn_primary')});"
            f"                color: #ffffff; border: none; border-radius: 4px;"
            f"                padding: 4px 12px; font-size: 12px; }}"
            f"QPushButton:hover {{ background: rgb({rgb(self._ui_theme, 'btn_primary_hover')}); }}"
            f"QPushButton:disabled {{ background: rgb({rgb(self._ui_theme, 'btn_disabled_bg')});"
            f"                          color: rgb({rgb(self._ui_theme, 'dlg_muted')}); }}"
            f"QPushButton#close_btn {{ background: rgb({rgb(self._ui_theme, 'close_bg')}); }}"
            f"QPushButton#close_btn:hover {{ background: rgb({rgb(self._ui_theme, 'close_hover')}); }}"
            f"QPushButton#done {{ background: rgb({rgb(self._ui_theme, 'btn_send')}); }}"
            f"QPushButton#done:hover {{ background: rgb({rgb(self._ui_theme, 'btn_send_hover')}); }}"
        )

    def _btn_style(self, kind: str) -> str:
        t = self._ui_theme
        if kind == "close":
            return (
                f"QPushButton {{ background: rgb({rgb(t, 'close_bg')});"
                f" color: rgb({rgb(t, 'text_primary')}); border: none;"
                f" border-radius: 4px; padding: 4px 10px; font-size: 13px; }}"
                f"QPushButton:hover {{ background: rgb({rgb(t, 'close_hover')}); }}"
            )
        if kind == "refresh":
            return (
                f"QPushButton {{ background: rgba({rgba(t, 'tab_bg')});"
                f" color: rgb({rgb(t, 'text_primary')}); border: 1px solid rgba({rgba(t, 'tab_border')});"
                f" border-radius: 4px; padding: 4px 10px; font-size: 11px; }}"
                f"QPushButton:hover {{ background: rgba({rgba(t, 'tab_checked')}); }}"
            )
        if kind == "primary":
            return (
                f"QPushButton {{ background: rgb({rgb(t, 'btn_primary')});"
                f" color: #ffffff; border: none; border-radius: 4px;"
                f" padding: 6px 14px; font-size: 12px; }}"
                f"QPushButton:hover {{ background: rgb({rgb(t, 'btn_primary_hover')}); }}"
            )
        return ""

    def _on_theme_changed(self, theme: str):
        if theme not in ("dark", "light"):
            return
        self._ui_theme = theme
        self.setStyleSheet(self._build_qss())
        apply_glass_shadow(self._card, theme)
        self._list_frame.setStyleSheet(
            f"background: rgba({rgba(theme, 'input_bg')});"
            f" border: 1px solid rgba({rgba(theme, 'input_border')});"
            f" border-radius: 6px;"
        )
        for row in self._row_widgets:
            if hasattr(row, '_refresh_style'):
                row._refresh_style(theme)

    # ── 数据加载 ──

    def refresh(self):
        """重新拉取 overview_rows() 并重建行列表"""
        for w in self._row_widgets:
            w.setParent(None)
            w.deleteLater()
        self._row_widgets.clear()

        try:
            rows = self._api.overview_rows()
        except Exception as e:
            logger.warning("overview_rows 读取失败: %s", e)
            rows = []

        if not rows:
            empty = QLabel("暂无桌宠，请先在设置中启用角色并启动桌宠。")
            empty.setStyleSheet(
                f"color: rgb({rgb(self._ui_theme, 'text_muted')});"
                f" font-size: 11px; padding: 8px;"
            )
            empty.setAlignment(Qt.AlignCenter)
            self._list_layout.addWidget(empty)
            self._row_widgets.append(empty)
            return

        for row in rows:
            widget = self._make_row(row)
            self._list_layout.addWidget(widget)
            self._row_widgets.append(widget)

    def _make_row(self, row: dict) -> QWidget:
        """构造单行 control widget"""
        aid = row.get("agent_id", "?")
        running = bool(row.get("running", False))
        muted = bool(row.get("muted", False))
        passthrough = bool(row.get("passthrough", False))
        visible = bool(row.get("visible", False))
        name = row.get("name", aid)

        container = QWidget()
        container.setObjectName("petRow")
        h = QHBoxLayout(container)
        h.setContentsMargins(4, 3, 4, 3)
        h.setSpacing(8)

        # 左侧：徽标 + agent_id + name
        status_icon = "🟢" if running else "⚪"
        left = QLabel(f"{status_icon}  {aid}")
        if name and name != aid:
            left.setText(f"{status_icon}  {aid}  ·  {name}")
        left.setFont(QFont("Microsoft YaHei UI", 11, QFont.Bold))
        left.setStyleSheet(
            f"color: rgb({'80, 200, 130' if running else '160, 165, 180'});"
        )
        h.addWidget(left, stretch=1)

        # 右侧按钮组
        right = QHBoxLayout()
        right.setSpacing(4)

        if not running:
            # 未运行：仅一个「启动」按钮（关闭后可从总览恢复，避免桌宠"没了"）
            launch_btn = QPushButton("▶ 启动")
            launch_btn.setFixedWidth(64)
            launch_btn.setFixedHeight(26)
            launch_btn.setStyleSheet(self._btn_style("primary"))
            launch_btn.setToolTip("重新启动该桌宠窗口")
            launch_btn.clicked.connect(
                lambda _, a=aid: self._api.launch_window(a)
            )
            right.addWidget(launch_btn)
            h.addLayout(right)
            return container

        def make_btn(label: str, disabled: bool, click_cb, obj_name: str = "") -> QPushButton:
            btn = QPushButton(label)
            btn.setFixedWidth(32)
            btn.setFixedHeight(26)
            btn.setDisabled(disabled or not running)
            btn.clicked.connect(click_cb)
            if obj_name:
                btn.setObjectName(obj_name)
            return btn

        # 🔊/🔇 静音
        mute_btn = make_btn("🔊" if not muted else "🔇", False,
                            lambda _, a=aid: self._api.set_muted(a, not muted))
        mute_btn.setToolTip("静音切换")
        right.addWidget(mute_btn)

        # 🖱️ 穿透
        pass_btn = make_btn("🖱️" if passthrough else "🖱", False,
                            lambda _, a=aid: self._api.toggle_passthrough(a))
        pass_btn.setToolTip("鼠标穿透切换")
        right.addWidget(pass_btn)

        # 👁/🙈 显示/隐藏
        vis_btn = make_btn("👁" if visible else "🙈", False,
                           lambda _, a=aid: self._api.set_visible(a, not visible))
        vis_btn.setToolTip("显示/隐藏窗口")
        right.addWidget(vis_btn)

        # ✖️ 关闭（防误触：二次确认）
        close_btn = make_btn("✖", False,
                             lambda _, a=aid: self._confirm_close(a),
                             obj_name="close_btn")
        close_btn.setToolTip("关闭该桌宠窗口（可在总览中重新启动）")
        right.addWidget(close_btn)

        h.addLayout(right)
        return container

    def _confirm_close(self, agent_id: str):
        """关闭桌宠窗口前二次确认（防误触，关掉后有「启动」按钮可恢复）。"""
        from PySide6.QtWidgets import QMessageBox
        ret = QMessageBox.question(
            self, "关闭桌宠",
            f"确定关闭桌宠「{agent_id}」吗？\n关闭后仍可从总览面板重新启动。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ret == QMessageBox.Yes:
            try:
                self._api.close_window(agent_id)
            except Exception as e:
                logger.warning("close_window(%s) failed: %s", agent_id, e)
            self.refresh()

    # ── 行为 ──

    def _on_hide_all(self):
        try:
            self._api.hide_all()
        except Exception as e:
            logger.warning("hide_all 失败: %s", e)
        self.refresh()

    def _on_show_all(self):
        try:
            self._api.show_all()
        except Exception as e:
            logger.warning("show_all 失败: %s", e)
        self.refresh()
