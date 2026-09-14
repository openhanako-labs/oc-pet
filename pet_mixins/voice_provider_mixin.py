"""VoiceProviderMixin — TTS / ASR provider 管理（创建 / 按需重建 / 签名判断）。

由 PetWindow 多重继承。访问 self.config / self._engine / self._tts_provider_sig /
self._tts_reload_gen 等，均由 PetWindow 提供（鸭子类型，无需 import pet）。

关键点：TTS provider 的构造与预热必须在后台线程完成（cosyvoice 分支的
import 链会阻塞 Qt 事件循环几十秒），因此重建用代际号保证只采纳最后一次结果。

拆分自 pet.py 的语音 provider 区块（原 1304-1437 行），降低 PetWindow 体积。
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)


class VoiceProviderMixin:
    """TTS / ASR provider 的创建与按需重建。"""

    def _create_tts_provider(self):
        """根据配置创建 TTS provider，失败返回 None"""
        provider = self.config.get("tts", {}).get("provider", "cosyvoice")
        try:
            if provider == "mimo":
                from tts_provider.mimo_tts import MimoTtsProvider
                from env_config import get_tts_api_config
                cfg = get_tts_api_config()
                if not cfg.get("base_url") or not cfg.get("api_key"):
                    logger.warning(
                        "TTS 引擎设为 MIMO 但 API 配置为空，回退到本地 CosyVoice"
                    )
                    from tts_provider.cosyvoice import CosyVoiceProvider
                    return CosyVoiceProvider()
                mimo = MimoTtsProvider()
                mimo.configure(
                    base_url=cfg["base_url"],
                    api_key=cfg["api_key"],
                    model=cfg.get("model", ""),
                    voice=cfg.get("voice", "default_zh"),
                )
                return mimo
            elif provider == "api":
                from tts_provider.api_tts import ApiTtsProvider
                return ApiTtsProvider()
            elif provider == "edge":
                from tts_provider.edge_tts import EdgeTtsProvider
                tts_cfg = self.config.get("tts", {}) or {}
                edge = EdgeTtsProvider()
                edge.configure(
                    voice=tts_cfg.get("edge_voice", ""),
                    rate=tts_cfg.get("edge_rate", ""),
                    pitch=tts_cfg.get("edge_pitch", ""),
                )
                return edge
            elif provider == "aqua":
                from tts_provider.aqua_tts import AquaTtsProvider
                return AquaTtsProvider()
            elif provider == "qwen":
                from tts_provider.qwen_tts import QwenTtsProvider, load_voice_refs
                tts_cfg = self.config.get("tts", {}) or {}
                voice_refs = load_voice_refs(self.config)
                qwen = QwenTtsProvider(
                    voice_refs=voice_refs,
                    default_voice=tts_cfg.get("qwen_default_voice", ""),
                )
                return qwen
            else:
                from tts_provider.cosyvoice import CosyVoiceProvider
                return CosyVoiceProvider()
        except Exception as e:
            logger.warning("TTS provider 创建失败 (%s): %s", provider, e)
            return None

    def _tts_provider_signature(self) -> tuple:
        """TTS provider 的身份签名——只有它变了才需要重建实例。

        只纳入决定「创建出哪个 provider 实例」的字段。volume / enabled
        这类运行期开关不算，它们由 _tts_player 直接生效，无需重建。
        """
        tts_cfg = self.config.get("tts", {}) or {}
        provider = tts_cfg.get("provider", "cosyvoice")
        api_sig: tuple = ()
        if provider in ("mimo", "api"):
            try:
                from env_config import get_tts_api_config
                c = get_tts_api_config() or {}
                api_sig = (
                    c.get("base_url", ""), c.get("api_key", ""),
                    c.get("model", ""), c.get("voice", ""),
                )
            except Exception:
                api_sig = ()
        elif provider == "edge":
            # edge 引擎参数改变时也要重建
            tts_cfg = self.config.get("tts", {}) or {}
            api_sig = (
                tts_cfg.get("edge_voice", ""),
                tts_cfg.get("edge_rate", ""),
                tts_cfg.get("edge_pitch", ""),
            )
        elif provider == "qwen":
            # qwen 引擎：base_url / voice_refs / mode / default_voice 任何一项变了都要重建
            try:
                from tts_provider.qwen_tts import load_voice_refs
                import os as _os
                refs = load_voice_refs(self.config)
                tts_cfg_q = self.config.get("tts", {}) or {}
                api_sig = (
                    _os.environ.get("QWEN_TTS_MODE", "serve").strip().lower(),
                    _os.environ.get("QWEN_TTS_BASE_URL", "") or _os.environ.get("TTS_BASE_URL", ""),
                    _os.environ.get("QWEN_TTS_MODEL", ""),
                    _os.environ.get("QWEN_TTS_DEFAULT_VOICE", ""),
                    tuple(sorted((k, str(v)) for k, v in refs.items())),
                )
            except Exception:
                api_sig = ()
        return (provider, api_sig)

    # ── P2-7 语音身份/音色 ──────────────────────────────────────

    def _resolve_tts_voice(self, agent_id: str, emotion: str = "") -> str:
        """按当前配置解析「该桌宠 + 该情绪」的生效音色；空 = 用 provider 默认。

        调用方：conversation_engine 每句 TTS 合成前（voice_resolver 回调）。
        解析链：角色音色 tts.voices[agent_id] → 情绪音色 tts.voice_emotion_map[emotion]
        → ""（provider 默认）。失败永不抛出：任何异常都回退到默认音色。
        """
        try:
            from tts_provider.voice_profile import resolve_voice, provider_valid_voices
            tts_cfg = self._effective_tts()
            provider = tts_cfg.get("provider", "cosyvoice")
            valid = provider_valid_voices(provider)
            return resolve_voice(tts_cfg, provider, agent_id, emotion, valid_voices=valid)
        except Exception as e:
            logger.debug("voice 解析失败，使用 provider 默认: %s", e)
            return ""

    def _wire_voice_resolver(self):
        """P2-7: 把桌宠自身的音色解析器挂到对话引擎。

        每句 TTS 合成前引擎都会调用它 (agent_id, emotion) → voice，
        从而让多桌宠各自使用独立音色、且 [emotion:xxx] 情绪标签可切换语气音色。
        """
        engine = getattr(self, "_engine", None)
        if engine is not None:
            try:
                engine._voice_resolver = self._resolve_tts_voice
            except Exception as e:
                # 失败 = 角色音色/情绪音色不生效，说话用默认声
                logger.warning("voice resolver 挂接失败，角色音色将不生效: %s", e)

    def _maybe_reload_tts_provider(self):
        """按需重建 TTS provider —— 构造与预热全部在后台线程完成。

        绝不能在 Qt 主线程调 _create_tts_provider()：cosyvoice 分支的
        import 链（funasr → torch/lightning/diffusers → onnxruntime → wetext
        → ModelScope 下载）会把事件循环冻住几十秒。
        """
        if not self._engine:
            return

        sig = self._tts_provider_signature()
        if sig == getattr(self, "_tts_provider_sig", None):
            return  # 配置没变：跳过，避免重复拉起重型依赖
        self._tts_provider_sig = sig

        # 代际号：连续保存时只有最后一次的结果会被采纳
        self._tts_reload_gen = getattr(self, "_tts_reload_gen", 0) + 1
        gen = self._tts_reload_gen
        old = getattr(self._engine, "_tts", None)

        def _discard(p):
            if p is None:
                return
            try:
                p.cleanup()
            except Exception:
                logger.debug("voice_provider_mixin: 非致命异常(已静默吞掉)", exc_info=True)

        def _rebuild():
            provider = None
            try:
                provider = self._create_tts_provider()   # 重型构造，后台执行
                if gen != self._tts_reload_gen:
                    _discard(provider)                   # 期间又保存过，本次作废
                    return
                if provider is not None:
                    provider.preload()
                if gen != self._tts_reload_gen:
                    _discard(provider)
                    return
                # 加锁保护赋值原子性（引擎 worker 线程同时读 _tts/_tts_ready）
                with self._engine._lock:
                    self._engine._tts = provider
                    self._engine._tts_ready = bool(provider is not None and provider.is_ready)
                logger.info(
                    "TTS provider 已切换: %s (ready=%s)",
                    getattr(provider, "name", None), self._engine._tts_ready,
                )
            except Exception as e:
                logger.warning("TTS provider 重建失败: %s", e)
                _discard(provider)
            finally:
                # 旧实例只有确实被换下来时才释放。
                # 若 worker 正在用 old 合成（_tts_in_use>0），立即 cleanup 会破坏在途合成
                # （use-after-cleanup 竞态）。这里延迟到引用计数归零再清理，最多等 180s（单句合成上限）。
                if old is not None and old is not getattr(self._engine, "_tts", None):
                    _defer_cleanup(old)

        def _defer_cleanup(old_provider):
            """延迟清理旧 provider：等引擎的 _tts_in_use 归零（或超时）再 cleanup。"""
            def _wait_and_cleanup():
                engine = self._engine
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    with engine._lock:
                        if getattr(engine, "_tts_in_use", 0) <= 0:
                            break
                    time.sleep(0.5)
                _discard(old_provider)
            threading.Thread(target=_wait_and_cleanup, name="TTSOldCleanup", daemon=True).start()

        threading.Thread(target=_rebuild, name="TTSReload", daemon=True).start()

    def _create_asr_provider(self):
        """根据配置创建 ASR provider，失败返回 None"""
        provider = self.config.get("asr", {}).get("provider", "whisper_local")
        try:
            if provider == "mimo":
                from asr_provider.mimo_asr import MimoAsrProvider
                from env_config import get_asr_api_config
                mimo = MimoAsrProvider()
                cfg = get_asr_api_config()
                mimo.configure(
                    base_url=cfg.get("base_url", ""),
                    api_key=cfg.get("api_key", ""),
                    model=cfg.get("model", ""),
                )
                return mimo
            elif provider == "api":
                from asr_provider.api_asr import ApiAsrProvider
                return ApiAsrProvider()
            elif provider == "sensevoice":
                # R1（2026-09-14）：中文专用本地 ASR（FunASR/SenseVoice）。
                # 顺带输出情感标签。注意 import 链重（实测 13.5s），
                # 模型加载由调用方放后台线程（与 whisper_local 同约定）。
                from asr_provider.sensevoice import SenseVoiceProvider
                return SenseVoiceProvider()
            else:
                from asr_provider.whisper_local import WhisperLocalProvider
                return WhisperLocalProvider()
        except Exception as e:
            logger.warning("ASR provider 创建失败 (%s): %s", provider, e)
            return None
