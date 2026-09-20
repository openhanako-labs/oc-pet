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
    return "\n".join(_all_sources().values())


def _all_sources() -> dict:
    """{相对路径: 源码文本}，含 pet.py 与全部 pet_mixins。

    单独返回映射（而非合并文本）是为了报错时能指出连接在哪个文件。
    """
    out = {"pet.py": _read("pet.py")}
    mixin_dir = os.path.join(ROOT, "pet_mixins")
    if os.path.isdir(mixin_dir):
        for fn in sorted(os.listdir(mixin_dir)):
            if fn.endswith(".py"):
                rel = f"pet_mixins/{fn}"
                out[rel] = _read(rel)
    return out


# ── 当前契约快照（重构前后必须一致）──────────────────────────────────────────

# 18 个 Signal：名称 → 参数签名
EXPECTED_SIGNALS = {
    "engine_reply_signal": "str, str, str, str, object",
    "engine_status_signal": "str, str",
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
    # 下面两条也是 Signal → 槽（非定时器），一并归入关键集
    "idle_chatter_signal": "_do_idle_chatter",
    "focus_ui_signal": "_on_focus_ui_changed",
}

# ── QTimer.timeout → 槽（内部定时器）────────────────────────────────────
#
# 这 16 个定时器驱动桌宠的「心跳」：拖拽轮询 / 统一 tick / 动效 / 气泡超时 /
# 情绪过期 / 休息提醒 / 前台检测 / presence / 鼠标追踪 / 连击重置 /
# 抚摸回退 / 单击延迟 / 帧动画 / 状态气泡节流 / 思考超时。
#
# 搬漏一个的表现：「某个功能不刷新」——不报错、不崩溃，但那个行为永远静止。
# 比跨线程回调好查（行为可观察），但仍属静默失效，故一并钉死。
#
# 键：定时器属性名（去 self. 前缀）；值：槽名（lambda 记为 "<lambda>"）。
EXPECTED_TIMER_CONNECTIONS = {
    "_drag_poll_timer": "_drag_poll_tick",
    "_unified_timer": "_unified_tick",
    "_motion_timer": "<lambda>",
    "_hanako_poll_timer": "_hanako_monitor.tick",
    "_bubble_timer": "_clear_hanako_bubble",
    "_emotion_expiry_timer": "_on_emotion_expired",
    "_break_timer": "_break_check",
    "_foreground_timer": "_foreground_tick",
    "_presence_timer": "_presence_tick",
    "_mouse_tracker_timer": "_mouse_tracker.tick",
    "_pet_combo_timer": "_reset_pet_combo",
    "_pet_revert_timer": "_pet_revert",
    "_click_timer": "_fire_pending_click",
    "_anim_timer": "_anim_tick",
    "_think_timeout": "_on_think_timeout",
}

# ── 控件/外部对象信号 → 槽（UI 交互入口）────────────────────────────
#
# 这些是「用户碰得到的东西」：聊天输入框回车 / 发送按钮 / 托盘菜单 /
# 右键菜单 / 主题切换 / 互动卡片按钮 / 聊天面板提交。
# 搬漏一个的表现：那个交互彻底没反应（用户立刻能发现，但仍是静默的
# ——代码层无报错）。
#
# 键：发射者（去 self. 前缀）；值：(信号名, 槽名) 列表。
# 同一发射者可能有多个信号（如 _chat_panel 有两个）。
EXPECTED_WIDGET_CONNECTIONS = {
    "_chat_panel": [
        ("message_submitted", "_on_chat_panel_submit"),
        ("close_requested", "_close_chat_panel"),
    ],
    "input_field": [("returnPressed", "_send_message")],
    "send_btn": [("clicked", "_send_message")],
    "_tray": [("activated", "_on_tray_activated")],
    "mgr": [("theme_changed", "_refresh_window_theme")],
    "_interaction_card": [
        ("action_clicked", "_on_interaction_card_action"),
        ("dismiss_requested", "<lambda>"),
    ],
    "panel": [("card_action", "_on_interaction_card_action")],
    "win": [
        ("game_finished", "_on_mini_game_finished"),
        ("close_requested", "<lambda>"),
    ],
}

