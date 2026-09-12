"""skyrim_bridge — 让桌宠接入 Skyrim 的 MCP server（SkyLink AI / SkyrimNet）

设计要点（参照 mc_bridge 模式，但走标准 MCP 协议）：
- 两个 server 类型（用户在设置「⚔️ Skyrim」页选择）：
  * skylink   (SkyLink AI)：stdio MCP server，由 `dotnet SkyrimMCP.dll` 启动，
              通过命名管道连游戏内 SKSE 插件。需 .NET 10 Runtime（注意：本机目前是 .NET 8）。
  * skyrimnet (SkyrimNet)：HTTP(SSE) MCP server，游戏内 SKSE 插件监听 localhost:8889。
              需在游戏里装好 SkyrimNet 并运行，桌宠才能连上。
              **实测要点（beta24）**：只实现了 SSE 传输入口 `/sse`（Streamable HTTP 与
              根路径都 404）；且 host=localhost 在 Windows 上常只绑 IPv6 `[::1]:8889`，
              故 URL 用 `localhost` 比 `127.0.0.1` 稳，桥接内还会自动换写法重试。
- 桌宠作为标准 MCP client（mcp SDK 1.27）。每个 server 一条长连，后台 asyncio 线程托管。
- 零阻塞主循环：call_tool / list_tools 经 run_coroutine_threadsafe 调度到后台 loop，带超时。
- 护栏（默认收紧）：skyrimnet 仅本机（allow_remote=False 拒绝非 loopback 地址）；
  skylink 是 stdio 子进程，无远程暴露风险。
- 懒连接、离线优雅降级、不阻塞主循环：连不上就返回失败，不崩桌宠。
- 不碰 pet.py 主循环；只通过 capability_registry.register_capability 注入能力。

配置：
  - 主来源：config.json 的 `skyrim:` 块（由设置面板「⚔️ Skyrim」标签页写入），
    经 init_skyrim_bridge(skyrim_config=...) 传入。
  - 后备/兼容：环境变量（设置面板未配置时的降级路径）：
      OC_SKYRIM_ENABLE        1/true           启用
      OC_SKYRIM_SERVER_TYPE   skylink|skyrimnet（默认 skyrimnet，最稳）
      OC_SKYRIM_SKYLINK_DLL   SkyrimMCP.dll 完整路径
      OC_SKYRIM_DOTNET        dotnet 可执行路径（默认 dotnet，需在 PATH 或绝对路径）
      OC_SKYRIM_URL           http://localhost:8889/sse（缺路径会自动补 sse→/sse）
      OC_SKYRIM_TRANSPORT     sse|streamable_http（默认 sse）
      OC_SKYRIM_ALLOW_REMOTE  1/true           允许非本机地址（默认关）
      OC_SKYRIM_TIMEOUT       单次调用最长等待秒（默认 30）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Optional

try:
    from contextlib import AsyncExitStack
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import streamablehttp_client
    MCP_AVAILABLE = True
except Exception:  # noqa: BLE001 — 缺 mcp SDK 时优雅降级，不崩导入
    MCP_AVAILABLE = False
    AsyncExitStack = ClientSession = sse_client = stdio_client = \
        streamablehttp_client = StdioServerParameters = None

logger = logging.getLogger(__name__)


def _is_loopback_host(url: str) -> bool:
    """判定 URL 是否指向本机。非本机（含解析失败/空 host）一律视为非 loopback（失败闭合）。"""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    if not host:
        return False
    return host in ("127.0.0.1", "::1", "localhost", "0.0.0.0", "[::1]")


def _normalize_mcp_url(url: str, transport: str = "sse") -> str:
    """给 MCP 端点补齐路径。

    实测（2026-09-12，SkyrimNet beta24）：它的 MCP server **只**实现了 SSE 传输入口
    `/sse`，POST/GET `/`、`/mcp` 一律 404；而 Streamable HTTP 那条路走不通。
    用户若只填 `http://localhost:8889`（缺路径），握手必然失败，弹窗只报
    「连接超时/失败」，很难看出是路径问题 —— 这里在路径为空时按传输方式补默认路径。
    用户显式写了路径就原样尊重（可指向反代等自定义入口）。
    """
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return raw
    try:
        from urllib.parse import urlparse
        parsed = urlparse(raw)
    except Exception:  # noqa: BLE001
        return raw
    if not parsed.scheme or not parsed.netloc:
        return raw
    if (parsed.path or "").strip():
        return raw
    return raw + ("/mcp" if transport == "streamable_http" else "/sse")


# 同一台机器上「本机」的两种常见写法，互为备用
_LOOPBACK_SWAPS = {
    "127.0.0.1": "localhost",
    "localhost": "127.0.0.1",
}


def _loopback_fallback_url(url: str) -> Optional[str]:
    """给出同一端点的「另一种本机写法」，没有则返回 None。

    病灶（2026-09-12 实测）：SkyrimNet 的 MCP.yaml 默认 `host: localhost`，而 Windows
    优先把 localhost 解析成 IPv6 回环 `::1`，于是插件**只监听 [::1]:8889**；
    此时填 `127.0.0.1`（IPv4）会被「积极拒绝」，反向亦然。
    换一种回环写法再试一次，两种宿主写法都能连上，用户不必关心绑的是 v4 还是 v6。
    """
    try:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return None
    alt = _LOOPBACK_SWAPS.get((parsed.hostname or "").lower())
    if not alt:
        return None
    netloc = f"{alt}:{parsed.port}" if parsed.port else alt
    return urlunparse(parsed._replace(netloc=netloc))


# ─────────────────────────────────────────────────────────────
# Result 类型 —— 对应 N.E.K.O 的 status 分档：绝不静默当成功
# ─────────────────────────────────────────────────────────────
@dataclass
class SkyrimResult:
    ok: bool = True
    status: str = "ok"            # ok | failed
    value: Any = None
    error: str = ""
    raw: Any = None

    def __bool__(self) -> bool:
        return self.ok

    @classmethod
    def fail(cls, error: str, status: str = "failed") -> "SkyrimResult":
        return cls(ok=False, status=status, error=error)


# ─────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────
def _load_config(skyrim_config: Optional[dict] = None) -> dict:
    """构建运行时配置。

    优先级：skyrim_config（config.json 的 skyrim: 块，用户通过 GUI 明确设置）覆盖环境变量，
    环境变量覆盖内置默认值。无 skyrim_config 时退化为「纯环境变量」路径（向后兼容旧用法）。
    """
    cfg: dict = {
        "server_type": (os.environ.get("OC_SKYRIM_SERVER_TYPE") or "skyrimnet").strip().lower(),
        "skylink_dll": (os.environ.get("OC_SKYRIM_SKYLINK_DLL") or "").strip(),
        "dotnet_path": (os.environ.get("OC_SKYRIM_DOTNET") or "dotnet").strip(),
        "skynet_url": (os.environ.get("OC_SKYRIM_URL") or "http://localhost:8889/sse").strip().rstrip("/"),
        "skynet_transport": (os.environ.get("OC_SKYRIM_TRANSPORT") or "sse").strip().lower(),
        "allow_remote": os.environ.get("OC_SKYRIM_ALLOW_REMOTE", "").strip().lower()
                            in ("1", "true", "yes", "on"),
        "timeout": float(os.environ.get("OC_SKYRIM_TIMEOUT", "30")),
    }
    if skyrim_config:
        if "server_type" in skyrim_config and skyrim_config["server_type"]:
            cfg["server_type"] = str(skyrim_config["server_type"]).strip().lower()
        if "skylink_dll" in skyrim_config and skyrim_config["skylink_dll"]:
            cfg["skylink_dll"] = str(skyrim_config["skylink_dll"]).strip()
        if "dotnet_path" in skyrim_config and skyrim_config["dotnet_path"]:
            cfg["dotnet_path"] = str(skyrim_config["dotnet_path"]).strip()
        if "skynet_url" in skyrim_config and skyrim_config["skynet_url"]:
            cfg["skynet_url"] = str(skyrim_config["skynet_url"]).strip().rstrip("/")
        if "skynet_transport" in skyrim_config and skyrim_config["skynet_transport"]:
            cfg["skynet_transport"] = str(skyrim_config["skynet_transport"]).strip().lower()
        if "allow_remote" in skyrim_config:
            cfg["allow_remote"] = bool(skyrim_config["allow_remote"])
        if "timeout" in skyrim_config:
            try:
                cfg["timeout"] = float(skyrim_config["timeout"])
            except (TypeError, ValueError):
                logger.debug("skyrim_bridge: 非致命异常(已静默吞掉)", exc_info=True)
    if cfg["server_type"] not in ("skylink", "skyrimnet"):
        logger.warning("[skyrim_bridge] server_type=%s 非法，回退 skyrimnet", cfg["server_type"])
        cfg["server_type"] = "skyrimnet"
    if cfg["skynet_transport"] not in ("sse", "streamable_http"):
        cfg["skynet_transport"] = "sse"
    # 补全端点路径（用户只填 host:port 时也能连上）
    cfg["skynet_url"] = _normalize_mcp_url(cfg["skynet_url"], cfg["skynet_transport"])
    return cfg


# ─────────────────────────────────────────────────────────────
# 桥接主体
# ─────────────────────────────────────────────────────────────
class SkyrimBridge:
    """标准 MCP client：后台 asyncio 线程托管一条长连，向外暴露同步 call_tool/list_tools。"""

    def __init__(self, config: dict):
        self.cfg = config
        self.server_type = config.get("server_type", "skyrimnet")
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stack: Optional[Any] = None
        self._session: Optional[Any] = None
        self._connected = False
        self._tools_cache: list[str] = []
        self._active_url: Optional[str] = None
        self._lock = threading.Lock()
        self._result_handlers: list[Any] = []
        self._start_loop()

    # ── asyncio 线程 ──
    def _start_loop(self) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever, daemon=True, name="skyrim-bridge-async",
            )
            self._thread.start()
        except Exception:  # noqa: BLE001
            self._loop = None
            logger.debug("skyrim_bridge: 非致命异常(已静默吞掉)", exc_info=True)

    def on_result(self, handler) -> None:
        self._result_handlers.append(handler)

    def _emit(self, res: SkyrimResult, src: str) -> None:
        for h in self._result_handlers:
            try:
                h(res, src)
            except Exception:  # noqa: BLE001
                logger.debug("skyrim_bridge: 非致命异常(已静默吞掉)", exc_info=True)

    # ── 连接（同步入口）──
    def connect(self) -> SkyrimResult:
        if self._loop is None:
            return SkyrimResult.fail("asyncio 循环未启动（mcp SDK 可能未安装或启动失败）")
        fut = asyncio.run_coroutine_threadsafe(self._connect_async(), self._loop)
        try:
            return fut.result(timeout=self.cfg.get("timeout", 30))
        except Exception as e:  # noqa: BLE001
            return SkyrimResult.fail(f"连接超时/失败：{e}")

    async def _connect_async(self) -> SkyrimResult:
        # 先收掉旧连接
        await self._close_async()
        if not MCP_AVAILABLE:
            return SkyrimResult.fail("未安装 mcp SDK（pip install 'mcp~=1.27'）")
        self._stack = AsyncExitStack()
        try:
            if self.server_type == "skylink":
                dll = self.cfg.get("skylink_dll")
                if not dll:
                    return SkyrimResult.fail("SkyLink 未配置 SkyrimMCP.dll 路径（设置「⚔️ Skyrim」页填）")
                dotnet = self.cfg.get("dotnet_path") or "dotnet"
                params = StdioServerParameters(command=dotnet, args=[dll])
                try:
                    read, write = await self._stack.enter_async_context(stdio_client(params))
                except Exception as e:  # noqa: BLE001
                    return SkyrimResult.fail(
                        f"启动 SkyLink(stdio) 失败：{e}\n（需 .NET 10 Runtime，当前本机为 .NET 8，"
                        f"请先装 .NET 10 Desktop/Console Runtime）"
                    )
            else:
                url = self.cfg.get("skynet_url", "http://localhost:8889/sse")
                transport = self.cfg.get("skynet_transport", "sse")
                if not self.cfg.get("allow_remote", False) and not _is_loopback_host(url):
                    return SkyrimResult.fail(
                        "护栏拦截：URL 非本机，且未开启「允许远程」(allow_remote)"
                    )
                # 本机两种写法都试一遍（server 可能只绑了 IPv6 ::1 或只绑了 IPv4）
                candidates = [url]
                alt = _loopback_fallback_url(url)
                if alt:
                    candidates.append(alt)
                last_err: Optional[Exception] = None
                read = write = None
                for cand in candidates:
                    try:
                        if transport == "streamable_http":
                            trio = await self._stack.enter_async_context(streamablehttp_client(cand))
                            read, write = trio[0], trio[1]
                        else:
                            read, write = await self._stack.enter_async_context(sse_client(cand))
                        self._active_url = cand
                        last_err = None
                        break
                    except Exception as e:  # noqa: BLE001
                        last_err = e
                        logger.debug("[skyrim_bridge] 连接 %s 失败，换回环写法重试", cand)
                        try:  # 丢弃这一轮的连接栈，给下一个候选一个干净的 stack
                            await self._stack.aclose()
                        except Exception:  # noqa: BLE001
                            logger.debug("skyrim_bridge: 非致命异常(已静默吞掉)", exc_info=True)
                        self._stack = AsyncExitStack()
                if read is None or write is None or last_err is not None:
                    return SkyrimResult.fail(
                        f"连接 SkyrimNet({transport}) @ {url} 失败：{last_err}\n"
                        f"（确认游戏已运行且装好 SkyrimNet；它的 MCP server 走 SSE 传输，"
                        f"端点 http://localhost:8889/sse，端口 8889 需在监听）"
                    )
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
            tools = await self._session.list_tools()
            self._tools_cache = [t.name for t in (getattr(tools, "tools", None) or [])]
            self._connected = True
            return SkyrimResult(ok=True, value={"tools": self._tools_cache, "count": len(self._tools_cache)})
        except Exception as e:  # noqa: BLE001
            return SkyrimResult.fail(f"MCP 握手 / list_tools 失败：{e}")

    # ── 调用工具（同步入口）──
    def call_tool(self, name: str, arguments: Optional[dict] = None) -> SkyrimResult:
        if not self._connected or self._session is None:
            res = self.connect()
            if not res.ok:
                return res
        fut = asyncio.run_coroutine_threadsafe(
            self._call_async(name, arguments or {}), self._loop,
        )
        try:
            return fut.result(timeout=self.cfg.get("timeout", 30))
        except Exception as e:  # noqa: BLE001
            return SkyrimResult.fail(f"调用工具 {name} 超时/失败：{e}")

    async def _call_async(self, name: str, arguments: dict) -> SkyrimResult:
        try:
            result = await self._session.call_tool(name, arguments)
        except Exception as e:  # noqa: BLE001
            return SkyrimResult.fail(f"调用 {name} 异常：{e}")
        if getattr(result, "isError", False):
            return SkyrimResult.fail(_extract_text(result) or f"{name} 返回错误", status="failed")
        return SkyrimResult(ok=True, value=_extract_text(result), raw=result)

    # ── 列出工具（同步入口）──
    def list_tools(self) -> SkyrimResult:
        if not self._connected:
            res = self.connect()
            if not res.ok:
                return res
        return SkyrimResult(ok=True, value=self._tools_cache)

    # ── 关闭 ──
    def close(self) -> None:
        if self._loop is not None:
            fut = asyncio.run_coroutine_threadsafe(self._close_async(), self._loop)
            try:
                fut.result(timeout=5)
            except Exception:  # noqa: BLE001
                logger.debug("skyrim_bridge: 非致命异常(已静默吞掉)", exc_info=True)
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:  # noqa: BLE001
                logger.debug("skyrim_bridge: 非致命异常(已静默吞掉)", exc_info=True)
            self._loop = None

    async def _close_async(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:  # noqa: BLE001
                logger.debug("skyrim_bridge: 非致命异常(已静默吞掉)", exc_info=True)
        self._stack = None
        self._session = None
        self._connected = False


def _extract_text(result: Any) -> str:
    """从 CallToolResult.content 提取所有文本片段拼成字符串。"""
    items = getattr(result, "content", None) or []
    parts: list[str] = []
    for it in items:
        t = getattr(it, "type", None)
        if t == "text":
            parts.append(getattr(it, "text", "") or "")
        elif hasattr(it, "data"):
            parts.append(str(getattr(it, "data", "")))
    return "\n".join(p for p in parts if p)


# ─────────────────────────────────────────────────────────────
# 注册为 oc-pet 能力（最小 core 改动：capability_registry 扩展点）
# ─────────────────────────────────────────────────────────────
_TOOL_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_.]*)\s*(?:\((.*)\))?\s*$",
    re.IGNORECASE,
)


_TRIGGER_PREFIX_RE = re.compile(
    r"^(?:\s*(?:天际|skyrim|老滚|上古卷轴|sr|调用|方法|工具|查询|call|tool)\s*[:：]?\s*)+",
    re.IGNORECASE,
)


def _strip_prefix(text: str) -> str:
    """去掉已知触发词（天际/skyrim/老滚 + 调用/方法/工具/查询，顺序无关、可重复）。"""
    t = text.strip()
    t = _TRIGGER_PREFIX_RE.sub("", t, count=8).strip()
    return t.strip(" :：").strip()


def _route(text: str, bridge: SkyrimBridge, cfg: dict):
    """自然语言 → Skyrim MCP 工具调用。返回 RouteResult。"""
    from core.capability_registry import RouteResult

    t = text.strip()
    # 列出可用工具
    if re.search(r"列出|有哪些工具|工具列表|list\s*tools|list_tools|list tool|\blist\b", t, re.IGNORECASE):
        res = bridge.list_tools()
        if not res.ok:
            return RouteResult(capability="skyrim_tool", text=res.error, emotion="sad", anim="idle")
        names = res.value or []
        if not names:
            return RouteResult(
                capability="skyrim_tool",
                text="已连接但暂无可用工具——请确认游戏里 SkyrimNet/SkyLink 已启用并加载完成。",
                emotion="neutral", anim="idle",
            )
        header = f"已连接 Skyrim MCP（{cfg['server_type']}），可用工具 {len(names)} 个："
        listed = "\n".join(f"· {n}" for n in names[:60])
        more = f"\n…（其余 {len(names) - 60} 个未显示）" if len(names) > 60 else ""
        return RouteResult(capability="skyrim_tool", text=f"{header}\n{listed}{more}",
                           emotion="happy", anim="extra")

    stripped = _strip_prefix(t)
    if not stripped:
        return RouteResult(
            capability="skyrim_tool",
            text="用法示例：「调用 天际 getPlayerStats」或「天际 列出工具」。\n"
                 "工具名 + 参数可写 JSON：调用 天际 setTime {\"hour\": 12}",
            emotion="neutral", anim="idle",
        )
    m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_.]*)\s*(.*)$", stripped, re.DOTALL)
    if not m or not m.group(1):
        return RouteResult(
            capability="skyrim_tool",
            text="用法示例：「调用 天际 getPlayerStats」或「天际 列出工具」。\n"
                 "工具名 + 参数可写 JSON：调用 天际 setTime {\"hour\": 12}",
            emotion="neutral", anim="idle",
        )
    name = m.group(1)
    rest = (m.group(2) or "").strip()
    args: dict = {}
    if rest:
        try:
            args = json.loads(rest)
        except Exception:  # noqa: BLE001
            # 圆括号参数：setTime(arg) 或裸值
            pm = re.match(r"^\((.*)\)$", rest, re.DOTALL)
            if pm:
                inner = pm.group(1).strip()
                try:
                    args = json.loads(inner) if inner.startswith("{") else {"arg": inner}
                except Exception:  # noqa: BLE001
                    args = {"arg": inner}
            else:
                args = {"arg": rest}
    res = bridge.call_tool(name, args)
    if not res.ok:
        return RouteResult(capability="skyrim_tool", text=res.error, emotion="sad", anim="idle")
    val = res.value if isinstance(res.value, str) else json.dumps(res.value, ensure_ascii=False)
    return RouteResult(
        capability="skyrim_tool",
        text=f"[{name}] → {val[:500]}",
        emotion="happy", anim="extra",
    )


def init_skyrim_bridge(skyrim_config: Optional[dict] = None) -> Optional[SkyrimBridge]:
    """初始化 skyrim_bridge 并注册能力。返回桥接实例（供测试/调试），未启用时返回 None。

    启用判定（GUI 显式优先，环境变量后备）：
      - skyrim_config 里 enabled 显式 True/False → 直接决定（设置面板可开可关）
      - 没给 skyrim_config 或没写 enabled → 退回环境变量 OC_SKYRIM_ENABLE=1
    不启用就不注册能力，免得给没跑 Skyrim 的用户注入一个只会报错的能力。
    """
    if not MCP_AVAILABLE:
        logger.warning("[skyrim_bridge] 未安装 mcp SDK（pip install 'mcp~=1.27'）；跳过能力注册")
        return None

    _gui_enabled: Optional[bool] = None
    if isinstance(skyrim_config, dict) and "enabled" in skyrim_config:
        _gui_enabled = bool(skyrim_config["enabled"])

    if _gui_enabled is False:
        logger.info("[skyrim_bridge] 已在设置中关闭；跳过能力注册")
        return None
    if _gui_enabled is None:
        if os.environ.get("OC_SKYRIM_ENABLE", "").strip().lower() not in ("1", "true", "yes", "on"):
            logger.info(
                "[skyrim_bridge] 未启用（在设置「🔗 MCP」分类的 Skyrim 子标签打开，或设 OC_SKYRIM_ENABLE=1）；跳过能力注册"
            )
            return None

    from core.capability_registry import (
        Capability, RouteResult, register_capability, unregister_capability,
    )

    cfg = _load_config(skyrim_config)
    bridge = SkyrimBridge(cfg)

    # 幂等：重复初始化时先摘掉旧能力，避免同名重复注册
    unregister_capability("skyrim_tool")

    bridge.on_result(lambda res, src: logger.info("[skyrim_bridge] %s: %s", src, res))

    def handler(text: str) -> RouteResult:
        return _route(text, bridge, cfg)

    register_capability(Capability(
        name="skyrim_tool",
        patterns=[
            # ── 中文（全名）──
            "天际", "老滚", "上古卷轴",
            "天际调用", "天际方法", "天际工具", "天际查询", "天际指令",
            "天际列出工具", "天际有哪些工具", "天际工具列表", "天际列工具",
            "老滚调用", "老滚方法", "老滚工具", "老滚查询",
            "老滚列出工具", "老滚有哪些工具",
            "上古卷轴调用", "上古卷轴方法", "上古卷轴工具", "上古卷轴查询",
            # ── 英文（全名）──
            "skyrim", "skyrim调用", "skyrim call", "skyrim tool", "skyrim method", "skyrim query",
            "调用skyrim", "skyrim列出工具", "skyrim list", "skyrim有哪些工具",
            # ── 中文缩写（老滚 即 Skyrim 圈常用简称，已含于上方）──
            # ── 英文缩写（sr）──
            "sr", "sr调用", "sr call", "sr方法", "sr tool", "sr查询",
            "sr列出工具", "sr list", "sr有哪些工具",
        ],
        handler="callable", callable=handler,
        description="调用 Skyrim MCP 工具（SkyLink AI / SkyrimNet）：查状态、改属性、列工具等",
        emotion="neutral", anim="idle",
    ))

    logger.info("[skyrim_bridge] initialized (server_type=%s)", cfg["server_type"])
    return bridge
