"""
Hanako 状态监控模块 — 增强版

轮询 TODO 文件 + 通知文件 + 回复文件 + 情绪映射
"""

import json
import re
import time
from pathlib import Path

from config import EXPRESSION_MAP, HANAKO_STATE_MAP

TODO_FILE = Path.home() / ".hanako/plugin-data/todo/todos.json"
NOTIFY_FILE = Path.home() / ".hanako/plugins/hanako-desktop-companion/notifications.json"
RESPONSE_FILE = Path.home() / ".hanako/plugins/hanako-desktop-companion/response.json"

# 情绪关键词 → 表情映射（从回复文本中检测）
EMOTION_KEYWORDS = {
    "happy": ["哈", "笑", "开心", "好耶", "太棒了", "嘻嘻", "嘿嘿", "www", "哈哈", "！"],
    "sad": ["呜", "难过", "伤心", "哭", "呜呜", "sad", "emo"],
    "angry": ["哼", "气", "怒", "可恶", "混蛋", "烦", "啊啊"],
    "surprised": ["诶", "欸", "！", "？", "什么", "不会吧", "哇"],
    "thinking": ["嗯", "让我想想", "思考", "…", "..."],
}

STALE_TIMEOUT = 30


class HanakoMonitor:
    def __init__(self, on_state_change=None):
        self._on_state_change = on_state_change
        self._current_anim = "idle"
        self._last_state = None
        self._last_update = 0
        self._last_todo_count = -1
        # 情绪缓存（用于气泡颜色）
        self._current_emotion = "neutral"

    def tick(self):
        now = time.time()

        # 1. TODO
        todos = self._read_todos()
        if len(todos) != self._last_todo_count:
            self._last_todo_count = len(todos)
            if todos:
                lines = [f"📋 {t['text'][:20]}" for t in todos[:2]]
                if len(todos) > 2:
                    lines.append(f"⋯ 还有 {len(todos)-2} 条")
                msg = "\n".join(lines)
            else:
                msg = ""
            if msg:
                self._set_if_changed("idle", msg, emotion="neutral")

        # 2. 通知
        for n in self._read_notifications():
            self._set_if_changed("extra", n.get("text", ""), emotion="neutral")

        # 3. 回复（桌宠消息的回显）—— 增强：检测情绪
        reply = self._read_response()
        if reply:
            emotion = self._detect_emotion(reply)
            anim = EXPRESSION_MAP.get(emotion, "idle")
            self._set_if_changed(anim, reply, emotion=emotion)

        # 4. 超时
        if self._last_update > 0 and (now - self._last_update) > STALE_TIMEOUT:
            self._set_if_changed("idle", "", emotion="neutral")

    def _detect_emotion(self, text: str) -> str:
        """从文本中检测情绪。返回 emotion key（happy/sad/angry/surprised/thinking/neutral）"""
        scores = {k: 0 for k in EMOTION_KEYWORDS}
        text_lower = text.lower()
        for emotion, keywords in EMOTION_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in text_lower:
                    scores[emotion] += 1
        max_emotion = max(scores, key=scores.get)
        if scores[max_emotion] > 0:
            return max_emotion
        return "neutral"

    def _read_todos(self):
        try:
            if not TODO_FILE.exists():
                return []
            data = json.loads(TODO_FILE.read_text("utf-8"))
            return [t for t in data.get("todos", []) if not t.get("done")]
        except:
            return []

    def _read_notifications(self):
        try:
            if not NOTIFY_FILE.exists():
                return []
            notes = json.loads(NOTIFY_FILE.read_text("utf-8"))
            NOTIFY_FILE.write_text("[]", "utf-8")
            return notes
        except:
            return []

    def _read_response(self):
        """读取回复文件，有回复时返回文字并清空"""
        try:
            if not RESPONSE_FILE.exists():
                return ""
            raw = RESPONSE_FILE.read_text("utf-8").strip()
            if not raw:
                return ""
            data = json.loads(raw)
            reply = data.get("reply", "")
            if reply:
                RESPONSE_FILE.write_text("{}", "utf-8")
            return reply
        except:
            return ""

    def _set_if_changed(self, anim, msg, emotion="neutral"):
        if anim != self._current_anim or msg:
            self._current_anim = anim
            self._current_emotion = emotion
            if self._on_state_change:
                self._on_state_change(anim, msg, emotion=emotion)

    def force_idle(self):
        self._last_state = None
        self._current_emotion = "neutral"
        self._set_if_changed("idle", "", emotion="neutral")

    @property
    def current_emotion(self):
        return self._current_emotion
