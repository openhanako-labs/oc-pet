"""配置管理"""
import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULT_CONFIG = {
    "character": "ophelia",
    "window": {
        "width": 200,
        "height": 300,
        "x": -1,
        "y": -1
    }
}

CHARACTER_INFO = {
    "yuexiye": {
        "name": "月曦夜",
        "path": "characters/yuexiye",
    },
    "ophelia": {
        "name": "奥菲莉娅",
        "path": "characters/ophelia",
    },
}

# 情绪 → 帧动画序列映射
# oc-pet 靠帧序列名切换表情，对应 live2d-deskpet 的 expression 表
EXPRESSION_MAP = {
    "happy": "extra",       # 开心 → 额外帧（如笑、蹦跳）
    "sad": "idle",          # 悲伤 → 不做动画（或用特定帧）
    "angry": "extra",       # 生气 → 额外帧
    "surprised": "extra",   # 惊讶 → 额外帧
    "neutral": "idle",      # 中性 → 待机
    "thinking": "extra",    # 思考中
    "working": "extra",     # 工作中
    "listening": "idle",    # 倾听
    "speaking": "idle",     # 说话 → 用 idle + 气泡闪烁
}

# Hanako 状态 → 桌宠动作
HANAKO_STATE_MAP = {
    "listening": {"anim": "idle", "desc": "倾听"},
    "thinking": {"anim": "extra", "desc": "思考"},
    "working": {"anim": "extra", "desc": "工作"},
    "speaking": {"anim": "idle", "desc": "说话", "bubble_bright": True},
}

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        merged = DEFAULT_CONFIG.copy()
        merged.update(cfg)
        return merged
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
