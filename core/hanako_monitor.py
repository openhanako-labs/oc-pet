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

# ── 气泡精简算法（移植自 HanakoPro） ─────────────────────

BUBBLE_MAX_CHARS = 72
BUBBLE_MIN_SENTENCE_LEN = 8


def clean_bubble_text(text: str) -> str:
    """清理气泡文本：去代码块、markdown、HTML 标签、元信息。
    移植自 HanakoPro 的 cleanPetChatText()。
    """
    if not text:
        return ""
    # 去代码块
    text = re.sub(r'```[\s\S]*?```', ' ', text)
    # 去行内代码
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # 去 HTML 标签
    text = re.sub(r'<[^>]+>', ' ', text)
    # 去 markdown 格式（标题、加粗、列表、引用）
    text = re.sub(r'^[\s*#>-]+', '', text, flags=re.MULTILINE)
    # 去 MOOD/thinking/tool/status 等元信息
    text = re.sub(r'\b(?:MOOD|mood|thinking|tool|status)[:：]?[^\n。！？!?]*', ' ', text, flags=re.IGNORECASE)
    # 去引号和括号
    text = re.sub(r'[{}\\]"\'`]', ' ', text)
    # 压缩空白
    text = re.sub(r'\s+', ' ', text).strip()
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
    "mood_text",
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
        message = "待机中"
        emotion = "neutral"

    # tool_end 特殊处理（有 success 字段）
    if event_type == "tool_end":
        mood = "happy" if event.get("success", True) else "error"
        message = "完成啦" if mood == "happy" else "遇到问题"
        emotion = "happy" if mood == "happy" else "angry"

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

STALE_TIMEOUT = 30


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
}


class HanakoMonitor:
    def __init__(self, on_state_change=None):
        self._on_state_change = on_state_change
        self._current_anim = "idle"
        self._last_state = None
        self._last_update = 0
        self._last_todo_count = -1
        self._ws_connected = False
        # 情绪缓存（用于气泡颜色）
        self._current_emotion = "neutral"
        # 状态推断
        self._current_state_name = "idle"
        self._last_response_time = 0
        self._last_response_ts = 0  # response.json 的最后 ts（用于检测更新）
        self._last_audio_path = ""  # 最后播放的音频路径
        self._pending_notification_count = 0


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

        # 5. 超时空闲
        if self._last_update > 0 and (now - self._last_update) > STALE_TIMEOUT:
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

    def push_event(self, event: dict):
        """直接推送事件（WebSocket 模式回调）。
        当 BridgeClient 通过 WebSocket 收到事件时调用此方法。
        """
        result = map_event_to_mood(event)
        if result:
            mood, message, emotion = result
            # P3: EXPRESSION_MAP 已改为 tuple 格式，提取序列名
            mapped = EXPRESSION_MAP.get(emotion, ("idle", None, None))
            anim = mapped[0] if isinstance(mapped, tuple) else mapped
            self._set_if_changed(anim, message, emotion=emotion, state=mood)
