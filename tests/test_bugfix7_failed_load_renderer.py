"""回归测试：load() 失败的 Live2D 渲染器（无模型文件）被 tick 调用 set_emotion 时不得崩溃。

复现场景：用户切换到占位角色（如 characters/shizuku，仅有 pet.json 无 live2d/ 目录），
load() 提前 return False，_model 为 None 且情感冷却等属性未初始化；但 _unified_tick 每帧
无条件调用 renderer.set_emotion → 旧代码在访问 self._emotion_motion_cooldown 时抛
AttributeError → 进程崩溃 → 监督器重启循环。

修复后：set_emotion 顶部 `if not self._model: return` 守卫使失败加载的渲染器成为安全 no-op。
"""
import pytest

from avatar.live2d_renderer import Live2DRenderer


def _make_failed_load_renderer():
    """模拟 load() 提前 return False 的渲染器：_model 为 None，且不初始化情感冷却属性。

    用 object.__new__ 跳过 __init__（避免 GL 控件构造），仅设置最小必要状态，
    精确复现崩溃路径——_model 存在为 None，但 _emotion_motion_cooldown 等缺失。
    """
    r = object.__new__(Live2DRenderer)
    r._model = None
    return r


def test_set_emotion_noop_on_failed_load():
    """失败加载的渲染器调用 set_emotion 不得抛 AttributeError。"""
    r = _make_failed_load_renderer()
    for emo in ("happy", "sad", "neutral", "angry", "thinking", "surprised"):
        r.set_emotion(emo)  # 必须安全返回，不抛异常
        r.set_emotion(emo, intensity=0.5)


def test_set_emotion_expression_only_noop_on_failed_load():
    """失败加载的渲染器调用 set_emotion_expression_only 也不得崩溃。"""
    r = _make_failed_load_renderer()
    r.set_emotion_expression_only("neutral")
    r.set_emotion_expression_only("happy")
