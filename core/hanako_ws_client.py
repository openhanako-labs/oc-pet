"""Threaded WebSocket client for the Hanako server.

The client owns transport concerns only: short-lived ticket acquisition,
WebSocket lifecycle, heartbeat, reconnect backoff, and filtered event
subscriptions.  Business callbacks are always executed by the dispatch
thread, never by the WebSocket IO thread.
"""
from __future__ import annotations

import json
import logging
import queue
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable
from urllib.parse import urlencode, urlsplit, urlunsplit

import requests
import websocket

logger = logging.getLogger(__name__)


class HanakoWSClientError(RuntimeError):
    """Base error raised by the Hanako WebSocket transport."""


class HanakoTicketError(HanakoWSClientError):
    """A WebSocket ticket could not be acquired."""


class HanakoUnavailableBeforeSend(HanakoWSClientError):
    """The prompt was definitely not handed to a WebSocket."""


class HanakoSendError(HanakoWSClientError):
    """A socket send failed after a ready socket had been selected."""


class ConnectionState(str, Enum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    READY = "ready"
    BACKOFF = "backoff"
    CLOSING = "closing"


@dataclass(frozen=True)
class StreamCursor:
    session_id: str | None
    session_path: str
    stream_id: str | None
    last_seq: int = 0


@dataclass(frozen=True)
class ReconnectPolicy:
    initial_delay: float = 0.75
    max_delay: float = 30.0
    multiplier: float = 1.8
    jitter: float = 0.2
    max_attempts: int | None = None

    @classmethod
    def from_value(cls, value: "ReconnectPolicy | dict[str, Any] | None") -> "ReconnectPolicy":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            allowed = {name for name in cls.__dataclass_fields__}
            return cls(**{key: val for key, val in value.items() if key in allowed})
        raise TypeError("reconnect must be ReconnectPolicy, dict, or None")


@dataclass
class _EventSubscriber:
    callback: Callable[[dict[str, Any]], None]
    event_types: frozenset[str] | None
    session_id: str | None
    session_path: str | None


class Subscription:
    """Idempotent subscription handle."""

    def __init__(self, cancel: Callable[[], None]):
        self._cancel = cancel
        self._lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._cancel()

    unsubscribe = close

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class HanakoWSClient:
    """One-process, shared Hanako WebSocket connection."""

    _DISPATCH_STOP = object()
    _MAX_DEDUP_KEYS = 4096

    def __init__(
        self,
        base_url: str,
        token: str = "",
        *,
        connect_timeout: float = 10.0,
        ping_interval: float = 20.0,
        ping_timeout: float = 10.0,
        reconnect: ReconnectPolicy | dict[str, Any] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._token = token.strip()
        self.connect_timeout = max(1.0, float(connect_timeout))
        self.ping_interval = max(1.0, float(ping_interval))
        self.ping_timeout = max(1.0, float(ping_timeout))
        self.reconnect = ReconnectPolicy.from_value(reconnect)

        self._state = ConnectionState.STOPPED
        self._state_error: str | None = None
        self._state_lock = threading.RLock()
        self._ready_event = threading.Event()
        self._stop_event = threading.Event()

        self._socket: websocket.WebSocket | None = None
        self._socket_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._http = requests.Session()

        self._subscriptions: dict[int, _EventSubscriber] = {}
        self._state_subscriptions: dict[int, Callable[[ConnectionState, str | None], None]] = {}
        self._subscription_lock = threading.RLock()
        self._next_subscription_id = 1

        self._event_queue: queue.Queue[object] = queue.Queue()
        self._seen_keys: set[tuple[Any, ...]] = set()
        self._seen_order: deque[tuple[Any, ...]] = deque()

        self._io_thread: threading.Thread | None = None
        self._dispatch_thread: threading.Thread | None = None

    @property
    def state(self) -> ConnectionState:
        with self._state_lock:
            return self._state

    @property
    def state_error(self) -> str | None:
        with self._state_lock:
            return self._state_error

    @property
    def is_ready(self) -> bool:
        return self.state is ConnectionState.READY and self._ready_event.is_set()

    def start(self) -> None:
        """Start the dispatch and WebSocket IO threads once."""
        with self._state_lock:
            if self._io_thread and self._io_thread.is_alive():
                return
            self._stop_event.clear()
            self._ready_event.clear()
            self._dispatch_thread = threading.Thread(
                target=self._dispatch_loop,
                name="hanako-ws-dispatch",
                daemon=True,
            )
            self._io_thread = threading.Thread(
                target=self._io_loop,
                name="hanako-ws-io",
                daemon=True,
            )
            self._dispatch_thread.start()
            self._io_thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop reconnecting, close the socket, and join worker threads."""
        if self.state is ConnectionState.STOPPED:
            return
        self._set_state(ConnectionState.CLOSING)
        self._stop_event.set()
        self._ready_event.clear()
        with self._socket_lock:
            sock = self._socket
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

        deadline = time.monotonic() + max(0.0, timeout)
        io_thread = self._io_thread
        if io_thread and io_thread is not threading.current_thread():
            io_thread.join(max(0.0, deadline - time.monotonic()))

        self._event_queue.put(self._DISPATCH_STOP)
        dispatch_thread = self._dispatch_thread
        if dispatch_thread and dispatch_thread is not threading.current_thread():
            dispatch_thread.join(max(0.0, deadline - time.monotonic()))
        self._set_state(ConnectionState.STOPPED, dispatch=False)

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        return self._ready_event.wait(timeout)

    def subscribe(
        self,
        callback: Callable[[dict[str, Any]], None],
        *,
        event_types: Iterable[str] | None = None,
        session_id: str | None = None,
        session_path: str | None = None,
    ) -> Subscription:
        types = frozenset(event_types) if event_types is not None else None
        subscriber = _EventSubscriber(callback, types, session_id, session_path)
        subscription_id = self._store_subscription(subscriber, state_callback=False)
        return Subscription(lambda: self._remove_subscription(subscription_id, False))

    def subscribe_state(
        self,
        callback: Callable[[ConnectionState, str | None], None],
    ) -> Subscription:
        subscription_id = self._store_subscription(callback, state_callback=True)
        state, error = self.state, self.state_error
        self._event_queue.put(("state", state, error))
        return Subscription(lambda: self._remove_subscription(subscription_id, True))

    def send_json(self, payload: dict[str, Any]) -> None:
        """Serialize and send one frame without logging credentials or content."""
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._send_lock:
            with self._socket_lock:
                sock = self._socket
            if not self.is_ready or sock is None or not sock.connected:
                raise HanakoUnavailableBeforeSend("Hanako WebSocket is not ready")
            try:
                sent = sock.send(encoded, opcode=websocket.ABNF.OPCODE_TEXT)
            except Exception as exc:
                raise HanakoSendError("Hanako WebSocket send failed") from exc
            if sent <= 0:
                raise HanakoSendError("Hanako WebSocket accepted no bytes")

    def send_prompt(
        self,
        *,
        session_id: str,
        session_path: str,
        text: str,
        client_message_id: str,
        display_message: Any = None,
        ui_context: Any = None,
    ) -> None:
        payload: dict[str, Any] = {
            "type": "prompt",
            "text": text,
            "clientMessageId": client_message_id,
            "sessionId": session_id,
            "sessionPath": session_path,
        }
        if display_message is not None:
            payload["displayMessage"] = display_message
        if ui_context is not None:
            payload["uiContext"] = ui_context
        self.send_json(payload)

    def resume_stream(self, cursor: StreamCursor) -> None:
        payload: dict[str, Any] = {
            "type": "resume_stream",
            "sessionPath": cursor.session_path,
            "sinceSeq": max(0, int(cursor.last_seq)),
        }
        if cursor.session_id:
            payload["sessionId"] = cursor.session_id
        if cursor.stream_id:
            payload["streamId"] = cursor.stream_id
        self.send_json(payload)

    def abort_stream(self, cursor: StreamCursor, reason: str = "user_abort") -> None:
        payload: dict[str, Any] = {
            "type": "abort",
            "sessionPath": cursor.session_path,
            "reason": reason,
        }
        if cursor.session_id:
            payload["sessionId"] = cursor.session_id
        if cursor.stream_id:
            payload["streamId"] = cursor.stream_id
        self.send_json(payload)

    def _store_subscription(self, value: Any, *, state_callback: bool) -> int:
        with self._subscription_lock:
            subscription_id = self._next_subscription_id
            self._next_subscription_id += 1
            target = self._state_subscriptions if state_callback else self._subscriptions
            target[subscription_id] = value
            return subscription_id

    def _remove_subscription(self, subscription_id: int, state_callback: bool) -> None:
        with self._subscription_lock:
            target = self._state_subscriptions if state_callback else self._subscriptions
            target.pop(subscription_id, None)

    def _set_state(
        self,
        state: ConnectionState,
        error: str | None = None,
        *,
        dispatch: bool = True,
    ) -> None:
        with self._state_lock:
            changed = state is not self._state or error != self._state_error
            self._state = state
            self._state_error = error
            if state is ConnectionState.READY:
                self._ready_event.set()
            else:
                self._ready_event.clear()
        if changed and dispatch:
            self._event_queue.put(("state", state, error))

    def _io_loop(self) -> None:
        attempt = 0
        delay = self.reconnect.initial_delay
        while not self._stop_event.is_set():
            self._set_state(ConnectionState.CONNECTING)
            error_text: str | None = None
            try:
                ticket = self._fetch_ticket()
                self._run_socket(ticket)
                attempt = 0
                delay = self.reconnect.initial_delay
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                error_text = f"{type(exc).__name__}: {exc}"
                logger.warning("Hanako WebSocket disconnected: %s", error_text)
                attempt += 1
            finally:
                self._ready_event.clear()
                with self._socket_lock:
                    self._socket = None

            if self._stop_event.is_set():
                break
            if self.reconnect.max_attempts is not None and attempt >= self.reconnect.max_attempts:
                logger.error("Hanako WebSocket reconnect limit reached")
                break

            self._set_state(ConnectionState.BACKOFF, error_text)
            wait_for = self._jittered_delay(delay)
            if self._stop_event.wait(wait_for):
                break
            delay = min(self.reconnect.max_delay, delay * self.reconnect.multiplier)

        if self.state is not ConnectionState.CLOSING:
            self._set_state(ConnectionState.STOPPED)

    def _fetch_ticket(self) -> str:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            response = self._http.post(
                f"{self.base_url}/api/ws-ticket",
                headers=headers,
                json={},
                timeout=self.connect_timeout,
            )
        except requests.RequestException as exc:
            raise HanakoTicketError("ticket request failed") from exc
        if not response.ok:
            raise HanakoTicketError(f"ticket request returned HTTP {response.status_code}")
        try:
            ticket = str(response.json().get("ticket") or "").strip()
        except (TypeError, ValueError) as exc:
            raise HanakoTicketError("ticket response was not JSON") from exc
        if not ticket:
            raise HanakoTicketError("ticket response did not contain a ticket")
        return ticket

    def _run_socket(self, ticket: str) -> None:
        ws_url = self._websocket_url(ticket)
        sock = websocket.create_connection(
            ws_url,
            timeout=self.connect_timeout,
            enable_multithread=True,
        )
        sock.settimeout(min(1.0, self.ping_interval))
        with self._socket_lock:
            self._socket = sock
        self._set_state(ConnectionState.READY)
        logger.info("Hanako WebSocket connected")

        last_ping = time.monotonic()
        awaiting_pong_at: float | None = None
        while not self._stop_event.is_set() and sock.connected:
            now = time.monotonic()
            if awaiting_pong_at is not None and now - awaiting_pong_at > self.ping_timeout:
                raise TimeoutError("WebSocket pong timeout")
            if awaiting_pong_at is None and now - last_ping >= self.ping_interval:
                with self._send_lock:
                    sock.ping("oc-pet")
                last_ping = now
                awaiting_pong_at = now

            try:
                opcode, data = sock.recv_data(control_frame=True)
            except websocket.WebSocketTimeoutException:
                continue
            if opcode == websocket.ABNF.OPCODE_PONG:
                awaiting_pong_at = None
            elif opcode == websocket.ABNF.OPCODE_PING:
                with self._send_lock:
                    sock.pong(data)
            elif opcode == websocket.ABNF.OPCODE_CLOSE:
                break
            elif opcode in (websocket.ABNF.OPCODE_TEXT, websocket.ABNF.OPCODE_BINARY):
                self._queue_raw_message(data)

    def _websocket_url(self, ticket: str) -> str:
        parsed = urlsplit(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        path = f"{parsed.path.rstrip('/')}/ws" or "/ws"
        # v0.407.15 consumes wsTicket; ticket is retained for older builds.
        query = urlencode({"wsTicket": ticket, "ticket": ticket})
        return urlunsplit((scheme, parsed.netloc, path, query, ""))

    def _queue_raw_message(self, raw: Any) -> None:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            event = json.loads(raw)
        except (UnicodeDecodeError, TypeError, ValueError):
            logger.warning("Ignoring malformed Hanako WebSocket message")
            return
        if isinstance(event, dict):
            self._event_queue.put(("event", event))

    def _dispatch_loop(self) -> None:
        while True:
            item = self._event_queue.get()
            if item is self._DISPATCH_STOP:
                return
            try:
                kind = item[0]
                if kind == "state":
                    self._dispatch_state(item[1], item[2])
                elif kind == "event":
                    self._dispatch_event(item[1])
            except Exception:
                logger.exception("Hanako WebSocket dispatch failure")

    def _dispatch_state(self, state: ConnectionState, error: str | None) -> None:
        with self._subscription_lock:
            callbacks = list(self._state_subscriptions.values())
        for callback in callbacks:
            try:
                callback(state, error)
            except Exception:
                logger.exception("Hanako state subscriber failed")

    def _dispatch_event(self, event: dict[str, Any]) -> None:
        if self._is_duplicate(event):
            return
        event_type = str(event.get("type") or "")
        session_id = event.get("sessionId")
        session_path = event.get("sessionPath")
        with self._subscription_lock:
            subscribers = list(self._subscriptions.values())
        for subscriber in subscribers:
            if subscriber.event_types is not None and event_type not in subscriber.event_types:
                continue
            if subscriber.session_id is not None and subscriber.session_id != session_id:
                continue
            if subscriber.session_path is not None and subscriber.session_path != session_path:
                continue
            try:
                subscriber.callback(event)
            except Exception:
                logger.exception("Hanako event subscriber failed | type=%s", event_type)

    def _is_duplicate(self, event: dict[str, Any]) -> bool:
        seq = event.get("seq")
        stream_id = event.get("streamId")
        session_key = event.get("sessionId") or event.get("sessionPath")
        if not isinstance(seq, int) or not stream_id or not session_key:
            return False
        key = (session_key, stream_id, seq)
        if key in self._seen_keys:
            return True
        if len(self._seen_order) >= self._MAX_DEDUP_KEYS:
            oldest = self._seen_order.popleft()
            self._seen_keys.discard(oldest)
        self._seen_order.append(key)
        self._seen_keys.add(key)
        return False

    def _jittered_delay(self, delay: float) -> float:
        spread = max(0.0, self.reconnect.jitter)
        return max(0.0, delay * random.uniform(1.0 - spread, 1.0 + spread))
