"""盯住 config.json 的改动，改了就回调——"改配置不用重启"。

## 为什么非要它

``a2a`` 有设置页，保存时会主动重载；但 ``game`` / ``lip_sync`` / ``llm_gate``
**没有设置页**，只能手改 ``config.json``。"在设置保存时重载"这条路对它们
**根本不会触发**——所以光把那几个子块接到保存路径上等于写**死代码**。

（2026-09-19 先查了 ``ui/settings_dialog.py``：那三个键在面板里一行都没有。
所以没直接接保存路径，而是补了这条"文件变了就生效"的路径。）

## 纪律

- 只**读**文件，绝不写（写盘归 ``config.py``）。
- 读/解析失败（文件正被写、暂时半截）→ **当没变**，下次再看，不抛异常；
  也**绝不**把"读不到"当成"配置变成空"（那会把功能全卸掉）。
- 按**内容**去重，不按 mtime：mtime 会被与本键无关的写盘碰开，
  而"内容没变却重建对象"是有代价的——重建闸门会清掉已用配额与冷却，
  重建派活会丢掉还没念的结论。
- path / clock / loader 全可注入 → 能单测，不需要 Qt、不需要真文件。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 3.0


def config_signature(block: Any) -> str:
    """把配置子块压成一个稳定的字符串指纹（键序无关）。"""
    try:
        return json.dumps(block if block is not None else {}, sort_keys=True,
                          ensure_ascii=False, default=str)
    except Exception:
        return repr(block)


def load_json_file(path) -> Optional[dict]:
    """读 json；失败返回 ``None``（**不代表"配置空了"**，代表"这次没读到"）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


class ConfigWatcher:
    """按间隔检查 config.json 内容，**真变了**才回调 ``on_change(cfg)``。

    用法::

        w = ConfigWatcher(CONFIG_PATH, on_change=pet._on_config_file_changed)
        w.prime()                                  # 记当前内容为基线
        QTimer(...).timeout.connect(w.poll)         # 定时 poll
    """

    def __init__(self, path, on_change: Callable[[dict], None],
                 loader: Optional[Callable] = None,
                 interval_s: float = DEFAULT_INTERVAL_S,
                 clock: Callable[[], float] = time.monotonic):
        self._path = Path(path)
        self._on_change = on_change
        self._loader = loader or load_json_file
        self._interval = max(0.2, float(interval_s))
        self._clock = clock
        self._last_check = -1e9
        self._baseline: Optional[str] = None
        self.changes = 0

    @property
    def interval_s(self) -> float:
        return self._interval

    def prime(self) -> None:
        """把**当前磁盘内容**记为基线（启动时调一次，避免开箱即误触发）。"""
        data = self._loader(self._path)
        self._baseline = None if data is None else config_signature(data)
        self._last_check = self._clock()

    def poll(self) -> bool:
        """到点才查；内容真变 → 回调并返回 True。任何异常都不外抛。"""
        try:
            now = self._clock()
            # 未取到基线（文件当时读不到）时不受间隔限制，尽快补上
            if self._baseline is not None and now - self._last_check < self._interval:
                return False
            self._last_check = now
            data = self._loader(self._path)
            if data is None:
                return False
            sig = config_signature(data)
            if self._baseline is None:
                self._baseline = sig          # 首次读到内容 = 记基线，不触发
                return False
            if sig == self._baseline:
                return False
            self._baseline = sig
            self.changes += 1
            self._on_change(data)
            return True
        except Exception as e:  # noqa: BLE001 — 监视器绝不能拖垮主循环
            logger.warning("config 监视失败（忽略本次）: %s", e)
            return False
