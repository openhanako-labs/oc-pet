"""端到端实测：快照候选真的进了 LLM prompt 吗？（2026-09-20）

这不是单元测试，是**实际跑一遍**看结果。目的是回答一个具体问题：

    B 方案改完之后，发给 LLM 的 system prompt 里，
    候选清单到底是手写常量还是实测快照？

单元测试只证明「函数能调通」，证明不了「它真的被用在 prompt 里」。
本脚本构造真实 HarnessAdapter，抓它实际组装出的 messages。

用法：python tools/verify_action_prompt.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def _fake_renderer():
    """最小渲染器 stub：只提供 _AI_DO_PROMPT（手写常量），用于对比。"""
    class _R:
        _AI_DO_PROMPT = (
            "开心:灿烂笑容/咧嘴大笑/咯咯笑/兴奋/眼睛发亮/得意 "
            "害羞:害羞脸红/羞怯/偷瞄/嘟嘴 "
            "惊讶:惊讶吸气/瞪大眼睛/挑眉 "
            "思考:思考/抬头往上看/抿嘴 "
            "疑惑:疑惑歪头/歪头/侧目/耸肩 "
            "生气:生气瞪视/翻白眼/眯起眼睛 "
            "失落:失落垂眼/低头往下看/叹气 "
            "困:犯困/打哈欠/伸懒腰打哈欠 "
            "平静:温柔微笑/连续眨眼/点头/身体轻晃"
        )
    return _R()


def main() -> int:
    from core.harness_adapter import HanakoPetAdapter

    print("=" * 72)
    print("端到端实测：action prompt 里的候选来自哪里")
    print("=" * 72)

    # ── 1. 构造真实 adapter ──
    try:
        ad = HanakoPetAdapter(agent_id="ophelia")
    except TypeError:
        # 签名可能不同，退化为不带参数
        ad = HanakoPetAdapter()
    print(f"[1] HanakoPetAdapter 构造成功: {type(ad).__name__}")

    # 注入假渲染器（提供手写常量，用于对比）
    ad._renderer = _fake_renderer()
    print("[1] 已注入 stub 渲染器（含手写常量 _AI_DO_PROMPT）")

    # ── 2. 角色 id 解析 ──
    cid = ad._current_character_id()
    print(f"[2] _current_character_id() = {cid!r}")
    if not cid:
        print("    ⚠ 取不到角色 id → 快照路径会返回空串，回退手写常量")

    # ── 3. 快照路径 ──
    from_snap = ad._action_options_from_snapshot(ad._renderer)
    print(f"[3] 快照生成候选: {'成功' if from_snap else '空（会回退）'} "
          f"({len(from_snap)} 字符)")

    # ── 4. 实际 prompt ──
    prompt = ad._build_action_prompt()
    print(f"[4] _build_action_prompt() 输出 ({len(prompt)} 字符)")

    # ── 5. 判定来源 ──
    print()
    print("-" * 72)
    manual = _fake_renderer()._AI_DO_PROMPT
    if not prompt:
        print("判定：✗ prompt 为空 —— 候选清单根本没进去")
        return 1
    if from_snap and from_snap in prompt:
        print("判定：✓ 候选来自**实测快照**（B 生效）")
    elif manual in prompt:
        print("判定：△ 候选来自**手写常量**（快照路径未生效，走了回退）")
    else:
        print("判定：? 两者都不完全匹配，需人工看输出")

    # ── 6. 差异对比 ──
    print()
    print("实际注入的候选清单：")
    print("  " + prompt[:600])
    print()

    if from_snap:
        def _labels(line: str) -> set[str]:
            """拆出标签集：先按空格分组，每组去掉「情绪:」前缀再按 / 拆。

            第一版直接 replace(':','/') 会把情绪名当成标签
            （如「抿嘴 失落」），对比结果不可信。
            """
            out: set[str] = set()
            for group in line.split():
                if ":" in group:
                    group = group.split(":", 1)[1]
                for lb in group.split("/"):
                    lb = lb.strip()
                    if lb:
                        out.add(lb)
            return out

        snap_labels = _labels(from_snap)
        manual_labels = _labels(manual)
        only_snap = sorted(snap_labels - manual_labels)
        only_manual = sorted(manual_labels - snap_labels)
        print(f"快照标签 {len(snap_labels)} 个 / 手写常量 {len(manual_labels)} 个")
        print(f"快照独有（手写没有）: {len(only_snap)} 个")
        print("  " + "/".join(only_snap))
        if only_manual:
            print(f"手写独有（快照没有，可能已失效）: {len(only_manual)} 个")
            print("  " + "/".join(only_manual))

    # ── 7. 开关生效验证 ──
    print()
    print("-" * 72)
    ad._config = {"dialog": {"action_options_from_snapshot": False}}
    off = ad._action_options_from_snapshot(ad._renderer)
    print(f"[7] 关掉开关后快照路径返回: {off!r} (应为空串)")
    prompt_off = ad._build_action_prompt()
    uses_manual = manual in prompt_off
    print(f"    此时 prompt 用手写常量: {uses_manual}")

    ok = bool(from_snap) and (from_snap in prompt) and (off == "") and uses_manual
    print()
    print("=" * 72)
    print("结果: " + ("全部通过 ✓" if ok else "存在问题 ✗"))
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
