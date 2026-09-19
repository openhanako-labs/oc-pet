# -*- coding: utf-8 -*-
# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""混合记忆召回 — BM25 + cosine(接口) + RRF 融合（移植自 N.E.K.O.
``memory/hybrid_recall.py`` 纯算法部分，保留 Apache 头）。

与 N.E.K.O. 的差异（oc-pet 重写适配）
========================================
- **同步**：oc-pet 是 PySide6 单体 threading 架构，无 asyncio；BM25/cosine
  都是纯计算，直接同步调用即可（P1-1 若引入 ONNX embedding，嵌入批处理
  仍为同步接口，耗时侧由调用方决定是否放后台线程）。
- **不依赖文件池加载**：N.E.K.O. 的 pool loader（facts/reflections/archive）
  对应 oc-pet 的场景/事件/事实内存列表；本模块只负责"给定 doc 池打分"，
  数据装配由 ``SceneMemory`` 等调用方完成。
- **cosine 端留接口（P0）→ P1-1 填实现**：``EmbeddingProvider`` 协议 + 无 embedding 时
  ``_cosine_rank`` 返回 ``[]``（fallback gate）——config ``memory.embedding.enabled=false``
  时自动退化为纯 BM25，不报错；P1-1 的 ``core.memory_embedding.EmbeddingService`` 实现该
  协议，开启且模型就绪时走「BM25+cosine+RRF」。

Pipeline（与上游一致）
========================
1. BM25 path — ``tokenize``（2/3-gram for CJK，whitespace split for Latin，
   繁简折叠对称处理）+ 标准 Okapi BM25，阈值过滤，取 top ``budget_each``。
2. Cosine path — 通过 ``EmbeddingProvider`` 嵌入 query/doc，余弦相似度，
   阈值过滤，取 top ``budget_each``；provider 不可用/异常 → 空列表。
3. RRF 融合 — ``RRF(d) = Σᵢ 1/(k + rankᵢ(d))``（k 默认 60），按 id 去重
   合并，降序，cap 在 ``budget_total``。

打分行为说明
============
- doc 必须带 ``id``（RRF 去重键）与 ``text``（BM25 语料），缺一 skip。
- 返回的 doc 是浅拷贝副本（不污染调用方缓存），并附加 ``_rrf_score``。
"""
from __future__ import annotations

import math
import logging
from typing import Any, Callable, Protocol

from .memory_keywords import tokenize

logger = logging.getLogger(__name__)

# Okapi BM25 默认参数（检索用经典 1.5/0.75，与 anti_repeat 的低 k1 不同）
_BM25_K1 = 1.5
_BM25_B = 0.75

# 默认检索预算（可被构造参数覆盖）
DEFAULT_BM25_THRESHOLD = 0.0    # BM25 最低得分（上游默认 0：无词重叠即淘汰）
DEFAULT_COSINE_THRESHOLD = 0.0  # cosine 最低相似度
DEFAULT_BUDGET_EACH = 10        # 每路（BM25 / cosine）各取 top N
DEFAULT_BUDGET_TOTAL = 8        # 融合后返回条数上限
DEFAULT_RRF_K = 60              # RRF 融合常数


# ── embedding provider 协议（P1-1 实现）──────────────────────────────


class EmbeddingProvider(Protocol):
    """向量嵌入提供者协议（P0 只留接口，P1-1 填实现）。

    - ``is_available()``：服务就绪（模型已加载/无禁用原因）→ True
    - ``embed_texts(texts)``：批量嵌入，返回与输入等长的向量列表；
      单条失败以 None 占位。实现方异常应自行捕获并降级，不抛给调用方。
    """

    def is_available(self) -> bool:
        """嵌入服务当前是否可用。"""
        ...

    def embed_texts(self, texts: list[str]) -> list[list[float] | None]:
        """批量嵌入；返回与输入等长的 ``list[list[float] | None]``。"""
        ...


class NoopEmbeddingProvider:
    """空实现：永远不可用（P0 默认）。等价于"无 embedding → 纯 BM25"。"""

    def is_available(self) -> bool:
        return False

    def embed_texts(self, texts: list[str]) -> list[list[float] | None]:
        return [None] * len(texts)


# ── 余弦相似度（零依赖实现，numpy 可选加速）────────────────────────────


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """两个等长向量的余弦相似度；零向量/维度不一致返回 0.0。

    纯 Python 实现（numpy 不是 memory 层的硬依赖；P1-1 若引入 numpy，
    可在此处无缝替换为向量化版本）。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    try:
        dot = 0.0
        norm_a = 0.0
        norm_b = 0.0
        for x, y in zip(a, b):
            fx = float(x)
            fy = float(y)
            dot += fx * fy
            norm_a += fx * fx
            norm_b += fy * fy
        if norm_a <= 0.0 or norm_b <= 0.0:
            return 0.0
        return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
    except Exception:
        return 0.0


