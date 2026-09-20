# -*- coding: utf-8 -*-
"""人格来源 / 模板变量 / 提示冲突 三个修复的回归测试（2026-09-20）。

## 用户指正

> 谁的人设：助手的啊，桌宠本身就不应该有人设，理解吗

## 三个问题同一个根因

桌宠的 `_current_char` 是**模型包名**（"miku"），但被当成了**人格来源**。
于是：

1. **人格读错**：读 `agents/miku/identity.md`（初音未来）而不是
   `agents/ophelia/identity.md`（奥菲莉娅）
2. **模板变量没替换**：miku 的 identity.md 里有角色卡语法 `{{userName}}`，
   桌宠读原文 → 模型看到字面量
3. **提示重复冲突**：idle_chatter 模板末尾的 `加 [emotion:xxx]`
   与输出规则块的 `[emotion:情绪词]` 打架，且 `xxx` 是字面占位符

概念区分：

| | 含义 | 来源 |
|---|---|---|
| `_current_char` | 画的是谁（Live2D 模型包） | `config.character` |
| 人格来源 | 说话的是谁（助手） | `config.dialog.agent_id` |
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("OC_DISABLE_TRAY", "1")
os.environ.setdefault("OC_DISABLE_PERCEPTION", "1")
os.environ.setdefault("OC_DISABLE_LIVE2D", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


@pytest.fixture()
def pet():
    """最小 PetWindow 替身（不构造 Qt 窗口）。"""
    from pet import PetWindow

    w = PetWindow.__new__(PetWindow)
    w.config = {"dialog": {"agent_id": "ophelia"}}
    w._current_char = "miku"
    return w


# ── 1. 人格来源 ──


def test_persona_agent_id_prefers_dialog_agent(pet):
    """★ 核心：人格来源是 dialog.agent_id（助手），不是模型包名。"""
    assert pet._persona_agent_id() == "ophelia"


def test_persona_falls_back_to_char_when_unbound():
    """未绑定 dialog agent 时回退模型包名（不崩）。"""
    from pet import PetWindow

    w = PetWindow.__new__(PetWindow)
    w.config = {}
    w._current_char = "miku"
    assert w._persona_agent_id() == "miku"


def test_persona_survives_bad_config():
    """config 异常时回退（不抛）。"""
    from pet import PetWindow

    w = PetWindow.__new__(PetWindow)
    w.config = None
    w._current_char = "miku"
    assert w._persona_agent_id() == "miku"


def test_inject_uses_persona_not_char():
    """源码级：_inject_agent_identity 必须用 _persona_agent_id()。

    注意：docstring 里会提到旧写法作反例，所以**只看代码行**
    （去掉注释与字符串后的部分）。
    """
    import ast

    path = os.path.join(_REPO, "pet.py")
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_inject_agent_identity")
    # 只取函数体内非 docstring 的语句
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(
            getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    code = "\n".join(ast.get_source_segment(src, s) or "" for s in body)
    assert "_persona_agent_id()" in code, "身份注入未用助手来源"
    assert "HanakoContext(self._current_char)" not in code, (
        "不该直接用模型包名当身份来源")


# ── 2. 模板变量替换 ──


def test_substitute_replaces_username(pet):
    """★ `{{userName}}` 必须被替换掉，不能留字面量。"""
    out = pet._substitute_card_vars("你是{{userName}}的桌宠。")
    assert "{{" not in out, "仍有未替换的模板变量"
    assert "userName" not in out


def test_substitute_handles_multiple_occurrences(pet):
    out = pet._substitute_card_vars("{{userName}}你好，{{userName}}再见。")
    assert "{{" not in out
    assert out.count("再见") == 1


def test_substitute_noop_without_placeholder(pet):
    """没有占位符时原样返回。"""
    s = "普通文本，没有变量。"
    assert pet._substitute_card_vars(s) == s


def test_substitute_handles_empty(pet):
    assert pet._substitute_card_vars("") == ""
    assert pet._substitute_card_vars(None) is None


def test_substitute_never_leaves_placeholder_on_failure(pet, monkeypatch):
    """取用户名失败时回退中性称呼，**绝不留占位符**。"""
    monkeypatch.setattr(type(pet), "_user_display_name", lambda self: "")
    out = pet._substitute_card_vars("你是{{userName}}的桌宠。")
    assert "{{" not in out, "失败时留了字面占位符"


def test_real_miku_identity_has_placeholder():
    """实测前提：miku 的 identity.md 里确实有 {{userName}}。

    若这条失败，说明该文件被改了——本组修复的动机需重新评估。
    """
    from core.hanako_context import HanakoContext
    ident = HanakoContext("miku").read_identity() or ""
    if not ident:
        pytest.skip("miku 的 identity.md 不存在")
    assert "{{userName}}" in ident, "miku identity 已无占位符（前提变了）"


# ── 3. 模板冲突 ──


def test_chatter_templates_have_no_emotion_placeholder():
    """★ idle_chatter 模板不得含字面 `[emotion:xxx]`。

    它与输出规则块冲突：规则说从 10 个词里选，模板说写 `xxx`。
    """
    from core.idle_chatter import _CHATTER_TEMPLATES
    bad = [t for t in _CHATTER_TEMPLATES if "emotion:xxx" in t]
    assert not bad, f"这些模板仍含字面占位符: {bad}"


def test_chatter_templates_have_no_emotion_tag_at_all():
    """情绪标签的唯一来源是输出规则，模板不该重复教。"""
    from core.idle_chatter import _CHATTER_TEMPLATES
    for t in _CHATTER_TEMPLATES:
        assert "[emotion:" not in t, f"模板重复教情绪标签: {t[:50]}"


def test_screen_templates_clean():
    """屏幕主动评论模板同样不该含情绪标签占位符。"""
    src = open(os.path.join(_REPO, "core", "perception", "screen.py"),
               encoding="utf-8").read()
    i = src.index("_PROACTIVE_TEMPLATES = [")
    body = src[i:i + 1800]
    assert "emotion:xxx" not in body


# ── 4. 身份不截断 ──


def test_identity_not_truncated_in_idle():
    """身份注入不得截断——会把助手 identity 切在半句。"""
    src = open(os.path.join(_REPO, "core", "idle_chatter.py"),
               encoding="utf-8").read()
    assert "agent_identity[:200]" not in src, "idle_chatter 仍截断身份"


def test_identity_not_truncated_in_screen():
    src = open(os.path.join(_REPO, "core", "perception", "screen.py"),
               encoding="utf-8").read()
    assert "agent_brief[:150]" not in src, "screen 仍截断身份"


def test_persona_identity_is_longer_than_old_limit():
    """实测：助手 identity 比旧的 150/200 字上限长——所以截断确实会切坏。"""
    from core.hanako_context import HanakoContext
    ident = HanakoContext("ophelia").read_identity() or ""
    if not ident:
        pytest.skip("ophelia 的 identity.md 不存在")
    assert len(ident) > 200, (
        f"助手身份仅 {len(ident)} 字——截断问题的影响需重估")
