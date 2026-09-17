# -*- coding: utf-8 -*-
"""回归：屏幕主动评论的关键词堆问题。

## 现象（日志 2026-09-17 13:24:09）

    LLM 回复: 桌宠 鼓励 用户 阅读 技术博客 桌面宠物 交互风格

这不是句子，是词语罗列。

## 根因

`screen.py::_check_screen_proactive` 的模板把整段指令（含 `[action:]`
标签说明与**多个示例**）发给 LLM，而 `{detail}` 填的是场景摘要
（非具体内容）——模型缺素材时把指令里的关键词复述了出来。

实测：原模板每条 **200+ 字符**，其中 2/3 是动作标签说明。

## 三处修法

1. **模板瘦身**（根因）：每条压到 ~60 字，只留一句要求；
   动作说明抽到 `_PROACTIVE_ACTION_HINT`（单一示例）
2. **触发率减半**：20% → 10%（减少缺素材场景的绝对发生数）
3. **出口校验**（兜底）：`looks_like_keyword_salad()` 拦截，
   与来源无关——任何链路产生的堆词都不上气泡

## 判据校准

用日志里 **33 条真实回复**校准：精确命中那 1 条堆词，32 条正常回复
零误伤。判据三层：无句读标点 + 含中文 + token≥4 且平均长度≤3。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── 一、模板瘦身 ────────────────────────────────────────────────────────────


def test_templates_are_short():
    """模板必须短——长模板是「模型复述指令」的主要素材来源。

    原每条 200+ 字符；现目标 < 120。
    """
    from core.perception.screen import ScreenPerception

    for i, t in enumerate(ScreenPerception._PROACTIVE_TEMPLATES, 1):
        assert len(t) < 120, f"模板{i} 仍有 {len(t)} 字，过长"


def test_action_hint_is_separate_and_single_example():
    """动作说明与模板分离，且只给一个最小示例。"""
    from core.perception.screen import ScreenPerception

    hint = ScreenPerception._PROACTIVE_ACTION_HINT
    assert hint, "动作提示不应为空"
    # 只出现一次 [action: 示例
    assert hint.count("[action:") == 1, "只应给一个示例（多个示例是复述素材）"
    # 模板本体不含动作示例
    for i, t in enumerate(ScreenPerception._PROACTIVE_TEMPLATES, 1):
        assert "[action:" not in t, f"模板{i} 不应内嵌动作示例"


def test_prompt_renders_without_keyerror():
    """模板经 .format 渲染不得抛 KeyError（JSON 大括号需转义）。"""
    from core.perception.screen import ScreenPerception

    for t in ScreenPerception._PROACTIVE_TEMPLATES:
        out = t.format(detail="测试内容")
        assert "测试内容" in out


# ── 二、关键词堆检测 ────────────────────────────────────────────────────────


def test_detects_real_salad_from_log():
    """精确命中日志里的那条。"""
    from core.hanako_monitor import looks_like_keyword_salad

    assert looks_like_keyword_salad("桌宠 鼓励 用户 阅读 技术博客 桌面宠物 交互风格")
    assert looks_like_keyword_salad("正在 思考 中 处理")


def test_normal_replies_not_flagged():
    """日志里 33 条真实回复中的正常句，一条都不该被误判。"""
    from core.hanako_monitor import looks_like_keyword_salad

    normal = [
        "慢慢来，跑通一个就是一个。",
        "嗯，你好。我在这。",
        "45个标签页配《死神来了》，死神来了都得先翻完",
        "Java Spring Boot 同时开着，你是在用代码重写天际省吗？",
        "Skyrim modding和Java Spring Boot同时开着，你是在用代码重写天际省吗？",
        "Skyrim modding 和 Java 同时开着",
        "桌宠 MCP 是 HanaAgent 的桌面插件，我这边是 Agent 会话，两套系统分开运行。",
        "看不到——pet-context 里只有时间、情绪、定时任务、手机活动这几栏",
        "在",
        "",
        "(对话被打断了)",
        "Minecraft 加初音，这组合挺妙的，慢慢看。",
    ]
    bad = [t for t in normal if looks_like_keyword_salad(t)]
    assert not bad, f"以下正常回复被误判为堆词: {bad}"


def test_pure_english_not_flagged():
    """纯英文短语不判（可能是正常技术名）。"""
    from core.hanako_monitor import looks_like_keyword_salad

    for t in ("Java Spring Boot", "VS Code", "MCP Server"):
        assert not looks_like_keyword_salad(t), f"{t!r} 不应被判为堆词"


def test_short_text_not_flagged():
    """token 不足 4 个不判（太短不足以判断）。"""
    from core.hanako_monitor import looks_like_keyword_salad

    assert not looks_like_keyword_salad("好的")
    assert not looks_like_keyword_salad("嗯 好 的")


# ── 三、出口接线 ────────────────────────────────────────────────────────────


def test_bubble_exit_checks_salad():
    """气泡出口必须调用堆词检测（兜底，与来源无关）。"""
    import inspect

    from pet import PetWindow

    src = inspect.getsource(PetWindow._do_engine_reply_inner)
    assert "looks_like_keyword_salad" in src, "出口未接堆词检测"


def test_trigger_rate_reduced():
    """触发率已从 20% 降到 10%。"""
    import inspect

    from core.perception import screen

    src = inspect.getsource(screen.ScreenPerception._check_screen_proactive)
    assert "0.10" in src or "0.1" in src, "触发率应已降低"
