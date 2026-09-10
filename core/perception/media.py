"""SMTC 感知 — 读取正在播放的媒体信息（歌曲名、艺术家、专辑等）

Windows SMTC（System Media Transport Controls）API 读取系统级媒体播放状态。
成本极低（无网络调用），最自然的搭话由头。

外部依赖：
- pywin32：Windows COM API

功能：
- 定时轮询 SMTC（默认 10s）
- 变化检测：歌曲名/艺术家变化时触发事件
- 媒体元数据：title / artist / album / duration
- 播放状态：playing / paused / stopped
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import comtypes

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════
#  类型定义
# ════════════════════════════════════════════════════════════

@dataclass
class MediaEvent:
    """媒体播放事件"""
    title: str = ""          # 歌曲名
    artist: str = ""         # 艺术家
    album: str = ""          # 专辑
    duration: float = 0.0    # 时长（秒）
    state: str = "playing"   # playing / paused / stopped
    timestamp: float = 0.0   # 事件时间戳

@dataclass
class MediaInfo:
    """媒体信息快照"""
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    state: str = "stopped"
    source: str = ""         # 来源应用（如 "Spotify", "网易云音乐"）


# ════════════════════════════════════════════════════════════
#  SMTC 读取
# ════════════════════════════════════════════════════════════

def _read_smtc() -> Optional[MediaInfo]:
    """从 Windows SMTC 读取当前播放的媒体信息。
    
    Returns:
        MediaInfo 或 None（无播放/读取失败）
    """
    try:
        # Windows COM API
        from comtypes.client import CreateObject
        from comtypes import CoInitialize, CoUninitialize
        
        CoInitialize(None)
        
        try:
            # 获取 SystemMediaTransportControlsManager
            manager = CreateObject(
                "Windows.System_MEDIA_TRANSPORT_CONTROLS_MANAGER",
                interface=comtypes.IDispatch
            )
            
            # 获取当前会话
            session = manager.GetCurrentSession()
            if not session:
                return None
            
            # 获取媒体信息
            info = MediaInfo()
            
            # 标题
            title = session.GetTitle()
            if title:
                info.title = str(title)
            
            # 艺术家
            artist = session.GetArtist()
            if artist:
                info.artist = str(artist)
            
            # 专辑
            album = session.GetAlbumTitle()
            if album:
                info.album = str(album)
            
            # 时长
            duration = session.GetDuration()
            if duration:
                info.duration = float(duration) / 10000000.0  # 100ns -> 秒
            
            # 播放状态
            state = session.GetPlaybackStatus()
            if state == 1:  # PlayStatus.Playing
                info.state = "playing"
            elif state == 2:  # PlayStatus.Paused
                info.state = "paused"
            else:
                info.state = "stopped"
            
            # 来源应用（简化：从 ProcessId 读取进程名）
            try:
                pid = session.GetProcessId()
                if pid:
                    info.source = f"pid:{pid}"
            except Exception as e:
                # 仅丢失来源应用名，不影响标题/艺术家读取
                logger.debug("media: GetProcessId 失败（来源应用名缺失）: %s", e)
            
            if info.title or info.artist:
                return info
            return None
            
        finally:
            CoUninitialize()
            
    except Exception as e:
        logger.debug("SMTC 读取失败: %s", e)
        return None


# ════════════════════════════════════════════════════════════
#  感知控制器
# ════════════════════════════════════════════════════════════

class MediaPerception:
    """媒体感知控制器 — 定时轮询 SMTC，变化时触发回调"""
    
    POLL_INTERVAL = 10.0  # 默认 10 秒轮询
    
    def __init__(self, poll_interval: float = None, on_media_change: Callable[[MediaEvent], None] = None):
        """初始化媒体感知
        
        Args:
            poll_interval: 轮询间隔（秒），默认 10s
            on_media_change: 媒体变化回调（MediaEvent）
        """
        self._poll_interval = poll_interval or self.POLL_INTERVAL
        self._on_media_change = on_media_change
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_info: Optional[MediaInfo] = None
        self._current: Optional[MediaInfo] = None
    
    def start(self):
        """启动感知线程"""
        if self._thread and self._thread.is_alive():
            return
        
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="MediaPerception")
        self._thread.start()
        logger.info("MediaPerception started | interval=%.1fs", self._poll_interval)
    
    def stop(self):
        """停止感知线程"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("MediaPerception stopped")
    
    def _run(self):
        """感知主循环"""
        while not self._stop_event.is_set():
            try:
                info = _read_smtc()
                
                if info:
                    self._current = info
                    # 变化检测
                    if self._last_info != info:
                        self._last_info = info
                        if self._on_media_change:
                            event = MediaEvent(
                                title=info.title,
                                artist=info.artist,
                                album=info.album,
                                duration=info.duration,
                                state=info.state,
                                timestamp=time.time(),
                            )
                            self._on_media_change(event)
                else:
                    self._current = None
                    self._last_info = None
                
            except Exception as e:
                # 失败 = 媒体感知本轮停摆；轮询线程不能死，但也不能无声地一直失败
                _seen = getattr(self, "_poll_err_reported", None)
                if _seen is None:
                    _seen = self._poll_err_reported = set()
                key = str(e)
                if key not in _seen:
                    _seen.add(key)
                    logger.warning("MediaPerception 轮询异常（同类后续静音）: %s", e)
            
            self._stop_event.wait(self._poll_interval)
    
    def get_current(self) -> Optional[MediaInfo]:
        """获取当前媒体信息"""
        return self._current
    
    def is_playing(self) -> bool:
        """是否正在播放"""
        return self._current is not None and self._current.state == "playing"


# ════════════════════════════════════════════════════════════
#  导出
# ════════════════════════════════════════════════════════════

__all__ = [
    "MediaPerception",
    "MediaEvent",
    "MediaInfo",
]
