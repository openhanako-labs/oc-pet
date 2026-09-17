# -*- coding: utf-8 -*-
"""回归：external 回复的语音补合成（用户实测问题）。

## 现象（2026-09-17 14:35）

用户说「放首歌」，气泡出来了但**没有 TTS 声音**。

日志链路：
    14:31:21  处理消息: 放首歌
    14:31:42  LLM 429 限流
    14:31:43  turn failed: 429 → LLM 回复: …（占位符）
    14:35:44  origin=external 推来真回复
    14:35:44  Showing bubble: 九月下午…（无 TTS）

## 根因

`origin=external` 的回复（服务端兜底推送 / Hana 主窗口 / 插件）
走镜像路径，`_call_reply_cb(..., audio_path="", ...)` —— **不合成语音**。

于是同一句话：走本地路径时有声音，429 失败后经 external 兜底推回来
就没声音。体验不一致。

## 修法（C 方案）

external 且文本可朗读时，静默补一次本地合成：
- 交 `_tts_executor` 调 `_synth_and_reply`（复用既有合成链路）
- 合成成功 → 带 audio_path 回调 → 气泡延到 TTS 开播时显示
- 合成失败/未就绪 → 回退原行为（直接上气泡）

**三态返回**（关键设计）：
- `synthesized` — 已接管，调用方 return
- `duplicate`   — 同文本刚处理过，**整条丢弃**（防重复上气泡）
- `skip`        — 不可合成，调用方走原逻辑

**不先发纯文字回调**：否则气泡会显示两次（纯文字一次 + 合成完一次）。
代价是用户等 1-3s 才看到气泡（Edge TTS 合成时间），可接受。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeTTS:
    name = "fake"
    last_error = ""

    def __init__(self, audio=""):
        self.audio = audio
        self.calls = []

    def can_stream(self, voice):
        return False

    def synthesize(self, text, **kw):
        self.calls.append(text)
        return self.audio


class _FakeAdapter:
    @staticmethod
    def parse_emotion(text):
        return text, "neutral"


def _engine(tts=None, ready=True):
    """构造最小引擎（跳过 __init__ 副作用）。"""
    import threading
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor

    from core.conversation_engine import ConversationEngine

    e = object.__new__(ConversationEngine)
    e._lock = threading.Lock()
    e._generation = 0
    e._queue = deque(maxlen=50)
    e._tts = tts
    e._tts_ready = ready
    e._tts_in_use = 0
    e._tts_executor = ThreadPoolExecutor(max_workers=1)
    e._external_tts_dedup = {}
    e._external_tts_dedup_window = 30.0
    e._character_id = "miku"
    e._voice_resolver = None
    e._adapter = _FakeAdapter()
    e.on_reply = lambda *a, **k: None
    e.on_status = lambda msg: None
    return e


# ── 三态行为 ────────────────────────────────────────────────────────────────


def test_synthesizes_when_tts_ready():
    e = _engine(tts=_FakeTTS())
    assert e._maybe_synthesize_external("九月下午，选的是《September》。", "neutral", "idle") \
        == "synthesized"


def test_skips_when_tts_not_ready():
    """TTS 未配置/未就绪 → skip（回退纯文字，行为与改前一致）。"""
    assert _engine(tts=None, ready=False)._maybe_synthesize_external(
        "测试文本", "neutral", "idle") == "skip"
    assert _engine(tts=_FakeTTS(), ready=False)._maybe_synthesize_external(
        "测试文本", "neutral", "idle") == "skip"


def test_skips_placeholder_text():
    """占位符不合成（朗读“…”无意义）。"""
    e = _engine(tts=_FakeTTS())
    for t in ("…", "...", "", "   "):
        assert e._maybe_synthesize_external(t, "neutral", "idle") == "skip", \
            f"{t!r} 应 skip"


def test_dedups_same_text_within_window():
    """同文本在时间窗内重复推送 → duplicate（整条丢弃，不重复上气泡）。"""
    e = _engine(tts=_FakeTTS())
    assert e._maybe_synthesize_external("同一句话", "neutral", "idle") == "synthesized"
    assert e._maybe_synthesize_external("同一句话", "neutral", "idle") == "duplicate"


def test_different_texts_both_synthesized():
    e = _engine(tts=_FakeTTS())
    assert e._maybe_synthesize_external("第一句", "neutral", "idle") == "synthesized"
    assert e._maybe_synthesize_external("第二句", "neutral", "idle") == "synthesized"


def test_dedup_works_even_when_tts_not_ready():
    """去重先于就绪检查——未就绪时也防重复上气泡。"""
    e = _engine(tts=None, ready=False)
    assert e._maybe_synthesize_external("同一句话", "neutral", "idle") == "skip"
    assert e._maybe_synthesize_external("同一句话", "neutral", "idle") == "duplicate"


# ── 接线 ────────────────────────────────────────────────────────────────────


def test_handle_session_reply_uses_three_state():
    """`_handle_session_reply` 必须按三态处理（duplicate 要整条丢弃）。"""
    import inspect

    from core.conversation_engine import ConversationEngine

    src = inspect.getsource(ConversationEngine._handle_session_reply)
    assert "_maybe_synthesize_external" in src
    assert '"synthesized"' in src
    assert '"duplicate"' in src, "duplicate 必须整条丢弃（否则重复上气泡）"


def test_no_premature_text_callback():
    """不得先发纯文字回调（否则气泡显示两次）。"""
    import inspect

    from core.conversation_engine import ConversationEngine

    src = inspect.getsource(ConversationEngine._maybe_synthesize_external)
    # 去掉注释后不应有「合成前调 _call_reply_cb」
    lines = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
    code = "\n".join(lines)
    # 允许出现 _call_reply_cb，但不得出现在 submit 之前
    if "_call_reply_cb" in code:
        idx_call = code.find("_call_reply_cb")
        idx_submit = code.find("_tts_executor.submit")
        assert idx_call > idx_submit or idx_submit == -1, (
            "不应在提交合成前调 _call_reply_cb（会导致气泡显示两次）"
        )


def test_dedup_dict_bounded():
    """去重字典不应无限增长（超阈值时清理过期条目）。"""
    import inspect

    from core.conversation_engine import ConversationEngine

    src = inspect.getsource(ConversationEngine._maybe_synthesize_external)
    assert "len(self._external_tts_dedup) >" in src, "应有字典大小上限"
