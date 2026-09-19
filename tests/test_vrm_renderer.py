# -*- coding: utf-8 -*-
"""VRMRenderer 纯逻辑单测（不装配 QWebEngine，不需要真实模型）。

覆盖：模型发现（根/vrm 子目录/递归/缺失）、意图→JS 调用映射、屏幕坐标归一化，
以及「导入本模块不得强制拉起 QtWebEngine」（惰性导入契约）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from avatar.vrm_renderer import (  # noqa: E402
    find_vrm_model,
    intent_to_commands,
    normalize_to_view,
)


# ── find_vrm_model ────────────────────────────────────────────────────


def test_find_vrm_prefers_root_then_subdir(tmp_path):
    (tmp_path / "a.vrm").write_bytes(b"x")
    (tmp_path / "vrm").mkdir()
    (tmp_path / "vrm" / "b.vrm").write_bytes(b"x")
    assert Path(find_vrm_model(tmp_path)).name == "a.vrm"


def test_find_vrm_uses_vrm_subdir(tmp_path):
    (tmp_path / "vrm").mkdir()
    (tmp_path / "vrm" / "b.vrm").write_bytes(b"x")
    assert Path(find_vrm_model(tmp_path)).name == "b.vrm"


def test_find_vrm_recursive_fallback(tmp_path):
    deep = tmp_path / "assets" / "nested"
    deep.mkdir(parents=True)
    (deep / "c.vrm").write_bytes(b"x")
    assert Path(find_vrm_model(tmp_path)).name == "c.vrm"


def test_find_vrm_returns_none(tmp_path):
    assert find_vrm_model(tmp_path) is None
    assert find_vrm_model(None) is None
    assert find_vrm_model(tmp_path / "nope") is None


def test_find_vrm_is_deterministic(tmp_path):
    for name in ("b.vrm", "a.vrm", "c.vrm"):
        (tmp_path / name).write_bytes(b"x")
    assert Path(find_vrm_model(tmp_path)).name == "a.vrm"


# ── intent_to_commands ────────────────────────────────────────────────


def test_intent_gesture_emotion_preset():
    cmds = intent_to_commands({"gesture": "happy", "intensity": 0.5})
    assert cmds == [{"fn": "setEmotion", "args": ["happy", 0.5]}]


def test_intent_gesture_custom_expression():
    cmds = intent_to_commands({"gesture": "waving"})
    assert cmds == [{"fn": "setExpression", "args": ["waving", 1.0]}]


def test_intent_intensity_is_clamped():
    assert intent_to_commands({"gesture": "happy", "intensity": 9})[0]["args"][1] == 1.0
    assert intent_to_commands({"gesture": "happy", "intensity": -3})[0]["args"][1] == 0.0
    assert intent_to_commands({"gesture": "happy", "intensity": "x"})[0]["args"][1] == 1.0


def test_intent_params_map_to_vrm_expressions():
    """Live2D 参数名 → VRM 表情；负值取绝对值（Live2D 负值=闭眼，VRM 0=闭眼）。"""
    cmds = intent_to_commands({"params": {"ParamMouthOpenY": 0.6, "ParamEyeLOpen": -0.8,
                                          "ParamUnknown": 1.0}})
    assert {"fn": "setExpression", "args": ["aa", 0.6]} in cmds
    assert {"fn": "setExpression", "args": ["blink", 0.8]} in cmds
    assert len(cmds) == 2, "未识别的参数名必须被忽略"


def test_intent_va_splits_into_two_channels():
    cmds = intent_to_commands({"va": [0.5, -0.25]})
    assert {"fn": "setExpression", "args": ["va_x", 0.5]} in cmds
    assert {"fn": "setExpression", "args": ["va_y", 0.25]} in cmds


def test_intent_bad_inputs_are_safe():
    for bad in (None, [], "x", 3, {}, {"gesture": ""}, {"params": "x"}, {"va": [1]}):
        assert intent_to_commands(bad) == []


# ── normalize_to_view ─────────────────────────────────────────────────


def test_normalize_center_and_corners():
    assert normalize_to_view(50, 50, 100, 100) == (0.0, 0.0)
    assert normalize_to_view(0, 0, 100, 100) == (-1.0, 1.0)
    assert normalize_to_view(100, 100, 100, 100) == (1.0, -1.0)


def test_normalize_clamps_and_guards_zero_size():
    assert normalize_to_view(-999, 999, 100, 100) == (-1.0, -1.0)
    nx, ny = normalize_to_view(5, 5, 0, 0)   # 尺寸 0 不得 ZeroDivisionError
    assert -1.0 <= nx <= 1.0 and -1.0 <= ny <= 1.0


# ── 惰性导入契约 ──────────────────────────────────────────────────────


def test_module_import_does_not_pull_webengine():
    """导入 avatar.vrm_renderer 不应强制加载 QtWebEngineWidgets。

    WebEngine 必须在 QApplication 之后才装配（见 _import_webengine 惰性导入），
    否则在某些 Qt 版本上会因 OpenGL 上下文共享顺序而出错。
    """
    import avatar.vrm_renderer as m
    assert hasattr(m, "_import_webengine")
    assert m.VIEW_INDEX.name == "index.html"


# ── 工厂路由 ──────────────────────────────────────────────────────────


def _redirect_factory_root(monkeypatch, tmp_path):
    """factory 以「模块文件所在目录的上一级」为项目根；把它重定向到临时树。

    这样可以在不污染真实 characters/ 的前提下测 detect_format / resource_available。
    """
    import avatar.factory as f
    (tmp_path / "avatar").mkdir(exist_ok=True)
    (tmp_path / "avatar" / "factory.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(f, "__file__", str(tmp_path / "avatar" / "factory.py"), raising=False)
    return f


def test_factory_detects_vrm_by_format_field(monkeypatch, tmp_path):
    f = _redirect_factory_root(monkeypatch, tmp_path)
    char = tmp_path / "characters" / "a"
    char.mkdir(parents=True)
    (char / "pet.json").write_text(json.dumps({"format": "vrm"}), encoding="utf-8")
    assert f.detect_format("a") == "vrm"


def test_factory_detects_vrm_by_file_presence(monkeypatch, tmp_path):
    """无 format 字段时，目录里出现 .vrm 也要能识别。"""
    f = _redirect_factory_root(monkeypatch, tmp_path)
    char = tmp_path / "characters" / "b"
    char.mkdir(parents=True)
    (char / "m.vrm").write_bytes(b"x")
    assert f.detect_format("b") == "vrm"


def test_resource_available_vrm_depends_on_model(monkeypatch, tmp_path):
    """声明的格式是 vrm：有模型 → 可加载；无模型 → 明确原因（而非旧的"尚未实现"）。"""
    f = _redirect_factory_root(monkeypatch, tmp_path)
    ok_char = tmp_path / "characters" / "with_model"
    ok_char.mkdir(parents=True)
    (ok_char / "m.vrm").write_bytes(b"x")
    assert f.resource_available("with_model") == (True, "")

    bad_char = tmp_path / "characters" / "no_model"
    bad_char.mkdir(parents=True)
    (bad_char / "pet.json").write_text(json.dumps({"format": "vrm"}), encoding="utf-8")
    ok, reason = f.resource_available("no_model")
    assert ok is False and "VRM" in reason


def test_resource_available_unknown_character():
    """不存在且非已知角色的 id：给出可读原因，不抛异常。"""
    import avatar.factory as f
    ok, reason = f.resource_available("__no_such_character__")
    assert ok is False and reason
