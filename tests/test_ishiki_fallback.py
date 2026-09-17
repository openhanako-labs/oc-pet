# -*- coding: utf-8 -*-
"""需求④B 回归：ishiki 断链修复。

Hanako 将 ishiki.md 改名为 AGENTS.md 后,oc-pet 仍只读 ishiki.md,
导致意识层整层静默失效(无任何告警,且 build_prompt 漏掉人格)。

修复：
- read_ishiki 回落链 AGENTS.md→ishiki.md→awareness.md
- read_public_ishiki 回落链 AGENTS.public.md→public-ishiki.md
- 全部缺失/空时告警(不再静默返回 "")
- validate() 用意识层可用性替代字面 ishiki.md 检查
"""
import os
import sys
import logging
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core import hanako_context as hc
from core.hanako_context import HanakoContext


@pytest.fixture()
def hanako_home(tmp_path, monkeypatch):
    """把 Hana 数据目录指到 tmp_path。

    2026-09-17：改用 HANA_HOME 环境变量而非 patch 模块常量——
    `core/hanako_context.py` 不再用模块级 `HANAKO_HOME`（那会在 import
    时锁死路径），改成 `hanako_home()` 每次解析，与 Hana 官方逻辑一致。
    """
    monkeypatch.setenv("HANA_HOME", str(tmp_path))
    return tmp_path


def _touch(home, rel, text):
    p = home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_read_ishiki_falls_back_to_agents_md(hanako_home):
    """AGENTS.md 存在、ishiki.md 缺失 → 读到 AGENTS.md 内容。"""
    _touch(hanako_home, "agents/aimis/AGENTS.md", "# aimis\n- 说话风格: 冷幽默\n")
    _touch(hanako_home, "agents/aimis/identity.md", "# aimis")
    _touch(hanako_home, "agents/aimis/description.md", "desc")
    ctx = HanakoContext("aimis")
    assert "冷幽默" in ctx.read_ishiki()


def test_read_ishiki_falls_back_to_awareness_md(hanako_home):
    """AGENTS.md/ishiki.md 都缺失,awareness.md 存在 → 读到 awareness.md。"""
    _touch(hanako_home, "agents/miku/awareness.md", "# Awareness\n- 设定: 元气\n")
    ctx = HanakoContext("miku")
    assert "元气" in ctx.read_ishiki()


def test_read_public_ishiki_falls_back_to_agents_public_md(hanako_home):
    """AGENTS.public.md 存在、public-ishiki.md 缺失 → 读到 AGENTS.public.md。"""
    _touch(hanako_home, "agents/aimis/AGENTS.public.md", "对外: 友善\n")
    ctx = HanakoContext("aimis")
    assert "友善" in ctx.read_public_ishiki()


def test_read_ishiki_warns_when_all_missing(hanako_home, caplog):
    """意识文件全缺失 → 必须告警(不再静默返回 "")。"""
    _touch(hanako_home, "agents/ghost/identity.md", "# ghost")
    ctx = HanakoContext("ghost")
    with caplog.at_level(logging.WARNING, logger="core.hanako_context"):
        out = ctx.read_ishiki()
    assert out == ""
    assert any("意识/规则文件缺失或为空" in r.message for r in caplog.records)


def test_validate_does_not_false_flag_ishiki(hanako_home):
    """有 AGENTS.md 时,validate() 不应把 ishiki.md 列为缺失。"""
    _touch(hanako_home, "agents/aimis/AGENTS.md", "# aimis")
    _touch(hanako_home, "agents/aimis/identity.md", "# aimis")
    _touch(hanako_home, "agents/aimis/description.md", "desc")
    ctx = HanakoContext("aimis")
    missing = ctx.validate()
    assert not any("ishiki" in m for m in missing), missing


def test_validate_flags_consciousness_when_truly_absent(hanako_home):
    """意识文件全缺 → validate() 应列出意识文件缺失。"""
    _touch(hanako_home, "agents/ghost/identity.md", "# ghost")
    ctx = HanakoContext("ghost")
    missing = ctx.validate()
    assert any("意识文件" in m for m in missing), missing
