# -*- coding: utf-8 -*-
"""回归：Hana 数据目录必须遵循 HANA_HOME 环境变量。

## 背景（2026-09-17 排查发现）

oc-pet 有 18 处读 Hana 数据，全部写死 `Path.home() / ".hanako"`。
但 Hana 官方的解析规则是 `HANA_HOME` 环境变量优先：

    if (d && typeof d == "string") return i(d, p);      // HANA_HOME 优先
    if (h) return e.resolve(e.join(p, ".hanako"));      // 没设才用默认

本机 `HANA_HOME` 恰好等于默认值，所以没暴露——但用户一旦把
`HANA_HOME` 指到别处（多开 / 测试环境 / 换盘符），oc-pet 会读
旧目录或空目录，全部配置静默失效。

本用例钉死这个行为，防止回归。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hanako_home import hanako_home, hanako_path  # noqa: E402


def test_defaults_to_home_hanako(monkeypatch):
    """未设 HANA_HOME → ~/.hanako。"""
    monkeypatch.delenv("HANA_HOME", raising=False)
    assert hanako_home() == Path.home() / ".hanako"


def test_hana_home_env_wins(monkeypatch, tmp_path):
    """设了 HANA_HOME → 必须用它（这是关键不变式）。"""
    custom = tmp_path / "custom-hana"
    monkeypatch.setenv("HANA_HOME", str(custom))
    assert hanako_home() == custom, "HANA_HOME 必须优先于默认 ~/.hanako"


def test_empty_env_falls_back(monkeypatch):
    """空字符串视为未设置（与 Hana 的 falsy 判断一致）。"""
    monkeypatch.setenv("HANA_HOME", "   ")
    assert hanako_home() == Path.home() / ".hanako", "空串应回退默认"


def test_hanako_path_joins(monkeypatch, tmp_path):
    """hanako_path 拼接子路径。"""
    custom = tmp_path / "h"
    monkeypatch.setenv("HANA_HOME", str(custom))
    assert hanako_path("agents", "ophelia", "config.yaml") == (
        custom / "agents" / "ophelia" / "config.yaml"
    )


def test_matches_hana_official_logic(monkeypatch, tmp_path):
    """对照 Hana 官方逻辑的三个分支。"""
    # 分支 1：envValue 有值 → 用它
    monkeypatch.setenv("HANA_HOME", str(tmp_path))
    assert hanako_home() == tmp_path
    # 分支 2：envValue 空 → 默认
    monkeypatch.setenv("HANA_HOME", "")
    assert hanako_home() == Path.home() / ".hanako"
    # 分支 3：env 完全不存在 → 默认
    monkeypatch.delenv("HANA_HOME", raising=False)
    assert hanako_home() == Path.home() / ".hanako"


def test_consumers_use_helper():
    """静态检查：读取 Hana 数据的模块应引用 helper，而非写死路径。

    这是防回归的哨兵——新增写死的 `Path.home() / ".hanako"` 会被抓到。

    2026-09-17 扩展：原只查 2 个文件，后排查发现全项目实际有 25 处
    （报告里写 18 处是 grep 漏了 tts_provider/ 和 ui/ 子目录）。
    现已全部替换，哨兵扩到所有曾出问题的模块。
    """
    import io
    import re

    root = Path(__file__).resolve().parent.parent
    # 这些模块负责读 Hana 数据，必须用 helper
    must_use = [
        "env_config.py",
        "core/hanako_context.py",
        "core/hana_catalog.py",
        "core/tool_executor.py",
        "core/tool_registry.py",
        "core/startup_check.py",
        "core/capability_registry.py",
        "core/conversation_engine.py",
        "core/perception/schedule.py",
        "pet_manager.py",
        "pet.py",
        "ui/plugin_panel.py",
        "ui/character_card.py",
        "ui/settings_dialog.py",
        "tts_provider/api_tts.py",
        "tts_provider/cosyvoice.py",
        "tts_provider/edge_tts.py",
        "tts_provider/mimo_tts.py",
        "tts_provider/qwen_tts.py",
    ]
    offenders = []
    for rel in must_use:
        fp = root / rel
        if not fp.exists():
            continue
        src = io.open(fp, encoding="utf-8").read()
        if re.search(r'Path\.home\(\)\s*/\s*"\.hanako"', src):
            offenders.append(rel)
    assert not offenders, (
        f"以下模块仍有写死的 Path.home()/'.hanako'，应改用 hanako_home()：{offenders}"
    )


def test_whole_project_has_no_hardcoded_path():
    """全项目扫描：除 hanako_home.py 自身的 fallback 外，不得有写死路径。

    比上面那个哨兵更严——它只查已知模块，这个查全项目，
    能抓到新增文件里的写死路径（今天就是因为只查已知模块而漏了
    tts_provider/ 和 ui/ 子目录）。
    """
    import io
    import re

    root = Path(__file__).resolve().parent.parent
    pattern = re.compile(r'Path\.home\(\)\s*/\s*"\.hanako"')
    offenders = []
    for fp in root.rglob("*.py"):
        s = str(fp)
        if "__pycache__" in s or "\\.venv" in s or "\\tests\\" in s:
            continue
        if fp.name == "hanako_home.py":
            continue  # helper 自身的 fallback 是正当的
        try:
            src = io.open(fp, encoding="utf-8").read()
        except Exception:
            continue
        if pattern.search(src):
            offenders.append(str(fp.relative_to(root)))
    assert not offenders, (
        f"全项目仍有写死的 Path.home()/'.hanako'：{offenders}"
    )
