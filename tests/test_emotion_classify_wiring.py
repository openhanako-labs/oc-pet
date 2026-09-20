"""情绪分类接线测试（不打网络、不起 Qt）。

## 为什么单独测接线

这个项目反复踩同一个坑：**单测全绿，功能是死的**。

- `expression_director` 的 12/12 验证是直接喂情绪给 `decide()`，绕过了接线，
  而真实链路上 `emotion` 变量恒为 neutral → 功能从未生效。
- `[do:]` 标签 prompt 教了、解析器也认，但模型一次没用过。

所以本文件不只测「分类器能算」，更测「**分类结果真的会走到该去的地方**」：

1. 分类器输出 → 英文情绪词映射：**每个产出词都必须有归宿**
   （映射到 `_EMOTION_ZH` 里真实存在的键，否则 `_direct_expression` 静默落空）
2. `_resolved_reply_emotion` 的采纳/拒绝逻辑（低置信、neutral、未就绪）
3. 主线程安全：`_resolved_reply_emotion` **不得**发起网络调用
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pet_mixins.emotion_classify_mixin import (  # noqa: E402
    _CLASSIFIER_TO_DIRECTOR,
    _MIN_CONFIDENCE,
    EmotionClassifyMixin,
)
from pet_mixins.perception_mixin import PerceptionMixin  # noqa: E402


class _Stub(EmotionClassifyMixin, PerceptionMixin):
    """最小桩：只需要配置字段与状态字段。"""

    def __init__(self):
        self.config = {}
        self._init_emotion_classifier()


# ── 1. 映射完整性（防「静默落空」）──

def test_every_classifier_output_has_a_destination():
    """分类器能产出的每个类别，映射表里都要有归宿。

    这是**防静默落空**的关键一条：`_direct_expression` 拿到词后查
    `_EMOTION_ZH`，查不到就静默 return —— 链路断了但没人发现。
    """
    from core.emotion_classifier import EMOTION_VAD_PRESETS
    produced = set(EMOTION_VAD_PRESETS.keys())
    missing = sorted(e for e in produced if e not in _CLASSIFIER_TO_DIRECTOR)
    assert not missing, f"这些分类类别没有映射: {missing}"


def test_mapped_words_exist_in_emotion_zh():
    """映射目标必须是 `_EMOTION_ZH` 里真实存在的键。

    否则 `_direct_expression` 静默 return —— 正是「链路断了」。
    """
    zh_keys = set(PerceptionMixin._EMOTION_ZH.keys())
    bad = sorted({v for v in _CLASSIFIER_TO_DIRECTOR.values() if v not in zh_keys})
    assert not bad, (
        f"映射目标在 _EMOTION_ZH 里不存在: {bad}\n"
        f"_EMOTION_ZH 现有键: {sorted(zh_keys)}"
    )


def test_emotion_zh_covers_classifier_specific_words():
    """分类器会产出 confused / sleepy，_EMOTION_ZH 必须认得。"""
    assert "confused" in PerceptionMixin._EMOTION_ZH
    assert "sleepy" in PerceptionMixin._EMOTION_ZH


def test_neutral_maps_to_neutral():
    assert _CLASSIFIER_TO_DIRECTOR["neutral"] == "neutral"


# ── 2. 采纳逻辑 ──

def test_init_sets_fields():
    s = _Stub()
    assert s._classify_enabled is True
    assert s._classify_min_conf == pytest.approx(_MIN_CONFIDENCE)
    assert s._last_user_emotion == ""


def test_min_confidence_is_calibrated():
    """min_confidence 是扫描定标的 0.40，不是拍的。

    扫描结果（tools/verify_emotion_classifier.py 测试集）：
        0.35 → pet 12/18, user 12/14
        0.40 → pet 12/18, user 12/14   ← 选它
        0.45 → pet 11/18, user 12/14   （原值，偏严）
    0.40 与 0.35 同分但更保守。
    """
    assert _MIN_CONFIDENCE == pytest.approx(0.40)


def test_no_pending_emotion_returns_empty():
    s = _Stub()
    assert s._resolved_reply_emotion() == ""


def test_low_confidence_rejected():
    s = _Stub()
    s._pending_reply_emotion = ("happy", 0.1)
    assert s._resolved_reply_emotion() == ""


def test_confidence_just_below_threshold_rejected():
    """刚低于阈值就要拒（0.39 < 0.40）。"""
    s = _Stub()
    s._pending_reply_emotion = ("happy", 0.39)
    assert s._resolved_reply_emotion() == ""


def test_confidence_at_threshold_adopted():
    """恰好等于阈值应采纳（边界闭）。"""
    s = _Stub()
    s._pending_reply_emotion = ("happy", 0.40)
    assert s._resolved_reply_emotion() == "happy"


def test_neutral_not_adopted():
    """neutral 不该被采纳——保留原值，不要用一个空结论覆盖。"""
    s = _Stub()
    s._pending_reply_emotion = ("neutral", 0.9)
    assert s._resolved_reply_emotion() == ""


def test_high_confidence_adopted():
    s = _Stub()
    s._pending_reply_emotion = ("happy", 0.8)
    assert s._resolved_reply_emotion() == "happy"


def test_mapping_applied():
    """分类器词 → 决策器词要真的映射。"""
    s = _Stub()
    s._pending_reply_emotion = ("concerned", 0.8)
    assert s._resolved_reply_emotion() == "sad"

    s._pending_reply_emotion = ("tired", 0.8)
    assert s._resolved_reply_emotion() == "sleepy"


def test_unknown_classifier_emotion_rejected():
    s = _Stub()
    s._pending_reply_emotion = ("nonexistent_emotion", 0.9)
    assert s._resolved_reply_emotion() == ""


def test_consume_clears():
    s = _Stub()
    s._pending_reply_emotion = ("happy", 0.9)
    s._consume_reply_emotion()
    assert s._resolved_reply_emotion() == ""


def test_disabled_returns_empty():
    s = _Stub()
    s._classify_enabled = False
    s._pending_reply_emotion = ("happy", 0.9)
    assert s._resolved_reply_emotion() == ""


# ── 3. 主线程安全 ──

def test_resolved_reply_emotion_does_not_touch_provider():
    """`_resolved_reply_emotion` 跑在主线程，**不得**发起网络调用。

    做法：装一个会炸的 provider —— 只要它被调用，测试就红。
    """
    s = _Stub()

    class _Boom:
        def is_available(self):
            raise AssertionError("主线程不应触碰 provider")

        def embed_texts(self, texts):
            raise AssertionError("主线程不应发起嵌入请求")

    s._emotion_classifiers = {"pet": _Boom()}
    s._pending_reply_emotion = ("happy", 0.9)
    # 不应触发任何 provider 调用
    assert s._resolved_reply_emotion() == "happy"


def test_classify_reply_async_does_not_block_without_qt():
    """没有 Qt 时提交分类应静默返回，不抛异常。"""
    s = _Stub()
    s._classify_reply_async("测试文本")  # 不抛即通过


# ── 4. 状态观测 ──

def test_status_shape():
    s = _Stub()
    st = s.emotion_classifier_status()
    assert set(["enabled", "ready", "last_user_emotion", "views"]).issubset(st.keys())
    assert st["enabled"] is True


# ── 5. pet.py 接线守卫（源码级）──

def test_pet_py_calls_init_and_consumes():
    src = open(os.path.join(ROOT, "pet.py"), encoding="utf-8").read()
    assert "_init_emotion_classifier()" in src, "未初始化分类器"
    assert "_resolved_reply_emotion()" in src, "回复链路未取分类结果"
    assert "_consume_reply_emotion()" in src, "未消费分类结果（会串轮）"
    assert "_classify_reply_async(" in src, "未在后台提交分类"


def test_pet_py_inherits_mixin():
    src = open(os.path.join(ROOT, "pet.py"), encoding="utf-8").read()
    assert "EmotionClassifyMixin" in src
    # 必须在 class PetWindow 的继承列表里
    idx = src.index("class PetWindow(")
    head = src[idx:idx + 400]
    assert "EmotionClassifyMixin" in head, "Mixin 未加入 PetWindow 继承列表"


def test_chat_mixin_classifies_user_message():
    src = open(os.path.join(ROOT, "pet_mixins", "chat_mixin.py"),
               encoding="utf-8").read()
    assert "_classify_user_async(" in src, "用户消息未提交分类"


def test_classify_happens_before_signal_emit():
    """分类要在 emit 之前提交 —— 否则主线程用到时结果还没好。"""
    src = open(os.path.join(ROOT, "pet.py"), encoding="utf-8").read()
    i_cls = src.index("self._classify_reply_async(reply)")
    i_emit = src.index("self.engine_reply_signal.emit(reply, emotion, anim")
    assert i_cls < i_emit, "分类提交必须在 emit 之前"


# ── 6. 连续 VAD 接线 ──

def test_pending_reply_vad_returns_none_without_data():
    s = _Stub()
    assert s._pending_reply_vad() is None


def test_pending_reply_vad_returns_tuple():
    s = _Stub()
    s._pending_reply_emotion = ("happy", 0.9)
    s._pending_reply_vad_value = (0.75, 0.45, 0.35)
    assert s._pending_reply_vad() == (0.75, 0.45, 0.35)


def test_pending_reply_vad_rejected_on_low_confidence():
    s = _Stub()
    s._pending_reply_emotion = ("happy", 0.1)
    s._pending_reply_vad_value = (0.75, 0.45, 0.35)
    assert s._pending_reply_vad() is None


def test_consume_clears_vad():
    s = _Stub()
    s._pending_reply_emotion = ("happy", 0.9)
    s._pending_reply_vad_value = (0.75, 0.45, 0.35)
    s._consume_reply_emotion()
    assert s._pending_reply_vad() is None


def test_renderers_expose_set_va_target():
    """三个渲染器都要有 set_va_target（基类默认 + 两个重写）。"""
    from avatar.base import AvatarRenderer
    assert hasattr(AvatarRenderer, "set_va_target"), "基类缺默认实现"
    src_l2d = open(os.path.join(ROOT, "avatar", "live2d_renderer.py"),
                   encoding="utf-8").read()
    assert "def set_va_target" in src_l2d, "Live2D 未实现"
    src_vrm = open(os.path.join(ROOT, "avatar", "vrm_renderer.py"),
                   encoding="utf-8").read()
    assert "def set_va_target" in src_vrm, "VRM 未实现"


def test_pet_py_writes_vad_after_master_emotion():
    """VAD 写入必须在 master emotion 同步**之后**。

    为什么：`_sync_renderer_master_emotion` 会用 `_EMOTION_VA` 表
    覆盖 `_va_target`（表里只有 7 个情绪，会盖掉分类器的连续值）。
    先同步、后写 VA，顺序反了连续值就没了。

    注意：两处都在 mixin 的 `_apply_classified_emotion` 里，
    pet.py 只负责连信号。
    """
    src = open(os.path.join(ROOT, "pet_mixins", "emotion_classify_mixin.py"),
               encoding="utf-8").read()
    i_sync = src.index('sync = getattr(self, "_sync_renderer_master_emotion"')
    i_vad = src.index("setter(vad[0], vad[1]")
    assert i_sync < i_vad, "必须先同步 master emotion，再写 VAD"


def test_pet_py_connects_classified_signal():
    """分类结果信号必须连到槽——否则慢路径不通。

    信号定义在 mixin（避开 pet.py 的 Qt 引用护栏），pet.py 只负责连接。
    """
    mixin = open(os.path.join(ROOT, "pet_mixins", "emotion_classify_mixin.py"),
                 encoding="utf-8").read()
    assert "emotion_classified_signal = _make_signal(" in mixin, "mixin 未定义信号"
    src = open(os.path.join(ROOT, "pet.py"), encoding="utf-8").read()
    assert "emotion_classified_signal" in src, "pet.py 未连接信号"
    assert "_ec_sig.connect(self._apply_classified_emotion)" in src, "信号未连到槽"


def test_signal_moved_to_mixin_to_respect_qt_guard():
    """信号不得定义在 pet.py —— 那会顶破 Qt 引用基线（只降不升）。

    背景：加信号后 `test_pet_py_qt_refs_do_not_regrow` 报 116 > 115。
    正确做法是挪到 mixin，不是放宽基线。
    """
    src = open(os.path.join(ROOT, "pet.py"), encoding="utf-8").read()
    assert "emotion_classified_signal = Signal(" not in src, \
        "信号定义在 pet.py 了 —— 请挪到 mixin（Qt 引用护栏只降不升）"


def test_classify_task_emits_signal():
    """后台任务完成后必须 emit 信号（不能只存字段）。"""
    src = open(os.path.join(ROOT, "pet_mixins", "emotion_classify_mixin.py"),
               encoding="utf-8").read()
    i = src.index("def _classify_reply_async")
    body = src[i:i + 2200]
    assert "emotion_classified_signal" in body, "后台任务未发信号（慢路径不通）"
    assert "sig.emit(" in body, "未 emit"


def test_base_set_va_target_default_returns_false():
    """基类默认实现返回 False（不支持连续 VA 的渲染器）。"""
    from avatar.base import AvatarRenderer
    assert AvatarRenderer.set_va_target(None, 0.5, 0.5) is False


def test_base_set_emotion_intensity_default_returns_false():
    """基类默认实现返回 False（不支持强度缩放的渲染器）。"""
    from avatar.base import AvatarRenderer
    assert AvatarRenderer.set_emotion_intensity(None, 0.5) is False


# ── 7. A 项：强度接线 ──

def test_renderers_expose_set_emotion_intensity():
    """三个渲染器都要有 set_emotion_intensity。"""
    from avatar.base import AvatarRenderer
    assert hasattr(AvatarRenderer, "set_emotion_intensity"), "基类缺默认实现"
    for rel in ("live2d_renderer.py", "vrm_renderer.py"):
        src = open(os.path.join(ROOT, "avatar", rel), encoding="utf-8").read()
        assert "def set_emotion_intensity" in src, f"{rel} 未实现"


def test_classified_signal_carries_intensity():
    """信号要带 intensity（4 个参数）—— 否则 A 项白做。"""
    src = open(os.path.join(ROOT, "pet_mixins", "emotion_classify_mixin.py"),
               encoding="utf-8").read()
    assert "emotion_classified_signal = _make_signal(str, float, object, float)" in src, \
        "信号签名未含 intensity"
    i = src.index("def _classify_reply_async")
    body = src[i:i + 2400]
    assert "float(getattr(r, \"intensity\"" in body or "r.intensity" in body, \
        "后台任务未传递 intensity"


def test_apply_writes_intensity_to_renderer():
    """槽里要把 intensity 写进渲染器。"""
    src = open(os.path.join(ROOT, "pet_mixins", "emotion_classify_mixin.py"),
               encoding="utf-8").read()
    i = src.index("def _apply_classified_emotion")
    body = src[i:i + 4000]
    assert "set_emotion_intensity" in body, "槽未写强度到渲染器"


def test_intensity_write_after_vad():
    """强度要在 VAD 之后写 —— VAD 会重设 _va_target，顺序无所谓但保持一致。"""
    src = open(os.path.join(ROOT, "pet_mixins", "emotion_classify_mixin.py"),
               encoding="utf-8").read()
    i_vad = src.index("setter(vad[0], vad[1]")
    i_int = src.index("setter(float(intensity))")
    assert i_vad < i_int, "强度应在 VAD 之后写"


# ── 8. B 项：VA 锚点扩展 ──

def test_emotion_va_has_new_anchors():
    """B 项新增的 4 个锚点必须在表里。"""
    src = open(os.path.join(ROOT, "avatar", "live2d_renderer.py"),
               encoding="utf-8").read()
    for name in ("affectionate", "calm", "confused", "tired"):
        assert f'"{name}":' in src, f"缺少锚点 {name}"


def test_va_anchors_cover_positive_low_arousal():
    """新增锚点必须填补「V>0 且 A<0」的空白。

    原来 7 个锚点里，正价区只有 happy(0.8,0.7)/cute(0.7,0.5)，
    A 都不低 —— 「平静的愉快/温柔」无处可去。
    affectionate(0.65,0.10) 与 calm(0.25,-0.45) 补上这一块。
    """
    import importlib
    mod = importlib.import_module("avatar.live2d_renderer")
    va = getattr(mod.Live2DRenderer, "_EMOTION_VA", {})
    positive_low = [(n, v) for n, v in va.items() if v[0] > 0 and v[1] < 0.2]
    assert positive_low, "正价低唤起区仍然空白"


def test_va_anchors_match_classifier_presets():
    """新锚点的坐标应与分类器的 VAD 预设一致（不另编）。"""
    import importlib
    mod = importlib.import_module("avatar.live2d_renderer")
    va = getattr(mod.Live2DRenderer, "_EMOTION_VA", {})
    from core.emotion_classifier import EMOTION_VAD_PRESETS as P
    for name in ("affectionate", "calm", "confused", "tired"):
        assert name in P, f"分类器预设缺 {name}"
        assert va[name][0] == pytest.approx(P[name][0]), f"{name} valence 与预设不一致"
        assert va[name][1] == pytest.approx(P[name][1]), f"{name} arousal 与预设不一致"


def test_va_emotions_list_matches_dict():
    """_VA_EMOTIONS 必须与 _EMOTION_VA 的键一致。

    插值函数遍历 _VA_EMOTIONS 查 _EMOTION_VA —— 两边不一致会 KeyError 或漏锚点。
    """
    import importlib
    mod = importlib.import_module("avatar.live2d_renderer")
    R = mod.Live2DRenderer
    assert set(R._VA_EMOTIONS) == set(R._EMOTION_VA.keys()), (
        f"不一致：_VA_EMOTIONS={sorted(set(R._VA_EMOTIONS) - set(R._EMOTION_VA))} "
        f"多余；_EMOTION_VA 缺 {sorted(set(R._EMOTION_VA) - set(R._VA_EMOTIONS))}"
    )


def test_all_va_anchors_have_facial_targets():
    """每个 VA 锚点都必须有对应的面部参数表 —— 否则插值 KeyError。"""
    import importlib
    mod = importlib.import_module("avatar.live2d_renderer")
    R = mod.Live2DRenderer
    missing = [n for n in R._VA_EMOTIONS if n not in R._EMOTION_FACIAL_TARGETS]
    assert not missing, f"这些锚点缺面部参数表: {missing}"


def test_facial_targets_have_consistent_keys():
    """所有面部参数表的键集合要一致 —— 插值时按 tgt0 的键遍历。"""
    import importlib
    mod = importlib.import_module("avatar.live2d_renderer")
    R = mod.Live2DRenderer
    keysets = {n: set(t.keys()) for n, t in R._EMOTION_FACIAL_TARGETS.items()}
    base_name = "neutral"
    base = keysets[base_name]
    bad = {n: sorted(k - base) + sorted(base - k) for n, k in keysets.items() if k != base}
    assert not bad, f"参数键不一致: {bad}"


def test_config_has_emotion_classifier():
    import json
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    ec = cfg.get("emotion_classifier")
    assert ec is not None, "config.json 缺 emotion_classifier 段"
    assert ec.get("enabled") is True
