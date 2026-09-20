# -*- coding: utf-8 -*-
"""资产可用性回归测试（2026-09-20 真机验证发现）。

## 为什么单独一个文件

用户反复问「参数/表情/动作 ok 了吗」。之前我给的都是**清单**——
证明「存在」，不证明「能用」。真机验证后发现有真有假：

| 资产 | 真机结果 |
|---|---|
| 7 个 motion（idle/happy/waving/angry/sad/thinking/touch） | **全部真的播了** ✓ |
| `pet_expression` 工具（比心/唱歌/脸红/…） | **全部 no-match** ✗ ← 真 bug |
| 53 个表情预设（纯参数驱动） | 走 play_emote_sequence，独立于 motion 文件 |

## 那个真 bug

`pet_expression` 收的是**表情名**（比心），却调 `_apply_expression(name)`——
后者收**情绪名**（happy/sad），内部 `_match_expression` 用情绪关键词匹配。

    _apply_expression("比心") → _match_expression("比心") → 无情绪关键词 → no-match

而工具仍回「已派发」——**静默失效**。用户看到「已派发」以为成功，
实际脸上什么都没变。

修法：新增 `set_named_expression(name)`（按名字直接 SetExpression），
`interface_mixin` 走它。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


# ── 1. 源码级：两个概念不能混 ──


def test_renderer_has_named_expression_entry():
    """渲染器必须有按**表情名**设置的入口。"""
    from avatar.live2d_renderer import Live2DRenderer
    assert hasattr(Live2DRenderer, "set_named_expression"), \
        "缺少按表情名设置的入口（pet_expression 会静默失效）"


def test_interface_uses_named_entry():
    """MCP 的 expression 动作必须走命名入口，不能直接调 `_apply_expression`。

    注意：不能只比较字符串位置——回退分支里有 `hasattr(r, "_apply_expression")`，
    它的位置在命名入口之前（那是能力探测，不是调用）。
    所以要检查的是**调用形式**：`r._apply_expression(` 不应出现在主路径。
    """
    src = open(os.path.join(_REPO, "pet_mixins", "interface_mixin.py"),
               encoding="utf-8").read()
    i = src.index('elif action == "expression":')
    body = src[i:i + 900]
    assert "set_named_expression" in body, (
        "expression 动作未走命名入口——会退化成按情绪匹配，全部 no-match")
    # 调用 `r._apply_expression(...)` 必须只在回退分支（elif hasattr 之后）
    call = "r._apply_expression("
    if call in body:
        # 回退分支必须以注释标明是旧渲染器回退
        assert "回退" in body, (
            "直接调 _apply_expression 必须是标明的回退分支，不能是主路径")


def test_two_concepts_are_distinct():
    """`_apply_expression` 收情绪名、`set_named_expression` 收表情名——签名要能区分。"""
    import inspect

    from avatar.live2d_renderer import Live2DRenderer
    sig_apply = inspect.signature(Live2DRenderer._apply_expression)
    sig_named = inspect.signature(Live2DRenderer.set_named_expression)
    assert "emotion" in sig_apply.parameters, "_apply_expression 应收情绪"
    assert "name" in sig_named.parameters, "set_named_expression 应收表情名"


# ── 2. 行为级：真模型上验证（有模型才跑）──


@pytest.fixture(scope="module")
def renderer():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QWidget

    app = QApplication.instance() or QApplication([])
    from avatar.live2d_renderer import Live2DRenderer

    holder = QWidget()
    r = Live2DRenderer(holder)
    if not r.load("miku"):
        pytest.skip("miku 模型未加载")
    return r


def test_named_expression_sets_real_expression(renderer):
    """★ 真机：命名表情应真的设上（修复前是 no-match）。"""
    names = getattr(renderer, "_expression_names", []) or []
    if not names:
        pytest.skip("模型未暴露表情名（可能未初始化）")
    target = str(names[0])
    assert renderer.set_named_expression(target) is True, (
        f"命名表情 {target!r} 设置失败——pet_expression 会静默失效")


def test_named_expression_rejects_unknown(renderer):
    """不存在的表情名应返回 False（而不是静默成功）。"""
    assert renderer.set_named_expression("这个表情不存在xyz") is False


def test_named_expression_handles_empty(renderer):
    assert renderer.set_named_expression("") is False


def test_expression_names_nonempty_for_miku(renderer):
    """miku 的 model3.json 声明了 7 个表情，运行时必须扫到。"""
    names = getattr(renderer, "_expression_names", []) or []
    if not names:
        pytest.skip("模型未初始化（离屏环境可能不支持）")
    # 水印类会被过滤，其余应可见
    assert len(names) >= 5, f"扫到的表情太少: {names}"


# ── 3. 动作可用性（真机播过，这里锁住映射表）──


def test_all_seven_motions_mapped():
    """miku 的 7 个 motion 名都要在映射表里——否则 play_anim 报未知动作。"""
    from avatar.live2d_renderer import Live2DRenderer
    kw = Live2DRenderer._ANIM_TO_MOTION_KW
    for name in ("idle", "happy", "waving", "angry", "sad", "thinking", "touch"):
        assert name in kw, f"{name} 不在动作映射表里"


def test_motion_kw_matches_actual_files():
    """映射表的关键词要能匹配模型真实文件名。"""
    from avatar.live2d_renderer import Live2DRenderer
    import json

    p = _REPO / "characters" / "miku" / "live2d" / "miku.model3.json"
    if not p.is_file():
        pytest.skip("缺 miku model3.json")
    d = json.loads(p.read_text(encoding="utf-8"))
    files = [m.get("File", "") for m in
             (d.get("FileReferences", {}).get("Motions", {}) or {}).get("", [])]
    assert files, "model3.json 未声明 Motions"

    kw = Live2DRenderer._ANIM_TO_MOTION_KW
    for name in ("idle", "happy", "waving", "angry", "sad", "thinking", "touch"):
        groups = kw[name]
        hit = any(
            all(k.lower() in f.lower() for k in g)
            for g in groups for f in files
        )
        assert hit, f"{name} 的关键词 {groups} 匹配不到任何文件 {files}"
