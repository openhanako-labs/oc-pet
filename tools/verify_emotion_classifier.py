"""端到端验证：情绪分类器在两个视角上的实际表现。

跑真实 embedding API，不是单测。回答的问题是：

    **分类器接进链路后，两个视角分别有多准？**

## 两个视角

- ``user``：用户第一人称倾诉，与 SDK 语料同分布
- ``pet``：桌宠第二人称嘱咐，分布错配 → 需补语料

## 用法

    python tools/verify_emotion_classifier.py
    python tools/verify_emotion_classifier.py --view pet
    python tools/verify_emotion_classifier.py --rebuild   # 强制重建向量缓存
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from emotion_test_sets import NOTE, PET_VIEW, USER_VIEW  # noqa: E402


def load_pet_corpus() -> dict[str, list[str]]:
    p = os.path.join(ROOT, "core", "emotion_data", "emotion_corpus_pet.json")
    data = json.load(open(p, encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def run_view(clf, tests, title):
    print()
    print("=" * 96)
    print(title)
    print("=" * 96)
    print(f"{'测试句':34s} {'期望':12s} {'判定':12s} {'置信':>5s} {'相似':>5s} 结果")
    print("-" * 96)
    hits = 0
    for text, want, why in tests:
        r = clf.classify(text)
        good = r.emotion == want
        hits += good
        mark = "✓" if good else f"✗ 期望{want}"
        print(f"{text[:32]:34s} {want:12s} {r.emotion:12s} "
              f"{r.confidence:5.2f} {r.similarity:5.2f} {mark}")
        if not good:
            top3 = ", ".join(f"{e}({s:.2f})" for e, s in r.scores[:3])
            print(f"{'':34s} 得分: {top3}   [{why}]")
            print(f"{'':34s} 最近: {', '.join(t[:12] for t, _l, _s in r.matched[:3])}")
    pct = hits / len(tests) * 100 if tests else 0
    print("-" * 96)
    print(f"命中 {hits}/{len(tests)} = {pct:.0f}%")
    return hits, len(tests)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--view", choices=["user", "pet", "both"], default="both")
    ap.add_argument("--rebuild", action="store_true", help="强制重建向量缓存")
    args = ap.parse_args()

    from core.emotion_classifier import get_classifier

    print("=" * 96)
    print("情绪分类器端到端验证")
    print("=" * 96)

    results = {}

    if args.view in ("user", "both"):
        clf_u = get_classifier("user")
        print(f"\n[user] 准备中...  available={clf_u.available()}")
        ok = clf_u.prepare(force=args.rebuild)
        print(f"[user] 就绪={ok}  {clf_u.status()}")
        if ok:
            results["user"] = run_view(
                clf_u, USER_VIEW,
                "视角一：用户（第一人称倾诉）—— 与 SDK 语料同分布")

    if args.view in ("pet", "both"):
        clf_p = get_classifier("pet", extra_corpus=load_pet_corpus())
        print(f"\n[pet] 准备中...  available={clf_p.available()}")
        ok = clf_p.prepare(force=args.rebuild)
        print(f"[pet] 就绪={ok}  {clf_p.status()}")
        if ok:
            results["pet"] = run_view(
                clf_p, PET_VIEW,
                "视角二：桌宠（第二人称嘱咐）—— 分布错配，已补语料")

    print()
    print("=" * 96)
    print("结论")
    print("=" * 96)
    for k, (h, n) in results.items():
        label = {"user": "用户视角", "pet": "桌宠视角"}[k]
        pct = h / n * 100
        verdict = "可用" if pct >= 75 else ("勉强" if pct >= 60 else "不足")
        print(f"  {label}: {h}/{n} = {pct:.0f}%  [{verdict}]")
    if NOTE:
        print()
        print(f"  有争议标注 {len(NOTE)} 条（已按主要意图判定，不计入扣分）：")
        for t, why in NOTE.items():
            print(f"    「{t}」—— {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
