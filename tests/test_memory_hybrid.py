# -*- coding: utf-8 -*-
"""P0-3 BM25 混合检索单元测试（验收标准见 docs/migration-neko-port-plan.md）

验收覆盖：
1. 构造 5 条中文场景，检索"深夜加班"经关键词路径命中 `late_night_work`
2. 语义近但关键词不同的场景经 RRF 提升（embedding mock 注入）
3. 无 embedding 时退化为 BM25-only 不报错（fallback gate）
4. 繁简折叠：繁体 query 命中简体 doc（script_fold）
5. tokenize 保留词频（BM25 TF 信号）；rrf_fuse 按 id 去重
6. SceneMemory.find_matching 混合路径 / 回退精确路径
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.memory_hybrid import (
    HybridMemoryRecall,
    NoopEmbeddingProvider,
    bm25_rank,
    rrf_fuse,
)
from core.memory_keywords import extract_keywords, fold_script, tokenize

# ── 5 条中文场景（验收标准用例）──────────────────────────────────────────

DOCS = [
    {"id": "late_night_work", "text": "深夜加班 写代码 重构事件流模块 深夜 工作"},
    {"id": "gaming_weekend", "text": "周末打游戏 娱乐 深夜 游戏 联机"},
    {"id": "video_watching", "text": "看视频 追剧 娱乐 周末"},
    {"id": "tutorial_follow", "text": "学习教程 学新框架 看文档 学习"},
    {"id": "chat_idle", "text": "安静发呆 聊天 沟通 摸鱼"},
]


def test_bm25_hits_late_night_work_by_keyword():
    """检索"深夜加班"经关键词路径命中 late_night_work（验收 1）。"""
    recall = HybridMemoryRecall(hybrid_enabled=True)
    hits = recall.recall("深夜加班", DOCS)
    assert hits, "混合召回不应为空"
    assert hits[0]["id"] == "late_night_work", (
        f"首个命中应为 late_night_work，实际 {hits[0]['id']}"
    )
    assert hits[0].get("_rrf_score", 0.0) > 0.0


def test_keyword_path_requires_no_embedding():
    """关键词路径不依赖 embedding（无 provider 也能命中）。"""
    hits = HybridMemoryRecall(hybrid_enabled=True).recall("深夜加班", DOCS)
    assert hits[0]["id"] == "late_night_work"


def test_bm25_threshold_filters_zero_overlap():
    """无词重叠的 doc 不会出现在结果里（score>0 过滤）。"""
    recall = HybridMemoryRecall(hybrid_enabled=True)
    hits = recall.recall("深夜加班", DOCS)
    ids = {h["id"] for h in hits}
    # chat_idle 与"深夜加班"无任何 token 重叠 → 不应命中
    assert "chat_idle" not in ids


# ── RRF 提升（验收 2）──────────────────────────────────────────────────


class MockEmbeddingProvider:
    """可编程 embedding mock：把指定文本映射到目标向量（1-hot 风格）。

    query 与 deadline_crunch 的 doc 文本映射到同一向量 → cosine=1.0；
    其余映射到正交向量 → cosine=0.0。
    """

    def __init__(self, query_vec: list[float], target_text: str, target_vec: list[float],
                 other_vec: list[float], available: bool = True):
        self._query_vec = query_vec
        self._target_text = target_text
        self._target_vec = target_vec
        self._other_vec = other_vec
        self._available = available

    def is_available(self) -> bool:
        return self._available

    def embed_texts(self, texts: list[str]) -> list[list[float] | None]:
        out = []
        for t in texts:
            if t == "深夜加班":
                out.append(list(self._query_vec))
            elif t == self._target_text:
                out.append(list(self._target_vec))
            else:
                out.append(list(self._other_vec))
        return out


def _build_provider() -> MockEmbeddingProvider:
    """构造：query「深夜加班」语义最近的是「项目交付冲刺 凌晨两点睡 压力很大」。"""
    vec_a = [1.0, 0.0, 0.0, 0.0, 0.0]
    vec_b = [0.0, 1.0, 0.0, 0.0, 0.0]
    return MockEmbeddingProvider(
        query_vec=vec_a,
        target_text="项目交付冲刺 凌晨两点睡 压力很大",
        target_vec=vec_a,
        other_vec=vec_b,
    )


def test_rrf_lifts_semantic_near_keyword_different():
    """语义近但关键词不同的场景经 RRF 提升（验收 2）。

    构造：deadline_crunch 与"深夜加班"零关键词重叠（BM25 命中不了），
    但 embedding 给出高余弦 → RRF 把它提升进结果。
    """
    semantic_docs = DOCS + [
        {"id": "deadline_crunch", "text": "项目交付冲刺 凌晨两点睡 压力很大"},
    ]
    provider = _build_provider()

    # 无 embedding：纯 BM25 → deadline_crunch 不在结果里
    bm25_only = HybridMemoryRecall(hybrid_enabled=True, embedding_provider=None)
    ids_without_emb = {h["id"] for h in bm25_only.recall("深夜加班", semantic_docs)}
    assert "deadline_crunch" not in ids_without_emb, "零词重叠 doc 不应被 BM25 命中"

    # 有 embedding：RRF 融合 → deadline_crunch 被提升进结果
    hybrid = HybridMemoryRecall(
        hybrid_enabled=True, embedding_provider=provider,
        budget_total=10,
    )
    hits = hybrid.recall("深夜加班", semantic_docs)
    ids_with_emb = {h["id"] for h in hits}
    assert "deadline_crunch" in ids_with_emb, "RRF 应把语义近场景提升进结果"
    d = next(h for h in hits if h["id"] == "deadline_crunch")
    assert d.get("_rrf_score", 0.0) > 0.0, "RRF 得分应为正"


def test_hybrid_still_ranks_keyword_hit_first():
    """混合模式下关键词命中仍应领先（BM25 强信号不被语义淹没）。"""
    semantic_docs = DOCS + [
        {"id": "deadline_crunch", "text": "项目交付冲刺 凌晨两点睡 压力很大"},
    ]
    hybrid = HybridMemoryRecall(
        hybrid_enabled=True, embedding_provider=_build_provider(),
        budget_total=10,
    )
    hits = hybrid.recall("深夜加班", semantic_docs)
    assert hits[0]["id"] == "late_night_work"


# ── fallback gate（验收 3）─────────────────────────────────────────────


def test_no_embedding_falls_back_to_bm25_only():
    """无 embedding 时退化为 BM25-only 不报错（验收 3）。"""
    recall = HybridMemoryRecall(hybrid_enabled=True)  # 默认 NoopEmbeddingProvider
    assert recall.embedding_available is False
    hits = recall.recall("深夜加班", DOCS)
    assert hits, "无 embedding 也应正常返回 BM25 结果"
    assert hits[0]["id"] == "late_night_work"


def test_cosine_failure_falls_back_gracefully():
    """cosine 路径异常不应把 BM25 结果一起埋了（fallback gate 纵深）。"""

    class ExplodingProvider:
        def is_available(self) -> bool:
            return True

        def embed_texts(self, texts):
            raise RuntimeError("model crashed")

    recall = HybridMemoryRecall(
        hybrid_enabled=True, embedding_provider=ExplodingProvider(),
    )
    hits = recall.recall("深夜加班", DOCS)
    assert hits, "cosine 异常应降级为 BM25 结果，而非空召回"
    assert hits[0]["id"] == "late_night_work"


def test_disabled_returns_empty():
    """hybrid_enabled=False → recall 返回空（保守零行为）。"""
    recall = HybridMemoryRecall(hybrid_enabled=False)
    assert recall.recall("深夜加班", DOCS) == []


def test_empty_query_returns_empty():
    recall = HybridMemoryRecall(hybrid_enabled=True)
    assert recall.recall("", DOCS) == []
    assert recall.recall("   ", DOCS) == []


# ── 繁简折叠（script_fold）────────────────────────────────────────────


def test_traditional_query_hits_simplified_doc():
    """繁体 query「機器學習」命中简体 doc「机器学习」（繁简折叠对称）。"""
    assert fold_script("機器學習") == "机器学习"
    docs = [
        {"id": "ml", "text": "机器学习 研究 论文 模型"},
        {"id": "other", "text": "看视频 追剧 娱乐"},
    ]
    hits = HybridMemoryRecall(hybrid_enabled=True).recall("機器學習", docs)
    assert hits and hits[0]["id"] == "ml", "繁体 query 应命中简体 doc"


def test_fold_identity_on_simplified():
    assert fold_script("软件开发") == "软件开发"
    assert fold_script("") == ""


# ── 分词语义 ──────────────────────────────────────────────────────────


def test_tokenize_keeps_multiplicity_for_tf():
    """tokenize 保留词频（list 语义）："博士" 重复多次应得分更高。"""
    q = "博士"
    high = {"id": "high", "text": "博士 博士 博士 研究 论文"}
    low = {"id": "low", "text": "博士 研究 论文"}
    ranked = bm25_rank(q, [high, low])
    by_id = {d["id"]: s for d, s in ranked}
    assert by_id["high"] > by_id["low"], "TF 信号应让重复词 doc 得分更高"


def test_extract_keywords_set_semantics():
    """extract_keywords 是 set 语义（去重），用于 mention/anti_repeat。"""
    kw = extract_keywords("深夜加班写代码")
    assert "深夜" in kw and "加班" in kw and "写代" in kw


def test_rrf_fuse_dedup_by_id():
    """同一 id 同时出现在两路 → 合并为一条，得分累加。"""
    doc_a = {"id": "a", "text": "x"}
    doc_b = {"id": "b", "text": "y"}
    fused = rrf_fuse(
        [(doc_a, 1.0), (doc_b, 0.5)],
        [(doc_a, 0.9)],
        k=60, budget_total=10,
    )
    ids = [d["id"] for d in fused]
    assert ids == ["a", "b"], f"应去重为 2 条，实际 {ids}"
    score_a = next(d["_rrf_score"] for d in fused if d["id"] == "a")
    assert abs(score_a - (1 / 61 + 1 / 61)) < 1e-9, "两侧得分应累加"


# ── SceneMemory.find_matching 混合路径 ────────────────────────────────


def _make_scene(scene_id, label, category, scenario, tags, last_ts):
    from core.perception.scene_cluster import Scene
    return Scene(
        scene_id=scene_id, label=label, category=category, scenario=scenario,
        tags=tags, first_ts=last_ts - 3600, last_ts=last_ts, count=3,
        duration_min=30.0, emotion_summary="neutral", topics=[],
    )


def test_find_matching_hybrid_path_hits_late_night_work():
    """SceneMemory.find_matching 混合路径：中文 label + scenario 键命中。"""
    from core.scene_memory import SceneMemory
    old_ts = time.time() - 86400  # 昨天（避免"进行中"排除）
    scenes = [
        _make_scene("work|late_night_work|2026-08-01", "深夜加班", "work",
                    "late_night_work", ["work", "深夜", "晚上"], old_ts),
        _make_scene("ent|gaming_weekend|2026-08-02", "打游戏", "entertainment",
                    "gaming_weekend", ["entertainment", "周末"], old_ts),
        _make_scene("ent|video_watching|2026-08-03", "看视频", "entertainment",
                    "video_watching", ["entertainment", "周末"], old_ts),
        _make_scene("dev|tutorial_follow|2026-08-04", "学习教程", "development",
                    "tutorial_follow", ["development", "学习"], old_ts),
        _make_scene("comm|chat_idle|2026-08-05", "安静发呆", "communication",
                    "chat_idle", ["communication"], old_ts),
    ]
    sm = SceneMemory("t3-test", memory_dir=Path(__file__).resolve().parent / "_tmp_mem")
    sm.set_hybrid_recall(HybridMemoryRecall(hybrid_enabled=True))
    # 直接注入场景（避免 rebuild 时间窗口干扰）
    sm._scenes = scenes
    results = sm.find_matching(category="work", scenario="late_night_work",
                               tags=["work", "深夜", "晚上"], max_results=3)
    assert results, "应检索到至少一条场景"
    assert results[0].scene_id.startswith("work|late_night_work"), (
        f"首个命中应为 late_night_work 场景，实际 {results[0].scene_id}"
    )


def test_find_matching_exact_fallback_when_hybrid_disabled():
    """hybrid 关闭 → 回退旧精确标签匹配（行为不变）。"""
    from core.scene_memory import SceneMemory
    old_ts = time.time() - 86400
    scenes = [
        _make_scene("work|late_night_work|2026-08-01", "深夜加班", "work",
                    "late_night_work", ["work", "深夜"], old_ts),
        _make_scene("ent|gaming_weekend|2026-08-02", "打游戏", "entertainment",
                    "gaming_weekend", ["entertainment"], old_ts),
    ]
    sm = SceneMemory("t3-test2", memory_dir=Path(__file__).resolve().parent / "_tmp_mem")
    sm.set_hybrid_recall(HybridMemoryRecall(hybrid_enabled=False))
    sm._scenes = scenes
    results = sm.find_matching(category="work", scenario="late_night_work",
                               tags=["work"], max_results=3)
    assert results and results[0].scene_id.startswith("work|late_night_work")


def test_find_matching_skips_in_progress_scene():
    """进行中场景（5 分钟内）不参与回忆（两条路径共用过滤）。"""
    from core.scene_memory import SceneMemory
    now = time.time()
    scenes = [
        _make_scene("work|late_night_work|2026-08-01", "深夜加班", "work",
                    "late_night_work", ["work", "深夜"], now - 60),  # 进行中
    ]
    sm = SceneMemory("t3-test3", memory_dir=Path(__file__).resolve().parent / "_tmp_mem")
    sm.set_hybrid_recall(HybridMemoryRecall(hybrid_enabled=True))
    sm._scenes = scenes
    assert sm.find_matching(category="work", scenario="late_night_work",
                            tags=["work"], max_results=3) == []
