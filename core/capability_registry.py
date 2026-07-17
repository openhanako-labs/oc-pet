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
    # ── 音乐 ──
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
        name="search_music",
        patterns=["搜歌", "搜音乐", "找歌", "search music"],
        handler="tool",
        tool_name="play",
        plugin_id="hanako-audio-player",
        extract_args=_extract_search_query,
        description="搜索音乐",
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
    Capability(
        name="music_state",
        patterns=["现在放的什么", "当前播放", "在听什么", "正在播什么"],
        handler="tool",
        tool_name="audio_bus",
        plugin_id="hanako-audio-player",
        extract_args=lambda text: {"action": "state"},
        description="查看当前播放状态",
    ),
    Capability(
        name="clear_playlist",
        patterns=["清空播放列表", "清空列表", "clear playlist"],
        handler="tool",
        tool_name="audio_bus",
        plugin_id="hanako-audio-player",
        extract_args=lambda text: {"action": "clear"},
        description="清空播放列表",
    ),

    # ── 日报与感知 ──
    Capability(
        name="daily_diary",
        patterns=["今天做了什么", "日报", "今日总结", "做了啥", "今天干了啥"],
        handler="internal",
        description="生成今日日报",
    ),
    Capability(
        name="session_info",
        patterns=["当前会话", "session信息", "会话状态", "你在处理什么"],
        handler="internal",
        description="查看当前 Session 信息",
    ),
    Capability(
        name="perception_status",
        patterns=["感知状态", "你在感知什么", "权限状态", "你看到了什么"],
        handler="internal",
        description="查看感知状态",
    ),
    Capability(
        name="screenshot_now",
        patterns=["截个图", "看看屏幕", "截图", "screenshot"],
        handler="internal",
        description="立即截图分析",
        emotion="thinking",
    ),
    Capability(
        name="recent_activities",
        patterns=["最近在干嘛", "最近活动", "活动记录", "recent activities"],
        handler="internal",
        description="查看近期活动",
    ),

    # ── 网页与搜索 ──
    Capability(
        name="web_search",
        patterns=["搜一下", "搜索", "帮我搜", "帮我查", "search", "查一下", "搜搜"],
        handler="tool",
        tool_name="tavily_search",
        plugin_id="tavily-usage-monitor",
        extract_args=_extract_search_query,
        description="网页搜索",
        emotion="thinking",
    ),
    Capability(
        name="fetch_webpage",
        patterns=["打开网页", "读网页", "看看这个链接", "fetch"],
        handler="tool",
        tool_name="fetch_page",
        plugin_id="webpage-archiver",
        extract_args=_extract_url,
        description="读取网页内容",
    ),
    Capability(
        name="save_webpage",
        patterns=["存档网页", "保存网页", "archive"],
        handler="tool",
        tool_name="save_page",
        plugin_id="webpage-archiver",
        extract_args=_extract_url,
        description="存档网页",
    ),

    # ── 图库 ──
    Capability(
        name="search_images",
        patterns=["找图", "搜图", "图片", "相册", "gallery"],
        handler="tool",
        tool_name="gallery_search",
        plugin_id="hanako-gallery",
        extract_args=lambda text: {"keyword": text.replace("找图", "").replace("搜图", "").replace("图片", "").strip()},
        description="搜索图片",
    ),

    # ── RSS ──
    Capability(
        name="check_feeds",
        patterns=["看看订阅", "rss", "有什么新闻", "订阅更新", "feeds"],
        handler="tool",
        tool_name="items",
        plugin_id="hanako-rss",
        extract_args=lambda text: {"action": "list", "mode": "unread", "limit": 5},
        description="查看未读订阅",
    ),

    # ── B 站 ──
    Capability(
        name="search_bilibili",
        patterns=["搜b站", "搜bilibili", "b站搜索", "搜视频"],
        handler="tool",
        tool_name="bilibili_video_intake",
        plugin_id="hanako-bilibili-intake",
        extract_args=lambda text: {"mode": "search", "searchKeyword": re.sub(r"搜(b站|bilibili|视频)", "", text).strip(), "searchLimit": 5},
        description="搜索 B 站视频",
    ),

    # ── 待办 ──
    Capability(
        name="list_todos",
        patterns=["待办", "有什么任务", "todo", "任务清单"],
        handler="tool",
        tool_name="list_undone_tasks_by_time_query",
        plugin_id="todo-list",
        extract_args=lambda text: {"timeRange": "today"},
        description="查看今日待办",
    ),

    # ── 时间统计 ──
    Capability(
        name="time_stats",
        patterns=["时间统计", "今天用了什么", "屏幕时间", "time tracker"],
        handler="tool",
        tool_name="tracker_today",
        plugin_id="hana-time-tracker",
        extract_args=lambda text: {},
        description="今日时间统计",
    ),

    # ── 记忆 ──
    Capability(
        name="export_memory",
        patterns=["导出记忆", "备份记忆", "export memory"],
        handler="internal",
        description="导出记忆快照",
    ),

    # ── 系统 ──
    Capability(
        name="pet_help",
        patterns=["你都会什么", "你能干什么", "help", "帮助"],
        handler="internal",
        description="查看所有能力",
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

        elif cap.name == "recent_activities":
            summary = self._perception._screen.get_activity_summary(minutes=120)
            return RouteResult(
                capability=cap.name,
                text=summary or "最近 2 小时无活动记录",
                emotion="neutral",
                anim="idle",
            )

        elif cap.name == "export_memory":
            if hasattr(self._perception, '_memory_snapshot_mgr'):
                # 尝试通过引擎导出
                return RouteResult(
                    capability=cap.name,
                    text="记忆导出需要通过对话引擎执行",
                    emotion="neutral",
                    anim="idle",
                )
            return RouteResult(
                capability=cap.name,
                text="记忆系统未就绪",
                emotion="sad",
                anim="idle",
            )

        elif cap.name == "pet_help":
            caps = self.get_available_capabilities()
            lines = ["我能做的事："]
            for c in caps:
                patterns = " / ".join(c['patterns'][:3])
                lines.append(f"  • {c['description']}（试试说：{patterns}）")
            return RouteResult(
                capability=cap.name,
                text="\n".join(lines),
                emotion="happy",
                anim="extra",
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
