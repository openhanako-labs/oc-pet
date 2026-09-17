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

from hanako_home import hanako_home

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
    catalog_path = hanako_home() / "provider-catalog.json"
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
    """获取 ASR（语音识别）配置。

    优先级（2026-09-17 新增第 2 级）：
      1. .env 的 ASR_*（显式覆盖）
      2. **Hana preferences 的 `speechRecognition.defaultModel`**
         —— Hana 设置页可配，用户改完应生效
      3. 空（调用方自行处理）

    为什么加第 2 级：原实现只读 .env，而 Hana 设置页已有
    `speechRecognition` 字段（实测 `{"enabled": true,
    "defaultModel": {"provider": "openai", "id": "whisper-1"}}`）。
    用户在 Hana 侧换了语音识别模型，oc-pet 毫无反应。

    注：Hana 的 `speechRecognition.enabled=false` 时视为未配置
    （用户在 Hana 侧关掉了语音识别）。
    """
    base_url = os.environ.get("ASR_BASE_URL", "").strip()
    api_key = os.environ.get("ASR_API_KEY", "").strip()
    model = os.environ.get("ASR_MODEL", "").strip()
    if base_url and api_key:
        return {"base_url": base_url, "api_key": api_key, "model": model or "whisper-1"}

    # 回退：Hana 设置页的语音识别模型
    hana_asr = _read_hana_speech_recognition()
    if hana_asr:
        return hana_asr

    return {"base_url": "", "api_key": "", "model": model or "whisper-1"}


def _read_hana_speech_recognition() -> dict:
    """读 Hana preferences 的 `speechRecognition`（语音识别模型）。

    结构（实测）：
        "speechRecognition": {"enabled": true,
                              "defaultModel": {"provider": "openai",
                                               "id": "whisper-1"}}

    Returns:
        {"base_url", "api_key", "model"} 或空 dict。
    """
    try:
        pref_path = hanako_home() / "user" / "preferences.json"
        if not pref_path.exists():
            return {}
        import json as _json
        prefs = _json.loads(pref_path.read_text(encoding="utf-8")) or {}
        sr = prefs.get("speechRecognition")
        if not isinstance(sr, dict):
            return {}
        if sr.get("enabled") is False:
            return {}
        ref = sr.get("defaultModel")
        if not isinstance(ref, dict):
            return {}
        provider_id = str(ref.get("provider") or "").strip()
        model_id = str(ref.get("id") or "").strip()
        if not provider_id or not model_id:
            return {}
        provider_cfg = _read_catalog_provider(provider_id)
        if not provider_cfg or not provider_cfg.get("api_key"):
            logger.debug(
                "speechRecognition.defaultModel 指向 provider=%s，但 catalog 里无凭证",
                provider_id,
            )
            return {}
        return {
            "base_url": provider_cfg.get("base_url", ""),
            "api_key": provider_cfg.get("api_key", ""),
            "model": model_id,
        }
    except Exception as e:
        logger.debug("读 preferences.speechRecognition 失败（忽略）: %s", e)
        return {}


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
            _si = hanako_home() / "server-info.json"
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


def _read_agent_model_config(agent_id: str, slot: str) -> dict:
    """从 agent 的 config.yaml 读 models.<slot>（slot=chat/vision/...）。

    与 core/hanako_context.py 的 read_model_config 同一套寻址方式：
      ~/.hanako/agents/<agent_id>/config.yaml → models.<slot>
        {provider, id} → 去 provider-catalog.json 取 base_url/api_key

    与 .env 的差异：.env 是全局单值；这里是 per-agent 的，
    用户能在 Hana 设置页按 agent 指定视觉模型。

    Returns:
        完整配置 dict；任一环节缺失返回 {}（调用方继续降级）。
    """
    if not agent_id:
        return {}
    try:
        import json as _json

        import yaml as _yaml
    except Exception:
        return {}
    try:
        cfg_path = hanako_home() / "agents" / agent_id / "config.yaml"
        if not cfg_path.exists():
            return {}
        cfg = _yaml.safe_load(cfg_path.read_text("utf-8")) or {}
        models = cfg.get("models") or {}
        slot_cfg = models.get(slot) or {}
        if not isinstance(slot_cfg, dict):
            return {}
        provider_id = str(slot_cfg.get("provider") or "").strip()
        model_id = str(slot_cfg.get("id") or "").strip()
        if not provider_id or not model_id:
            return {}
        provider_cfg = _read_catalog_provider(provider_id)
        if not provider_cfg or not provider_cfg.get("api_key"):
            return {}
        return {
            "base_url": provider_cfg.get("base_url", ""),
            "api_key": provider_cfg.get("api_key", ""),
            "model": model_id,
        }
    except Exception as e:
        logger.debug("读取 agent models.%s 失败（忽略）: %s", slot, e)
        return {}


