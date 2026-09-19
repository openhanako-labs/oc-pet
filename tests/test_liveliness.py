# -*- coding: utf-8 -*-
"""生动层（台词池 + 情境选择）单元测试。

随机走注入的 rng，所以断言是可复现的、不靠运气。
"""
from __future__ import annotations

import random
from pathlib import Path

from core.liveliness import (
    POOLS,
    LineBank,
    greeting,
    greeting_key,
    is_night,
)

ROOT = Path(__file__).resolve().parent.parent
KNOWN_EMOTIONS = {
    "happy", "cute", "neutral", "thinking", "sad", "surprised",
    "working", "idle",
}


# ── 池子本身 ──────────────────────────────────────────────


def test_every_pool_has_enough_variety():
    """池子太小就等于没生动——每池至少 3 句。"""
    for key, pool in POOLS.items():
        assert len(pool) >= 3, f"{key} 只有 {len(pool)} 句"


def test_every_line_is_well_formed():
    for key, pool in POOLS.items():
        for text, emotion in pool:
            assert isinstance(text, str) and text.strip(), f"{key} 有空白台词"
            assert emotion in KNOWN_EMOTIONS, f"{key} 用了未知情绪 {emotion!r}"


def test_no_duplicate_lines_in_a_pool():
    for key, pool in POOLS.items():
        texts = [t for t, _ in pool]
        assert len(texts) == len(set(texts)), f"{key} 有重复台词"


def test_greeting_keys_are_all_defined():
    """greeting_key 可能返回的每个 key 都必须真有池子。"""
    for hour in (0, 3, 9, 14, 23):
        for gap in (0, 600, 3600, 20000, 999999):
            key = greeting_key(gap, hour=hour)
            assert key in POOLS, f"{key} 没有对应的台词池"


# ── LineBank 取词 ─────────────────────────────────────────


def test_pick_returns_text_and_emotion():
    d = LineBank().pick("return_brief")
    assert d is not None
    assert d["text"] in [t for t, _ in POOLS["return_brief"]]
    assert d["emotion"] in KNOWN_EMOTIONS


def test_unknown_key_returns_none():
    assert LineBank().pick("nope") is None


def test_empty_pool_returns_none():
    assert LineBank(pools={"empty": ()}).pick("empty") is None


def test_no_immediate_repeat_across_many_picks():
    """连续取很多次，紧邻两句不得相同（这是"生动"的底线）。"""
    b = LineBank(rng=random.Random(7))
    prev = None
    for _ in range(200):
        cur = b.pick("return_long")["text"]
        assert cur != prev, "连续两次说了同一句"
        prev = cur


def test_two_line_pool_still_alternates():
    """池子只有 2 句、history 很大时，也必须轮流（历史长度被钳到 len-1）。"""
    pool = (("甲", "happy"), ("乙", "cute"))
    b = LineBank(pools={"pair": pool}, history=99, rng=random.Random(1))
    seq = [b.pick("pair")["text"] for _ in range(10)]
    assert all(seq[i] != seq[i + 1] for i in range(len(seq) - 1))
    assert set(seq) == {"甲", "乙"}


def test_history_is_bounded():
    b = LineBank(history=3, rng=random.Random(2))
    for _ in range(50):
        b.pick("return_long")
    assert len(b.recent("return_long")) <= 4     # history + 1


def test_recent_is_per_key():
    b = LineBank(rng=random.Random(3))
    b.pick("return_brief")
    assert b.recent("return_brief")
    assert b.recent("return_night") == []


def test_every_line_gets_a_turn():
    """取 len(pool)*N 次，池子里每一句都应出现过（没有永远轮不到的死句）。"""
    key = "return_mid"
    b = LineBank(rng=random.Random(11))
    seen = {b.pick(key)["text"] for _ in range(len(POOLS[key]) * 20)}
    assert seen == {t for t, _ in POOLS[key]}


def test_rng_injection_is_reproducible():
    a = [LineBank(rng=random.Random(42)).pick("return_long")["text"] for _ in range(1)]
    b = LineBank(rng=random.Random(42))
    assert a[0] == b.pick("return_long")["text"]


# ── 情境选择（逻辑生动的那一半）────────────────────────────


def test_night_detection():
    assert is_night(23) and is_night(0) and is_night(3) and is_night(4)
    assert not is_night(5) and not is_night(12) and not is_night(22)


def test_night_beats_duration():
    """凌晨三点回来，重点不是走了多久，是"这个点了"。"""
    assert greeting_key(10, hour=3) == "return_night"
    assert greeting_key(999999, hour=2) == "return_night"


def test_gap_tiers():
    assert greeting_key(60, hour=12) == "return_brief"
    assert greeting_key(300, hour=12) == "return_brief"
    assert greeting_key(3600, hour=12) == "return_mid"
    assert greeting_key(7200, hour=12) == "return_mid"
    assert greeting_key(86400, hour=12) == "return_long"


def test_gap_boundaries_are_exclusive_upper():
    assert greeting_key(1799, hour=12) == "return_brief"
    assert greeting_key(1800, hour=12) == "return_mid"
    assert greeting_key(10799, hour=12) == "return_mid"
    assert greeting_key(10800, hour=12) == "return_long"


def test_bad_gap_does_not_blow_up():
    assert greeting_key(None, hour=12) == "return_brief"
    assert greeting_key("oops", hour=12) == "return_brief"
    assert greeting_key(-5, hour=12) == "return_brief"


def test_greeting_is_gap_sensitive():
    """同样在下午，走了 1 分钟和走了 1 天的台词池必须不同。"""
    bank = LineBank(rng=random.Random(5))
    brief = {greeting(60, hour=14, bank=bank)["text"] for _ in range(30)}
    long_ = {greeting(200000, hour=14, bank=bank)["text"] for _ in range(30)}
    assert not (brief & long_), "刚走开和久别用了同一套话"


def test_greeting_never_returns_none():
    """打招呼是"该说话"的场合——池子坏了也得有保底，不能静默。"""
    empty = LineBank(pools={})
    g = greeting(60, hour=12, bank=empty)
    assert isinstance(g, dict) and g["text"] and g["emotion"]


def test_greeting_emotions_stay_in_vocabulary():
    bank = LineBank(rng=random.Random(9))
    for gap in (30, 3600, 999999):
        for hour in (3, 12, 23):
            assert greeting(gap, hour=hour, bank=bank)["emotion"] in KNOWN_EMOTIONS


# ── 接线守卫（别让硬编码回来）──────────────────────────────


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_hardcoded_video_line_is_no_longer_shown():
    """那句“你看了个有趣的视频啊～”（断言自己没看清的内容）不能再被**显示**。

    只看调用形式、不看字面量：注释里引它作反面例子是可以的。
    """
    s = _src("pet_mixins/behavior_mixin.py")
    assert 'self._show_bubble("你看了个有趣的视频啊' not in s
    assert "_say_from(\"screen_fallback\"" in s, "兜底没走台词池"


def test_behavior_uses_liveliness():
    s = _src("pet_mixins/behavior_mixin.py")
    assert "_greet_on_return(" in s and "def _greet_on_return" in s
    assert "_say_from(" in s and "def _say_from" in s
    assert 'get_bank().pick(' in s


def test_return_greeting_no_longer_inline():
    """回来打招呼必须走生动层，不能又退回内联的固定句。"""
    s = _src("pet_mixins/behavior_mixin.py")
    assert 'self._show_bubble("你回来啦~", emotion="happy")' in s   # 只剩保底那一处
    assert s.count('self._show_bubble("你回来啦~", emotion="happy")') == 1
