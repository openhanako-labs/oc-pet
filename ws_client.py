"""桥梁客户端 — 桌宠与 Hanako 插件的实时通信适配

仅 WebSocket 模式（无 HTTP 回退）。
事件驱动情绪映射通过 on_event 回调实时推送。

用法:
    bridge = BridgeClient(ws_url="ws://localhost:19900/companion")
    bridge.on_message = lambda msg: print(msg)
    bridge.on_event = lambda event: print(event)  # 实时事件推送（工具调用等）
    bridge.start()
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer

logger = logging.getLogger(__name__)

WS_AVAILABLE = False
try:
    from PySide6.QtWebSockets import QWebSocket
    from PySide6.QtCore import QUrl
    WS_AVAILABLE = True
except ImportError:
    pass


# ── 消息脱弹器（outbox 压缩）──────────────────────────

def compress_messages(messages: list[dict]) -> str:
    """TokenJuice 式压缩 — 多条消息 → 一段摘要文本。

    3 条以内不压缩，直接原文传递。
    超过 3 条：只显示首 2 条 + 最新 1 条 + 计数。
    """
    if not messages:
        return ""
    if len(messages) <= 3:
        return "\n".join(
            f"[{m.get('time', '?')}] {m.get('text', '')}"
            for m in messages
        )

    parts = [
        f"你有 {len(messages)} 条新消息：",
        f"[{messages[0].get('time', '?')}] {messages[0].get('text', '')}",
        f"[{messages[1].get('time', '?')}] {messages[1].get('text', '')}",
        f"… 还有 {len(messages) - 3} 条未显示",
        f"[最新] {messages[-1].get('text', '')}",
    ]
    return "\n".join(parts)


class BridgeClient(QObject):
    """WebSocket 桥梁客户端。

    仅 WebSocket 模式。通过 on_message / on_event 回调接收消息。
    支持通过 WS 发送 outbox 消息（替代文件轮询写入）。
    """

    from paths import RESPONSE_FILE
    from paths import OUTBOX_FILE

    def __init__(self, ws_url="ws://localhost:19900/companion", parent=None):
        super().__init__(parent)
        self._ws = None
        self._connected = False

        # 回调
        self.on_message: callable = lambda _: None      # 完整回复消息
        self.on_event: callable = lambda _: None         # 实时事件（tool_start/text_delta/等）
        self.on_connected: callable = lambda: None
        self.on_disconnected: callable = lambda: None

        # 配置
        self._ws_url = ws_url
        self._send_queue: list[dict] = []              # 离线下发的 outbox 消息

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self):
        """启动 WebSocket 连接"""
        if not WS_AVAILABLE:
            logger.warning("QWebSocket not available, BridgeClient cannot start")
            return
        self._ws = QWebSocket()
        self._ws.connected.connect(self._on_ws_open)
        self._ws.disconnected.connect(self._on_ws_close)
        self._ws.textMessageReceived.connect(self._on_ws_text)

        url = QUrl(self._ws_url)
        logger.info("Bridge: connecting to %s", url.toString())
        self._ws.open(url)

    def stop(self):
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def _on_ws_open(self):
        self._connected = True
        logger.info("Bridge: WebSocket connected")
        # 发送订阅声明
        if self._ws:
            try:
                self._ws.send(json.dumps({"type": "subscribe", "events": ["*"]}))
            except Exception:
                pass
        # 发送缓存的 outbox 消息
        self._flush_send_queue()
        self.on_connected()

    def _on_ws_close(self):
        self._connected = False
        logger.info("Bridge: WebSocket disconnected")
        self.on_disconnected()

    def _on_ws_text(self, text: str):
        """接收 WebSocket 消息，分发到 on_message 或 on_event。
        
        自动识别消息类型：
        - 含 type 字段且为事件类型（thinking_start/text_delta/tool_start/等）
          → 作为实时事件推送，触发桌宠情绪变化
        - 含 reply 字段 → 作为完整回复消息
        - 其他 → 作为普通消息
        """
        try:
            msg = json.loads(text)
            msg_type = msg.get("type", "")

            # 实时事件列表（移植 HanakoPro 的事件转发机制）
            if msg_type in (
                "thinking_start", "thinking_delta",
                "text_delta", "mood_text",
                "tool_start", "tool_end", "tool_progress",
                "vision_progress", "file_write_prepare",
                "turn_end", "status",
            ):
                logger.debug("Bridge: event %s", msg_type)
                self.on_event(msg)
                return

            # 完整回复消息
            if msg_type == "response" or "reply" in msg:
                logger.info("Bridge: received message %s", msg)
                self.on_message(msg)
                return

            # 兜底
            logger.info("Bridge: received raw message")
            self.on_message(msg)
        except json.JSONDecodeError:
            logger.warning("WS received non-JSON: %s", text[:100])

    # ── Outbox 读取 + 压缩 ─────────────────────────────

    @staticmethod
    def read_outbox_compressed() -> str:
        """读取 outbox.json → 压缩后返回文本（用于 Agent prompt）"""
        try:
            outbox = BridgeClient.OUTBOX_FILE
            if not outbox.exists():
                return ""
            raw = outbox.read_text("utf-8").strip()
            if not raw or raw == "{}" or raw == "[]":
                return ""
            messages = json.loads(raw)
            if not isinstance(messages, list):
                messages = [messages]
            return compress_messages(messages)
        except Exception as e:
            logger.warning("read_outbox_compressed error: %s", e)
            return ""

    # ── Outbox 发送 ─────────────────────────────────────

    def send_outbox(self, text: str, character: str = ""):
        """发送桌宠消息到 Agent（通过 WS 实时推送）。
        
        如果 WS 连接中，直接发送；否则缓存到队列，下次连接时发送。
        """
        msg = {"type": "outbox", "text": text, "character": character}
        if self._connected and self._ws:
            try:
                self._ws.send(json.dumps(msg))
            except Exception as e:
                logger.warning("WS send failed, queuing: %s", e)
                self._send_queue.append(msg)
        else:
            self._send_queue.append(msg)

    def _flush_send_queue(self):
        """发送缓存的 outbox 消息"""
        for msg in self._send_queue:
            try:
                self._ws.send(json.dumps(msg))
            except Exception as e:
                logger.warning("Queue flush failed: %s", e)
                break
        self._send_queue.clear()