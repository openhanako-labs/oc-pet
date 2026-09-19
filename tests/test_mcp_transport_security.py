# -*- coding: utf-8 -*-
"""MCP 传输安全（Origin / DNS-rebinding）单元测试 —— O2 的收束。

**前提核实结论**：MCP SDK 默认就开着这层保护，所以「缺 Origin 校验」不成立。
本文件守的是另一件事：**别让这个安全默认值静默消失**。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.mcp_server import (
    DEFAULT_ALLOWED_HOSTS,
    DEFAULT_ALLOWED_ORIGINS,
    build_transport_security,
    build_from_config,
)

pytest.importorskip("mcp.server.transport_security", reason="需要 MCP SDK")

ROOT = Path(__file__).resolve().parent.parent


# ── 默认值：只认本机，且不能是全通配 ──────────────────────


def test_default_enables_protection():
    sec = build_transport_security(None)
    assert sec is not None
    assert sec.enable_dns_rebinding_protection is True


def test_default_is_localhost_only():
    sec = build_transport_security({})
    assert list(sec.allowed_hosts) == list(DEFAULT_ALLOWED_HOSTS)
    assert list(sec.allowed_origins) == list(DEFAULT_ALLOWED_ORIGINS)


def test_default_never_uses_bare_wildcard():
    """`*` 单独出现就等于放行一切——这是安全回归的红线。"""
    sec = build_transport_security({})
    for entry in list(sec.allowed_hosts) + list(sec.allowed_origins):
        assert entry != "*", "出现裸通配，保护形同虚设"


def test_default_matches_sdk_default():
    """不能比 SDK 自带的默认更松（否则我们是“显式放松”而非“钉死”）。"""
    from mcp.server.fastmcp import FastMCP

    sdk = FastMCP("probe").settings.transport_security
    ours = build_transport_security({})
    assert ours.enable_dns_rebinding_protection == sdk.enable_dns_rebinding_protection
    assert list(ours.allowed_hosts) == list(sdk.allowed_hosts)
    assert list(ours.allowed_origins) == list(sdk.allowed_origins)


# ── 配置覆盖 ──────────────────────────────────────────────


def test_empty_lists_fall_back_to_defaults():
    """空列表不是“允许全部”，也不是“拒绝全部”——回退内置白名单。"""
    sec = build_transport_security({"allowed_hosts": [], "allowed_origins": []})
    assert list(sec.allowed_hosts) == list(DEFAULT_ALLOWED_HOSTS)


def test_blank_entries_are_ignored():
    sec = build_transport_security({"allowed_origins": ["  ", ""]})
    assert list(sec.allowed_origins) == list(DEFAULT_ALLOWED_ORIGINS)


def test_custom_lists_are_respected():
    sec = build_transport_security({
        "allowed_hosts": ["127.0.0.1:*"],
        "allowed_origins": ["http://127.0.0.1:5173"],
    })
    assert list(sec.allowed_hosts) == ["127.0.0.1:*"]
    assert list(sec.allowed_origins) == ["http://127.0.0.1:5173"]


def test_protection_can_be_disabled_explicitly():
    sec = build_transport_security({"dns_rebinding_protection": False})
    assert sec.enable_dns_rebinding_protection is False


def test_truthy_strings_do_not_silently_disable():
    """`"false"` 这种字符串不该被当成 True 或 False 的意外——按真值判，但要有测试钉住。"""
    sec = build_transport_security({"dns_rebinding_protection": "false"})
    assert sec.enable_dns_rebinding_protection is True   # 非空字符串 → 真；想关就写 false


# ── 接线 ──────────────────────────────────────────────────


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_build_app_passes_security_only_when_available():
    """⚠️ 不能无条件传 transport_security=None——那等于显式关掉 SDK 默认保护。"""
    s = _src("core/mcp_server.py")
    assert 'extra = {"transport_security": security} if security is not None else {}' in s
    assert "**extra" in s


def test_build_from_config_reads_flat_keys():
    srv = build_from_config(
        {"mcp_server": {"enabled": True, "port": 8979,
                        "allowed_origins": ["http://127.0.0.1:5173"],
                        "dns_rebinding_protection": False}},
        state_provider=lambda: {},
    )
    assert srv is not None
    assert srv._transport_security_cfg["dns_rebinding_protection"] is False
    assert srv._transport_security_cfg["allowed_origins"] == ["http://127.0.0.1:5173"]


def test_build_from_config_omits_unset_keys():
    srv = build_from_config({"mcp_server": {"enabled": True}},
                            state_provider=lambda: {})
    assert srv is not None
    assert srv._transport_security_cfg == {}


def test_build_from_config_still_returns_none_when_disabled():
    assert build_from_config({"mcp_server": {"enabled": False}},
                             state_provider=lambda: {}) is None


def test_config_templates_carry_security_keys():
    cfg = json.loads((ROOT / "config.template.json").read_text(encoding="utf-8"))
    blk = cfg["mcp_server"]
    assert blk.get("dns_rebinding_protection") is True
    assert blk.get("allowed_hosts") == [] and blk.get("allowed_origins") == []
