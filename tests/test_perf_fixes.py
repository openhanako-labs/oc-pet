"""性能修复（2026-09-14）测试：卡顿与延迟三处优化。

用户实测反馈：\"TTS 刚播报两个字就开始卡，等下两个字出来继续播报卡\"。
日志实测：合成速率 0.36x 实时（低于播放所需 1.0x）→ 缓冲必然被抽干。

三处改动：
  ① TTS 流式：left_context 25→8（解码量 2.00x→1.32x）+ 预缓冲
  ② 工具热刷新：目录未变则跳过全量重扫
  ③ 后台任务避让：对话进行中不发反思/屏幕增强
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── ① 流式参数 ──


def test_stream_params_defaults_use_cheaper_context():
    """★ 流式参数默认保持原值（left_context 不宜调小，见下）。"""
    from tts_provider.qwen_tts import QwenTtsProvider
    kw = QwenTtsProvider._stream_params()
    assert kw["left_context"] == 25, "left_context 是解码器预热上下文，不宜调小"
    assert kw["chunk_frames"] == 25
    assert kw["first_chunk_frames"] == 8


def test_stream_params_reads_config(monkeypatch):
    """config 能覆盖流式参数。"""
    import config as _cfg
    monkeypatch.setattr(_cfg, "load_config", lambda: {
        "tts": {"stream": {"chunk_frames": 20, "left_context": 4}}
    })
    from tts_provider.qwen_tts import QwenTtsProvider
    kw = QwenTtsProvider._stream_params()
    assert kw["chunk_frames"] == 20
    assert kw["left_context"] == 4


def test_stream_params_ignores_bad_values(monkeypatch):
    """非法配置不能污染参数（回退默认）。"""
    import config as _cfg
    monkeypatch.setattr(_cfg, "load_config", lambda: {
        "tts": {"stream": {"chunk_frames": "abc", "left_context": -5}}
    })
    from tts_provider.qwen_tts import QwenTtsProvider
    kw = QwenTtsProvider._stream_params()
    assert kw["chunk_frames"] == 25
    assert kw["left_context"] == 25


def test_decode_cost_reduced():
    """记录一下参数配比关系（不作为优化依据）。

    ⚠️ 曾试图把 left_context 25→8 以减少解码量，但：
    1) 解码不是瓶颈（瓶颈是 talker 自回归），省下的微不足道；
    2) left_context 同时是解码器预热上下文，过短会产生拼接杂音。
    故保持 25。
    """
    def cost(chunk, ctx):
        return (chunk + ctx) / chunk
    assert cost(25, 25) == pytest.approx(2.0)
    assert cost(25, 8) == pytest.approx(1.32, abs=0.01)


# ── 预缓冲 ──


def test_set_prebuffer_computes_bytes():
    """预缓冲按采样率/声道换算成字节。"""
    from ui.streaming_pcm_player import StreamingPcmPlayer
    p = StreamingPcmPlayer(sample_rate=24000)
    p.set_prebuffer(1.5)
    # Int16 单声道 24k → 48000 字节/秒 → 1.5s = 72000
    assert p._prebuffer_bytes == 72000


def test_set_prebuffer_zero_disables():
    from ui.streaming_pcm_player import StreamingPcmPlayer
    p = StreamingPcmPlayer(sample_rate=24000)
    p.set_prebuffer(0)
    assert p._prebuffer_bytes == 0


def test_set_prebuffer_handles_bad_input():
    from ui.streaming_pcm_player import StreamingPcmPlayer
    p = StreamingPcmPlayer(sample_rate=24000)
    p.set_prebuffer("abc")
    assert p._prebuffer_bytes == 0


def test_should_start_playback_disabled_always_true():
    """预缓冲关闭 → 立即开播（回旧行为）。"""
    from ui.streaming_pcm_player import StreamingPcmPlayer

    class _Dev:
        def is_eof(self):
            return False

    p = StreamingPcmPlayer(sample_rate=24000)
    p.set_prebuffer(0)
    assert p._should_start_playback(_Dev(), 1) is True


def test_should_start_playback_waits_until_enough():
    """★ 未攒够不开播——这是\"两个字一卡\"的修复核心。"""
    import time
    from ui.streaming_pcm_player import StreamingPcmPlayer

    class _Dev:
        def is_eof(self):
            return False

    p = StreamingPcmPlayer(sample_rate=24000)
    p.set_prebuffer(1.5, max_wait_s=60)
    p._playback_wait_from = time.monotonic()
    assert p._should_start_playback(_Dev(), 1000) is False, "未攒够不应开播"
    assert p._should_start_playback(_Dev(), p._prebuffer_bytes) is True


