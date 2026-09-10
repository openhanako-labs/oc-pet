# -*- coding: utf-8 -*-
"""P0-4 事件日志字段归一 + 隐私截断单元测试

验收覆盖（docs/migration-neko-port-plan.md P0-4）：
1. record_event 写入事件含 scenario/intent/emotion/intensity/source 字段
2. 隐私规则生效：source=vision 不落文本（topic/summary/detail/text 剥离）
3. 旧文件兼容加载不崩（缺新字段 → 读取时自动补默认值）
4. topic 截断 ≤ 60 字；intensity 夹到 [0,1]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.companion_memory import CompanionMemory
from core.event_stream import (
    DEFAULT_EMOTION,
    DEFAULT_INTENSITY,
    DEFAULT_SCENARIO,
    EventStream,
    TOPIC_MAX_CHARS,
)


def _stream(tmp_path: Path) -> EventStream:
    return EventStream("t4-test", memory_dir=tmp_path)


def _mem(tmp_path: Path, stream: EventStream) -> CompanionMemory:
    mem = CompanionMemory("t4-test", memory_dir=tmp_path)
    mem.set_event_stream(stream)
    return mem


# ── 1. 字段归一 ───────────────────────────────────────────────────────


def test_record_event_writes_all_normalized_fields(tmp_path):
    """record_event 写入事件含 scenario/intent/emotion/intensity/source。"""
    stream = _stream(tmp_path)
    mem = _mem(tmp_path, stream)
    mem.set_emotion_provider(lambda: ("happy", 0.8))
    mem.record_event(
        category="development", scenario="late_night_work", intent="deep_work",
        emotion="", intensity=0.0, topic="重构事件流模块", source="foreground",
    )
    events = stream.read_all()
    assert len(events) == 1
    ev = events[0]
    assert ev["scenario"] == "late_night_work"
    assert ev["intent"] == "deep_work"
    assert ev["emotion"] == "happy"
    assert ev["intensity"] == 0.8
    assert ev["source"] == "foreground"
    assert ev["category"] == "development"


def test_record_event_explicit_emotion_intensity(tmp_path):
    """显式 emotion+intensity 时不再覆盖为 provider 快照。"""
    stream = _stream(tmp_path)
    mem = _mem(tmp_path, stream)
    mem.set_emotion_provider(lambda: ("sad", 0.9))
    mem.record_event(category="gaming", emotion="angry", intensity=0.4,
                     source="foreground")
    ev = stream.read_all()[0]
    assert ev["emotion"] == "angry"
    assert ev["intensity"] == 0.4


# ── 2. 隐私规则：source=vision 不落文本 ───────────────────────────────


def test_vision_event_does_not_persist_topic(tmp_path):
    """source=vision：CompanionMemory 不传 topic，EventStream 再剥离兜底。"""
    stream = _stream(tmp_path)
    mem = _mem(tmp_path, stream)
    mem.record_event(category="browsing", topic="机密文档标题", source="vision")
    ev = stream.read_all()[0]
    assert ev["source"] == "vision"
    assert "topic" not in ev, "vision 事件不应落 topic 文本"
    assert ev["category"] == "browsing"


def test_event_stream_strips_vision_private_keys_direct(tmp_path):
    """直接 append source=vision 且带 summary/detail/text → 全部剥离。"""
    stream = _stream(tmp_path)
    stream.append({
        "category": "browsing", "topic": "主题", "summary": "视觉摘要",
        "detail": "细节", "text": "原文", "source": "vision",
    })
    ev = stream.read_all()[0]
    for key in ("topic", "summary", "detail", "text"):
        assert key not in ev, f"vision 事件不应含 {key}"


def test_non_vision_event_keeps_topic(tmp_path):
    """非 vision 事件保留 topic（截断后）。"""
    stream = _stream(tmp_path)
    stream.append({"category": "development", "topic": "正常话题", "source": "topic"})
    ev = stream.read_all()[0]
    assert ev["topic"] == "正常话题"


# ── 3. 旧文件兼容 ─────────────────────────────────────────────────────


def test_old_file_compat_loads_with_defaults(tmp_path):
    """旧文件（无新字段）读取自动补默认值，不崩。"""
    stream = _stream(tmp_path)
    # 手写一行旧格式（只有 category/ts）
    stream._dir.mkdir(parents=True, exist_ok=True)
    with stream.path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 1755417600.0, "category": "development"},
                           ensure_ascii=False) + "\n")
    events = stream.read_all()
    assert len(events) == 1
    ev = events[0]
    assert ev["category"] == "development"
    assert ev["scenario"] == DEFAULT_SCENARIO
    assert ev["emotion"] == DEFAULT_EMOTION
    assert ev["intensity"] == DEFAULT_INTENSITY
    assert "source" in ev


def test_corrupt_line_skipped_others_survive(tmp_path):
    """坏行跳过，其余行正常读取（行级损坏隔离）。"""
    stream = _stream(tmp_path)
    stream._dir.mkdir(parents=True, exist_ok=True)
    with stream.path.open("a", encoding="utf-8") as f:
        f.write("{broken json}\n")
        f.write(json.dumps({"ts": 1755417600.0, "category": "gaming"},
                           ensure_ascii=False) + "\n")
    events = stream.read_all()
    assert len(events) == 1
    assert events[0]["category"] == "gaming"


# ── 4. 截断 / 强度夹取 ───────────────────────────────────────────────


def test_topic_truncated_to_max_chars(tmp_path):
    long_topic = "长" * (TOPIC_MAX_CHARS + 20)
    stream = _stream(tmp_path)
    stream.append({"category": "development", "topic": long_topic, "source": "topic"})
    ev = stream.read_all()[0]
    assert len(ev["topic"]) == TOPIC_MAX_CHARS


def test_intensity_clamped(tmp_path):
    stream = _stream(tmp_path)
    stream.append({"category": "gaming", "intensity": 1.5, "source": "foreground"})
    stream.append({"category": "gaming", "intensity": -0.5, "source": "foreground"})
    stream.append({"category": "gaming", "intensity": "oops", "source": "foreground"})
    events = stream.read_all()
    assert events[0]["intensity"] == 1.0
    assert events[1]["intensity"] == 0.0
    assert events[2]["intensity"] == 0.0


def test_normalize_record_is_pure(tmp_path):
    """normalize_record 不修改入参（纯函数）。"""
    stream = _stream(tmp_path)
    raw = {"category": "gaming", "source": "foreground"}
    stream.normalize_record(raw)
    assert "scenario" not in raw, "归一不应修改原 dict"
    assert "emotion" not in raw
