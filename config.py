"""配置管理"""
import json
import os
import threading
import time

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULT_CONFIG = {
    "character": "",  # 默认角色已取消（2026-09-04）；首次启动由引导流程选择
    "dialog": {
        "agent_id": "",  # 对话后端绑定的 Hanako agent（空=未绑定，首次启动引导选择；不硬编码默认）
        # 每个桌宠实例可自定义绑定；多桌宠各自独立
    },
    "session_memory_enabled": False,  # 2026-09-07: 新开对话是否保存记忆（默认关闭，避免污染主会话）
    "scale": 1.0,
    "opacity": 1.0,
    "behavior": "normal",
    "theme_mode": "auto",  # "auto" | "light" | "dark" — 主题模式（桌宠主题系统）
    "window": {
        "width": 458,
        "height": 520,
        "x": -1,
        "y": -1
    },
    "break_reminder": {
        "enabled": True,
        "idle_minutes": 15,
        "gradual": True,
        "cooldown_minutes": 30
    },
    # P2-5 休息提醒升级：联动专注模式（focus）的连续工作计时。
    # 连续工作超 after_minutes（默认 90）且非深夜（<22 点）时提醒休息；
    # 深夜（>=22 点或 <6 点）阈值 ×late_night_multiplier 降频。
    "work_reminder": {
        "enabled": True,
        "after_minutes": 90,
        "late_night_hour": 22,
        "late_night_end_hour": 6,
        "late_night_multiplier": 3.0,
        "cooldown_minutes": 60,
        "snooze_minutes": 10,
        "tts_enabled": False
    },
    "action_linker": {
        "enabled": True,
        "highlight_duration": 30
    },
    "asr": {
        "provider": "whisper_local",
        "backend": "faster_whisper",
        "model": "small",
        "language": "zh",
        "device": ""
    },
    "tts": {
        "enabled": True,
        "volume": 0.8,
        # edge 引擎可选项：默认晓晓，可在设置面板切换
        "edge_voice": "zh-CN-XiaoxiaoNeural",
        # P2-7 语音身份/音色（均可选；不配置时沿用 provider 默认音色，向后兼容）：
        #   voices             = {agent_id: voice}   角色音色（身份）
        #   voice_emotion_map  = {emotion: voice}    情绪音色（语气，来自 [emotion:xxx] 标签）
        "voices": {},
        "voice_emotion_map": {},
    },
    "sfx": {
        "enabled": True,
        "volume": 0.5
    },
    "ui": {
        "onboarded": False  # 首屏引导是否已看过
    },
    "greeting": {
        "enabled": True,   # 开场问候总开关（零配置首次启动时触发本地问候气泡）
    },
    "proactive": {
        "enabled": True,
        "cooldown_minutes": 10,
        # 2026-09-06: 打扰预算（每日上限，按时间段分配）
        "daily_budget": {
            "enabled": True,
            "daily_limit": 6,  # 每日总上限
            "period_limits": {  # 按时间段分配
                "morning": 2,   # 08:00-12:00
                "afternoon": 2, # 12:00-18:00
                "evening": 2,   # 18:00-24:00
                "night": 0      # 00:00-08:00（深夜不响）
            }
        },
        # 2026-09-06: 静默模式（Do Not Disturb）
        "dnd": {
            "enabled": True,
            "late_night_start": 0,  # 00:00
            "late_night_end": 8     # 08:00
        },
        # P0-1 主动搭话 LLM 生成开关（默认开）：模板池 → 候选生成 + LLM 决策是否开口；
        # 生成失败/超时自动回退固定模板池（fallback），不影响既有规则引擎
        "llm_generation": True,
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
    },
    "presence": {
        "enabled": True,
        "min_idle_minutes": 5,
        "interval_minutes": 8,
    },
    "window_interaction": {
        "enabled": True,
        "auto_walk": False,
        "cooldown_seconds": 600
    },
    "memory": {
        "budget_chars": 0,
        "budget_percent": 1.0,
        # P0-3 BM25+RRF 混合检索开关（默认开）：场景/事件检索从"标签精确匹配"升级为
        # "CJK 2/3-gram 关键词 + BM25 + RRF" 混合召回；无 embedding 时自动退化为 BM25-only
        "hybrid_bm25": True,
        # P1-1 向量嵌入（默认关）：本地 ONNX EmbeddingService，为 hybrid 检索的
        # cosine 路径提供语义向量；onnxruntime 不可用/模型缺失时自动降级纯 BM25（fallback gate）
        "embedding": {
            "enabled": False,
            # 本地 ONNX 模型文件路径（或含 onnx/ 子目录的模型目录，按 N.E.K.O.
            # 布局找 model_quantized.onnx / model.onnx）；不配置/不存在 → 自动降级纯 BM25，
            # 绝不自动下载大模型
            "model_path": "",
            # 参考模型（仅信息展示，不触发下载）：
            #   jinaai/jina-embeddings-v5-text-nano-retrieval
            #   revision ac5d898c8d382b17167c33e5c8af644a3519b47d（N.E.K.O. profile local-text-retrieval-v1）
            "model_name": "jinaai/jina-embeddings-v5-text-nano-retrieval",
            # 输出向量维度（Matryoshka 截断）：32/64/128/256/512/768 或 "auto"（按内存选档）
            "dim": 256,
            # 量化变体："auto" | "int8" | "fp32"（auto 优先 int8）
            "quantization": "auto",
            # tokenizer 截断长度（与向量缓存指纹 model_id 绑定）
            "max_length": 1024,
            # 单次推理超时（秒）：超时不阻塞主线程，连续超时达阈值后粘性降级
            "timeout_seconds": 8.0,
            # 模型加载超时（秒）：首次懒加载的有界等待上限
            "load_timeout_seconds": 60.0,
        },
        # D 场景回忆（proactive 命中历史场景时主动说一句带记忆的话）
        "recall": {
            "enabled": True,
            "cooldown_minutes": 30,
        },
        # E 跨场景联想（标签交集规则版；误触发率高可关）
        "associate": {
            "enabled": True,
        },
        # P1-2 事实库（LLM 抽取 + 本地去重；抽取失败自动跳过，不阻塞记忆写入）
        "facts": {
            "enabled": True,
            "dedup_threshold": 0.75,  # n-gram Jaccard 去重阈值（同事实不同表述命中线）
        },
        # P1-3 反思/摘要引擎（事件流 → LLM 摘要压缩；LLM 不可用跳过并记日志）
        "reflection": {
            "enabled": True,
            "interval_hours": 24,    # 反思周期（小时）
            "min_events": 5,         # 触发所需最少事件数
            "max_events": 200,       # 单次反思纳入事件上限
            "retry_minutes": 60,     # LLM 失败后退避重试间隔
        },
    },
    # P0-5/P0-7 专注模式（默认关）：专注模式下主动搭话频率下降、视觉安静；
    # 开启后聊天面板边缘 + 气泡轻微呼吸辉光（不遮屏、不抢焦点）
    "focus": {
        "enabled": False,
        "glow_strength": 0.3,  # 专注辉光强度 0~1（0=零视觉，默认 0.3）
    },
    # G celebrating（庆祝态：撒花动作 + 完工音；关掉即恢复旧 happy 行为）
    "celebrating": {
        "enabled": True,
        "tts_enabled": True,
    },
    # F 本地状态口（默认关；开启后 127.0.0.1:8977 提供只读状态 + 可选白名单写）
    "state_http": {
        "enabled": False,
        "port": 8977,
        "auth_token": "",
        "allow_set_mode": False,
    },
    # P4 通用外部触发入口（默认关；开启后 127.0.0.1:8988 接收 POST /trigger）。
    # 任何外部调度器可推送动作触发（remind/say/praise/custom），桌宠自身本地
    # 提醒保持自包含；这是可选附加入口，不绑定任何特定调度器或个人任务。
    "external_trigger": {
        "enabled": False,
        "port": 8988,
        "auth_token": "",
    },
    # P6 插件工具（默认关：plugins/ 目录为空时保留接口但不扫，避免误导用户以为可用）
    "plugin_tools": {
        "enabled": False,
    },
    # 需求②：Minecraft 桥接配置（默认关；启用后注册 mc_method/mc_task 能力）。
    # 跟 state_http/external_trigger/plugin_tools 同款形状。配置由设置面板「🎮 Minecraft」
    # 标签页读写（config.json），不进 .env（.env 只放 API 凭据）。
    # guardrails（P4，默认收紧）：
    #   allow_remote=False  → 仅允许本机 127.0.0.1/localhost，防误连远程/防 token 泄露
    #   require_token=True  → 调用方法必须带 token
    #   block_destructive=True → 拦截高危方法（op/ban/give/fill/summon/execute…）
    #   allowed_methods=[]  → 空=放行除高危外全部；非空=仅白名单前缀放行
    "mc": {
        "enabled": False,
        "transport": "http",            # http | ws
        "http_url": "http://127.0.0.1:8765",
        "ws_url": "ws://127.0.0.1:48909",
        "token": "",
        "timeout": 120,                  # 单任务最长等待秒
        "http_timeout_ms": 10000,
        "guardrails": {
            "allow_remote": False,
            "require_token": True,
            "block_destructive": True,
            "allowed_methods": [],
        },
    },
    # 需求③：Hanako QQ/微信桥接（只读——收到消息时让桌宠提醒，刻意不提供发送能力。
    # 实测 server 对外没有第三方可用的纯文本发送口，回复仍由 Hanako 侧完成）。
    # agent_id 必须显式填，模块不会自动猜别人的 agent。
    "hanako_bridge": {
        "enabled": False,
        "agent_id": "",                  # 必填：~/.hanako/agents/ 下某个 agent（如 ophelia）
        "platforms": ["qq", "wechat"],
        "owner_only": True,
        "max_per_hour": 10,              # 每小时最多提醒几次，防刷屏
        "poll_interval": 30,             # 轮询间隔（秒）
        "message_limit": 20,             # 每次拉取条数（用于水位对齐与去重）
        "localhost_only": True,          # 强制走 127.0.0.1，不用 server-info 广告的内网 IP
    },
    # P1-5 反重复（语义指纹 + 时间窗去重）：阈值与 N.E.K.O. session_settings 一致，
    # 可在此覆盖；关闭 enabled 后 proactive 仅保留字符串相似去重（旧行为）
    "anti_repeat": {
        "enabled": True,
        "bg_window": 100,
        "fg_window": 5,
        "fg_ttl_seconds": 600.0,
        "bm25_k1": 1.5,
        "bm25_b": 0.75,
        "min_draft_tokens": 12,
        "regen_threshold": 8.0,
        "drop_threshold": 16.0,
    },
    # P1-6 屏幕/意图感知升级：场景分类 + LLM 语义增强（可选）
    "screen": {
        "enabled": True,
        "interval": 120,
        "blur": False,
        "blacklist": False,
        "compress": True,
        # LLM 语义增强开关（默认开）：未注入 provider 时自动退化为纯规则分类；
        # 增强失败/超时/解析错误 → 保留规则结果，不阻塞感知
        "llm_enrich": True,
        # 429 限流缓解：LLM 语义增强冷却（秒）。场景未变化时最多每 N 秒补一次，
        # 避免"每次截图 = 视觉 API + enrich LLM 两次请求"打满限流；场景变化立即补。
        "llm_enrich_cooldown": 300,
    },
    # P6-A：感知层配置
    "perception": {
        # Obsidian 日报输出目录（generate_daily_diary 用）。
        # 缺省空串 → 回退到环境变量 OC_PET_OBSIDIAN_DIR → 内置默认路径。
        "obsidian_diary_dir": "",
    },
}

