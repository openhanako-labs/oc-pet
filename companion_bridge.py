"""桌宠桥接守护 — 自动连接 LLM，无需 Hanako Agent

功能：
  1. 监控 outbox.json 新消息
  2. 调用 harness_adapter 生成角色回复（含记忆注入）
  3. 写入 response.json（含情绪检测、动画建议）
  4. 可选 TTS 音频生成

启动：
  python companion_bridge.py

与 pet.py 的关系：
  - 独立进程，不依赖 Hanako Agent
  - pet.py 写入 outbox → bridge 读取 → LLM 回复 → 写入 response
  - pet.py 的 hanako_monitor 读取 response 并显示气泡
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness_adapter import HanakoPetAdapter
from perception import PerceptionController

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("companion_bridge")

# ── 路径 ───────────────────────────────────────────────

DATA_DIR = Path.home() / ".hanako" / "plugins" / "hanako-desktop-companion"
OUTBOX_FILE = DATA_DIR / "outbox.json"
PENDING_FLAG = DATA_DIR / ".pending"
RESPONSE_FILE = DATA_DIR / "response.json"

# 角色配置路径
SKILLS_DIR = Path(__file__).parent / "skills" / "public"


def detect_emotion(text: str) -> str:
    """从回复文本中检测情绪（简单关键词匹配）"""
    text_lower = text.lower()
    happy_kw = ["哈", "笑", "开心", "好耶", "嘻嘻", "嘿嘿", "可爱", "棒"]
    sad_kw = ["呜", "难过", "伤心", "哭", "叹气", "唉", "失落"]
    angry_kw = ["哼", "气", "怒", "可恶", "烦", "讨厌"]
    cute_kw = ["喵", "呐", "呢", "嘛", "啾", "贴贴", "蹭蹭"]

    scores = {"happy": 0, "sad": 0, "angry": 0, "cute": 0, "thinking": 0}
    for kw in happy_kw:
        if kw in text_lower:
            scores["happy"] += 1
    for kw in sad_kw:
        if kw in text_lower:
            scores["sad"] += 1
    for kw in angry_kw:
        if kw in text_lower:
            scores["angry"] += 1
    for kw in cute_kw:
        if kw in text_lower:
            scores["cute"] += 1

    max_score = max(scores.values())
    if max_score == 0:
        return "neutral"
    priority = ["angry", "sad", "cute", "happy", "thinking"]
    for em in priority:
        if scores[em] == max_score:
            return em
    return "neutral"


def map_emotion_to_anim(emotion: str) -> str:
    """情绪 → 动画序列"""
    return "extra" if emotion in ("happy", "angry", "surprised", "thinking") else "idle"


def main():
    # 加载配置
    config_path = Path(__file__).parent / "config.json"
    try:
        config = json.loads(config_path.read_text("utf-8"))
    except Exception as e:
        logger.error("无法读取 config.json: %s", e)
        sys.exit(1)

    # 注意：API 配置直接从 Hanako 本体文件读取
    # （harness_adapter.py 中的 HanakoPetAdapter 自动从 provider-catalog.json 获取）
    logger.info("API 配置将自动从 Hanako 上下文读取")

    # 初始化适配器（直接从 Hanako 本体读取配置）
    try:
        agent_id = config.get("character", "ophelia")
        adapter = HanakoPetAdapter(agent_id=agent_id)
        logger.info("适配器就绪 | agent=%s | model=%s", agent_id, adapter.model_config.get("model", "?"))
    except Exception as e:
        logger.error("适配器初始化失败: %s", e)
        sys.exit(1)

    # 初始化感知控制器
    perception = PerceptionController(agent_id)
    perception.tick_schedule()  # 首次刷新日程

    last_check = 0
    check_interval = 1.0  # 秒
    running = True

    logger.info("=" * 50)
    logger.info("桌宠桥接守护启动")
    logger.info("监控: %s", OUTBOX_FILE)
    logger.info("回复: %s", RESPONSE_FILE)
    logger.info("按 Ctrl+C 停止")
    logger.info("=" * 50)

    while running:
        try:
            now = time.time()
            if now - last_check < check_interval:
                time.sleep(0.1)
                continue
            last_check = now

            # 1. 检查待处理标记
            if not PENDING_FLAG.exists():
                continue

            # 2. 读取 outbox
            if not OUTBOX_FILE.exists():
                PENDING_FLAG.unlink(missing_ok=True)
                continue

            raw = OUTBOX_FILE.read_text("utf-8").strip()
            if not raw or raw in ("{}", "[]"):
                PENDING_FLAG.unlink(missing_ok=True)
                continue

            try:
                messages = json.loads(raw)
            except json.JSONDecodeError:
                PENDING_FLAG.unlink(missing_ok=True)
                continue

            if not isinstance(messages, list) or not messages:
                PENDING_FLAG.unlink(missing_ok=True)
                continue

            # 3. 取最新的消息
            msg = messages[-1]
            text = msg.get("text", "").strip()
            character = msg.get("character", config.get("character", "ophelia"))
            msg_type = msg.get("type", "")

            if not text:
                PENDING_FLAG.unlink(missing_ok=True)
                continue

            logger.info("收到消息 [%s]: %s", character, text[:50])

            # 4. 调用 LLM 生成回复（使用 Hanako 原生适配器 + 感知上下文）
            try:
                perception_ctx = perception.build_context()
                reply = adapter.chat(message=text, inject_memory=True, extra_context=perception_ctx)
                if not reply:
                    reply = "…"
                logger.info("生成回复: %s", reply[:60])
            except Exception as e:
                logger.error("LLM 调用失败: %s", e)
                reply = "…（信号不太好，你再说一遍？）"

            # 5. 情绪检测
            emotion = detect_emotion(reply)
            anim = map_emotion_to_anim(emotion)

            # 6. 写入 response.json
            payload = {
                "reply": reply,
                "character": character,
                "anim": anim,
                "emotion": emotion,
                "audioPath": "",
                "ts": time.time(),
                "status": "ok",
            }
            RESPONSE_FILE.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
            logger.info("已写入回复 [%s] anim=%s emotion=%s", character, anim, emotion)

            # 7. 清空 outbox（已处理）
            OUTBOX_FILE.write_text("[]", "utf-8")
            PENDING_FLAG.unlink(missing_ok=True)

        except KeyboardInterrupt:
            logger.info("收到中断信号，停止")
            running = False
        except Exception as e:
            logger.error("循环异常: %s", e)
            time.sleep(1)

    logger.info("桥接守护已停止")


if __name__ == "__main__":
    main()