"""core/lip_sync.py — 文本 → 口型时间轴（LIP-1）

目标：让嘴巴"知道自己在发什么音"，而不只是跟着音量开合。

为什么要这个
-------------
- **振幅级**（已实现）：嘴跟音量动。重音/停顿能看出来，但说"啊——"和"嘶——"
  嘴型一样。
- **音素级**（本模块）：嘴跟**具体发音**动。`/a/` 张大、`/i/` 咧嘴、`/m/` 闭唇。

为什么不用 Paraformer 对齐
--------------------------
R2 实测 Paraformer 确实输出字符级时间戳（每字 start/end ms），但：
**它需要完整音频才能出时间戳**，而 oc-pet 的 TTS 是**边合成边播**——
拿到完整音频时话已经说完了，事后对齐赶不上。

所以走**文本推导**：`begin` 回调时文本已完整，从文本直接算口型时间轴，
再用播放位置同步。

映射原理
--------
中文一个字 ≈ 一个音节。用 `pypinyin` 拿到每个字的声母/韵母：
- **韵母决定开口度**（mouth_open）：`a` 大、`e` 中、`i/u` 小
- **韵母决定唇形**（mouth_form）：`u/o/ü` 圆唇（负）、`i/e` 扁唇（正）
- **声母决定起始闭合**：`b/p/m/f` 是唇音，起始瞬间要闭嘴
- **标点决定停顿**：句号停顿长、逗号短，期间嘴闭合

实测（`pypinyin` 本机已装）：
    对话被打断了 → d+uei, h+ua, b+ei, d+a, d+uan, l+e
    字符数与声母/韵母数一一对应（含英文按字母拆）

失败闭合
--------
`pypinyin` 不可用、文本为空、时长异常 → 返回空时间轴，
调用方回退到振幅驱动（行为与 LIP-1 之前一致）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from pypinyin import Style, pinyin as _pinyin
    _PYPINYIN_OK = True
except ImportError:  # pragma: no cover
    _PYPINYIN_OK = False
    Style = None  # type: ignore
    _pinyin = None  # type: ignore


# ── 语速与时长 ──

# 每字基准时长（秒）。中文对话语速约 4.5~6 字/秒，取中值 5.5 字/秒 ≈ 0.18s。
DEFAULT_CHAR_SEC = 0.18

# 标点停顿（秒）——嘴闭合，模拟换气/断句
PAUSE_SEC: dict[str, float] = {
    "。": 0.30, "！": 0.30, "？": 0.30, "…": 0.30,
    "，": 0.16, "、": 0.14, "；": 0.20, "：": 0.20,
    ".": 0.30, "!": 0.30, "?": 0.30,
    ",": 0.16, ";": 0.20, ":": 0.20,
}
_DEFAULT_PAUSE = 0.10


# ── 韵母 → 开口度（0~1）──

FINAL_OPEN: dict[str, float] = {
    # 大开口
    "a": 0.95, "ai": 0.85, "ao": 0.85, "an": 0.80, "ang": 0.80,
    "ia": 0.85, "iao": 0.80, "ian": 0.72, "iang": 0.75,
    "ua": 0.80, "uan": 0.75, "uang": 0.75,
    # 中开口
    "o": 0.75, "ong": 0.70, "uo": 0.70, "ou": 0.58,
    "e": 0.60, "er": 0.60, "eng": 0.55, "en": 0.50,
    "ei": 0.50, "ie": 0.50, "ue": 0.50, "ve": 0.50,
    "uen": 0.50, "ueng": 0.55,
    # 小开口
    "i": 0.30, "in": 0.30, "ing": 0.30, "iu": 0.35,
    "u": 0.35, "un": 0.40, "v": 0.35, "vn": 0.35,
    "ui": 0.40, "iong": 0.45,
}
_DEFAULT_OPEN = 0.50


# ── 韵母 → 唇形（-1 圆唇 ~ +1 扁唇）──

FINAL_ROUND: dict[str, float] = {
    # 圆唇（负）
    "u": -0.50, "v": -0.50, "uo": -0.45, "vn": -0.45,
    "o": -0.40, "ong": -0.40, "ou": -0.35, "ue": -0.40,
    "iu": -0.30, "ui": -0.30, "un": -0.35, "iong": -0.35,
    # 扁唇（正）
    "i": 0.25, "ie": 0.20, "in": 0.20, "ing": 0.20,
    "e": 0.15, "ei": 0.15, "en": 0.10, "eng": 0.10,
    "a": 0.05, "ai": 0.10, "ao": -0.15, "an": 0.05,
}
_DEFAULT_ROUND = 0.0


# ── 声母 → 起始闭合强度（唇音起始要闭嘴）──

CLOSING_INITIALS: dict[str, float] = {
    "b": 0.9, "p": 0.9, "m": 0.95, "f": 0.5,
}


# 英文字母 → 开口度（英文按字母拆，元音开口大）
EN_LETTER_OPEN: dict[str, float] = {
    "a": 0.90, "e": 0.60, "i": 0.35, "o": 0.75, "u": 0.40, "y": 0.35,
    "b": 0.20, "p": 0.20, "m": 0.15, "f": 0.30, "v": 0.30,
    "d": 0.35, "t": 0.35, "n": 0.30, "l": 0.40,
    "s": 0.25, "z": 0.25, "c": 0.30, "k": 0.35, "g": 0.35,
    "h": 0.45, "j": 0.30, "q": 0.30, "x": 0.30, "r": 0.40, "w": 0.35,
}
_DEFAULT_EN_OPEN = 0.35


@dataclass(frozen=True)
class LipFrame:
    """口型时间轴上的一个片段。"""
    start: float      # 起始秒
    end: float        # 结束秒
    mouth_open: float  # 开口度 0~1
    mouth_form: float  # 唇形 -1~+1

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def _is_punctuation(ch: str) -> bool:
    return bool(re.match(r"[\s。！？…，、；：\.\!\?\,\;\:]", ch))


def _pause_for(ch: str) -> float:
    return PAUSE_SEC.get(ch, _DEFAULT_PAUSE)


def _char_shape(ch: str, init: str, final: str) -> tuple[float, float]:
    """单字 → (开口度, 唇形)。"""
    # 标点/空白：闭合
    if _is_punctuation(ch):
        return 0.0, 0.0
    # 英文/数字：按字母规则
    if re.match(r"[A-Za-z]", ch):
        return EN_LETTER_OPEN.get(ch.lower(), _DEFAULT_EN_OPEN), 0.0
    if re.match(r"[0-9]", ch):
        return 0.45, 0.0
    # 中文：用韵母；韵母拿不到时退回声母/默认
    open_v = FINAL_OPEN.get(final, None)
    if open_v is None:
        open_v = _DEFAULT_OPEN
    form_v = FINAL_ROUND.get(final, _DEFAULT_ROUND)
    # 唇音声母：起始闭嘴——不改变整体开口度，但由调用方在起段做短促闭合
    return open_v, form_v


def build_timeline(
    text: str,
    *,
    char_sec: float = DEFAULT_CHAR_SEC,
    start_at: float = 0.0,
    speed: float = 1.0,
) -> list[LipFrame]:
    """从文本生成口型时间轴。

    Args:
        text: 要朗读的文本（应已去掉 [emotion:xxx] 等标签）。
        char_sec: 每字基准时长（秒）。
        start_at: 时间轴起点（秒），通常为 0。
        speed: 语速倍率（>1 更快，每字时长除以 speed）。

    Returns:
        LipFrame 列表（按时间升序）。文本为空或 pypinyin 不可用时返回 []。
    """
    if not text or not text.strip():
        return []
    if not _PYPINYIN_OK:
        logger.debug("pypinyin 不可用，口型时间轴跳过（回退振幅驱动）")
        return []
    try:
        per_char = max(0.03, float(char_sec) / max(0.1, float(speed)))
    except (TypeError, ValueError):
        per_char = DEFAULT_CHAR_SEC

    # 拿逐字声母/韵母（errors 用 identity，保证与字符一一对应）
    try:
        initials = [x[0] for x in _pinyin(
            text, style=Style.INITIALS, errors=lambda s: list(s))]
        finals = [x[0] for x in _pinyin(
            text, style=Style.FINALS, errors=lambda s: list(s))]
    except Exception as e:
        logger.debug("pypinyin 解析失败，跳过口型时间轴: %s", e)
        return []

    n = len(text)
    # 数量不一致时保守放弃（宁可回退，也不要错位）
    if len(initials) != n or len(finals) != n:
        logger.debug("拼音数(%d/%d)与字符数(%d)不符，放弃时间轴",
                     len(initials), len(finals), n)
        return []

    frames: list[LipFrame] = []
    t = float(start_at)
    for ch, init, fin in zip(text, initials, finals):
        if _is_punctuation(ch):
            dur = _pause_for(ch)
            frames.append(LipFrame(t, t + dur, 0.0, 0.0))
            t += dur
            continue
        open_v, form_v = _char_shape(ch, init, fin)
        frames.append(LipFrame(t, t + per_char, open_v, form_v))
        t += per_char
    return frames


def total_duration(frames: list[LipFrame]) -> float:
    """时间轴总时长（秒）。"""
    return frames[-1].end if frames else 0.0


def sample(frames: list[LipFrame], at: float) -> Optional[tuple[float, float]]:
    """取 `at` 秒处的 (开口度, 唇形)。超出范围返回 None。

    二分查找，供每帧调用（渲染热路径）。
    """
    if not frames:
        return None
    lo, hi = 0, len(frames) - 1
    if at < frames[0].start or at > frames[-1].end:
        return None
    while lo <= hi:
        mid = (lo + hi) // 2
        f = frames[mid]
        if at < f.start:
            hi = mid - 1
        elif at > f.end:
            lo = mid + 1
        else:
            return f.mouth_open, f.mouth_form
    return None
