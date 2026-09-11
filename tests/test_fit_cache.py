"""窗口贴合缓存（avatar/fit_cache.py）的行为锁定。

背景：`_fit_window_to_model` 的 HitDrawable 扫描实测 **21.8s**，
但输入（模型 + 缩放 + 视口）每次启动都一样 → 除首次外都在重算同一结果。

这里锁住的不变量：
  1. 输入变则键变（少放一项就会拿旧值套新模型——比慢 20 秒更糟）
  2. 值坏则当没缓存（越界/退化/损坏），绝不拿坏值去贴合窗口
  3. 缓存故障不影响启动（读不到、写不进都只是「没命中」）
"""
from __future__ import annotations

import json

import pytest

from avatar import fit_cache


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """每个用例一个独立缓存文件——别碰真实的 data/fit_cache.json。"""
    monkeypatch.setattr(fit_cache, "_CACHE_PATH",
                        str(tmp_path / "fit_cache.json"))


def _key(**kw):
    args = {"model_path": "C:/chars/miku/miku.model3.json",
            "fit_scale": 1.0, "fit_scale_x": 1.22,
            "gl_w": 824, "gl_h": 936}
    args.update(kw)
    return fit_cache.make_key(
        args["model_path"], args["fit_scale"],
        args["fit_scale_x"], args["gl_w"], args["gl_h"])


# ══════════════════════════════════════════════════════════════
#  1. 键的敏感性
# ══════════════════════════════════════════════════════════════

def test_key_is_stable():
    assert _key() == _key()


@pytest.mark.parametrize("field,value", [
    ("model_path", "C:/chars/other/other.model3.json"),
    ("fit_scale", 1.5),
    ("fit_scale_x", 1.0),
    ("gl_w", 825),
    ("gl_h", 937),
])
def test_key_changes_when_any_input_changes(field, value):
    """每一项都必须进键。

    漏掉任何一项，都会在换模型/改缩放/换分辨率后拿旧 bbox 去套 ——
    窗口贴到错的尺寸上，且没人知道为什么。
    """
    assert _key() != _key(**{field: value})


def test_key_is_case_insensitive_on_path():
    """Windows 上同一文件可能以不同大小写出现，不该因此重扫。"""
    a = _key(model_path="C:/Chars/Miku/Miku.model3.json")
    b = _key(model_path="c:/chars/miku/miku.model3.json")
    assert a == b


# ══════════════════════════════════════════════════════════════
#  2. 往返 + 校验
# ══════════════════════════════════════════════════════════════

def test_round_trip():
    k = _key()
    assert fit_cache.load(k, 824, 936) is None, "初始必须无缓存"

    fit_cache.save(k, (12.0, 30.0, 500.0, 900.0))

    assert fit_cache.load(k, 824, 936) == (12.0, 30.0, 500.0, 900.0)


def test_miss_on_unknown_key():
    fit_cache.save(_key(), (12.0, 30.0, 500.0, 900.0))
    other = _key(gl_w=1920, gl_h=1080)
    assert fit_cache.load(other, 1920, 1080) is None


def test_save_does_not_clobber_other_keys():
    """多角色/多分辨率时，键之间不能互相覆盖。"""
    k1, k2 = _key(), _key(gl_w=1920, gl_h=1080)
    fit_cache.save(k1, (1.0, 2.0, 300.0, 400.0))
    fit_cache.save(k2, (5.0, 6.0, 700.0, 800.0))

    assert fit_cache.load(k1, 824, 936) == (1.0, 2.0, 300.0, 400.0)
    assert fit_cache.load(k2, 1920, 1080) == (5.0, 6.0, 700.0, 800.0)


def test_save_creates_missing_dir(tmp_path, monkeypatch):
    nested = tmp_path / "a" / "b" / "fit_cache.json"
    monkeypatch.setattr(fit_cache, "_CACHE_PATH", str(nested))
    fit_cache.save(_key(), (1.0, 2.0, 300.0, 400.0))
    assert nested.exists()


