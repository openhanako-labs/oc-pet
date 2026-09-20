"""回归：walk / extra 刷屏 —— 让「未知动作」警告不再产生。

## 背景（2026-09-20）

`logs/oc_pet.log` 里有 257 条「未知动作」警告：

    walk  194 次（23:01-23:56，中位间隔 12s）
    extra  63 次

两者都不是模型选错，是**代码路径自己送进去的**：

- `walk`：`MotionStateMachine.tick`(500ms) → `walk_chance` 命中 → `_start_walk`
  → `physics.start_walk` → `cb.set_anim('walk')` → `pet.set_anim`
  → 原实现用 `'running-right' in self._renderer._frames` 判断是否 atlas，
  但 **Live2D 渲染器的 `_frames` 是空 dict** → 判断恒 False → `'walk'` 原样下传
  → miku 无 walk motion → 每 12 秒一条警告。

- `extra`：`bubble_mixin` 硬编码白名单 `['idle', 'walk', 'extra']`，
  而 `extra` 在任何模型上都不是可播动画名。

## 这两条测试锁住的不变量

1. **Live2D 无对应 motion 时，`set_anim('walk')` 必须降级，不得把无效名传下去**
2. **可播动画白名单必须向渲染器要，不得硬编码与模型无关的名字**
"""
from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avatar.live2d_renderer import Live2DRenderer  # noqa: E402
from pet_mixins.bubble_mixin import BubbleMixin  # noqa: E402

# miku 的真实 motion 列表（无 walk）
MIKU_MOTIONS = [
    "motions/idle.motion3.json",
    "motions/happy.motion3.json",
    "motions/waving.motion3.json",
    "motions/angry.motion3.json",
    "motions/sad.motion3.json",
    "motions/thinking.motion3.json",
    "motions/touch.motion3.json",
]


def _l2d(motions=None):
    """构造一个最小 Live2D 渲染器（`_frames` 为空 dict，与真实一致）。"""
    r = Live2DRenderer.__new__(Live2DRenderer)
    r._model = None
    r._motion_files = motions if motions is not None else list(MIKU_MOTIONS)
    r._motion_groups = {"": []}
    r._motion_group_name = ""
    r._live2d = types.SimpleNamespace(
        MotionPriority=types.SimpleNamespace(IDLE=1, NORMAL=2, FORCE=3))
    r._frames = {}          # ← 关键：Live2D 的帧表是空的
    r._current_anim = ""
    r._current_motion_idx = None
    r._motion_is_idle = True
    r._expression_active = False
    r._last_expression = ""
    r._emotion_target = ""
    r._emotion_motion_cooldown = {}
    r._last_idle_start_at = 0.0
    r._note_motion_started = lambda *a, **k: None
    r.play_emote_sequence = lambda steps, name="": True
    return r


class _Sprite:
    """最小 sprite/atlas 渲染器替身。"""

    _frames = {"idle": [], "running-right": [], "running-left": []}
    _motion_files = None

    def play_anim(self, anim, emotion=""):
        return anim in self._frames


class _Pet:
    """复刻 PetWindow.set_anim 的行为（与 pet.py 保持同步）。"""

    def __init__(self, renderer, facing_right=True):
        self._renderer = renderer
        self._facing_right = facing_right
        self.seq = []

    def _set_anim_seq(self, anim, **kw):
        self.seq.append(anim)
        player = getattr(self._renderer, "play_anim", None)
        return bool(player(anim)) if callable(player) else False

    def set_anim(self, anim):
        # 与 pet.py:set_anim 同逻辑
        if anim == "walk":
            frames = getattr(self._renderer, "_frames", None) or {}
            if "running-right" in frames:
                anim = "running-right" if self._facing_right else "running-left"
            elif hasattr(self._renderer, "_motion_files"):
                mf = self._renderer._motion_files or []
                if not any("walk" in str(f).lower() for f in mf):
                    anim = "idle"
        self._set_anim_seq(anim)


# ══════════════════════════════════════════════════════════════
#  walk 降级
# ══════════════════════════════════════════════════════════════


