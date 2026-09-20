"""LLM 闸门按来源归因（2026-09-19）。

背景
----
oc-pet 有三条**互不共享配额**的 LLM 流：

    vision   屏幕视觉（screen.py 独立 HTTP 直连，走 vision_model）
    utility  后台任务（screen_enrich / proactive / idle /
             memory_extract / memory_reflect，走 Hana utility_model）
    chat     用户对话（走 models.chat）

但 429 此前只有一个全局 int ``_hits_429``，撞墙时**无法回答「到底是哪条流在撞」**，
只能笼统归因为「共用 provider」，进而调错杠杆（把已拉到位的杠杆再拉一次）。

本测试钉死新增的归因能力：
  1. 429 按 source 分别计数，且**不因一次成功而清零**（累计证据）
  2. 全局 429 计数仍然复位（它驱动冷却翻倍，语义不同）
  3. source → 三条流的归类正确
  4. stats() 是**纯增量**扩展，旧字段一个不少（防止砸掉既有消费者）

放行 / 冷却语义**完全不变**，本测试不涉及。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.llm_gate import LlmGate  # noqa: E402


@pytest.fixture()
def gate():
    """独立实例，避免动进程级单例。"""
    return LlmGate(enabled=True, max_concurrent=8, cooldown_seconds=1.0,
                   max_cooldown_seconds=2.0)


# ── 1. 归因核心 ──

def test_429_counted_per_source(gate):
    gate.notify_429("vision")
    gate.notify_429("vision")
    gate.notify_429("proactive")

    st = gate.stats()
    assert st["hits_429_by_source"]["vision"] == 2
    assert st["hits_429_by_source"]["proactive"] == 1


def test_429_by_source_survives_notify_ok(gate):
    """核心：成功不该抹掉「哪条流在撞」这个答案。"""
    gate.notify_429("vision")
    gate.notify_429("vision")
    gate.notify_ok()

    # 全局计数复位（它驱动冷却翻倍）
    assert gate.stats()["hits_429"] == 0
    # 但按 source 的累计证据必须保留
    assert gate.stats()["hits_429_by_source"]["vision"] == 2


def test_unknown_source_is_recorded(gate):
    gate.notify_429("")
    gate.notify_429(None)
    assert gate.stats()["hits_429_by_source"].get("unknown") == 2


# ── 2. 流归类 ──

@pytest.mark.parametrize("source,stream", [
    ("vision", "vision"),
    ("enrich", "vision"),           # 屏幕语义增强同属屏幕流
    ("proactive", "utility"),
    ("idle", "utility"),
    ("memory_extract", "utility"),
    ("memory_reflect", "utility"),
    ("user", "chat"),
    ("direct", "chat"),
    ("some_new_thing", "other"),    # 未知来源不丢，归到 other
])
def test_stream_classification(source, stream):
    assert LlmGate.stream_of(source) == stream


def test_stream_breakdown_aggregates(gate):
    """按流汇总：used / 429 / 拒绝 三类都要能分开看。"""
    gate.notify_429("vision")
    gate.notify_429("enrich")       # 同属 vision 流
    gate.notify_429("proactive")    # utility 流

    bs = gate.stats()["by_stream"]
    assert bs["vision"]["hits_429"] == 2
    assert bs["utility"]["hits_429"] == 1
    assert bs["chat"]["hits_429"] == 0


def test_stream_breakdown_sums_budget(gate):
    """预算按流汇总（None 表示不限量）。"""
    g = LlmGate(enabled=True, budgets={"vision": 120, "enrich": 40,
                                       "proactive": 30})
    bs = g.stats()["by_stream"]
    assert bs["vision"]["budget"] == 160      # 120 + 40
    assert bs["utility"]["budget"] == 30
    assert bs["chat"]["budget"] is None       # 未设预算 = 不限量


# ── 3. 拒绝（被闸门拦下）也按 source 记 ──

def test_rejects_counted_per_source(gate):
    """预算耗尽导致的拒绝也要能归因 —— 否则「没调用」和「被拦了」分不清。"""
    g = LlmGate(enabled=True, budgets={"enrich": 1})
    assert g.acquire("enrich") is True
    # 第二次超预算 → 被拒
    assert g.acquire("enrich") is False

    st = g.stats()
    assert st["rejects_by_source"].get("enrich") == 1
    assert st["by_stream"]["vision"]["rejects"] == 1


# ── 4. 不破坏既有契约（最关键的一条）──

def test_stats_keeps_legacy_fields(gate):
    """stats() 必须是纯增量：旧字段一个都不能少。

    既有消费者：pet.py:1214（启动日志）、a2a.py:323、llm_gate.py:385。
    """
    gate.notify_429("vision")
    st = gate.stats()
    for key in ("enabled", "max_concurrent", "cooldown_remaining",
                "cooldown_len", "hits_429", "budgets", "used_last_hour"):
        assert key in st, f"旧字段 {key} 丢失，会破坏既有消费者"


def test_notify_429_still_returns_cooldown_length(gate):
    """返回值语义不变：仍返回本轮冷却时长。"""
    n = gate.notify_429("vision")
    assert n > 0
    # 连续撞墙翻倍（既有行为）
    n2 = gate.notify_429("vision")
    assert n2 >= n


def test_disabled_gate_is_noop(gate):
    """闸门关闭时一切是空操作（既有契约）。"""
    g = LlmGate(enabled=False)
    assert g.notify_429("vision") == 0.0
    g.notify_ok()
    assert g.stats()["hits_429_by_source"] == {}


# ── 5. 人可读归因 ──

def test_attribution_report_readable(gate):
    gate.notify_429("vision")
    gate.acquire("proactive")
    r = gate.attribution_report()
    assert "vision" in r
    assert "429=1" in r


def test_attribution_report_empty(gate):
    assert "无后台调用" in gate.attribution_report()
