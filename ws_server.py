"""WebSocket 服务器 — 桌宠与 Hanako Agent 的实时通信

桌宠通过 WebSocket 连接到此服务器，接收 Agent 的实时事件推送（text_delta、tool_start 等）
和完整回复消息。同时接收桌宠发来的 outbox 消息并写入文件队列。

用法:
    python ws_server.py    # 默认 ws://localhost:19900/companion
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

import websockets

logger = logging.getLogger(__name__)

# ── 数据路径 ──────────────────────────────────────────────

HOME_DIR = Path.home()
DATA_DIR = HOME_DIR / ".hanako" / "plugins" / "hanako-desktop-companion"
OUTBOX_FILE = DATA_DIR / "outbox.json"
PENDING_FLAG = DATA_DIR / ".pending"
RESPONSE_FILE = DATA_DIR / "response.json"

DEFAULT_PORT = 19900


class WebSocketServer:
    """简单的 WebSocket 服务器，处理桌宠连接和消息路由。"""

    def __init__(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self._clients: set[websockets.WebSocketServerProtocol] = set()
        self._running = False
        self._server: Optional[websockets.WebSocketServer] = None
        self._outbox_lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self):
        """启动 WebSocket 服务器"""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._server = await websockets.serve(
            self._handle_client, self.host, self.port,
            ping_interval=20,
            ping_timeout=10,
        )
        self._running = True
        logger.info("WS server started at ws://%s:%d", self.host, self.port)

    async def stop(self):
        """停止服务器"""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        self._running = False
        logger.info("WS server stopped")

    async def _handle_client(self, ws: websockets.WebSocketServerProtocol, path: str = ""):
        """处理单个客户端连接"""
        self._clients.add(ws)
        logger.info("Client connected (total: %d)", len(self._clients))

        try:
            # 发送连接确认
            await ws.send(json.dumps({
                "type": "connected",
                "clients": len(self._clients),
            }))

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    await self._handle_incoming(ws, msg)
                except json.JSONDecodeError:
                    logger.warning("WS: non-JSON message from client")
        except websockets.ConnectionClosed:
            logger.info("Client disconnected normally")
        except Exception as e:
            logger.error("WS: client error: %s", e)
        finally:
            self._clients.discard(ws)
            logger.info("Client removed (total: %d)", len(self._clients))

    async def _handle_incoming(self, ws: websockets.WebSocketServerProtocol, msg: dict):
        """处理客户端发来的消息"""
        msg_type = msg.get("type", "")

        if msg_type == "outbox":
            # 桌宠发送的新消息 → 写入 outbox.json
            await self._enqueue_outbox(msg.get("text", ""), msg.get("character"))
        elif msg_type == "ping":
            await ws.send(json.dumps({"type": "pong", "ts": time.time()}))
        elif msg_type == "subscribe":
            # 客户端声明感兴趣的事件类型
            logger.info("Client subscribed: %s", msg.get("events", []))

    async def _enqueue_outbox(self, text: str, character: Optional[str] = None):
        """将消息追加到 outbox.json"""
        async with self._outbox_lock:
            messages = []
            if OUTBOX_FILE.exists():
                try:
                    messages = json.loads(OUTBOX_FILE.read_text("utf-8"))
                except (json.JSONDecodeError, ValueError):
                    messages = []

            if not isinstance(messages, list):
                messages = []

            messages.append({
                "text": text,
                "character": character,
                "time": time.time(),
            })

            OUTBOX_FILE.write_text(json.dumps(messages, ensure_ascii=False), "utf-8")
            PENDING_FLAG.write_text("1", "utf-8")
            logger.info("Outbox enqueued: %d messages", len(messages))

    def send_to_clients(self, msg: dict):
        """向所有连接的客户端发送消息（同步方法，供 Agent 插件调用）"""
        if not self._clients:
            return

        payload = json.dumps(msg, ensure_ascii=False)
        # 异步发送（非阻塞）
        try:
            loop = asyncio.get_running_loop()
            async def _send():
                tasks = []
                for c in list(self._clients):
                    tasks.append(asyncio.shield(c.send(payload)))
                await asyncio.gather(*tasks, return_exceptions=True)
            asyncio.ensure_future(_send(), loop=loop)
        except RuntimeError:
            # 没有事件循环，记录但不阻断
            logger.debug("WS: no event loop, message not sent: %s", msg.get("type"))

    def send_response(self, reply: str, character: str = "", anim: str = "idle", emotion: str = "neutral", audio_path: str = ""):
        """向桌宠发送完整回复消息"""
        self.send_to_clients({
            "type": "response",
            "reply": reply,
            "character": character,
            "anim": anim,
            "emotion": emotion,
            "audioPath": audio_path,
            "ts": time.time(),
        })

    def push_event(self, event: dict):
        """推送实时事件（tool_start, text_delta, thinking_start 等）"""
        self.send_to_clients({
            "type": event.get("type", "unknown"),
            "data": event.get("data", {}),
            "ts": time.time(),
        })


# ── 全局单例 ──────────────────────────────────────────────

_server: Optional[WebSocketServer] = None


def get_server() -> WebSocketServer:
    global _server
    if _server is None:
        _server = WebSocketServer()
    return _server


# ── CLI 入口 ──────────────────────────────────────────────

async def main():
    srv = WebSocketServer()
    await srv.start()

    print(f"OC Pet WebSocket Server running at ws://127.0.0.1:{DEFAULT_PORT}/companion")
    print("Press Ctrl+C to stop...")

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        await srv.stop()


if __name__ == "__main__":
    asyncio.run(main())
