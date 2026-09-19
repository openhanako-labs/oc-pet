"""本地状态口（F）— 复用 phone_receiver.py 范式的 HTTP 服务

任意外部程序（构建脚本/游戏/自动化/另一只桌宠）可查询桌宠状态；
写仅白名单、经事件总线转发、不直连渲染线程（硬约束 3）。

接口：
    GET  /pet/state      -> {"state": <6态>, "emotion": ..., "anim": ...,
                             "scenario": ..., "agent_id": ..., "renderer_format": ...,
                             "celebrating_active": bool, "ts": ...}
    GET  /pet/health     -> {"ok": True}
    POST /pet/set-mode   -> body {"mode": "celebrating"}；白名单校验
                             → EventBus.emit("pet_set_mode", mode=...)
                             → 200；白名单外 → 400；未开启写 → 403

安全/线程模型（与 PhoneActivityReceiver 一致）：
- 127.0.0.1 监听，可选 X-Auth-Token
- 写路径只 `EventBus.emit` 事件；PetWindow 订阅后经 Qt 信号转主线程再驱动，
  绝不直连渲染线程（EventBus.emit 在 HTTP 线程同步执行，但 handler 只做
  状态登记/信号发射，Qt 对象操作由信号转到主线程）。
- 默认关：config `state_http.enabled=False` 时不启动，零行为、不占端口。
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Callable
from urllib.parse import parse_qs, urlparse

from core.event_bus import EventBus

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8977

# 浏览器访问 /pet/memory 时返回的静态检视页（不含数据；数据端点仍需 X-Auth-Token）。
_MEMORY_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>桌宠记忆检视</title>
<style>
body{font:14px/1.5 system-ui,sans-serif;margin:1rem;background:#0f1115;color:#d8dee9}
h3{margin:.2rem 0 .8rem}
input,button{font:inherit;padding:.3rem .5rem;background:#1b1f27;color:#d8dee9;border:1px solid #333;border-radius:4px}
button{cursor:pointer}
pre{background:#1b1f27;padding:.6rem;border-radius:6px;overflow:auto;max-height:36vh;white-space:pre-wrap}
.row{display:flex;gap:.5rem;margin:.5rem 0;flex-wrap:wrap;align-items:center}
</style></head><body>
<h3>桌宠记忆检视</h3>
<div class="row">
  <input id="tok" placeholder="X-Auth-Token" size="30">
  <input id="q" placeholder="查询（空=全部）" size="18">
  <button onclick="load()">加载</button>
  <button onclick="recall()">召回</button>
</div>
<div class="row"><b>事实</b></div><pre id="facts">—</pre>
<div class="row"><b>场景</b></div><pre id="scenes">—</pre>
<div class="row"><b>召回命中</b></div><pre id="hits">—</pre>
<script>
var K='pet_mem_token';
document.getElementById('tok').value=localStorage.getItem(K)||'';
async function load(){
  var tok=document.getElementById('tok').value.trim();
  localStorage.setItem(K,tok);
  var q=document.getElementById('q').value.trim();
  var r=await fetch('/pet/memory'+(q?('?q='+encodeURIComponent(q)):''),
    {headers:{'X-Auth-Token':tok,'Accept':'application/json'}});
  var d={};try{d=await r.json();}catch(e){}
  document.getElementById('facts').textContent=r.ok?JSON.stringify(d.facts,null,2):('HTTP '+r.status+' '+JSON.stringify(d));
  document.getElementById('scenes').textContent=r.ok?JSON.stringify(d.scenes,null,2):'';
}
async function recall(){
  var tok=document.getElementById('tok').value.trim();
  localStorage.setItem(K,tok);
  var q=document.getElementById('q').value.trim();
  if(!q){document.getElementById('hits').textContent='请先填查询词';return;}
  var r=await fetch('/pet/memory/recall?q='+encodeURIComponent(q),
    {headers:{'X-Auth-Token':tok,'Accept':'application/json'}});
  var d={};try{d=await r.json();}catch(e){}
  document.getElementById('hits').textContent=r.ok?JSON.stringify(d.hits,null,2):('HTTP '+r.status+' '+JSON.stringify(d));
}
</script></body></html>"""