# ── BM25 检索（verbatim from hybrid_recall._bm25_rank）────────────────


def bm25_rank(
    query: str,
    pool: list[dict],
    *,
    stop_names: list[str] | None = None,
    k1: float = _BM25_K1,
    b: float = _BM25_B,
) -> list[tuple[dict, float]]:
    """标准 Okapi BM25 — 对 ``pool`` 中每条 doc 按 query 打分。

    Returns:
        ``[(doc, score)]`` 按得分降序；零分 doc 剔除（无词重叠）。
        空 query / 空池 / 全零 → ``[]``。
    """
    if not query or not pool:
        return []
    query_terms = tokenize(query, stop_names)
    if not query_terms:
        return []

    # 全部 doc 一次性分词；同一份结果同时供 DF/TF 使用
    doc_terms_list: list[list[str]] = [
        tokenize(d.get('text', '') or '', stop_names) for d in pool
    ]

    n_docs = len(pool)
    total_len = sum(len(t) for t in doc_terms_list)
    if total_len == 0:
        return []
    avgdl = total_len / n_docs

    query_unique = set(query_terms)

    # DF + 每 doc 的 TF，只累计 query 词（#2550：全词表 df/tf 表在大语料下
    # 是 99.97% 查不到的死数据，单趟扫描只保留 query 词即可，得分逐位不变）
    df: dict[str, int] = dict.fromkeys(query_unique, 0)
    doc_tf_list: list[dict[str, int]] = []
    for terms in doc_terms_list:
        tf_map: dict[str, int] = {}
        for t in terms:
            if t in query_unique:
                tf_map[t] = tf_map.get(t, 0) + 1
        doc_tf_list.append(tf_map)
        for t in tf_map:
            df[t] += 1

    scored: list[tuple[dict, float]] = []
    for doc, doc_terms, doc_tf in zip(pool, doc_terms_list, doc_tf_list):
        if not doc_terms:
            continue
        dl = len(doc_terms)
        norm = 1.0 - b + b * dl / avgdl
        score = 0.0
        for q_term in query_unique:
            n = df.get(q_term, 0)
            if n <= 0:
                continue
            # Robertson-Sparck-Jones IDF with +0.5 smoothing
            idf = math.log((n_docs - n + 0.5) / (n + 0.5) + 1.0)
            if idf <= 0:
                continue
            tf = doc_tf.get(q_term, 0)
            if tf == 0:
                continue
            score += idf * (tf * (k1 + 1)) / (tf + k1 * norm)
        if score > 0:
            scored.append((doc, score))

    scored.sort(key=lambda p: p[1], reverse=True)
    return scored


# ── cosine 检索（接口端；无 embedding 时返回 []）──────────────────────


def cosine_rank(
    query: str,
    pool: list[dict],
    provider: EmbeddingProvider | None,
) -> list[tuple[dict, float]]:
    """嵌入 query，与每条带有效向量的 doc 算余弦。返回 ``[(doc, cosine)]`` 降序。

    跳过无/坏向量的 doc；下列情况返回 ``[]``（fallback gate，不抛异常）：
    - provider 为 None / 不可用
    - 空 query / 空池
    - query 嵌入失败
    - cosine 路径任意异常（不把已算出的 BM25 结果一起埋了）

    P1-1 向量化：优先用 doc 里缓存的 ``embedding``（list/tuple），缺失的 doc
    文本**批量**现嵌（单次 ``embed_texts`` 调用，避免逐条 submit 到推理线程池
    的往返开销）。
    """
    if not query or not pool or provider is None:
        return []
    try:
        if not provider.is_available():
            return []
        query_vectors = provider.embed_texts([query])
        if not query_vectors or not query_vectors[0]:
            return []
        qvec = query_vectors[0]

        # 缓存向量 + 缺失批量现嵌（保持与输入池逐位对齐）
        cached: list = [doc.get('embedding') for doc in pool]
        missing_idx = [
            i for i, v in enumerate(cached)
            if not isinstance(v, (list, tuple)) or len(v) == 0
        ]
        if missing_idx:
            missing_texts = [(pool[i].get('text', '') or '') for i in missing_idx]
            embedded = provider.embed_texts(missing_texts)
            for pos, i in enumerate(missing_idx):
                vec = embedded[pos] if pos < len(embedded) else None
                if isinstance(vec, (list, tuple)) and len(vec) > 0:
                    cached[i] = vec

        scored: list[tuple[dict, float]] = []
        for doc, cvec in zip(pool, cached):
            if not isinstance(cvec, (list, tuple)) or len(cvec) == 0:
                continue
            cos = cosine_similarity(list(qvec), list(cvec))
            if cos > 0:
                scored.append((doc, cos))

        scored.sort(key=lambda p: p[1], reverse=True)
        return scored
    except Exception as exc:
        logger.warning("[memory_hybrid] cosine 路径失败，退化为 BM25-only: %s: %s",
                       type(exc).__name__, exc)
        return []


