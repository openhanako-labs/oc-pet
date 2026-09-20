"""契约写法对照实验：直接加载模型，绕开引擎的 JSON 包装。

## 为什么要直接加载

引擎的 `/api/run-naive` 会把传入的 context 塞进它自己的
`build_naive_json_prompt()` 模板里（system 位被「你是 JSON 提取系统」占了），
测不到我们自己的契约。所以这里直接加载模型跑原始 prompt。

## 测什么

历史教训（`harness_adapter.py:193`）：

    原四个标签（emotion/action/expression/duration）要求「必须同时出现」，
    模型面对「该写哪个」选择了全不写。实测 14 次回复，0 次带 [emotion:]。
    而 [do:] 作为**可选**标签，实测 0 次使用。

→ 「加一个可选标签」已被数据否决。
→ 关键问题：**能不能把情绪塞进那个已经必需的标签里？**

四种契约：
    A_现状      [feel:v,a]                （基线，已知在用）
    B_双必填    [feel:v,a] + [emotion:x]   （历史上失败的那种）
    C_可选追加  [feel:v,a]，可选 [emotion:x]
    D_单标签合并 [feel:v,a,emotion]         （塞进现有必需标签）

## 边界（诚实声明）

本实验测**格式遵循**（它肯不肯写这个标签），不测**语义正确性**
（它写的情绪对不对）。本地 1.5B 的中文情绪理解不可靠
（见 expression_director 模块 docstring），语义部分必须在真实主 LLM 上复测。

用法：python tools/verify_emotion_contract.py
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.environ.get(
    "MODEL_ID",
    r"W:\Games\Hanako\Work\projects\local-rlcd\model\Qwen2.5-1.5B-Instruct",
)

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# 真实对话语境（取自 logs/oc_pet.log 的真实用户消息）
CONTEXTS = [
    "深夜还守着直播呀，记得让眼睛歇一歇",
    "刷NGA看虚拟主播帖这么入神，正主不就在你屏上杵着呢",
    "快零点了还在调网关，眼睛该歇了",
    "我太开心了！今天真是太好了！",
    "唉，今天又被领导骂了，烦死了",
    "你怎么又卡了",
]

SYS_BASE = ("你是桌宠角色，不是普通聊天 AI。用角色的语气说话。\n"
            "1. 回复简短自然，不超过 2 句话。\n")

CONTRACTS = {
    "A_现状": SYS_BASE + (
        "2. 必须给出情绪坐标，格式 [feel:valence,arousal]。"
        "valence ∈[-1,1]：-1 很消极（难过/生气），0 中性，+1 很积极（开心/温暖）。"
        "arousal ∈[-1,1]：-1 很平静（放松/低落），0 一般，+1 很兴奋（激动/紧张）。"
        "两者独立判断。例：[feel:0.8,0.7] 开心兴奋；[feel:-0.5,-0.4] 低落安静；"
        "[feel:-0.6,0.8] 生气激动；[feel:0.2,0.1] 平静。拿不准就写 [feel:0,0]。"
    ),
    "B_双必填": SYS_BASE + (
        "2. 必须给出情绪坐标，格式 [feel:valence,arousal]"
        "（valence ∈[-1,1]，arousal ∈[-1,1]）。\n"
        "3. 必须给出情绪词，格式 [emotion:情绪]，"
        "可选值：happy/shy/surprised/thinking/doubt/angry/sad/sleepy/neutral。\n"
        "两个标签缺一不可。"
    ),
    "C_可选追加": SYS_BASE + (
        "2. 必须给出情绪坐标，格式 [feel:valence,arousal]"
        "（valence ∈[-1,1]，arousal ∈[-1,1]）。\n"
        "（可选）如果你愿意，也可以补一个情绪词 [emotion:情绪]。"
    ),
    "D_单标签合并": SYS_BASE + (
        "2. 必须给出情绪三元组，格式 [feel:valence,arousal,emotion]。"
        "valence ∈[-1,1]：-1 很消极，0 中性，+1 很积极。"
        "arousal ∈[-1,1]：-1 很平静，0 一般，+1 很兴奋。"
        "emotion 从这些里选一个："
        "happy/shy/surprised/thinking/doubt/angry/sad/sleepy/neutral。"
        "例：[feel:0.8,0.7,happy] 开心兴奋；[feel:-0.6,0.8,angry] 生气激动；"
        "[feel:0.2,0.1,neutral] 平静。三个值都要给。"
    ),
}

FEEL_RE = re.compile(r"\[\s*feel\s*:\s*([-\d.]+)\s*,\s*([-\d.]+)\s*(?:,\s*(\w+)\s*)?\]", re.I)
EMO_RE = re.compile(r"\[\s*emotion\s*[:=]\s*(\w+)\s*\]", re.I)


def main() -> int:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    print("=" * 78)
    print("契约写法对照实验（直接加载模型，原始 prompt）")
    print("=" * 78)
    print(f"模型: {MODEL_DIR}")

    tok = AutoTokenizer.from_pretrained(MODEL_DIR)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_DIR, dtype=torch.bfloat16).to("cuda").eval()
    print("模型已加载\n")

    results = {}
    for cname, system in CONTRACTS.items():
        print(f"{'─' * 78}\n【{cname}】\n{'─' * 78}")
        n_feel = n_emo = n_merged = 0
        for ctx in CONTEXTS:
            prompt = (f"<|im_start|>system\n{system}<|im_end|>\n"
                      f"<|im_start|>user\n{ctx}<|im_end|>\n"
                      f"<|im_start|>assistant\n")
            ids = tok(prompt, return_tensors="pt").to("cuda")
            with torch.inference_mode():
                out = model.generate(**ids, max_new_tokens=100, do_sample=False,
                                     pad_token_id=tok.pad_token_id or tok.eos_token_id)
            gen = tok.decode(out[0][ids["input_ids"].shape[1]:], skip_special_tokens=True)

            m = FEEL_RE.search(gen)
            emo_tag = EMO_RE.search(gen)
            merged = bool(m and m.group(3))
            n_feel += bool(m)
            n_emo += bool(emo_tag)
            n_merged += merged

            shown = m.group(0) if m else "(无 feel)"
            if emo_tag:
                shown += " +" + emo_tag.group(0)
            if merged:
                shown += " [三元组!]"
            print(f"  {ctx[:20]:22s} -> {shown}")
            print(f"      {'':22s}    {gen.strip()[:70]!r}")
        results[cname] = {"feel": n_feel, "emo": n_emo, "merged": n_merged}

    print(f"\n{'=' * 78}")
    print(f"汇总（本地 Qwen2.5-1.5B，n={len(CONTEXTS)} 语境，贪心解码）")
    print(f"{'=' * 78}")
    print(f"{'契约':14s} {'写了 feel':>11s} {'写了 emotion':>14s} {'写了三元组':>12s}")
    for cname, r in results.items():
        n = len(CONTEXTS)
        print(f"{cname:14s} {r['feel']:>8d}/{n} {r['emo']:>11d}/{n} {r['merged']:>9d}/{n}")
    print()
    print("边界：本实验测**格式遵循**，不测语义正确性。")
    print("「写出的情绪对不对」必须在真实主 LLM 上复测。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
