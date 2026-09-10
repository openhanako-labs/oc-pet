"""P2 表现层 — 流式打字机（P2-1）+ Live2D 表情丰富度（P2-6）离屏单测。

无显示器环境用 offscreen QPA 平台运行：
    python -m pytest tests/test_p2_typewriter_emotion.py -v
（需要 PySide6；本机 oc-pet 环境已装。）

覆盖：
- P2-1：打字机逐字流式、点击跳过、思考点衔接、标点停顿、长文本、速度可配、clear 停止
- P2-6：master_emotion 驱动面部参数映射、平滑插值（tau 可配）、缺参数不崩、
        set_master_emotion 只同步表情不触发 motion、无模型安全
"""
import os
import sys
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

_app = QApplication.instance() or QApplication([])

from avatar.live2d_renderer import Live2DRenderer
from ui.chat_message import ROLE_ASSISTANT
from ui.chat_panel import ChatPanel, TYPING_PUNCT_CHARS, TYPING_PUNCT_MS


# ── P2-1 流式打字机 ──

def test_typewriter_streams_progressively():
    panel = ChatPanel(theme="light")
    text = "你好呀，今天过得怎么样？" * 3  # 足够长，便于观察中间态
    msg = panel.start_assistant_stream(text, char_interval_ms=30)
    assert panel.is_streaming is True
    assert msg.text == ""  # 初始无文本
    # 手动推进若干 tick（不依赖真实时钟，确定性验证中间态）
    for _ in range(4):
        panel._stream_tick()
    assert 0 < panel._stream_pos < len(text)
    assert msg.text == text[:panel._stream_pos]
    # 推进到结束
    while panel._stream_msg is not None:
        panel._stream_tick()
    assert msg.text == text
    assert panel.is_streaming is False
    panel.deleteLater()


def test_typewriter_skip_shows_full():
    panel = ChatPanel(theme="light")
    text = "这是一条很长的回复，" + "内容" * 40
    msg = panel.start_assistant_stream(text, char_interval_ms=30)
    panel._stream_tick()  # 先推进一点点，确认处于中间态
    assert panel._stream_pos < len(text)
    panel.skip_stream()
    assert msg.text == text
    assert panel.is_streaming is False
    assert panel._stream_pos == 0  # 状态已复位
    panel.deleteLater()


def test_typewriter_click_skips():
    panel = ChatPanel(theme="light")
    text = "点击气泡应该立刻显示全文。" * 2
    msg = panel.start_assistant_stream(text, char_interval_ms=30)
    panel._stream_tick()
    assert panel._stream_pos < len(text)
    # 模拟点击文本 label → 事件过滤器拦截 → typewriter_clicked → skip
    QTest.mouseClick(msg._text_label, Qt.LeftButton)
    assert msg.text == text
    assert panel.is_streaming is False
    panel.deleteLater()


def test_typewriter_thinking_handoff():
    panel = ChatPanel(theme="light")
    panel.set_thinking(True)
    assert panel._thinking_msg is not None  # 思考点显示期间无文本
    msg = panel.start_assistant_stream("想好了，现在开始说话。", char_interval_ms=30)
    # 思考点已消失，文本开始流式输出
    assert panel._thinking_msg is None
    assert panel._stream_msg is msg
    assert panel._thinking_msg is None
    panel.deleteLater()


def test_typewriter_punctuation_pause():
    panel = ChatPanel(theme="light")
    panel.start_assistant_stream("测试", char_interval_ms=30)
    punct_delay = panel._next_stream_delay("。")
    normal_delay = panel._next_stream_delay("字")
    assert punct_delay >= TYPING_PUNCT_MS
    assert punct_delay > normal_delay
    # 所有定义的中文/英文标点都属于停顿集合
    for ch in "。！？；：,.!?;:…—~～\n":
        assert ch in TYPING_PUNCT_CHARS
    panel.deleteLater()


def test_typewriter_long_text_completes():
    panel = ChatPanel(theme="light")
    text = "长文本" * 800  # 2400 字
    msg = panel.start_assistant_stream(text, char_interval_ms=10)
    assert panel._stream_chars_per_tick == 3  # 长文本自动加速
    while panel._stream_msg is not None:
        panel._stream_tick()
    assert msg.text == text
    assert panel.is_streaming is False
    panel.deleteLater()


