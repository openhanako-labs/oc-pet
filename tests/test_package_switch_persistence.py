# -*- coding: utf-8 -*-
"""BugFix #4 回归测试：设置面板「角色包」入口收敛到角色包管理 tab。

背景（任务 #4）：用户截图显示设置面板「基础」tab 有"桌宠角色"卡片 + "角色包"
下拉框——该下拉框与角色包管理 tab（M5）的「切换选中桌宠」功能重复。用户意图是
**删除**基础 tab 的角色包下拉框，只保留角色包管理 tab 的切换功能。

验收：
  - 基础 tab 不再构建 _pkg_select 下拉框（hasattr == False）
  - _save 不再写 config.character_package（下拉框死字段彻底移除）
  - 角色包管理 tab（M5）的 _switch_pet 仍可用：写 agents[].enabled +
    character + character_package，重启生效
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _StubPetManager:
    """仅用于让 SettingsDialog 构建（pet_manager 传入后不应再触发下拉框）。"""

    def __init__(self):
        self.agents = []


def _build_dialog(config: dict):
    from PySide6.QtWidgets import QApplication
    from ui.settings_dialog import SettingsDialog

    app = QApplication.instance() or QApplication([])
    return SettingsDialog(config=config, pet_manager=_StubPetManager())


def _base_config():
    return {
        "agents": [
            {"id": "miku", "enabled": True, "position": {"x": 0, "y": 0}, "scale": 1.0, "builtin": True},
            {"id": "yuexinmiao", "enabled": False, "position": {"x": 0, "y": 0}, "scale": 1.0, "builtin": True},
        ],
        "character": "miku",
        "character_package": "default",
    }


def test_basic_tab_has_no_role_package_dropdown():
    """BugFix #4：基础 tab 不再有"角色包"下拉框（用户截图里的下拉框应删除）。"""
    config = _base_config()
    dialog = _build_dialog(config)

    assert not hasattr(dialog, "_pkg_select"), "基础 tab 不应再构建角色包下拉框"
    # 保存路径中也不再引用 _pkg_select（下拉框死字段彻底移除）
    import inspect
    src = inspect.getsource(dialog._save)
    assert "_pkg_select" not in src, "_save 不应再引用角色包下拉框"


def test_current_active_agent_id_reads_enabled_agents():
    config = _base_config()
    dialog = _build_dialog(config)

    assert dialog._current_active_agent_id() == "miku"

    # 多宠同时启用 → None（多宠模式不强制单一角色）
    config["agents"][1]["enabled"] = True
    assert dialog._current_active_agent_id() is None

    # 仅剩 yuexinmiao 启用 → 返回它
    config["agents"][0]["enabled"] = False
    assert dialog._current_active_agent_id() == "yuexinmiao"


def test_apply_package_selection_writes_startup_truth_source():
    """_apply_package_selection 必须同时写 agents[].enabled + character +
    character_package，保证 pet_manager.launch_all（只读 agents[].enabled）重启生效。"""
    config = _base_config()
    dialog = _build_dialog(config)

    dialog._apply_package_selection("shizuku")

    by_id = {a["id"]: a for a in config["agents"]}
    assert by_id["shizuku"]["enabled"] is True, "选中的角色必须启用"
    assert by_id["miku"]["enabled"] is False, "其他角色必须禁用"
    assert by_id["yuexinmiao"]["enabled"] is False
    assert config["character"] == "shizuku"
    assert config["character_package"] == "shizuku", "展示字段应与真相源同步"


def test_save_no_longer_writes_character_package(monkeypatch):
    """BugFix #4：基础 tab _save 不再写 config.character_package（下拉框已删除）。"""
    config = _base_config()
    config.pop("character_package", None)
    dialog = _build_dialog(config)

    saved = {}
    import ui.settings_dialog as sd
    monkeypatch.setattr(sd, "save_config", lambda cfg: saved.update(cfg))
    monkeypatch.setattr(dialog, "_save_env", lambda: None)

    dialog._save()

    assert "character_package" not in saved, "基础 tab 保存不再写 character_package 死字段"
    # 其他基础设置仍保存
    assert "behavior" in saved
    assert "opacity" in saved


