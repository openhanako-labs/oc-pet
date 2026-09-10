# -*- coding: utf-8 -*-
"""BugFix #7: 屏幕主动搭话 / 定时主动生成 升级为结构化 [action:{...}] 动态动作意图。

回归点：
- 屏幕感知 _PROACTIVE_TEMPLATES 经 .format(detail=...) 渲染不得抛 KeyError
  （模板内 JSON 大括号已转义为 {{ }}）。
- 模板产出 [action:{...}] 指令；对话引擎 parse_action_intent 能解析并剥离标签。
- proactive_generation.build_proactive_prompt 含 [action:] 说明。
- proactive_generation.clean_generated 剥离 [action:...] 标签，不泄漏到气泡。
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.perception import screen as screen_mod
from core.perception.proactive_generation import (
    build_proactive_prompt,
    clean_generated,
)


class TestScreenProactiveActionIntent(unittest.TestCase):
    def test_templates_render_via_format_no_keyerror(self):
        """模板用 .format(detail=...) 渲染，含 [action:{...}] 但不应崩。"""
        detail = "用户在 VS Code 写 Python，屏幕有很多终端窗口"
        for i, tpl in enumerate(screen_mod.ScreenPerception._PROACTIVE_TEMPLATES):
            rendered = tpl.format(detail=detail)
            self.assertIn("[action:", rendered, f"tpl{i} 缺少 [action:] 指令")
            self.assertNotIn("{detail}", rendered, f"tpl{i} 占位符未替换")

    def test_reply_with_action_intent_parses_and_strips(self):
        """模拟屏幕主动评论回复：单标签应被解析且从正文剥离。"""
        from core.conversation_engine import ConversationEngine

        reply = (
            '你在写代码呀，好认真 '
            '[action:{"gesture":"peek","intensity":0.6,"params":{"ParamAngleX":12}}]'
        )
        text, intent = ConversationEngine.parse_action_intent(None, reply)
        self.assertIsNotNone(intent)
        self.assertEqual(intent["gesture"], "peek")
        self.assertEqual(intent["params"]["ParamAngleX"], 12)
        self.assertNotIn("[action:", text)
        self.assertEqual(text.strip(), "你在写代码呀，好认真")


class TestProactiveGenerationActionIntent(unittest.TestCase):
    def test_build_proactive_prompt_includes_action_directive(self):
        prompt = build_proactive_prompt({
            "scenario": "late_night_work",
            "signals": {"period": "night", "category": "work", "conversation_idle_min": 12},
            "fallback_prompt": "早点睡",
        })
        self.assertIn("[action:", prompt)

    def test_clean_generated_strips_action_tag(self):
        raw = '早点休息呀 [action:{"gesture":"concern","intensity":0.5,"params":{"ParamAngleX":8}}]'
        cleaned = clean_generated(raw)
        self.assertNotIn("[action:", cleaned)
        self.assertIn("早点休息呀", cleaned)


class TestParseActionIntentRobustness(unittest.TestCase):
    def test_nested_params_single_tag(self):
        from core.conversation_engine import ConversationEngine

        reply = '认真呀 [action:{"gesture":"peek","intensity":0.6,"params":{"ParamAngleX":12,"ParamMouthOpenY":0.4}}]'
        text, intent = ConversationEngine.parse_action_intent(None, reply)
        self.assertIsNotNone(intent)
        self.assertEqual(intent["params"]["ParamAngleX"], 12)
        self.assertNotIn("[action:", text)

    def test_multiple_tags_do_not_collapse(self):
        """QA 隐患：一条回复多个 [action:] 标签时，贪婪正则会把全部吞成非法 JSON。
        配平扫描应逐个解析，取最后一个合法标签，且不静默丢弃。"""
        from core.conversation_engine import ConversationEngine

        reply = (
            '先探头 [action:{"gesture":"peek","intensity":0.6,"params":{"ParamAngleX":12}}]'
            ' 再挥手 [action:{"gesture":"wave","intensity":0.8,"params":{"ParamAngleZ":10}}]'
        )
        text, intent = ConversationEngine.parse_action_intent(None, reply)
        self.assertIsNotNone(intent, "多标签不应被吞成 None")
        self.assertEqual(intent["gesture"], "wave")
        self.assertEqual(intent["params"]["ParamAngleZ"], 10)
        self.assertNotIn("[action:", text)


if __name__ == "__main__":
    unittest.main()
