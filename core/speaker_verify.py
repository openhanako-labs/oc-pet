"""说话人验证（声纹门卫）——判断一段录音是不是"主人"说的。

用途（2026-09-15 新增）：
    桌宠的持续监听/语音输入会被视频声、他人说话误触发。在 ASR 之前
    加一道声纹门卫：非主人声音直接丢弃，不进入 ASR，也不触发回复。

设计要点：
    - **CPU 推理**：3d-speaker campplus（ONNX），不吃显存——与本地 Qwen TTS
      共用 6GB 卡时这点很关键。
    - **失败放行**：模型/样本缺失、推理异常时一律 ``return True``（放行）。
      声纹是"锦上添花"的过滤器，不能因为它的故障让整个语音输入失效。
      这条与 MewCo 的 verify_speakers() 一致（except 分支返回 True）。
    - **懒加载**：首次 verify 才建 extractor，避免拖慢启动。
    - **主人样本**：``data/voiceprint/owner.wav``（16kHz 单声道）。
      采集走 :func:`register_owner`，或直接放一段干净的主人语音。

参考实现：MewCo-AI/mewco_ai_assistant_comm ``asr.py::verify_speakers``
（Apache/GPL 项目，仅借鉴"embedding + 余弦相似度 + 阈值"的思路，代码独立实现）。
"""
from __future__ import annotations

import logging
import os
import threading

import numpy as np

logger = logging.getLogger(__name__)

# 默认模型与样本路径（相对项目根；由 paths.py 兜底解析）
_DEFAULT_MODEL = "data/models/speaker/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
_DEFAULT_OWNER = "data/voiceprint/owner.wav"

# 相似度阈值：campplus 同人通常 >0.6，异人 <0.4；0.55 是较稳的分界。
# 可通过 config: voiceprint.threshold 覆盖。
_DEFAULT_THRESHOLD = 0.55


