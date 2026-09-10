# -*- coding: utf-8 -*-
"""voice_profile — 语音身份/音色配置解析层（P2-7）。

目标：让不同桌宠角色 / 不同语气场景使用不同音色，而无需改动底层 TTS API。

纯配置层映射，只做「解析 + 校验 + 回退」，不发起任何网络 / 模型调用：

    tts.voices               = {agent_id: voice}              角色音色（身份，优先）
    tts.voice_per_agent      = {agent_id: voice}              voices 的别名（二选一）
    tts.voice_emotion_map    = {emotion: voice}               情绪音色（语气，全局）
    tts.voice_emotion_map    = {provider: {emotion: voice}}   情绪音色（按引擎细分）

解析优先级（高 → 低）：
    1. 角色音色：voices[agent_id]（/ voice_per_agent[agent_id]）
    2. 情绪音色：voice_emotion_map[emotion]（provider 细分优先，全局兜底）
    3. 空字符串 "" → 交给 provider 使用其默认音色（向后兼容）

音色白名单校验：
    当 provider 提供已知音色集合（如 edge 的 EDGE_VOICES）时，解析出的音色
    不在白名单内会被跳过并沿回退链继续；全部无效则返回 ""（用默认）。
    这样能避免把非法音色（如把 CosyVoice 的 "ophelia" 配到 edge 引擎上）
    透传给底层导致合成失败——未配置 / 非法配置都不改变当前发声。
"""
from __future__ import annotations

import logging
import re
from typing import Collection, Optional

logger = logging.getLogger(__name__)

# 引擎名集合（用于 voice_emotion_map 的 provider 细分识别）
KNOWN_PROVIDERS = frozenset({"edge", "mimo", "api", "cosyvoice", "aqua"})

# MIMO 已知音色：模块常量 MIMO_VOICES 只列了 3 个预置，实际服务端还支持
# 设置面板里的一批中文/英文音色；这里合并成完整白名单用于校验回退。
MIMO_KNOWN_VOICES = frozenset({
    "mimo_default", "default_zh", "default_en",
    "冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean",
})

# [emotion:xxx] 标签正则（与 harness_adapter.parse_emotion 对齐，支持空格变体）
_EMOTION_TAG_RE = re.compile(r"\[\s*emotion\s*:\s*(\w+)\s*\]", re.IGNORECASE)


def parse_emotion_tag(text: str) -> Optional[str]:
    """从回复文本中解析最后一个 [emotion:xxx] 标签。

    与对话引擎既有链路（harness_adapter.parse_emotion）语义一致：
    全文匹配所有标签，取最后一个；无标签返回 None。
    供 TTS 触发点在未提前解析情绪时直接取用（如完工音等旁路）。

    Args:
        text: 可能含 [emotion: happy] 等标签的回复文本

    Returns:
        情绪名（小写），无标签返回 None
    """
    if not text:
        return None
    matches = _EMOTION_TAG_RE.findall(text)
    if not matches:
        return None
    return matches[-1].lower()


def get_agent_voice_map(tts_cfg: dict) -> dict:
    """读取角色音色映射（tts.voices，兼容 tts.voice_per_agent 别名）。"""
    if not isinstance(tts_cfg, dict):
        return {}
    voices = tts_cfg.get("voices")
    if not isinstance(voices, dict):
        voices = tts_cfg.get("voice_per_agent")
    return voices if isinstance(voices, dict) else {}


def get_emotion_voice_map(tts_cfg: dict, provider: str = "") -> dict:
    """读取情绪音色映射。

    支持两种形态（provider 细分优先，全局兜底）：
      tts.voice_emotion_map = {"happy": "voiceA"}                     # 全局
      tts.voice_emotion_map = {"edge": {"happy": "voiceB"}}           # 按引擎
    """
    if not isinstance(tts_cfg, dict):
        return {}
    raw = tts_cfg.get("voice_emotion_map")
    if not isinstance(raw, dict):
        return {}
    # provider 细分：值为 dict 的键视为引擎名
    if provider and provider in raw and isinstance(raw[provider], dict):
        return dict(raw[provider])
    # 全局：只收集值为字符串的键（跳过 provider 细分 dict）
    merged: dict = {}
    for k, v in raw.items():
        if isinstance(v, str) and v:
            merged[k] = v
    return merged


def resolve_voice(
    tts_cfg: dict,
    provider: str = "",
    agent_id: str = "",
    emotion: str = "",
    valid_voices: Optional[Collection[str]] = None,
) -> str:
    """解析「该桌宠 + 该情绪」的生效音色；返回 "" 表示用 provider 默认。

    Args:
        tts_cfg: 生效的 tts 配置段（全局 + agent 级覆盖合并后）
        provider: TTS 引擎名（edge / mimo / api / cosyvoice ...），用于细分映射与白名单
        agent_id: 桌宠角色 ID（语音身份）
        emotion: 情绪名（语音语气，来自 [emotion:xxx] 标签解析）
        valid_voices: 可选白名单；解析出的音色不在白名单内会被跳过（回退链继续）

    Returns:
        音色名；"" = 无配置 / 全部非法，交由 provider 默认（向后兼容）。
    """
    if not isinstance(tts_cfg, dict):
        return ""

    # 候选回退链：角色音色 → 情绪音色
    candidates: list[str] = []
    agent_voice = get_agent_voice_map(tts_cfg).get(agent_id or "", "")
    if agent_voice:
        candidates.append(agent_voice)
    if emotion:
        emo_voice = get_emotion_voice_map(tts_cfg, provider).get(emotion, "")
        if emo_voice:
            candidates.append(emo_voice)

    for cand in candidates:
        cand = (cand or "").strip()
        if not cand:
            continue
        if valid_voices is not None and cand not in valid_voices:
            logger.debug(
                "音色 '%s' 不在 provider=%s 白名单内，跳过（继续回退）",
                cand, provider or "?",
            )
            continue
        return cand

    return ""


def provider_valid_voices(provider: str) -> Optional[frozenset]:
    """返回 provider 的已知音色白名单；None = 不做校验（后端音色不定）。

    - edge: 微软服务端固定音色表（EDGE_VOICES）——传错音色会直接合成失败，必须校验。
    - mimo: 预置 + 设置面板常见音色合并表——传错可能失败，做宽松校验。
    - api / cosyvoice / aqua: 后端音色各异 / 内部自带回退，不在此层校验。
    """
    if provider == "edge":
        try:
            from .edge_tts import EDGE_VOICES
            return frozenset(EDGE_VOICES)
        except Exception as e:
            # 失败 = 回退为「不校验音色」→ 传错音色会在合成阶段莫名失败
            logger.warning("voice_profile: EDGE_VOICES 不可用，edge 音色校验已停用: %s", e)
            return None
    if provider == "mimo":
        return MIMO_KNOWN_VOICES
    return None