# POST /pet/set-mode 白名单（写，只写不读渲染线程）
SET_MODE_WHITELIST: tuple[str, ...] = (
    "celebrating", "idle", "working", "review", "waiting", "failed",
    "happy", "thinking", "sad", "surprised",
)


def _make_handler(state_provider: Callable[[], dict], auth_token: str,
                  allow_set_mode: bool, memory_provider: Callable[[], dict] | None = None,
                  memory_recall_provider: Callable[..., dict] | None = None):
    """动态创建请求处理器，绑定状态提供器 / auth_token / 写开关"""

    class Handler(BaseHTTPRequestHandler):
        """处理桌宠状态查询与受控写请求"""

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

        def _send_html(self, code: int, html: str):
            body = html.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == '/pet/health':
                self._send_json(200, {'ok': True, 'service': 'pet-status'})
                return
            if self.path == '/pet/state':
                if not self._check_auth():
                    self._send_json(401, {'ok': False, 'error': 'Unauthorized'})
                    return
                try:
                    snapshot = state_provider() if callable(state_provider) else {}
                    if not isinstance(snapshot, dict):
                        snapshot = {}
                    snapshot.setdefault('ok', True)
                    self._send_json(200, snapshot)
                except Exception as e:
                    logger.warning("GET /pet/state error: %s", e)
                    self._send_json(500, {'ok': False, 'error': str(e)})
                return
            if self.path.split('?', 1)[0] == '/pet/memory':
                # 浏览器直接打开 → 返回静态检视页（需自己填 token；数据端点仍鉴权）
                if 'text/html' in (self.headers.get('Accept', '') or ''):
                    self._send_html(200, _MEMORY_HTML)
                    return
                if not self._check_auth():
                    self._send_json(401, {'ok': False, 'error': 'Unauthorized'})
                    return
                try:
                    _q = (parse_qs(urlparse(self.path).query).get('q', ['']) or [''])[0].strip()
                    if callable(memory_provider):
                        try:
                            data = memory_provider(_q)
                        except TypeError:
                            data = memory_provider()  # 兼容无参 provider
                    else:
                        data = {'ok': False, 'error': 'memory provider 未配置'}
                    if not isinstance(data, dict):
                        data = {'ok': False, 'error': 'memory provider 返回非 dict'}
                    data.setdefault('ok', True)
                    self._send_json(200, data)
                except Exception as e:
                    logger.warning("GET /pet/memory error: %s", e)
                    self._send_json(500, {'ok': False, 'error': str(e)})
                return
            if self.path.split('?', 1)[0] == '/pet/memory/recall':
                if not self._check_auth():
                    self._send_json(401, {'ok': False, 'error': 'Unauthorized'})
                    return
                try:
                    _q = (parse_qs(urlparse(self.path).query).get('q', ['']) or [''])[0].strip()
                    if callable(memory_recall_provider):
                        data = memory_recall_provider(_q)
                    else:
                        data = {'ok': False, 'error': 'recall provider 未配置'}
                    if not isinstance(data, dict):
                        data = {'ok': False, 'error': 'recall provider 返回非 dict'}
                    data.setdefault('ok', True)
                    self._send_json(200, data)
                except Exception as e:
                    logger.warning("GET /pet/memory/recall error: %s", e)
                    self._send_json(500, {'ok': False, 'error': str(e)})
                return
            self._send_json(404, {'ok': False, 'error': 'Not found'})

        def do_POST(self):
            # 路由匹配
            if self.path != '/pet/set-mode':
                self._send_json(404, {'ok': False, 'error': 'Not found'})
                return
            # 认证
            if not self._check_auth():
                self._send_json(401, {'ok': False, 'error': 'Unauthorized'})
                return
            # 写开关（默认关）
            if not allow_set_mode:
                self._send_json(403, {'ok': False, 'error': 'Set-mode disabled'})
                return
            # 读取 body
            try:
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length) if length > 0 else b'{}'
                data = json.loads(raw)
            except Exception as e:
                self._send_json(400, {'ok': False, 'error': f'Invalid JSON: {e}'})
                return
            mode = str(data.get('mode', '')).strip()
            # 白名单校验
            if mode not in SET_MODE_WHITELIST:
                self._send_json(400, {
                    'ok': False, 'error': f'Invalid mode: {mode}',
                    'whitelist': list(SET_MODE_WHITELIST),
                })
                return
            # 经事件总线转发（不直连渲染线程）
            try:
                EventBus.emit('pet_set_mode', mode=mode)
                logger.info("F set-mode: %s", mode)
                self._send_json(200, {'ok': True, 'mode': mode})
            except Exception as e:
                logger.warning("F set-mode emit error: %s", e)
                self._send_json(500, {'ok': False, 'error': str(e)})

        def log_message(self, fmt, *args):
            """抑制默认 stderr 日志，用 logger 代替"""
            logger.debug(fmt, *args)

    return Handler


