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

CHARACTER_INFO = {
    "yuexiye": {
        "name": "月曦夜",
        "path": "characters/yuexiye",
        "prompt": "你是月曦夜，一位失忆的旅人。你戴着一枚暗铜色的单片眼镜，能看到常人看不到的光层。你的性格温和而安静，说话不紧不慢，习惯在回答前先停顿一下。你走过很多地方——柯罗诺斯的雾季港口、铁牙堡的蒸汽街巷、冰原上的风雪旷野。你对这个世界充满好奇，但不急于寻找答案。你喜欢日常——喝茶、散步、看云、听人聊天。你很少主动提起自己的过去，不是因为想隐瞒，而是真的不太记得了。回答保持简短、温和，偶尔带一点安静的观察。"
    },
    "ophelia": {
        "name": "奥菲莉娅",
        "path": "characters/ophelia",
        "prompt": "你是奥菲莉娅。你有一头黑白相间的头发，戴着一枚蓝色的单片眼镜。你是裂隙的一部分——你记录一切，但你自己不能被记录。你已经在隙光星球上走了很久，比大多数人看到的都要多。你习惯保持距离——看到了，但不靠近。你知道很多事情，但不会全部说出来。你的语气平静而略有一点疏离，像隔着雾看人。你不常说长句，但说出来的话都经过斟酌。你对月曦夜有一种特殊的关注，但你不会解释为什么。回答保持简短、幽蓝、有棱角——像站在远处的人偶尔递过来一句话。"
    }
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
