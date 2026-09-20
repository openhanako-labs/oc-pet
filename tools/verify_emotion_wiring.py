"""实测：对话主链路上 emotion 变量到底是什么？（2026-09-20）

背景：交接指出 `_direct_expression(emotion)` 挂在 `[emotion:]` 标签上，
而该标签的生产者恒为 neutral。本脚本**实际跑一遍解析链路**来验证。

不猜、不读注释——直接把真实会出现的 LLM 输出喂进解析器，看 emotion 出来是什么。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.harness_adapter import HanakoPetAdapter  # noqa: E402


# 真实 LLM 会输出的形态（来自 harness_adapter 里记录的实测样本）
SAMPLES = [
    # (说明, LLM 原始输出)
    ("带 feel + action（主链路最常见）",
     '[feel:0.8,0.6] 你今天这个反应好快啊，厉害 [action:{"gesture":"开心"}]'),
    ("只带 feel（LLM 没点动作）",
     "[feel:0.8,0.6] 你今天这个反应好快啊，厉害"),
    ("带 feel 的负面情绪",
     "[feel:-0.6,0.4] 唉，今天又被领导骂了"),
    ("带显式 emotion 标签（少见）",
     "我好开心！ [emotion:happy]"),
    ("什么都没有（纯文本）",
     "在吗"),
]


def main() -> int:
    print("=" * 78)
    print("实测：对话主链路上 emotion 变量是什么")
    print("=" * 78)

    parse_emotion = HanakoPetAdapter.parse_emotion

    print(f"{'场景':<34} {'parse_emotion 得到':<12} 判定")
    print("-" * 78)

    results = []
    for label, raw in SAMPLES:
        cleaned, emotion = parse_emotion(raw)
        results.append((label, emotion))
        print(f"{label:<34} {emotion:<12}")

    print()
    print("=" * 78)
    print("分析")
    print("=" * 78)

    # 统计：带 feel 的样本里，emotion 是否都是 neutral
    feel_samples = [(l, e) for l, e in results if "feel" in l or "action" in l]
    neutral_count = sum(1 for _, e in feel_samples if e == "neutral")

    print(f"带 [feel:]/[action:] 的样本: {len(feel_samples)} 个")
    print(f"其中 parse_emotion 返回 neutral: {neutral_count} 个")
    print()

    if neutral_count == len(feel_samples):
        print("✓ 转述成立：[feel:]/[action:] 路径下 emotion 恒为 neutral")
        print()
        print("→ 所以 pet.py:3017 的 _direct_expression(emotion, ...)")
        print("  在真实对话里永远收到 'neutral'，决策器只能选「平静」组的预设。")
        print("→ 我之前的 12/12 验证是**直接喂情绪给决策器**，绕过了这条接线。")
        verdict = True
    else:
        print("✗ 转述不成立：存在非 neutral 的情况，需进一步查")
        verdict = False

    # 反证：显式 emotion 标签确实能被解析
    print()
    print("-" * 78)
    _, e = parse_emotion("我好开心！ [emotion:happy]")
    print(f"反证：显式 [emotion:happy] → {e!r}（说明解析器本身没问题）")
    print("      问题在于**主链路不产生这个标签**。")

    print()
    print("=" * 78)
    print("结论: " + ("接线确实有误 ✓" if verdict else "需进一步查 ✗"))
    print("=" * 78)
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
