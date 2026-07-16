"""手机活动 HTTP 接收器

接收 MacroDroid 上报的手机前台应用切换事件。
用 Python 标准库实现，不需要 Flask/FastAPI。

配置（.env）：
    PHONE_RECEIVER_PORT=8077
    PHONE_AUTH_TOKEN=your-secret-token

接口：
    POST /phone/activity
    Header: X-Auth-Token: <token>
    Body: {"app": "小红书", "event": "switch"}
"""
from __future__ import annotations

import json
import logging
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .phone_activity import PhoneActivityPerception

logger = logging.getLogger(__name__)


def _make_handler(perception: 'PhoneActivityPerception', auth_token: str):
    """动态创建请求处理器，绑定 perception 实例和 auth_token"""

    class Handler(BaseHTTPRequestHandler):
        """处理 MacroDroid 上报的手机活动"""

        def _check_auth(self) -> bool:
            token = self.headers.get('X-Auth-Token', '')
            if not auth_token:
                return True  # 未配置 token 则跳过验证
            return token == auth_token

        def _send_json(self, code: int, data: dict):
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            # 路由匹配
            if self.path != '/phone/activity':
                self._send_json(404, {'ok': False, 'error': 'Not found'})
                return

            # 认证
            if not self._check_auth():
                self._send_json(401, {'ok': False, 'error': 'Unauthorized'})
                return

            # 读取 body
            try:
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length) if length > 0 else b'{}'
                data = json.loads(raw)
            except Exception as e:
                self._send_json(400, {'ok': False, 'error': f'Invalid JSON: {e}'})
                return

            # 提取字段
            app_name = data.get('app', '').strip()
            event = data.get('event', 'switch').strip()

            if not app_name:
                self._send_json(400, {'ok': False, 'error': 'Missing "app" field'})
                return

            # 写入感知层
            try:
                perception.add_activity(app_name, event)
                logger.info("Phone activity: app=%s event=%s", app_name, event)
                self._send_json(200, {'ok': True, 'app': app_name, 'event': event})
            except Exception as e:
                logger.warning("Phone activity error: %s", e)
                self._send_json(500, {'ok': False, 'error': str(e)})

        def do_GET(self):
            """健康检查"""
            if self.path == '/phone/health':
                self._send_json(200, {'ok': True, 'service': 'phone-receiver'})
            else:
                self._send_json(404, {'ok': False, 'error': 'Not found'})

        def log_message(self, fmt, *args):
            """抑制默认 stderr 日志，用 logger 代替"""
            logger.debug(fmt, *args)

    return Handler


class PhoneActivityReceiver:
    """手机活动 HTTP 接收器

    用法：
        from phone_activity import PhoneActivityPerception
        perception = PhoneActivityPerception()
        receiver = PhoneActivityReceiver(perception, auth_token="xxx")
        receiver.start()  # 后台线程启动
        # ... 主程序运行 ...
        receiver.stop()
    """

    def __init__(self, perception: 'PhoneActivityPerception', auth_token: str = ''):
        self._perception = perception
        self._auth_token = auth_token
        self._port = int(os.environ.get('PHONE_RECEIVER_PORT', '8077'))
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        """在后台线程启动 HTTP 服务"""
        handler_cls = _make_handler(self._perception, self._auth_token)
        self._server = HTTPServer(('127.0.0.1', self._port), handler_cls)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info("PhoneActivityReceiver started on port %d", self._port)

    def stop(self):
        """停止 HTTP 服务"""
        if self._server:
            self._server.shutdown()
            self._server = None
            logger.info("PhoneActivityReceiver stopped")

    @property
    def port(self) -> int:
        return self._port
