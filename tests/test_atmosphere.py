# -*- coding: utf-8 -*-
"""氛围累积层单测（core/atmosphere.py）。

方案见 docs/氛围累积层-方案-2026-09-21.md v3。这里锁住的是**机制的契约**，
不是参数取值——参数由离线回放扫参决定，测试只保证：
1. 份额 EMA 会收敛、数值不漂；
2. 状态机不振荡（迟滞生效）；
3. neu 主导**永不**触发（"没倾向"不能被读成"有倾向"）；
4. 关掉开关时零副作用；
5. 任何异常不冒出（绝不影响对话）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.atmosphere import (
    ACT_ARM, ACT_DISABLED, ACT_HOLD, ACT_IDLE, ACT_RELEASE, ACT_REFRESH,
    POL_NEG, POL_NEU, POL_POS,
    AtmosphereConfig, AtmosphereState,
    clear_value, dominant, new_shares, polarity_of, prompt_section, update_shares,
)

CFG = {"enabled": True, "beta": 0.10, "theta_hi": 0.40, "theta_lo": 0.25}


# ── 纯函数 ──────────────────────────────────────────────

def test_polarity_mapping():
    assert polarity_of("happy") == POL_POS
    assert polarity_of("anxiety") == POL_NEG
    assert polarity_of("neutral") == POL_NEU
    # 未知 / 空 / 非字符串：一律 neu（宁可不动，不能乱动）
    assert polarity_of("不存在的情感") == POL_NEU
    assert polarity_of("") == POL_NEU
    assert polarity_of(None) == POL_NEU
    assert polarity_of(123) == POL_NEU


def test_shares_start_neutral():
    s = new_shares()
    assert s[POL_NEU] == 1.0
    assert clear_value(s) == 0.0
    assert dominant(s) == POL_NEU


def test_update_is_one_hot_ema_and_keeps_sum_one():
    s = new_shares()
    for _ in range(50):
        s = update_shares(s, POL_POS, 0.10)
    assert abs(sum(s.values()) - 1.0) < 1e-9
    assert s[POL_POS] > s[POL_NEU]
    assert dominant(s) == POL_POS


def test_update_does_not_mutate_input():
    s = new_shares()
    before = dict(s)
    update_shares(s, POL_POS, 0.5)
    assert s == before


def test_clear_value_ignores_neutral():
    s = {POL_POS: 0.3, POL_NEG: 0.2, POL_NEU: 0.5}
    assert clear_value(s) == pytest.approx(0.3)
    assert dominant(s) == POL_NEU  # 0.5 > 0.3，neu 主导


def test_dominant_prefers_neutral_on_tie():
    s = {POL_POS: 0.4, POL_NEG: 0.0, POL_NEU: 0.4}
    assert dominant(s) == POL_NEU


# ── 配置归一 ────────────────────────────────────────────

def test_config_defaults_and_disabled():
    c = AtmosphereConfig.from_dict(None)
    assert c.enabled is False
    assert c.beta == 0.10 and c.theta_hi == 0.40 and c.theta_lo == 0.25


def test_config_bad_values_fall_back():
    c = AtmosphereConfig.from_dict({"enabled": True, "beta": "x", "theta_hi": None})
    assert c.beta == 0.10 and c.theta_hi == 0.40


def test_config_reversed_hysteresis_falls_back():
    """迟滞带写反（lo > hi）→ 回退默认，别把状态机拧成振荡器。"""
    c = AtmosphereConfig.from_dict({"enabled": True, "theta_hi": 0.1, "theta_lo": 0.9})
    assert c.theta_hi == 0.40 and c.theta_lo == 0.25


def test_render_min_turns_at_least_one():
    c = AtmosphereConfig.from_dict({"enabled": True, "render_min_turns": 0})
    assert c.render_min_turns == 1


# ── 状态机 ──────────────────────────────────────────────

def test_disabled_updates_nothing():
    st = AtmosphereState({"enabled": False})
    before = st.snapshot()["shares"]
    for _ in range(20):
        d = st.observe("anxiety")
        assert d.action == ACT_DISABLED
    assert st.snapshot()["shares"] == before
    assert st.snapshot()["turns"] == 0


def test_neutral_never_arms():
    """neu 主导时即便 clear 很大也不触发——份额会归一，别被自己骗。"""
    st = AtmosphereState(CFG)
    for _ in range(60):
        d = st.observe("neutral")
    assert d.action == ACT_IDLE
    assert d.polarity == POL_NEU


def test_arm_after_sustained_polarity():
    st = AtmosphereState(CFG)
    actions = [st.observe("anxiety").action for _ in range(20)]
    assert ACT_ARM in actions, f"持续同向未触发：{actions}"
    st.mark_rendered("你们这几轮偏紧绷")
    assert st.text == "你们这几轮偏紧绷"


def test_hysteresis_does_not_oscillate():
    """进入 rendered 后，clear 在阈值附近抖动不应反复 arm/release。"""
    st = AtmosphereState(CFG)
    for _ in range(20):
        st.observe("anxiety")
    st.mark_rendered("紧绷")

    seq = [st.observe("neutral").action for _ in range(10)]
    # 一次回落之后应稳定在 idle，不允许出现 arm/release 交替
    assert seq.count(ACT_RELEASE) <= 1, f"迟滞失效，反复抖动：{seq}"
    assert ACT_ARM not in seq, f"回落过程中又触发：{seq}"


def test_release_when_clear_falls_below_lo():
    st = AtmosphereState(CFG)
    for _ in range(30):
        st.observe("anxiety")
    st.mark_rendered("紧绷")
    actions = []
    for _ in range(60):
        d = st.observe("neutral")
        actions.append(d.action)
        if d.action == ACT_RELEASE:
            break
    assert ACT_RELEASE in actions, "长期走平后没有回落"
    assert st.text == ""
    assert st.snapshot()["rendered"] is False


def test_refresh_by_turns_and_by_drift():
    st = AtmosphereState(CFG)
    for _ in range(20):
        st.observe("anxiety")
    st.mark_rendered("紧绷")

    # 同向继续 → 份额继续走高 → drift 触发 refresh
    got = None
    for _ in range(30):
        d = st.observe("anxiety")
        if d.action == ACT_REFRESH:
            got = d
            break
    assert got is not None, "份额漂移后没有要求重渲染"

    # 按轮数也能触发（把 delta 调大到不可能，只剩轮数这一条路）
    st2 = AtmosphereState({**CFG, "delta_render": 10.0, "render_min_turns": 3})
    for _ in range(20):
        st2.observe("anxiety")
    st2.mark_rendered("紧绷")
    acts = [st2.observe("anxiety").action for _ in range(3)]
    assert ACT_REFRESH in acts, f"轮数阈值未生效：{acts}"


def test_hold_while_stable_after_render():
    st = AtmosphereState({**CFG, "delta_render": 10.0, "render_min_turns": 100})
    for _ in range(20):
        st.observe("anxiety")
    st.mark_rendered("紧绷")
    assert st.observe("anxiety").action == ACT_HOLD


def test_set_config_disable_clears_state():
    st = AtmosphereState(CFG)
    for _ in range(20):
        st.observe("anxiety")
    st.mark_rendered("紧绷")
    st.set_config({"enabled": False})
    snap = st.snapshot()
    assert snap["rendered"] is False and snap["text"] == ""
    assert snap["shares"][POL_NEU] == 1.0


def test_observe_never_raises(monkeypatch):
    """内部异常不得冒出（硬纪律：绝不影响对话）。"""
    st = AtmosphereState(CFG)
    monkeypatch.setattr("core.atmosphere.update_shares",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    d = st.observe("anxiety")
    assert d.action == ACT_IDLE


# ── prompt 段 ───────────────────────────────────────────

def test_prompt_section_prefers_model_text():
    assert prompt_section("你们这几轮偏紧绷") == "【氛围】你们这几轮偏紧绷"


def test_prompt_section_falls_back_to_numeric():
    shares = {POL_POS: 0.0, POL_NEG: 0.5, POL_NEU: 0.5}
    out = prompt_section("", shares)
    assert out.startswith("【氛围】") and "紧绷" in out


def test_prompt_section_empty_when_no_signal():
    assert prompt_section("", new_shares()) == ""
    assert prompt_section("", None) == ""
    assert prompt_section("   ") == ""


def test_snapshot_shape():
    st = AtmosphereState(CFG)
    st.observe("happy")
    snap = st.snapshot()
    for k in ("enabled", "shares", "clear", "polarity", "rendered", "turns", "text"):
        assert k in snap
    assert set(snap["shares"]) == {POL_POS, POL_NEG, POL_NEU}


# ── 输入入口：类别 vs 槽位（回归守卫）───────────────────

def test_observe_label_accepts_slot_directly():
    """★ 回归守卫：槽位标签必须被**直接**接受，不能再被映射一次。

    2026-09-21 真踩过的坑：结构输入产出的就是槽位标签，若让它走 observe()，
    会被 polarity_of 再映一次——而 polarity_of("pos") 查不到预设、**静默回 neu**，
    于是整层永不触发（扫参脚本全表 0.0%，连理想输入都是 0，且**不报错**）。
    """
    st = AtmosphereState(CFG)
    for _ in range(30):
        d = st.observe_label(POL_POS)
    assert d.action == ACT_ARM, f"槽位被二次映射了：{d.action} clear={d.clear:.3f}"
    assert st.snapshot()["polarity"] == POL_POS


def test_observe_label_unknown_label_is_neutral():
    st = AtmosphereState(CFG)
    for _ in range(30):
        d = st.observe_label("完全不存在的标签")
    assert d.action == ACT_IDLE
    assert st.snapshot()["polarity"] == POL_NEU


# ── 隔时回落（decay）────────────────────────────────────

def _armed(beta=0.10):
    st = AtmosphereState({**CFG, "beta": beta})
    for _ in range(30):
        st.observe_label(POL_NEG)      # ← 槽位，不是类别（走 observe 会被再映一次→neu）
    st.mark_rendered("紧绷")
    return st


def test_decay_zero_elapsed_is_identity():
    st = _armed()
    before = st.snapshot()["shares"]
    assert st.decay(0) == 1.0
    assert st.decay(None) == 1.0
    assert st.decay(-5) == 1.0
    assert st.snapshot()["shares"] == before


def test_decay_one_half_life_halves_edge():
    st = _armed()
    before = st.snapshot()["shares"]
    f = st.decay(3 * 3600)          # 正好一个半衰期
    assert abs(f - 0.5) < 1e-9
    after = st.snapshot()["shares"]
    assert abs(after[POL_NEG] - before[POL_NEG] * 0.5) < 1e-6
    assert abs(sum(after.values()) - 1.0) < 1e-9, "份额和必须恒为 1"
    assert after[POL_NEU] > before[POL_NEU]


def test_decay_overnight_effectively_wipes():
    st = _armed()
    st.decay(24 * 3600)
    shares = st.snapshot()["shares"]
    assert shares[POL_NEU] > 0.98, f"隔夜不该还留着氛围：{shares}"


def test_decay_can_trigger_release():
    """隔久了 → 份额被拉回 → 应回落并撤注入，而不是继续注入陈旧的"氛围"。"""
    st = _armed()
    assert st.snapshot()["rendered"] is True
    st.observe(POL_NEU, elapsed_sec=24 * 3600)
    assert st.snapshot()["rendered"] is False
    assert st.snapshot()["text"] == ""


def test_zero_half_life_disables_decay():
    st = AtmosphereState({**CFG, "half_life_hours": 0})
    for _ in range(30):
        st.observe_label(POL_NEG)
    before = st.snapshot()["shares"]
    assert st.decay(999 * 3600) == 1.0
    assert st.snapshot()["shares"] == before


def test_slot_through_category_door_maps_to_neu_and_warns(caplog):
    """★ 记录一条锋利的边：把槽位当类别喂给 observe() → 静默变 neu。

    2026-09-21 连写测试时都本能地写了 observe(POL_NEG)——说明这个误用太好犯。
    所以不能只靠"调用方小心"：必须报一次 warning。
    """
    import logging as _logging
    from core import atmosphere as _a
    _a._UNKNOWN_CATEGORY_WARNED.discard("neg")
    _a._UNKNOWN_CATEGORY_WARNED.discard("pos")
    st = AtmosphereState(CFG)
    with caplog.at_level(_logging.WARNING, logger="core.atmosphere"):
        for _ in range(30):
            st.observe(POL_NEG)
    assert st.snapshot()["polarity"] == POL_NEU, "槽位被当类别，结果应是 neu"
    assert "未知情绪类别" in caplog.text, "误用没有报警"


def test_known_category_does_not_warn(caplog):
    import logging as _logging
    st = AtmosphereState(CFG)
    with caplog.at_level(_logging.WARNING, logger="core.atmosphere"):
        for _ in range(5):
            st.observe("anxiety")
    assert "未知情绪类别" not in caplog.text


def test_decay_never_raises_on_garbage():
    st = _armed()
    assert st.decay("不是数字") == 1.0
    assert st.decay(float("nan")) == 1.0


def test_snapshot_reports_last_decay():
    st = _armed()
    st.decay(3 * 3600)
    assert st.snapshot()["last_decay"] < 1.0
