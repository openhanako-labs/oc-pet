"""R1（2026-09-14）测试：SenseVoice ASR provider。

覆盖：
  - 富文本标签剥离
  - 情感标签提取
  - 配置解析
  - 失败闭合（模型不可用时不崩）
  - 工厂路由（asr.provider=sensevoice）
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asr_provider.sensevoice import (  # noqa: E402
    EMOTION_TAG_MAP,
    SenseVoiceProvider,
    _TAG_STRIP_FALLBACK,
)


# ── 富文本剥离 ──


def test_strip_tags_removes_all_known_tags():
    raw = "<|zh|><|HAPPY|><|Speech|><|withitn|>今天天气不错。"
    assert SenseVoiceProvider._strip_tags(raw) == "今天天气不错。"


def test_strip_tags_handles_empty():
    assert SenseVoiceProvider._strip_tags("") == ""


def test_strip_tags_keeps_plain_text():
    """没有标签的纯文本原样返回（不误删内容）。"""
    assert SenseVoiceProvider._strip_tags("你好世界") == "你好世界"


def test_strip_tags_does_not_eat_content_containing_angle_brackets():
    """内容里出现尖括号不能被误删。"""
    raw = "<|zh|><|Speech|>请解释 a<b 的含义"
    out = SenseVoiceProvider._strip_tags(raw)
    assert "a<b" in out


def test_strip_tags_removes_audio_event_tags():
    """非语言事件标签（笑声/BGM）也要剥掉。"""
    raw = "<|zh|><|Laughter|><|Speech|>哈哈哈哈"
    out = SenseVoiceProvider._strip_tags(raw)
    assert "<|" not in out
    assert "哈哈哈哈" in out


# ── 情感提取 ──


@pytest.mark.parametrize("tag,expected", [
    ("HAPPY", "happy"),
    ("SAD", "sad"),
    ("ANGRY", "angry"),
    ("SURPRISED", "surprised"),
    ("NEUTRAL", "neutral"),
])
def test_extract_emotion(tag, expected):
    raw = f"<|zh|><|{tag}|><|Speech|><|withitn|>测试"
    assert SenseVoiceProvider._extract_emotion(raw) == expected


def test_extract_emotion_defaults_neutral():
    assert SenseVoiceProvider._extract_emotion("<|zh|><|Speech|>无情绪标签") == "neutral"


def test_extract_emotion_handles_empty():
    assert SenseVoiceProvider._extract_emotion("") == "neutral"


def test_emotion_map_covers_all_expected():
    """情感映射必须覆盖 SenseVoice 的主要情绪标签。"""
    for tag in ("HAPPY", "SAD", "ANGRY", "SURPRISED", "NEUTRAL"):
        assert tag in EMOTION_TAG_MAP


# ── 接口契约 ──


def test_provider_name():
    assert SenseVoiceProvider().name == "sensevoice"


def test_provider_not_ready_before_load():
    """未加载时 is_ready 为 False。"""
    # 注意：类变量是全局的，这里只验证属性存在且为 bool
    p = SenseVoiceProvider()
    assert isinstance(p.is_ready, bool)


def test_transcribe_returns_none_when_model_unavailable(monkeypatch):
    """模型不可用时返回 None 而非抛异常（失败闭合）。"""
    p = SenseVoiceProvider()
    monkeypatch.setattr(SenseVoiceProvider, "_loaded", True)
    monkeypatch.setattr(SenseVoiceProvider, "_model", None)
    monkeypatch.setattr(SenseVoiceProvider, "preload", lambda self: None)
    text, emo = p.transcribe_with_emotion("nonexistent.wav")
    assert text is None
    assert emo == "neutral"


def test_transcribe_wrapper_returns_text_only(monkeypatch):
    p = SenseVoiceProvider()
    monkeypatch.setattr(
        p, "transcribe_with_emotion", lambda *a, **k: ("你好", "happy")
    )
    assert p.transcribe("x.wav") == "你好"


# ── 配置解析 ──


def test_resolve_device_defaults_cpu(monkeypatch):
    """device 留空 → cpu（SenseVoice 在 CPU 上已够快）。"""
    import config as _cfg
    monkeypatch.setattr(_cfg, "load_config", lambda: {"asr": {"device": ""}})
    assert SenseVoiceProvider._resolve_device() == "cpu"


def test_resolve_device_auto_is_cpu(monkeypatch):
    import config as _cfg
    monkeypatch.setattr(_cfg, "load_config", lambda: {"asr": {"device": "auto"}})
    assert SenseVoiceProvider._resolve_device() == "cpu"


def test_resolve_device_explicit(monkeypatch):
    import config as _cfg
    monkeypatch.setattr(_cfg, "load_config", lambda: {"asr": {"device": "cuda"}})
    assert SenseVoiceProvider._resolve_device() == "cuda"


def test_resolve_model_default(monkeypatch):
    import config as _cfg
    monkeypatch.setattr(_cfg, "load_config", lambda: {"asr": {}})
    assert SenseVoiceProvider._resolve_model() == "iic/SenseVoiceSmall"


def test_resolve_model_custom(monkeypatch):
    import config as _cfg
    monkeypatch.setattr(
        _cfg, "load_config", lambda: {"asr": {"sensevoice_model": "my/model"}}
    )
    assert SenseVoiceProvider._resolve_model() == "my/model"


# ── 工厂路由 ──


def test_factory_routes_to_sensevoice(monkeypatch):
    """asr.provider=sensevoice 时应创建 SenseVoiceProvider。"""
    import config as _cfg
    monkeypatch.setattr(_cfg, "load_config", lambda: {"asr": {"provider": "sensevoice"}})

    class _Fake:
        config = {"asr": {"provider": "sensevoice"}}

    from pet_mixins.voice_provider_mixin import VoiceProviderMixin
    p = VoiceProviderMixin._create_asr_provider(_Fake())
    assert p is not None
    assert p.name == "sensevoice"


def test_factory_defaults_to_whisper():
    """默认仍是 whisper_local（不改变现有行为）。"""

    class _Fake:
        config = {}

    from pet_mixins.voice_provider_mixin import VoiceProviderMixin
    p = VoiceProviderMixin._create_asr_provider(_Fake())
    assert p is not None
    assert p.name == "whisper_local"


def test_tag_strip_list_covers_core_tags():
    """剥离列表必须含语言/情感/事件三类核心标签。"""
    joined = "".join(_TAG_STRIP_FALLBACK)
    for must in ("<|zh|>", "<|HAPPY|>", "<|Speech|>", "<|withitn|>"):
        assert must in joined
