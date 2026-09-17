# -*- coding: utf-8 -*-
"""回归：后台任务走 utility_model，不再抢对话配额。

## 背景（2026-09-17）

oc-pet 有四五个"非用户主动"的 LLM 来源，全部与用户对话共用
`models.chat`（同一个 API Key、同一份配额）：

    screen_enrich    屏幕感知语义标注    ← 最高频（占 LLM 调用 73%）
    proactive        桌宠主动搭话
    idle             闲置闲聊
    memory_extract   记忆事实抽取
    memory_reflect   记忆反思

结果：用户打字时，屏幕感知也在打 API，两边抢同一份配额 → 429。

Hana 设置页早就提供 `utility_model` 字段专供这类用途，
但 oc-pet 不读它（用户已配 deepseek-v4-flash，一直没被用上）。

## 修法

`get_utility_config()` 读 Hana preferences 的 `utility_model`；
`chat_direct` / `_chat_stream_direct` 用 `_using_utility_model(source)`
上下文按来源临时切换模型，退出时**一定**还原。

关键不变式：
  - `user` 来源永远用对话模型（不得被切）
  - 未配置 utility_model → 完全不动（保持旧行为）
  - 异常路径也必须还原（否则后续用户对话会串到 utility 模型）
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CHAT = ("https://token.sensenova.cn/v1", "sk-chat", "sensenova-6.8-flash-lite")
UTIL = {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "sk-ds",
    "model": "deepseek-v4-flash",
}


@pytest.fixture
def adapter(monkeypatch):
    """最小 adapter 实例：只装模型相关属性，不跑 __init__（避免拉起 Qt/网络）。

    冻结 `_refresh_utility_cfg` —— 这些用例测的是**切换逻辑**，不是配置刷新；
    不冻结的话每次进入都会读真实 preferences.json 并覆盖测试桩。
    刷新行为另有专门用例（见 test_refresh_*）。
    """
    from core.harness_adapter import HanakoPetAdapter

    a = HanakoPetAdapter.__new__(HanakoPetAdapter)
    a._base_url, a._api_key, a._model = CHAT
    a._utility_cfg = dict(UTIL)
    monkeypatch.setattr(a, "_refresh_utility_cfg", lambda: None)
    return a


def test_user_source_never_switches(adapter):
    """最关键：用户对话不得被切到 utility 模型。

    切错了会导致用户消息由后台小模型回答 —— 质量骤降且难以察觉。
    """
    with adapter._using_utility_model("user"):
        assert adapter._model == CHAT[2], "user 来源必须用对话模型"
        assert adapter._base_url == CHAT[0]


@pytest.mark.parametrize(
    "source",
    ["screen_enrich", "proactive", "idle", "memory_extract", "memory_reflect"],
)
def test_internal_sources_switch_and_restore(adapter, source):
    """五个内部来源：进去切、出来还原。"""
    with adapter._using_utility_model(source):
        assert adapter._model == UTIL["model"], f"{source} 应走后台模型"
        assert adapter._base_url == UTIL["base_url"]
        assert adapter._api_key == UTIL["api_key"]
    # 退出后必须还原
    assert adapter._model == CHAT[2], f"{source} 退出后未还原模型"
    assert adapter._base_url == CHAT[0]
    assert adapter._api_key == CHAT[1]


def test_restores_on_exception(adapter):
    """异常路径也要还原 —— 否则后续用户对话会串到 utility 模型。"""
    with pytest.raises(RuntimeError):
        with adapter._using_utility_model("screen_enrich"):
            raise RuntimeError("boom")
    assert adapter._model == CHAT[2], "异常后必须还原"
    assert adapter._base_url == CHAT[0]


def test_unconfigured_utility_is_noop(monkeypatch):
    """未配置 utility_model → 完全不动（旧行为保持）。"""
    from core.harness_adapter import HanakoPetAdapter

    a = HanakoPetAdapter.__new__(HanakoPetAdapter)
    a._base_url, a._api_key, a._model = CHAT
    a._utility_cfg = {}
    monkeypatch.setattr(a, "_refresh_utility_cfg", lambda: None)
    with a._using_utility_model("screen_enrich"):
        assert a._model == CHAT[2], "未配置时不应改动"
    assert a._model == CHAT[2]


def test_missing_attr_is_safe(monkeypatch):
    """对象没有 _utility_cfg 属性（老实例）也不能炸。"""
    from core.harness_adapter import HanakoPetAdapter

    a = HanakoPetAdapter.__new__(HanakoPetAdapter)
    a._base_url, a._api_key, a._model = CHAT
    # 故意不设 _utility_cfg
    monkeypatch.setattr(a, "_refresh_utility_cfg", lambda: None)
    with a._using_utility_model("screen_enrich"):
        assert a._model == CHAT[2]


def test_unknown_source_not_switched(adapter):
    """未知来源不切（白名单制，避免新来源被误切）。"""
    with adapter._using_utility_model("something_new"):
        assert adapter._model == CHAT[2]


def test_partial_utility_cfg_fills_gaps(adapter):
    """utility 配置只给了 model 时，base_url/api_key 沿用对话的。"""
    adapter._utility_cfg = {"model": "cheap-model"}
    with adapter._using_utility_model("idle"):
        assert adapter._model == "cheap-model"
        assert adapter._base_url == CHAT[0], "缺 base_url 时应沿用"
        assert adapter._api_key == CHAT[1], "缺 api_key 时应沿用"


# ── 动态刷新：用户改完 Hana 设置页应立即生效，不用重启桌宠 ──

def test_refresh_picks_up_preference_change(tmp_path, monkeypatch):
    """关键：改 preferences.json 后，下一次调用就生效（mtime 失效检测）。

    视觉配置（screen.py）是每次截屏都读，用户改完立即生效；
    utility 若只在 __init__ 读一次，改完要重启桌宠 —— 两条链行为不一致，
    用户会以为“又改了没反应”。本用例钉死动态性。
    """
    import importlib
    import json

    import env_config
    from core.harness_adapter import HanakoPetAdapter

    ec = importlib.reload(env_config)
    home = tmp_path / ".hanako"
    (home / "user").mkdir(parents=True)
    pref = home / "user" / "preferences.json"
    (home / "provider-catalog.json").write_text(
        json.dumps({"providers": {
            "ds": {"base_url": "https://api.deepseek.com/v1", "api_key": "k"},
            "other": {"base_url": "https://other.example/v1", "api_key": "k2"},
        }}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("HANA_HOME", str(tmp_path / ".hanako"))

    a = HanakoPetAdapter.__new__(HanakoPetAdapter)
    a._base_url, a._api_key, a._model = CHAT
    a._utility_cfg = {}
    a._utility_cfg_mtime = "__unset__"

    # 初始：utility 指向 deepseek
    pref.write_text(json.dumps(
        {"utility_model": {"id": "deepseek-v4-flash", "provider": "ds"}}),
        encoding="utf-8")
    a._refresh_utility_cfg()
    assert a._utility_cfg.get("model") == "deepseek-v4-flash"

    # 用户改设置页 → 换成 other 家的模型
    import time
    time.sleep(0.01)  # 确保 mtime 变化
    pref.write_text(json.dumps(
        {"utility_model": {"id": "other-cheap", "provider": "other"}}),
        encoding="utf-8")
    a._refresh_utility_cfg()
    assert a._utility_cfg.get("model") == "other-cheap", "改完应立即生效"
    assert a._utility_cfg.get("base_url") == "https://other.example/v1"


def test_refresh_skips_when_unchanged(tmp_path, monkeypatch):
    """文件未变 → 不重复读盘（mtime 相同直接返回）。"""
    import importlib
    import json

    import env_config
    from core.harness_adapter import HanakoPetAdapter

    ec = importlib.reload(env_config)
    home = tmp_path / ".hanako"
    (home / "user").mkdir(parents=True)
    (home / "user" / "preferences.json").write_text(
        json.dumps({"utility_model": {"id": "m1", "provider": "ds"}}),
        encoding="utf-8")
    (home / "provider-catalog.json").write_text(
        json.dumps({"providers": {"ds": {"base_url": "u", "api_key": "k"}}}),
        encoding="utf-8")
    monkeypatch.setenv("HANA_HOME", str(tmp_path / ".hanako"))

    a = HanakoPetAdapter.__new__(HanakoPetAdapter)
    a._base_url, a._api_key, a._model = CHAT
    a._utility_cfg = {}
    a._utility_cfg_mtime = "__unset__"
    a._refresh_utility_cfg()
    first = dict(a._utility_cfg)

    # 手动篡改缓存值；若真的重读，会被文件内容覆盖回来
    a._utility_cfg = {"model": "SENTINEL"}
    a._refresh_utility_cfg()
    assert a._utility_cfg.get("model") == "SENTINEL", (
        "mtime 未变时不应重读（缓存被覆盖说明每次都读盘了）"
    )
    assert first.get("model") == "m1"


# ── env_config 侧：读 Hana preferences 的 utility_model ──

def test_get_utility_config_reads_preferences(tmp_path, monkeypatch):
    """utility_model 应从 preferences.json 读出并解析成完整配置。"""
    import importlib
    import json

    import env_config

    ec = importlib.reload(env_config)
    home = tmp_path / ".hanako"
    (home / "user").mkdir(parents=True)
    (home / "user" / "preferences.json").write_text(
        json.dumps(
            {"utility_model": {"id": "deepseek-v4-flash", "provider": "ds"}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (home / "provider-catalog.json").write_text(
        json.dumps(
            {"providers": {"ds": {"base_url": "https://api.deepseek.com/v1",
                                  "api_key": "sk-ds"}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HANA_HOME", str(tmp_path / ".hanako"))

    cfg = ec.get_utility_config()
    assert cfg["model"] == "deepseek-v4-flash"
    assert cfg["base_url"] == "https://api.deepseek.com/v1"


def test_get_utility_config_empty_when_unset(tmp_path, monkeypatch):
    """preferences 里没有 utility_model → 空 dict（调用方回退对话模型）。"""
    import importlib
    import json

    import env_config

    ec = importlib.reload(env_config)
    home = tmp_path / ".hanako"
    (home / "user").mkdir(parents=True)
    (home / "user" / "preferences.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HANA_HOME", str(tmp_path / ".hanako"))
    monkeypatch.delenv("UTILITY_BASE_URL", raising=False)
    monkeypatch.delenv("UTILITY_API_KEY", raising=False)

    assert ec.get_utility_config() == {}
