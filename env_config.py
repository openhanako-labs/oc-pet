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
import tempfile
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


# ── Hanako WebSocket 客户端配置 ──────────────────────────────

def get_hanako_config() -> dict:
    """读取 Hanako WS 客户端配置

    Returns:
        {
            "base_url": str,
            "api_token": str,
            "transport_mode": "direct" | "prefer_hanako" | "hanako_only",
            "reply_timeout": int (秒),
            "mirror_external_replies": bool,
        }
    """
    base_url = os.environ.get("HANAKO_BASE_URL", "http://127.0.0.1:20099").strip()
    api_token = os.environ.get("HANAKO_API_TOKEN", "").strip()
    # 自动从 server-info.json 读取 token（如果环境变量为空）
    if not api_token:
        try:
            _si = Path.home() / ".hanako" / "server-info.json"
            if _si.exists():
                import json as _json
                api_token = _json.loads(_si.read_text("utf-8")).get("token", "")
        except Exception:
            logger.debug("env_config: 非致命异常(已静默吞掉)", exc_info=True)
    transport_mode = os.environ.get("HANAKO_TRANSPORT_MODE", "prefer_hanako").strip().lower()
    if transport_mode not in ("direct", "prefer_hanako", "hanako_only"):
        logger.warning("Unknown HANAKO_TRANSPORT_MODE=%s, fallback to prefer_hanako", transport_mode)
        transport_mode = "prefer_hanako"
    try:
        reply_timeout = int(os.environ.get("HANAKO_REPLY_TIMEOUT", "180").strip())
    except ValueError:
        reply_timeout = 180
    mirror_external_replies = os.environ.get(
        "HANAKO_MIRROR_EXTERNAL_REPLIES", "true"
    ).strip().lower() in ("1", "true", "yes", "on")

    return {
        "base_url": base_url,
        "api_token": api_token,
        "transport_mode": transport_mode,
        "reply_timeout": reply_timeout,
        "mirror_external_replies": mirror_external_replies,
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
        # 屏幕感知需要视觉理解模型，不是视频生成模型。选型优先级：
        #   1. 显式标 vision=True 的模型
        #   2. chat 多模态模型（如 agnes-2.5-flash，1M context，支持图像输入）
        #   3. 兜底 agnes-2.0-flash
        vision_model = "agnes-2.0-flash"
        vision_flagged = ""
        chat_fallback = ""
        for m in models:
            if not isinstance(m, dict):
                continue
            mid = m.get("id", "")
            if m.get("vision"):
                vision_flagged = mid
            elif (
                not chat_fallback
                and "flash" in mid
                and m.get("context", 0) >= 100000
            ):
                # chat 多模态（大 context 的 flash 模型基本都支持图像输入）
                chat_fallback = mid
        if vision_flagged:
            vision_model = vision_flagged
        elif chat_fallback:
            vision_model = chat_fallback
        return {
            "base_url": catalog_cfg["base_url"],
            "api_key": catalog_cfg["api_key"],
            "model": model or vision_model,
        }
    return {}


def update_env(updates: dict[str, str]) -> None:
    """合并式更新 .env：保留未知键/注释/空行，仅更新或追加 updates 中的键。

    原实现（save_env / settings_dialog._save_env）整文件覆写，只写对话框已知字段，
    会丢掉 HANAKO_BASE_URL / HANAKO_API_TOKEN / HANAKO_TRANSPORT_MODE /
    PHONE_RECEIVER_PORT / PHONE_AUTH_TOKEN / OC_PET_COSYVOICE_DIR /
    FRAMEBAKER_PATH 等所有未知键。PHONE_AUTH_TOKEN 丢失会让 phone_receiver
    空 token 直接放行（认证降级）；OC_PET_COSYVOICE_DIR 丢失会让 cosyvoice
    回退硬编码路径。本函数改为：读原文件逐行保留，仅替换 updates 中的键，
    新键追加到末尾，最后原子写回（tempfile + os.replace）。

    Args:
        updates: {KEY: value} 映射。value 为空字符串也照写（允许清空字段）。
    """
    lines: list[str] = []
    if ENV_PATH.exists():
        try:
            lines = ENV_PATH.read_text("utf-8").splitlines()
        except Exception as e:
            logger.warning("读取 .env 失败（将按新配置重建）: %s", e)
            lines = []

    updated_keys = set(updates)
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)  # 注释/空行/非法行原样保留
            continue
        key = stripped.partition("=")[0].strip()
        if key in updated_keys:
            if key not in seen:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
            # 重复键：旧值行丢弃（保留第一个新值位置）
        else:
            out.append(line)  # 未知键原样保留

    # 追加尚未出现的更新键（新字段）
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")

    try:
        ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(ENV_PATH.parent), suffix=".env.tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write("\n".join(out) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, ENV_PATH)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                logger.debug("env_config: 非致命异常(已静默吞掉)", exc_info=True)
            raise
        logger.info("Saved .env config (%d keys updated)", len(updates))
        # 重新加载
        _load_env(force=True)
    except Exception as e:
        logger.error("Failed to save .env: %s", e)


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
    """保存 API 配置到 .env 文件（合并式：保留未知键，仅更新已知键）"""
    updates: dict[str, str] = {}

    # LLM
    if llm_provider:
        updates["LLM_PROVIDER"] = llm_provider
    if llm_base_url:
        updates["LLM_BASE_URL"] = llm_base_url
    if llm_api_key:
        updates["LLM_API_KEY"] = llm_api_key
    if llm_model:
        updates["LLM_MODEL"] = llm_model

    # TTS
    if tts_base_url:
        updates["TTS_BASE_URL"] = tts_base_url
    if tts_api_key:
        updates["TTS_API_KEY"] = tts_api_key
    if tts_model:
        updates["TTS_MODEL"] = tts_model
    if tts_voice:
        updates["TTS_VOICE"] = tts_voice

    # ASR
    if asr_base_url:
        updates["ASR_BASE_URL"] = asr_base_url
    if asr_api_key:
        updates["ASR_API_KEY"] = asr_api_key
    if asr_model:
        updates["ASR_MODEL"] = asr_model

    # Vision
    if vision_base_url:
        updates["VISION_BASE_URL"] = vision_base_url
    if vision_api_key:
        updates["VISION_API_KEY"] = vision_api_key
    if vision_model:
        updates["VISION_MODEL"] = vision_model

    update_env(updates)
