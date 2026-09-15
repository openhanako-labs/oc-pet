"""流式 PCM 播放器 —— 边合成边播放，用 QAudioSink 播裸 PCM。

与 TTSTtsPlayer（QMediaPlayer + 文件路径）的区别：
  - QMediaPlayer 需要完整文件才能播 → 首音延迟 = 完整合成时间
  - QAudioSink 直接吃 PCM 字节流 → 首音延迟 = 首块合成时间

线程模型（关键）
----------------
QAudioSink 是 Qt 对象，start/stop 必须在创建它的线程（主线程）调用；
PCM 数据来自 TTS 工作线程。因此：

  - ``feed()`` 只往纯 Python 缓冲追加字节（线程安全，无 Qt 调用）
  - 音频数据由 ``_PcmStreamDevice.readData()`` 按需拉取（Qt 音频线程回调）
  - 主线程 QTimer 负责：有数据就开/续接 sink；播完就收尾

所有 Qt 调用都在主线程；跨线程只共享一个带锁的字节缓冲。

用法（桌宠主线程）：
    p = StreamingPcmPlayer(sample_rate=24000)
    p.on_start = lambda: ...      # 口型开始
    p.on_end = lambda: ...        # 口型复位
    p.prepare()                   # 建缓冲 + 启动泵（不打开声卡）
    # ... TTS 工作线程：p.feed(pcm) / p.finish()
    p.stop()                      # 立即中断
"""
from __future__ import annotations

import array
import logging
import math
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)


def _rms_int16(data: bytes) -> float:
    """计算 Int16 LE 单声道 PCM 的归一化 RMS（0.0~1.0）。

    用于口型驱动：返回值就是"此刻声音有多大"。
    空数据/非偶数长度返回 0.0（失败闭合，不驱动口型）。
    numpy 可用时走向量化，否则纯 Python 回退（音频线程，必须够快）。
    """
    if not data:
        return 0.0
    usable = len(data) - (len(data) % 2)
    if usable <= 0:
        return 0.0
    try:
        import numpy as np
        samples = np.frombuffer(data[:usable], dtype="<i2")
        if samples.size == 0:
            return 0.0
        mean_sq = float(np.mean(samples.astype("f4") ** 2))
    except Exception:
        arr = array.array("h")
        arr.frombytes(data[:usable])
        if not arr:
            return 0.0
        mean_sq = sum(float(v) * float(v) for v in arr) / len(arr)
    rms = math.sqrt(mean_sq) / 32768.0
    return min(1.0, max(0.0, rms))

try:
    from PySide6.QtCore import QIODevice, QTimer, QObject
    _QT_OK = True
except ImportError:  # pragma: no cover - 无 Qt 环境下优雅降级
    _QT_OK = False
    QIODevice = object  # type: ignore
    QObject = object  # type: ignore


