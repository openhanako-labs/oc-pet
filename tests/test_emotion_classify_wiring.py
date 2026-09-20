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


def test_pet_py_writes_vad_before_master_emotion():
    """VAD 必须在 _sync_renderer_master_emotion **之前**写。

    因为那个方法里有 _va_hold_until 保护，且它会用 _EMOTION_VA 表
    覆盖 _va_target（表里只有 7 个情绪，会盖掉分类器的连续值）。

    注意：`_sync_renderer_master_emotion` 在 pet.py 里出现多次，
    要比较的是**回复链路内**的那一次（紧跟 VAD 写入之后的）。
    """
    src = open(os.path.join(ROOT, "pet.py"), encoding="utf-8").read()
    i_vad = src.index("self._pending_reply_vad()")
    # 从 VAD 写入点往后找最近的一次同步
    i_sync = src.index("self._sync_renderer_master_emotion(self._current_emotion)", i_vad)
    assert i_vad < i_sync, "VAD 必须在 master emotion 同步之前写"


def test_base_set_va_target_default_returns_false():
    """基类默认实现返回 False（不支持连续 VA 的渲染器）。"""
    from avatar.base import AvatarRenderer
    assert AvatarRenderer.set_va_target(None, 0.5, 0.5) is False


def test_config_has_emotion_classifier():
    import json
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    ec = cfg.get("emotion_classifier")
    assert ec is not None, "config.json 缺 emotion_classifier 段"
    assert ec.get("enabled") is True
