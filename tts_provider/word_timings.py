"""词级时间戳侧车（TTS 口型精度提升）。

背景
----
文件式 TTS（Edge / CosyVoice / MiMo / API）走 QMediaPlayer 播放，
拿不到 PCM，因此 ``ui/tts_player.current_level()`` 只能用**播放位置驱动的
正弦包络**粗略开合嘴——嘴一直在动，句读处也不闭嘴，看着假。

Edge TTS 在合成时会一并返回 ``WordBoundary`` 事件（每个词的
offset / duration，单位 100ns）。把它落盘为 ``<音频>.words.json``
侧车文件，播放器就能按"当前播放位置落在哪个词区间"驱动口型：

- 词内：正弦包络开合（有起伏，不是恒定张开）
- 词间：归零（真的闭嘴，停顿可见）

设计约束
--------
- **零调用点改动**：provider 合成时顺手写侧车，播放器按路径约定读；
  没有任何函数签名变化。
- **失败即忽略**：侧车缺失/损坏/格式不符一律返回 None，播放器回落
  到原有正弦包络。绝不因为口型增强而影响出声。
- **纯函数可测**：``level_at`` 不碰文件系统、不碰 Qt。

数据格式
--------
``<audio>.words.json``::

    {"words": [{"o": 0, "d": 475, "t": "你好"}, ...]}

``o`` = 起始毫秒，``d`` = 时长毫秒，``t`` = 词文本（可选）。
"""
from __future__ import annotations

import json
import logging
import math
import os
from typing import Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

# 与 ui/tts_player._MOUTH_ENVELOPE_MAX 保持一致：渲染器按这个量级
# 映射为可见开合（0.15~0.85），词级口型不应超出同一范围。
_LEVEL_MAX = 0.27

# 词内包络频率：比全局 5Hz 略高，贴合单字/双字词的短促开合
_WORD_ENVELOPE_HZ = 6.0


def sidecar_path(audio_path: str) -> str:
    """音频路径 → 侧车路径（``a.mp3`` → ``a.mp3.words.json``）。"""
    return f"{audio_path}.words.json"


def save_words(audio_path: str, words: Iterable[dict]) -> Optional[str]:
    """把词边界写入侧车文件。返回侧车路径；无有效词或写失败返回 None。

    ``words`` 每项形如 ``{"o": 起始ms, "d": 时长ms, "t": 文本}``。
    """
    if not audio_path:
        return None
    rows: list[dict] = []
    for w in words or ():
        if not isinstance(w, dict):
            continue
        try:
            off = int(w.get("o", 0))
            dur = int(w.get("d", 0))
        except (TypeError, ValueError):
            continue
        if off < 0 or dur <= 0:
            continue
        row = {"o": off, "d": dur}
        text = w.get("t")
        if isinstance(text, str) and text:
            row["t"] = text
        rows.append(row)
    if not rows:
        return None
    rows.sort(key=lambda r: r["o"])
    path = sidecar_path(audio_path)
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"words": rows}, f, ensure_ascii=False)
        os.replace(tmp, path)
        return path
    except OSError:
        logger.debug("词边界侧车写入失败: %s", path, exc_info=True)
        return None


def load_words(audio_path: str) -> Optional[list[tuple[int, int]]]:
    """读侧车 → ``[(start_ms, end_ms), ...]``（已排序）。

    任何异常（不存在 / JSON 坏 / 格式不符 / 空）一律返回 None。
    """
    if not audio_path:
        return None
    path = sidecar_path(audio_path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        logger.debug("词边界侧车读取失败: %s", path, exc_info=True)
        return None
    rows = data.get("words") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    out: list[tuple[int, int]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            off = int(r.get("o", 0))
            dur = int(r.get("d", 0))
        except (TypeError, ValueError):
            continue
        if off < 0 or dur <= 0:
            continue
        out.append((off, off + dur))
    if not out:
        return None
    out.sort(key=lambda p: p[0])
    return out


def level_at(words: Sequence[tuple[int, int]], position_seconds: float) -> float:
    """播放位置（秒）→ 口型电平（0~_LEVEL_MAX）。纯函数。

    - 位置落在某个词区间内：按词内相位给正弦包络
    - 落在词间空隙 / 越界：0.0（闭嘴）
    """
    if not words:
        return 0.0
    try:
        t = float(position_seconds)
    except (TypeError, ValueError):
        return 0.0
    if t < 0:
        return 0.0
    ms = t * 1000.0
    for start, end in words:
        if start <= ms < end:
            span = (end - start) / 1000.0
            if span <= 0:
                return 0.0
            phase = (ms - start) / 1000.0
            return _LEVEL_MAX * (0.5 + 0.5 * math.sin(2.0 * math.pi * _WORD_ENVELOPE_HZ * phase))
    return 0.0