CHARACTER_INFO = {
    "default": {
        "name": "幽灵团子",
        "path": "characters/default",
    },
    "phoebe": {
        "name": "菲比",
        "path": "characters/phoebe",
    },
}

# 情绪 → 帧动画序列映射（P3 连续参数：帧区间）
# oc-pet 靠帧序列名切换表情，P3 增强后不同情绪映射到同一序列的不同帧子范围
# 格式: 情绪名 -> (序列名, 起始帧索引, 结束帧索引)
#          起始/结束为 None 时使用全序列
EXPRESSION_MAP = {
    "happy":      ("waving",   None, None),  # 开心 -> 挥手
    "surprised":  ("surprise", None, None),  # 惊讶 -> 专属惊讶帧（P5：不再复用 jumping）
    "angry":      ("angry",    None, None),  # 生气 -> 专属生气帧（P5）
    "sad":        ("failed",   None, None),  # 悲伤 -> 失败（低落动画）
    "thinking":   ("waiting",  None, None),  # 思考 -> 等待（张望）
    "working":    ("review",   None, None),  # 工作 -> 审阅
    "cute":       ("waving",   None, None),  # 卖萌 -> 复用挥手
    "missing":    ("waiting",  None, None),  # 张望 -> 等待
    "neutral":    ("idle",     None, None),  # 中性 -> 空闲
    "listening":  ("idle",     None, None),  # 倾听 -> 空闲
    "speaking":   ("idle",     None, None),  # 说话 -> 空闲
}

