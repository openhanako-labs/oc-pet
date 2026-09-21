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

HOT = ("a2a", "game", "lip_sync", "llm_gate", "atmosphere", "life_cursor")


class _Host:
    """最小宿主：接住装卸方法，记录谁被调了。"""

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

    # 2026-09-21：语境注入层（设置面板开关 → 热生效）
    def _init_atmosphere_layer(self):
        return self._hit("atmosphere")

    def _init_p1_life_cursor(self):
        return self._hit("life_cursor")


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


def test_context_layer_inits_must_report_bool():
    """★ 语境注入层的 init 必须返回**明确的 bool**。

    它们原先是隐式返回 None，而 `_apply_runtime_config` 把 None 当失败——
    后果是**每次保存配置都重建一次**：氛围层的历史份额白攒、
    生活游标的定时器重建。不报错，只是白干。
    """
    from pet_mixins.emotion_classify_mixin import EmotionClassifyMixin
    from pet_mixins.perception_mixin import PerceptionMixin
    from types import SimpleNamespace

    atmo = EmotionClassifyMixin._init_atmosphere_layer(
        SimpleNamespace(config={"atmosphere": {"enabled": False}}))
    assert atmo is True

    cur = PerceptionMixin._init_p1_life_cursor(SimpleNamespace(config={}))
    assert cur is True


def test_missing_blocks_are_treated_as_empty():
    h = _Host({})
    out = h._apply_runtime_config()
    assert set(out.values()) == {"applied"}
    assert h.config["game"] == {}, "缺失块按空块处理，不能崩"


def test_non_dict_config_falls_back_to_self_config():
    h = _Host(_cfg())
    out = h._apply_runtime_config("这不是配置")
    assert set(out.values()) == {"applied"}


def test_unchanged_reload_is_not_logged_as_info(caplog):
    """★ 桌宠**一边散步一边写盘**（位置变化 → `_AsyncConfigSaver` 防抖 150ms），
    每次写盘都会走到 `_on_config_file_changed`。原实现无条件 `logger.info(
    "配置热生效：...")`，于是"全部 unchanged"的 INFO 每 40 秒刷一行
    （实测 53 分钟 70 行）。真装卸了某个块才值得 INFO。
    """
    import logging

    class _H2(_Host):
        _on_config_file_changed = pet_mod.PetWindow._on_config_file_changed

        def _apply_runtime_config(self, cfg=None):
            return {k: "unchanged" for k in HOT}

    h = _H2(_cfg())
    with caplog.at_level("DEBUG"):
        h._on_config_file_changed(h.config)
    infos = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert not any("配置热生效：" in m for m in infos), f"unchanged 不该打 INFO：{infos}"
    assert any("无变化" in r.getMessage() for r in caplog.records), \
        "降到 DEBUG 也得留痕，否则下次又得靠猜"


# ── 源码护栏：三条路径真的都接上了 ────────────────────────


def _pet_src() -> str:
    return Path(__file__).resolve().parents[1].joinpath("pet.py").read_text(encoding="utf-8")


def test_three_entrypoints_share_one_path():
    src = _pet_src()
    # 启动
    assert "self._apply_runtime_config()" in src
    assert "self._start_config_watch()" in src
    # 设置保存（面板内部写盘；重载在面板返回之后）
    body = src[src.index("def _open_settings"):]
    assert "self._apply_runtime_config()" in body
    # 2026-09-21：写盘由设置面板内部用 config_diff 提交（只含动过的键），
    # _open_settings 不再整份回写；不变的是"先落盘、后热重载"的次序。
    assert body.index("dialog.exec()") < body.index("self._apply_runtime_config()")


def test_startup_no_longer_calls_inits_separately():
    """启动必须走统一入口，不能自己再装一遍（否则又变成两条路）。"""
    src = _pet_src()
    block = src[src.index("# 可热生效的运行时配置"):]
    block = block[:block.index("_start_config_watch()")]
    for stray in ("self._init_game_watch()", "self._init_llm_gate()", "self._init_lip_sync()"):
        assert stray not in block, f"启动路径不该还有 {stray}"


def test_inits_report_success_for_the_ledger():
    """装卸方法必须回报真值/假值——否则记账逻辑无从判断。

    2026-09-20：断言从「字面量 return True/return False」改为
    **行为验证**。原断言把「返回语句长什么样」当成契约，
    导致把实现搬到 ``core/hot_config_appliers.py``（并改成 ``return ok``）
    时误报。真正的契约是「调用方能据此判断装没装上」。
    """
    import importlib
    import sys

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)

    mod = importlib.import_module("core.hot_config_appliers")

    # lip_sync：返回 bool
    assert isinstance(mod.apply_lip_sync({}), bool)

    # game_watch：返回 (bool, watcher|None)
    ok, watcher = mod.apply_game_watch({})
    assert isinstance(ok, bool)
    assert watcher is None or hasattr(watcher, "on_foreground")

    # pet.py 侧仍需保留这三个可被 _apply_runtime_config 调度的 applier
    src = _pet_src()
    for name in ("def _init_game_watch", "def _init_llm_gate", "def _init_lip_sync"):
        assert name in src, name
    # 且它们确实被登记进调度表
    assert "\"lip_sync\": self._init_lip_sync" in src
