"""工具注册表 — 扫描 Hanako 插件，提取工具定义为 OpenAI tool calling 格式

注意：仅直连模式 (transport_mode=direct) 使用。
Hanako WS 模式下工具由服务端执行，不需要本地注册。

用法:
    registry = ToolRegistry()
    registry.discover()
    tools = registry.get_tools()  # OpenAI tools 格式
    tool = registry.get_tool("play")  # 查找单个工具
"""
from __future__ import annotations

import json
import logging
import re
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from hanako_home import hanako_home

HANAKO_PLUGINS = hanako_home() / "plugins"
HANAKO_APPS = hanako_home() / "apps"
LOCAL_PLUGINS = Path(__file__).parent.parent / "plugins"

# 外部 Hanako 插件触发词集中声明（单一来源）。
# 这些插件由 Hanako 平台托管（Git 仓库），无法在其自身 manifest 声明 triggers
# （改动会被上游覆盖），故在 oc-pet 侧以「工具名为键」声明一次。元素为
# {"text": 触发词, "args": 透传给工具执行的参数}；args 让"下一首"等携带
# action，避免统一路由默认 action="state" 而只查不操作（见 hanako-audio-player
# tools/bus.js: input.action || "state"）。
# 关键：不使用歧义裸词（"暂停"/"截图"/"下一个"），只保留明确组合词，根除子串碰撞。
_EXTERNAL_TOOL_TRIGGERS: dict[str, list] = {
    "audio_bus": [
        {"text": "下一首", "args": {"action": "next"}},
        {"text": "切歌", "args": {"action": "next"}},
        {"text": "暂停播放", "args": {"action": "pause"}},
        {"text": "继续播放", "args": {"action": "resume"}},
        {"text": "清空播放列表", "args": {"action": "clear"}},
        {"text": "播放状态", "args": {"action": "state"}},
        {"text": "音乐状态", "args": {"action": "state"}},
        {"text": "audio", "args": {}},
        {"text": "bus", "args": {}},
    ],
}


class ToolDef:
    """单个工具定义"""

    def __init__(self, name: str, description: str, parameters: dict,
                 plugin_id: str, source_path: str, triggers: list = None):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.plugin_id = plugin_id
        self.source_path = source_path
        # 触发词（单一来源）：本地插件从 .js 的 export const triggers 解析；
        # 外部 Hanako 插件无法在自身 manifest 声明（上游托管会被覆盖），
        # 由 _EXTERNAL_TOOL_TRIGGERS 以工具名为键集中声明一次。
        # 元素为 str 或 {"text": "...", "args": {...}}（args 透传给工具执行）。
        self.triggers = triggers or []

    def to_openai_tool(self) -> dict:
        """转换为 OpenAI tool calling 格式"""
        # 清理工具名：只保留 [a-zA-Z0-9_-]
        import re
        clean_name = re.sub(r'[^a-zA-Z0-9_-]', '_', self.name)
        if not clean_name or not clean_name[0].isalpha():
            clean_name = 'tool_' + clean_name
        return {
            "type": "function",
            "function": {
                "name": clean_name,
                "description": self.description[:200] if self.description else self.name,
                "parameters": self.parameters,
            }
        }

    def __repr__(self):
        return f"Tool({self.name}, plugin={self.plugin_id})"


