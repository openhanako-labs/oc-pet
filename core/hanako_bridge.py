"""hanako_bridge — 让桌宠感知 Hanako 侧的 QQ/微信消息（需求③，只读）

前置事实（2026-09-10 实测确认，勿凭印象改）：
- Hanako server 在 `~/.hanako/server-info.json` 暴露 {port, token, version}；每次重启会变，
  所以**每次轮询都按 mtime 重新读**，不许在进程里长期缓存地址/令牌。
- 读的口子是 HTTP API（不是去读 bridge 的 sync 文件！）：
      GET /api/bridge/status?agentId=<agent>
      GET /api/bridge/sessions?agentId=<agent>
      GET /api/bridge/sessions/<sessionKey>/messages?agentId=<agent>&limit=N
  鉴权：`Authorization: Bearer <token>`。
- `agentId` 走 **query string**（不是 header）。
- 会话形如 `qq_dm_<chatId>@<agent>` / `wx_dm_<userId>@im.wechat@<agent>`；
  消息形如 {"role":"user"|"assistant","content":str,"hasMedia":bool,"mediaCount":int,"ts":ISO8601}。

**只读**：枚举了 server 全部路由，对外只有 `/api/bridge/send-media`（仅媒体）和
`/api/bridge/channels/:name/messages`（共聊频道），**没有给第三方用的纯文本发送口**。
所以本模块刻意不提供任何发送能力——回复仍由 Hanako 侧完成。

护栏（默认收紧）：
- localhost_only：永远用 127.0.0.1:port，**不用 server-info 里广告的内网 IP**
  （该 server 是 0.0.0.0 LAN 模式且未开 TLS，别把 Bearer 令牌往内网广播）。
- agent_id 必须显式配置：不猜、不自动挑，避免意外读到别人的会话。
- owner_only：默认只看 owner 的会话。
- 首次轮询只立水位、**不补历史**（老会话动辄两千条，全倒出来等于刷屏）。
- 去重 + 每小时上限：防止高并发刷屏。
- 全部失败都静默降级：server 没起 / 令牌失效 → 打日志不等价于成功。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

SERVER_INFO_RELPATH = (".hanako", "server-info.json")


def _default_base_and_fn():
    return os.path.join(os.path.expanduser("~"), *SERVER_INFO_RELPATH)


# ─────────────────────────────────────────────────────────────
# Result —— 与 mc_bridge 同源纪律：失败绝不伪装成成功
# ─────────────────────────────────────────────────────────────
class Result:
    __slots__ = ("ok", "value", "error")

    def __init__(self, ok: bool, value: Any = None, error: str = ""):
        self.ok = ok
        self.value = value
        self.error = error

    def __bool__(self) -> bool:
        return self.ok

    @classmethod
    def fail(cls, error: str) -> "Result":
        return cls(False, None, error)

    @classmethod
    def ok_(cls, value: Any = None) -> "Result":
        return cls(True, value, "")


# ─────────────────────────────────────────────────────────────
# server-info（按 mtime 重读）
# ─────────────────────────────────────────────────────────────
class ServerInfoLoader:
    """读 ~/.hanako/server-info.json。带 mtime 缓存，服务器重启后自动换新的。"""

    def __init__(self, path: Optional[str] = None):
        self.path = path or _default_base_and_fn()
        self._mtime: Optional[float] = None
        self._cached: Optional[dict] = None
        self._lock = threading.Lock()

    def load(self) -> Result:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return Result.fail(f"未找到 {self.path}（Hanako server 未运行？）")
        with self._lock:
            if self._cached is not None and self._mtime == mtime:
                return Result.ok_(self._cached)
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    info = json.load(f)
            except Exception as e:  # noqa: BLE001
                return Result.fail(f"读取 server-info 失败：{e}")
            self._cached, self._mtime = info, mtime
            return Result.ok_(info)


# ─────────────────────────────────────────────────────────────
# 客户端（只读）
# ─────────────────────────────────────────────────────────────
class BridgeClient:
    """极薄 HTTP 客户端。只用 GET，只读。"""

    def __init__(self, info_loader: ServerInfoLoader, localhost_only: bool = True,
                 timeout: float = 8.0):
        self.info_loader = info_loader
        self.localhost_only = localhost_only
        self.timeout = timeout

    def _base_and_token(self) -> Result:
        res = self.info_loader.load()
        if not res:
            return res
        info = res.value
        port = info.get("port")
        token = info.get("token")
        if not port or not token:
            return Result.fail("server-info 缺少 port/token")
        host = "127.0.0.1" if self.localhost_only else str(
            info.get("network", {}).get("advertisedHost")
            or info.get("host") or "127.0.0.1"
        )
        return Result.ok_((f"http://{host}:{port}", token))

    def get(self, path: str, params: Optional[dict] = None) -> Result:
        base_res = self._base_and_token()
        if not base_res:
            return base_res
        base, token = base_res.value
        try:
            import requests
            r = requests.get(
                base + path,
                headers={"Authorization": f"Bearer {token}"},
                params=params or {},
                timeout=self.timeout,
            )
        except Exception as e:  # noqa: BLE001 —— 离线/未启动必须优雅降级
            return Result.fail(f"请求 {path} 失败：{e}")
        if r.status_code != 200:
            return Result.fail(f"{path} 返回 {r.status_code}：{r.text[:160]}")
        try:
            return Result.ok_(r.json())
        except Exception as e:  # noqa: BLE001
            return Result.fail(f"{path} 返回非 JSON：{e}")

    # ---- 语义化接口 ----
    def status(self, agent_id: str) -> Result:
        return self.get("/api/bridge/status", {"agentId": agent_id})

    def sessions(self, agent_id: str) -> Result:
        res = self.get("/api/bridge/sessions", {"agentId": agent_id})
        if not res or not isinstance(res.value, dict):
            return res
        return Result.ok_(res.value.get("sessions") or [])

    def messages(self, agent_id: str, session_key: str, limit: int) -> Result:
        from urllib.parse import quote
        path = "/api/bridge/sessions/" + quote(session_key, safe="") + "/messages"
        res = self.get(path, {"agentId": agent_id, "limit": limit})
        if not res:
            return res
        data = res.value
        msgs = data.get("messages") if isinstance(data, dict) else data
        return Result.ok_(msgs or [])


# ─────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────
def load_config(hb_config: Optional[dict] = None) -> dict:
    """构建运行时配置。config.json 的 hanako_bridge: 块覆盖环境变量后备。"""
    cfg = {
        "enabled": False,
        "agent_id": "",
        "platforms": ["qq", "wechat"],
        "owner_only": True,
        "max_per_hour": 10,
        "poll_interval": 30.0,
        "message_limit": 20,
        "localhost_only": True,
        "timeout": 8.0,
    }
    env_agent = os.environ.get("OC_HANAKO_AGENT", "").strip()
    if env_agent:
        cfg["agent_id"] = env_agent
        cfg["enabled"] = True

    if hb_config:
        for k in ("enabled", "owner_only", "localhost_only"):
            if k in hb_config:
                cfg[k] = bool(hb_config[k])
        if "agent_id" in hb_config:
            cfg["agent_id"] = str(hb_config.get("agent_id") or "").strip()
        pl = hb_config.get("platforms")
        if isinstance(pl, (list, tuple)) and pl:
            cfg["platforms"] = [str(x).strip().lower() for x in pl if str(x).strip()]
        elif isinstance(pl, str) and pl.strip():
            cfg["platforms"] = [x.strip() for x in pl.replace("，", ",").split(",") if x.strip()]
        for k in ("max_per_hour", "message_limit"):
            if k in hb_config:
                try:
                    cfg[k] = int(hb_config[k])
                except (TypeError, ValueError):
                    logger.debug("hanako_bridge: 非致命异常(已静默吞掉)", exc_info=True)
        for k in ("poll_interval", "timeout"):
            if k in hb_config:
                try:
                    cfg[k] = float(hb_config[k])
                except (TypeError, ValueError):
                    logger.debug("hanako_bridge: 非致命异常(已静默吞掉)", exc_info=True)
    return cfg


def _parse_ts(ts: Any) -> Optional[datetime]:
    """解析 ISO8601（含 Z 后缀）；解析不了就 None，宁缺勿滥。"""
    if not ts or not isinstance(ts, str):
        return None
    try:
        t = ts.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(t)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _fingerprint(msg: dict) -> str:
    raw = f"{msg.get('role')}|{msg.get('ts')}|{msg.get('content')}"
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────
# 观察者
# ─────────────────────────────────────────────────────────────
class IncomingWatcher(threading.Thread):
    """后台守护线程：轮询 bridge，把**新到的** user 消息交给回调。

    设计要点：
    - 首轮只立水位不投递（历史会话几千条，全投等于刷屏）。
    - 去重按 (role, ts, content) 指纹，防止 limit 窗口抖动导致重复播报。
    - 每小时上限，超出就丢弃并记录——宁可少说，不可刷屏。
    - server 没起也只是 debug 日志，绝不把异常抛出去。
    """

    def __init__(self, config: dict, client: BridgeClient,
                 on_incoming: Optional[Callable[[list], None]] = None):
        super().__init__(daemon=True, name="hanako-bridge-watcher")
        self.cfg = config
        self.client = client
        self.on_incoming = on_incoming
        self._stop = threading.Event()
        self._watermark: dict[str, Optional[datetime]] = {}   # sessionKey -> 最新已见 ts
        self._file_stamp: dict[str, tuple] = {}               # sessionKey -> (size, mtime) 变更检测
        self._seen: set[str] = set()
        self._hour_bucket: list[float] = []
        self._primed = False

    @staticmethod
    def _session_stamp(session: dict):
        """会话 jsonl 的 (size, mtime)：廉价的变更检测信号。

        实测 `/messages` 端点**忽略 limit/after/since 所有参数**，永远返回整段会话
        （某 QQ 会话一次 690 KB），所以每次轮询都去拉是白白烧 CPU。
        会话的实时落盘文件就在 sessions[].sessionPath，先 stat 一下：
        文件没变 → 连请求都不发，直接跳过。拿不到路径就退化成每次都拉。
        """
        path = session.get("sessionPath") or ""
        if not path:
            return None
        try:
            st = os.stat(path)
            return (st.st_size, int(st.st_mtime))
        except OSError:
            return None

    # ---- 限流 ----
    def _within_budget(self) -> bool:
        now = time.time()
        self._hour_bucket = [t for t in self._hour_bucket if now - t < 3600]
        cap = int(self.cfg.get("max_per_hour", 0) or 0)
        if cap <= 0:
            return True
        return len(self._hour_bucket) < cap

    def _consume_budget(self) -> None:
        self._hour_bucket.append(time.time())

    # ---- 筛选 ----
    def _allowed_session(self, s: dict) -> bool:
        platform = str(s.get("platform") or "").lower()
        if platform not in [str(p).lower() for p in self.cfg.get("platforms", [])]:
            return False
        if self.cfg.get("owner_only", True) and not s.get("isOwner", False):
            return False
        return True

    # ---- 主循环 ----
    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        agent = self.cfg.get("agent_id", "")
        if not agent:
            logger.warning("[hanako_bridge] 未配置 agent_id，观察者未启动")
            return
        while not self._stop.is_set():
            try:
                self._poll_once(agent)
            except Exception as e:  # noqa: BLE001 —— 后台线程绝不能炸穿
                logger.debug("[hanako_bridge] 轮询异常（已忽略）：%s", e)
            self._stop.wait(max(5.0, float(self.cfg.get("poll_interval", 30.0))))

    def _poll_once(self, agent: str) -> None:
        res = self.client.sessions(agent)
        if not res:
            logger.debug("[hanako_bridge] 取会话失败（server 可能没起）：%s", res.error)
            return
        sessions = [s for s in (res.value or []) if self._allowed_session(s)]

        fresh: list[dict] = []
        for s in sessions:
            key = s.get("sessionKey") or ""
            if not key:
                continue
            # 文件没动过 -> 直接跳过（该端点忽略 limit，稳态下没必要反复拉整段会话）
            stamp = self._session_stamp(s)
            if stamp is not None and self._file_stamp.get(key) == stamp:
                continue
            mres = self.client.messages(agent, key, int(self.cfg.get("message_limit", 20)))
            if not mres:
                continue
            msgs = [m for m in (mres.value or [])
                    if isinstance(m, dict) and m.get("role") == "user"]
            newest = None
            for m in msgs:
                dt = _parse_ts(m.get("ts"))
                if dt is None:
                    continue
                newest = dt if newest is None or dt > newest else newest
                fp = _fingerprint(m)
                if fp in self._seen:
                    continue
                wm = self._watermark.get(key)
                if wm is not None and dt <= wm:
                    continue
                if not self._primed:
                    continue          # 首轮只记水位，不投历史
                item = {
                    "session_key": key,
                    "platform": s.get("platform"),
                    "from": s.get("displayName") or s.get("chatId") or "某人",
                    "content": str(m.get("content") or "").strip(),
                    "ts": dt,
                    "has_media": bool(m.get("hasMedia")),
                }
                if not item["content"] and not item["has_media"]:
                    continue
                self._seen.add(fp)
                if len(self._seen) > 4000:      # 长期运行别让集合无限涨
                    self._seen = set(list(self._seen)[-2000:])
                fresh.append(item)
            if newest is not None:
                prev = self._watermark.get(key)
                self._watermark[key] = newest if prev is None or newest > prev else prev
            if stamp is not None:
                self._file_stamp[key] = stamp

        if not self._primed:
            self._primed = True
            logger.info("[hanako_bridge] 已完成水位对齐（%d 个会话），之后只报新消息", len(sessions))
            return

        if fresh:
            if self._within_budget():
                self._consume_budget()
                if self.on_incoming:
                    try:
                        self.on_incoming(fresh)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("[hanako_bridge] on_incoming 回调异常：%s", e)
            else:
                logger.info("[hanako_bridge] 已达每小时上限，丢弃 %d 条（不是错误）", len(fresh))


# ─────────────────────────────────────────────────────────────
# 对外入口
# ─────────────────────────────────────────────────────────────
def init_hanako_bridge(hb_config: Optional[dict] = None,
                       server_info_path: Optional[str] = None,
                       on_incoming: Optional[Callable[[list], None]] = None
                       ) -> Optional[IncomingWatcher]:
    """按配置启动观察者。未启用/配置不全返回 None（不注册任何东西）。"""
    cfg = load_config(hb_config)
    if not cfg["enabled"]:
        logger.info("[hanako_bridge] 未启用（在设置「💬 QQ/微信」页打开，或设 OC_HANAKO_AGENT）")
        return None
    if not cfg["agent_id"]:
        logger.warning("[hanako_bridge] enabled=True 但未填 agent_id；拒绝猜测，不启动")
        return None

    loader = ServerInfoLoader(server_info_path)
    # 先探一次，server 没起就别开线程空转
    probe = loader.load()
    if not probe:
        logger.warning("[hanako_bridge] %s；未启动观察者（启动后需重启桌宠或保存一次设置）", probe.error)

    client = BridgeClient(loader, localhost_only=bool(cfg["localhost_only"]),
                          timeout=float(cfg["timeout"]))
    watcher = IncomingWatcher(cfg, client, on_incoming=on_incoming)
    watcher.start()
    logger.info("[hanako_bridge] 观察者已启动（agent=%s, 平台=%s, 间隔=%ss）",
                cfg["agent_id"], ",".join(cfg["platforms"]), cfg["poll_interval"])
    return watcher
