#!/usr/bin/env python3
"""verify_skyrim_bridge.py — Skyrim MCP 桥接离线/逻辑验证

不依赖真实游戏（SkyrimNet/SkyLink 通常没在跑），用 fake bridge 覆盖：
  1) mcp SDK 可用 / 模块导入
  2) 配置加载（默认 / config 覆盖 / 非法回退）
  3) 护栏 _is_loopback_host
  4) 文本提取 _extract_text
  5) 触发前缀剥离 _strip_prefix
  6) 路由 _route：列出工具 / 调用工具 / 用法提示 / 离线降级
  7) 能力注册 init_skyrim_bridge（启用→注册 skyrim_tool；未启用→None）
  8) SkyrimBridge 后台 loop 能启动（不连网）
运行：python verify_skyrim_bridge.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.skyrim_bridge as sb
from core.capability_registry import EXTERNAL_CAPABILITIES, RouteResult

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  \u2713 {name}")
    else:
        failed += 1
        print(f"  \u2717 {name}")


print("== 1. 模块与依赖 ==")
check("mcp SDK 可用 (MCP_AVAILABLE)", sb.MCP_AVAILABLE is True)
check("SkyrimResult 可构造", hasattr(sb, "SkyrimResult"))


print("== 2. 配置加载 ==")
cfg = sb._load_config()
check("默认 server_type=skyrimnet", cfg["server_type"] == "skyrimnet")
check("默认 skynet_url", cfg["skynet_url"] == "http://127.0.0.1:8889")
check("默认 transport=sse", cfg["skynet_transport"] == "sse")
check("默认 allow_remote=False", cfg["allow_remote"] is False)
check("默认 timeout=30", cfg["timeout"] == 30)

cfg2 = sb._load_config({"enabled": True, "server_type": "skylink",
                        "skylink_dll": "X.dll", "timeout": 15})
check("config 覆盖 server_type", cfg2["server_type"] == "skylink")
check("config 覆盖 skylink_dll", cfg2["skylink_dll"] == "X.dll")
check("config 覆盖 timeout", cfg2["timeout"] == 15)

cfg3 = sb._load_config({"server_type": "bogus"})
check("非法 server_type 回退 skyrimnet", cfg3["server_type"] == "skyrimnet")
cfg4 = sb._load_config({"skynet_transport": "weird"})
check("非法 transport 回退 sse", cfg4["skynet_transport"] == "sse")


print("== 3. 护栏 _is_loopback_host ==")
check("loopback 127.0.0.1", sb._is_loopback_host("http://127.0.0.1:8889") is True)
check("loopback localhost", sb._is_loopback_host("http://localhost:8889") is True)
check("非 loopback 拒绝", sb._is_loopback_host("http://192.168.1.5:8889") is False)
check("空/坏 host 拒绝", sb._is_loopback_host("not a url") is False)


print("== 4. 文本提取 _extract_text ==")


class _C:
    def __init__(self, type, text=None, data=None):
        self.type = type
        self.text = text
        self.data = data


class _R:
    def __init__(self, content):
        self.content = content


r = _R([_C("text", "hello"), _C("text", "world")])
check("_extract_text 多段拼接", sb._extract_text(r) == "hello\nworld")
r2 = _R([_C("image", data={"x": 1})])
check("_extract_text 非文本回退 data", "x" in sb._extract_text(r2))
r3 = _R([])
check("_extract_text 空 content", sb._extract_text(r3) == "")


print("== 5. 触发前缀剥离 _strip_prefix ==")
check("去「天际调用」", sb._strip_prefix("天际调用 getPlayerStats") == "getPlayerStats")
check("去「skyrim:」", sb._strip_prefix("skyrim: getX") == "getX")
check("去「老滚」", sb._strip_prefix("老滚 列出工具") == "列出工具")


print("== 6. 路由 _route（fake bridge，不连网）==")
class FakeBridge:
    def __init__(self, tools=None, call_value=None):
        self.called = []
        self._tools = tools      # False=离线失败；list=工具名
        self._call = call_value  # False=调用失败；否则返回值

    def list_tools(self):
        if self._tools is False:
            return sb.SkyrimResult.fail("未连接")
        return sb.SkyrimResult(ok=True, value=self._tools)

    def call_tool(self, name, args):
        self.called.append((name, args))
        if self._call is False:
            return sb.SkyrimResult.fail("调用失败")
        return sb.SkyrimResult(ok=True, value=self._call or f"ok:{name}")


rr = sb._route("天际 列出工具", FakeBridge(["getPlayerStats", "setTime", "getInventory"]), cfg)
check("列出工具 → capability 正确", isinstance(rr, RouteResult) and rr.capability == "skyrim_tool")
check("列出工具 → 文本含工具名", "getPlayerStats" in rr.text and "setTime" in rr.text)

rr2 = sb._route('调用 天际 getPlayerStats {"hour": 12}', FakeBridge(), cfg)
check("调用工具 → capability 正确", rr2.capability == "skyrim_tool")
check("调用工具 → 文本含工具名", "[getPlayerStats]" in rr2.text)

rr3 = sb._route("天际 工具", FakeBridge(), cfg)
check("无工具名 → 用法提示", "用法" in rr3.text and rr3.capability == "skyrim_tool")

rr4 = sb._route("天际 列出工具", FakeBridge(tools=False), cfg)
check("离线降级 → emotion=sad", rr4.emotion == "sad" and rr4.capability == "skyrim_tool")

# 参数 JSON 解析
fb = FakeBridge()
sb._route('调用 天际 setTime {"hour": 12}', fb, cfg)
check("参数 JSON 解析传入", fb.called and fb.called[0] == ("setTime", {"hour": 12}))


print("== 7. 能力注册 init_skyrim_bridge ==")
# 先清掉可能存在的旧注册
sb.init_skyrim_bridge({"enabled": False})
b_none = sb.init_skyrim_bridge({"enabled": False})
check("未启用返回 None", b_none is None)
check("未启用不注册能力", not any(c.name == "skyrim_tool" for c in EXTERNAL_CAPABILITIES))

b_on = sb.init_skyrim_bridge({"enabled": True, "server_type": "skyrimnet",
                              "skynet_url": "http://127.0.0.1:8889", "timeout": 5})
check("启用返回 bridge", b_on is not None)
check("启用注册 skyrim_tool", any(c.name == "skyrim_tool" for c in EXTERNAL_CAPABILITIES))
# 取注册的 callable 跑一次（离线，会因连不上走降级，但不崩）
cap = next(c for c in EXTERNAL_CAPABILITIES if c.name == "skyrim_tool")
rr_cap = cap.callable("天际 列出工具")
check("已注册能力 callable 返回 RouteResult", isinstance(rr_cap, RouteResult))
# 清理
from core.capability_registry import unregister_capability
unregister_capability("skyrim_tool")
check("清理：能力已卸载", not any(c.name == "skyrim_tool" for c in EXTERNAL_CAPABILITIES))


print("== 8. SkyrimBridge 后台 loop 启动 ==")
if b_on is not None:
    b_on.close()
# 构造但不连网
b = sb.SkyrimBridge({"server_type": "skyrimnet", "timeout": 5})
check("桥接实例已建", b is not None)
check("后台 loop 已启动", b._loop is not None and b._loop.is_running())
b.close()
check("close 后 loop 停止", b._loop is None or not b._loop.is_running())


print(f"\n结果：{passed} 通过 / {failed} 失败")
sys.exit(1 if failed else 0)
