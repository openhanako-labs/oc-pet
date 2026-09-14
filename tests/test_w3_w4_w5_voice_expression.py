"""W3/W4/W5（2026-09-14）测试：口型振幅驱动 + 白名单模型校验 + 体检别名匹配。

对应三项改动：
  - 口型：_update_mouth 优先跟实时 RMS 电平，无电平源回退正弦
  - W4：参数白名单按模型实际参数校验（_effective_whitelist）
  - W5：model_health 用别名元组匹配，消除命名变体误报
"""
import json
import math
import struct
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ui.streaming_pcm_player import _rms_int16  # noqa: E402
from avatar.model_health import (  # noqa: E402
    STANDARD_PARAMS,
    check_parameter_coverage,
    generate_report,
)
from avatar.live2d_renderer import Live2DRenderer  # noqa: E402

MIKU_CDI3 = (
    Path(__file__).resolve().parent.parent
    / "characters" / "miku" / "live2d" / "miku.cdi3.json"
)


# ── 口型：RMS 计算 ──


def test_rms_silence_is_zero():
    assert _rms_int16(b"\x00\x00" * 100) == 0.0


def test_rms_full_scale_is_one():
    full = struct.pack("<" + "h" * 100, *([32767] * 100))
    assert _rms_int16(full) == pytest.approx(1.0, abs=1e-3)


def test_rms_half_amplitude():
    half = struct.pack("<" + "h" * 100, *([16384] * 100))
    assert _rms_int16(half) == pytest.approx(0.5, abs=1e-3)


def test_rms_edge_cases():
    """空数据 / 奇数长度 / 非零短块都不能抛异常。"""
    assert _rms_int16(b"") == 0.0
    assert _rms_int16(b"\x01") == 0.0  # 不足一个 int16
    v = _rms_int16(b"\x01\x02\x03")  # 3 字节 → 只用前 2 字节
    assert 0.0 <= v <= 1.0


def test_rms_never_exceeds_one():
    """极端输入也不能越界（口型参数范围 0~1）。"""
    loud = struct.pack("<" + "h" * 50, *([-32768] * 50))
    assert 0.0 <= _rms_int16(loud) <= 1.0


# ── 口型：渲染器电平驱动与回退 ──


class _FakeStdParams:
    ParamMouthOpenY = "MouthOpenY"


class _FakeModel:
    def __init__(self):
        self.writes = []

    def SetParameterValue(self, pid, val, weight):
        self.writes.append((pid, val, weight))


def _make_renderer_with_mouth():
    """构造只带口型所需最小状态的渲染器实例（绕过 Qt/GL 初始化）。"""
    r = Live2DRenderer.__new__(Live2DRenderer)
    r._model = _FakeModel()
    r._speaking = True
    r._mouth_phase = 0.0
    r._mouth_env = 0.0
    r._mouth_level_fn = lambda: None
    r._live2d = type("L", (), {"StandardParams": _FakeStdParams})()
    r._note_frame_failure = lambda *a, **k: None
    r._param_intent = {}
    r._param_intent_set_at = 0.0
    return r


def test_mouth_falls_back_to_sine_without_level_source():
    """无电平源时走正弦：连续两次调用应产生不同的嘴型值。"""
    r = _make_renderer_with_mouth()
    r._update_mouth()
    first = r._model.writes[-1][1]
    r._update_mouth()
    second = r._model.writes[-1][1]
    assert first != second
    assert 0.0 <= first <= 1.0 and 0.0 <= second <= 1.0


def test_mouth_follows_level_when_source_available():
    """有电平源时跟音量：大音量应比小音量张嘴更大。"""
    r = _make_renderer_with_mouth()
    r._mouth_level_fn = lambda: 0.4
    for _ in range(20):
        r._update_mouth()
    loud = r._model.writes[-1][1]

    r2 = _make_renderer_with_mouth()
    r2._mouth_level_fn = lambda: 0.02
    for _ in range(20):
        r2._update_mouth()
    quiet = r2._model.writes[-1][1]

    assert loud > quiet


def test_mouth_level_source_exception_falls_back():
    """电平源抛异常时不能崩帧循环，应回退正弦。"""
    r = _make_renderer_with_mouth()

    def boom():
        raise RuntimeError("device gone")

    r._mouth_level_fn = boom
    r._update_mouth()  # 不应抛异常
    assert len(r._model.writes) == 1


def test_mouth_level_source_none_falls_back():
    r = _make_renderer_with_mouth()
    r._mouth_level_fn = lambda: None
    r._update_mouth()
    assert len(r._model.writes) == 1


