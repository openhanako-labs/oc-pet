"""微软 Edge TTS - 免费在线合成（edge-tts 库）

使用微软 Edge 浏览器的朗读接口（无需注册、无需 API key、零成本）。
输出 mp3，秒级合成；QMediaPlayer 原生支持 mp3 无需转码。

音色：微软自带 zh-CN 系列（晓晓 XiaoxiaoNeural 等），不做角色克隆映射。
可经 config `tts.edge_voice` 覆盖默认音色（设置面板「微软 Edge」引擎下可选）。

词级口型（2026-09-16）：合成时同时抓 WordBoundary，落盘为
``<mp3>.words.json`` 侧车文件。播放器按播放位置查词区间驱动口型，
比位置正弦包络精确一档（词与词之间真的闭嘴）。

依赖：edge-tts>=6.1.0（pip install edge-tts）
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Optional

from .base import TTSProvider
from .word_timings import save_words
from hanako_home import hanako_home

logger = logging.getLogger(__name__)

OUTPUT_DIR = hanako_home() / "pets" / "tts_cache"

# 缓存 TTL：超过 1 天的 mp3 可清理；每 10 分钟最多扫一次，避免每次合成都遍历目录
CACHE_TTL = 24 * 3600
_SWEEP_INTERVAL = 600

# 微软自带中文音色（edge-tts 服务端音色名）
EDGE_VOICES = [
    "zh-CN-XiaoxiaoNeural",   # 晓晓（女，温暖，默认）
    "zh-CN-XiaoyiNeural",     # 晓伊（女，活泼）
    "zh-CN-YunxiNeural",      # 云希（男，少年）
    "zh-CN-YunjianNeural",    # 云健（男，成熟）
    "zh-CN-YunyangNeural",    # 云扬（男，新闻播报）
    "zh-CN-XiaochenNeural",   # 晓辰（女，儿童）
    "zh-CN-XiaohanNeural",    # 晓涵（女，柔和）
    "zh-CN-XiaomengNeural",   # 晓梦（女，俏皮）
    "zh-CN-XiaomoNeural",     # 晓墨（女，叙事）
    "zh-CN-XiaoruiNeural",    # 晓睿（女，少儿）
    "zh-CN-XiaoshuangNeural", # 晓双（女，儿童）
    "zh-CN-XiaoxuanNeural",   # 晓萱（女，甜）
    "zh-CN-XiaoyanNeural",    # 晓颜（女，童声）
    "zh-CN-XiaoyouNeural",    # 晓悠（女，幼童）
    "zh-CN-XiaozhenNeural",   # 晓臻（女，温和）
    "zh-CN-YunfengNeural",    # 云枫（男，成熟）
    "zh-CN-YunhaoNeural",     # 云皓（男，磁性）
    "zh-CN-YunxiaNeural",     # 云夏（男，少年）
    "zh-CN-YunyeNeural",      # 云野（男，元气）
    "zh-CN-YunzeNeural",      # 云泽（男，浑厚）
    "zh-CN-liaoning-XiaobeiNeural",  # 晓北（东北话女）
    "zh-CN-shaanxi-XiaoniNeural",    # 晓妮（陕西话女）
]

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


class EdgeTtsProvider(TTSProvider):
    """微软 Edge TTS provider（免费在线，edge-tts 库）"""

    def __init__(self, voice: str = DEFAULT_VOICE, rate: str = "+0%", pitch: str = "+0Hz"):
        self._voice = voice or DEFAULT_VOICE
        self._rate = rate
        self._pitch = pitch
        self._ready = False
        self._last_error = ""
        self._last_sweep = 0.0          # 上次清理缓存目录的时间（节流）
    def configure(self, voice: str = "", rate: str = "", pitch: str = ""):
        """从配置覆盖默认参数"""
        if voice:
            self._voice = voice
        if rate:
            self._rate = rate
        if pitch:
            self._pitch = pitch

    @property
    def name(self) -> str:
        return "edge"

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def last_error(self) -> str:
        """最近一次合成/预检失败的原因（供引擎层在“无语音”时给出明确报错）。"""
        return self._last_error

    def preload(self):
        """轻量预检：库可用即就绪（在线服务，网络失败在 synthesize 期暴露）"""
        try:
            import edge_tts  # noqa: F401
            self._ready = True
            logger.info("Edge TTS ready | voice=%s | rate=%s | pitch=%s",
                        self._voice, self._rate, self._pitch)
        except ImportError:
            self._ready = False
            self._last_error = "edge-tts 未安装：pip install edge-tts"
            logger.warning("Edge TTS preload 失败: %s", self._last_error)

    def synthesize(self, text: str, character_id: str = "", instruct: str = "",
                   voice: str = "", emotion: str = "") -> Optional[str]:
        if not text or not text.strip():
            return None
        text = text.strip()[:500]
        try:
            import edge_tts
        except ImportError as e:
            self._last_error = f"edge-tts 未安装: {e}"
            logger.warning("Edge TTS: %s", self._last_error)
            return None

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        # 节流式 TTL 清理：>1 天的 mp3 删除，封顶磁盘增长（每次合成最多清一次，10 分钟最小间隔）
        now = time.monotonic()
        if now - self._last_sweep > _SWEEP_INTERVAL:
            self._last_sweep = now
            self._sweep_cache(CACHE_TTL)

        # P2-7: 显式 voice 参数优先（角色/情绪音色映射），未提供则用本 provider 默认
        eff_voice = voice or self._voice
        
        # P0: 情感 TTS 参数（emotion → pitch/volume）
        #
        # 2026-09-16 改：旧实现用 emotion 同时改 rate 与 pitch，
        # 而「改语速」会让听感从"她心情变了"变成"换了一个人说话"。
        # 现在语速恒定（+0%），情绪只走 pitch/volume，见 emotion_prosody。
        eff_rate = self._rate
        eff_pitch = self._pitch
        eff_volume = "+0%"
        if emotion:
            from .emotion_prosody import add_prosody, prosody_for
            emo_pitch, emo_vol = prosody_for(emotion)
            eff_pitch = add_prosody(self._pitch, emo_pitch)
            eff_volume = emo_vol
            logger.debug(
                "情感 TTS 参数: emotion=%s pitch=%s volume=%s (rate 恒定 %s)",
                emotion, eff_pitch, eff_volume, eff_rate,
            )

        # 缓存：同文本+音色+语速+音调+音量+情感复用
        cache_key = f"edge:{eff_voice}:{eff_rate}:{eff_pitch}:{eff_volume}:{text}"
        text_hash = hashlib.md5(cache_key.encode()).hexdigest()[:12]
        output_path = OUTPUT_DIR / f"edge_{text_hash}.mp3"

        if output_path.exists() and output_path.stat().st_size > 0:
            logger.info("Edge TTS cache hit: %s", output_path.name)
            return str(output_path)

        try:
            # 异步接口：放入新事件循环执行（worker 线程无 Qt 循环，asyncio.run 安全）
            #
            # 词级口型（2026-09-16）：用 stream() 而非 save()，
            # 一边写音频一边收 WordBoundary，落盘为 <mp3>.words.json。
            # 侧车写失败不影响出声（save_words 内部吞异常）。
            async def _synth() -> list[dict]:
                comm = edge_tts.Communicate(
                    text, eff_voice, rate=eff_rate, pitch=eff_pitch, volume=eff_volume,
                    boundary="WordBoundary",
                )
                collected: list[dict] = []
                with open(output_path, "wb") as f:
                    async for chunk in comm.stream():
                        ctype = chunk.get("type")
                        if ctype == "audio":
                            f.write(chunk["data"])
                        elif ctype == "WordBoundary":
                            # offset/duration 单位 100ns → 毫秒
                            try:
                                collected.append({
                                    "o": int(chunk.get("offset", 0)) // 10000,
                                    "d": int(chunk.get("duration", 0)) // 10000,
                                    "t": chunk.get("text") or "",
                                })
                            except (TypeError, ValueError):
                                pass
                return collected

            words = asyncio.run(_synth())
            if output_path.exists() and output_path.stat().st_size > 0:
                logger.info("Edge TTS done: %s (%d bytes)",
                            output_path.name, output_path.stat().st_size)
                # 口型时间轴：有词边界写词边界，没有则让通用层算能量分段
                try:
                    from .audio_timings import ensure_timings
                    ensure_timings(str(output_path), words)
                except Exception:
                    logger.debug("Edge TTS: 口型时间轴生成失败", exc_info=True)
                self._ready = True
                return str(output_path)
            logger.warning("Edge TTS: 合成结果为空文件")
            return None
        except Exception as e:
            self._last_error = str(e)
            logger.warning("Edge TTS 合成失败: %s", e)
            return None

    def _sweep_cache(self, max_age: float) -> None:
        """删除超过 max_age 秒的 edge 缓存 mp3（只在 synthesize 节流调用）。

        只清本 provider 的 edge_*.mp3，不碰 CosyVoice 的 wav 等其他引擎缓存。
        """
        try:
            cutoff = time.time() - max_age
            removed = 0
            for f in OUTPUT_DIR.glob("edge_*.mp3"):
                try:
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink()
                        removed += 1
                except FileNotFoundError:
                    logger.debug("edge_tts: 非致命异常(已静默吞掉)", exc_info=True)
            if removed:
                logger.info("Edge TTS 缓存清理: 删除 %d 个过期 mp3", removed)
        except Exception:
            logger.debug("edge_tts: 非致命异常(已静默吞掉)", exc_info=True)

    def get_speaker_info(self, character_id: str) -> dict:
        return {"voice": self._voice, "provider": "edge-tts", "free": True}
