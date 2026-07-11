"""对话引擎 - 合并 bridge + pet 的核心逻辑

在 pet 进程内后台运行，不依赖文件中转：
  用户消息 -> LLM -> TTS -> 回调（气泡 + 音频）

用法:
    engine = ConversationEngine(character_id="ophelia")
    engine.start()  # 启动后台线程 + 预加载 TTS
    engine.send("你好")  # 发送消息，异步处理
    # 结果通过 on_reply 回调返回
"""
from __future__ import annotations

import logging
import os
import threading
import time

from .harness_adapter import HanakoPetAdapter
from .perception import PerceptionController

logger = logging.getLogger(__name__)


def map_emotion_to_anim(emotion: str) -> str:
    """情绪 -> 动画序列"""
    return "extra" if emotion in ("happy", "angry", "surprised", "thinking") else "idle"


class ConversationEngine:
    """对话引擎 - LLM + TTS 一体化，后台线程处理

    生命周期：随 pet 启动而启动，随 pet 关闭而关闭。
    """

    def __init__(self, character_id: str = "ophelia", perception: PerceptionController = None, tts_provider=None):
        self._character_id = character_id
        self._adapter = None
        self._tts = tts_provider  # 外部注入，None 时用默认
        self._perception = perception or PerceptionController(character_id)  # 外部注入优先
        self._queue: list[dict] = []
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        self._tts_ready = False

        # 回调（由 pet 设置）
        self.on_reply: callable = lambda reply, emotion, anim, audio_path: None
        self.on_status: callable = lambda msg: None  # 状态提示
        self.on_tts_ready: callable = lambda: None  # TTS 加载完成

    @property
    def tts_ready(self) -> bool:
        return self._tts_ready

    def start(self):
        """启动引擎（后台线程）"""
        self._running = True

        # 初始化 LLM 适配器
        try:
            self._adapter = HanakoPetAdapter(agent_id=self._character_id)
            logger.info("LLM 适配器就绪 | model=%s", self._adapter.model_config.get("model", "?"))
        except Exception as e:
            logger.error("LLM 适配器初始化失败: %s", e)
            return

        # 初始化 TTS（如果未注入）
        if not self._tts:
            from tts_provider.cosyvoice import CosyVoiceProvider
            self._tts = CosyVoiceProvider()
        spk_info = self._tts.get_speaker_info(self._character_id) if hasattr(self._tts, 'get_speaker_info') else {}
        if spk_info:
            logger.info("TTS 配置就绪 | ref=%s", spk_info.get("ref_audio", "?")[-30:])
        else:
            logger.info("TTS provider: %s", getattr(self._tts, 'name', 'unknown'))

        # 启动后台线程
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """停止引擎"""
        self._running = False
        self._perception.stop_screen()
        with self._lock:
            self._queue.clear()

    def send(self, text: str, character: str = ""):
        """发送消息（异步，结果通过 on_reply 回调）"""
        with self._lock:
            self._queue.append({
                "text": text,
                "character": character or self._character_id,
                "time": time.time(),
            })

    def _run(self):
        """后台线程主循环"""
        # 预加载 TTS
        self.on_status("正在准备声音...")
        if self._tts:
            self._tts.preload()
            self._tts_ready = self._tts.is_ready
        self.on_status("")
        self.on_tts_ready()

        # 刷新日程 + 启动屏幕感知
        self._perception.tick()
        self._perception.start_screen(interval=120)

        logger.info("对话引擎启动完成")

        while self._running:
            # 取消息
            msg = None
            with self._lock:
                if self._queue:
                    msg = self._queue.pop(0)

            if msg:
                self._process_message(msg)
            else:
                time.sleep(0.2)

    def _process_message(self, msg: dict):
        """处理一条消息：LLM -> 回调文字 -> TTS 异步合成"""
        text = msg["text"]
        character = msg["character"]

        logger.info("处理消息 [%s]: %s", character, text[:50])

        # 1. LLM 回复
        try:
            perception_ctx = self._perception.build_context()
            reply, emotion = self._adapter.chat(
                message=text, inject_memory=True, extra_context=perception_ctx
            )
            if not reply:
                reply = "…"
            logger.info("LLM 回复: %s [emotion:%s]", reply[:60], emotion)
        except Exception as e:
            logger.error("LLM 失败: %s", e)
            reply = "…（信号不太好，你再说一遍？）"
            emotion = "neutral"

        # 2. 动画映射
        anim = map_emotion_to_anim(emotion)

        # 3. TTS 合成（同步，和文字一起回调）
        audio_path = ""
        skip_reason = ""
        if not self._tts:
            skip_reason = "no tts provider"
        elif not self._tts_ready:
            skip_reason = "tts not ready"
        elif not reply.strip():
            skip_reason = "empty reply"
        elif reply.strip() in ("\u2026", "..."):
            skip_reason = "ellipsis reply"
        
        if not skip_reason:
            try:
                instruct_map = {
                    "happy": "开心", "sad": "难过", "angry": "生气",
                    "cute": "可爱", "thinking": "思考",
                }
                instruct = instruct_map.get(emotion, "")
                audio_path = self._tts.synthesize(reply, character_id=character, instruct=instruct) or ""
                if audio_path:
                    logger.info("TTS done: %s", os.path.basename(audio_path))
                else:
                    logger.warning("TTS failed, no audio")
            except Exception as e:
                logger.warning("TTS error: %s", e)
        else:
            logger.info("TTS skipped: %s", skip_reason or "unknown")

        # 4. 回调（文字 + 音频一起）
        self.on_reply(reply, emotion, anim, audio_path)

    def switch_character(self, character_id: str):
        """切换角色 - 清空队列和历史"""
        with self._lock:
            self._queue.clear()
        self._character_id = character_id
        try:
            self._adapter = HanakoPetAdapter(agent_id=character_id)
            if hasattr(self._adapter, '_history'):
                self._adapter._history.clear()
            logger.info("角色切换: %s", character_id)
        except Exception as e:
            logger.error("角色切换失败: %s", e)
