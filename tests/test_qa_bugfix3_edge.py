# -*- coding: utf-8 -*-
"""QA 独立回归（fresh eyes）：BugFix #3（commit 091263c）边界验证。

验证对象：
  - Problem A：tool-silent 恢复在多 tool 事件密集 + 空 text_delta 场景失效。
    根因：received_final_text_event 被 text_delta（增量事件）误置位 → 恢复条件
    `not received_final_text` 永久为假 → turn 拖到 deadline 墙才恢复。
    修复：① 标志只在 turn_end 置位；② 恢复条件改 `not turn.done`；
          ③ _recover_from_history 以历史权威文本整体替换 text_parts。
  - Problem B：设置面板「角色包」切换重启不生效（commit 091263c 修复）。
    根因：基础 tab 下拉框只写 character_package（启动无人读取的死字段）。
    修复：统一以 agents[].enabled 为启动真相源，_apply_package_selection /
          _current_active_agent_id / _sync_pkg_select 三函数统一。
    BugFix #4（本任务）：用户意图是**删除**基础 tab 的角色包下拉框，只保留
    角色包管理 tab（M5）的「切换选中桌宠」；因此本文件不再断言 _pkg_select
    存在，改为断言其不存在 + _save 不写 character_package。

本文件独立验证（非重跑工程师测试），针对 team-lead 指定边界：
  a) 多 tool 事件密集 + 空 text_delta → tool-silent 恢复触发，且 text_parts
     不被空 delta 污染（修复验证：旧代码该场景拖到 deadline 墙）；
  b) 正常工具+文本链路（text_delta → turn_end）→ 不触发恢复、流式文本不丢失
     （防误恢复，回归点：恢复路径整体替换 text_parts 不得在正常路径生效）；
  c) 角色包切换：_apply_package_selection 写 agents[].enabled → 启动读取生效；
     旧配置（只有 character_package 无 agents[]）兼容；多宠模式保存不回退；
  d) turn 已 done（turn_end 到但文本没上屏）→ 正常空文本完成，不误恢复。
"""
from __future__ import annotations

import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.hanako_session_manager import (
    HanakoSessionManager,
    HistoryPage,
    SessionRef,
    TurnAccumulator,
)


class _Sub:
    def __init__(self, fn, event_types=None):
        self.fn = fn
        self.event_types = event_types

    def close(self):
        pass


class _FakeWS:
    """最小 WS 客户端：永远就绪、可发 prompt、记录 abort。"""

    def __init__(self):
        self.sent_prompts: list[dict] = []
        self.ready = True
        self.aborts: list = []

    @property
    def is_ready(self):
        return self.ready

    def start(self):
        pass

    def wait_until_ready(self, timeout):
        return self.ready

    def subscribe(self, callback, event_types=None, session_id=None, session_path=None):
        return _Sub(callback, event_types)

    def subscribe_state(self, callback):
        return _Sub(callback, None)

    def send_prompt(self, **kwargs):
        self.sent_prompts.append(kwargs)

    def resume_stream(self, cursor):
        pass

    def abort_stream(self, cursor, reason="user_abort"):
        self.aborts.append((cursor, reason))


def _make_sm(grace: float = 0.05, reply_timeout: float = 180.0, activity_timeout: float = 60.0):
    sm = HanakoSessionManager(
        ws_client=_FakeWS(),
        base_url="https://hanako.invalid",
        token="",
        reply_timeout=reply_timeout,
        activity_timeout=activity_timeout,
        silent_turn_grace=grace,
    )
    return sm


def _history_with_reply(session, limit=30):
    return HistoryPage(
        session=session,
        messages=(
            {"role": "user", "displayText": "你好", "content": "你好"},
            {"role": "assistant", "content": "晚上好呀，月曦夜。", "thinking": ""},
        ),
        content_blocks=(),
        has_more=False,
    )


def _history_without_reply(session, limit=30):
    return HistoryPage(
        session=session,
        messages=(
            {"role": "user", "displayText": "你好", "content": "你好"},
        ),
        content_blocks=(),
        has_more=False,
    )


