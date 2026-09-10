# -*- coding: utf-8 -*-
"""P1-1 本地 ONNX EmbeddingService 单元测试（验收标准见 docs/migration-neko-port-plan.md）。

验收覆盖：
1. 无模型（disabled / model 缺失）→ embedding_available=False，
   HybridMemoryRecall 自动降级纯 BM25，不报错（fallback gate）
2. mock 模型（固定向量）→ cosine+RRF 提升语义近但关键词不同的 doc
3. 服务进程级单例（get_embedding_service 唯一 + reset 可重建）
4. 并发调用线程安全（并发首次加载单飞 + 多线程 embed_texts 结果对齐）
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.memory_embedding import (
    EmbeddingService,
    get_embedding_service,
    reset_embedding_service_for_tests,
)
from core.memory_hybrid import HybridMemoryRecall

# ── 5 条中文场景（与 test_memory_hybrid.py 同款验收用例）────────────────

DOCS = [
    {"id": "late_night_work", "text": "深夜加班 写代码 重构事件流模块 深夜 工作"},
    {"id": "gaming_weekend", "text": "周末打游戏 娱乐 深夜 游戏 联机"},
    {"id": "video_watching", "text": "看视频 追剧 娱乐 周末"},
    {"id": "tutorial_follow", "text": "学习教程 学新框架 看文档 学习"},
    {"id": "chat_idle", "text": "安静发呆 聊天 沟通 摸鱼"},
]


class MockEmbeddingService(EmbeddingService):
    """测试替身：固定向量映射替代真实 ONNX 推理。

    仍走真实 EmbeddingService 代码路径：is_available → 懒加载（单飞线程池）
    → embed_texts（独立线程池 + 超时）→ cosine_rank → RRF。
    """

    def __init__(self, vec_map: dict, **kwargs):
        kwargs.setdefault("enabled", True)
        kwargs.setdefault("model_path", "__mock__")
        super().__init__(**kwargs)
        self._vec_map = vec_map

    def _load_blocking(self) -> None:
        # mock：不碰真实 onnxruntime/tokenizers，仅占位让状态机进入 READY
        self._session = object()
        self._tokenizer = object()

    def _infer_blocking(self, texts: list[str]) -> list[list[float]]:
        default_vec = self._vec_map.get("__other__", [0.0, 0.0, 0.0, 0.0, 0.0])
        return [list(self._vec_map.get(t, default_vec)) for t in texts]


def _semantic_service() -> MockEmbeddingService:
    """query「深夜加班」语义最近的是「项目交付冲刺 凌晨两点睡 压力很大」。"""
    vec_sem = [1.0, 0.0, 0.0, 0.0, 0.0]
    vec_orth = [0.0, 1.0, 0.0, 0.0, 0.0]
    return MockEmbeddingService(
        vec_map={
            "深夜加班": vec_sem,
            "项目交付冲刺 凌晨两点睡 压力很大": vec_sem,
            "__other__": vec_orth,
        },
    )


# ── 1. 无模型降级（fallback gate）─────────────────────────────────────


def test_disabled_service_falls_back_to_bm25():
    """enabled=False → embedding_available=False，纯 BM25 不报错（验收 1）。"""
    svc = EmbeddingService(enabled=False)
    assert svc.is_available() is False
    assert svc.disable_reason() == "user_disabled_via_config"
    assert svc.embed_texts(["深夜加班"]) == [None]
    assert svc.embed("深夜加班") is None

    recall = HybridMemoryRecall(hybrid_enabled=True, embedding_provider=svc)
    assert recall.embedding_available is False
    hits = recall.recall("深夜加班", DOCS)
    assert hits, "无 embedding 也应正常返回 BM25 结果"
    assert hits[0]["id"] == "late_night_work"


def test_missing_model_disables_cleanly():
    """model_path 不存在 → 懒加载后 sticky DISABLED，不抛异常（验收 1）。"""
    svc = EmbeddingService(
        enabled=True, model_path="C:/definitely/not/here/model.onnx",
    )
    assert svc.is_available() is False
    assert svc.disable_reason() == "model_file_missing"
    assert svc.is_disabled() is True
    assert svc.embed_texts(["深夜加班"]) == [None]
    assert svc.embed("深夜加班") is None

    # HybridMemoryRecall 用它 → 自动降级纯 BM25
    recall = HybridMemoryRecall(hybrid_enabled=True, embedding_provider=svc)
    assert recall.embedding_available is False
    hits = recall.recall("深夜加班", DOCS)
    assert hits and hits[0]["id"] == "late_night_work"


def test_model_path_none_disables():
    """未配置 model_path（默认空串）→ 同样干净降级。"""
    svc = EmbeddingService(enabled=True, model_path="")
    assert svc.is_available() is False
    assert svc.disable_reason() == "model_file_missing"


# ── 2. mock 模型 → cosine+RRF 提升 ────────────────────────────────────


def test_mock_model_embed_single():
    """embed(text) 返回固定向量（mock 模型）。"""
    svc = _semantic_service()
    assert svc.is_available() is True
    assert svc.embed("深夜加班") == [1.0, 0.0, 0.0, 0.0, 0.0]
    assert svc.embed("") is None


def test_mock_model_rrf_lifts_semantic_near_keyword_different():
    """mock 模型：语义近但关键词不同的 doc 经 RRF 提升（验收 2）。"""
    semantic_docs = DOCS + [
        {"id": "deadline_crunch", "text": "项目交付冲刺 凌晨两点睡 压力很大"},
    ]
    svc = _semantic_service()

    # 无 embedding：纯 BM25 → deadline_crunch 不在结果里
    bm25_only = HybridMemoryRecall(hybrid_enabled=True, embedding_provider=None)
    ids_without_emb = {h["id"] for h in bm25_only.recall("深夜加班", semantic_docs)}
    assert "deadline_crunch" not in ids_without_emb, "零词重叠 doc 不应被 BM25 命中"

    # 有 embedding（真实 EmbeddingService mock）：RRF 融合 → 提升进结果
    hybrid = HybridMemoryRecall(
        hybrid_enabled=True, embedding_provider=svc, budget_total=10,
    )
    assert hybrid.embedding_available is True
    hits = hybrid.recall("深夜加班", semantic_docs)
    ids_with_emb = {h["id"] for h in hits}
    assert "deadline_crunch" in ids_with_emb, "RRF 应把语义近场景提升进结果"
    d = next(h for h in hits if h["id"] == "deadline_crunch")
    assert d.get("_rrf_score", 0.0) > 0.0, "RRF 得分应为正"

    # 关键词强信号仍领先
    assert hits[0]["id"] == "late_night_work"


def test_mock_model_cosine_uses_cached_embedding():
    """doc 缓存向量优先：不重复现嵌（批量路径只嵌入缺失 doc）。"""
    svc = _semantic_service()
    assert svc.is_available() is True

    calls: list[list[str]] = []
    original_embed = svc.embed_texts

    def spy(texts: list[str]) -> list[list[float] | None]:
        calls.append(list(texts))
        return original_embed(texts)

    svc.embed_texts = spy

    docs = [
        {"id": "a", "text": "深夜加班 写代码", "embedding": [1.0, 0.0, 0.0, 0.0, 0.0]},
        {"id": "b", "text": "周末打游戏 娱乐"},
    ]
    from core.memory_hybrid import cosine_rank
    ranked = cosine_rank("深夜加班", docs, svc)
    assert ranked and ranked[0][0]["id"] == "a"
    # 只发生 2 次嵌入调用：query + 缺失向量 doc（b 的 text，一次批量）
    assert len(calls) == 2
    assert calls[0] == ["深夜加班"]
    assert calls[1] == ["周末打游戏 娱乐"]


# ── 3. 进程级单例 ─────────────────────────────────────────────────────


def test_singleton_identity_and_reset():
    """get_embedding_service 进程级唯一；reset 后可重建且仍单例（验收 3）。"""
    reset_embedding_service_for_tests()
    try:
        a = get_embedding_service()
        b = get_embedding_service()
        assert a is b
    finally:
        reset_embedding_service_for_tests()
    c = get_embedding_service()
    d = get_embedding_service()
    assert c is d, "重建后仍是单例"
    reset_embedding_service_for_tests()


# ── 4. 并发线程安全 ───────────────────────────────────────────────────


def test_concurrent_first_load_is_single_flight():
    """并发首次 is_available()：单飞加载，所有调用方最终一致（验收 4）。"""
    svc = _semantic_service()  # state INIT
    results: list[tuple[bool, list[list[float] | None]]] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            ok = svc.is_available()
            vecs = svc.embed_texts(["深夜加班"])
            with lock:
                results.append((ok, vecs))
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发加载出现异常: {errors}"
    assert len(results) == 4
    for ok, vecs in results:
        assert ok is True, "所有调用方都应观察到就绪"
        assert vecs[0] == [1.0, 0.0, 0.0, 0.0, 0.0]


def test_concurrent_embed_thread_safe():
    """多线程 embed_texts：不炸、结果对齐、空文本 None 占位（验收 4）。"""
    svc = _semantic_service()
    assert svc.is_available() is True  # 先加载，聚焦推理并发

    texts = ["深夜加班", "周末打游戏 娱乐 深夜 游戏 联机", ""]
    n = 8
    results: list[list[list[float] | None]] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            vecs = svc.embed_texts(texts)
            with lock:
                results.append(vecs)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发调用出现异常: {errors}"
    assert len(results) == n
    for vecs in results:
        assert len(vecs) == 3
        assert vecs[0] == [1.0, 0.0, 0.0, 0.0, 0.0]
        assert vecs[1] == [0.0, 1.0, 0.0, 0.0, 0.0]
        assert vecs[2] is None  # 空文本 → None 占位


def test_submit_embed_texts_async_callback():
    """异步路径不阻塞调用方，回调收到正确结果（主线程友好）。"""
    svc = _semantic_service()
    assert svc.is_available() is True

    done: list[list[list[float] | None]] = []
    lock = threading.Lock()

    def on_done(vecs: list[list[float] | None]) -> None:
        with lock:
            done.append(vecs)

    svc.submit_embed_texts(["深夜加班", ""], on_done=on_done)

    # 轮询等待回调（最多 5s；mock 推理立即完成）
    deadline = 5.0
    import time
    start = time.monotonic()
    while time.monotonic() - start < deadline:
        with lock:
            if done:
                break
        time.sleep(0.02)
    with lock:
        assert done, "异步回调未触发"
        assert done[0][0] == [1.0, 0.0, 0.0, 0.0, 0.0]
        assert done[0][1] is None
