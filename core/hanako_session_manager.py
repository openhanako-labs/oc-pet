"""Hanako Session REST facade and streamed-turn aggregator.

The manager sits above :mod:`hanako_ws_client`.  It resolves stable Session
identities, guarantees one pending turn per Session, aggregates sequenced
stream events, exposes Future-based replies, and resumes interrupted streams
without ever resending a prompt.
"""
from __future__ import annotations

import logging
import threading
import time
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
    # 活跃时间戳：每次收到本 turn 的事件时刷新（工具链/tool 结果后继续流式回复时，
    # 用"最近活动"判断 turn 是否还活着，而不是死等固定的 reply_timeout 墙）。
    last_event_ts: float = field(default_factory=time.monotonic)
    # BugFix #1：turn 创建时刻。用于识别"一条事件都没收到"的静默 turn——
    # last_event_ts 初始值 == created_at，收到带 seq/streamId 的事件后会被刷新。
    created_at: float = field(default_factory=time.monotonic)
    # BugFix #2：是否收到过本 turn 的流式事件（accept_event 命中）。显式布尔，
    # 不依赖 time.monotonic() 严格递增——本机粒度 ~16ms，同一 tick 内刷新
    # last_event_ts 后可能与 created_at 相等，导致"收到事件的 turn"被误判静默、
    # 静默恢复误触发（历史有旧回复时提前用旧文本完成/截断流式回复）。
    received_any_event: bool = False
    # BugFix #4（任务 #2）：是否收到过"携带最终回复文本"的事件（text_delta/turn_end）。
    # tool_start/tool_end/thinking_* 等事件会刷新 last_event_ts（turn 活跃、顺延
    # deadline），但**不携带最终回复文本**——若 turn 身份与 WS 事件不匹配，服务端
    # 在别的流身份上回推文本，text_delta/turn_end 永远不会到达本 turn。此时只靠
    # tool 事件计数"活跃"会把 turn 拖到 activity_timeout(60s) 才恢复，用户看到
    # "工具气泡有、最终回复气泡/TTS 没有"。用本标志区分"turn 还活着（工具事件）"
    # 与"turn 已拿到最终文本"，让 send_and_wait 在工具链静默 grace 秒后主动从
    # 会话历史恢复最终回复。
    received_final_text_event: bool = False

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
        self.received_any_event = True
        self.last_seq_by_stream[stream_id] = seq
        self.last_seq = max(self.last_seq, seq)
        self.last_event_ts = time.monotonic()
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
        activity_timeout: float = 60.0,
        silent_turn_grace: float = 20.0,
    ):
        self.ws_client = ws_client
        self.base_url = (base_url or ws_client.base_url).rstrip("/")
        self._token = token.strip()
        self.request_timeout = max(1.0, float(request_timeout))
        self.reply_timeout = max(1.0, float(reply_timeout))
        self.mirror_external_replies = bool(mirror_external_replies)
        # 活跃超时：turn 在 reply_timeout 墙内只要持续收到事件（tool_*/text_delta/
        # thinking_*），就按此窗口顺延。避免"服务端工具链（play→tts→tts_bus 等
        # 慢工具）>180s"被固定超时墙误杀，导致最终回复没回灌桌宠（Bug A）。
        # send_and_wait 的超时墙同样按活跃窗口顺延。默认 60s；下限 0.5s 仅防病态配置。
        self.activity_timeout = max(0.5, float(activity_timeout))
        # BugFix #1：静默 turn 快速恢复宽限（秒）。send 后 turn 在宽限期内一条
        # WS 事件都没收到（连 ack/stream 都没绑上）时，视为"turn 身份与 WS 事件
        # 不匹配"（服务端在别的 session 身份上流式回推，本地永远等不到 turn_end）。
        # 主动从会话历史恢复最终回复，把 send_and_wait 死等 reply_timeout(180s)
        # 造成的"桌宠思考中... 卡 3 分钟、无回复无 TTS"缩到 ~20s。
        self.silent_turn_grace = max(1.0, float(silent_turn_grace))
        self._http = requests.Session()

        self._lock = threading.RLock()
        self._pending_by_session: dict[str, TurnAccumulator] = {}
        self._pending_by_client: dict[str, TurnAccumulator] = {}
        self._pending_by_stream: dict[str, TurnAccumulator] = {}
        # P0：超时判死但 abort 发送失败（如 WS 断线）的 session 登记表，
        # 断线重连 READY 后补发 abort，避免服务端 turn 空转占住 session_busy。
        self._abort_pending: dict[str, tuple[str, str | None]] = {}  # session_id -> (session_path, stream_id)
        self._sessions_by_id: dict[str, SessionRef] = {}
        self._sessions_by_path: dict[str, SessionRef] = {}
        # WS 断连时刻（monotonic）；READY 后清空。用于 send_and_wait 判断
        # “断连超过宽限期 → 不再死等，直接判死 turn”（修复桌宠卡“还在想”）。
        self._ws_offline_since: float | None = None

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
        # Bug A 修复：超时改为"活跃窗口顺延"——服务端工具链（play→tts→tts_bus 等
        # 慢工具）期间 turn 持续收到 tool_*/text_delta 事件，deadline 随最近活动顺延，
        # 不再被固定的 timeout 墙误杀。真正超时（长时间无事件）时先尝试从会话历史
        # 恢复最终回复（云端有文本，只是 WS 没送达），恢复失败才判失败。
        deadline = time.monotonic() + max(1.0, float(timeout))
        while True:
            now = time.monotonic()
            remaining = deadline - now
            # BugFix #1：以 ~1s 轮询粒度等待（而非一次性等满 deadline），
            # 让"静默 turn 快速恢复"能在 silent_turn_grace 到期后立刻触发，
            # 而不是等满 reply_timeout(180s) 才进超时处理。
            try:
                return future.result(timeout=max(0.1, min(1.0, remaining)))
            except FutureTimeoutError:
                with self._lock:
                    turn = next(
                        (item for item in self._unique_pending_locked() if item.future is future),
                        None,
                    )
                if turn is None:
                    return future.result(timeout=1.0)
                now = time.monotonic()
                last = getattr(turn, "last_event_ts", 0.0)
                created = getattr(turn, "created_at", last)
                # 是否收到过本 turn 的事件：显式布尔（accept_event 命中置位），
                # 不依赖时间戳严格递增（BugFix #2：monotonic 粒度下可能相等）。
                received_any_event = bool(getattr(turn, "received_any_event", False))
                # BugFix #4（任务 #2）+ 任务 #3 修正：turn 收到过事件（tool_start/
                # text_delta 等），但**从未被终态事件完成**（turn_end/error/abort
                # 未到）——典型场景是 turn 身份与 WS 事件不匹配：工具进度事件能到达
                # （hanako_monitor 独立推送分支），而本 turn 的 turn_end 永远不会来；
                # 或 text_delta 到达后流中断（含空 delta），received_final_text_event
                # 误置位把旧条件 `not received_final_text` 永久卡死。修复：只依据
                # `not turn.done`（turn_end/error/abort 都会置 done，即"已拿到最终
                # 文本"的权威信号），工具链/流静默超过 silent_turn_grace 秒后主动
                # 从会话历史恢复；历史暂无回复则继续等（finish_on_missing=False，
                # 绝不提前判死），并每隔 grace 秒重试，直到 deadline 墙兜底。
                # 修复"工具气泡有、最终回复气泡/TTS 没有"（多 tool 事件密集场景）。
                if (received_any_event and not turn.done
                        and (now - last) >= self.silent_turn_grace):
                    last_tool_recovery = float(getattr(turn, "_last_tool_silent_recovery_ts", 0.0))
                    if (now - last_tool_recovery) >= self.silent_turn_grace:
                        turn._last_tool_silent_recovery_ts = now
                        logger.warning(
                            "[SM] tool-silent turn: %.0fs 仅收到活跃事件、"
                            "无终态完成，尝试从会话历史恢复 (session=%s)",
                            now - last, getattr(turn.session, "session_id", "?"),
                        )
                        try:
                            self._recover_from_history(turn, finish_on_missing=False)
                        except Exception:
                            logger.debug("hanako_session_manager: 非致命异常(已静默吞掉)", exc_info=True)
                        if turn.done:
                            return future.result(timeout=1.0)
                if received_any_event and (now - last) < self.activity_timeout:
                    deadline = now + self.activity_timeout
                    continue
                # BugFix #1：静默 turn 快速恢复——send 后宽限期内一条事件都没收到，
                # 极可能是 turn 身份与 WS 事件不匹配：服务端在另一个 session 身份上
                # 流式回推，本地永远等不到 turn_end。云端其实已生成回复（Hanako 主
                # 窗口可见），主动从历史恢复；历史暂无回复则继续等 WS/正常超时，
                # 绝不提前打断仍可能"正在处理"的 turn。
                if (not getattr(turn, "_silent_recovery_tried", False)
                        and not received_any_event
                        and (now - created) >= self.silent_turn_grace):
                    turn._silent_recovery_tried = True
                    logger.warning(
                        "[SM] silent turn: %.0fs 无 WS 事件，尝试从会话历史恢复回复 "
                        "(session=%s) — 桌宠不再死等 reply_timeout",
                        now - created, getattr(turn.session, "session_id", "?"),
                    )
                    try:
                        self._recover_from_history(turn, finish_on_missing=False)
                    except Exception:
                        logger.debug("hanako_session_manager: 非致命异常(已静默吞掉)", exc_info=True)
                    if turn.done:
                        return future.result(timeout=1.0)
                # deadline 墙已到：先尝试从历史恢复最终回复，失败才判死
                if remaining <= 0:
                    try:
                        self._recover_from_history(turn)
                    except Exception:
                        logger.debug("hanako_session_manager: 非致命异常(已静默吞掉)", exc_info=True)
                    if turn.done:
                        return future.result(timeout=1.0)
                    self._finish_with_error(turn, f"Hanako reply timed out after {timeout:g}s")
                    return future.result(timeout=1.0)
                # WS 断连超过宽限期：回复不可能送达（服务端已生成也收不到），
                # 尝试从历史恢复（云端有文本）；恢复失败则判死，让桌宠快速出兑底，
                # 不再死等到 deadline（修复桌宠一直“还在想”）。
                with self._lock:
                    offline_since = self._ws_offline_since
                if offline_since is not None and not turn.done:
                    offline_for = now - offline_since
                    if offline_for >= self.silent_turn_grace:
                        logger.warning(
                            "[SM] WS 断连 %.0fs（grace=%0.fs），尝试历史恢复后判死 (session=%s)",
                            offline_for, self.silent_turn_grace,
                            getattr(turn.session, "session_id", "?"),
                        )
                        try:
                            self._recover_from_history(turn)
                        except Exception:
                            logger.debug("hanako_session_manager: 非致命异常(已静默吞掉)", exc_info=True)
                        if turn.done:
                            return future.result(timeout=1.0)
                        self._finish_with_error(
                            turn,
                            f"WS 断连 {offline_for:.0f}s，Hanako 回复未送达",
                        )
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

    def _abort_server_turn(self, turn: TurnAccumulator, reason: str = "client_timeout") -> None:
        """Best-effort 通知服务端取消该 turn（P0 修复）。

        客户端判死（超时/放弃）时主动 abort：不等待服务端 abort_result，
        本地立即完成 turn；服务端收到 abort 后应取消当前工具/LLM 执行并释放
        session_busy，避免后台空转（最长 20min）且锁死后续消息。
        """
        if turn is None or turn.done:
            return
        session = turn.session
        try:
            self.ws_client.abort_stream(
                StreamCursor(session.session_id, session.session_path, turn.stream_id, turn.last_seq),
                reason=reason,
            )
        except HanakoWSClientError:
            logger.debug(
                "[SM] best-effort abort send failed, will retry on reconnect "
                "| session=%s stream=%s", session.session_id, turn.stream_id, exc_info=True,
            )
            with self._lock:
                self._abort_pending[session.session_id] = (session.session_path, turn.stream_id)
            return
        with self._lock:
            self._abort_pending.pop(session.session_id, None)

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
        if turn is None:
            return
        if turn.done:
            return

        # BugFix #3：_find_turn 会按 session_id/session_path 兜底匹配，同 session
        # 其他消息的事件（如用户在 Hanako 主窗口同时发消息）会被路由到本 turn。
        # 若事件携带 clientMessageId 且与本 turn 不匹配 → 是别的消息，直接拒绝：
        # 不绑定 stream、不刷新 last_seq，避免污染本 turn 文本 / 掩盖静默。
        # （与 _handle_user_echo 的 clientMessageId 校验同语义，这里覆盖全部事件类型。）
        _event_client_id = event.get("clientMessageId")
        if not _event_client_id and isinstance(event.get("message"), dict):
            _event_client_id = event.get("message", {}).get("clientMessageId")
        if _event_client_id and turn.client_message_id and _event_client_id != turn.client_message_id:
            return

        turn.bind_stream(event.get("streamId"))
        if not turn.accept_event(event):
            return
        if turn.stream_id:
            with self._lock:
                self._pending_by_stream[turn.stream_id] = turn

        # BugFix #4（任务 #2）+ 任务 #3 修正：received_final_text_event 只在
        # turn_end 置位。text_delta 是**增量**事件——服务端可能先推空 delta 再推
        # 正文，也可能推了部分 delta 后流中断（turn_end 永远不到）。若 text_delta
        # 就置位，会把"已拿到最终文本"误判为 True，永久屏蔽 tool-silent 恢复分支，
        # 导致 turn 拖到 deadline 墙（~2-3min）才恢复，用户看到"工具气泡有、最终
        # 回复气泡/TTS 没有"（多 tool 事件密集 + 空 text_delta 场景实测复现）。
        # turn_end 才是唯一权威的"最终文本已送达"信号，且 turn_end 会走
        # _complete_turn（turn.done=True）——tool-silent 分支的 `not turn.done`
        # 已天然排除已完成 turn。
        if event_type == "turn_end":
            turn.received_final_text_event = True

        if event_type == "session_user_message":
            self._handle_user_echo(turn, event)
        elif event_type == "status":
            self._handle_status(turn, event)
        elif event_type == "text_delta":
            delta = str(event.get("delta") or event.get("text") or "")
            turn.text_parts.append(delta)
            turn.state = TurnState.STREAMING
            self._emit("progress", turn.session, "正在回复…")
        elif event_type == "thinking_start":
            turn.state = TurnState.STREAMING
            self._emit("progress", turn.session, "正在思考…")
        elif event_type == "thinking_delta":
            turn.thinking_parts.append(str(event.get("delta") or event.get("text") or ""))
            turn.state = TurnState.STREAMING
            self._emit("progress", turn.session, "正在思考…")
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
            logger.info("[SM] turn_end: text_parts=%d, text='%s'", len(turn.text_parts), "".join(turn.text_parts)[:80])
            self._complete_turn(turn)
        elif event_type == "error":
            err_msg = str(event.get("message") or event.get("error") or "Hanako turn failed")
            # P0 修复：服务端 error event 不一定是终态失败（如流中断、tool 未收尾、
            # 模型端 429/5xx）——若会话历史里已经有完整回复（云端生成、WS 未送达），
            # 直接用历史文本完成 turn，避免桌宠只看到 “…” 或误把错误当回复。
            # finish_on_missing=False：历史无回复时不判死，交给下面 _finish_with_error。
            try:
                self._recover_from_history(turn, finish_on_missing=False)
            except Exception as exc:
                logger.debug("[SM] error event recovery failed (ignored): %s", exc)
            if turn.done:
                return
            self._finish_with_error(turn, err_msg)
        elif event_type == "abort_rejected":
            self._emit("progress", turn.session, "终止请求被拒绝…")
        elif event_type == "abort_result":
            # P5 修复：abort_result 视为中止成功，完成 turn
            # 服务端会先发 status(aborted=true) 再发 abort_result，两者都触发完成
            # _handle_status 已经处理了 status(aborted=true)，这里处理 abort_result
            turn.aborted = True
            turn.state = TurnState.ABORTED
            self._complete_turn(turn)
            self._emit("progress", turn.session, "已终止")

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
            # 断连/重连中：记录首次断连时刻（不覆盖更早的记录），
            # send_and_wait 据此判断宽限期。
            with self._lock:
                if self._ws_offline_since is None:
                    self._ws_offline_since = time.monotonic()
            return
        # READY（重连成功）：清空断连记录
        with self._lock:
            self._ws_offline_since = None
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
        
        # P0：补发超时判死但当时未能送达的 abort（WS 曾不可用）
        with self._lock:
            pending_aborts = list(self._abort_pending.items())
        for session_id, (session_path, stream_id) in pending_aborts:
            with self._lock:
                current = self._pending_by_session.get(session_id)
                if current is not None and not current.done:
                    continue  # 该 session 已有新 turn，不能再 abort
            try:
                self.ws_client.abort_stream(
                    StreamCursor(session_id, session_path, stream_id, 0),
                    reason="client_timeout",
                )
            except HanakoWSClientError:
                continue
            with self._lock:
                self._abort_pending.pop(session_id, None)

    def _recover_from_history(self, turn: TurnAccumulator, *, finish_on_missing: bool = True) -> None:
        """从会话历史恢复最终回复（Bug A / BugFix #1 共用）。

        Args:
            finish_on_missing: 历史里找不到最终回复时是否判死 turn。
              - True（正常超时路径）：找不到 → _finish_with_error（180s 已到，判死合理）
              - False（静默恢复路径）：找不到 → 直接返回，turn 保持 pending，
                继续等 WS 事件或走正常超时（服务端可能仍在生成，绝不提前打断）。
        """
        if turn.done:
            return
        try:
            history = self.get_history(turn.session, limit=30)
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
            if start < 0:
                # 精确匹配失败（如服务端历史存了带 [pet-context] 前缀的完整文本）：
                # 退而取**最后一条** user 消息之后的区间（单轮场景下就是我们这条 prompt）。
                for index in range(len(messages) - 1, -1, -1):
                    if messages[index].get("role") == "user":
                        start = index
                        break
            # Bug A 修复：取用户消息之后**最后一条**有正文的 assistant 消息。
            # 工具调用轮历史里第一条 assistant 消息往往是 tool_call 决策（content 空
            # 或只有"我来播放"），最终回复在最后一条——取最后一条有内容的才回灌正确文本。
            candidate_span = messages[start + 1:] if start >= 0 else messages
            assistant = None
            for message in reversed(candidate_span):
                if message.get("role") != "assistant":
                    continue
                content = str(message.get("content") or "").strip()
                # 工具调用决策消息常带 tool_calls 而 content 为空——跳过
                if not content:
                    continue
                assistant = message
                break
            if assistant is None and start >= 0:
                assistant = next(
                    (message for message in candidate_span if message.get("role") == "assistant"),
                    None,
                )
            if assistant is None:
                if not finish_on_missing:
                    # BugFix #1 静默恢复：历史尚无最终回复（服务端可能仍在处理/
                    # 流式），不打断 turn，继续等 WS 事件或走正常超时。
                    return
                self._finish_with_error(turn, "Stream ended before reply history was available")
                return
            # 任务 #3：恢复路径以历史权威文本为准——流式 text_parts 可能被空 delta
            # 污染（[""]）或只有中断的残缺文本（如"晚"），都直接替换为历史完整
            # 回复，避免用户看到空壳/残句（修复"工具气泡有、最终回复气泡/TTS 没
            # 有"）。thinking 同理只填非空（思考内容不展示，保持原语义）。
            turn.text_parts[:] = [str(assistant.get("content") or "")]
            if not any(str(part).strip() for part in turn.thinking_parts):
                turn.thinking_parts[:] = [str(assistant.get("thinking") or "")]
            if not turn.content_blocks:
                turn.content_blocks.extend(history.content_blocks)
            turn.state = TurnState.COMPLETED
            self._complete_turn(turn)
        except Exception as exc:
            if not finish_on_missing:
                # BugFix #1（QA #1）：静默恢复路径的 get_history 瞬时异常（REST 抖动）
                # 不得提前判死 turn，更不得 abort 服务端——turn 保持存活，继续等
                # WS 事件或走正常超时（服务端可能仍在生成）。
                logger.warning(
                    "[SM] silent recovery get_history 异常（忽略，继续等 WS 事件/超时）: %s", exc
                )
                return
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
        # P0 修复：客户端判死（超时/放弃）时主动 abort 服务端，避免服务端
        # 继续执行工具/LLM（最长 20min）且 session_busy 锁死后续消息。
        self._abort_server_turn(turn, reason="client_timeout")
        # 诊断：记录 turn 在失败时刻的状态（文本/工具事件数），便于区分
        # "WS 没送达最终回复" 与 "服务端根本没生成"（Bug A 排查）。
        logger.warning(
            "[SM] turn failed: %s | text_parts=%d tool_events=%d seq=%d last_event=%.1fs ago",
            message, len(turn.text_parts), len(turn.tool_calls),
            turn.last_seq,
            time.monotonic() - getattr(turn, "last_event_ts", time.monotonic()),
        )
        turn.error = message
        turn.state = TurnState.FAILED
        self._complete_turn(turn)

    def _expire_turn(self, turn: TurnAccumulator) -> None:
        """超时定时器回调：活跃则顺延，否则先尝试历史恢复，再判失败。

        Bug A 修复：服务端工具链（play→tts→tts_bus 等）可能远超 reply_timeout，
        只要 turn 持续收到事件就顺延活动窗口；长时间无事件才真正超时，并且
        超时前先从会话历史恢复最终回复（云端有文本，只是 WS 没送达）。
        """
        if turn.done:
            return
        now = time.monotonic()
        last = getattr(turn, "last_event_ts", 0.0)
        if last and (now - last) < self.activity_timeout:
            with self._lock:
                turn.timeout_timer = threading.Timer(
                    self.activity_timeout, self._expire_turn, args=(turn,)
                )
                turn.timeout_timer.daemon = True
                turn.timeout_timer.start()
            return
        try:
            self._recover_from_history(turn)
        except Exception:
            logger.debug("hanako_session_manager: 非致命异常(已静默吞掉)", exc_info=True)
        if turn.done:
            return
        self._finish_with_error(
            turn,
            f"Hanako reply timed out after {self.reply_timeout:g}s "
            f"(no activity for {self.activity_timeout:g}s)",
        )

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
                    logger.debug("hanako_session_manager: 非致命异常(已静默吞掉)", exc_info=True)

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
