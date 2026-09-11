"""回归锁定：桌宠用自己的会话，不复用「最近修改的那个」

## 背景（含一次我自己的误判，如实记录）

2026-09-11 排查 `[feel:]` 采纳率不稳定时，我从「桌宠回复出现在主 session 文件里」
推断出「桌宠在共享我的会话」，并据此加了一个 `_create_isolated_session`。

**该推断是错的。** 我用 API（而非文件）复核后确认：

    主 session 的 200 条消息里，含 `[pet-output-rules]` 的 4 条**全是
    role=assistant** —— 那是我自己诊断时写下的文本（我在引用它）。
    角色分布 assistant 195 / user 5，就是「我」在写。

我因为**用自己的输出当证据**，在同一个循环论证上栽了四次。
（教训：验证「A 是否在 B 里」时，不能只看字符串出现，要看**角色/来源**。）

事实：原代码在进程内首次使用时会 `create_session` —— 桌宠本来就有自己的会话。
所以那个"修复"被回退，只保留一条诊断日志。

## 现在锁住的不变量

    **桌宠首次用时必须走 create_session（自己的会话），
      不得直接 ensure_session（那会返回「最近修改的」= 主对话）。**

这条仍有价值：若将来有人把首次分支改成 ensure_session，
桌宠就会开始继承主对话历史 —— 那才是真正的污染。
"""
from __future__ import annotations

import types

import pytest

from core.harness_adapter import HanakoPetAdapter


class _FakeSession:
    def __init__(self, sid):
        self.session_id = sid
        self.session_path = f"/fake/{sid}.jsonl"


class _FakeSM:
    def __init__(self):
        self.create_calls = []
        self.ensure_calls = []

    def create_session(self, agent_id=None, **kw):
        self.create_calls.append(agent_id)
        return _FakeSession(f"own-{agent_id}")

    def ensure_session(self, agent_id=None, preferred_session_id=None,
                       create_if_missing=True):
        self.ensure_calls.append((agent_id, preferred_session_id))
        return _FakeSession("recent-main")


def _adapter(sm, pinned=None, sessions=None):
    a = HanakoPetAdapter.__new__(HanakoPetAdapter)
    a.agent_id = "ophelia"
    a._session_manager = sm
    a._current_session = None
    a._agent_sessions = dict(sessions or {})
    a._agent_pinned = dict(pinned or {})
    a._pinned_session_id = None
    return a


def _prime(a, sm):
    """执行 chat_via_hanako 里的会话准备段（不真发消息）。"""
    aid = a.agent_id
    pinned = a._agent_pinned.get(aid)
    if pinned:
        a._current_session = sm.ensure_session(
            agent_id=aid, preferred_session_id=pinned)
    elif aid in a._agent_sessions:
        a._current_session = a._agent_sessions[aid]
    else:
        a._current_session = sm.create_session(agent_id=aid)
    a._agent_sessions[aid] = a._current_session
    a._agent_pinned[aid] = getattr(a._current_session, "session_id", None)


# ══════════════════════════════════════════════════════════════
#  核心：首次必须新建
# ══════════════════════════════════════════════════════════════

def test_first_use_creates_own_session():
    sm = _FakeSM()
    a = _adapter(sm)

    _prime(a, sm)

    assert sm.create_calls == ["ophelia"], "首次应 create_session 自己的会话"
    assert sm.ensure_calls == [], "首次不得 ensure（那会滑向主对话）"
    assert a._agent_pinned["ophelia"] == "own-ophelia"


def test_pinned_session_is_reused_within_process():
    """进程内已钉住 → 续聊同一个（这是 F3 的意图）。"""
    sm = _FakeSM()
    a = _adapter(sm, pinned={"ophelia": "own-ophelia"})

    _prime(a, sm)

    assert sm.ensure_calls == [("ophelia", "own-ophelia")]
    assert sm.create_calls == [], "已有钉住的就不该再新建"


def test_in_memory_session_reused():
    sm = _FakeSM()
    ref = _FakeSession("own-ophelia")
    a = _adapter(sm, sessions={"ophelia": ref})

    _prime(a, sm)

    assert a._current_session is ref
    assert sm.create_calls == []
    assert sm.ensure_calls == []


# ══════════════════════════════════════════════════════════════
#  源码级守卫
# ══════════════════════════════════════════════════════════════

def test_first_branch_uses_create_not_ensure():
    """首次分支必须 create_session —— 防止将来被改成 ensure（会继承主对话）。"""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "core" / "harness_adapter.py").read_text(encoding="utf-8")

    idx = src.index("elif aid in self._agent_sessions")
    window = src[idx:idx + 400]
    assert "sm.create_session(agent_id=aid)" in window, (
        "首次分支必须 create_session——用 ensure 会复用「最近修改的」主对话"
    )


def test_session_id_is_logged():
    """会话 id 必须进日志。

    这是排查「桌宠在哪个会话里说话」的唯一直接凭据——
    我为此查了一整轮（文件取证 + API 取证 + 被自己的输出骗了四次）。
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "core" / "harness_adapter.py").read_text(encoding="utf-8")

    assert '[session] agent=%s session=%s' in src


def test_no_leftover_isolated_session_helper():
    """那次误判引入的 helper 已回退；防止有人把它当"特性"再引回来。"""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "core" / "harness_adapter.py").read_text(encoding="utf-8")

    assert "_create_isolated_session" not in src, (
        "该 helper 基于一个错误前提（桌宠共享主会话）被引入，已回退"
    )