_SESSION = SessionRef("sess-1", "/agents/aimis/sessions/sess-1", "aimis")


def _wait_for_turn(sm, session, timeout=2.0) -> TurnAccumulator:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        turn = sm._pending_by_session.get(session.session_id)
        if turn is not None:
            return turn
        time.sleep(0.01)
    raise AssertionError("turn 未被索引，无法注入事件")


def _inject(sm, event_type, seq, *, delta=None, name=None, success=None, stream_id=None):
    event = {
        "type": event_type,
        "sessionId": _SESSION.session_id,
        "sessionPath": _SESSION.session_path,
        "streamId": stream_id or ("tool-stream" if event_type.startswith("tool") else "own-stream"),
        "seq": seq,
    }
    if delta is not None:
        event["delta"] = delta
    if name is not None:
        event["name"] = name
    if success is not None:
        event["success"] = success
    sm._handle_event(event)


# ── a) 问题 A 复现 + 修复验证：多 tool 密集 + 空 text_delta ──────────────────


def test_dense_tool_events_with_empty_text_delta_recovers_and_replaces_polluted_parts():
    """多 tool 事件密集到达，中间夹空 text_delta（无 turn_end）→ tool-silent
    恢复必须在 grace 后立即触发（旧代码被空 delta 误置位，拖到 deadline 墙），
    且恢复后 text_parts 以历史权威文本替换（空 delta 不再污染正文）。"""
    sm = _make_sm(grace=0.05, reply_timeout=180.0)
    sm.get_history = _history_with_reply
    holder: dict = {}

    def run():
        holder["result"] = sm.send_and_wait(_SESSION, "你好", display_text="你好", timeout=180.0)

    t0 = time.monotonic()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    turn = _wait_for_turn(sm, _SESSION)

    # 密集 tool 事件 + 空 text_delta（无 turn_end）——#3 复现场景
    for seq, ev in enumerate((
        {"type": "tool_start", "name": "search_memory"},
        {"type": "text_delta", "delta": ""},
        {"type": "tool_start", "name": "biaoqingbao_express"},
        {"type": "tool_end", "name": "biaoqingbao_express", "success": True},
        {"type": "text_delta", "delta": ""},
        {"type": "tool_start", "name": "play"},
    ), start=1):
        _inject(sm, ev["type"], seq, delta=ev.get("delta"), name=ev.get("name"), success=ev.get("success"))
        time.sleep(0.01)
    t.join(timeout=8.0)
    elapsed = time.monotonic() - t0

    assert not t.is_alive(), "多 tool + 空 text_delta 也应从历史恢复并返回（不得拖到 deadline 墙）"
    assert elapsed < 8.0, f"应在 grace 后即恢复，实际 {elapsed:.2f}s（旧代码拖到 ~60s+）"
    result = holder["result"]
    assert result.error is None
    assert "晚上好呀" in result.text, f"应恢复历史权威文本，实际={result.text!r}"
    assert result.text == "晚上好呀，月曦夜。", "空 delta 不得污染最终文本"
    assert turn.received_final_text_event is False, "空 text_delta/无 turn_end 不得置最终文本标志"
    assert turn.received_any_event is True
    assert float(getattr(turn, "_last_tool_silent_recovery_ts", 0.0)) > 0.0, "应已触发 tool-silent 恢复"
    assert turn.text_parts == ["晚上好呀，月曦夜。"], f"恢复路径应整体替换 text_parts: {turn.text_parts!r}"