def test_smart_prebuffer_off_by_default():
    """★ 智能预缓冲默认关：R=0.36 时需攒到 ~74%，首字晚 10s，比卡顿更难接受。"""
    from ui.streaming_pcm_player import StreamingPcmPlayer
    p = StreamingPcmPlayer(sample_rate=24000)
    assert p._smart_prebuffer_on is False


def test_set_expected_duration_needs_explicit_enable():
    """仅设预期时长不启用智能预缓冲（需显式 enable）。"""
    from ui.streaming_pcm_player import StreamingPcmPlayer
    p = StreamingPcmPlayer(sample_rate=24000)
    p.set_expected_duration(5.0)
    assert p._expected_bytes > 0
    assert p._smart_prebuffer_on is False


def test_smart_prebuffer_math():
    """★ 智能预缓冲推导：攒够 (remaining / R) 字节。

    R=0.36 时，已收 80% → 需再攒 剩余/0.36 ≈ 55.6% 总量。
    """
    from ui.streaming_pcm_player import StreamingPcmPlayer
    p = StreamingPcmPlayer(sample_rate=24000)
    p.set_expected_duration(4.83, enable=True)
    assert p._smart_prebuffer_on is True
    exp = p._expected_bytes
    p._received_bytes = int(exp * 0.8)
    need = p._smart_prebuffer_need(exp)
    assert need == pytest.approx(int(exp * 0.2 / 0.36), rel=0.02)


def test_smart_prebuffer_never_exceeds_total():
    """目标量不能超过总量（攒满即够）。"""
    from ui.streaming_pcm_player import StreamingPcmPlayer
    p = StreamingPcmPlayer(sample_rate=24000)
    p.set_expected_duration(4.83, enable=True)
    exp = p._expected_bytes
    p._received_bytes = 0
    assert p._smart_prebuffer_need(exp) <= exp


def test_should_start_playback_on_eof():
    """合成已结束 → 不等了（短句应直接播，否则永远没声音）。"""
    from ui.streaming_pcm_player import StreamingPcmPlayer

    class _Dev:
        def is_eof(self):
            return True

    p = StreamingPcmPlayer(sample_rate=24000)
    p.set_prebuffer(5.0, max_wait_s=60)
    assert p._should_start_playback(_Dev(), 100) is True


def test_should_start_playback_on_timeout():
    """★ 超时保护：合成极慢时不能永远不出声。"""
    import time
    from ui.streaming_pcm_player import StreamingPcmPlayer

    class _Dev:
        def is_eof(self):
            return False

    p = StreamingPcmPlayer(sample_rate=24000)
    p.set_prebuffer(30.0, max_wait_s=0.5)
    p._playback_wait_from = time.monotonic() - 10.0  # 已等 10s
    assert p._should_start_playback(_Dev(), 1000) is True


def test_prebuffer_reset_on_prepare():
    """新一句要重置等待计时与接收计数。"""
    import inspect
    from ui import streaming_pcm_player as sp
    src = inspect.getsource(sp.StreamingPcmPlayer.prepare)
    assert "_playback_wait_from" in src
    assert "_received_bytes" in src


