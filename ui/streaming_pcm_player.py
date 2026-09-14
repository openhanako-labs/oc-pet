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

        def bytesAvailable(self) -> int:  # noqa: N802
            with self._lock:
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
        with self._lock:
            if self._sink is not None:
                try:
                    self._sink.setVolume(self._volume)
                except Exception:
                    logger.debug("streaming_pcm: 音量设置失败", exc_info=True)

    def is_playing(self) -> bool:
        with self._lock:
            if self._sink is None:
                return False
            try:
                from PySide6.QtMultimedia import QAudio
                return self._sink.state() != QAudio.State.StoppedState
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
        """立即中断播放并释放资源（主线程调用）。"""
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
        """主线程定时器：有数据就开/续接 sink；播完就收尾。"""
        with self._lock:
            dev = self._device
            if dev is None:
                return
            try:
                from PySide6.QtMultimedia import QAudio
            except ImportError:
                return
            try:
                pending = dev.pending()
                if self._sink is None:
                    # 首块数据到了 → 打开声卡
                    if pending > 0:
                        self._open_sink()
                    elif dev.is_eof():
                        # 一直没有数据就结束（空合成）
                        self._cleanup()
                        self.on_end()
                    return

                state = self._sink.state()
                if pending > 0 and state == QAudio.State.IdleState:
                    # 数据又到了但设备已停 → 重新挂上
                    self._sink.start(dev)
                elif dev.is_eof() and pending == 0 and state == QAudio.State.IdleState:
                    self._cleanup()
                    try:
                        self.on_end()
                    except Exception:
                        logger.debug("streaming_pcm: on_end 回调失败", exc_info=True)
            except Exception:
                logger.debug("streaming_pcm: 泵循环异常", exc_info=True)

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
                self._sink.stop()
                self._sink.deleteLater()
            except Exception:
                logger.debug("streaming_pcm: sink 释放失败", exc_info=True)
            self._sink = None
        if self._device is not None:
            try:
                self._device.close()
                self._device.deleteLater()
            except Exception:
                logger.debug("streaming_pcm: 设备释放失败", exc_info=True)
            self._device = None