def test_typewriter_speed_config():
    panel = ChatPanel(theme="light")
    panel.set_typewriter_speed(50)
    assert panel._stream_interval == 50
    panel.set_typewriter_speed(1)
    assert panel._stream_interval == 5  # 下限保护
    panel.deleteLater()


def test_append_assistant_still_instant():
    """P2-1 不破坏旧 API：append_assistant 仍立即显示全文。"""
    panel = ChatPanel(theme="light")
    msg = panel.append_assistant("非流式助手消息")
    assert msg.text == "非流式助手消息"
    assert panel.is_streaming is False
    panel.deleteLater()


def test_clear_stops_stream():
    panel = ChatPanel(theme="light")
    panel.start_assistant_stream("流式中被清空", char_interval_ms=30)
    assert panel.is_streaming is True
    panel.clear()
    assert panel.is_streaming is False
    assert panel.message_count == 0
    panel.deleteLater()


# ── P2-6 表情丰富度 ──

def test_emotion_facial_targets_cover_known_emotions():
    """所有已知情绪（_EMOTION_KEYWORDS 全集）都有面部参数目标，且键完整。"""
    from avatar.live2d_renderer import Live2DRenderer as LR
    known = set(LR._EMOTION_KEYWORDS) | {"neutral"}
    for emo in known:
        assert emo in LR._EMOTION_FACIAL_TARGETS, f"缺少情绪目标: {emo}"
    neutral_keys = set(LR._EMOTION_FACIAL_TARGETS["neutral"].keys())
    for emo, profile in LR._EMOTION_FACIAL_TARGETS.items():
        assert set(profile.keys()) == neutral_keys, f"{emo} 参数键不完整"
    # 约束：miku 无手/臂/腿 —— 程序化目标里不允许出现肢体参数
    for profile in LR._EMOTION_FACIAL_TARGETS.values():
        assert not any("arm" in k or "leg" in k or "body" in k for k in profile)


class _FakeParams:
    ParamEyeLOpen = "EyeLOpen"
    ParamEyeROpen = "EyeROpen"
    ParamEyeLSmile = "EyeLSmile"
    ParamEyeRSmile = "EyeRSmile"
    ParamBrowLAngle = "BrowLAngle"
    ParamBrowRAngle = "BrowRAngle"
    ParamBrowLForm = "BrowLForm"
    ParamBrowRForm = "BrowRForm"
    ParamMouthForm = "MouthForm"
    ParamMouthOpenY = "MouthOpenY"
    ParamEyeBallX = "EyeBallX"
    ParamEyeBallY = "EyeBallY"
    ParamAngleX = "AngleX"
    ParamAngleY = "AngleY"
    ParamBreath = "Breath"
    ParamHairFront = "HairFront"
    ParamHairSide = "HairSide"


class _FakeModel:
    """记录 SetParameterValue 调用的假模型（可注入指定参数抛异常）。"""

    def __init__(self, fail_on: set | None = None):
        self.calls: list[tuple] = []
        self.fail_on: set = fail_on or set()

    def SetParameterValue(self, pid, val, weight=1.0):
        if pid in self.fail_on:
            raise RuntimeError(f"no param {pid}")
        self.calls.append((pid, float(val), float(weight)))


def _make_renderer(model=None):
    r = Live2DRenderer(parent=None)
    r._live2d = SimpleNamespace(StandardParams=_FakeParams)
    r._model = model if model is not None else _FakeModel()
    r._proc_last_t = time.monotonic() - 0.033  # 模拟一帧 33ms
    return r


def test_master_emotion_no_model_safe():
    r = Live2DRenderer(parent=None)  # _model = None
    r.set_master_emotion("happy")
    assert r.master_emotion == "happy"
    r._update_procedural_emotion()  # 无模型不崩
    assert r.master_emotion == "happy"


