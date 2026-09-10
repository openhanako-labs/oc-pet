"""接线测试：memory_filter（事实类记忆过滤 + 引用日期明示）

覆盖 2026-09-10 接线：
1. 关键词表重叠的回归锁定（原实现「喜欢」同在两表，情感优先 → 偏好类记忆全丢）
2. is_fact_memory 行为符合模块 docstring
3. filter_facts_only
4. add_memory_citation
5. build_memory_context 中 长期/记忆 两段被过滤，今日/事实 不被过滤
"""
from __future__ import annotations

import pytest

from core import memory_filter as mf
from core.memory_filter import (
    EMOTIONAL_STATE_KEYWORDS,
    FACT_KEYWORDS,
    PREFERENCE_KEYWORDS,
    add_memory_citation,
    filter_facts_only,
    is_fact_memory,
)


# ── 1. 回归锁定：关键词表不得重叠 ────────────────────────────────

def test_no_keyword_overlap_between_lists():
    """原 bug：『喜欢』同时存在于情感表与事实表，且情感判定在前。

    这个不变量就是当初缺的那道闸门——有它就不会出现偏好类记忆被整类丢弃。
    """
    facts = set(FACT_KEYWORDS) | set(PREFERENCE_KEYWORDS)
    overlap = set(EMOTIONAL_STATE_KEYWORDS) & facts
    assert not overlap, f"情感表与事实/偏好表重叠：{sorted(overlap)}"


def test_no_bare_single_char_broad_keywords():
    """单字泛词（想/爱）会造成大面积误判：『爱好』『想法』『想要』都含它们。

    允许的单字表仅限语义明确的（情感表里此处为空）。
    """
    singles = [kw for kw in EMOTIONAL_STATE_KEYWORDS if len(kw) == 1]
    assert not singles, f"情感表含过泛单字词：{singles}"


# ── 2. is_fact_memory 符合 docstring ─────────────────────────────

@pytest.mark.parametrize("content,expected", [
    # docstring 明确列举的应保留项
    ("今天有考试", True),
    ("下午三点有会议", True),
    ("用户喜欢美式咖啡", True),          # ← 原实现返回 False，本接线修的就是它
    # 偏好 / 立场
    ("用户偏好深色主题", True),
    ("用户讨厌香菜", True),
    ("用户的爱好是摄影", True),
    # 情感状态 → 不进调度
    ("最近心情不好", False),
    ("我觉得很孤单", False),
    ("今天很难过", False),
])
def test_is_fact_memory(content, expected):
    assert is_fact_memory(content) is expected


def test_datetime_fact_not_masked_by_emotional_word():
    """『考试压力』含情感词，但带日期的时间事实应通过。"""
    assert is_fact_memory("2026-09-20 考试，压力有点大") is True


def test_empty_content_is_not_fact():
    assert is_fact_memory("") is False


# ── 3. filter_facts_only ────────────────────────────────────────

def test_filter_facts_only_keeps_facts_drops_emotions():
    ctx = "\n".join([
        "用户喜欢美式咖啡",
        "最近心情很低落",
        "明天上午十点有会议",
        "",
        "   ",
        "用户讨厌香菜",
    ])
    out = filter_facts_only(ctx)
    lines = [l for l in out.split("\n") if l.strip()]
    assert "用户喜欢美式咖啡" in lines
    assert "明天上午十点有会议" in lines
    assert "用户讨厌香菜" in lines
    assert "最近心情很低落" not in lines


def test_filter_facts_only_empty_input():
    assert filter_facts_only("") == ""


# ── 4. add_memory_citation ──────────────────────────────────────

def test_add_memory_citation_appends_date():
    out = add_memory_citation("你之前提过这家店", "2026-09-08")
    assert "2026-09-08" in out
    assert out.startswith("你之前提过这家店")


def test_add_memory_citation_without_date_is_noop():
    assert add_memory_citation("你之前提过这家店") == "你之前提过这家店"


# ── 5. build_memory_context 的过滤落点 ───────────────────────────

def _ctx_with(**readers):
    """构造一个只带读取器的 HanakoContext 替身。

    readers 的值本身就是可调用对象（如 `read_today=lambda: "..."`），
    直接挂到实例上即可——不要再包一层。
    """
    from core.hanako_context import HanakoContext
    ctx = HanakoContext.__new__(HanakoContext)
    for name, reader in readers.items():
        setattr(ctx, name, reader)
    return ctx


def test_longterm_and_memory_are_filtered():
    ctx = _ctx_with(
        read_today=lambda: "",
        read_facts=lambda: "",
        read_longterm=lambda: "用户喜欢美式咖啡\n最近情绪很低落",
        read_memory=lambda: "用户讨厌香菜\n今天很焦虑",
    )
    out = ctx.build_memory_context(max_chars=2000)

    assert "用户喜欢美式咖啡" in out
    assert "用户讨厌香菜" in out
    assert "最近情绪很低落" not in out
    assert "今天很焦虑" not in out


def test_today_and_facts_are_not_filtered():
    """今日/事实两段不过滤：前者按日期作用域，后者文件名即事实。"""
    ctx = _ctx_with(
        read_today=lambda: "今天心情不错",
        read_facts=lambda: "用户情绪记录：想家",
        read_longterm=lambda: "",
        read_memory=lambda: "",
    )
    out = ctx.build_memory_context(max_chars=2000)

    assert "今天心情不错" in out
    assert "用户情绪记录：想家" in out


def test_filter_failure_does_not_break_memory_injection(monkeypatch):
    """filter 不可用 → 原样注入，绝不阻断。"""
    def _boom(_):
        raise RuntimeError("boom")

    monkeypatch.setattr(mf, "filter_facts_only", _boom)

    ctx = _ctx_with(
        read_today=lambda: "",
        read_facts=lambda: "",
        read_longterm=lambda: "最近情绪很低落",
        read_memory=lambda: "",
    )
    out = ctx.build_memory_context(max_chars=2000)

    # 要么过滤失败被兜住原样保留，要么调用方捕获异常；不论哪种都不得抛
    assert isinstance(out, str)
