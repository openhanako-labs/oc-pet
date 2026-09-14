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
      "auth_token": "",
      "allow_actions": true
    }

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
    """

    def __init__(
        self,
        state_provider: Callable[[], dict],
        capabilities_provider: Optional[Callable[[], list]] = None,
        action_sink: Optional[Callable[[str, dict], Any]] = None,
        catalog_provider: Optional[Callable[[str], dict]] = None,
        port: int = DEFAULT_PORT,
        allow_actions: bool = True,
    ):
        self._state_provider = state_provider
        self._capabilities_provider = capabilities_provider
        self._action_sink = action_sink
        self._catalog_provider = catalog_provider
        self._port = int(port or DEFAULT_PORT)
        self._allow_actions = bool(allow_actions)
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
        app = FastMCP(
            "oc-pet",
            instructions=(
                "OC Pet 桌宠的能力接口。可用工具查询桌宠当前状态、"
                "列举它自己能做的事，以及驱动它的表现（情绪/动画/表情/说话）。\n"
                "注意：桌宠是被看着的角色，请勿高频刷写动作；"
                "表现类动作为异步生效，不阻塞。"
            ),
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


def build_from_config(
    config: dict,
    state_provider: Callable[[], dict],
    capabilities_provider: Optional[Callable[[], list]] = None,
    action_sink: Optional[Callable[[str, dict], Any]] = None,
    catalog_provider: Optional[Callable[[str], dict]] = None,
) -> Optional[PetMCPServer]:
    """按 config 的 `mcp_server` 块构建 server；未启用时返回 None。

    配置项：
        enabled (bool)        默认 False
        port (int)            默认 8979
        allow_actions (bool)  默认 True
    """
    cfg = (config or {}).get("mcp_server") or {}
    if not cfg.get("enabled", False):
        logger.info("MCP server 未启用（config mcp_server.enabled=false）")
        return None
    try:
        port = int(cfg.get("port", DEFAULT_PORT) or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    return PetMCPServer(
        state_provider=state_provider,
        capabilities_provider=capabilities_provider,
        action_sink=action_sink,
        catalog_provider=catalog_provider,
        port=port,
        allow_actions=bool(cfg.get("allow_actions", True)),
    )
