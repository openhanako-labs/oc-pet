# -*- coding: utf-8 -*-
"""本地 ONNX EmbeddingService（P1-1 语义检索向量化）。

参考 N.E.K.O. ``memory/embeddings.py`` 的 fallback gate / 生命周期思路，
按 oc-pet threading 架构重写（同步协议、独立线程池、有界超时）。参考文件
原样拷贝见 ``third_party_reference/neko/memory/embeddings.py``（Apache 2.0，
合规登记见 docs/THIRD_PARTY_NOTICES.md）。

设计（与 N.E.K.O. 的差异）
============================
- **同步协议**：实现 ``core.memory_hybrid.EmbeddingProvider`` 协议
  （``is_available()`` / ``embed_texts(texts)``），供 HybridMemoryRecall 的
  cosine 路径直接调用。
- **推理独立线程池 + 超时**：onnxruntime 推理提交到专用
  ``ThreadPoolExecutor``（单 worker，天然串行化）；同步调用方最多等待
  ``timeout_seconds``。超时不 cancel 原生调用（无法安全中断），按连续超时
  阈值 sticky-disable，避免每次调用都卡满超时（0x8001010D 约束：重 CPU
  推理不占用 Qt/COM 主线程的消息泵）。
- **懒加载**：构造时不碰磁盘、不 import onnxruntime；首次 ``is_available()``
  触发加载（单飞 + 有界等待）。缺模型文件 / 缺 onnxruntime / 缺 tokenizers /
  加载异常 → sticky DISABLED，``embedding_available=False``，HybridMemoryRecall
  自动降级纯 BM25（fallback gate，P0 行为不变）。
- **不自动下载大模型**：只读取 ``config memory.embedding.model_path`` 指定的
  本地 ONNX 文件（+ 自动推导同目录 ``tokenizer.json``）；未配置/不存在 →
  降级，绝不联网。
- **线程安全**：加载用 ``threading.Lock`` 单飞（观察者轮询等 READY）；
  session/tokenizer 访问用 ``threading.RLock`` 串行化
  （``tokenizers.Tokenizer.encode_batch`` 无线程安全保证）。

模型说明（参考 N.E.K.O. 事实）
================================
N.E.K.O. 默认 profile ``local-text-retrieval-v1`` 对应 HuggingFace 模型
``jinaai/jina-embeddings-v5-text-nano-retrieval``（revision
``ac5d898c8d382b17167c33e5c8af644a3519b47d``），官方导出 ONNX 布局：
``onnx/model_quantized.onnx``（int8，默认）/ ``onnx/model.onnx``（fp32），
配 ``tokenizer.json``；Matryoshka 维度 32~768，默认 256d。oc-pet
**不捆绑 / 不自动下载**该模型，仅把 ``model_path`` 指向用户自备文件即可。
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from concurrent import futures
from typing import Callable

logger = logging.getLogger(__name__)

# ── 默认配置（与 config.py memory.embedding 段对齐；模块内兜底便于裸测试）──

DEFAULT_DIM = 256
DEFAULT_QUANTIZATION = "auto"            # "auto" | "int8" | "fp32"
DEFAULT_MAX_LENGTH = 1024
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_LOAD_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_CONSECUTIVE_TIMEOUTS = 3
DEFAULT_BATCH_SIZE = 8

_DIM_STEPS = (32, 64, 128, 256, 512, 768)
# Matryoshka "auto" 维度：按可用内存选档（psutil 缺失时退回 DEFAULT_DIM）
_AUTO_DIM_RAM_BANDS = ((8.0, 512), (4.0, 256), (0.0, 128))


class EmbeddingState(enum.Enum):
    """服务生命周期。DISABLED 是粘性的：一旦关闭，本进程内不再重试。"""
    INIT = "init"
    LOADING = "loading"
    READY = "ready"
    DISABLED = "disabled"
    CLOSED = "closed"


class DisableReason(enum.Enum):
    """为什么 ``is_available()`` 为 False——日志区分「用户关」与「装不上」。"""
    NONE = "none"
    USER_DISABLED = "user_disabled_via_config"
    NO_ONNXRUNTIME = "onnxruntime_not_importable"
    NO_TOKENIZERS = "tokenizers_not_importable"
    NO_MODEL_FILE = "model_file_missing"
    TRUNCATION_SETUP_FAILED = "tokenizer_truncation_setup_failed"
    LOAD_ERROR = "load_raised"
    LOAD_TIMEOUT = "load_timed_out"
    INFERENCE_ERROR = "inference_raised"
    INFERENCE_TIMEOUT = "inference_timed_out"


# ── 纯函数小工具 ──────────────────────────────────────────────────────


def _is_nonempty_file(path: str) -> bool:
    """文件存在且非空（拦截中断下载留下的 0 字节残渣）。"""
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _detect_total_ram_gb() -> float | None:
    """系统总内存 GiB；检测失败返回 None（psutil 缺失等）。"""
    try:
        import psutil
        return psutil.virtual_memory().total / (1024 ** 3)
    except Exception as exc:  # noqa: BLE001 — psutil 是可选的
        logger.debug("[memory_embedding] RAM 检测失败: %s", exc)
        return None


def _resolve_dim(setting: int | str | None) -> int:
    """把配置维度解析为整数。"auto"/None/非法值按内存选档。"""
    if isinstance(setting, int) and setting > 0:
        if setting in _DIM_STEPS:
            return setting
        logger.warning(
            "[memory_embedding] dim=%d 不在 %s，回退 auto", setting, _DIM_STEPS,
        )
    ram = _detect_total_ram_gb()
    if ram is not None:
        for floor, dim in _AUTO_DIM_RAM_BANDS:
            if ram >= floor:
                return dim
    return DEFAULT_DIM


def _resolve_model_file(model_path: str, quantization: str) -> str | None:
    """把配置路径解析为实际 ONNX 文件；目录按 N.E.K.O. ``onnx/`` 布局找。

    - 文件路径 → 直接使用（必须是非空文件）
    - 目录路径 → 依次尝试 ``onnx/model_quantized.onnx`` / ``onnx/model.onnx``
      （quantization="int8"/"fp32" 时只找对应变体；"auto" 优先 int8）
    - 找不到 → None（→ NO_MODEL_FILE 降级）
    """
    if not model_path:
        return None
    p = os.path.abspath(model_path)
    if _is_nonempty_file(p):
        return p
    if not os.path.isdir(p):
        return None
    if quantization == "fp32":
        candidates = [os.path.join(p, "onnx", "model.onnx")]
    elif quantization == "int8":
        candidates = [os.path.join(p, "onnx", "model_quantized.onnx")]
    else:  # auto
        candidates = [
            os.path.join(p, "onnx", "model_quantized.onnx"),
            os.path.join(p, "onnx", "model.onnx"),
            os.path.join(p, "model_quantized.onnx"),
            os.path.join(p, "model.onnx"),
        ]
    for c in candidates:
        if _is_nonempty_file(c):
            return c
    return None


def _default_tokenizer_path(model_file: str) -> str:
    """由 ONNX 文件位置推导 tokenizer.json（支持 ``onnx/`` 子目录布局）。

    ``<profile>/onnx/model.onnx`` → ``<profile>/tokenizer.json``；
    ``<dir>/model.onnx`` → ``<dir>/tokenizer.json``。
    """
    parent = os.path.dirname(os.path.abspath(model_file))
    if os.path.basename(parent).lower() == "onnx":
        return os.path.join(os.path.dirname(parent), "tokenizer.json")
    return os.path.join(parent, "tokenizer.json")


def _embedding_config() -> dict:
    """读取 config 的 ``memory.embedding`` 段；失败返回空 dict。"""
    try:
        from config import load_config
        cfg = load_config()
        return dict((cfg.get("memory", {}) or {}).get("embedding", {}) or {})
    except Exception as exc:  # noqa: BLE001
        logger.debug("[memory_embedding] 读取 embedding 配置失败: %s", exc)
        return {}


class _DisabledError(Exception):
    """加载路径的已知降级原因（不打印堆栈，直接 disable）。"""

    def __init__(self, reason: DisableReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


# ── 服务 ─────────────────────────────────────────────────────────────


class EmbeddingService:
    """本地 ONNX 向量编码服务（进程级单例，经 :func:`get_embedding_service` 获取）。

    实现 ``core.memory_hybrid.EmbeddingProvider`` 协议。状态机：
    INIT → LOADING → READY；任一失败路径 → DISABLED（粘性）。

    线程模型：
    - ``is_available()`` / ``embed_texts()`` 可从任意线程调用（内部有锁保护）；
    - 所有磁盘/推理工作提交到专用单 worker 线程池，调用方等待有界；
    - 主线程（Qt）建议用 :meth:`submit_embed_texts` 异步路径，完全不等待。
    """

    def __init__(
        self,
        *,
        model_path: str = "",
        tokenizer_path: str = "",
        model_name: str = "",
        dim: int | str | None = None,
        quantization: str = DEFAULT_QUANTIZATION,
        max_length: int = DEFAULT_MAX_LENGTH,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        load_timeout_seconds: float = DEFAULT_LOAD_TIMEOUT_SECONDS,
        max_consecutive_timeouts: int = DEFAULT_MAX_CONSECUTIVE_TIMEOUTS,
        enabled: bool = True,
    ) -> None:
        self._model_path = model_path or ""
        self._tokenizer_path = tokenizer_path or ""
        self._model_name = model_name or ""
        self._quantization = (
            quantization if quantization in ("auto", "int8", "fp32") else DEFAULT_QUANTIZATION
        )
        self._max_length = max(16, int(max_length))
        self._timeout_seconds = max(0.1, float(timeout_seconds))
        self._load_timeout_seconds = max(1.0, float(load_timeout_seconds))
        self._max_consecutive_timeouts = max(1, int(max_consecutive_timeouts))
        self._dim = _resolve_dim(dim)

        self._state = EmbeddingState.INIT
        self._disable_reason = DisableReason.NONE
        self._session = None
        self._tokenizer = None
        self._load_lock = threading.Lock()
        self._infer_lock = threading.RLock()
        self._consecutive_timeouts = 0
        self._executor: futures.ThreadPoolExecutor | None = None

        if not enabled:
            self._mark_disabled(DisableReason.USER_DISABLED, log=False)
        else:
            # 不预检模型文件：加载失败在首次 is_available() 时降级（懒加载）。
            # 单 worker：推理天然串行；线程池惰性建线程，未 submit 前零开销。
            self._executor = futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="ocpet-embed",
            )

    # ── EmbeddingProvider 协议 ────────────────────────────────────────

    def is_available(self) -> bool:
        """就绪可推理 → True。首次调用会触发懒加载（有界等待）。

        并发首次调用安全：一个线程负责加载，其余观察者轮询等 READY。
        """
        if self._state == EmbeddingState.READY:
            return True
        if self._state in (EmbeddingState.DISABLED, EmbeddingState.CLOSED):
            return False
        return self.ensure_loaded()

    def embed_texts(self, texts: list[str]) -> list[list[float] | None]:
        """批量嵌入；返回与输入等长的 ``list[list[float] | None]``。

        空文本以 None 占位；服务不可用/超时/异常 → 全 None（调用方走
        fallback gate，绝不上抛）。
        """
        if not texts:
            return []
        if not self.is_available():
            return [None] * len(texts)
        assert self._executor is not None
        future = self._executor.submit(self._embed_texts_core, texts)
        try:
            result = future.result(timeout=self._timeout_seconds)
        except futures.TimeoutError:
            self._register_timeout()
            return [None] * len(texts)
        except Exception as exc:  # noqa: BLE001 — 推理异常 → 粘性降级
            logger.warning(
                "EmbeddingService: inference failed (%s: %s); vectors disabled",
                type(exc).__name__, exc,
            )
            self._mark_disabled(DisableReason.INFERENCE_ERROR)
            return [None] * len(texts)
        self._consecutive_timeouts = 0
        return result

    def embed(self, text: str) -> list[float] | None:
        """单条文本向量；不可用/失败返回 None（调用方自行降级）。"""
        if not text:
            return None
        vectors = self.embed_texts([text])
        return vectors[0] if vectors else None

    def submit_embed_texts(
        self,
        texts: list[str],
        on_done: Callable[[list[list[float] | None]], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """主线程友好异步嵌入：推理在独立线程池，不阻塞调用方。

        结果经 ``on_done(list)`` 回调（在工作线程执行，回调内不要碰 Qt/COM；
        需要回主线程请自行经 Signal 转发）。首次调用前建议先在非主线程
        ``ensure_loaded()``，否则异步路径无法安全等待加载锁，首轮可能
        以 None 占位。
        """
        if not texts:
            if on_done:
                on_done([])
            return
        if self._executor is None:
            if on_done:
                on_done([None] * len(texts))
            return

        def _task() -> None:
            try:
                result = self._embed_texts_core(texts)
            except Exception as exc:  # noqa: BLE001
                if on_error:
                    on_error(exc)
                return
            if on_done:
                on_done(result)

        self._executor.submit(_task)

    # ── 生命周期 ──────────────────────────────────────────────────────

    def ensure_loaded(self) -> bool:
        """首次加载 ONNX session（幂等、单飞、有界等待）。

        任一失败 → sticky DISABLED；返回是否就绪。线程安全：
        并发调用只有一个线程负责加载，其余轮询等结果。
        """
        with self._load_lock:
            if self._state == EmbeddingState.READY:
                return True
            if self._state in (EmbeddingState.DISABLED, EmbeddingState.CLOSED):
                return False
            if self._state == EmbeddingState.INIT:
                self._state = EmbeddingState.LOADING
                if self._executor is None:
                    self._mark_disabled(DisableReason.USER_DISABLED, log=False)
                    return False
                try:
                    future = self._executor.submit(self._load_blocking)
                except Exception as exc:  # noqa: BLE001 — 提交失败（executor 已关）
                    logger.warning(
                        "EmbeddingService: load submit failed (%s: %s); vectors disabled",
                        type(exc).__name__, exc,
                    )
                    self._mark_disabled(DisableReason.LOAD_ERROR)
                    return False
                owner = True
            else:  # LOADING — 另一个线程正在加载
                future = None
                owner = False

        # 锁外等待（不阻塞其他线程观察状态）
        if owner:
            try:
                future.result(timeout=self._load_timeout_seconds)
            except futures.TimeoutError:
                with self._load_lock:
                    if self._state == EmbeddingState.LOADING:
                        logger.warning(
                            "EmbeddingService: load timed out after %.1fs; vectors disabled",
                            self._load_timeout_seconds,
                        )
                        self._mark_disabled(DisableReason.LOAD_TIMEOUT)
                return False
            except _DisabledError as e:
                with self._load_lock:
                    if self._state == EmbeddingState.LOADING:
                        self._mark_disabled(e.reason)
                return False
            except Exception as exc:  # noqa: BLE001 — 任何加载失败 → 关
                with self._load_lock:
                    if self._state == EmbeddingState.LOADING:
                        logger.warning(
                            "EmbeddingService: load failed (%s: %s); vectors disabled",
                            type(exc).__name__, exc,
                        )
                        self._mark_disabled(DisableReason.LOAD_ERROR)
                return False
            with self._load_lock:
                if self._state == EmbeddingState.LOADING:
                    self._state = EmbeddingState.READY
                    logger.info(
                        "EmbeddingService: ready (model=%s dim=%d max_length=%d)",
                        self._model_path or "<model_path>", self._dim, self._max_length,
                    )
                return self._state == EmbeddingState.READY

        # 观察者：轮询等 READY / DISABLED / CLOSED
        deadline = time.monotonic() + self._load_timeout_seconds
        while time.monotonic() < deadline:
            with self._load_lock:
                if self._state == EmbeddingState.READY:
                    return True
                if self._state in (EmbeddingState.DISABLED, EmbeddingState.CLOSED):
                    return False
            time.sleep(0.05)
        return False

    def close(self) -> None:
        """释放原生 session/tokenizer 引用并关闭线程池（进程退出前调用）。"""
        with self._load_lock:
            if self._state == EmbeddingState.CLOSED:
                return
            self._state = EmbeddingState.CLOSED
            self._session = None
            self._tokenizer = None
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=False)

    # ── 查询 ──────────────────────────────────────────────────────────

    def is_disabled(self) -> bool:
        """已到粘性 DISABLED 状态（区别于未加载的 INIT）。"""
        return self._state == EmbeddingState.DISABLED

    def disable_reason(self) -> str:
        return self._disable_reason.value

    def model_id(self) -> str | None:
        """规范模型 id（供向量缓存指纹；DISABLED 时 None）。"""
        if self._state == EmbeddingState.DISABLED:
            return None
        name = self._model_name or "local-text-retrieval-v1"
        return f"{name}-{self._dim}d-mlen{self._max_length}"

    def dim(self) -> int:
        return self._dim

    def state(self) -> str:
        return self._state.value

    # ── internal：推理 / 加载（在工作线程执行）────────────────────────

    def _embed_texts_core(self, texts: list[str]) -> list[list[float] | None]:
        """在工作线程执行的实际嵌入；串行化 session/tokenizer 访问。"""
        if self._state != EmbeddingState.READY:
            return [None] * len(texts)
        result: list[list[float] | None] = [None] * len(texts)
        active_idx = [i for i, t in enumerate(texts) if t]
        if not active_idx:
            return result
        with self._infer_lock:
            vectors = self._infer_blocking([texts[i] for i in active_idx])
        for pos, i in enumerate(active_idx):
            if pos < len(vectors):
                result[i] = vectors[pos]
        return result

    def _load_blocking(self) -> None:
        """同步加载（在专用线程池执行）。失败抛 ``_DisabledError`` 或异常。

        检查顺序：文件存在性（最便宜）→ onnxruntime import → tokenizers
        import → session 创建 → tokenizer 截断契约建立。
        """
        model_file = _resolve_model_file(self._model_path, self._quantization)
        if model_file is None:
            raise _DisabledError(DisableReason.NO_MODEL_FILE)
        tokenizer_file = self._tokenizer_path or _default_tokenizer_path(model_file)
        if not _is_nonempty_file(tokenizer_file):
            raise _DisabledError(DisableReason.NO_MODEL_FILE)
        try:
            import onnxruntime as ort  # type: ignore
        except ImportError as e:
            raise _DisabledError(DisableReason.NO_ONNXRUNTIME) from e
        try:
            from tokenizers import Tokenizer  # type: ignore
        except ImportError as e:
            raise _DisabledError(DisableReason.NO_TOKENIZERS) from e

        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = max(1, (os.cpu_count() or 2) // 2)
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # 关 arena：一次性大分配后 RSS 不钉在高水位（N.E.K.O. 同款取舍）
        sess_opts.enable_cpu_mem_arena = False
        try:
            sess_opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        except Exception:  # noqa: BLE001 — 老版本 ORT 无此键，保持默认 spin
            logger.debug("memory_embedding: 非致命异常(已静默吞掉)", exc_info=True)
        self._session = ort.InferenceSession(
            model_file, sess_options=sess_opts, providers=["CPUExecutionProvider"],
        )
        self._tokenizer = Tokenizer.from_file(tokenizer_file)
        try:
            self._tokenizer.enable_truncation(max_length=self._max_length)
        except Exception as e:  # noqa: BLE001 — 截断契约没建立 → 宁可关
            logger.warning(
                "EmbeddingService: tokenizer truncation setup failed (max_length=%d): %s",
                self._max_length, e,
            )
            raise _DisabledError(DisableReason.TRUNCATION_SETUP_FAILED) from e

    def _infer_blocking(self, texts: list[str]) -> list[list[float]]:
        """tokenize → ONNX run → last-token pool → L2 归一化 → Matryoshka 截断。

        Matryoshka 截断是 ``model_id`` 编码维度、缓存指纹必须含 dim 的原因：
        64d 与 256d 向量即使同源也不可比。
        """
        if self._session is None or self._tokenizer is None:
            raise RuntimeError("session not loaded")
        import numpy as np

        encoded = self._tokenizer.encode_batch(texts)
        input_names = {i.name for i in self._session.get_inputs()}
        ids = [e.ids for e in encoded]
        masks = [e.attention_mask for e in encoded]
        max_len = max((len(x) for x in ids), default=0)
        if max_len == 0:
            return [[] for _ in texts]
        ids_arr = np.zeros((len(texts), max_len), dtype=np.int64)
        mask_arr = np.zeros((len(texts), max_len), dtype=np.int64)
        for i, (row, mask_row) in enumerate(zip(ids, masks)):
            ids_arr[i, : len(row)] = row
            mask_arr[i, : len(mask_row)] = mask_row
        feeds: dict[str, object] = {"input_ids": ids_arr}
        if "attention_mask" in input_names:
            feeds["attention_mask"] = mask_arr
        if "token_type_ids" in input_names:
            feeds["token_type_ids"] = np.zeros_like(ids_arr)
        outputs = self._session.run(None, feeds)
        token_embeddings = outputs[0]
        # 默认 profile 用 last-token pooling
        last_indices = np.maximum(mask_arr.sum(axis=1) - 1, 0)
        pooled = token_embeddings[np.arange(len(texts)), last_indices]
        if self._dim and self._dim < pooled.shape[1]:
            pooled = pooled[:, : self._dim]
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        normalized = pooled / norms
        return [normalized[i].tolist() for i in range(len(texts))]

    # ── disable 记账 ──────────────────────────────────────────────────

    def _register_timeout(self) -> None:
        self._consecutive_timeouts += 1
        logger.warning(
            "EmbeddingService: inference timed out after %.1fs (consecutive=%d/%d)",
            self._timeout_seconds, self._consecutive_timeouts,
            self._max_consecutive_timeouts,
        )
        if self._consecutive_timeouts >= self._max_consecutive_timeouts:
            self._mark_disabled(DisableReason.INFERENCE_TIMEOUT)

    def _mark_disabled(self, reason: DisableReason, *, log: bool = True) -> None:
        if self._state == EmbeddingState.CLOSED:
            return
        if self._state != EmbeddingState.DISABLED and log:
            logger.warning("EmbeddingService: vectors disabled (%s)", reason.value)
        self._state = EmbeddingState.DISABLED
        self._disable_reason = reason
        self._session = None
        self._tokenizer = None


# ── 进程级单例 ───────────────────────────────────────────────────────

_SERVICE: EmbeddingService | None = None
_SERVICE_LOCK = threading.Lock()


def get_embedding_service() -> EmbeddingService:
    """返回进程级单例（线程安全；懒构造，读取 config ``memory.embedding``）。"""
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = _build_from_config()
    return _SERVICE


def reset_embedding_service_for_tests() -> None:
    """测试专用：丢弃单例并关闭旧 executor。"""
    global _SERVICE
    with _SERVICE_LOCK:
        service = _SERVICE
        _SERVICE = None
    if service is not None:
        service.close()


def _build_from_config() -> EmbeddingService:
    cfg = _embedding_config()
    return EmbeddingService(
        model_path=str(cfg.get("model_path") or ""),
        tokenizer_path=str(cfg.get("tokenizer_path") or ""),
        model_name=str(cfg.get("model_name") or ""),
        dim=cfg.get("dim"),
        quantization=str(cfg.get("quantization") or DEFAULT_QUANTIZATION),
        max_length=int(cfg.get("max_length") or DEFAULT_MAX_LENGTH),
        timeout_seconds=float(cfg.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
        load_timeout_seconds=float(
            cfg.get("load_timeout_seconds") or DEFAULT_LOAD_TIMEOUT_SECONDS
        ),
        max_consecutive_timeouts=int(
            cfg.get("max_consecutive_timeouts") or DEFAULT_MAX_CONSECUTIVE_TIMEOUTS
        ),
        enabled=bool(cfg.get("enabled", False)),
    )


def default_embedding_provider() -> EmbeddingService | None:
    """供 ``HybridMemoryRecall`` 默认注入。

    config ``memory.embedding.enabled`` 关闭 → None（memory_hybrid 内部回退
    NoopEmbeddingProvider，纯 BM25）；开启 → 进程单例（首次 recall 懒加载）。
    """
    if not bool(_embedding_config().get("enabled", False)):
        return None
    return get_embedding_service()


__all__ = [
    "EmbeddingService",
    "EmbeddingState",
    "DisableReason",
    "default_embedding_provider",
    "get_embedding_service",
    "reset_embedding_service_for_tests",
    "DEFAULT_DIM",
    "DEFAULT_QUANTIZATION",
    "DEFAULT_MAX_LENGTH",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_LOAD_TIMEOUT_SECONDS",
    "DEFAULT_MAX_CONSECUTIVE_TIMEOUTS",
]