def test_tool_silent_recovery_does_not_fire_when_history_has_no_reply_but_delta_pollution():
    """空 text_delta 污染 + 历史暂无回复 → 恢复不判死、不 abort；随后晚到
    text_delta/turn_end 仍能正常完成（空 delta 不置位不屏蔽晚到流）。"""
    sm = _make_sm(grace=0.05, reply_timeout=180.0)
    sm.get_history = _history_without_reply  # 恢复拿不到回复 → 必须继续等
    holder: dict = {}

    def run():
        holder["result"] = sm.send_and_wait(_SESSION, "你好", display_text="你好", timeout=180.0)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    turn = _wait_for_turn(sm, _SESSION)

    _inject(sm, "tool_start", 1, name="search_memory")
    _inject(sm, "text_delta", 2, delta="")  # 空 delta（#3 复现元素）
    _inject(sm, "tool_end", 3, name="search_memory", success=True)
    time.sleep(1.3)  # 越过 ~1s 轮询 + grace：恢复已尝试但未完成 turn
    assert turn is not None and not turn.done, "历史无回复时恢复不得判死 turn"
    assert float(getattr(turn, "_last_tool_silent_recovery_ts", 0.0)) > 0.0, "应已触发恢复尝试"

    # 晚到流仍能正常完成（空 delta 不再永久屏蔽后续事件）
    _inject(sm, "text_delta", 4, delta="晚上好呀（晚到）")
    _inject(sm, "turn_end", 5)
    t.join(timeout=3.0)

    assert not t.is_alive()
    result = holder["result"]
    assert result.error is None, f"晚到 WS 事件应正常完成 turn: {result.error}"
    assert result.text == "晚上好呀（晚到）", f"晚到流文本应保留，实际={result.text!r}"
    assert turn.done


# ── b) 防误恢复：正常工具+文本链路不触发恢复、文本不丢失 ──────────────────


def test_normal_tool_text_chain_preserves_streamed_text_without_recovery():
    """text_delta（含非空正文）→ turn_end 在 grace 内到达 → 用流式文本正常完成；
    _recover_from_history 的整体替换不得在正常路径生效（回归点：不截断实时回复）。"""
    sm = _make_sm(grace=0.2)
    sm.get_history = _history_with_reply  # 若误恢复会拿到历史旧文本（回归点）
    holder: dict = {}

    def run():
        holder["result"] = sm.send_and_wait(_SESSION, "你好", display_text="你好", timeout=180.0)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    turn = _wait_for_turn(sm, _SESSION)

    _inject(sm, "tool_start", 1, name="biaoqingbao_express")
    _inject(sm, "tool_end", 2, name="biaoqingbao_express", success=True)
    time.sleep(0.1)  # 文本稍晚（仍在 grace 内）
    _inject(sm, "text_delta", 3, delta="实时回复")
    _inject(sm, "turn_end", 4)
    t.join(timeout=5.0)

    assert not t.is_alive(), "send_and_wait 应在事件流完成后返回"
    result = holder["result"]
    assert result.error is None
    assert result.text == "实时回复", f"应保留流式文本，实际={result.text!r}"
    assert "晚上好呀" not in result.text, "正常链路不得被历史旧文本截断"
    assert turn.text_parts == ["实时回复"], f"正常路径 text_parts 应保留增量: {turn.text_parts!r}"
    assert turn.received_final_text_event is True, "turn_end 应置最终文本标志"
    assert float(getattr(turn, "_last_tool_silent_recovery_ts", 0.0)) == 0.0, (
        "grace 内收到 turn_end → tool-silent 恢复不应触发"
    )


# ── d) turn 已 done 但文本未上屏的边界 ──────────────────────────────────────


def test_turn_end_without_text_completes_empty_without_recovery():
    """turn_end 到达但文本没上屏（无 text_delta）→ turn 已 done，恢复条件
    `not turn.done` 天然排除已完成 turn——不得用历史旧文本顶替空文本。"""
    sm = _make_sm(grace=0.05, reply_timeout=180.0)
    sm.get_history = _history_with_reply  # 历史有回复，若误恢复会拿到旧文本
    holder: dict = {}

    def run():
        holder["result"] = sm.send_and_wait(_SESSION, "你好", display_text="你好", timeout=180.0)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    turn = _wait_for_turn(sm, _SESSION)

    _inject(sm, "turn_end", 1)  # 无任何 text_delta
    t.join(timeout=3.0)

    assert not t.is_alive()
    result = holder["result"]
    assert result.error is None
    assert result.text == "", f"turn_end 无文本 → 空文本完成，不得回灌历史: {result.text!r}"
    assert turn.done
    assert turn.received_final_text_event is True, "turn_end 是权威终态信号"
    assert float(getattr(turn, "_last_tool_silent_recovery_ts", 0.0)) == 0.0, (
        "turn 已 done → 不得触发 tool-silent 恢复"
    )
    assert len(sm.ws_client.aborts) == 0, "正常完成不得 abort"