class ToolRegistry:
    """工具注册表"""

    def __init__(self):
        self._tools: dict[str, ToolDef] = {}  # name -> ToolDef
        self._name_map: dict[str, str] = {}  # sanitized_name -> original_name

    def refresh(self):
        """重新扫描插件目录（清空旧索引后重建），供热刷新调用。

        新增/删除插件后调用，30 秒内生效，无需重启桌宠。
        注意：先清空 _tools/_name_map 再 discover()，否则旧工具残留。
        """
        self._tools.clear()
        self._name_map.clear()
        self.discover()

    def discover(self):
        """扫描所有插件目录，提取工具定义"""
        # P6: 插件工具全局开关（默认关闭，plugins/ 为空时保留接口但不扫）
        # 防抖：同一 disabled 日志 60s 内只打一次，避免 30s 轮询刷屏
        try:
            import time as _time
            from config import load_config
            cfg = load_config()
            pt_cfg = cfg.get("plugin_tools", {}) or {}
            if not pt_cfg.get("enabled", False):
                now = _time.time()
                if now - getattr(self, "_disabled_logged_at", 0.0) >= 60.0:
                    logger.info("Plugin tools disabled by config (plugin_tools.enabled=false)")
                    self._disabled_logged_at = now
                return
        except Exception as e:
            logger.warning("Plugin tools config check failed: %s", e)
            return
        # 扫描 Hanako 全局插件 + V2 Apps + oc-pet 本地插件
        #
        # 2026-09-17：补上 V2 Apps（原只扫 V1 plugins，漏掉 11 个工具）。
        # V2 的发现机制与 V1 **不同**：官方 schema 不承认 `contributes.tools`
        # 字段（实测 V2 manifest 的 contributes 只有 cards/settings/ui），
        # Hana 靠「tools/ 目录存在」发现工具。故 V2 走独立扫描路径。
        plugin_dirs = []
        if HANAKO_PLUGINS.exists():
            plugin_dirs.append(HANAKO_PLUGINS)
        if LOCAL_PLUGINS.exists():
            plugin_dirs.append(LOCAL_PLUGINS)
            logger.info("Local plugins dir: %s", LOCAL_PLUGINS)

        if not plugin_dirs and not HANAKO_APPS.exists():
            logger.warning("No plugin dirs found")
            return

        for base_dir in plugin_dirs:
            self._scan_dir(base_dir)

        # V2 Apps：按 tools/ 目录发现（不读 contributes.tools）
        if HANAKO_APPS.exists():
            self._scan_apps_dir(HANAKO_APPS)

        logger.info("Tool registry: %d tools from plugins", len(self._tools))

        # 构建名称映射（sanitized -> original）
        import re
        for name in self._tools:
            clean = re.sub(r'[^a-zA-Z0-9_-]', '_', name)
            if not clean or not clean[0].isalpha():
                clean = 'tool_' + clean
            self._name_map[clean] = name

    def _scan_dir(self, plugins_dir: Path):
        """扫描单个插件目录"""
        for plugin_dir in sorted(plugins_dir.iterdir()):
            if not plugin_dir.is_dir():
                continue
            manifest = plugin_dir / "manifest.json"
            if not manifest.exists():
                continue
            try:
                m = json.loads(manifest.read_text("utf-8"))
                contributes = m.get("contributes", {})
                if not isinstance(contributes, dict):
                    continue
                tools_raw = contributes.get("tools", [])
                if not isinstance(tools_raw, list):
                    continue

                plugin_id = m.get("id", plugin_dir.name)

                for t in tools_raw:
                    if isinstance(t, str):
                        # 字符串格式：可能是工具名或 source 路径
                        if t.endswith('.js') or '/' in t:
                            # 是 source 路径（如 "./tools/tavern-chat.js"）
                            tool_path = plugin_dir / t.lstrip('./')
                            tool_def = self._parse_tool_file(tool_path, plugin_id)
                            if tool_def:
                                if tool_def.name in _EXTERNAL_TOOL_TRIGGERS:
                                    tool_def.triggers = _EXTERNAL_TOOL_TRIGGERS[tool_def.name]
                                if tool_def.name in self._tools:
                                    tool_def.name = f"{plugin_id}.{tool_def.name}"
                                self._tools[tool_def.name] = tool_def
                        else:
                            # 是工具 ID
                            tool_def = ToolDef(
                                name=t, description="", parameters={"type": "object", "properties": {}},
                                plugin_id=plugin_id, source_path=""
                            )
                            # 外部插件触发词集中声明（单一来源）
                            if t in _EXTERNAL_TOOL_TRIGGERS:
                                tool_def.triggers = _EXTERNAL_TOOL_TRIGGERS[t]
                            self._tools[tool_def.name] = tool_def
                        continue
                    if not isinstance(t, dict):
                        continue

                    source = t.get("source", "")
                    name = t.get("name", "")
                    if source:
                        tool_path = plugin_dir / source
                        tool_def = self._parse_tool_file(tool_path, plugin_id)
                        if tool_def:
                            if tool_def.name in _EXTERNAL_TOOL_TRIGGERS:
                                tool_def.triggers = _EXTERNAL_TOOL_TRIGGERS[tool_def.name]
                            # 避免重名覆盖
                            if tool_def.name in self._tools:
                                tool_def.name = f"{plugin_id}.{tool_def.name}"
                            self._tools[tool_def.name] = tool_def
                    elif name:
                        # P: 无 source 字段的声明式工具（如 tavily-usage-monitor 的
                        # name 声明式）——按 name 映射 tools/ 同名文件。
                        tool_path = self._resolve_tool_source(plugin_dir, name)
                        tool_def = self._parse_tool_file(tool_path, plugin_id) if tool_path else None
                        if tool_def is None:
                            # 找不到本地脚本 → 注册空参数工具（name/description 用
                            # manifest 里的，工具至少可见可调；source_path="" 标注无本地脚本）。
                            tool_def = ToolDef(
                                name=name,
                                description=t.get("description", "") or "",
                                parameters={"type": "object", "properties": {}},
                                plugin_id=plugin_id,
                                source_path="",
                            )
                            if tool_def.name in _EXTERNAL_TOOL_TRIGGERS:
                                tool_def.triggers = _EXTERNAL_TOOL_TRIGGERS[tool_def.name]
                        # 避免重名覆盖
                        if tool_def.name in self._tools:
                            tool_def.name = f"{plugin_id}.{tool_def.name}"
                        self._tools[tool_def.name] = tool_def

            except Exception as e:
                logger.warning("Failed to parse plugin %s: %s", plugin_dir.name, e)

    def _scan_apps_dir(self, apps_dir: Path):
        """扫描 V2 Apps（~/.hanako/apps）。

        **与 `_scan_dir` 的关键差异**：V2 不能读 `contributes.tools`——
        官方 schema 不承认该字段（实测 V2 manifest 的 contributes 只有
        cards/settings/ui 等），Hana 靠「tools/ 目录存在」发现工具。
        故这里**直接枚举 tools/*.js**，解析每个文件的导出声明。

        容错：
        - 无 manifest.json → 跳过（不是合法 App）
        - manifestVersion != 2 → 跳过（v1 已由 _scan_dir 处理，避免重复）
        - 无 tools/ 目录 → 跳过（该 App 没有工具，如纯卡片型）
        - 单文件解析失败 → 跳过该文件，不影响其他
        """
        for app_dir in sorted(apps_dir.iterdir()):
            if not app_dir.is_dir():
                continue
            manifest = app_dir / "manifest.json"
            if not manifest.exists():
                continue
            try:
                m = json.loads(manifest.read_text("utf-8"))
            except Exception as e:
                logger.warning("Failed to parse app manifest %s: %s", app_dir.name, e)
                continue
            # 只处理 v2；v1 的 manifest 若出现在 apps/ 下则跳过（由 _scan_dir 负责）
            if m.get("manifestVersion") != 2:
                continue
            tools_dir = app_dir / "tools"
            if not tools_dir.is_dir():
                continue
            app_id = m.get("id") or app_dir.name
            for f in sorted(tools_dir.iterdir()):
                if not f.is_file() or f.suffix not in (".js", ".mjs", ".ts"):
                    continue
                try:
                    tool_def = self._parse_tool_file(f, app_id)
                except Exception as e:
                    logger.debug("解析 App 工具文件失败 (%s): %s", f.name, e)
                    continue
                if tool_def is None:
                    continue
                # 外部触发词（与 V1 同一套单一来源）
                if tool_def.name in _EXTERNAL_TOOL_TRIGGERS:
                    tool_def.triggers = _EXTERNAL_TOOL_TRIGGERS[tool_def.name]
                # 避免重名覆盖（v1/v2 可能有同名工具）
                if tool_def.name in self._tools:
                    tool_def.name = f"{app_id}.{tool_def.name}"
                self._tools[tool_def.name] = tool_def

    def _resolve_tool_source(self, plugin_dir: Path, name: str) -> Optional[Path]:
        """无 source 字段时，按工具名在 tools/ 下定位同名脚本文件。

        匹配规则（大小写不敏感，按优先级）：
        1. 直接名：tavily_search → tools/tavily_search.js
        2. kebab-case：tavily_search → tools/tavily-search.js
        3. token 子序列：工具名 token 序列包含文件 stem token 序列（保持顺序）——
           check_tavily_usage → [check, tavily, usage] ⊇ [check, usage]
           → tools/check-usage.js；tavily_usage_history → [tavily, usage, history]
           ⊇ [usage, history] → tools/usage-history.js
        找不到返回 None（调用方回退注册空参数工具）。
        """
        tools_dir = plugin_dir / "tools"
        if not tools_dir.is_dir():
            return None

        # 1/2. 直接名 + kebab-case（.js 优先，其次 .mjs/.ts）
        base = str(name).strip()
        for suffix in (".js", ".mjs", ".ts", ""):
            for candidate in (f"{base}{suffix}", f"{base.replace('_', '-')}{suffix}"):
                p = tools_dir / candidate
                if p.is_file():
                    return p

        # 3. token 子序列匹配（取 token 最长、最精确的候选）
        name_tokens = [t for t in re.split(r'[^a-zA-Z0-9]+', base.lower()) if t]
        if not name_tokens:
            return None
        best: Optional[Path] = None
        best_len = -1
        for f in sorted(tools_dir.iterdir()):
            if not f.is_file() or f.suffix not in (".js", ".mjs", ".ts"):
                continue
            stem_tokens = [t for t in re.split(r'[^a-zA-Z0-9]+', f.stem.lower()) if t]
            if not stem_tokens:
                continue
            if self._is_subsequence(stem_tokens, name_tokens) and len(stem_tokens) > best_len:
                best = f
                best_len = len(stem_tokens)
        return best

    @staticmethod
    def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
        """判断 needle 是否为 haystack 的子序列（保持顺序，可跳元素）。

        例：["check", "usage"] 是 ["check", "tavily", "usage"] 的子序列 → True
        """
        it = iter(haystack)
        return all(any(n == h for h in it) for n in needle)

    def _parse_tool_file(self, path: Path, plugin_id: str) -> Optional[ToolDef]:
        """从 JS 工具文件提取 name/description/parameters。

        2026-09-17：解析逻辑抽到 `core/js_tool_parser.py`（单一实现）。
        此前 tool_registry / plugin_panel / hana_catalog 三处各写一份，
        已出现行为不一致。现在只在此处适配成 ToolDef。
        """
        from core.js_tool_parser import parse_tool_file as _parse

        parsed = _parse(path)
        if parsed is None:
            return None
        return ToolDef(
            name=parsed["name"],
            description=parsed["description"],
            parameters=parsed["parameters"],
            plugin_id=plugin_id,
            source_path=str(path),
            triggers=parsed["triggers"],
        )


    def get_tools(self) -> list[dict]:
        """返回所有工具的 OpenAI 格式列表"""
        return [t.to_openai_tool() for t in self._tools.values()]

    def get_tool(self, name: str) -> Optional[ToolDef]:
        """按名称查找工具（支持 sanitized 名称）"""
        # 先尝试原始名称
        if name in self._tools:
            return self._tools[name]
        # 再尝试 sanitized 名称映射
        original = self._name_map.get(name)
        if original and original in self._tools:
            return self._tools[original]
        return None

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    def list_tools(self) -> list[str]:
        """返回所有工具名称"""
        return list(self._tools.keys())
