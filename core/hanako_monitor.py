"""
Hanako 状态监控模块 — 增强版

轮询 TODO 文件 + 通知文件 + 回复文件 + 情绪映射
集成 HanakoPro 风格的气泡精简和事件驱动情绪映射

事件驱动情绪映射通过 WebSocket 实时推送（BridgeClient.on_event）。
"""

import json
import logging
import re
import time
from pathlib import Path

from config import EXPRESSION_MAP, HANAKO_STATE_MAP
from paths import NOTIFY_FILE, RESPONSE_FILE

logger = logging.getLogger(__name__)

TODO_FILE = Path.home() / ".hanako/plugin-data/todo/todos.json"
# NOTIFY_FILE imported from paths
# RESPONSE_FILE imported from paths

# 会话路径 → agent 归属：HanaAgent 把每个助手的会话放在
# `~/.hanako/agents/<agent_id>/{sessions,subagent-sessions,workflow-sessions}/` 下。
# 取 `agents/` 后那一段即可——**刻意不限定 `/sessions/`**：子代理会话在
# `agents/<agent_id>/subagent-sessions/` 下，旧实现只认 `/sessions/`，于是
# 子进程会话被判成"判断不出归属"，走保守放行混了进来。
AGENT_DIR_RE = re.compile(r"/agents/([^/]+)/")

# ── 气泡精简算法（移植自 HanakoPro） ─────────────────────

BUBBLE_MAX_CHARS = 72
BUBBLE_MIN_SENTENCE_LEN = 8

# ── BugFix #5-E：celebrating 带摘要汇报阈值 ───────────────
# 摘要清洗后至少这么长才算"有实质摘要"（否则维持原庆祝动画）
TOOL_END_SUMMARY_MIN_CHARS = 8
# 工具链耗时超过该秒数视为"长任务"（无摘要文本时也主动汇报）
LONG_TOOL_MIN_SECONDS = 30.0

# ── 庆祝时机（2026-09-21）─────────────────────
# 庆祝的**触发点**从 tool_end success 改到了 turn_end：
#
#   旧行为：一次对话里几十个工具调用 = 几十次「完成啦！」撒花 + 语音播报。
#           实测 19:28–19:30 三分钟内播了 6 次（工具结束只是中间步骤，
#           不是"做完了"）。
#   新行为：只在一轮对话真的结束时庆祝一次。
#
# ⚠️ 这条链路历史上是**死的**（监视归属绑错键，助手自己的对话被挡在门外），
# 所以它的强度从没被真实流量检验过；2026-09-21 修复生效后才暴露。

#: 两次庆祝之间的最小间隔（秒）。只用于防刷屏（WS 重放/镜像/子代理会话
#: 都可能带来成串的 turn_end），不承担"每次都不播"的语义。
CELEBRATE_MIN_INTERVAL = 10.0

#: 只有"本回合真跑过工具"才庆祝。否则每句闲聊（"谢谢"）都撒花播报。
CELEBRATE_REQUIRES_TOOL = True


