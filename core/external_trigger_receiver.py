"""通用外部触发接收器 — 桌宠的通用外部事件入口（P4）

任何外部调度器（Hana 自动化 / 手机脚本 / 另一只桌宠 / 任意 HTTP 客户端）
都可以 POST 一个"动作触发"给桌宠。桌宠自身本地提醒保持自包含，
本模块是**可选的附加入口**：config `external_trigger.enabled=True` 才启动，
默认关 = 零行为、不占端口。通用机制，不绑定任何特定调度器或个人任务。

接口：
    POST /trigger
    Header: X-Auth-Token: <token>
    Body: {
        "action":  "remind" | "say" | "praise" | "custom",
        "type":    "break" | "work" | "custom",
        "text":    "写了这么久，休息一下吧",
        "emotion": "happy"                      # 可选，默认 neutral
    }
    → 200 {"ok": true, "received": true}
    → 400 非法 JSON / 缺 text / 未知 action
    → 401 token 不匹配

线程模型（与 phone_receiver / status_http_server 一致）：
- 127.0.0.1 监听，可选 X-Auth-Token
- on_trigger 回调在 HTTP 线程执行；PetWindow 侧用 QTimer.singleShot
  转主线程再驱动 Qt 对象（气泡/动画），不直连渲染线程。
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8988

# action 白名单（通用语义动作，收窄外部触发范围）
ACTION_WHITELIST: tuple[str, ...] = ("remind", "say", "praise", "custom")


def _make_handler(on_trigger: Callable[[str, str, str], None], auth_token: str):
    """动态创建请求处理器，绑定回调 / auth_token"""

    class Handler(BaseHTTPRequestHandler):
        """处理通用外部触发请求"""

        def _check_auth(self) -> bool:
            token = self.headers.get('X-Auth-Token', '')
            if not auth_token:
                return False  # 空 token 拒绝访问（安全修复：不再跳过验证）
            return token == auth_token

        def _send_json(self, code: int, data: dict):
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != '/trigger':
                self._send_json(404, {'ok': False, 'error': 'Not found'})
                return
            if not self._check_auth():
                self._send_json(401, {'ok': False, 'error': 'Unauthorized'})
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length) if length > 0 else b'{}'
                data = json.loads(raw)
            except Exception as e:
                self._send_json(400, {'ok': False, 'error': f'Invalid JSON: {e}'})
                return

            action = str(data.get('action', 'custom')).strip() or 'custom'
            text = str(data.get('text', '')).strip()
            emotion = str(data.get('emotion', 'neutral')).strip() or 'neutral'

            if action not in ACTION_WHITELIST:
                self._send_json(400, {'ok': False, 'error': f'Unknown action: {action}'})
                return
            if not text:
                self._send_json(400, {'ok': False, 'error': 'Missing "text" field'})
                return

            try:
                on_trigger(action, text, emotion)
            except Exception as e:
                logger.warning("外部触发回调异常: %s", e)
                self._send_json(500, {'ok': False, 'error': 'Callback failed'})
                return
            # P6: 同时转发到事件总线（让 pet.py 统一订阅 external_trigger 事件）
            try:
                from core.event_bus import EventBus
                EventBus.emit("external_trigger", action=action, text=text, emotion=emotion, source="http")
            except Exception:
                logger.debug("external_trigger_receiver: 非致命异常(已静默吞掉)", exc_info=True)
            self._send_json(200, {'ok': True, 'received': True})

        def log_message(self, *args):  # 静默标准库访问日志
            pass

    return Handler


class ExternalTriggerReceiver:
    """通用外部触发接收器（127.0.0.1，后台线程）。"""

    def __init__(self, on_trigger: Callable[[str, str, str], None],
                 auth_token: str = "", port: int = DEFAULT_PORT):
        self._on_trigger = on_trigger
        # 安全修复：空 token 时自动生成随机 token，避免裸奔
        if not auth_token:
            self._auth_token = secrets.token_hex(16)
            logger.info("ExternalTriggerReceiver: auth_token 未配置，已自动生成随机 token（请记录到 config 或环境变量）")
        else:
            self._auth_token = auth_token
        self._port = int(port or DEFAULT_PORT)
        self._httpd: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        """后台线程启动 HTTP 服务。返回是否成功。"""
        if self._httpd is not None:
            return True
        try:
            handler = _make_handler(self._on_trigger, self._auth_token)
            self._httpd = HTTPServer(('127.0.0.1', self._port), handler)
            self._thread = threading.Thread(
                target=self._httpd.serve_forever,
                name="external-trigger", daemon=True,
            )
            self._thread.start()
            logger.info("外部触发接收器已启动: 127.0.0.1:%d", self._port)
            return True
        except Exception as e:
            logger.warning("外部触发接收器启动失败（非致命）: %s", e)
            self._httpd = None
            return False

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                logger.debug("external_trigger_receiver: 非致命异常(已静默吞掉)", exc_info=True)
            self._httpd = None
