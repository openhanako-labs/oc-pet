# -*- coding: utf-8 -*-
"""oc-pet MCP 端点体检：握手 → 列工具 → 调一次 pet_state。

用途：Hana 连接器「刷新工具」刷不出东西时，先跑这个区分两种情况——

  * 端口没人听  => **桌宠没在运行**（最常见；服务端跑在桌宠进程里）
  * 端口在听但握手失败 => 服务端/协议/端口占用问题

只读探针：不触发截图、不改状态（只调 pet_state 这一个只读工具）。

用法：
    python tools/probe_mcp_endpoint.py [--port 8979]
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.error
import urllib.request


def port_open(host: str, port: int, timeout: float = 0.8) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _post(url: str, payload: dict, sid: str | None = None, timeout: float = 10.0):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if sid:
        req.add_header("Mcp-Session-Id", sid)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")


def _parse(body: str):
    """streamable-http 可能回 SSE（``data: {...}``）也可能回纯 JSON。"""
    body = (body or "").strip()
    if not body:
        return None
    if body.startswith("{") or body.startswith("["):
        try:
            return json.loads(body)
        except Exception:
            return None
    payloads = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try:
                payloads.append(json.loads(line[5:].strip()))
            except Exception:
                pass
    return payloads[-1] if payloads else None


def main() -> int:
    ap = argparse.ArgumentParser(description="oc-pet MCP 端点体检")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8979)
    ap.add_argument("--state-tool", default="pet_state")
    args = ap.parse_args()

    url = "http://%s:%d/mcp" % (args.host, args.port)

    if not port_open(args.host, args.port):
        print("[1] 端口 %d：**没人听**" % args.port)
        print("    => 桌宠没在运行，或运行着但 MCP 未起来。")
        print("       检查 config.json 的 mcp_server.enabled 是否为 true，然后启动桌宠。")
        return 1
    print("[1] 端口 %d：在听" % args.port)

    try:
        st, hdrs, body = _post(url, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "oc-pet-probe", "version": "0.1"}},
        })
        sid = next((v for k, v in hdrs.items() if k.lower() == "mcp-session-id"), None)
        msg = _parse(body) or {}
        result = msg.get("result") or {}
        print("[2] initialize -> HTTP %s  session=%s" % (st, bool(sid)))
        print("    serverInfo: %s" % json.dumps(result.get("serverInfo") or {},
                                                ensure_ascii=False))
        if sid:
            try:
                _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized",
                            "params": {}}, sid, timeout=6.0)
            except Exception as e:  # noqa: BLE001 — 通知失败通常无妨
                print("    (initialized 通知异常: %s)" % e)

        st, _, body = _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                                  "params": {}}, sid)
        tools = ((_parse(body) or {}).get("result") or {}).get("tools") or []
        print("[3] tools/list -> HTTP %s，共 %d 个工具：" % (st, len(tools)))
        for t in tools:
            print("      - %s" % t.get("name"))

        st, _, body = _post(url, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                  "params": {"name": args.state_tool, "arguments": {}}}, sid)
        res = (_parse(body) or {}).get("result") or {}
        content = res.get("content") or []
        txt = content[0].get("text") if content and isinstance(content[0], dict) else body
        print("[4] tools/call %s -> HTTP %s" % (args.state_tool, st))
        try:
            snap = json.loads(txt)
            print("    顶层键: %s" % sorted(snap.keys()))
            print("    screen 段: %s" % json.dumps(snap.get("screen"),
                                                   ensure_ascii=False)[:300])
        except Exception:
            print("    原文: %s" % str(txt)[:300])
        return 0
    except urllib.error.HTTPError as e:
        print("!! HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
        return 1
    except Exception as e:  # noqa: BLE001
        print("!! 探针异常 %s: %s" % (type(e).__name__, e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
