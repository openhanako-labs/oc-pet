# -*- coding: utf-8 -*-
"""运行时配置热生效（`_apply_runtime_config`）的语义验收。

这层最容易出的错不是"崩"，而是**悄悄记错账**：
把"没装成功"的配置记成"已生效"，下次真该装的时候就被"没变"挡掉了
（典型现场：启动时 A2A 还没拿到会话管理器，失败被记账，注入后永远装不上）。

所以这里用一个桩宿主**真跑一遍** `PetWindow._apply_runtime_config`，
而不是只做源码字符串检查。
"""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pet_mod = pytest.importorskip("pet")     # 需要 PySide6；装不上就跳过

HOT = ("a2a", "game", "lip_sync", "llm_gate")


class _Host:
    """最小宿主：接住四个装卸方法，记录谁被调了。"""

    HOT_CONFIG_KEYS = pet_mod.PetWindow.HOT_CONFIG_KEYS
    _apply_runtime_config = pet_mod.PetWindow._apply_runtime_config

    def __init__(self, config, results=None):
        self.config = config
        self.calls = []
        self._results = results or {}

    def _hit(self, key):
        self.calls.append(key)
        r = self._results.get(key, True)
        return r(self) if callable(r) else r

    def _apply_a2a_config(self):
        return self._hit("a2a")

    def _init_game_watch(self):
        return self._hit("game")

    def _init_lip_sync(self):
        return self._hit("lip_sync")

    def _init_llm_gate(self):
        return self._hit("llm_gate")


def _cfg(**over):
    base = {k: {"enabled": True} for k in HOT}
    base.update(over)
    return base


# ── 基本 ──────────────────────────────────────────────────


def test_applies_all_hot_keys_first_time():
    h = _Host(_cfg())
    out = h._apply_runtime_config()
    assert set(out.values()) == {"applied"}
    assert sorted(h.calls) == sorted(HOT)


def test_returns_unchanged_on_second_run():
    h = _Host(_cfg())
    h._apply_runtime_config()
    h.calls.clear()
    out = h._apply_runtime_config()
    assert set(out.values()) == {"unchanged"}
    assert h.calls == [], "内容没变就不该重装"


def test_reapplies_only_changed_key():
    h = _Host(_cfg())
    h._apply_runtime_config()
    h.calls.clear()
    h.config["lip_sync"] = {"mouth_peak": 0.75}
    out = h._apply_runtime_config()
    assert out["lip_sync"] == "applied"
    assert out["game"] == "unchanged"
    assert h.calls == ["lip_sync"], "别的键改了不能被连坐重建"


# ── 关键：没装成功就不记账 ────────────────────────────────


def test_failed_apply_is_not_recorded():
    h = _Host(_cfg(), results={"a2a": False})
    out = h._apply_runtime_config()
    assert out["a2a"] == "failed"

    h.calls.clear()
    out2 = h._apply_runtime_config()
    assert out2["a2a"] == "failed", "失败不能被记成'没变'——否则再也装不上"
    assert "a2a" in h.calls, "失败后下次还得再试"


def test_startup_failure_then_late_injection_works():
    """真实现场：启动时没会话管理器 → a2a 失败；之后注入成功 → 必须能装上。"""
    state = {"sm": None}

    def a2a(self):
        self.calls.append("a2a")
        return state["sm"] is not None          # 没会话管理器就装不上

    h = _Host(_cfg(), results={"a2a": a2a})
    assert h._apply_runtime_config()["a2a"] == "failed"

    state["sm"] = object()                       # 会话管理器注入
    assert h._apply_runtime_config()["a2a"] == "applied"


def test_none_return_counts_as_failure():
    """装卸方法返回 None（老代码）要按失败处理，不能算成功。"""
    h = _Host(_cfg(), results={"game": None})
    assert h._apply_runtime_config()["game"] == "failed"
    h.calls.clear()
    h._apply_runtime_config()
    assert "game" in h.calls


def test_applier_exception_is_contained():
    def boom(self):
        raise RuntimeError("炸")

    h = _Host(_cfg(), results={"llm_gate": boom})
    out = h._apply_runtime_config()
    assert out["llm_gate"] == "failed"
    assert out["a2a"] == "applied", "一个块炸了不能拖住别的块"


# ── 不整份替换 self.config ────────────────────────────────


def test_does_not_replace_whole_config():
    """运行期改过的位置/缩放等状态不能被磁盘版本冲掉。"""
    h = _Host(_cfg())
    h.config["agents"] = [{"id": "miku", "position": {"x": 999, "y": 888}}]
    same_object = h.config
    h._apply_runtime_config()
    assert h.config is same_object
    assert h.config["agents"][0]["position"]["x"] == 999


def test_copies_changed_block_into_memory_config():
    h = _Host(_cfg())
    h._apply_runtime_config()
    h._apply_runtime_config({**h.config, "lip_sync": {"mouth_peak": 0.55}})
    assert h.config["lip_sync"] == {"mouth_peak": 0.55}


def test_extra_keys_are_ignored():
    h = _Host(_cfg(some_future_key={"x": 1}))
    out = h._apply_runtime_config()
    assert "some_future_key" not in out
    assert set(out) == set(HOT)


def test_missing_blocks_are_treated_as_empty():
    h = _Host({})
    out = h._apply_runtime_config()
    assert set(out.values()) == {"applied"}
    assert h.config["game"] == {}, "缺失块按空块处理，不能崩"


def test_non_dict_config_falls_back_to_self_config():
    h = _Host(_cfg())
    out = h._apply_runtime_config("这不是配置")
    assert set(out.values()) == {"applied"}


# ── 源码护栏：三条路径真的都接上了 ────────────────────────


def _pet_src() -> str:
    return Path(__file__).resolve().parents[1].joinpath("pet.py").read_text(encoding="utf-8")


def test_three_entrypoints_share_one_path():
    src = _pet_src()
    # 启动
    assert "self._apply_runtime_config()" in src
    assert "self._start_config_watch()" in src
    # 设置保存（在 save_config 之后）
    body = src[src.index("def _open_settings"):]
    assert "self._apply_runtime_config()" in body
    assert body.index("save_config(self.config)") < body.index("self._apply_runtime_config()")


def test_startup_no_longer_calls_inits_separately():
    """启动必须走统一入口，不能自己再装一遍（否则又变成两条路）。"""
    src = _pet_src()
    block = src[src.index("# 可热生效的运行时配置"):]
    block = block[:block.index("_start_config_watch()")]
    for stray in ("self._init_game_watch()", "self._init_llm_gate()", "self._init_lip_sync()"):
        assert stray not in block, f"启动路径不该还有 {stray}"


def test_inits_report_success_for_the_ledger():
    """四个装卸方法必须回报 True/False——否则记账逻辑无从判断。"""
    src = _pet_src()
    for name in ("def _init_game_watch", "def _init_llm_gate", "def _init_lip_sync"):
        body = src[src.index(name):]
        body = body[:body.index("\n    def ", 10)]
        assert "return True" in body and "return False" in body, name
