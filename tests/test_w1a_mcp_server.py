"""W1a（2026-09-14）测试：桌宠作为 MCP 提供方。

覆盖：
  - PetMCPServer 构建与工具注册
  - 读工具（pet_state / pet_capabilities）
  - 写工具（动作白名单、只读模式、action_sink 派发）
  - 失败闭合（provider 抛异常不崩）
  - build_from_config 的启用/关闭
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.mcp_server import (  # noqa: E402
    ACTION_WHITELIST,
    DEFAULT_PORT,
    MCP_AVAILABLE,
    PetMCPServer,
    build_from_config,
)


# ── 桩 ──


def _state_provider():
    return {"state": "idle", "emotion": "happy", "agent_id": "ophelia"}


def _caps_provider():
    return [{"name": "daily_diary", "description": "生成今日日报"}]


class _Sink:
    def __init__(self):
        self.calls = []

    def __call__(self, action, params):
        self.calls.append((action, params))
        return f"已派发: {action}"


def _server(**kw):
    kw.setdefault("state_provider", _state_provider)
    kw.setdefault("capabilities_provider", _caps_provider)
    kw.setdefault("action_sink", _Sink())
    return PetMCPServer(**kw)


def test_default_port_is_8979():
    """端口不能撞 8977/8988/8077/8889。"""
    assert DEFAULT_PORT == 8979


def test_url_format():
    s = _server(port=8979)
    assert s.url() == "http://127.0.0.1:8979/mcp"


def test_action_whitelist_is_restrictive():
    """白名单只放表现类动作，不放配置/行为修改。"""
    assert "set_emotion" in ACTION_WHITELIST
    assert "say" in ACTION_WHITELIST
    # 不该出现的东西
    for forbidden in ("set_config", "restart", "shutdown", "exec", "shell"):
        assert forbidden not in ACTION_WHITELIST


# ── 读路径 ──


def test_safe_state_returns_dict():
    s = _server()
    assert s._safe_state()["state"] == "idle"


def test_safe_state_survives_provider_exception():
    """provider 抛异常时必须降级返回 error，不能崩 MCP 线程。"""

    def boom():
        raise RuntimeError("perception dead")

    s = _server(state_provider=boom)
    st = s._safe_state()
    assert "error" in st


def test_safe_state_handles_non_dict():
    s = _server(state_provider=lambda: "not a dict")
    assert "error" in s._safe_state()


def test_safe_capabilities_returns_list():
    s = _server()
    caps = s._safe_capabilities()
    assert len(caps) == 1 and caps[0]["name"] == "daily_diary"


def test_safe_capabilities_survives_exception():
    def boom():
        raise RuntimeError("caps dead")

    s = _server(capabilities_provider=boom)
    assert s._safe_capabilities() == []


def test_safe_capabilities_without_provider():
    s = _server(capabilities_provider=None)
    assert s._safe_capabilities() == []


# ── 写路径 ──


def test_dispatch_allowed_action_reaches_sink():
    sink = _Sink()
    s = _server(action_sink=sink)
    msg = s._dispatch_action("set_emotion", {"emotion": "happy"})
    assert "已派发" in msg
    assert sink.calls == [("set_emotion", {"emotion": "happy"})]


def test_dispatch_rejects_non_whitelisted_action():
    sink = _Sink()
    s = _server(action_sink=sink)
    msg = s._dispatch_action("rm_rf", {})
    assert "白名单" in msg
    assert sink.calls == []  # 未派发


def test_dispatch_blocked_in_readonly_mode():
    sink = _Sink()
    s = _server(action_sink=sink, allow_actions=False)
    msg = s._dispatch_action("set_emotion", {"emotion": "happy"})
    assert "只读" in msg
    assert sink.calls == []


def test_dispatch_without_sink():
    s = _server(action_sink=None)
    msg = s._dispatch_action("set_emotion", {"emotion": "happy"})
    assert "未接入" in msg


def test_dispatch_survives_sink_exception():
    def boom(action, params):
        raise RuntimeError("sink dead")

    s = _server(action_sink=boom)
    msg = s._dispatch_action("say", {"text": "hi"})
    assert "失败" in msg


def test_dispatch_copies_params():
    """派发时传副本，避免调用方后续修改污染。"""
    received = {}

    def sink(action, params):
        received["p"] = params
        return "ok"

    s = _server(action_sink=sink)
    original = {"emotion": "happy"}
    s._dispatch_action("set_emotion", original)
    original["emotion"] = "MUTATED"
    assert received["p"]["emotion"] == "happy"


# ── app 构建 ──


@pytest.mark.skipif(not MCP_AVAILABLE, reason="未安装 mcp SDK")
def test_build_app_registers_expected_tools():
    import asyncio

    s = _server()
    app = s._build_app()
    tools = asyncio.run(app.list_tools())
    names = {t.name for t in tools}
    expected = {
        "pet_state", "pet_capabilities", "pet_set_emotion",
        "pet_play_anim", "pet_expression", "pet_say",
        "pet_celebrate", "pet_reset_idle",
    }
    assert expected <= names, f"缺少工具: {expected - names}"


@pytest.mark.skipif(not MCP_AVAILABLE, reason="未安装 mcp SDK")
def test_build_app_tools_have_descriptions():
    """每个工具都要有描述（Hana 侧靠描述选工具）。"""
    import asyncio

    s = _server()
    app = s._build_app()
    tools = asyncio.run(app.list_tools())
    for t in tools:
        assert t.description and len(t.description) > 10, t.name


# ── config 构建 ──


def test_build_from_config_disabled_returns_none():
    assert build_from_config({"mcp_server": {"enabled": False}},
                             _state_provider) is None


def test_build_from_config_missing_block_returns_none():
    assert build_from_config({}, _state_provider) is None


def test_build_from_config_enabled_builds_server():
    s = build_from_config(
        {"mcp_server": {"enabled": True, "port": 8979}},
        _state_provider, _caps_provider, _Sink(),
    )
    assert s is not None
    assert s.port == 8979
    assert s.allow_actions is True


def test_build_from_config_bad_port_falls_back():
    s = build_from_config(
        {"mcp_server": {"enabled": True, "port": "abc"}},
        _state_provider,
    )
    assert s is not None
    assert s.port == DEFAULT_PORT


def test_build_from_config_readonly_flag():
    s = build_from_config(
        {"mcp_server": {"enabled": True, "allow_actions": False}},
        _state_provider,
    )
    assert s is not None and s.allow_actions is False


# ── DISC-2：Hana 目录查询 ──


def test_safe_catalog_without_provider():
    s = _server()
    r = s._safe_catalog("summary")
    assert "error" in r


def test_safe_catalog_returns_provider_result():
    s = _server(catalog_provider=lambda sys_: {"totals": {"plugins": 26}})
    assert s._safe_catalog("summary")["totals"]["plugins"] == 26


def test_safe_catalog_survives_exception():
    def boom(_):
        raise RuntimeError("catalog dead")

    s = _server(catalog_provider=boom)
    assert "error" in s._safe_catalog("summary")


def test_safe_catalog_handles_non_dict():
    s = _server(catalog_provider=lambda _: "not a dict")
    assert "error" in s._safe_catalog("summary")


@pytest.mark.skipif(not MCP_AVAILABLE, reason="未安装 mcp SDK")
def test_catalog_tool_registered():
    import asyncio

    s = _server(catalog_provider=lambda sys_: {"ok": True})
    app = s._build_app()
    tools = asyncio.run(app.list_tools())
    assert "pet_hana_catalog" in {t.name for t in tools}


@pytest.mark.skipif(not MCP_AVAILABLE, reason="未安装 mcp SDK")
def test_build_from_config_passes_catalog_provider():
    s = build_from_config(
        {"mcp_server": {"enabled": True}},
        _state_provider, _caps_provider, _Sink(),
        lambda sys_: {"totals": {}},
    )
    assert s is not None
    assert s._safe_catalog("summary") == {"totals": {}}