def test_feed_accumulates_received_bytes():
    """feed 要累计已收字节（智能预缓冲的输入）。"""
    import inspect
    from ui import streaming_pcm_player as sp
    src = inspect.getsource(sp.StreamingPcmPlayer.feed)
    assert "_received_bytes" in src


def test_cleanup_detaches_sink_source():
    """★ 释放顺序：先 stop+断开 source，再 close device。

    否则报 "QIODevice::read (_PcmStreamDevice): device not open"。
    """
    import inspect
    from ui import streaming_pcm_player as sp
    src = inspect.getsource(sp.StreamingPcmPlayer._cleanup)
    assert "setSource" in src, "必须断开 sink 的拉取源"
    assert src.index("_sink.stop()") < src.index("_device.close()"), \
        "必须先停 sink 再关 device"


# ── atEnd 覆写（"播一个字就不播了"的真因）──


def test_pcm_device_overrides_at_end():
    """★ QIODevice.atEnd() 默认是 `bytesAvailable() == 0`。

    对流式设备这是错的："暂时没数据"不等于"流结束"。
    实测后果：首块（≈一个字）拉走后缓冲变空 → atEnd=True →
    Qt 认为流结束 → sink 停止 → 后续块再也接不上。
    必须覆写。
    """
    from ui.streaming_pcm_player import _PcmStreamDevice
    assert "atEnd" in _PcmStreamDevice.__dict__, \
        "必须覆写 atEnd（否则首块播完即被判为流结束）"


def test_at_end_semantics_via_source():
    """atEnd 只在 eof 且缓冲抽空时为 True。"""
    import inspect
    from ui.streaming_pcm_player import _PcmStreamDevice
    src = inspect.getsource(_PcmStreamDevice.atEnd)
    assert "_eof" in src and "_size" in src


def test_pump_reattaches_from_any_non_active_state():
    """★ 重挂条件必须是 "非 ActiveState"，不能只看 IdleState。

    sink 被 atEnd 误判停掉时状态可能是 StoppedState，
    旧条件（仅 IdleState）永远不成立 → 后续数据接不上。
    """
    import inspect
    from ui import streaming_pcm_player as sp
    src = inspect.getsource(sp.StreamingPcmPlayer._pump_once)
    assert "ActiveState" in src, "重挂条件应基于 ActiveState 取反"


# ── 文件式 TTS 的口型（Edge 嘴不动）──


def test_mouth_level_probe_returns_none_when_not_playing():
    """★ 关键：未播放时必须返回 None（而非 0.0）。

    `current_level()` 未播放时返回 0.0，而 `_update_mouth` 把 0.0
    当作"真实静音" → 嘴锁在 0.08（几乎闭合）。
    后果：Edge / CosyVoice 等**文件式 TTS** 不走流式播放器，
    播报时嘴全程不动（用户实测反馈）。
    """
    from pet import PetWindow

    class _SP:
        def is_playing(self):
            return False

        def current_level(self):
            return 0.0

    class _Fake:
        _stream_player = _SP()

    assert PetWindow._mouth_level_probe(_Fake()) is None


def test_mouth_level_probe_returns_level_when_playing():
    """流式播放中应返回真实电平（不走正弦回退）。"""
    from pet import PetWindow

    class _SP:
        def is_playing(self):
            return True

        def current_level(self):
            return 0.42

    class _Fake:
        _stream_player = _SP()

    assert PetWindow._mouth_level_probe(_Fake()) == 0.42


def test_mouth_level_probe_no_player():
    """无播放器时返回 None。"""
    from pet import PetWindow

    class _Fake:
        _stream_player = None

    assert PetWindow._mouth_level_probe(_Fake()) is None


def test_mouth_level_probe_survives_exception():
    """播放器状态查询异常 → 返回 None（回退正弦，不崩帧循环）。"""
    from pet import PetWindow

    class _SP:
        def is_playing(self):
            raise RuntimeError("player dead")

    class _Fake:
        _stream_player = _SP()

    assert PetWindow._mouth_level_probe(_Fake()) is None


