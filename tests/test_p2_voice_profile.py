# -*- coding: utf-8 -*-
"""P2-7 语音身份/音色 回归测试

覆盖：
1. voice_profile 配置解析：角色音色（voices / voice_per_agent）、情绪音色
   （voice_emotion_map，含 provider 细分）、优先级与回退链、白名单校验。
2. [emotion:xxx] 标签解析。
3. 各 TTS provider synthesize 的 voice 参数透传（MIMO/API/Edge 用 stub 网络，
   CosyVoice 直接测 _resolve_voice 音色解析）。
4. conversation_engine 合成前按「角色+情绪」解析音色并透传。

全程无真实 TTS 服务：网络调用一律 monkeypatch，CosyVoice 不拉起子进程。
"""
from __future__ import annotations

import base64
import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tts_provider.voice_profile import (
    MIMO_KNOWN_VOICES,
    get_agent_voice_map,
    get_emotion_voice_map,
    parse_emotion_tag,
    provider_valid_voices,
    resolve_voice,
)


# ── voice_profile: 配置解析 ─────────────────────────────────────

def test_resolve_voice_agent_map():
    """角色音色映射：tts.voices[agent_id] 直接命中。"""
    cfg = {"voices": {"miku": "ophelia", "yuexinmiao": "luoqixi"}}
    assert resolve_voice(cfg, "cosyvoice", "miku") == "ophelia"
    assert resolve_voice(cfg, "cosyvoice", "yuexinmiao") == "luoqixi"


def test_resolve_voice_voice_per_agent_alias():
    """voice_per_agent 作为 voices 的别名同样生效。"""
    cfg = {"voice_per_agent": {"miku": "ophelia"}}
    assert get_agent_voice_map(cfg) == {"miku": "ophelia"}
    assert resolve_voice(cfg, "cosyvoice", "miku") == "ophelia"


def test_resolve_voice_emotion_map():
    """情绪音色映射：voice_emotion_map[emotion] 命中。"""
    cfg = {"voice_emotion_map": {"happy": "zh-CN-XiaoyiNeural", "sad": "zh-CN-XiaoxiaoNeural"}}
    assert resolve_voice(cfg, "edge", "", "happy") == "zh-CN-XiaoyiNeural"
    assert resolve_voice(cfg, "edge", "", "sad") == "zh-CN-XiaoxiaoNeural"


def test_resolve_voice_agent_precedence_over_emotion():
    """角色音色优先于情绪音色（身份 > 语气）。"""
    cfg = {
        "voices": {"miku": "ophelia"},
        "voice_emotion_map": {"happy": "zh-CN-XiaoyiNeural"},
    }
    assert resolve_voice(cfg, "cosyvoice", "miku", "happy") == "ophelia"


def test_resolve_voice_no_config_default():
    """无任何配置 → 返回 ""（provider 默认，向后兼容）。"""
    assert resolve_voice({}, "edge", "miku", "happy") == ""
    assert resolve_voice(None, "edge", "miku", "happy") == ""
    assert resolve_voice({"voices": {}}, "edge", "miku", "happy") == ""
    assert resolve_voice({"voices": {"other": "x"}}, "edge", "miku") == ""


def test_resolve_voice_invalid_agent_falls_back_to_emotion():
    """角色音色非法（不在 provider 白名单）→ 沿回退链落到情绪音色。"""
    cfg = {
        # "ophelia" 是 CosyVoice 音色，配到 edge 上非法
        "voices": {"miku": "ophelia"},
        "voice_emotion_map": {"happy": "zh-CN-XiaoyiNeural"},
    }
    edge_valid = provider_valid_voices("edge")
    assert resolve_voice(cfg, "edge", "miku", "happy", valid_voices=edge_valid) == "zh-CN-XiaoyiNeural"


def test_resolve_voice_all_invalid_fallback_default():
    """全部候选音色非法 → 返回 ""（用 provider 默认，不改变当前发声）。"""
    cfg = {"voices": {"miku": "ophelia"}}  # 对 edge 非法
    edge_valid = provider_valid_voices("edge")
    assert resolve_voice(cfg, "edge", "miku", "", valid_voices=edge_valid) == ""


def test_resolve_voice_provider_scoped_emotion_map():
    """情绪音色支持按引擎细分：voice_emotion_map[provider] 优先，全局兜底。"""
    cfg = {
        "voice_emotion_map": {
            "edge": {"happy": "zh-CN-XiaoyiNeural"},
            "happy": "zh-CN-XiaoxiaoNeural",  # 全局兜底
        },
    }
    assert resolve_voice(cfg, "edge", "", "happy") == "zh-CN-XiaoyiNeural"
    # 其他引擎无细分 → 用全局兜底
    assert resolve_voice(cfg, "mimo", "", "happy") == "zh-CN-XiaoxiaoNeural"
    # 未配置情绪 → ""
    assert resolve_voice(cfg, "edge", "", "angry") == ""


