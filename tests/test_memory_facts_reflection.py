# -*- coding: utf-8 -*-
"""P1-2/P1-3 事实库 FactStore + 反思/摘要引擎 单元测试

验收覆盖（docs/migration-neko-port-plan.md P1-2/P1-3 + 团队任务）：
1. 事实去重：同事实不同表述 → 合并（importance 提升 + reinforcement 累计）；
   反义事实 → 不合并；归一化精确匹配（繁简/标点）
2. LLM 失败跳过：抛异常/空返回/无通道 → 不阻塞、返回全 0
3. 持久化读写：save → 新实例 load → 数据一致（含证据引用）
4. 旧文件兼容：v1 裸数组 / v2 缺字段 → 归一化加载不崩
5. 反思摘要入口：llm_fn 注入 → maybe_reflect/trigger_reflection → 入库 +
   last_reflect_at 推进；周期未到 not_due
6. 反思 LLM 不可用/事件不足 → 跳过并记日志（不推进 last_reflect_at）
7. 面板新卡片类型（offscreen）：reflection_card/fact_card 渲染 +
   MemoryPanel 空数据占位 + 真实 facts/reflections 文件读取
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.memory_facts import (
    FactStore,
    build_extract_prompt,
    normalize_fact_text,
    parse_extracted_facts,
    text_similarity,
)
from core.memory_reflection import (
    ReflectionEngine,
    build_reflect_prompt,
    parse_reflection_insights,
)
from ui.memory_card import KIND_FACT, KIND_REFLECTION, MemoryCard
from ui.memory_panel import (
    MemoryPanel,
    read_fact_store,
    read_facts,
    read_reflections,
)
from PySide6.QtWidgets import QApplication, QLabel


def _ensure_app():
    """offscreen QApplication 单例（面板/卡片测试需要）。"""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ── 1. 事实去重 ──────────────────────────────────────────────────────


def test_dedup_same_fact_different_wording(tmp_path):
    """同事实不同表述 → 合并：importance 取高、reinforcement 累计、不新增。"""
    store = FactStore("miku", memory_dir=tmp_path, use_qt_bridge=False)
    r1 = store.add_facts([{"text": "主人喜欢喝咖啡", "importance": 5, "category": "偏好"}])
    assert r1 == {"added": 1, "merged": 0, "skipped": 0}

    r2 = store.add_facts([{"text": "主人喜欢喝拿铁咖啡", "importance": 6, "category": "偏好"}])
    assert r2 == {"added": 0, "merged": 1, "skipped": 0}

    facts = store.get_facts()
    assert len(facts) == 1, "同事实不同表述应合并为一条"
    f = facts[0]
    assert f["importance"] == 6
    assert f["reinforcement"] > 0.0, "重申应累计 reinforcement"
    assert f["user_fact_reinforce_count"] >= 1


def test_dedup_normalized_exact_match(tmp_path):
    """归一化精确匹配：标点/空白差异 → 命中。"""
    store = FactStore("miku", memory_dir=tmp_path, use_qt_bridge=False)
    store.add_facts([{"text": "主人喜欢喝咖啡。"}])
    store.add_facts([{"text": "  主人喜欢喝咖啡 "}])
    assert len(store.get_facts()) == 1


def test_dedup_antonym_not_merged(tmp_path):
    """反义事实（喜欢猫 vs 讨厌猫）→ 不合并（N.E.K.O. 0.85 余弦阈值语义）。"""
    store = FactStore("miku", memory_dir=tmp_path, use_qt_bridge=False)
    store.add_facts([{"text": "主人喜欢猫"}])
    store.add_facts([{"text": "主人讨厌猫"}])
    assert len(store.get_facts()) == 2


def test_text_similarity_calibrated():
    """相似度函数：改写命中（coverage 高），反义不命中（jaccard/coverage 双低）。"""
    sim = text_similarity("主人喜欢喝咖啡", "主人喜欢喝拿铁咖啡")
    assert sim["coverage"] >= 0.70, f"改写句 coverage 应≥0.70，实际 {sim['coverage']}"
    ant = text_similarity("主人喜欢猫", "主人讨厌猫")
    assert ant["jaccard"] < 0.75 and ant["coverage"] < 0.70


def test_normalize_fact_text():
    assert normalize_fact_text("主人喜欢喝咖啡。") == normalize_fact_text("主人喜欢喝咖啡")


# ── 2. LLM 失败跳过 ─────────────────────────────────────────────────


def test_llm_exception_skips(tmp_path):
    """LLM 抛异常 → 跳过，不阻塞、不写库。"""

    def boom(_prompt: str) -> str:
        raise RuntimeError("llm down")

    store = FactStore("miku", memory_dir=tmp_path, llm_fn=boom, use_qt_bridge=False)
    result = store.record_text_sync("主人喜欢喝咖啡")
    assert result == {"added": 0, "merged": 0, "skipped": 0}
    assert len(store.get_facts()) == 0


def test_llm_empty_skips(tmp_path):
    """LLM 返回空 → 跳过。"""
    store = FactStore("miku", memory_dir=tmp_path, llm_fn=lambda _p: "", use_qt_bridge=False)
    result = store.record_text_sync("主人喜欢喝咖啡")
    assert result["added"] == 0 and result["merged"] == 0


def test_llm_none_channel_skips(tmp_path):
    """无 LLM 通道（无 adapter 无 llm_fn）→ 跳过不崩溃。"""
    store = FactStore("miku", memory_dir=tmp_path, use_qt_bridge=False)
    result = store.record_text_sync("主人喜欢喝咖啡")
    assert result["added"] == 0 and result["merged"] == 0


def test_parse_extracted_facts_defensive():
    """LLM 输出解析：代码块/坏 JSON/非数组 → 防御式处理。"""
    assert len(parse_extracted_facts('[{"text": "A", "importance": 7, "category": "偏好"}]')) == 1
    assert len(parse_extracted_facts('```json\n[{"text": "B", "importance": 3}]\n```')) == 1
    assert len(parse_extracted_facts("不是 JSON")) == 0
    assert len(parse_extracted_facts("")) == 0
    assert len(parse_extracted_facts('{"not": "a list"}')) == 0
    # 坏条目丢弃、合法条目保留
    parsed = parse_extracted_facts('[{"text": "好事实"}, {"text": "", "importance": 9}, 42]')
    assert len(parsed) == 1 and parsed[0]["text"] == "好事实"


def test_build_extract_prompt_contains_source_text():
    prompt = build_extract_prompt("主人喜欢喝咖啡", extra_context="对话")
    assert "主人喜欢喝咖啡" in prompt
    assert "JSON" in prompt


# ── 3. 持久化读写 ───────────────────────────────────────────────────


def test_persistence_roundtrip(tmp_path):
    store = FactStore("miku", memory_dir=tmp_path, use_qt_bridge=False)
    store.add_facts(
        [{"text": "主人喜欢喝咖啡", "importance": 7, "category": "偏好"}],
        evidence=[{"event_id": "e1", "ts": 123.0, "source": "conversation", "quote": "我喜欢喝咖啡"}],
    )
    store2 = FactStore("miku", memory_dir=tmp_path, use_qt_bridge=False)
    facts = store2.get_facts()
    assert len(facts) == 1
    f = facts[0]
    assert f["text"] == "主人喜欢喝咖啡"
    assert f["importance"] == 7
    assert f["evidence"][0]["event_id"] == "e1"
    assert f["status"] in ("pending", "confirmed", "promoted", "archive_candidate")
    assert 0.0 <= f["confidence"] <= 1.0


def test_record_text_sync_with_llm(tmp_path):
    """record_text_sync：LLM 抽取 → 去重 → 入库。"""
    store = FactStore(
        "miku", memory_dir=tmp_path, use_qt_bridge=False,
        llm_fn=lambda _p: '[{"text": "主人喜欢喝咖啡", "importance": 6, "category": "偏好"}]',
    )
    result = store.record_text_sync("我今天喝了咖啡")
    assert result["added"] == 1
    assert store.get_facts()[0]["text"] == "主人喜欢喝咖啡"
    assert store.get_facts()[0]["source"] == "memory_extract"


def test_qt_bridge_flag(tmp_path):
    """use_qt_bridge 开关：开启建桥、关闭走同步。"""
    store_on = FactStore("miku", memory_dir=tmp_path, llm_fn=lambda _p: "[]")
    assert store_on._bridge is not None
    store_off = FactStore("miku", memory_dir=tmp_path, use_qt_bridge=False)
    assert store_off._bridge is None
    # 无桥时 record_text 退化为同步
    store_off2 = FactStore(
        "miku", memory_dir=tmp_path, use_qt_bridge=False,
        llm_fn=lambda _p: '[{"text": "异步测试", "importance": 5, "category": "偏好"}]',
    )
    assert store_off2.record_text("x") is False
    assert len(store_off2.get_facts()) == 1


# ── 4. 旧文件兼容 ───────────────────────────────────────────────────


def test_old_v1_bare_list_compat(tmp_path):
    """v1 旧格式（裸数组 + 最小字段）→ 归一化加载不崩。"""
    (tmp_path / "miku_facts.json").write_text(
        json.dumps([
            {"text": "旧事实一", "importance": 8},
            {"text": "旧事实二"},
        ]), encoding="utf-8")
    store = FactStore("miku", memory_dir=tmp_path, use_qt_bridge=False)
    facts = store.get_facts()
    assert len(facts) == 2
    for f in facts:
        assert f["id"]
        assert f["category"] == "其他"
        assert isinstance(f["evidence"], list)
        assert isinstance(f["confidence"], float)
        assert f["status"] in ("pending", "confirmed", "promoted", "archive_candidate")


def test_old_dict_missing_fields_compat(tmp_path):
    """v2 文件缺字段 → setdefault 补齐，不崩。"""
    (tmp_path / "miku_facts.json").write_text(
        json.dumps({
            "version": 2,
            "meta": {"k": "v"},
            "facts": [{"id": "f_x", "text": "只有文本的旧事实"}],
        }), encoding="utf-8")
    store = FactStore("miku", memory_dir=tmp_path, use_qt_bridge=False)
    facts = store.get_facts()
    assert len(facts) == 1
    assert facts[0]["text"] == "只有文本的旧事实"
    assert facts[0]["importance"] == 5  # 缺省默认


def test_corrupt_file_loads_empty(tmp_path):
    """损坏 JSON → 空档加载，不崩。"""
    (tmp_path / "miku_facts.json").write_text("{broken json", encoding="utf-8")
    store = FactStore("miku", memory_dir=tmp_path, use_qt_bridge=False)
    assert store.get_facts() == []


# ── 5. 查询接口 ─────────────────────────────────────────────────────


def test_fact_query_interfaces(tmp_path):
    store = FactStore("miku", memory_dir=tmp_path, use_qt_bridge=False)
    store.add_facts([
        {"text": "主人喜欢喝咖啡", "importance": 8, "category": "偏好"},
        {"text": "主人讨厌下雨天", "importance": 5, "category": "偏好"},
        {"text": "主人最近在学 Python", "importance": 6, "category": "学习"},
    ])
    # 关键词检索
    hits = store.search("咖啡")
    assert hits and "咖啡" in hits[0]["text"]
    # 主题过滤
    topic = store.by_topic("学习")
    assert len(topic) == 1 and topic[0]["category"] == "学习"
    # 时间范围
    ranged = store.by_time(0, time.time() + 1000)
    assert len(ranged) == 3
    # 置信度过滤（importance 8 → 初始 rein 0.4 → sigmoid≈0.6）
    conf = store.by_confidence(0.55)
    assert conf and conf[0]["text"] == "主人喜欢喝咖啡"
    # 强化后置信度提升、排最前
    top_id = store.get_facts()[0]["id"]
    store.apply_signal(top_id, reinforcement=1.0, source="user_fact")
    assert store.get_facts()[0]["id"] == top_id
    assert store.stats()["total"] == 3


# ── 6. 反思摘要入口 ─────────────────────────────────────────────────


def _make_events(n: int, start_ts: float = 100.0) -> list[dict]:
    return [
        {
            "ts": start_ts + i * 100.0,
            "category": "development",
            "scenario": "late_night_work",
            "topic": f"深夜写代码 {i}",
            "source": "foreground",
            "emotion": "happy",
            "intensity": 0.8,
        }
        for i in range(n)
    ]


def test_reflection_entry_and_period(tmp_path):
    """反思摘要入口：llm_fn 注入 → insight 入库 + last_reflect_at 推进 + 周期 not_due。"""
    events = _make_events(6)

    def fake_llm(_prompt: str) -> str:
        return json.dumps([{
            "observation": "本周有多次深夜写代码",
            "conclusion": "主人最近工作较忙，常在深夜加班",
            "confidence": 0.8,
            "category": "工作",
        }], ensure_ascii=False)

    engine = ReflectionEngine(
        "miku", memory_dir=tmp_path, llm_fn=fake_llm,
        event_source=lambda: events, use_qt_bridge=False,
    )
    result = engine.maybe_reflect(force=True)
    assert result["triggered"] is True
    assert result["added"] == 1
    assert result["reason"] == "ok"

    rs = engine.get_reflections()
    assert len(rs) == 1
    assert rs[0]["conclusion"] == "主人最近工作较忙，常在深夜加班"
    assert rs[0]["confidence"] == 0.8
    assert rs[0]["source_event_count"] == 6
    assert rs[0]["status"] == "pending"
    assert engine.last_reflect_at

    # 周期未到（24h 默认）→ not_due，不重复
    result2 = engine.maybe_reflect()
    assert result2["triggered"] is False
    assert result2["reason"] == "not_due"
    assert len(engine.get_reflections()) == 1


def test_reflection_manual_trigger(tmp_path):
    """手动触发 trigger_reflection() 忽略周期限制。"""
    events = _make_events(5)
    engine = ReflectionEngine(
        "miku", memory_dir=tmp_path,
        llm_fn=lambda _p: '[{"observation": "o", "conclusion": "手动反思", "confidence": 0.6, "category": "其他"}]',
        event_source=lambda: events, use_qt_bridge=False,
    )
    result = engine.trigger_reflection()
    assert result["added"] == 1
    assert len(engine.get_reflections()) == 1


def test_reflection_llm_unavailable_skip(tmp_path):
    """无 LLM 通道 → 跳过（added=0），不推进 last_reflect_at。"""
    events = _make_events(6)
    engine = ReflectionEngine(
        "miku", memory_dir=tmp_path,
        event_source=lambda: events, use_qt_bridge=False,
    )
    result = engine.trigger_reflection()
    assert result["triggered"] is True
    assert result["added"] == 0
    assert result["reason"] == "llm_empty_or_failed"
    assert engine.last_reflect_at == ""
    assert len(engine.get_reflections()) == 0


def test_reflection_llm_exception_skip(tmp_path):
    """LLM 抛异常 → 跳过（added=0），不阻塞。"""
    events = _make_events(6)

    def boom(_p: str) -> str:
        raise RuntimeError("reflect down")

    engine = ReflectionEngine(
        "miku", memory_dir=tmp_path, llm_fn=boom,
        event_source=lambda: events, use_qt_bridge=False,
    )
    result = engine.trigger_reflection()
    assert result["added"] == 0
    assert engine.last_reflect_at == ""


def test_reflection_not_enough_events(tmp_path):
    """事件不足（< min_events）→ not_enough_events，不写库。"""
    events = _make_events(2)
    engine = ReflectionEngine(
        "miku", memory_dir=tmp_path,
        llm_fn=lambda _p: '[{"observation": "o", "conclusion": "c", "confidence": 0.6, "category": "其他"}]',
        event_source=lambda: events, use_qt_bridge=False,
    )
    result = engine.maybe_reflect()
    assert result["reason"] == "not_enough_events"
    assert result["added"] == 0
    assert engine.last_reflect_at == ""
    assert len(engine.get_reflections()) == 0


def test_reflection_persistence_and_old_compat(tmp_path):
    """反思持久化读写 + 旧文件兼容（v1 裸数组）。"""
    events = _make_events(6)
    engine = ReflectionEngine(
        "miku", memory_dir=tmp_path,
        llm_fn=lambda _p: '[{"observation": "o", "conclusion": "持久化反思", "confidence": 0.7, "category": "工作"}]',
        event_source=lambda: events, use_qt_bridge=False,
    )
    engine.trigger_reflection()

    engine2 = ReflectionEngine("miku", memory_dir=tmp_path, use_qt_bridge=False)
    assert len(engine2.get_reflections()) == 1
    assert engine2.get_reflections()[0]["conclusion"] == "持久化反思"
    assert engine2.last_reflect_at

    # 旧文件（v1 裸数组）兼容
    (tmp_path / "miku_reflections.json").write_text(
        json.dumps([{"conclusion": "旧反思", "confidence": 0.5}]), encoding="utf-8")
    engine3 = ReflectionEngine("miku", memory_dir=tmp_path, use_qt_bridge=False)
    assert len(engine3.get_reflections()) == 1
    assert engine3.get_reflections()[0]["conclusion"] == "旧反思"


def test_reflection_privacy_vision_events_stripped(tmp_path):
    """source=vision 事件不参与反思（隐私：不落文本）。"""
    events = _make_events(5) + [
        {"ts": 9999.0, "category": "screen", "topic": "屏幕上显示机密文档", "source": "vision"},
    ]

    def fake_llm(prompt: str) -> str:
        assert "机密文档" not in prompt, "vision 事件文本不应进入反思 prompt"
        return '[{"observation": "o", "conclusion": "c", "confidence": 0.6, "category": "其他"}]'

    engine = ReflectionEngine(
        "miku", memory_dir=tmp_path, llm_fn=fake_llm,
        event_source=lambda: events, use_qt_bridge=False,
    )
    result = engine.trigger_reflection()
    assert result["added"] == 1
    assert engine.get_reflections()[0]["source_event_count"] == 5


def test_parse_reflection_insights_defensive():
    assert len(parse_reflection_insights('[{"observation": "o", "conclusion": "c"}]')) == 1
    assert len(parse_reflection_insights("")) == 0
    assert len(parse_reflection_insights("not json")) == 0
    assert len(parse_reflection_insights('[{"observation": "", "conclusion": ""}]')) == 0
    assert parse_reflection_insights('[{"conclusion": "只有结论"}]')[0]["observation"] == "只有结论"


def test_build_reflect_prompt_contains_events():
    prompt = build_reflect_prompt("深夜写代码 01\n深夜写代码 02")
    assert "深夜写代码" in prompt
    assert "JSON" in prompt


# ── 7. 面板新卡片类型（offscreen）───────────────────────────────────


def test_reflection_card_render_offscreen():
    _ensure_app()
    card = MemoryCard.reflection_card({
        "conclusion": "主人深夜常加班",
        "observation": "多次深夜写代码",
        "confidence": 0.8,
        "category": "工作",
        "status": "pending",
        "source_event_count": 6,
        "created_ts": 1234567890.0,
    })
    assert card.kind == KIND_REFLECTION
    assert "深夜常加班" in card.text
    assert "置信度 80%" in card.meta
    assert "基于 6 条事件" in card.meta


def test_fact_card_from_fact_store_offscreen():
    _ensure_app()
    card = MemoryCard.fact_card({
        "title": "偏好",
        "text": "主人喜欢喝咖啡",
        "meta": "置信度 80% · 状态:confirmed",
        "ts": 1234567890.0,
    })
    assert card.kind == KIND_FACT
    assert "咖啡" in card.text


def test_panel_empty_placeholder_offscreen(tmp_path):
    _ensure_app()
    panel = MemoryPanel("ghost", memory_dir=tmp_path)
    assert panel.card_count == 0
    empties = panel.findChildren(QLabel, "memoryEmpty")
    assert len(empties) >= 1, "全空数据应显示占位文案"


def test_panel_reads_fact_and_reflection_files(tmp_path):
    _ensure_app()
    (tmp_path / "miku_facts.json").write_text(json.dumps({
        "version": 2,
        "facts": [{
            "id": "f1", "text": "主人喜欢喝咖啡", "importance": 6, "category": "偏好",
            "confidence": 0.8, "status": "confirmed",
            "evidence": [{"event_id": "e1", "ts": 1.0, "source": "conversation", "quote": "q"}],
            "created_ts": 1234567890.0,
        }],
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "miku_reflections.json").write_text(json.dumps({
        "version": 2,
        "reflections": [{
            "id": "r1", "conclusion": "深夜常加班", "observation": "多次深夜写代码",
            "confidence": 0.8, "category": "工作", "status": "pending",
            "source_event_count": 6, "created_ts": 1234567890.0,
        }],
    }, ensure_ascii=False), encoding="utf-8")

    panel = MemoryPanel("miku", memory_dir=tmp_path)
    assert panel.card_count >= 2
    kinds = {c.kind for c in panel.cards}
    assert KIND_FACT in kinds
    assert KIND_REFLECTION in kinds


def test_read_fact_store_and_reflections_helpers(tmp_path):
    (tmp_path / "miku_facts.json").write_text(json.dumps({
        "version": 2,
        "facts": [{"id": "f1", "text": "喜欢咖啡", "importance": 6, "category": "偏好",
                   "confidence": 0.8, "status": "confirmed",
                   "evidence": [{"event_id": "e1"}], "created_ts": 1234567890.0}],
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "miku_reflections.json").write_text(json.dumps({
        "version": 2,
        "reflections": [{"id": "r1", "conclusion": "深夜常加班", "observation": "多次深夜写代码",
                         "confidence": 0.8, "category": "工作", "status": "pending",
                         "source_event_count": 6, "created_ts": 1234567890.0}],
    }, ensure_ascii=False), encoding="utf-8")

    fact_cards = read_fact_store("miku", tmp_path)
    assert fact_cards and fact_cards[0]["text"] == "喜欢咖啡"
    assert "置信度" in fact_cards[0]["meta"]
    ref_cards = read_reflections("miku", tmp_path)
    assert ref_cards and ref_cards[0]["text"] == "深夜常加班"
    assert "基于 6 条事件" in ref_cards[0]["meta"]
    # read_facts 优先 FactStore，不回退陪伴摘要
    assert read_facts("miku", tmp_path) == fact_cards


def test_read_facts_fallback_to_companion(tmp_path):
    """FactStore 文件缺失 → read_facts 回退陪伴摘要（旧行为）。"""
    (tmp_path / "miku.json").write_text(json.dumps({
        "total_days": 3,
        "streak_days": 2,
        "last_topic": "项目进展",
        "today": {"development": 5},
    }, ensure_ascii=False), encoding="utf-8")
    cards = read_facts("miku", tmp_path)
    assert cards, "应回退到陪伴摘要"
    texts = " ".join(c["text"] for c in cards)
    assert "陪伴了 3 天" in texts