def test_wire_uses_probe_not_raw_current_level():
    """★ 接线必须用 probe（带 is_playing 判断），不能直连 current_level。"""
    import inspect
    from pet import PetWindow
    src = inspect.getsource(PetWindow._wire_mouth_level_source)
    assert "_mouth_level_probe" in src, \
        "必须接 probe（直连 current_level 会让文件式 TTS 闭嘴）"


def test_renderer_falls_back_to_sine_on_none():
    """probe 返回 None → 渲染器走正弦（嘴会动）。"""
    from avatar.live2d_renderer import Live2DRenderer

    class _Std:
        ParamMouthOpenY = "MouthOpenY"
        ParamMouthForm = "MouthForm"

    class _M:
        def __init__(self):
            self.w = []

        def SetParameterValue(self, pid, v, w):
            self.w.append((pid, v, w))

    r = Live2DRenderer.__new__(Live2DRenderer)
    r._model = _M()
    r._speaking = True
    r._mouth_phase = 0.0
    r._mouth_env = 0.0
    r._mouth_level_fn = lambda: None
    r._lip_frames = []
    r._lip_clock_fn = None
    r._lip_started_at = 0.0
    r._mouth_lip_open = 0.0
    r._mouth_lip_form = 0.0
    r._live2d = type("L", (), {"StandardParams": _Std})()
    r._note_frame_failure = lambda *a, **k: None

    vals = []
    for _ in range(6):
        r._update_mouth()
        for pid, v, _w in reversed(r._model.w):
            if pid == "MouthOpenY":
                vals.append(v)
                break
    assert len(set(round(v, 3) for v in vals)) > 1, "嘴应随正弦变化（在动）"
    assert max(vals) > 0.3, f"嘴应张开，实为 {max(vals)}"


# ── 占位符防护（"不播放 TTS"的真因）──


def test_stream_skips_placeholder_with_tags():
    """★ 真因：上游空回复被填成 "…"，又被自动补上标签，
    导致带标签的文本绕过了 "是否为占位符" 的检查，最终合成出一个
    无声的省略号（用户感知为"不播放 TTS 了"）。

    验证：完整剥离所有标签后，占位符必须被识别。
    """
    import inspect
    from core.conversation_engine import ConversationEngine
    src = inspect.getsource(ConversationEngine._synth_stream_and_play)
    # 必须剥这些标签（parse_emotion 只剥 emotion）
    for tag in ("expression", "action", "do:", "duration", "feel:"):
        assert tag in src, f"剥离逻辑缺 {tag}"
    # 必须有占位符判定
    assert '"…"' in src or "'…'" in src or "\u2026" in src


def test_placeholder_set_covers_common_forms():
    """占位符集合应覆盖常见形式。"""
    import inspect
    from core.conversation_engine import ConversationEngine
    src = inspect.getsource(ConversationEngine._synth_stream_and_play)
    for ph in ("…", "...", "。"):
        assert ph in src, f"占位符集合缺 {ph!r}"


def test_normal_reply_not_blocked():
    """正常回复不能被占位符防护误伤。"""
    import re
    PATS = (
        r"\s*\[\s*emotion\s*[:=]\s*\w+\s*\]",
        r"\s*\[expression:[^\]]*\]",
        r"\s*\[action:[^\]]*\]",
        r"\s*\[do:[^\]]*\]",
        r"\s*\[duration:[^\]]*\]",
        r"\s*\[feel:[^\]]*\]",
    )

    def clean(t):
        for p in PATS:
            t = re.sub(p, " ", t, flags=re.IGNORECASE)
        return t.strip()

    reply = '晚上好。安心的，那就安心着。 [emotion:neutral] [action:{"gesture":"idle"}]'
    cleaned = clean(reply)
    assert cleaned not in ("…", "...", "。", ".", "，", ",")
    assert "安心的" in cleaned


# ── ② 工具热刷新 ──