def _project_root() -> str:
    """项目根目录（core/ 的上一级）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve(path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.join(_project_root(), path)


def _read_wav_16k_mono(path: str) -> tuple[np.ndarray, int] | None:
    """读取 wav → (float32 [-1,1], sample_rate)。失败返回 None。

    只支持 PCM wav（soundfile 可选则优先用，否则标准库 wave）。
    """
    try:
        import soundfile as sf  # oc-pet 已依赖（whisper 链路）
        data, sr = sf.read(path, dtype="float32", always_2d=True)
        mono = data[:, 0]
        return mono, int(sr)
    except Exception:
        pass
    try:
        import wave
        with wave.open(path, "rb") as wf:
            nch, sw, sr, n = wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getnframes()
            raw = wf.readframes(n)
        if sw != 2:
            return None
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if nch > 1:
            arr = arr.reshape(-1, nch).mean(axis=1)
        return arr, int(sr)
    except Exception as e:
        logger.warning("speaker_verify: 读取 wav 失败 %s: %s", path, e)
        return None


class SpeakerVerifier:
    """声纹验证器（进程级可复用）。"""

    def __init__(self, model_path: str | None = None, owner_path: str | None = None,
                 threshold: float | None = None):
        self._model_path = _resolve(model_path or _DEFAULT_MODEL)
        self._owner_path = _resolve(owner_path or _DEFAULT_OWNER)
        self._threshold = float(threshold if threshold is not None else self._cfg_threshold())
        self._extractor = None
        self._owner_emb: np.ndarray | None = None
        self._lock = threading.Lock()
        self._failed = False  # 一旦初始化失败，后续直接放行（sticky）

    # ── 配置 ──────────────────────────────────────────────
    @staticmethod
    def _cfg_threshold() -> float:
        try:
            from config import load_config
            v = (load_config().get("voiceprint", {}) or {}).get("threshold")
            return float(v) if v is not None else _DEFAULT_THRESHOLD
        except Exception:
            return _DEFAULT_THRESHOLD

    # ── 资源 ──────────────────────────────────────────────
    def _ensure_ready(self) -> bool:
        """确保 extractor + 主人 embedding 就绪。失败置 sticky 并返回 False。"""
        if self._failed:
            return False
        if self._extractor is not None and self._owner_emb is not None:
            return True
        with self._lock:
            if self._extractor is not None and self._owner_emb is not None:
                return True
            if self._failed:
                return False
            try:
                if not os.path.exists(self._model_path):
                    logger.info("speaker_verify: 模型不存在，声纹放行: %s", self._model_path)
                    self._failed = True
                    return False
                if not os.path.exists(self._owner_path):
                    logger.info("speaker_verify: 主人样本不存在，声纹放行（未注册）: %s", self._owner_path)
                    self._failed = True
                    return False
                import sherpa_onnx
                cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                    model=self._model_path, debug=False, provider="cpu",
                    num_threads=max(1, (os.cpu_count() or 2) - 1))
                self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
                owner = _read_wav_16k_mono(self._owner_path)
                if owner is None:
                    self._failed = True
                    return False
                emb = self._embed(owner[0], owner[1])
                if emb is None:
                    self._failed = True
                    return False
                self._owner_emb = emb
                logger.info("speaker_verify: 就绪 (threshold=%.2f)", self._threshold)
                return True
            except Exception as e:
                logger.warning("speaker_verify: 初始化失败，声纹放行: %s", e)
                self._failed = True
                return False

    def _embed(self, audio: np.ndarray, sample_rate: int) -> np.ndarray | None:
        """提取说话人 embedding。"""
        try:
            stream = self._extractor.create_stream()
            stream.accept_waveform(sample_rate=sample_rate, waveform=audio)
            stream.input_finished()
            return np.array(self._extractor.compute(stream), dtype=np.float32)
        except Exception as e:
            logger.warning("speaker_verify: embedding 提取失败: %s", e)
            return None

    # ── 主接口 ────────────────────────────────────────────
    def similarity(self, wav_path: str) -> float | None:
        """返回录音与主人声纹的余弦相似度；不可用/失败返回 None。"""
        if not self._ensure_ready():
            return None
        got = _read_wav_16k_mono(wav_path)
        if got is None:
            return None
        emb2 = self._embed(got[0], got[1])
        if emb2 is None:
            return None
        a, b = self._owner_emb, emb2
        denom = float(np.linalg.norm(a) * np.linalg.norm(b))
        if denom == 0:
            return None
        return float(np.dot(a, b) / denom)

    def verify(self, wav_path: str) -> bool:
        """判断是否为主人。**任何异常/不可用都放行（True）**，绝不阻断语音输入。"""
        sim = self.similarity(wav_path)
        if sim is None:
            return True  # 失败放行
        ok = sim >= self._threshold
        logger.debug("speaker_verify: sim=%.4f threshold=%.2f -> %s", sim, self._threshold, ok)
        return ok

    @property
    def available(self) -> bool:
        """是否已就绪（可用于 UI 判断要不要显示声纹开关）。"""
        return self._ensure_ready()


# ── 进程级单例 ────────────────────────────────────────────
_singleton: SpeakerVerifier | None = None
_singleton_lock = threading.Lock()


def get_verifier() -> SpeakerVerifier:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = SpeakerVerifier()
    return _singleton


def verify_speaker(wav_path: str) -> bool:
    """便捷入口：判断 wav 是否为主人。不可用时放行。"""
    try:
        return get_verifier().verify(wav_path)
    except Exception as e:
        logger.warning("speaker_verify: 顶层异常，放行: %s", e)
        return True


def register_owner(src_wav: str) -> bool:
    """把一段主人录音登记为主人声纹样本（拷贝到 data/voiceprint/owner.wav）。

    仅做文件落地 + 重置单例缓存；不做质量校验（由调用方保证是干净的主人语音）。
    """
    try:
        import shutil
        dst = _resolve(_DEFAULT_OWNER)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src_wav, dst)
        global _singleton
        with _singleton_lock:
            _singleton = None  # 重置缓存，下次 verify 重新载入样本
        logger.info("speaker_verify: 主人声纹已登记 -> %s", dst)
        return True
    except Exception as e:
        logger.warning("speaker_verify: 登记失败: %s", e)
        return False


if __name__ == "__main__":
    # 自测：给两段 wav，打印各自与主人的相似度
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("用法: python -m core.speaker_verify <wav1> [wav2 ...]")
        print(f"模型: {_resolve(_DEFAULT_MODEL)}")
        print(f"样本: {_resolve(_DEFAULT_OWNER)}")
        sys.exit(0)
    v = get_verifier()
    print("available:", v.available, "threshold:", v._threshold)
    for p in sys.argv[1:]:
        sim = v.similarity(p)
        print(f"{p}: similarity={sim}  verify={v.verify(p)}")
