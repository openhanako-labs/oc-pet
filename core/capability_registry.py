"""能力路由器 — 关键词匹配 → 直接执行，跳过 LLM 选工具

快速路径：用户说"放首歌" → 匹配 play_music → 直接调用 audio_player.play()
LLM 路径：匹配失败 → 回退到 LLM 选工具

用法:
    from core.capability_registry import CapabilityRouter
    router = CapabilityRouter(perception, tool_registry, tool_executor)
    result = router.route("放首歌 周杰伦的晴天")
    if result:
        # 直接执行，不走 LLM
        on_reply(result.text, result.emotion, result.anim, result.audio_path)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional, Callable, Any

logger = logging.getLogger(__name__)


@dataclass
class Capability:
    """一个可路由的能力"""
    name: str                       # 唯一标识 "play_music"
    patterns: list[str]             # 触发词 ["放歌", "播放", "放一首"]
    handler: str                    # 处理方式 "tool" / "internal"
    tool_name: str = ""             # 工具名（handler=tool 时）
    plugin_id: str = ""             # 插件 ID（handler=tool 时）
    extract_args: Callable = None   # 从文本提取参数的函数
    description: str = ""           # 描述（日志用）
    emotion: str = "happy"          # 执行后的情绪
    anim: str = "extra"             # 执行后的动画


@dataclass
class RouteResult:
    """路由结果"""
    capability: str                 # 匹配的能力名
    text: str = ""                  # 回复文本
    emotion: str = "neutral"        # 情绪
    anim: str = "idle"              # 动画
    audio_path: str = ""            # 音频路径
    tool_result: str = ""           # 工具执行结果


# ── 参数提取函数 ──────────────────────────────────────────

def _extract_song_name(text: str) -> dict:
    """从文本中提取歌名"""
    # 去掉触发词
    triggers = ["放歌", "播放", "放一首", "来首", "听", "play", "给我放", "来点"]
    cleaned = text
    for t in triggers:
        cleaned = cleaned.replace(t, "").strip()
    # 去掉"的"
    cleaned = cleaned.replace("的", "").strip()
    if cleaned:
        return {"source": cleaned}
    return {}


def _extract_search_query(text: str) -> dict:
    """从文本中提取搜索关键词"""
    triggers = ["搜", "搜索", "搜一下", "查", "查一下", "search", "帮我搜", "帮我查"]
    cleaned = text
    for t in triggers:
        cleaned = cleaned.replace(t, "").strip()
    if cleaned:
        return {"query": cleaned}
    return {}


def _extract_url(text: str) -> dict:
    """从文本中提取 URL"""
    url_match = re.search(r'https?://\S+', text)
    if url_match:
        return {"url": url_match.group()}
    return {}


# ── 能力定义 ──────────────────────────────────────────────

CAPABILITIES: list[Capability] = [
    # 音乐
    Capability(
        name="play_music",
        patterns=["放歌", "播放", "放一首", "来首", "听歌", "来点音乐", "play"],
        handler="tool",
        tool_name="play",
        plugin_id="hanako-audio-player",
        extract_args=_extract_song_name,
        description="播放音乐",
    ),
    Capability(
        name="pause_music",
        patterns=["暂停", "暂停播放", "停一下", "pause"],
        handler="tool",
        tool_name="audio_bus",
        plugin_id="hanako-audio-player",
        extract_args=lambda text: {"action": "pause"},
        description="暂停音乐",
    ),
    Capability(
        name="resume_music",
        patterns=["继续播放", "继续", "恢复播放", "resume"],
        handler="tool",
        tool_name="audio_bus",
        plugin_id="hanako-audio-player",
        extract_args=lambda text: {"action": "resume"},
        description="恢复播放",
    ),
    Capability(
        name="next_track",
        patterns=["下一首", "切歌", "下一个", "next"],
        handler="tool",
        tool_name="audio_bus",
        plugin_id="hanako-audio-player",
        extract_args=lambda text: {"action": "next"},
        description="下一首",
    ),

    # 日报
    Capability(
        name="daily_diary",
        patterns=["今天做了什么", "日报", "今日总结", "做了啥", "今天干了啥"],
        handler="internal",
        description="生成今日日报",
    ),

    # Session
    Capability(
        name="session_info",
        patterns=["当前会话", "session信息", "会话状态", "你在处理什么"],
        handler="internal",
        description="查看当前 Session 信息",
    ),

    # 感知状态
    Capability(
        name="perception_status",
        patterns=["感知状态", "你在感知什么", "权限状态", "你看到了什么"],
        handler="internal",
        description="查看感知状态",
    ),

    # 截图
    Capability(
        name="screenshot_now",
        patterns=["截个图", "看看屏幕", "截图", "screenshot"],
        handler="internal",
        description="立即截图分析",
        emotion="thinking",
    ),
]


class CapabilityRouter:
    """能力路由器"""

    def __init__(self, perception=None, tool_registry=None, tool_executor=None):
        self._perception = perception
        self._tool_registry = tool_registry
        self._tool_executor = tool_executor

    def route(self, text: str) -> RouteResult | None:
        """尝试匹配用户文本到能力

        Returns:
            RouteResult 如果匹配成功，None 如果需要回退到 LLM
        """
        text_lower = text.strip().lower()
        if not text_lower:
            return None

        for cap in CAPABILITIES:
            for pattern in cap.patterns:
                if pattern in text_lower:
                    logger.info("Capability matched: %s (pattern='%s')", cap.name, pattern)
                    try:
                        if cap.handler == "tool":
                            return self._handle_tool(cap, text)
                        elif cap.handler == "internal":
                            return self._handle_internal(cap, text)
                    except Exception as e:
                        logger.warning("Capability %s failed: %s", cap.name, e)
                        return RouteResult(
                            capability=cap.name,
                            text=f"操作失败：{e}",
                            emotion="sad",
                            anim="idle",
                        )
        return None

    def _handle_tool(self, cap: Capability, text: str) -> RouteResult:
        """处理工具类能力"""
        if not self._tool_registry or not self._tool_executor:
            return RouteResult(
                capability=cap.name,
                text="工具系统未就绪",
                emotion="sad",
                anim="idle",
            )

        # 提取参数
        args = {}
        if cap.extract_args:
            args = cap.extract_args(text)

        # 查找工具
        tool = self._tool_registry.get_tool(cap.tool_name)
        if not tool:
            # 尝试带插件前缀
            tool = self._tool_registry.get_tool(f"{cap.plugin_id}.{cap.tool_name}")
        if not tool:
            return RouteResult(
                capability=cap.name,
                text=f"找不到工具 {cap.tool_name}",
                emotion="sad",
                anim="idle",
            )

        # 执行
        result_text = self._tool_executor.execute(tool, args)
        logger.info("Tool %s executed: %s", cap.name, result_text[:100])

        return RouteResult(
            capability=cap.name,
            text=result_text[:200] if result_text else "执行完成",
            emotion=cap.emotion,
            anim=cap.anim,
            tool_result=result_text,
        )

    def _handle_internal(self, cap: Capability, text: str) -> RouteResult:
        """处理内部能力"""
        if not self._perception:
            return RouteResult(
                capability=cap.name,
                text="感知系统未就绪",
                emotion="sad",
                anim="idle",
            )

        if cap.name == "daily_diary":
            diary = self._perception.generate_daily_diary(preview_only=True)
            return RouteResult(
                capability=cap.name,
                text=diary or "今日暂无活动记录",
                emotion="happy",
                anim="extra",
            )

        elif cap.name == "session_info":
            session = self._perception.get_current_session()
            if session:
                text = (
                    f"当前会话：{session['session_id'][:12]}...\n"
                    f"Agent：{session['agent']}\n"
                    f"消息数：{session['message_count']}\n"
                    f"平台：{session.get('platform', '未知')}\n"
                    f"最近消息：{session.get('last_user_msg', '无')[:50]}"
                )
            else:
                text = "暂无会话信息"
            return RouteResult(
                capability=cap.name,
                text=text,
                emotion="neutral",
                anim="idle",
            )

        elif cap.name == "perception_status":
            status = self._perception.get_perception_status()
            perms = status.get("permissions", {})
            lines = [
                "🔍 感知状态：",
                f"截图: {'✅' if perms.get('screenshot_enabled') else '❌'}",
                f"日报: {'✅' if perms.get('diary_enabled') else '❌'}",
                f"Session: {'✅' if perms.get('session_read_enabled') else '❌'}",
                f"跨Session: {'✅' if perms.get('cross_session_enabled') else '❌'}",
                f"工具调用: {'✅' if perms.get('tool_call_enabled') else '❌'}",
                f"情绪: {status.get('emotion', {}).get('current', 'neutral')}",
            ]
            screen = status.get("screen", {})
            if screen.get("last_description"):
                lines.append(f"屏幕: {screen['last_description'][:50]}")
            return RouteResult(
                capability=cap.name,
                text="\n".join(lines),
                emotion="neutral",
                anim="idle",
            )

        elif cap.name == "screenshot_now":
            event = self._perception._screen.capture_now(mode="manual")
            if event and event.description:
                return RouteResult(
                    capability=cap.name,
                    text=f"屏幕分析：{event.description}",
                    emotion="thinking",
                    anim="idle",
                )
            else:
                return RouteResult(
                    capability=cap.name,
                    text="截图失败或无变化",
                    emotion="neutral",
                    anim="idle",
                )

        return RouteResult(
            capability=cap.name,
            text="未知内部能力",
            emotion="neutral",
            anim="idle",
        )

    def get_available_capabilities(self) -> list[dict]:
        """列出所有可用能力（用于帮助信息）"""
        return [
            {"name": c.name, "patterns": c.patterns, "description": c.description}
            for c in CAPABILITIES
        ]