def test_get_emotion_voice_map_provider_scoped():
    """get_emotion_voice_map 的 provider 细分读取。"""
    cfg = {"voice_emotion_map": {"edge": {"happy": "A"}, "happy": "B", "sad": "C"}}
    assert get_emotion_voice_map(cfg, "edge") == {"happy": "A"}
    assert get_emotion_voice_map(cfg, "mimo") == {"happy": "B", "sad": "C"}
    assert get_emotion_voice_map({}, "edge") == {}
    assert get_emotion_voice_map(None, "edge") == {}


# ── voice_profile: 白名单 ──────────────────────────────────────

def test_provider_valid_voices_edge():
    valid = provider_valid_voices("edge")
    assert valid is not None
    assert "zh-CN-XiaoxiaoNeural" in valid
    assert "zh-CN-YunxiNeural" in valid
    assert "ophelia" not in valid  # CosyVoice 音色不属于 edge


def test_provider_valid_voices_mimo():
    valid = provider_valid_voices("mimo")
    assert valid is not None
    assert "default_zh" in valid
    assert "冰糖" in valid
    assert MIMO_KNOWN_VOICES == valid


def test_provider_valid_voices_api_none():
    """api 后端音色各异，不做白名单校验。"""
    assert provider_valid_voices("api") is None
    assert provider_valid_voices("cosyvoice") is None
    assert provider_valid_voices("unknown") is None


# ── voice_profile: [emotion:xxx] 标签解析 ──────────────────────

def test_parse_emotion_tag():
    assert parse_emotion_tag("别熬太晚 [emotion:concerned]") == "concerned"
    assert parse_emotion_tag("[emotion: happy] 你好呀") == "happy"
    assert parse_emotion_tag("[ emotion : thinking ] 嗯…") == "thinking"
    assert parse_emotion_tag("先 [emotion:happy] 再 [emotion:sad]") == "sad"  # 取最后一个
    assert parse_emotion_tag("没有任何标签") is None
    assert parse_emotion_tag("") is None
    assert parse_emotion_tag(None) is None


# ── provider synthesize voice 透传 ─────────────────────────────

def test_mimo_synthesize_voice_override(monkeypatch, tmp_path):
    import tts_provider.mimo_tts as mimo_mod
    monkeypatch.setattr(mimo_mod, "OUTPUT_DIR", tmp_path)
    prov = mimo_mod.MimoTtsProvider()
    prov.configure(base_url="http://tts.invalid/v1", api_key="k", voice="default_zh")
    prov.preload()
    assert prov.is_ready

    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"audio": {"data": base64.b64encode(b"RIFFfake").decode()}}}]}

        @property
        def text(self):
            return ""

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return _Resp()

    monkeypatch.setattr(mimo_mod.requests, "post", _fake_post)

    # 显式 voice 参数 → 透传到请求体
    out = prov.synthesize("你好呀", voice="default_en")
    assert out is not None
    assert captured["payload"]["audio"]["voice"] == "default_en"
    # 未提供 voice → 用 provider 默认
    out2 = prov.synthesize("你好呀", voice="")
    assert out2 is not None
    assert captured["payload"]["audio"]["voice"] == "default_zh"


def test_api_synthesize_voice_override(monkeypatch, tmp_path):
    import tts_provider.api_tts as api_mod
    monkeypatch.setattr(api_mod, "OUTPUT_DIR", tmp_path)
    prov = api_mod.ApiTtsProvider()
    prov._cfg = {"base_url": "http://tts.invalid/v1", "api_key": "k",
                 "model": "tts-1", "voice": "alloy", "format": "wav"}
    prov._ready = True

    captured: dict = {}

    class _Resp:
        status_code = 200
        content = b"RIFFfake"

        @property
        def text(self):
            return ""

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["payload"] = json
        return _Resp()

    monkeypatch.setattr(api_mod.requests, "post", _fake_post)

    out = prov.synthesize("hi", voice="nova")
    assert out is not None
    assert captured["payload"]["voice"] == "nova"
    prov.synthesize("hi", voice="")
    assert captured["payload"]["voice"] == "alloy"


