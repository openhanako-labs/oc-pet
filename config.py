"""配置管理"""
import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULT_CONFIG = {
    "api": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat"
    },
    "character": "yuexiye",     # yuexiye | ophelia
    "window": {
        "width": 200,
        "height": 300,
        "x": -1,                 # -1 = auto-center
        "y": -1
    }
}

# 角色基本信息
# prompt（角色设定/说话方式）现在由 Skills 系统管理，
# 见 skills/public/<character_id>/SKILL.md 为单一数据源。
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


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # merge with defaults for any missing keys
        merged = DEFAULT_CONFIG.copy()
        merged.update(cfg)
        return merged
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
