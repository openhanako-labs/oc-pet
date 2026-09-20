"""可热生效配置的应用器（2026-09-19 从 pet.py 迁出）。

## 为什么单独成一个模块

这三个应用器原先内联在 ``PetWindow`` 里，但它们**完全不需要窗口**——
只读一份 config 字典，调 ``core/`` 下的模块，返回成功与否。
放在 pet.py 里意味着：

1. pet.py 有行数护栏（基线 3456 / 上限 3550），接线已经挤满；
2. 它们**无法脱离 Qt 单测**（import pet.py 就要 PySide6）；
3. 它们是 PetSystem/PetShell 拆分的自然切点——契约已经很清楚。

## 统一契约

每个 applier 都是 ``apply(config) -> bool`` 形式的**纯函数**（除 a2a 外），
只做「按配置装卸某能力」，不碰 UI、不碰 Qt、不持有状态。

返回值语义（``_apply_runtime_config`` 依赖它）：

- ``True``  —— 装成功，调用方**可以记账**（下次配置没变就跳过）
- ``False`` —— 没装成功，调用方**不能记账**（否则下次真该装时会被"没变"挡掉）

⚠ ``apply_a2a`` 是例外：它返回 Delegate 对象或 ``None``，由调用方判断真假。
   这是既有契约，本次不改（只搬家）。

## 依赖注入

需要外部能力的（会话管理器、气泡回调）一律**从参数传入**，
不在本模块里反向 import pet.py —— 那是这次拆分的核心目的。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ── lip_sync ──────────────────────────────────────────────

def apply_lip_sync(config: dict) -> bool:
    """按 ``config.lip_sync`` 配置口型开口上限。

    起音/收音（40ms/90ms）是常量，本身不需配置；这里只接**开口上限**：
    ``mouth_peak`` 默认 1.0 = 不压（保持现行为）。

    为何不给默认值：AgentAtelierR 用 55% 避免"每次都张满嘴"，但 oc-pet 的
    ``/a/`` 是自己调到 0.95 的（miku 模型实测值）——压多少是**看脸决定**的事，
    看不见成品就不替你定。想试就把 ``lip_sync.mouth_peak`` 改成 0.75 或 0.55。
    """
    try:
        from core.lip_sync import set_mouth_peak

        cfg = (config.get("lip_sync", {}) if isinstance(config, dict) else {}) or {}
        set_mouth_peak(cfg.get("mouth_peak", 1.0))
        return True          # 供调用方判断"真装上了"
    except Exception as e:
        logger.warning("口型配置失败（非致命）: %s", e)
        return False


# ── game watch ────────────────────────────────────────────

def apply_game_watch(config: dict):
    """按 ``config.game`` 建游戏会话状态机。

    P0-1（陪玩）。默认**开**，但这一步**不做任何用户可见动作**（不弹气泡、
    不说话）——只把"现在在玩哪个游戏、玩了多久"变成可观测的事实 + 一条
    EventBus 事件（``game_session``）。说话是后续阶段的事，所以可以放心常开。

    白名单来自内置表（零配置可用）+ ``config.game.games`` 覆盖，
    ``config.game.disabled`` 可剔除条目，全部关闭用 ``enabled=false``。

    Returns:
        ``(ok: bool, watcher_or_None)``。调用方负责把 watcher 存到自己身上。
    """
    try:
        cfg = (config.get("game", {}) if isinstance(config, dict) else {}) or {}
        if not cfg.get("enabled", True):
            logger.info("陪玩 P0-1：游戏识别未启用（config game.enabled=false）")
            return True, None

        from core.game.registry import load_games
        from core.game.session import GameSessionWatcher

        games = load_games(cfg)
        watcher = GameSessionWatcher(games=games)
        logger.info("陪玩 P0-1：游戏识别就绪，白名单 %d 款（%s）",
                    len(games), "、".join(g.name for g in games[:5]))
        return True, watcher
    except Exception as e:
        logger.warning("陪玩 P0-1 初始化失败（非致命）: %s", e)
        return False, None


# ── a2a（把活派给 Hana 的 agent）──────────────────────────

def apply_a2a(
    config: dict,
    session_manager: Any,
    on_result: Optional[Callable] = None,
):
    """按**当前** config 装/卸派活能力。

    启动和「设置面板保存后热重载」走**同一条路**
    （``core.a2a_capability.apply_config``）——只有一条路，
    就不会出现"启动时对、重载时不对"。所以改完设置**不用重启**。

    默认**关**（``config.a2a.enabled``），护栏全在 ``core/a2a.py``。
    结果**不自动念**——(b) 方案：只响一声门铃，用户问才讲。

    Args:
        config: 整份配置字典。
        session_manager: Hana 会话管理器；``None`` 时返回 ``False``
            （表示"没装成功"，调用方**不记账**，否则 ``_init_a2a`` 拿到
            管理器后会被"配置没变"挡掉）。
        on_result: 结果回调（门铃）。由调用方注入，本模块不反向依赖 UI。

    Returns:
        Delegate 对象 / ``False``（无会话管理器）/ ``None``（异常）。
    """
    try:
        from core.a2a_capability import apply_config

        if session_manager is None:
            logger.info("A2A: 无会话管理器，跳过")
            # 返回 False = 没装成功 → 调用方**不记账**
            return False

        def _create(agent_id):
            # create_session 的 agent_id 是**关键字参数**
            return session_manager.create_session(agent_id=agent_id)

        def _send(session, text, timeout):
            # send_and_wait 的 timeout 也是关键字参数，
            # 且返回 ReplyResult 而不是 str——两处都跟默认假设不一样
            r = session_manager.send_and_wait(session, text, timeout=timeout)
            return getattr(r, "text", "") or ""

        d = apply_config(
            config or {}, _create, _send, on_result=on_result,
        )
        logger.info("A2A %s：%s",
                    "已生效" if (d is not None and getattr(d, "enabled", False))
                    else "未启用",
                    d.stats() if d is not None else "无会话管理器")
        return d
    except Exception as e:
        logger.warning("A2A 应用配置失败（非致命）: %s", e)
        return None


def build_a2a_bell(bubble: Callable[[str, str, str], None]) -> Callable:
    """构造 a2a 结果门铃回调（(b) 方案：**只响门铃，不念结论**）。

    讲不讲、什么时候讲由用户决定；结论在 ``Delegator.results`` 里等他问。
    **失败也响**（内容不同）——不响也不存就等于替他吞掉了失败。

    但"失败"要分两种：会话建起来了 = 活已经交出去了，只是还没回话，
    **不能替它宣布失败**。

    Args:
        bubble: 气泡入口，签名 ``(text, emotion, source)``。
            调用方负责保证它是**线程安全**的那个入口
            （本回调会在派活的后台线程里被调）。

    Returns:
        可直接作为 ``on_result`` 传入 ``apply_a2a`` 的回调。
    """
    def _on_result(result):
        try:
            from core.a2a_capability import label_of

            label = label_of(getattr(result, "agent_id", ""))
            if getattr(result, "ok", False):
                text, emotion = f"{label} 那边有结果了，想听就说一声～", "happy"
            elif getattr(result, "delivered", False):
                text, emotion = f"{label} 那边还没回话…要我看看吗？", "thinking"
            else:
                text, emotion = f"{label} 那边没交出去…要我细说吗？", "sad"
            bubble(text, emotion, "a2a")
        except Exception as e:
            logger.debug("A2A 门铃失败（忽略）: %s", e)

    return _on_result
