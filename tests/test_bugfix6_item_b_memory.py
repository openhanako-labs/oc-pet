# -*- coding: utf-8 -*-
"""BugFix #6-B 单测：对话时注入 Hanako 记忆（today/facts/longterm/memory）。

- 文件存在 → 注入内容（受 max_chars 预算约束，分段截断）。
- 文件缺失（aimis 当前 memory/ 下可能无 today.md/facts.md/longterm.md）
  → read_* 返回空串、build_memory_context 不抛异常、对话不崩。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core import hanako_context as hc
from core.hanako_context import HanakoContext


def _touch(home: Path, rel: str, text: str):
    p = home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture()
def hanako_home(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "HANAKO_HOME", tmp_path)
    return tmp_path


def test_readers_return_empty_when_missing(hanako_home):
    """B：memory 文件缺失 → read_* 返回空串、不抛异常。"""
    ctx = HanakoContext("aimis")
    assert ctx.read_today() == ""
    assert ctx.read_facts() == ""
    assert ctx.read_longterm() == ""
    assert ctx.read_memory() == ""


def test_build_memory_context_empty_when_missing(hanako_home):
    """B：全缺失 → build_memory_context 返回空串（对话不崩）。"""
    ctx = HanakoContext("aimis")
    assert ctx.build_memory_context(max_chars=1000) == ""
    # 也覆盖默认预算
    assert ctx.build_memory_context() == ""


def test_build_memory_context_injects_existing(hanako_home):
    """B：文件存在 → 注入 today/facts/longterm/memory 内容。"""
    _touch(hanako_home, "agents/aimis/memory/today.md", "今日已完成签到")
    _touch(hanako_home, "agents/aimis/memory/facts.md", "用户喜欢咖啡")
    _touch(hanako_home, "agents/aimis/memory/longterm.md", "长期目标：健身")
    _touch(hanako_home, "agents/aimis/memory/memory.md", "最近在学 Python")
    ctx = HanakoContext("aimis")
    out = ctx.build_memory_context(max_chars=1000)
    assert "【今日】" in out and "今日已完成签到" in out
    assert "【事实】" in out and "用户喜欢咖啡" in out
    assert "【长期】" in out and "长期目标：健身" in out
    assert "【记忆】" in out and "最近在学 Python" in out


def test_build_memory_context_respects_budget(hanako_home):
    """B：超预算时截断，合计字符数不超过 max_chars。"""
    big = "内容" * 500  # 1000 字
    _touch(hanako_home, "agents/aimis/memory/today.md", big)
    _touch(hanako_home, "agents/aimis/memory/facts.md", big)
    ctx = HanakoContext("aimis")
    out = ctx.build_memory_context(max_chars=120)
    assert len(out) <= 120


def test_build_memory_context_today_capped_300(hanako_home):
    """B：今日段落硬上限 300 字（其余段吃满剩余预算）。"""
    _touch(hanako_home, "agents/aimis/memory/today.md", "今日" * 200)  # 400 字
    ctx = HanakoContext("aimis")
    out = ctx.build_memory_context(max_chars=1000)
    prefix = "【今日】\n"
    assert out.startswith(prefix)
    assert len(out) - len(prefix) <= 300
