"""环境变量配置 - 从 .env 读取 API 凭据

优先级：.env 文件 > Hanako provider-catalog.json > 默认值

.env 格式：
  LLM_BASE_URL=https://api.openai.com/v1
  LLM_API_KEY=sk-...
  LLM_MODEL=gpt-4o-mini
  TTS_BASE_URL=...
  TTS_API_KEY=...
  ...
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_PATH = Path(__file__).parent / ".env"


def _load_env(force: bool = False):
    """读取 .env 文件到 os.environ
    
    Args:
        force: 是否强制重新加载（覆盖已有值）
    """
    if not ENV_PATH.exists():
        return
    try:
        for line in ENV_PATH.read_text("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key:
                # 强制模式或值不存在时才更新
                if force or key not in os.environ:
                    os.environ[key] = value
    except Exception as e:
        logger.warning("Failed to load .env: %s", e)


# 启动时加载
_load_env()


def get_llm_config() -> dict:
    """获取 LLM 配置 - .env 优先，回退到 Hanako
    
    每次调用都重新读取 .env 文件，确保配置实时更新。

    Returns:
        {"base_url": ..., "api_key": ..., "model": ...}
        如果 .env 没配则返回空 dict（调用方用 Hanako 的）
    """
    # 每次都重新读取 .env 文件
    _load_env(force=True)
    
    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()

    if base_url and api_key:
        return {"base_url": base_url, "api_key": api_key, "model": model or "agnes-2.0-flash"}
    return {}  # 空 = 用 Hanako 默认


def get_tts_api_config() -> dict:
    """获取 TTS API 配置
    
    每次调用都重新读取 .env 文件，确保配置实时更新。
    """
    _load_env(force=True)
    return {
        "base_url": os.environ.get("TTS_BASE_URL", "").strip(),
        "api_key": os.environ.get("TTS_API_KEY", "").strip(),
        "model": os.environ.get("TTS_MODEL", "tts-1").strip(),
        "voice": os.environ.get("TTS_VOICE", "alloy").strip(),
    }


def get_asr_api_config() -> dict:
    """获取 ASR API 配置
    
    每次调用都重新读取 .env 文件，确保配置实时更新。
    """
    _load_env(force=True)
    return {
        "base_url": os.environ.get("ASR_BASE_URL", "").strip(),
        "api_key": os.environ.get("ASR_API_KEY", "").strip(),
        "model": os.environ.get("ASR_MODEL", "whisper-1").strip(),
    }


def get_vision_config() -> dict:
    """获取视觉模型配置（屏幕感知专用）
    
    每次调用都重新读取 .env 文件，确保配置实时更新。
    优先使用视觉专用配置，回退到 LLM 配置。
    
    Returns:
        {"base_url": ..., "api_key": ..., "model": ...}
        如果没有配置则返回空 dict
    """
    _load_env(force=True)
    base_url = os.environ.get("VISION_BASE_URL", "").strip()
    api_key = os.environ.get("VISION_API_KEY", "").strip()
    model = os.environ.get("VISION_MODEL", "").strip()
    
    if base_url and api_key:
        return {"base_url": base_url, "api_key": api_key, "model": model}
    return {}
