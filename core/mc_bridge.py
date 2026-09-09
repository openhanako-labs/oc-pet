"""mc_bridge — 让桌宠接入 Minecraft 游戏（需求②原型，单模块）

设计要点（详见 oc-pet-插件接口与MC接入设计-2026-09-09.md §4）：
- 两个 transport 并存，语义不同（这是读完 minecraft-mcp 源码后的关键修正）：
  * ws   (mc-agent-neko, 默认 :48909)：高层 `task` 协议 —— 派一个目标，bot 自己玩，
          回 `task_finished` / `log` / `screenshot`。对应「看 bot 玩 MC」体验。
  * http (minecraft-mcp, 默认 :8765)：JSON-RPC **方法桥**（POST /rpc + Bearer token，
          恒定时间比对）—— 没有「自主任务执行器」，只有原子方法（读状态/建造/设块…）。
          对应「桌宠调用具体 MC 方法」。
- 零新依赖：http 走 `requests`（已在 requirements），ws 走 `websocket-client`（已在 requirements）。
- 懒连接、离线优雅降级、不阻塞主循环：mc_task 火速返回「已派发」，结果经回调异步回报。
- 不碰 pet.py 主循环；只通过 capability_registry.register_capability 注入能力。

配置（环境变量，避免硬依赖 config 模块；也可以后并入 oc-pet config）：
  OC_MC_TRANSPORT   http | ws   （默认 http，最稳、零配置即连本机 minecraft-mcp）
  OC_MC_HTTP_URL    http://127.0.0.1:8765
  OC_MC_WS_URL      ws://127.0.0.1:48909
  OC_MC_TOKEN       minecraft-mcp 的 Bearer 令牌（与 MCPBRIDGE_TOKEN 同源）
  OC_MC_TIMEOUT     单任务最长等待秒数（默认 120）
  OC_MC_HTTP_TIMEOUT 单次 HTTP 方法调用超时毫秒（默认 10000）
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Result 类型 —— 对应 N.E.K.O 的 status 分档：绝不静默当成功
# ─────────────────────────────────────────────────────────────
@dataclass
class McResult:
    ok: bool = True
    status: str = "ok"            # ok | interrupted | failed
    value: Any = None
    error: str = ""
    raw: Any = None

    def __bool__(self) -> bool:
        return self.ok

    @classmethod
    def fail(cls, error: str, status: str = "failed") -> "McResult":
        return cls(ok=False, status=status, error=error)


# ─────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────
def _load_config() -> dict:
    return {
        "transport": os.environ.get("OC_MC_TRANSPORT", "http").strip().lower(),
        "ws_url": os.environ.get("OC_MC_WS_URL", "ws://127.0.0.1:48909").strip(),
        "http_url": os.environ.get("OC_MC_HTTP_URL", "http://127.0.0.1:8765").strip().rstrip("/"),
        "token": (os.environ.get("OC_MC_TOKEN") or os.environ.get("MCPBRIDGE_TOKEN") or "").strip(),
        "task_timeout": float(os.environ.get("OC_MC_TIMEOUT", "120")),
        "http_timeout_ms": int(os.environ.get("OC_MC_HTTP_TIMEOUT", "10000")),
    }


# ─────────────────────────────────────────────────────────────
# HTTP transport（minecraft-mcp 方法桥，零依赖 requests）
# ─────────────────────────────────────────────────────────────
class HttpTransport:
    """连 minecraft-mcp 的模组内嵌 HTTP 桥：POST /rpc + Bearer token。"""

    def __init__(self, url: str, token: str, timeout_ms: int):
        self.url = url
        self.token = token
        self.timeout_ms = timeout_ms
        self._lock = threading.Lock()

    # 健康检查无需鉴权
    def health(self) -> McResult:
        try:
            import requests
            r = requests.get(f"{self.url}/health", timeout=self.timeout_ms / 1000.0)
            if r.status_code == 200:
                return McResult(ok=True, value=r.json())
            return McResult.fail(f"/health 返回 {r.status_code}")
        except Exception as e:  # noqa: BLE001 —— 离线/未启动必须优雅降级
            return McResult.fail(f"无法连接 minecraft-mcp HTTP 桥：{e}")

    def call_method(self, name: str, params: Optional[dict] = None) -> McResult:
        if not self.token:
            return McResult.fail("缺少 OC_MC_TOKEN / MCPBRIDGE_TOKEN（minecraft-mcp 首次启动后生成）")
        payload = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": name,
            "params": params or {},
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        try:
            import requests
            r = requests.post(
                f"{self.url}/rpc",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                timeout=self.timeout_ms / 1000.0,
            )
            body = r.json()
        except Exception as e:  # noqa: BLE001
            return McResult.fail(f"调用方法 {name} 失败：{e}")

        if "error" in body and body["error"]:
            err = body["error"]
            return McResult.fail(f"{err.get('message', '未知错误')}", status="failed")
        return McResult(ok=True, status="ok", value=body.get("result"))


# ─────────────────────────────────────────────────────────────
# WS transport（mc-agent-neko ：48909 高层 task 协议）
# ─────────────────────────────────────────────────────────────
class WsTransport:
    """连 mc-agent-neko 的 WS 服务，派高层 task，异步回 task_finished。

    后台守护线程跑 websocket；dispatch_task 火速返回「已派发」，
    结果经 self.on_task_finished(task_id, McResult) 回调异步回报，绝不阻塞调用方。
    """

    def __init__(self, url: str, task_timeout: float):
        self.url = url
        self.task_timeout = task_timeout
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._pending: dict[str, Callable[[McResult], None]] = {}
        self.on_task_finished: Optional[Callable[[str, McResult], None]] = None
        self.connected = False

    def connect(self) -> McResult:
        try:
            import websocket  # websocket-client
        except ImportError:
            return McResult.fail("未安装 websocket-client（pip install websocket-client）")
        try:
            self._ws = websocket.WebSocketApp(
                self.url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=lambda *a: logger.warning("[mc_bridge] ws error: %s", a[-1]),
                on_close=lambda *a: setattr(self, "connected", False),
            )
            self._thread = threading.Thread(target=self._ws.run_forever, daemon=True)
            self._thread.start()
            return McResult(ok=True, value="connecting")
        except Exception as e:  # noqa: BLE001
            return McResult.fail(f"WS 连接失败：{e}")

    def _on_open(self, ws):  # noqa: ANN001
        self.connected = True
        logger.info("[mc_bridge] WS 已连接 %s", self.url)

    def _on_message(self, ws, message):  # noqa: ANN001
        try:
            data = json.loads(message)
        except Exception:  # noqa: BLE001
            return
        t = data.get("type")
        if t == "task_finished":
            tid = data.get("task_id")
            cb = self._pending.pop(tid, None) if tid else None
            res = self._grade(data)
            if cb:
                cb(res)
            if self.on_task_finished:
                self.on_task_finished(tid, res)
        # log / screenshot / agent_status 等：原型阶段仅记录，P4 接入 proactive_state
        elif t in ("log", "screenshot", "agent_status"):
            logger.debug("[mc_bridge] %s frame received", t)

    @staticmethod
    def _grade(data: dict) -> McResult:
        status = str(data.get("status", "ok")).lower()
        text = data.get("text") or data.get("error") or ""
        if status in ("ok",):
            return McResult(ok=True, status="ok", value=text)
        if status in ("interrupted", "superseded"):
            return McResult(ok=True, status="interrupted", value=text)
        # failed / timeout / 断连 / 未知 —— 一律当失败，绝不报成 ok
        return McResult(ok=False, status="failed", error=text or f"status={status}")

    def dispatch_task(self, goal: str, on_done: Callable[[McResult], None]) -> McResult:
        if not self.connected or self._ws is None:
            # 尝试建连（惰性）
            res = self.connect()
            if not self.connected:
                return McResult.fail("MC agent(WS) 未连接：请先启动 mc-agent-neko")
        tid = uuid.uuid4().hex
        self._pending[tid] = on_done
        try:
            self._ws.send(json.dumps({"type": "task", "task": goal, "task_id": tid}))
        except Exception as e:  # noqa: BLE001
            self._pending.pop(tid, None)
            return McResult.fail(f"派发任务失败：{e}")
        return McResult(ok=True, status="ok", value="dispatched", raw=tid)


# ─────────────────────────────────────────────────────────────
# 桥接主体
# ─────────────────────────────────────────────────────────────
class GameBridge:
    def __init__(self, config: Optional[dict] = None):
        self.cfg = config or _load_config()
        self.transport_kind = self.cfg["transport"]
        self.http = HttpTransport(self.cfg["http_url"], self.cfg["token"], self.cfg["http_timeout_ms"])
        self.ws = WsTransport(self.cfg["ws_url"], self.cfg["task_timeout"])
        self._result_handlers: list[Callable[[McResult, str], None]] = []
        # WS 结果回调控本桥 -> 对外回调
        self.ws.on_task_finished = self._on_ws_finished

    def on_result(self, handler: Callable[[McResult, str], None]) -> None:
        """注册结果回调（参数：McResult, 来源标识 "mc_task"/"mc_method"）。"""
        self._result_handlers.append(handler)

    def _emit(self, res: McResult, src: str) -> None:
        for h in self._result_handlers:
            try:
                h(res, src)
            except Exception as e:  # noqa: BLE001
                logger.warning("[mc_bridge] result handler error: %s", e)

    def _on_ws_finished(self, tid: str, res: McResult) -> None:
        self._emit(res, "mc_task")

    # ---- 外部能力入口 ----
    def dispatch_task(self, goal: str) -> McResult:
        if self.transport_kind == "ws":
            def _done(r: McResult) -> None:
                self._emit(r, "mc_task")
            return self.ws.dispatch_task(goal, _done)
        # http transport 没有自主任务执行器
        return McResult.fail(
            "当前为 minecraft-mcp HTTP 桥，无自主任务执行器。"
            "请用 mc_method 调用具体方法，或把 OC_MC_TRANSPORT 设为 ws 连接 mc-agent-neko。"
        )

    def dispatch_method(self, name: str, params: Optional[dict] = None) -> McResult:
        if self.transport_kind != "http":
            return McResult.fail("mc_method 仅适用于 http(minecraft-mcp) transport。")
        return self.http.call_method(name, params)


# ─────────────────────────────────────────────────────────────
# 注册为 oc-pet 能力（最小 core 改动：capability_registry 扩展点）
# ─────────────────────────────────────────────────────────────
_GOAL_RE = re.compile(
    r"^(?:让|派|叫|让桌宠的)?\s*(?:bot|机器人|桌宠)?\s*[:：]?\s*(.*)$", re.IGNORECASE
)
_METHOD_RE = re.compile(
    # 方法名用 ASCII 字符类（MC 方法名如 getInventory 都是 ASCII），
    # 不能用 \w（Python 里会匹配中日韩字符，把 "mc方法" 整个吞成方法名）。
    r"^(?:调用|调|执行)?\s*(?:mc|minecraft|我的世界)?\s*(?:方法|method)?\s*[:：]?\s*([A-Za-z_][A-Za-z0-9_.]*)\s*(?:\((.*)\))?\s*$",
    re.IGNORECASE,
)


def _extract_goal(text: str) -> str:
    t = text.strip()
    # 去掉已知触发前缀（让bot / mc任务: / minecraft任务 等）
    for p in ("让bot", "让 bot", "派bot", "派 bot", "让机器人", "让桌宠的bot",
              "mc任务:", "mc任务", "minecraft任务:", "minecraft任务",
              "我的世界任务:", "我的世界任务", "mc task", "minecraft task"):
        if t.lower().startswith(p.lower()):
            t = t[len(p):]
            break
    t = t.strip(" :：")
    return t or text.strip()


def init_mc_bridge() -> Optional[GameBridge]:
    """初始化 mc_bridge 并注册能力。返回桥接实例（供测试/调试），未启用时返回 None。

    显式启用才注册能力，避免无 MC 配置的用户被注入一个只会报错的能力：
      - 设置 OC_MC_ENABLE=1，或
      - 设置了 OC_MC_TRANSPORT（http/ws）
    即视为启用。
    """
    _enabled = (
        os.environ.get("OC_MC_ENABLE", "").strip().lower() in ("1", "true", "yes", "on")
        or bool(os.environ.get("OC_MC_TRANSPORT"))
    )
    if not _enabled:
        logger.info("[mc_bridge] 未配置（设置 OC_MC_ENABLE=1 或 OC_MC_TRANSPORT 开启）；跳过能力注册")
        return None

    from core.capability_registry import Capability, RouteResult, register_capability

    cfg = _load_config()
    bridge = GameBridge(cfg)

    # 默认回调：记录结果（P4 再接 proactive_state 主动播报）
    bridge.on_result(lambda res, src: logger.info("[mc_bridge] %s 结果: %s", src, res))

    if cfg["transport"] == "ws":
        def task_handler(text: str) -> RouteResult:
            goal = _extract_goal(text)
            res = bridge.dispatch_task(goal)
            if not res.ok:
                return RouteResult(capability="mc_task", text=res.error, emotion="sad", anim="idle")
            return RouteResult(
                capability="mc_task",
                text=f"已派发任务给 bot：{goal}（最长等待 {int(cfg['task_timeout'])}s，完成后回报）",
                emotion="neutral", anim="idle",
            )
        register_capability(Capability(
            name="mc_task",
            patterns=["让bot", "让 bot", "派bot", "派 bot", "mc任务", "minecraft任务",
                      "我的世界任务", "mc task", "minecraft task"],
            handler="callable", callable=task_handler,
            description="让 MC bot（mc-agent-neko）去执行一个任务",
            emotion="neutral", anim="idle",
        ))

    if cfg["transport"] == "http":
        _NON_METHOD_TOKENS = {"mc", "minecraft", "方法", "method", "我的世界", "task"}
        def method_handler(text: str) -> RouteResult:
            m = _METHOD_RE.match(text.strip())
            # 裸触发词（如「mc方法」「调用mc」）正则会把 "mc" 本身当方法名，
            # 这里兜底成用法提示，避免把它当成名为 mc 的方法去调用。
            if not m or not m.group(1) or m.group(1).lower() in _NON_METHOD_TOKENS:
                return RouteResult(
                    capability="mc_method",
                    text="用法示例：「调用 mc 方法 getInventory」或「minecraft:getPosition」",
                    emotion="neutral", anim="idle",
                )
            name = m.group(1)
            params = {}
            if m.group(2):
                try:
                    params = json.loads(m.group(2)) if m.group(2).strip().startswith("{") \
                        else {"arg": m.group(2).strip()}
                except Exception:  # noqa: BLE001
                    params = {"arg": m.group(2).strip()}
            res = bridge.dispatch_method(name, params)
            if not res.ok:
                return RouteResult(capability="mc_method", text=res.error, emotion="sad", anim="idle")
            return RouteResult(
                capability="mc_method",
                text=f"[{name}] → {json.dumps(res.value, ensure_ascii=False)[:400]}",
                emotion="happy", anim="extra",
            )
        register_capability(Capability(
            name="mc_method",
            patterns=["调用mc", "调mc", "mc方法", "minecraft方法", "我的世界方法",
                      "调用minecraft", "call mc", "mc method"],
            handler="callable", callable=method_handler,
            description="调用 minecraft-mcp 桥的具体方法（读状态/建造/设块…）",
            emotion="happy", anim="extra",
        ))

    logger.info("[mc_bridge] initialized (transport=%s)", cfg["transport"])
    return bridge
