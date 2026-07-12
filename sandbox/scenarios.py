"""预设测试场景 - 自动化验证桌宠行为。

每个场景是一组 (输入, 期望) 对，沙盒运行器自动发送输入并检查输出。

场景格式：
    {
        "name": "场景名",
        "description": "描述",
        "steps": [
            {"send": "你好", "expect_emotion": "happy"},
            {"send": "再见", "expect_emotion": "sad"},
        ]
    }
"""
from __future__ import annotations

SCENARIOS: list[dict] = [
    {
        "name": "basic_chat",
        "description": "基础对话：发送多条消息验证回复和情绪",
        "steps": [
            {"send": "你好呀", "expect_emotion": "happy", "expect_contains": "你好"},
            {"send": "再见啦", "expect_emotion": "sad"},
            {"send": "你真可爱", "expect_emotion": "happy"},
            {"send": "你很笨", "expect_emotion": "angry"},
            {"send": "今天天气怎么样", "expect_emotion": "thinking"},
        ],
    },
    {
        "name": "emotion_cycle",
        "description": "情绪循环：连续触发不同情绪",
        "steps": [
            {"send": "你好", "expect_emotion": "happy"},
            {"send": "你很笨", "expect_emotion": "angry"},
            {"send": "再见", "expect_emotion": "sad"},
            {"send": "你好", "expect_emotion": "happy"},
            {"send": "无聊啊", "expect_emotion": "neutral"},
        ],
    },
    {
        "name": "script_mode",
        "description": "脚本模式：预设回复序列验证 UI 渲染",
        "script": [
            "这是一条测试消息。[emotion:happy]",
            "第二条预设回复，检查气泡更新。[emotion:thinking]",
            "第三条，验证情绪切换。[emotion:surprised]",
            "最后一条，测试完成。[emotion:neutral]",
        ],
        "steps": [
            {"send": "测试1"},
            {"send": "测试2"},
            {"send": "测试3"},
            {"send": "测试4"},
        ],
    },
    {
        "name": "rapid_fire",
        "description": "快速连发：验证消息队列不丢",
        "steps": [
            {"send": "消息1"},
            {"send": "消息2"},
            {"send": "消息3"},
            {"send": "消息4"},
            {"send": "消息5"},
        ],
    },
]


def get_scenario(name: str) -> dict | None:
    """按名称获取场景"""
    for s in SCENARIOS:
        if s["name"] == name:
            return s
    return None


def list_scenarios() -> list[str]:
    """列出所有场景名"""
    return [s["name"] for s in SCENARIOS]