def clean_bubble_text(text: str) -> str:
    """清理气泡文本：去代码块、markdown、HTML 标签、元信息。
    移植自 HanakoPro 的 cleanPetChatText()。
    """
    if not text:
        return ""
    # BugFix #4：整段剥离 <mood>...</mood> 内省块（Vibe/Reflections/Will/Sparks
    # 字段是给服务端/记忆用的元数据，不是给用户的回复）。必须在 HTML 标签剥离
    # 之前做——否则 <mood> 标签被剥掉后只剩 Vibe 文本，后面的前缀剥离会漏。
    text = re.sub(r'<mood>.*?</mood>', ' ', text, flags=re.IGNORECASE | re.DOTALL)
    # BugFix #4：剥离结构化的 mood 前缀块（无 <mood> 包裹的纯文本形式）。
    # LLM 输出或历史恢复常以 "Vibe: ..." / "Reflections: ..." / "Will: ..." /
    # "Sparks: ..." 开头（一行或多行），全部剥到该段末尾，不上气泡。
    text = re.sub(
        r'(?im)^\s*(?:Vibe|Reflections|Will|Sparks)\s*[:：][^\n]*(?:\n|$)',
        ' ', text,
    )
    # 去代码块
    text = re.sub(r'```[\s\S]*?```', ' ', text)
    # 去行内代码
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # 去 HTML 标签
    text = re.sub(r'<[^>]+>', ' ', text)
    # 去 markdown 格式（标题、加粗、列表、引用）
    text = re.sub(r'^[\s*#>-]+', '', text, flags=re.MULTILINE)
    # 去方括号协议标签：`[mood:xxx]` / `[feel:v,a]` / `[do:xxx]` 及模型自创变体，
    # 以及方括号形式的 mood 内省块（`[Vibe: xxx]` / `[Sparks: xxx]`）。
    #
    # 2026-09-17：必须在下面的「元信息剥离」**之前**做。
    # 根因：模型会输出 `[mood:被冷落] 你倒是说点什么啊——…吧。`，而下面的
    # MOOD 正则从 "mood:" 一路吃到句末标点，只留下一个孤立的 "[" 和 "。"，
    # 气泡上就显示成 `[ 。`（实测 2026-09-16 12:37 / 12:43 两条）。
    # 先把整对标签消费掉，就不会留下半截括号残渣。
    #
    # 方括号 Vibe 块也要在这里剥：`clean_bubble_text` 是独立的清洗入口
    # （hanako_monitor 的文件回退路径直接调它，不经过 parse_emotion），
    # 只靠上面的纯文本行剥离会漏掉带方括号的形式。
    text = re.sub(
        r'\[\s*(?:mood|feel|do|emotion|expression|action|duration|message|vibe|sparks|reflections|will)\s*[:=][^\]]*\]',
        ' ', text, flags=re.IGNORECASE,
    )
    # 去 MOOD/thinking/tool/status 等元信息。
    #
    # 2026-09-17：冒号从可选改为**必需**。原 `[:：]?` 配合 `[^\n。！？!?]*`
    # 会在正文里出现这些词时把整句吃到句末标点：
    #   "你 mood 看起来不错。" → "你 。"
    # 这类词当标签用时一定带冒号（"mood: 被冷落"），要求冒号即可区分。
    text = re.sub(r'\b(?:MOOD|mood|thinking|tool|status)\s*[:：][^\n。！？!?]*', ' ', text, flags=re.IGNORECASE)
    # 去 emotion 标签（如 [emotion: curious]）—— 这是 LLM 内部情绪标记，
    # 应被解析为表情/动画，不应出现在气泡文本中。
    text = re.sub(r'\[\s*emotion\s*[:=]\s*[^\]\n]+\]\s*', ' ', text, flags=re.IGNORECASE)
    # 去引号和括号
    text = re.sub(r'[{}\\]"\'`]', ' ', text)
    # 去掉残留的孤立方括号（上面元信息剥离吃掉了标签内容，可能只剩半截 "["）。
    text = re.sub(r'(?:^|\s)[\[\]{}()]+(?=\s|$)', ' ', text)
    # 压缩空白
    text = re.sub(r'\s+', ' ', text).strip()
    # 清洗后只剩标点/括号的残渣（如 "[ 。"）→ 视为空，不上气泡。
    # 判据：去掉空白、方括号和常见中英标点后什么都不剩。
    if not re.sub(r'[\s\[\]()（）【】{}。，、,.!?！？~…—－-]', '', text):
        return ""
    return text


def compact_bubble_text(text: str) -> str:
    """将文本压缩为适合气泡显示的短句（≤72字）。
    移植自 HanakoPro 的 compactPetChatText()。
    """
    normalized = clean_bubble_text(text)
    if not normalized:
        return ""
    # 按句末标点拆分
    sentences = [
        s.strip()
        for s in re.split(r'(?<=[。！？!?])\s*', normalized)
        if s.strip()
    ]
    # 气泡显示完整回复，超长才截断
    candidate = normalized
    if len(candidate) <= BUBBLE_MAX_CHARS:
        return candidate
    # 超长：取前两句
    if len(sentences) >= 2:
        first_two = sentences[0] + sentences[1]
        if len(first_two) <= BUBBLE_MAX_CHARS:
            return first_two
    return candidate[:BUBBLE_MAX_CHARS - 1] + "…"


# ── 关键词堆检测（2026-09-17）──────────────────────────
#
# 现象（日志 13:24:09）：屏幕主动评论返回的不是句子，而是词语罗列：
#     LLM 回复: 桌宠 鼓励 用户 阅读 技术博客 桌面宠物 交互风格
#
# 根因：模板把整段指令（含动作标签说明与多个示例）发给 LLM，
# 而 {detail} 填的是场景摘要（非具体内容），模型缺素材时把指令里的
# 关键词复述了出来。模板已瘦身，此处再加一道**与来源无关的兜底**。
#
# 判据（基于实测 33 条回复校准）：
#   · 空格分隔的 token ≥ 4
#   · 且全句没有任何句读标点（。！？，、；：等）
#   · 且含中文（避免误伤正常英文短语，如 "Java Spring Boot"）
#
# 为什么这组判据：正常中文回复词间无空格（整句连写），而关键词堆
# 是空格分隔的短 token。实测 33 条中仅 1 条命中，且无误伤。
KEYWORD_SALAD_MIN_TOKENS = 4
# 平均 token 长度上限：关键词堆是短词罗列（“桌宠 鼓励 用户”平均 2 字）；
# 中英混排正常句里的英文词较长，平均会被拉上去。
KEYWORD_SALAD_AVG_TOKEN_LEN = 3.0
_SENTENCE_PUNCT_RE = re.compile(r"[。！？，、；：…～!?,;:]")


