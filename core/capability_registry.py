"""能力路由器 — 关键词匹配 → 直接执行，跳过 LLM 选工具

注意：仅直连模式 (transport_mode=direct) 使用。
Hanako WS 模式下工具由服务端执行，不需要本地路由。

静态能力（仅 oc-pet 内部能力：日报/会话/感知/截图/记忆/帮助，无法"动态读插件"）；
插件工具（audio_bus 音乐控制 / linjian-peek 手机控制）由 unified_tool_router 从
tool_registry 动态读取 triggers 路由，本模块不再镜像。
LLM 路径：匹配失败 → 回退到 LLM 选工具

设计决策（2026-08-2x R3 架构收敛）：
- **搜索/网页/图库/RSS/B站/待办/时间统计一律走 Hanako LLM 自动选工具**，
  oc-pet 不静态绑定 Hanako 插件。这些能力原先是 oc-pet 侧写 pattern + 参数提取
  替 Hanako 插件"抢活"——web_search（tavily-usage-monitor/tavily_search）、
  fetch_webpage/save_webpage（webpage-archiver）、search_images（hanako-gallery）、
  check_feeds（hanako-rss）、search_bilibili（hanako-bilibili-intake）、
  list_todos（todo-list）、time_stats（hana-time-tracker）已全部从 CAPABILITIES 移除。
  相关触发词（"搜一下"/"查一下"等）不再被本地静态路由拦截，交给 Hanako 端 LLM
  按工具名+描述+参数 schema 语义自动选工具（Hanako 插件工具由 Hanako 服务端执行，
  oc-pet 只做消息转发 + WS 事件回灌气泡/TTS）。
- play_music 保留**收窄版**随机播放（Bug B 修复的过渡方案）：只匹配明确的随机
  播放意图（随机放/随便放/来一首），本地直接从 hanako-audio-player 音乐库
  （~/.hanako/plugin-data/.../playlist.json）随机选一首真实可播曲目调 play 工具。
  pattern 不含"播放"/"暂停"等通用词；带具体歌名/歌手时 _handle_internal 检测
  余量内容返回 None 让位给 LLM（不拦截"播放周杰伦的晴天"）。
- 保留 pause/resume/next/state/clear：这些是确定性动作（audio_bus 固定 action
  参数），本地直达又快又准，不劳烦 LLM。
- 其余自然语言（"播首歌"/"播放周杰伦的晴天"/"搜一下XXX"）走 LLM 工具调用路径：
  LLM 自动选择对应插件工具并传参，像 Hana 原界面一样自然。

用法:
    from core.capability_registry import CapabilityRouter
    router = CapabilityRouter(perception, tool_registry, tool_executor)
    result = router.route("暂停一下")
    if result:
        # 直接执行，不走 LLM
        on_reply(result.text, result.emotion, result.anim, result.audio_path)
"""
from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable, Any

from hanako_home import hanako_home

logger = logging.getLogger(__name__)

# 中文字符（用于能力关键词的"边界"判断，避免中文子串碰撞误匹配，
# 如"接管一下一个对话"中的"一下一个"⊃"下一个"被 next_track 劫持）
_CJK_RE = re.compile(r'[\u4e00-\u9fff]')


def _is_valid_capability_match(text: str, pattern: str, allow_embedded: bool = False) -> bool:
    """判断 pattern 是否作为"独立关键词"命中 text（而非嵌在大词里被夹住）。

    纯子串 `pattern in text` 对中文会产生碰撞：'下一个' 会命中 '接管一下一个对话'
    （下-一-个 恰好连续）。规则：仅当关键词前后都被中文字符夹住时，视为误匹配
    （嵌在更大词内），放过给 LLM；其余情况（开头/结尾/被标点/空格/英文/数字包围）
    视为有效命中。

    ``allow_embedded=True`` 时**只跳过"被中文夹住"那道保护**，
    "关键词到底在不在文本里"这段始终执行。

    （2026-09-19 踩坑：最初写成 `allow_embedded or _is_valid_capability_match(...)`，
    短路把 ``text.find()`` 也一起跳了——于是所有声明豁免的关键词**无条件命中**，
    每一句话都被劫持。是测试抓出来的。）
    """
    idx = text.find(pattern)
    if idx < 0:
        return False
    if allow_embedded:
        return True
    before = text[idx - 1] if idx > 0 else ''
    after = text[idx + len(pattern)] if idx + len(pattern) < len(text) else ''
    if _CJK_RE.match(before or ' ') and _CJK_RE.match(after or ' '):
        return False
    return True