# 表情 -> 默认过渡样式（snap=瞬切/fade=缓出/spring=弹簧）
# 驱动 PetWindow._set_anim_seq 的 style 参数
# 选型依据：
#   - surprise/angry → 突变形情绪，弹簧反弹强化冲击
#   - happy/cute/thinking → 温和切换
#   - listening/speaking/working → 高频切勿过渡，保持同步感
#   - neutral/sad/missing → 默认缓出
EXPRESSION_TRANSITION_STYLE = {
    "happy":      "fade",
    "surprised":  "spring",
    "angry":      "spring",
    "sad":        "fade",
    "thinking":   "fade",
    "working":    "snap",
    "cute":       "fade",
    "missing":    "fade",
    "neutral":    "fade",
    "listening":  "snap",
    "speaking":   "snap",
}


def get_transition_style(emotion: str, default: str = "snap") -> str:
    """查表情过渡样式。未匹配返回 default。

    Args:
        emotion: 表情名（如 'happy'）
        default: 未匹配时的回退样式（默认 'snap'，保持向后兼容）
    Returns:
        'snap' | 'fade' | 'spring'
    """
    if not emotion:
        return default
    return EXPRESSION_TRANSITION_STYLE.get(emotion, default)

# atlas 模式的状态→动画映射（9 种动画）
# 如果 pet.json 的 emotions 字段存在，优先用它；否则用这个回退
ATLAS_STATE_MAP = {
    "idle":         "idle",
    "walking":      "running-right",
    "greeting":     "waving",
    "excited":      "jumping",
    "error":        "failed",
    "waiting":      "waiting",
    "thinking":     "review",
    "working":      "review",
    "done":         "review",
    "happy":        "waving",
    "surprised":    "jumping",
    "angry":        "jumping",
    "sad":          "failed",
    "cute":         "waving",
    "missing":      "waiting",
    "neutral":      "idle",
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
    """深度合并：override 的键覆盖 base，但 base 独有的键保留。

    空值保护：override 的值为空字符串/None 时不覆盖 base 的已有非空值，
    避免旧快照用空值把真实配置（如 dialog.agent_id=aimis）冲掉。
    """
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        elif v is None or v == "":
            # 空值不覆盖已有非空值（保留 base 现值）
            if k in result:
                continue
            result[k] = v
        else:
            result[k] = v
    return result

def save_config(cfg):
    """原子写入配置文件（合并式）

    以磁盘现有内容为底，用传入 cfg 覆盖后写回。
    这样只更新调用方关心的字段，不会把其他系统（如 dialog.agent_id）
    用旧快照冲掉——避免 F5 绑定丢失问题。
    """
    import tempfile
    merged = {}
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                merged = json.load(f)
    except Exception:
        merged = {}
    # 传入的 cfg 覆盖（含其内部 dict 键）
    for k, v in cfg.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(CONFIG_PATH), suffix='.tmp')
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        # Windows 下目标文件可能被瞬时占用（杀软扫描/编辑器锁/其他进程），
        # os.replace 直接抛 PermissionError 会让设置保存崩溃。加短重试。
        for attempt in range(5):
            try:
                os.replace(tmp_path, CONFIG_PATH)  # 原子替换
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class _AsyncConfigSaver:
    """防抖异步配置保存器 — 高频位置写入不阻塞 GUI 线程。

    设计：
      - 同一次调度周期内多次 schedule() 只落盘一次（保留最新配置）
      - 写盘在后台线程执行，绝不阻塞调用方（GUI 主线程）
      - 线程安全：schedule 可从任意线程调用

    用法：
        saver = AsyncConfigSaver()
        saver.schedule(cfg)   # 防抖 150ms 后异步写盘
    """

    def __init__(self, debounce_ms: int = 150):
        self._debounce = debounce_ms / 1000.0
        self._lock = threading.Lock()
        self._pending: dict | None = None          # 待写的最新配置
        self._due: float | None = None             # 下次写盘时刻（防抖窗口）
        self._thread: threading.Thread | None = None
        self._stop = False

    def schedule(self, cfg: dict) -> None:
        """登记一次保存。同窗口内多次调用合并为一次落地。"""
        with self._lock:
            self._pending = cfg
            now = time.monotonic()
            self._due = now + self._debounce
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def _run(self) -> None:
        """后台循环：等到防抖窗口，写盘最新快照。"""
        while True:
            with self._lock:
                if self._stop:
                    return
                now = time.monotonic()
                if self._pending is None:
                    return
                if now < self._due:
                    wait = self._due - now
                else:
                    wait = None
            if wait is not None:
                time.sleep(wait)
                continue
            # 窗口已到：取最新快照并写盘
            with self._lock:
                cfg = self._pending
                self._pending = None
            try:
                save_config(cfg)
            except Exception:
                pass
            # 若调度期间又有新值，继续循环；否则退出
            with self._lock:
                if self._pending is None:
                    return

    def shutdown(self) -> None:
        """停止并处理最后一次待写（进程退出前调用）。"""
        with self._lock:
            self._stop = True
            cfg = self._pending
            self._pending = None
        if cfg is not None:
            try:
                save_config(cfg)
            except Exception:
                pass


# 进程级共享实例：桌宠位置这类高频写入都走它，合并落盘。
async_config_saver = _AsyncConfigSaver()
