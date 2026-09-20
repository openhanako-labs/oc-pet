"""检查打包产物里的 config.json 是否泄露真实凭据。

## 背景

`dist/oc_pet/_internal/config.json` 被打进了产物（5.5KB）。
它含 `api_key` / `token` 这类**字段名**——需要确认值是空的还是真的。

这个检查必须在**分发之前**做。
"""
from __future__ import annotations

import json
import re
import sys

PACKED = "dist/oc_pet/_internal/config.json"
LOCAL = "config.json"

# 看起来像真凭据的模式
SUSPICIOUS = [
    (r"sk-[A-Za-z0-9]{16,}", "OpenAI 风格 key"),
    (r"Bearer\s+[A-Za-z0-9._-]{20,}", "Bearer token"),
    (r"[A-Fa-f0-9]{32,}", "长十六进制串（可能是 token）"),
]


def main() -> int:
    packed = json.load(open(PACKED, encoding="utf-8"))
    s = json.dumps(packed, ensure_ascii=False)

    print("=" * 70)
    print("1. 敏感字段的实际值")
    print("=" * 70)
    found_value = False
    for m in re.finditer(
            r'"([A-Za-z_]*(?:api_key|token|secret|password)[A-Za-z_]*)"\s*:\s*'
            r'("(?:[^"\\]|\\.)*"|[^,}\]\s]+)', s):
        name, val = m.group(1), m.group(2)
        val_clean = val.strip('"')
        empty = val_clean in ("", "null", "None")
        flag = "  " if empty else "★ "
        if not empty:
            found_value = True
        print(f"  {flag}{name} = {val_clean[:60]!r}")

    print()
    print("=" * 70)
    print("2. 模式扫描（sk- / Bearer / 长 hex）")
    print("=" * 70)
    hit_any = False
    for pat, desc in SUSPICIOUS:
        hits = re.findall(pat, s)
        if hits:
            hit_any = True
            print(f"  ★ {desc}: {hits[:3]}")
        else:
            print(f"  ✓ 无 {desc}")

    print()
    print("=" * 70)
    print("3. 与本机 config.json 的差异（判是否同一份）")
    print("=" * 70)
    try:
        local = json.load(open(LOCAL, encoding="utf-8"))
        same = local == packed
        print(f"  完全相同: {same}")
        if not same:
            keys = [k for k in set(local) | set(packed)
                    if local.get(k) != packed.get(k)]
            print(f"  不同键 ({len(keys)}): {sorted(keys)[:12]}")
            # 关键：本机 config 里的敏感值有没有出现在产物里
            ls = json.dumps(local, ensure_ascii=False)
            leaked = []
            for pat, desc in SUSPICIOUS:
                for h in re.findall(pat, ls):
                    if h in s:
                        leaked.append((desc, h[:20]))
            print(f"  本机凭据泄露到产物: {leaked if leaked else '无'}")
    except Exception as e:
        print(f"  读本机 config 失败: {e}")

    print()
    print("=" * 70)
    verdict = ("★ 有真实凭据值 —— 分发前必须处理"
               if (found_value or hit_any) else "✓ 未发现真实凭据（字段值为空）")
    print(f"结论: {verdict}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
