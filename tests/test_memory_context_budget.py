# -*- coding: utf-8 -*-
"""build_memory_context 分段保底预算（reserved floor）单测。

背景（2026-09-21）：原实现是「先到先得」——每段只能吃当前剩余，一段吃满
就把后面的段饿成空串。事实段 cap=None，实测可以把「长期」「记忆」整段挤掉。
本文件锁住三条契约：

1. 事实段很长时，「长期」「记忆」仍能拿到自己的保底份额；
2. 任何预算下输出总长都不超过 max_chars；
3. 预算小到连前缀/分隔符都装不下时，退化为不预留（回到先到先得）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.hanako_context import HanakoContext


def _touch(home: Path, rel: str, text: str):
    p = home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture()
def hanako_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HANA_HOME", str(tmp_path))
    return tmp_path


def test_reserve_prevents_starvation(hanako_home):
    """事实段极长时，「长期」「记忆」不应被挤成空串。"""
    _touch(hanako_home, "agents/miku/memory/today.md", "今日已完成签到")
    _touch(hanako_home, "agents/miku/memory/facts.md", "用户喜欢咖啡。" * 400)
    _touch(hanako_home, "agents/miku/memory/longterm.md", "长期目标：健身")
    _touch(hanako_home, "agents/miku/memory/memory.md", "最近在学 Python")

    out = HanakoContext("miku").build_memory_context(max_chars=800)

    assert "【事实】" in out
    assert "【长期】" in out, "长期段被事实段饿死了（保底预算未生效）"
    assert "【记忆】" in out, "记忆段被事实段饿死了（保底预算未生效）"
    assert len(out) <= 800


@pytest.mark.parametrize("budget", [40, 60, 120, 300, 800, 1200, 3000, 6000])
def test_budget_never_exceeded(hanako_home, budget):
    """任意预算下总长都不超过 max_chars（含段间分隔符）。"""
    big = "内容很长的段落。" * 300
    for name in ("today", "facts", "longterm", "memory"):
        _touch(hanako_home, f"agents/miku/memory/{name}.md", big)

    out = HanakoContext("miku").build_memory_context(max_chars=budget)
    assert len(out) <= budget, f"预算 {budget} 被突破：实际 {len(out)}"


def test_small_budget_degrades_to_first_come(hanako_home):
    """预算连前缀/分隔符都装不下 → 不预留，保住第一段。"""
    for name in ("today", "facts", "longterm", "memory"):
        _touch(hanako_home, f"agents/miku/memory/{name}.md", "内容很长的段落。" * 50)

    out = HanakoContext("miku").build_memory_context(max_chars=20)
    assert len(out) <= 20
    assert out.startswith("【今日】")


def test_order_preserved_and_no_empty_parts(hanako_home):
    """各段相对顺序不变，且不产出空段（【x】\\n 后面必须有内容）。"""
    _touch(hanako_home, "agents/miku/memory/today.md", "今日内容")
    _touch(hanako_home, "agents/miku/memory/facts.md", "事实内容")
    _touch(hanako_home, "agents/miku/memory/longterm.md", "长期内容")

    out = HanakoContext("miku").build_memory_context(max_chars=1000)
    assert out.index("【今日】") < out.index("【事实】") < out.index("【长期】")
    for label in ("今日", "事实", "长期"):
        assert f"【{label}】\n" in out
