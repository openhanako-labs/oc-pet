"""叙事生成引擎 (M1) — 空闲时段生成"桌面小事件"

职责:
  1. 在空闲时段生成微事件 (micro_event / observation / care / joke ...)
  2. 将叙事结果注入对话历史 (通过 callback 与 ConversationEngine 对接)
  3. 维护叙事上下文 (最近 N 个事件，用于 prompt 去重)
  4. 情境缓存 + 冷却控制 + 本地模板兜底

用法:
    engine = NarrativeEngine(character_id="yuexinmiao", perception=perception_ctrl, adapter=adapter)
    event = engine.request_event()       # 同步，阻塞等待 LLM 或模板
    if event:
        engine.apply(event, on_reply)    # 触发 UI 展示

与 ConversationEngine 的对接:
    - NarrativeEngine 不直接调用 TTS，只生成 {content, emotion, animation}
    - 由 ConversationEngine 或 PetWindow 拿到 event 后，走已有的 TTS 流程
    - apply() 的 callback 签名与 ConversationEngine.on_reply 一致:
      callback(reply: str, emotion: str, anim: str, audio_path: str = "")
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════
#  数据结构
# ════════════════════════════════════════════════════════════

@dataclass
class NarrativeEvent:
    """叙事事件数据结构"""
    event_type: str           # micro_event / greeting / observation / joke / question / care
    content: str              # 叙事文本 (不超过 2 句话)
    emotion: str              # happy / sad / thinking / surprised / neutral
    animation: str | None = None   # 可选动画: draw / sit / wave / look / sleep / extra
    priority: float = 0.5     # 优先级 0-1，高优先级可打断低优先级
    source: str = "narrative" # 来源: narrative / template / cached


# ════════════════════════════════════════════════════════════
#  本地模板兜底
# ════════════════════════════════════════════════════════════

# 情绪 → 动画映射 (与 ConversationEngine 保持一致)
_EMOTION_TO_ANIM = {
    "happy": "extra",
    "sad": "idle",
    "thinking": "extra",
    "surprised": "extra",
    "neutral": "idle",
    "cute": "extra",
    "angry": "extra",
}

# 场景类型 → 模板池
_TEMPLATE_POOLS: dict[str, list[dict]] = {
    "observation": [
        {"content": "你在看视频笑什么，分享一下嘛。", "emotion": "happy"},
        {"content": "Steam 打开了……今天又要肝游戏？", "emotion": "thinking"},
        {"content": "窗外好像下雨了，适合窝在椅子上发呆。", "emotion": "neutral"},
        {"content": "你写了三个小时代码了，眼睛还好吗？", "emotion": "care"},
        {"content": "终端里报错红了一片，看起来不太好。", "emotion": "surprised"},
        {"content": "你在听音乐？什么歌这么入迷。", "emotion": "happy"},
        {"content": "浏览器开了这么多标签页，你不怕卡死吗。", "emotion": "surprised"},
        {"content": "你又在刷手机了，工作做完了吗。", "emotion": "thinking"},
    ],
    "joke": [
        {"content": "你知道程序员最怕什么吗？……空指针。喵。", "emotion": "happy"},
        {"content": "为什么程序员总是分不清万圣节和圣诞节？因为 Oct 31 == Dec 25。", "emotion": "happy"},
        {"content": "我的代码从不写注释——因为代码本身就是注释。……骗你的，我也不会写代码。", "emotion": "thinking"},
        {"content": "听说你最近在学 Python？那你知道为什么 Python 很受欢迎吗？因为它没有 Java 那么多麻烦。", "emotion": "happy"},
        {"content": "我在想，如果 AI 能代替人类工作，那谁来替 AI 写代码呢？", "emotion": "thinking"},
    ],
    "care": [
        {"content": "喝水了吗？你已经两个小时没离开了。", "emotion": "care"},
        {"content": "该站起来活动一下啦～一直坐着腰会坏的。", "emotion": "care"},
        {"content": "快到饭点了，要不要点外卖？我可以帮你看看附近有什么好吃的。", "emotion": "happy"},
        {"content": "别熬太晚哦，明天还要早起呢。", "emotion": "care"},
        {"content": "看你一直在忙，记得给自己放个五分钟的小假吧。", "emotion": "thinking"},
    ],
    "greeting": [
        {"content": "你回来了！等你好久了～", "emotion": "happy"},
        {"content": "早上好！今天也要加油哦。", "emotion": "happy"},
        {"content": "终于有人理我了……开玩笑的，我一直都在。", "emotion": "cute"},
        {"content": "你刚才去哪了？我以为你把我忘了。", "emotion": "sad"},
    ],
    "question": [
        {"content": "你今天过得怎么样？", "emotion": "thinking"},
        {"content": "有没有什么想和我分享的？", "emotion": "happy"},
        {"content": "你在忙什么呀，看起来好专注。", "emotion": "curious"},
        {"content": "中午吃什么？这真是世纪难题了。", "emotion": "thinking"},
    ],
    "idle": [
        {"content": "(……安静地待着也很好。)", "emotion": "neutral"},
        {"content": "什么都不做的时候，反而最舒服呢。", "emotion": "neutral"},
        {"content": "我就在这里，你想说话的时候随时叫我。", "emotion": "neutral"},
    ],
}


def _pick_template() -> NarrativeEvent:
    """从本地模板池中随机选取一个叙事事件"""
    scene_keys = list(_TEMPLATE_POOLS.keys())
    scene = random.choice(scene_keys)
    pool = _TEMPLATE_POOLS[scene]
    tpl = random.choice(pool)
    return NarrativeEvent(
        event_type=scene,
        content=tpl["content"],
        emotion=tpl.get("emotion", "neutral"),
        animation=_EMOTION_TO_ANIM.get(tpl.get("emotion", "neutral"), "idle"),
        priority=0.3,
        source="template",
    )


# ════════════════════════════════════════════════════════════
#  叙事 Prompt 模板
# ════════════════════════════════════════════════════════════

NARRATIVE_PROMPT_TEMPLATE = """你是 {name}，一只桌面宠物。现在没有人在和你说话，但你想主动分享一些东西。

