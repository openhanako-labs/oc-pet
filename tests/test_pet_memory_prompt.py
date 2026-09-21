# -*- coding: utf-8 -*-
"""桌宠本体记忆进对话（读取侧合并）单测。

背景（2026-09-21）：oc-pet 自建的事实库（core/memory_facts.FactStore）带
证据双时钟与状态机，但此前只活在 UI 卡片与场景召回里，**不进对话**。

本文件锁住两件事：

1. 渲染端 `render_facts_for_prompt`：按证据置信度排序、跳过该淘汰的、
   去重、限长、无内容返回空串；
2. 注入端 `HanakoContext.build_memory_context`：推送过才有【桌宠】段，
   没推送过完全不出现（行为与旧版一致，零回归面）。
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.hanako_context import HanakoContext
from core.memory_facts import render_facts_for_prompt


def _touch(home: Path, rel: str, text: str):
    p = home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture()
def hanako_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HANA_HOME", str(tmp_path))
    return tmp_path


# ── 渲染端 ──────────────────────────────────────────────

def test_render_empty_and_noise():
    assert render_facts_for_prompt([]) == ""
    assert render_facts_for_prompt([{"id": "a"}]) == ""           # 无 text
    assert render_facts_for_prompt([{"text": "   "}]) == ""       # 空白 text
    assert render_facts_for_prompt([None, "x"]) == ""             # 非 dict 容错


def test_render_orders_by_confidence():
    """protected 事实置信度 1.0，应排在最前。"""
    now = datetime.now(timezone.utc)
    facts = [
        {"id": "1", "text": "普通事实"},
        {"id": "2", "text": "受保护事实", "protected": True},
        {"id": "3", "text": "另一条普通事实"},
    ]
    out = render_facts_for_prompt(facts, now=now)
    lines = [l for l in out.split("\n") if l]
    assert lines[0] == "- 受保护事实"
    assert len(lines) == 3


def test_render_skips_archive_candidate():
    """证据净强度 ≤ -0.5 → archive_candidate → 不进对话。"""
    now = datetime.now(timezone.utc)
    facts = [
        {"id": "keep", "text": "该留的事实"},
        {
            "id": "drop",
            "text": "该淘汰的事实",
            "disputation": 5.0,
            "disp_last_signal_at": now.isoformat(),
        },
    ]
    out = render_facts_for_prompt(facts, now=now)
    assert "该留的事实" in out
    assert "该淘汰的事实" not in out


def test_render_dedups_and_limits():
    now = datetime.now(timezone.utc)
    facts = [{"id": str(i), "text": "重复事实"} for i in range(5)]
    facts += [{"id": f"u{i}", "text": f"唯一事实{i}"} for i in range(30)]
    out = render_facts_for_prompt(facts, limit=4, now=now)
    lines = [l for l in out.split("\n") if l]
    assert len(lines) <= 4
    assert len([l for l in lines if l == "- 重复事实"]) == 1


def test_render_respects_max_chars():
    now = datetime.now(timezone.utc)
    facts = [{"id": str(i), "text": "很长的单条事实内容" * 20} for i in range(10)]
    out = render_facts_for_prompt(facts, limit=10, max_chars=120, now=now)
    assert len(out) <= 120


# ── 注入端 ──────────────────────────────────────────────

def test_pet_section_absent_by_default(hanako_home):
    """没推送过 → 【桌宠】不出现（默认行为与旧版一致）。"""
    _touch(hanako_home, "agents/miku/memory/today.md", "今日已完成签到")
    _touch(hanako_home, "agents/miku/memory/facts.md", "用户喜欢咖啡")
    out = HanakoContext("miku").build_memory_context(max_chars=1000)
    assert "【桌宠】" not in out
    assert "【今日】" in out


def test_pet_section_injected_when_pushed(hanako_home):
    _touch(hanako_home, "agents/miku/memory/today.md", "今日已完成签到")
    _touch(hanako_home, "agents/miku/memory/facts.md", "用户喜欢咖啡")
    ctx = HanakoContext("miku")
    ctx.set_pet_memory("- 主人有只猫叫布丁")
    out = ctx.build_memory_context(max_chars=1000)
    assert "【桌宠】" in out
    assert "主人有只猫叫布丁" in out
    assert len(out) <= 1000


def test_pet_section_cleared_by_empty_push(hanako_home):
    _touch(hanako_home, "agents/miku/memory/today.md", "今日已完成签到")
    ctx = HanakoContext("miku")
    ctx.set_pet_memory("- 主人有只猫叫布丁")
    assert "【桌宠】" in ctx.build_memory_context(max_chars=1000)
    ctx.set_pet_memory("")
    assert "【桌宠】" not in ctx.build_memory_context(max_chars=1000)


def test_pet_memory_reader_is_safe_without_push():
    """直接调 read_pet_memory（未经 __init__ 设置）也不该抛。"""
    ctx = HanakoContext("miku")
    assert ctx.read_pet_memory() == ""
