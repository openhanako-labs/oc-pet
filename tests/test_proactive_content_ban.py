"""接用测试：ProactiveScheduler 的内容禁言门（core/usage_memory.py 接线）

覆盖 2026-09-10 接线：所有主动说话路径必须经过 _speak，禁言在此拦截一次。
"""
from __future__ import annotations

import time

import pytest

from core.perception.proactive import ProactiveScheduler


class _FakeUsageMemory:
    """可预测的 UsageMemory 替身。"""

    def __init__(self, banned=()):
        self._banned = set(banned)
        self.records = []

    def is_banned(self, content_type: str) -> bool:
        return content_type in self._banned

    def record_usage(self, content_type, action, text="", confidence=1.0):
        self.records.append((content_type, action))
        if action == "explicit_ban":
            self._banned.add(content_type)


def _make_scheduler(memory=None):
    spoken = []
    sched = ProactiveScheduler(on_proactive=spoken.append)
    sched.set_usage_memory(memory if memory is not None else _FakeUsageMemory())
    return sched, spoken


# ── 1. _speak 是唯一出口：禁言在出口拦截 ────────────────────────────

def test_speak_blocks_banned_content():
    mem = _FakeUsageMemory(banned={"writing"})
    sched, spoken = _make_scheduler(mem)

    sched._speak("写了这么久，休息一下吧？", "writing")

    assert spoken == [], "被禁言的内容不得投递"
    assert sched._last_speak_key == "", "被拦截时不应更新最近话题"


def test_speak_allows_unbanned_content():
    mem = _FakeUsageMemory(banned={"writing"})
    sched, spoken = _make_scheduler(mem)

    sched._speak("带我一起玩嘛～", "gaming")

    assert spoken == ["带我一起玩嘛～"]
    assert sched._last_speak_key == "gaming"


def test_speak_without_source_key_is_never_blocked():
    """无内容键 → 不做禁言判断（避免误伤无分类的触发）。"""
    mem = _FakeUsageMemory(banned={"", "writing"})
    sched, spoken = _make_scheduler(mem)

    sched._speak("你好呀", "")

    assert spoken == ["你好呀"]


# ── 2. _deliver 在簿记前拦截（不浪费当日预算与冷却） ────────────────

def test_deliver_skips_before_bookkeeping_when_banned():
    mem = _FakeUsageMemory(banned={"writing"})
    sched, spoken = _make_scheduler(mem)
    before = sched._daily_count

    sched._deliver("写了这么久，休息一下吧？", source_key="writing")

    assert spoken == []
    assert sched._daily_count == before, "被禁内容不得计入当日预算"


def test_deliver_still_works_for_unbanned():
    mem = _FakeUsageMemory()
    sched, spoken = _make_scheduler(mem)

    sched._deliver("好安静啊……", source_key="idle")

    assert spoken == ["好安静啊……"]


# ── 3. ban_last_content：菜单入口写入 explicit_ban，之后同键被拦 ────

def test_ban_last_content_then_blocked():
    mem = _FakeUsageMemory()
    sched, spoken = _make_scheduler(mem)

    sched._speak("又在加班呀？", "overtime")
    assert spoken == ["又在加班呀？"]

    key = sched.ban_last_content(hours=24.0)
    assert key == "overtime"
    assert ("overtime", "explicit_ban") in mem.records

    # 禁言生效：同键再说话被拦
    sched._speak("又在加班呀？", "overtime")
    assert spoken == ["又在加班呀？"], "禁言后不应再投递同内容键"


def test_ban_last_content_without_prior_speech_is_noop():
    sched, _ = _make_scheduler(_FakeUsageMemory())

    assert sched.ban_last_content() == ""


# ── 4. 容错：使用记忆不可用时不得阻断主动对话 ──────────────────────

def test_missing_usage_memory_does_not_block_speaking():
    spoken = []
    sched = ProactiveScheduler(on_proactive=spoken.append)
    sched._usage_memory = False  # 哨兵：已尝试且不可用

    sched._speak("你好呀", "greeting")

    assert spoken == ["你好呀"], "使用记忆不可用时必须放行"


def test_broken_usage_memory_does_not_block_speaking():
    class _Broken:
        def is_banned(self, content_type):
            raise RuntimeError("boom")

    sched, spoken = _make_scheduler(_Broken())

    sched._speak("你好呀", "greeting")

    assert spoken == ["你好呀"], "禁言查询异常时必须放行"


# ── 5. 回归锁定：不得有绕过 _speak 的直调 ────────────────────────────

def test_no_direct_on_proactive_call_outside_speak():
    """源码级检查：on_proactive 只允许在 _speak 内被调用。

    这是本次接线的核心不变量——新增触发路径若直调 on_proactive，禁言会被绕过。
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "core" / "perception" / "proactive.py"
    lines = src.read_text(encoding="utf-8").splitlines()

    start = next(i for i, l in enumerate(lines) if l.strip().startswith("def _speak("))
    # _speak 函数体：缩进 8 空格，直到下一个同级 def
    end = next(
        (i for i in range(start + 1, len(lines))
         if lines[i].strip().startswith(("def ", "class "))),
        len(lines),
    )

    offenders = [
        (i + 1, lines[i]) for i, _ in enumerate(lines)
        if re.search(r"(?<![\w.])self\.on_proactive\(", lines[i])
        and not (start <= i < end)
    ]

    assert not offenders, f"存在绕过 _speak 的 on_proactive 直调：{offenders}"
