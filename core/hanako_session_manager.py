"""Hanako Session REST facade and streamed-turn aggregator.

The manager sits above :mod:`hanako_ws_client`.  It resolves stable Session
identities, guarantees one pending turn per Session, aggregates sequenced
stream events, exposes Future-based replies, and resumes interrupted streams
without ever resending a prompt.
"""
from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable
from urllib.parse import quote

import requests

from .hanako_ws_client import (
    ConnectionState,
    HanakoSendError,
    HanakoUnavailableBeforeSend,
    HanakoWSClient,
    HanakoWSClientError,
    StreamCursor,
    Subscription,
)

logger = logging.getLogger(__name__)


class HanakoSessionError(HanakoWSClientError):
    """Session API or turn lifecycle failure."""


class HanakoSessionBusyError(HanakoSessionError):
    """A Session already has an oc-pet turn in progress."""


class HanakoTurnError(HanakoSessionError):
    """A prompt was sent but its turn did not complete successfully."""


class TurnState(str, Enum):
    CREATED = "created"
    SENT = "sent"
    ACKED = "acked"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass(frozen=True)
class SessionRef:
    session_id: str
    session_path: str
    agent_id: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    session_path: str
    agent_id: str | None = None
    agent_name: str | None = None
    title: str | None = None
    modified: str | None = None
    message_count: int = 0
    cwd: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def ref(self) -> SessionRef:
        return SessionRef(self.session_id, self.session_path, self.agent_id, self.title)


@dataclass(frozen=True)
class HistoryPage:
    session: SessionRef
    messages: tuple[dict[str, Any], ...]
    content_blocks: tuple[dict[str, Any], ...]
    has_more: bool
    revision: str | None = None
    todos: tuple[dict[str, Any], ...] = ()
    session_files: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ReplyResult:
    session: SessionRef
    text: str
    thinking: str
    tool_calls: tuple[dict[str, Any], ...]
    content_blocks: tuple[dict[str, Any], ...]
    client_message_id: str | None
    stream_id: str | None
    origin: str
    aborted: bool = False
    error: str | None = None


@dataclass(frozen=True)
class ToolProgress:
    session: SessionRef
    tool_call_id: str | None
    tool_name: str
    phase: str
    display_text: str
    success: bool | None = None


TOOL_ACTIVITY = {
    "web_search": "正在搜索…",
    "web_fetch": "正在读取网页…",
    "browser": "正在浏览…",
    "media_generate-image": "正在生成图片…",
    "media_generate-video": "正在生成视频…",
    "read": "正在读取文件…",
    "write": "正在编辑…",
    "edit": "正在编辑…",
    "exec_command": "正在执行命令…",
}


