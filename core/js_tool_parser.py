"""core/js_tool_parser.py — JS 工具文件解析（单一实现）

## 为什么存在

Hana 的插件/App 工具文件（`tools/*.js`）导出结构在 **v1 与 v2 完全一致**：

```js
export const name = "mail_accounts";
export const description = "邮件账号管理：列表/创建/删除账号";
export const parameters = { type: "object", properties: { ... } };
export const triggers = [{ text: "下一首", args: { action: "next" } }];  // 可选
export async function execute(input) { ... }
```

**v1 与 v2 的差异只在「怎么发现工具」**（v1 读 `contributes.tools`，
v2 靠 `tools/` 目录存在性），解析文件本身完全一样。

在此之前，同一份解析逻辑被**三处重复实现**：
- `core/tool_registry.py`（需要完整参数，喂 LLM 工具调用）
- `ui/plugin_panel.py`（只要 name/description 显示）
- `core/hana_catalog.py`（只要 name/description 清单）

三份实现各自演化，已经出现行为不一致（如 `split("=")` 截断问题）。
本模块统一为单一实现，三处按需取字段。

## 解析策略（纯正则，不 import JS 运行时）

JS 对象 → JSON 需要处理四件事，缺一即解析失败：
1. 去注释（`//` 与 `/* */`）
2. 去尾逗号（`{a: 1,}`）
3. key 加引号（`{type: ...}` → `{"type": ...}`）——**key 限定 ASCII**，
   因为 Python 的 `\\w` 会匹配中文（历史坑）
4. 单引号字符串 → 双引号——**仅当安全路径失败才做**，因为双引号串里
   嵌单引号（如 `"如 '加班,累'"`）会被转换破坏

## 不做什么

不执行 JS、不解析 `execute` 函数体、不做语法校验。只提取声明式元数据。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "TOOL_FILE_SUFFIXES",
    "parse_tool_file",
    "parse_tool_source",
    "parse_tool_summary",
    "iter_tool_files",
]

# 工具文件扩展名（Hana 生态用 .js；.mjs/.ts 为兼容预留）
TOOL_FILE_SUFFIXES = (".js", ".mjs", ".ts")

# 声明式字段的匹配前缀：export const / const / let / var
_DECL = r"(?:export\s+(?:const|let|var)|const|let|var)"


def iter_tool_files(tools_dir: Path) -> list[Path]:
    """列出 tools/ 目录下的工具文件（排序，只含已知后缀）。"""
    if not tools_dir.is_dir():
        return []
    try:
        return [
            f for f in sorted(tools_dir.iterdir())
            if f.is_file() and f.suffix in TOOL_FILE_SUFFIXES
        ]
    except Exception as e:
        logger.debug("列目录失败 (%s): %s", tools_dir, e)
        return []


def parse_tool_file(path: Path) -> Optional[dict]:
    """解析单个工具文件 → {name, description, parameters, triggers, file}。

    读取失败返回 None（调用方跳过该文件，不影响其他）。
    """
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.debug("读工具文件失败 (%s): %s", path, e)
        return None
    out = parse_tool_source(src, fallback_name=path.stem)
    out["file"] = path.name
    return out


def parse_tool_source(src: str, fallback_name: str = "") -> dict:
    """解析工具文件源码 → {name, description, parameters, triggers}。

    Args:
        src: JS 源码
        fallback_name: 未声明 name 时的回退值（通常是文件名 stem）

    Returns:
        parameters 保证是 dict（解析失败回退空 schema）；
        triggers 保证是 list[{text, args}]。
    """
    name = _extract_string(src, "name") or fallback_name
    description = _extract_string(src, "description") or ""
    parameters = _extract_object(src, "parameters") or {
        "type": "object",
        "properties": {},
    }
    triggers = _extract_triggers(src)
    return {
        "name": name,
        "description": description,
        "parameters": parameters,
        "triggers": triggers,
    }


def parse_tool_summary(path: Path) -> dict:
    """轻量版：只要 name/description（供面板/清单显示）。

    不解析 parameters（省掉一次 JSON 转换），但仍走同一套字符串提取，
    保证与完整解析**结果一致**。
    """
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {"name": path.stem, "description": "", "file": path.name}
    return {
        "name": _extract_string(src, "name") or path.stem,
        "description": (_extract_string(src, "description") or "")[:200],
        "file": path.name,
    }


# ── 内部：字段提取 ──


def _extract_string(src: str, var: str) -> Optional[str]:
    """提取 `const <var> = '...'` 或 `= "..."` 的字符串值。"""
    m = re.search(
        rf"{_DECL}\s+{re.escape(var)}\s*=\s*['\"]([^'\"]+)['\"]",
        src,
    )
    return m.group(1) if m else None


def _extract_object(src: str, var_name: str) -> Optional[dict]:
    """提取 `const <var> = { ... }` 并转成 dict。

    转换链见模块 docstring。任一环节失败返回 None（调用方回退空 schema）。
    """
    m = re.search(
        rf"{_DECL}\s+{re.escape(var_name)}\s*=\s*(\{{[\s\S]*?\}})\s*;",
        src,
    )
    if not m:
        return None
    raw = m.group(1)
    # 1) 去注释
    raw = re.sub(r"//.*?\n", "\n", raw)
    raw = re.sub(r"/\*[\s\S]*?\*/", "", raw)
    # 2) 去尾逗号
    raw = re.sub(r",\s*([\]}])", r"\1", raw)
    # 3) key 加引号（限定 ASCII key——\w 会匹配中文，历史坑）
    raw = re.sub(r"([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)(\s*:)", r'\1"\2"\3', raw)
    # 4a) 安全路径：值已是双引号/纯数字（多数工具文件）
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # 4b) 兜底：单引号串 → 双引号串
    #     仅当安全路径失败才做——双引号串里嵌单引号会被破坏
    raw = re.sub(r"'((?:[^'\\]|\\.)*)'", r'"\1"', raw)
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, Exception) as e:
        logger.debug("解析 %s 对象失败: %s", var_name, e)
        return None


def _extract_triggers(src: str) -> list:
    """提取 `const triggers = [...]` → [{"text": str, "args": dict}]。

    支持两种写法：
        export const triggers = ['下一首', '切歌'];
        export const triggers = [{ text: '下一首', args: { action: 'next' } }];
    """
    m = re.search(rf"{_DECL}\s+triggers\s*=\s*(\[[\s\S]*?\])\s*;", src)
    if not m:
        return []
    raw = m.group(1)
    raw = re.sub(r"'((?:[^'\\]|\\.)*)'", r'"\1"', raw)
    raw = re.sub(r"([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)(\s*:)", r'\1"\2"\3', raw)
    try:
        arr = json.loads(raw)
    except (json.JSONDecodeError, Exception):
        return []
    if not isinstance(arr, list):
        return []
    out: list = []
    for item in arr:
        if isinstance(item, str):
            out.append({"text": item, "args": {}})
        elif isinstance(item, dict) and item.get("text"):
            out.append({"text": item["text"], "args": item.get("args") or {}})
    return out
