# -*- coding: utf-8 -*-
"""程序化生成动作文件（motion3.json）的验收。

最要紧的三条：
  1. **解析器能完整走通模型自带的每一个动作文件**——这是"段编码读对了"的硬证据；
  2. **参数名必须是这个模型真有的**——写错不会报错，只会静静地什么都不做；
  3. **第一版那种错编码必须被挡住**——它让原生 SDK 越界读，把桌宠打崩过。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.make_motion import (
    build_motion,
    nod_motion,
    parse_segments,
    validate_motion,
    write_motion,
)

ROOT = Path(__file__).resolve().parents[1]
MIKU_MOTIONS = ROOT / "characters" / "miku" / "live2d" / "motions"


def _real_motion_files() -> list:
    return sorted(p for p in MIKU_MOTIONS.glob("*.motion3.json") if p.exists())


def _existing_motion() -> dict:
    for name in ("happy.motion3.json", "waving.motion3.json", "sad.motion3.json"):
        p = MIKU_MOTIONS / name
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    pytest.skip("找不到可对照的模型动作文件")


# ── 硬证据：解析器走通真文件 ──────────────────────────────


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
    cands = sorted(MIKU_MOTIONS.glob("*.motion3.json")) + sorted(MIKU_MOTIONS.glob("*.json.bak"))
    seen = 0
    for p in cands:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        has_bezier = any(_has_id(c.get("Segments", []), 1) for c in data.get("Curves", []))
        if not has_bezier:
            continue
        info = validate_motion(data)          # 能被完整走通才算数
        assert info["points"] > 0
        seen += 1
    if not seen:
        pytest.skip("这批模型文件里没有带贝塞尔的样本")


def _has_id(segments, target: int) -> bool:
    """按段编码走一遍，判断某个标识符是否出现过。"""
    i = 2
    while i < len(segments):
        if segments[i] == target:
            return True
        i += 1 + 2 * (3 if segments[i] == 1 else 1)
    return False


# ── 我写的第一版编码：必须被挡住 ──────────────────────────


def test_rejects_the_encoding_that_crashed_the_pet():
    """第一版写成 ``[标识符, 初始值, 时间, 值…]``（标识符放开头）。

    SDK 会把后面的数字当段标识符解析，读到 0.125 这种非法 id → 越界读 →
    0xC0000005。守门人必须在装包前就把它拦下。
    """
    wrong = [0, 0.0, 0.125, -12.0, 0.25, 0.0]
    with pytest.raises(ValueError):
        parse_segments(wrong)


def test_rejects_unknown_segment_id():
    with pytest.raises(ValueError):
        parse_segments([0.0, 0.0, 9, 1.0, 1.0])


def test_rejects_truncated_segment():
    with pytest.raises(ValueError):
        parse_segments([0.0, 0.0, 0, 1.0])       # 线性段少一个点


def test_rejects_backwards_times_in_segments():
    with pytest.raises(ValueError):
        parse_segments([0.0, 0.0, 0, 2.0, 1.0, 0, 1.0, 1.0])


def test_validate_rejects_point_past_duration():
    bad = build_motion({"ParamAngleY": [(0.0, 0.0), (1.0, 5.0)]})
    bad["Meta"]["Duration"] = 0.5                # 手动改坏
    with pytest.raises(ValueError):
        validate_motion(bad)


def test_validate_rejects_wrong_counts():
    bad = build_motion({"ParamAngleY": [(0.0, 0.0), (1.0, 5.0)]})
    bad["Meta"]["CurveCount"] = 7
    with pytest.raises(ValueError):
        validate_motion(bad)


# ── 编码形状（与官方规范一致） ────────────────────────────


def test_linear_encoding_is_point_first_then_id():
    """官方：先第一个点，再段标识符；线性段 = 标识符 + 1 个点。"""
    m = build_motion({"ParamAngleY": [(0.0, 3.0), (0.5, -10.0), (1.0, 0.0)]})
    assert m["Curves"][0]["Segments"] == [0.0, 3.0, 0, 0.5, -10.0, 0, 1.0, 0.0]


def test_top_level_shape_matches_real_file():
    real = _existing_motion()
    mine = build_motion({"ParamAngleY": [(0.0, 0.0), (0.5, -10.0)]})
    assert set(mine) == set(real), "顶层键要和模型自带动作一致"
    assert set(mine["Meta"]) == set(real["Meta"]), "Meta 键要和模型自带动作一致"
    assert list(mine["Meta"]) == list(real["Meta"]), "连键的顺序也照抄"
    assert mine["Version"] == real["Version"]


def test_curve_shape_matches_real_file():
    real = _existing_motion()
    mine = build_motion({"ParamAngleY": [(0.0, 0.0), (0.5, -10.0)]})
    assert set(mine["Curves"][0]) == set(real["Curves"][0])


def test_meta_counts_are_consistent():
    m = build_motion({
        "ParamAngleY": [(0.0, 0.0), (0.5, -10.0), (1.0, 0.0)],
        "ParamAngleX": [(0.0, 0.0), (1.0, 5.0)],
    })
    meta = m["Meta"]
    assert meta["CurveCount"] == 2
    assert meta["TotalPointCount"] == 3 + 2
    assert meta["TotalSegmentCount"] == 2 + 1
    assert meta["Duration"] == 1.0, "不给 duration 就取最后关键帧时间"


def test_duration_equals_last_keyframe_like_real_files():
    """真文件里曲线的最后时间点**正好等于** Meta.Duration——照着学。"""
    m = nod_motion()
    assert m["Curves"][0]["Segments"][-2] == m["Meta"]["Duration"]


def test_fade_matches_real_files():
    real = _existing_motion()["Meta"]
    mine = build_motion({"ParamAngleY": [(0.0, 0.0), (0.5, -10.0)]})["Meta"]
    assert mine["FadeInTime"] == real["FadeInTime"]
    assert mine["FadeOutTime"] == real["FadeOutTime"]


# ── 参数名（最容易静默失败的地方） ────────────────────────


def test_generated_params_exist_in_this_model():
    """**关键防呆**：参数名必须是模型真有的，否则它静静地什么都不做。"""
    real_ids = set()
    for p in MIKU_MOTIONS.glob("*.motion3.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for c in data.get("Curves", []):
            if c.get("Id"):
                real_ids.add(c["Id"])
    if not real_ids:
        pytest.skip("模型动作文件里没读到参数名")
    for c in nod_motion()["Curves"]:
        assert c["Id"] in real_ids, f"{c['Id']} 不在这个模型的参数里"


# ── 点头动作本身 ──────────────────────────────────────────


def test_nod_round_trips_through_parser():
    pts = parse_segments(nod_motion()["Curves"][0]["Segments"])
    assert pts[0] == (0.0, 0.0)
    assert pts[-1][1] == 0.0, "回到静止（否则脸会歪着停在最后）"


def test_nod_actually_moves():
    pts = parse_segments(nod_motion(amplitude=12.0)["Curves"][0]["Segments"])
    values = [v for _, v in pts]
    assert max(values) > 4 and min(values) < -4, "幅度要看得出来"


def test_nod_does_not_loop():
    """点头不该循环——循环的话它会一直点头。"""
    assert nod_motion()["Meta"]["Loop"] is False


def test_nod_times_ascend():
    pts = parse_segments(nod_motion()["Curves"][0]["Segments"])
    times = [t for t, _ in pts]
    assert times == sorted(times)


# ── 输入校验 ──────────────────────────────────────────────


def test_rejects_empty_curves():
    with pytest.raises(ValueError):
        build_motion({})


def test_rejects_missing_t0():
    with pytest.raises(ValueError):
        build_motion({"ParamAngleY": [(0.5, 1.0)]})


def test_rejects_backwards_times():
    with pytest.raises(ValueError):
        build_motion({"ParamAngleY": [(0.0, 0.0), (1.0, 1.0), (0.5, 0.0)]})


def test_rejects_duration_shorter_than_keyframes():
    with pytest.raises(ValueError):
        build_motion({"ParamAngleY": [(0.0, 0.0), (2.0, 1.0)]}, duration=1.0)


# ── 落盘 ──────────────────────────────────────────────────


def test_write_is_valid_json_without_bom(tmp_path):
    p = write_motion(nod_motion(), tmp_path / "nod.motion3.json")
    raw = p.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "BOM 会让解析端炸"
    assert json.loads(raw.decode("utf-8"))["Version"] == 3


def test_write_refuses_invalid_motion(tmp_path):
    """坏文件宁可不落盘——它会让桌宠原生崩溃。"""
    bad = nod_motion()
    bad["Curves"][0]["Segments"] = [0, 0.0, 0.125, -12.0]
    with pytest.raises(ValueError):
        write_motion(bad, tmp_path / "bad.motion3.json")
    assert not (tmp_path / "bad.motion3.json").exists()