# QAction.triggered → 槽（托盘菜单 / 右键菜单项）。
#
# 这些发射者是局部变量（vis / passthrough / quit_a / a），无法按属性名索引，
# 因此只断言「数量」与「关键项存在」。
# 关键项：托盘菜单的显示/穿透/退出——丢一个用户就少一个入口。
EXPECTED_ACTION_SLOTS = {
    "_toggle_visibility",
    "_toggle_passthrough",
    "close",
}
MIN_ACTION_CONNECTIONS = 4  # 实测 4 条（含 2 个 lambda 右键菜单项）

# customContextMenuRequested → 槽（右键菜单）。
# 与 QAction.triggered 不同：它是「请求菜单」的信号，不是菜单项本身。
EXPECTED_CONTEXT_MENU_SLOT = "_show_context_menu"

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


# 实测快照（2026-09-17，2026-09-20 增 _init_emotion_classifier）。
#
# 关键事实：`__init__` 只有 37 行，**只直接调 10 个** `_init_*`。
# 其余 10 个是嵌套调用（如 _init_neko_p1 内部调 p1_* 系列），
# 不在 __init__ 文本里。本快照只记录 __init__ 的直接调用。
#
# 顺序有语义：_init_states 必须先于 _init_engine（引擎依赖状态字段）；
# _init_visual_startup 依赖渲染器已就绪。
#
# 2026-09-20 新增 `_init_emotion_classifier`（情绪分类层，见
# docs/情绪分类层-2026-09-20.md）。插在 _init_engine 之后：
# 它在对话链路上（回复回调要用），且只依赖 self.config（__init__ 开头已 load），
# 对前后各项均无依赖。
EXPECTED_INIT_ORDER = [
    "_init_diag_switches",
    "_init_states",
    "_init_schedulers",
    "_init_interaction",
    "_init_engine",
    "_init_emotion_classifier",
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


def test_init_emotion_classifier_exists_in_mixin():
    """`_init_emotion_classifier` 在 mixin 里，不在 pet.py。

    上面那条嵌套检查只搜 pet.py 源码，盖不到 mixin ——
    单独守一道，防止「调了但方法不存在」的导入期崩溃。
    """
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "pet_mixins", "emotion_classify_mixin.py")
    src = open(p, encoding="utf-8").read()
    assert re.search(r"def _init_emotion_classifier\(self", src), \
        "mixin 里缺 _init_emotion_classifier"


def test_nested_init_methods_exist():
    """嵌套调用的 _init_* 也必须存在（否则某条链路整段失效）。"""
    src = _pet_sources()
    missing = [
        m for m in EXPECTED_NESTED_INIT_METHODS
        if not re.search(rf"def {re.escape(m)}\(self", src)
    ]
    assert not missing, f"缺失嵌套 _init_* 方法: {missing}"


# ── 四、定时器 / 控件 / 动作连接（补全安全绳）─────────────────────────────


def _slot_name(slot: str) -> str:
    """规范化槽名：剥掉前导 `self.`，保留剩余点号链。

    `self._drag_poll_tick`      → `_drag_poll_tick`
    `self._hanako_monitor.tick` → `_hanako_monitor.tick`
    `<lambda>`                  → `<lambda>`
    """
    return slot[5:] if slot.startswith("self.") else slot


def _all_connections() -> list:
    r"""收集全部 `xxx.connect(yyy)` → [(文件, 行号, 发射者, 槽)]。

    槽可能是 lambda（`connect(lambda: ...)`）——此时槽名记为 `<lambda>`。
    不能只用 `\w` 匹配，否则 lambda 会整条漏掉。

    槽保留完整点号链（如 `_hanako_monitor.tick`），不截末段——
    截了会丢失「哪个对象的方法」这个信息。
    """
    out = []
    pat = re.compile(r"([\w\.]+)\.connect\(\s*(lambda|[\w\.]+)")
    for f, src in _all_sources().items():
        for m in pat.finditer(src):
            ln = src[: m.start()].count("\n") + 1
            slot = m.group(2)
            out.append((f, ln, m.group(1), "<lambda>" if slot == "lambda" else slot))
    return out


