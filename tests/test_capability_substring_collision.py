# -*- coding: utf-8 -*-
"""回归测试：静态能力路由（CapabilityRouter，仅内部能力）的中文子串碰撞防护。

注意：音乐控制 / 手机控制等**插件能力**已不再由 CapabilityRouter 镜像，改由
unified_tool_router 从 tool_registry 动态读取 triggers（见
test_unified_router_dynamic_triggers.py）。本文件只验证 oc-pet 内部能力：

  - 子串碰撞防护：关键词被中文字符前后夹住时视为嵌在大词里的误匹配，放过给 LLM。
    （实证："接管一下一个对话" 中的 "一下一个" ⊃ "下一个"）
  - 不再复活插件镜像能力：裸歧义词（下一个/暂停/截图/继续）在静态层一律 None。
  - 内部能力（日报/截图）明确组合词仍正常命中，不回归。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.capability_registry import CapabilityRouter, _is_valid_capability_match


def _router():
    # route() 仅做匹配；无 tool_registry/executor 时命中返回"工具系统未就绪"
    return CapabilityRouter()


# ---- 子串碰撞防护（中间被中文夹住）----

def test_collision_takeover_dialog_not_hijacked():
    """"接管一下一个对话(音频播放器那个" 不应被任何静态能力劫持 → None。"""
    out = _router().route("请接管一下一个对话(音频播放器那个")
    assert out is None


def test_sandwiched_false_positive_rejected_general():
    """通用性：被中文字符夹住的关键词视为误匹配（"看一下一个视频"不命中）。"""
    assert _is_valid_capability_match("看一下一个视频", "下一个") is False


# ---- 裸歧义词不再被静态层劫持（移交动态路由 / LLM）----

def test_bare_next_not_static():
    """"下一个" 在静态层不再是能力 → None（交给动态路由/LLM）。"""
    assert _router().route("下一个") is None


def test_bare_pause_not_static():
    """"暂停一下手头活" 不命中静态能力 → None。"""
    assert _router().route("暂停一下手头活") is None


def test_bare_screenshot_not_static():
    """"截图工具在哪" 不命中静态能力 → None。"""
    assert _router().route("截图工具在哪") is None


def test_bare_resume_not_static():
    """"继续聊刚才的话题" 不命中静态能力 → None。"""
    assert _router().route("继续聊刚才的话题") is None


# ---- 内部能力明确组合词仍正常命中（不回归）----

def test_screenshot_explicit_still_matches():
    """"截个图" 命中内部能力 screenshot_now。"""
    assert _router().route("截个图").capability == "screenshot_now"


def test_daily_report_still_matches():
    """"今天的日报" 命中 daily_diary。"""
    out = _router().route("今天的日报")
    assert out is not None
    assert "daily" in out.capability


def test_valid_match_helper_boundaries():
    """_is_valid_capability_match：仅当两端皆为中文才拒绝。"""
    assert _is_valid_capability_match("接管一下一个对话", "下一个") is False  # 两端中文
    assert _is_valid_capability_match("下一首", "下一首") is True            # 独立
    assert _is_valid_capability_match("切到下一首", "下一首") is True         # 尾端边界
    assert _is_valid_capability_match("我想听下一首", "下一首") is True      # 尾端边界