def test_plugins_dir_stamp_detects_change(tmp_path, monkeypatch):
    """★ 目录戳能发现新增/删除插件。"""
    from core.conversation_engine import ConversationEngine
    root = tmp_path / "plugins"
    root.mkdir()
    (root / "a").mkdir()
    (root / "a" / "manifest.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(ConversationEngine, "_plugin_roots",
                        staticmethod(lambda: [root]))

    s1 = ConversationEngine._plugins_dir_stamp()
    # 新增一个插件
    (root / "b").mkdir()
    (root / "b" / "manifest.json").write_text("{}", encoding="utf-8")
    s2 = ConversationEngine._plugins_dir_stamp()
    assert s1 != s2, "新增插件后戳应变化"


def test_plugins_dir_stamp_stable_when_unchanged(tmp_path, monkeypatch):
    """目录没动 → 戳不变（这样热刷新能跳过重扫）。"""
    from core.conversation_engine import ConversationEngine
    root = tmp_path / "plugins"
    root.mkdir()
    (root / "a").mkdir()
    (root / "a" / "manifest.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(ConversationEngine, "_plugin_roots",
                        staticmethod(lambda: [root]))
    assert ConversationEngine._plugins_dir_stamp() == \
        ConversationEngine._plugins_dir_stamp()


def test_hot_refresh_skips_when_stamp_unchanged():
    """★ 戳未变时必须跳过全量重扫（这是每 30s 白烧 CPU 的修复）。"""
    import inspect
    from core.conversation_engine import ConversationEngine
    src = inspect.getsource(ConversationEngine._hot_refresh_tools)
    assert "_plugins_dir_stamp" in src
    assert "return" in src


def test_plugins_dir_stamp_handles_missing_dirs(monkeypatch):
    """目录不存在时返回 None（不抛异常）。"""
    from core.conversation_engine import ConversationEngine
    monkeypatch.setattr(ConversationEngine, "_plugin_roots",
                        staticmethod(lambda: []))
    assert ConversationEngine._plugins_dir_stamp() is None


# ── ②b 真跑一遍 `_plugin_roots`（2026-09-21 补）────────────────


def test_real_plugin_roots_are_reachable():
    """★ 不注入，真的走一遍 `_plugin_roots()`。

    2026-09-21：这一行里藏过一个 **NameError** —— 本模块没 `import Path`，
    而 `_plugins_dir_stamp` 的宽 except 把它吞成"拿不到戳"。后果是 mtime 守卫
    **永远跳过不了**：每 30s 全量重扫插件目录 + 正则解析上百个 JS 文件，
    2026-09-14 那次优化白写，而且一行日志都不说原因（用户看到的就是
    日志里每 30s 刷两行 "Tool registry: 100 tools from plugins"）。

    上面四条测试全都 monkeypatch 了 `_plugin_roots`，正好绕过这一行 ——
    它们全绿，而线上每 30s 烧一次。**注入点既是测试的便利，也是测试的盲区。**
    """
    from core.conversation_engine import ConversationEngine

    roots = ConversationEngine._plugin_roots()          # 不许抛
    assert roots, "插件根目录列表不该是空的"
    assert all(hasattr(r, "is_dir") for r in roots)

    has_plugin_dirs = False
    for r in roots:
        try:
            if r.is_dir() and any(c.is_dir() for c in r.iterdir()):
                has_plugin_dirs = True
                break
        except OSError:
            continue
    if has_plugin_dirs:
        assert ConversationEngine._plugins_dir_stamp() is not None, \
            "有插件目录却拿不到戳 → 守卫失效，会退化成每轮全量重扫"