def test_timer_connections_present():
    """16 个内部定时器的 timeout → 槽 全部存在。

    这些是桌宠的「心跳」。搬漏一个的表现：「某个功能不刷新」
    ——不报错、不崩溃，但那个行为永远静止。
    """
    conns = {}
    for _f, _ln, sig, slot in _all_connections():
        if sig.endswith(".timeout"):
            # 发射者取点号链末段（`self._bubble_timer` → `_bubble_timer`）；
            # 槽保留完整链（`self._hanako_monitor.tick` → `_hanako_monitor.tick`）
            conns[sig.split(".")[-2]] = _slot_name(slot)

    missing = []
    for timer, slot in EXPECTED_TIMER_CONNECTIONS.items():
        if timer not in conns:
            missing.append(f"{timer}.timeout 未连接")
        elif conns[timer] != slot:
            missing.append(f"{timer}.timeout -> {conns[timer]}，预期 {slot}")
    assert not missing, "定时器连接缺失/错位:\n  " + "\n  ".join(missing)


def test_widget_connections_present():
    """控件/外部对象信号 → 槽 全部存在（UI 交互入口）。

    发射者可能是 `self.xxx`（属性）或局部变量（mgr / panel / win）。
    两种都要能识别：前者取点号链末段，后者就是变量名本身。
    """
    conns = {}
    for _f, _ln, sig, slot in _all_connections():
        parts = sig.split(".")
        if len(parts) >= 2:
            # self._chat_panel.message_submitted → (_chat_panel, message_submitted)
            # mgr.theme_changed                 → (mgr, theme_changed)
            conns[(parts[-2], parts[-1])] = _slot_name(slot)

    missing = []
    for emitter, sigs in EXPECTED_WIDGET_CONNECTIONS.items():
        for signame, slot in sigs:
            key = (emitter, signame)
            if key not in conns:
                missing.append(f"{emitter}.{signame} 未连接")
            elif conns[key] != slot:
                missing.append(f"{emitter}.{signame} -> {conns[key]}，预期 {slot}")
    assert not missing, "控件连接缺失/错位:\n  " + "\n  ".join(missing)


def test_context_menu_connection_present():
    """右键菜单请求信号必须连到菜单构建槽。"""
    conns = {}
    for _f, _ln, sig, slot in _all_connections():
        conns[sig.split(".")[-1]] = _slot_name(slot)
    assert conns.get("customContextMenuRequested") == EXPECTED_CONTEXT_MENU_SLOT, (
        f"customContextMenuRequested -> {conns.get('customContextMenuRequested')}，"
        f"预期 {EXPECTED_CONTEXT_MENU_SLOT}"
    )


def test_action_triggered_connections_present():
    """QAction.triggered → 槽 的关键项存在（托盘菜单入口）。

    发射者是局部变量（vis / passthrough / quit_a），无法按属性名索引，
    故只断言「关键槽存在」+「总数不少于实测值」。

    注：右键菜单走的是 `customContextMenuRequested`（不是 triggered），
    其槽由 test_widget_connections_present 覆盖。
    """
    slots = set()
    n = 0
    for _f, _ln, sig, slot in _all_connections():
        if sig.endswith(".triggered"):
            n += 1
            slots.add(_slot_name(slot).split(".")[-1])

    missing = EXPECTED_ACTION_SLOTS - slots
    assert not missing, f"QAction 关键槽缺失（菜单入口丢了）: {missing}"
    assert n >= MIN_ACTION_CONNECTIONS, (
        f"QAction.triggered 连接数 {n} < 实测下限 {MIN_ACTION_CONNECTIONS}"
    )


def test_total_connection_count_not_regressed():
    """连接总数不得减少。

    重构前实测 51 条。搬家只应「移动」连接，不应「删除」。
    减少说明有连接在搬家中丢失。
    """
    n = len(_all_connections())
    assert n >= 51, (
        f"连接总数 {n} < 基线 51——搬家中丢了连接（每条都是一条功能生命线）"
    )


# ── 五、防止回退 ────────────────────────────────────────────────────────────


def test_pet_py_does_not_regrow():
    """pet.py 不得继续膨胀（搬家是减法，不是加法）。

    重构后基线 3456 行（原 3839）。本断言是软上限：允许 ±50 行浮动，
    超过说明新增接线又堆回了 pet.py。
    """
    n = len(_read("pet.py").splitlines())
    assert n < 3550, (
        f"pet.py 已 {n} 行（基线 3456）。新增接线应放进对应 mixin，"
        f"而不是继续堆进 pet.py。"
    )
