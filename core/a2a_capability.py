"""A2A 的对话接线：把派活挂成两条本地能力。

## 为什么用 ``register_capability`` 而不是改 ``CAPABILITIES``

``capability_registry`` 本来就留了官方扩展点：``register_capability()`` +
``EXTERNAL_CAPABILITIES``（``mc_bridge`` 就是这么挂 ``mc_task``/``mc_method``
的）。所以这里**一行都不用动那个文件**（本轮只在它那儿加了一个默认关闭的字段，
见下）。

## 两条能力

1. ``delegate_task``：关键词**自带助手名**（"交给红莉栖" / "派给红莉栖"）。
   这样"把这句话交给我"之类**不会**被截胡——那种话本来就该走 LLM。
   命中 → 后台线程派活 → **立刻**回一句"交出去了"。
2. ``delegation_result``：命中 → 把**未读**结果念出来。

## ``allow_embedded``：为什么必须显式声明

``capability_registry.route()`` 默认对纯中文关键词做"被中文夹住即误匹配"保护
（防"接管一下一个对话"被"下一个"劫持）。可派活的关键词**天然嵌在中文句子里**：
"把这件事交给红莉栖总结一下"两侧都是中文字 → 默认规则**永不命中**，
等于功能装上了却不触发。所以这两条能力显式声明 ``allow_embedded=True``，
换成自己的约束：**关键词必须自带助手名**（足够独特，不怕碰撞）。

## (b) 方案：门铃

派活完成（**含失败**）时只响一声铃，结论躺在 ``Delegator.results`` 里，
等他自己开口问——讲不讲、什么时候讲，由他决定。
"""
from __future__ import annotations

import logging
from typing import Optional

from core.a2a import Delegator
from core.capability_registry import (
    Capability,
    RouteResult,
    register_capability,
    unregister_capability,
)

logger = logging.getLogger(__name__)

# ── 助手名 ────────────────────────────────────────────────
# 别名表只用于**认名字**；能不能派，仍由 Delegator 的白名单说了算。
AGENT_LABELS: dict[str, str] = {
    "aimis": "爱弥斯",
    "alice": "艾莉丝",
    "glados": "GLaDOS",
    "kurisu": "牧濑红莉栖",
    "luoqixi": "洛琪希",
    "ophelia": "奥菲莉娅",
    "rebecca": "瑞贝卡",
}

AGENT_ALIASES: dict[str, tuple[str, ...]] = {
    "aimis": ("aimis", "爱弥斯"),
    "alice": ("alice", "艾莉丝", "爱丽丝"),
    "glados": ("glados", "格拉多斯"),
    "kurisu": ("kurisu", "红莉栖", "牧濑红莉栖", "助手"),
    "luoqixi": ("luoqixi", "洛琪希", "洛琪西"),
    "ophelia": ("ophelia", "奥菲莉娅", "奥菲莉亚"),
    "rebecca": ("rebecca", "瑞贝卡"),
}

# 派活动词（会与助手名拼成关键词，见 build_patterns）
DELEGATE_VERBS: tuple[str, ...] = ("交给", "派给", "委派给", "委托给", "安排给")

# "问你派了什么"的说法（够独特，不会跟日常话撞）
RESULT_PATTERNS: tuple[str, ...] = (
    "那边有结果", "那边有消息", "有结果了吗", "结果回来了",
    "派活的结果", "问一下结果", "派出去的事",
)

# 任务文本最短长度：剥掉"交给红莉栖"之后余下的字若太短，就不剥
_MIN_TASK_CHARS = 8
CAPABILITY_NAMES = ("delegate_task", "delegation_result")


def label_of(agent_id: str) -> str:
    return AGENT_LABELS.get(agent_id, agent_id or "那位")


def aliases_for(agent_id: str) -> tuple[str, ...]:
    return AGENT_ALIASES.get(agent_id, (agent_id,))


def resolve_agent(text: str, allowed: Optional[list] = None) -> Optional[tuple]:
    """从文本里认出助手：返回 ``(agent_id, 命中的别名)``；认不出返回 ``None``。

    **绝不猜**：没有名字就是没有。（能不能派，由 ``Delegator.check`` 再判。）
    长别名优先——"牧濑红莉栖"要压过"红莉栖"。
    """
    low = (text or "").lower()
    if not low:
        return None
    pool = list(allowed) if allowed else list(AGENT_ALIASES)
    best: Optional[tuple] = None
    for agent in pool:
        for alias in aliases_for(agent):
            if alias and alias.lower() in low:
                if best is None or len(alias) > len(best[1]):
                    best = (agent, alias)
    return best


