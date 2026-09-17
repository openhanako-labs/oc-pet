# -*- coding: utf-8 -*-
"""回归：ASR 配置应读 Hana 的 speechRecognition。

## 背景（2026-09-17 排查发现）

`get_asr_api_config()` 原先只读 `.env` 的 `ASR_*`，没有任何回退链。
而 Hana 设置页已有语音识别配置（实测）：

    "speechRecognition": {"enabled": true,
                          "defaultModel": {"provider": "openai",
                                           "id": "whisper-1"}}

用户在 Hana 侧换语音识别模型 → oc-pet 毫无反应（改了没反应）。

## 修法

优先级：.env ASR_* → Hana speechRecognition.defaultModel → 空
`speechRecognition.enabled=false` 视为未配置。
"""
from __future__ import annotations

import importlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CATALOG = {
    "openai": {"base_url": "https://api.openai.com/v1", "api_key": "sk-oa"},
    "agnes": {"base_url": "https://api.agnes-ai.cn/v1", "api_key": "sk-ag"},
}


def _setup(tmp_path, prefs: dict, catalog: dict = None):
    home = tmp_path / ".hanako"
    (home / "user").mkdir(parents=True, exist_ok=True)
    (home / "user" / "preferences.json").write_text(
        json.dumps(prefs, ensure_ascii=False), encoding="utf-8"
    )
    (home / "provider-catalog.json").write_text(
        json.dumps({"providers": catalog or CATALOG}, ensure_ascii=False),
        encoding="utf-8",
    )
    return home


def _ec(monkeypatch, tmp_path, prefs, catalog=None):
    ec = importlib.reload(importlib.import_module("env_config"))
    _setup(tmp_path, prefs, catalog)
    monkeypatch.setenv("HANA_HOME", str(tmp_path / ".hanako"))
    for k in ("ASR_BASE_URL", "ASR_API_KEY", "ASR_MODEL"):
        monkeypatch.delenv(k, raising=False)
    return ec


def test_reads_hana_speech_recognition(tmp_path, monkeypatch):
    """Hana 的 speechRecognition.defaultModel 应被读到。"""
    ec = _ec(monkeypatch, tmp_path, {
        "speechRecognition": {
            "enabled": True,
            "defaultModel": {"provider": "openai", "id": "whisper-1"},
        }
    })
    cfg = ec.get_asr_api_config()
    assert cfg["model"] == "whisper-1", f"应读 Hana 配置: {cfg!r}"
    assert cfg["base_url"] == "https://api.openai.com/v1"
    assert cfg["api_key"] == "sk-oa"


def test_env_overrides_hana(tmp_path, monkeypatch):
    """.env 显式配置优先（高级覆盖语义）。"""
    ec = _ec(monkeypatch, tmp_path, {
        "speechRecognition": {
            "enabled": True,
            "defaultModel": {"provider": "openai", "id": "whisper-1"},
        }
    })
    monkeypatch.setenv("ASR_BASE_URL", "https://my-asr.example/v1")
    monkeypatch.setenv("ASR_API_KEY", "sk-mine")
    monkeypatch.setenv("ASR_MODEL", "my-asr")
    cfg = ec.get_asr_api_config()
    assert cfg["model"] == "my-asr"
    assert cfg["base_url"] == "https://my-asr.example/v1"


def test_disabled_speech_recognition_ignored(tmp_path, monkeypatch):
    """enabled=false → 视为未配置（用户在 Hana 侧关掉了）。"""
    ec = _ec(monkeypatch, tmp_path, {
        "speechRecognition": {
            "enabled": False,
            "defaultModel": {"provider": "openai", "id": "whisper-1"},
        }
    })
    assert ec._read_hana_speech_recognition() == {}


def test_missing_field_returns_empty(tmp_path, monkeypatch):
    """没有 speechRecognition 字段 → 空 dict，不炸。"""
    ec = _ec(monkeypatch, tmp_path, {})
    assert ec._read_hana_speech_recognition() == {}


def test_unknown_provider_returns_empty(tmp_path, monkeypatch):
    """provider 不在 catalog → 空 dict（不返回半截配置）。"""
    ec = _ec(monkeypatch, tmp_path, {
        "speechRecognition": {
            "enabled": True,
            "defaultModel": {"provider": "ghost", "id": "m"},
        }
    })
    assert ec._read_hana_speech_recognition() == {}


def test_falls_back_to_default_model_name(tmp_path, monkeypatch):
    """全都没配 → 返回空 base_url，但 model 保留 whisper-1 默认。"""
    ec = _ec(monkeypatch, tmp_path, {})
    cfg = ec.get_asr_api_config()
    assert cfg["base_url"] == ""
    assert cfg["model"] == "whisper-1"
