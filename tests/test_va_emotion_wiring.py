"""VA→情绪映射 + 引擎熔断（2026-09-20）。

## 背景：一个「单测全绿、功能是死的」的真实案例

`expression_director` 原先接线在 `_direct_expression(emotion)` 上，
而 `emotion` 变量在主链路上**恒为 "neutral"**：

- LLM 主链路输出 `[feel:valence,arousal]`（VA 坐标）
- `[emotion:xxx]` 标签**只有兜底才补，补的就是 neutral**
- 实测（tools/verify_emotion_wiring.py）：带 `[feel:]` 的样本
  `parse_emotion` 全部返回 "neutral"

后果：决策器永远收到 neutral → 只能选「平静」组预设。

而此前的 12/12 验证是**直接喂情绪给决策器**，绕过了这条接线 ——
所以测试全绿但功能无效。

## 修复

1. `_va_to_emotion()`：VA 坐标 → 情绪词（主链路真信号）
2. `_direct_expression_from_va()`：从 VA 驱动决策
3. pet.py 优先用 `action_intent["va"]`，无 VA 才回退离散情绪词
4. **引擎熔断**：`decide()` 是同步阻塞且跑在主线程，
   引擎卡死时每次回复会卡满 8 秒（客户端 timeout）。
   连续失败 2 次停用 300 秒。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pet_mixins.perception_mixin import PerceptionMixin  # noqa: E402


# ── 1. VA → 情绪映射 ──

@pytest.mark.parametrize("v,a,want", [
    (0.8, 0.6, "happy"),     # 积极高唤起 → 开心
    (0.9, 0.3, "happy"),     # 积极低唤起 → 仍开心
    (0.9, -0.8, "happy"),    # 积极但很平静 → 开心（不分唤起）
    (-0.6, 0.4, "sad"),      # 消极 + 中等唤起 → 失落（不是生气！实测样本）
    (-0.7, 0.9, "angry"),    # 消极 + 很高唤起 → 生气
    (-0.8, -0.5, "sad"),     # 消极低唤起 → 失落
    (0.0, 0.6, "surprised"),  # 中性价 + 高唤起 → 惊讶
])
def test_va_to_emotion(v, a, want):
    assert PerceptionMixin._va_to_emotion(v, a) == want


@pytest.mark.parametrize("v,a", [
    (0.0, 0.0),      # 完全中性
    (0.1, 0.2),      # 弱信号
    (0.05, -0.1),
    (-0.1, 0.1),
])
def test_va_neutral_zone_returns_empty(v, a):
    """中性区返回空串——表示「不值得动」，调用方应跳过。

    不要默认成 neutral 去占一次决策（那会把「平静」组预设刷上去）。
    """
    assert PerceptionMixin._va_to_emotion(v, a) == ""


def test_va_bad_input_returns_empty():
    assert PerceptionMixin._va_to_emotion(None, None) == ""
    assert PerceptionMixin._va_to_emotion("x", "y") == ""


def test_va_boundary_is_half_open():
    """边界：valence 恰好 0.15 应进积极区（下限闭）。"""
    assert PerceptionMixin._va_to_emotion(0.15, 0.5) == "happy"


def test_angry_threshold_is_conservative():
    """生气阈值取 arousal ≥ 0.7（保守）——避免把失落误判成生气。

    实测依据：「唉，今天又被领导骂了」给的是 VA(-0.6, +0.4)，
    那是失落不是生气。
    """
    assert PerceptionMixin._va_to_emotion(-0.6, 0.4) == "sad"
    assert PerceptionMixin._va_to_emotion(-0.6, 0.69) == "sad"
    assert PerceptionMixin._va_to_emotion(-0.6, 0.7) == "angry"


# ── 2. 端到端：真实解析器 → VA → 情绪 ──

def test_real_llm_output_produces_non_neutral_emotion():
    """核心回归：真实 LLM 输出（含 [feel:]）必须产出**非 neutral** 的情绪。

    这条钉死「接线有误」那个 bug——修复前所有样本都映射到 neutral。
    """
    from core.conversation_engine import ConversationEngine

    eng = object.__new__(ConversationEngine)
    samples = [
        ("[feel:0.8,0.6] 你今天这个反应好快啊", "happy"),
        ("[feel:-0.6,0.4] 唉，今天又被领导骂了", "sad"),
        ("[feel:-0.8,-0.5] 我最近有点累", "sad"),
    ]
    for raw, want in samples:
        _text, intent = eng.parse_action_intent(raw)
        va = (intent or {}).get("va")
        assert va, f"真实解析器没提取出 VA: {raw!r}"
        got = PerceptionMixin._va_to_emotion(va[0], va[1])
        assert got == want, f"{raw!r} -> {got}（期望 {want}）"


def test_emotion_tag_path_still_works():
    """无 VA 时（显式 [emotion:] 路径）仍应能映射——不能只修一条路。"""
    assert PerceptionMixin._EMOTION_ZH.get("happy") == "开心"
    assert PerceptionMixin._EMOTION_ZH.get("sad") == "失落"


def test_every_va_output_has_chinese_label():
    """守卫：`_va_to_emotion` 产出的每个词，`_EMOTION_ZH` 里都要有中文。

    这条是**同类陷阱的防火墙**：`_direct_expression` 拿到英文词后立刻查
    `_EMOTION_ZH`，查不到就静默 return。若映射表与中文表不同步，
    整条链还是断的——而单测可能全绿。
    """
    produced = {emo for _v1, _v2, _a1, _a2, emo in PerceptionMixin._VA_EMOTION_RULES}
    missing = sorted(e for e in produced if e not in PerceptionMixin._EMOTION_ZH)
    assert not missing, f"VA 映射产出的词在 _EMOTION_ZH 里没有中文: {missing}"


def test_va_rules_are_wellformed():
    """规则表结构校验：每条 5 元组，范围合法，不重叠。"""
    rules = PerceptionMixin._VA_EMOTION_RULES
    assert rules, "规则表不能为空"
    for r in rules:
        assert len(r) == 5, f"应为 5 元组: {r}"
        v_lo, v_hi, a_lo, a_hi, emo = r
        assert v_lo < v_hi and a_lo < a_hi, f"范围下界应小于上界: {r}"
        assert isinstance(emo, str) and emo, f"情绪词应为非空串: {r}"

    # 抽样验证：整个 -1..1 方格上，最多命中一条规则
    for vi in range(-10, 11):
        for ai in range(-10, 11):
            v, a = vi / 10.0, ai / 10.0
            hits = [r[4] for r in rules
                    if r[0] <= v < r[1] and r[2] <= a < r[3]]
            assert len(hits) <= 1, f"VA({v},{a}) 命中了多条规则: {hits}"


# ── 3. 引擎熔断（防止主线程被卡死）──

class _Stub(PerceptionMixin):
    pass


@pytest.fixture()
def stub():
    return _Stub()


def test_engine_usable_initially(stub):
    assert stub._expression_engine_usable(None) is True


def test_engine_circuit_breaks_after_two_failures(stub):
    """连续失败 2 次应熔断——否则每次回复都卡 8 秒。"""
    stub._note_expression_engine_result(False)
    assert stub._expression_engine_usable(None) is True, "单次失败不熔断"
    stub._note_expression_engine_result(False)
    assert stub._expression_engine_usable(None) is False, "两次失败应熔断"


def test_engine_recovers_on_success(stub):
    stub._note_expression_engine_result(False)
    stub._note_expression_engine_result(False)
    assert stub._expression_engine_usable(None) is False
    stub._note_expression_engine_result(True)
    assert stub._expression_engine_usable(None) is True, "成功后应立即复位"


def test_circuit_break_is_time_bounded(stub):
    """熔断有时限（300s），不是永久关闭。"""
    stub._note_expression_engine_result(False)
    stub._note_expression_engine_result(False)
    until = stub._expr_engine_cool_until
    import time
    assert until > time.time(), "熔断应有未来时间点"
    assert until - time.time() <= 305, "熔断时长应约 300s"


# ── 4. 接线本身（源码级守卫）──

def test_pet_py_prefers_va_over_emotion():
    """pet.py 应优先用 action_intent['va']，无 VA 才回退情绪词。"""
    src = open(os.path.join(ROOT, "pet.py"), encoding="utf-8").read()
    i = src.index("_direct_expression_from_va")
    body = src[max(0, i - 900):i + 400]
    assert 'action_intent.get("va")' in body, "未从 action_intent 取 VA"
    assert "_direct_expression(emotion" in body, "缺少无 VA 时的回退"


def test_config_defaults_to_disabled():
    """默认关闭——引擎 /api/run-rlcd 实测卡死，开启会让主线程卡 8 秒。

    等该端点可用后再开。这条防止「误开一个已知不可用的组件」。
    """
    import json
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    ed = cfg.get("expression_director", {}) or {}
    assert ed.get("enabled") is False, (
        "expression_director 应默认关闭：引擎决策端点实测卡死，"
        "开启会让主线程每次回复卡 8 秒"
    )
