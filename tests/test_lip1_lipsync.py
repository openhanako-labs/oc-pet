"""LIP-1（2026-09-14）测试：音素级口型（文本 → 口型时间轴）。

覆盖：
  - 时间轴生成（中文/英文/标点/混合）
  - 韵母 → 开口度/唇形映射
  - 标点停顿
  - sample() 二分查找
  - 失败闭合（空文本 / pypinyin 缺失）
  - 渲染器接线（三级优先：音素 → 振幅 → 正弦）
  - 播放时钟（基于实际消费字节，非墙钟）
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.lip_sync import (  # noqa: E402
    DEFAULT_CHAR_SEC,
    LipFrame,
    build_timeline,
    sample,
    total_duration,
)


# ── 时间轴生成 ──


def test_build_timeline_basic():
    fr = build_timeline("对话")
    assert len(fr) == 2
    assert fr[0].start == 0.0
    assert fr[1].start == pytest.approx(fr[0].end)


def test_build_timeline_empty_text():
    assert build_timeline("") == []
    assert build_timeline("   ") == []


def test_build_timeline_none_text():
    assert build_timeline(None) == []


def test_timeline_is_contiguous():
    """片段必须首尾相接，不留空洞（否则采样会掉到缝里）。"""
    fr = build_timeline("你好世界，测试。")
    for a, b in zip(fr, fr[1:]):
        assert b.start == pytest.approx(a.end), "时间轴有空洞"


def test_timeline_is_ascending():
    fr = build_timeline("你好世界")
    for a, b in zip(fr, fr[1:]):
        assert b.start >= a.start
        assert b.end >= b.start


def test_char_sec_scales_duration():
    fast = build_timeline("你好世界", char_sec=0.1)
    slow = build_timeline("你好世界", char_sec=0.3)
    assert total_duration(fast) < total_duration(slow)


def test_speed_scales_duration():
    normal = build_timeline("你好世界")
    quick = build_timeline("你好世界", speed=2.0)
    assert total_duration(quick) < total_duration(normal)


def test_invalid_char_sec_falls_back():
    """非法 char_sec 不能崩，回退默认值。"""
    fr = build_timeline("你好", char_sec="abc")
    assert fr and total_duration(fr) > 0


def test_start_at_offsets_timeline():
    fr = build_timeline("你好", start_at=1.0)
    assert fr[0].start == 1.0


# ── 韵母映射（核心价值）──


def test_open_vowels_differ_from_closed():
    """★ 核心：a/i/u 的开口度必须明显不同（振幅级做不到这点）。"""
    a = build_timeline("啊")[0]
    i = build_timeline("衣")[0]
    u = build_timeline("乌")[0]
    assert a.mouth_open > 0.8, f"a 应大开口，实为 {a.mouth_open}"
    assert i.mouth_open < 0.5, f"i 应小开口，实为 {i.mouth_open}"
    assert a.mouth_open > i.mouth_open
    assert a.mouth_open > u.mouth_open


def test_rounded_vowels_have_negative_form():
    """u/o 是圆唇 → mouth_form 为负。"""
    u = build_timeline("乌")[0]
    assert u.mouth_form < -0.2, f"u 应圆唇（负），实为 {u.mouth_form}"


def test_spread_vowels_have_positive_form():
    """i 是扁唇 → mouth_form 为正。"""
    i = build_timeline("衣")[0]
    assert i.mouth_form > 0.1, f"i 应扁唇（正），实为 {i.mouth_form}"


def test_rounded_vs_spread_differ():
    u = build_timeline("乌")[0]
    i = build_timeline("衣")[0]
    assert u.mouth_form < i.mouth_form


# ── 标点 ──


def test_punctuation_closes_mouth():
    fr = build_timeline("好。")
    last = fr[-1]
    assert last.mouth_open == 0.0, "标点应闭嘴"
    assert last.mouth_form == 0.0


def test_period_pauses_longer_than_comma():
    p = build_timeline("好。")[-1]
    c = build_timeline("好，")[-1]
    assert p.duration > c.duration


def test_space_is_pause():
    fr = build_timeline("a b")
    mid = fr[1]
    assert mid.mouth_open == 0.0


# ── 英文/数字 ──


def test_english_letters_have_shapes():
    fr = build_timeline("abc")
    assert len(fr) == 3
    # 'a' 是元音，应比 'b' 开口大
    assert fr[0].mouth_open > fr[1].mouth_open


def test_digits_get_shape():
    fr = build_timeline("123")
    assert len(fr) == 3
    assert all(f.mouth_open > 0 for f in fr)


def test_mixed_cn_en():
    fr = build_timeline("你好 winnt")
    assert len(fr) > 4


# ── sample() ──


def test_sample_at_start():
    fr = build_timeline("你好")
    r = sample(fr, 0.0)
    assert r is not None


def test_sample_out_of_range_returns_none():
    fr = build_timeline("你好")
    assert sample(fr, -1.0) is None
    assert sample(fr, 999.0) is None


def test_sample_empty_timeline():
    assert sample([], 0.0) is None


def test_sample_picks_correct_frame():
    """采样必须命中对应片段的形状（二分查找正确性）。"""
    fr = build_timeline("啊衣")
    a_open = fr[0].mouth_open
    i_open = fr[1].mouth_open
    mid_a = (fr[0].start + fr[0].end) / 2
    mid_i = (fr[1].start + fr[1].end) / 2
    assert sample(fr, mid_a)[0] == pytest.approx(a_open)
    assert sample(fr, mid_i)[0] == pytest.approx(i_open)


def test_sample_at_boundary():
    fr = build_timeline("啊衣")
    # 边界点应能取到值（不返回 None）
    assert sample(fr, fr[0].end) is not None


def test_lip_frame_duration():
    f = LipFrame(1.0, 1.5, 0.5, 0.0)
    assert f.duration == pytest.approx(0.5)


def test_char_sec_matches_measured_tts_speed():
    """★ 字速必须是实测值（2026-09-14 修正）。

    实测：14 字文本 → 58 帧 @12Hz = 4.83s，即 0.345 s/字。
    原估 0.18 导致时间轴短 48% → 口型提前走完、不贴。
    """
    assert DEFAULT_CHAR_SEC == pytest.approx(0.345, abs=0.001)


def test_timeline_duration_close_to_real_audio():
    """★ 时间轴总时长应与实测音频时长接近（误差 < 20%）。"""
    # 实测样本：'晚上好。安心的，那就安心着。' → 58 帧 @12Hz = 4.83s
    text = "晚上好。安心的，那就安心着。"
    fr = build_timeline(text)
    dur = total_duration(fr)
    real = 58 / 12.0
    assert abs(dur - real) / real < 0.20, \
        f"时间轴 {dur:.2f}s 与实测音频 {real:.2f}s 偏差过大"


def test_char_sec_scales_proportionally():
    """字速翻倍 → 时长翻倍（保证它是线性因子）。"""
    a = total_duration(build_timeline("你好世界", char_sec=0.2))
    b = total_duration(build_timeline("你好世界", char_sec=0.4))
    assert b == pytest.approx(a * 2, rel=0.01)


# ── 渲染器接线 ──


class _FakeStd:
    ParamMouthOpenY = "MouthOpenY"
    ParamMouthForm = "MouthForm"


class _FakeModel:
    def __init__(self):
        self.writes = []

    def SetParameterValue(self, pid, val, weight):
        self.writes.append((pid, val, weight))


def _mk_renderer():
    from avatar.live2d_renderer import Live2DRenderer
    r = Live2DRenderer.__new__(Live2DRenderer)
    r._model = _FakeModel()
    r._speaking = True
    r._mouth_phase = 0.0
    r._mouth_env = 0.0
    r._mouth_level_fn = lambda: None
    r._lip_frames = []
    r._lip_clock_fn = None
    r._lip_started_at = 0.0
    r._mouth_lip_open = 0.0
    r._mouth_lip_form = 0.0
    r._live2d = type("L", (), {"StandardParams": _FakeStd})()
    r._note_frame_failure = lambda *a, **k: None
    return r


def _mouth_open_of(r):
    """取最近一次写入的 ParamMouthOpenY 值（按参数名筛选，不取 writes[-1]）。"""
    for pid, val, _w in reversed(r._model.writes):
        if pid == _FakeStd.ParamMouthOpenY:
            return val
    raise AssertionError("未写入 ParamMouthOpenY")


def test_renderer_without_timeline_uses_level():
    """无时间轴 + 有电平 → 振幅级（与 LIP-1 之前一致）。"""
    r = _mk_renderer()
    r._mouth_level_fn = lambda: 0.4
    for _ in range(10):
        r._update_mouth()
    val = _mouth_open_of(r)
    assert 0 < val <= 1


def test_renderer_with_timeline_uses_lip_shape():
    """★ 有音素时间轴 → 嘴型由发音决定（不同音不同嘴型）。"""
    r = _mk_renderer()
    r._mouth_level_fn = lambda: 0.3
    fr = build_timeline("啊")
    r.set_lip_timeline(fr, clock_fn=lambda: fr[0].start + 0.01)
    r._update_mouth()
    val_a = _mouth_open_of(r)

    r2 = _mk_renderer()
    r2._mouth_level_fn = lambda: 0.3
    fr2 = build_timeline("衣")
    r2.set_lip_timeline(fr2, clock_fn=lambda: fr2[0].start + 0.01)
    r2._update_mouth()
    val_i = _mouth_open_of(r2)

    assert val_a > val_i, f"啊({val_a}) 应比 衣({val_i}) 张嘴更大"


def test_renderer_writes_mouth_form_unconditionally():
    """★ 唇形必须无条件写入（包括 0）——否则标点/静音段会残留上一字的唇形。"""
    r = _mk_renderer()
    r._mouth_level_fn = lambda: 0.3
    fr = build_timeline("乌")  # 圆唇，form 为负
    r.set_lip_timeline(fr, clock_fn=lambda: fr[0].start + 0.01)
    r._update_mouth()
    forms = [v for pid, v, _ in r._model.writes if pid == _FakeStd.ParamMouthForm]
    assert forms and forms[-1] < -0.2, f"乌 应写入负唇形，实为 {forms}"


def test_renderer_clears_timeline():
    r = _mk_renderer()
    r.set_lip_timeline(build_timeline("啊"))
    assert r._lip_frames
    r.clear_lip_timeline()
    assert r._lip_frames == []


def test_renderer_timeline_out_of_range_falls_back():
    """时间轴用尽（时钟超出）→ 回退振幅，不僵住。"""
    r = _mk_renderer()
    r._mouth_level_fn = lambda: 0.5
    fr = build_timeline("啊")
    r.set_lip_timeline(fr, clock_fn=lambda: 999.0)
    r._update_mouth()  # 不应抛异常
    assert r._model.writes


def test_renderer_clock_exception_falls_back():
    """时钟抛异常 → 回退，不崩帧循环。"""
    r = _mk_renderer()
    r._mouth_level_fn = lambda: 0.5

    def boom():
        raise RuntimeError("clock dead")

    r.set_lip_timeline(build_timeline("啊"), clock_fn=boom)
    r._update_mouth()
    assert r._model.writes


def test_renderer_stops_resets_lip_state():
    r = _mk_renderer()
    r._mouth_lip_open = 0.8
    r._speaking = False
    r._update_mouth()
    assert r._mouth_lip_open == 0.0


def test_renderer_sine_fallback_still_works():
    """无时间轴无电平 → 正弦回退（行为不变）。"""
    r = _mk_renderer()
    r._update_mouth()
    v1 = _mouth_open_of(r)
    r._update_mouth()
    v2 = _mouth_open_of(r)
    assert v1 != v2


def test_set_lip_timeline_accepts_none():
    r = _mk_renderer()
    r.set_lip_timeline(None)
    assert r._lip_frames == []


# ── 播放时钟 ──


def test_played_seconds_uses_consumed_bytes():
    """★ 时钟基于实际消费字节，而非墙钟（否则卡顿时口型会跑到声音前面）。"""
    import inspect
    from ui import streaming_pcm_player as sp
    src = inspect.getsource(sp.StreamingPcmPlayer.played_seconds)
    assert "played_bytes" in src
    assert "time.monotonic" not in src, "不应基于墙钟"


def test_consumed_bytes_accumulates():
    """readData 每取走一块，消费字节数应累加。"""
    import inspect
    from ui import streaming_pcm_player as sp
    src = inspect.getsource(sp._PcmStreamDevice.readData)
    assert "consumed_bytes" in src


def test_consumed_bytes_reset_on_prepare():
    import inspect
    from ui import streaming_pcm_player as sp
    src = inspect.getsource(sp.StreamingPcmPlayer.prepare)
    assert "consumed_bytes = 0" in src, "新一句必须重置时钟"
