# -*- coding: utf-8 -*-
"""回归：视觉模型配置的解析链。

背景（2026-09-17 核实）：
  - `get_vision_config()` 原先**硬编码只读 catalog 的 agnes provider**，
    用户无法指定视觉模型（catalog 里有 23 个 provider 却用不上）。
  - `ScreenPerception` 创建时不传 agent_id，导致内部
    `HanakoContext()` 用默认值 `yuexinmiao` —— 读的是别人的配置。

修复：三级优先级
  1. .env 的 VISION_*（显式覆盖）
  2. **agent config.yaml 的 models.vision**（新增；与 models.chat 同级）
  3. catalog 的 agnes（历史默认）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _reload_env_config():
    import importlib

    import env_config

    return importlib.reload(env_config)


def test_vision_config_accepts_agent_id():
    """签名必须接受 agent_id（新增参数，向后兼容无参调用）。"""
    import inspect

    import env_config

    sig = inspect.signature(env_config.get_vision_config)
    assert "agent_id" in sig.parameters, "get_vision_config 必须支持 agent_id"
    # 无参调用仍合法（旧调用方不受影响）
    assert sig.parameters["agent_id"].default == ""


def test_agent_model_config_reads_vision_slot(tmp_path, monkeypatch):
    """agent config.yaml 的 models.vision 能被解析成完整配置。"""
    ec = _reload_env_config()

    home = tmp_path / ".hanako"
    (home / "agents" / "testagent").mkdir(parents=True)
    (home / "agents" / "testagent" / "config.yaml").write_text(
        "models:\n"
        "  chat:\n"
        "    id: chat-model\n"
        "    provider: p1\n"
        "  vision:\n"
        "    id: vision-model\n"
        "    provider: p2\n",
        encoding="utf-8",
    )
    (home / "provider-catalog.json").write_text(
        '{"providers": {'
        '"p1": {"base_url": "https://p1.example/v1", "api_key": "k1"},'
        '"p2": {"base_url": "https://p2.example/v1", "api_key": "k2"}'
        "}}",
        encoding="utf-8",
    )
    monkeypatch.setattr(ec.Path, "home", staticmethod(lambda: tmp_path))

    vision = ec._read_agent_model_config("testagent", "vision")
    assert vision["model"] == "vision-model", f"应读 vision 槽: {vision!r}"
    assert vision["base_url"] == "https://p2.example/v1"
    assert vision["api_key"] == "k2"

    # 对照：chat 槽读到的应是另一个 provider
    chat = ec._read_agent_model_config("testagent", "chat")
    assert chat["model"] == "chat-model"
    assert chat["base_url"] == "https://p1.example/v1"


def test_agent_model_config_missing_returns_empty(tmp_path, monkeypatch):
    """未配置 models.vision / agent 不存在 / provider 不在 catalog → 空 dict。"""
    ec = _reload_env_config()
    home = tmp_path / ".hanako"
    (home / "agents" / "noagent").mkdir(parents=True)
    (home / "agents" / "noagent" / "config.yaml").write_text(
        "models:\n  chat:\n    id: x\n    provider: p1\n", encoding="utf-8"
    )
    (home / "provider-catalog.json").write_text('{"providers": {}}', encoding="utf-8")
    monkeypatch.setattr(ec.Path, "home", staticmethod(lambda: tmp_path))

    assert ec._read_agent_model_config("noagent", "vision") == {}, "无 vision 槽应返回空"
    assert ec._read_agent_model_config("ghost", "vision") == {}, "agent 不存在应返回空"
    assert ec._read_agent_model_config("", "vision") == {}, "空 agent_id 应返回空"


def test_vision_config_falls_back_without_agent():
    """未传 agent_id 时仍能拿到配置（回退链不因新参数断裂）。"""
    import env_config

    cfg = env_config.get_vision_config()
    # 本机配了 agnes → 应有值；若未来清空 catalog 也允许为空
    if cfg:
        assert "base_url" in cfg and "api_key" in cfg


def test_screen_perception_accepts_agent_id():
    """ScreenPerception 必须接受 agent_id 并记住它。"""
    import inspect

    from core.perception.screen import ScreenPerception

    sig = inspect.signature(ScreenPerception.__init__)
    assert "agent_id" in sig.parameters, "ScreenPerception 必须支持 agent_id"

    sp = ScreenPerception(agent_id="ophelia")
    assert sp._agent_id == "ophelia"
    # 无参调用向后兼容
    assert ScreenPerception()._agent_id == ""


def test_controller_passes_agent_to_screen():
    """PerceptionController 创建 ScreenPerception 时必须传 hanako agent。

    原缺陷：`ScreenPerception()` 不传参 → 内部 `HanakoContext()` 用默认
    yuexinmiao → 读别人的配置。
    """
    import inspect

    from core.perception import controller as ctrl_mod

    src = inspect.getsource(ctrl_mod.PerceptionController.__init__)
    assert "ScreenPerception(agent_id=" in src, (
        "创建 ScreenPerception 必须传 agent_id，否则回退到默认 agent 的配置"
    )
