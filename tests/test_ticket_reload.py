"""接线测试：WS ticket 403 时重读凭证

2026-09-10 实测问题（今晚真实发生）：

    22:31:19  server-info.json 被改写（Hanako server 重启，token 换了）
    22:31:39  桌宠开始 403 —— ticket request returned HTTP 403
    22:45:58  还在 403（刷了 14 分钟，每分钟重连一次，永不成功）
    22:47:14  用户手动重启桌宠 → 读到新 token → 恢复

根因：桌宠只在**进程启动时**读一次 token（`pet_manager.py:387` →
`HanakoWSClient(cfg["base_url"], cfg["api_token"])`），构造后
`self._token = token.strip()` 再不刷新。

而 `hana` CLI 不受影响——它每次调用都是新进程，每次重读 server-info.json。

修法：403 时重读凭证，**仅在 token 确实变化时**重试一次。
"""
from __future__ import annotations

import types

import pytest

from core.hanako_ws_client import HanakoTicketError, HanakoWSClient


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = payload if payload is not None else {"ticket": "T-1"}

    def json(self):
        return self._payload


class _HTTP:
    """按调用次序返回预设响应，并记录每次请求的 Authorization 头。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.seen_auth = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.seen_auth.append((headers or {}).get("Authorization"))
        return self._responses.pop(0)


def _client(responses, token="OLD"):
    c = HanakoWSClient.__new__(HanakoWSClient)
    c.base_url = "http://127.0.0.1:20099"
    c._token = token
    c.connect_timeout = 5.0
    c._http = _HTTP(responses)
    return c


# ══════════════════════════════════════════════════════════════
#  正常路径不受影响
# ══════════════════════════════════════════════════════════════

def test_success_returns_ticket():
    c = _client([_Resp(200, {"ticket": "T-OK"})])

    assert c._fetch_ticket() == "T-OK"
    assert len(c._http.seen_auth) == 1


def test_non_403_error_does_not_retry():
    """500 之类不属认证问题，不该触发重读。"""
    c = _client([_Resp(500)])

    with pytest.raises(HanakoTicketError) as ei:
        c._fetch_ticket()

    assert "500" in str(ei.value)
    assert len(c._http.seen_auth) == 1, "非 403 不得重试"


# ══════════════════════════════════════════════════════════════
#  核心：403 → 重读凭证 → 重试成功
# ══════════════════════════════════════════════════════════════

def test_403_reloads_token_and_retries(monkeypatch):
    """模拟 server 重启：第一次 403，重读拿到新 token，第二次成功。"""
    c = _client([_Resp(403), _Resp(200, {"ticket": "T-NEW"})])
    monkeypatch.setattr(
        "env_config.get_hanako_config",
        lambda: {"api_token": "NEW"},
    )

    assert c._fetch_ticket() == "T-NEW"
    assert len(c._http.seen_auth) == 2
    assert c._http.seen_auth[0] == "Bearer OLD"
    assert c._http.seen_auth[1] == "Bearer NEW", "重试必须带新凭证"


def test_403_with_unchanged_token_does_not_retry(monkeypatch):
    """凭证没变 → 重试也是白搭，不浪费一次请求。"""
    c = _client([_Resp(403)])
    monkeypatch.setattr(
        "env_config.get_hanako_config",
        lambda: {"api_token": "OLD"},        # 与当前相同
    )

    with pytest.raises(HanakoTicketError) as ei:
        c._fetch_ticket()

    assert "403" in str(ei.value)
    assert len(c._http.seen_auth) == 1, "token 未变时不得重试"


def test_403_twice_still_raises(monkeypatch):
    """换了 token 仍然 403 → 报错，不无限重试。"""
    c = _client([_Resp(403), _Resp(403)])
    monkeypatch.setattr(
        "env_config.get_hanako_config",
        lambda: {"api_token": "NEW"},
    )

    with pytest.raises(HanakoTicketError) as ei:
        c._fetch_ticket()

    assert "403" in str(ei.value)
    assert len(c._http.seen_auth) == 2, "只重试一次"


def test_403_with_unreadable_config_does_not_retry(monkeypatch):
    """读不到凭证 → 不重试，保持原错误。"""
    c = _client([_Resp(403)])

    def _boom():
        raise RuntimeError("server-info.json 不存在")

    monkeypatch.setattr("env_config.get_hanako_config", _boom)

    with pytest.raises(HanakoTicketError):
        c._fetch_ticket()

    assert len(c._http.seen_auth) == 1


def test_403_with_empty_new_token_does_not_retry(monkeypatch):
    c = _client([_Resp(403)])
    monkeypatch.setattr("env_config.get_hanako_config", lambda: {"api_token": ""})

    with pytest.raises(HanakoTicketError):
        c._fetch_ticket()

    assert len(c._http.seen_auth) == 1


# ══════════════════════════════════════════════════════════════
#  token 确实被更新（后续请求都用新的）
# ══════════════════════════════════════════════════════════════

def test_token_is_persisted_after_reload(monkeypatch):
    c = _client([_Resp(403), _Resp(200, {"ticket": "T"})])
    monkeypatch.setattr("env_config.get_hanako_config", lambda: {"api_token": "NEW"})

    c._fetch_ticket()

    assert c._token == "NEW", "重读后必须存下来，否则下次又要 403 一轮"


def test_network_error_message_is_distinct_from_403():
    """403（认证被拒）与 ticket request failed（连不上）含义不同，别混。"""
    import requests

    class _Fail:
        def post(self, *a, **k):
            raise requests.RequestException("connection refused")

    c = _client([])
    c._http = _Fail()

    with pytest.raises(HanakoTicketError) as ei:
        c._fetch_ticket()

    assert "failed" in str(ei.value)
    assert "403" not in str(ei.value)