# ── RRF 融合（verbatim from hybrid_recall._rrf_fuse）─────────────────


def rrf_fuse(
    bm25_ranking: list[tuple[dict, float]],
    cosine_ranking: list[tuple[dict, float]],
    *,
    k: int = DEFAULT_RRF_K,
    budget_total: int = DEFAULT_BUDGET_TOTAL,
) -> list[dict]:
    """Reciprocal Rank Fusion:

        RRF(d) = Σᵢ 1 / (k + rankᵢ(d))

    其中 ``rankᵢ`` 是 doc 在检索器 i 中的 1 基名次；缺席的检索器贡献 0
    （等价于名次 ∞）。按 ``doc['id']`` 去重合并——候选必须带 id，缺 id 的
    doc 防御性跳过。返回的 doc 为浅拷贝副本，附加 ``_rrf_score``。
    """
    by_id: dict[str, dict] = {}
    rrf_score: dict[str, float] = {}

    for rank, (doc, _) in enumerate(bm25_ranking, start=1):
        did = doc.get('id') or ''
        if not did:
            continue
        by_id[did] = doc
        rrf_score[did] = rrf_score.get(did, 0.0) + 1.0 / (k + rank)

    for rank, (doc, _) in enumerate(cosine_ranking, start=1):
        did = doc.get('id') or ''
        if not did:
            continue
        # 同一 id 两边都有 → 保留一份 doc，RRF 得分累加
        by_id.setdefault(did, doc)
        rrf_score[did] = rrf_score.get(did, 0.0) + 1.0 / (k + rank)

    sorted_ids = sorted(rrf_score.keys(), key=lambda i: rrf_score[i], reverse=True)
    out: list[dict] = []
    for did in sorted_ids[:budget_total]:
        d = dict(by_id[did])  # 浅拷贝，不污染调用方缓存
        d['_rrf_score'] = rrf_score[did]
        out.append(d)
    return out


# ── 显著度加权（score_patch：RRF 之后的二次修正）──────────────────────

#: ``(doc, base_score) -> 新分``。丢错不抛：调用方自行防御。
ScorePatch = Callable[[dict, float], float]


def _clamp01(x: Any) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def make_salience_patch(gain: float = 0.5) -> ScorePatch:
    """构造「显著度加权」 score_patch：``score = base * (1 + gain * w)``，w ∈ [0,1]。

    w 的来源按 doc 字段优先级：
      1. 事实（有 ``importance``）：``w = imp_norm * (0.5 + 0.5 * confidence)``，
         ``imp_norm = (clamp(importance,1,10) - 1) / 9``；``confidence`` 缺省按 1.0
      2. 场景（有 ``count``）：``w = min(count / 5, 1)``（出现频次）
      3. 其它：``salience`` 字段直接取（clamp 0..1）

    性质：``gain=0`` → 恒等；w ≥ 0 → factor ≥ 1（只放大、不缩小），既不改
    变「谁入选」/不改入选集，只调入选者之间的名次。base 是 RRF（量级 ≈ 1/60），
    加权不足以盖过强词法命中，只动近邻名次。
    """
    g = max(0.0, float(gain or 0.0))

    def _patch(doc: dict, base: float) -> float:
        if g == 0.0 or not isinstance(doc, dict):
            return base
        if "importance" in doc:
            try:
                imp = int(doc.get("importance") or 1)
            except (TypeError, ValueError):
                imp = 1
            imp_norm = (max(1, min(10, imp)) - 1) / 9.0
            conf = doc.get("confidence", 1.0)
            if conf is None:
                conf = 1.0
            w = imp_norm * (0.5 + 0.5 * _clamp01(conf))
        elif "count" in doc:
            try:
                w = min(max(0.0, float(doc.get("count") or 0)) / 5.0, 1.0)
            except (TypeError, ValueError):
                w = 0.0
        else:
            w = _clamp01(doc.get("salience", 0.0))
        return base * (1.0 + g * w)

    return _patch


