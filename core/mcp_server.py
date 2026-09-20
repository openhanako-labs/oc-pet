"""mcp_server — 桌宠作为 MCP 提供方，让 Hana（及其他 MCP 客户端）看见桌宠。

W1a（2026-09-14）：解决「Hana 完全看不见桌宠」这个单向问题。

背景（核实所得，勿凭印象改）
----------------------------
- 桌宠**已有** MCP 客户端能力（`core/skyrim_bridge.py` 连 SkyrimNet），
  但一直是**消费方**，从没做过提供方。
- Hana 侧对桌宠一无所知：桌宠的工具/能力只存在于进程内
  （`ToolRegistry._tools`、`capability_registry.EXTERNAL_CAPABILITIES`），
  没有任何对外暴露接口。
- 桌宠原有的 3 个 HTTP 口（8977 状态 / 8988 外部触发 / 8077 手机）
  **都不是 MCP 协议**，Hana 的 MCP 注册表接不上。

暴露什么（有意为之的范围）
--------------------------
只暴露**桌宠自己的东西**：
  - 状态查询（pet_state）
  - 能力清单（pet_capabilities）
  - 桌宠专属动作（情绪/动画/表情/说话）

**不暴露那 100 个插件工具**——那些是 Hana 自己的插件，桌宠只是消费者；
把它们绕回来暴露给 Hana 是循环，且会造成「同一个工具两个来源」的认知混乱。

线程模型（硬约束，照抄 status_http_server 的教训）
--------------------------------------------------
MCP server 跑在独立 asyncio 线程里。**任何写操作都不许直接碰渲染器/Qt 对象**，
只能 `EventBus.emit(...)`；PetWindow 订阅后经 Qt 信号转主线程再驱动。
EventBus.emit 在 MCP 线程同步执行，但 handler 只做状态登记/信号发射。

安全
----
- 只绑 127.0.0.1（决策 2：只给本机 Hana），不开 LAN
- 默认关：config `mcp_server.enabled=False` 时不启动，零行为、不占端口
- 写操作另有 `allow_actions` 开关（默认 True；设 False 则只读）

配置（config.json 的 `mcp_server` 块）
--------------------------------------
    {
      "enabled": false,
      "port": 8979,
      "allow_actions": true
    }

⚠ 关于鉴权（2026-09-19 核实）
--------------------------------------
只绑 127.0.0.1，**未实现 token 校验**。config 里曾记过 `auth_token` 键，但
代码从未读取它（只存在于文档字符串）——已从上面的示例里删掉，避免误以为
“填了 token 就安全”。本机任意进程都能调 `pet_say` / `pet_set_emotion` 等
写工具；要真鉴权需另行实现（需要时再说）。

端口选择：8977(状态) / 8988(外部触发) / 8077(手机) / 8889(SkyrimNet) 已占，
取 **8979**。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8979


# ── 传输安全（Origin / DNS-rebinding 保护）────────────────
#
# 2026-09-19 核实：**MCP SDK 默认就开着**这层保护，且只允本机来源：
#   TransportSecuritySettings(enable_dns_rebinding_protection=True,
#       allowed_hosts=['127.0.0.1:*', 'localhost:*', '[::1]:*'],
#       allowed_origins=['http://127.0.0.1:*', 'http://localhost:*', 'http://[::1]:*'])
# 所以报告里那条「MCP 提供端缺 Origin 校验」**不成立**。
#
# 那为什么还显式传一遍？因为**安全默认值不该是隐式的**：哪天 SDK 改了默认，
# oc-pet 会静默丢掉这层保护。这里把它钉死，并用测试守住。
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = ("127.0.0.1:*", "localhost:*", "[::1]:*")
DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
)


def build_transport_security(cfg: Optional[dict] = None):
    """按 config 构建 ``TransportSecuritySettings``；无 SDK 时返回 None。

    配置（写在 ``mcp_server`` 块里）::

        "dns_rebinding_protection": true,   # 默认 true
        "allowed_hosts": [],                # 留空 = 用内置本机白名单
        "allowed_origins": []

    ⚠️ **空列表一律回退内置白名单**，既不当“允许全部”、也不当“拒绝全部”：
    向开启了保护的 SDK 传空列表会让谁都进不来——那是坏配置，不是安全。
    """
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except Exception:  # noqa: BLE001 — 无 SDK 时优雅降级（与 MCP_AVAILABLE 同路）
        return None
    c = cfg or {}
    hosts = [str(h) for h in (c.get("allowed_hosts") or []) if str(h).strip()]
    origins = [str(o) for o in (c.get("allowed_origins") or []) if str(o).strip()]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(c.get("dns_rebinding_protection", True)),
        allowed_hosts=hosts or list(DEFAULT_ALLOWED_HOSTS),
        allowed_origins=origins or list(DEFAULT_ALLOWED_ORIGINS),
    )

try:
    from mcp.server.fastmcp import FastMCP
    MCP_AVAILABLE = True
except Exception:  # noqa: BLE001 — 缺 mcp SDK 时优雅降级，不崩导入
    MCP_AVAILABLE = False
    FastMCP = None  # type: ignore


# 允许通过 MCP 驱动的动作白名单。
# 只放「无副作用、可随时回退」的表现类动作——桌宠是被看着的东西，
# 不该让外部随便改它的行为模式/配置。
ACTION_WHITELIST: tuple[str, ...] = (
    "set_emotion",   # 设情绪
    "play_anim",     # 播动画/动作
    "expression",    # 触发表情
    "say",           # 让桌宠说一句话（气泡 + TTS）
    "celebrate",     # 庆祝特效
    "idle",          # 强制回 idle
)


class PetMCPServer:
    """桌宠 MCP 提供方（streamable-http，仅本机）。

    用法（桌宠主线程）：
        srv = PetMCPServer(
            state_provider=self._status_snapshot,
            capabilities_provider=self._mcp_capabilities,
            action_sink=self._mcp_action_sink,   # 只做 EventBus.emit，立即返回
            port=8979,
        )
        srv.start()   # 后台线程
        ...
        srv.stop()

    Args:
        state_provider: 无参可调用，返回状态快照 dict。
        capabilities_provider: 无参可调用，返回能力清单 list[dict]。
        action_sink: (action: str, params: dict) -> str|None，**必须立即返回**
            （只登记/发信号，不做实际动作）。
        port: 监听端口（默认 8979）。
        allow_actions: 是否允许写操作（False = 只读模式）。
        transport_security: 传输安全配置（见 :func:`build_transport_security`）。
    """

    def __init__(
        self,
        state_provider: Callable[[], dict],
        capabilities_provider: Optional[Callable[[], list]] = None,
        action_sink: Optional[Callable[[str, dict], Any]] = None,
        catalog_provider: Optional[Callable[[str], dict]] = None,
        computer_provider: Optional[Callable[[str, dict], dict]] = None,
        port: int = DEFAULT_PORT,
        allow_actions: bool = True,
        transport_security: Optional[dict] = None,
    ):
        self._state_provider = state_provider
        self._capabilities_provider = capabilities_provider
        self._action_sink = action_sink
        self._catalog_provider = catalog_provider
        self._computer_provider = computer_provider
        self._port = int(port or DEFAULT_PORT)
        self._allow_actions = bool(allow_actions)
        self._transport_security_cfg: dict = dict(transport_security or {})
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._app = None
        self._started = False

    # ── 属性 ──

    @property
    def port(self) -> int:
        return self._port

    @property
    def started(self) -> bool:
        return self._started

    @property
    def allow_actions(self) -> bool:
        return self._allow_actions

    def url(self) -> str:
        """MCP 端点 URL（供写入 Hana 的 connector 配置）。"""
        return f"http://127.0.0.1:{self._port}/mcp"

    # ── 内部：读 provider（异常一律降级，绝不让 MCP 线程崩） ──

    def _safe_state(self) -> dict:
        try:
            st = self._state_provider()
            return st if isinstance(st, dict) else {"error": "state_provider 返回非 dict"}
        except Exception as e:
            logger.warning("MCP: 读状态失败: %s", e)
            return {"error": str(e)}

    def _safe_capabilities(self) -> list:
        if self._capabilities_provider is None:
            return []
        try:
            caps = self._capabilities_provider()
            return caps if isinstance(caps, list) else []
        except Exception as e:
            logger.warning("MCP: 读能力清单失败: %s", e)
            return []

    def _safe_computer(self, op: str, params: dict) -> dict:
        """代理一次 computer-use 调用。失败一律降级为 error dict，绝不抛出。"""
        if self._computer_provider is None:
            return {"error": "桌宠未接入 computer-use（config computer_use.enabled=false 或未安装 cua-driver）"}
        try:
            r = self._computer_provider(str(op), dict(params or {}))
            return r if isinstance(r, dict) else {"result": r}
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP: computer-use 调用失败 (%s): %s", op, e)
            return {"error": str(e)[:300]}

    def _safe_catalog(self, system: str) -> dict:
        """读 Hana 全体系目录（DISC-2）。失败降级为 error dict。"""
        if self._catalog_provider is None:
            return {"error": "桌宠未接入 Hana 目录（catalog_provider 未配置）"}
        try:
            r = self._catalog_provider(system)
            return r if isinstance(r, dict) else {"error": "catalog_provider 返回非 dict"}
        except Exception as e:
            logger.warning("MCP: 读 Hana 目录失败: %s", e)
            return {"error": str(e)[:200]}

    def _dispatch_action(self, action: str, params: dict) -> str:
        """派发写操作。只登记/发信号，立即返回（不碰渲染线程）。"""
        if not self._allow_actions:
            return "只读模式：写操作已禁用（config mcp_server.allow_actions=false）"
        if action not in ACTION_WHITELIST:
            return f"动作不在白名单内: {action}（可用: {', '.join(ACTION_WHITELIST)}）"
        if self._action_sink is None:
            return "桌宠未接入动作通道（action_sink 未配置）"
        try:
            result = self._action_sink(action, dict(params or {}))
            return str(result) if result else f"已派发: {action}"
        except Exception as e:
            logger.warning("MCP: 派发动作失败 (%s): %s", action, e)
            return f"派发失败: {e}"

    # ── 构建 MCP app ──

    def _build_app(self):
        """构建 FastMCP 实例并注册工具。"""
        security = build_transport_security(self._transport_security_cfg)
        # ⚠️ 不能写 transport_security=None：那是“显式传空”，会**关掉** SDK 的默认保护。
        # 拿不到设置对象时干脆不传这个参数，让 SDK 用它的默认值。
        extra = {"transport_security": security} if security is not None else {}
        app = FastMCP(
            "oc-pet",
            instructions=(
                "OC Pet 桌宠的能力接口。可用工具查询桌宠当前状态、"
                "列举它自己能做的事，以及驱动它的表现（情绪/动画/表情/说话）。\n"
                "注意：桌宠是被看着的角色，请勿高频刷写动作；"
                "表现类动作为异步生效，不阻塞。"
            ),
            **extra,
        )
        if security is not None:
            logger.info(
                "MCP 传输安全：DNS-rebinding 保护=%s ｜ 允许来源=%s",
                security.enable_dns_rebinding_protection, security.allowed_origins,
            )

        @app.tool()
        def pet_state() -> dict:
            """查询桌宠当前状态（state/emotion/anim/scenario/agent_id/渲染格式）。

            只读。用于了解桌宠此刻在做什么、什么情绪。
            """
            return self._safe_state()

        @app.tool()
        def pet_capabilities() -> dict:
            """列出桌宠自己能做的事（内部能力清单）。

            只读。返回桌宠内置能力（日报/截图/会话信息/感知状态等）的名称与描述。
            注意：这里**不含** Hana 插件工具——那些由 Hana 自己管理。
            """
            caps = self._safe_capabilities()
            return {"count": len(caps), "capabilities": caps}

        @app.tool()
        def pet_hana_catalog(system: str = "summary") -> dict:
            """查询 Hana 全体系目录（桌宠读到的 Hana 全景）。

            Args:
                system: 要查的体系，可选 summary（默认，只给统计）/
                    plugins / apps / mcp / skills / agents。
                    除 summary 外会返回该体系的明细清单（名称/描述/工具数/状态）。

            只读。注意：返回的是**清单**（有什么），不含用户数据内容。
            """
            return self._safe_catalog(system)

        @app.tool()
        def pet_set_emotion(emotion: str, intensity: float = 1.0) -> str:
            """设置桌宠情绪。

            Args:
                emotion: 情绪名，如 happy / sad / angry / surprised / thinking /
                    cute / neutral（具体可用值取决于角色包配置）。
                intensity: 强度 0.0~1.0，默认 1.0。
            """
            return self._dispatch_action(
                "set_emotion", {"emotion": emotion, "intensity": intensity}
            )

        @app.tool()
        def pet_play_anim(anim: str) -> str:
            """播放桌宠动画/动作。

            Args:
                anim: 动画名，如 idle / happy / waving / touch / thinking /
                    sad / angry（取决于角色包配置）。
            """
            return self._dispatch_action("play_anim", {"anim": anim})

        @app.tool()
        def pet_expression(name: str) -> str:
            """触发桌宠表情（贴图/参数类）。

            Args:
                name: 表情名，如 比心 / 葱 / 唱歌 / 脸红 / 前倾 / 圈圈 / QQ人
                    （取决于角色包提供哪些 exp3.json）。
            """
            return self._dispatch_action("expression", {"name": name})

        @app.tool()
        def pet_say(text: str) -> str:
            """让桌宠说一句话（气泡显示 + TTS 朗读）。

            Args:
                text: 要说的内容（建议短句，长文本会显著增加合成耗时）。
            """
            return self._dispatch_action("say", {"text": text})

        @app.tool()
        def pet_celebrate() -> str:
            """触发桌宠庆祝特效（爱心粒子等）。"""
            return self._dispatch_action("celebrate", {})

        @app.tool()
        def pet_reset_idle() -> str:
            """让桌宠回到待机状态（清表情/动作，回到 idle）。"""
            return self._dispatch_action("idle", {})

        # ── computer-use（桌宠的「手」，接本机 cua-driver）──
        # 仅在 config computer_use.enabled=true 时注册；否则这几个工具根本不出现。
        if self._computer_provider is not None:

            @app.tool()
            def pet_computer_status() -> dict:
                """查询桌宠的 computer-use 能力状态（驱动版本 / daemon / 是否允许写）。只读。"""
                return self._safe_computer("status", {})

            @app.tool()
            def pet_computer_apps() -> dict:
                """列出本机已安装/在运行的应用（供 launch 取 aumid/name）。只读，可能较大。"""
                return self._safe_computer("apps", {})

            @app.tool()
            def pet_computer_windows(pid: int = 0) -> dict:
                """列出顶层窗口；给 pid 时只列该进程的窗口。只读。

                Args:
                    pid: 进程 ID；0 或省略 = 列全部。
                """
                return self._safe_computer("windows", {"pid": int(pid)} if pid else {})

            @app.tool()
            def pet_computer_window_state(pid: int, window_id: int) -> dict:
                """读取某窗口的 UIA 元素树（点/输入前先取它拿 element_token）。只读。

                Args:
                    pid: 目标进程 ID。
                    window_id: 目标窗口 ID（见 pet_computer_windows）。
                """
                return self._safe_computer("window_state", {"pid": int(pid), "window_id": int(window_id)})

            @app.tool()
            def pet_computer_launch(aumid: str = "", name: str = "", path: str = "") -> dict:
                """启动一个本机应用（后台启动，不抢焦点）。写操作，需 allow_actions。

                Args:
                    aumid: 打包应用 AUMID（如 Microsoft.WindowsCalculator_8wekyb3d8bbwe!App）。
                    name: 应用显示名（aumid 缺失时的回退）。
                    path: 可执行文件完整路径（优先级最高）。
                """
                return self._safe_computer("launch", {"aumid": aumid, "name": name, "path": path})

            @app.tool()
            def pet_computer_click(pid: int, window_id: int = 0, element_token: str = "",
                                   x: int = -1, y: int = -1) -> dict:
                """点击一个元素或坐标（默认后台 UIA Invoke，不抢焦点）。写操作，需 allow_actions。

                Args:
                    pid: 目标进程 ID。
                    window_id: 目标窗口 ID（用 element_token 时必填）。
                    element_token: 来自 pet_computer_window_state 的元素句柄（优先）。
                    x / y: 窗口内像素坐标（element_token 缺失时的回退，-1 表示不传）。
                """
                params: dict = {"pid": int(pid)}
                if window_id:
                    params["window_id"] = int(window_id)
                if element_token:
                    params["element_token"] = element_token
                elif x >= 0 and y >= 0:
                    params["x"] = int(x)
                    params["y"] = int(y)
                return self._safe_computer("click", params)

            @app.tool()
            def pet_computer_type(text: str, pid: int = 0) -> dict:
                """向目标窗口输入文字。写操作，需 allow_actions。

                Args:
                    text: 要输入的内容。
                    pid: 目标进程 ID（0 = 当前焦点窗口）。
                """
                return self._safe_computer("type", {"text": text, "pid": int(pid)} if pid else {"text": text})

            @app.tool()
            def pet_computer_key(key: str, pid: int = 0) -> dict:
                """向目标窗口发送一次按键（如 Return / Escape / Tab）。写操作，需 allow_actions。

                Args:
                    key: 键名。
                    pid: 目标进程 ID（0 = 当前焦点窗口）。
                """
                return self._safe_computer("key", {"key": key, "pid": int(pid)} if pid else {"key": key})

        # ── B 项：通用 Hana 操作（2026-09-20，用户要求）──────────
        #
        # 用户原话：“需要 CLI 作为通用”。
        # 实现走 HTTP API，**不 spawn `hana.cmd`**：
        #   - CLI 每次启一个 node 进程（status/sessions 实测 1-2s 起步）
        #   - HTTP 约 10ms，且复用已有的 base_url/token 解析
        #   - 不依赖 hana.cmd 的安装路径（用户升级/换版本也不影响）
        # 详见 core/hana_client.py 的模块 docstring。
        #
        # 定位：这几个工具是**只读操作**（查会话/agent/app），
        # “对话”仍走桌宠自己的 WebSocket 链路——不要在这里发消息。

        @app.tool()
        def pet_hana_status() -> dict:
            """查询 Hana 服务器状态（版本/agent/模型/会话数）。只读。

            等价于 `hana status`，但走 HTTP 不启进程。
            """
            try:
                from core.hana_client import HanaClient
                c = HanaClient.from_env()
                st = c.status()
                return {
                    "ok": True,
                    "version": st.get("version"),
                    "agent": st.get("agent"),
                    "agent_id": st.get("agentId"),
                    "model": st.get("model"),
                    "session_count": len(c.list_sessions()),
                    "summary": c.summary(),
                }
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": str(e)[:200]}

        @app.tool()
        def pet_hana_sessions(agent_id: str = "", limit: int = 10) -> dict:
            """列出 Hana 会话（可按 agent 过滤）。只读。

            Args:
                agent_id: 只列该 agent 的会话；空=全部（**跨 agent**）。
                limit: 最多返回几个（按最近修改排序），默认 10。

            等价于 `hana sessions`。用于“管理统筹所有已开启的会话”。
            """
            try:
                from core.hana_client import HanaClient
                c = HanaClient.from_env()
                rows = c.recent_sessions(agent_id=agent_id, limit=max(1, int(limit)))
                return {
                    "ok": True,
                    "count": len(rows),
                    "sessions": [
                        {"session_id": r.get("sessionId"),
                         "agent_id": r.get("agentId"),
                         "title": r.get("title"),
                         "modified": r.get("modified")}
                        for r in rows
                    ],
                }
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": str(e)[:200]}

        @app.tool()
        def pet_hana_agents() -> dict:
            """列出 Hana 的全部 agent（含其他助手）。只读。"""
            try:
                from core.hana_client import HanaClient
                rows = HanaClient.from_env().list_agents()
                return {
                    "ok": True,
                    "count": len(rows),
                    "agents": [{"id": a.get("id"), "name": a.get("name"),
                                "identity": (a.get("identity") or "")[:80]}
                               for a in rows],
                }
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": str(e)[:200]}

        @app.tool()
        def pet_hana_apps() -> dict:
            """列出 Hana 的应用/插件目录。只读。"""
            try:
                from core.hana_client import HanaClient
                rows = HanaClient.from_env().list_apps()
                return {
                    "ok": True,
                    "count": len(rows),
                    "apps": [{"id": a.get("id"), "name": a.get("name"),
                              "version": a.get("version"), "state": a.get("state")}
                             for a in rows],
                }
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": str(e)[:200]}

        return app

    # ── 生命周期 ──

    def start(self) -> bool:
        """后台守护线程启动 MCP server。返回是否成功启动。"""
        if not MCP_AVAILABLE:
            logger.warning("MCP server 未启动：未安装 mcp SDK（pip install mcp）")
            return False
        if self._started:
            return True
        try:
            self._app = self._build_app()
        except Exception as e:
            logger.warning("MCP server 构建失败: %s", e)
            return False

        def _run():
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                # streamable-http：常驻 GUI 进程的合适传输（stdio 需客户端拉起进程）
                self._app.settings.host = "127.0.0.1"
                self._app.settings.port = self._port
                loop.run_until_complete(
                    self._app.run_streamable_http_async()
                )
            except SystemExit as e:
                # ⚠️ uvicorn 端口被占时会 sys.exit(1)。
                # 在非主线程里 SystemExit 会直接终止整个进程（实测：
                # test_real_startup_smoke 因 8979 被占而挂掉）。
                # MCP server 是可选能力，**绝不能因为端口占用而拖死桌宠**。
                logger.warning(
                    "MCP server 启动失败（端口 %d 可能被占），已降级为不提供 MCP: %s",
                    self._port, e,
                )
            except OSError as e:
                # 同样是端口占用（某些路径不走 SystemExit）
                logger.warning(
                    "MCP server 绑定失败（端口 %d 可能被占），已降级: %s",
                    self._port, e,
                )
            except Exception as e:
                logger.warning("MCP server 运行结束/异常: %s", e)
            finally:
                try:
                    loop.close()
                except Exception:
                    logger.debug("MCP loop close 失败", exc_info=True)
                self._started = False

        try:
            self._thread = threading.Thread(
                target=_run, name="PetMCPServer", daemon=True
            )
            self._thread.start()
            self._started = True
            logger.info(
                "PetMCPServer started: %s (allow_actions=%s)",
                self.url(), self._allow_actions,
            )
            return True
        except Exception as e:
            logger.warning("PetMCPServer start failed: %s", e)
            self._thread = None
            self._started = False
            return False

    def stop(self) -> None:
        """停止 MCP server（尽力而为；守护线程随进程退出）。"""
        self._started = False
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception as e:
                logger.debug("MCP loop stop: %s", e)
        self._loop = None
        self._app = None
        logger.info("PetMCPServer stopped")


def _make_computer_provider(bridge) -> Callable[[str, dict], dict]:
    """把 ComputerUseBridge 包成 (op, params) -> dict 的 provider。"""

    def _provider(op: str, params: dict) -> dict:
        p = params or {}
        if op == "status":
            return bridge.status()
        if op == "apps":
            return bridge.list_apps()
        if op == "windows":
            return bridge.list_windows(p.get("pid"))
        if op == "window_state":
            return bridge.window_state(int(p["pid"]), int(p["window_id"]))
        if op == "launch":
            return bridge.launch(aumid=p.get("aumid", ""), name=p.get("name", ""), path=p.get("path", ""))
        if op == "click":
            return bridge.click(
                pid=int(p["pid"]), window_id=p.get("window_id"),
                element_token=p.get("element_token", ""), x=p.get("x"), y=p.get("y"),
            )
        if op == "type":
            return bridge.type_text(text=str(p.get("text", "")), pid=p.get("pid"))
        if op == "key":
            return bridge.press_key(key=str(p.get("key", "")), pid=p.get("pid"))
        return {"error": f"未知 computer op: {op}"}

    return _provider


def build_from_config(
    config: dict,
    state_provider: Callable[[], dict],
    capabilities_provider: Optional[Callable[[], list]] = None,
    action_sink: Optional[Callable[[str, dict], Any]] = None,
    catalog_provider: Optional[Callable[[str], dict]] = None,
    computer_provider: Optional[Callable[[str, dict], dict]] = None,
) -> Optional[PetMCPServer]:
    """按 config 的 `mcp_server` 块构建 server；未启用时返回 None。

    配置项：
        enabled (bool)        默认 False
        port (int)            默认 8979
        allow_actions (bool)  默认 True
        dns_rebinding_protection (bool)  默认 True（保持 SDK 默认，不放松）
        allowed_hosts / allowed_origins  留空 = 内置本机白名单

    另：config `computer_use.enabled=true` 时会自动构建 computer-use provider
    （见 core/computer_use_bridge.py），把桌宠的「手」一并暴露。

    注：不含鉴权（无 token 校验），见模块顶部说明。
    “Origin 校验”不缺——SDK 默认就开，这里只是把它显式钉住。
    """
    cfg = (config or {}).get("mcp_server") or {}
    if not cfg.get("enabled", False):
        logger.info("MCP server 未启用（config mcp_server.enabled=false）")
        return None
    try:
        port = int(cfg.get("port", DEFAULT_PORT) or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    # computer-use 未显式传入时，尝试按 config 构建（未启用则 None，工具不注册）
    if computer_provider is None:
        try:
            from core.computer_use_bridge import build_bridge
            bridge = build_bridge(config)
            if bridge is not None:
                computer_provider = _make_computer_provider(bridge)
                logger.info("computer-use 已接入：驱动=%s allow_actions=%s",
                            bridge.driver_path, bridge.allow_actions)
        except Exception as e:  # noqa: BLE001 — 可选能力，绝不影响 MCP 主功能
            logger.warning("computer-use bridge 构建失败（非致命）: %s", e)
    return PetMCPServer(
        state_provider=state_provider,
        capabilities_provider=capabilities_provider,
        action_sink=action_sink,
        catalog_provider=catalog_provider,
        computer_provider=computer_provider,
        port=port,
        allow_actions=bool(cfg.get("allow_actions", True)),
        transport_security={
            k: cfg[k]
            for k in ("dns_rebinding_protection", "allowed_hosts", "allowed_origins")
            if k in cfg
        },
    )
