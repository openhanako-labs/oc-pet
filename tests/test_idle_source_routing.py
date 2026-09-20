# -*- coding: utf-8 -*-
"""idle_chatter source 路由回归测试（2026-09-20）。

## 真机发现的污染

用户在桌宠的**固定会话** jsonl 里看到了 `role=user` 的消息，内容是：

```
[pet-output-rules] 1. 回复简短自然…3. 必须给出 [emotion:情绪词]…
你的身份：# 奥菲莉娅…
你在桌面待机，突然想到一些有的没的。说一句奇怪的感悟…
```

**Hana 眼里这是「用户」在说这些话**——而它本该是桌宠的内部待机指令。

## 根因：调用方漏传 source

`core/idle_chatter.py` 原实现：

```python
result = self._adapter.chat(prompt, inject_memory=True)   # ← 没传 source
```

`chat()` 的签名是 `source: str = "user"`，而它的 docstring 明写：

> - user 消息：走 Hanako session（工具、记忆、多轮）
> - 内部来源（proactive/idle/memory_extract/memory_reflect/screen_enrich）：
>   直接走 LLM API（轻量快速，不占 session、不写本地 _history）

`idle` 本来就在那份名单里——只是调用方没传，于是路由到了 `chat_via_hanako`，
待机自言自语被写进了用户会话。

## 修法

补上 `source="idle"`。

## 顺带说明：身份注入是**保留**的

`chat_direct` 走的是 utility 模型，且它的 system 里虽有 `_system_prompt`，
但 idle 场景需要明确的「你现在是待机状态」语境——身份 + 模板一起构成该语境。
这条不是 bug，不删。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.idle_chatter import IdleChatter  # noqa: E402


class _SpyAdapter:
    """记录每次 chat 调用的 source。"""

    def __init__(self):
        self.calls = []

    def chat(self, message, inject_memory=True, extra_context="",
             tools=None, source="user"):
        self.calls.append({"source": source, "message": message})
        return ("测试回复", "neutral")


def _chatter(spy):
    return IdleChatter(
        llm_adapter=spy,
        on_chatter=lambda *a: None,
        min_interval_sec=0.01,
        max_interval_sec=0.02,
        character_id="miku",
    )


# ── 1. 核心：source 必须是 idle ──


def test_idle_uses_idle_source():
    """★ 核心：待机自言自语必须带 source="idle"。

    否则 `chat()` 默认成 "user" → 走 chat_via_hanako → 污染 Hanako 会话。
    """
    spy = _SpyAdapter()
    ic = _chatter(spy)
    ic._generate(generation=0)
    assert spy.calls, "没有发起调用"
    assert spy.calls[0]["source"] == "idle", (
        f"source 应为 idle，实为 {spy.calls[0]['source']!r}——会污染 Hanako 会话")


def test_idle_source_is_in_direct_route_list():
    """`chat()` 的内部来源名单必须含 idle——否则修了也没用。"""
    src = open(os.path.join(_REPO, "core", "harness_adapter.py"),
               encoding="utf-8").read()
    i = src.index("def chat(self")
    body = src[i:i + 1600]
    assert '"idle"' in body, "chat() 的内部来源名单里没有 idle"


# ── 2. 源码级守卫：别退回不传 source ──


def test_idle_chatter_passes_source():
    src = open(os.path.join(_REPO, "core", "idle_chatter.py"),
               encoding="utf-8").read()
    assert 'source="idle"' in src, "idle_chatter 又没传 source"


def test_idle_chatter_call_site_has_source():
    """调用点本身（不是注释）要带 source。"""
    import ast

    path = os.path.join(_REPO, "core", "idle_chatter.py")
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "chat":
            kws = {k.arg for k in node.keywords}
            found.append("source" in kws)
    assert found, "没找到 adapter.chat 调用"
    assert all(found), "有 chat 调用没传 source"


# ── 3. 行为：其他内部来源也不该漏 ──


@pytest.mark.parametrize("source", ["idle", "proactive", "memory_extract",
                                    "memory_reflect", "screen_enrich"])
def test_internal_sources_route_to_direct(source):
    """这些来源都应走 chat_direct（不写 Hanako 会话）。"""
    src = open(os.path.join(_REPO, "core", "harness_adapter.py"),
               encoding="utf-8").read()
    i = src.index("def chat(self")
    body = src[i:i + 1600]
    assert f'"{source}"' in body, f"{source} 不在内部来源名单里"


# ── 4. 真实调用链（不真发消息）──


def test_generate_does_not_use_default_user_source():
    """回归：`_generate` 走完后，绝不能出现 source="user"。"""
    spy = _SpyAdapter()
    ic = _chatter(spy)
    ic.set_agent_identity("# 测试身份")
    ic._generate(generation=0)
    bad = [c for c in spy.calls if c["source"] == "user"]
    assert not bad, f"待机自言自语走了 user 路径（会进会话）: {bad}"