def test_mouth_env_resets_when_not_speaking():
    """停止说话后包络归零，下次开口不会继承上次幅度。"""
    r = _make_renderer_with_mouth()
    r._mouth_level_fn = lambda: 0.5
    for _ in range(10):
        r._update_mouth()
    assert r._mouth_env > 0.1
    r._speaking = False
    r._update_mouth()
    assert r._mouth_env == 0.0
    assert r._model.writes[-1][1] == 0.0


def test_mouth_set_level_source_rejects_non_callable():
    r = _make_renderer_with_mouth()
    r.set_mouth_level_source("not callable")
    assert r._read_mouth_level() is None


# ── W4：白名单按模型校验 ──


def _miku_ids():
    j = json.loads(MIKU_CDI3.read_text(encoding="utf-8"))
    return {p["Id"] for p in j["Parameters"]}


class _ProbeParam:
    def __init__(self, pid):
        self.id = pid


class _ProbeModel:
    def __init__(self, ids):
        self._ids = sorted(ids)

    def GetParameterCount(self):
        return len(self._ids)

    def GetParameter(self, i):
        return _ProbeParam(self._ids[i])


def test_effective_whitelist_filters_missing_params():
    """按模型校验后，白名单里模型没有的参数必须被滤掉。"""
    r = Live2DRenderer.__new__(Live2DRenderer)
    r._model = _ProbeModel(_miku_ids())
    r._effective_wl_cache = None
    eff = r._effective_whitelist()
    assert "ParamEyeOpen" not in eff  # 白名单有、miku 无
    assert "ParamEyeLOpen" in eff     # 白名单有、miku 有
    assert eff <= Live2DRenderer._PARAM_WHITE_LIST


def test_effective_whitelist_keeps_w4_new_params():
    """W4 新增的表情参数在 miku 上必须全部可用。"""
    r = Live2DRenderer.__new__(Live2DRenderer)
    r._model = _ProbeModel(_miku_ids())
    r._effective_wl_cache = None
    eff = r._effective_whitelist()
    for pid in ("Paramwaizui", "Paramguzui", "mouthRollLower",
                "mouthRollLower2", "EyeL_Squint", "EyeR_Squint",
                "ParamBrowLX", "ParamHairFront"):
        assert pid in eff, pid


def test_effective_whitelist_falls_back_on_probe_failure():
    """探测失败时回退原白名单（不能因探测问题禁掉全部功能）。"""
    r = Live2DRenderer.__new__(Live2DRenderer)
    r._model = object()  # 没有 GetParameterCount
    r._effective_wl_cache = None
    assert r._effective_whitelist() == Live2DRenderer._PARAM_WHITE_LIST


def test_effective_whitelist_is_cached():
    r = Live2DRenderer.__new__(Live2DRenderer)
    r._model = _ProbeModel(_miku_ids())
    r._effective_wl_cache = None
    assert r._effective_whitelist() is r._effective_whitelist()


def test_param_name_map_targets_exist_in_miku():
    """_PARAM_NAME_MAP 里映射的目标参数，在 miku 上必须真实存在。"""
    ids = _miku_ids()
    missing = {k: v for k, v in Live2DRenderer._PARAM_NAME_MAP.items() if v not in ids}
    # 允许部分映射指向其他模型专有参数（如 arm/cheek），但必须是有意为之：
    # 这里只要求“W4 新增的那些”全部有效。
    w4_new = {
        "mouth_waizui", "mouth_guzui", "mouth_guzui2", "cheek_gulian",
        "mouth_tushe", "mouth_bite_lip", "mouth_pucker", "mouth_shrug",
        "eye_squint_l", "eye_squint_r", "eye_deform",
        "brow_lx", "brow_rx", "brow_ly", "brow_ry",
        "hair_front", "hair_side", "hair_back",
    }
    bad = {k for k in w4_new if k in missing}
    assert not bad, f"W4 映射指向 miku 不存在的参数: {bad}"


# ── W5：体检别名匹配 ──


def test_health_standard_params_use_alias_tuples():
    """STANDARD_PARAMS 每项必须是 (别名元组, required, note) 三元组。"""
    for channel, spec in STANDARD_PARAMS.items():
        assert len(spec) == 3, channel
        aliases, required, note = spec
        assert isinstance(aliases, tuple) and aliases, channel
        assert all(isinstance(a, str) for a in aliases), channel
        assert isinstance(required, bool), channel
        assert isinstance(note, str), channel