class PetStatusHTTPServer:
    """桌宠本地状态 HTTP 服务（守护线程，复用 PhoneActivityReceiver 模式）

    用法：
        server = PetStatusHTTPServer(state_provider=self._status_snapshot,
                                     auth_token="", port=8977)
        server.start()   # 后台守护线程
        ...
        server.stop()
    """

    def __init__(self, state_provider: Callable[[], dict], auth_token: str = "",
                 port: int = DEFAULT_PORT, allow_set_mode: bool = False,
                 memory_provider: Callable[[], dict] | None = None,
                 memory_recall_provider: Callable[..., dict] | None = None):
        """
        Args:
            state_provider: 返回状态快照 dict 的可调用对象（PetWindow._status_snapshot）
            auth_token: X-Auth-Token；空则自动生成随机 token（安全修复）
            port: 监听端口（默认 8977）
            allow_set_mode: 是否允许 POST /pet/set-mode（默认 False=只读）
        """
        self._state_provider = state_provider
        # 安全修复：空 token 时自动生成随机 token，避免裸奔
        if not auth_token:
            self._auth_token = secrets.token_hex(16)
            logger.info("PetStatusHTTPServer: auth_token 未配置，已自动生成随机 token（请记录到 config 或环境变量）")
        else:
            self._auth_token = auth_token
        self._port = int(port or DEFAULT_PORT)
        self._allow_set_mode = bool(allow_set_mode)
        self._memory_provider = memory_provider
        self._memory_recall_provider = memory_recall_provider
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> None:
        """在后台守护线程启动 HTTP 服务"""
        try:
            handler_cls = _make_handler(self._state_provider, self._auth_token,
                                        self._allow_set_mode, self._memory_provider,
                                        self._memory_recall_provider)
            self._server = HTTPServer(('127.0.0.1', self._port), handler_cls)
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            logger.info("PetStatusHTTPServer started on port %d (set_mode=%s)",
                        self._port, self._allow_set_mode)
        except Exception as e:
            logger.warning("PetStatusHTTPServer start failed: %s", e)
            self._server = None
            self._thread = None

    def stop(self) -> None:
        """停止 HTTP 服务（shutdown 停循环 + server_close 释放 socket）"""
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception as e:
                logger.debug("PetStatusHTTPServer shutdown: %s", e)
            try:
                self._server.server_close()
            except Exception as e:
                logger.warning("PetStatusHTTPServer server_close failed: %s", e)
            self._server = None
            self._thread = None
            logger.info("PetStatusHTTPServer stopped")


__all__ = ["PetStatusHTTPServer", "SET_MODE_WHITELIST", "DEFAULT_PORT"]
