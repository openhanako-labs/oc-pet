# -*- coding: utf-8 -*-
"""回归：JS 工具解析的单一实现（core/js_tool_parser.py）。

## 背景（2026-09-17）

同一份 JS 工具解析逻辑曾被**三处重复实现**：
- `core/tool_registry.py`（完整参数，喂 LLM）
- `ui/plugin_panel.py`（只要 name/description）
- `core/hana_catalog.py`（只要 name/description）

三份各自演化，已出现行为不一致（如 `split("=")` 截断把
`mail_accounts` 解析成 `accounts`）。

抽到 `core/js_tool_parser.py` 后，三处按需取字段，解析行为统一。

## 本测试锁什么

- 解析器对真实 Hana 工具文件的行为（name/description/parameters/triggers）
- 三处消费结果一致（同一文件解析出的 name 必须相同）
- 边界：缺字段 / 单引号 / 尾逗号 / 注释 / 中文 key 不误伤
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── 一、基础解析 ────────────────────────────────────────────────────────────


def test_parse_basic_exports():
    from core.js_tool_parser import parse_tool_source

    src = (
        'export const name = "mail_accounts";\n'
        'export const description = "邮件账号管理：列表/创建/删除账号";\n'
        'export const parameters = { type: "object", properties: { action: { type: "string" } } };\n'
    )
    out = parse_tool_source(src, fallback_name="accounts")
    assert out["name"] == "mail_accounts", "不应被 split('=') 截断"
    assert out["description"].startswith("邮件账号管理")
    assert "action" in out["parameters"]["properties"]


def test_parse_single_quotes_and_trailing_comma():
    """JS 常见写法：单引号字符串 + 尾逗号 + 注释。"""
    from core.js_tool_parser import parse_tool_source

    src = (
        "// 这是注释\n"
        "export const name = 'single_quoted';\n"
        "export const parameters = {\n"
        "  type: 'object',\n"
        "  properties: {\n"
        "    query: { type: 'string' },  // 行内注释\n"
        "  },\n"
        "};\n"
    )
    out = parse_tool_source(src)
    assert out["name"] == "single_quoted"
    assert "query" in out["parameters"]["properties"], (
        f"单引号/尾逗号/注释应被正确处理: {out['parameters']}"
    )


def test_parse_missing_fields_falls_back():
    from core.js_tool_parser import parse_tool_source

    out = parse_tool_source("// 空文件", fallback_name="fallback_tool")
    assert out["name"] == "fallback_tool"
    assert out["description"] == ""
    assert out["parameters"] == {"type": "object", "properties": {}}
    assert out["triggers"] == []


def test_parse_triggers_both_forms():
    from core.js_tool_parser import parse_tool_source

    plain = parse_tool_source("export const triggers = ['下一首', '切歌'];")
    assert plain["triggers"] == [
        {"text": "下一首", "args": {}},
        {"text": "切歌", "args": {}},
    ]

    with_args = parse_tool_source(
        "export const triggers = [{ text: '下一首', args: { action: 'next' } }];"
    )
    assert with_args["triggers"][0]["text"] == "下一首"
    assert with_args["triggers"][0]["args"] == {"action": "next"}


def test_chinese_key_not_mangled():
    """key 加引号时限定 ASCII——Python 的 \\w 会匹配中文（历史坑）。"""
    from core.js_tool_parser import parse_tool_source

    src = (
        'export const description = "默认 20 条";\n'
        "export const parameters = { type: 'object', properties: { n: { type: 'number' } } };\n"
    )
    out = parse_tool_source(src)
    assert out["description"] == "默认 20 条"
    assert "n" in out["parameters"]["properties"]


# ── 二、三处消费一致 ────────────────────────────────────────────────────────


def test_three_consumers_agree_on_name(tmp_path):
    """同一工具文件，三处消费拿到的 name 必须一致。"""
    from core.js_tool_parser import parse_tool_file, parse_tool_summary

    f = tmp_path / "x.js"
    f.write_text(
        'export const name = "shared_name";\n'
        'export const description = "共享测试";\n'
        "export const parameters = { type: 'object', properties: {} };\n",
        encoding="utf-8",
    )

    full = parse_tool_file(f)
    summary = parse_tool_summary(f)
    assert full["name"] == summary["name"] == "shared_name"

    # tool_registry 侧
    from core.tool_registry import ToolRegistry

    reg = ToolRegistry()
    td = reg._parse_tool_file(f, "test-plugin")
    assert td is not None and td.name == "shared_name"

    # hana_catalog 侧（走 _scan_tool_files）
    from core.hana_catalog import _scan_tool_files

    scanned = _scan_tool_files(tmp_path)
    assert scanned and scanned[0]["name"] == "shared_name"


def test_real_hana_tools_parse_consistently():
    """真实 Hana 工具目录：解析器不崩、名称非空。

    无工具目录时跳过（CI 环境）。
    """
    from core.js_tool_parser import iter_tool_files, parse_tool_summary

    dirs = [
        Path.home() / ".hanako" / "plugins",
        Path.home() / ".hanako" / "apps",
    ]
    files = []
    for d in dirs:
        if not d.is_dir():
            continue
        for sub in sorted(d.iterdir()):
            if sub.is_dir():
                files.extend(iter_tool_files(sub / "tools"))
    if not files:
        import pytest

        pytest.skip("本机无 Hana 工具文件")

    bad = []
    for f in files:
        s = parse_tool_summary(f)
        if not s["name"]:
            bad.append(f.name)
    assert not bad, f"以下文件解析出空名称: {bad[:10]}"
