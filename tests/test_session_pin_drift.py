# -*- coding: utf-8 -*-
"""会话 pin 漂移回归测试（2026-09-20 真机事故）。

## 事故经过（三重独立判据，非字符串推断）

`~/.hanako/pets/session_ophelia.json` 里 pin 的是 `sess_0mu9hqd0r`。
`/api/sessions` 显示它的 path **就是助手主对话的 jsonl**（标题
「MCP连接测试讨论」，首条消息是用户问「继续吧,你可以做完整测试吧」）。

机制：

    ensure_session(agent_id="ophelia", preferred_session_id=<已删会话>)
      → 遍历没找到
      → if sessions: sessions.sort(by modified desc); return sessions[0]
      → 对 ophelia 而言「最近修改的」= 助手主对话

于是桌宠 pin 到主对话 → 待机自言自语/输出规则/身份全写进用户与助手的对话。

真机验证（`tools/diag_ensure_session_drift.py`）：
    ensure_session(preferred=<不存在>) 返回 sess_0mu9hqd0r，非新建。

## 修复

1. `ensure_session`：给了 preferred 却找不到 → **新建**，不兜底。
2. pin 文件加 `owned` 归属标记；无标记的 pin 启动时丢弃。

## 这个文件为什么写这么长

项目里 `tests/test_pet_session_isolation.py` 记录了我在这同一个问题上
**栽过四次**——都是「看到字符串出现在文件里」就下结论，而那些字符串
其实是我自己诊断时写下的。教训：判据必须是**角色/来源/独立状态文件**。

这里锁住的两条不变量，都来自独立状态（pin 文件 + API），不是文本匹配。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.hanako_session_manager import (  # noqa: E402
    HanakoSessionError,
    HanakoSessionManager,
    SessionSummary,
)


def _summary(sid, modified, n=0, title=None):
    return SessionSummary(
        session_id=sid, session_path=f"/p/{sid}.jsonl", agent_id="ophelia",
        agent_name="奥菲莉娅", title=title, modified=modified,
        message_count=n, cwd=None, raw={})


class _FakeSM(HanakoSessionManager):
    """只实现 ensure_session 依赖的两个方法，避免建 WS。"""

    def __init__(self, sessions, created_id="sess_NEW"):
        self._sessions = list(sessions)
        self._created_id = created_id
        self.create_calls = []

    def list_sessions(self, agent_id=None):
        return list(self._sessions)

    def create_session(self, agent_id=None, **kw):
        self.create_calls.append(agent_id)
        return _summary(self._created_id, "2026-09-20T18:00:00Z").ref


# ══════════════════════════════════════════════════════════════
#  1. 核心：pin 找不到时不得接管「最近修改的」
# ══════════════════════════════════════════════════════════════

def test_missing_preferred_creates_new_not_take_recent():
    """★ 核心回归：preferred 不存在 → 新建，绝不能返回「最近修改的」。

    这就是事故本身：桌宠 pin 失效后接管了助手主对话。
    """
    sm = _FakeSM([
        _summary("sess_MAIN_DIALOG", "2026-09-20T18:00:00Z", 1049,
                 "MCP连接测试讨论"),
        _summary("sess_OLD", "2026-09-01T10:00:00Z", 100),
    ])
    ref = sm.ensure_session(agent_id="ophelia",
                            preferred_session_id="sess_DELETED")
    assert ref.session_id != "sess_MAIN_DIALOG", (
        "★ pin 失效时接管了「最近修改的」会话——这正是漂移事故")
    assert sm.create_calls == ["ophelia"], "应新建专属会话"


def test_missing_preferred_without_create_raises():
    """create_if_missing=False 时，找不到应报错——不得静默换会话。"""
    sm = _FakeSM([_summary("sess_MAIN", "2026-09-20T18:00:00Z", 1000)])
    with pytest.raises(HanakoSessionError):
        sm.ensure_session(agent_id="ophelia",
                          preferred_session_id="sess_GONE",
                          create_if_missing=False)


def test_preferred_found_is_returned():
    """找得到就用它（这是 pin 的正常路径，不能被修坏）。"""
    sm = _FakeSM([
        _summary("sess_MINE", "2026-09-20T10:00:00Z", 5),
        _summary("sess_MAIN", "2026-09-20T18:00:00Z", 1049),
    ])
    ref = sm.ensure_session(agent_id="ophelia",
                            preferred_session_id="sess_MINE")
    assert ref.session_id == "sess_MINE"
    assert sm.create_calls == [], "找得到就不该新建"


def test_no_preferred_still_allows_recent():
    """**未点名**时复用最近的会话是合法语义——别把这个也改坏。"""
    sm = _FakeSM([
        _summary("sess_A", "2026-09-20T10:00:00Z", 5),
        _summary("sess_B", "2026-09-20T18:00:00Z", 9),
    ])
    ref = sm.ensure_session(agent_id="ophelia")
    assert ref.session_id == "sess_B", "未点名时应取最近修改的"
    assert sm.create_calls == []


# ══════════════════════════════════════════════════════════════
#  2. 归属标记：无标记的 pin 必须被丢弃
# ══════════════════════════════════════════════════════════════

def _adapter(tmp_path, monkeypatch, pinned=None, owned=None):
    from core.harness_adapter import HanakoPetAdapter

    a = HanakoPetAdapter.__new__(HanakoPetAdapter)
    a.agent_id = "ophelia"
    a._current_session = None
    a._agent_sessions = {}
    a._agent_pinned = dict(pinned or {})
    a._owned_sessions = dict(owned or {})
    a._pinned_session_id = None
    # 把落盘路径指到 tmp
    monkeypatch.setattr(a, "_pinned_path",
                        lambda: tmp_path / "session_ophelia.json")
    return a


def test_unowned_pin_is_discarded(tmp_path, monkeypatch):
    """★ 无归属标记的 pin = 漂移残留 → 丢弃。

    旧代码写的 pin 没有 owned 字段，正好借此清掉事故残留。
    """
    a = _adapter(tmp_path, monkeypatch,
                 pinned={"ophelia": "sess_DRIFTED"}, owned={})
    a._pinned_session_id = "sess_DRIFTED"

    a._validate_pin_async()

    assert "ophelia" not in a._agent_pinned, "无标记的 pin 应被丢弃"
    assert a._pinned_session_id is None


def test_owned_pin_is_kept(tmp_path, monkeypatch):
    """有标记且指向同一会话 → 信任（不因联网失败而误删）。"""
    a = _adapter(tmp_path, monkeypatch,
                 pinned={"ophelia": "sess_MINE"},
                 owned={"ophelia": "sess_MINE"})
    a._pinned_session_id = "sess_MINE"

    a._validate_pin_async()

    assert a._agent_pinned.get("ophelia") == "sess_MINE", "合法 pin 不该被丢"


def test_owned_mismatch_is_discarded(tmp_path, monkeypatch):
    """标记与 pin 指向不同会话 → 丢弃（数据不一致，宁可新建）。"""
    a = _adapter(tmp_path, monkeypatch,
                 pinned={"ophelia": "sess_X"},
                 owned={"ophelia": "sess_Y"})
    a._validate_pin_async()
    assert "ophelia" not in a._agent_pinned


def test_discard_persists_to_disk(tmp_path, monkeypatch):
    """丢弃必须落盘——否则下次启动又读到同一条脏 pin。"""
    a = _adapter(tmp_path, monkeypatch,
                 pinned={"ophelia": "sess_DRIFTED"}, owned={})
    a._validate_pin_async()

    p = tmp_path / "session_ophelia.json"
    assert p.exists(), "丢弃后应落盘"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "ophelia" not in (data.get("pinned") or {})


# ══════════════════════════════════════════════════════════════
#  3. 归属标记的写入点
# ══════════════════════════════════════════════════════════════

def test_create_branch_marks_owned():
    """首次 create_session 时必须标记归属。"""
    src = (_REPO / "core" / "harness_adapter.py").read_text(encoding="utf-8")
    idx = src.index("elif aid in self._agent_sessions")
    window = src[idx:idx + 700]
    assert "self._owned_sessions[aid]" in window, (
        "首次新建分支必须标记 owned——否则重启后合法 pin 会被误丢")


def test_set_session_marks_owned():
    """菜单「新建对话」走 set_session，也要标记。"""
    src = (_REPO / "core" / "harness_adapter.py").read_text(encoding="utf-8")
    i = src.index("def set_session(self")
    window = src[i:i + 900]
    assert "_owned_sessions[self.agent_id]" in window


def test_pin_file_roundtrips_owned(tmp_path, monkeypatch):
    """owned 要能存盘 + 读回。"""
    a = _adapter(tmp_path, monkeypatch,
                 pinned={"ophelia": "sess_MINE"},
                 owned={"ophelia": "sess_MINE"})
    a._save_pinned_sessions()

    b = _adapter(tmp_path, monkeypatch)
    b._load_pinned_sessions()
    assert b._owned_sessions.get("ophelia") == "sess_MINE"
    assert b._agent_pinned.get("ophelia") == "sess_MINE"


# ══════════════════════════════════════════════════════════════
#  4. 源码守卫：兜底分支不得复活
# ══════════════════════════════════════════════════════════════

def test_ensure_session_has_no_unconditional_fallback():
    """ensure_session 里「找不到 preferred 就返回 sessions[0]」必须消失。

    正确结构：preferred 分支里找不到 → create/raise；
    未点名时的 sessions.sort 兜底只能在 preferred 分支**之后**。
    """
    src = (_REPO / "core" / "hanako_session_manager.py").read_text(
        encoding="utf-8")
    i = src.index("def ensure_session")
    j = src.index("def send_message")
    body = src[i:j]
    # preferred 分支里必须 create/raise
    assert "return self.create_session(agent_id=agent_id)" in body
    # 关键：preferred 分支内部**不得**直接 return sessions[0]
    pref = body.index("if preferred_session_id:")
    tail = body[pref:]
    first_sort = tail.find("sessions.sort(")
    # 从 preferred 分支到 sort 之间，必须已经出现 create/raise
    between = tail[:first_sort] if first_sort != -1 else tail
    assert "create_session(agent_id=agent_id)" in between or \
        "raise HanakoSessionError" in between, (
        "preferred 分支在兜底之前必须 create/raise——否则 pin 失效会漂移")


def test_ensure_session_logs_drift():
    """丢弃/新建时要留日志——这是排查的唯一直接凭据。"""
    src = (_REPO / "core" / "hanako_session_manager.py").read_text(
        encoding="utf-8")
    i = src.index("def ensure_session")
    j = src.index("def send_message")
    assert "preferred session" in src[i:j], "缺漂移告警日志"