# ── c) 问题 B：角色包切换持久化 + 旧配置兼容 ───────────────────────────────


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


def test_apply_package_selection_then_startup_read_effective():
    """_apply_package_selection 写 agents[].enabled + character +
    character_package → 模拟 pet_manager 启动读取（enabled_agents 过滤
    a.get("enabled", True)）→ 选中角色生效，其余禁用。"""
    config = _base_config()
    dialog = _build_dialog(config)

    dialog._apply_package_selection("shizuku")

    by_id = {a["id"]: a for a in config["agents"]}
    assert by_id["shizuku"]["enabled"] is True
    assert by_id["miku"]["enabled"] is False
    assert by_id["yuexinmiao"]["enabled"] is False
    assert config["character"] == "shizuku"
    assert config["character_package"] == "shizuku", "展示字段应与真相源同步"

    # 模拟 pet_manager.enabled_agents（launch_all 唯一读取路径）
    enabled = [a["id"] for a in config.get("agents", []) if a.get("enabled", True)]
    assert enabled == ["shizuku"], f"启动应只启用 shizuku，实际={enabled!r}"


def test_old_config_without_agents_list_is_compatible():
    """旧配置只有 character_package、没有 agents[] → _apply_package_selection
    会新建 agents[] 条目并置 enabled，保证重启生效（旧实现只写 character_package
    无人读取）。BugFix #4 后基础 tab 无下拉框，不再断言下拉框显示。"""
    config = {"character_package": "shizuku", "character": "shizuku"}
    dialog = _build_dialog(config)

    assert dialog._current_active_agent_id() is None, "无 agents[] 时应视为无启用桌宠"
    assert not hasattr(dialog, "_pkg_select"), "BugFix #4：基础 tab 不应有角色包下拉框"

    dialog._apply_package_selection("shizuku")
    by_id = {a["id"]: a for a in config.get("agents", [])}
    assert by_id["shizuku"]["enabled"] is True, "旧配置切换应补建 agents[] 条目并启用"
    assert config["character"] == "shizuku"
    assert config["character_package"] == "shizuku"

    enabled = [a["id"] for a in config.get("agents", []) if a.get("enabled", True)]
    assert enabled == ["shizuku"], "启动读取路径应生效"


def test_save_with_default_in_multi_pet_mode_does_not_converge_to_single():
    """多宠模式（≥2 个启用）保存后 agents[].enabled 不变（绝不悄悄收敛成单宠）；
    _current_active_agent_id 返回 None。BugFix #4 后无下拉框，不写死 character_package。"""
    config = _base_config()
    config.pop("character_package", None)  # 输入不带死字段，断言 _save 不会新增
    config["agents"][1]["enabled"] = True  # miku + yuexinmiao 同时启用
    dialog = _build_dialog(config)

    assert dialog._current_active_agent_id() is None, "多宠模式应返回 None"
    assert not hasattr(dialog, "_pkg_select"), "BugFix #4：基础 tab 不应有角色包下拉框"

    saved = {}
    import ui.settings_dialog as sd
    from unittest.mock import patch

    with patch.object(sd, "save_config", lambda cfg: saved.update(cfg)), \
         patch.object(dialog, "_save_env", lambda: None):
        dialog._save()

    by_id = {a["id"]: a for a in saved.get("agents", [])}
    assert by_id["miku"]["enabled"] is True, "多宠模式保存不得收敛成单宠"
    assert by_id["yuexinmiao"]["enabled"] is True
    assert "character_package" not in saved, (
        "BugFix #4：基础 tab 保存不再写 character_package 死字段"
    )
