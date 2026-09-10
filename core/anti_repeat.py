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
"""反重复 AntiRepeatCorpus — 语义指纹 + 时间窗去重（P1-5）。

移植说明
========
- ``bm25_score`` / ``AntiRepeatCorpus`` / ``UnansweredProactiveRepeatSignal``
  **直接搬运**自 N.E.K.O. ``memory/anti_repeat.py``（Apache-2.0，见
  ``docs/THIRD_PARTY_NOTICES.md``），保留版权头；仅做 oc-pet 适配：
  - 去 asyncio / 文件池：oc-pet 是 threading 单体，落盘同步 + 数据锁，
    ``flush_staged_detached`` 语义简化为同步 ``record_output``；
  - ngram 提取复用 oc-pet 已有的 ``core.memory_keywords.tokenize``
    （CJK 2/3-gram + 繁简折叠，与 P0-3 同一分词层，保持单一事实源）；
  - 持久化落 ``~/.oc-pet/memory/anti_repeat.json``（多角色共用一个文件，
    按 name 分 corpus），N.E.K.O. 原版是每角色一个文件。
- 阈值数值与 N.E.K.O. ``config/session_settings.py`` 完全一致
  （BG_WINDOW=100 / FG_WINDOW=5 / FG_TTL=600s / BM25 k1=1.5 b=0.75 …）。

设计（来自上游，保持原样）
--------
- background corpus = 最近 ``ANTI_REPEAT_BG_WINDOW`` 条 AI 输出（count 封顶，
  不做时间过滤 → IDF 语境跨空闲期存活）
- foreground query = 最近 ``ANTI_REPEAT_FG_WINDOW`` 条 + ``FG_TTL_SECONDS``
  内的条目（TF/复读是新鲜度信号；空闲超过 TTL 前景清空 → 分数 0 → 放行，
  修复"空闲死锁"）
- 新 draft 分数 = Σ BM25(term, fg)；高频常见词（今天/觉得/哈哈）DF 高 →
  IDF 低 → 几乎不贡献；话题词（老虎/那个 bug）DF 低 → IDF 高 → 强信号
- 两条路径共用 corpus：proactive 超 ``REGEN_THRESHOLD`` → 建议换文案；
  仍超 ``DROP_THRESHOLD`` → 放弃本轮投递；普通回复不硬拦，只提示话题
- ``score_unanswered_proactive_draft`` 是长窗口信号：用户持续无互动时，
  跨小时/隔几轮出现的高度相似主动搭话（Dice ≥ 0.85，命中 ≥ 2 次）→ 拒绝，
  用于"同文案跨会话拒绝"（与 BM25 短窗互补）

线程约束：纯 Python + ``threading.Lock``，无 Qt / asyncio；由调用方
（ProactiveScheduler，主线程）持有；后台线程不直接触碰。
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.memory_keywords import tokenize

logger = __import__("logging").getLogger(__name__)

# ── 阈值（与 N.E.K.O. config/session_settings.py 一致；config.anti_repeat 可覆盖）──
ANTI_REPEAT_BG_WINDOW = 100           # 背景 corpus 窗口长度（条数，count 封顶）
ANTI_REPEAT_FG_WINDOW = 5             # 前景窗长度（最近 N 条算 TF）
ANTI_REPEAT_FG_TTL_SECONDS = 600.0    # 前景时间新鲜度上限（10 分钟）
ANTI_REPEAT_UNANSWERED_WINDOW = 64    # 长窗口复读信号最多检查的主动搭话条数
ANTI_REPEAT_UNANSWERED_MAX_AGE_SECONDS = 86400.0   # 长窗口回看最长时间（24h）
ANTI_REPEAT_UNANSWERED_SIMILARITY_THRESHOLD = 0.85 # ngram Dice 相似度阈值
ANTI_REPEAT_UNANSWERED_MIN_MATCHES = 2             # 命中 ≥2 条未回应相似内容才触发
ANTI_REPEAT_UNANSWERED_MIN_DRAFT_TOKENS = 4        # 长窗口评分最小 ngram 数
ANTI_REPEAT_INJECT_TOP_K = 6          # top_recent_topics 返回数量
ANTI_REPEAT_REGEN_THRESHOLD = 8.0     # proactive 出口 BM25 总分超此值 → 建议换文案
ANTI_REPEAT_DROP_THRESHOLD = 16.0     # 仍超此值 → 放弃投递
ANTI_REPEAT_BM25_K1 = 1.5             # BM25 k1（TF saturation）
ANTI_REPEAT_BM25_B = 0.75             # BM25 b（文档长度归一化）
ANTI_REPEAT_MIN_DRAFT_TOKENS = 12     # 短于此 ngram 数不评分直接放行

_SCHEMA_VERSION = 1
_DEFAULT_KEY = "default"

# 默认记忆目录（与 companion_memory.DEFAULT_MEMORY_DIR 对齐）
DEFAULT_MEMORY_DIR = os.path.join(os.path.expanduser("~"), ".oc-pet", "memory")


@dataclass(frozen=True, slots=True)
class UnansweredProactiveRepeatSignal:
    """长窗口证据：用户持续忽略同一形状的内容（跨会话去重信号）。"""

    triggered: bool = False
    match_count: int = 0
    considered_count: int = 0
    best_similarity: float = 0.0
    repeated_terms: Tuple[str, ...] = ()


def _resolve_name(name: Optional[str]) -> Optional[str]:
    """空 / None 角色名归一化到 ``_DEFAULT_KEY``。"""
    if not name:
        return _DEFAULT_KEY
    return name


def _now() -> float:
    return time.time()


# ── ngram 提取（复用 core.memory_keywords.tokenize，保持单一分词事实源）──

def _ngrams(text: str) -> List[str]:
    """从 ``text`` 提取 ngram（list 语义保留词频，BM25 TF 依赖 multiplicity）。

    与 N.E.K.O. ``_extract_keywords`` 语义对齐：CJK 2/3-gram + Latin token。
    失败时兜底空格切分（绝不阻塞主流程）。
    """
    try:
        return tokenize(text or "")
    except Exception:  # pragma: no cover - 防御式兜底
        return list({t for t in (text or "").split() if len(t) >= 2})


# ── 持久化 schema ────────────────────────────────────────────

def _default_payload() -> Dict[str, Any]:
    return {"version": _SCHEMA_VERSION, "corpora": {}}


def _normalize_entry(raw: Any) -> Optional[Dict[str, Any]]:
    """归一化磁盘读入的 entry；失败返回 None。

    Entry shape: ``{"ts": float, "ngrams": [str], "is_proactive": bool}``
    """
    if not isinstance(raw, dict):
        return None
    try:
        ngrams = raw.get("ngrams") or []
        if not isinstance(ngrams, list):
            return None
        clean = [s for s in ngrams if isinstance(s, str) and s]
        if not clean:
            return None
        return {
            "ts": float(raw.get("ts") or 0) or _now(),
            "ngrams": clean,
            "is_proactive": bool(raw.get("is_proactive", False)),
        }
    except Exception:
        return None


def _normalize_corpus(raw: Any) -> List[Dict[str, Any]]:
    """归一化磁盘读入的整个 corpus（window 列表）。"""
    if not isinstance(raw, dict):
        return []
    items = raw.get("window")
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for entry in items:
        norm = _normalize_entry(entry)
        if norm is not None:
            out.append(norm)
    return out


# ── BM25 评分（verbatim from N.E.K.O. memory/anti_repeat.py）──

def bm25_score(
    draft_ngrams: List[str],
    fg_docs: List[List[str]],
    bg_docs: Optional[List[List[str]]] = None,
    *,
    k1: float = ANTI_REPEAT_BM25_K1,
    b: float = ANTI_REPEAT_BM25_B,
) -> Tuple[float, Dict[str, float]]:
    """计算 ``draft`` 相对前景窗 ``fg_docs`` 的"复读程度" BM25 分数。

    与经典检索 BM25 的关键区别：经典 BM25 给"语料中稀有"的词高分（检索
    相关性偏好稀有关键词），而**复读检测**要的是"背景里稀有 + 最近频繁"——
    前者来自大 BG 窗上的 IDF，后者来自小 FG 窗上的累计 TF：

    - ``bg_docs``（默认 = fg_docs）算 DF/IDF：词在整个窗口多少篇文档出现
    - ``fg_docs`` 算 TF：词在**最近 FG 条目**的累计频率
    - total = Σ_term IDF_bg(term) × Σ_doc∈fg BM25_tf_norm(term, doc)

    Returns ``(total, per_term)``；``per_term`` 只含正贡献项，按分数降序。
    空 fg_docs / 空 draft_ngrams → ``(0.0, {})``。
    """
    if not draft_ngrams or not fg_docs:
        return 0.0, {}
    if bg_docs is None:
        bg_docs = fg_docs

    n_bg = len(bg_docs) or 1
    avgdl = sum(len(d) for d in fg_docs) / len(fg_docs) if fg_docs else 0.0
    if avgdl <= 0:
        return 0.0, {}

    # DF 在 BG 窗上算；用 set 避免一条文档里同 ngram 重复
    df: Dict[str, int] = {}
    for doc in bg_docs:
        for term in set(doc):
            df[term] = df.get(term, 0) + 1

    draft_unique = set(draft_ngrams)

    total = 0.0
    per_term_total: Dict[str, float] = {}
    for term in draft_unique:
        n = df.get(term, 0)
        # IDF Robertson-Sparck-Jones（+0.5 平滑）。term 没在 BG 里出现按 0 处理。
        if n <= 0:
            continue
        idf = math.log((n_bg - n + 0.5) / (n + 0.5) + 1.0)
        if idf <= 0:
            continue
        term_score = 0.0
        for doc in fg_docs:
            tf = doc.count(term)
            if tf == 0:
                continue
            dl = len(doc) or 1
            norm = 1 - b + b * dl / avgdl
            term_score += idf * (tf * (k1 + 1)) / (tf + k1 * norm)
        if term_score > 0:
            per_term_total[term] = term_score
            total += term_score
    return total, dict(
        sorted(per_term_total.items(), key=lambda kv: kv[1], reverse=True)
    )


# ── 管理器 ──────────────────────────────────────────────────

class AntiRepeatCorpus:
    """按角色滚动的反重复 corpus（线程安全，同步落盘）。

    用法:
        store = AntiRepeatCorpus()
        store.record_output(name, ai_text, is_proactive=True)
        total, terms = store.score_draft(name, draft_text)
        if total > ANTI_REPEAT_DROP_THRESHOLD: ... 放弃投递 ...
        hint_terms = store.top_recent_topics(name, k=6)
        sig = store.score_unanswered_proactive_draft(name, draft, silence_since=ts)
    """

    def __init__(self, memory_dir: str | os.PathLike | None = None) -> None:
        self._memory_dir = str(memory_dir) if memory_dir else DEFAULT_MEMORY_DIR
        self._cache: Dict[str, List[Dict[str, Any]]] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ── path / lock ────────────────────────────────────────

    def _file_path(self) -> str:
        return os.path.join(self._memory_dir, "anti_repeat.json")

    def _get_lock(self, name: str) -> threading.Lock:
        if name not in self._locks:
            with self._locks_guard:
                if name not in self._locks:
                    self._locks[name] = threading.Lock()
        return self._locks[name]

    # ── load / save（锁由调用方持有）──────────────────────

    def _read_all_from_disk(self) -> Dict[str, List[Dict[str, Any]]]:
        """读磁盘上的全部 corpus（按 name）；损坏/缺失 → 空 dict。"""
        result: Dict[str, List[Dict[str, Any]]] = {}
        path = self._file_path()
        if not os.path.exists(path):
            return result
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            corpora = raw.get("corpora") if isinstance(raw, dict) else None
            if isinstance(corpora, dict):
                for name, corpus in corpora.items():
                    window = _normalize_corpus(corpus)
                    # 立即按当前 BG_WINDOW 裁掉过老条目（磁盘可能是旧配置写的）
                    if len(window) > ANTI_REPEAT_BG_WINDOW:
                        window.sort(key=lambda e: float(e.get("ts", 0)))
                        window = window[-ANTI_REPEAT_BG_WINDOW:]
                    result[name] = window
        except Exception as exc:
            logger.warning("[AntiRepeat] load failed, starting empty: %s", exc)
            result = {}
        return result

    def _load_unlocked(self, name: str) -> List[Dict[str, Any]]:
        if name in self._cache:
            return self._cache[name]
        all_corpora = self._read_all_from_disk()
        self._cache.update(all_corpora)
        self._cache.setdefault(name, [])
        return self._cache[name]

    def _save_unlocked(self) -> None:
        """把当前内存里全部 corpus 原子写盘。调用方已持锁。"""
        try:
            os.makedirs(self._memory_dir, exist_ok=True)
            payload = {
                "version": _SCHEMA_VERSION,
                "corpora": {name: {"window": window} for name, window in self._cache.items()},
            }
            fd, tmp_path = tempfile.mkstemp(dir=self._memory_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self._file_path())
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    logger.debug("anti_repeat: 非致命异常(已静默吞掉)", exc_info=True)
                raise
        except Exception as exc:
            logger.warning("[AntiRepeat] save failed: %s", exc)

    # ── public API ─────────────────────────────────────────

    def preload(self, name: str) -> None:
        """预热某角色的 corpus（首次磁盘读）。后续同步调用不再阻塞读盘。"""
        name = _resolve_name(name)
        with self._get_lock(name):
            if name in self._cache:
                return
            self._load_unlocked(name)

    def record_output(
        self,
        name: str,
        text: str,
        *,
        is_proactive: bool = False,
        now: Optional[float] = None,
    ) -> bool:
        """登记一条 AI 输出（写入 BG corpus 并立即落盘）。

        - 普通输出短于 ``ANTI_REPEAT_MIN_DRAFT_TOKENS`` 不存储；主动搭话用
          较低 ``ANTI_REPEAT_UNANSWERED_MIN_DRAFT_TOKENS`` 阈值，让简短提醒
          仍可被长窗口评分器看到，同时"嗯/好"不稀释 DF。
        - 插入后超 BG_WINDOW 弹出最老条目。
        - 空名归一化到 ``_DEFAULT_KEY``。

        Returns:
            True=已写入；False=跳过（空文本/过短）。
        """
        if not text or not text.strip():
            return False
        name = _resolve_name(name)
        ngrams = _ngrams(text)
        min_tokens = (
            ANTI_REPEAT_UNANSWERED_MIN_DRAFT_TOKENS
            if is_proactive
            else ANTI_REPEAT_MIN_DRAFT_TOKENS
        )
        if len(ngrams) < min_tokens:
            return False
        ts = float(now if now is not None else _now())
        entry = {"ts": ts, "ngrams": ngrams, "is_proactive": bool(is_proactive)}
        with self._get_lock(name):
            window = self._load_unlocked(name)
            window.append(entry)
            window.sort(key=lambda e: float(e.get("ts", 0)))
            if len(window) > ANTI_REPEAT_BG_WINDOW:
                del window[: len(window) - ANTI_REPEAT_BG_WINDOW]
            self._cache[name] = window
            self._save_unlocked()
        return True

    @staticmethod
    def _split_fg_bg(
        window: List[Dict[str, Any]],
        fg_window: int,
        now: Optional[float],
    ) -> Tuple[List[List[str]], List[List[str]]]:
        """构建 ``(fg_docs, bg_docs)``。

        - **BG** = 整个 count 封顶窗口，不过滤 → DF/IDF 词频背景。从不做时间
          过滤，IDF 语境跨空闲期完整存活。
        - **FG** = 尾部 ``fg_window`` 条，但只取 ``ANTI_REPEAT_FG_TTL_SECONDS``
          内的条目。TF/复读是新鲜度信号：过期条目掉出 → 空闲冻结的窗停止评分
          （空闲死锁修复）。
        """
        bg_docs = [e["ngrams"] for e in window]
        ref = float(now if now is not None else _now())
        fresh = [
            e for e in window
            if ref - float(e.get("ts", 0.0)) <= ANTI_REPEAT_FG_TTL_SECONDS
        ]
        if fg_window > 0 and len(fresh) > fg_window:
            fg_docs = [e["ngrams"] for e in fresh[-fg_window:]]
        else:
            fg_docs = [e["ngrams"] for e in fresh]
        return fg_docs, bg_docs

    def score_draft(
        self,
        name: str,
        draft_text: str,
        *,
        fg_window: int = ANTI_REPEAT_FG_WINDOW,
        now: Optional[float] = None,
    ) -> Tuple[float, Dict[str, float]]:
        """BM25 评分草稿（相对最近 ``fg_window`` 条 AI 输出）。

        Returns ``(total_score, per_term_score)``。
        - 太短草稿 / 空 corpus → ``(0.0, {})``
        - FG 只计 ``ANTI_REPEAT_FG_TTL_SECONDS`` 内条目；整个尾部窗过期（空闲）
          → 分数 0 → 放行（防复读死锁修复）
        - ``now`` 可注入（测试）；空名归一化到 ``_DEFAULT_KEY``
        """
        if not draft_text or not draft_text.strip():
            return 0.0, {}
        name = _resolve_name(name)
        draft_ngrams = _ngrams(draft_text)
        if len(draft_ngrams) < ANTI_REPEAT_MIN_DRAFT_TOKENS:
            return 0.0, {}
        with self._get_lock(name):
            window = self._load_unlocked(name)
            fg_docs, bg_docs = self._split_fg_bg(window, fg_window, now)
        if not fg_docs:
            return 0.0, {}
        return bm25_score(draft_ngrams, fg_docs, bg_docs)

    def is_repeat(
        self,
        name: str,
        draft_text: str,
        *,
        drop_threshold: float = ANTI_REPEAT_DROP_THRESHOLD,
        now: Optional[float] = None,
    ) -> bool:
        """便捷封装：BM25 总分超 ``drop_threshold`` 视为"近重复"，应放弃投递。

        与 proactive 的字符串相似去重互补：这里是语义指纹（BM25 IDF×TF），
        能抓住"换了个说法但仍是同一话题"的近重复；字符串去重抓字面复读。
        """
        total, _terms = self.score_draft(name, draft_text, now=now)
        return total >= drop_threshold

    def score_unanswered_proactive_draft(
        self,
        name: str,
        draft_text: str,
        *,
        silence_since: Optional[float],
        now: Optional[float] = None,
        max_age_seconds: float = ANTI_REPEAT_UNANSWERED_MAX_AGE_SECONDS,
        window: int = ANTI_REPEAT_UNANSWERED_WINDOW,
        similarity_threshold: float = ANTI_REPEAT_UNANSWERED_SIMILARITY_THRESHOLD,
        min_matches: int = ANTI_REPEAT_UNANSWERED_MIN_MATCHES,
    ) -> UnansweredProactiveRepeatSignal:
        """检测用户持续无互动时的重复主动搭话内容（跨会话/跨小时）。

        与短生命周期 BM25 前景窗刻意分开：BM25 防 back-to-back 话题复读；
        本信号抓"隔几轮或隔几小时又出现的高度相似模板"。只有 ``silence_since``
        之后投递的主动搭话参与——任何真实用户消息都会重置证据（不改 corpus）。
        """
        if (
            silence_since is None
            or not draft_text
            or not draft_text.strip()
            or window <= 0
            or min_matches <= 0
        ):
            return UnansweredProactiveRepeatSignal()

        name = _resolve_name(name)
        draft_ngrams = set(_ngrams(draft_text))
        if len(draft_ngrams) < ANTI_REPEAT_UNANSWERED_MIN_DRAFT_TOKENS:
            return UnansweredProactiveRepeatSignal()

        ref = float(now if now is not None else _now())
        lower_bound = max(float(silence_since), ref - max(0.0, max_age_seconds))
        with self._get_lock(name):
            corpus = self._load_unlocked(name)
            candidates = [
                entry
                for entry in corpus
                if entry.get("is_proactive")
                and lower_bound < float(entry.get("ts", 0.0)) <= ref
            ][-window:]

        matches: List[Tuple[float, set]] = []
        for entry in candidates:
            old_ngrams = set(entry.get("ngrams") or ())
            if not old_ngrams:
                continue
            overlap = draft_ngrams & old_ngrams
            similarity = (2.0 * len(overlap)) / (
                len(draft_ngrams) + len(old_ngrams)
            )
            if similarity >= similarity_threshold:
                matches.append((similarity, old_ngrams))

        if not matches:
            return UnansweredProactiveRepeatSignal(
                considered_count=len(candidates),
            )

        term_frequency: Dict[str, int] = {}
        for _similarity, old_ngrams in matches:
            for term in draft_ngrams & old_ngrams:
                term_frequency[term] = term_frequency.get(term, 0) + 1
        repeated_terms = tuple(
            term
            for term, _count in sorted(
                term_frequency.items(),
                key=lambda item: (-item[1], -len(item[0]), item[0]),
            )[:ANTI_REPEAT_INJECT_TOP_K]
        )
        match_count = len(matches)
        return UnansweredProactiveRepeatSignal(
            triggered=match_count >= min_matches,
            match_count=match_count,
            considered_count=len(candidates),
            best_similarity=max(similarity for similarity, _ in matches),
            repeated_terms=repeated_terms,
        )

    def top_recent_topics(
        self,
        name: str,
        *,
        k: int = ANTI_REPEAT_INJECT_TOP_K,
        fg_window: int = ANTI_REPEAT_FG_WINDOW,
        now: Optional[float] = None,
    ) -> List[str]:
        """返回最近 fg_window 条里 BM25 排名前 K 的 ngram（话题提示词）。

        用法：注入下一轮 prompt 告诉模型"你最近聊过 X / Y / Z"。
        DF 用整个 BG 窗（常见词 IDF 低），TF 用 FG 窗：效果是"最近 5 条里
        频繁 + 整体语料里少见"的 ngram 排最前。FG 过期 → 空列表。
        """
        if k <= 0:
            return []
        name = _resolve_name(name)
        with self._get_lock(name):
            window = self._load_unlocked(name)
            if not window:
                return []
            fg_docs, bg_docs = self._split_fg_bg(window, fg_window, now)
        if not fg_docs:
            return []
        synthetic_draft: List[str] = []
        for doc in fg_docs:
            synthetic_draft.extend(doc)
        if not synthetic_draft:
            return []
        _total, per_term = bm25_score(synthetic_draft, fg_docs, bg_docs)
        return list(per_term.keys())[:k]

    def clear(self, name: str) -> None:
        """清空某角色 corpus 并落盘（测试/重置用）。"""
        name = _resolve_name(name)
        with self._get_lock(name):
            self._cache[name] = []
            self._save_unlocked()


# ── 进程级单例 ─────────────────────────────────────────────
_GLOBAL_CORPUS: Optional[AntiRepeatCorpus] = None
_GLOBAL_LOCK = threading.Lock()


def get_anti_repeat_corpus(memory_dir: str | os.PathLike | None = None) -> AntiRepeatCorpus:
    """返回进程级 AntiRepeatCorpus 单例（可指定 memory_dir 覆盖默认）。"""
    global _GLOBAL_CORPUS
    if _GLOBAL_CORPUS is None:
        with _GLOBAL_LOCK:
            if _GLOBAL_CORPUS is None:
                _GLOBAL_CORPUS = AntiRepeatCorpus(memory_dir=memory_dir)
    return _GLOBAL_CORPUS


# 兼容别名：与 N.E.K.O. 上游同名（下游 import 时不改代码）
AntiRepeatCorpus.__name__ = "AntiRepeatCorpus"

__all__ = [
    "AntiRepeatCorpus",
    "UnansweredProactiveRepeatSignal",
    "bm25_score",
    "get_anti_repeat_corpus",
    "ANTI_REPEAT_BG_WINDOW",
    "ANTI_REPEAT_FG_WINDOW",
    "ANTI_REPEAT_FG_TTL_SECONDS",
    "ANTI_REPEAT_UNANSWERED_WINDOW",
    "ANTI_REPEAT_UNANSWERED_MAX_AGE_SECONDS",
    "ANTI_REPEAT_UNANSWERED_SIMILARITY_THRESHOLD",
    "ANTI_REPEAT_UNANSWERED_MIN_MATCHES",
    "ANTI_REPEAT_UNANSWERED_MIN_DRAFT_TOKENS",
    "ANTI_REPEAT_INJECT_TOP_K",
    "ANTI_REPEAT_REGEN_THRESHOLD",
    "ANTI_REPEAT_DROP_THRESHOLD",
    "ANTI_REPEAT_BM25_K1",
    "ANTI_REPEAT_BM25_B",
    "ANTI_REPEAT_MIN_DRAFT_TOKENS",
    "DEFAULT_MEMORY_DIR",
]
