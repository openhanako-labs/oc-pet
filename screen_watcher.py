"""实时视觉 - 定时截屏 + agnes 视觉模型分析

每 N 秒截屏一次，发给 agnes-2.0-flash 分析用户在做什么。
结果注入对话引擎的感知上下文。

用法:
    watcher = ScreenWatcher()
    watcher.start()  # 后台线程启动
    ctx = watcher.get_context()  # "用户正在用 VS Code 写代码"
"""
from __future__ import annotations

import base64
import io
import logging
import threading
import time

import requests
from PIL import ImageGrab

logger = logging.getLogger(__name__)

# ── 配置 ───────────────────────────────────────────────

DEFAULT_INTERVAL = 120  # 默认截屏间隔（秒）
SCREENSHOT_SCALE = 4    # 缩小倍率（减小 base64 体积）
JPEG_QUALITY = 50       # JPEG 压缩质量

# 视觉模型 API（从 Hanako context 动态读取）
from hanako_context import HanakoContext

VISION_PROMPT = "用一句话简短描述用户当前在屏幕上做什么（不超过20字）"


class ScreenWatcher:
    """屏幕感知 - 后台定时截屏 + 视觉分析

    随 pet 进程启停。结果通过 get_context() 读取。
    """

    def __init__(self, interval: int = DEFAULT_INTERVAL):
        self._interval = interval
        self._running = False
        self._thread = None
        self._last_description: str = ""
        self._last_screenshot_time: float = 0
        self._lock = threading.Lock()

        # 回调
        self.on_update: callable = lambda desc: None

    @property
    def last_description(self) -> str:
        with self._lock:
            return self._last_description

    def get_context(self) -> str:
        """获取当前屏幕感知上下文"""
        with self._lock:
            if self._last_description:
                return f"[屏幕画面：{self._last_description}]"
        return ""

    def start(self):
        """启动后台截屏线程"""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("ScreenWatcher started | interval=%ds", self._interval)

    def stop(self):
        self._running = False

    def _run(self):
        """后台主循环"""
        # 首次延迟 10 秒（等 pet 窗口稳定）
        time.sleep(10)

        while self._running:
            try:
                self._capture_and_analyze()
            except Exception as e:
                logger.warning("ScreenWatcher error: %s", e)

            # 等待下一个周期
            for _ in range(self._interval):
                if not self._running:
                    return
                time.sleep(1)

    def _capture_and_analyze(self):
        """截屏 + 视觉分析"""
        # 1. 截屏
        img = ImageGrab.grab()
        # 缩小（减小体积）
        new_size = (img.width // SCREENSHOT_SCALE, img.height // SCREENSHOT_SCALE)
        img = img.resize(new_size)

        # 2. 转 base64 JPEG
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
        b64 = base64.b64encode(buf.getvalue()).decode()

        logger.info("Screenshot: %s, %dKB base64", new_size, len(b64) // 1024)

        # 3. 调视觉模型
        # 从 Hanako context 读取 API 配置
        ctx = HanakoContext()
        cfg = ctx.read_model_config()
        api_url = cfg.get("base_url", "") + "/chat/completions"
        api_key = cfg.get("api_key", "")
        model = cfg.get("model", "")

        if not api_url or not api_key:
            logger.warning("No API config for vision")
            return

        try:
            resp = requests.post(
                api_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": VISION_PROMPT},
                            {"type": "image_url",
                             "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        ]
                    }],
                    "max_tokens": 200,
                    "temperature": 0.3,
                },
                timeout=30,
            )

            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"].get("content", "")
                content = content.strip()
                if content:
                    with self._lock:
                        self._last_description = content
                        self._last_screenshot_time = time.time()
                    logger.info("Screen analysis: %s", content[:50])
                    self.on_update(content)
                else:
                    logger.warning("Vision API returned empty content")
            else:
                logger.warning("Vision API error: %d", resp.status_code)
        except requests.exceptions.Timeout:
            logger.warning("Vision API timeout")
        except Exception as e:
            logger.warning("Vision analysis failed: %s", e)
