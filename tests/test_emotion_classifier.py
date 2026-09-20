"""情绪分类器单测（不打网络）。

## 覆盖什么

1. **算法忠实性**——top-K 加权投票、相似度阈值、置信度公式
2. **边界与降级**——空文本、provider 缺失、嵌入失败、低相似度
3. **语料完整性**——1400 条 / 14 类、强度区间、VAD 预设齐备
4. **不抛异常**——分类器对上层承诺「永不抛异常」

## 为什么用假 provider

真实 provider 要打 embedding API（慢、要凭据、有配额）。
单测要的是**算法行为**，用一个确定性的假向量空间就够了：
把每条语料映射到它类别的基向量上，相似度就完全可控。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.emotion_classifier import (  # noqa: E402
    DEFAULT_SIMILARITY_THRESHOLD,
    EMOTION_VAD_PRESETS,
    EmotionClassifier,
    _clamp01,
    _cosine,
)

CORPUS_PATH = os.path.join(ROOT, "core", "emotion_data", "emotion_corpus.json")


# ── 假 provider ─────────────────────────────────────────────

class FakeProvider:
    """把文本映射到「类别基向量」的假 provider。

    ``vector_for(text)`` 由构造时的映射决定；未映射的文本返回一个
    与所有基向量都正交的向量（相似度 0）。
    """

    def __init__(self, mapping: dict[str, list[float]], dim: int = 4):
        self.mapping = dict(mapping)
        self.dim = dim
        self.calls = 0

    def is_available(self) -> bool:
        return True

    def embed_texts(self, texts):
        self.calls += 1
        out = []
        for t in texts:
            v = self.mapping.get(t)
            out.append(list(v) if v else [0.0] * self.dim)
        return out


class BrokenProvider:
    def is_available(self) -> bool:
        return True

    def embed_texts(self, texts):
        return [None] * len(texts)


class NoIsAvailableProvider:
    """没有 is_available 方法——应回退到「有 provider 就算可用」。"""

    def embed_texts(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


# ── 工具函数 ────────────────────────────────────────────────

def test_cosine_basic():
    assert _cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert _cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert _cosine([1, 0], [-1, 0]) == pytest.approx(-1.0)


def test_cosine_degenerate():
    """零向量不炸，返回 0。"""
    assert _cosine([0, 0], [1, 1]) == 0.0
    assert _cosine([], []) == 0.0


def test_clamp01():
    assert _clamp01(-5) == 0.0
    assert _clamp01(5) == 1.0
    assert _clamp01(0.42) == pytest.approx(0.42)


# ── 语料完整性 ──────────────────────────────────────────────

def test_corpus_file_exists():
    assert os.path.exists(CORPUS_PATH), "语料文件缺失，先跑 tools/extract_soullink_corpus.py"


def test_corpus_shape():
    data = json.load(open(CORPUS_PATH, encoding="utf-8"))
    corpus = data["corpus"]
    assert len(corpus) == 14, f"应为 14 类，实际 {len(corpus)}"
    total = sum(len(v) for v in corpus.values())
    assert total == 1400, f"应为 1400 条，实际 {total}"
    for cat, items in corpus.items():
        assert len(items) == 100, f"{cat} 应为 100 条，实际 {len(items)}"
        assert all(isinstance(s, str) and s for s in items), f"{cat} 有空条目"
        assert len(set(items)) == len(items), f"{cat} 有重复"


def test_corpus_has_license_attribution():
    """MIT 搬运必须保留归属。"""
    data = json.load(open(CORPUS_PATH, encoding="utf-8"))
    assert "soullink-emotion-sdk" in data.get("_source", "")
    assert data.get("_license") == "MIT"


def test_corpus_is_tracked_by_git():
    """语料必须能被 git 跟踪。

    为什么单独守一道：语料曾经放在 ``core/data/``，而 ``.gitignore`` 里的
    ``data/`` 把它整个忽略了 —— 新克隆会没有语料，分类器静默降级，
    又是一个「单测全绿但功能是死的」。

    现在语料在 ``core/emotion_data/``（不被忽略）。本测试用
    ``git check-ignore`` 验证，防止将来又搬回被忽略的目录。
    """
    import subprocess
    repo = os.path.dirname(ROOT)
    r = subprocess.run(
        ["git", "check-ignore", CORPUS_PATH],
        cwd=repo, capture_output=True, text=True)
    assert r.returncode != 0, (
        f"语料被 .gitignore 忽略了（{r.stdout.strip()}）——"
        "新克隆会拿不到语料，分类器会静默降级"
    )


def test_vad_presets_cover_corpus():
    data = json.load(open(CORPUS_PATH, encoding="utf-8"))
    missing = [c for c in data["corpus"] if c not in EMOTION_VAD_PRESETS]
    assert not missing, f"这些类别没有 VAD 预设: {missing}"


def test_defaults_have_intensity_ranges():
    data = json.load(open(CORPUS_PATH, encoding="utf-8"))
    defaults = data.get("defaults") or {}
    for cat in data["corpus"]:
        d = defaults.get(cat)
        assert d, f"{cat} 缺 defaults"
        lo, hi = d["intensity"]
        assert 0.0 <= lo <= hi <= 1.0, f"{cat} 强度区间非法: {lo},{hi}"


# ── 分类行为 ────────────────────────────────────────────────

def _mk_classifier(provider, corpus_path=CORPUS_PATH, **kw):
    return EmotionClassifier(provider=provider, corpus_path=corpus_path,
                             cache_path=None, **kw)


def test_exact_hit_short_circuits(tmp_path):
    """语料原文应精确命中，不走向量。"""
    data = json.load(open(CORPUS_PATH, encoding="utf-8"))
    sample = data["corpus"]["happy"][0]
    p = FakeProvider({sample: [1.0, 0.0, 0.0, 0.0]})
    clf = _mk_classifier(p)
    assert clf.prepare()
    r = clf.classify(sample)
    assert r.source == "exact"
    assert r.emotion == "happy"
    assert r.confidence == pytest.approx(1.0)
    assert r.similarity == pytest.approx(1.0)


def test_low_similarity_returns_neutral():
    """所有相似度都低于阈值 → neutral，不是硬塞一个类别。"""
    p = FakeProvider({})  # 全部返回零向量 → 相似度 0
    clf = _mk_classifier(p)
    assert clf.prepare()
    r = clf.classify("完全无关的一句话")
    assert r.emotion == "neutral"
    assert r.source == "neutral"
    assert r.vad == (0.0, 0.0, 0.0)


def test_empty_text_returns_neutral():
    p = FakeProvider({})
    clf = _mk_classifier(p)
    clf.prepare()
    for t in ("", "   ", None):
        r = clf.classify(t)
        assert r.emotion == "neutral"


def test_provider_failure_degrades():
    """嵌入失败 → neutral + fallback，不抛。"""
    clf = _mk_classifier(BrokenProvider())
    clf.prepare()  # 语料嵌入全失败 → prepare 返回 False
    r = clf.classify("随便什么")
    assert r.emotion == "neutral"
    assert r.source in ("fallback", "neutral")


def test_no_provider_is_unavailable():
    clf = _mk_classifier(None)
    assert clf.available() is False
    r = clf.classify("测试")
    assert r.emotion == "neutral"
    assert r.source == "fallback"


def test_provider_without_is_available_method():
    """没有 is_available 的 provider 也应算可用。"""
    clf = _mk_classifier(NoIsAvailableProvider())
    assert clf.available() is True


def test_weighted_voting_picks_majority():
    """两个同类近邻应胜过单个更相似的异类近邻。

    构造：语料里 happy 两条、sad 一条；查询向量与两条 happy 都很近，
    与 sad 稍近但只有一条 —— 投票应选 happy。
    """
    p = FakeProvider({
        "h1": [1.0, 0.0],
        "h2": [0.99, 0.1],
        "s1": [0.98, 0.0],
        "query": [1.0, 0.0],
    })
    clf = EmotionClassifier(provider=p, corpus_path=CORPUS_PATH, cache_path=None)
    # 直接注入语料（跳过文件加载）
    clf._texts = ["h1", "h2", "s1"]
    clf._labels = ["happy", "happy", "sad"]
    clf._intensities = [0.7, 0.7, 0.7]
    clf._vecs = p.embed_texts(["h1", "h2", "s1"])
    clf._ready = True

    r = clf.classify("query")
    assert r.source == "embedding"
    assert r.emotion == "happy", f"投票应选 happy，实际 {r.emotion}"


def test_confidence_in_range():
    p = FakeProvider({"q": [1.0, 0.0]})
    clf = EmotionClassifier(provider=p, corpus_path=CORPUS_PATH, cache_path=None)
    clf._texts = ["a"]
    clf._labels = ["happy"]
    clf._intensities = [0.7]
    clf._vecs = [[1.0, 0.0]]
    clf._ready = True
    r = clf.classify("q")
    assert 0.0 <= r.confidence <= 1.0
    assert 0.0 <= r.intensity <= 1.0


def test_intensity_from_same_class_neighbours():
    """强度取同类近邻的加权平均，不是全局平均。"""
    p = FakeProvider({"q": [1.0, 0.0]})
    clf = EmotionClassifier(provider=p, corpus_path=CORPUS_PATH, cache_path=None)
    clf._texts = ["a", "b"]
    clf._labels = ["happy", "happy"]
    clf._intensities = [0.2, 0.8]
    clf._vecs = [[1.0, 0.0], [1.0, 0.0]]
    clf._ready = True
    r = clf.classify("q")
    assert r.intensity == pytest.approx(0.5, abs=0.05)


def test_extra_corpus_is_merged():
    """extra_corpus 应并入语料。"""
    p = FakeProvider({})
    clf = EmotionClassifier(
        provider=p, corpus_path=CORPUS_PATH, cache_path=None,
        extra_corpus={"concerned": ["记得让眼睛歇一歇"]})
    clf._load_corpus()
    assert "记得让眼睛歇一歇" in clf._texts
    idx = clf._texts.index("记得让眼睛歇一歇")
    assert clf._labels[idx] == "concerned"


def test_status_reports_state():
    p = FakeProvider({})
    clf = _mk_classifier(p)
    st = clf.status()
    assert "ready" in st and "corpus_size" in st and "emotions" in st
    assert st["available"] is True


def test_threshold_is_configurable():
    clf = EmotionClassifier(provider=FakeProvider({}), corpus_path=CORPUS_PATH,
                            cache_path=None, threshold=0.9)
    assert clf.status()["threshold"] == pytest.approx(0.9)


def test_default_threshold_matches_sdk():
    """默认阈值应与 SDK 一致（0.65）。"""
    assert DEFAULT_SIMILARITY_THRESHOLD == pytest.approx(0.65)
