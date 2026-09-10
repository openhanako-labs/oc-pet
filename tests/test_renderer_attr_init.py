"""回归锁定：渲染器「属性读了但从未赋值」这一类 bug

2026-09-10 定位到的真因：

    MotionMixer 一直被 import、被调用（submit_motion_request / force_idle /
    get_motion_layer / is_motion_idle 四处），但 **从未实例化** ——
    全仓搜不到任何 `self._mixer = ...`。

    后果：每次带 duration 的动作意图都在 `if not self._mixer.submit(req)` 抛
    AttributeError，被上游 try 吞掉。日志里 `已提交 MotionRequest` 一次都没出现，
    而 `apply_action_intent` 的入口日志每次都打 —— 两条日志本在同一个 if 分支里。

    实测数据：历史日志中**每一次** apply_action_intent 都是 duration=3.0，
    也就是说这些动作意图全部静默死亡。桌宠能动的只有空闲时随机播的那几个。

同类：`_param_intent` 只在 _set_intent_params 里赋值，而 submit_motion_request
会先直接 .update() —— 同样 AttributeError。

本测试用 AST 扫描这一类 bug，避免再出现「用了但没赋值」的成员。
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

RENDERER = Path(__file__).resolve().parent.parent / "avatar" / "live2d_renderer.py"


# ══════════════════════════════════════════════════════════════
#  AST 扫描：实例属性不得「只读未赋值」
# ══════════════════════════════════════════════════════════════

def _scan_unassigned_instance_attrs(src: str) -> dict[str, list[int]]:
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))

    methods: set[str] = set()
    class_attrs: set[str] = set()
    for n in cls.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    class_attrs.add(t.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            class_attrs.add(n.target.id)

    inst_assigned: set[str] = set()
    for node in ast.walk(cls):
        tgts = None
        if isinstance(node, ast.Assign):
            tgts = list(node.targets)
        elif isinstance(node, ast.AugAssign):
            tgts = [node.target]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            tgts = [node.target]
        for t in tgts or []:
            if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                    and t.value.id == "self"):
                inst_assigned.add(t.attr)

    reads: dict[str, list[int]] = {}
    for node in ast.walk(cls):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == "self"):
            reads.setdefault(node.attr, []).append(node.lineno)

    known = methods | class_attrs | inst_assigned
    return {k: sorted(set(v)) for k, v in reads.items() if k not in known}


def test_no_instance_attr_read_without_assignment():
    """渲染器不得存在「读了但整个文件从未赋值」的实例属性。

    修复前这条会报出 self._mixer 与 self._param_intent —— 正是本次的根因。
    """
    missing = _scan_unassigned_instance_attrs(RENDERER.read_text(encoding="utf-8"))
    assert not missing, (
        "存在只读未赋值的实例属性（会在运行期抛 AttributeError 并被静默吞掉）：\n"
        + "\n".join(f"  self.{k} 读于 {v}" for k, v in sorted(missing.items()))
    )


def test_scanner_actually_catches_the_bug():
    """扫描器自检：对修复前的代码形态必须能报出来（否则测试是空转）。"""
    broken = '''
class R:
    def go(self):
        return self._nothing_ever_assigned
'''
    missing = _scan_unassigned_instance_attrs(broken)
    assert "_nothing_ever_assigned" in missing


# ══════════════════════════════════════════════════════════════
#  MotionMixer 必须被实例化
# ══════════════════════════════════════════════════════════════

def test_motion_mixer_is_instantiated():
    src = RENDERER.read_text(encoding="utf-8")
    assert "self._mixer = MotionMixer()" in src, "MotionMixer 必须被实例化，否则四处调用全抛 AttributeError"


def test_submit_motion_request_works_after_init():
    """行为验证：防御性初始化后，submit_motion_request 不再抛 AttributeError。"""
    from avatar.live2d_renderer import Live2DRenderer
    from avatar.motion_mixer import Layer, MotionMixer, MotionRequest

    r = Live2DRenderer.__new__(Live2DRenderer)
    r._mixer = MotionMixer()          # ← 修复点
    r._param_intent = {}              # ← 修复点
    r._model = None
    r._motion_files = []
    r._motion_groups = {}
    r._live2d = None

    req = MotionRequest(layer=Layer.DIALOG, motion_group="waving",
                        params={"smile": 80.0}, duration=3.0, name="intent")

    # 不抛异常即通过；返回 False 是因为无模型可播（合理）
    result = r.submit_motion_request(req)
    assert isinstance(result, bool)
    assert r._param_intent.get("smile") == 80.0, "params 应被并入意图目标集"


# ══════════════════════════════════════════════════════════════
#  「已触发动作」不得谎报
# ══════════════════════════════════════════════════════════════

def _bare_renderer():
    from avatar.live2d_renderer import Live2DRenderer
    from avatar.motion_mixer import MotionMixer

    r = Live2DRenderer.__new__(Live2DRenderer)
    r._model = None
    r._motion_files = []
    r._motion_groups = {}
    r._live2d = None
    r._param_intent = {}
    r._mixer = MotionMixer()          # 与真实初始化一致（仲裁保护会读它）
    r._current_anim = ""
    r._emotion_motion_cooldown = {}
    r._emotion_target = ""
    r._last_expression = ""
    r._expression_active = False
    return r


def test_trigger_gesture_returns_false_when_nothing_played():
    r = _bare_renderer()
    assert r._trigger_gesture("nope", 0.5) is False


def test_apply_action_intent_does_not_claim_success_on_failure(caplog):
    """原实现无条件打「已触发动作」，即使什么都没播——排查时最大的干扰源。"""
    r = _bare_renderer()

    with caplog.at_level(logging.INFO, logger="avatar.live2d_renderer"):
        r.apply_action_intent({"gesture": "nope", "intensity": 0.5})

    msgs = [rec.getMessage() for rec in caplog.records]
    assert not any(m.startswith("已触发动作") for m in msgs), "没播就不能说已触发"
    assert any("动作未生效" in m for m in msgs), "失败必须可见"


def test_play_anim_returns_bool():
    """play_anim 必须能区分「播了」与「没播」。"""
    r = _bare_renderer()
    assert r.play_anim("nope") is False