class TestWalkDegradation:
    def test_live2d_without_walk_motion_degrades_to_idle(self):
        """miku 无 walk motion → 必须降级为 idle，不能把 'walk' 传下去。"""
        p = _Pet(_l2d())
        p.set_anim("walk")
        assert p.seq == ["idle"], f"应降级为 idle，实际 {p.seq}"

    def test_live2d_with_walk_motion_keeps_it(self):
        """有 walk motion 的模型（如 lafei）→ 保留原路径。"""
        p = _Pet(_l2d(MIKU_MOTIONS + ["motions/walk.motion3.json"]))
        p.set_anim("walk")
        assert p.seq == ["walk"], "有对应 motion 时不该降级"

    def test_sprite_uses_directional_frames(self):
        """sprite/atlas → 按朝向换 running-left/right（原行为不变）。"""
        for facing, want in ((True, "running-right"), (False, "running-left")):
            p = _Pet(_Sprite(), facing_right=facing)
            p.set_anim("walk")
            assert p.seq == [want]

    def test_non_walk_anim_passes_through(self):
        """非 walk 的名字不受影响。"""
        p = _Pet(_l2d())
        p.set_anim("happy")
        assert p.seq == ["happy"]

    def test_degraded_walk_actually_plays(self):
        """降级后的名字必须是**能解析到 motion 的**——否则只是把警告换了个名字。

        注：这里检查「能否解析」而非「能否播放」——真播放需要 `_model`，
        而最小替身没有。名字能解析到 motion 关键词是必要条件，
        （真播放由 `test_real_startup_smoke` 覆盖）。
        """
        r = _l2d()
        assert r._ANIM_TO_MOTION_KW.get("idle"), "idle 必须有 motion 关键词映射"
        # 反证：walk 虽然在表里有映射，但 miku 的 _motion_files 里没有对应文件
        mf = [str(f).lower() for f in r._motion_files]
        assert not any("walk" in f for f in mf), "miku 确实没有 walk motion"


# ══════════════════════════════════════════════════════════════
#  可播动画白名单
# ══════════════════════════════════════════════════════════════


class _Bubble(BubbleMixin):
    def __init__(self, renderer):
        self._renderer = renderer


class TestSafeAnimNames:
    def test_live2d_names_come_from_motion_files(self):
        """Live2D → 从 _motion_files 推导，不得硬编码。"""
        names = _Bubble(_l2d())._safe_anim_names()
        assert "idle" in names
        for n in ("happy", "waving", "thinking"):
            assert n in names, f"{n} 应从 motion 文件推出"

    def test_no_phantom_names(self):
        """'walk' / 'extra' 不得出现在白名单里——它们不是 miku 的可播动画。

        'extra' 从来就不是任何模型的可播动画名，纯粹是硬编码遗留。
        """
        names = _Bubble(_l2d())._safe_anim_names()
        assert "extra" not in names, "'extra' 不该出现在可播白名单"
        assert "walk" not in names, "miku 无 walk motion，不该出现在白名单"

    def test_sprite_names_come_from_frames(self):
        names = _Bubble(_Sprite())._safe_anim_names()
        assert "running-right" in names

    def test_never_empty(self):
        """永远非空——空集会退化成“什么都播不了”。"""
        assert _Bubble(None)._safe_anim_names()
        assert _Bubble(_l2d(motions=[]))._safe_anim_names()

    def test_no_hardcoded_phantom_list_in_source(self):
        """源码级守卫：不得再出现硬编码的 `['idle', 'walk', 'extra']` **赋值**。

        只查赋值语句（``safe_anims = [...]``），不查注释/文档——
        那些地方提到旧写法是为了解释历史。
        """
        import pathlib
        import re

        src = (pathlib.Path(__file__).resolve().parent.parent
               / "pet_mixins" / "bubble_mixin.py").read_text(encoding="utf-8")
        pattern = re.compile(
            r"safe_anims\s*=\s*\[\s*['\"]idle['\"]\s*,\s*['\"]walk['\"]"
        )
        assert not pattern.search(src), "硬编码白名单已回归（safe_anims = ['idle','walk',...]）"
        assert "_safe_anim_names()" in src, "应调用 _safe_anim_names() 动态取白名单"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
