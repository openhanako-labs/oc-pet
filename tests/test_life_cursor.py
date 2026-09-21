# -*- coding: utf-8 -*-
"""生活游标单测（core/life_cursor.py + 注入段 + 渲染/推送接线）。

锁住四件事：

1. **素材边界**：只吃 category/scenario/topic + 时间戳；`source="vision"` 双重排除；
   **绝不碰 emotion 字段**（旧管线产物，正价区全塌成 happy）。
2. **触发条件**：间隔到期 + 窗口内样本够 + brief 有实质变化，三者都满足才花钱。
3. **不静默**：不触发的**原因**要落在 `snapshot()['last_skip']` 里——
   "没配好"和"时间没到"不能长得一样。
4. **模型可选**：拿不到 utility 模型 → 退化为确定性 brief，这一层不消失。
"""
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.hanako_context import HanakoContext
from core.life_cursor import (
    LifeCursor, build_brief, build_prompt, category_cn, pick_records, prompt_section,
)
from pet_mixins.perception_mixin import PerceptionMixin


def _rec(ts, category="development", topic="", source="foreground", **kw):
    r = {"ts": ts, "category": category, "topic": topic, "source": source}
    r.update(kw)
    return r


# ── pick_records：窗口 + 隐私 ────────────────────────────

def test_pick_records_window_filter():
    now = 1_000_000.0
    recs = [_rec(now - 10), _rec(now - 5000), _rec(now - 100000)]
    got = pick_records(recs, now - 3600, now)
    assert len(got) == 1


def test_pick_records_excludes_vision():
    """视觉事件入库时已不落文本，这里再排一次（双保险）。"""
    now = 1_000_000.0
    recs = [_rec(now - 10, source="vision", topic="某网页标题"),
            _rec(now - 20, topic="写代码")]
    got = pick_records(recs, now - 3600, now)
    assert len(got) == 1
    assert got[0]["topic"] == "写代码"


def test_pick_records_caps_length_keeping_latest():
    now = 1_000_000.0
    recs = [_rec(now - i, topic=f"t{i}") for i in range(300)]
    got = pick_records(recs, 0, now, max_events=5)
    assert len(got) == 5
    assert got[-1]["topic"] == "t0"      # 保最近


def test_pick_records_tolerates_garbage():
    assert pick_records(None, 0, 1) == []
    assert pick_records(["不是字典", 42], 0, 1) == []
    assert pick_records([{"ts": "不是数字"}], 0, 1e18) == []


# ── build_brief：确定性、零模型 ─────────────────────────

def test_build_brief_mentions_count_and_categories():
    now = 1_000_000.0
    recs = [_rec(now - 60, "development", "改配置"),
            _rec(now - 120, "development", "跑测试"),
            _rec(now - 180, "entertainment", "看视频")]
    brief = build_brief(recs, now=now)
    assert "3 条" in brief
    assert "写代码" in brief
    assert "改配置" in brief


def test_build_brief_empty_on_no_records():
    assert build_brief([], now=1.0) == ""
    assert build_brief(None, now=1.0) == ""


def test_build_brief_ignores_noise_categories():
    now = 1_000_000.0
    brief = build_brief([_rec(now - 5, "unknown"), _rec(now - 6, "")], now=now)
    assert "unknown" not in brief


def test_build_brief_never_uses_emotion_field():
    """★ 回归守卫：emotion 是旧管线产物（正价区全塌成 happy），不许出现在 brief 里。"""
    now = 1_000_000.0
    recs = [_rec(now - 5, "development", "改配置", emotion="happy"),
            _rec(now - 6, "development", "跑测试", emotion="happy")]
    brief = build_brief(recs, now=now)
    assert "happy" not in brief
    assert "情绪" not in brief


def test_category_cn_maps_and_falls_back():
    assert category_cn("development") == "写代码"
    assert category_cn("DEVELOPMENT") == "写代码"
    assert category_cn("某种没见过的分类") == "某种没见过的分类"
    assert category_cn(None) == "其他"


# ── prompt ──────────────────────────────────────────────

def test_build_prompt_carries_brief_and_constraints():
    p = build_prompt("最近 6 小时：3 条活动记录")
    assert "3 条活动记录" in p
    assert "不要编造" in p


def test_prompt_section_empty_when_blank():
    assert prompt_section("") == ""
    assert prompt_section("   ") == ""
    assert prompt_section("他刚在改配置") == "【近况】他刚在改配置"


# ── LifeCursor 状态机 ───────────────────────────────────

CFG = {"enabled": True, "window_hours": 6, "interval_minutes": 60,
       "min_events": 3, "max_events": 50}


def test_disabled_returns_no_brief():
    cur = LifeCursor({"enabled": False})
    now = 1_000_000.0
    recs = [_rec(now - i) for i in range(10)]
    assert cur.maybe_brief(recs, now=now) is None
    assert cur.snapshot()["last_skip"] == "disabled"


