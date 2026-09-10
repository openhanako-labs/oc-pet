# -*- coding: utf-8 -*-
"""BugFix #6-A 单测：Obsidian 日记目录配置化（perception.obsidian_diary_dir）。

优先级：显式 output_dir > config.perception.obsidian_diary_dir >
环境变量 OC_PET_OBSIDIAN_DIR > 内置默认路径。
config 缺 perception 键 / 该项为空 → 安全回退默认，不抛异常。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.perception.controller import (
    PerceptionController,
    DEFAULT_OBSIDIAN_DIARY_DIR,
)


def _make_ctrl(config):
    """绕过重量级 __init__（会起网络/线程），仅注入 _config 测试解析逻辑。"""
    ctrl = object.__new__(PerceptionController)
    ctrl._config = config
    return ctrl


def test_resolve_default_when_empty(monkeypatch):
    monkeypatch.delenv("OC_PET_OBSIDIAN_DIR", raising=False)
    ctrl = _make_ctrl({})
    assert ctrl._resolve_obsidian_diary_dir() == DEFAULT_OBSIDIAN_DIARY_DIR


def test_resolve_from_config(monkeypatch):
    monkeypatch.delenv("OC_PET_OBSIDIAN_DIR", raising=False)
    ctrl = _make_ctrl({"perception": {"obsidian_diary_dir": "/cfg/diary"}})
    assert ctrl._resolve_obsidian_diary_dir() == "/cfg/diary"


def test_resolve_from_env(monkeypatch):
    monkeypatch.setenv("OC_PET_OBSIDIAN_DIR", "/env/diary")
    ctrl = _make_ctrl({})
    assert ctrl._resolve_obsidian_diary_dir() == "/env/diary"


def test_resolve_output_arg_wins(monkeypatch):
    monkeypatch.setenv("OC_PET_OBSIDIAN_DIR", "/env/diary")
    ctrl = _make_ctrl({"perception": {"obsidian_diary_dir": "/cfg/diary"}})
    assert ctrl._resolve_obsidian_diary_dir("/explicit") == "/explicit"


def test_resolve_missing_perception_key_no_error(monkeypatch):
    monkeypatch.delenv("OC_PET_OBSIDIAN_DIR", raising=False)
    # config 缺 perception 键 → 回退默认，不抛异常
    ctrl = _make_ctrl({"screen": {}})
    assert ctrl._resolve_obsidian_diary_dir() == DEFAULT_OBSIDIAN_DIARY_DIR


def test_resolve_empty_config_value_falls_back(monkeypatch):
    monkeypatch.delenv("OC_PET_OBSIDIAN_DIR", raising=False)
    # perception.obsidian_diary_dir 为空串 → 回退默认
    ctrl = _make_ctrl({"perception": {"obsidian_diary_dir": ""}})
    assert ctrl._resolve_obsidian_diary_dir() == DEFAULT_OBSIDIAN_DIARY_DIR
