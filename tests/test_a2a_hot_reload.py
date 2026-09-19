# -*- coding: utf-8 -*-
"""派活热重载验收（用户 2026-09-19："改完要重启生效不是好状态" → "可以做"）。

要验的不是"函数能跑"，而是三件事：
  1. 设置保存后**立刻生效**（不用重启）；
  2. **关掉开关真的会卸载**（否则"关了还照做"比不能关更糟）；
  3. 启动路径与热重载路径是**同一条**（不然会出现"启动时对、保存后不对"）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.a2a_capability import apply_config, unregister_a2a
from core.capability_registry import (
    CAPABILITIES,
    EXTERNAL_CAPABILITIES,
    CapabilityRouter,
)
from core.a2a_capability import CAPABILITY_NAMES


class _Session:
    def __init__(self, agent):
        self.session_id = f"s-{agent}"
        self.session_path = f"/s/{agent}.jsonl"


def _make(allowed=("kurisu",), **cfg):
    base = {"enabled": True, "allowed_agents": list(allowed)}
    base.update(cfg)
    return lambda config: apply_config(
        {"a2a": {**base, **config}},
        lambda a: _Session(a),
        lambda s, t, to: "结论",
    )


def _a2a_caps():
    return [c for c in EXTERNAL_CAPABILITIES if c.name in CAPABILITY_NAMES]


@pytest.fixture(autouse=True)
def _clean():
    unregister_a2a()
    yield
    unregister_a2a()


# ── 开 ────────────────────────────────────────────────────


def test_apply_enables_immediately():
    apply_config({"a2a": {"enabled": True, "allowed_agents": ["kurisu"]}},
                 lambda a: _Session(a), lambda s, t, to: "结论")
    assert CapabilityRouter().route("把这件事交给红莉栖看看") is not None


def test_disabled_config_registers_nothing():
    apply_config({"a2a": {"enabled": False, "allowed_agents": ["kurisu"]}},
                 lambda a: _Session(a), lambda s, t, to: "结论")
    assert _a2a_caps() == []
    assert CapabilityRouter().route("把这件事交给红莉栖看看") is None


def test_returns_delegator_even_when_disabled():
    """关着也给个实例，好在状态页/日志里看它到底认了多少人。"""
    d = apply_config({"a2a": {"enabled": False, "allowed_agents": ["kurisu"]}},
                     lambda a: _Session(a), lambda s, t, to: "结论")
    assert d is not None and d.enabled is False
    assert d.allowed_agents == ["kurisu"]


# ── 关（热重载的核心） ────────────────────────────────────


def test_turning_off_unregisters_for_real():
    """开了再关，关键词必须**不再命中**——"关了还照做"比不能关更糟。"""
    create, send = (lambda a: _Session(a)), (lambda s, t, to: "结论")
    apply_config({"a2a": {"enabled": True, "allowed_agents": ["kurisu"]}}, create, send)
    assert CapabilityRouter().route("交给红莉栖看看") is not None

    apply_config({"a2a": {"enabled": False, "allowed_agents": ["kurisu"]}}, create, send)
    assert _a2a_caps() == []
    assert CapabilityRouter().route("交给红莉栖看看") is None


def test_missing_session_manager_unregisters():
    """会话管理器没注入时不能留半截能力在那儿。"""
    apply_config({"a2a": {"enabled": True, "allowed_agents": ["kurisu"]}},
                 lambda a: _Session(a), lambda s, t, to: "结论")
    assert apply_config({"a2a": {"enabled": True}}, None, None) is None
    assert _a2a_caps() == []


# ── 改（换人 / 换配额） ───────────────────────────────────


def test_swapping_whitelist_takes_effect():
    create, send = (lambda a: _Session(a)), (lambda s, t, to: "结论")
    apply_config({"a2a": {"enabled": True, "allowed_agents": ["kurisu"]}}, create, send)
    assert CapabilityRouter().route("交给红莉栖看看") is not None

    apply_config({"a2a": {"enabled": True, "allowed_agents": ["alice"]}}, create, send)
    assert CapabilityRouter().route("交给红莉栖看看") is None, "旧名单应立刻失效"
    assert CapabilityRouter().route("交给艾莉丝看看") is not None, "新名单应立刻生效"


def test_budget_change_takes_effect():
    create, send = (lambda a: _Session(a)), (lambda s, t, to: "结论")
    d = apply_config({"a2a": {"enabled": True, "allowed_agents": ["kurisu"],
                              "max_per_hour": 1, "max_per_day": 1}}, create, send)
    assert d.delegate("一", "kurisu").ok is True
    assert d.delegate("二", "kurisu").ok is False

    # 放宽配额后立刻能用（新实例，计数归零）
    d2 = apply_config({"a2a": {"enabled": True, "allowed_agents": ["kurisu"],
                               "max_per_hour": 9, "max_per_day": 9}}, create, send)
    assert d2.delegate("三", "kurisu").ok is True


def test_reapply_does_not_duplicate_capabilities():
    """反复保存设置不能把能力越挂越多（重复挂 = 同一句话被处理多次）。"""
    create, send = (lambda a: _Session(a)), (lambda s, t, to: "结论")
    for _ in range(3):
        apply_config({"a2a": {"enabled": True, "allowed_agents": ["kurisu"]}}, create, send)
    assert len(_a2a_caps()) == len(CAPABILITY_NAMES)
    assert CapabilityRouter().route("交给红莉栖看看") is not None


def test_hot_reload_never_touches_builtin_capabilities():
    """只摘自己挂的——绝不能把内置能力（日报/截图…）顺手删了。"""
    before = list(CAPABILITIES)
    apply_config({"a2a": {"enabled": True, "allowed_agents": ["kurisu"]}},
                 lambda a: _Session(a), lambda s, t, to: "结论")
    unregister_a2a()
    assert CAPABILITIES == before


# ── 同一条路（启动与热重载） ──────────────────────────────


def test_startup_and_reload_share_one_path():
    """pet.py 里必须只有一条装卸路径，否则会出现'启动时对、保存后不对'。"""
    src = Path(__file__).resolve().parents[1].joinpath("pet.py").read_text(encoding="utf-8")
    assert "def _apply_a2a_config" in src
    assert "_init_a2a" in src and "_apply_a2a_config()" in src
    # 启动路径要转发到同一个方法，而不是自己再装一次
    init_body = src[src.index("def _init_a2a"):]
    init_body = init_body[:init_body.index("def _apply_a2a_config")]
    assert "return self._apply_a2a_config()" in init_body
    assert "register_a2a" not in init_body, "启动路径不该自己装能力"


def test_settings_save_triggers_reload():
    """设置面板保存后必须真的调重载——不然'不用重启'只是句口号。"""
    src = Path(__file__).resolve().parents[1].joinpath("pet.py").read_text(encoding="utf-8")
    body = src[src.index("def _open_settings"):]
    # 2026-09-19：从只重载 a2a 改成统一入口（a2a/game/lip_sync/llm_gate）
    assert "self._apply_runtime_config()" in body
    # 必须在 save_config 之后（拿的是新 config）
    assert body.index("save_config(self.config)") < body.index("self._apply_runtime_config()")
