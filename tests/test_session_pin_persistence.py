# -*- coding: utf-8 -*-
"""会话 pin 持久化测试（2026-09-20，用户要求 A）。

## 用户原话

> A（无论桌宠是否重启，都是在一个固定会话进行回复，最好还可以随时重启上下文）

## 之前的毛病

`_agent_pinned` 是**纯内存 dict**，重启即丢。于是每次重启桌宠都新建会话：

    实测日志里 5 个不同 session：
    sess_0mu9gdei1_... sess_0mu9he1tk_... sess_0mu9hl78h_...
    sess_0mu9ipht1_... sess_0mu9ktsps_...

后果：上下文不连贯（每轮从零开始）+ Hana 会话列表被碎片灌满。

## 修法

pin 落盘到 `~/.hanako/pets/session_<agent>.json`（沿用桌宠已有的 pets/ 约定，
`greet_*.json` 同目录）。启动时读回。

## 「随时重启上下文」那一半

已存在：右键菜单「🔄 新对话」→ `chat_mixin._create_new_session()`
→ `engine.create_new_session()`（它同时更新 pin 并落盘）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.harness_adapter import HanakoPetAdapter  # noqa: E402


@pytest.fixture()
def adapter(tmp_path, monkeypatch):
    """最小 adapter：把 pin 路径指到 tmp，避免污染真实 ~/.hanako。"""
    a = HanakoPetAdapter.__new__(HanakoPetAdapter)
    a.agent_id = "ophelia"
    a._agent_sessions = {}
    a._agent_pinned = {}
    a._pinned_session_id = None
    a._current_session = None
    monkeypatch.setattr(
        HanakoPetAdapter, "_pinned_path",
        lambda self: tmp_path / f"session_{self.agent_id}.json",
    )
    return a


class _Ref:
    def __init__(self, sid):
        self.session_id = sid
        self.session_path = f"/p/{sid}"


# ── 1. 落盘 ──


def test_save_writes_pin(adapter, tmp_path):
    adapter._agent_pinned["ophelia"] = "sess_A"
    adapter._save_pinned_sessions()
    p = tmp_path / "session_ophelia.json"
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["pinned"]["ophelia"] == "sess_A"
    assert "updated_at" in data


def test_set_session_persists(adapter, tmp_path):
    """set_session 是主入口——它必须落盘。"""
    adapter.set_session(_Ref("sess_B"))
    p = tmp_path / "session_ophelia.json"
    assert p.exists(), "set_session 未落盘 → 重启后 pin 会丢"
    assert json.loads(p.read_text(encoding="utf-8"))["pinned"]["ophelia"] == "sess_B"


# ── 2. 读回（跨重启）──


def test_load_restores_pin(adapter, tmp_path):
    """★ 核心：模拟重启——新实例从磁盘读回 pin。

    2026-09-20：pin 格式新增 `owned` 归属标记（区分桌宠专属会话与
    漂移来的助手主对话）。这里必须带上它，否则会被归属校验当作
    无标记的脏 pin 丢弃——那是另一个测试（test_session_pin_drift.py）
    要验证的行为。
    """
    (tmp_path / "session_ophelia.json").write_text(
        json.dumps({"pinned": {"ophelia": "sess_OLD"},
                    "owned": {"ophelia": "sess_OLD"}}), encoding="utf-8")
    adapter._load_pinned_sessions()
    assert adapter._agent_pinned["ophelia"] == "sess_OLD"
    assert adapter._pinned_session_id == "sess_OLD", (
        "恢复后 _pinned_session_id 也该同步（chat_via_hanako 读它）")


def test_roundtrip_survives_new_instance(adapter, tmp_path, monkeypatch):
    """完整往返：存 → 新实例 → 读回。"""
    adapter.set_session(_Ref("sess_ROUNDTRIP"))

    fresh = HanakoPetAdapter.__new__(HanakoPetAdapter)
    fresh.agent_id = "ophelia"
    fresh._agent_sessions = {}
    fresh._agent_pinned = {}
    fresh._owned_sessions = {}
    fresh._pinned_session_id = None
    monkeypatch.setattr(
        HanakoPetAdapter, "_pinned_path",
        lambda self: tmp_path / f"session_{self.agent_id}.json",
    )
    fresh._load_pinned_sessions()
    assert fresh._pinned_session_id == "sess_ROUNDTRIP"


# ── 3. 健壮性 ──


def test_load_missing_file_is_silent(adapter):
    """没有 pin 文件时不能抛（首次启动）。"""
    adapter._load_pinned_sessions()
    assert adapter._agent_pinned == {}


def test_load_corrupt_file_is_silent(adapter, tmp_path):
    """文件损坏不能崩（宁可重开会话，不能起不来）。"""
    (tmp_path / "session_ophelia.json").write_text("{不是合法 json", encoding="utf-8")
    adapter._load_pinned_sessions()
    assert adapter._agent_pinned == {}


def test_load_filters_non_string_values(adapter, tmp_path):
    """脏数据（null / 数字 / 空串）不得进 pin。"""
    (tmp_path / "session_ophelia.json").write_text(
        json.dumps({"pinned": {"ophelia": None, "x": 123, "y": "", "z": "ok"},
                    "owned": {"z": "ok"}}),
        encoding="utf-8")
    adapter._load_pinned_sessions()
    assert adapter._agent_pinned == {"z": "ok"}


def test_save_failure_does_not_raise(adapter, monkeypatch):
    """写盘失败不能影响对话（静默）。"""
    def boom(self):
        raise OSError("disk full")

    monkeypatch.setattr(HanakoPetAdapter, "_pinned_path", boom)
    adapter._save_pinned_sessions()  # 不抛即通过


# ── 4. 源码级守卫 ──


def test_init_calls_load():
    """__init__ 必须调 _load_pinned_sessions——否则重启恢复不了。"""
    src = open(os.path.join(_REPO, "core", "harness_adapter.py"),
               encoding="utf-8").read()
    assert "self._load_pinned_sessions()" in src


def test_set_session_calls_save():
    """set_session 必须调 _save_pinned_sessions。"""
    src = open(os.path.join(_REPO, "core", "harness_adapter.py"),
               encoding="utf-8").read()
    i = src.index("def set_session")
    body = src[i:i + 1200]
    assert "_save_pinned_sessions()" in body


def test_pinned_path_uses_pets_dir():
    """pin 文件放 ~/.hanako/pets/（沿用桌宠已有约定）。"""
    src = open(os.path.join(_REPO, "core", "harness_adapter.py"),
               encoding="utf-8").read()
    i = src.index("def _pinned_path")
    body = src[i:i + 500]
    assert '"pets"' in body
    assert "session_" in body


def test_reset_session_entry_exists():
    """「随时重启上下文」——右键菜单入口必须存在。"""
    src = open(os.path.join(_REPO, "pet.py"), encoding="utf-8").read()
    assert "_create_new_session" in src, "缺「新对话」入口"
    cm = open(os.path.join(_REPO, "pet_mixins", "chat_mixin.py"),
              encoding="utf-8").read()
    assert "def _create_new_session" in cm
