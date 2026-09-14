"""core/audio_input/vad.py — 语音活动检测（VAD）

ASR-1（2026-09-14）：把 PoC 验证过的 Silero VAD（onnxruntime 直跑）接进桌宠。

背景
----
连续监听（`chat_mixin._on_voice_vad`）此前用 **RMS 能量阈值**判断"是否有人说话"，
分不清「你在说话」和「B站视频里的人在说话」——靠自适应底噪（`_vad_noise_floor`）
打补丁，但那是补丁不是解法。

`docs/poc-silero-vad-onnx-2026-09-04.md` 已完成 PoC 并判定 **PASS**：
- **不用 torch**：`silero-vad` 的 Python 包装在 import 时就要 torch，装不了；
  但它自带的 ONNX 模型可以直接用 `onnxruntime` 跑
- **开销可忽略**：每 32ms 音频窗 0.16ms 推理 ≈ 占实时 **0.5%**
- 模型文件已 vendored 到 `core/audio_input/models/silero_vad_16k_op15.onnx`（1.26MB）

设计
----
- `SileroVAD`：ONNX 迭代器，逐 512 样本窗喂入，输出语音概率
- `EnergyVAD`：RMS 阈值回退（无 onnxruntime / 模型缺失时）
- `create_vad()`：自动选择（`auto` → Silero，失败 → Energy）

**失败闭合**：onnxruntime 缺失、模型文件损坏、推理异常 → 一律回退能量 VAD，
行为与改动前一致，不会因为 VAD 问题导致听不见人说话。

线程
----
`is_speech()` 在音频回调线程调用（sounddevice callback）。PoC 实测开销 0.5%，
可接受。内部状态仅该线程访问，无需锁。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import numpy as np
    _NP_OK = True
except ImportError:  # pragma: no cover
    _NP_OK = False

try:
    import onnxruntime as ort
    _ORT_OK = True
except ImportError:  # pragma: no cover
    _ORT_OK = False
    ort = None  # type: ignore

# vendored 模型路径
_MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
DEFAULT_ONNX = os.path.join(_MODEL_DIR, "silero_vad_16k_op15.onnx")

# Silero 固定窗口：16k 采样率下 512 样本 ≈ 32ms
WINDOW = 512

# ⚠️ 关键：Silero VAD 需要 64 样本的 context 前缀。
# 输入实际是 [64 旧样本(context) + 512 新样本] = 576。
# 漏掉这一步会导致概率恒低（实测 max 仅 0.16，判不出语音）。
# 依据：silero_vad/utils_vad.py `OnnxWrapper.__call__` 的
#   `x = torch.cat([self._context, x], dim=1)`。
CONTEXT_SIZE = 64


class EnergyVAD:
    """能量阈值 VAD（回退实现）。

    与改动前 `chat_mixin._on_voice_vad` 的判据一致：RMS > 阈值 视为语音。
    保留它是为了「onnxruntime 不可用时行为不变」。
    """

    backend = "energy"

    def __init__(self, threshold: float = 0.02):
        self._threshold = float(threshold)
        self._noise_floor = 0.006

    def is_speech(self, chunk, rms: float) -> bool:
        """chunk: float32 单声道；rms: 调用方已算好的能量。"""
        # 自适应底噪：环境噪声抬高时阈值随之抬高
        nf = 0.9 * self._noise_floor + 0.1 * max(rms, 1e-5)
        self._noise_floor = nf
        return rms > max(self._threshold, nf * 3.0)

    def reset(self) -> None:
        self._noise_floor = 0.006


class SileroVAD:
    """Silero VAD（onnxruntime 直跑，torch-free）。

    用法（音频回调线程）：
        v = SileroVAD()
        if v.is_speech(chunk):   # chunk = 512 样本 float32
            ...
        v.reset()                # 语音段结束后重置状态
    """

    backend = "silero"

    def __init__(
        self,
        onnx_path: str = DEFAULT_ONNX,
        sr: int = 16000,
        threshold: float = 0.5,
        neg_threshold: float = 0.35,
    ):
        if not _ORT_OK:
            raise RuntimeError("onnxruntime 不可用")
        if not _NP_OK:
            raise RuntimeError("numpy 不可用")
        if not os.path.isfile(onnx_path):
            raise FileNotFoundError(f"VAD 模型不存在: {onnx_path}")
        self._sess = ort.InferenceSession(
            onnx_path, providers=["CPUExecutionProvider"]
        )
        self._sr = int(sr)
        self._sr_in = np.array(self._sr, dtype=np.int64)
        self._threshold = float(threshold)
        self._neg_threshold = float(neg_threshold)
        self.reset()

    def reset(self) -> None:
        """重置内部状态（新语音段开始时调用）。"""
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        # context：上一次窗口的末尾 64 样本，首次为零
        self._context = np.zeros((1, CONTEXT_SIZE), dtype=np.float32)
        self._triggered = False
        self._prob = 0.0

    @property
    def prob(self) -> float:
        """最近一窗的语音概率（0~1）。"""
        return self._prob

    def is_speech(self, chunk) -> bool:
        """判断当前窗是否属于语音。

        Args:
            chunk: float32 单声道音频。不足 512 样本时右侧补零
                   （Silero 需要固定长度输入）。
        """
        try:
            w = np.asarray(chunk, dtype=np.float32).flatten()
            if w.shape[0] < WINDOW:
                w = np.pad(w, (0, WINDOW - w.shape[0]))
            elif w.shape[0] > WINDOW:
                w = w[:WINDOW]
            w = w[None, :]  # (1, 512)
            # 拼接 context → (1, 576)，这是 Silero 期望的输入
            x = np.concatenate([self._context, w], axis=1)
            out, state = self._sess.run(
                None,
                {
                    "input": x.astype(np.float32),
                    "state": self._state,
                    "sr": self._sr_in,
                },
            )
            self._state = state
            self._context = x[..., -CONTEXT_SIZE:]
            p = float(out[0, 0])
            self._prob = p
        except Exception as e:
            logger.debug("SileroVAD 推理失败，本窗判为非语音: %s", e)
            return False
        # 触发后放宽阈值（滞回），避免句尾被切断
        if self._triggered:
            if p < self._neg_threshold:
                self._triggered = False
            return p >= self._neg_threshold
        if p >= self._threshold:
            self._triggered = True
            return True
        return False


def create_vad(
    backend: str = "auto",
    *,
    energy_threshold: float = 0.02,
    onnx_path: str = DEFAULT_ONNX,
):
    """创建 VAD 实例。

    Args:
        backend: "auto"（优先 Silero，失败回退能量）/ "silero" / "energy"
        energy_threshold: 能量 VAD 的阈值
        onnx_path: Silero ONNX 模型路径

    Returns:
        SileroVAD 或 EnergyVAD 实例（永不返回 None，保证失败闭合）。
    """
    backend = (backend or "auto").strip().lower()
    if backend == "energy":
        logger.info("VAD backend=energy（显式指定）")
        return EnergyVAD(threshold=energy_threshold)

    if backend in ("auto", "silero"):
        try:
            v = SileroVAD(onnx_path=onnx_path)
            logger.info("VAD backend=silero（onnxruntime，模型=%s）",
                        os.path.basename(onnx_path))
            return v
        except Exception as e:
            if backend == "silero":
                logger.warning("Silero VAD 不可用，回退能量 VAD: %s", e)
            else:
                logger.info("Silero VAD 不可用，使用能量 VAD: %s", e)
            return EnergyVAD(threshold=energy_threshold)

    logger.warning("未知 VAD backend=%s，回退能量 VAD", backend)
    return EnergyVAD(threshold=energy_threshold)
