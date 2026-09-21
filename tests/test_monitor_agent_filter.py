"""回归锁定：桌宠只观测**自己绑定的助手**（2026-09-21）

## 两个叠加的缺陷（实测复现）

1. **绑定键用错了对象**：`set_agent_context(self._agent_id, ...)` 传的是
   **Live2D 模型包 id**（`agents[].id`，如 `miku`），而事件按**助手 agent**
   落盘（`dialog.agent_id`，如 `ophelia`）。两个键永远不等 → 助手自己的
   主对话被整体挡在门外。

2. **归属判不出时保守放行**：旧实现的路径正则只认 `/agents/<id>/sessions/`，
   而子代理会话在 `agents/<id>/subagent-sessions/` 下 → 匹配不上 → 判成"未知"
   → 放行。于是**任意**助手的子进程活动都能驱动本桌宠（用户报告的现象）。

两条叠加后的实测行为（绑定 = `miku`）::

    助手主对话  ophelia/sessions/            → 被挡（错）
    助手子进程  ophelia/subagent-sessions/   → 放行（唯一进来的东西）
    别的助手    kurisu/subagent-sessions/    → 放行（错）

## 现在的契约（用户 2026-09-21 明确）

    需要：ophelia/sessions、ophelia/subagent-sessions
    不要：kurisu/sessions、kurisu/subagent-sessions
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.hanako_monitor import HanakoMonitor

AGENTS = r"C:\Users\Administrator\.hanako\agents"

#: 路径形态实测有三种：sessions / subagent-sessions / workflow-sessions
CASES = [
    ("助手主对话", rf"{AGENTS}\ophelia\sessions\2026-09-21T10-48-52-048Z_a.jsonl", True),
    ("助手子进程", rf"{AGENTS}\ophelia\subagent-sessions\2026-09-21T10-00-00-000Z_b.jsonl", True),
    ("助手工作流", rf"{AGENTS}\ophelia\workflow-sessions\2026-09-21T10-00-00-000Z_c.jsonl", True),
    ("别的助手主对话", rf"{AGENTS}\kurisu\sessions\2026-09-21T10-00-00-000Z_d.jsonl", False),
    ("别的助手子进程", rf"{AGENTS}\kurisu\subagent-sessions\2026-09-21T10-00-00-000Z_e.jsonl", False),
]


def _monitor(bound: str | None, session_manager=None) -> HanakoMonitor:
    m = HanakoMonitor(on_state_change=lambda *a, **k: None)
    m.set_agent_context(bound, session_manager)
    return m


def _event(session_path: str) -> dict:
    return {"type": "turn_start", "sessionId": "sess_x", "sessionPath": session_path}


@pytest.mark.parametrize("label,path,allowed", CASES)
def test_agent_filter_matrix(label, path, allowed):
    """四条边界的完整矩阵（用户逐条点名的规则）。"""
    assert _monitor("ophelia")._event_belongs_to_agent(_event(path)) is allowed, label


def test_subagent_sessions_are_recognised_not_fail_open():
    """子进程会话必须被**判出归属**，而不是靠"判不出所以放行"混进来。

    这条是本 bug 的核心：断言路径解析真的返回了 agent id。
    """
    m = _monitor("ophelia")
    path = rf"{AGENTS}\ophelia\subagent-sessions\x.jsonl"

    assert m._agent_of_event(_event(path)) == "ophelia"


def test_other_agent_subagent_is_blocked():
    """别的助手的子代理会话一律挡掉（旧实现正是从这条路漏进来的）。"""
    m = _monitor("ophelia")
    path = rf"{AGENTS}\kurisu\subagent-sessions\x.jsonl"

    assert m._agent_of_event(_event(path)) == "kurisu"
    assert m._event_belongs_to_agent(_event(path)) is False


def test_unbound_monitor_does_not_filter():
    """未绑定 agent_id → 不过滤（向后兼容，文件轮询路径也走这里）。"""
    assert _monitor(None)._event_belongs_to_agent(_event(fr"{AGENTS}\kurisu\sessions\x.jsonl"))


def test_unknown_path_fails_open():
    """真的判不出归属（没有 sessionPath）→ 保守放行，避免误滤正常事件。"""
    assert _monitor("ophelia")._event_belongs_to_agent({"type": "turn_start", "sessionId": "s1"})


def test_windows_backslash_path_is_normalised():
    """Windows 反斜杠路径必须能解析（开发机就是反斜杠）。"""
    m = _monitor("ophelia")
    assert m._agent_of_event(_event(rf"{AGENTS}\ophelia\sessions\x.jsonl")) == "ophelia"


def test_session_manager_is_used_as_fallback():
    """没有 sessionPath 时用 session_manager 的 SessionRef.agent_id 兜底。"""

    class _Ref:
        agent_id = "ophelia"

    class _SM:
        def _session_for_event(self, event):
            return _Ref()

    assert _monitor("ophelia", _SM())._event_belongs_to_agent({"type": "turn_start", "sessionId": "s1"})


def test_binding_is_case_insensitive():
    assert _monitor("Ophelia")._event_belongs_to_agent(_event(rf"{AGENTS}\ophelia\sessions\x.jsonl"))


# ══════════════════════════════════════════════════════════════
#  源码级守卫：绑定键必须来自"助手"，不是"模型包"
# ══════════════════════════════════════════════════════════════

def _pet_src() -> str:
    return (Path(__file__).resolve().parents[1] / "pet.py").read_text(encoding="utf-8")


def test_binding_uses_persona_agent_id():
    """绑定必须用 _persona_agent_id()（dialog.agent_id 优先）。

    用 self._agent_id 传模型包 id 是原 bug 的根：两者永远不等，
    助手自己的主对话被整体挡下。
    """
    src = _pet_src()
    assert "set_agent_context(\n                        self._persona_agent_id(), session_manager" in src or \
           "set_agent_context(self._persona_agent_id()" in src
    assert "set_agent_context(self._agent_id" not in src, (
        "绑定键又用回了模型包 id —— 助手主对话会被整体挡下"
    )


def test_settings_save_rebinds_monitor_agent():
    """设置面板可以改 per-pet 的助手绑定 → 保存后必须重新绑定监视归属。"""
    src = _pet_src()
    body = src[src.index("def _open_settings"):]

    assert "self._rebind_monitor_agent()" in body
