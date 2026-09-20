"""可行性闸门：bge-m3 + Soullink 语料，能否支撑 oc-pet 的情绪分类？

## 背景

2026-09-20 决策：移植 Soullink Emotion SDK（MIT）的 embedding 情绪分类，
替代「VA 坐标映射」（正价区全塌成 happy）与「改 LLM 契约」（测不出主 LLM）。

## 这个脚本回答三个问题

1. **用户视角**（第一人称倾诉）分类准不准？—— 与语料同分布，应最好
2. **桌宠视角**（第二人称嘱咐，如「记得让眼睛歇一歇」）准不准？—— 分布错配，怀疑较差
3. **补几条桌宠视角语料能不能修好第 2 点？** —— 决定这条路值不值得走

## 判据修正（v3）

v2 把「neutral 高相似」判为失败 —— 错的。语料里有「在吗」，
相似度 1.0 是**精确命中**，且答案就是 neutral。
本版：top1 标签正确即命中；neutral 无特殊处理。

## 用法

    python tools/verify_emotion_classifier_feasibility.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CORPUS_PATH = os.path.join(ROOT, "core", "data", "emotion_corpus.json")
CACHE_PATH = os.path.join(ROOT, "core", "data", "emotion_corpus_vecs.json")

# ── 测试组 ──
# 用户视角：第一人称倾诉，与 SDK 语料同分布
USER_VIEW = [
    ("唉，今天又被领导骂了，烦死了", "anger", "日志真实样本"),
    ("我最近有点累", "tired", "日志真实样本"),
    ("这个报错我搞了一下午都没解决", "confused", "常见场景"),
    ("在吗", "neutral", "日常寒暄"),
    ("我先去吃饭了，一会儿回来", "neutral", "告别"),
    ("哇！这个东西居然真的能跑起来！", "surprised", "意外成功"),
    ("别、别这样看着我啦……", "shy", "被盯着看"),
    ("我有点担心明天的面试", "anxiety", "担忧"),
]

# 桌宠视角：第二人称嘱咐/关心，与语料分布错配
PET_VIEW = [
    ("深夜还守着直播呀，记得让眼睛歇一歇", "concerned", "日志真实样本"),
    ("快零点了还在调网关，眼睛该歇了", "concerned", "日志真实样本"),
    ("忙了一天了，早点休息吧", "concerned", "关心"),
    ("记得按时吃饭，别又忘了", "concerned", "关心"),
    ("我太开心了！今天真是太好了！", "happy", "日志真实样本"),
    ("你怎么又卡了", "anger", "日志真实样本"),
]

# 桌宠视角补充语料（待验证的修复方案）
PET_PERSPECTIVE_CORPUS = {
    "concerned": [
        "记得让眼睛歇一歇", "快零点了，眼睛该歇了", "早点休息吧，别熬夜了",
        "记得按时吃饭", "别太累了，注意身体", "记得多喝点水",
        "你要照顾好自己", "忙完记得休息一下", "别又忘记吃饭了",
        "注意身体，别硬撑",
    ],
    "affectionate": [
        "陪着你呢", "我一直都在", "有什么想说的都可以跟我说",
        "今天也辛苦你了", "有我在呢", "想聊什么都可以",
    ],
}


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def embed_all(prov, texts, tries: int = 4):
    """嵌入一批（带重试）。siliconflow 批量大时偶发超时。"""
    out = list(prov.embed_texts(texts))
    for attempt in range(tries):
        missing = [i for i, v in enumerate(out) if not v]
        if not missing:
            break
        print(f"    重试 {attempt + 1}/{tries}：补 {len(missing)} 条")
        time.sleep(1.5)
        for i in missing:
            got = prov.embed_texts([texts[i]])[0]
            if got:
                out[i] = got
    return out


def load_corpus_vecs(prov):
    """加载或构建语料向量（缓存到磁盘，避免每次重嵌 1400 条）。"""
    data = json.load(open(CORPUS_PATH, encoding="utf-8"))
    corpus = data["corpus"]
    flat, labels = [], []
    for cat, items in corpus.items():
        for s in items:
            flat.append(s)
            labels.append(cat)

    if os.path.exists(CACHE_PATH):
        cached = json.load(open(CACHE_PATH, encoding="utf-8"))
        if cached.get("labels") == labels and len(cached.get("vecs") or []) == len(flat):
            print(f"  语料向量命中缓存（{len(flat)} 条）")
            return flat, labels, cached["vecs"]

    print(f"  嵌入 {len(flat)} 条语料（首次，会缓存）...")
    vecs = embed_all(prov, flat)
    ok = sum(1 for v in vecs if v)
    print(f"  成功 {ok}/{len(flat)}")
    if ok < len(flat):
        raise RuntimeError(f"仍有 {len(flat) - ok} 条未嵌入")
    json.dump({"labels": labels, "vecs": vecs}, open(CACHE_PATH, "w", encoding="utf-8"))
    return flat, labels, vecs


def evaluate(prov, corpus_vecs, labels, tests, title, quiet=False):
    if not quiet:
        print()
        print("=" * 90)
        print(title)
        print("=" * 90)
        print(f"{'测试句':34s} {'期望':11s} {'top1':11s} {'相似':>6s} 判定")
        print("-" * 90)
    hits = 0
    rows = []
    for text, want, note in tests:
        tv = prov.embed_texts([text])[0]
        if not tv:
            rows.append((text, want, None, 0, False))
            continue
        sims = sorted(((cosine(tv, v), lab) for v, lab in zip(corpus_vecs, labels)),
                      reverse=True)
        sim, lab = sims[0]
        good = lab == want
        hits += good
        rows.append((text, want, lab, sim, good))
        if not quiet:
            mark = "✓" if good else f"✗ 期望 {want}"
            print(f"{text[:32]:34s} {want:11s} {lab:11s} {sim:6.3f} {mark}")
            if not good:
                top3 = ", ".join(f"{l}({s:.2f})" for s, l in sims[:3])
                print(f"{'':34s} top3: {top3}   [{note}]")
    if not quiet:
        print("-" * 90)
        print(f"命中 {hits}/{len(tests)} = {hits / len(tests) * 100:.0f}%")
    return hits, len(tests), rows


def main() -> int:
    from core.memory_embedding_api import default_api_embedding_provider

    prov = default_api_embedding_provider()
    print("=" * 90)
    print("可行性闸门 v3：bge-m3 + Soullink 语料（1400 条 / 14 类）")
    print("=" * 90)
    if prov is None or not prov.is_available():
        print("embedding provider 不可用")
        return 2

    flat, labels, vecs = load_corpus_vecs(prov)

    hu, ju, _ = evaluate(prov, vecs, labels, USER_VIEW,
                         "组 1：用户视角（第一人称倾诉）—— 与语料同分布")
    hp, jp, rows_pet = evaluate(prov, vecs, labels, PET_VIEW,
                                "组 2：桌宠视角（第二人称嘱咐）—— 分布错配")

    # 组 3：补桌宠视角语料后重测
    print()
    print("=" * 90)
    print("组 3：补入桌宠视角语料后，重测组 2 的样本")
    print("=" * 90)
    extra_flat, extra_labels = [], []
    for cat, items in PET_PERSPECTIVE_CORPUS.items():
        for s in items:
            extra_flat.append(s)
            extra_labels.append(cat)
    print(f"  新增 {len(extra_flat)} 条：{dict((k, len(v)) for k, v in PET_PERSPECTIVE_CORPUS.items())}")
    extra_vecs = embed_all(prov, extra_flat)
    if any(v is None for v in extra_vecs):
        print("  补充语料嵌入失败")
        return 2
    aug_vecs = list(vecs) + extra_vecs
    aug_labels = list(labels) + extra_labels
    hp2, jp2, _ = evaluate(prov, aug_vecs, aug_labels, PET_VIEW,
                           "组 3：补语料后（桌宠视角）")

    print()
    print("=" * 90)
    print("结论")
    print("=" * 90)
    print(f"  组 1 用户视角（同分布）      : {hu}/{ju} = {hu / ju * 100:.0f}%")
    print(f"  组 2 桌宠视角（错配，原语料）: {hp}/{jp} = {hp / jp * 100:.0f}%")
    print(f"  组 3 桌宠视角（补语料后）    : {hp2}/{jp2} = {hp2 / jp2 * 100:.0f}%")
    print()
    if hu / ju >= 0.75:
        print("  ✓ 用户视角可用（≥75%）——分类器对**用户说的话**有效")
    if hp2 > hp:
        print(f"  ✓ 补语料有效：{hp}/{jp} → {hp2}/{jp2}（+{hp2 - hp} 条）")
        print("    → 分布错配可修，值得移植并自建桌宠视角语料")
    elif hp / jp < 0.7:
        print("  ✗ 补语料无效，桌宠视角这条路需重新考虑")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