@dataclass
class TurnAccumulator:
    session: SessionRef
    client_message_id: str | None
    origin: str
    prompt_text: str = ""
    display_text: str | None = None
    future: Future[ReplyResult] = field(default_factory=Future)
    state: TurnState = TurnState.CREATED
    text_parts: list[str] = field(default_factory=list)
    thinking_parts: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    content_blocks: list[dict[str, Any]] = field(default_factory=list)
    stream_id: str | None = None
    last_seq: int = 0
    aborted: bool = False
    error: str | None = None
    acked: bool = False
    timeout_timer: threading.Timer | None = field(default=None, repr=False)
    last_seq_by_stream: dict[str, int] = field(default_factory=dict, repr=False)
    tool_indexes: dict[str, int] = field(default_factory=dict, repr=False)

    @property
    def done(self) -> bool:
        return self.future.done()

    def accept_event(self, event: dict[str, Any]) -> bool:
        seq = event.get("seq")
        stream_id = str(event.get("streamId") or self.stream_id or "")
        if not isinstance(seq, int) or not stream_id:
            return True
        previous = self.last_seq_by_stream.get(stream_id, 0)
        if seq <= previous:
            return False
        self.last_seq_by_stream[stream_id] = seq
        self.last_seq = max(self.last_seq, seq)
        return True

    def bind_stream(self, stream_id: str | None) -> None:
        if stream_id:
            self.stream_id = stream_id

    def start_tool(self, event: dict[str, Any]) -> dict[str, Any]:
        tool_id = str(event.get("id") or event.get("toolCallId") or "")
        tool_name = str(event.get("name") or event.get("toolName") or event.get("tool") or "tool")
        item = {
            "id": tool_id or None,
            "name": tool_name,
            "args": event.get("args"),
            "phase": "start",
            "success": None,
        }
        index = len(self.tool_calls)
        self.tool_calls.append(item)
        self.tool_indexes[tool_id or f"name:{tool_name}"] = index
        return item

    def finish_tool(self, event: dict[str, Any]) -> dict[str, Any]:
        tool_id = str(event.get("id") or event.get("toolCallId") or "")
        tool_name = str(event.get("name") or event.get("toolName") or event.get("tool") or "tool")
        key = tool_id or f"name:{tool_name}"
        index = self.tool_indexes.get(key)
        success = event.get("success")
        if success is None and "isError" in event:
            success = not bool(event.get("isError"))
        if index is None:
            item = self.start_tool(event)
            index = len(self.tool_calls) - 1
        else:
            item = self.tool_calls[index]
        updated = {
            **item,
            "phase": "end",
            "success": bool(success) if success is not None else None,
            "details": event.get("details"),
        }
        self.tool_calls[index] = updated
        return updated

    def make_result(self) -> ReplyResult:
        return ReplyResult(
            session=self.session,
            text="".join(self.text_parts).strip(),
            thinking="".join(self.thinking_parts).strip(),
            tool_calls=tuple(dict(item) for item in self.tool_calls),
            content_blocks=tuple(dict(block) for block in self.content_blocks),
            client_message_id=self.client_message_id,
            stream_id=self.stream_id,
            origin=self.origin,
            aborted=self.aborted,
            error=self.error,
        )