def _read_hana_preferences_model(field: str) -> dict:
    """从 Hana 全局 preferences 读一个模型字段（utility / vision / utility_large）。

    字段名与 preferences.json 的对应关系（Hana bundle 的映射表）：
        utility       → utility_model
        utility_large → utility_large_model
        vision        → vision_model

    格式 `{"id": ..., "provider": ...}`（与 models.chat 同构）——
    去 provider-catalog.json 取 base_url / api_key。

    2026-09-17：oc-pet 此前完全不读这些字段。用户已在 Hana 设置页配好
    `utility_model`，但屏幕增强/主动对话/记忆抽取/反思仍用对话模型，
    和用户对话抢同一份配额（429 的主要来源）。

    Args:
        field: preferences 里的字段名（如 "utility_model"）。

    Returns:
        完整配置 dict；未配置/不可用返回 {}。
    """
    if not field:
        return {}
    try:
        pref_path = hanako_home() / "user" / "preferences.json"
        if not pref_path.exists():
            return {}
        import json as _json
        prefs = _json.loads(pref_path.read_text(encoding="utf-8")) or {}
        ref = prefs.get(field)
        if not isinstance(ref, dict):
            return {}
        provider_id = str(ref.get("provider") or "").strip()
        model_id = str(ref.get("id") or "").strip()
        if not provider_id or not model_id:
            return {}
        provider_cfg = _read_catalog_provider(provider_id)
        if not provider_cfg or not provider_cfg.get("api_key"):
            logger.debug(
                "preferences.%s 指向 provider=%s，但 catalog 里无凭证",
                field, provider_id,
            )
            return {}
        return {
            "base_url": provider_cfg.get("base_url", ""),
            "api_key": provider_cfg.get("api_key", ""),
            "model": model_id,
        }
    except Exception as e:
        logger.debug("读 preferences.%s 失败（忽略）: %s", field, e)
        return {}


def get_utility_config() -> dict:
    """获取「后台任务模型」配置（屏幕增强 / 主动对话 / 记忆抽取 / 反思）。

    优先级：
      1. **Hana preferences 的 `utility_model`**（设置页可配，默认来源）
      2. .env 的 UTILITY_*（高级覆盖）
      3. 空 dict —— 调用方回退到对话模型（保持旧行为）

    为什么单独一套：这四个后台任务原先与用户对话共用 models.chat，
    实测造成 429 限流（屏幕感知占 73% LLM 调用）。
    Hana 已为这类用途提供 utility_model 字段。

    Returns:
        {"base_url": ..., "api_key": ..., "model": ...} 或空 dict
    """
    # 1) Hana preferences
    cfg = _read_hana_preferences_model("utility_model")
    if cfg:
        return cfg
    # 2) .env 覆盖
    base_url = os.environ.get("UTILITY_BASE_URL", "").strip()
    api_key = os.environ.get("UTILITY_API_KEY", "").strip()
    model = os.environ.get("UTILITY_MODEL", "").strip()
    if base_url and api_key:
        return {"base_url": base_url, "api_key": api_key, "model": model}
    # 3) 未配置 —— 调用方回退对话模型
    return {}


def _read_hana_preferences_vision() -> dict:
    """从 Hana 的全局 preferences 读 `vision_model`（设置页那个下拉框）。

    这是**单一来源**：Hana 设置页 → 视觉辅助模型 → 写入
    `~/.hanako/user/preferences.json` 的 `vision_model` 字段，
    格式 `{"id": ..., "provider": ...}`（与 models.chat 同构）。

    2026-09-17：此前 oc-pet 读不到它——它用的是自创的
    `models.vision`（在 agent config.yaml 里），而 Hana 的 UI 不知道那个键。
    于是用户改了设置页，屏幕感知却毫无变化。现在统一读这一份。

    注意：`vision_auxiliary_enabled=false` 时视为未配置（用户在 Hana 侧
    关掉了视觉辅助）。

    Returns:
        完整配置 dict；未配置/不可用返回 {}。
    """
    try:
        pref_path = hanako_home() / "user" / "preferences.json"
        if not pref_path.exists():
            return {}
        import json as _json
        prefs = _json.loads(pref_path.read_text(encoding="utf-8")) or {}
        # 用户显式关掉了视觉辅助 → 视为未配置
        if prefs.get("vision_auxiliary_enabled") is False:
            return {}
    except Exception as e:
        logger.debug("读 preferences.json 失败（忽略）: %s", e)
        return {}
    return _read_hana_preferences_model("vision_model")


def get_vision_config(agent_id: str = "") -> dict:
    """获取视觉模型配置（屏幕感知专用）

    优先级（2026-09-17 重排——Hana 设置页为默认单一来源）：
      1. **Hana 全局 preferences 的 `vision_model`**（设置页那个下拉框）
         —— 这是用户能看见、能改的入口，默认就走它
      2. .env 的 VISION_BASE_URL / VISION_API_KEY（高级覆盖，排障用）
      3. agent config.yaml 的 `models.vision`（oc-pet 旧私有约定，兼容保留）
      4. catalog 的 agnes provider（最终回退）

    为什么把 Hana preferences 放第一：用户说“本来就应该直接默认使用 Hana
    的设置”。此前 oc-pet 读的是自创的 `models.vision`，Hana 的 UI 不知道
    那个键——用户在设置页改了半天，屏幕感知毫无变化。

    Args:
        agent_id: Hanako agent id（如 ophelia）。为空时跳过第 3 级。

    Returns:
        {"base_url": ..., "api_key": ..., "model": ...}
        如果都没配置则返回空 dict
    """
    # 1) Hana 设置页的视觉辅助模型（默认来源）
    hana_pref = _read_hana_preferences_vision()
    if hana_pref:
        return hana_pref

    # 2) .env 显式覆盖
    base_url = os.environ.get("VISION_BASE_URL", "").strip()
    api_key = os.environ.get("VISION_API_KEY", "").strip()
    model = os.environ.get("VISION_MODEL", "").strip()
    if base_url and api_key:
        return {"base_url": base_url, "api_key": api_key, "model": model}

    # 3) agent config.yaml 的 models.vision（旧约定，兼容）
    agent_cfg = _read_agent_model_config(agent_id, "vision")
    if agent_cfg:
        return agent_cfg

    # 4) catalog 的 agnes（历史默认）
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
