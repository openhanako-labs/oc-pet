# -*- coding: utf-8 -*-
"""回归：视觉模型配置以 Hana 设置页为单一来源。

## 背景（2026-09-17 用户实测）

用户在 Hana 设置页把「视觉辅助模型」选成 `agnes/agnes-2.5-flash`，
但 oc-pet 的屏幕感知仍用 `agnes-3.0-flash` —— 设置页改了毫无变化。

## 根因

两套字段，互不相通：

| | 字段 | 位置 |
|---|---|---|
| Hana 设置页写的 | `vision_model` | `~/.hanako/user/preferences.json` |
| oc-pet 读的 | `models.vision` | `~/.hanako/agents/<id>/config.yaml` |

Hana 的 UI 不知道 `models.vision` 这个 oc-pet 私有约定，
于是「用户能改的」和「程序读的」是两个地方。

## 修法

`get_vision_config()` 优先级重排，**Hana preferences 提到第一**：

    1. preferences.json 的 vision_model   ← 设置页那个下拉框（默认来源）
    2. .env 的 VISION_*                    ← 高级覆盖，排障用
    3. agent config.yaml 的 models.vision  ← 旧约定，兼容保留
    4. catalog 的 agnes                    ← 最终回退

`vision_auxiliary_enabled=false` 时视为未配置（用户在 Hana 侧关掉了）。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _reload_env_config():
    import importlib

    import env_config

    return importlib.reload(env_config)


def _setup_home(tmp_path, prefs: dict | None, catalog: dict | None = None):
    """搭一个假的 ~/.hanako 结构。"""
    home = tmp_path / ".hanako"
    (home / "user").mkdir(parents=True, exist_ok=True)
    if prefs is not None:
        (home / "user" / "preferences.json").write_text(
            json.dumps(prefs, ensure_ascii=False), encoding="utf-8"
        )
    if catalog is not None:
        (home / "provider-catalog.json").write_text(
            json.dumps({"providers": catalog}, ensure_ascii=False), encoding="utf-8"
        )
    return home


CATALOG = {
    "agnes": {"base_url": "https://api.agnes-ai.cn/v1", "api_key": "sk-agnes"},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-ds"},
}


def test_reads_hana_preferences_vision_model(tmp_path, monkeypatch):
    """preferences.json 的 vision_model 应被解析成完整配置。"""
    ec = _reload_env_config()
    _setup_home(
        tmp_path,
        {"vision_model": {"id": "agnes-2.5-flash", "provider": "agnes"},
         "vision_auxiliary_enabled": True},
        CATALOG,
    )
    monkeypatch.setattr(ec.Path, "home", staticmethod(lambda: tmp_path))

    cfg = ec._read_hana_preferences_vision()
    assert cfg["model"] == "agnes-2.5-flash", f"应读到设置页选定的模型: {cfg!r}"
    assert cfg["base_url"] == "https://api.agnes-ai.cn/v1"
    assert cfg["api_key"] == "sk-agnes"


def test_preferences_takes_priority_over_agent_config(tmp_path, monkeypatch):
    """关键：preferences 的优先级**高于** agent config.yaml 的 models.vision。

    这正是用户遇到的问题——设置页改了但程序读的是另一个字段。
    """
    ec = _reload_env_config()
    home = _setup_home(
        tmp_path,
        {"vision_model": {"id": "agnes-2.5-flash", "provider": "agnes"}},
        CATALOG,
    )
    # 同时放一份 agent config.yaml（旧的 models.vision）
    agent_dir = home / "agents" / "testagent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "config.yaml").write_text(
        "models:\n  vision:\n    id: agnes-3.0-flash\n    provider: agnes\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ec.Path, "home", staticmethod(lambda: tmp_path))

    cfg = ec.get_vision_config("testagent")
    assert cfg["model"] == "agnes-2.5-flash", (
        f"应优先用 Hana 设置页的模型（不是 agent config 的）: {cfg!r}"
    )


def test_disabled_aux_means_not_configured(tmp_path, monkeypatch):
    """vision_auxiliary_enabled=false → 视为未配置（回退下一级）。"""
    ec = _reload_env_config()
    _setup_home(
        tmp_path,
        {"vision_model": {"id": "agnes-2.5-flash", "provider": "agnes"},
         "vision_auxiliary_enabled": False},
        CATALOG,
    )
    monkeypatch.setattr(ec.Path, "home", staticmethod(lambda: tmp_path))

    assert ec._read_hana_preferences_vision() == {}, "关掉视觉辅助应返回空"


def test_missing_or_malformed_returns_empty(tmp_path, monkeypatch):
    """preferences 缺失/字段畸形 → 空 dict（调用方继续降级）。"""
    ec = _reload_env_config()
    monkeypatch.setattr(ec.Path, "home", staticmethod(lambda: tmp_path))

    # 完全无 preferences.json
    assert ec._read_hana_preferences_vision() == {}

    # vision_model 是字符串而非 dict
    _setup_home(tmp_path, {"vision_model": "agnes-2.5-flash"}, CATALOG)
    assert ec._read_hana_preferences_vision() == {}

    # 缺 provider
    _setup_home(tmp_path, {"vision_model": {"id": "x"}}, CATALOG)
    assert ec._read_hana_preferences_vision() == {}


def test_unknown_provider_returns_empty(tmp_path, monkeypatch):
    """provider 不在 catalog（无凭证）→ 空 dict，继续降级。"""
    ec = _reload_env_config()
    _setup_home(
        tmp_path,
        {"vision_model": {"id": "m", "provider": "ghost"}},
        CATALOG,
    )
    monkeypatch.setattr(ec.Path, "home", staticmethod(lambda: tmp_path))
    assert ec._read_hana_preferences_vision() == {}


def test_fallback_chain_still_works(tmp_path, monkeypatch):
    """preferences 无配置时，仍能回退到 catalog 的 agnes（不破坏原行为）。"""
    ec = _reload_env_config()
    _setup_home(tmp_path, {}, CATALOG)  # preferences 里没有 vision_model
    monkeypatch.setattr(ec.Path, "home", staticmethod(lambda: tmp_path))

    cfg = ec.get_vision_config("")
    assert cfg.get("model"), f"应回退到 catalog agnes: {cfg!r}"
    assert cfg["base_url"] == "https://api.agnes-ai.cn/v1"
