"""音频能量分段（口型时间轴的通用来源）。

为什么需要它
------------
``word_timings`` 依赖 provider 主动返回词边界——只有 Edge TTS 给。
Qwen / MiMo / CosyVoice / OpenAI 兼容 API 都不给，于是这些引擎下
口型只能退回"位置正弦包络"（匀速开合，停顿处不闭嘴）。

但只要拿到音频文件本身，就能算出**能量包络**：说话的地方能量高、
停顿的地方接近静音。把它切成段，就得到了与词级时间戳同样形状的
时间轴——**任何 provider、任何格式都能用**。

精度取舍
--------
能量分段的边界不如 WordBoundary 精确（词边界是引擎内部的真实对齐，
能量只能看到"有声音"）。但对**口型**而言，能量其实更贴近需求：
嘴本来就该在有声音时张开、静音时闭上。所以：

- Edge（有词边界）→ 用词边界（更精确，且能对到具体词）
- 其余 → 用能量分段（远好于正弦包络）

失败即忽略
----------
解码失败 / 无 numpy / 音频全是静音 / 任何异常 → 返回 None。
调用方（播放器）回落到正弦包络。**绝不因为口型增强而影响出声。**
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# 分段参数（默认值按"中文短句 TTS"调过）
_FRAME_MS = 20          # 分析帧长
_THRESHOLD_RATIO = 0.12 # 阈值 = 峰值 × 该比例（自适应，不依赖绝对音量）
_MIN_SEG_MS = 70        # 短于此的段丢弃（气声/爆音）
_MERGE_GAP_MS = 60      # 间隔短于此的段合并（词内换气不该分段）
_TARGET_SR = 16000      # 降采样目标（只要包络，不需要高保真）


def segments_path(audio_path: str) -> str:
    """音频路径 → 能量分段侧车路径（``a.mp3`` → ``a.mp3.segments.json``）。"""
    return f"{audio_path}.segments.json"


# ── 解码 ─────────────────────────────────────────────


def _decode_mono(audio_path: str):
    """音频文件 → (float32 单声道数组, 采样率)。失败返回 (None, 0)。

    三级尝试：soundfile（wav/flac/ogg）→ pydub（mp3 等，需 ffmpeg）
    → ffmpeg 子进程。任一成功即返回。
    """
    try:
        import numpy as np
        import soundfile as sf

        data, sr = sf.read(audio_path, dtype="float32", always_2d=True)
        mono = data.mean(axis=1)
        return np.asarray(mono, dtype=np.float32), int(sr)
    except Exception:
        logger.debug("soundfile 解码失败，尝试 pydub: %s", audio_path, exc_info=True)

    try:
        import numpy as np
        from pydub import AudioSegment

        seg = AudioSegment.from_file(audio_path)
        seg = seg.set_channels(1)
        sr = seg.frame_rate
        samples = np.frombuffer(seg.raw_data, dtype=np.int16).astype(np.float32) / 32768.0
        return samples, int(sr)
    except Exception:
        logger.debug("pydub 解码失败，尝试 ffmpeg: %s", audio_path, exc_info=True)

    try:
        import numpy as np
        import wave

        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            return None, 0
        proc = subprocess.run(
            [ffmpeg, "-v", "quiet", "-i", audio_path,
             "-ac", "1", "-ar", str(_TARGET_SR), "-f", "wav", "-"],
            capture_output=True, timeout=30,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None, 0
        import io

        with wave.open(io.BytesIO(proc.stdout), "rb") as wf:
            sr = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return samples, int(sr)
    except Exception:
        logger.debug("ffmpeg 解码失败: %s", audio_path, exc_info=True)
        return None, 0


def _find_ffmpeg() -> Optional[str]:
    """找一个可用的 ffmpeg：PATH → imageio_ffmpeg 自带 → 已知安装位置。"""
    import shutil

    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        logger.debug("imageio_ffmpeg 不可用", exc_info=True)
    for cand in (r"W:\Games\ffmpeg\bin\ffmpeg.exe",):
        if os.path.exists(cand):
            return cand
    return None


# ── 分段 ─────────────────────────────────────────────


def compute_segments(
    samples,
    sr: int,
    *,
    frame_ms: int = _FRAME_MS,
    threshold_ratio: float = _THRESHOLD_RATIO,
    min_seg_ms: int = _MIN_SEG_MS,
    merge_gap_ms: int = _MERGE_GAP_MS,
) -> list[tuple[int, int]]:
    """能量包络 → 有声段 ``[(start_ms, end_ms), ...]``。纯函数（无 IO）。

    算法：分帧算 RMS → 阈值（峰值 × ratio，自适应）→ 取连续超阈区间
    → 合并近邻 → 丢弃过短段。
    """
    import numpy as np

    if samples is None or len(samples) == 0 or sr <= 0:
        return []

    hop = max(1, int(sr * frame_ms / 1000))
    n_frames = len(samples) // hop
    if n_frames < 2:
        return []

    trimmed = samples[: n_frames * hop].reshape(n_frames, hop)
    rms = np.sqrt(np.mean(np.square(trimmed.astype(np.float64)), axis=1))

    peak = float(rms.max())
    if peak <= 1e-6:
        return []   # 全静音
    thr = peak * float(threshold_ratio)

    above = rms >= thr
    segs: list[tuple[int, int]] = []
    start = None
    for i, flag in enumerate(above):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            segs.append((start, i))
            start = None
    if start is not None:
        segs.append((start, n_frames))

    # 帧索引 → 毫秒，顺带合并近邻与过滤短段
    merged: list[list[int]] = []
    for s, e in segs:
        s_ms = int(s * frame_ms)
        e_ms = int(e * frame_ms)
        if merged and s_ms - merged[-1][1] <= merge_gap_ms:
            merged[-1][1] = e_ms
        else:
            merged.append([s_ms, e_ms])

    return [(s, e) for s, e in merged if e - s >= min_seg_ms]


def analyze_file(audio_path: str, **kw) -> Optional[list[tuple[int, int]]]:
    """音频文件 → 有声段。任何失败返回 None。"""
    if not audio_path or not os.path.exists(audio_path):
        return None
    samples, sr = _decode_mono(audio_path)
    if samples is None or sr <= 0:
        return None
    try:
        segs = compute_segments(samples, sr, **kw)
    except Exception:
        logger.debug("能量分段计算失败: %s", audio_path, exc_info=True)
        return None
    return segs or None


# ── 侧车读写 ─────────────────────────────────────────


def save_segments(audio_path: str, segs) -> Optional[str]:
    """把能量分段写入侧车。无有效段或写失败返回 None。"""
    if not audio_path or not segs:
        return None
    rows = []
    for s, e in segs:
        try:
            s_i, e_i = int(s), int(e)
        except (TypeError, ValueError):
            continue
        if s_i < 0 or e_i <= s_i:
            continue
        rows.append([s_i, e_i])
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])
    path = segments_path(audio_path)
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"segments": rows}, f)
        os.replace(tmp, path)
        return path
    except OSError:
        logger.debug("能量分段侧车写入失败: %s", path, exc_info=True)
        return None


def load_segments(audio_path: str) -> Optional[list[tuple[int, int]]]:
    """读能量分段侧车。任何异常返回 None。"""
    if not audio_path:
        return None
    path = segments_path(audio_path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        logger.debug("能量分段侧车读取失败: %s", path, exc_info=True)
        return None
    rows = data.get("segments") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    out: list[tuple[int, int]] = []
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) < 2:
            continue
        try:
            s, e = int(r[0]), int(r[1])
        except (TypeError, ValueError):
            continue
        if s < 0 or e <= s:
            continue
        out.append((s, e))
    if not out:
        return None
    out.sort(key=lambda p: p[0])
    return out


# ── 统一入口（供 provider 收尾调用）─────────────────


def ensure_timings(audio_path: str, words=None) -> Optional[str]:
    """为音频准备口型时间轴侧车。

    - ``words`` 非空（provider 有原生词边界）→ 写 ``.words.json``
    - 否则 → 分析音频能量，写 ``.segments.json``

    已有侧车则跳过（避免重复分析）。失败静默返回 None。
    """
    if not audio_path or not os.path.exists(audio_path):
        return None
    try:
        from .word_timings import save_words, sidecar_path

        if words:
            if os.path.exists(sidecar_path(audio_path)):
                return sidecar_path(audio_path)
            return save_words(audio_path, words)
        if os.path.exists(segments_path(audio_path)):
            return segments_path(audio_path)
        segs = analyze_file(audio_path)
        return save_segments(audio_path, segs) if segs else None
    except Exception:
        logger.debug("口型时间轴生成失败: %s", audio_path, exc_info=True)
        return None