# ── 配置读取 ──────────────────────────────────────────────────────────


def _memory_config() -> dict:
    """读取 config 的 memory 段；加载失败返回空 dict（默认走保守路径）。"""
    try:
        from config import load_config
        cfg = load_config()
        return cfg.get("memory", {}) or {}
    except Exception as exc:
        logger.debug("[memory_hybrid] 读取 memory 配置失败: %s", exc)
        return {}


def default_score_patch() -> ScorePatch | None:
    """按 config 返回默认 score_patch；关闭 → None（行为与旧版完全一致）。

    config ``memory`` 段：
      - ``score_patch``（bool，默认 False）：总开关
      - ``score_patch_gain``（float，默认 0.5）：放大系数
    """
    cfg = _memory_config()
    if not bool(cfg.get("score_patch", False)):
        return None
    return make_salience_patch(cfg.get("score_patch_gain", 0.5))


def _default_embedding_provider() -> EmbeddingProvider | None:
    """按 config 返回默认 provider。

    provider 选择（``memory.embedding.provider``）：
    - ``"api"`` → ``core.memory_embedding_api``（OpenAI 兼容 /v1/embeddings，
      免本地模型、免 onnxruntime 版本约束）
    - ``"local"``（默认） → ``core.memory_embedding`` 进程单例（本地 ONNX）

    任一：未启用 / 未配置齐 / 模块不可用 → None（调用方回退
    NoopEmbeddingProvider，cosine 路径自动跳过，行为等同纯 BM25）。
    """
    emb_cfg = _memory_config().get("embedding") or {}
    kind = str(emb_cfg.get("provider", "local") or "local").strip().lower()
    if kind == "api":
        try:
            from .memory_embedding_api import default_api_embedding_provider as _api_factory
            return _api_factory()
        except Exception as exc:
            logger.debug("[memory_hybrid] api embedding provider 不可用: %s", exc)
            return None
    try:
        from .memory_embedding import default_embedding_provider as _factory
        return _factory()
    except Exception as exc:
        logger.debug("[memory_hybrid] 默认 embedding provider 不可用: %s", exc)
        return None


# ── 公开入口 ──────────────────────────────────────────────────────────