def test_switch_pet_persists_and_no_dropdown(monkeypatch):
    """BugFix #4：角色包管理 tab「切换选中桌宠」仍可用：config 写全
    （agents/character/character_package），且基础 tab 已无下拉框可同步。"""
    config = _base_config()
    dialog = _build_dialog(config)

    saved = {}
    import ui.settings_dialog as sd
    monkeypatch.setattr(sd, "save_config", lambda cfg: saved.update(cfg))

    # 模拟 角色包管理 tab 列表选中 shizuku
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem
    item = QListWidgetItem("Shizuku v1.0.0")
    item.setData(Qt.UserRole, "shizuku")
    dialog._pkg_list.addItem(item)
    dialog._pkg_list.setCurrentRow(0)

    # 替换模态弹窗/信息框，避免测试阻塞
    import ui.settings_dialog as sd
    import avatar.factory as fac
    # 资源预校验在资源齐备时放行（本测试只验证切换写盘逻辑，不校验资源）
    monkeypatch.setattr(fac, "resource_available", lambda cid: (True, ""))
    monkeypatch.setattr(sd.QMessageBox, "information", lambda *a, **k: None)
    dialog._switch_pet()

    by_id = {a["id"]: a for a in saved.get("agents", [])}
    assert by_id["shizuku"]["enabled"] is True
    assert by_id["miku"]["enabled"] is False
    assert saved.get("character") == "shizuku"
    assert saved.get("character_package") == "shizuku", "_switch_pet 应同步 character_package"
    assert not hasattr(dialog, "_pkg_select"), "基础 tab 无下拉框可同步"


def test_resource_available_marks_missing_models():
    """factory.resource_available 必须区分『有模型』与『占位缺模型』。
    shizuku 声明 live2d 但无模型文件 → False；sample_live2d 有完整模型 → True。"""
    from avatar.factory import resource_available
    import os

    ok_sh, reason = resource_available("shizuku")
    assert ok_sh is False, "shizuku 声明 live2d 但模型未下载，应标记不可加载"
    assert ("模型" in reason) or ("model" in reason.lower()), "原因应指向缺少模型文件"

    ok_none, _ = resource_available("__no_such_character__")
    assert ok_none is False, "不存在的角色应标记不可加载"

    # sample_live2d 在本地有完整 Haru 模型；仓库不随附模型文件，故仅在
    # 真正存在模型素材（.model3.json）时才做强断言，否则跳过（model-less checkout）。
    live2d_dir = os.path.join("characters", "sample_live2d", "live2d")
    has_model = os.path.isdir(live2d_dir) and any(
        f.endswith(".model3.json") for f in os.listdir(live2d_dir)
    )
    ok_s, _ = resource_available("sample_live2d")
    if has_model:
        assert ok_s is True, "sample_live2d 有完整模型，应可加载"


def test_switch_pet_blocks_missing_resource(monkeypatch):
    """缺模型资源（如 shizuku）不切换、不写盘，并弹警告。"""
    config = _base_config()
    dialog = _build_dialog(config)

    import avatar.factory as fac
    monkeypatch.setattr(fac, "resource_available", lambda cid: (False, "缺少模型文件"))

    saved = {}
    import ui.settings_dialog as sd
    monkeypatch.setattr(sd, "save_config", lambda cfg: saved.update(cfg))
    monkeypatch.setattr(sd.QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(sd.QMessageBox, "information", lambda *a, **k: None)

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem
    item = QListWidgetItem("Shizuku（需下载模型）")
    item.setData(Qt.UserRole, "shizuku")
    dialog._pkg_list.addItem(item)
    dialog._pkg_list.setCurrentRow(0)

    dialog._switch_pet()

    assert not saved, "缺资源时不应写盘"
    by_id = {a["id"]: a for a in config["agents"]}
    assert "shizuku" not in by_id, "缺资源角色不应被启用"


def test_switch_pet_schedules_async_saver_on_success(monkeypatch):
    """切换成功时刷新 async_config_saver pending，避免退出时被旧 config 覆盖回原角色。"""
    config = _base_config()
    dialog = _build_dialog(config)

    import avatar.factory as fac
    monkeypatch.setattr(fac, "resource_available", lambda cid: (True, ""))

    import config as cfg_mod
    scheduled = []
    monkeypatch.setattr(cfg_mod.async_config_saver, "schedule", lambda c: scheduled.append(c))

    import ui.settings_dialog as sd
    monkeypatch.setattr(sd.QMessageBox, "information", lambda *a, **k: None)

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem
    item = QListWidgetItem("Sample Live2D (Haru)")
    item.setData(Qt.UserRole, "sample_live2d")
    dialog._pkg_list.addItem(item)
    dialog._pkg_list.setCurrentRow(0)

    dialog._switch_pet()

    assert scheduled, "切换成功应刷新 async_config_saver pending"
    assert scheduled[-1] is config, "pending 应为最新 config（含切换结果）"