def test_edge_synthesize_voice_override(monkeypatch, tmp_path):
    import sys as _sys
    import types

    import tts_provider.edge_tts as edge_mod
    monkeypatch.setattr(edge_mod, "OUTPUT_DIR", tmp_path)

    captured: dict = {}

    class _FakeCommunicate:
        def __init__(self, text, voice, rate="+0%", pitch="+0Hz"):
            captured["text"] = text
            captured["voice"] = voice

        async def save(self, path):
            Path(path).write_bytes(b"ID3fake")

    fake_mod = types.ModuleType("edge_tts")
    fake_mod.Communicate = _FakeCommunicate
    monkeypatch.setitem(_sys.modules, "edge_tts", fake_mod)

    prov = edge_mod.EdgeTtsProvider()
    out = prov.synthesize("你好呀", voice="zh-CN-YunxiNeural")
    assert out is not None
    assert captured["voice"] == "zh-CN-YunxiNeural"
    prov.synthesize("你好呀", voice="")
    assert captured["voice"] == edge_mod.DEFAULT_VOICE


def test_cosyvoice_resolve_voice_override():
    """CosyVoice 音色解析：显式 voice 覆盖角色映射，未提供时维持角色映射。"""
    from tts_provider.cosyvoice import CosyVoiceProvider

    prov = CosyVoiceProvider()
    prov._speaker_refs = {
        "luoqixi": {"ref_audio": "luoqixi.wav", "ref_text": "你好"},
        "ophelia": {"ref_audio": "ophelia.wav", "ref_text": "hello"},
    }
    prov._speakers = []
    prov._voice_map = {"miku": "luoqixi"}

    # 角色映射（无显式 voice）→ luoqixi
    info, vid = prov._resolve_voice("miku")
    assert vid == "luoqixi"
    assert info["ref_audio"] == "luoqixi.wav"

    # 显式 voice 覆盖（情绪/上层映射）→ ophelia
    info2, vid2 = prov._resolve_voice("miku", "ophelia")
    assert vid2 == "ophelia"
    assert info2["ref_audio"] == "ophelia.wav"

    # 未配置角色 → default
    _info3, vid3 = prov._resolve_voice("unknown")
    assert vid3 == "default"


# ── conversation_engine: 合成前按「角色+情绪」解析音色并透传 ────

class _FakeTTS:
    """记录 synthesize 调用的最小 TTS provider 桩。"""

    name = "fake"
    is_ready = True
    last_error = ""

    def __init__(self):
        self.calls: list[dict] = []

    def synthesize(self, text, character_id="", instruct="", voice="", emotion=""):
        self.calls.append({
            "text": text, "character_id": character_id,
            "instruct": instruct, "voice": voice, "emotion": emotion,
        })
        return "/tmp/fake_voice.wav"


def _make_engine(voice_resolver=None):
    """构造最小可用的 ConversationEngine 桩（跳过 __init__ 副作用）。"""
    from core.conversation_engine import ConversationEngine
    eng = object.__new__(ConversationEngine)
    eng._lock = threading.Lock()
    eng._tts = _FakeTTS()
    eng._tts_ready = True
    eng._tts_in_use = 0
    eng._generation = 0
    eng._voice_resolver = voice_resolver
    eng.on_status = lambda msg: None
    eng.on_reply = lambda *a: None
    return eng


def test_engine_passes_resolved_voice_to_synthesize():
    eng = _make_engine(voice_resolver=lambda agent, emotion: {
        "miku": "ophelia",
    }.get(agent, ""))
    eng._synth_and_reply("你好", "happy", "idle", "miku", "开心", "user", 0)
    assert eng._tts.calls, "应至少调用一次 synthesize"
    call = eng._tts.calls[-1]
    assert call["character_id"] == "miku"
    assert call["voice"] == "ophelia"
    assert call["instruct"] == "开心"


def test_engine_emotion_resolver_used():
    eng = _make_engine(voice_resolver=lambda agent, emotion: {
        "happy": "zh-CN-XiaoyiNeural",
    }.get(emotion, ""))
    eng._synth_and_reply("好耶", "happy", "idle", "miku", "开心", "user", 0)
    assert eng._tts.calls[-1]["voice"] == "zh-CN-XiaoyiNeural"


def test_engine_no_resolver_uses_default_voice():
    """未挂解析器（向后兼容）→ voice 传 ""，由 provider 用默认音色。"""
    eng = _make_engine(voice_resolver=None)
    eng._synth_and_reply("你好", "neutral", "idle", "miku", "", "user", 0)
    assert eng._tts.calls[-1]["voice"] == ""


def test_engine_resolver_exception_falls_back_default():
    """解析器抛异常 → 回退 ""（provider 默认），不阻塞合成。"""
    def _boom(agent, emotion):
        raise RuntimeError("resolver broken")

    eng = _make_engine(voice_resolver=_boom)
    eng._synth_and_reply("你好", "happy", "idle", "miku", "开心", "user", 0)
    assert eng._tts.calls[-1]["voice"] == ""
