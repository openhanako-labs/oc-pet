# -*- coding: utf-8 -*-
"""回归测试：unified_tool_router 动态读取 tool_registry 的 triggers（单一来源）。

验证治本方向：路由不再依赖手写镜像表（旧 _TOOL_KEYWORD_MAP / CapabilityRouter
插件能力），而是从 ToolDef.triggers 动态构建关键词索引——本地插件在 .js 里
export const triggers，外部 Hanako 插件由 tool_registry._EXTERNAL_TOOL_TRIGGERS
集中声明。

用确定性 fake registry（不依赖 ~/.hanako/plugins 是否在场），直接验证路由逻辑
对「动态提供 triggers」的反应。
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tool_registry import ToolDef
from core.unified_tool_router import UnifiedToolRouter


def _make_reg(tools: dict) -> SimpleNamespace:
    reg = SimpleNamespace(_tools=tools)
    return reg


def _tool(name, plugin_id, triggers):
    return ToolDef(name=name, description=f"{name} tool", parameters={},
                   plugin_id=plugin_id, source_path="", triggers=list(triggers))


def _router_for(tools: dict) -> UnifiedToolRouter:
    reg = _make_reg(tools)
    r = UnifiedToolRouter()
    r.refresh(reg)
    return r, reg


# audio_bus：外部插件，白名单内工具名；triggers 由 _EXTERNAL_TOOL_TRIGGERS 提供
_AUDIO = _tool("audio_bus", "hanako-audio-player", [
    {"text": "下一首", "args": {"action": "next"}},
    {"text": "切歌", "args": {"action": "next"}},
    {"text": "暂停播放", "args": {"action": "pause"}},
    {"text": "继续播放", "args": {"action": "resume"}},
])

# 手机控制：本地插件，白名单内 plugin_id；triggers 来自 .js 的 export const triggers
_PHONE = _tool("phone_peek_screen", "linjian-peek", [
    "手机截图", "看手机屏幕", "查看手机屏幕", "看一下手机",
])
_PHONE_STATE = _tool("phone_state", "linjian-peek", ["手机状态", "手机前台"])


def test_audio_bus_triggers_dynamic_and_carries_action():
    """"下一首"/"切歌" 经动态 triggers 命中 audio_bus，且携带 action=next。"""
    r, reg = _router_for({"audio_bus": _AUDIO})
    hit = r._match_keyword("下一首", reg)
    assert hit is not None
    assert hit[0] == "audio_bus"
    assert hit[2] == {"action": "next"}

    hit2 = r._match_keyword("切歌", reg)
    assert hit2[0] == "audio_bus"
    assert hit2[2] == {"action": "next"}


def test_audio_bus_pause_resume_dynamic():
    """"暂停播放"/"继续播放" 命中 audio_bus 并携带对应 action。"""
    r, reg = _router_for({"audio_bus": _AUDIO})
    assert r._match_keyword("暂停播放", reg)[2] == {"action": "pause"}
    assert r._match_keyword("继续播放", reg)[2] == {"action": "resume"}


def test_phone_triggers_from_local_plugin_js():
    """"手机截图" 经本地插件动态 triggers 命中 phone_peek_screen；
    "手机状态" 在含 phone_state 的 reg 中正确命中 phone_state（不被同义词"手机"误抢）。"""
    r, reg = _router_for({"phone_peek_screen": _PHONE})
    hit = r._match_keyword("手机截图", reg)
    assert hit is not None
    assert hit[0] == "phone_peek_screen"

    # 需把 phone_state 也纳入索引，验证多工具下"手机状态"命中最具体的能力
    r2, reg2 = _router_for({"phone_peek_screen": _PHONE, "phone_state": _PHONE_STATE})
    hit2 = r2._match_keyword("手机状态", reg2)
    assert hit2 is not None
    assert hit2[0] == "phone_state"


def test_no_ambiguous_bare_words_in_dynamic_triggers():
    """动态 triggers 不含歧义裸词；句首歧义句不被误命中。"""
    r, reg = _router_for({
        "audio_bus": _AUDIO,
        "phone_peek_screen": _PHONE,
        "phone_state": _PHONE_STATE,
    })
    # 句首歧义：这些应走 LLM（None），而非命中任何白名单工具
    for phrase in ["暂停一下手头活", "截图工具在哪", "继续聊刚才的话题",
                   "接管一下一个对话(音频播放器那个)"]:
        assert r._match_keyword(phrase, reg) is None


def test_unknown_plugin_not_indexed():
    """非白名单插件工具不参与关键词索引（让位 Hanako LLM）。"""
    unknown = _tool("some_weather", "hanako-weather-x", ["天气", "下雨"])
    r, reg = _router_for({"some_weather": unknown})
    # 白名单外：即便 triggers 含"天气"，也不建索引
    assert r._match_keyword("今天天气如何", reg) is None


def test_route_full_pipeline_caries_args_to_result():
    """"切歌" 经 route() 端到端命中 audio_bus（execute=False 不实际调用）。"""
    r, reg = _router_for({"audio_bus": _AUDIO})
    res = r.route("切歌", tool_registry=reg, execute=False)
    assert res is not None
    assert res.capability == "audio_bus"
    # 不执行时返回"执行中"标记，不抛错
    assert "audio_bus" in res.text
