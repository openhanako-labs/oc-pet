"""tests/test_computer_use.py — computer-use 桥接与 MCP 工具注册。

覆盖：
  - ComputerUseBridge 的驱动解析、写操作门控、缺驱动时的异常降级
  - build_bridge 的启停（enabled=false → None）
  - PetMCPServer 在提供 computer_provider 时注册计算机工具；未提供时不注册
全部不依赖真实 cua-driver（用不存在的显式路径 / 假 bridge 保证确定性）。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.computer_use_bridge import ComputerUseBridge, build_bridge, resolve_driver_path
from core.mcp_server import MCP_AVAILABLE, PetMCPServer, build_from_config

BOGUS = os.path.join("Z:", "definitely", "missing", "cua-driver.exe")

EXPECTED_CU_TOOLS = {
    "pet_computer_status", "pet_computer_apps", "pet_computer_windows",
    "pet_computer_window_state", "pet_computer_launch", "pet_computer_click",
    "pet_computer_type", "pet_computer_key",
}
BASE_TOOLS = {
    "pet_state", "pet_capabilities", "pet_hana_catalog", "pet_set_emotion",
    "pet_play_anim", "pet_expression", "pet_say", "pet_celebrate", "pet_reset_idle",
}


# ── bridge 解析 / 启停 ──

def test_build_bridge_disabled_returns_none():
    assert build_bridge({"computer_use": {"enabled": False}}) is None
    assert build_bridge({}) is None


def test_build_bridge_enabled_returns_bridge():
    b = build_bridge({"computer_use": {"enabled": True, "driver_path": BOGUS}})
    assert isinstance(b, ComputerUseBridge)


def test_resolve_explicit_missing_path_is_none():
    # 显式路径不存在时不得回退到 PATH（保证确定性）
    assert resolve_driver_path({"driver_path": BOGUS}) is None


def test_missing_driver_reports_unavailable():
    b = ComputerUseBridge({"driver_path": BOGUS})
    st = b.status()
    assert st["available"] is False
    assert st["allow_actions"] is False


# ── 写操作门控 ──

def test_write_denied_when_allow_actions_false():
    b = ComputerUseBridge({"driver_path": BOGUS, "allow_actions": False})
    for r in (
        b.launch(name="Calculator"),
        b.click(pid=1, element_token="s1:2"),
        b.type_text(text="hi"),
        b.press_key(key="Return"),
    ):
        assert "error" in r and "写操作已禁用" in r["error"]


def test_write_requires_allow_actions_even_with_driver():
    # allow_actions=True 但驱动缺失 → 走真实调用路径并降级为 error（不抛）
    b = ComputerUseBridge({"driver_path": BOGUS, "allow_actions": True})
    r = b.launch(name="Calculator")
    assert "error" in r


def test_click_requires_target():
    b = ComputerUseBridge({"driver_path": BOGUS, "allow_actions": True})
    r = b.click(pid=1)
    assert "error" in r and "element_token" in r["error"]


def test_call_json_graceful_without_driver():
    b = ComputerUseBridge({"driver_path": BOGUS})
    r = b.list_apps()
    assert isinstance(r, dict) and "error" in r


# ── MCP 工具注册 ──

def _server(**kw):
    kw.setdefault("state_provider", lambda: {"ok": True})
    kw.setdefault("capabilities_provider", lambda: [])
    kw.setdefault("action_sink", lambda a, p: "ok")
    return PetMCPServer(**kw)


@pytest.mark.skipif(not MCP_AVAILABLE, reason="未安装 mcp SDK")
def test_tools_without_computer_provider():
    app = _server()._build_app()
    names = {t.name for t in asyncio.run(app.list_tools())}
    assert BASE_TOOLS <= names
    assert not (EXPECTED_CU_TOOLS & names), "未接 computer_provider 时不应注册计算机工具"


@pytest.mark.skipif(not MCP_AVAILABLE, reason="未安装 mcp SDK")
def test_tools_with_computer_provider():
    calls = []

    def provider(op, params):
        calls.append((op, params))
        return {"ok": op}

    app = _server(computer_provider=provider)._build_app()
    names = {t.name for t in asyncio.run(app.list_tools())}
    assert BASE_TOOLS <= names
    assert EXPECTED_CU_TOOLS <= names, f"缺: {EXPECTED_CU_TOOLS - names}"


@pytest.mark.skipif(not MCP_AVAILABLE, reason="未安装 mcp SDK")
def test_computer_tools_have_descriptions():
    app = _server(computer_provider=lambda op, p: {"ok": True})._build_app()
    for t in asyncio.run(app.list_tools()):
        if t.name.startswith("pet_computer_"):
            assert t.description and len(t.description) > 10, t.name


@pytest.mark.skipif(not MCP_AVAILABLE, reason="未安装 mcp SDK")
def test_safe_computer_degrades_on_provider_error():
    def boom(op, params):
        raise RuntimeError("driver exploded")

    s = _server(computer_provider=boom)
    r = s._safe_computer("status", {})
    assert "error" in r and "exploded" in r["error"]


@pytest.mark.skipif(not MCP_AVAILABLE, reason="未安装 mcp SDK")
def test_safe_computer_without_provider():
    r = _server()._safe_computer("status", {})
    assert "error" in r


# ── build_from_config 贯通 ──

def test_build_from_config_disabled_mcp_returns_none():
    assert build_from_config({"mcp_server": {"enabled": False}}, state_provider=lambda: {}) is None


@pytest.mark.skipif(not MCP_AVAILABLE, reason="未安装 mcp SDK")
def test_build_from_config_wires_computer_use():
    cfg = {
        "mcp_server": {"enabled": True, "port": 0, "allow_actions": False},
        "computer_use": {"enabled": True, "driver_path": BOGUS},
    }
    srv = build_from_config(cfg, state_provider=lambda: {"ok": True})
    assert srv is not None
    names = {t.name for t in asyncio.run(srv._build_app().list_tools())}
    assert EXPECTED_CU_TOOLS <= names
