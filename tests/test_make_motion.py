# -*- coding: utf-8 -*-
"""程序化生成动作文件（motion3.json）的验收。

一圈实测踩出来的三条铁律，都变成了测试：

  1. **段编码写错不是"动作不播"，是"桌宠原生崩溃"**（0xC0000005）。
     解析器必须能完整走通模型自带的每一个动作文件——这是编码读对了的硬证据。
  2. **参数名必须是这个模型真有的**——写错不会报错，只会静静地什么都不做。
  3. **写在渲染器每帧覆盖的参数上，动作会播但看不见**（`ParamAngleY` 的
     "点头"实测完全看不见；原版"挥手"靠的是身体倾斜 + 头发 + 眨眼）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.make_motion import (
    RENDERER_OVERWRITTEN_PARAMS,
    VISIBLE_PARAMS,
    build_motion,
    model_params,
    nod_motion,
    parse_segments,
    tilt_motion,
    validate_motion,
    write_motion,
)

ROOT = Path(__file__).resolve().parents[1]
MIKU_MOTIONS = ROOT / "characters" / "miku" / "live2d" / "motions"


def _real_motion_files() -> list:
    return sorted(p for p in MIKU_MOTIONS.glob("*.motion3.json") if p.exists())


def _all_motion_files() -> list:
    return sorted(MIKU_MOTIONS.glob("*.motion3.json")) + sorted(MIKU_MOTIONS.glob("*.json.bak"))


def _existing_motion() -> dict:
    for name in ("happy.motion3.json", "waving.motion3.json", "sad.motion3.json"):
        p = MIKU_MOTIONS / name
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    pytest.skip("找不到可对照的模型动作文件")


def _has_id(segments, target: int) -> bool:
    """按段编码走一遍，判断某个标识符是否出现过。"""
    i = 2
    while i < len(segments):
        if segments[i] == target:
            return True
        i += 1 + 2 * (3 if segments[i] == 1 else 1)
    return False


# ── 铁律 1：段编码 —— 解析器走通真文件 ────────────────────


def test_parser_consumes_every_real_model_motion():
    """**这条是主轴**：解析器必须能完整走通模型自带的每个动作。

    走不通说明我对段编码的理解是错的——而写错的代价是桌宠原生崩溃。
    """
    files = _real_motion_files()
    if not files:
        pytest.skip("找不到模型动作文件")
    checked = 0
    for p in files:
        data = json.loads(p.read_text(encoding="utf-8"))
        info = validate_motion(data)          # 结构 + 计数 + 时间递增
        assert info["points"] > 0
        checked += 1
    assert checked == len(files), f"只校验了 {checked}/{len(files)} 个文件"


def test_parser_handles_bezier_segments():
    """真文件里存在贝塞尔段（标识符 1 后面跟 3 个点）——必须支持。

    注意：**当前 7 个动作文件全是纯线性**（ids 只有 0）；
    贝塞尔出现在 ``idle.motion3.json.bak`` 里（ids [0,1]，72 个点），
    它被解析器完整走通了——这就是贝塞尔支持的硬证据。
    外面作者做的动作包大量用贝塞尔，所以这条路不能断。
    """
    seen = 0
    for p in _all_motion_files():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not any(_has_id(c.get("Segments", []), 1) for c in data.get("Curves", [])):
            continue
        info = validate_motion(data)          # 能被完整走通才算数
        assert info["points"] > 0
        seen += 1
    if not seen:
        pytest.skip("这批模型文件里没有带贝塞尔的样本")


# ── 铁律 1 的另一面：会崩的编码必须被挡住 ──────────────────


def test_rejects_the_encoding_that_crashed_the_pet():
    """第一版写成 ``[标识符, 初始值, 时间, 值…]``（标识符放开头）。

    SDK 会把后面的数字当段标识符解析，读到 0.125 这种非法 id → 越界读 →
    0xC0000005，桌宠被看门狗反复拉起又崩。守门人必须拦下它。
    """
    with pytest.raises(ValueError):
        parse_segments([0, 0.0, 0.125, -12.0, 0.25, 0.0])


def test_rejects_unknown_segment_id():
    with pytest.raises(ValueError):
        parse_segments([0.0, 0.0, 9, 1.0, 1.0])


def test_rejects_truncated_segment():
    with pytest.raises(ValueError):
        parse_segments([0.0, 0.0, 0, 1.0])


def test_rejects_backwards_times_in_segments():
    with pytest.raises(ValueError):
        parse_segments([0.0, 0.0, 0, 2.0, 1.0, 0, 1.0, 1.0])


def test_validate_rejects_point_past_duration():
    bad = build_motion({"ParamAngleZ": [(0.0, 0.0), (1.0, 5.0)]})
    bad["Meta"]["Duration"] = 0.5
    with pytest.raises(ValueError):
        validate_motion(bad)


def test_validate_rejects_wrong_counts():
    bad = build_motion({"ParamAngleZ": [(0.0, 0.0), (1.0, 5.0)]})
    bad["Meta"]["CurveCount"] = 7
    with pytest.raises(ValueError):
        validate_motion(bad)


# ── 铁律 3：被覆盖的参数拒绝生成 ──────────────────────────


def test_overwritten_params_are_refused_by_default():
    """**这条是拿一次崩溃换来的**：写在视线/口型参数上的动作看不见。"""
    with pytest.raises(ValueError):
        build_motion({"ParamAngleY": [(0.0, 0.0), (0.5, 10.0)]})


def test_nod_motion_is_refused_by_default():
    """`nod_motion` 保留作反例：它播得出来，但看不见。"""
    with pytest.raises(ValueError):
        nod_motion()


def test_overwrite_can_be_forced():
    m = build_motion({"ParamAngleY": [(0.0, 0.0), (0.5, 10.0)]}, allow_overwritten=True)
    assert validate_motion(m)["curves"] == 1


def test_blacklist_covers_what_renderer_writes_every_frame():
    """黑名单里的每一条都能在渲染器源码里找到逐帧写入的依据。"""
    src = (ROOT / "avatar" / "live2d_renderer.py").read_text(encoding="utf-8")
    assert "SetParameterValue(P.ParamAngleY" in src
    assert "SetParameterValue(P.ParamAngleX" in src
    assert "SetParameterValue(P.ParamMouthOpenY" in src
    assert "ParamAngleY" in RENDERER_OVERWRITTEN_PARAMS


def test_visible_params_are_not_in_blacklist():
    assert not (VISIBLE_PARAMS & RENDERER_OVERWRITTEN_PARAMS)


def test_visible_params_come_from_real_files():
    """白名单不是拍出来的：真文件里**在动的**参数（去掉被覆盖的）应当都在里面。"""
    moving = set()
    for p in _real_motion_files():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for c in data.get("Curves", []):
            pts = parse_segments(c["Segments"])
            vals = [v for _, v in pts]
            if max(vals) - min(vals) > 0.2:          # 幅度小到看不出的不算
                moving.add(c["Id"])
    moving -= RENDERER_OVERWRITTEN_PARAMS
    if not moving:
        pytest.skip("没统计到在动的参数")
    missing = moving - VISIBLE_PARAMS
    assert not missing, f"这些参数在真文件里看得见，却没进白名单：{sorted(missing)}"


# ── 编码形状（与官方规范一致） ────────────────────────────


def test_linear_encoding_is_point_first_then_id():
    """官方：先第一个点，再段标识符；线性段 = 标识符 + 1 个点。"""
    m = build_motion({"ParamAngleZ": [(0.0, 3.0), (0.5, -10.0), (1.0, 0.0)]})
    assert m["Curves"][0]["Segments"] == [0.0, 3.0, 0, 0.5, -10.0, 0, 1.0, 0.0]


def test_top_level_shape_matches_real_file():
    real = _existing_motion()
    mine = build_motion({"ParamAngleZ": [(0.0, 0.0), (0.5, -10.0)]})
    assert set(mine) == set(real), "顶层键要和模型自带动作一致"
    assert list(mine["Meta"]) == list(real["Meta"]), "连键的顺序也照抄"
    assert mine["Version"] == real["Version"]


def test_curve_shape_matches_real_file():
    real = _existing_motion()
    mine = build_motion({"ParamAngleZ": [(0.0, 0.0), (0.5, -10.0)]})
    assert set(mine["Curves"][0]) == set(real["Curves"][0])


def test_meta_counts_are_consistent():
    m = build_motion({
        "ParamAngleZ": [(0.0, 0.0), (0.5, -10.0), (1.0, 0.0)],
        "ParamBodyAngleZ": [(0.0, 0.0), (1.0, 5.0)],
    })
    meta = m["Meta"]
    assert meta["CurveCount"] == 2
    assert meta["TotalPointCount"] == 3 + 2
    assert meta["TotalSegmentCount"] == 2 + 1
    assert meta["Duration"] == 1.0, "不给 duration 就取最后关键帧时间"


def test_duration_equals_last_keyframe_like_real_files():
    """真文件里曲线的最后时间点**正好等于** Meta.Duration——照着学。"""
    m = tilt_motion()
    for c in m["Curves"]:
        assert c["Segments"][-2] == m["Meta"]["Duration"]


def test_fade_matches_real_files():
    real = _existing_motion()["Meta"]
    mine = build_motion({"ParamAngleZ": [(0.0, 0.0), (0.5, -10.0)]})["Meta"]
    assert mine["FadeInTime"] == real["FadeInTime"]
    assert mine["FadeOutTime"] == real["FadeOutTime"]


# ── 铁律 2：参数名必须是模型真有的 ────────────────────────


def test_generated_params_exist_in_this_model():
    """**关键防呆**：参数名必须是模型真有的，否则它静静地什么都不做。

    依据是模型自己声明的参数表（`cdi3.json`），**不是“动作文件里出现过”**：
    老动作里可能残留模型根本没有的参数。
    """
    params = model_params(MIKU_MOTIONS.parent / "miku.model3.json")
    if not params:
        pytest.skip("读不到模型的参数表（cdi3）")
    assert len(params) > 50, f"参数表看起来不对，只有 {len(params)} 个"
    for c in tilt_motion()["Curves"]:
        assert c["Id"] in params, f"{c['Id']} 不在这个模型的参数里"


def test_blacklist_and_whitelist_params_also_exist_in_model():
    """两个名单里的参数名也得是模型真有的——名字打错就失去意义了。"""
    params = model_params(MIKU_MOTIONS.parent / "miku.model3.json")
    if not params:
        pytest.skip("读不到模型的参数表（cdi3）")
    bad = (RENDERER_OVERWRITTEN_PARAMS | VISIBLE_PARAMS) - params
    assert not bad, f"名单里的参数模型里没有：{sorted(bad)}"


# ── 看得见的动作本身 ──────────────────────────────────────


def test_tilt_uses_only_unoverwritten_params():
    assert not (set(c["Id"] for c in tilt_motion()["Curves"]) & RENDERER_OVERWRITTEN_PARAMS)


def test_tilt_round_trips_and_returns_to_rest():
    for c in tilt_motion()["Curves"]:
        pts = parse_segments(c["Segments"])
        assert pts[0] == (0.0, 0.0)
        assert pts[-1][1] == 0.0, "回到静止（否则会歪着停在最后）"


def test_tilt_amplitude_is_in_the_range_real_motions_use():
    """幅度参考真文件：happy 的 ParamAngleZ ±3，waving 的 ParamBodyAngleZ ±5。"""
    by_id = {c["Id"]: parse_segments(c["Segments"]) for c in tilt_motion()["Curves"]}
    head = max(v for _, v in by_id["ParamAngleZ"])
    body = max(v for _, v in by_id["ParamBodyAngleZ"])
    assert 2 <= head <= 15, f"歪头幅度 {head} 不在可看范围"
    assert 1 <= body <= 12, f"身体幅度 {body} 不在可看范围"


def test_tilt_does_not_loop():
    assert tilt_motion()["Meta"]["Loop"] is False


# ── 输入校验 ──────────────────────────────────────────────


def test_rejects_empty_curves():
    with pytest.raises(ValueError):
        build_motion({})


def test_rejects_missing_t0():
    with pytest.raises(ValueError):
        build_motion({"ParamAngleZ": [(0.5, 1.0)]})


def test_rejects_backwards_times():
    with pytest.raises(ValueError):
        build_motion({"ParamAngleZ": [(0.0, 0.0), (1.0, 1.0), (0.5, 0.0)]})


def test_rejects_duration_shorter_than_keyframes():
    with pytest.raises(ValueError):
        build_motion({"ParamAngleZ": [(0.0, 0.0), (2.0, 1.0)]}, duration=1.0)


# ── 落盘 ──────────────────────────────────────────────────


def test_write_is_valid_json_without_bom(tmp_path):
    p = write_motion(tilt_motion(), tmp_path / "tilt.motion3.json")
    raw = p.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "BOM 会让解析端炸"
    assert json.loads(raw.decode("utf-8"))["Version"] == 3


def test_write_refuses_invalid_motion(tmp_path):
    """坏文件宁可不落盘——它会让桌宠原生崩溃。"""
    bad = tilt_motion()
    bad["Curves"][0]["Segments"] = [0, 0.0, 0.125, -12.0]
    with pytest.raises(ValueError):
        write_motion(bad, tmp_path / "bad.motion3.json")
    assert not (tmp_path / "bad.motion3.json").exists()
