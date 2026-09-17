# -*- coding: utf-8 -*-
"""信号契约安全绳 —— `_init_*` 搬家重构的护栏。

## 为什么需要它

`PetWindow` 由 8 个 mixin 拼成，但所有接线动作集中在 `pet.py` 的 20 个
`_init_*` 方法里。计划是把这些方法搬进对应的 mixin（技术债①）。

**纯搬家重构的最大风险不是代码写错，而是搬漏一条信号连接**——那会让
某个回调静默失效：不报错、不崩溃、只是那个功能再也不响应。
现有测试抓不到这类问题（它们不检查「连接是否存在」）。

所以本文件先于重构存在：**把当前完整契约钉死**，搬家后逐条比对。

## 契约内容

- 18 个 Signal 声明（名称 + 参数签名）
- 所有 `signal.connect(slot)` 对（信号名 → 槽名）
- 20 个 `_init_*` 方法存在且可在 PetWindow 上调用

## 设计取舍

不实例化 PetWindow（需要 Qt + 模型 + 网络，太重且脆弱）。
改为**源码级断言**：解析 pet.py 的文本，比对结构。
搬家后 pet.py 变短，但这些结构必须仍然成立（信号可以定义在 mixin 里，
只要 PetWindow 能通过 MRO 访问到，且 connect 调用仍在）。

若搬家把 Signal 声明挪进 mixin，本测试的 `_signal_declarations` 需要
同时扫 pet_mixins/ —— 这是**预期的调整点**，不是失败。
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _pet_sources() -> str:
    """pet.py + 所有 pet_mixins/*.py 的合并文本。

    搬家后信号声明与 connect 可能出现在 mixin 里，
    合并扫描才能覆盖两种布局。
    """
    parts = [_read("pet.py")]
    mixin_dir = os.path.join(ROOT, "pet_mixins")
    if os.path.isdir(mixin_dir):
        for fn in sorted(os.listdir(mixin_dir)):
            if fn.endswith(".py"):
                parts.append(open(os.path.join(mixin_dir, fn), encoding="utf-8").read())
    return "\n".join(parts)


# ── 当前契约快照（重构前后必须一致）──────────────────────────────────────────

# 18 个 Signal：名称 → 参数签名
EXPECTED_SIGNALS = {
    "engine_reply_signal": "str, str, str, str, object",
    "engine_status_signal": "str",
    "engine_chunk_signal": "str, str, str, int",
    "voice_status_signal": "str",
    "tts_stop_signal": "",
    "tts_audio_signal": "str",
    "tts_stream_signal": "str, object, int",
    "chat_state_signal": "str",
    "screen_emotion_signal": "str, float",
    "screen_proactive_signal": "str",
    "screen_update_signal": "str",
    "hanako_state_signal": "str, str, str, str, str",
    "idle_chatter_signal": "str, str",
    "tool_progress_signal": "str, str, str, object",
    "bubble_signal": "str, str, int",
    "tts_celebration_signal": "str",
    "pet_set_mode_signal": "str",
    "focus_ui_signal": "bool, float, object",
}

# 跨线程信号 → 槽（这些是「后台线程绕回主线程」的生命线，最不能丢）
CRITICAL_CONNECTIONS = {
    "engine_reply_signal": "_do_engine_reply",
    "engine_status_signal": "_do_engine_status",
    "engine_chunk_signal": "_do_engine_chunk",
    "voice_status_signal": "_do_voice_status",
    "tts_stop_signal": "_do_tts_stop",
    "tts_audio_signal": "_do_play_tts_audio",
    "tts_stream_signal": "_do_engine_tts_stream",
    "chat_state_signal": "_do_chat_state",
    "screen_emotion_signal": "_do_screen_emotion",
    "screen_proactive_signal": "_do_screen_proactive",
    "screen_update_signal": "_do_screen_update",
    "hanako_state_signal": "_do_hanako_state",
    "tts_celebration_signal": "_do_tts_celebration",
    "pet_set_mode_signal": "_do_pet_set_mode",
    "bubble_signal": "_show_bubble_impl",
    "tool_progress_signal": "_do_tool_progress",
}

# 20 个 _init_* 方法（搬家后仍须存在，可由 mixin 提供）
EXPECTED_INIT_METHODS = [
    "_init_diag_switches",
    "_init_states",
    "_init_schedulers",
    "_init_interaction",
    "_init_engine",
    "_init_voice_audio",
    "_init_visual_startup",
    "_init_neko_t05",
    "_init_neko_panels",
    "_init_neko_p1",
    "_init_p1_anti_repeat",
    "_init_p1_screen_enrich",
    "_init_p1_fact_store",
    "_init_p1_reflection",
    "_init_p1_embedding_check",
    "_init_multi_pet_greeting",
    "_init_companion_memory",
    "_init_status_http",
    "_init_external_trigger",
    "_init_mcp_server",
]


# ── 一、Signal 声明完整 ─────────────────────────────────────────────────────


def test_all_signals_declared():
    """18 个 Signal 全部声明，且参数签名不变。

    签名变化是静默杀手：`Signal(str)` 改成 `Signal(object)` 仍能 emit，
    但跨线程队列连接的参数解包会出错。
    """
    src = _pet_sources()
    found = {
        name: args.strip()
        for name, args in re.findall(r"^\s*(\w+)\s*=\s*Signal\(([^)]*)\)", src, re.M)
    }
    missing = [k for k in EXPECTED_SIGNALS if k not in found]
    assert not missing, f"缺失 Signal 声明: {missing}"

    mismatched = {
        k: (EXPECTED_SIGNALS[k], found[k])
        for k in EXPECTED_SIGNALS
        if found[k] != EXPECTED_SIGNALS[k]
    }
    assert not mismatched, (
        f"Signal 签名变化（预期 → 实得）: {mismatched}"
    )


# ── 二、关键连接一条不少 ────────────────────────────────────────────────────


def test_critical_signal_connections_present():
    """跨线程信号 → 槽的连接必须全部存在。

    这是本测试最重要的断言：搬家漏掉一条，对应功能静默失效
    （不报错、不崩溃，只是再也不响应）。
    """
    src = _pet_sources()
    # 收集所有 `xxx.connect(yyy)`，取信号末段与槽末段
    conns = {}
    for sig, slot in re.findall(r"([\w\.]+)\.connect\(\s*([\w\.]+)", src):
        conns[sig.split(".")[-1]] = slot.split(".")[-1]

    missing = []
    for sig, slot in CRITICAL_CONNECTIONS.items():
        if sig not in conns:
            missing.append(f"{sig} 未连接任何槽")
        elif conns[sig] != slot:
            missing.append(f"{sig} 连到了 {conns[sig]}，预期 {slot}")
    assert not missing, "关键信号连接缺失/错位:\n  " + "\n  ".join(missing)


def test_slot_methods_exist():
    """所有被连接的槽方法必须在代码里有定义。"""
    src = _pet_sources()
    missing = []
    for slot in CRITICAL_CONNECTIONS.values():
        # 槽定义形式：def _do_xxx(self, ...)
        if not re.search(rf"def {re.escape(slot)}\(", src):
            missing.append(slot)
    assert not missing, f"槽方法未定义（连接会静默失效）: {missing}"


# ── 三、_init_* 方法齐全 ────────────────────────────────────────────────────


def test_all_init_methods_defined():
    """20 个 _init_* 方法全部有定义（可由 mixin 提供）。"""
    src = _pet_sources()
    missing = [
        m for m in EXPECTED_INIT_METHODS
        if not re.search(rf"def {re.escape(m)}\(self", src)
    ]
    assert not missing, f"缺失 _init_* 方法: {missing}"


def test_init_methods_are_callable_on_petwindow():
    """所有 _init_* 必须能通过 PetWindow 的 MRO 访问到。

    搬家后方法定义在 mixin 里——只要能通过 MRO 拿到就合格。
    用 `hasattr` 而非实例化，避免拖起 Qt + 模型加载。
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from pet import PetWindow

    missing = [m for m in EXPECTED_INIT_METHODS if not hasattr(PetWindow, m)]
    assert not missing, f"PetWindow 上访问不到: {missing}"


def _init_call_order() -> list:
    """__init__ 主干里 `_init_*` 的调用顺序（实测，非假设）。"""
    src = _read("pet.py")
    i = src.find("class PetWindow")
    init_start = src.find("def __init__", i)
    nxt = re.search(r"\n    def (?!__init__)", src[init_start + 10 :])
    init_end = init_start + 10 + (nxt.start() if nxt else len(src))
    seg = src[init_start:init_end]
    return re.findall(r"self\.(_init_[a-z0-9_]+)\(\)", seg)


# 实测快照（2026-09-17）。
#
# 关键事实：`__init__` 只有 37 行，**只直接调 10 个** `_init_*`。
# 其余 10 个是嵌套调用（如 _init_neko_p1 内部调 p1_* 系列），
# 不在 __init__ 文本里。本快照只记录 __init__ 的直接调用。
#
# 顺序有语义：_init_states 必须先于 _init_engine（引擎依赖状态字段）；
# _init_visual_startup 依赖渲染器已就绪。
EXPECTED_INIT_ORDER = [
    "_init_diag_switches",
    "_init_states",
    "_init_schedulers",
    "_init_interaction",
    "_init_engine",
    "_init_voice_audio",
    "_init_mcp_server",
    "_init_visual_startup",
    "_init_neko_t05",
    "_init_play_layer",
]

# 嵌套调用的 10 个（由其它 _init_* 内部调用）
EXPECTED_NESTED_INIT_METHODS = [
    "_init_companion_memory",
    "_init_multi_pet_greeting",
    "_init_neko_panels",
    "_init_neko_p1",
    "_init_p1_anti_repeat",
    "_init_p1_screen_enrich",
    "_init_p1_fact_store",
    "_init_p1_reflection",
    "_init_p1_embedding_check",
    "_init_status_http",
    "_init_external_trigger",
]


def test_init_call_order_preserved():
    """__init__ 里 _init_* 的直接调用顺序不得改变。"""
    actual = _init_call_order()
    assert actual == EXPECTED_INIT_ORDER, (
        f"__init__ 调用顺序改变:\n  预期 {EXPECTED_INIT_ORDER}\n  实得 {actual}"
    )


def test_nested_init_methods_exist():
    """嵌套调用的 _init_* 也必须存在（否则某条链路整段失效）。"""
    src = _pet_sources()
    missing = [
        m for m in EXPECTED_NESTED_INIT_METHODS
        if not re.search(rf"def {re.escape(m)}\(self", src)
    ]
    assert not missing, f"缺失嵌套 _init_* 方法: {missing}"


# ── 四、防止回退 ────────────────────────────────────────────────────────────


def test_pet_py_does_not_regrow():
    """pet.py 不得继续膨胀（搬家是减法，不是加法）。

    当前 3839 行。本断言是软上限：允许 ±50 行浮动，
    超过说明新增接线又堆回了 pet.py。
    """
    n = len(_read("pet.py").splitlines())
    assert n < 3900, (
        f"pet.py 已 {n} 行（基线 3839）。新增接线应放进对应 mixin，"
        f"而不是继续堆进 pet.py。"
    )