def extract_task(text: str, alias: str) -> str:
    """取"要办的那件事"。

    剥名字后面余下的部分（如"交给红莉栖：帮我查一下X" → "帮我查一下X"）；
    余下太短或等于空，就**原句整段发过去**——宁可多给一句上下文，
    也不要做脆弱的文本裁剪把任务切坏。
    """
    raw = (text or "").strip()
    if not raw or not alias:
        return raw
    idx = raw.lower().find(alias.lower())
    if idx < 0:
        return raw
    rest = raw[idx + len(alias):].strip()
    rest = rest.lstrip("，,。:：、;； 　\t").strip()
    if len(rest) >= _MIN_TASK_CHARS:
        return rest
    return raw


def build_patterns(agent_id: str) -> list:
    """为某个助手生成关键词：动词 × 别名（关键词自带名字 = 不会截胡）。"""
    out = []
    for verb in DELEGATE_VERBS:
        for alias in aliases_for(agent_id):
            out.append(f"{verb}{alias}")
    return out


# ── 两个处理函数 ──────────────────────────────────────────


def _delegate_callee(delegator: Delegator):
    def _callee(text: str) -> RouteResult:
        hit = resolve_agent(text, delegator.allowed_agents)
        if hit is None:
            return RouteResult(
                capability="delegate_task",
                text="没听出要交给谁。说清助手名字我再发。",
                emotion="thinking",
                anim="idle",
            )
        agent, alias = hit
        label = label_of(agent)
        ok, why = delegator.check(agent)
        if not ok:
            return RouteResult(
                capability="delegate_task",
                text=f"{label} 那边我发不过去：{why}。",
                emotion="sad",
                anim="idle",
            )
        task = extract_task(text, alias)
        if not delegator.delegate_async(task, agent):
            return RouteResult(
                capability="delegate_task",
                text="这次没发出去（配额用完了或任务不合法），等下再说。",
                emotion="sad",
                anim="idle",
            )
        return RouteResult(
            capability="delegate_task",
            text=f"已经交给{label}了，有结果我叫你。",
            emotion="happy",
            anim="extra",
        )

    return _callee


def _result_callee(delegator: Delegator):
    def _callee(text: str) -> RouteResult:
        unread = delegator.take_unread()
        if not unread:
            return RouteResult(
                capability="delegation_result",
                text="还没有新结果。",
                emotion="neutral",
                anim="idle",
            )
        lines = []
        for r in unread:
            label = label_of(r.agent_id)
            body = (r.reply or "").strip()
            if r.ok and body:
                lines.append(f"{label} 那边：{body}")
            elif not r.ok:
                # 失败也要说清——悄悄吞掉失败等于骗他
                lines.append(f"{label} 那边没办成（{r.error}）")
            else:
                lines.append(f"{label} 那边回了，但没给出内容")
        return RouteResult(
            capability="delegation_result",
            text="\n\n".join(lines),
            emotion="happy",
            anim="extra",
        )

    return _callee


# ── 注册 / 卸载 ───────────────────────────────────────────


def build_capabilities(delegator: Delegator) -> list:
    """构建两条能力（不注册）。

    白名单为空时，关键词按**全部已知助手**生成——这样用户说"交给红莉栖"
    会收到一句明确的"不在白名单里"，比默默回退给 LLM 有用得多。
    """
    caps = [
        Capability(
            name="delegation_result",
            patterns=list(RESULT_PATTERNS),
            handler="callable",
            callable=_result_callee(delegator),
            description="说出未读的派活结果（门铃：他负责响铃，讲不讲由用户开口）",
            emotion="happy",
            anim="extra",
            allow_embedded=True,
        )
    ]
    pool = delegator.allowed_agents or list(AGENT_ALIASES)
    patterns: list = []
    for agent in pool:
        patterns.extend(build_patterns(agent))
    if patterns:
        caps.insert(0, Capability(
            name="delegate_task",
            patterns=patterns,
            handler="callable",
            callable=_delegate_callee(delegator),
            description="把活派给 Hana 的某个助手（关键词自带助手名，避免截胡）",
            emotion="happy",
            anim="extra",
            allow_embedded=True,
        ))
    return caps


def register_a2a(delegator: Delegator) -> list:
    """把两条能力挂进全局能力表，返回挂上的名字。"""
    names = []
    for cap in build_capabilities(delegator):
        register_capability(cap)
        names.append(cap.name)
    logger.info("A2A 能力已注册：%s", names)
    return names


def unregister_a2a() -> None:
    for name in CAPABILITY_NAMES:
        unregister_capability(name)