class HybridMemoryRecall:
    """BM25 + cosine + RRF 混合召回（同步，线程安全由调用方保证）。

    Args:
        embedding_provider: EmbeddingProvider 实现；None → 按 config
            ``memory.embedding.enabled`` 自动选择（P1-1：开 → ONNX 单例，
            关 → Noop）。provider 不可用 → cosine 路径自动跳过，退化为
            纯 BM25（fallback gate）。
        hybrid_enabled: 总开关；None 时读 config ``memory.hybrid_bm25``
            （默认 True）。
        stop_names: 从 query/doc 中剥离的昵称列表（防高频实体名污染 IDF）。
        bm25_threshold / cosine_threshold: 各路最低得分。
        budget_each: 每路取 top N 进入融合。
        budget_total: 融合后返回条数上限。
        rrf_k: RRF 融合常数。
        score_patch: RRF 融合后的二次修正 ``(doc, base) -> 新分``；None → 不修正
            （见 ``make_salience_patch`` / ``default_score_patch``）。
    """

    def __init__(
        self,
        embedding_provider: EmbeddingProvider | None = None,
        *,
        hybrid_enabled: bool | None = None,
        stop_names: list[str] | None = None,
        bm25_threshold: float = DEFAULT_BM25_THRESHOLD,
        cosine_threshold: float = DEFAULT_COSINE_THRESHOLD,
        budget_each: int = DEFAULT_BUDGET_EACH,
        budget_total: int = DEFAULT_BUDGET_TOTAL,
        rrf_k: int = DEFAULT_RRF_K,
        score_patch: ScorePatch | None = None,
    ):
        if hybrid_enabled is None:
            hybrid_enabled = bool(_memory_config().get("hybrid_bm25", True))
        self._enabled = bool(hybrid_enabled)
        if embedding_provider is None:
            embedding_provider = _default_embedding_provider()
        self._provider = embedding_provider or NoopEmbeddingProvider()
        self._stop_names = list(stop_names or [])
        self._bm25_threshold = float(bm25_threshold)
        self._cosine_threshold = float(cosine_threshold)
        self._budget_each = max(1, int(budget_each))
        self._budget_total = max(1, int(budget_total))
        self._rrf_k = max(1, int(rrf_k))
        self._score_patch = score_patch

    # ── 属性 ──

    @property
    def enabled(self) -> bool:
        """混合检索总开关（config memory.hybrid_bm25）。"""
        return self._enabled

    @property
    def embedding_available(self) -> bool:
        """嵌入服务当前是否可用（不可用 → 纯 BM25 路径）。"""
        try:
            return bool(self._provider.is_available())
        except Exception:
            return False

    @property
    def embedding_provider(self) -> EmbeddingProvider:
        return self._provider

    # ── 检索 ──

    def recall(self, query: str, docs: list[dict]) -> list[dict]:
        """对 ``docs`` 池做混合召回。

        Args:
            query: 自然语言查询串
            docs: 候选池，每项 ``{"id": str, "text": str, ...}``；
                 可选 ``embedding``（P1-1 缓存向量）。

        Returns:
            RRF 融合后的 doc 浅拷贝列表（按得分降序，最多 ``budget_total`` 条），
            每条带 ``_rrf_score``；无结果返回 ``[]``。
        """
        if not self._enabled:
            return []
        if not query or not query.strip():
            return []
        pool = [
            d for d in (docs or [])
            if isinstance(d, dict) and d.get('id') and d.get('text')
        ]
        if not pool:
            return []

        # 1) BM25 路径
        bm25_scored = bm25_rank(query, pool, stop_names=self._stop_names)
        bm25_top = [
            (d, s) for d, s in bm25_scored if s >= self._bm25_threshold
        ][:self._budget_each]

        # 2) cosine 路径（无 embedding 时为空 → RRF 纯 BM25 单侧）
        cosine_top: list[tuple[dict, float]] = []
        if self.embedding_available:
            cosine_scored = cosine_rank(query, pool, self._provider)
            cosine_top = [
                (d, s) for d, s in cosine_scored if s >= self._cosine_threshold
            ][:self._budget_each]

        # 3) RRF 融合（有 score_patch 时先不截断，让被加权项也能浮上来）
        patch = self._score_patch
        fused = rrf_fuse(
            bm25_top, cosine_top,
            k=self._rrf_k,
            budget_total=(len(pool) if patch is not None else self._budget_total),
        )
        # 4) score_patch 二次修正（记录 _rrf_raw，便于对照「加权前/后」）
        if patch is not None and fused:
            for d in fused:
                base = float(d.get('_rrf_score', 0.0) or 0.0)
                d['_rrf_raw'] = base
                try:
                    d['_rrf_score'] = float(patch(d, base))
                except Exception as exc:
                    logger.debug("[memory_hybrid] score_patch 失败(保持原分): %s", exc)
                    d['_rrf_score'] = base
            fused.sort(key=lambda d: float(d.get('_rrf_score', 0.0) or 0.0), reverse=True)
            fused = fused[:self._budget_total]
        if fused:
            logger.debug(
                "[memory_hybrid] query=%r bm25_scored=%d cosine_scored=%d fused=%d",
                query[:30], len(bm25_top), len(cosine_top), len(fused),
            )
        return fused


__all__ = [
    "HybridMemoryRecall",
    "EmbeddingProvider",
    "NoopEmbeddingProvider",
    "ScorePatch",
    "make_salience_patch",
    "default_score_patch",
    "bm25_rank",
    "cosine_rank",
    "rrf_fuse",
    "cosine_similarity",
    "DEFAULT_BM25_THRESHOLD",
    "DEFAULT_COSINE_THRESHOLD",
    "DEFAULT_BUDGET_EACH",
    "DEFAULT_BUDGET_TOTAL",
    "DEFAULT_RRF_K",
]
