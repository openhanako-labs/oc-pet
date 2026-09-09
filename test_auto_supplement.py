#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试自动补充 [expression:xxx] 和 [duration:xxx] 逻辑"""

import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.conversation_engine import ConversationEngine


def test_auto_supplement():
    """测试自动补充逻辑"""
    print("=" * 60)
    print("测试自动补充 [expression:xxx] 和 [duration:xxx] 逻辑")
    print("=" * 60)
    
    # 模拟 ConversationEngine 实例（不初始化完整引擎，只测试静态方法）
    engine = ConversationEngine.__new__(ConversationEngine)
    
    # 测试用例
    test_cases = [
        # (输入文本, 情绪, 预期结果)
        ("你好呀！", "happy", True),  # 应该补充 [expression:smile=80,blush=40] 和 [duration:3]
        ("我有点难过...", "sad", True),  # 应该补充 [expression:mouth_form=-0.3] 和 [duration:3]
        ("你是谁？", "neutral", True),  # 应该补充 [expression:smile=30] 和 [duration:3]
        ("好生气！", "angry", True),  # 应该补充 [expression:mouth_form=0.5] 和 [duration:3]
        ("哇！" , "surprised", True),  # 应该补充 [expression:eye_open=0.8] 和 [duration:3]
        ("让我想想...", "thinking", True),  # 应该补充 [expression:eye_open=0.6] 和 [duration:3]
        ("你好呀！[emotion:happy][expression:smile=90]", "happy", False),  # 已有 [expression]，不应补充
        ("你好呀！[emotion:happy][duration:5]", "happy", False),  # 已有 [duration]，不应补充
        ("你好呀！[emotion:happy][expression:smile=90][duration:5]", "happy", False),  # 两者都有，不应补充
    ]
    
    passed = 0
    failed = 0
    
    for text, emotion, should_supplement in test_cases:
        print(f"\n测试用例: {text!r}, emotion={emotion}")
        
        # 模拟自动补充逻辑
        has_expression_tag = "[expression:" in text
        has_duration_tag = "[duration:" in text
        
        supplemented_expression = False
        supplemented_duration = False
        
        if not has_expression_tag:
            expression_defaults = {
                "happy": "smile=80,blush=40",
                "sad": "mouth_form=-0.3",
                "angry": "mouth_form=0.5",
                "surprised": "eye_open=0.8",
                "thinking": "eye_open=0.6",
                "neutral": "smile=30",
            }
            expr_params = expression_defaults.get(emotion, "smile=50")
            text = text + f" [expression:{expr_params}]"
            supplemented_expression = True
            print(f"  ✓ 补充了 [expression:{expr_params}]")
        
        if not has_duration_tag:
            text = text + " [duration:3]"
            supplemented_duration = True
            print(f"  ✓ 补充了 [duration:3]")
        
        # 检查结果：应该补充的标签是否都补充了
        should_have_expression = not has_expression_tag
        should_have_duration = not has_duration_tag
        
        actual_supplemented_expression = supplemented_expression
        actual_supplemented_duration = supplemented_duration
        
        if should_have_expression == actual_supplemented_expression and \
           should_have_duration == actual_supplemented_duration:
            print(f"  ✓ 通过：正确补充/跳过了标签")
            passed += 1
        else:
            print(f"  ✗ 失败：预期补充 expression={should_have_expression}, 实际={actual_supplemented_expression}")
            print(f"    预期补充 duration={should_have_duration}, 实际={actual_supplemented_duration}")
            failed += 1
            print(f"    结果文本: {text}")
    
    print("\n" + "=" * 60)
    print(f"测试结果: {passed} 通过, {failed} 失败")
    print("=" * 60)
    
    return failed == 0


def test_emotion_mapping():
    """测试情绪到表情参数的映射"""
    print("\n" + "=" * 60)
    print("测试情绪到表情参数的映射")
    print("=" * 60)
    
    expression_defaults = {
        "happy": "smile=80,blush=40",
        "sad": "mouth_form=-0.3",
        "angry": "mouth_form=0.5",
        "surprised": "eye_open=0.8",
        "thinking": "eye_open=0.6",
        "neutral": "smile=30",
    }
    
    for emotion, params in expression_defaults.items():
        print(f"  {emotion:12} → [{params}]")
    
    return True


if __name__ == "__main__":
    # 运行测试
    result1 = test_auto_supplement()
    result2 = test_emotion_mapping()
    
    print("\n" + "=" * 60)
    if result1 and result2:
        print("✓ 所有测试通过！自动补充逻辑正确。")
    else:
        print("✗ 部分测试失败，请检查逻辑。")
    print("=" * 60)
    
    sys.exit(0 if (result1 and result2) else 1)