def test_health_accepts_cubism_standard_names():
    """Cubism 标准命名（ParamEyeLOpen）必须被认作存在，不再误报。"""
    available = _miku_ids()
    results = check_parameter_coverage(available, True)
    by_ch = {r["channel"]: r for r in results}
    for ch in ("eye_open", "eye_smile", "brow_angle", "brow_form"):
        assert by_ch[ch]["status"] == "✅ 完整", f"{ch}: {by_ch[ch]['status']}"
        assert by_ch[ch]["found"], ch


def test_health_no_false_negative_on_miku():
    """miku 真实参数表下，不应有“真缺失”（❌）的必需通道。"""
    results = check_parameter_coverage(_miku_ids(), True)
    missing = [r["channel"] for r in results if r["status"] == "❌ 缺失"]
    assert not missing, f"误报缺失: {missing}"


def test_health_reports_acceptable_missing_separately():
    """非必需且无替代的通道算“可接受缺失”，不混入“真缺失”。"""
    results = check_parameter_coverage(_miku_ids(), True)
    by_ch = {r["channel"]: r for r in results}
    # ParamEyeBallZ 是非必需，miku 没有 → 应为可接受缺失
    assert by_ch["ParamEyeBallZ"]["status"] == "⚠️ 可接受缺失"


def test_health_generate_report_counts():
    report = generate_report(_ProbeModel(_miku_ids()))
    cov = report["coverage"]
    assert cov["missing"] == 0
    assert cov["ok"] >= 15
    assert "acceptable_missing" in cov


def test_health_still_detects_truly_missing():
    """真正缺失的参数仍要被报出来（不能因为放宽匹配就漏报）。"""
    results = check_parameter_coverage({"ParamMouthForm"}, True)
    by_ch = {r["channel"]: r for r in results}
    assert by_ch["eye_open"]["status"] == "❌ 缺失"
    assert by_ch["mouth_form"]["status"] == "✅ 完整"


# ── W3：动作意图参数超时释放 ──


def test_intent_params_record_set_time():
    """写入意图时记录时刻（否则无法判过期）。"""
    r = _make_renderer_with_mouth()
    r._param_intent = {}
    r._param_intent_set_at = 0.0
    r._set_intent_params({"ParamMouthForm": 0.5}, 1.0)
    assert r._param_intent == {"ParamMouthForm": 0.5}
    assert r._param_intent_set_at > 0.0


def test_intent_params_expire_after_ttl():
    """超过 TTL 后必须释放（旧实现下永不过期，表情会卡住）。"""
    r = _make_renderer_with_mouth()
    r._param_intent = {"ParamMouthForm": 0.5}
    r._param_intent_ttl = 0.01
    r._param_intent_set_at = time.monotonic() - 1.0  # 1 秒前设的
    r._expire_intent_params()
    assert r._param_intent == {}


def test_intent_params_kept_within_ttl():
    """TTL 内不能释放（否则表情还没显完就收了）。"""
    r = _make_renderer_with_mouth()
    r._param_intent = {"ParamMouthForm": 0.5}
    r._param_intent_ttl = 60.0
    r._param_intent_set_at = time.monotonic()
    r._expire_intent_params()
    assert r._param_intent == {"ParamMouthForm": 0.5}


def test_intent_ttl_zero_disables_expiry():
    """TTL=0 表示关闭超时（保留旧行为的对照开关）。"""
    r = _make_renderer_with_mouth()
    r._param_intent = {"ParamMouthForm": 0.5}
    r._param_intent_ttl = 0
    r._param_intent_set_at = time.monotonic() - 9999.0
    r._expire_intent_params()
    assert r._param_intent == {"ParamMouthForm": 0.5}


def test_intent_expiry_noop_when_empty():
    """无意图时不做事（避免无意义日志）。"""
    r = _make_renderer_with_mouth()
    r._param_intent = {}
    r._expire_intent_params()
    assert r._param_intent == {}


def test_intent_new_write_resets_timer():
    """新意图到来重置计时，不会被上一次的过期时间误杀。"""
    r = _make_renderer_with_mouth()
    r._param_intent = {"ParamMouthForm": 0.5}
    r._param_intent_ttl = 0.05
    r._param_intent_set_at = time.monotonic() - 10.0
    r._set_intent_params({"ParamMouthForm": 0.8}, 1.0)
    r._expire_intent_params()
    assert r._param_intent == {"ParamMouthForm": 0.8}
