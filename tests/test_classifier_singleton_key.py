# -*- coding: utf-8 -*-
"""情绪分类器单例缓存键回归测试（2026-09-20 真实 bug）。

## 背景：单例只按 name 缓存，extra_corpus 被忽略

原实现：

    _INSTANCES.get(name)   # ← 只认 name

于是同一视角的两种变体**互相踩**：

    get_classifier('pet')             ← 无桌宠语料（1400 条）
    get_classifier('pet', extra=...)  ← 有桌宠语料（1474 条）

谁先调谁定义内存实例。真机表现：一次误用（不带 extra）后，
`emotion_corpus_vecs_pet.json` 被覆盖成 1400 条，真正的 pet 路径
下次发现 labels 不匹配 → 重建 → **实测 154 秒**（卡在 embedding API
超时重试）。

正确性其实是安全的（labels 校验会触发重建，不会用错结果），
但性能被反复打穿。

## 修法

缓存键从 `name` 改成 `(name, extra_corpus 指纹)`。
指纹用 (类别 → 条数) 排序元组——便宜且足以区分 base/pet 两种变体。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.emotion_classifier import (  # noqa: E402
    _INSTANCES,
    _extra_fingerprint,
    get_classifier,
)


@pytest.fixture(autouse=True)
def _clear_instances():
    """每个测试前后清空单例表，避免互相污染。"""
    _INSTANCES.clear()
    yield
    _INSTANCES.clear()


# ── 1. 指纹 ──


def test_fingerprint_none_is_empty_tuple():
    assert _extra_fingerprint(None) == ()
    assert _extra_fingerprint({}) == ()


def test_fingerprint_encodes_structure():
    fp = _extra_fingerprint({"happy": ["a", "b"], "sad": ["c"]})
    assert fp == (("happy", 2), ("sad", 1))


def test_fingerprint_order_independent():
    a = _extra_fingerprint({"happy": ["a"], "sad": ["b"]})
    b = _extra_fingerprint({"sad": ["b"], "happy": ["a"]})
    assert a == b


def test_fingerprint_differs_on_count_change():
    a = _extra_fingerprint({"happy": ["a"]})
    b = _extra_fingerprint({"happy": ["a", "b"]})
    assert a != b


# ── 2. 单例键（核心回归）──


def test_same_name_different_extra_are_distinct():
    """★ 核心：同 name、不同 extra 必须是不同实例。

    修复前：`a is b` 为 True，b 的桌宠语料被静默丢弃。
    """
    a = get_classifier("pet")
    b = get_classifier("pet", extra_corpus={"happy": ["你好呀"]})
    assert a is not b, "不同 extra 应得到不同实例"
    assert not (getattr(a, "_extra_corpus", {}) or {}), "无 extra 实例不该有桌宠语料"
    assert getattr(b, "_extra_corpus", {}), "带 extra 实例应保留桌宠语料"


def test_same_name_same_extra_is_cached():
    """同 name、同 extra 应复用（单例的意义）。"""
    extra = {"happy": ["你好呀"]}
    a = get_classifier("pet", extra_corpus=extra)
    b = get_classifier("pet", extra_corpus=extra)
    assert a is b


def test_different_name_distinct():
    assert get_classifier("user") is not get_classifier("pet")


def test_base_variant_not_polluted_by_pet_variant():
    """先建 pet 变体，再取无 extra 变体，后者不能带上桌宠语料。"""
    get_classifier("pet", extra_corpus={"happy": ["桌宠语料"]})
    base = get_classifier("pet")
    assert not (getattr(base, "_extra_corpus", {}) or {}), (
        "base 变体被 pet 变体污染")


def test_pet_variant_not_polluted_by_base():
    """先建 base 变体，再取 pet 变体，后者必须带桌宠语料。"""
    get_classifier("pet")
    pet = get_classifier("pet", extra_corpus={"concerned": ["早点睡吧"]})
    assert getattr(pet, "_extra_corpus", {}), "pet 变体的桌宠语料被 base 丢弃"


# ── 3. 真实语料路径（不打网络：只验实例装配，不 prepare）──


def test_real_pet_corpus_attached():
    """真实 `_load_pet_corpus()` 应被挂到 pet 变体上。"""
    from pet_mixins.emotion_classify_mixin import _load_pet_corpus

    extra = _load_pet_corpus()
    pet = get_classifier("pet", extra_corpus=extra)
    attached = getattr(pet, "_extra_corpus", {}) or {}
    assert len(attached) == len(extra) > 0, "桌宠语料未正确挂载"