# ── 坏值一律当「没缓存」 ──────────────────────────────────────

def test_rejects_out_of_bounds():
    """越界 bbox 会算出比屏幕还大的窗口。"""
    k = _key()
    fit_cache.save(k, (0.0, 0.0, 5000.0, 900.0))
    assert fit_cache.load(k, 824, 936) is None


def test_rejects_negative():
    k = _key()
    fit_cache.save(k, (-5.0, 0.0, 300.0, 400.0))
    assert fit_cache.load(k, 824, 936) is None


def test_rejects_inverted_rect():
    k = _key()
    fit_cache.save(k, (300.0, 400.0, 100.0, 200.0))
    assert fit_cache.load(k, 824, 936) is None


def test_rejects_degenerate_rect():
    """宽度只有几像素 = 扫描时模型没画出来，不能固化。"""
    k = _key()
    fit_cache.save(k, (100.0, 100.0, 103.0, 400.0))
    assert fit_cache.load(k, 824, 936) is None


def test_rejects_wrong_arity():
    k = _key()
    fit_cache.save(k, (1.0, 2.0, 3.0))  # save 内部会 unpack 失败 → 不写
    assert fit_cache.load(k, 824, 936) is None


# ── 缓存故障不得影响启动 ─────────────────────────────────────

def test_corrupt_file_is_treated_as_empty():
    with open(fit_cache._CACHE_PATH, "w", encoding="utf-8") as f:
        f.write("{ this is not json")

    assert fit_cache.load(_key(), 824, 936) is None
    # 坏文件不该妨碍后续写入（save 会重建）
    fit_cache.save(_key(), (1.0, 2.0, 300.0, 400.0))
    assert fit_cache.load(_key(), 824, 936) == (1.0, 2.0, 300.0, 400.0)


def test_non_dict_json_is_treated_as_empty():
    with open(fit_cache._CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump([1, 2, 3], f)
    assert fit_cache.load(_key(), 824, 936) is None


def test_non_numeric_value_is_rejected():
    with open(fit_cache._CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({_key(): ["a", "b", "c", "d"]}, f)
    assert fit_cache.load(_key(), 824, 936) is None


def test_write_to_unwritable_path_does_not_raise(monkeypatch):
    """写不进去只是「下次还要重扫」，不能把桌宠搞崩。"""
    monkeypatch.setattr(fit_cache, "_CACHE_PATH",
                        "\0invalid/fit_cache.json")
    fit_cache.save(_key(), (1.0, 2.0, 300.0, 400.0))  # 不抛异常即通过


def test_cache_does_not_grow_without_bound():
    for i in range(60):
        fit_cache.save(_key(gl_w=800 + i), (1.0, 2.0, 300.0, 400.0))
    with open(fit_cache._CACHE_PATH, encoding="utf-8") as f:
        assert len(json.load(f)) <= 32


# ══════════════════════════════════════════════════════════════
#  3. 渲染器接线
# ══════════════════════════════════════════════════════════════

def test_renderer_consults_cache_before_scanning():
    """源码级守卫：缓存必须挡在扫描**之前**，否则缓存毫无意义。"""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "avatar" / "live2d_renderer.py").read_text(encoding="utf-8")

    i_load = src.index("fit_cache.load(")
    i_save = src.index("fit_cache.save(")
    assert i_load < i_save, "先查缓存，再扫描，最后才写缓存"


def test_renderer_key_includes_everything_that_changes_bbox():
    """键必须含模型路径 + 两个缩放系数 + 视口尺寸。"""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "avatar" / "live2d_renderer.py").read_text(encoding="utf-8")

    i = src.index("fit_cache.make_key(")
    # 取足够长的一段（不能用第一个 ')' —— getattr(...) 里就有括号）
    call = src[i:i + 300]
    for field in ("_model_path", "_fit_scale", "_fit_scale_x", "gl_w", "gl_h"):
        assert field in call, f"缓存键漏了 {field} —— 会拿旧 bbox 套新输入"
