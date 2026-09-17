"""core/hana_catalog.py — Hana 全体系发现器（DISC-1）

目标：让桌宠知道 Hana **完整**有什么，而不只是 v1 插件 manifest 的 tools 字段。

背景（核实所得，勿凭印象改）
------------------------------
Hana 实际有五套体系，桌宠此前只读了不到三分之一：

| 体系 | 数量 | 位置 | 旧状态 |
|---|---|---|---|
| plugins (v1) | 26 | `~/.hanako/plugins/` | 只读 manifest 的 contributes.tools |
| Apps (v2) | 9~10 | `~/.hanako/apps/` | 完全不读 |
| MCP connectors | 3 | `~/.hanako/plugin-data/mcp/config.json` | 不读 |
| skills | 92 | `~/.hanako/skills/` | 不读 |
| agents | 7 | `~/.hanako/agents/` | 只读角色设定 |

双通道设计（关键）
--------------------
- **API 优先**：`GET /api/plugins`、`/api/apps`、`/api/agents` 带运行时状态
  （loaded/disabled/activationState），信息最全
- **静态回退**：server 没起时直接扫目录，仍能给出清单（但无运行时状态）

> token 每次重启会变 → **每次请求按 mtime 重读 server-info.json**，
> 绝不在进程内长期缓存（`core/hanako_bridge.py:75` 已有此教训）。

范围边界
----------
**只读清单，不读数据**——拿工具名/描述/参数/激活状态，
**不读**待办正文、笔记内容、会话消息等用户数据。

安全
----
只连 `127.0.0.1`，不用 `server-info.json` 里广告的内网 IP
（该 server 是 LAN 模式且未开 TLS，`hanako_bridge.py:20` 的教训）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

from hanako_home import hanako_home

HANAKO_HOME = hanako_home()
SERVER_INFO = HANAKO_HOME / "server-info.json"
PLUGINS_DIR = HANAKO_HOME / "plugins"
APPS_DIR = HANAKO_HOME / "apps"
SKILLS_DIR = HANAKO_HOME / "skills"
AGENTS_DIR = HANAKO_HOME / "agents"
MCP_CONFIG = HANAKO_HOME / "plugin-data" / "mcp" / "config.json"

# 缓存 TTL（秒）——扫描有成本，但状态会变
DEFAULT_TTL = 60.0


# ── server-info / HTTP ──


def _read_server_info() -> Optional[dict]:
    """读 server-info.json。**每次都重读**（token 每次重启会变）。"""
    try:
        if not SERVER_INFO.is_file():
            return None
        data = json.loads(SERVER_INFO.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        port = data.get("port")
        token = data.get("token")
        if not port or not token:
            return None
        return {"port": int(port), "token": str(token)}
    except Exception as e:
        logger.debug("读 server-info.json 失败: %s", e)
        return None


def _api_get(path: str, timeout: float = 5.0) -> Optional[Any]:
    """调 Hana 本地 API。失败返回 None（调用方回退静态扫描）。

    只连 127.0.0.1——不用 server-info 广告的内网 IP。
    """
    info = _read_server_info()
    if info is None:
        return None
    url = f"http://127.0.0.1:{info['port']}{path}"
    try:
        import requests
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {info['token']}"},
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.debug("API %s 返回 %s", path, r.status_code)
            return None
        return r.json()
    except Exception as e:
        logger.debug("API %s 调用失败: %s", path, e)
        return None


# ── 各体系发现 ──


def _discover_plugins() -> dict:
    """plugins v1：静态扫 manifest + API 补运行时状态。"""
    items: list[dict] = []
    # 静态：扫目录
    try:
        if PLUGINS_DIR.is_dir():
            for d in sorted(PLUGINS_DIR.iterdir()):
                if not d.is_dir():
                    continue
                mf = d / "manifest.json"
                if not mf.is_file():
                    continue
                try:
                    m = json.loads(mf.read_text(encoding="utf-8"))
                except Exception:
                    continue
                contributes = m.get("contributes") or {}
                tools_raw = contributes.get("tools") or []
                items.append({
                    "id": m.get("id") or d.name,
                    "name": m.get("name") or d.name,
                    "version": m.get("version") or "",
                    "description": m.get("description") or "",
                    "has_tools": bool(tools_raw),
                    "tool_count": len(tools_raw) if isinstance(tools_raw, list) else 0,
                    "status": "unknown",  # API 补
                })
    except Exception as e:
        logger.warning("扫描 plugins 目录失败: %s", e)

    # API：补运行时状态
    api = _api_get("/api/plugins")
    if isinstance(api, list):
        by_id = {p.get("id"): p for p in api if isinstance(p, dict)}
        for it in items:
            p = by_id.get(it["id"])
            if p:
                it["status"] = p.get("status") or "unknown"
                it["activation_state"] = p.get("activationState")
                it["contributions"] = p.get("contributions") or []
                if p.get("description"):
                    it["description"] = p["description"]
        # API 里有但目录扫不到的（如内置插件）
        known = {it["id"] for it in items}
        for p in api:
            if not isinstance(p, dict) or p.get("id") in known:
                continue
            items.append({
                "id": p.get("id"),
                "name": p.get("name") or p.get("id"),
                "version": p.get("version") or "",
                "description": p.get("description") or "",
                "has_tools": "tools" in (p.get("contributions") or []),
                "tool_count": 0,
                "status": p.get("status") or "unknown",
                "activation_state": p.get("activationState"),
                "contributions": p.get("contributions") or [],
                "source": "api-only",
            })
    return {"count": len(items), "items": items}


def _discover_apps() -> dict:
    """Apps v2：API + 静态扫目录 + 解析 tools/*.js。

    v2 的工具是**编程式注册**的，但工具文件导出结构与 v1 一致
    （`export const name/description/parameters` + `execute`），
    所以可以静态解析拿到清单。
    """
    items: list[dict] = []
    try:
        if APPS_DIR.is_dir():
            for d in sorted(APPS_DIR.iterdir()):
                if not d.is_dir():
                    continue
                mf = d / "manifest.json"
                if not mf.is_file():
                    continue
                try:
                    m = json.loads(mf.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if m.get("manifestVersion") != 2:
                    continue  # 只认 v2
                tools = _scan_tool_files(d / "tools")
                items.append({
                    "id": m.get("id") or d.name,
                    "name": m.get("name") or d.name,
                    "version": m.get("version") or "",
                    "description": m.get("description") or "",
                    "capabilities": m.get("capabilities") or [],
                    "tool_count": len(tools),
                    "tools": tools,
                    "status": "unknown",
                })
    except Exception as e:
        logger.warning("扫描 apps 目录失败: %s", e)

    # API 补状态（返回结构是 {plugins: [...]}）
    api = _api_get("/api/apps")
    if isinstance(api, dict):
        lst = api.get("plugins")
        if isinstance(lst, list):
            by_id = {p.get("id"): p for p in lst if isinstance(p, dict)}
            for it in items:
                p = by_id.get(it["id"])
                if p:
                    it["status"] = p.get("state") or "unknown"
                    if p.get("capabilities"):
                        it["capabilities"] = p["capabilities"]
    return {"count": len(items), "items": items}


def _scan_tool_files(tools_dir: Path) -> list[dict]:
    """解析 tools/*.js 的导出声明（v1/v2 结构一致）。

    2026-09-17：解析逻辑抽到 `core/js_tool_parser.py`（单一实现）。
    本函数只取清单所需字段（name/description/file），不取完整 parameters。
    """
    from core.js_tool_parser import iter_tool_files, parse_tool_summary

    return [parse_tool_summary(f) for f in iter_tool_files(tools_dir)]


def _re_str(src: str, var: str) -> Optional[str]:
    """从 JS 源码提取 `export const <var> = '...'` 的字符串值。"""
    m = re.search(
        rf"(?:export\s+)?(?:const|let|var)\s+{var}\s*=\s*['\"]([^'\"]+)['\"]",
        src,
    )
    return m.group(1) if m else None


def _discover_mcp() -> dict:
    """MCP connectors：读 config.json（含缓存的 tools 列表）。"""
    items: list[dict] = []
    try:
        if MCP_CONFIG.is_file():
            cfg = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
            conns = ((cfg.get("global") or {}).get("mcp") or {}).get("connectors") or []
            for c in conns:
                if not isinstance(c, dict):
                    continue
                tools = c.get("tools") or []
                items.append({
                    "id": c.get("id"),
                    "name": c.get("name") or c.get("id"),
                    "transport": c.get("transport"),
                    "enabled": bool(c.get("enabled", False)),
                    "tool_count": len(tools) if isinstance(tools, list) else 0,
                    "tools": [
                        {"name": t.get("name"), "description": (t.get("description") or "")[:150]}
                        for t in tools if isinstance(t, dict)
                    ] if isinstance(tools, list) else [],
                })
    except Exception as e:
        logger.warning("读 MCP 配置失败: %s", e)
    return {"count": len(items), "items": items}


def _discover_skills() -> dict:
    """skills：扫 SKILL.md 的 frontmatter（name/description）。

    注意两个实测例外：
      - 92 个目录中 91 个有 SKILL.md（`expert/` 没有）→ 容错跳过
      - `last30days-cn/` 有嵌套子技能（skills/*/skills/*）→ 扫两层
    """
    items: list[dict] = []
    try:
        if not SKILLS_DIR.is_dir():
            return {"count": 0, "items": []}
        for d in sorted(SKILLS_DIR.iterdir()):
            if not d.is_dir():
                continue
            md = d / "SKILL.md"
            if md.is_file():
                info = _parse_frontmatter(md)
                items.append({
                    "name": info.get("name") or d.name,
                    "description": (info.get("description") or "")[:200],
                    "trigger_keywords": info.get("trigger_keywords") or [],
                })
            # 嵌套子技能
            sub = d / "skills"
            if sub.is_dir():
                for sd in sorted(sub.iterdir()):
                    if not sd.is_dir():
                        continue
                    smd = sd / "SKILL.md"
                    if smd.is_file():
                        info = _parse_frontmatter(smd)
                        items.append({
                            "name": info.get("name") or sd.name,
                            "description": (info.get("description") or "")[:200],
                            "trigger_keywords": info.get("trigger_keywords") or [],
                            "parent": d.name,
                        })
    except Exception as e:
        logger.warning("扫描 skills 失败: %s", e)
    return {"count": len(items), "items": items}


def _parse_frontmatter(path: Path) -> dict:
    """解析 SKILL.md 的 YAML frontmatter（轻量，不依赖 pyyaml）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    block = text[3:end]
    out: dict = {}
    for line in block.splitlines():
        if ":" not in line or line.startswith((" ", "\t", "-")):
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k == "trigger_keywords":
            out[k] = []
        elif v:
            out[k] = v
    # trigger_keywords 是列表，简单收集后续 "- xxx"
    if "trigger_keywords:" in block:
        kws = []
        in_kw = False
        for line in block.splitlines():
            if line.strip().startswith("trigger_keywords:"):
                in_kw = True
                continue
            if in_kw:
                s = line.strip()
                if s.startswith("- "):
                    kws.append(s[2:].strip().strip('"').strip("'"))
                elif s and not line.startswith((" ", "\t")):
                    break
        out["trigger_keywords"] = kws
    return out


def _discover_agents() -> dict:
    """agents：扫目录 + API 补状态。"""
    items: list[dict] = []
    try:
        if AGENTS_DIR.is_dir():
            for d in sorted(AGENTS_DIR.iterdir()):
                if d.is_dir():
                    items.append({
                        "id": d.name,
                        "has_identity": (d / "identity.md").is_file(),
                    })
    except Exception as e:
        logger.warning("扫描 agents 失败: %s", e)

    api = _api_get("/api/agents")
    if isinstance(api, dict):
        lst = api.get("agents")
        if isinstance(lst, list):
            by_id = {a.get("id"): a for a in lst if isinstance(a, dict)}
            for it in items:
                a = by_id.get(it["id"])
                if a:
                    it["name"] = a.get("name") or it["id"]
                    cm = a.get("chatModel")
                    if isinstance(cm, dict):
                        it["model"] = cm.get("id")
                    it["memory_master_enabled"] = a.get("memoryMasterEnabled")
    return {"count": len(items), "items": items}


# ── 对外入口 ──


class HanaCatalog:
    """Hana 全体系目录（带 TTL 缓存）。"""

    def __init__(self, ttl: float = DEFAULT_TTL):
        self._ttl = float(ttl)
        self._cached: Optional[dict] = None
        self._cached_at = 0.0

    def get(self, force: bool = False) -> dict:
        """返回全体系目录。命中缓存则直接返回。"""
        now = time.monotonic()
        if not force and self._cached is not None and (now - self._cached_at) < self._ttl:
            return self._cached
        cat = self._build()
        self._cached = cat
        self._cached_at = now
        return cat

    def _build(self) -> dict:
        api_ok = _read_server_info() is not None

        def _safe(fn, label: str) -> dict:
            """单个体系扫描失败不能拖垮整体（每节独立兑底）。"""
            try:
                return fn()
            except Exception as e:
                logger.warning("发现 %s 失败（已降级）: %s", label, e)
                return {"count": 0, "items": [], "error": str(e)[:200]}

        plugins = _safe(_discover_plugins, "plugins")
        apps = _safe(_discover_apps, "apps")
        mcp = _safe(_discover_mcp, "mcp")
        skills = _safe(_discover_skills, "skills")
        agents = _safe(_discover_agents, "agents")
        return {
            "plugins": plugins,
            "apps": apps,
            "mcp": mcp,
            "skills": skills,
            "agents": agents,
            "totals": {
                "plugins": plugins.get("count", 0),
                "apps": apps.get("count", 0),
                "mcp_connectors": mcp.get("count", 0),
                "skills": skills.get("count", 0),
                "agents": agents.get("count", 0),
            },
            "hana_server_reachable": api_ok,
            "ts": time.time(),
        }

    def summary(self) -> dict:
        """轻量摘要（供 MCP/日志用，不含明细）。"""
        c = self.get()
        return {
            "totals": c["totals"],
            "hana_server_reachable": c["hana_server_reachable"],
        }


_default_catalog: Optional[HanaCatalog] = None


def get_catalog(force: bool = False) -> dict:
    """模块级入口：拿全体系目录（进程内单例 + TTL 缓存）。"""
    global _default_catalog
    if _default_catalog is None:
        _default_catalog = HanaCatalog()
    return _default_catalog.get(force=force)
