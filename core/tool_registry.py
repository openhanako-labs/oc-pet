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

HANAKO_PLUGINS = Path.home() / ".hanako" / "plugins"
LOCAL_PLUGINS = Path(__file__).parent.parent / "plugins"


class ToolDef:
    """单个工具定义"""

    def __init__(self, name: str, description: str, parameters: dict,
                 plugin_id: str, source_path: str):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.plugin_id = plugin_id
        self.source_path = source_path

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

    def discover(self):
        """扫描所有插件目录，提取工具定义"""
        # 扫描 Hanako 全局插件 + oc-pet 本地插件
        plugin_dirs = []
        if HANAKO_PLUGINS.exists():
            plugin_dirs.append(HANAKO_PLUGINS)
        if LOCAL_PLUGINS.exists():
            plugin_dirs.append(LOCAL_PLUGINS)
            logger.info("Local plugins dir: %s", LOCAL_PLUGINS)

        if not plugin_dirs:
            logger.warning("No plugin dirs found")
            return

        for base_dir in plugin_dirs:
            self._scan_dir(base_dir)

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
                                if tool_def.name in self._tools:
                                    tool_def.name = f"{plugin_id}.{tool_def.name}"
                                self._tools[tool_def.name] = tool_def
                        else:
                            # 是工具 ID
                            self._tools[t] = ToolDef(
                                name=t, description="", parameters={"type": "object", "properties": {}},
                                plugin_id=plugin_id, source_path=""
                            )
                        continue
                    if not isinstance(t, dict):
                        continue

                    source = t.get("source", "")
                    if not source:
                        continue

                    tool_path = plugin_dir / source
                    tool_def = self._parse_tool_file(tool_path, plugin_id)
                    if tool_def:
                        # 避免重名覆盖
                        if tool_def.name in self._tools:
                            tool_def.name = f"{plugin_id}.{tool_def.name}"
                        self._tools[tool_def.name] = tool_def

            except Exception as e:
                logger.warning("Failed to parse plugin %s: %s", plugin_dir.name, e)

    def _parse_tool_file(self, path: Path, plugin_id: str) -> Optional[ToolDef]:
        """从 JS 工具文件提取 name/description/parameters"""
        if not path.exists():
            return None
        try:
            content = path.read_text("utf-8")

            # 提取 name
            name_match = re.search(r"(?:export\s+(?:const|let|var)|const|let|var)\s+name\s*=\s*['\"]([^'\"]+)['\"]", content)
            name = name_match.group(1) if name_match else path.stem

            # 提取 description
            desc_match = re.search(r"(?:export\s+(?:const|let|var)|const|let|var)\s+description\s*=\s*['\"]([^'\"]+)['\"]", content)
            description = desc_match.group(1) if desc_match else ""

            # 提取 parameters (JSON 对象)
            parameters = self._extract_json_block(content, "parameters")
            if not parameters:
                parameters = {"type": "object", "properties": {}}

            return ToolDef(
                name=name,
                description=description,
                parameters=parameters,
                plugin_id=plugin_id,
                source_path=str(path),
            )
        except Exception as e:
            logger.warning("Failed to parse tool %s: %s", path, e)
            return None

    def _extract_json_block(self, content: str, var_name: str) -> Optional[dict]:
        """从 JS 源码中提取 JSON 对象赋值"""
        # 匹配: const parameters = { ... };
        pattern = rf"(?:export\s+(?:const|let|var)|const|let|var)\s+{var_name}\s*=\s*(\{{[\s\S]*?\}})\s*;"
        match = re.search(pattern, content)
        if not match:
            return None
        try:
            # JS 对象 → JSON：去掉尾逗号、注释
            raw = match.group(1)
            raw = re.sub(r'//.*?\n', '\n', raw)  # 去单行注释
            raw = re.sub(r'/\*[\s\S]*?\*/', '', raw)  # 去多行注释
            raw = re.sub(r',\s*([\]}])', r'\1', raw)  # 去尾逗号
            # JS 对象 key 没引号 → 加引号
            raw = re.sub(r'(\s)(\w+)(\s*:)', r'\1"\2"\3', raw)
            return json.loads(raw)
        except (json.JSONDecodeError, Exception):
            return None

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
