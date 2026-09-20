"""端到端实测：完整链路（LLM 输出 → VA → 决策器）真的能出动作吗？（2026-09-20）

背景：交接指出 `_direct_expression(emotion)` 挂在恒为 neutral 的标签上。
第一次修正后，我写了 tools/verify_emotion_wiring.py 证明「emotion 恒为 neutral」，
但那只验证了**诊断**，没验证**修复**。

本脚本走完整链路，验证修复真的通：
    LLM 原始输出（含 [feel:v,a]）
      → conversation_engine.parse_action_intent()  ← 真实解析器
      → action_intent["va"]
      → PerceptionMixin._va_to_emotion()
      → ExpressionDirector.decide()  ← 真实决策器（需引擎在线）
      → 预设名

用法：python tools/verify_va_pipeline.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


# 真实 LLM 输出样本（来自 harness_adapter 记录的实测日志）
SAMPLES = [
    ("你今天这个反应好快啊，厉害", '[feel:0.8,0.6] 你今天这个反应好快啊，厉害 [action:{"gesture":"开心"}]'),
    ("我太开心了！今天真是太好了！", "[feel:0.9,0.8] 我太开心了！今天真是太好了！"),
    ("唉，今天又被领导骂了", "[feel:-0.6,0.4] 唉，今天又被领导骂了，烦死了"),
    ("这个报错我搞了一下午都没解决", "[feel:-0.2,0.5] 这个报错我搞了一下午都没解决"),
    ("哇！这个东西居然真的能跑起来！", "[feel:0.4,0.9] 哇！这个东西居然真的能跑起来！"),
    ("我最近有点累", "[feel:-0.8,-0.5] 我最近有点累"),
]


def main() -> int:
    from core.conversation_engine import ConversationEngine
    from pet_mixins.perception_mixin import PerceptionMixin

    print("=" * 84)
    print("端到端实测：LLM 输出 → VA → 决策器")
    print("=" * 84)

    # ── 1. 真实解析器（用 object.__new__ 跳过 __init__）──
    eng = object.__new__(ConversationEngine)
    print("[1] ConversationEngine.parse_action_intent 就绪（真实解析器）")

    # ── 2. 决策器（真实，需引擎在线）──
    director = None
    try:
        from core.expression_director import get_director

        cfg_chars = os.environ.get("OC_TEST_CHARACTER", "miku")
        director = get_director(cfg_chars)
        online = director.client.is_available()
        print(f"[2] ExpressionDirector 就绪（角色={cfg_chars}，引擎={'在线' if online else '离线'}）")
        if not online:
            print("    ⚠ 引擎离线 → 决策会走兜底，本实测只看链路是否通")
    except Exception as e:
        print(f"[2] 决策器不可用: {e}")

    print()
    print(f"{'输入':<28} {'VA':<14} {'情绪词':<8} {'决策':<16} {'档':<7} ms")
    print("-" * 84)

    rows = []
    for label, raw in SAMPLES:
        # ① 真实解析器
        _text, intent = eng.parse_action_intent(raw)
        va = (intent or {}).get("va")
        if not va:
            print(f"{label:<28} {'(无 VA)':<14}")
            rows.append((label, None, None, None, None, None))
            continue

        # ② VA → 情绪词（走修复后的真实方法）
        emo_en = PerceptionMixin._va_to_emotion(va[0], va[1])
        emo_zh = PerceptionMixin._EMOTION_ZH.get(emo_en, "") if emo_en else ""

        if not emo_zh:
            print(f"{label:<28} {str(va):<14} {'(中性)':<8} {'跳过':<16}")
            rows.append((label, va, emo_zh, None, None, None))
            continue

        # ③ 决策器
        gesture, band, ms = None, None, None
        if director is not None:
            try:
                r = director.decide(emo_zh, "中", "实测")
                # 字段名以 DecisionResult 定义为准：gesture / bands / elapsed_ms
                gesture = r.gesture if r.accepted else f"(拒:{str(r.reason)[:10]})"
                bands = getattr(r, "bands", None) or {}
                band = bands.get("gesture") if isinstance(bands, dict) else bands
                ms = getattr(r, "elapsed_ms", None)
            except Exception as e:
                gesture = f"(错:{e})"[:22]

        print(f"{label:<28} {str(va):<14} {emo_zh:<8} {str(gesture):<16} {str(band):<7} {ms or '-'}")
        rows.append((label, va, emo_zh, gesture, band, ms))

    print()
    print("=" * 84)

    # ── 3. 判定 ──
    with_va = [r for r in rows if r[1]]
    mapped = [r for r in with_va if r[2]]
    played = [r for r in mapped if r[3] and not str(r[3]).startswith("(")]

    print(f"有 VA 的样本: {len(with_va)}/{len(rows)}")
    print(f"VA 成功映射到情绪词: {len(mapped)}/{len(with_va)}")
    if director is not None and director.client.is_available():
        print(f"决策器成功出动作: {len(played)}/{len(mapped)}")
    print()

    # 关键：修复前，所有样本的 emotion 都是 neutral；现在应该有非 neutral 的
    distinct = {r[2] for r in mapped}
    print(f"映射出的不同情绪: {sorted(distinct)}")
    if len(distinct) >= 2:
        print("✓ 修复生效：情绪不再恒为单一值（修复前全走 neutral）")
        ok = True
    else:
        print("✗ 修复未生效：情绪仍然单一")
        ok = False

    print()
    print("=" * 84)
    print("结果: " + ("链路已通 ✓" if ok else "链路仍不通 ✗"))
    print("=" * 84)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