【当前情境】
{context}

【最近发生的事】
{recent_events}

请生成一个简短的桌面小事件。可以是：
- 看到用户在做什么的有趣观察
- 一句随口的话
- 一个小动作的描述
- 一个轻松的笑话或吐槽

输出格式（严格按照以下格式，每行一个字段）：
type: micro_event
content: 你的叙事文本 (不超过 2 句话)
emotion: happy/sad/thinking/surprised/neutral/cute
animation: draw/sit/wave/look/sleep/extra (可选，没有就省略这行)

规则：
1. 保持角色一致性
2. 不要重复最近发生过的事件
3. 语气自然，像朋友随口说的话
4. 如果实在没什么好说的，生成一个安静的观察
"""


def _parse_llm_narrative_response(text: str) -> NarrativeEvent:
    """解析 LLM 返回的叙事响应文本 → NarrativeEvent"""
    event_type = "micro_event"
    content = ""
    emotion = "neutral"
    animation = None

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("type:"):
            event_type = line[len("type:"):].strip() or "micro_event"
        elif line.startswith("content:"):
            content = line[len("content:"):].strip()
        elif line.startswith("emotion:"):
            emotion = line[len("emotion:"):].strip() or "neutral"
        elif line.startswith("animation:"):
            animation = line[len("animation:"):].strip() or None

    if not content:
        content = text[:100]  # 兜底：直接用全文

    return NarrativeEvent(
        event_type=event_type,
        content=content,
        emotion=emotion,
        animation=animation,
        priority=0.5,
        source="narrative",
    )


# ════════════════════════════════════════════════════════════
#  缓存管理
# ════════════════════════════════════════════════════════════

CACHE_TTL_SECONDS = 3600  # 缓存 1 小时


def _compute_context_hash(context: str, recent_events: list[dict]) -> str:
    """计算情境 hash，用于缓存命中
    
    基于感知上下文 + 最近事件内容组合，确保相似情境不会重复生成。
    """
    combined = f"{context}|{'|'.join(e.get('content', '')[:30] for e in recent_events[-3:])}"
    return hashlib.md5(combined.encode()).hexdigest()[:12]


_global_cache_dir: Path | None = None


def _ensure_global_cache_dir() -> Path:
    """确保全局缓存目录存在（懒初始化）"""
    global _global_cache_dir
    if _global_cache_dir is None:
        _global_cache_dir = Path.home() / ".hanako" / "narrative_cache"
        _global_cache_dir.mkdir(parents=True, exist_ok=True)
    return _global_cache_dir


def _load_cached_event(cache_key: str) -> NarrativeEvent | None:
    """从磁盘缓存读取叙事事件"""
    cache_file = _ensure_global_cache_dir() / f"{cache_key}.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text("utf-8"))
        age = time.time() - data.get("timestamp", 0)
        if age > CACHE_TTL_SECONDS:
            cache_file.unlink(missing_ok=True)
            return None
        return NarrativeEvent(**data["event"])
    except Exception as e:
        logger.debug("Cache read failed: %s", e)
        return None


def _save_cached_event(cache_key: str, event: NarrativeEvent):
    """保存叙事事件到磁盘缓存"""
    try:
        cache_file = _ensure_global_cache_dir() / f"{cache_key}.json"
        data = {
            "timestamp": time.time(),
            "event": {
                "event_type": event.event_type,
                "content": event.content,
                "emotion": event.emotion,
                "animation": event.animation,
                "priority": event.priority,
                "source": event.source,
            }
        }
        cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    except Exception as e:
        logger.debug("Cache write failed: %s", e)


# ════════════════════════════════════════════════════════════
#  叙事生成引擎主类
# ════════════════════════════════════════════════════════════

class NarrativeEngine:
    """叙事生成引擎
    
    职责:
      1. 在空闲时段生成微事件 (micro_event)
      2. 将叙事结果通过 callback 注入 UI
      3. 维护叙事上下文 (最近 N 个事件)
      4. 情境缓存 + 冷却控制 + 本地模板兜底
    
    线程安全: 所有公共方法均可在任意线程调用
    """

    def __init__(
        self,
        character_id: str = "yuexinmiao",
        perception: "PerceptionController" = None,
        adapter: "HanakoPetAdapter" = None,
        cooldown_minutes: float = 5.0,  # 测试用，正式版改回 15.0
        max_recent_events: int = 20,
        prefer_local_template: bool = False,
        enabled_scenarios: list[str] | None = None,
    ):
        self._character_id = character_id
        self._perception = perception
        self._adapter = adapter
        self._cooldown_seconds = cooldown_minutes * 60
        self._max_recent_events = max_recent_events
        self._prefer_local_template = prefer_local_template
        self._enabled_scenarios = enabled_scenarios or [
            "micro_event", "observation", "joke", "care", "question", "greeting"
        ]

        # 状态
        self._recent_events: list[dict] = []   # 最近 N 个事件 (用于上下文)
        self._last_event_time: float = 0.0
        self._lock = threading.Lock()
        self._running = False
        self._background_thread: threading.Thread | None = None

        # 回调 (由 PetWindow 设置)
        self.on_event: callable = lambda event: None  # NarrativeEvent → None

    @property
    def character_id(self) -> str:
        return self._character_id

    @property
    def recent_events(self) -> list[dict]:
        with self._lock:
            return list(self._recent_events)

    @property
    def cooldown_seconds(self) -> float:
        return self._cooldown_seconds

    @cooldown_seconds.setter
    def cooldown_seconds(self, value: float):
        self._cooldown_seconds = value

    # ── 公共接口 ──

    def request_event(self) -> NarrativeEvent | None:
        """请求一个叙事事件 (同步，可在任意线程调用)
        
        流程:
          1. 检查冷却时间
          2. 构建感知上下文
          3. 尝试缓存命中
          4. 优先 LLM 生成，失败/禁用时回退到本地模板
          5. 记录事件，更新冷却
        
        Returns:
            NarrativeEvent 或 None (冷却中/生成失败)
        """
        # 1. 冷却检查
        now = time.time()
        with self._lock:
            if now - self._last_event_time < self._cooldown_seconds:
                remaining = int(self._cooldown_seconds - (now - self._last_event_time))
                logger.debug("Narrative cooldown active, %ds remaining", remaining)
                return None
            self._last_event_time = now

        # 2. 构建感知上下文
        context = ""
        if self._perception:
            context = self._perception.build_context()

        # 3. 缓存检查
        with self._lock:
            recent_snippet = [e.get("content", "") for e in self._recent_events[-3:]]
        ctx_hash = _compute_context_hash(context, recent_snippet)
        cached = _load_cached_event(ctx_hash)
        if cached:
            logger.info("Narrative cache hit: %s", cached.content[:40])
            return cached

        # 4. 生成叙事
        event = None
        if not self._prefer_local_template and self._adapter:
            event = self._generate_via_llm(context)

        if not event:
            event = _pick_template()
            logger.info("Using local template: %s", event.content[:40])

        # 5. 记录事件
        event_record = {
            "content": event.content,
            "type": event.event_type,
            "time": time.time(),
        }
        with self._lock:
            self._recent_events.append(event_record)
            if len(self._recent_events) > self._max_recent_events:
                self._recent_events.pop(0)

        # 6. 缓存
        _save_cached_event(ctx_hash, event)

        # 7. 通知回调
        try:
            self.on_event(event)
        except Exception as e:
            logger.warning("on_event callback failed: %s", e)

        logger.info("Narrative event generated [%s]: %s | emotion=%s | anim=%s",
                     event.event_type, event.content[:40], event.emotion, event.animation)
        return event

    def apply(self, event: NarrativeEvent, callback: callable):
        """应用叙事事件到 UI
        
        将 NarrativeEvent 转换为 ConversationEngine.on_reply 的回调格式:
          callback(reply: str, emotion: str, anim: str, audio_path: str = "")
        
        Args:
            event: 叙事事件
            callback: 回调函数，签名 (reply, emotion, anim, audio_path)
        """
        anim = event.animation or "idle"
        # 如果没有指定动画，根据情绪自动映射
        if not event.animation:
            anim = _EMOTION_TO_ANIM.get(event.emotion, "idle")
        
        try:
            callback(event.content, event.emotion, anim, "")
        except Exception as e:
            logger.error("Narrative apply callback failed: %s", e)

    def start_background_loop(self, interval_seconds: float = 900.0):
        """启动后台叙事循环 (每 N 秒尝试生成一次)
        
        Args:
            interval_seconds: 尝试间隔 (默认 15 分钟)，实际生成受冷却控制
        """
        if self._running:
            logger.warning("NarrativeEngine already running")
            return
        self._running = True
        self._background_thread = threading.Thread(
            target=self._background_loop,
            args=(interval_seconds,),
            daemon=True,
            name=f"narrative-{self._character_id}",
        )
        self._background_thread.start()
        logger.info("Narrative background loop started | interval=%ds", interval_seconds)

    def stop_background_loop(self):
        """停止后台叙事循环"""
        self._running = False
        if self._background_thread:
            self._background_thread.join(timeout=5)
            self._background_thread = None
        logger.info("Narrative background loop stopped")

    def clear_recent_events(self):
        """清除最近事件记录"""
        with self._lock:
            self._recent_events.clear()
        logger.info("Recent narrative events cleared")

    def update_cooldown(self, minutes: float):
        """动态更新冷却时间"""
        self._cooldown_seconds = minutes * 60
        logger.info("Narrative cooldown updated: %.0f min", minutes)

    # ── 内部方法 ──

    def _generate_via_llm(self, context: str) -> NarrativeEvent | None:
        """通过 LLM 生成叙事事件
        
        Returns:
            NarrativeEvent 或 None (LLM 不可用时)
        """
        try:
            # 构建最近事件列表
            with self._lock:
                recent = self._recent_events[-5:]
            
            recent_text = "\n".join(f"- {e['content']}" for e in recent) or "(刚安静了一会儿)"
            
            prompt = NARRATIVE_PROMPT_TEMPLATE.format(
                name=self._character_id,
                context=context or "暂无额外情境信息",
                recent_events=recent_text,
            )

            # 通过 HanakoPetAdapter.chat() 调用 LLM
            # 注意：chat() 期望用户消息，我们把它当作一个"提示"来发
            reply, emotion = self._adapter.chat(
                message=prompt,
                inject_memory=False,  # 叙事不需要记忆注入
                extra_context="",
            )

            if not reply:
                return None

            # 尝试解析结构化输出
            event = _parse_llm_narrative_response(reply)
            
            # 验证事件类型是否在允许列表中
            if event.event_type not in self._enabled_scenarios:
                event = NarrativeEvent(
                    event_type=random.choice(self._enabled_scenarios),
                    content=event.content,
                    emotion=event.emotion,
                    animation=event.animation,
                    source="narrative",
                )

            return event

        except Exception as e:
            logger.warning("LLM narrative generation failed: %s", e)
            return None

    def _background_loop(self, interval: float):
        """后台循环：定期尝试生成叙事事件"""
        while self._running:
            try:
                event = self.request_event()
                if event:
                    logger.info("Background narrative: %s", event.content[:50])
                else:
                    logger.debug("Background narrative: skipped (cooldown or no event)")
            except Exception as e:
                logger.error("Background narrative loop error: %s", e)
            
            # 等待下一个周期
            for _ in range(int(interval * 10)):
                if not self._running:
                    return
                time.sleep(0.1)

    def build_context_for_prompt(self) -> str:
        """构建叙事 prompt 所需的上下文 (公开方法，供外部调试用)"""
        if not self._perception:
            return ""
        return self._perception.build_context()
