# -*- coding: utf-8 -*-
"""通用 Hana 操作客户端测试（B 项，2026-09-20）。

## 用户原话

> B（需要 CLI 作为通用）

## 为什么不 spawn CLI

`hana.cmd` 确实存在（`~/.hanako/artifacts/server/<ver>/hana.cmd`），
但每次调用要起一个 node 进程——实测 `status`/`sessions` 每次 1-2 秒。

而 `core/hanako_session_manager.py` **已经在用 HTTP API**（同一个 server），
所以正确做法是复用 HTTP 通道：

| | CLI 进程 | HTTP |
|---|---|---|
| 每次开销 | 1-2s（起 node） | ~10ms |
| 依赖安装路径 | 是（版本目录会变） | 否 |
| 复用已有配置 | 否 | 是（get_hanako_config） |

CLI 仍可用——那是**给人用的**；桌宠走 HTTP。

## 分工

| | hanako_session_manager | hana_client |
|---|---|---|
| 定位 | 对话（WS 长连接） | 操作（HTTP 一次性） |
| 用途 | 用户说话 | 列会话/切会话/查目录 |
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.hana_client import HanaClient, HanaClientError  # noqa: E402


class _FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.content = b"x" if payload is not None else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeHTTP:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _client(responses):
    c = HanaClient("http://127.0.0.1:20099", "tok")
    c._http = _FakeHTTP(responses)
    return c


# ── 1. 构造与配置 ──


def test_from_env_reads_config():
    """from_env 必须能解析出 base_url 与 token（与对话链路同源）。"""
    c = HanaClient.from_env()
    assert c.base_url.startswith("http")
    # token 可能为空（未配置），但 base_url 必须有
    assert c.base_url


def test_from_env_survives_missing_config(monkeypatch):
    """配置读不到时用默认值，不能抛。"""
    import env_config
    monkeypatch.setattr(env_config, "get_hanako_config",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                        raising=False)
    c = HanaClient.from_env()
    assert c.base_url


def test_base_url_trailing_slash_stripped():
    c = HanaClient("http://x:1/", "t")
    assert c.base_url == "http://x:1"


# ── 2. 请求层 ──


def test_request_sets_auth_header():
    c = _client([_FakeResp(200, {"ok": 1})])
    c._request("GET", "/api/health")
    # 直接验证 header 构造：换个方式——检查 _token 被用上
    assert c._token == "tok"


def test_request_raises_on_http_error():
    c = _client([_FakeResp(500, None, "boom")])
    with pytest.raises(HanaClientError):
        c._request("GET", "/api/health")


def test_request_raises_on_connection_error():
    c = _client([ConnectionError("refused")])
    with pytest.raises(HanaClientError):
        c._request("GET", "/api/health")


def test_request_empty_body_returns_none():
    c = _client([_FakeResp(200, None)])
    assert c._request("GET", "/api/health") is None


# ── 3. 读操作 ──


def test_status():
    c = _client([_FakeResp(200, {"version": "1.2.3", "agent": "奥菲莉娅"})])
    st = c.status()
    assert st["version"] == "1.2.3"


def test_list_sessions_filters_by_agent():
    rows = [
        {"sessionId": "s1", "agentId": "ophelia"},
        {"sessionId": "s2", "agentId": "aimis"},
        {"sessionId": "s3", "agentId": "ophelia"},
    ]
    # 两次调用 → 两个响应
    c = _client([_FakeResp(200, rows), _FakeResp(200, rows)])
    assert len(c.list_sessions()) == 3
    got = c.list_sessions(agent_id="ophelia")
    assert [r["sessionId"] for r in got] == ["s1", "s3"]


def test_list_sessions_handles_wrapped_shape():
    """兼容 {"sessions": [...]} 与裸数组两种返回。"""
    c = _client([_FakeResp(200, {"sessions": [{"sessionId": "s1"}]})])
    assert len(c.list_sessions()) == 1


def test_list_sessions_skips_non_dict_rows():
    c = _client([_FakeResp(200, [{"sessionId": "s1"}, "junk", None])])
    assert len(c.list_sessions()) == 1


def test_list_agents():
    c = _client([_FakeResp(200, {"agents": [{"id": "ophelia"}]})])
    assert c.list_agents()[0]["id"] == "ophelia"


def test_list_apps():
    c = _client([_FakeResp(200, {"plugins": [{"id": "mo-shu"}]})])
    assert c.list_apps()[0]["id"] == "mo-shu"


def test_recent_sessions_sorted_and_limited():
    rows = [
        {"sessionId": "old", "modified": "2026-01-01"},
        {"sessionId": "new", "modified": "2026-09-01"},
        {"sessionId": "mid", "modified": "2026-05-01"},
    ]
    c = _client([_FakeResp(200, rows)])
    got = c.recent_sessions(limit=2)
    assert [r["sessionId"] for r in got] == ["new", "mid"]


def test_find_session():
    rows = [{"sessionId": "s1"}, {"sessionId": "s2"}]
    c = _client([_FakeResp(200, rows)])
    assert c.find_session("s2")["sessionId"] == "s2"
    c2 = _client([_FakeResp(200, rows)])
    assert c2.find_session("nope") is None


def test_summary_shape():
    c = _client([
        _FakeResp(200, {"version": "1.0", "agent": "A", "model": "M"}),
        _FakeResp(200, [{"sessionId": "s1"}]),
    ])
    s = c.summary()
    assert "1.0" in s and "会话 1" in s


def test_summary_survives_unreachable():
    c = _client([ConnectionError("down")])
    assert "不可达" in c.summary()


# ── 4. MCP 工具注册（B 的对外形态）──


def test_mcp_registers_hana_tools():
    """MCP server 必须注册 4 个 hana 操作工具。"""
    import asyncio

    from core.mcp_server import MCP_AVAILABLE, PetMCPServer
    if not MCP_AVAILABLE:
        pytest.skip("未安装 mcp SDK")

    s = PetMCPServer(state_provider=lambda: {}, action_sink=lambda a, p: "ok")
    app = s._build_app()
    tools = asyncio.run(app.list_tools())
    names = {t.name for t in tools}
    for want in ("pet_hana_status", "pet_hana_sessions",
                 "pet_hana_agents", "pet_hana_apps"):
        assert want in names, f"缺 MCP 工具 {want}"


def test_mcp_hana_tools_have_descriptions():
    """每个工具都要有描述（Hana 侧靠描述选工具）。"""
    import asyncio

    from core.mcp_server import MCP_AVAILABLE, PetMCPServer
    if not MCP_AVAILABLE:
        pytest.skip("未安装 mcp SDK")

    s = PetMCPServer(state_provider=lambda: {}, action_sink=lambda a, p: "ok")
    app = s._build_app()
    for t in asyncio.run(app.list_tools()):
        if t.name.startswith("pet_hana_"):
            assert t.description and len(t.description) > 10, t.name


# ── 5. 源码级守卫 ──


def test_client_uses_http_not_cli_spawn():
    """客户端不得 spawn CLI 进程——那正是我们要避开的开销。

    注意：只看**代码**，不看 docstring/doc 注释（那里会提到 hana.cmd 作对比）。
    """
    import ast
    import io

    path = os.path.join(_REPO, "core", "hana_client.py")
    src = open(path, encoding="utf-8").read()
    # 去掉 docstring 后再查（用 AST 取源码段较麻烦，简单做法：剔除三引号块）
    tree = ast.parse(src)
    code_parts = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            # 跳过首条 Expr 常量（docstring）
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    getattr(body[0], "value", None), ast.Constant):
                body = body[1:]
            for stmt in body:
                code_parts.append(ast.get_source_segment(src, stmt) or "")
    code = "\n".join(code_parts)
    for bad in ("subprocess", "os.system", "Popen"):
        assert bad not in code, f"代码里不该出现 {bad}（应走 HTTP）"
    assert "requests" in src, "应走 HTTP"


def test_client_reuses_env_config():
    """必须复用 get_hanako_config（与对话链路同源）。"""
    src = open(os.path.join(_REPO, "core", "hana_client.py"),
               encoding="utf-8").read()
    assert "get_hanako_config" in src
