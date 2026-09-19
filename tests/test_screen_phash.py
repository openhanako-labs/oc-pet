# -*- coding: utf-8 -*-
"""O1-P1：屏幕帧感知哈希（dHash）近邻去重。

背景：``screen.py`` 原变化检测是 ``md5(img.tobytes())`` 整帧精确哈希——
光标闪一下、时钟跳一秒就失效，照打一次视觉 API（正对 73% LLM 时间 + 429）。
本文件覆盖：dHash 稳定性 / 微小改动鲁棒 / 不可用回退 / 跳过不更新基准哈希
（避免缓慢渐变无限跳过）/ 阈值钳位。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw  # noqa: E402

from core.perception.screen import ScreenPerception, dhash_frame, hamming  # noqa: E402


def test_dhash_stable_for_identical_images():
    a = Image.new("L", (128, 128), 100)
    b = Image.new("L", (128, 128), 100)
    assert dhash_frame(a) == dhash_frame(b)
    assert hamming(dhash_frame(a), dhash_frame(b)) == 0


def test_dhash_ignores_tiny_patch_change():
    """极小局部改动（≈闪烁光标）→ Hamming 很小，可被阈值挡住。"""
    a = Image.new("L", (128, 128), 128)
    b = a.copy()
    ImageDraw.Draw(b).rectangle([0, 0, 3, 3], fill=255)
    assert hamming(dhash_frame(a), dhash_frame(b)) <= 3


def test_dhash_distinguishes_different_images():
    flat = Image.new("L", (128, 128), 0)
    grad = Image.new("L", (128, 128))
    grad.putdata([(x * 4) % 256 for _y in range(128) for x in range(128)])
    assert hamming(dhash_frame(flat), dhash_frame(grad)) > 8


def test_dhash_unavailable_returns_negative():
    assert dhash_frame(None) == -1
    assert hamming(-1, 0) > 64          # 不可用 → 极大距离（恒放行)


def test_set_phash_threshold_clamps_and_resets():
    s = ScreenPerception()
    s.set_phash_threshold(999)
    assert s._phash_threshold == 64
    s.set_phash_threshold(-3)
    assert s._phash_threshold == 0
    s.set_phash_threshold("bad")
    assert s._phash_threshold == 0
    s._last_phash = 12345
    s.set_phash_threshold(0)
    assert s._last_phash is None


def test_perceptual_skip_keeps_last_analyzed_hash():
    """跳过时**不更新**基准 → 缓慢渐变会累积到超阈值、自然触发分析。"""
    s = ScreenPerception()
    s.set_phash_threshold(4)
    base = 0b0
    assert s._is_perceptually_unchanged(base) is False   # 首帧：不跳，记为已分析
    assert s._last_phash == base
    near = 0b1111                                        # Hamming 4 ≤ 4 → 跳
    assert s._is_perceptually_unchanged(near) is True
    assert s._last_phash == base                         # 基准未被顶掉
    assert s._phash_skips == 1
    far = 0b111111111                                    # Hamming 9 > 4 → 放行并更新
    assert s._is_perceptually_unchanged(far) is False
    assert s._last_phash == far
    assert s._phash_skips == 0


def test_perceptual_skip_disabled_when_threshold_zero():
    s = ScreenPerception()
    s.set_phash_threshold(0)
    assert s._is_perceptually_unchanged(0) is False
    assert s._is_perceptually_unchanged(0) is False      # 关着就永远放行


def test_perceptual_skip_ignores_unavailable_hash():
    s = ScreenPerception()
    s.set_phash_threshold(6)
    assert s._is_perceptually_unchanged(-1) is False     # 算不出来 → 放行
    assert s._last_phash is None
