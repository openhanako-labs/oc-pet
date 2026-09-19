# -*- coding: utf-8 -*-
"""F 本地状态口 GET /pet/memory（记忆检视）单元测试。

覆盖：
1. 带 auth_token 请求 → 200，返回 memory_provider 的内容
2. 无 auth_token → 401（鉴权生效）
3. 未配置 memory_provider → 200 但 ok=False（不崩、不裸奔）
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.status_http_server import PetStatusHTTPServer
from pet_mixins.interface_mixin import InterfaceMixin

PORT = 8891


def _get(port: int, path: str, token: str | None = None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if token:
        req.add_header("X-Auth-Token", token)
    return urllib.request.urlopen(req, timeout=3)


def test_pet_memory_endpoint_authed():
    srv = PetStatusHTTPServer(
        state_provider=lambda: {"state": "idle"},
        auth_token="tok",
        port=PORT,
        memory_provider=lambda: {"ok": True, "facts": [{"id": "f1"}], "scenes": [{"scene_id": "s1"}]},
    )
    srv.start()
    try:
        time.sleep(0.2)
        with _get(PORT, "/pet/memory", "tok") as r:
            data = json.loads(r.read().decode("utf-8"))
        assert data["ok"] is True
        assert data["facts"][0]["id"] == "f1"
        assert data["scenes"][0]["scene_id"] == "s1"
    finally:
        srv.stop()


def test_pet_memory_endpoint_requires_auth():
    srv = PetStatusHTTPServer(
        state_provider=lambda: {"state": "idle"},
        auth_token="tok",
        port=PORT + 1,
        memory_provider=lambda: {"ok": True},
    )
    srv.start()
    try:
        time.sleep(0.2)
        try:
            _get(PORT + 1, "/pet/memory")
            raise AssertionError("无 auth 应返回 401")
        except urllib.error.HTTPError as e:
            assert e.code == 401
    finally:
        srv.stop()


def test_pet_memory_no_provider():
    srv = PetStatusHTTPServer(state_provider=lambda: {}, auth_token="tok", port=PORT + 2)
    srv.start()
    try:
        time.sleep(0.2)
        with _get(PORT + 2, "/pet/memory", "tok") as r:
            data = json.loads(r.read().decode("utf-8"))
        assert data["ok"] is False
    finally:
        srv.stop()


def test_pet_memory_query_passthrough():
    seen = {}

    def provider(q=""):
        seen["q"] = q
        return {"ok": True, "query": q, "facts": [], "scenes": []}

    srv = PetStatusHTTPServer(state_provider=lambda: {}, auth_token="tok",
                              port=PORT + 3, memory_provider=provider)
    srv.start()
    try:
        time.sleep(0.2)
        with _get(PORT + 3, "/pet/memory?q=%E6%B7%B1%E5%A4%9C", "tok") as r:
            data = json.loads(r.read().decode("utf-8"))
        assert data["query"] == "深夜"
        assert seen["q"] == "深夜"
    finally:
        srv.stop()


def test_pet_memory_html_shell():
    srv = PetStatusHTTPServer(state_provider=lambda: {}, auth_token="tok", port=PORT + 4,
                              memory_provider=lambda q="": {"ok": True})
    srv.start()
    try:
        time.sleep(0.2)
        req = urllib.request.Request(f"http://127.0.0.1:{PORT + 4}/pet/memory")
        req.add_header("Accept", "text/html")
        with urllib.request.urlopen(req, timeout=3) as r:
            html = r.read().decode("utf-8")
            assert r.headers.get("Content-Type", "").startswith("text/html")
        assert "桌宠记忆检视" in html
        assert "召回" in html
    finally:
        srv.stop()


def test_pet_memory_recall_route():
    srv = PetStatusHTTPServer(
        state_provider=lambda: {}, auth_token="tok", port=PORT + 5,
        memory_recall_provider=lambda q="": {"ok": True, "query": q,
                                              "hits": [{"id": "f1", "score": 0.5}]},
    )
    srv.start()
    try:
        time.sleep(0.2)
        with _get(PORT + 5, "/pet/memory/recall?q=%E6%B7%B1%E5%A4%9C", "tok") as r:
            data = json.loads(r.read().decode("utf-8"))
        assert data["query"] == "深夜"
        assert data["hits"][0]["id"] == "f1"
        try:
            _get(PORT + 5, "/pet/memory/recall")
            raise AssertionError("无 auth 应返回 401")
        except urllib.error.HTTPError as e:
            assert e.code == 401
    finally:
        srv.stop()


# ── mixin 层（真实 HybridMemoryRecall，用 dataclass 假 store）──────────────

from dataclasses import dataclass  # noqa: E402


@dataclass
class _FakeScene:
    scene_id: str
    label: str
    scenario: str
    category: str
    tags: list
    topics: list


class _FakeFactStore:
    def get_facts(self, limit=100):
        return [{"id": "f1", "text": "喜欢深夜写代码", "topic": "习惯"},
                {"id": "f2", "text": "爱喝美式咖啡", "topic": "饮食"}]

    def search(self, query, limit=10):
        return [f for f in self.get_facts() if query in f["text"]][:limit]


class _FakeSceneMemory:
    @property
    def scenes(self):
        return [_FakeScene("s1", "深夜加班", "写代码到凌晨", "工作", ["深夜"], []),
                _FakeScene("s2", "中午吃饭", "食堂排队", "日常", [], [])]


class _Dummy(InterfaceMixin):
    def __init__(self):
        self._fact_store = _FakeFactStore()
        self._scene_memory = _FakeSceneMemory()
        self._last_reply = {}


def test_memory_snapshot_full():
    snap = _Dummy()._memory_snapshot()
    assert snap["ok"] is True
    assert {f["id"] for f in snap["facts"]} == {"f1", "f2"}
    assert {s["scene_id"] for s in snap["scenes"]} == {"s1", "s2"}
    assert "scenes_error" not in snap and "facts_error" not in snap


def test_memory_snapshot_query_filter():
    d = _Dummy()
    assert [f["id"] for f in d._memory_snapshot("美式")["facts"]] == ["f2"]
    assert [s["scene_id"] for s in d._memory_snapshot("深夜")["scenes"]] == ["s1"]


def test_memory_recall_ranks_hits():
    res = _Dummy()._memory_recall("深夜写代码")
    assert res["ok"] is True
    assert res["pool_size"] == 4
    ids = [h["id"] for h in res["hits"]]
    assert ids[0] == "fact:f1"
    assert "scene:s1" in ids
    assert all(isinstance(h["score"], float) for h in res["hits"])


def test_memory_recall_rejects_empty_query():
    assert _Dummy()._memory_recall("")["ok"] is False