class HanakoSessionManager:
    """Shared Session API and stream aggregation layer."""

    EVENT_TYPES = {
        "session_user_message",
        "status",
        "thinking_start",
        "thinking_delta",
        "thinking_end",
        "text_delta",
        "mood_start",
        "mood_text",
        "mood_end",
        "tool_start",
        "tool_progress",
        "tool_end",
        "content_block",
        "deferred_result",
        "stream_resume",
        "turn_end",
        "error",
        "abort_result",
        "abort_rejected",
    }

    def __init__(
        self,
        ws_client: HanakoWSClient,
        base_url: str | None = None,
        token: str = "",
        *,
        request_timeout: float = 15.0,
        reply_timeout: float = 180.0,
        mirror_external_replies: bool = True,
    ):
        self.ws_client = ws_client
        self.base_url = (base_url or ws_client.base_url).rstrip("/")
        self._token = token.strip()
        self.request_timeout = max(1.0, float(request_timeout))
        self.reply_timeout = max(1.0, float(reply_timeout))
        self.mirror_external_replies = bool(mirror_external_replies)
        self._http = requests.Session()

        self._lock = threading.RLock()
        self._pending_by_session: dict[str, TurnAccumulator] = {}
        self._pending_by_client: dict[str, TurnAccumulator] = {}
        self._pending_by_stream: dict[str, TurnAccumulator] = {}
        self._sessions_by_id: dict[str, SessionRef] = {}
        self._sessions_by_path: dict[str, SessionRef] = {}

        self._callbacks: dict[str, list[Callable[..., None]]] = {
            "progress": [],
            "tool": [],
            "reply": [],
        }
        self._event_subscription = ws_client.subscribe(
            self._handle_event,
            event_types=self.EVENT_TYPES,
        )
        self._state_subscription = ws_client.subscribe_state(self._handle_connection_state)

    def start(self) -> None:
        self.ws_client.start()

    def close(self) -> None:
        self._event_subscription.close()
        self._state_subscription.close()
        with self._lock:
            turns = self._unique_pending_locked()
        for turn in turns:
            self._finish_with_error(turn, "Session manager closed")

    def health(self) -> dict[str, Any]:
        try:
            return self._request("GET", "/api/health")
        except HanakoSessionError:
            return self._request("GET", "/health")

    def list_sessions(self, agent_id: str | None = None) -> list[SessionSummary]:
        data = self._request("GET", "/api/sessions")
        rows = data if isinstance(data, list) else data.get("sessions", [])
        summaries: list[SessionSummary] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_agent_id = row.get("agentId")
            if agent_id and row_agent_id != agent_id:
                continue
            session_id = str(row.get("sessionId") or "").strip()
            session_path = str(row.get("path") or row.get("sessionPath") or "").strip()
            if not session_id or not session_path:
                continue
            summary = SessionSummary(
                session_id=session_id,
                session_path=session_path,
                agent_id=row_agent_id,
                agent_name=row.get("agentName"),
                title=row.get("title"),
                modified=row.get("modified"),
                message_count=int(row.get("messageCount") or 0),
                cwd=row.get("cwd"),
                raw=dict(row),
            )
            summaries.append(summary)
            self._remember_session(summary.ref)
        return summaries

    def create_session(
        self,
        *,
        agent_id: str | None = None,
        cwd: str | None = None,
        memory_enabled: bool = True,
        current_session_path: str | None = None,
        workspace_folders: Iterable[str] | None = None,
        project_id: str | None = None,
        thinking_level: str | None = None,
    ) -> SessionRef:
        payload: dict[str, Any] = {"memoryEnabled": bool(memory_enabled)}
        if agent_id:
            payload["agentId"] = agent_id
            payload["currentAgentId"] = agent_id
        if cwd:
            payload["cwd"] = cwd
        if current_session_path:
            payload["currentSessionPath"] = current_session_path
        if workspace_folders:
            payload["workspaceFolders"] = list(workspace_folders)
        if project_id is not None:
            payload["projectId"] = project_id
        if thinking_level:
            payload["thinkingLevel"] = thinking_level
        data = self._request("POST", "/api/sessions/new", json=payload)
        session = self._session_from_payload(data)
        if session is None:
            raise HanakoSessionError("create_session response did not contain a stable Session identity")
        self._remember_session(session)
        return session

    def get_history(
        self,
        session: SessionRef,
        *,
        limit: int = 50,
        before: int | None = None,
    ) -> HistoryPage:
        params: dict[str, Any] = {
            "sessionId": session.session_id,
            "path": session.session_path,
            "limit": max(1, min(int(limit), 200)),
        }
        if before is not None:
            params["before"] = int(before)
        data = self._request("GET", "/api/sessions/messages", params=params)
        return HistoryPage(
            session=session,
            messages=tuple(data.get("messages") or ()),
            content_blocks=tuple(data.get("blocks") or ()),
            has_more=bool(data.get("hasMore")),
            revision=data.get("revision"),
            todos=tuple(data.get("todos") or ()),
            session_files=tuple(data.get("sessionFiles") or ()),
        )

    def ensure_session(
        self,
        *,
        agent_id: str,
        preferred_session_id: str | None = None,
        create_if_missing: bool = True,
    ) -> SessionRef:
        sessions = self.list_sessions(agent_id=agent_id)
        if preferred_session_id:
            for summary in sessions:
                if summary.session_id == preferred_session_id:
                    return summary.ref
        if sessions:
            sessions.sort(key=lambda item: item.modified or "", reverse=True)
            return sessions[0].ref
        if not create_if_missing:
            raise HanakoSessionError(f"No Session found for agent {agent_id}")
        return self.create_session(agent_id=agent_id)

    def send_message(
        self,
        session: SessionRef,
        text: str,
        *,
        display_text: str | None = None,
        ui_context: Any = None,
    ) -> Future[ReplyResult]:
        if not text or not text.strip():
            raise ValueError("text must not be empty")
        self.start()
        if not self.ws_client.is_ready and not self.ws_client.wait_until_ready(self.request_timeout):
            raise HanakoUnavailableBeforeSend("Hanako WebSocket did not become ready")

        self._remember_session(session)
        client_message_id = f"ocpet_{uuid.uuid4().hex}"
        turn = TurnAccumulator(
            session=session,
            client_message_id=client_message_id,
            origin="oc_pet",
            prompt_text=text,
            display_text=display_text,
        )
        with self._lock:
            existing = self._pending_by_session.get(session.session_id) or self._pending_by_session.get(session.session_path)
            if existing and not existing.done:
                raise HanakoSessionBusyError("This Hanako Session already has a pending turn")
            self._index_turn_locked(turn)
            turn.state = TurnState.SENT
            turn.timeout_timer = threading.Timer(self.reply_timeout, self._expire_turn, args=(turn,))
            turn.timeout_timer.daemon = True
            turn.timeout_timer.start()

        display_message = None
        if display_text is not None:
            display_message = {
                "text": display_text,
                "source": "oc-pet",
                "origin": "oc_pet",
            }
        try:
            self.ws_client.send_prompt(
                session_id=session.session_id,
                session_path=session.session_path,
                text=text,
                client_message_id=client_message_id,
                display_message=display_message,
                ui_context=ui_context,
            )
        except (HanakoUnavailableBeforeSend, HanakoSendError):
            self._discard_turn(turn)
            raise
        return turn.future

    def send_and_wait(
        self,
        session: SessionRef,
        text: str,
        *,
        timeout: float = 180.0,
        display_text: str | None = None,
        ui_context: Any = None,
    ) -> ReplyResult:
        future = self.send_message(
            session,
            text,
            display_text=display_text,
            ui_context=ui_context,
        )
        try:
            return future.result(timeout=max(1.0, float(timeout)))
        except FutureTimeoutError:
            with self._lock:
                turn = next((item for item in self._unique_pending_locked() if item.future is future), None)
            if turn is not None:
                self._finish_with_error(turn, f"Hanako reply timed out after {timeout:g}s")
            return future.result(timeout=1.0)

    def abort(self, session: SessionRef, reason: str = "user_abort") -> bool:
        with self._lock:
            turn = self._pending_by_session.get(session.session_id) or self._pending_by_session.get(session.session_path)
        if turn is None or turn.done:
            return False
        self.ws_client.abort_stream(
            StreamCursor(session.session_id, session.session_path, turn.stream_id, turn.last_seq),
            reason=reason,
        )
        return True

    def resolve_confirmation(
        self,
        confirm_id: str,
        *,
        confirmed: bool,
        value: Any = None,
    ) -> bool:
        payload: dict[str, Any] = {"action": "confirmed" if confirmed else "rejected"}
        if value is not None:
            payload["value"] = value
        try:
            data = self._request("POST", f"/api/confirm/{quote(confirm_id, safe='')}", json=payload)
            return bool(data.get("ok"))
        except HanakoSessionError:
            return False

    def on_progress(self, callback: Callable[[SessionRef, str], None]) -> Callable[[], None]:
        return self._add_callback("progress", callback)

    def on_tool(self, callback: Callable[[ToolProgress], None]) -> Callable[[], None]:
        return self._add_callback("tool", callback)

    def on_reply(self, callback: Callable[[ReplyResult], None]) -> Callable[[], None]:
        return self._add_callback("reply", callback)

    def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "stream_resume":
            self._handle_stream_resume(event)
            return

        turn = self._find_turn(event)
        if turn is None and event_type in {"session_user_message", "status"}:
            turn = self._create_external_turn(event)
        if turn is None or turn.done:
            return

        turn.bind_stream(event.get("streamId"))
        if not turn.accept_event(event):
            return
        if turn.stream_id:
            with self._lock:
                self._pending_by_stream[turn.stream_id] = turn

        if event_type == "session_user_message":
            self._handle_user_echo(turn, event)
        elif event_type == "status":
            self._handle_status(turn, event)
        elif event_type == "thinking_start":
            turn.state = TurnState.STREAMING
            self._emit("progress", turn.session, "正在思考…")
        elif event_type == "thinking_delta":
            turn.thinking_parts.append(str(event.get("delta") or event.get("text") or ""))
            turn.state = TurnState.STREAMING
            self._emit("progress", turn.session, "正在思考…")
        elif event_type == "text_delta":
            turn.text_parts.append(str(event.get("delta") or event.get("text") or ""))
            turn.state = TurnState.STREAMING
            self._emit("progress", turn.session, "正在回复…")
        elif event_type in {"tool_start", "tool_progress", "tool_end"}:
            self._handle_tool_event(turn, event)
        elif event_type == "content_block":
            block = event.get("block")
            if isinstance(block, dict):
                turn.content_blocks.append(dict(block))
        elif event_type == "deferred_result":
            turn.content_blocks.append({"type": "deferred_result", **event})
            self._emit("progress", turn.session, "正在处理异步结果…")
        elif event_type == "turn_end":
            turn.state = TurnState.ABORTED if turn.aborted else TurnState.COMPLETED
            self._complete_turn(turn)
        elif event_type == "error":
            self._finish_with_error(turn, str(event.get("message") or event.get("error") or "Hanako turn failed"))
        elif event_type == "abort_rejected":
            self._emit("progress", turn.session, "终止请求被拒绝…")

    def _handle_user_echo(self, turn: TurnAccumulator, event: dict[str, Any]) -> None:
        client_id = event.get("clientMessageId") or (event.get("message") or {}).get("clientMessageId")
        if turn.client_message_id and client_id and client_id != turn.client_message_id:
            return
        turn.acked = True
        turn.state = TurnState.ACKED

    def _handle_status(self, turn: TurnAccumulator, event: dict[str, Any]) -> None:
        if event.get("isStreaming"):
            turn.state = TurnState.STREAMING
            self._emit("progress", turn.session, "正在思考…")
            return
        if event.get("aborted"):
            turn.aborted = True
            turn.state = TurnState.ABORTED
            self._complete_turn(turn)

    def _handle_tool_event(self, turn: TurnAccumulator, event: dict[str, Any]) -> None:
        event_type = str(event.get("type"))
        if event_type == "tool_end":
            item = turn.finish_tool(event)
            phase = "end"
        elif event_type == "tool_progress":
            item = turn.start_tool(event) if not turn.tool_calls else turn.tool_calls[-1]
            phase = "progress"
        else:
            item = turn.start_tool(event)
            phase = "start"
        name = str(item.get("name") or "tool")
        success = item.get("success")
        display = self._tool_display(name, phase, success)
        progress = ToolProgress(
            session=turn.session,
            tool_call_id=item.get("id"),
            tool_name=name,
            phase=phase,
            display_text=display,
            success=success,
        )
        self._emit("tool", progress)
        self._emit("progress", turn.session, display)

    def _handle_stream_resume(self, event: dict[str, Any]) -> None:
        nested = event.get("events") if isinstance(event.get("events"), list) else []
        for item in nested:
            if not isinstance(item, dict) or not isinstance(item.get("event"), dict):
                continue
            replay = dict(item["event"])
            replay.setdefault("sessionId", event.get("sessionId"))
            replay.setdefault("sessionPath", event.get("sessionPath"))
            replay.setdefault("streamId", event.get("streamId"))
            replay.setdefault("seq", item.get("seq"))
            replay["__fromReplay"] = True
            self._handle_event(replay)

        turn = self._find_turn(event)
        if turn is None or turn.done:
            return
        turn.bind_stream(event.get("streamId"))
        next_seq = event.get("nextSeq")
        if isinstance(next_seq, int) and next_seq > 0:
            turn.last_seq = max(turn.last_seq, next_seq - 1)
        if event.get("isStreaming") or event.get("runtimeIsStreaming"):
            return
        threading.Thread(
            target=self._recover_from_history,
            args=(turn,),
            name="hanako-history-recovery",
            daemon=True,
        ).start()

    def _handle_connection_state(self, state: ConnectionState, _error: str | None) -> None:
        if state is not ConnectionState.READY:
            return
        with self._lock:
            turns = self._unique_pending_locked()
        for turn in turns:
            if turn.done:
                continue
            try:
                self.ws_client.resume_stream(
                    StreamCursor(
                        turn.session.session_id,
                        turn.session.session_path,
                        turn.stream_id,
                        turn.last_seq,
                    )
                )
            except HanakoWSClientError:
                break

    def _recover_from_history(self, turn: TurnAccumulator) -> None:
        if turn.done:
            return
        try:
            history = self.get_history(turn.session, limit=20)
            messages = list(history.messages)
            start = -1
            expected = (turn.display_text or turn.prompt_text).strip()
            for index in range(len(messages) - 1, -1, -1):
                message = messages[index]
                if message.get("role") != "user":
                    continue
                content = str(message.get("displayText") or message.get("content") or "").strip()
                if not expected or content == expected:
                    start = index
                    break
            assistant = next(
                (message for message in messages[start + 1:] if message.get("role") == "assistant"),
                None,
            )
            if assistant is None:
                self._finish_with_error(turn, "Stream ended before reply history was available")
                return
            if not turn.text_parts:
                turn.text_parts.append(str(assistant.get("content") or ""))
            if not turn.thinking_parts:
                turn.thinking_parts.append(str(assistant.get("thinking") or ""))
            if not turn.content_blocks:
                turn.content_blocks.extend(history.content_blocks)
            turn.state = TurnState.COMPLETED
            self._complete_turn(turn)
        except Exception as exc:
            self._finish_with_error(turn, f"Stream recovery failed: {exc}")

    def _create_external_turn(self, event: dict[str, Any]) -> TurnAccumulator | None:
        if not self.mirror_external_replies:
            return None
        session = self._session_for_event(event)
        if session is None:
            return None
        with self._lock:
            existing = self._pending_by_session.get(session.session_id) or self._pending_by_session.get(session.session_path)
            if existing and not existing.done:
                return existing
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            client_id = event.get("clientMessageId") or message.get("clientMessageId")
            turn = TurnAccumulator(
                session=session,
                client_message_id=client_id,
                origin="external",
                prompt_text=str(message.get("text") or ""),
                state=TurnState.ACKED,
                acked=True,
            )
            self._index_turn_locked(turn)
            turn.timeout_timer = threading.Timer(self.reply_timeout, self._expire_turn, args=(turn,))
            turn.timeout_timer.daemon = True
            turn.timeout_timer.start()
            return turn

    def _find_turn(self, event: dict[str, Any]) -> TurnAccumulator | None:
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        client_id = event.get("clientMessageId") or message.get("clientMessageId")
        stream_id = event.get("streamId")
        session_id = event.get("sessionId")
        session_path = event.get("sessionPath")
        with self._lock:
            if client_id and client_id in self._pending_by_client:
                return self._pending_by_client[client_id]
            if stream_id and stream_id in self._pending_by_stream:
                return self._pending_by_stream[stream_id]
            if session_id and session_id in self._pending_by_session:
                return self._pending_by_session[session_id]
            if session_path and session_path in self._pending_by_session:
                return self._pending_by_session[session_path]
        return None

    def _session_for_event(self, event: dict[str, Any]) -> SessionRef | None:
        session_id = str(event.get("sessionId") or "").strip()
        session_path = str(event.get("sessionPath") or "").strip()
        with self._lock:
            session = self._sessions_by_id.get(session_id) or self._sessions_by_path.get(session_path)
        if session:
            return session
        if session_id and session_path:
            session = SessionRef(session_id, session_path)
            self._remember_session(session)
            return session
        return None

    def _session_from_payload(self, payload: dict[str, Any]) -> SessionRef | None:
        nested = payload.get("session") if isinstance(payload.get("session"), dict) else {}
        session_id = str(payload.get("sessionId") or nested.get("sessionId") or "").strip()
        session_path = str(payload.get("path") or payload.get("sessionPath") or nested.get("path") or nested.get("sessionPath") or "").strip()
        if not session_id or not session_path:
            return None
        return SessionRef(
            session_id,
            session_path,
            payload.get("agentId") or nested.get("agentId"),
            payload.get("title") or nested.get("title"),
        )

    def _remember_session(self, session: SessionRef) -> None:
        with self._lock:
            self._sessions_by_id[session.session_id] = session
            self._sessions_by_path[session.session_path] = session

    def _index_turn_locked(self, turn: TurnAccumulator) -> None:
        self._pending_by_session[turn.session.session_id] = turn
        self._pending_by_session[turn.session.session_path] = turn
        if turn.client_message_id:
            self._pending_by_client[turn.client_message_id] = turn
        if turn.stream_id:
            self._pending_by_stream[turn.stream_id] = turn

    def _discard_turn(self, turn: TurnAccumulator) -> None:
        with self._lock:
            self._remove_turn_locked(turn)
        if turn.timeout_timer:
            turn.timeout_timer.cancel()

    def _complete_turn(self, turn: TurnAccumulator) -> None:
        with self._lock:
            if turn.done:
                return
            self._remove_turn_locked(turn)
            if turn.timeout_timer:
                turn.timeout_timer.cancel()
            result = turn.make_result()
            turn.future.set_result(result)
        self._emit("reply", result)

    def _finish_with_error(self, turn: TurnAccumulator, message: str) -> None:
        if turn.done:
            return
        turn.error = message
        turn.state = TurnState.FAILED
        self._complete_turn(turn)

    def _expire_turn(self, turn: TurnAccumulator) -> None:
        self._finish_with_error(turn, f"Hanako reply timed out after {self.reply_timeout:g}s")

    def _remove_turn_locked(self, turn: TurnAccumulator) -> None:
        for mapping in (self._pending_by_session, self._pending_by_client, self._pending_by_stream):
            stale = [key for key, value in mapping.items() if value is turn]
            for key in stale:
                mapping.pop(key, None)

    def _unique_pending_locked(self) -> list[TurnAccumulator]:
        unique: list[TurnAccumulator] = []
        seen: set[int] = set()
        for turn in self._pending_by_session.values():
            if id(turn) not in seen:
                unique.append(turn)
                seen.add(id(turn))
        return unique

    def _add_callback(self, kind: str, callback: Callable[..., None]) -> Callable[[], None]:
        with self._lock:
            self._callbacks[kind].append(callback)
        closed = False

        def remove() -> None:
            nonlocal closed
            with self._lock:
                if closed:
                    return
                closed = True
                try:
                    self._callbacks[kind].remove(callback)
                except ValueError:
                    pass

        return remove

    def _emit(self, kind: str, *args: Any) -> None:
        with self._lock:
            callbacks = list(self._callbacks[kind])
        for callback in callbacks:
            try:
                callback(*args)
            except Exception:
                logger.exception("Hanako Session callback failed | kind=%s", kind)

    @staticmethod
    def _tool_display(tool_name: str, phase: str, success: bool | None) -> str:
        if phase == "end":
            return "工具执行完成…" if success is not False else "工具执行失败…"
        exact = TOOL_ACTIVITY.get(tool_name)
        if exact:
            return exact
        lowered = tool_name.lower()
        for key, text in TOOL_ACTIVITY.items():
            if key in lowered:
                return text
        return f"正在使用 {tool_name}…"

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Accept", "application/json")
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            response = self._http.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                timeout=self.request_timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise HanakoSessionError(f"Hanako REST request failed: {method} {path}") from exc
        if not response.ok:
            raise HanakoSessionError(f"Hanako REST {method} {path} returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise HanakoSessionError(f"Hanako REST {method} {path} returned invalid JSON") from exc
        if isinstance(data, dict) and data.get("error"):
            raise HanakoSessionError(str(data.get("detail") or data.get("error")))
        return data
