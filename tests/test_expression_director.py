"""表达决策层测试 —— 覆盖快照、schema、回退闸、端到端决策。

设计原则：**不依赖本地引擎在线**。引擎不可用时全部走兜底路径，
所以 CI / 无 GPU 环境下这套测试必须全绿。

⚠️ 2026-09-20 补：快照测试还隐含依赖 **Live2D 模型文件**
（`characters/*/live2d/*.model3.json`）。该文件因版权不随仓库分发
（见 .gitignore），CI 的 checkout 没有它 —— 不加保护就会本地绿、CI 红。
故加 `_require_model()` 前置：缺模型则 skip。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.capability_snapshot import (  # noqa: E402
    MAX_CHOICES_PER_EMOTION,
    PRESET_EMOTIONS,
    PRESET_LABELS,
    build_snapshot,
    preset_label,
    presets_for_emotion,
)
from core.expression_director import (  # noqa: E402
    EMOTION_CHOICES,
    ExpressionDirector,
    INTENSITY_CHOICES,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _require_model(character_id: str) -> None:
    """确保该角色有真实 Live2D 模型文件，否则 skip。

    模型文件在 .gitignore 里（版权原因），CI 上没有。
    本地开发机有模型时会真跑。
    """
    char_dir = os.path.join(_ROOT, "characters", character_id)
    if not os.path.isdir(char_dir):
        pytest.skip(f"角色目录不存在（CI 上正常）: characters/{character_id}")
    for pattern in ("live2d/*.model3.json", "*.model3.json",
                    "live2d/*.model.json", "*.model.json"):
        import glob
        if glob.glob(os.path.join(char_dir, pattern)):
            return
    pytest.skip(
        f"characters/{character_id} 缺 Live2D 模型文件（.gitignore 排除，"
        f"CI 上正常）——本测试需要真模型"
    )


# ── 测试替身 ──────────────────────────────────────────────


class FakeClient:
    """假引擎客户端：可注入任意返回，用于验证决策逻辑本身。"""

    def __init__(self, response=None, available=True):
        self.response = response
        self._available = available
        self.calls: list[tuple] = []

    def is_available(self, ttl: float = 30.0) -> bool:
        return self._available

    def decide(self, context, schema, temperature=0.2):
        self.calls.append((context, schema))
        return self.response


# ── 快照 ──────────────────────────────────────────────────


class TestSnapshot:
    """快照测试。

    ⚠️ 2026-09-20：以下三个测试需要**真实的 Live2D 模型文件**
    （`characters/*/live2d/*.model3.json`），而它在 `.gitignore` 里
    （版权原因不随仓库分发）。CI 的 checkout 没有它 —— 所以统一加
    `_require_miku_model()` / `_require_model()` 前置：缺模型就 skip，
    不假红。

    本地有模型的开发机仍会真跑这三条。
    """

    def test_miku_has_motions_and_expressions(self):
        _require_model("miku")
        s = build_snapshot("miku")
        assert len(s.motions) >= 7
        assert "waving" in s.motion_names
        assert s.expression_names, "miku 有 8 个表情文件（水印类已过滤）"

    def test_model_without_motions_degrades_gracefully(self):
        """Rory 模型没有 Motions/Expressions 字段——不能崩，要出 warning。"""
        _require_model("Rory")
        s = build_snapshot("Rory")
        assert s.motions == []
        assert s.expressions == []
        assert any("motion" in w for w in s.warnings)
        # 关键：预设是纯参数驱动，不依赖 motion 文件 → 必须仍然可用
        assert len(s.presets) >= 50

    def test_unknown_character_returns_snapshot_with_warning(self):
        s = build_snapshot("__no_such_character__")
        assert s.motions == []
        assert s.warnings
        assert len(s.presets) >= 50, "预设表与角色无关，必须仍能读到"

    def test_presets_parsed_from_python_source(self):
        s = build_snapshot("miku")
        names = s.preset_names
        for expect in ("blush_shy", "smile_bright", "angry_glare", "sad_droop"):
            assert expect in names, f"预设 {expect} 未解析出来"

    def test_preset_duration_computed(self):
        s = build_snapshot("miku")
        p = next(p for p in s.presets if p.name == "blush_shy")
        assert p.duration > 0

    def test_param_ranges_measured_from_model(self):
        """参数范围必须来自模型自己的曲线（cdi3 没有 min/max）。"""
        _require_model("miku")
        s = build_snapshot("miku")
        assert s.param_ranges, "应从 motion/expression 曲线量出参数范围"
        lo, hi = s.param_range("ParamMouthForm")
        assert lo <= hi

    def test_all_labels_are_chinese_or_ascii(self):
        """标签必须是引擎能分辨的中文语义名（英文裸名实测分辨力不足）。"""
        s = build_snapshot("miku")
        for label in s.preset_labels:
            assert label and not label.startswith("_")
        # 收录率：至少一半预设要有中文标签
        covered = sum(1 for p in s.presets if p.name in PRESET_LABELS)
        assert covered >= len(s.presets) // 2

    def test_preset_by_label_roundtrip(self):
        s = build_snapshot("miku")
        for name in ("blush_shy", "smile_bright"):
            p = next(x for x in s.presets if x.name == name)
            assert s.preset_by_label(p.label) is p
            assert s.preset_by_label(name) is p, "原名也应能查到"


# ── 情绪分组 ──────────────────────────────────────────────


class TestEmotionGrouping:
    def test_grouping_shrinks_candidates(self):
        """两阶段决策的核心：按情绪筛候选，把 53 个缩到十来个。"""
        s = build_snapshot("miku")
        for emo in EMOTION_CHOICES:
            group = presets_for_emotion(s, emo)
            assert group, f"情绪 {emo} 没有候选"
            assert len(group) <= MAX_CHOICES_PER_EMOTION

    def test_unmapped_emotion_falls_back_to_all(self):
        """未收录的情绪必须回退到全量，不能返回空（否则调用方无候选可用）。"""
        s = build_snapshot("miku")
        group = presets_for_emotion(s, "__不存在的情绪__")
        assert group

    def test_every_emotion_choice_has_coverage(self):
        """EMOTION_CHOICES 里的每个情绪都应至少有一个专属预设。"""
        covered = {e for emos in PRESET_EMOTIONS.values() for e in emos}
        for emo in EMOTION_CHOICES:
            assert emo in covered, f"情绪 {emo} 在 PRESET_EMOTIONS 里没有对应预设"

    def test_generic_presets_not_in_emotional_groups(self):
        """通用项（连续眨眼/坐下）不该混进情绪组，否则稀释表现力（实测踩过）。"""
        for generic in ("blink3", "blink_quick", "sit"):
            emos = PRESET_EMOTIONS.get(generic, ())
            assert emos == ("平静",), f"{generic} 只应属于平静组，实际 {emos}"


# ── schema ────────────────────────────────────────────────


class TestSchema:
    def test_choices_are_in_description_first_line(self):
        """硬约束：引擎只取 description 第一行，选项必须写在第一行。"""
        d = ExpressionDirector("miku", client=FakeClient())
        schema = d.build_schema("开心")
        for field, spec in schema.items():
            assert "\n" not in spec["description"], f"{field} 描述含换行，选项会被截掉"
            for choice in spec["choices"]:
                assert choice in spec["description"], \
                    f"{field} 的选项 {choice!r} 不在描述第一行里"

    def test_schema_narrows_by_emotion(self):
        d = ExpressionDirector("miku", client=FakeClient())
        happy = d.build_schema("开心")["preset"]["choices"]
        sleepy = d.build_schema("困")["preset"]["choices"]
        assert happy != sleepy, "不同情绪应给出不同候选"

    def test_schema_respects_engine_cardinality_limit(self):
        """引擎单字段上限 255。"""
        d = ExpressionDirector("miku", client=FakeClient())
        for emo in EMOTION_CHOICES:
            assert len(d.build_schema(emo)["preset"]["choices"]) <= 255

    def test_intensity_field_present(self):
        d = ExpressionDirector("miku", client=FakeClient())
        assert d.build_schema("开心")["intensity"]["choices"] == list(INTENSITY_CHOICES)


# ── 决策 + 回退闸 ─────────────────────────────────────────


class TestDecide:
    def _director(self, response, available=True):
        return ExpressionDirector("miku", client=FakeClient(response, available))

    def test_high_confidence_accepted(self):
        d = self._director({
            "emotion": {"value": "开心", "prob": 0.99},
            "intensity": {"value": "强", "prob": 0.99},
            "preset": {"value": "灿烂笑容", "prob": 0.95},
        })
        r = d.decide("开心", "强", "被夸奖")
        assert r.accepted
        assert r.gesture == "smile_bright", "中文标签应被翻译回预设名"
        assert r.scale == 1.0

    def test_low_confidence_falls_back(self):
        """低置信 → 不采纳引擎结果，走兜底（宁可不动，不能乱动）。"""
        d = self._director({
            "emotion": {"value": "开心", "prob": 0.99},
            "intensity": {"value": "强", "prob": 0.99},
            "preset": {"value": "灿烂笑容", "prob": 0.20},
        })
        r = d.decide("开心", "强", "")
        assert r.source == "fallback"
        assert r.gesture == "smile_bright", "兜底也要给情绪一致的表情"

    def test_engine_offline_falls_back(self):
        d = self._director(None, available=False)
        r = d.decide("开心", "强", "")
        assert r.source == "fallback"
        assert r.accepted

    def test_engine_returns_none_falls_back(self):
        d = self._director(None, available=True)
        r = d.decide("失落", "中", "")
        assert r.source == "fallback"
        assert r.gesture == "sad_droop"

    def test_disabled_director_falls_back(self):
        d = self._director(None)
        d.set_enabled(False)
        r = d.decide("害羞", "强", "")
        assert r.source == "fallback"
        assert r.gesture == "blush_shy"

    def test_unknown_emotion_never_crashes(self):
        d = self._director(None, available=False)
        r = d.decide("__没有这个情绪__", "中", "")
        assert not r.accepted
        assert r.source == "none"

    def test_context_is_structured_not_raw_sentence(self):
        """硬约束：喂给引擎的必须是结构化状态，不是原始句子。"""
        client = FakeClient({"preset": {"value": "灿烂笑容", "prob": 0.9}})
        d = ExpressionDirector("miku", client=client)
        d.decide("开心", "强", "被夸奖", extra={"screen": "在看代码"})
        assert client.calls, "应调用过引擎"
        context = client.calls[0][0]
        assert "emotion=开心" in context
        assert "intensity=强" in context
        assert "cause=被夸奖" in context
        assert "screen=在看代码" in context

    def test_long_cause_is_truncated(self):
        """长文本会让引擎自信判错（实测）——必须截断。"""
        client = FakeClient({"preset": {"value": "思考", "prob": 0.9}})
        d = ExpressionDirector("miku", client=client)
        d.decide("思考", "轻", "x" * 500)
        assert len(client.calls[0][0]) < 200

    def test_scale_follows_intensity(self):
        client = FakeClient({
            "emotion": {"value": "开心", "prob": 0.9},
            "intensity": {"value": "轻", "prob": 0.9},
            "preset": {"value": "灿烂笑容", "prob": 0.9},
        })
        d = ExpressionDirector("miku", client=client)
        r = d.decide("开心", "轻", "")
        assert r.scale == 0.5

    def test_stats_tracked(self):
        d = self._director(None, available=False)
        d.decide("开心", "强", "")
        d.decide("失落", "中", "")
        assert d.stats["total"] == 2
        assert d.stats["fallback"] == 2

    def test_result_has_readable_line(self):
        d = self._director(None, available=False)
        r = d.decide("开心", "强", "")
        assert "开心" in r.as_line()

    def test_status_shape(self):
        d = self._director(None, available=False)
        st = d.status()
        for key in ("enabled", "engine_available", "presets", "stats", "accept_rate"):
            assert key in st


# ── 兜底表完整性 ──────────────────────────────────────────


class TestFallbackTable:
    def test_every_emotion_has_fallback(self):
        """兜底表必须覆盖所有情绪——否则引擎挂掉时某些情绪会彻底没反应。"""
        from core.expression_director import _FALLBACK_PRESET

        for emo in EMOTION_CHOICES:
            assert emo in _FALLBACK_PRESET, f"情绪 {emo} 没有兜底预设"

    def test_fallback_presets_actually_exist(self):
        """兜底表里写的预设名必须真实存在，否则兜底也是空的。"""
        from core.expression_director import _FALLBACK_PRESET

        s = build_snapshot("miku")
        names = set(s.preset_names)
        for emo, preset in _FALLBACK_PRESET.items():
            assert preset in names, f"兜底预设 {preset}（情绪 {emo}）不存在"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