def test_set_master_emotion_updates_driver():
    r = _make_renderer()
    assert r.master_emotion == "neutral"
    r.set_master_emotion("sad")
    assert r.master_emotion == "sad"
    # 未知情绪回退 neutral（不崩）
    r.set_master_emotion("whatever")
    assert r.master_emotion == "whatever"
    assert r._EMOTION_FACIAL_TARGETS.get("whatever") is None


def test_procedural_emotion_smoothing_and_params():
    r = _make_renderer()
    r.set_master_emotion("happy")
    r._proc_cur = dict(r._EMOTION_FACIAL_TARGETS["neutral"])  # 从 neutral 起步
    r._proc_last_t = time.monotonic() - 0.033
    r._update_procedural_emotion()
    # 平滑：向 happy 目标移动但未瞬达
    assert 0.0 < r._proc_cur["eye_smile"] < 0.75
    # 面部参数被写入（眼睛/眉毛/嘴/眼神/呼吸）
    pids = {c[0] for c in r._model.calls}
    assert {"EyeLSmile", "EyeRSmile", "BrowLAngle", "BrowRAngle",
            "MouthForm", "EyeBallX", "Breath", "HairFront"} <= pids
    # 眼睛开合被 clamp 到 [0,1]（surprised 目标 1.1 → 1.0）
    r.set_master_emotion("surprised")
    r._proc_cur["eye_open"] = 1.0
    r._proc_last_t = time.monotonic() - 0.5
    r._update_procedural_emotion()
    eye_open_vals = [c[1] for c in r._model.calls if c[0] == "EyeLOpen"]
    assert eye_open_vals and all(0.0 <= v <= 1.0 for v in eye_open_vals)


def test_procedural_emotion_missing_param_no_crash():
    """模型缺某参数（SetParameterValue 抛异常）时该组跳过，其余参数继续。"""
    model = _FakeModel(fail_on={"BrowLAngle", "Breath"})
    r = _make_renderer(model=model)
    r.set_master_emotion("angry")
    r._update_procedural_emotion()  # 不崩
    pids = {c[0] for c in model.calls}
    assert "BrowLAngle" not in pids  # 抛异常的组被跳过
    assert "EyeLSmile" in pids       # 其余参数照常写入
    assert "Breath" not in pids      # 呼吸缺参数同样跳过


def test_procedural_mouth_defer_to_speaking():
    """说话（TTS）期间程序化层不驱动嘴张，避免覆盖口型。"""
    r = _make_renderer()
    r._speaking = True
    r.set_master_emotion("surprised")
    r._proc_cur["mouth_open"] = 0.55
    r._proc_last_t = time.monotonic() - 0.5
    r._update_procedural_emotion()
    assert not any(c[0] == "MouthOpenY" for c in r._model.calls)
    # 不说话时嘴张正常驱动
    r._speaking = False
    r._proc_last_t = time.monotonic() - 0.5
    r._update_procedural_emotion()
    assert any(c[0] == "MouthOpenY" for c in r._model.calls)


def test_procedural_smoothing_configurable():
    r = _make_renderer()
    default = r._proc_smooth_tau
    r.set_procedural_smoothing(0.5)
    assert r._proc_smooth_tau == 0.5
    r.set_procedural_smoothing(-1)  # 非法 → 回退默认
    assert r._proc_smooth_tau == default
    r.set_procedural_smoothing("x")  # 非法类型 → 回退默认
    assert r._proc_smooth_tau == default


def test_base_renderer_default_master_emotion():
    """精灵/VRM 渲染器继承 base 缺省实现，不崩、只更新状态。"""
    from PySide6.QtWidgets import QWidget
    from avatar.sprite_renderer import SpriteRenderer
    r = SpriteRenderer(parent=QWidget())
    r.set_master_emotion("happy")
    assert r.current_emotion == "happy"
    r.set_procedural_smoothing(0.3)  # 空操作，不崩


def test_pet_wiring_source_level():
    """pet.py 已接入：情绪链路同步 master emotion + 助手消息走流式。"""
    import pet as pet_mod
    src = open(pet_mod.__file__, encoding="utf-8").read()
    assert "_sync_renderer_master_emotion" in src
    assert "start_assistant_stream" in src
    # 三个情绪写入点都同步渲染器
    assert src.count("_sync_renderer_master_emotion(") >= 3