@dataclass
class Capability:
    """一个可路由的能力"""
    name: str                       # 唯一标识，如 "pause_music"
    patterns: list[str]             # 触发词，如 ["暂停", "暂停播放", "停一下"]
    handler: str                    # 处理方式 "tool" / "internal" / "callable"
    tool_name: str = ""             # 工具名（handler=tool 时）
    plugin_id: str = ""             # 插件 ID（handler=tool 时）
    extract_args: Callable = None   # 从文本提取参数的函数
    description: str = ""           # 描述（日志用）
    emotion: str = "happy"          # 执行后的情绪
    anim: str = "extra"             # 执行后的动画
    callable: Callable = None       # handler="callable" 时的处理函数 (text) -> RouteResult|str
    allow_embedded: bool = False    # True = 跳过"被中文夹住即误匹配"保护（见下）


@dataclass
class RouteResult:
    """路由结果"""
    capability: str                 # 匹配的能力名
    text: str = ""                  # 回复文本
    emotion: str = "neutral"        # 情绪
    anim: str = "idle"              # 动画
    audio_path: str = ""            # 音频路径
    tool_result: str = ""           # 工具执行结果


# ── 能力定义 ──────────────────────────────────────────────

CAPABILITIES: list[Capability] = [
    # ── 音乐 ──
    # 注意：play_music 于 2026-08-23 重新恢复（收窄版）。上一轮移除它是因为
    # pattern 太宽（'play'/'播放'）会拦截"播放周杰伦的晴天"等自然语言并把歌名
    # 乱传给 play 工具。本轮恢复只覆盖**明确的随机播放意图**（随机/随便/来一首），
    # 本地直接从 hanako-audio-player 音乐库随机选一首真实可播的曲目调用 play 工具，
    # 不再让 LLM 瞎传 source（Bug B：日志里 play 收到过 source="随便放首歌"）。
    # pattern 不含"播放"/"暂停"等通用词，避免拦截"播放进度/暂停"等表达；
    # 有具体歌名/歌手的请求（如"来一首周杰伦的晴天"）在 _handle_internal 里
    # 检测到余量内容会返回 None 让位给 LLM。search_music 保持移除状态不恢复。
    Capability(
        name="play_music",
        patterns=["随机放", "随机播", "随机播放", "随便放", "随便播", "来一首", "来首歌", "来一曲"],
        handler="internal",
        description="随机播放音乐（从音乐库随机选一首）",
        emotion="happy",
        anim="extra",
    ),
    # 注意：audio_bus 的 pause/resume/next/state/clear 等**不再在此镜像**——
    # 其触发词由 tool_registry 动态提供（外部插件 _EXTERNAL_TOOL_TRIGGERS 集中声明，
    # 单一来源），经 unified_tool_router 关键词路由直达，根除手写镜像表漂移。
    # 仅保留 oc-pet 内部能力（日报/会话/感知/截图/记忆/帮助），这些无法"动态读插件"。
    # 音乐控制类 keywords（下一首/切歌/暂停播放/继续播放/清空播放列表）已统一迁到
    # tool_registry 动态索引；自然语言说法交给 LLM 工具调用。

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
        patterns=["截个图", "看看屏幕", "screenshot"],
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


# ── 外部/插件可动态注册的能力（需求② mc_bridge 等 Python 插件用）──────────
# 与 CAPABILITIES（手写静态列表）并存；route() 同时遍历两者。
# 注册用 register_capability()，卸载用 unregister_capability()。
EXTERNAL_CAPABILITIES: list[Capability] = []


def register_capability(cap: "Capability") -> None:
    """注册一个外部能力（如 mc_bridge 的 mc_task / mc_method）。"""
    if not isinstance(cap, Capability):
        raise TypeError("cap must be a Capability")
    # 同名先卸载，避免重复注册
    unregister_capability(cap.name)
    EXTERNAL_CAPABILITIES.append(cap)
    logger.info("Registered external capability: %s", cap.name)


def unregister_capability(name: str) -> bool:
    """按名称卸载外部能力。"""
    for i, cap in enumerate(EXTERNAL_CAPABILITIES):
        if cap.name == name:
            EXTERNAL_CAPABILITIES.pop(i)
            logger.info("Unregistered external capability: %s", name)
            return True
    return False


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

        关于 ``allow_embedded``：默认走 :func:`_is_valid_capability_match`，
        纯中文关键词被中文字夹住时判为误匹配（防"接管一下一个对话"被
        "下一个"劫持）。但有一类能力的关键词**天然就嵌在中文句子里**，
        例如派活关键词"交给红莉栖"——只要用户写"把这件事交给红莉栖总结一下"，
        它两侧都是中文字，默认规则永远不命中（等于功能装上了却不触发）。
        这类能力由注册方**显式声明** ``allow_embedded=True`` 换成自己的
        约束：pattern 必须自带足够独特的限定（如带具体助手名），
        以避开它想避开的碰撞。默认 False = 原有行为一模一样。
        """
        text_lower = text.strip().lower()
        if not text_lower:
            return None

        for cap in (CAPABILITIES + EXTERNAL_CAPABILITIES):
            for pattern in cap.patterns:
                if _is_valid_capability_match(
                        text_lower, pattern,
                        allow_embedded=getattr(cap, "allow_embedded", False)):
                    logger.info("Capability matched: %s (pattern='%s')", cap.name, pattern)
                    try:
                        if cap.handler == "tool":
                            return self._handle_tool(cap, text)
                        elif cap.handler == "internal":
                            return self._handle_internal(cap, text)
                        elif cap.handler == "callable":
                            return self._handle_callable(cap, text)
                    except Exception as e:
                        logger.warning("Capability %s failed: %s", cap.name, e)
                        return RouteResult(
                            capability=cap.name,
                            text=f"操作失败：{e}",
                            emotion="sad",
                            anim="idle",
                        )
        return None

    def _handle_callable(self, cap: "Capability", text: str) -> RouteResult:
        """处理 callable 类型能力（外部插件注册的处理函数）。"""
        if cap.callable is None:
            return RouteResult(
                capability=cap.name,
                text=f"能力 {cap.name} 未绑定处理函数",
                emotion="sad",
                anim="idle",
            )
        try:
            out = cap.callable(text)
            if isinstance(out, RouteResult):
                return out
            return RouteResult(
                capability=cap.name,
                text=str(out),
                emotion=cap.emotion,
                anim=cap.anim,
            )
        except Exception as e:
            logger.warning("Capability %s callable failed: %s", cap.name, e)
            return RouteResult(
                capability=cap.name,
                text=f"操作失败：{e}",
                emotion="sad",
                anim="idle",
            )

    # ── 随机播放（Bug B 本地直达） ──────────────────────────

    # 随机意图触发词（与 CAPABILITIES 里 play_music.patterns 保持一致）
    _RANDOM_TRIGGERS = (
        "随机放", "随机播", "随机播放", "随便放", "随便播",
        "来一首", "来首歌", "来一曲",
    )
    # 去除后用于判断"是否还有具体点歌内容"的填充词（歌名/歌手/描述除外）
    _MUSIC_FILLER = (
        "随机", "随便", "放", "播", "播放", "来一首", "来首歌", "来一曲",
        "一首", "首歌", "一曲", "首", "个", "歌", "曲", "音乐", "歌曲",
        "帮我", "给我", "想听", "听听", "一下", "吧", "呀", "啊", "呗", "呢", "的",
    )

    def _has_specific_music_request(self, text: str) -> bool:
        """判断"随机放X"类文本里是否夹带了具体点歌内容。

        去掉随机触发词 + 常见填充词后仍剩非空内容 → 用户其实想点某首歌/某个歌手
        （如"来一首周杰伦的晴天"剩"周杰伦晴天"）→ 返回 True，让位给 LLM。
        纯随机意图（"随机放首歌"/"帮我随便放首歌听听"）去除后为空 → False，本地直达。
        """
        if not text:
            return False
        remaining = str(text)
        for token in self._RANDOM_TRIGGERS + self._MUSIC_FILLER:
            remaining = remaining.replace(token, "")
        remaining = re.sub(r"[\s，。！？!?、·~～,.]+", "", remaining)
        return bool(remaining.strip())

    def _pick_random_music(self) -> Optional[tuple[str, str]]:
        """从 hanako-audio-player 音乐库随机选一首真实可播的在线音乐。

        Returns:
            (name, url)；音乐库不可用/无可播曲目返回 None。
        只选 http(s) URL——widget media 相对路径（/api/plugins/...）是 TTS 产物，
        直接传给本地 play 工具会被当成不存在的本地路径，产生"空播/错播"。
        """
        playlist_path = (
            hanako_home() / "plugin-data" / "hanako-audio-player" / "playlist.json"
        )
        if not playlist_path.exists():
            logger.warning("play_music: 音乐库不存在 %s", playlist_path)
            return None
        try:
            data = json.loads(playlist_path.read_text("utf-8"))
        except Exception as e:
            logger.warning("play_music: 读取音乐库失败: %s", e)
            return None
        if not isinstance(data, list):
            return None
        candidates = [
            item for item in data
            if isinstance(item, dict)
            and str(item.get("url", "")).startswith(("http://", "https://"))
            and str(item.get("name", "")).strip()
        ]
        if not candidates:
            logger.warning("play_music: 音乐库没有可播的在线曲目（共 %d 条）", len(data))
            return None
        chosen = random.choice(candidates)
        return str(chosen.get("name", "")).strip(), str(chosen.get("url", "")).strip()

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
        # play_music 不依赖感知系统，必须在 perception 守卫之前处理——
        # 否则未注入 perception 时永远返回"感知系统未就绪"（Bug B 回归风险）。
        if cap.name == "play_music":
            # 收窄版随机播放：检测文本里是否还有"具体点歌"内容（歌名/歌手/描述）。
            # 有 → 返回 None 让位给 LLM（不拦截"来一首周杰伦的晴天"这类显式点歌）。
            if self._has_specific_music_request(text):
                logger.info("play_music 检测到具体点歌内容，让位 LLM: %s", text[:50])
                return None
            picked = self._pick_random_music()
            if not picked:
                return RouteResult(
                    capability=cap.name,
                    text="音乐库还没有可播放的歌曲，先去音频播放器里加几首吧～",
                    emotion="sad",
                    anim="idle",
                )
            name, url = picked
            tool = self._tool_registry.get_tool("play") if self._tool_registry else None
            if tool is None and self._tool_registry is not None:
                tool = self._tool_registry.get_tool("hanako-audio-player.play")
            if tool is None or self._tool_executor is None:
                return RouteResult(
                    capability=cap.name,
                    text=f"好呀，随机播放：{name}（播放工具暂不可用）",
                    emotion="happy",
                    anim="extra",
                )
            try:
                result_text = self._tool_executor.execute(
                    tool, {"source": url, "title": name}
                ) or ""
                text_reply = f"好呀，随机播放一首：{name}\n{result_text}" if result_text else f"好呀，随机播放一首：{name}"
            except Exception as e:
                logger.warning("play_music 执行失败: %s", e)
                text_reply = f"随机播放失败了：{e}"
            return RouteResult(
                capability=cap.name,
                text=text_reply[:200],
                emotion="happy",
                anim="extra",
                tool_result=text_reply,
            )

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
            # 异步截图：避免 Vision API 阻塞对话线程（最长 30s 超时）
            self._perception._screen.capture_async(mode="manual")
            return RouteResult(
                capability=cap.name,
                text="📸 正在分析屏幕...",
                emotion="thinking",
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
