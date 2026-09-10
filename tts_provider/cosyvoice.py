"""CosyVoice 本地 TTS —— 子进程版客户端。

设计说明
--------
早先这里是**进程内**加载：``CosyVoice2(model_path)`` 直接在桌宠进程里跑。
实测代价是 48 秒（import 链 17s + 4.6GB 模型构造 31s），且这段时间 torch 在
加载 C 扩展、反序列化权重，会长时间独占 GIL。哪怕把 ``preload()`` 丢进后台
线程，Qt 主线程照样被饿死——日志里 ``eventFilter[release] slow: 246.5ms`` 就是
这么来的；连点几次「保存设置」还会产生多个实例并发抢 GIL，直接假死。

现在改为：重活全部关进 ``cosyvoice_worker.py`` 子进程，本进程只做三件轻活
——MD5 缓存命中判断、speaker_refs 查表、管道收发 JSON。等待管道读取时
CPython 会释放 GIL，事件循环全程不受影响。

对外接口与旧版完全一致，pet.py / conversation_engine.py 无需改动。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from .base import TTSProvider

logger = logging.getLogger(__name__)


def _resolve_output_dir() -> Path:
    """定位 TTS 缓存目录；HOME 不可用（极少见的环境）时兜底到系统临时目录。

    Path.home() 在 HOME/USERPROFILE 均未设置的环境会抛 RuntimeError，
    模块导入期绝不能因此崩掉。
    """
    try:
        return Path.home() / ".hanako" / "pets" / "tts_cache"
    except Exception:
        logger.debug("cosyvoice: 非致命异常(已静默吞掉)", exc_info=True)
    try:
        import tempfile
        return Path(tempfile.gettempdir()) / "hanako" / "pets" / "tts_cache"
    except Exception:
        # 最后兜底：模块所在目录下的 .tts_cache（项目可写目录）
        return Path(__file__).resolve().parent / ".tts_cache"


OUTPUT_DIR = _resolve_output_dir()
MODEL_NAME = "CosyVoice2-0.5B"

CACHE_TTL = 24 * 3600        # 超过 1 天视为可清理
_SWEEP_INTERVAL = 600        # 每 10 分钟最多扫一次缓存目录，避免每次合成都遍历


def _resolve_cosyvoice_dir() -> Path:
    """定位 cosyvoice-tts 项目目录（含 src/ 与 models/）。

    解析优先级（命中即返回）：
      1. 环境变量 OC_PET_COSYVOICE_DIR
      2. oc-pet 配置 config.tts.cosyvoice_dir
      3. 与 oc-pet 相邻的 <父目录>/cosyvoice-tts（把两个仓库并排放即零配置）
      4. 兜底硬编码路径（仅本机可用，换机器必失效）

    都不存在时抛清晰错误，而不是在模型加载阶段诡异崩溃。
    """
    env = os.environ.get("OC_PET_COSYVOICE_DIR", "").strip()
    if env:
        return Path(env)

    try:
        from config import load_config
        c = (load_config().get("tts", {}) or {}).get("cosyvoice_dir", "")
        if c:
            return Path(c)
    except Exception:
        logger.debug("cosyvoice: 非致命异常(已静默吞掉)", exc_info=True)

    adjacent = Path(__file__).resolve().parents[2] / "cosyvoice-tts"
    if adjacent.exists():
        return adjacent

    # 兜底硬编码路径（仅本机可用）：正常应通过 env/配置/相邻目录命中，
    # 走到这里说明三处都未配置，换机器必然失效。
    logger.warning(
        "CosyVoice 目录未通过 env/配置/相邻目录定位，回退到硬编码路径 "
        "W:/Games/Hanako/Work/projects/cosyvoice-tts（仅限本机，换机器请配置 OC_PET_COSYVOICE_DIR）")
    return Path("W:/Games/Hanako/Work/projects/cosyvoice-tts")


COSYVOICE_DIR = _resolve_cosyvoice_dir()

# 默认角色音色映射。config.json 被 gitignore，不能直接依赖它来兜底——
# 否则新克隆 / 清配置后 yuexinmiao 又会静默退回 spk_list[0]（ophelia）串音。
# 这里写死一个合理默认，config.tts.voices 仍可逐角色覆盖。
_DEFAULT_VOICE_MAP = {
    "yuexinmiao": "luoqixi",
    "miku": "ophelia",
}
SPEAKER_REFS = COSYVOICE_DIR / "speaker_refs.json"
_WORKER = Path(__file__).with_name("cosyvoice_worker.py")

# 首次加载实测 ~48s（import 17s + 模型 31s），冷启动留足余量
LOAD_TIMEOUT = 600.0
# 合成超时：CPU 环境下单句实测 60-150s（无 CUDA 时），180s 偏紧——长句/低配
# 机器可能误超时，导致 worker 仍在合成但客户端已放弃（脏队列假死）。放宽到 300s，
# 与 LOAD_TIMEOUT 的“给 CPU 环境留足余量”策略一致。
SYNTH_TIMEOUT = 300.0


def _project_venv_python() -> Optional[str]:
    """cosyvoice-tts 项目自带的解释器，找不到返回 None。"""
    for rel in ("venv/Scripts/python.exe", "venv/bin/python",
                ".venv/Scripts/python.exe", ".venv/bin/python"):
        cand = COSYVOICE_DIR / rel
        if cand.exists():
            return str(cand)
    return None


def _resolve_worker_python() -> str:
    """挑选运行 worker 的解释器。

    worker 是独立进程，本可以用任意 Python。cosyvoice-tts 自带一个 venv
    （torch 2.3.1+cu121），直觉上「专用环境更对口」，但实测反而更慢：

        主进程环境 torch 2.11+cu128 : import 17.4s / 建模 48.3s / 合成 13.5s
        项目 venv  torch 2.3.1+cu121: import 19.4s / 建模 90.9s / 合成 17.5s

    新版 torch 的权重加载快将近一倍，所以主环境能跑就用主环境；venv 只作为
    「主环境缺 torch/soundfile」时的兜底。

    find_spec 只定位不导入，不会把 torch 拉进桌宠进程——这正是我们要避免的事。
    """
    override = os.environ.get("OC_PET_COSYVOICE_PYTHON", "").strip()
    if override and Path(override).exists():
        return override

    try:
        import importlib.util
        if all(importlib.util.find_spec(m) is not None
               for m in ("torch", "soundfile")):
            return sys.executable
    except Exception:
        logger.debug("cosyvoice: 非致命异常(已静默吞掉)", exc_info=True)

    return _project_venv_python() or sys.executable


def _cuda_dll_path_entries(python: str) -> list[str]:
    """收集 worker 子进程需要的 CUDA/cuDNN 运行时 DLL 目录。

    CosyVoice2 的解码器是 ONNX，能否跑在显卡上取决于 ORT 的
    CUDAExecutionProvider 能不能初始化——它要搜到 cudart64_12.dll /
    cublas64_12.dll / cublasLt64_12.dll / cudnn64_9.dll 这几样。

    系统装的是 CUDA 12（torch 自带的运行时就在 site-packages/torch/lib 里），
    但 cudnn64_9.dll 是后来用 ``nvidia-cudnn-cu12`` 这个 pip 包补的，路径在
    site-packages/nvidia/cudnn/lib。这些都未必在系统 PATH 上，所以这里显式
    拼出来，让 worker 子进程继承，避免 ORT 静默退回 CPU（那样每句要 60-150s）。
    """
    sp = os.path.dirname(python)
    site = os.path.join(sp, "Lib", "site-packages")
    entries: list[str] = []
    # torch 自带的 CUDA 12 运行时
    torch_lib = os.path.join(site, "torch", "lib")
    if os.path.isdir(torch_lib):
        entries.append(torch_lib)
    # nvidia-* pip 轮子（cudnn / cuda-runtime / cublas …）的 lib / bin 目录
    # 注意：这些轮子把 DLL 放在 bin/ 下（不是 lib/），两种都扫一遍
    nvidia_root = os.path.join(site, "nvidia")
    if os.path.isdir(nvidia_root):
        for name in sorted(os.listdir(nvidia_root)):
            for sub in ("lib", "bin"):
                lib = os.path.join(nvidia_root, name, sub)
                if os.path.isdir(lib):
                    entries.append(lib)
    # 去重保序
    seen, out = set(), []
    for e in entries:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


class CosyVoiceProvider(TTSProvider):
    """本地 CosyVoice2 TTS（子进程隔离）"""

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._loaded = False
        self._speaker_refs: dict = {}
        self._voice_map: dict = {}      # 角色 -> 音色别名（oc-pet 自己的配置）
        self._speakers: list[str] = []
        self._replies: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._lock = threading.Lock()   # 串行化请求/响应配对
        self._req_id = 0
        self._closing = False
        self._last_sweep = 0.0          # 上次清理缓存目录的时间（节流）

    # ── 基本属性 ────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "cosyvoice"

    @property
    def is_ready(self) -> bool:
        return self._loaded and self._alive

    @property
    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ── 子进程生命周期 ──────────────────────────────────────────

    def _spawn(self) -> bool:
        if self._alive:
            return True
        if not _WORKER.exists():
            logger.warning("CosyVoice worker 脚本缺失: %s", _WORKER)
            return False

        env = os.environ.copy()
        env.setdefault("OC_PET_COSYVOICE_DIR", str(COSYVOICE_DIR))
        env["PYTHONIOENCODING"] = "utf-8"
        # 子进程会自己把 fd 1 改道到 stderr，只在协议流上写 JSON
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        python = _resolve_worker_python()
        if python != sys.executable:
            logger.info("CosyVoice worker 使用专用环境: %s", python)

        # 把 CUDA/cuDNN 运行时 DLL 目录塞进 PATH，让 worker 能初始化
        # ORT 的 CUDAExecutionProvider（否则静默退回 CPU，每句 60-150s）
        cuda_entries = _cuda_dll_path_entries(python)
        if cuda_entries:
            env["PATH"] = ";".join(cuda_entries) + ";" + env.get("PATH", "")
            logger.info("CosyVoice worker 注入 CUDA DLL 路径: %s",
                        ";".join(os.path.basename(e) for e in cuda_entries))

        try:
            self._proc = subprocess.Popen(
                [python, "-u", str(_WORKER)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=str(_WORKER.parent),
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except Exception as e:
            logger.warning("CosyVoice worker 启动失败: %s", e)
            self._proc = None
            return False

        self._closing = False
        # 清空上一条命的残留
        while not self._replies.empty():
            try:
                self._replies.get_nowait()
            except queue.Empty:
                break

        threading.Thread(target=self._pump_stdout, daemon=True,
                         name="cosyvoice-stdout").start()
        threading.Thread(target=self._pump_stderr, daemon=True,
                         name="cosyvoice-stderr").start()
        logger.info("CosyVoice worker 已启动 (pid=%s)", self._proc.pid)
        return True

    def _pump_stdout(self):
        """协议流 → 队列。EOF 时投递 None 作为死亡哨兵。"""
        proc = self._proc
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._replies.put(json.loads(line))
                except Exception:
                    logger.debug("CosyVoice worker 非 JSON 输出: %s", line[:200])
        except Exception:
            logger.debug("cosyvoice: 非致命异常(已静默吞掉)", exc_info=True)
        finally:
            self._replies.put(None)

    def _pump_stderr(self):
        """必须排空，否则管道写满会把子进程卡死。

        可见性策略（2026-09-10 修）：
        - 以 ``[cosyvoice-worker]`` 开头的是我们自己 worker 的 _log 输出，一律提到 info。
          原实现靠关键词（fail/error/cuda/...）猜，会漏掉不带关键词的失败描述，
          如「instruct2 produced no audio, falling back to zero_shot」——
          而这类恰恰是「没声音但也不报错」的情形，必须可见。
        - 第三方噪声（torch/ORT 警告）仍留 debug 防刷屏。
        """
        proc = self._proc
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if not line:
                    continue
                if line.startswith("[cosyvoice-worker]"):
                    logger.info("[cosyvoice] %s", line[:300])
                elif any(k in line for k in ("fp16", "ORT", "ready", "cuda", "CUDA",
                                             "provider", "error", "Error", "fail")):
                    logger.info("[cosyvoice] %s", line[:300])
                else:
                    logger.debug("[cosyvoice] %s", line[:500])
        except Exception as e:
            # stderr 停止排空 = 管道写满 → worker 卡死 → 合成永久停摆。不能静默。
            logger.warning("cosyvoice: stderr 排空中断，worker 可能卡死: %s", e)

    def _drain_replies(self) -> int:
        """清空响应队列，返回丢弃条数。

        超时/异常后调用：把 worker 迟到的陈旧响应全部丢弃，避免“超时后下一条
        请求配到旧响应”的脏队列问题（配错 id 后永久假死）。
        """
        drained = 0
        while True:
            try:
                self._replies.get_nowait()
                drained += 1
            except queue.Empty:
                break
        return drained

    def _request(self, payload: dict, timeout: float) -> Optional[dict]:
        """发一条请求并等待同 id 响应；失败返回 None。"""
        with self._lock:
            if not self._alive:
                return None
            self._req_id += 1
            rid = self._req_id
            payload = {**payload, "id": rid}
            try:
                self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except Exception as e:
                logger.warning("CosyVoice worker 写入失败: %s", e)
                self._loaded = False
                return None

            while True:
                try:
                    msg = self._replies.get(timeout=timeout)
                except queue.Empty:
                    logger.warning("CosyVoice worker 响应超时 (cmd=%s, %.0fs)",
                                   payload.get("cmd"), timeout)
                    # 超时后必须清空响应队列 + 置 _loaded=False：
                    # 1) 陈旧响应（本次请求的超时响应）若留着，会配给下一条请求的
                    #    同 id 检查，导致“拿旧响应当新结果”甚至假死；
                    # 2) 置 _loaded=False 让 is_ready 变 False，下次 synthesize 前
                    #    preload 会重新 load/重启 worker，而不是继续用脏状态。
                    self._loaded = False
                    _drained = self._drain_replies()
                    if _drained:
                        logger.warning("CosyVoice 超时后丢弃 %d 条陈旧响应", _drained)
                    return None
                if msg is None:                      # 子进程死了
                    if not self._closing:
                        logger.warning("CosyVoice worker 意外退出")
                    self._loaded = False
                    self._drain_replies()
                    return None
                if msg.get("id") == rid:
                    return msg
                # hello 之类的单向事件，忽略后继续等

    # ── TTSProvider 接口 ────────────────────────────────────────

    def preload(self):
        if self.is_ready:
            return

        # 校验模型目录：缺失时给清晰指引，而不是在 worker 加载阶段诡异崩溃
        model_dir = COSYVOICE_DIR / "models" / MODEL_NAME
        if not model_dir.exists():
            logger.warning(
                "CosyVoice 模型目录不存在：%s\n"
                "  → 请先运行 scripts/setup_tts_env.py 下载模型，或在 .env 设置 "
                "OC_PET_COSYVOICE_DIR 指向含 models/%s 的 cosyvoice-tts 目录。",
                model_dir, MODEL_NAME,
            )
            self._loaded = False
            return

        try:
            if SPEAKER_REFS.exists():
                self._speaker_refs = json.loads(SPEAKER_REFS.read_text("utf-8"))
        except Exception as e:
            logger.debug("speaker_refs 读取失败: %s", e)

        # 角色→音色映射放在 oc-pet 的 config 里，不去改 cosyvoice-tts
        # 项目共享的 speaker_refs.json（那份还被 CLI 等其他工具使用）。
        # config 被 gitignore，所以再用 _DEFAULT_VOICE_MAP 兜底——config
        # 里写的会覆盖默认，没写的沿用默认。
        cfg_map: dict = {}
        try:
            from config import load_config
            cfg_map = (load_config().get("tts", {}) or {}).get("voices", {}) or {}
        except Exception as e:
            logger.debug("tts.voices 读取失败: %s", e)
        self._voice_map = {**_DEFAULT_VOICE_MAP, **cfg_map}

        if not self._spawn():
            self._loaded = False
            return

        resp = self._request({"cmd": "load"}, LOAD_TIMEOUT)
        if not resp or not resp.get("ok"):
            # CosyVoice 是可选依赖，缺失时静默降级，不报 ERROR
            reason = (resp or {}).get("error", "worker 无响应")
            logger.info("CosyVoice 不可用: %s", reason)
            self._loaded = False
            self.cleanup()
            return

        self._loaded = True
        speakers = resp.get("speakers") or []
        self._speakers = speakers
        logger.info(
            "CosyVoice 模型就绪 | sample_rate=%s | 可用音色(%d): %s",
            resp.get("sample_rate"), len(speakers), ", ".join(speakers[:16]),
        )
        # 无 CUDA 时本地 TTS 会跑在 CPU 上（每句 60-150s），明确告知用户，
        # 而不是静默慢速——他们可以改用设置里的 MIMO / 在线 TTS。
        if not resp.get("cuda_available"):
            logger.warning(
                "⚠ 当前环境无可用 CUDA（NVIDIA 显卡），本地 CosyVoice 将运行在 CPU 上，"
                "单句合成可能需 1-2 分钟。建议在「设置 → TTS」改用 MIMO / 在线 TTS，"
                "或安装 NVIDIA 驱动 + CUDA 后重跑 scripts/setup_tts_env.py。"
            )
        # 角色没登记音色时会退回 SFT 的第一个说话人，容易「串音」，明确提示
        if not self._speaker_refs:
            logger.info("speaker_refs.json 为空，所有角色将使用默认说话人")
        if self._voice_map:
            logger.info("角色音色映射: %s", self._voice_map)
        refs = sorted(self._speaker_refs.keys())
        if refs:
            logger.info("可克隆参考音色(%d): %s", len(refs), ", ".join(refs))

    def get_speaker_info(self, character_id: str) -> dict:
        return self._resolve_voice(character_id)[0]

    def _resolve_voice(self, character_id: str, voice: str = "") -> tuple[dict, str]:
        """解析角色该用哪个声音，返回 (合成参数, 音色标识)。

        音色标识用于日志和缓存键——换了音色必须让旧缓存失效，否则角色会
        一直用改之前的嗓子说话。

        解析顺序（P2-7 起支持显式 voice 覆盖）：
          · voice 参数非空（上层角色/情绪音色映射）→ 直接用它当音色名；
          · 否则看 config 的 tts.voices 有没有为该角色指定音色；
          · 都没有就拿角色 id 本身当音色名试一次。
        命中之后：

          · 是模型里已注册的 SFT 说话人 → 走 SFT。这些说话人当初就是用
            speaker_refs 的参考音频注册进去的，音色一致，但省掉了每次合成
            重新编码参考音频的开销，明显更快。
          · 只在 speaker_refs 里有参考音频 → 零样本克隆。
          · 都不是 → 交给 worker 退回 SFT 第一个说话人（会串音，已告警）。
        """
        alias = (voice or self._voice_map.get(character_id) or character_id).strip() or character_id

        # 优先零样本克隆：只要角色有参考音频就用 ref（我们注册的 8 个音色全有参考音频，
        # 且它们被 add_zero_shot_spk 写进 spk2info 后，键是 llm_embedding/flow_embedding，
        # 不是 SFT 用的 embedding——当成 SFT 说话人会让 frontend_sft 抛 KeyError: 'embedding'。
        ref = self._speaker_refs.get(alias)
        if ref:
            return dict(ref), alias

        # 只有模型原生 SFT 说话人才走 spk 分支（CosyVoice2 0.5B 通常为空）
        if alias in self._speakers:
            return {"spk": alias}, alias

        if alias != character_id:
            logger.warning(
                "角色 '%s' 配置的音色 '%s' 既不是 SFT 说话人也没有参考音频，已忽略",
                character_id, alias,
            )
        return {}, "default"

    def synthesize(self, text: str, character_id: str = "", instruct: str = "",
                   voice: str = "", emotion: str = "") -> Optional[str]:
        if not text or not text.strip():
            return None

        text = text.strip()[:500]
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        # 节流式 TTL 清理：>1 天的 wav 删除，封顶磁盘增长（缓存键含完整文本，命中率≈0）
        now = time.monotonic()
        if now - self._last_sweep > _SWEEP_INTERVAL:
            self._last_sweep = now
            self._sweep_cache(CACHE_TTL)

        # 音色进缓存键：换了嗓子必须重新合成，否则一直放旧声音
        # P2-7: 显式 voice 参数优先（角色/情绪音色映射），空则走角色音色映射
        spk_info, voice_id = self._resolve_voice(character_id, voice)
        text_hash = hashlib.md5(
            f"{character_id}:{voice_id}:{instruct or ''}:{text}".encode()
        ).hexdigest()[:12]
        output_path = OUTPUT_DIR / f"{character_id}_{text_hash}.wav"

        # 缓存命中不需要惊动子进程
        if output_path.exists():
            logger.info("TTS cache hit: %s", output_path.name)
            return str(output_path)

        if not self.is_ready:
            return None

        resp = self._request({
            "cmd": "synth",
            "text": text,
            "out": str(output_path),
            "ref_audio": spk_info.get("ref_audio", ""),
            "ref_text": spk_info.get("ref_text", ""),
            "spk": spk_info.get("spk", ""),
            "instruct": instruct or "",
        }, SYNTH_TIMEOUT)

        if not resp or not resp.get("ok"):
            logger.warning("TTS 合成失败: %s", (resp or {}).get("error", "无响应"))
            return None

        logger.info("TTS done (%s%s): %s", resp.get("mode"),
                    f", spk={resp['spk']}" if resp.get("spk") else "",
                    output_path.name)
        return resp.get("path") or str(output_path)

    def _sweep_cache(self, max_age: float) -> None:
        """删除超过 max_age 秒的缓存 wav（只在 synthesize 节流调用）。"""
        try:
            cutoff = time.time() - max_age
            removed = 0
            for f in OUTPUT_DIR.glob("*.wav"):
                try:
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink()
                        removed += 1
                except FileNotFoundError:
                    logger.debug("cosyvoice: 非致命异常(已静默吞掉)", exc_info=True)
            if removed:
                logger.info("TTS 缓存清理: 删除 %d 个过期 wav", removed)
        except Exception:
            logger.debug("cosyvoice: 非致命异常(已静默吞掉)", exc_info=True)

    def cleanup(self):
        self._closing = True
        self._loaded = False
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.write('{"cmd":"quit"}\n')
            proc.stdin.flush()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    logger.debug("cosyvoice: 非致命异常(已静默吞掉)", exc_info=True)
        logger.info("CosyVoice worker 已停止")