if _QT_OK:

    class _PcmStreamDevice(QIODevice):
        """拉取式 PCM 源：QAudioSink 需要数据时回调 readData。"""

        def __init__(self):
            super().__init__()
            self._chunks: deque = deque()
            self._size = 0
            self._lock = threading.Lock()
            self._eof = False
            # 最近一次被声卡拉走的音频块的归一化 RMS（0~1）。
            # 由音频线程写、渲染线程读；Python 浮点赋值是原子的，无需额外锁。
            self.level: float = 0.0
            # 已被声卡取走的**总字节数**（LIP-1：口型时间轴的播放时钟）。
            # 用它而非墙钟，避免网络/合成卡顿时口型跑到声音前面。
            self.consumed_bytes: int = 0

        def append(self, data: bytes) -> None:
            if not data:
                return
            with self._lock:
                self._chunks.append(data)
                self._size += len(data)

        def mark_eof(self) -> None:
            with self._lock:
                self._eof = True

        def pending(self) -> int:
            with self._lock:
                return self._size

        def is_eof(self) -> bool:
            with self._lock:
                return self._eof

        def reset_eof(self) -> None:
            with self._lock:
                self._eof = False

        def current_level(self) -> float:
            """当前音频电平（0~1）。无数据/已播完时为 0.0。"""
            if self.is_eof() and self.pending() == 0:
                return 0.0
            return self.level

        def played_bytes(self) -> int:
            """已被声卡取走的总字节数。"""
            return self.consumed_bytes

        def isSequential(self) -> bool:  # noqa: N802 (Qt 命名)
            return True

        def atEnd(self) -> bool:  # noqa: N802 (Qt 命名)
            """流是否已结束。

            ⚠️ **必须覆写**（2026-09-14 修）。
            QIODevice.atEnd() 的默认实现是 `bytesAvailable() == 0`——
            对**流式**设备这是错的："暂时没数据"不等于"流结束"。

            实测后果：首块被拉走后缓冲变空，atEnd() 立即变 True →
            Qt 认为流已结束 → sink 停止；等下一块（可能 6 秒后）到达时，
            _pump_once 的重挂条件不成立 → 用户听到"播了一个字就不播了"。

            正确语义：只有收到 eof 标记（上游 finish()）且缓冲抽空才算结束。

            ⚠️ **绝不取 self._lock**（2026-09-15 死锁修复）。
            本方法由 **Qt 音频线程**在持有 Qt 内部锁时高频调用（每次 pull）。
            若在此取 self._lock，就与主线程形成 ABBA 死锁：
              音频线程：Qt内部锁 → 等 self._lock
              主线程  ：self._lock → 等 Qt内部锁（_sink.start/state）
            实测后果：主线程心跳停 → launcher 判卡死强杀（事故 2026-09-15）。

            无锁实现：`_eof` 是 bool、`_size` 是 int，CPython 下单个赋值/读取
            本身就是原子的；这里只做一次快照判断，不需要互斥。
            注意**不要调 self.bytesAvailable()**——那会进 super() 的 Qt 路径，
            反而把 Qt 内部锁卷进来。
            """
            if self._eof and self._size == 0:
                return True
            return False

        def bytesAvailable(self) -> int:  # noqa: N802
            # 无锁：_size 是 int（原子读），避开音频线程与主线程的锁交叉
            return self._size + super().bytesAvailable()

        def readData(self, maxlen: int) -> bytes:  # noqa: N802
            with self._lock:
                if not self._chunks:
                    return b""
                out = bytearray()
                while self._chunks and len(out) < maxlen:
                    chunk = self._chunks[0]
                    need = maxlen - len(out)
                    if len(chunk) <= need:
                        out += chunk
                        self._chunks.popleft()
                        self._size -= len(chunk)
                    else:
                        out += chunk[:need]
                        self._chunks[0] = chunk[need:]
                        self._size -= need
                result = bytes(out)
            # 实时电平：在"真正被声卡拉走"的这一层采样，是口型的唯一真相点。
            # 注意必须在锁外算（RMS 可能耗时，不能阻塞 append/feed）。
            self.level = _rms_int16(result)
            self.consumed_bytes += len(result)
            return result

        def writeData(self, data) -> int:  # noqa: N802
            return 0

else:  # pragma: no cover
    class _PcmStreamDevice:  # type: ignore
        def __init__(self):
            raise RuntimeError("PySide6 不可用")


