# -*- coding: utf-8 -*-
"""主动搭话 LLM 生成 — 候选生成 + LLM 决策是否开口。

设计（参考 N.E.K.O. `main_logic/proactive_chat/generation.py` 思路重写，
去 asyncio/HTTP，走 oc-pet 的 harness_adapter 同步通道）：

  - ``ProactiveGenerator`` 复用 HanakoPetAdapter（source="proactive"）生成一句
    自然、简短、贴合当前场景的主动搭话文案；
  - 生成失败 / 超时 / 空结果 → 返回 None，直接不搭话（无模板池兜底）；
  - 线程约束：生成在后台线程执行，结果经 Qt Signal 回主线程再投递（0x8001010D
    教训——所有 UI/COM 操作只能在主线程）。无 Qt 环境（headless 单测）时
    退化为同步回调路径。

prompt 模式：proactive 专用指令（场景 + 最近对话间隔 + 参考方向 + 防重复），
不污染 Hanako 会话历史（chat() 对 source="proactive" 走 chat_direct 直连）。

理念：感知触发交给 AI 自由发挥，不念预置台词。网络是必需条件，无模板池兜底。
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 生成文案长度上限（字符；超长截断，避免大段输出弹气泡）
GENERATED_TEXT_MAX_CHARS = 60


def build_proactive_prompt(context: dict) -> str:
    """构造 proactive 专用 LLM 指令（作为 user 消息发给适配器）。

    Args:
        context: 调度器传入的生成上下文，含：
            scenario: 意图场景名（如 late_night_work）
            intent: 意图分类结果 dict（可选）
            signals: 感知信号 dict（period/category/conversation_idle_min…）
            fallback_prompt: 模板池文案（作为话题参考方向，提示 LLM 不要照抄）

    Returns:
        面向 LLM 的中文指令文本。
    """
    scenario = str(context.get("scenario") or "")
    signals = context.get("signals") or {}
    category = signals.get("category") or "other"
    period = signals.get("period") or "other"
    try:
        conv_idle_min = int(float(signals.get("conversation_idle_min", 0) or 0))
    except (TypeError, ValueError):
        conv_idle_min = 0
    fallback = str(context.get("fallback_prompt") or "")

    lines = [
        "现在是一个适合主动搭话的时刻。请以你的身份，主动对主人说一句自然、简短、有温度的搭话。",
    ]
    if scenario:
        lines.append(f"当前场景：{scenario}（{period}时段）")
    else:
        lines.append(f"当前场景：{category}（{period}时段）")
    if conv_idle_min > 0:
        lines.append(f"最近一次对话距今约 {conv_idle_min} 分钟。")
    # 注入真实时间戳（取自系统时钟），防止 LLM 凭空编造与时间不符的
    # "周末 / 周五" 等表述——凡是涉及时间的内容都以这个时间为准。
    try:
        from .time import TimePerception
        time_label = TimePerception().format_for_prompt()
        if time_label:
            lines.append(f"真实时间参考：{time_label}")
    except Exception:
        pass
    if fallback:
        lines.append(f"参考方向（可自由发挥，不要照抄）：{fallback}")
    lines.append("要求：一句话，不超过 20 字，口语化，贴合场景，不要重复最近说过的话。")
    lines.append(
        "若想配合一个动作/表情，可在句末用 [action:{\"gesture\":\"<动作名>\","
        "\"intensity\":<0到1>,\"params\":{<可选Live2D参数>}}] 表达，例如好奇探头 "
        "[action:{\"gesture\":\"peek\",\"intensity\":0.6,\"params\":{\"ParamAngleX\":12}}]；"
        "普通搭话可省略该标签，只说正文。"
    )
    return "\n".join(lines)


def build_proactive_extra_context(context: dict) -> str:
    """构造注入适配器的感知上下文（system 消息，供 LLM 参考）。

    与 build_proactive_prompt 互补：prompt 是"指令"，这里是"背景信息"。
    """
    signals = context.get("signals") or {}
    parts = []
    if signals.get("period"):
        parts.append(f"时段：{signals['period']}")
    if signals.get("category"):
        parts.append(f"前台分类：{signals['category']}")
    if signals.get("activity"):
        parts.append(f"活动状态：{signals['activity']}")
    return "；".join(parts) if parts else ""


def clean_generated(text: str) -> str:
    """清洗 LLM 生成结果：去引号 / [proactive] / [emotion:xxx] 标签泄漏，限长。

    Returns:
        清洗后的文案；空串表示无有效内容（调用方应回退模板池）。
    """
    if not text:
        return ""
    text = (text or "").strip().strip('"\'“”‘’《》')
    # 剥离可能泄漏的 [proactive] / [emotion:xxx] 前缀
    text = re.sub(r"^\[proactive\]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*\[\s*emotion\s*:\s*\w+\s*\]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\[\s*emotion\s*:\s*\w+\s*\]\s*$", "", text, flags=re.IGNORECASE)
    # 剥离结构化动作意图 [action:{...}]（动态参数标签不该上气泡；由渲染器消费）
    text = re.sub(r"\[action:\s*\{.*?\}\s*\]", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\s{2,}", " ", text).strip()
    text = text.strip()
    if not text:
        return ""
    if len(text) > GENERATED_TEXT_MAX_CHARS:
        text = text[: GENERATED_TEXT_MAX_CHARS - 1].rstrip() + "…"
    return text


class ProactiveGenerator:
    """主动搭话文案生成器 — 复用 Hanako 通道，失败回退由调用方处理。

    Args:
        adapter: HanakoPetAdapter（或实现了 ``chat(message, inject_memory,
            extra_context, source) -> (text, emotion)`` 的对象）。None 时若
            llm_fn 也为 None，则 is_available() 为 False（调度器不启用生成）。
        llm_fn: 可选注入的同步生成函数 ``fn(context: dict) -> str | None``，
            主要用于单测与无适配器环境（headless）。
        timeout: LLM 调用超时（秒）。适配器内部已有网络超时；此值用于
            headless 同步路径的兜底计时（0=不额外限时）。

    线程模型：
        - ``generate()`` 在调用线程（主线程）被调度器调用，立即返回；
        - 实际 LLM 调用在后台 daemon 线程执行；
        - 结果通过 Qt Signal（QObject 桥）回主线程回调 on_generated/on_fallback；
        - 无 Qt 可用时（headless 单测）退化为同步回调，行为一致但阻塞调用线程。
    """

    def __init__(
        self,
        adapter: Any = None,
        llm_fn: Callable[[dict], str | None] | None = None,
        timeout: float = 12.0,
        use_qt_bridge: bool = True,
    ):
        self._adapter = adapter
        self._llm_fn = llm_fn
        self._timeout = float(timeout) if timeout and timeout > 0 else 0.0
        self._on_generated: Callable[[str], None] | None = None
        self._on_fallback: Callable[[str], None] | None = None
        self._bridge = None
        if use_qt_bridge:
            try:
                from PySide6.QtCore import QObject, Signal

                class _Bridge(QObject):
                    generated = Signal(str)
                    fallback = Signal(str)

                self._bridge = _Bridge()
                self._bridge.generated.connect(self._deliver_generated)
                self._bridge.fallback.connect(self._deliver_fallback)
            except Exception as exc:  # 无 Qt / 无 PySide6 → 同步路径
                logger.debug("Qt bridge unavailable, use sync path: %s", exc)
                self._bridge = None

    # ── 生命周期 ────────────────────────────────────────────

    def set_callbacks(
        self,
        on_generated: Callable[[str], None] | None,
        on_fallback: Callable[[str], None] | None,
    ) -> None:
        """注册结果回调（应在主线程调用）。

        on_generated(text): 生成成功（text 非空）；
        on_fallback(fallback_text): 生成失败/空，回退模板池文案。
        """
        self._on_generated = on_generated
        self._on_fallback = on_fallback

    def is_available(self) -> bool:
        """是否有可用的生成通道（适配器或注入函数）。"""
        return self._adapter is not None or self._llm_fn is not None

    def shutdown(self) -> None:
        """清理回调引用（进程退出时调用，防止悬挂回调）。"""
        self._on_generated = None
        self._on_fallback = None

    # ── 对外入口 ────────────────────────────────────────────

    def generate(self, context: dict, fallback_text: str = "") -> None:
        """发起一次主动搭话生成（异步，立即返回）。

        Args:
            context: 生成上下文（scenario/signals/fallback_prompt…）。
            fallback_text: 兼容参数（已弃用，生成失败直接不搭话）。
        """
        if not self.is_available():
            logger.debug("Proactive generation skipped: LLM not available")
            return
        if self._bridge is None:
            # headless / 无 Qt：同步执行（单测路径）
            try:
                text = self._generate_sync(context)
            except Exception as exc:
                logger.warning("Proactive generation failed (sync): %s", exc)
                text = ""
            if text:
                self._deliver_generated(text)
            else:
                logger.debug("Proactive generation empty: no fallback, skip")
            return
        threading.Thread(
            target=self._worker,
            args=(context, fallback_text),
            daemon=True,
            name="proactive-gen",
        ).start()

    # ── 内部实现 ────────────────────────────────────────────

    def _worker(self, context: dict, fallback_text: str = "") -> None:
        """后台线程：执行生成并 emit 信号回主线程。"""
        try:
            text = self._generate_sync(context)
        except Exception as exc:
            logger.warning("Proactive generation failed (thread): %s", exc)
            text = ""
        if text:
            self._bridge.generated.emit(text)
        else:
            logger.debug("Proactive generation empty: no fallback, skip")

    def _generate_sync(self, context: dict) -> str:
        """同步生成一条主动搭话文案（可被单测直接调用）。

        Returns:
            清洗后的文案；空串 = 失败/空结果（调用方回退模板池）。
        """
        if self._llm_fn is not None:
            raw = self._llm_fn(context)
            return clean_generated(raw or "")
        if self._adapter is None:
            return ""
        prompt = build_proactive_prompt(context)
        extra = build_proactive_extra_context(context)
        try:
            reply, _emotion = self._adapter.chat(
                prompt,
                inject_memory=True,  # 2026-09-06: 注入 Hana 记忆，避免生成"感到寂寞"等无上下文内容
                extra_context=extra,
                source="proactive",
            )
            text = clean_generated(reply or "")
            if text:
                return text
        except Exception as exc:
            logger.warning("Proactive adapter chat failed: %s", exc)
        
        # 2026-09-06: S2 失败降级（LLM 超时/挂了走模板库）
        return self._generate_fallback_template(context)

    def _generate_fallback_template(self, context: dict) -> str:
        """2026-09-06: S2 失败降级（LLM 超时/挂了走模板库）
        
        从 context 中获取模板文本，或使用模板库生成。
        """
        # 优先使用 context 中的模板文本
        template_text = context.get("template_text")
        if template_text:
            return template_text
        
        # 2026-09-06: P2 #5 使用模板库生成降级文案
        try:
            from core.template_library import generate_template_fallback
            return generate_template_fallback(context)
        except Exception as e:
            logger.warning("Template library failed: %s", e)
            return ""
    
    # ── Qt 桥槽 / 同步回调（均在主线程执行）──────────────────

    def _deliver_generated(self, text: str) -> None:
        if self._on_generated is not None:
            try:
                self._on_generated(text)
            except Exception as exc:
                logger.error("on_generated callback error: %s", exc)

    def _deliver_fallback(self, fallback_text: str) -> None:
        if self._on_fallback is not None:
            try:
                self._on_fallback(fallback_text)
            except Exception as exc:
                logger.error("on_fallback callback error: %s", exc)
