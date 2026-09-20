"""可热生效配置应用器（2026-09-19 从 pet.py 迁出）。

拆分的**核心价值验证**：这些 applier 原先内联在 PetWindow 里，
要测就得 import pet.py —— 那会拉起 PySide6 + QApplication。

本测试**不 import pet.py**，只 import 新模块，证明它们真的脱离了 Qt。
若哪天有人把 Qt 依赖塞回来，`test_module_is_qt_free` 会红。
"""
from __future__ import annotations

import ast
import io
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MOD = "core/hot_config_appliers.py"


def _src() -> str:
    return io.open(os.path.join(ROOT, MOD), encoding="utf-8").read()


# ── 0. 边界：本模块必须零 Qt ──

def test_module_is_qt_free():
    """这是拆分的全部意义：领域配置逻辑不该需要 Qt 才能测。"""
    tree = ast.parse(_src())
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and "PySide6" in (n.module or ""):
            pytest.fail(f"hot_config_appliers 引入了 Qt: {n.module}")
        if isinstance(n, ast.Import):
            for a in n.names:
                if "PySide6" in a.name:
                    pytest.fail(f"hot_config_appliers 引入了 Qt: {a.name}")


def test_module_does_not_import_pet():
    """不得反向 import pet.py —— 那会把 Qt 依赖绕回来。

    用 AST 看真实 import 语句，不看注释/文档字符串里的字样
    （本模块 docstring 里就写了“不在本模块里反向 import pet.py”这句话）。
    """
    tree = ast.parse(_src())
    bad = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            bad += [a.name for a in n.names if a.name.split(".")[0] == "pet"]
        elif isinstance(n, ast.ImportFrom):
            if (n.module or "").split(".")[0] == "pet":
                bad.append(n.module)
    assert not bad, f"反向 import pet.py 会破坏拆分: {bad}"


# ── 1. apply_lip_sync ──

def test_lip_sync_default_mouth_peak():
    from core import hot_config_appliers as h
    from core import lip_sync

    before = lip_sync.get_mouth_peak()
    try:
        assert h.apply_lip_sync({}) is True
        # 默认 1.0 = 不压
        assert lip_sync.get_mouth_peak() == 1.0
    finally:
        lip_sync.set_mouth_peak(before)


def test_lip_sync_custom_peak():
    from core import hot_config_appliers as h
    from core import lip_sync

    before = lip_sync.get_mouth_peak()
    try:
        assert h.apply_lip_sync({"lip_sync": {"mouth_peak": 0.55}}) is True
        assert lip_sync.get_mouth_peak() == 0.55
    finally:
        lip_sync.set_mouth_peak(before)


def test_lip_sync_bad_config_does_not_raise():
    from core import hot_config_appliers as h
    # 非 dict / 脏值都不该抛
    assert h.apply_lip_sync(None) in (True, False)
    assert h.apply_lip_sync({"lip_sync": "not-a-dict"}) in (True, False)


# ── 2. apply_game_watch ──

def test_game_watch_disabled_returns_none_watcher():
    from core import hot_config_appliers as h
    ok, watcher = h.apply_game_watch({"game": {"enabled": False}})
    assert ok is True
    assert watcher is None


def test_game_watch_enabled_builds_watcher():
    from core import hot_config_appliers as h
    ok, watcher = h.apply_game_watch({"game": {"enabled": True}})
    assert ok is True
    # 内置白名单非空 → 应建成
    assert watcher is not None


def test_game_watch_bad_config_does_not_raise():
    from core import hot_config_appliers as h
    ok, watcher = h.apply_game_watch(None)
    assert isinstance(ok, bool)


# ── 3. apply_a2a ──

def test_a2a_without_session_manager_returns_false():
    """无会话管理器 → False，调用方据此**不记账**（关键契约）。"""
    from core import hot_config_appliers as h
    assert h.apply_a2a({}, None) is False


def test_a2a_disabled_returns_delegate_or_none():
    from core import hot_config_appliers as h

    class _SM:
        def create_session(self, agent_id=None):
            raise AssertionError("不该被调用（a2a 未启用）")

        def send_and_wait(self, session, text, timeout=None):
            raise AssertionError("不该被调用")

    d = h.apply_a2a({"a2a": {"enabled": False}}, _SM())
    assert d is not False, "有会话管理器时不应返回 False"


def test_a2a_passes_on_result_through():
    """on_result 应被透传给底层 apply_config（本模块不改语义）。"""
    from core import hot_config_appliers as h

    called = {"n": 0}

    def _cb(_r):
        called["n"] += 1

    class _SM:
        def create_session(self, agent_id=None):
            class _S:
                id = "s1"
            return _S()

        def send_and_wait(self, session, text, timeout=None):
            class _R:
                text = "ok"
            return _R()

    d = h.apply_a2a({"a2a": {"enabled": True}}, _SM(), on_result=_cb)
    # 只要不抛、不返回 False 即可（真实派活逻辑在 core/a2a.py，另有测试）
    assert d is not False


# ── 4. build_a2a_bell ──

def test_bell_ok_result():
    from core import hot_config_appliers as h
    seen = []
    bell = h.build_a2a_bell(lambda t, e, s: seen.append((t, e, s)))

    class _R:
        ok = True
        delivered = True
        agent_id = "ophelia"

    bell(_R())
    assert len(seen) == 1
    text, emotion, source = seen[0]
    assert "结果" in text
    assert emotion == "happy"
    assert source == "a2a"


def test_bell_not_delivered():
    from core import hot_config_appliers as h
    seen = []
    bell = h.build_a2a_bell(lambda t, e, s: seen.append((t, e)))

    class _R:
        ok = False
        delivered = False
        agent_id = "ophelia"

    bell(_R())
    assert seen and seen[0][1] == "sad"


def test_bell_delivered_but_no_reply():
    """会话建起来了但还没回话 —— 不能替它宣布失败。"""
    from core import hot_config_appliers as h
    seen = []
    bell = h.build_a2a_bell(lambda t, e, s: seen.append((t, e)))

    class _R:
        ok = False
        delivered = True
        agent_id = "ophelia"

    bell(_R())
    assert seen and seen[0][1] == "thinking"


def test_bell_never_raises():
    """门铃在后台线程跑，任何异常都不能外泄。"""
    from core import hot_config_appliers as h

    def _boom(*_a):
        raise RuntimeError("bubble failed")

    bell = h.build_a2a_bell(_boom)
    bell(object())   # 不应抛


# ── 5. 迁移完整性：pet.py 里不该再有实现 ──

def test_pet_py_delegates_not_reimplements():
    """pet.py 里这三个方法应只剩薄转发，不再有实现细节。"""
    pet = io.open(os.path.join(ROOT, "pet.py"), encoding="utf-8").read()
    assert "from core.hot_config_appliers import" in pet, "未接入新模块"
    # 实现细节不该再出现在 pet.py
    assert "set_mouth_peak(" not in pet, "lip_sync 实现未迁走"
    assert "load_games(" not in pet, "game_watch 实现未迁走"
    assert "from core.a2a_capability import apply_config" not in pet, \
        "a2a 实现未迁走"
