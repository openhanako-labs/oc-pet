"""情绪分类器：文本 → 情绪类别 + 强度 + 连续 VAD。

## 它从哪来

移植自 Soullink Emotion SDK 的 ``@soullink-emotion/classifier-embedding``
（https://github.com/nanlingyin/soullink-emotion-sdk ，MIT）。

原实现是 TypeScript；本模块是 Python 重写，算法保持一致：
**top-K 近邻 → 相似度加权投票 → 加权 VAD → 置信度**。

语料（1400 条 / 14 类）与 VAD 预设表按 MIT 协议搬运，归属见
``core/data/emotion_corpus.json`` 的 ``_source`` 字段。

## 为什么需要它（2026-09-20 的决策）

之前两条路都撞墙：

1. **VA 坐标映射**（``perception_mixin._va_to_emotion``）——
   VA 二维承载不了情绪细粒度，正价区全塌成 happy。
   实测日志样本「记得让眼睛歇一歇」被判 happy，语义其实是**关心**。
2. **改 LLM 契约**（要求模型吐 ``[emotion:]``）——
   本地模型测不出主 LLM 行为；且两条历史教训压着：
   多标签必填 → 模型全不写（14 次 0 命中）；
   可选标签 → 模型无视（``[do:]`` 0 次使用）。

第三条路：**不碰契约、不问模型，直接对文本做向量近邻**。

## 两个视角（实测差异巨大）

| 视角 | 输入 | 实测命中 |
|---|---|---|
| ``user`` | 用户第一人称倾诉（「我今天有点累」） | 7/8 = 88% |
| ``pet`` | 桌宠第二人称嘱咐（「记得让眼睛歇一歇」） | 2/6 = 33%（原语料） |

分布错配是真实的：语料是**第一人称倾诉**，而桌宠说的是**第二人称嘱咐**。
最近邻会抓到「歇」的字面，抓不到「关心」的意图。
补桌宠视角语料能修（33% → 67%，仅补 16 条）。

## 边界（不要过度信任）

- **置信度不是校准概率**——它是「相似度余量」和「投票占比」的加权，
  用来排序可以，当概率用不行（与 ``expression_director`` 同一原则）。
- 低置信**一律回退 neutral**，宁可不动，不能乱动。
- 类别重叠真实存在：桌宠的「关心」与「亲昵」相似度只差 0.02 的情况出现过。
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 语料是**源码资产**（随仓库分发），放 core/emotion_data/。
# 不放 core/data/ —— 那个目录被 .gitignore 的 `data/` 忽略，
# 语料放进去会导致新克隆没有语料 → 分类器静默降级 → 又是一个「功能是死的」。
CORPUS_PATH = os.path.join(ROOT, "core", "emotion_data", "emotion_corpus.json")
# 向量缓存是**运行时产物**（31MB ×N），放被忽略的 core/data/。
CACHE_DIR = os.path.join(ROOT, "core", "data")

# ── 算法常量（与 SDK 默认一致）──
DEFAULT_SIMILARITY_THRESHOLD = 0.65
DEFAULT_TOP_K = 5

# VAD 预设（来自 SDK 的 EmotionPresetRegistry.ts）
EMOTION_VAD_PRESETS: dict[str, tuple[float, float, float]] = {
    "neutral":     (0.00,  0.00,  0.00),
    "calm":        (0.25, -0.45,  0.20),
    "happy":       (0.75,  0.45,  0.35),
    "excited":     (0.85,  0.85,  0.45),
    "shy":         (0.35,  0.60, -0.45),
    "affectionate": (0.65,  0.10,  0.10),
    "curious":     (0.35,  0.55,  0.20),
    "confused":    (-0.10,  0.35, -0.30),
    "tired":       (-0.25, -0.70, -0.30),
    "sad":         (-0.65, -0.45, -0.50),
    "anxiety":     (-0.60,  0.70, -0.55),
    "anger":       (-0.70,  0.75,  0.55),
    "angry":       (-0.70,  0.75,  0.55),   # 别名：语料里两种写法都出现过
    "concerned":   (-0.18,  0.28, -0.20),
    "surprised":   (0.18,  0.78, -0.08),
}


@dataclass
class ClassificationResult:
    """一次分类的结果。"""

    emotion: str = "neutral"
    intensity: float = 0.3
    confidence: float = 0.0
    similarity: float = -1.0
    vad: tuple[float, float, float] = (0.0, 0.0, 0.0)
    source: str = "fallback"          # exact | embedding | neutral | fallback
    matched: list[tuple[str, str, float]] = field(default_factory=list)  # (文本, 类别, 相似度)
    scores: list[tuple[str, float]] = field(default_factory=list)        # (类别, share)
    elapsed_ms: float = 0.0

    def as_line(self) -> str:
        v, a, d = self.vad
        return (f"{self.emotion}({self.confidence:.2f}) "
                f"VAD[{v:+.2f},{a:+.2f},{d:+.2f}] src={self.source}")


def _cosine(a: list[float], b: list[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


class EmotionClassifier:
    """embedding 近邻情绪分类器。

    Args:
        provider: 实现 ``embed_texts(list[str]) -> list[list[float] | None]``
            的对象（如 ``core.memory_embedding_api.ApiEmbeddingProvider``）。
            传 ``None`` 时 ``available()`` 为 False，``classify()`` 走兜底。
        corpus_path: 语料 JSON 路径。
        extra_corpus: 额外语料 ``{类别: [文本, ...]}``——用于补桌宠视角语料。
        threshold: 相似度阈值，低于此值判 neutral。
        top_k: 参与投票的最近邻数。
        cache_path: 语料向量缓存路径（避免每次重启重嵌 1400 条）。
    """

    def __init__(
        self,
        provider: Any = None,
        corpus_path: str = CORPUS_PATH,
        extra_corpus: Optional[dict[str, list[str]]] = None,
        threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        top_k: int = DEFAULT_TOP_K,
        cache_path: Optional[str] = None,
    ) -> None:
        self._provider = provider
        self._corpus_path = corpus_path
        self._extra_corpus = extra_corpus or {}
        self._threshold = threshold
        self._top_k = max(1, top_k)
        self._cache_path = cache_path

        self._texts: list[str] = []
        self._labels: list[str] = []
        self._intensities: list[float] = []
        self._vecs: list[list[float]] = []
        self._ready = False
        self._lock = threading.Lock()
        self._load_error: str = ""

    # ── 生命周期 ────────────────────────────────────────────
    def available(self) -> bool:
        """provider 是否配置齐备（不发网络请求）。"""
        if self._provider is None:
            return False
        fn = getattr(self._provider, "is_available", None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:  # noqa: BLE001
                return False
        return True

    def prepare(self, force: bool = False) -> bool:
        """加载语料 + 向量（首次会打 embedding API，之后走缓存）。

        Returns:
            是否就绪。失败不抛异常——调用方据 ``available()`` 决定降级。
        """
        with self._lock:
            if self._ready and not force:
                return True
            if not self.available():
                self._load_error = "provider 不可用"
                return False
            try:
                self._load_corpus()
                self._load_or_build_vectors(force=force)
                self._ready = True
                self._load_error = ""
                logger.info(
                    "EmotionClassifier 就绪：%d 条语料 / %d 类",
                    len(self._texts), len(set(self._labels)))
                return True
            except Exception as exc:  # noqa: BLE001
                self._load_error = str(exc)
                logger.warning("EmotionClassifier 初始化失败：%s", exc)
                return False

    def _load_corpus(self) -> None:
        """读语料 + 强度区间；叠加 extra_corpus。"""
        with open(self._corpus_path, encoding="utf-8") as f:
            data = json.load(f)
        corpus: dict[str, list[str]] = dict(data.get("corpus") or {})
        defaults: dict[str, dict] = dict(data.get("defaults") or {})

        # 叠加额外语料（桌宠视角等）
        for cat, items in self._extra_corpus.items():
            corpus.setdefault(cat, [])
            corpus[cat] = list(corpus[cat]) + [t for t in items if t]

        texts: list[str] = []
        labels: list[str] = []
        intensities: list[float] = []
        for cat, items in corpus.items():
            rng = (defaults.get(cat) or {}).get("intensity") or [0.5, 0.5]
            lo, hi = float(rng[0]), float(rng[1])
            n = len(items)
            for i, t in enumerate(items):
                texts.append(t)
                labels.append(cat)
                # 与 SDK 一致：按语料顺序在区间内线性插值
                intensities.append(lo if n <= 1 else lo + (hi - lo) * i / (n - 1))
        self._texts, self._labels, self._intensities = texts, labels, intensities

    def _load_or_build_vectors(self, force: bool = False) -> None:
        """向量化语料（优先读缓存）。"""
        cache_ok = False
        if self._cache_path and not force and os.path.exists(self._cache_path):
            try:
                cached = json.load(open(self._cache_path, encoding="utf-8"))
                if (cached.get("labels") == self._labels
                        and len(cached.get("vecs") or []) == len(self._texts)):
                    self._vecs = cached["vecs"]
                    cache_ok = True
                    logger.info("情绪语料向量命中缓存（%d 条）", len(self._vecs))
            except Exception as exc:  # noqa: BLE001
                logger.debug("语料向量缓存读取失败，将重建：%s", exc)

        if cache_ok:
            return

        raw = self._provider.embed_texts(self._texts)
        missing = [i for i, v in enumerate(raw) if not v]
        # 重试缺失项（远程 API 批量大时偶发超时）
        for attempt in range(3):
            if not missing:
                break
            time.sleep(1.5)
            logger.info("情绪语料嵌入重试 %d/3：补 %d 条", attempt + 1, len(missing))
            still = []
            for i in missing:
                got = self._provider.embed_texts([self._texts[i]])[0]
                if got:
                    raw[i] = got
                else:
                    still.append(i)
            missing = still
        if missing:
            raise RuntimeError(f"{len(missing)} 条语料未能嵌入")
        self._vecs = [v for v in raw]

        if self._cache_path:
            try:
                os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
                json.dump({"labels": self._labels, "vecs": self._vecs},
                          open(self._cache_path, "w", encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("语料向量缓存写入失败：%s", exc)

    # ── 分类 ────────────────────────────────────────────────
    def classify(self, text: str) -> ClassificationResult:
        """分类一段文本。**永不抛异常**，最差返回 neutral。"""
        t0 = time.perf_counter()

        def done(r: ClassificationResult) -> ClassificationResult:
            r.elapsed_ms = (time.perf_counter() - t0) * 1000
            return r

        if not text or not str(text).strip():
            return done(ClassificationResult(emotion="neutral", source="neutral"))

        if not self._ready and not self.prepare():
            return done(ClassificationResult(
                emotion="neutral", source="fallback",
                scores=[(f"未就绪：{self._load_error}", 0.0)]))

        # 精确命中（规范化后完全相同）→ 直接返回该条
        norm = str(text).strip()
        for i, t in enumerate(self._texts):
            if t == norm:
                lab = self._labels[i]
                return done(ClassificationResult(
                    emotion=lab,
                    intensity=self._intensities[i],
                    confidence=1.0,
                    similarity=1.0,
                    vad=self._vad_of(lab),
                    source="exact",
                    matched=[(t, lab, 1.0)],
                    scores=[(lab, 1.0)],
                ))

        try:
            qv = self._embed_query(norm)
        except Exception as exc:  # noqa: BLE001
            logger.debug("查询嵌入失败：%s", exc)
            qv = None
        if not qv:
            return done(ClassificationResult(
                emotion="neutral", source="fallback",
                scores=[("查询嵌入失败", 0.0)]))

        # top-K 近邻
        scored = sorted(((_cosine(qv, v), i) for i, v in enumerate(self._vecs)),
                        key=lambda x: x[0], reverse=True)[: self._top_k]
        best_sim = scored[0][0] if scored else -1.0

        if best_sim <= self._threshold:
            return done(ClassificationResult(
                emotion="neutral", source="neutral", similarity=best_sim,
                vad=self._vad_of("neutral"),
                matched=[(self._texts[i], self._labels[i], s) for s, i in scored],
                scores=[("相似度不足阈值", 0.0)]))

        # 相似度加权投票
        accepted = [(s, i) for s, i in scored if s > self._threshold]
        weights = {i: self._weight(s) for s, i in accepted}
        score_map: dict[str, float] = {}
        for _s, i in accepted:
            lab = self._labels[i]
            score_map[lab] = score_map.get(lab, 0.0) + weights[i]
        total = sum(score_map.values()) or 1.0
        scores = sorted(((k, v / total) for k, v in score_map.items()),
                        key=lambda x: x[1], reverse=True)
        winner = scores[0][0]

        # 强度：同类近邻的加权平均
        same = [(s, i) for s, i in accepted if self._labels[i] == winner]
        wsum = sum(weights[i] for _s, i in same) or 1.0
        intensity = sum(self._intensities[i] * weights[i] for _s, i in same) / wsum

        # 置信度：相似度余量 0.55 + 投票占比 0.45（与 SDK 一致）
        sim_conf = _clamp01((best_sim - self._threshold)
                            / max(1e-6, 1 - self._threshold))
        confidence = _clamp01(sim_conf * 0.55 + scores[0][1] * 0.45)

        return done(ClassificationResult(
            emotion=winner,
            intensity=_clamp01(intensity),
            confidence=confidence,
            similarity=best_sim,
            vad=self._vad_of(winner),
            source="embedding",
            matched=[(self._texts[i], self._labels[i], s) for s, i in accepted],
            scores=scores,
        ))

    def _embed_query(self, text: str, tries: int = 3) -> Optional[list[float]]:
        """嵌入单条查询文本（带重试）。

        为什么需要：实测远程 embedding API 偶发 8s 超时，
        不重试会让分类结果变成「查询嵌入失败」——那是**基础设施抖动**，
        不是分类器判错，但会让验证数字变假（实测踩到 2 条）。
        """
        for attempt in range(tries):
            try:
                v = self._provider.embed_texts([text])[0]
            except Exception as exc:  # noqa: BLE001
                logger.debug("查询嵌入第 %d 次失败：%s", attempt + 1, exc)
                v = None
            if v:
                return v
            if attempt < tries - 1:
                time.sleep(1.2)
        return None

    def _weight(self, sim: float) -> float:
        """相似度 → 投票权重（与 SDK 的 similarityWeight 一致）。"""
        if sim <= self._threshold:
            return 0.0
        return max(1e-6, (sim - self._threshold) / max(1e-6, 1 - self._threshold))

    @staticmethod
    def _vad_of(emotion: str) -> tuple[float, float, float]:
        return EMOTION_VAD_PRESETS.get(emotion, (0.0, 0.0, 0.0))

    # ── 观测 ────────────────────────────────────────────────
    def status(self) -> dict:
        return {
            "ready": self._ready,
            "available": self.available(),
            "corpus_size": len(self._texts),
            "emotions": sorted(set(self._labels)),
            "threshold": self._threshold,
            "top_k": self._top_k,
            "error": self._load_error,
        }


# ── 单例 ────────────────────────────────────────────────────
# 键是 (name, extra_corpus 指纹)。
#
# 2026-09-20 修复：原来只按 name 缓存（`_INSTANCES.get(name)`），
# 于是**同一视角的两种变体互相踩**：
#   `get_classifier('pet')`            ← 无桌宠语料（1400 条）
#   `get_classifier('pet', extra=...)` ← 有桌宠语料（1474 条）
# 谁先调谁定义内存实例，且两者写**同一个缓存文件**——后调的发现
# labels 不匹配 → 重建 → 实测 154 秒（卡在 embedding API 超时重试）。
# 正确性没错（labels 校验会触发重建，不会用错结果），但性能被反复打穿。
_INSTANCES: dict[tuple, EmotionClassifier] = {}
_INST_LOCK = threading.Lock()


def _extra_fingerprint(extra: Optional[dict[str, list[str]]]) -> tuple:
    """extra_corpus 的轻量指纹（只取结构，不哈希全量文本）。"""
    if not extra:
        return ()
    return tuple(sorted((k, len(v or [])) for k, v in extra.items()))


def get_classifier(
    name: str = "user",
    extra_corpus: Optional[dict[str, list[str]]] = None,
) -> EmotionClassifier:
    """按视角取分类器单例。

    Args:
        name: ``"user"``（用户消息）/ ``"pet"``（桌宠回复）——
            仅用于缓存键与缓存文件命名；两者共用语料，
            差异由 ``extra_corpus`` 与阈值体现。
        extra_corpus: 额外语料（如桌宠视角补充语料）。
            **参与缓存键**：同一 name 传不同 extra 得到不同实例，
            不再互相覆盖（2026-09-20 修复）。
    """
    key = (name, _extra_fingerprint(extra_corpus))
    with _INST_LOCK:
        inst = _INSTANCES.get(key)
        if inst is not None:
            return inst
        provider = None
        try:
            from core.memory_embedding_api import default_api_embedding_provider
            provider = default_api_embedding_provider()
        except Exception as exc:  # noqa: BLE001
            logger.debug("取 embedding provider 失败：%s", exc)
        cache = os.path.join(CACHE_DIR, f"emotion_corpus_vecs_{name}.json")
        inst = EmotionClassifier(
            provider=provider,
            extra_corpus=extra_corpus,
            cache_path=cache,
        )
        _INSTANCES[key] = inst
        return inst