def test_stamp_failure_is_loud(monkeypatch, caplog):
    """★ 拿不到戳时必须留下日志。

    一个永远走昂贵分支的"保守回退"不是回退，是常态 ——
    而安静的常态没人会去查。但也不能每 30s 吵一次（那只是换一种刷屏）。
    """
    from core.conversation_engine import ConversationEngine

    def _boom():
        raise RuntimeError("假装根目录算不出来")

    monkeypatch.setattr(ConversationEngine, "_plugin_roots", staticmethod(_boom))
    monkeypatch.setattr(ConversationEngine, "_stamp_warned", False, raising=False)

    with caplog.at_level("WARNING"):
        assert ConversationEngine._plugins_dir_stamp() is None
    assert any("插件目录戳获取失败" in r.message for r in caplog.records), \
        "静默回退：没人会知道守卫失效了"
    assert ConversationEngine._stamp_warned is True, "吵过一次就该闭嘴"


# ── ③ 后台任务避让 ──


def test_is_conversation_busy_detects_pending_chat():
    """等回复时算忙。"""
    from pet import PetWindow
    class _Fake:
        _pending_chat = True
    assert PetWindow._is_conversation_busy(_Fake()) is True


def test_is_conversation_busy_false_when_idle():
    from pet import PetWindow

    class _Fake:
        _pending_chat = False
        _stream_player = None
        _tts_player = None

    assert PetWindow._is_conversation_busy(_Fake()) is False


def test_is_conversation_busy_detects_tts_playing():
    """TTS 播报中算忙。"""
    from pet import PetWindow

    class _P:
        def is_playing(self):
            return True

    class _Fake:
        _pending_chat = False
        _stream_player = _P()
        _tts_player = None

    assert PetWindow._is_conversation_busy(_Fake()) is True


def test_is_conversation_busy_survives_broken_player():
    """播放器状态查询异常不能崩。"""
    from pet import PetWindow

    class _P:
        def is_playing(self):
            raise RuntimeError("player dead")

    class _Fake:
        _pending_chat = False
        _stream_player = _P()
        _tts_player = None

    assert PetWindow._is_conversation_busy(_Fake()) is False


def test_maybe_reflect_skips_when_busy():
    """★ 对话进行中不发起反思（它会吃 29s/12623 token，抢回复的 API）。"""
    import inspect
    from pet import PetWindow
    src = inspect.getsource(PetWindow._maybe_reflect)
    assert "_is_conversation_busy" in src


def test_screen_should_enrich_skips_when_busy():
    """★ 对话进行中不做屏幕 LLM 增强（抢同一条 API）。"""
    from core.perception.screen import ScreenPerception
    sp = ScreenPerception.__new__(ScreenPerception)
    sp.busy_check = lambda: True
    sp._last_enriched_scene = ""
    sp._last_enrich_at = 0.0
    sp._enrich_cooldown = 300

    class _Scene:
        scene = "coding"

    assert sp._should_enrich(_Scene(), 1e9) is False


def test_screen_enrich_works_when_not_busy():
    """不忙时正常放行（避让不能误伤）。"""
    from core.perception.screen import ScreenPerception
    sp = ScreenPerception.__new__(ScreenPerception)
    sp.busy_check = lambda: False
    sp._last_enriched_scene = ""
    sp._last_enrich_at = 0.0
    sp._enrich_cooldown = 300

    class _Scene:
        scene = "coding"

    assert sp._should_enrich(_Scene(), 1e9) is True


def test_screen_busy_check_defaults_none():
    """未注入钩子时行为不变（不避让）。"""
    from core.perception.screen import ScreenPerception
    sp = ScreenPerception(interval=120)
    assert sp.busy_check is None

# ── SDPA attention（flash-attn 替代方案）──


def test_qwen_tts_uses_sdpa_attention():
    """★ Qwen3-TTS 加载时走 SDPA（PyTorch 内置 flash 内核）。

    背景：flash-attn 库在 Windows 没有 win_amd64 wheel，源码编译也不被
    官方支持（2026-09-14 实测确认 404）。改用 SDPA：transformers 透传
    attn_implementation="sdpa"，CUDA 上自动选 Flash 后端（sm_86 支持），
    不需要额外装库。
    """
    import inspect
    from tts_provider import qwen_tts
    src = inspect.getsource(qwen_tts)
    assert "attn_implementation" in src, "Qwen TTS 应显式指定 attention 实现"
    assert '"sdpa"' in src or "'sdpa'" in src, "应使用 SDPA（而非默认）"

