"""情绪 → TTS prosody 映射（语气层）。

借自 Amadeus-for-Hana 的做法，核心是那条硬规矩：

    **语速绝对不能变。只允许通过 pitch / volume 改变语气语调。**

为什么：语速是"这个人说话的样子"里最稳的特征。一改语速，听感从
"她心情变了"变成"换了一个人在说话"——情绪调节反而破坏角色一致性。

所以这里的 rate 一律 `+0%`，只用 pitch（音高）和 volume（响度）做区分：

- **pitch**：情绪的能量高低。开心/惊讶上扬，难过/失落下沉。
- **volume**：情绪的外放程度。生气/兴奋更响，低落/温柔更轻。

适用范围：所有走 edge-tts 的音色。``<mstts:express-as>`` style 仅
``ja-JP-NanamiNeural`` 支持（Azure 官方语言支持表），中文音色没有
可用 style，因此本表**只输出 prosody**，不做 style 注入。

用法::

    from tts_provider.emotion_prosody import prosody_for
    pitch, volume = prosody_for("happy")     # ('+8Hz', '+6%')
"""
from __future__ import annotations

import re

# 情绪 → (pitch 增量, volume 增量)，均相对用户的全局基线叠加。
#
# 设计标尺（与 avatar/live2d_renderer._EMOTION_VA 的 VA 坐标同向）：
#   valence 高 → pitch 上抬；arousal 高 → volume 加大
# 各档留出差距但不夸张——超过 ±24Hz 会明显"换人"（Amadeus 的调参笔记）。
_EMOTION_PROSODY: dict[str, tuple[str, str]] = {
    # ── 渲染器核心七情绪（live2d_renderer._EMOTION_VA 的键）──
    "neutral":   ("+0Hz",  "+0%"),
    "happy":     ("+8Hz",  "+6%"),
    "cute":      ("+5Hz",  "+2%"),
    "surprised": ("+18Hz", "+10%"),
    "thinking":  ("+3Hz",  "-4%"),
    "sad":       ("-12Hz", "-8%"),
    "angry":     ("-2Hz",  "+14%"),

    # ── 关键词检测器会吐出的额外情绪（core/hanako_monitor.EMOTION_KEYWORDS）──
    "missing":   ("-6Hz",  "-6%"),
    "working":   ("+4Hz",  "+4%"),

    # ── 常见别名（模型可能自创，归一后落到这里）──
    "excited":   ("+20Hz", "+14%"),
    "joy":       ("+10Hz", "+7%"),
    "soft":      ("-4Hz",  "-6%"),
    "gentle":    ("-4Hz",  "-6%"),
    "annoyed":   ("-1Hz",  "+10%"),
    "frustrated":("-2Hz",  "+12%"),
    "worried":   ("-5Hz",  "-4%"),
    "anxious":   ("+2Hz",  "+6%"),
    "curious":   ("+10Hz", "+4%"),
    "confused":  ("+6Hz",  "-2%"),
    "tired":     ("-8Hz",  "-8%"),
    "sleepy":    ("-10Hz", "-10%"),
}

# 别名 → 有 prosody 的情绪名。
#
# 只放 ``_EMOTION_PROSODY`` 里**没有**的名字——已经在上表里的
# （如 joy / soft / annoyed / tired）不必再列，否则是被遮蔽的死条目。
_ALIASES: dict[str, str] = {
    # → happy
    "joyful": "happy", "delighted": "happy", "glad": "happy",
    "cheerful": "happy", "pleased": "happy", "content": "happy",
    # → sad
    "sadness": "sad", "sorrow": "sad", "depressed": "sad", "down": "sad",
    "upset": "sad", "lonely": "sad", "disappointed": "sad",
    "melancholy": "sad", "heartbroken": "sad",
    # → angry
    "mad": "angry", "furious": "angry", "irritated": "angry", "rage": "angry",
    "cross": "angry",
    # → surprised
    "shocked": "surprised", "amazed": "surprised", "astonished": "surprised",
    "wow": "surprised",
    # → thinking
    "calm": "thinking", "focused": "thinking", "pondering": "thinking",
    "deliberate": "thinking", "neutral_face": "thinking",
    # → cute
    "affectionate": "cute", "playful": "cute", "shy": "cute", "blush": "cute",
    "sweet": "cute",
    # → missing
    "longing": "missing", "homesick": "missing",
    # → working
    "busy": "working", "productive": "working", "eager": "working",
}

# 校验：prosody 值形如 "+8Hz" / "-12%" / "+0%"
_PROSODY_RE = re.compile(r"^[+-]\d+(?:\.\d+)?(?:Hz|%)$")


def normalize_emotion(emotion: str) -> str:
    """情绪名归一：小写去空白，别名映射到规范情绪。未知返回 "neutral"。"""
    key = (emotion or "").strip().lower()
    if not key:
        return "neutral"
    if key in _EMOTION_PROSODY:
        return key
    return _ALIASES.get(key, "neutral")


def prosody_for(emotion: str) -> tuple[str, str]:
    """情绪 → ``(pitch, volume)``。未知情绪回落 neutral（不影响出声）。

    **rate 不在此表内**——语速恒定是硬约束，见模块 docstring。
    """
    key = normalize_emotion(emotion)
    return _EMOTION_PROSODY.get(key, _EMOTION_PROSODY["neutral"])


def add_prosody(base: str, delta: str) -> str:
    """把 ``'+8Hz'`` 与 ``'+4Hz'`` 相加 → ``'+12Hz'``。

    单位不一致或任一不可解析时，返回 ``delta``（宁可只取情绪增量，
    也不要拼出一个非法字符串导致 Edge TTS 报错）。
    """
    m1 = re.match(r"^([+-]?\d+(?:\.\d+)?)(Hz|%)$", (base or "").strip())
    m2 = re.match(r"^([+-]?\d+(?:\.\d+)?)(Hz|%)$", (delta or "").strip())
    if not m1 or not m2 or m1.group(2) != m2.group(2):
        return delta or base
    num = float(m1.group(1)) + float(m2.group(1))
    unit = m1.group(2)
    return f"{num:+.0f}{unit}"


def is_valid_prosody(value: str) -> bool:
    """``'+8Hz'`` 合法；``''`` / ``'fast'`` / ``'8'`` 不合法。"""
    return bool(_PROSODY_RE.match((value or "").strip()))


def rate_for(emotion: str) -> str:
    """恒返回 ``'+0%'`` —— 存在的唯一理由是让"语速不变"可被显式断言。"""
    return "+0%"