def looks_like_keyword_salad(text: str) -> bool:
    """这段文本是否像「关键词堆」（词语罗列而非句子）？

    真值时调用方应丢弃它、不上气泡。容错：空文本返回 False
    （交由现有判空逻辑处理，本函数只管「堆」这一种病）。

    判据（三层，全部满足才算）：
      1. 无句读标点（有标点就是句子）
      2. 含中文
      3. 空格分隔的 token ≥ 4，**且平均 token 长度 ≤ 3**

    第 3 条的「平均长度」是关键：关键词堆是「桌宠 鼓励 用户 阅读」
    这种短词，而中英混排正常句（"Skyrim modding 和 Java 同时开着"）
    里的英文词较长，平均会被拉上去。
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    # 1) 有句读标点 → 是句子，不是堆
    if _SENTENCE_PUNCT_RE.search(stripped):
        return False
    # 2) 无中文 → 可能是正常英文短语（如 "Java Spring Boot"），不判
    if not re.search(r"[\u4e00-\u9fff]", stripped):
        return False
    tokens = [t for t in stripped.split() if t]
    if len(tokens) < KEYWORD_SALAD_MIN_TOKENS:
        return False
    # 3) 平均 token 长度短 → 堆
    avg_len = sum(len(t) for t in tokens) / len(tokens)
    return avg_len <= KEYWORD_SALAD_AVG_TOKEN_LEN


# ── 事件驱动情绪映射（移植自 HanakoPro） ───────────────────

EVENT_TO_MOOD = {
    "thinking_start": "thinking",
    "thinking_delta": "thinking",
    "text_delta": "talking",
    "mood_text": "talking",
    "vision_progress": "working",
    "file_write_prepare": "working",
    "tool_start": "working",
    "tool_progress": "working",
    "tool_end": "idle",  # M4: 工具完成 → 由 map_event_to_mood 里的 success 分支重新映射
    "turn_end": "idle",
}

# M4: HanakoMonitor 订阅的事件类型（明确订阅避免被无关事件刷屏）
MONITOR_EVENT_TYPES = {
    "thinking_start",
    "thinking_delta",
    "text_delta",
    "mood_start",   # P0：补齐边界，用于 mood 内省块累积
    "mood_text",
    "mood_end",     # P0：补齐边界
    "vision_progress",
    "file_write_prepare",
    "tool_start",
    "tool_progress",
    "tool_end",
    "turn_end",
}

# tool_start 的 name → 具体消息映射
TOOL_NAME_MESSAGES = {
    "write": "编辑中",
    "edit": "编辑中",
    "patch": "编辑中",
    "replace": "编辑中",
    "create": "编辑中",
    "delete": "编辑中",
    "file": "编辑中",
    "todo": "编辑中",
    "notebook": "编辑中",
    "bash": "执行中",
    "terminal": "执行中",
    "shell": "执行中",
    "command": "执行中",
    "run": "执行中",
    "exec": "执行中",
    "browser": "浏览中",
    "search": "浏览中",
    "web": "浏览中",
    "fetch": "浏览中",
    "url": "浏览中",
    "open": "浏览中",
    "computer": "观察中",
    "screen": "观察中",
    "screenshot": "观察中",
    "vision": "观察中",
    "image": "观察中",
    "camera": "观察中",
}


def compact_tool_name(name: str) -> str:
    """压缩工具名用于气泡显示。移植自 HanakoPro。"""
    if not name or not isinstance(name, str):
        return "工具"
    # 去前缀
    name = re.sub(r'^mcp[_:.]', '', name, flags=re.IGNORECASE)
    # 去多余分隔符
    name = re.sub(r'[_\-.]+', ' ', name).strip()
    return name[:32] or "工具"


def event_tool_message(event: dict) -> str:
    """根据 tool_start 事件推断具体消息。移植自 HanakoPro。"""
    name = (
        event.get("name")
        or event.get("toolName")
        or event.get("tool")
        or event.get("action")
        or ""
    ).lower()
    for key, msg in TOOL_NAME_MESSAGES.items():
        if key in name:
            return msg
    return compact_tool_name(event.get("name", "工具"))


def map_event_to_mood(event: dict) -> tuple:
    """将事件映射为 (mood, message, emotion) 三元组。
    移植自 HanakoPro 的 mapDesktopPetEventToState()，扩展 emotion 字段。
    返回 (mood, message, emotion) 或 None（无法映射）。
    """
    if not event or not isinstance(event, dict):
        return None
    event_type = event.get("type", "")
    if event_type not in EVENT_TO_MOOD:
        return None

    mood = EVENT_TO_MOOD[event_type]
    emotion = "neutral"
    message = ""

    if event_type in ("thinking_start", "thinking_delta"):
        message = "思考中"
        emotion = "thinking"
    elif event_type in ("text_delta", "mood_text"):
        message = "回复中"
        if event_type == "mood_text":
            # P0 修复：从 mood_text.delta 解析真实情绪词
            emotion = _mood_keyword_score(str(event.get("delta") or event.get("data") or ""))
        else:
            emotion = "neutral"
    elif event_type == "vision_progress":
        message = "观察中"
        emotion = "neutral"
    elif event_type == "file_write_prepare":
        message = "编辑中"
        emotion = "neutral"
    elif event_type in ("tool_start", "tool_progress"):
        message = event_tool_message(event)
        emotion = "neutral"
    elif event_type == "turn_end":
        # 2026-09-21：庆祝**挪到这里** —— 只有真正的对话回合结束才庆祝。
        # 原先挂在 tool_end success 上：一次对话里几十个工具调用 = 几十次
        # 「完成啦！」撒花 + 语音播报（实测 19:28–19:30 三分钟内 6 次）。
        # 工具结束只是中间步骤，不是"做完了"；回合结束才是用户能感知的那件事。
        mood = "celebrating"
        message = ""   # 气泡交给庆祝分支；这里不塞字，避免覆盖对话回复
        emotion = "happy"

    # tool_end 特殊处理（有 success 字段）
    # G：failure → error（遇到问题）；2026-09-21 起 success **不再庆祝**（见上），
    # 只把状态钉在 working，免得工具链中途被推回 idle 再弹起来。
    if event_type == "tool_end":
        if event.get("success", True):
            mood = "working"
            message = ""
            emotion = "neutral"
        else:
            mood = "error"
            message = "遇到问题"
            emotion = "angry"

    return (mood, message, emotion)

# 情绪关键词 → 表情映射（从回复文本中检测）
# 规则：用具体词汇避免误判，不依赖标点符号
EMOTION_KEYWORDS = {
    "happy": ["哈", "笑", "开心", "好耶", "太棒了", "嘻嘻", "嘿嘿", "哈哈", "乐", "高兴", "可爱", "棒", "赞"],
    "sad": ["呜", "难过", "伤心", "哭", "呜呜", "sad", "emo", "叹气", "唉", "失落", "委屈"],
    "angry": ["哼", "气", "怒", "可恶", "混蛋", "烦", "啊啊", "受不了", "滚", "讨厌"],
    "surprised": ["诶", "欸", "什么", "不会吧", "哇", "真的假的", "居然", "竟然", "没想到"],
    "thinking": ["嗯", "让我想想", "思考", "…", "...", "琢磨", "考虑", "分析一下", "等等"],
    "cute": ["喵", "呐", "呢", "哦～", "哦~", "嘛", "啾", "贴贴", "蹭蹭", "摸摸"],
    "missing": ["走了？", "去哪了", "还在吗", "人呢", "消失", "离开"],
}

# ── mood_text → 情绪映射（MOOD 意识流内省块） ─────────────────
# Hanako 在 <mood>...</mood> 内以 "Vibe: 好奇 / Will: … / Sparks: …" 的
# 结构化文本输出内省状态，Vibe 字段即情绪基调。把这些真实情绪词映射到
# EXPRESSION_MAP 的 emotion key，驱动 Live2D 表情（而不仅靠 [emotion:xxx] 标签）。
MOOD_WORD_TO_EMOTION = {
    "happy": ["开心", "高兴", "愉快", "快乐", "兴奋", "愉悦", "轻松", "雀跃", "喜悦", "欢快", "温暖", "甜蜜", "欣慰", "自豪", "好", "haha", "lol", "excited", "joyful", "glad", "delighted", "happy"],
    "surprised": ["好奇", "疑惑", "惊讶", "吃惊", "震惊", "意外", "诧异", "惊叹", "真的假的", "不会吧", "哇", "诶", "curious", "surprised", "shocked", "amazed", "wow"],
    "angry": ["生气", "愤怒", "恼火", "不满", "烦躁", "恼", "气", "炸毛", "不耐烦", "讨厌", "可恶", "不爽", "angry", "annoyed", "frustrated", "mad"],
    "sad": ["难过", "伤心", "低落", "委屈", "沮丧", "失落", "哀伤", "悲伤", "郁闷", "想哭", "惆怅", "心疼", "sad", "down", "upset", "melancholy", "heartbroken"],
    "thinking": ["平静", "思考", "认真", "沉稳", "冷静", "沉思", "琢磨", "专注", "梳理", "权衡", "端详", "calm", "focused", "thinking", "pondering", "deliberate"],
    "working": ["干劲", "投入", "忙碌", "高效", "行动", "推进", "执行", "解决", "工作", "working", "busy", "productive"],
    "cute": ["温柔", "宠溺", "卖萌", "撒娇", "软", "亲昵", "可爱", "黏人", "甜甜的", "cute", "gentle", "affectionate", "playful"],
    "missing": ["想念", "惦记", "担心", "牵挂", "不安", "等你", "想念你", "missing", "worried", "anxious"],
}
MOOD_EMOTION_PRIORITY = ["angry", "sad", "surprised", "happy", "thinking", "working", "cute", "missing"]


def _mood_keyword_score(candidate: str) -> str:
    """对 mood 文本做情绪词计分，返回得分最高的 emotion key。"""
    if not candidate:
        return "neutral"
    lowered = candidate.lower()
    scores = {k: 0 for k in MOOD_WORD_TO_EMOTION}
    for emotion, words in MOOD_WORD_TO_EMOTION.items():
        for word in words:
            if word in lowered or word in candidate:
                scores[emotion] += 1
    best = "neutral"
    best_score = 0
    for emotion in MOOD_EMOTION_PRIORITY:
        if scores[emotion] > best_score:
            best_score = scores[emotion]
            best = emotion
    return best


def mood_text_to_emotion(text: str) -> str:
    """把 <mood> 内省块累积文本映射为 emotion key。

    优先解析 "Vibe:" 字段（情绪基调）；无 Vibe 或未命中时对整段文本计分；
    都未命中返回 "neutral"。结果直接喂 EXPRESSION_MAP 选择 Live2D 表情序列。
    """
    if not text or not isinstance(text, str):
        return "neutral"
    vibe = ""
    for line in text.splitlines():
        m = re.match(r"^\s*Vibe\s*[:：]\s*(.+?)\s*$", line)
        if m:
            vibe = m.group(1)
            break
    if not vibe:
        m = re.search(r"Vibe\s*[:：]\s*([^\n<]+)", text)
        if m:
            vibe = m.group(1).strip()
    if vibe:
        emotion = _mood_keyword_score(vibe)
        if emotion != "neutral":
            return emotion
    return _mood_keyword_score(text)

# E-watchdog: 最后一次事件后超过此秒数且未收到 turn_end，强制回 idle。
# 由 push_event() 维护事件心跳（_last_update），tick() 消费本常量：
# 若 turn_end 丢失/WS 断流，状态可能卡在 thinking/working，15s 后兜底回 idle。
WATCHDOG_TIMEOUT = 15.0


# 状态名称与显示文本
STATE_LABELS = {
    "idle": "⚪ 空闲",
    "listening": "👂 倾听",
    "thinking": "💭 思考",
    "working": "🔧 工作",
    "speaking": "💬 说话",
    "happy": "😊 开心",
    "error": "⚠️ 异常",
    "cute": "✨ 卖萌",
    "missing": "🔍 张望",
    "celebrating": "🎉 完成",  # G：庆祝态（状态指示器容忍未知状态，缺失时显示原样）
}


class HanakoMonitor:
    def __init__(self, on_state_change=None):
        self._on_state_change = on_state_change
        self._current_anim = "idle"
        self._last_state = None
        self._last_update = 0
        self._last_todo_count = -1
        self._ws_connected = False
        # 会话过滤：一个桌宠只观测自己对应助手的会话，
        # 不转播其他 agent 的活动（避免“没操作却一直在动”）。
        self._agent_id: str | None = None
        self._session_manager = None
        # 情绪缓存（用于气泡颜色）
        self._current_emotion = "neutral"
        # 状态推断
        self._current_state_name = "idle"
        self._last_response_time = 0
        self._last_response_ts = 0  # response.json 的最后 ts（用于检测更新）
        self._last_audio_path = ""  # 最后播放的音频路径
        self._pending_notification_count = 0
        # P0：mood_text 内省块累积（mood_start..mood_end 之间），
        # 用于把真实情绪词映射到 emotion key，驱动 Live2D 表情。
        self._mood_acc = ""
        # P2：按 mood 分别节流（thinking/working/talking 互不干扰）
        self._mood_last_push: dict[str, float] = {}
        # P2-8：tool_end 独立节流——success→celebrating 会触发撒花+完工音，
        # WS 重放/镜像/工具链连续成功会产生高频 tool_end，10s 窗口内只推一次。
        self._tool_end_last_push = 0.0
        # BugFix #5-E：缓存最近一次成功 tool_end 事件（celebrating 带摘要汇报用）
        self._last_tool_end_event: dict = {}
        # 2026-09-21：本回合有没有跑过工具 + 上次庆祝时刻（见 CELEBRATE_* 常量）
        self._turn_had_tool = False
        self._celebrate_last_push = 0.0

    # ── BugFix #5-E：tool_end 摘要提取 ─────────────────────

    def get_last_tool_end_summary(self, max_chars: int = 72) -> str:
        """最近一次成功 tool_end 的摘要文本（无实质摘要返回空串）。

        数据来源：tool_end 事件的 details（dict.text/content 或 str）或 summary
        字段；清洗（clean_bubble_text）+ 精简（compact_bubble_text）后返回。
        清洗后长度 < TOOL_END_SUMMARY_MIN_CHARS 视为无实质摘要；此时若工具链
        耗时 >= LONG_TOOL_MIN_SECONDS（长任务），回退"长任务完成啦"占位，
        否则返回空串（调用方维持原庆祝动画）。
        """
        event = self._last_tool_end_event or {}
        raw = ""
        details = event.get("details")
        if isinstance(details, dict):
            raw = str(details.get("text") or details.get("content") or "")
        elif isinstance(details, str):
            raw = details
        if not raw:
            raw = str(event.get("summary") or "")
        raw = (raw or "").strip()
        if raw:
            cleaned = clean_bubble_text(raw)
            if len(cleaned) >= TOOL_END_SUMMARY_MIN_CHARS:
                return compact_bubble_text(cleaned)[:max_chars]
        duration = self._tool_end_duration_seconds(event)
        if duration is not None and duration >= LONG_TOOL_MIN_SECONDS:
            return "长任务完成啦"
        return ""

    @staticmethod
    def _tool_end_duration_seconds(event: dict) -> float | None:
        """从 tool_end 事件提取耗时（秒）；字段缺失/非法返回 None。"""
        if not event:
            return None
        for key in ("duration", "elapsedMs", "elapsed_ms", "costMs", "elapsed", "duration_ms"):
            v = event.get(key)
            if v is None:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            return v / 1000.0 if key in ("elapsedMs", "elapsed_ms", "costMs", "duration_ms") else v
        return None


    def tick(self):
        now = time.time()
        derived_state = "idle"

        # 1. TODO → 工作状态
        todos = self._read_todos()
        if len(todos) != self._last_todo_count:
            self._last_todo_count = len(todos)
            if todos:
                lines = [f"📋 {t['text'][:20]}" for t in todos[:2]]
                if len(todos) > 2:
                    lines.append(f"⋯ 还有 {len(todos)-2} 条")
                msg = "\n".join(lines)
                derived_state = "working"
            else:
                msg = ""
            if msg:
                self._set_if_changed("idle", msg, emotion="neutral", state="working")

        # 2. 通知
        notifications = self._read_notifications()
        for n in notifications:
            self._set_if_changed("extra", n.get("text", ""), emotion="neutral", state="listening")
            derived_state = "listening"

        # 3. 回复 — WS 连接时实时推送，文件轮询作为 fallback
        if self._ws_connected:
            # WS 模式下，回复通过 on_message 回调已经推送到 PetWindow
            # tick() 不需要再读 response.json
            pass
        else:
            reply, audio_path = self._read_response()
            if reply:
                self._last_response_time = now
                emotion = self._detect_emotion(reply)
                mapped = EXPRESSION_MAP.get(emotion, ("idle", None, None))
                anim = mapped[0] if isinstance(mapped, tuple) else mapped
                # 用精简后的文本显示气泡
                compact_reply = compact_bubble_text(reply)
                self._set_if_changed(anim, compact_reply, emotion=emotion, state="speaking",
                                     audio_path=audio_path)
                derived_state = "speaking"
            elif audio_path:
                self._last_response_time = now
                self._set_if_changed("idle", "", emotion="neutral", state="speaking",
                                     audio_path=audio_path)

        # 4. 状态回退：说完话后保持 speaking 状态短暂时间
        if derived_state == "idle" and self._last_response_time > 0:
            if now - self._last_response_time < 3.0:
                derived_state = "speaking"

        # 5. E-watchdog：最后事件超过 WATCHDOG_TIMEOUT 秒且未收到 turn_end → 强制回 idle。
        #    _last_update 由 push_event() 心跳维护（WS 事件模式）；文件轮询模式为 0 不触发。
        if self._last_update > 0 and (now - self._last_update) > WATCHDOG_TIMEOUT:
            self._set_if_changed("idle", "", emotion="neutral", state="idle")
            derived_state = "idle"

        # 更新持久化的状态名（用于状态指示器）
        if derived_state != self._current_state_name:
            self._current_state_name = derived_state
            self._set_if_changed(
                self._current_anim,
                "",
                emotion=self._current_emotion,
                state=derived_state,
                is_state_only=True
            )

    def _detect_emotion(self, text: str) -> str:
        """从文本中检测情绪。
        返回 emotion key（happy/sad/angry/surprised/thinking/neutral）。
        同分时优先 angry > sad > surprised > happy > thinking。
        """
        scores = {k: 0 for k in EMOTION_KEYWORDS}
        text_lower = text.lower()
        for emotion, keywords in EMOTION_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in text_lower:
                    scores[emotion] += 1
        # 取最高分，同分按优先级
        max_score = max(scores.values())
        if max_score == 0:
            return "neutral"
        priority = ["angry", "sad", "surprised", "happy", "thinking"]
        for em in priority:
            if scores[em] == max_score:
                return em
        return "neutral"

    def _read_todos(self):
        try:
            if not TODO_FILE.exists():
                return []
            data = json.loads(TODO_FILE.read_text("utf-8"))
            return [t for t in data.get("todos", []) if not t.get("done")]
        except Exception as e:
            logger.warning("_read_todos failed: %s", e)
            return []

    def _read_notifications(self):
        try:
            if not NOTIFY_FILE.exists():
                return []
            notes = json.loads(NOTIFY_FILE.read_text("utf-8"))
            # 先读再清（防止清空失败重复触发）
            try:
                NOTIFY_FILE.write_text("[]", "utf-8")
            except Exception as e:
                logger.warning("failed to clear notifications: %s", e)
            return notes
        except Exception as e:
            logger.warning("_read_notifications failed: %s", e)
            return []

    def _read_response(self):
        """读取回复文件，有新回复时返回文字。
        不再清空文件，改用时间戳检测更新（支持先文本后音频的两步写入）。
        """
        try:
            if not RESPONSE_FILE.exists():
                return "", ""
            raw = RESPONSE_FILE.read_text("utf-8").strip()
            if not raw:
                return "", ""
            data = json.loads(raw)
            reply = data.get("reply", "")
            audio_path = data.get("audioPath", "")
            ts = data.get("ts", 0)

            # 用时间戳判断是否是新回复
            if ts <= self._last_response_ts:
                # 不是新回复，但检查是否有新音频
                if audio_path and audio_path != self._last_audio_path:
                    self._last_audio_path = audio_path
                    return "", audio_path  # 只返回音频，不重复显示文本
                return "", ""

            self._last_response_ts = ts
            self._last_audio_path = audio_path
            return reply, audio_path
        except Exception as e:
            logger.warning("_read_response failed: %s", e)
            return "", ""

    def _set_if_changed(self, anim, msg, emotion="neutral", state="idle", is_state_only=False, audio_path=""):
        if anim != self._current_anim or msg or is_state_only:
            self._current_anim = anim
            self._current_emotion = emotion
            self._current_state_name = state
            if self._on_state_change:
                self._on_state_change(anim, msg, emotion=emotion, state=state, audio_path=audio_path)

    def force_idle(self):
        self._last_state = None
        self._current_emotion = "neutral"
        self._current_state_name = "idle"
        self._set_if_changed("idle", "", emotion="neutral", state="idle")

    @property
    def current_emotion(self):
        return self._current_emotion

    @property
    def current_state_name(self):
        return self._current_state_name

    @property
    def ws_connected(self) -> bool:
        return self._ws_connected

    def set_ws_connected(self, connected: bool):
        self._ws_connected = connected
        if connected:
            logger.info("HanakoMonitor: WS connected, disabling file poll")
        else:
            logger.info("HanakoMonitor: WS disconnected, re-enabling file poll")

    def set_ws_client(self, ws_client) -> None:
        """订阅共享 WS 客户端的事件 — M4: 复用 HanakoWSClient，不重建连接。

        ws_client 应提供：
          - subscribe(callback, event_types=set) -> Subscription
          - subscribe_state(callback) -> Subscription  (callback 签名: (state, err))
        """
        if ws_client is None:
            logger.warning("set_ws_client(None) - 保持文件轮询")
            return

        try:
            sub = ws_client.subscribe(
                self.push_event,
                event_types=MONITOR_EVENT_TYPES,
            )
            self._ws_subscription = sub
            self.set_ws_connected(True)
            logger.info("HanakoMonitor: 已订阅共享 WS 客户端 (event_types=%d)",
                        len(MONITOR_EVENT_TYPES))
        except Exception as e:
            logger.warning("订阅 WS 事件失败: %s — 退回文件轮询", e)
            self.set_ws_connected(False)
            return

        # 订阅状态变化，用于断线重连 / 补拉时反馈到 UI
        try:
            ws_client.subscribe_state(self._on_ws_state)
        except Exception as e:
            logger.warning("订阅 WS 状态变化失败: %s", e)

    def _on_ws_state(self, state, err=None) -> None:
        """WS 客户端状态回调（HanakoWSClient.ConnectionState）"""
        from_state = str(state).lower()
        ready_states = {"ready", "connected"}
        bad_states = {"stopped", "backoff", "closing", "disconnected"}
        if from_state in ready_states:
            self.set_ws_connected(True)
        elif from_state in bad_states:
            self.set_ws_connected(False)
            if err:
                logger.warning("WS 状态异常: %s, err=%s — 启用文件轮询 fallback", state, err)

    def set_agent_context(self, agent_id: str, session_manager=None) -> None:
        """绑定桌宠对应的助手 agent，只观测该助手的会话事件。

        一个桌宠对应一个助手：传入 agent_id 后，WS 事件将按 agent 过滤，
        不再转播其他 agent 的活动（避免“没操作却一直在动”）。

        Args:
            agent_id: 桌宠对应的助手 id（如 "yuexinmiao"）
            session_manager: 可选，用于把事件的 sessionId 映射到 agent_id。
        """
        self._agent_id = agent_id
        self._session_manager = session_manager

    def _agent_of_event(self, event: dict):
        """判断事件属于哪个助手；判断不出返回 ``None``。

        优先级：

        1. ``sessionPath`` 里的 ``agents/<agent_id>/`` —— 磁盘上的真实归属，最可靠；
        2. ``session_manager`` 的 ``sessionId`` → ``SessionRef.agent_id``。

        路径形态实测有三种：``sessions`` / ``subagent-sessions`` /
        ``workflow-sessions``。后两种下属于该助手的会话一样是它在干活，但旧实现
        因为匹配不上 ``/sessions/`` 而把它归为"判断不出"，走保守放行让**任意**
        助手的子进程活动都驱动本桌宠（用户报告的正是这个）。
        """
        path = str(event.get("sessionPath") or "").replace("\\", "/")
        m = AGENT_DIR_RE.search(path)
        if m:
            return m.group(1).strip() or None
        sm = self._session_manager
        if sm is not None:
            try:
                session = sm._session_for_event(event) if hasattr(sm, "_session_for_event") else None
                if session is not None:
                    return (getattr(session, "agent_id", None) or "").strip() or None
            except Exception:
                return None
        return None

    def _event_belongs_to_agent(self, event: dict) -> bool:
        """判断事件是否属于本桌宠对应的助手。

        归属判定见 :meth:`_agent_of_event`。规则：

        - 未绑定 agent_id → 不过滤（向后兼容）
        - 归属 = 绑定的助手 → 放行（**含**它自己的 ``subagent-sessions``：
          子任务也是这个助手在干活）
        - 归属 = 别的助手 → 挡掉（**含**别的助手的子代理会话）
        - 真的判断不出归属 → 保守放行（避免误滤掉正常事件）
        """
        if not self._agent_id:
            return True
        agent = self._agent_of_event(event)
        if agent is None:
            return True  # 无法判断，保守放行
        return agent.casefold() == str(self._agent_id).strip().casefold()

    def push_event(self, event: dict):
        """直接推送事件（WebSocket 模式回调）。
        当 BridgeClient 通过 WebSocket 收到事件时调用此方法。
        
        P0 修复：thinking/tool 事件节流，避免高频覆盖情绪。
        只有转台状态变化（thinking→idle, working→idle）才立即推送，
        同状态高频事件节流 1s。
        
        E-watchdog: turn_end 后立即回 idle，不等 STALE_TIMEOUT。
        """
        # 会话过滤：只观测本桌宠对应助手的活动
        if not self._event_belongs_to_agent(event):
            return
        # E-watchdog 心跳：本桌宠每次事件都刷新最后活动时间戳，
        # 供 tick() 的 WATCHDOG_TIMEOUT 兜底检测。
        self._last_update = time.time()
        event_type = event.get("type", "")
        # BugFix #5-E：缓存最近一次成功 tool_end 事件（celebrating 带摘要汇报用）
        if event_type == "tool_end" and event.get("success", True) is not False:
            self._last_tool_end_event = dict(event)
        # 2026-09-21：本回合有没有跑过工具——庆祝只在"真的干了活"的回合发生。
        # 必须写在**任何早返回之前**：tool_start 会被 1s 节流吞掉。
        if event_type in ("tool_start", "tool_progress"):
            self._turn_had_tool = True
        
        # P0 修复：mood_start/mood_text/mood_end —— 累积 <mood> 内省块文本，
        # 解析真实情绪词（开心/好奇/生气…）映射到 emotion key 驱动 Live2D 表情。
        # 之前 mood_text 只命中 EVENT_TO_MOOD 的 talking/neutral，delta 被丢弃。
        if event_type == "mood_start":
            self._mood_acc = ""
            return
        if event_type == "mood_text":
            self._mood_acc += str(event.get("delta") or event.get("data") or "")
            self._apply_mood_emotion()
            return
        if event_type == "mood_end":
            self._apply_mood_emotion()
            self._mood_acc = ""
            return
        
        result = map_event_to_mood(event)
        if result:
            mood, message, emotion = result
            event_type = event.get("type", "")
            
            # E-watchdog: turn_end 是明确终止信号
            #
            # 2026-09-21：**庆祝就在这里发生**（原先挂在 tool_end success 上，
            # 一次对话几十个工具调用 = 几十次「完成啦！」语音播报）。
            #
            # ⚠️ 本分支原先硬编码推 "idle" 并 return —— map_event_to_mood 对
            # turn_end 的返回值在这条路径上是**死代码**。两处必须一起改，
            # 否则"改了却不见效"。
            if event_type == "turn_end":
                self._mood_last_push.clear()  # P2: 清除所有 mood 节流
                self._current_emotion = "neutral"
                self._current_state_name = "idle"
                self._current_anim = "idle"
                self._mood_acc = ""  # P0：清掉未收尾的 mood 内省块累积
                had_tool = self._turn_had_tool
                self._turn_had_tool = False  # 下一回合重新计
                if self._on_state_change:
                    now = time.time()
                    want = (mood == "celebrating"
                            and (had_tool or not CELEBRATE_REQUIRES_TOOL)
                            and now - self._celebrate_last_push >= CELEBRATE_MIN_INTERVAL)
                    if want:
                        self._celebrate_last_push = now
                        self._on_state_change("happy", "", emotion="happy",
                                              state="celebrating")
                    else:
                        self._on_state_change("idle", "", emotion="neutral", state="idle")
                return
            
            # P0 节流：thinking/tool 事件在 turn 期间高频到来，
            # 同一 mood 事件 1s 内只推送一次，避免持续重置情绪过期计时器。
            # turn_end/tool_end(success) 等终态事件不受节流限制。
            # P2 修复：改用 per-mood 节流字典，避免 thinking→working 状态转换被吞。
            # P2-8 修复：tool_end 不再无限制放行——WS 重放/镜像/多工具连续成功会
            # 造成每 5-10s 一次庆祝刷屏，统一按 10s 窗口节流；turn_end 仍直通。
            now = time.time()
            if event_type == "tool_end":
                if now - self._tool_end_last_push < 10.0:
                    return  # 节流：10s 内同一 tool_end 只推一次
                self._tool_end_last_push = now
            elif event_type != "turn_end":
                last_push = self._mood_last_push.get(mood, 0.0)
                if now - last_push < 1.0:
                    return  # 节流：1s 内同一 mood 事件只推一次
                self._mood_last_push[mood] = now
            
            # P3: EXPRESSION_MAP 已改为 tuple 格式，提取序列名
            mapped = EXPRESSION_MAP.get(emotion, ("idle", None, None))
            anim = mapped[0] if isinstance(mapped, tuple) else mapped
            self._set_if_changed(anim, message, emotion=emotion, state=mood)
    
    def _apply_mood_emotion(self) -> None:
        """把累积的 <mood> 内省块文本映射成 emotion 并推送到渲染层。

        只推送识别出明确情绪词的状态（如 开心→happy、好奇→surprised），
        neutral 不动表情，避免覆盖 thinking/working 等既有状态；
        不受 push_event 的 1s 节流限制（mood 情绪变化是明确的转台信号）。
        """
        emotion = mood_text_to_emotion(self._mood_acc)
        if emotion == "neutral" or emotion == self._current_emotion:
            return
        mapped = EXPRESSION_MAP.get(emotion, ("idle", None, None))
        anim = mapped[0] if isinstance(mapped, tuple) else mapped
        self._set_if_changed(anim, "", emotion=emotion, state="speaking")
        self._mood_last_push.clear()  # P2: 重置所有 mood 节流
