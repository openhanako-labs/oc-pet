# -*- coding: utf-8 -*-
"""API embedding provider 单元测试。

覆盖：
1. 未配置（缺 base_url/key/model）→ is_available False，embed_texts 全 None
2. 正常返回：按 index 归位（乱序也能对位）
3. 未给 index：按顺序兜底
4. 批处理按 batch_size 切分
5. 网络/服务异常 → 该批 None，不上抛（fallback gate）
6. 配置缺 enabled / 缺 api 段 → default_api_embedding_provider 返回 None
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.memory_embedding_api as mea
from core.memory_embedding_api import ApiEmbeddingProvider


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_unavailable_without_config():
    p = ApiEmbeddingProvider()
    assert p.is_available() is False
    assert p.embed_texts(["a", "b"]) == [None, None]


def test_empty_input_returns_empty():
    p = ApiEmbeddingProvider(base_url="https://x/v1", api_key="k", model="m")
    assert p.embed_texts([]) == []


def test_index_reorder(monkeypatch):
    p = ApiEmbeddingProvider(base_url="https://x/v1", api_key="k", model="m")

    def fake_post(url, headers=None, json=None, timeout=None):
        assert url.endswith("/embeddings")
        assert json["model"] == "m"
        assert json["input"] == ["a", "b"]
        return _FakeResp({"data": [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]})

    monkeypatch.setattr(mea.requests, "post", fake_post)
    assert p.embed_texts(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]


def test_no_index_fallback_order(monkeypatch):
    p = ApiEmbeddingProvider(base_url="https://x/v1", api_key="k", model="m")

    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResp({"data": [
            {"embedding": [1.0]},
            {"embedding": [2.0]},
        ]})

    monkeypatch.setattr(mea.requests, "post", fake_post)
    assert p.embed_texts(["a", "b"]) == [[1.0], [2.0]]


def test_batching_splits(monkeypatch):
    p = ApiEmbeddingProvider(base_url="https://x/v1", api_key="k", model="m", batch_size=2)
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(list(json["input"]))
        return _FakeResp({"data": [
            {"index": i, "embedding": [float(i)]} for i in range(len(json["input"]))
        ]})

    monkeypatch.setattr(mea.requests, "post", fake_post)
    out = p.embed_texts(["a", "b", "c"])
    assert len(calls) == 2
    assert out == [[0.0], [1.0], [0.0]]


def test_network_error_returns_none(monkeypatch):
    p = ApiEmbeddingProvider(base_url="https://x/v1", api_key="k", model="m")

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(mea.requests, "post", boom)
    assert p.embed_texts(["a", "b"]) == [None, None]


def test_cache_avoids_repeat_calls(monkeypatch):
    p = ApiEmbeddingProvider(base_url="https://x/v1", api_key="k", model="m")
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(list(json["input"]))
        return _FakeResp({"data": [
            {"index": i, "embedding": [float(i), 0.0]} for i in range(len(json["input"]))
        ]})

    monkeypatch.setattr(mea.requests, "post", fake_post)
    first = p.embed_texts(["a", "b"])
    second = p.embed_texts(["a", "b"])
    assert first == second
    assert len(calls) == 1, "同文本二次调用应命中缓存，不再请求"
    p.embed_texts(["a", "c"])
    assert calls[-1] == ["c"], "只应请求未命中的文本"


def test_cache_does_not_store_failures(monkeypatch):
    p = ApiEmbeddingProvider(base_url="https://x/v1", api_key="k", model="m")
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(list(json["input"]))
        return _FakeResp({"data": []})  # 空 → 该批 None

    monkeypatch.setattr(mea.requests, "post", fake_post)
    assert p.embed_texts(["a"]) == [None]
    assert p.embed_texts(["a"]) == [None]
    assert len(calls) == 2, "失败结果不应缓存，应重试"


def test_factory_disabled(monkeypatch):
    monkeypatch.setattr(mea, "_embedding_config", lambda: {"enabled": False, "api": {}})
    assert mea.default_api_embedding_provider() is None


def test_factory_enabled_but_missing_key(monkeypatch):
    monkeypatch.setattr(mea, "_embedding_config", lambda: {
        "enabled": True,
        "api": {"base_url": "https://x/v1", "model": "m"},  # 缺 api_key
    })
    assert mea.default_api_embedding_provider() is None


def test_factory_enabled_and_configured(monkeypatch):
    monkeypatch.setattr(mea, "_embedding_config", lambda: {
        "enabled": True,
        "api": {"base_url": "https://x/v1", "api_key": "k", "model": "m", "dim": 3},
    })
    p = mea.default_api_embedding_provider()
    assert p is not None and p.is_available() is True


def test_hybrid_factory_selects_api(monkeypatch):
    import core.memory_hybrid as mh
    emb = {"provider": "api", "enabled": True,
           "api": {"base_url": "https://x/v1", "api_key": "k", "model": "m"}}
    monkeypatch.setattr(mh, "_memory_config", lambda: {"embedding": emb})
    monkeypatch.setattr(mea, "_embedding_config", lambda: emb)
    p = mh._default_embedding_provider()
    assert p is not None and p.is_available() is True


def test_hybrid_factory_local_disabled_returns_none(monkeypatch):
    import core.memory_hybrid as mh
    import core.memory_embedding as me
    monkeypatch.setattr(mh, "_memory_config", lambda: {"embedding": {"provider": "local", "enabled": False}})
    monkeypatch.setattr(me, "_embedding_config", lambda: {"enabled": False})
    assert mh._default_embedding_provider() is None


def test_hybrid_factory_api_unconfigured_returns_none(monkeypatch):
    import core.memory_hybrid as mh
    emb = {"provider": "api", "enabled": True, "api": {}}
    monkeypatch.setattr(mh, "_memory_config", lambda: {"embedding": emb})
    monkeypatch.setattr(mea, "_embedding_config", lambda: emb)
    assert mh._default_embedding_provider() is None


def test_resolve_from_catalog_by_provider(monkeypatch):
    monkeypatch.setattr(mea, "_load_catalog_providers", lambda: {
        "siliconflow": {"base_url": "https://api.siliconflow.cn/v1", "api_key": "k1",
                         "models": ["BAAI/bge-m3"]},
    })
    assert mea._resolve_from_catalog("siliconflow", "") == ("https://api.siliconflow.cn/v1", "k1")


def test_resolve_from_catalog_by_model(monkeypatch):
    monkeypatch.setattr(mea, "_load_catalog_providers", lambda: {
        "x": {"base_url": "https://x/v1", "api_key": "k2", "models": [{"id": "BAAI/bge-m3"}]},
    })
    assert mea._resolve_from_catalog("", "BAAI/bge-m3") == ("https://x/v1", "k2")


def test_resolve_from_catalog_miss(monkeypatch):
    monkeypatch.setattr(mea, "_load_catalog_providers", lambda: {})
    assert mea._resolve_from_catalog("nope", "BAAI/bge-m3") is None


def test_factory_resolves_key_from_catalog(monkeypatch):
    monkeypatch.setattr(mea, "_embedding_config", lambda: {
        "enabled": True,
        "api": {"provider": "siliconflow", "model": "BAAI/bge-m3", "dim": 1024},
    })
    monkeypatch.setattr(mea, "_load_catalog_providers", lambda: {
        "siliconflow": {"base_url": "https://api.siliconflow.cn/v1", "api_key": "k1"},
    })
    p = mea.default_api_embedding_provider()
    assert p is not None and p.is_available() is True
