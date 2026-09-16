"""情绪 → TTS 引擎提示（语气层，全引擎通用）。

各引擎接受的情绪表达方式不同，本模块把它们统一到一套词汇表：

| 引擎 | 接受形式 | 本模块提供 |
|---|---|---|
| Edge TTS | SSML prosody（pitch/volume） | ``prosody_for()`` |
| Qwen / MiMo / CosyVoice | ``instruct`` 自然语言 | ``instruct_for()`` |
| OpenAI 兼容 API | 仅 speed | （不硬塞——speed 会改语速） |

**共同硬规矩**：

    **语速绝对不能变。只允许通过 pitch / volume / 语气描述改变语气。**

为什么：语速是"这个人说话的样子"里最稳的特征。一改语速，听感从
"她心情变了"变成"换了一个人在说话"——情绪调节反而破坏角色一致性。

调用方::

    from tts_provider.emotion_prosody import prosody_for, instruct_for
    pitch, volume = prosody_for("happy")   # Edge 用
    hint = instruct_for("happy")           # Qwen / MiMo / CosyVoice 用
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


# ── instruct 提示（Qwen / MiMo / CosyVoice 的公共入口）──
#
# 这些引擎走自然语言指令而非数值 prosody。指令里**绝不能提语速**
# （“说快一点”会让听感变成另一个人），只描述语气与音色质感。
#
# 用词注意：避开“快/慢”字，哪怕“轻快”“稍慢”这种词也可能被模型
# 当成语速指令（测试会抳）。
_INSTRUCT: dict[str, str] = {
    "neutral":   "用平常的语气自然地说",
    "happy":     "用明朗、上扬的语气说，声音里带点笑意",
    "cute":      "用柔软、亲昵的语气说，声音放轻一些",
    "surprised": "用略带惊讶的语气说，语调上扬",
    "thinking":  "用平稳、沉思的语气说，语气放松",
    "sad":       "用低沉、放轻的语气说，声音里带点疲惫",
    "angry":     "用加重、压低的语气说，字字分明",
    "missing":   "用温柔而略显牵挂的语气说",
    "working":   "用专注、干脆的语气说",
}

# 别名 → instruct 表的键（与 prosody 表共用同一套归一逻辑）
_INSTRUCT_FALLBACK: dict[str, str] = {
    "excited": "happy", "joy": "happy", "joyful": "happy",
    "soft": "cute", "gentle": "cute", "shy": "cute",
    "annoyed": "angry", "frustrated": "angry", "furious": "angry",
    "curious": "thinking", "confused": "thinking", "calm": "thinking",
    "tired": "sad", "sleepy": "sad", "lonely": "sad",
    "worried": "missing", "anxious": "missing",
    "busy": "working", "eager": "working",
}


def instruct_for(emotion: str) -> str:
    """情绪 → 自然语言语气指令。未知情绪返回空串（不干扰引擎默认行为）。

    空串是**故意的**：引擎对 ``instruct`` 为空时的默认表现往往已经不错，
    强行塞一句 generic 的"自然地说话"反而可能拉低质量。

    neutral 也返回空串——中性不需要额外指令。
    """
    key = (emotion or "").strip().lower()
    if not key or key == "neutral":
        return ""
    if key in _INSTRUCT:
        return _INSTRUCT[key]
    mapped = _ALIASES.get(key) or _INSTRUCT_FALLBACK.get(key)
    if mapped and mapped != "neutral":
        return _INSTRUCT.get(mapped, "")
    return ""
