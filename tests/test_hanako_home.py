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
    """静态检查：读取 Hana 数据的关键模块应引用 helper，而非写死路径。

    这是防回归的哨兵——新增写死的 `Path.home() / ".hanako"` 会被抓到。
    """
    import io
    import re

    root = Path(__file__).resolve().parent.parent
    # 这些模块负责读 Hana 数据，必须用 helper
    must_use = ["env_config.py", "core/hanako_context.py"]
    for rel in must_use:
        fp = root / rel
        if not fp.exists():
            continue
        src = io.open(fp, encoding="utf-8").read()
        hardcoded = re.findall(r'Path\.home\(\)\s*/\s*"\.hanako"', src)
        assert not hardcoded, (
            f"{rel} 里仍有写死的 Path.home()/'.hanako'（{len(hardcoded)} 处）——"
            f" 应改用 hanako_home()"
        )
