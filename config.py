"""配置管理"""
import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULT_CONFIG = {
    "character": "ophelia",
    "scale": 1.0,
    "opacity": 1.0,
    "behavior": "normal",
    "window": {
        "width": 200,
        "height": 300,
        "x": -1,
        "y": -1
    },
    "break_reminder": {
        "enabled": True,
        "idle_minutes": 15,
        "gradual": True,
        "cooldown_minutes": 30
    },
    "action_linker": {
        "enabled": True,
        "highlight_duration": 30
    },
    "tts": {
        "enabled": True,
        "volume": 0.8
    },
    "proactive": {
        "enabled": True,
        "cooldown_minutes": 10,
        "rules": [
            {
                "idle_min": 5,
                "foreground": ["writing", "development", "browsing"],
                "prompt": "写了这么久，休息一下吧？",
                "weight": 0.7
            },
            {
                "idle_min": 15,
                "foreground": ["gaming", "entertainment"],
                "prompt": "带我一起玩嘛～",
                "weight": 0.5
            },
            {
                "idle_min": 30,
                "foreground": ["communication"],
                "prompt": "还在忙吗？想和你说说话～",
                "weight": 0.3
            },
            {
                "idle_min": 60,
                "foreground": ["*"],
                "prompt": "好安静啊……你在做什么呢？",
                "weight": 0.3
            }
        ]
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

# 情绪 → 帧动画序列映射（P3 连续参数：帧区间）
# oc-pet 靠帧序列名切换表情，P3 增强后不同情绪映射到同一序列的不同帧子范围
# 格式: 情绪名 -> (序列名, 起始帧索引, 结束帧索引)
#          起始/结束为 None 时使用全序列
EXPRESSION_MAP = {
    "happy":      ("extra", 0, 1),   # 开心 -> extra[0..1]
    "angry":      ("extra", 2, 3),   # 生气 -> extra[2..3]
    "surprised":  ("extra", 4, 5),   # 惊讶 -> extra[4..5]
    "thinking":   ("extra", 6, 7),   # 思考中 -> extra[6..7]
    "working":    ("extra", 6, 7),   # 工作中 -> extra[6..7]
    "neutral":    ("idle",  None, None),  # 中性 -> idle 全序列
    "sad":        ("idle",  None, None),  # 悲伤 -> idle 全序列
    "listening":  ("idle",  None, None),  # 倾听 -> idle 全序列
    "speaking":   ("idle",  None, None),  # 说话 -> idle 全序列
}

# Hanako 状态 → 桌宠动作
HANAKO_STATE_MAP = {
    "listening": {"anim": "idle", "desc": "倾听"},
    "thinking": {"anim": "extra", "desc": "思考"},
    "working": {"anim": "extra", "desc": "工作"},
    "speaking": {"anim": "idle", "desc": "说话", "bubble_bright": True},
}

def load_config():
    """加载配置，深度合并默认值（确保新增字段不丢失）"""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        merged = _deep_merge(DEFAULT_CONFIG.copy(), cfg)
        return merged
    return DEFAULT_CONFIG.copy()


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并：override 的键覆盖 base，但 base 独有的键保留"""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result

def save_config(cfg):
    """原子写入配置文件"""
    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(CONFIG_PATH), suffix='.tmp')
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, CONFIG_PATH)  # 原子替换
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
