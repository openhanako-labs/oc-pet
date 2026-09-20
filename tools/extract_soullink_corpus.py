"""从 Soullink Emotion SDK（MIT）提取中文情绪语料为 JSON。

## 为什么

2026-09-20 决策：移植 Soullink 的 embedding 情绪分类思路到 oc-pet，
替代「VA 坐标映射」（实测正价区全塌成 happy）和「改 LLM 契约」（测不出主 LLM）。

语料是**数据**，MIT 协议下可搬运（需保留归属声明）。
本脚本把它从 TypeScript 源文件转成 Python 可读的 JSON。

## 归属

- 来源：https://github.com/nanlingyin/soullink-emotion-sdk
- 包：`@soullink-emotion/classifier-embedding`
- 协议：MIT
- 文件：`packages/classifier-embedding/src/defaultCorpus.ts`

## 用法

    python tools/extract_soullink_corpus.py
    # 输出 core/data/emotion_corpus.json
"""
from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.environ.get(
    "SOULLINK_CORPUS_TS",
    r"W:\Games\Hanako\Work\_ref\soullink-emotion-sdk"
    r"\packages\classifier-embedding\src\defaultCorpus.ts",
)
OUT = os.path.join(ROOT, "core", "emotion_data", "emotion_corpus.json")
SRC_DEFAULTS = os.environ.get(
    "SOULLINK_DEFAULTS_TS",
    r"W:\Games\Hanako\Work\_ref\soullink-emotion-sdk"
    r"\packages\classifier-embedding\src\defaultExamples.ts",
)


def parse_corpus(ts: str) -> dict[str, list[str]]:
    """解析 TS 源里的 DEFAULT_EMOTION_CORPUS 对象。

    结构形如：

        export const DEFAULT_EMOTION_CORPUS = {
          neutral: [
            "你好", "哈喽", ...
          ],
          calm: [ ... ],
        };
    """
    # 截取对象体（结尾是 `} as const satisfies ...`，故不能用 `\s*$`）
    m = re.search(r"DEFAULT_EMOTION_CORPUS\s*=\s*\{(.*?)\n\}\s*(?:as\s+const|;)",
                  ts, re.S)
    if not m:
        raise ValueError("找不到 DEFAULT_EMOTION_CORPUS")
    body = m.group(1)

    # 逐个类别：`name: [ ... ],`
    out: dict[str, list[str]] = {}
    for cm in re.finditer(r"(\w+)\s*:\s*\[(.*?)\]", body, re.S):
        cat = cm.group(1)
        items = re.findall(r'"((?:[^"\\]|\\.)*)"', cm.group(2))
        # 反转义常见序列
        cleaned = []
        for it in items:
            s = it.replace('\\"', '"').replace("\\\\", "\\").strip()
            if s:
                cleaned.append(s)
        if cleaned:
            out[cat] = cleaned
    return out


def parse_defaults(ts: str) -> dict:
    """解析 defaultExamples.ts 里的 EMOTION_EXAMPLE_DEFAULTS。

    结构：
        neutral: {
          variants: ["neutral_ack", "attentive"],
          intensity: [0.22, 0.4],
          contextTags: ["normal_chat"]
        },
    """
    m = re.search(r"EMOTION_EXAMPLE_DEFAULTS[^=]*=\s*\{(.*?)\n\};\s*$", ts, re.S | re.M)
    if not m:
        return {}
    body = m.group(1)
    out = {}
    # 逐块：`name: { ... }`
    for bm in re.finditer(r"(\w+)\s*:\s*\{(.*?)\n\s*\}", body, re.S):
        name, blk = bm.group(1), bm.group(2)
        variants = re.findall(r'"([^"]+)"', (re.search(r"variants:\s*\[(.*?)\]", blk, re.S) or [None, ""])[1])
        inten = re.search(r"intensity:\s*\[\s*([\d.]+)\s*,\s*([\d.]+)\s*\]", blk)
        tags = re.findall(r'"([^"]+)"', (re.search(r"contextTags:\s*\[(.*?)\]", blk, re.S) or [None, ""])[1])
        out[name] = {
            "variants": variants,
            "intensity": [float(inten.group(1)), float(inten.group(2))] if inten else [0.5, 0.5],
            "context_tags": tags,
        }
    return out


def main() -> int:
    if not os.path.exists(SRC):
        print(f"源文件不存在: {SRC}")
        print("请先克隆：git clone --depth 1 "
              "https://github.com/nanlingyin/soullink-emotion-sdk.git")
        print("或用 SOULLINK_CORPUS_TS 环境变量指定路径。")
        return 2

    ts = open(SRC, encoding="utf-8").read()
    corpus = parse_corpus(ts)

    defaults = {}
    if os.path.exists(SRC_DEFAULTS):
        defaults = parse_defaults(open(SRC_DEFAULTS, encoding="utf-8").read())

    total = sum(len(v) for v in corpus.values())
    print("=" * 70)
    print("提取 Soullink 中文情绪语料")
    print("=" * 70)
    for cat, items in corpus.items():
        d = defaults.get(cat) or {}
        rng = d.get("intensity")
        rng_s = f"强度{rng[0]:.2f}-{rng[1]:.2f}" if rng else ""
        print(f"  {cat:14s} {len(items):>4d} 条  {rng_s:16s} 例：{items[0][:22]}")
    print(f"{'':14s} {'─' * 4}")
    print(f"  {'合计':14s} {total:>4d} 条 / {len(corpus)} 类")
    if defaults:
        print(f"  强度区间: {len(defaults)} 类已解析")

    payload = {
        "_source": "https://github.com/nanlingyin/soullink-emotion-sdk",
        "_package": "@soullink-emotion/classifier-embedding",
        "_license": "MIT",
        "_file": "packages/classifier-embedding/src/defaultCorpus.ts",
        "_file_defaults": "packages/classifier-embedding/src/defaultExamples.ts",
        "_extracted_by": "tools/extract_soullink_corpus.py",
        "emotions": list(corpus.keys()),
        "defaults": defaults,
        "corpus": corpus,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"\n已写入 {OUT}  ({os.path.getsize(OUT)} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
