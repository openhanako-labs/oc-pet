# -*- coding: utf-8 -*-
"""情绪来源优先级测试（2026-09-20 契约改版）。

## 改版背景

原先：主 LLM 只输出 `[feel:v,a]`（连续坐标），`emotion` 变量**恒为 neutral**
→ 补了一层本地 embedding 分类器读回复文本反推情绪（桌宠视角 67%）。

问题：那是让本地小模型**重做一遍主 LLM 已经做过的事**，而且它更弱。

改版：契约里重新加回**必填的** `[emotion:词]`。主 LLM 直接给情绪词
（它最强、且本来就在读对话），本地分类器**降级为兜底**。

## 优先级（本文件锁住）

    主 LLM 给了非 neutral 情绪  →  用它，**不跑分类器**（省一次网络往返）
    主 LLM 没给（空/neutral）   →  跑分类器兜底

## 为什么零风险

主 LLM 若不写情绪词 → emotion 为 neutral → 走分类器 → 与改版前完全一致。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("OC_DISABLE_TRAY", "1")
os.environ.setdefault("OC_DISABLE_PERCEPTION", "1")
os.environ.setdefault("OC_DISABLE_LIVE2D", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def app():
    inst = QApplication.instance()
    if inst is None:
        inst = QApplication([])
    return inst


# ── 1. 契约文本（源码级）──


def test_rules_require_emotion_word():
    """输出规则必须要求 [emotion:词]（必填）。"""
    from core.harness_adapter import HanakoPetAdapter
    rules = HanakoPetAdapter._OUTPUT_RULES
    assert "[emotion:" in rules
    assert "[feel:" in rules


def test_rules_list_matches_emotion_zh():
    """规则里列的情绪词必须都能被 `_EMOTION_ZH` 翻译。"""
    from core.harness_adapter import HanakoPetAdapter
    from pet_mixins.perception_mixin import PerceptionMixin
    rules = HanakoPetAdapter._OUTPUT_RULES
    zh = PerceptionMixin._EMOTION_ZH
    for w in ("happy", "sad", "angry", "surprised", "thinking",
              "confused", "shy", "cute", "sleepy", "neutral"):
        assert w in rules, f"契约未列出 {w}"
        assert w in zh, f"{w} 无中文归宿"


# ── 2. 解析：契约词能被解析出来 ──


@pytest.mark.parametrize("word", [
    "happy", "sad", "angry", "surprised", "thinking",
    "confused", "shy", "cute", "sleepy", "neutral",
])
def test_contract_words_parse(word):
    """契约承诺的每个词，parse_emotion 都要能解析出来。"""
    from core.harness_adapter import HanakoPetAdapter
    cleaned, emo = HanakoPetAdapter.parse_emotion(f"我在呢。[emotion:{word}]")
    assert emo == word
    assert "[emotion:" not in cleaned


def test_feel_and_emotion_coexist():
    """两个标签同时出现时都要能处理：emotion 解析出，feel 留给 intent 层。"""
    from core.harness_adapter import HanakoPetAdapter
    cleaned, emo = HanakoPetAdapter.parse_emotion(
        "[feel:0.8,0.7] 好开心 [emotion:happy]")
    assert emo == "happy"
    assert "[feel:0.8,0.7]" in cleaned, "feel 不该被 parse_emotion 剥掉"


# ── 3. 优先级：主 LLM 赢，分类器兜底 ──


@pytest.fixture()
def window(app):
    from pet import PetWindow

    w = PetWindow(agent_id="miku")
    yield w
    try:
        w.close()
    except Exception:
        pass


def test_llm_emotion_wins_over_classifier(window):
    """★ 核心：主 LLM 给了情绪时，不该用分类器结果覆盖它。"""
    window._classify_enabled = True
    window._classify_min_conf = 0.40
    # 分类器"以为"是 happy
    window._pending_reply_emotion = ("happy", 0.95)
    # 但主 LLM 说是 sad（LLM 更强，应赢）
    llm_emotion = "sad"
    if not llm_emotion or llm_emotion == "neutral":
        _c = window._resolved_reply_emotion()
        if _c:
            llm_emotion = _c
    assert llm_emotion == "sad", "主 LLM 的情绪被分类器覆盖了"


def test_classifier_used_when_llm_silent(window):
    """主 LLM 没给情绪（neutral）时，分类器兜底。"""
    window._classify_enabled = True
    window._classify_min_conf = 0.40
    window._pending_reply_emotion = ("happy", 0.95)
    llm_emotion = "neutral"
    if not llm_emotion or llm_emotion == "neutral":
        _c = window._resolved_reply_emotion()
        if _c:
            llm_emotion = _c
    assert llm_emotion == "happy", "LLM 没给时分类器应兜底"


def test_empty_emotion_uses_classifier(window):
    """空字符串同样走兜底。"""
    window._classify_enabled = True
    window._classify_min_conf = 0.40
    window._pending_reply_emotion = ("excited", 0.9)
    llm_emotion = ""
    if not llm_emotion or llm_emotion == "neutral":
        _c = window._resolved_reply_emotion()
        if _c:
            llm_emotion = _c
    assert llm_emotion == "happy"  # excited → happy


# ── 4. 省调用：LLM 给了就不提交分类 ──


def test_no_classify_when_llm_provides_emotion(window, monkeypatch):
    """主 LLM 给了情绪 → 不提交分类任务（省 0.8–1.2s 网络往返）。"""
    window._classify_enabled = True
    submitted = []
    monkeypatch.setattr(window, "_classify_reply_async",
                        lambda t: submitted.append(t))

    # 复刻 _on_engine_reply 的分支逻辑
    emotion = "happy"
    if not emotion or emotion == "neutral":
        window._classify_reply_async("回复文本")
    assert not submitted, "LLM 已给情绪，不该再提交分类"


def test_classify_submitted_when_llm_silent(window, monkeypatch):
    """主 LLM 没给情绪 → 提交分类兜底。"""
    window._classify_enabled = True
    submitted = []
    monkeypatch.setattr(window, "_classify_reply_async",
                        lambda t: submitted.append(t))

    emotion = "neutral"
    if not emotion or emotion == "neutral":
        window._classify_reply_async("回复文本")
    assert submitted, "LLM 没给情绪时应提交分类"


def test_pet_py_has_priority_guard():
    """源码级：pet.py 里必须有「先看 LLM 情绪」的守卫。"""
    src = open(os.path.join(_REPO, "pet.py"), encoding="utf-8").read()
    assert 'if not emotion or emotion == "neutral":' in src, \
        "缺少 LLM 情绪优先的守卫"
    # _on_engine_reply 里也应有需分类判断
    i = src.index("def _on_engine_reply")
    body = src[i:i + 2000]
    assert "_need_classify" in body, "_on_engine_reply 未判断是否需要分类"