class StreamingPcmPlayer(QObject):
    """QAudioSink 裸 PCM 流式播放器（单声道 Int16）。

    feed/finish 线程安全；prepare/stop 与内部定时器须在主线程。
    """

    def __init__(self, sample_rate: int = 24000, channels: int = 1, parent=None):
        if _QT_OK:
            super().__init__(parent)
        self._sample_rate = sample_rate
        self._channels = channels
        self._sink = None
        self._device: _PcmStreamDevice | None = None
        self._pump: QTimer | None = None
        self._lock = threading.RLock()
        self._active = False          # 正在流式播放（sink 已开或待开）
        self._enabled = True
        self._volume = 0.8
        self._last_error = ""
        self._finished = False
        # 2026-09-14 预缓冲：实测合成速率仅 0.36x 实时（低于播放所需 1.0x），
        # "首块一到就开声卡"会立即抽干缓冲 → 用户听到"两个字一卡"。
        # 分两档：
        #   - 固定档（_prebuffer_bytes）：无文本信息时的降级手段
        #   - 智能档（_expected_bytes）：知道本句总时长时，
        #     攒够"剩下合成时间所需的量"再开播——数学上永不断粮
        self._prebuffer_bytes = 0
        self._expected_bytes = 0
        self._received_bytes = 0
        # 智能预缓冲开关。**默认关**（2026-09-14 实测教训）：
        # R=0.36x 时，"撑到合成结束"需要攒到 ~74%——首字会晚 10 秒，
        # 比卡顿更难接受。故默认回到"首块即播"；想要流畅可手动开。
        self._smart_prebuffer_on = False
        self._prebuffer_max_wait_s = 8.0
        self._playback_wait_from = 0.0
        # 合成速率估计（实时倍率）。用于智能预缓冲的"剩余时间"推算。
        # 0.36 是实测值；每句结束后根据实际数据滑动更新。
        self._synth_rate = 0.36
        # 设备未就绪时先落在这里，prepare() 时冲刷（避免 feed 早于 prepare）
        self._pending: deque = deque()

        # 事件回调（与 TTSTtsPlayer 对齐，方便复用口型/状态链路）
        self.on_start: callable = lambda: None
        self.on_end: callable = lambda: None
        self.on_error: callable = lambda msg: None

    # ── 状态 ──

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def current_level(self) -> float:
        """当前正在播放的音频电平（0.0~1.0），供口型驱动。

        取的是声卡刚刚拉走那块数据的 RMS，所以与听到的声音同步。
        设备未就绪/已停时为 0.0（失败闭合：嘴闭上，不会僵在半开）。
        """
        dev = self._device
        if dev is None or not self._active:
            return 0.0
        try:
            return dev.current_level()
        except Exception:
            return 0.0

    def played_seconds(self) -> float:
        """已播放时长（秒）。LIP-1 口型时间轴的同步时钟。

        基于声卡实际取走的字节数计算，而非墙钟——
        这样网络/合成卡顿时口型不会跑到声音前面。
        """
        dev = self._device
        if dev is None:
            return 0.0
        try:
            nbytes = int(dev.played_bytes())
        except Exception:
            return 0.0
        # Int16 单声道：每样本 2 字节
        frames = nbytes / 2.0 / max(1, self._channels)
        return max(0.0, frames / float(self._sample_rate or 1))

    def disable(self) -> None:
        self._enabled = False
        self.stop()

    def set_volume(self, vol: float) -> None:
        self._volume = max(0.0, min(1.0, vol))
        # 锁内只取快照，Qt 调用在锁外（2026-09-15 锁序反转修复）
        with self._lock:
            sink = self._sink
        if sink is not None:
            try:
                sink.setVolume(self._volume)
            except Exception:
                logger.debug("streaming_pcm: 音量设置失败", exc_info=True)

    def set_prebuffer(self, seconds: float, max_wait_s: float = 8.0) -> None:
        """固定预缓冲时长（秒）。0 = 关闭。

        ⚠️ **固定值在慢合成下必然失败**（2026-09-14 教训）：
        实测合成速率仅 0.36x 实时（低于播放所需 1.0x）——
        消耗比供给快 0.64x，攒 1.5s 只够撑 2.3 秒就抽干。
        只要合成速率 < 1.0x，**任何"攒一点再播"都必然断粮，除非攒满**。

        所以优先用 `set_expected_duration()` 的智能预缓冲；
        本方法仅作为无文本信息时的降级手段。

        Args:
            seconds: 预缓冲目标时长。
            max_wait_s: 最长等待；超过则不等了。
        """
        try:
            sec = max(0.0, float(seconds))
        except (TypeError, ValueError):
            sec = 0.0
        # Int16 单声道 → 每秒字节数
        bytes_per_sec = 2 * max(1, self._channels) * max(1, self._sample_rate)
        self._prebuffer_bytes = int(sec * bytes_per_sec)
        try:
            self._prebuffer_max_wait_s = max(0.5, float(max_wait_s))
        except (TypeError, ValueError):
            self._prebuffer_max_wait_s = 8.0
        logger.info(
            "流式预缓冲（固定）：%.1fs（%d 字节），最长等 %.1fs",
            sec, self._prebuffer_bytes, self._prebuffer_max_wait_s,
        )

    def set_expected_duration(self, seconds: float, enable: bool = False) -> None:
        """告知本句的**预期音频总时长**（秒），供智能预缓冲使用。

        原理：合成速率 R < 1.0x 时，固定预缓冲必然失败。
        但我们可以**估计还剩多久合成完**，攒够那段时间即可：

            buffered_s >= (expected - received) / R

        即"缓冲里已有的音频，够不够撑到合成结束"。

        ⚠️ **默认不启用**（enable=False）：R=0.36 时需攒到 ~74% 才满足，
        首字会晚 10 秒——这是真实取舍，由调用方/配置决定。

        Args:
            seconds: 预期总时长（≤0 则不启用）。
            enable: 是否真的启用智能预缓冲。
        """
        try:
            sec = float(seconds)
        except (TypeError, ValueError):
            sec = 0.0
        bytes_per_sec = 2 * max(1, self._channels) * max(1, self._sample_rate)
        self._expected_bytes = int(max(0.0, sec) * bytes_per_sec)
        self._received_bytes = 0
        self._smart_prebuffer_on = bool(enable) and self._expected_bytes > 0
        if self._smart_prebuffer_on:
            logger.info("智能预缓冲已启用：预期音频 %.2fs（首字会晚，换流畅）", sec)

    def is_playing(self) -> bool:
        """是否正在播放。

        ⚠️ 本方法被**每帧口型探测**调用（高频）。绝不能持 `self._lock` 调
        `sink.state()`——那是 Qt 内部锁，会与音频线程形成锁序反转，
        主线程会冻死（事故 2026-09-15）。
        """
        with self._lock:
            sink = self._sink
        if sink is None:
            return False
        try:
            from PySide6.QtMultimedia import QAudio
            return sink.state() != QAudio.State.StoppedState
        except Exception:
            return False

    # ── 生命周期（主线程）──

    def prepare(self) -> bool:
        """建缓冲 + 启动泵，但不打开声卡（等首块数据到了再开）。

        在主线程调用一次；返回是否可用。会把 feed() 提前收到的数据冲刷进去。
        """
        if not self._enabled or not _QT_OK:
            return False
        with self._lock:
            try:
                if self._device is None:
                    self._device = _PcmStreamDevice()
                    self._device.open(QIODevice.OpenModeFlag.ReadOnly)
                if self._pump is None:
                    self._pump = QTimer()
                    self._pump.setInterval(30)
                    self._pump.timeout.connect(self._pump_once)
                    self._pump.start()
                self._active = True
                self._finished = False
                # LIP-1：新一句从 0 起算播放时钟（否则口型会继承上一句的进度）
                self._device.consumed_bytes = 0
                # 预缓冲：记录起等时刻 + 清空本句计数
                self._playback_wait_from = time.monotonic()
                self._received_bytes = 0
                # 冲刷 prepare 之前 feed 进来的数据
                while self._pending:
                    self._device.append(self._pending.popleft())
                return True
            except Exception as e:
                self._last_error = str(e)
                logger.warning("流式播放器 prepare 失败: %s", e)
                return False

    def _open_sink(self) -> bool:
        """（主线程）打开声卡并开始拉取。"""
        try:
            from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
        except ImportError as e:
            self._last_error = f"QAudioSink 不可用: {e}"
            logger.warning(self._last_error)
            self.on_error(self._last_error)
            return False

        fmt = QAudioFormat()
        fmt.setSampleRate(self._sample_rate)
        fmt.setChannelCount(self._channels)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)

        dev = QMediaDevices.defaultAudioOutput()
        if dev is None or dev.isNull():
            self._last_error = "无可用音频输出设备"
            logger.warning(self._last_error)
            self.on_error(self._last_error)
            return False
        if not dev.isFormatSupported(fmt):
            self._last_error = f"设备不支持 {self._sample_rate}Hz/{self._channels}ch/Int16"
            logger.warning(self._last_error)
            self.on_error(self._last_error)
            return False

        try:
            self._sink = QAudioSink(dev, fmt)
            self._sink.setVolume(self._volume)
            self._sink.start(self._device)
            self.on_start()
            logger.info("StreamingPcmPlayer: 开始流式播放 (%dHz)", self._sample_rate)
            return True
        except Exception as e:
            self._last_error = str(e)
            logger.warning("流式播放启动失败: %s", e)
            self.on_error(str(e))
            return False

    def feed(self, pcm: bytes) -> None:
        """追加 PCM 数据（Int16 LE 单声道）。任意线程可调用。

        设备未就绪时先缓存，prepare() 时自动冲刷。
        """
        if not pcm:
            return
        self._received_bytes += len(pcm)
        dev = self._device
        if dev is None:
            self._pending.append(pcm)
            return
        dev.append(pcm)

    def finish(self) -> None:
        """声明数据发送完毕；缓冲播完后触发 on_end。"""
        dev = self._device
        if dev is not None:
            dev.mark_eof()
        else:
            self._finished = True

    def stop(self) -> None:
        """立即中断播放并释放资源（主线程调用）。

        ⚠️ `_cleanup()` 在**锁外**调用（2026-09-15）：它内部的 `sink.stop()`
        会与 Qt 音频线程同步；持 `self._lock` 调用会把主线程卡死
        （音频线程若在 `readData` 等锁，就互相等待）。
        """
        with self._lock:
            was = self._active or self._sink is not None
        self._cleanup()
        if was:
            try:
                self.on_end()
            except Exception:
                logger.debug("streaming_pcm: on_end 回调失败", exc_info=True)

    # ── 内部（主线程）──

    def _pump_once(self) -> None:
        """主线程定时器：有数据就开/续接 sink；播完就收尾。

        ⚠️ **Qt 调用一律在锁外**（2026-09-15 死锁修复）。
        `sink.start/stop/state` 会与 **Qt 音频线程**同步（内部锁），
        而音频线程又会在 `readData` 里取设备锁。
        若主线程持 `self._lock` 调这些方法，就形成锁序反转：
          主线程：self._lock → Qt内部锁
          音频线程：Qt内部锁 → 设备锁
        实测后果：主线程心跳停 → launcher 判卡死强杀（事故 2026-09-15）。

        本方法因此只在锁内**取快照**，锁外做所有 Qt 操作。
        `self._sink/_device` 由主线程独占写，快照不会读到半成品。
        """
        with self._lock:
            dev = self._device
            sink = self._sink
        if dev is None:
            return
        try:
            from PySide6.QtMultimedia import QAudio
        except ImportError:
            return
        try:
            pending = dev.pending()
            if sink is None:
                # 首块数据到了 → 判断是否攒够预缓冲再开声卡
                if pending > 0:
                    if self._should_start_playback(dev, pending):
                        self._open_sink()
                    # 不够就继续等（下个泵周期再看）
                elif dev.is_eof():
                    # 一直没有数据就结束（空合成）
                    self._cleanup()
                    self.on_end()
                return

            state = sink.state()
            # 2026-09-14：重挂条件从 "仅 IdleState" 改为 "非 ActiveState"。
            # 原因：atEnd 误判导致 sink 停止时，状态可能是 StoppedState
            # （而非 IdleState），旧条件永远不成立 → 后续数据再也接不上。
            # 只要还有数据且声卡没在跑，就重新 start。
            if pending > 0 and state != QAudio.State.ActiveState:
                try:
                    sink.start(dev)
                    logger.debug(
                        "流式播放：重新挂上 sink（state=%s, pending=%d）",
                        state, pending,
                    )
                except Exception as e:
                    logger.debug("重挂 sink 失败: %s", e)
            elif (dev.is_eof() and pending == 0
                  and state != QAudio.State.ActiveState):
                self._cleanup()
                try:
                    self.on_end()
                except Exception:
                    logger.debug("streaming_pcm: on_end 回调失败", exc_info=True)
        except Exception:
            logger.debug("streaming_pcm: 泵循环异常", exc_info=True)

    def _should_start_playback(self, dev, pending_bytes: int) -> bool:
        """是否该开声卡了（预缓冲判断）。

        优先智能档（知道预期总时长）：攒够"撑到合成结束"的量。
        降级固定档：攒够 _prebuffer_bytes。

        不等的情况：
        - 预缓冲已关闭
        - 合成已结束（eof）——再等也不会有新数据，短句应直接播
        - 已等超时（_prebuffer_max_wait_s）——防合成极慢时永远不出声
        """
        if dev.is_eof():
            return True
        expected = getattr(self, "_expected_bytes", 0)
        if expected > 0 and getattr(self, "_smart_prebuffer_on", False):
            need = self._smart_prebuffer_need(expected)
            if need is not None:
                if pending_bytes >= need:
                    return True
                waited = time.monotonic() - getattr(self, "_playback_wait_from", 0.0)
                if waited >= getattr(self, "_prebuffer_max_wait_s", 8.0):
                    logger.debug("智能预缓冲超时（等 %.1fs），先开播", waited)
                    return True
                return False
        need = getattr(self, "_prebuffer_bytes", 0)
        if need <= 0:
            return True
        if pending_bytes >= need:
            return True
        waited = time.monotonic() - getattr(self, "_playback_wait_from", 0.0)
        if waited >= getattr(self, "_prebuffer_max_wait_s", 8.0):
            logger.debug(
                "预缓冲超时（等了 %.1fs，仅 %d/%d 字节），先开播",
                waited, pending_bytes, need,
            )
            return True
        return False

    def _smart_prebuffer_need(self, expected_bytes: int):
        """智能预缓冲目标字节数；无法推算时返回 None（降级固定档）。

        推导（R = 合成速率，单位"音频秒/墙钟秒"）：
          - 已收到音频 = received / bytes_per_sec （秒）
          - 剩余待合成音频 = (expected - received) / bytes_per_sec
          - 合成这些还需墙钟 = 剩余音频 / R
          - 播放这些需墙钟 = 剩余音频 / 1.0
          要不抽干，需要缓冲里的音频 >= 合成剩余所需的墙钟时间：
              buffered_s >= 剩余音频 / R
          → buffered_bytes >= (expected - received) / R

        取 R = 估计的合成速率（默认 0.36，实测值）。
        """
        try:
            rate = float(getattr(self, "_synth_rate", 0.36))
        except (TypeError, ValueError):
            rate = 0.36
        if rate <= 0.01:
            return None
        received = min(int(getattr(self, "_received_bytes", 0)), int(expected_bytes))
        remaining = max(0, int(expected_bytes) - received)
        # 剩余音频 / R = 需要撑住的墙钟秒数，换算成字节
        need = int(remaining / rate)
        # 不超过总量（攒满就不必再等）
        return min(need, int(expected_bytes))

    def _cleanup(self) -> None:
        self._active = False
        if self._pump is not None:
            try:
                self._pump.stop()
                self._pump.deleteLater()
            except Exception:
                logger.debug("streaming_pcm: 定时器释放失败", exc_info=True)
            self._pump = None
        if self._sink is not None:
            try:
                # 先 stop（停止拉数据），再断开 source，最后才 close device。
                # 顺序很重要：deleteLater 是延迟删除，若先 close 了 device，
                # sink 在真正析构前仍可能去读 → 报
                # "QIODevice::read (_PcmStreamDevice): device not open"。
                self._sink.stop()
                try:
                    self._sink.setSource(None)  # 断开拉取源
                except Exception:
                    logger.debug("sink 断开 source 失败（非致命）", exc_info=True)
                self._sink.deleteLater()
            except Exception:
                logger.debug("streaming_pcm: sink 释放失败", exc_info=True)
            self._sink = None
        if self._device is not None:
            try:
                # 先确保没有活跃的 read 回调，再关闭
                try:
                    self._device.blockSignals(True)
                except Exception:
                    pass
                self._device.close()
                self._device.deleteLater()
            except Exception:
                logger.debug("streaming_pcm: 设备释放失败", exc_info=True)
            self._device = None
