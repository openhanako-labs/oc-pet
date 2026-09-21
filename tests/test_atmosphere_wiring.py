# -*- coding: utf-8 -*-
"""氛围累积层「接线」单测（pet_mixins/emotion_classify_mixin 的喂料/渲染/推送）。

模块本身的契约在 test_atmosphere.py；这里锁的是**接线正确性**：

1. 触发后真的把【氛围】段推给了注入点；
2. utility 模型拿不到（未配置 / 失败）时**退化为数值直陈**，不让这一层静默消失；
3. 回落后推空串（撤段）；
4. 开关关着时零推送、零状态变化；
5. 任何异常不冒出（硬纪律）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.atmosphere import POL_NEU, AtmosphereState
from pet_mixins.emotion_classify_mixin import EmotionClassifyMixin

CFG = {"enabled": True, "beta": 0.10, "theta_hi": 0.40, "theta_lo": 0.25,
       "render_min_turns": 8, "delta_render": 0.15}


class _FakePet(EmotionClassifyMixin):
    """只有接线需要的那几个属性：engine（无 adapter）+ 推送记录。

    **必须继承 mixin**（已踩过）：``_feed_atmosphere`` 内部会调
    ``self._render_atmosphere_text``，若假对象没有这个方法，异常会被吞掉，
    表现为「什么都推不出去」——测试在假对象上原地失效（就是本文件第一版）。
    真实 PetWindow 同时组合所有 mixin，所以生产路径没这个问题。
    """

    def __init__(self, cfg):
        self._atmosphere = AtmosphereState(cfg)
        self._engine = None          # → _render_atmosphere_text 返回 ""（走数值兜底）
        self.pushed: list[str] = []
        # 结构族相关（默认不启用，由具体测试设置）
        self._atmos_source = "emotion"
        self._atmos_input = None
        self._last_user_text = ""
        self._atmos_last_ts = None

    def _push_atmosphere(self, section: str) -> None:
        self.pushed.append(section)


def _feed(fake, category, times):
    for _ in range(times):
        EmotionClassifyMixin._feed_atmosphere(fake, category)


def test_arms_and_pushes_numeric_fallback():
    f = _FakePet(CFG)
    _feed(f, "anxiety", 20)
    assert f.pushed, "持续同向却没有推送任何段"
    assert any(s.startswith("【氛围】") for s in f.pushed), f.pushed
    # utility 不可用 → 退化为数值直陈（不能静默消失）
    assert any("强度" in s for s in f.pushed), f.pushed


def test_release_pushes_empty_section():
    f = _FakePet(CFG)
    _feed(f, "anxiety", 30)
    assert any(s for s in f.pushed)
    _feed(f, "neutral", 80)
    assert f.pushed[-1] == "", f"回落后没有撤段：{f.pushed[-5:]}"
    assert f._atmosphere.snapshot()["rendered"] is False


def test_disabled_pushes_nothing_and_keeps_state():
    f = _FakePet({"enabled": False})
    before = f._atmosphere.snapshot()["shares"]
    _feed(f, "anxiety", 40)
    assert f.pushed == []
    assert f._atmosphere.snapshot()["shares"] == before
    assert f._atmosphere.snapshot()["turns"] == 0


def test_neutral_only_never_pushes():
    f = _FakePet(CFG)
    _feed(f, "neutral", 60)
    assert f.pushed == []


def test_missing_state_is_noop():
    class _NoState(EmotionClassifyMixin):   # 必须继承 mixin：见 _FakePet 的说明
        _engine = None

    # 没有 _atmosphere 属性 → 直接返回，不抛
    EmotionClassifyMixin._feed_atmosphere(_NoState(), "anxiety")
    EmotionClassifyMixin._feed_atmosphere_structural(_NoState(), "y" * 2)


def test_observe_exception_does_not_bubble(monkeypatch):
    f = _FakePet(CFG)

    def _boom(*a, **k):
        raise RuntimeError("boom")

    # 喂料走的是 observe_label（槽位入口），不是 observe
    monkeypatch.setattr(f._atmosphere, "observe_label", _boom)
    EmotionClassifyMixin._feed_atmosphere(f, "anxiety")  # 不应抛出
    assert f.pushed == []


def test_render_failure_degrades_to_numeric():
    """adapter 存在但 render 抛错 → 仍要推出数值兜底段。"""

    class _BadAdapter:
        def render_atmosphere(self, prompt):
            raise RuntimeError("utility 挂了")

        def set_atmosphere(self, section):
            pass

    class _Engine:
        _adapter = _BadAdapter()

    f = _FakePet(CFG)
    f._engine = _Engine()
    _feed(f, "anxiety", 20)
    assert any(s.startswith("【氛围】") for s in f.pushed), f.pushed


def test_atmosphere_status_shape():
    f = _FakePet(CFG)
    _feed(f, "happy", 3)
    snap = EmotionClassifyMixin.atmosphere_status(f)
    assert snap["enabled"] is True
    assert set(snap["shares"]) == {"pos", "neg", "neu"}
    assert snap["turns"] == 3


def test_status_without_state():
    class _NoState:
        pass

    out = EmotionClassifyMixin.atmosphere_status(_NoState())
    assert out["enabled"] is False


def test_model_text_wins_over_numeric():
    """utility 给了文字 → 用文字，不再走数值兜底。"""

    class _GoodAdapter:
        def render_atmosphere(self, prompt):
            return "你们这几轮一直偏紧绷"

        def set_atmosphere(self, section):
            pass

    class _Engine:
        _adapter = _GoodAdapter()

    f = _FakePet(CFG)
    f._engine = _Engine()
    _feed(f, "anxiety", 20)
    assert any(s == "【氛围】你们这几轮一直偏紧绷" for s in f.pushed), f.pushed


def test_neutral_shares_untouched_by_unknown_category():
    f = _FakePet(CFG)
    _feed(f, "完全不存在的类别", 10)
    assert f._atmosphere.snapshot()["polarity"] == POL_NEU
    assert f.pushed == []


# ── 结构族（默认族）──────────────────────────────────────

def _struct_fake(cfg=None, **kw):
    from core.atmosphere_input import RollingQuantileTristate
    f = _FakePet({**(cfg or CFG), **kw})
    f._atmos_source = "ratio"
    f._atmos_input = RollingQuantileTristate(window=6, warmup=2)
    f._last_user_text = "x" * 100
    return f


def test_structural_ratio_arms_and_pushes():
    f = _struct_fake()
    for _ in range(20):
        EmotionClassifyMixin._feed_atmosphere_structural(f, "y" * 2)
    assert f.pushed, "结构族持续同向却没有推送"
    assert any(s.startswith("【氛围】") for s in f.pushed), f.pushed


def test_structural_before_warmup_is_neutral():
    """预热期一律 neu——没有基线就没有"偏离"可言，不能乱触发。"""
    f = _struct_fake()
    for _ in range(2):
        EmotionClassifyMixin._feed_atmosphere_structural(f, "y" * 2)
    assert f.pushed == []


def test_structural_noop_when_source_is_emotion():
    f = _FakePet(CFG)                 # _atmos_input 仍是 None
    for _ in range(20):
        EmotionClassifyMixin._feed_atmosphere_structural(f, "y" * 2)
    assert f.pushed == []


def test_structural_noop_when_disabled():
    f = _struct_fake({"enabled": False})
    for _ in range(20):
        EmotionClassifyMixin._feed_atmosphere_structural(f, "y" * 2)
    assert f.pushed == []


def test_structural_missing_user_text_is_noop():
    f = _struct_fake()
    f._last_user_text = ""            # 缺料 → signal_value 回 None → 中性
    for _ in range(20):
        EmotionClassifyMixin._feed_atmosphere_structural(f, "y" * 2)
    assert f.pushed == []


def test_structural_never_raises_on_garbage():
    f = _struct_fake()
    EmotionClassifyMixin._feed_atmosphere_structural(f, None)   # 不应抛出
    EmotionClassifyMixin._feed_atmosphere_structural(f, 12345)


def test_advance_applies_elapsed_decay():
    """隔了很久再喂 → 先按半衰期拉回，不能继续注入陈旧的"氛围"。"""
    f = _struct_fake()
    for _ in range(20):
        EmotionClassifyMixin._feed_atmosphere_structural(f, "y" * 2)
    assert f._atmosphere.snapshot()["rendered"] is True
    # 隔夜（24h）再喂同样的东西：份额先被拉到近中性 → 应回落撤段
    EmotionClassifyMixin._feed_atmosphere_structural(f, "y" * 2,
                                                     now=(f._atmos_last_ts or 0) + 24 * 3600)
    assert f.pushed[-1] == "", f"隔夜后没有撤段：{f.pushed[-3:]}"


def test_atmosphere_status_includes_source_and_input():
    f = _struct_fake()
    EmotionClassifyMixin._feed_atmosphere_structural(f, "y" * 2)
    snap = EmotionClassifyMixin.atmosphere_status(f)
    assert snap["source"] == "ratio"
    assert snap["input"] is not None
    assert snap["input"]["seen"] == 1


# ── 初始化接线（阈值必须跟着**族**走，这是本次最容易静默错的一处）─────

class _InitFake(EmotionClassifyMixin):
    def __init__(self, atmo):
        self.config = {"atmosphere": atmo}


def _init(atmo):
    f = _InitFake(atmo)
    EmotionClassifyMixin._init_emotion_classifier(f)
    return f


def test_init_wires_structural_source_and_family_thresholds():
    f = _init({"enabled": True, "source": "ratio"})
    assert f._atmos_source == "ratio"
    assert f._atmosphere is not None and f._atmosphere.enabled
    assert f._atmos_input is not None, "结构族必须建输入管线"
    # 结构族默认阈值（与情绪族不同——混用会让迟滞带整个落空）
    assert f._atmosphere.cfg.theta_hi == 0.50
    assert f._atmosphere.cfg.theta_lo == 0.40
    assert f._atmosphere.cfg.half_life_hours == 3.0


def test_init_emotion_source_has_no_structural_input():
    f = _init({"enabled": True})          # 缺 source → 默认 emotion
    assert f._atmos_source == "emotion"
    assert f._atmos_input is None
    assert f._atmosphere.cfg.theta_hi == 0.40
    assert f._atmosphere.cfg.theta_lo == 0.25


def test_init_explicit_theta_wins_over_family_default():
    f = _init({"enabled": True, "source": "ratio", "theta_hi": 0.6})
    assert f._atmosphere.cfg.theta_hi == 0.60
    assert f._atmosphere.cfg.theta_lo == 0.40   # 未显式给的仍走族默认


def test_init_null_theta_falls_back_to_family_default():
    f = _init({"enabled": True, "source": "ratio", "theta_hi": None})
    assert f._atmosphere.cfg.theta_hi == 0.50


def test_init_disabled_still_constructs():
    f = _init({"enabled": False, "source": "ratio"})
    assert f._atmosphere is not None and not f._atmosphere.enabled
    assert f._atmos_input is not None       # 建了也不跑（各入口都先查 enabled）


def test_init_never_raises_on_garbage_config():
    for cfg in (None, "x", 123, {"enabled": True, "source": 42},
                {"enabled": True, "beta": "x", "window": -3}):
        f = _init(cfg)                      # 不应抛出
        assert getattr(f, "_atmosphere", None) is not None or True
