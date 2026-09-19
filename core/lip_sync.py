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
import math
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


# ── 口型平滑（起音 / 收音）────────────────────────────
#
# 为什么要它：`sample()` 给的是**阶梯**——每个字保持恒定开口度 0.345s，
# 字与字之间一帧跳变（写入 `SetParameterValue(..., 1.0)`，SDK 不插值）。
# 而振幅那条路径**本来就有**非对称包络（`k = 0.55 if level > env else 0.18`，
# 起快落慢）——音素这条路没有，于是一个 0.95 的 /a/ 接到 0.30 的 /i/ 会硬切。
#
# 对齐 AgentAtelierR：40ms 起音 / 90ms 收音（收比起慢）。
# 这里的“秒”是**到达约 95% 目标**所需的时间，所以时间常数 τ = 秒/3。
MOUTH_ATTACK_S = 0.04
MOUTH_RELEASE_S = 0.09
_TAU_RATIO = 1.0 / 3.0

# 开口度上限（审美旋钮，默认 1.0 = 不压）。
# AgentAtelierR 用 55% 避免“每次都张满嘴”；oc-pet 的 /a/ 是自己调到 0.95 的
# （miku 模型实测值）——所以**不做默认压制**，留给你按自己眼睛调。
_mouth_peak: float = 1.0


def set_mouth_peak(peak) -> float:
    """设置开口度上限（0~1）。无效输入回退 1.0（不压）。返回生效值。"""
    global _mouth_peak
    try:
        v = float(peak)
    except (TypeError, ValueError):
        v = 1.0
    if v <= 0.0 or v > 1.0:
        v = 1.0
    _mouth_peak = v
    logger.info("口型开口上限 = %.2f%s", v, "（不压）" if v >= 1.0 else "")
    return v


def get_mouth_peak() -> float:
    """当前开口度上限（默认 1.0）。"""
    return _mouth_peak


def slew(cur: float, target: float, dt: float, *, attack_s: float = MOUTH_ATTACK_S,
         release_s: float = MOUTH_RELEASE_S, tau_ratio: float = _TAU_RATIO) -> float:
    """指数逆近一步：把阶梯型目标磨成有起音/收音的曲线。

    与帧率无关（用 dt 而不是固定系数）：``cur += (target-cur) * (1-exp(-dt/τ))``。
    升（``target > cur``）用 ``attack_s``，降用 ``release_s``——**收比起慢**，
    这是人说话的样子。

    Args:
        cur: 上一步的值。
        target: 目标值。
        dt: 距上一步的秒数；<=0 或非法时视为“无时间信息”→ 直接跳到目标
            （宁可不管，也不要把嘴卡在旧值上）。
        attack_s / release_s: 到达约 95% 目标所需秒数；<=0 表示该方向不平滑。
        tau_ratio: 秒 → 时间常数的换算比（默认 1/3）。

    Returns:
        新的当前值（与输入同为浮点，不做 0~1 截断——截断是调用方的事）。
    """
    try:
        cur = float(cur)
        target = float(target)
        dt = float(dt)
    except (TypeError, ValueError):
        return target
    if dt <= 0.0 or not (dt == dt):          # 无 dt（NaN 也走这）→ 不做平滑
        return target
    reach = attack_s if target > cur else release_s
    try:
        reach = float(reach)
    except (TypeError, ValueError):
        reach = 0.0
    if reach <= 0.0:
        return target
    tau = max(1e-4, reach * float(tau_ratio))
    alpha = 1.0 - math.exp(-dt / tau)
    return cur + (target - cur) * alpha


# ── 语速与时长 ──

# 每字基准时长（秒）。
#
# 2026-09-14 实测校正：原估 0.18 s/字，实际 TTS 输出为 **0.345 s/字**
# （实测：14 字文本 → 58 帧 @12Hz = 4.83s）。原值导致口型时间轴比音频短 48%，
# 表现为"口型不贴、提前走完"。
#
# 该值必须与 TTS 实际语速一致；若换音色/语速导致口型不同步，先校这里。
# 自检方法：口型时间轴总时长应与日志里的 "TTS 流式合成完成: N chunks"
# 换算的音频时长相近（N chunks ≈ 8 + 25*(N-1) 帧 @12Hz）。
DEFAULT_CHAR_SEC = 0.345

# 标点停顿（秒）——嘴闭合，模拟换气/断句
#
# 注：TTS 实际会自己读出停顿，这里的值需与之一致；
# 标点已包含在总字数里（计时按字符走），所以停顿取较小值避免重复计入。
PAUSE_SEC: dict[str, float] = {
    "。": 0.28, "！": 0.28, "？": 0.28, "…": 0.28,
    "，": 0.14, "、": 0.12, "；": 0.18, "：": 0.18,
    ".": 0.28, "!": 0.28, "?": 0.28,
    ",": 0.14, ";": 0.18, ":": 0.18,
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
