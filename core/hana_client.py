"""通用 Hana 操作客户端（B 项，2026-09-20）。

## 它治什么病

用户原话：

> B（需要 CLI 作为通用）

背景：桌宠现在只能做一件事——把用户消息发给 Hana 等回复（WebSocket 长连接）。
但"操作 Hana"远不止说话：

- 列出所有会话（含其他 agent 的）
- 切到某个会话 / 在指定会话里说话
- 看有哪些 agent / app / 插件
- 读 Hana 的运行状态

这些以前只能靠人去点 Hana 界面，或者手动跑 `hana.cmd`（那会每次 spawn 一个
node 进程，`status`/`sessions` 实测每次 1-2 秒起步）。

## 为什么不 spawn CLI 进程

发现：`core/hanako_session_manager.py` **已经在用 HTTP API**（`_request` →
`/api/sessions` 等），和 CLI 走的是**同一个 server**（`hana.cmd` 也是连它）。

所以正确做法是**复用 HTTP 通道**，不是 spawn 进程：
- 无进程启动开销（CLI 每次 1-2s vs HTTP ~10ms）
- 复用已有的 base_url / token 解析（`env_config.get_hanako_config`）
- 不依赖 `hana.cmd` 的具体安装路径（用户可能升级/换版本）

CLI 仍然可用（`hana.cmd`），但那是**给人用的**；桌宠该走 HTTP。

## 与 hanako_session_manager 的分工

| | session_manager | 本模块 |
|---|---|---|
| 定位 | **对话**（发消息、等回复、流式） | **操作**（查/切/管理） |
| 传输 | WebSocket（长连接，低延迟） | HTTP（一次性请求） |
| 用途 | 用户说话 | 列会话、切会话、查目录 |

两者共用同一个 server，不冲突。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class HanaClientError(RuntimeError):
    """Hana 操作失败（连接/鉴权/接口）。"""


class HanaClient:
    """通用 Hana 操作客户端（HTTP，一次性请求）。

    用法::

        c = HanaClient.from_env()
        c.status()                       # 服务器状态
        c.list_sessions()                # 全部会话
        c.list_sessions(agent_id="aimis")  # 按 agent 过滤
        c.list_agents()                  # agent 目录
        c.list_apps()                    # 插件/应用目录

    线程安全：内部用 requests.Session（本身线程安全）。
    """

    def __init__(self, base_url: str, token: str = "",
                 request_timeout: float = 10.0):
        self.base_url = (base_url or "").rstrip("/")
        self._token = (token or "").strip()
        self.request_timeout = max(1.0, float(request_timeout))
        self._http = None

    # ── 构造 ──────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "HanaClient":
        """从环境/配置解析 base_url 与 token（与对话链路同源）。"""
        base_url = "http://127.0.0.1:20099"
        token = ""
        try:
            from env_config import get_hanako_config
            cfg = get_hanako_config() or {}
            base_url = cfg.get("base_url") or base_url
            token = cfg.get("api_token") or ""
        except Exception:
            logger.debug("HanaClient: 读 hanako 配置失败，用默认", exc_info=True)
        return cls(base_url, token)

    def _session(self):
        if self._http is None:
            import requests
            self._http = requests.Session()
        return self._http

    # ── 底层请求 ──────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Accept", "application/json")
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        url = f"{self.base_url}{path}"
        try:
            resp = self._session().request(
                method, url, headers=headers,
                timeout=self.request_timeout, **kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            raise HanaClientError(f"请求失败 {method} {path}: {exc}") from exc
        if resp.status_code >= 400:
            raise HanaClientError(
                f"{method} {path} 返回 {resp.status_code}: {resp.text[:200]}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except Exception:
            return resp.text

    # ── 读操作 ────────────────────────────────────────────

    def status(self) -> dict:
        """服务器状态（版本/agent/模型/网络）。"""
        return self._request("GET", "/api/health") or {}

    def list_sessions(self, agent_id: str = "") -> list[dict]:
        """列出会话（可按 agent 过滤）。

        Returns:
            [{sessionId, path, title, agentId, ...}, ...]
        """
        data = self._request("GET", "/api/sessions")
        rows = data if isinstance(data, list) else (data or {}).get("sessions", [])
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if agent_id and row.get("agentId") != agent_id:
                continue
            out.append(row)
        return out

    def list_agents(self) -> list[dict]:
        """列出 agent 目录。"""
        data = self._request("GET", "/api/agents") or {}
        rows = data.get("agents") if isinstance(data, dict) else data
        return [r for r in (rows or []) if isinstance(r, dict)]

    def list_apps(self) -> list[dict]:
        """列出应用/插件目录。"""
        data = self._request("GET", "/api/apps") or {}
        rows = data.get("plugins") if isinstance(data, dict) else data
        return [r for r in (rows or []) if isinstance(r, dict)]

    # ── 便捷封装 ──────────────────────────────────────────

    def find_session(self, session_id: str) -> dict | None:
        """按 id 找会话（跨 agent）。"""
        for row in self.list_sessions():
            if row.get("sessionId") == session_id:
                return row
        return None

    def recent_sessions(self, agent_id: str = "", limit: int = 10) -> list[dict]:
        """最近会话（按 modified 倒序）。"""
        rows = self.list_sessions(agent_id=agent_id)
        rows.sort(key=lambda r: r.get("modified") or "", reverse=True)
        return rows[:limit]

    def summary(self) -> str:
        """一行概况（进日志/气泡用）。"""
        try:
            st = self.status()
            n = len(self.list_sessions())
            return (f"Hana {st.get('version', '?')} | "
                    f"agent={st.get('agent', '?')} | "
                    f"会话 {n} 个 | 模型 {st.get('model', '?')}")
        except HanaClientError as exc:
            return f"Hana 不可达：{exc}"


__all__ = ["HanaClient", "HanaClientError"]