def test_too_few_events_skip_reason_is_visible():
    cur = LifeCursor(CFG)
    now = 1_000_000.0
    assert cur.maybe_brief([_rec(now - 5)], now=now) is None
    assert "too_few" in cur.snapshot()["last_skip"], "跳过的原因必须可查"


def test_interval_gating():
    cur = LifeCursor(CFG)
    now = 1_000_000.0
    recs = [_rec(now - i, topic=f"t{i}") for i in range(6)]
    assert cur.maybe_brief(recs, now=now) is not None
    cur.mark_rendered("他刚在改配置", brief="x", now=now)
    assert cur.maybe_brief(recs, now=now + 60) is None
    assert cur.snapshot()["last_skip"] == "interval"
    # 过一个周期后可以再来
    later = now + 3601
    recs2 = [_rec(later - i, topic=f"n{i}") for i in range(6)]
    assert cur.maybe_brief(recs2, now=later) is not None


def test_same_brief_does_not_spend_a_call():
    cur = LifeCursor(CFG)
    now = 1_000_000.0
    recs = [_rec(now - i, topic=f"t{i}") for i in range(6)]
    b1 = cur.maybe_brief(recs, now=now)
    cur.mark_rendered("他刚在改配置", brief=b1, now=now)
    later = now + 3601
    recs2 = [_rec(later - i, topic=f"t{i}") for i in range(6)]
    assert cur.maybe_brief(recs2, now=later) is None
    assert cur.snapshot()["last_skip"] == "same_brief"


def test_mark_rendered_and_snapshot():
    cur = LifeCursor(CFG)
    cur.mark_rendered("他刚在改配置补丁", brief="b", now=123.0)
    snap = cur.snapshot()
    assert snap["text"] == "他刚在改配置补丁"
    assert snap["n_rendered"] == 1
    assert snap["enabled"] is True
    assert cur.text == "他刚在改配置补丁"


def test_disabling_resets_state():
    cur = LifeCursor(CFG)
    cur.mark_rendered("旧近况", brief="b", now=1.0)
    cur.set_config({"enabled": False})
    assert cur.text == ""
    assert cur.snapshot()["n_rendered"] == 0


def test_garbage_config_falls_back():
    cur = LifeCursor({"enabled": True, "window_hours": "x",
                      "interval_minutes": None, "min_events": -5})
    s = cur.snapshot()
    assert s["enabled"] is True
    assert s["window_hours"] > 0 and s["min_events"] >= 1


# ── 注入段 ──────────────────────────────────────────────

@pytest.fixture()
def hanako_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HANA_HOME", str(tmp_path))
    return tmp_path


def test_life_cursor_section_appears(hanako_home):
    ctx = HanakoContext(agent_id="miku")
    ctx.set_life_cursor("他刚在改配置，中途切了两次 B 站")
    out = ctx.build_memory_context(max_chars=2000)
    assert "【近况】" in out
    assert "改配置" in out


def test_life_cursor_section_absent_when_empty(hanako_home):
    ctx = HanakoContext(agent_id="miku")
    out = ctx.build_memory_context(max_chars=2000)
    assert "【近况】" not in out


# ── 渲染 / 推送接线 ─────────────────────────────────────

class _FakeAdapter:
    def __init__(self, text=""):
        self._text = text
        self.pushed = []

    def render_life_cursor(self, prompt):
        return self._text

    def set_life_cursor(self, section):
        self.pushed.append(section)


def _fake_with(adapter):
    f = SimpleNamespace(_engine=SimpleNamespace(_adapter=adapter),
                        _life_cursor=LifeCursor(CFG))
    return f


def test_worker_pushes_model_text():
    ad = _FakeAdapter("他最近一直在写代码")
    f = _fake_with(ad)
    PerceptionMixin._render_life_cursor_worker(f, "最近 6 小时：12 条活动记录")
    assert ad.pushed == ["【近况】他最近一直在写代码"]
    assert f._life_cursor.text == "他最近一直在写代码"


def test_worker_falls_back_to_brief_without_model():
    """★ 拿不到 utility 模型 → 用确定性 brief 兜底，不让这一层静默消失。"""
    ad = _FakeAdapter("")
    f = _fake_with(ad)
    brief = "最近 6 小时：12 条活动记录"
    PerceptionMixin._render_life_cursor_worker(f, brief)
    assert ad.pushed == [f"【近况】{brief}"]


def test_worker_never_raises_on_broken_adapter():
    class _Boom:
        def render_life_cursor(self, prompt):
            raise RuntimeError("utility 挂了")

        def set_life_cursor(self, section):
            pass

    f = _fake_with(_Boom())
    PerceptionMixin._render_life_cursor_worker(f, "brief")   # 不应抛出


def test_status_without_state():
    class _NoCursor:
        pass

    out = PerceptionMixin.life_cursor_status(_NoCursor())
    assert out["enabled"] is False
