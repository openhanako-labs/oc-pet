"""验证 ensure_session 的「兜底漂移」。

## 假设

`HanakoSessionManager.ensure_session(preferred_session_id=X)` 在 X 不存在时，
会返回「该 agent 最近修改的会话」——而不是新建。

对 ophelia 来说，「最近修改的」就是**助手的主对话**。
于是桌宠 pin 到了主对话 → 记忆污染。

## 本脚本

用**不存在的** preferred_session_id 调一次，看返回的是不是主对话。
只读（ensure_session 的兜底分支不产生副作用）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOGUS = "sess_THIS_DOES_NOT_EXIST_000000000000"
MAIN = "sess_0mu9hqd0r_d6de2bfb5b08f3108d43"


def main() -> int:
    from core.hanako_session_manager import HanakoSessionManager

    # 只测 ensure_session 的纯逻辑（不建 WS）：把 list_sessions / create_session 打桩，
    # 让"会话集合"来自真实 API（这才是关键事实）。
    class _SM(HanakoSessionManager):
        def __init__(self):
            self._created = []

        def list_sessions(self, agent_id=None):
            return self._real_sessions

        def create_session(self, agent_id=None, **kw):
            self._created.append(agent_id)
            raise AssertionError("create_session 被调用（说明走了新建分支）")

    sm = _SM()

    # 用真 API 拿会话集合
    import json
    import urllib.request
    si = json.load(open(os.path.expanduser("~/.hanako/server-info.json"),
                        encoding="utf-8"))
    r = urllib.request.Request(
        f"http://127.0.0.1:{si['port']}/api/sessions",
        headers={"Authorization": "Bearer " + si["token"]})
    rows = json.loads(urllib.request.urlopen(r, timeout=15).read().decode())

    from core.hanako_session_manager import SessionSummary
    sm._real_sessions = []
    for row in rows:
        if row.get("agentId") != "ophelia":
            continue
        sm._real_sessions.append(SessionSummary(
            session_id=row["sessionId"], session_path=row["path"],
            agent_id=row.get("agentId"), agent_name=row.get("agentName"),
            title=row.get("title"), modified=row.get("modified"),
            message_count=int(row.get("messageCount") or 0),
            cwd=row.get("cwd"), raw=dict(row)))

    sessions = sm._real_sessions
    print(f"ophelia 会话数: {len(sessions)}")
    for s in sorted(sessions, key=lambda x: x.modified or "", reverse=True)[:4]:
        print(f"  {s.session_id[:34]:36} n={s.message_count:>5} "
              f"{(s.title or '')[:20]}")
    print()

    print("=== ensure_session(preferred=<不存在>) ===")
    try:
        ref = sm.ensure_session(agent_id="ophelia",
                                preferred_session_id=BOGUS)
    except AssertionError as e:
        print(f"  走了新建分支: {e}")
        return 0
    sid = getattr(ref, "session_id", None)
    print(f"  返回 sessionId: {sid}")
    print(f"  是主对话吗？  {'★ 是——漂移确认' if sid == MAIN else '否'}")
    print(f"  是新建的吗？  {'否' if any(s.session_id == sid for s in sessions) else '是'}")
    print()
    print("  含义：pin 指向已删除的会话时，桌宠会静默接管")
    print("        「该 agent 最近修改的会话」= 助手主对话。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