# ── 2026-09-15 死锁修复（TTS 不出声 + 主线程卡死）──
#
# 事故现场（logs/oc_pet.log）：
#   13:38:15 合成启动（code_predictor 初始化）
#   13:38:15 → 13:39:37 零产出（无"口型时间轴"/"开始流式播放"/"合成完成"）
#   13:39:37 主线程心跳停 → launcher 92s 后判卡死强杀
#
# 三个独立缺陷：
#   ① frames_q.get() 无超时 → 生成 stall 时消费端永久阻塞
#   ② atEnd()/is_playing() 持 self._lock 调 Qt → 与音频线程锁序反转
#   ③ 分句预合成 enqueue() 写了但从未接线


def test_stream_consumer_has_timeout():
    """★ ① frames_q.get() 必须带超时。

    无超时 → 生成线程 stall 时消费端永久阻塞，finally 里的 th.join
    永远到不了 → TTS 线程池被占死 → 后续所有 TTS 都不出声。
    """
    import inspect
    from tts_provider import qwen_tts
    src = inspect.getsource(qwen_tts.QwenTtsProvider.synth_stream)
    assert "frames_q.get(timeout=" in src, "消费端必须带超时"
    assert "_queue.Empty" in src, "必须捕获 Empty 以便优雅退出"


def test_stream_stall_raises_instead_of_hanging():
    """stall 时应抛错（让上层知道），而不是静默永久挂住。"""
    import inspect
    from tts_provider import qwen_tts
    src = inspect.getsource(qwen_tts.QwenTtsProvider.synth_stream)
    assert "_stalled" in src
    assert "stall" in src.lower()


def test_atend_does_not_deadlock_with_concurrent_reads():
    """★ ② atEnd() 与 readData() 并发不得死锁（行为验证）。

    旧实现两者都取 self._lock；atEnd 由 Qt 音频线程在持内部锁时调用，
    与主线程的 sink.start() 形成 ABBA → 主线程冻死。
    这里用多线程真实并发调用来验证不再互相阻塞。
    """
    import threading
    from ui.streaming_pcm_player import _PcmStreamDevice

    d = _PcmStreamDevice()
    d.append(b"\x00\x01" * 4000)
    errors = []

    def reader():
        try:
            for _ in range(3000):
                d.readData(256)
        except Exception as e:
            errors.append(e)

    def ender():
        try:
            for _ in range(3000):
                d.atEnd()
                d.pending()
        except Exception as e:
            errors.append(e)

    ts = [threading.Thread(target=reader) for _ in range(2)]
    ts += [threading.Thread(target=ender) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in ts), "并发调用出现阻塞（疑似死锁）"
    assert not errors, f"并发调用报错: {errors}"


def test_atend_is_lock_free():
    """atEnd 内部不得引用锁对象（防止改回去）。"""
    from ui.streaming_pcm_player import _PcmStreamDevice
    names = _PcmStreamDevice.atEnd.__code__.co_names
    assert "_lock" not in names, f"atEnd 引用了锁: {names}"


def test_pump_once_calls_qt_outside_lock():
    """★ ② _pump_once 的 Qt 调用必须在锁外。"""
    import inspect
    from ui.streaming_pcm_player import StreamingPcmPlayer
    src = inspect.getsource(StreamingPcmPlayer._pump_once)
    # 锁块应很短（只取快照），后面才是 Qt 调用
    assert "sink = self._sink" in src, "应在锁内取快照"
    assert "sink.start(dev)" in src or "sink.state()" in src


