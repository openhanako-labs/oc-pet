"""远程 embedding provider（OpenAI 兼容 ``/v1/embeddings``）。

为 ``memory_hybrid`` 的混合召回提供「API 向量」路线，替代本地 ONNX 模型：
无需下载模型、不依赖特定 onnxruntime 版本。实现 ``EmbeddingProvider`` 协议
（``is_available`` / ``embed_texts``），行为对齐 ``memory_embedding``：

- **永不抛异常**；未配置 / 网络失败 / 超时 → 返回与输入等长的 ``[None]*n``，
  调用方 ``cosine_rank`` 自动走 fallback gate 退化为纯 BM25。
- **不做客户端归一 / 截断**：``memory_hybrid.cosine_similarity`` 自带归一，
  且要求 query 与 doc 同维；客户端截断会让不同来源向量维度不一致 → 直接用
  服务端原样返回的向量。

配置（``config.json`` → ``memory.embedding``）::

    "provider": "api",
    "api": {
        "base_url": "https://api.siliconflow.cn/v1",   # 不含 /embeddings
        "api_key": "sk-...",
        "model": "BAAI/bge-m3",
        "dim": 1024,          # 仅做维度一致性校验；0 = 不校验
        "timeout_seconds": 8.0,
        "batch_size": 32
    }
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 8.0
DEFAULT_BATCH = 32
DEFAULT_CACHE = 4096   # 文本→向量有界缓存条数（止住 recall 反复重嵌同一批 doc）


def _embedding_config() -> dict:
    """读取 config ``memory.embedding`` 段；失败返回空 dict。"""
    try:
        from config import load_config

        cfg = load_config() or {}
        return dict((cfg.get("memory", {}) or {}).get("embedding", {}) or {})
    except Exception as exc:  # noqa: BLE001
        logger.debug("[memory_embedding_api] 读取 embedding 配置失败: %s", exc)
        return {}


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        f = float(value)
        return f if f > 0 else default
    except (TypeError, ValueError):
        return default


class ApiEmbeddingProvider:
    """OpenAI 兼容 ``/v1/embeddings`` 远程向量 provider（实现 ``EmbeddingProvider``）。"""

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        dim: int = 0,
        timeout: float = DEFAULT_TIMEOUT,
        batch_size: int = DEFAULT_BATCH,
        cache_size: int = DEFAULT_CACHE,
    ) -> None:
        self._base_url = str(base_url or "").strip().rstrip("/")
        self._api_key = str(api_key or "").strip()
        self._model = str(model or "").strip()
        self._dim = _as_int(dim, 0) if dim else 0
        self._timeout = _as_float(timeout, DEFAULT_TIMEOUT)
        self._batch_size = max(1, _as_int(batch_size, DEFAULT_BATCH))
        self._dim_warned = False
        # 文本→向量有界缓存（LRU）：recall 每次用同一批 doc 文本，缓存后
        # 不重复打 API（本地/远程同受益）。
        self._cache: "OrderedDict[str, list[float]]" = OrderedDict()
        self._cache_lock = threading.Lock()
        self._cache_max = max(64, _as_int(cache_size, DEFAULT_CACHE))

    # ── EmbeddingProvider 协议 ──────────────────────────────────────────
    def is_available(self) -> bool:
        """base_url / api_key / model 三者齐备即视为可用（不发起网络请求）。"""
        return bool(self._base_url and self._api_key and self._model)

    def embed_texts(self, texts: list[str]) -> list[list[float] | None]:
        """批量嵌入；返回与输入等长的 list，失败位为 ``None``，**绝不上抛**。

        先查文本缓存（命中直接返回，不打 API）；未命中的按 ``batch_size`` 切批
        请求，成功结果写回有界 LRU 缓存（doc 文本不变时后续召回全命中）。
        """
        if not texts:
            return []
        if not self.is_available():
            return [None] * len(texts)

        norm = [t if isinstance(t, str) else str(t if t is not None else "") for t in texts]
        out: list[list[float] | None] = [None] * len(norm)
        missing: list[int] = []
        with self._cache_lock:
            for i, key in enumerate(norm):
                hit = self._cache.get(key)
                if hit is not None:
                    self._cache.move_to_end(key)
                    out[i] = hit
                else:
                    missing.append(i)
        if not missing:
            return out

        url = f"{self._base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        for start in range(0, len(missing), self._batch_size):
            idxs = missing[start:start + self._batch_size]
            chunk = [norm[i] for i in idxs]
            batch: list[list[float] | None] = [None] * len(chunk)
            try:
                resp = requests.post(
                    url,
                    headers=headers,
                    json={"model": self._model, "input": chunk},
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                data = (resp.json() or {}).get("data", []) or []
                items = [it for it in data if isinstance(it, dict)]
                indexed = [it for it in items if isinstance(it.get("index"), int)]
                if indexed:
                    # 服务端给了 index：按 index 归位（OpenAI 语义，可能乱序）
                    for it in indexed:
                        idx = it["index"]
                        emb = it.get("embedding")
                        if 0 <= idx < len(chunk) and isinstance(emb, list) and emb:
                            batch[idx] = [float(x) for x in emb]
                else:
                    # 未给 index：按返回顺序兜底
                    for i, it in enumerate(items):
                        if i >= len(chunk):
                            break
                        emb = it.get("embedding")
                        if isinstance(emb, list) and emb:
                            batch[i] = [float(x) for x in emb]
            except Exception as exc:  # noqa: BLE001 — 该批失败 → 该批走 BM25 兜底
                logger.warning(
                    "[memory_embedding_api] 批次请求失败（该批走 BM25 兜底）: %s: %s",
                    type(exc).__name__, exc,
                )
            with self._cache_lock:
                for pos, i in enumerate(idxs):
                    vec = batch[pos]
                    out[i] = vec
                    if vec:
                        self._cache[norm[i]] = vec
                        self._cache.move_to_end(norm[i])
                while len(self._cache) > self._cache_max:
                    self._cache.popitem(last=False)
            self._check_dim(batch)
        return out

    def _check_dim(self, vectors: list[list[float] | None]) -> None:
        """配置了 ``dim`` 时，一次性校验服务端返回维度是否匹配（只警告一次）。"""
        if self._dim <= 0 or self._dim_warned:
            return
        for vec in vectors:
            if vec:
                if len(vec) != self._dim:
                    logger.warning(
                        "[memory_embedding_api] 维度不匹配：配置 dim=%d，服务端返回 %d。"
                        "请把 memory.embedding.api.dim 改成模型原生维度（或设为 0 不校验）。",
                        self._dim, len(vec),
                    )
                    self._dim_warned = True
                return


def _load_catalog_providers() -> dict:
    """读取 Hana ``provider-catalog.json`` 的 ``providers`` 段；失败返回空 dict。"""
    try:
        import json

        from hanako_home import hanako_home

        path = hanako_home() / "provider-catalog.json"
        if not path.exists():
            return {}
        data = json.loads(path.read_text("utf-8"))
        return dict(data.get("providers", {}) or {})
    except Exception as exc:  # noqa: BLE001
        logger.debug("[memory_embedding_api] 读取 provider-catalog 失败: %s", exc)
        return {}


def _resolve_from_catalog(provider_name: str, model: str) -> tuple[str, str] | None:
    """从 Hana provider-catalog 解析 ``(base_url, api_key)``。

    优先按 ``provider_name`` 精确匹配；否则按 ``model`` 名在
    ``providers[*].models`` 里查找（兼容 str 与 ``{id}`` 两种格式）。
    未命中 → None（调用方置不可用，回退纯 BM25）。
    """
    providers = _load_catalog_providers()
    if not providers:
        return None
    name = str(provider_name or "").strip()
    if name and name in providers:
        p = providers.get(name) or {}
        bu, key = str(p.get("base_url", "") or ""), str(p.get("api_key", "") or "")
        if bu and key:
            return bu, key
    mdl = str(model or "").strip()
    if mdl:
        for p in providers.values():
            p = p or {}
            for m in (p.get("models") or []):
                mid = m.get("id") if isinstance(m, dict) else m
                if mid == mdl:
                    bu = str(p.get("base_url", "") or "")
                    key = str(p.get("api_key", "") or "")
                    if bu and key:
                        return bu, key
    return None


def default_api_embedding_provider() -> ApiEmbeddingProvider | None:
    """按 config 构造 API provider；未启用 / 未配置齐 → None（走纯 BM25）。

    base_url / api_key 未显式配置时，从 Hana provider-catalog 解析
    （按 ``api.provider`` 供应商名，或按 ``api.model`` 模型名自动匹配）——
    密钥不重复落盘。
    """
    cfg = _embedding_config()
    if not cfg.get("enabled"):
        return None
    api = dict(cfg.get("api", {}) or {})
    base_url = str(api.get("base_url", "") or "").strip()
    api_key = str(api.get("api_key", "") or "").strip()
    model = str(api.get("model", "") or "").strip()
    if not (base_url and api_key):
        resolved = _resolve_from_catalog(api.get("provider", ""), model)
        if resolved:
            base_url, api_key = resolved
    provider = ApiEmbeddingProvider(
        base_url=base_url,
        api_key=api_key,
        model=model,
        dim=api.get("dim", 0) or 0,
        timeout=api.get("timeout_seconds", DEFAULT_TIMEOUT),
        batch_size=api.get("batch_size", DEFAULT_BATCH),
    )
    return provider if provider.is_available() else None
