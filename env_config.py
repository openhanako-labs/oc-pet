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


def _read_catalog_provider(provider_id: str) -> dict:
    """从 Hanako provider-catalog.json 读取指定 provider 配置"""
    catalog_path = Path.home() / ".hanako" / "provider-catalog.json"
    if not catalog_path.exists():
        return {}
    try:
        import json
        data = json.loads(catalog_path.read_text("utf-8"))
        providers = data.get("providers", {})
        return providers.get(provider_id, {})
    except Exception:
        return {}


def get_llm_config() -> dict:
    """获取 LLM 配置 - .env 优先，回退到 Hanako

    Returns:
        {"base_url": ..., "api_key": ..., "model": ...}
        如果 .env 没配则返回空 dict（调用方用 Hanako 的）
    """
    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()

    if base_url and api_key:
        return {"base_url": base_url, "api_key": api_key, "model": model or "agnes-2.0-flash"}
    return {}  # 空 = 用 Hanako 默认


def get_tts_api_config() -> dict:
    """获取 TTS API 配置 — .env 优先，回退到 Hanako catalog"""
    base_url = os.environ.get("TTS_BASE_URL", "").strip()
    api_key = os.environ.get("TTS_API_KEY", "").strip()
    model = os.environ.get("TTS_MODEL", "").strip()
    voice = os.environ.get("TTS_VOICE", "").strip()

    if base_url and api_key:
        return {
            "base_url": base_url,
            "api_key": api_key,
            "model": model or "mimo-v2.5-tts",
            "voice": voice or "冰糖",
        }

    # 回退：从 Hanako provider-catalog 读 mimo-token-plan
    catalog_cfg = _read_catalog_provider("mimo-token-plan")
    if catalog_cfg:
        return {
            "base_url": catalog_cfg.get("base_url", ""),
            "api_key": catalog_cfg.get("api_key", ""),
            "model": "mimo-v2.5-tts",
            "voice": voice or "冰糖",
        }
    return {"base_url": "", "api_key": "", "model": "", "voice": ""}


def get_asr_api_config() -> dict:
    """获取 ASR API 配置"""
    return {
        "base_url": os.environ.get("ASR_BASE_URL", "").strip(),
        "api_key": os.environ.get("ASR_API_KEY", "").strip(),
        "model": os.environ.get("ASR_MODEL", "whisper-1").strip(),
    }


def get_vision_config() -> dict:
    """获取视觉模型配置（屏幕感知专用）

    优先使用视觉专用配置，回退到 Hanako catalog 的 agnes provider。

    Returns:
        {"base_url": ..., "api_key": ..., "model": ...}
        如果没有配置则返回空 dict
    """
    base_url = os.environ.get("VISION_BASE_URL", "").strip()
    api_key = os.environ.get("VISION_API_KEY", "").strip()
    model = os.environ.get("VISION_MODEL", "").strip()

    if base_url and api_key:
        return {"base_url": base_url, "api_key": api_key, "model": model}

    # 回退：从 Hanako catalog 读 agnes
    catalog_cfg = _read_catalog_provider("agnes")
    if catalog_cfg and catalog_cfg.get("api_key"):
        models = catalog_cfg.get("models", [])
        vision_model = "agnes-2.0-flash"
        for m in models:
            if isinstance(m, dict) and m.get("video"):
                vision_model = m.get("id", vision_model)
                break
        return {
            "base_url": catalog_cfg["base_url"],
            "api_key": catalog_cfg["api_key"],
            "model": model or vision_model,
        }
    return {}


def save_env(
    llm_provider: str = "",
    llm_base_url: str = "",
    llm_api_key: str = "",
    llm_model: str = "",
    tts_base_url: str = "",
    tts_api_key: str = "",
    tts_model: str = "",
    tts_voice: str = "",
    asr_base_url: str = "",
    asr_api_key: str = "",
    asr_model: str = "",
    vision_base_url: str = "",
    vision_api_key: str = "",
    vision_model: str = "",
):
    """保存 API 配置到 .env 文件"""
    lines = []
    
    # LLM
    if llm_provider:
        lines.append(f"LLM_PROVIDER={llm_provider}")
    if llm_base_url:
        lines.append(f"LLM_BASE_URL={llm_base_url}")
    if llm_api_key:
        lines.append(f"LLM_API_KEY={llm_api_key}")
    if llm_model:
        lines.append(f"LLM_MODEL={llm_model}")
    
    # TTS
    if tts_base_url:
        lines.append(f"TTS_BASE_URL={tts_base_url}")
    if tts_api_key:
        lines.append(f"TTS_API_KEY={tts_api_key}")
    if tts_model:
        lines.append(f"TTS_MODEL={tts_model}")
    if tts_voice:
        lines.append(f"TTS_VOICE={tts_voice}")
    
    # ASR
    if asr_base_url:
        lines.append(f"ASR_BASE_URL={asr_base_url}")
    if asr_api_key:
        lines.append(f"ASR_API_KEY={asr_api_key}")
    if asr_model:
        lines.append(f"ASR_MODEL={asr_model}")
    
    # Vision
    if vision_base_url:
        lines.append(f"VISION_BASE_URL={vision_base_url}")
    if vision_api_key:
        lines.append(f"VISION_API_KEY={vision_api_key}")
    if vision_model:
        lines.append(f"VISION_MODEL={vision_model}")
    
    # 写入文件
    try:
        ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("Saved .env config")
        # 重新加载
        _load_env(force=True)
    except Exception as e:
        logger.error("Failed to save .env: %s", e)