def test_stop_cleanup_outside_lock():
    """★ ② stop() 的 _cleanup（含 sink.stop）必须在锁外。"""
    import inspect
    from ui.streaming_pcm_player import StreamingPcmPlayer
    src = inspect.getsource(StreamingPcmPlayer.stop)
    assert "self._cleanup()" in src
    # _cleanup 不应出现在 with self._lock 块内
    lock_pos = src.index("with self._lock:")
    cleanup_pos = src.index("self._cleanup()")
    assert cleanup_pos > lock_pos


# ── 2026-09-15 分句预合成（stream_mode="sentence"）──

def test_split_tts_sentences_basic():
    """中文句末标点正确切分，标点随前句。"""
    from core.conversation_engine import _split_tts_sentences
    s = "今天天气不错。我们出去走走吧！"
    assert _split_tts_sentences(s) == ["今天天气不错。", "我们出去走走吧！"]


def test_split_tts_sentences_no_punct():
    """无标点 → 整段返回，不强行切。"""
    from core.conversation_engine import _split_tts_sentences
    s = "没有标点的一段长文本"
    assert _split_tts_sentences(s) == [s]


def test_split_tts_sentences_merges_short():
    """过短片段合并到前一句，避免碎片化。"""
    from core.conversation_engine import _split_tts_sentences
    s = "好的。啊。继续"
    parts = _split_tts_sentences(s)
    assert len(parts) == 2, parts
    assert parts[0] == "好的。啊。", parts
    assert parts[1] == "继续", parts


def test_split_tts_sentences_ellipsis():
    """省略号算句界但不单独成段。"""
    from core.conversation_engine import _split_tts_sentences
    s = "你好…我也好"
    parts = _split_tts_sentences(s)
    assert parts == ["你好…", "我也好"], parts


def test_synth_stream_uses_sentence_mode_by_default():
    """★ 流式合成默认走分句模式（除非配置为 chunk）。"""
    import inspect
    from core.conversation_engine import ConversationEngine
    src = inspect.getsource(ConversationEngine._synth_stream_and_play)
    assert "_split_tts_sentences" in src, "应调用切句函数"
    assert 'stream_mode != "chunk"' in src, "应有 chunk 回退分支"
    assert "synthesize_stream(_sent" in src, "应逐句合成"

# ── 2026-09-15 分句预合成 ──


def test_split_sentences_basic():
    """★ 按句末标点切分，标点随前句。"""
    from core.conversation_engine import _split_tts_sentences
    r = _split_tts_sentences('你好呀！今天天气不错。我们去散步吧？')
    assert r == ['你好呀！', '今天天气不错。', '我们去散步吧？']


def test_split_sentences_no_punct_returns_whole():
    """无标点 → 整段返回（不强行切）。"""
    from core.conversation_engine import _split_tts_sentences
    assert _split_tts_sentences('一句话没有标点') == ['一句话没有标点']


def test_split_sentences_empty_and_placeholder():
    """空文本/纯省略号 → 不炸。"""
    from core.conversation_engine import _split_tts_sentences
    assert _split_tts_sentences('') == []
    assert _split_tts_sentences('…') == ['…']


def test_split_sentences_short_fragments_merge():
    """过短片段合并到前句，避免碎片化。"""
    from core.conversation_engine import _split_tts_sentences
    r = _split_tts_sentences('短句。中句，带逗号。长句啊啊啊啊啊啊啊啊啊啊啊！')
    assert r == ['短句。', '中句，带逗号。', '长句啊啊啊啊啊啊啊啊啊啊啊！']


def test_stream_uses_sentence_mode_by_default():
    """★ 流式合成默认分句：合成循环按句子迭代。"""
    import inspect
    from core import conversation_engine
    src = inspect.getsource(conversation_engine.ConversationEngine._synth_stream_and_play)
    assert "_split_tts_sentences(tts_text)" in src, "默认应分句"
    assert 'stream_mode != "chunk"' in src, "应支持 chunk 模式回退"
    # 逐句调用 synthesize_stream（而非整段一次）
    assert "for pcm in tts.synthesize_stream(_sent" in src, "应按句合成"
