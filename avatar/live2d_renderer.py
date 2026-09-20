"""Live2DRenderer - Live2D (Cubism) 角色渲染器

用 live2d-py (live2d.v3, Cubism Native) 在透明 QOpenGLWidget 上渲染 Live2D 模型，
实现 AvatarRenderer 接口，使后端业务（对话/行为/感知）无需关心底层是精灵还是 Live2D。

参数驱动（让角色"活起来"，对齐视频里 Amadeus 的效果）：
    - 情绪(emotion)   -> Live2D Expression（按模型可用表情名模糊匹配）
    - TTS 说话         -> ParamMouthOpenY 口型（说话时振荡开合）
    - 视线(look_at)    -> ParamAngleX/Y + ParamEyeBallX/Y（瞳孔/头部朝向鼠标）
    - 待机            -> SetAutoBlink + SetAutoBreath（免费眨眼/呼吸）
    - 朝向            -> SetScaleX 正负镜像

注意：
    - 真实 GL 绘制必须在有 GPU/显示的机器上验证（headless/沙箱里 live2d.v3.init()
      会段错误，属环境限制，非代码缺陷）。
    - 所有 GL/live2d 调用都做了 try/except 降级：缺模型或 GL 不可用时角色区域透明，
      其余功能（气泡/抽卡/HUD）不受影响。
    - 模型缩放/偏移在不同 Cubism 模型上可能需要微调；可通过角色 pet.json 的
      live2d.scale / live2d.offset 覆盖。
"""
from __future__ import annotations

import logging
import math
import os
import random
import time
from typing import Optional

from PySide6.QtCore import Qt, QPoint

from avatar import fit_cache
from avatar.base import AvatarRenderer
from avatar.gl_char_widget import GLCharWidget
from avatar.emote_presets import LIVE2D_PRESETS, get_live2d_preset as _get_live2d_preset, get_preset_names as _get_preset_names
from avatar.model_profile import DEFAULT_PROFILE, load_profile_for_character
from avatar.param_writer import ParamWriter
from avatar.motion_mixer import (
    MotionMixer,
    MotionRequest,
    Layer,
    easeOut,
)
from avatar.frame_pipeline import create_default_pipeline
from avatar.decision_trace import trace

logger = logging.getLogger(__name__)

_global_l2d_inited: bool = False  # live2d.v3.init() 进程级只调一次（多宠不重复初始化）


class Live2DRenderer(AvatarRenderer):
    """Live2D (Cubism) 渲染器。"""

    # 情绪 -> 表情名关键词（模型有语义表情时匹配；否则用 motion 或忽略）
    _EMOTION_KEYWORDS = {
        "happy": ("happy", "joy", "smile", "fun", "usui", "唱歌", "比心"),
        "angry": ("angry", "ikari", "mad"),
        "sad": ("sad", "kanashii", "cry"),
        "surprised": ("surprise", "odoroki", "shock", "圈圈"),
        "thinking": ("think", "thinking", "doubt", "kangaeru"),
        "cute": ("cute", "脸红"),
        "neutral": (),
    }
    # 自动排除的表情名关键词（作者水印/版权声明等，桌宠不展示）
    # T2-2a: 改为可配置，不再硬编码；默认值保持兼容
    _IGNORED_EXPRESSIONS_DEFAULT = ("水印", "watermark", "版权", "author", "credit", "logo")
    _IGNORED_EXPRESSIONS = _IGNORED_EXPRESSIONS_DEFAULT  # 可被 config.json 覆盖
    # 卡手势防御：非 idle motion 播满此秒数强制回 idle（模型 motion 全 Loop=true，
    # waving/touch 等手势 mp3.json 都是 2.667s 循环，播 1.5 圈后回位）
    GESTURE_TIMEOUT = 5.0  # 2026-09-07: 从 3.0 提到 5.0，避免 LLM 生成期间表情被重置
    # W3（2026-09-14）：显式参数意图（LLM [action:{params:...}]）的保留时长。
    # 旧实现下 _param_intent 永不过期 → 写过的嘴型/眉眼会一直挂着。
    # 用户要求“情绪结束就收”；这类参数不由情绪驱动，故给一个保留窗口，
    # 到期后平滑回归情绪基准值。设 0 或负数 = 关闭超时（保留旧行为）。
    PARAM_INTENT_TTL: float = 6.0
    # idle 重播最小间隔（秒）：IdleLoopProcessor 每帧都会试，本值防止异常快速重播。
    # 正常 idle motion 长度 2-4s 远大于此值，不受影响。实测修复前 idle 一秒播 3 次。
    IDLE_RESTART_MIN_INTERVAL: float = 0.5
    # 动作过渡（easing）：表情（SetExpression）从上一个状态淡入到新状态的默认时长。
    # Live2D motion 文件本身的跨 blend 目前受限于 live2d-py wrapper 未暴露
    # motion weight API，无法在同一 motion 内部做淡入（仍为硬切）；但表情层
    # SetExpression 支持 weight 参数，可以平滑。默认 0.3s、easeOut 曲线。
    EXPRESSION_TRANSITION_S: float = 0.3
    # 默认缓动曲线：easeOut（快启慢收），适合“刚接上→自然放松”的表情接杆感。
    EXPRESSION_TRANSITION_EASING: str = "easeOut"
    # 情绪 -> motion 组名（模型有对应动作组时播放）
    _EMOTION_MOTION = {
        "happy": ("happy", "joy", "fun"),
        "angry": ("angry", "mad"),
        "sad": ("sad", "cry"),
        "surprised": ("surprise", "shock"),
        "thinking": ("think", "doubt"),
    }

    # P2-6: 程序化表情层插值时间常数（秒）。越大过渡越慢越柔，越小越跟手。
    # 情绪切换时所有面部参数按此常数做指数平滑（帧率无关：alpha = 1 - exp(-dt/tau)）。
    PROCEDURAL_SMOOTH_TAU = 0.28

    # P2-6: 情绪 -> 面部参数目标（master emotion 驱动）。
    # 全部为归一化值，经 _proc_cur 平滑插值后以 weight<1 叠加在 motion 之上：
    #   eye_open    - 眼睛开合 0=闭 1=全开（surprised 超 1 会被 clamp）
    #   eye_smile   - 眯眼（正=笑眼 负=瞪眼/吊眼）
    #   brow_angle  - 眉毛角度（正=挑眉 负=皱眉）
    #   brow_form   - 眉毛形态（正=弯 负=八字/竖）
    #   mouth_form  - 嘴型（正=笑 负=撇嘴）
    #   mouth_open  - 嘴张开（说话时让位给 _update_mouth，不叠加）
    #   eye_ball_x/y - 眼神方向偏移（低权重叠在视线跟随之上）
    #   head_angle_x/y - 头部轻微转向（仅视线跟随关闭时生效，避免打架）
    #   breath_amp / breath_rate - 呼吸幅度/频率倍率（1.0 = 常态）
    # 约束：miku moc3 无手/臂/腿参数（双手固定祈祷），只驱动面部/眼神/呼吸，
    # 不尝试任何肢体动作（ParamArm*/ParamLeg*/ParamBodyAngle* 一律不碰）。
    _EMOTION_FACIAL_TARGETS = {
        "neutral": {
            "eye_open": 0.85, "eye_smile": 0.2, "brow_angle": 0.1, "brow_form": 0.1,
            "mouth_form": 0.2, "mouth_open": 0.0, "eye_ball_x": 0.0, "eye_ball_y": 0.0,
            "head_angle_x": 0.0, "head_angle_y": 0.0, "breath_amp": 1.0, "breath_rate": 1.0,
        },
        "happy": {
            "eye_open": 0.9, "eye_smile": 0.75, "brow_angle": 0.35, "brow_form": 0.3,
            "mouth_form": 0.45, "mouth_open": 0.12, "eye_ball_x": 0.0, "eye_ball_y": 0.05,
            "head_angle_x": 0.0, "head_angle_y": 0.0, "breath_amp": 1.15, "breath_rate": 1.1,
        },
        "sad": {
            "eye_open": 0.72, "eye_smile": -0.35, "brow_angle": -0.45, "brow_form": -0.4,
            "mouth_form": -0.45, "mouth_open": 0.04, "eye_ball_x": 0.0, "eye_ball_y": -0.08,
            "head_angle_x": 0.0, "head_angle_y": 0.0, "breath_amp": 0.85, "breath_rate": 0.75,
        },
        "angry": {
            "eye_open": 0.8, "eye_smile": -0.25, "brow_angle": -0.7, "brow_form": -0.5,
            "mouth_form": -0.35, "mouth_open": 0.05, "eye_ball_x": 0.06, "eye_ball_y": -0.05,
            "head_angle_x": 0.0, "head_angle_y": 0.0, "breath_amp": 1.2, "breath_rate": 1.25,
        },
        "surprised": {
            "eye_open": 1.1, "eye_smile": 0.1, "brow_angle": 0.6, "brow_form": 0.45,
            "mouth_form": 0.5, "mouth_open": 0.55, "eye_ball_x": 0.05, "eye_ball_y": 0.12,
            "head_angle_x": 0.0, "head_angle_y": 0.0, "breath_amp": 1.3, "breath_rate": 1.4,
        },
        "thinking": {
            "eye_open": 0.78, "eye_smile": 0.05, "brow_angle": 0.28, "brow_form": 0.22,
            "mouth_form": -0.18, "mouth_open": 0.03, "eye_ball_x": 0.14, "eye_ball_y": 0.12,
            "head_angle_x": 0.08, "head_angle_y": 0.0, "breath_amp": 1.0, "breath_rate": 0.9,
        },
        "cute": {
            "eye_open": 0.92, "eye_smile": 0.68, "brow_angle": 0.25, "brow_form": 0.3,
            "mouth_form": 0.35, "mouth_open": 0.1, "eye_ball_x": 0.02, "eye_ball_y": 0.06,
            "head_angle_x": 0.0, "head_angle_y": 0.0, "breath_amp": 1.1, "breath_rate": 1.05,
        },
        # ── 2026-09-20 新增 4 个锚点（B 项）──
        #
        # 为什么：原 7 个锚点在 V/A 空间分布不均——
        #   V>0 且 A<0 的区域（「平静的愉快 / 温柔」）**完全空白**。
        # 实测后果：分类器的 affectionate(0.65,0.10)、calm(0.25,-0.45)
        # 落进去后只能被拉向 happy 或 thinking，做不出「柔和」的味。
        # 这四个新锚点直接对着分类器 14 类的 VAD 预设填（见
        # core/emotion_classifier.py::EMOTION_VAD_PRESETS）。
        "affectionate": {
            # 温柔/亲昵：眼微弯、嘴柔、呼吸缓（比 happy 淡，比 neutral 暖）
            "eye_open": 0.88, "eye_smile": 0.55, "brow_angle": 0.22, "brow_form": 0.2,
            "mouth_form": 0.32, "mouth_open": 0.04, "eye_ball_x": 0.0, "eye_ball_y": 0.03,
            "head_angle_x": 0.0, "head_angle_y": 0.0, "breath_amp": 1.02, "breath_rate": 0.92,
        },
        "calm": {
            # 平静/放松：眼半闭、呼吸慢（比 neutral 更低唤起）
            "eye_open": 0.75, "eye_smile": 0.3, "brow_angle": 0.12, "brow_form": 0.08,
            "mouth_form": 0.18, "mouth_open": 0.0, "eye_ball_x": 0.0, "eye_ball_y": -0.02,
            "head_angle_x": 0.0, "head_angle_y": 0.0, "breath_amp": 0.92, "breath_rate": 0.78,
        },
        "confused": {
            # 困惑：眉压、嘴形偏、眼神偏一侧（比 thinking 更「不确定」）
            "eye_open": 0.82, "eye_smile": -0.05, "brow_angle": -0.15, "brow_form": -0.1,
            "mouth_form": -0.22, "mouth_open": 0.02, "eye_ball_x": 0.16, "eye_ball_y": 0.04,
            "head_angle_x": 0.1, "head_angle_y": 0.0, "breath_amp": 1.0, "breath_rate": 1.0,
        },
        "tired": {
            # 疲倦：眼睑下沉、呼吸深而慢（比 sad 更「低能量」而非「低情绪」）
            "eye_open": 0.6, "eye_smile": -0.1, "brow_angle": -0.1, "brow_form": -0.12,
            "mouth_form": -0.15, "mouth_open": 0.06, "eye_ball_x": 0.0, "eye_ball_y": -0.1,
            "head_angle_x": 0.0, "head_angle_y": 0.0, "breath_amp": 1.1, "breath_rate": 0.65,
        },
    }

    # ── T10: V/A (valence-arousal) 坐标 ──
    # 每种情绪映射到 2D 情感空间，情绪切换变成 V/A 空间路径插值。
    # valence: -1(消极) → +1(积极), arousal: -1(平静) → +1(兴奋)
    #
    # 2026-09-20（B 项）：7 → 11 个锚点。新增的四个直接对着
    # core/emotion_classifier.py 的 14 类 VAD 预设，填的是原来
    # 「V>0 且 A<0」（温柔/平静）与「V<0 且 A≈0」（困惑/疲倦）的空白。
    # 坐标取自 EMOTION_VAD_PRESETS，不另编。
    _EMOTION_VA: dict[str, tuple[float, float]] = {
        "neutral":      (0.0,  0.0),
        "happy":        (0.8,  0.7),
        "cute":         (0.7,  0.5),
        "surprised":    (0.3,  0.9),
        "thinking":     (0.2,  0.2),
        "sad":          (-0.7, -0.3),
        "angry":        (-0.6,  0.8),
        # 新增（对着分类器 VAD 预设）
        "affectionate": (0.65, 0.10),
        "calm":         (0.25, -0.45),
        "confused":     (-0.10, 0.35),
        "tired":        (-0.25, -0.70),
    }

    # V/A → 参数插值用的情绪集合（用于最近邻查找）
    _VA_EMOTIONS = [
        "neutral", "happy", "cute", "surprised", "thinking", "sad", "angry",
        "affectionate", "calm", "confused", "tired",
    ]

    def _va_interpolate_targets(self, va_cur: tuple[float, float]) -> dict[str, float]:
        """T10: 从 V/A 坐标插值参数目标值。

        在 V/A 空间找最近的情绪对，bilinear 插值参数。
        返回与 _EMOTION_FACIAL_TARGETS 同结构的 dict。
        """
        v_cur, a_cur = va_cur
        # 找最近的两个情绪（按欧氏距离）
        distances = []
        for emo_name in self._VA_EMOTIONS:
            v_e, a_e = self._EMOTION_VA[emo_name]
            dist = math.sqrt((v_cur - v_e) ** 2 + (a_cur - a_e) ** 2)
            distances.append((dist, emo_name))
        distances.sort()
        # 取最近的两个情绪做插值
        (d0, e0), (d1, e1) = distances[0], distances[1]
        if d0 < 0.01:
            return dict(self._EMOTION_FACIAL_TARGETS[e0])
        if d1 < 0.01:
            return dict(self._EMOTION_FACIAL_TARGETS[e1])
        # 在两个情绪之间线性插值
        t = d0 / (d0 + d1) if (d0 + d1) > 0 else 0.5
        t = max(0.0, min(1.0, t))
        tgt0 = self._EMOTION_FACIAL_TARGETS[e0]
        tgt1 = self._EMOTION_FACIAL_TARGETS[e1]
        result = {}
        for k in tgt0:
            result[k] = tgt0[k] * (1.0 - t) + tgt1.get(k, 0.0) * t
        return result

    # ── 动作优化层（纯参数驱动，不依赖新 motion 文件）──
    #
    # 1) 动作 + 表情叠加：motion 文件名关键词 → 叠加参数。
    #    播放非 idle motion 时生效（_note_motion_started 里设置），回 idle 时清除；
    #    在 _update_procedural_emotion 的情绪目标基础上叠加（clamp 到合理范围）。
    #    键与 _EMOTION_FACIAL_TARGETS 一致；blush 特殊（优先 ParamCheek，否则组合模拟）。
    #    约束：miku moc3 无手/臂/腿参数，只动面部/眼神/呼吸，绝不碰肢体。
    _ACTION_OVERLAYS: dict[str, dict[str, float]] = {
        "waving":    {"eye_smile": 0.45, "eye_open": 0.7, "blush": 0.3, "mouth_form": 0.25},
        # happy：miku 的 happy 情绪 expression 已是"脸红"贴图，不重复叠 blush 避免过浓
        "happy":     {"eye_smile": 0.5, "mouth_form": 0.3},
        "thinking":  {"eye_ball_x": 0.12, "eye_ball_y": 0.08, "mouth_open": 0.05, "head_angle_x": 0.06},
        "surprised": {"eye_open": 0.1, "brow_angle": 0.25, "mouth_open": 0.18},
        "angry":     {"brow_angle": -0.2, "brow_form": -0.15, "eye_smile": -0.12},
        "sad":       {"brow_angle": -0.15, "mouth_form": -0.2, "eye_ball_y": -0.05},
        "touch":     {"blush": 0.45, "eye_smile": 0.3, "eye_open": 0.65},
        "stroke":    {"blush": 0.55, "eye_smile": 0.4},
        "pet":       {"blush": 0.55, "eye_smile": 0.4},
        "special":   {"blush": 0.4, "eye_open": 0.6, "mouth_open": 0.08},
        "wedding":   {"blush": 0.5, "eye_smile": 0.5, "mouth_form": 0.3},
        "login":     {"eye_smile": 0.3, "brow_angle": 0.2, "blush": 0.25},
        "mail":      {"eye_ball_x": 0.08, "mouth_open": 0.04},
        "complete":  {"eye_smile": 0.4, "brow_angle": 0.2, "mouth_form": 0.3},
    }

    # 2) 表情序列预设：不播 motion，只做表情切换（play_emote_sequence 通用接口）。
    #    步类型：
    #      {"type": "set",   "duration": 秒, "params": {参数: 目标值}}  —— 目标覆盖情绪
    #      {"type": "blink", "times": n, "interval": 秒, "side": "both"|"left"|"right"}
    #                                                              —— 眨眼脉冲（按 times
    #        计数：闭眼起点 0/interval/2*interval…共 n 次，播满即结束；side 支持单眼 wink）
    #      {"type": "clear", "duration": 秒}                         —— 恢复情绪目标
    #    参数取值范围（clamp 见 _update_procedural_emotion）：
    #      eye_open 0=闭 1=全开 / eye_smile +眯眼 -瞪眼 / brow_angle +挑眉 -皱眉 /
    #      brow_form +弯 -八字 / mouth_form +笑 -撇嘴 / mouth_open 0..1 /
    #      eye_ball_x/y 眼神偏移 / head_angle_x/y 头部（仅序列播放时生效，见下）/
    #      breath_amp/rate 呼吸 / blush 脸红。约束：miku moc3 无肢体参数。
    # T09: 预设已外置到 avatar/emote_presets.py
    _EMOTE_PRESETS: dict[str, list[dict]] = LIVE2D_PRESETS

    # ── AI 面向的语义动作别名（2026-09-11）──
    #
    # 为什么不直接把预设名给 AI：实测 `[do:]` **全历史 0 次使用**。
    # 原因：prompt 里把 53 个预设名 + 13 个 motion 名（共 66 个）挤成一行，
    # 无说明、无分类——模型无法从“angry_glare/arm_wave/blink3/...”里选择。
    #
    # 参考 Amadeus 的 TRIGGER_ALIASES：**只给少量带语义的标签**，
    # 内部再归一到具体预设/motion。值与中文相近，模型用母语选更稳。
    #
    # ── 2026-09-20 扩容（实证依据）──
    #
    # `logs/oc_pet.log` 里 LLM 实际输出过但**播不出来**的名字：
    #     gesture=歪头 × 4   → 不在别名表 → “未知动作 '歪头'”
    #     gesture=wave × 2   → 不在别名表 → “未知动作 'wave'”
    # 加上 `walk` 194 次、`extra` 63 次的刷屏（见 _ANIM_TO_MOTION_KW 缺项），
    # 合计 257 条警告——全是“模型说对了、栅栏接不住”。
    #
    # 对照实验（deepseek-chat，真实 prompt 结构，10 个对话语境）：
    #     旧 17 项清单 → **2/10**（“开心”“微笑”“困”“摸头”“害羞”全被判无效）
    #     全 53 标签   → **10/10**
    #     分组 25 项   → **10/10**
    # 结论：**不是模型不会选，是清单里根本没有它想用的词**。
    #
    # 所以扩容原则：把“语义正确但当时没收录”的口语词全部接上。
    # 预设的中文标签（见 core/capability_snapshot.PRESET_LABELS）由
    # `_label_alias_map()` 自动并入，不再手抄一遍。
    _AI_DO_ALIASES: dict[str, str] = {
        # ── 表情（走预设路径）──
        "害羞": "blush_shy",
        "脸红": "blush_shy",
        "微笑": "smile_soft",
        "开心": "smile_bright",
        "大笑": "grin",
        "惊讶": "surprise_gasp",
        "思考": "think_look",
        "疑惑": "doubt",
        "点头": "nod",
        "摇头": "head_shake",
        "叹气": "sigh",
        "眨眼": "wink",
        "俏皮": "wink",
        "得意": "proud",
        "失落": "sad_droop",
        "生气": "angry_glare",
        "困": "stretch_yawn",
        "伸懒腰": "stretch_yawn",
        # ── 肢体（走 motion 路径）──
        "挥手": "waving",
        "打招呼": "waving",
        "摸头": "touch",
        "开心跳": "happy",
        # ── 2026-09-20 新增：日志实证“模型用过但播不了”的 ──
        "歪头": "head_tilt",
        "wave": "waving",          # 英文同义（模型中英混用）
        "walk": "walk",            # 走 _ANIM_TO_MOTION_KW 的 motion 路径
        # ── 2026-09-20 新增：常见口语同义（模型倾向用这些）──
        "高兴": "smile_bright",
        "愉快": "smile_soft",
        "温暖": "smile_soft",
        "关心": "smile_soft",
        "欣慰": "smile_soft",
        "兴奋": "excited",
        "惊喜": "surprise_gasp",
        "吃惊": "surprise_gasp",
        "发愣": "eye_wide",
        "好奇": "gaze_side",
        "疑问": "doubt",
        "不解": "doubt",
        "犯困": "sleepy",
        "打哈欠": "yawn",
        "疲惫": "sleepy",
        "难过": "sad_droop",
        "伤心": "sad_droop",
        "沮丧": "sad_droop",
        "委屈": "pout",
        "不满": "angry_glare",
        "无奈": "sigh",
        "无语": "eye_roll",
        "翻白眼": "eye_roll",
        "挑眉": "brow_raise",
        "嘟嘴": "lip_pucker",
        "撅嘴": "pout",
        "抿嘴": "mouth_pursed",
        "吐舌": "tongue_out",
        "耸肩": "shoulder_shrug",
        "点头晃动": "head_bob",
        "侧目": "gaze_side",
        "偷看": "sneak_peek",
        "偷瞄": "sneak_peek",
        "眨眼俏皮": "wink",
        "缓缓眨眼": "blink_slow",
        "闭眼": "blink_slow",
        "瞪眼": "eye_wide",
        "眯眼": "eye_narrow",
        "傻笑": "giggle",
        "轻笑": "giggle",
        "笑": "smile_bright",
        "摆手": "waving",
        "摇手": "waving",
        # ── 2026-09-20 新增：分组标题词（prompt 里以“开心:xxx”形式出现）──
        # 模型有概率直接把分组名当动作名用（如 gesture="开心"）。
        # 实测旧 17 项清单时代，模型输出过 gesture=开心 / 微笑 / 困 / 摸头 / 害羞
        # 都被判无效——现在全部接住，分组名也不会静默失败。
        "平静": "smile_soft",
    }

    # 注入 prompt 的选项文案（只列 key + 极简说明；别再把 66 个名字堆上去）
    #
    # 2026-09-20：改为**按情绪分组**。实证（deepseek-chat，真实 prompt，10 语境）：
    #   旧 17 项平铺 → 2/10（“开心”“微笑”等语义正确的名字因不在清单里被判无效）
    #   分组 25 项    → 10/10，且 prompt 仅 319 字符（仍 <300 的旧上限附近）
    # 分组让模型能先定位情绪、再选具体动作，比平铺一堆名词好选。
    _AI_DO_PROMPT: str = (
        "开心:灿烂笑容/咧嘴大笑/咯咯笑/兴奋/眼睛发亮/得意 "
        "害羞:害羞脸红/羞怯/偷瞄/嘟嘴 "
        "惊讶:惊讶吸气/瞪大眼睛/挑眉 "
        "思考:思考/抬头往上看/抿嘴 "
        "疑惑:疑惑歪头/歪头/侧目/耸肩 "
        "生气:生气瞪视/翻白眼/眯起眼睛 "
        "失落:失落垂眼/低头往下看/叹气 "
        "困:犯困/打哈欠/伸懒腰打哈欠 "
        "平静:温柔微笑/连续眨眼/点头/身体轻晃"
    )

    # 模型专属 prompt 文件（放模型目录下，与 .model3.json 同级）
    _MODEL_PROMPT_FILE = "model_prompt.txt"

    # 参数名映射：emote_presets.py 用下划线小写命名，StandardParams 用大写驼峰。
    # 2026-09-06 新增：修复“身体动作”预设（stretch/dance/body_sway/arm_wave）因参数名
    # 不匹配而无效的问题——之前 getattr(P, "body_angle_y") 找不到，SDK 收到字符串而非 ID。
    _PARAM_NAME_MAP: dict[str, str] = {
        "body_angle_x": "ParamBodyAngleX",
        "body_angle_y": "ParamBodyAngleY",
        "body_angle_z": "ParamBodyAngleZ",
        "arm_angle_l": "ParamArmLA",
        "arm_angle_r": "ParamArmRA",
        "head_angle_x": "ParamAngleX",
        "head_angle_y": "ParamAngleY",
        "head_angle_z": "ParamAngleZ",
        "eye_ball_x": "ParamEyeBallX",
        "eye_ball_y": "ParamEyeBallY",
        "mouth_form": "ParamMouthForm",
        "mouth_open": "ParamMouthOpenY",
        "blush": "ParamCheek",
        # ── W4 扩容（2026-09-14）：语义名 → 模型实际参数 id ──
        # 全部经 miku.cdi3.json（141 参数）逐个核对存在。
        # 注意：这些是**模型自定义命名**，非 Cubism 标准，换模型需重核。
        "mouth_waizui": "Paramwaizui",      # 歪嘴（撇嘴/不屑）
        "mouth_guzui": "Paramguzui",        # 鼓嘴（生气/卖萌）
        "mouth_guzui2": "Paramguzui2",      # 鼓嘴2
        "cheek_gulian": "Paramgulian",      # 鼓脸
        "mouth_tushe": "Paramtushe",        # 吐舌（调皮）
        "mouth_bite_lip": "mouthRollLower",  # 咬唇（害羞/紧张）
        "mouth_pucker": "mouthRollLower2",  # 撅嘴（撒娇）
        "mouth_shrug": "ParamMouthShrug",   # 撇嘴变体
        "eye_squint_l": "EyeL_Squint",      # 眯眼 L
        "eye_squint_r": "EyeR_Squint",      # 眯眼 R
        "eye_deform": "Param7",             # 眼变形（眼型）
        "brow_lx": "ParamBrowLX",           # 左眉左右
        "brow_rx": "ParamBrowRX",           # 右眉左右
        "brow_ly": "ParamBrowLY",           # 左眉上下
        "brow_ry": "ParamBrowRY",           # 右眉上下
        "hair_front": "ParamHairFront",     # 前发摇动
        "hair_side": "ParamHairSide",       # 侧发摇动
        "hair_back": "ParamHairBack",       # 后发摇动
    }
    
    # 参数白名单（参考 LLM_Live2D 的 genericParameterCatalog，2026-09-06）
    # 只允许写入白名单里的参数，防止 LLM 或动画预设写入无效参数。
    #
    # ⚠️ v2（2026-09-14 W4）：白名单是**跨模型的允许集**（union），
    #    不代表当前模型都有。实际写入前还会过一层“按模型实际参数表校验”
    #    （_effective_whitelist）——单靠这个集合会在换模型时写出无效参数。
    #    历史上这里同时列了标准名与变体名（如 ParamEyeOpen 与 ParamEyeLOpen），
    #    正是为了兼容不同模型的命名差异；真正的有效性由模型校验那一层负责。
    _PARAM_WHITE_LIST: set[str] = {
        "ParamAngleX", "ParamAngleY", "ParamAngleZ",
        "ParamBodyAngleX", "ParamBodyAngleY", "ParamBodyAngleZ",
        "ParamEyeBallX", "ParamEyeBallY",
        "ParamBrowAngle", "ParamBrowForm",
        "ParamMouthForm", "ParamMouthOpenY",
        "ParamEyeOpen", "ParamEyeSmile",
        "ParamBreathAmp", "ParamBreathRate",
        "ParamArmLA", "ParamArmRA",
        "ParamBlush",
        "ParamCheek",
        "ParamEyeLOpen", "ParamEyeROpen",
        "ParamBrowLAngle", "ParamBrowRAngle",
        "ParamBrowLForm", "ParamBrowRForm",
        # ── W4 扩容（2026-09-14）：表情丰富度前置 ──
        # 全部经 miku.cdi3.json 逐个核对存在，见 _PARAM_NAME_MAP 注释。
        "Paramwaizui", "Paramguzui", "Paramguzui2", "Paramgulian",
        "Paramtushe", "mouthRollLower", "mouthRollLower2", "ParamMouthShrug",
        "EyeL_Squint", "EyeR_Squint", "Param7",
        "ParamBrowLX", "ParamBrowRX", "ParamBrowLY", "ParamBrowRY",
        "ParamHairFront", "ParamHairSide", "ParamHairBack",
    }

    def _effective_whitelist(self) -> set[str]:
        """白名单 ∩ 模型实际参数（缓存）。

        W4（2026-09-14）：白名单是跨模型允许集，其中必然包含当前模型没有的名字。
        这里按模型实际参数表做一次校验，把无效名字滤掉——否则 LLM 写入
        ParamEyeOpen 这类“白名单有、模型无”的参数会静默失效。

        探测失败（拿不到参数表）时返回原白名单：宁可能写无效，也不要因为
        探测问题把功能全禁了（与 param_writer 的失败闭合策略一致）。
        """
        cached = getattr(self, "_effective_wl_cache", None)
        if cached is not None:
            return cached
        wl = self._PARAM_WHITE_LIST
        model = getattr(self, "_model", None)
        if model is None:
            return wl
        try:
            count = model.GetParameterCount()
            available = {str(model.GetParameter(i).id) for i in range(count)}
        except (AttributeError, TypeError, RuntimeError):
            logger.debug("白名单模型校验失败，回退原白名单", exc_info=True)
            return wl
        effective = wl & available
        dropped = wl - available
        if dropped:
            logger.info(
                "参数白名单按模型校验：%d 个存在，%d 个此模型没有（已过滤）",
                len(effective), len(dropped),
            )
            logger.debug("被过滤的白名单参数: %s", ", ".join(sorted(dropped)))
        self._effective_wl_cache = effective
        return effective

    # 自动表情序列的加权随机权重（_tick_auto_motion 25% 分支用）。
    # 值越大越常出现；0 表示不参与自动播放。戏剧性/长时间表情给低权重，
    # 微妙/日常表情给高权重，避免待机时频繁出现夸张脸。
    _EMOTE_PRESET_WEIGHTS: dict[str, float] = {
        "blink3": 2.5, "wink": 1.8, "blink_slow": 1.2,
        "blush": 1.6, "blush_shy": 1.4, "blush_deny": 0.8,
        "gaze_shift": 2.2, "gaze_up": 1.2, "gaze_down": 1.2, "gaze_side": 1.2, "sneak_peek": 1.0,
        "smile_soft": 2.4, "smile_bright": 1.8, "pout": 1.0,
        "angry_glare": 0.5, "sad_droop": 0.5, "surprise_gasp": 0.5,
        "think_look": 1.8, "doubt": 1.4, "sigh": 1.0, "yawn": 0.6,
        "excited": 1.2, "grin": 1.0, "shy": 1.2, "proud": 1.0, "sleepy": 0.7,
        "nod": 2.0, "head_shake": 1.6, "head_tilt": 1.8,
        "giggle": 1.4, "sneeze": 0.4, "stretch_yawn": 0.5,
        # 身体动作（参数驱动）— 2026-09-06 提权：让用户看到“身体感”而不仅只是摇头晃脑
        "stretch": 2.5, "dance": 1.5, "sit": 2.0, "lie": 0.5, "blink_quick": 2.0,
        # 微表情
        "eye_twinkle": 1.8, "brow_raise": 1.2, "lip_pucker": 1.0,
        # 头部细节
        "head_bob": 1.5, "head_roll": 1.2, "head_tilt_left": 1.4, "head_tilt_right": 1.4,
        # 身体细节 — 2026-09-06 提权：肩/手部微动增加身体感
        "body_sway": 2.5, "shoulder_shrug": 1.8, "arm_wave": 2.5,
        # 嘴巴细节
        "mouth_smile": 2.0, "mouth_pursed": 1.0, "tongue_out": 0.6,
        # 眼神细节
        "eye_roll": 0.5, "eye_wide": 1.2, "eye_narrow": 0.8,
    }

    # 3) idle 微摆动（参数随机化）：正弦叠加，相位/幅度/频率随机，避免机械循环感。
    #    仅 _motion_is_idle 时生效；头部角度目标 ±1.5°（weight 0.35 → 实际 ±0.5° 内），
    #    眼神 ±0.15（weight 0.25）。每 8~15 秒或每次进 idle 重新随机。
    IDLE_SWAY_RANDOM_MIN_S = 8.0
    IDLE_SWAY_RANDOM_MAX_S = 15.0

    def __init__(self, parent):
        super().__init__()
        self._parent = parent
        self._scale: float = 1.0
        self._facing_right: bool = True
        self._model_path: Optional[str] = None
        self._model = None          # live2d.v3.Model 实例（GL 就绪后加载）
        self._ready: bool = False   # 模型是否成功加载并可在 draw 中渲染
        self._fit_scale: float = 1.0
        self._fit_scale_x: float = 1.0  # 横向比例系数（超高画布模型微调用）
        self._center_offset_x: float = 0.0  # 水平居中偏移（画布单位，_recompute_fit 里 SetScale 后应用）
        self._mirror_facing_enabled: bool = False  # 朝向镜像（精灵图玩法，Live2D 不启用）
        self._facing_right: bool = True
        self._offset_scale: tuple[float, float] = (0.0, 0.0)

        # 自动随机动作：miku 只有 idle 一个默认 motion 看着"一直比心"。【2026-08-20
        # 用户反馈"表情不生动"】启动后随机周期（默认 15~45s）播 waving/happy/
        # thinking 等非 idle 动作，结束后回到 idle，每次 emotion 切换也触发一次新
        # 动作（让表情/动作同步变化）。开关由 enable_auto_motion 控制（默认开）。
        # 2026-09-05 优化：缩短周期（原 30~80s → 15~45s），让动作更灵活自主。
        self._auto_motion_enabled: bool = True
        self._auto_motion_min_s: float = 15.0
        self._auto_motion_max_s: float = 45.0
        self._auto_motion_next_at: float = 0.0  # 0 表示 ready 后立刻算第一个随机值

        # 视线/朝向目标（draw 中平滑插值）
        self._gaze_target_angle_x: float = 0.0
        self._gaze_target_angle_y: float = 0.0
        self._gaze_target_ball_x: float = 0.0
        self._gaze_target_ball_y: float = 0.0
        self._gaze_cur_angle_x: float = 0.0
        self._gaze_cur_angle_y: float = 0.0
        self._gaze_cur_ball_x: float = 0.0
        self._gaze_cur_ball_y: float = 0.0
        self._gaze_enabled: bool = True

        # 说话（TTS 口型）
        self._speaking: bool = False
        self._mouth_phase: float = 0.0
        # 实时音频电平源（0~1 RMS）。默认不可用 → _update_mouth 回退正弦包络。
        # 由 set_mouth_level_source 注入；异常一律当作"无电平"，不崩帧循环。
        self._mouth_level_fn = lambda: None
        # 口型包络（快起慢落）：音频块粒度约 40~170ms，直接跟会让嘴"跳"。
        # 起得快要跟上辅音爆发，落得慢要避免字间出现生硬的闭合。
        self._mouth_env: float = 0.0
        # LIP-1：音素级口型时间轴（由 set_lip_timeline 注入）
        self._lip_frames: list = []
        self._lip_clock_fn = None
        self._lip_started_at: float = 0.0
        # 当前音素形状（用于振幅级模式下保持唇形连贯）
        self._mouth_lip_open: float = 0.0
        self._mouth_lip_form: float = 0.0

        # 当前情绪
        self._emotion_target: str = "neutral"

        # P2-6: 程序化表情层平滑插值状态（master emotion 驱动的面部参数）
        self._proc_cur: dict[str, float] = {
            "eye_open": 0.85, "eye_smile": 0.0, "brow_angle": 0.0, "brow_form": 0.0,
            "mouth_form": 0.0, "mouth_open": 0.0, "eye_ball_x": 0.0, "eye_ball_y": 0.0,
            "head_angle_x": 0.0, "head_angle_y": 0.0, "breath_amp": 1.0, "breath_rate": 1.0,
        }
        self._proc_smooth_tau: float = float(self.PROCEDURAL_SMOOTH_TAU)
        self._proc_last_t: float = time.monotonic()
        # T10: V/A 中间表示（valence-arousal 情感空间）
        self._va_cur: tuple[float, float] = (0.0, 0.0)  # 当前 V/A 坐标
        self._va_target: tuple[float, float] = (0.0, 0.0)  # 目标 V/A 坐标
        self._emotion_intensity: float = 1.0  # 情绪强度（缩放参数幅度）

        # P0: 动作配置（pet.json "actions" 字段）——供右键菜单/LLM prompt 使用
        self._pet_actions: dict[str, dict] = {}

        # 兼容属性（避免 pet.py 直接访问崩溃）
        self._base_label_pos: QPoint = QPoint(10, 0)
        self._gaze_offset_x: float = 0.0
        self._gaze_offset_y: float = 0.0
        self._frames: dict = {}
        self._frame_tops: dict = {}
        self._anim_timer = None
        self._anim_seq: str = "idle"
        self._anim_idx: int = 0
        self._anim_range: tuple = (None, None)
        self._opacity_effect = None
        self._opacity: float = 1.0

        # GL 承载控件（真正的渲染表面）
        self.char_label = GLCharWidget(parent)
        self.char_label.setFixedSize(220, 260)
        # 关键：必须显式设置位置并显示，否则 widget 存在但不显示（画了看不见）。
        # 对齐 SpriteRenderer 的 move()+lower() 行为。
        self.char_label.move(10, 0)
        self.char_label.lower()
        self.char_label.show()
        self.char_label.move(10, 0)
        self.char_label.lower()
        self.char_label.set_renderer(self)
        self.char_label.installEventFilter(parent)

        # live2d 模块（延迟导入，避免无谓 banner；init 在 GL 就绪后调用）
        self._live2d = None
        self._motion_groups: dict = {}
        self._expression_names: list = []
        # T2-1: 帧管线化 —— 每帧的 idle/手势超时/自动动作/表情超时/视线/口型/
        # 程序化表情/待机摇摆/绘制 全部收敛到 FramePipeline，各处理器独立 try/except。
        self._pipeline = create_default_pipeline()
        # ── T08 动作混流层（D2）──
        # 2026-09-10 接线：MotionMixer 一直被 import、被调用（submit_motion_request /
        # force_idle / get_motion_layer / is_motion_idle 四处），但**从未实例化**。
        # 后果：每次带 duration 的动作意图都在 `self._mixer.submit(req)` 抛
        # AttributeError，被上游 try 吞掉 → 标签解析出来的动作全部静默死亡，
        # 桌宠能动的只有空闲时随机播的那几个。
        self._mixer = MotionMixer()
        # 结构化动作意图的平滑目标集（_set_intent_params 写入、_update_procedural_emotion
        # 读取）。原只在 _set_intent_params 里赋值，而 submit_motion_request 会先
        # 直接 .update() → 同样 AttributeError。在此无条件初始化。
        self._param_intent: dict = {}
        # W3：参数意图设置时刻（_expire_intent_params 据此判断是否过期）
        self._param_intent_set_at: float = 0.0
        # [feel:] 设定的 VA 目标在此时刻前不被离散情绪覆盖（2026-09-10）。
        # 没有它的话：apply_action_intent 刚写完 _va_target，紧接着
        # pet.py:2873 的 _sync_renderer_master_emotion 就会用离散 emotion
        # 查表盖掉——新通道被旧通道踩掉。
        self._va_hold_until: float = 0.0
        # 渲染器运行状态（防御性初始化）：无论 load() 是否成功、模型是否存在，
        # 这些属性都必须存在，使 set_emotion 等被 tick 无条件调用的方法成为安全 no-op。
        # 否则 load() 提前 return False（如占位角色无 live2d/ 目录）时，_model 为 None，
        # 但每帧 tick 仍调用 set_emotion → 访问未初始化属性抛 AttributeError → 崩溃重启循环。
        self._emotion_motion_cooldown: dict[str, float] = {}
        self._motion_is_idle: bool = True
        self._motion_started_at: float = 0.0
        self._last_gesture_at: float = 0.0
        self._current_emotion: str = "neutral"
        # 表情超时重置（P4-1）：非中性表情（比心/葱/唱歌/前倾等贴图开关）设置后
        # 在 GESTURE_TIMEOUT 内若无新表情则自动 ResetExpressions——否则表情永远挂着，
        # 用户看到的"一直比心"就是这个（motion 有超时兜底，expression 之前没有）。
        self._expression_set_at: float = 0.0
        # 动作过渡（easing）：表情 SetExpression 的淡入状态
        #   _expression_transition_expr: 当前正在淡入的表情名
        #   _expression_transition_at:   淡入起始时刻
        #   _expression_transition_w:    当前帧的平滑权重（0~1，经 easeOut）
        # 每次 _apply_expression 切换新表情时重置；_update_expression_transition
        # 每帧推进。transition_w 在 0~1 之间，传给 SetExpression(weight=...)。
        self._expression_transition_expr: str = ""
        self._expression_transition_at: float = 0.0
        self._expression_transition_w: float = 0.0
        self._expression_active: bool = False
        self._last_expression: str = ""

    @property
    def available_presets(self) -> list[str]:
        """返回可点名的表情预设名（供 LLM prompt 注入）。

        2026-09-11：emote_presets 里的 53 个预设原本只能被随机挑中，
        AI 点不了名。[do:预设名] 现在能直接播它们。

        这些是**纯参数驱动**的表情序列（缺参数会按白名单跳过），
        不依赖模型是否有对应 motion 文件，所以比动作更可靠。
        """
        try:
            return sorted(self._EMOTE_PRESETS.keys())
        except Exception:
            return []

    @property
    def available_actions(self) -> list[dict]:
        """返回可用动作列表（供 LLM prompt 注入和右键菜单使用）。

        每个动作：{ "name": str, "label": str, "motion": str, "intensity": float }
        """
        result = []
        for name, cfg in self._pet_actions.items():
            result.append({
                "name": name,
                "label": cfg.get("label", name),
                "motion": cfg.get("motion", name),
                "intensity": cfg.get("intensity", 0.7),
            })
        return result

    # ── 生命周期 ──

    def load(self, character_id: str, sprite_dir: str = None) -> bool:
        """记录模型路径（真实加载推迟到 GL 上下文就绪）。"""
        self._character_id = character_id
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        char_dir = os.path.join(base, "characters", character_id)
        
        # 2026-09-08: 支持直接检查 char_dir 下的 .model3.json（live2d zip 导入）
        for f in sorted(os.listdir(char_dir)):
            low = f.lower()
            if low.endswith(".model3.json") or low.endswith(".model.json"):
                self._model_path = os.path.join(char_dir, f)
                # 可选：pet.json 里的 live2d 缩放/偏移覆盖
                self._apply_live2d_meta(char_dir)
                # T09: 加载模型 profile（characters/<id>/live2d/profile.json）
                self._model_profile = load_profile_for_character(character_id, base)
                logger.info("Live2DRenderer: 模型路径已记录 %s", self._model_path)
                return True
        
        # 回退到 live2d/ 子目录
        live2d_dir = os.path.join(char_dir, "live2d")
        if os.path.isdir(live2d_dir):
            for f in sorted(os.listdir(live2d_dir)):
                low = f.lower()
                if low.endswith(".model3.json") or low.endswith(".model.json"):
                    self._model_path = os.path.join(live2d_dir, f)
                    # 可选：pet.json 里的 live2d 缩放/偏移覆盖
                    self._apply_live2d_meta(char_dir)
                    # T09: 加载模型 profile（characters/<id>/live2d/profile.json）
                    self._model_profile = load_profile_for_character(character_id, base)
                    logger.info("Live2DRenderer: 模型路径已记录 %s", self._model_path)
                    return True
        
        logger.warning("Live2DRenderer: 未找到 .model3.json/.model.json: %s", char_dir)
        return False

    def _apply_live2d_meta(self, char_dir: str) -> None:
        import json
        meta_path = os.path.join(char_dir, "pet.json")
        if not os.path.exists(meta_path):
            return
        try:
            meta = json.loads(open(meta_path, encoding="utf-8").read())
            l2d = meta.get("live2d", {})
            if "scale" in l2d:
                self._fit_scale = float(l2d["scale"])
            # 横向比例系数：对超高画布（如 3500x8888）模型横向偏窄，
            # 用 SetScaleX(fit*sx)/SetScaleY(fit) 分开设，让角色更“方正”。
            self._fit_scale_x = float(l2d.get("scale_x", 1.0))
            if "offset" in l2d and isinstance(l2d["offset"], (list, tuple)):
                self._offset_scale = (float(l2d["offset"][0]), float(l2d["offset"][1]))
            # 情绪 → anim/expression 精确映射（P1 桌宠优化）：pet.json 的
            # emotions["happy"].anim/.expression 成为唯一真相源，覆盖类级关键词
            # 匹配。miku 模型 7 个 motion（含 touch）+ 8 个 expression 全在
            # model3.json 的空组里，关键词匹配摸不到，精确映射才能对上。
            emos = meta.get("emotions", {}) or {}
            self._emotion_anims = {}
            self._emotion_exprs = {}
            for _name, _spec in emos.items():
                if not isinstance(_spec, dict):
                    continue
                if _spec.get("anim"):
                    self._emotion_anims[_name] = str(_spec["anim"])
                if _spec.get("expression"):
                    self._emotion_exprs[_name] = str(_spec["expression"])
            if self._emotion_anims or self._emotion_exprs:
                logger.info(
                    "Live2DRenderer: pet.json 情绪映射已加载 anims=%s exprs=%s",
                    self._emotion_anims, self._emotion_exprs,
                )
            # P8: 动作权重（weight=0 不自动播；未配置默认 1.0）——通用机制，
            # 每个模型在 pet.json 的 animations 块里配自己的权重（key=动作名）。
            self._motion_weights: dict[str, float] = {}
            _anims_cfg = meta.get("animations", {}) or {}
            for _an, _ac in _anims_cfg.items():
                if isinstance(_ac, dict) and "weight" in _ac:
                    try:
                        self._motion_weights[str(_an).lower()] = float(_ac.get("weight", 1.0))
                    except Exception:
                        logger.debug("Live2DRenderer: 非致命异常(已静默吞掉)", exc_info=True)
            if self._motion_weights:
                logger.info("Live2DRenderer: pet.json 动作权重已加载 %s", self._motion_weights)
            # P0: 动作配置（pet.json "actions" 字段）——供右键菜单/LLM prompt 使用
            self._pet_actions: dict[str, dict] = {}
            _actions_cfg = meta.get("actions", {}) or {}
            for _an, _ac in _actions_cfg.items():
                if isinstance(_ac, dict):
                    self._pet_actions[str(_an).lower()] = {
                        "label": _ac.get("label", _an),
                        "motion": _ac.get("motion", _an),
                        "intensity": float(_ac.get("intensity", 0.7)),
                    }
            
            # P1: 自动扫描 motions/ 目录，补充 pet.json 未配置的动作
            _motions_dir = os.path.join(char_dir, "live2d", "motions")
            if os.path.isdir(_motions_dir):
                _auto_added = 0
                for _filename in os.listdir(_motions_dir):
                    if _filename.endswith(".motion3.json") and not _filename.endswith(".bak"):
                        _motion_name = _filename.replace(".motion3.json", "").lower()
                        if _motion_name not in self._pet_actions:
                            self._pet_actions[_motion_name] = {
                                "label": _motion_name,
                                "motion": _motion_name,
                                "intensity": 0.7,
                            }
                            _auto_added += 1
                if _auto_added:
                    logger.info("Live2DRenderer: 自动扫描 motions/ 目录，添加 %d 个动作 %s", _auto_added, list(self._pet_actions.keys())[-_auto_added:])

            # 动作槽位自动映射：正则把 motion 文件名归到语义槽位（idle/wave/happy/sad/…），
            # 用语义名补充 _pet_actions，使 play_anim("happy") 即使 pet.json 未配也能播，
            # 省去每模型手写 profile（miku 的 happy.motion3.json 名就能直接对上）。
            try:
                from core.motion_slot import map_motions
                _slot_dir = os.path.join(char_dir, "live2d")
                if not os.path.isdir(_slot_dir):
                    _slot_dir = char_dir
                _slot_res = map_motions(_slot_dir)
                self._motion_slot_map = _slot_res.slots
                for _slot, _files in _slot_res.slots.items():
                    if _files and _slot not in self._pet_actions:
                        self._pet_actions[_slot] = {
                            "label": _slot, "motion": _files[0], "intensity": 0.7,
                        }
                logger.info(
                    "Live2DRenderer: 动作槽位映射 覆盖 %d/%d, 未分配 %d -> %s",
                    _slot_res.assigned, _slot_res.total_motions, _slot_res.unassigned,
                    {k: len(v) for k, v in _slot_res.slots.items()},
                )
            except Exception as e:
                logger.warning("Live2DRenderer: 动作槽位映射失败(非致命): %s", e)

            if self._pet_actions:
                logger.info("Live2DRenderer: pet.json 动作列表已加载 %s", list(self._pet_actions.keys()))
        except Exception as e:
            logger.warning("读取 live2d meta 失败: %s", e)

    def on_gl_initialized(self) -> None:
        """GL 上下文就绪：初始化 live2d 后端并加载模型。"""
        # 调试二分：环境变量 L2D_DEBUG_MINIMAL=1 时跳过所有附加逻辑，
        # 只保留纯测试路径（init->glInit->LAppModel->Load->Resize->SetScale->Draw）
        self._debug_minimal = os.environ.get("L2D_DEBUG_MINIMAL") == "1"
        self._debug = os.environ.get("L2D_DEBUG") == "1"  # 调试诊断输出总开关
        if self._ready or not self._model_path:
            return
        try:
            import live2d.v3 as l2d
            self._live2d = l2d
            global _global_l2d_inited
            if not _global_l2d_inited:
                l2d.init()
                # 压住原生 C 库的 Info 刷屏（`motion priority is too low.`、`[CSM][I]`、
                # `Clear all expressions` 直接写 stderr，不走 Python logging，拦不住只能降级）。
                # setLogLevel 只按级别过滤，拦不住全部 Info；enableLog(False) 是真正总开关。
                try:
                    l2d.enableLog(False)
                    _l2d_log=True
                except Exception:
                    _l2d_log=False
                finally:
                    try:
                        l2d.setLogLevel(1)
                    except Exception:
                        logger.debug("Live2DRenderer: 非致命异常(已静默吞掉)", exc_info=True)
                logger.info("Live2DRenderer: enableLog(False) → 原生库日志已关闭%s", "" if _l2d_log else "(API 不可用，跳过)")
                _global_l2d_inited = True

            # GL 可用性检查：不 import PyOpenGL 的 GL（它与 live2d-py 的 glad 加载的
            # GL 函数可能冲突，污染函数指针导致模型绘制失败——纯测试从不 import
            # PyOpenGL 能正常画出）。直接尝试 glInit，失败则回退 sprite。
            try:
                import live2d.v3
                l2d.glInit()
                logger.info("Live2DRenderer: glInit OK（未依赖 PyOpenGL）")
            except Exception as e:
                logger.warning("Live2DRenderer: glInit 失败，回退 sprite 渲染: %s", e)
                self._ready = False
                self._model = None
                return

            # 用 LAppModel（高层封装，内部管理 dt/投影/自动眨眼/呼吸）。
            # 最小测试验证 LAppModel 能正确绘制（底层 Model 画不出来）。
            model = l2d.LAppModel()
            model.LoadModelJson(self._model_path)

            self._model = model
            # W4（2026-09-14）：换模型后白名单缓存必须失效，
            # 否则上一模型的参数集会串到新模型（新模型可能没有那些参数）。
            self._effective_wl_cache = None
            # 模型体检：换模型自动检测参数覆盖，是「缺参数→表情/动作静默失效」
            # 的第一道防线（live2d_renderer 换模型有 52 处裸 except:pass，缺这步会盲人摸象）。
            try:
                from avatar.model_health import generate_report, format_report_human
                self._health_report = generate_report(model)
                logger.info("Live2DRenderer: 模型体检\n%s", format_report_human(self._health_report))
            except Exception as e:
                self._health_report = None
                logger.warning("Live2DRenderer: 模型体检失败(非致命): %s", e)
            # LAppModel.LoadModelJson 内部已自动 CreateRenderer
            if not self._debug_minimal:
                model.SetAutoBlinkEnable(True)
                model.SetAutoBreathEnable(True)
            else:
                logger.info("Live2DRenderer: L2D_DEBUG_MINIMAL=1 跳过自动眨眼/呼吸")

            # 关键：live2d 文档要求初次加载必须 Resize(宽,高)，否则模型不显示。
            # 用 GLCharWidget 实际尺寸（而非硬编码默认 220），与纯测试一致。
            # 若 _gl_w 未设置（initializeGL 早于 resizeGL），回退到 widget 实际 size。
            try:
                gl_w = int(getattr(self, "_gl_w", 0) or 0)
                gl_h = int(getattr(self, "_gl_h", 0) or 0)
                if not gl_w or not gl_h:
                    cl = getattr(self, "char_label", None)
                    if cl is not None:
                        gl_w = cl.width() or 220
                        gl_h = cl.height() or 260
                model.Resize(gl_w, gl_h)
                logger.info("Live2DRenderer: Resize(%s,%s)", gl_w, gl_h)
            except Exception as e:
                logger.warning("Live2DRenderer: Resize 失败: %s", e)

            # 显式设置初始缩放（用像素画布尺寸，与最小测试一致）。
            # 不设置则模型保持默认 scale，可能过大/过小画不出来。
            try:
                cw_px, ch_px = model.GetCanvasSizePixel()
                if cw_px and ch_px:
                    gl_w = int(getattr(self, "_gl_w", 0) or 0)
                    gl_h = int(getattr(self, "_gl_h", 0) or 0)
                    if not gl_w or not gl_h:
                        cl = getattr(self, "char_label", None)
                        if cl is not None:
                            gl_w = cl.width() or 220
                            gl_h = cl.height() or 260
                    # SetScale 语义：1.0=画布适配窗口。fit 直接取用户缩放系数。
                    scale = self._fit_scale
                    model.SetScale(scale)
                    logger.info("Live2DRenderer: 初始缩放 scale=%.3f", scale)
            except Exception as e:
                logger.warning("Live2DRenderer: 初始缩放失败: %s", e)

            # 收集可用表情/动作组，用于情绪映射
            self._expression_names = []
            self._motion_groups = {}
            self._motion_files: list[str] = []      # 空组下的 motion 文件名列表
            self._motion_group_name: str = ""       # 实际使用的 motion 组名
            # 卡手势防御：模型 motion 全是 Loop=True（永不 finished），非 idle 手势一旦
            # 播放，IsMotionFinished 永不 True → idle 永不重启 → 卡在最后动作上。
            # 这里记录当前 motion 是否常态 idle + 起始时间，播满 GESTURE_TIMEOUT 强制回 idle。
            self._motion_is_idle = True
            self._motion_started_at = time.monotonic()
            # emotion → motion 上次播放时间，防止同一情绪手势被连续触发、看起来“永久卡死”
            self._emotion_motion_cooldown: dict[str, float] = {}
            if not self._debug_minimal:
                # wrapper 0.7.0.4 的真实 API 是 GetExpressionIds（pyi 写的 GetExpressions 不存在），
                # 且 GetExpressionIds 在初始化早期可能抛异常——都 fallback 到 model3.json 解析。
                self._expression_names = []
                try:
                    ids = model.GetExpressionIds()
                    if ids:
                        self._expression_names = [
                            n for n in ids
                            if not any(k in str(n).lower() for k in self._IGNORED_EXPRESSIONS)
                        ]
                except Exception:
                    logger.debug("Live2DRenderer: 非致命异常(已静默吞掉)", exc_info=True)
                if not self._expression_names:
                    import json as _json
                    try:
                        with open(self._model_path, encoding="utf-8") as _f:
                            _m3 = _json.load(_f)
                        exprs = _m3.get("FileReferences", {}).get("Expressions", []) or []
                        names = []
                        for _e in exprs:
                            nm = _e.get("Name") or os.path.splitext(os.path.basename(_e.get("File", "")))[0]
                            if nm and not any(k in str(nm).lower() for k in self._IGNORED_EXPRESSIONS):
                                names.append(nm)
                        # 去重保序
                        self._expression_names = list(dict.fromkeys(names))
                        if names:
                            logger.info("Live2DRenderer: 从 model3.json 解析 %d 个表情名: %s", len(names), names)
                    except Exception as _e:
                        logger.warning("Live2DRenderer: 解析表情失败: %s", _e)
                try:
                    if hasattr(model, "GetMotionGroups"):
                        groups = model.GetMotionGroups()
                        # 保留所有组名（含空串）。此模型 lafei.model3.json 把动作都放在
                        # 空字符串组 "" 下（idle/login/touch_* 等 14 个），空串是合法组名。
                        # 之前过滤空串导致 _motion_groups 为空，_start_idle 退回用 "Idle"
                        # 却找不到，待机动画不启动。这里保留空串组。
                        self._motion_groups = {g: [] for g in (groups or []) if g is not None}
                    else:
                        self._motion_groups = dict(model.GetMotions() or {})
                except Exception:
                    self._motion_groups = {}

                # 建 motion 文件索引（按文件名关键词匹配播放，因为组名是空串匹配不上）
                try:
                    motions = model.GetMotions()
                    self._motion_group_name = next(
                        (g for g in motions if g), next(iter(motions), ""))
                    self._motion_files = [
                        (m.get("File", "") if isinstance(m, dict) else "")
                        for m in motions.get(self._motion_group_name, [])
                    ]
                except Exception:
                    self._motion_files = []

                # 清除默认表情（模型常带作者水印/LOGO 表情，默认显示会遮挡角色）
                try:
                    model.ResetExpressions()
                except Exception:
                    logger.debug("Live2DRenderer: 非致命异常(已静默吞掉)", exc_info=True)
                # 缓存水印参数索引，每帧强制关闭（Param137=水印）
                self._cache_watermark_index()
                # 起始待机动作
                self._start_idle()

            self._ready = True
            logger.info(
                "Live2DRenderer: 模型加载成功 (expressions=%d, motion_groups=%s)",
                len(self._expression_names), list(self._motion_groups.keys()),
            )
            # 模型就绪后延迟测量角色 bbox，请求窗口自动贴合模型大小
            # （等 draw 首帧跑完，HitDrawable 才有有效状态）
            from PySide6.QtCore import QTimer
            QTimer.singleShot(300, self._fit_window_to_model)
        except ImportError as e:
            # 明确提示：live2d-py 未安装
            logger.error(
                "Live2DRenderer: live2d-py 未安装（%s）。\n"
                "请运行: pip install live2d-py\n"
                "或检查 requirements.txt 是否包含 live2d-py。",
                e,
            )
            self._ready = False
            self._model = None
        except Exception as e:
            logger.error("Live2DRenderer: 模型加载失败（角色区域将透明）: %s", e)
            self._ready = False
            self._model = None

    def _scan_bbox_adaptive(self, frames: int = 3, coarse_step: int = 32, fine_step: int = 6) -> tuple | None:
        """自适应两段式扫描：每帧先粗扫定位，再在同一帧内四带精扫，帧间并集。

        关键设计：粗扫与精扫必须在**同一帧**（同一摆动相位）执行——
        若分开跑，band 要同时覆盖粗步长误差 + 帧间摆动差（可达 56px+），盖不住。
        每帧成本 ≈ 粗扫(≈255 次) + 四带精扫(≈1.5k 次) ≈ 1.8k 次，3 帧 ≈ 5.4k 次，
        对比原 3 帧全视口 step=3 的 8 万次，降一个量级（约 15x）。
        返回 (min_x, min_y, max_x, max_y)，未命中返回 None。
        """
        mm = getattr(self._model, "_model", None) or self._model
        gl_w = int(getattr(self, "_gl_w", 0))
        gl_h = int(getattr(self, "_gl_h", 0))
        if gl_w <= 0 or gl_h <= 0:
            return None
        min_x, min_y, max_x, max_y = gl_w, gl_h, -1, -1
        hit_any_frame = False
        for _ in range(frames):
            try:
                self._model.Update()
                self._model.Draw()
            except Exception:
                logger.debug("Live2DRenderer: 非致命异常(已静默吞掉)", exc_info=True)

            def _hit(x: int, y: int) -> bool:
                try:
                    return bool(mm.HitDrawable(float(x), float(y)))
                except Exception:
                    return False

            # ① 粗扫本帧：大步长定位大致 bbox
            c_min_x, c_min_y, c_max_x, c_max_y = gl_w, gl_h, -1, -1
            for y in range(0, gl_h + 1, coarse_step):
                for x in range(0, gl_w + 1, coarse_step):
                    if _hit(x, y):
                        if x < c_min_x: c_min_x = x
                        if x > c_max_x: c_max_x = x
                        if y < c_min_y: c_min_y = y
                        if y > c_max_y: c_max_y = y
            if c_max_x < 0:
                continue  # 本帧未命中，等下一帧
            hit_any_frame = True
            # ② 同帧四带精扫：band 只需覆盖粗步长误差（同帧无摆动差）
            band = coarse_step + 8  # 40
            # 左带 / 右带（全高）
            for x in range(max(0, c_min_x - band), min(gl_w, c_min_x + band) + 1, fine_step):
                for y in range(0, gl_h + 1, fine_step):
                    if _hit(x, y):
                        if x < min_x: min_x = x
                        if x > max_x: max_x = x
                        if y < min_y: min_y = y
                        if y > max_y: max_y = y
            for x in range(max(0, c_max_x - band), min(gl_w, c_max_x + band) + 1, fine_step):
                for y in range(0, gl_h + 1, fine_step):
                    if _hit(x, y):
                        if x < min_x: min_x = x
                        if x > max_x: max_x = x
                        if y < min_y: min_y = y
                        if y > max_y: max_y = y
            # 上带 / 下带（角色宽度范围，含外扩）
            x_lo = max(0, c_min_x - band)
            x_hi = min(gl_w, c_max_x + band)
            for y in range(max(0, c_min_y - band), min(gl_h, c_min_y + band) + 1, fine_step):
                for x in range(x_lo, x_hi + 1, fine_step):
                    if _hit(x, y):
                        if x < min_x: min_x = x
                        if x > max_x: max_x = x
                        if y < min_y: min_y = y
                        if y > max_y: max_y = y
            for y in range(max(0, c_max_y - band), min(gl_h, c_max_y + band) + 1, fine_step):
                for x in range(x_lo, x_hi + 1, fine_step):
                    if _hit(x, y):
                        if x < min_x: min_x = x
                        if x > max_x: max_x = x
                        if y < min_y: min_y = y
                        if y > max_y: max_y = y
        if not hit_any_frame or max_x < 0:
            return None
        return (min_x, min_y, max_x, max_y)

    def _fit_window_to_model(self) -> None:
        """测量角色本体 bbox，请求窗口 resize 到模型大小（去掉多余透明边距）。

        HitDrawable 在 draw 后才有有效状态。原实现 3 帧全视口 STEP=3 扫描
        （458x520 视口约 8 万次命中检测，耗时 ≈ 70s）。
        现改为自适应两段式（每帧：粗扫定位 + 同帧四带精扫，帧间并集），
        总调用降一个量级（≈5.4k 次，约 15x），耗时降到秒级。
        """
        if not self._model or not self._ready:
            logger.debug("Live2DRenderer: fit 跳过（模型未就绪）")
            return
        try:
            mm = getattr(self._model, "_model", None) or self._model
            if not hasattr(mm, "HitDrawable"):
                logger.debug("Live2DRenderer: fit 跳过（无 HitDrawable）")
                return
            gl_w = int(getattr(self, "_gl_w", 0) or self._renderer_w() or 0)
            gl_h = int(getattr(self, "_gl_h", 0) or self._renderer_h() or 0)
            if gl_w <= 0 or gl_h <= 0:
                logger.debug("Live2DRenderer: fit 跳过（视口无效 %dx%d）", gl_w, gl_h)
                return
            logger.info("Live2DRenderer: fit 开始 gl=%dx%d", gl_w, gl_h)
            # 2026-09-11: 扫描结果缓存。
            #
            # 扫描（HitDrawable 网格命中检测）实测耗时 21.8s，但输入完全确定：
            #   模型文件 + 缩放系数 + 视口尺寸 → bbox
            # 桌宠每次启动的视口尺寸都相同（config.window 不随贴合写回），
            # 于是每次都在重算同一个结果。
            #
            # 键里放的是**决定扫描结果的每一项**：少放一项就会拿旧值去套新模型，
            # 那比慢 20 秒更糟。
            _ck = fit_cache.make_key(
                self._model_path, self._fit_scale,
                getattr(self, "_fit_scale_x", 1.0), gl_w, gl_h,
            )
            bbox = fit_cache.load(_ck, gl_w, gl_h)
            if bbox is not None:
                logger.info("Live2DRenderer: [fit-cache] 贴合命中缓存，跳过扫描")
                min_x, min_y, max_x, max_y = bbox
                bw = max_x - min_x + 1
                bh = max_y - min_y + 1
            else:
                t0 = time.time()
                bbox = self._scan_bbox_adaptive(
                    frames=3, coarse_step=32, fine_step=6)
                elapsed = time.time() - t0
                if bbox is None:
                    logger.info(
                        "Live2DRenderer: 未命中角色像素（扫描 %.2fs），跳过窗口贴合",
                        elapsed,
                    )
                    return
                min_x, min_y, max_x, max_y = bbox
                bw = max_x - min_x + 1
                bh = max_y - min_y + 1
                logger.info(
                    "Live2DRenderer: [fit] 扫描耗时 %.2fs (bbox=%dx%d)，已缓存",
                    elapsed, bw, bh,
                )
                # 只在扫描成功时写缓存：扫不到角色像素那一刻的"结果"
                # （其实是没结果）一旦固化，下次启动会直接跳过贴合。
                fit_cache.save(_ck, bbox)
            # 居中补偿：模型在画布里固位偏右（moc3 留白），用 SetOffsetX 平移居中。
            # 不在这里直接设——_fit_window_to_model 之后窗口贴合成新视口会触发 SetScale，
            # 与 offset 的时序交互在真实多并发环境有过闪退。改为缓存 offx，统一由
            # _recompute_fit 在每次 SetScale 之后应用（保证 SetScale → SetOffsetX 顺序，
            # 实测该顺序稳定不闪退）。
            try:
                center_x = (min_x + max_x) / 2.0
                offx = (gl_w / 2.0 - center_x) / (0.591 * gl_w)
                offx = max(-0.9, min(0.9, offx))
                self._center_offset_x = offx
                logger.info(
                    "Live2DRenderer: 居中补偿 中心x=%.0f→%.0f offx=%+.3f",
                    center_x, gl_w / 2.0, offx,
                )
            except Exception as _e:
                logger.debug("居中补偿失败（跳过）: %s", _e)
            # 边距：给 idle 摆幅留余量，同时避免窗口被 fit 成瘦高异形。
            # 旧实现用 pad_bottom=200 + 大幅 offsetY 上移来防截脚，结果头顶被推出窗口、
            # 比例瘦高。新策略：**不主动上移模型**，靠【窗口上下都留足边距】来包容
            # 头顶摆幅 + 裙摆/脚。
            # 关键：hit-bbox 不含脚/裙摆（这些区域通常在 HitDrawable 命中区外），
            # 所以底部 pad_bottom 必须大到够装下"看不见的脚"，否则用户站在小窗口
            # 里看不到脚。顶部同理——waving/happy 动作的头发/角度变化会暂时向上超出
            # bbox，需留余量；但**绝不能用 SetOffsetY 上移**模型（等价把头顶推出）。
            # pad_w：横向留余量给左右摆动的手/头发（hit-bbox 不含这些）。
            # 2026-08-20 修复"动作被窗口截断"：静态 bbox 不含动作摆幅（举葱/挥手/
            # 比心手部都会伸出 bbox 外），边距再加大一档，并**禁用 offsetY 上移**
            # （上移会把头顶/举葱动作推出窗口——这是用户反馈双马尾顶被裁的根因）。
            pad_w = max(24, int(bw * 0.55))
            pad_h = max(32, int(bh * 0.25))
            pad_bottom = max(200, int(bh * 0.5))  # P7: 从 120/0.35 提到 200/0.5，给脚留空间
            target_w = max(40, bw + pad_w)
            target_h = max(40, bh + pad_h + pad_bottom)

            # 保持窗口尺寸由 bbox + 边距决定。
            # 之前用 canvas-based 约束（ch_px * fit_scale * 0.82）会把窗口撑到
            # 数千像素（miku canvas 8888px → 窗口 7288px，比屏幕还大）。
            # 实际上 SetScale=1.0 时画布已经适配窗口，canvas 像素只是模型原始大小，
            # 不应作为最小窗口的约束。这里只保留 bbox + 边距路径。
            #
            # 但 miku 这种"hit-bbox 不含脚/发顶"的模型，bh 会偏小、窗口可能过窄。
            # 给一个宽高比的合理下限：宽 ≥ 高 / 2.5（避免瘦高）；高 ≥ 宽 * 2.0（避免扁宽）。
            try:
                if target_h > 0 and target_w / target_h < 0.4:
                    target_w = max(target_w, int(target_h * 0.4))
                if target_w > 0 and target_h / target_w < 1.5:
                    target_h = max(target_h, int(target_w * 1.5))
            except Exception:
                logger.debug("Live2DRenderer: 非致命异常(已静默吞掉)", exc_info=True)

            # 居中偏移：用户物理不主动改变模型 Y 位置（固定 0），避免上移截头顶。
            # 【2026-08-20 用户反馈双马尾顶被裁】【诊断】之前 _fit_offset_y 上移
            # 0.10~0.20 的 SetOffsetY 是把模型在画布里向上推——效果恰恰相反：本意
            # 是"给脚留底边"，结果把头顶推出窗口。正确做法是在 _recompute_fit 时
            # 取 **pad_bottom** 多大于顶部 offset 自然就能装下脚，头顶不会被推。
            # 所以此处设置 _fit_offset_y = 0，fit 路径永远不动模型的 Y 位置。
            self._fit_offset_y = 0.0
            logger.info(
                "Live2DRenderer: 角色 bbox=%dx%d (偏移 %d,%d)，窗口贴合到 %dx%d (+%dpx 边距, 上移 offsetY=%.3f)",
                bw, bh, min_x, min_y, target_w, target_h, pad_w, self._fit_offset_y,
            )
            parent = getattr(self, "_parent", None)
            fit_win = getattr(parent, "fit_window_to_model", None)
            if callable(fit_win):
                fit_win(target_w, target_h)
            # 保险：若窗口尺寸未变（on_resize 不触发），主动应用一次居中
            if getattr(self, "_center_offset_x", 0.0):
                try:
                    self._recompute_fit()
                except Exception:
                    logger.debug("Live2DRenderer: 非致命异常(已静默吞掉)", exc_info=True)
        except Exception as e:
            logger.warning("Live2DRenderer: 窗口贴合失败: %s", e)

    def _renderer_w(self):
        try:
            cl = getattr(self, "char_label", None)
            return cl.width() if cl is not None else 0
        except Exception:
            return 0

    def _renderer_h(self):
        try:
            cl = getattr(self, "char_label", None)
            return cl.height() if cl is not None else 0
        except Exception:
            return 0

    def on_resize(self, w: int, h: int) -> None:
        self._gl_w = w
        self._gl_h = h
        self._recompute_fit()

    def _recompute_fit(self) -> None:
        """根据视口与模型像素画布尺寸计算缩放/偏移（best-effort，可能需按模型微调）。"""
        if not self._model or not hasattr(self, "_gl_w"):
            return
        try:
            # 用像素画布尺寸（GetCanvasSizePixel），而非逻辑单位（GetCanvasSize）。
            # 逻辑单位（如 13.6）乘以 PixelsPerUnit(88.24) 才是真实像素。
            # 若用逻辑单位算 SetScale，会放大 ~88 倍导致角色超出视口不可见。
            cw_px, ch_px = self._model.GetCanvasSizePixel()
            if not cw_px or not ch_px:
                # 回退到逻辑单位 × PixelsPerUnit
                ppu = self._model.GetPixelsPerUnit() or 1.0
                cw_log, ch_log = self._model.GetCanvasSize()
                cw_px, ch_px = cw_log * ppu, ch_log * ppu
            if not cw_px or not ch_px:
                return
            # SetScale 语义：1.0 = 角色画布适配窗口（实测 scale=1.0 时角色占满窗口 100%×95%）。
            # 所以 fit 直接取用户配置的缩放系数（pet.json live2d.scale）：
            #   1.0 = 本体填满窗口（无背景）
            #   <1  = 缩小留白
            #   >1  = 放大裁剪（特写）
            fit = self._fit_scale
            # 横向比例系数：
            # - pet.json 显式配置 scale_x 时用它
            # - 否则超高画布（如 miku 3500x8888）等比缩放会因画布超高导致角色横向偏细，
            #   自动按画布比例补正（缓存后稳定，不再每次重算）。
            # 之前“偏移不稳定”的真凶是 offset 时序 bug（已移除），不是 scale_x 本身；
            # scale_x 是确定性缩放，恢复后模型稳定不跳。
            sx = getattr(self, "_fit_scale_x", 1.0)
            if sx == 1.0 and cw_px and ch_px and cw_px / ch_px < 0.7:
                try:
                    ratio = cw_px / ch_px
                    # 参考比 0.48：补到接近人体但不过度。之前 0.55 会让偏右的 miku
                    # 横向放得过大，右边贴到视口边缘被裁。0.48 → sx≈1.22，模型不瘦、
                    # 右边留一点余量不裁。
                    sx = round(max(1.0, min(0.48 / ratio, 1.5)), 3)
                    self._fit_scale_x = sx
                except Exception:
                    sx = 1.0
            if sx != 1.0:
                try:
                    self._model.SetScaleX(fit * sx)
                    self._model.SetScaleY(fit)
                except Exception:
                    # 老版本 wrapper 可能没有 SetScaleX；回退等比
                    self._model.SetScale(fit)
            else:
                self._model.SetScale(fit)
            # 居中：SetScale 之后应用水平偏移（保证 SetScale→SetOffsetX 顺序，实测稳定）。
            # 水平 = 自动居中补偿(_center_offset_x) + pet.json 的 live2d.offset[0]，二者叠加。
            _offx = getattr(self, "_center_offset_x", 0.0)
            _os = getattr(self, "_offset_scale", (0.0, 0.0))
            try:
                _ox = float(_offx) + float(_os[0] if len(_os) > 0 else 0.0)
                self._model.SetOffsetX(_ox)
            except Exception:
                logger.debug("Live2DRenderer: 非致命异常(已静默吞掉)", exc_info=True)
            # 垂直偏移：pet.json 的 live2d.offset[1]（脚贴地微调）+ fit 自动上移补偿。
            # Live2D 模型坐标 Y 轴向上，SetOffsetY 正值=上移、负值=下移。
            # 第二来源 _fit_offset_y：截脚修复——HitDrawable 命中区只覆盖上半身，bbox 不含脚，
            # 仅加窗口高度会把脚推出可见区；故把模型上移 _fit_offset_y（见 _fit_window_to_model），
            # 等效"顶部留白、脚贴窗口下缘"，让脚真正显示在窗口内。
            # 二者叠加：用户自定义 offset[1]（通常微调用，正值上移）与自动补偿合并。
            try:
                _oy_user = float(_os[1] if len(_os) > 1 else 0.0)
                _oy_fit = getattr(self, "_fit_offset_y", 0.0) or 0.0
                _oy = _oy_user + _oy_fit
                if _oy:
                    self._model.SetOffsetY(_oy)
            except Exception:
                logger.debug("Live2DRenderer: 非致命异常(已静默吞掉)", exc_info=True)
            logger.debug("Live2DRenderer: 缩放 fit=%.3f sx=%.3f offx=%.3f offy=%.3f (gl=%sx%s, canvas_px=%sx%s)",
                         fit, sx, float(_offx) + float(_os[0] if len(_os) > 0 else 0.0),
                         float(_os[1] if len(_os) > 1 else 0.0),
                         self._gl_w, self._gl_h, cw_px, ch_px)
        except Exception as e:
            logger.warning("Live2DRenderer: 缩放计算失败: %s", e)

    def draw(self) -> None:
        """由 GLCharWidget.paintGL 调用：每帧更新并绘制模型。T2-1 管线化集成。"""
        if not self._live2d or not self._model:
            return
        # 诊断：确认 draw 是否被调用、模型是否就绪（仅首次打印）
        if not getattr(self, "_draw_diag_logged", False):
            self._draw_diag_logged = True
            try:
                cw = ch = 0
                try:
                    cw, ch = self._model.GetCanvasSize()
                except Exception:
                    logger.debug("Live2DRenderer: 非致命异常(已静默吞掉)", exc_info=True)
                cw_px = ch_px = 0
                try:
                    cw_px, ch_px = self._model.GetCanvasSizePixel()
                except Exception:
                    logger.debug("Live2DRenderer: 非致命异常(已静默吞掉)", exc_info=True)
                logger.info(
                    "Live2DRenderer: draw 首帧就绪 model=%s canvas=(%s,%s)px=(%s,%s) gl=%sx%s scale=%.3f",
                    self._ready, cw, ch, cw_px, ch_px,
                    getattr(self, "_gl_w", "?"), getattr(self, "_gl_h", "?"), self._fit_scale,
                )
                # 离屏截图诊断（L2D_DEBUG=1 才启用）：保存 GL 内容，确认模型是否真的画出来
                if getattr(self, "_debug", False):
                    try:
                        self.char_label.grabFramebuffer().save(
                            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                         "logs", "l2d_diag.png"))
                        logger.info("Live2DRenderer: 已保存离屏截图 logs/l2d_diag.png")
                    except Exception as e:
                        logger.warning("Live2DRenderer: 离屏截图失败: %s", e)
            except Exception:
                logger.debug("Live2DRenderer: 非致命异常(已静默吞掉)", exc_info=True)
        try:
            l2d = self._live2d
            # 清除画布（live2d-py 0.7.0.4 的 clearBuffer 为无参调用）
            l2d.clearBuffer()
        except Exception as e:
            logger.warning("Live2DRenderer.clearBuffer 异常: %s", e)

        # L2D_DEBUG_MINIMAL=1 时保留最小路径（仅 _frame_update + Draw），用于二分定位 GL 问题。
        if getattr(self, "_debug_minimal", False):
            try:
                self._frame_update()
            except Exception as e:
                logger.warning("Live2DRenderer._frame_update 异常: %s", e)
            try:
                self._model.Draw()
            except Exception as e:
                logger.warning("Live2DRenderer.Draw 异常: %s", e)
            return

        # 完整帧更新：绕过 live2d-py 0.7.0.4 wrapper 残缺的 Update()（motion/blink/呼吸全被注释），
        # 直接驱动 C++ Model 的完整更新序列（UpdateMotion → Blink → Breath → Physics → Pose）。
        try:
            self._frame_update()
        except Exception as e:
            logger.warning("Live2DRenderer._frame_update 异常: %s", e)

        # T2-1: 每帧参数叠加 + 绘制全部收敛到 FramePipeline（各处理器独立 try/except）。
        # 序列：idle 循环 → 手势超时 → 自动动作 → 表情超时 → 视线 → 口型 → 程序化表情 →
        # idle 微摆 → 绘制。手动参数写入在 _frame_update 之后，避免被 motion 曲线覆盖；
        # 新触发的 motion 从下一帧的 _frame_update 开始应用曲线（相比旧实现在本帧立即应用，
        # 只差 1 帧延迟，视觉上无影响）。
        self._pipeline.run(self._model, self)

    def _draw_model(self, model) -> None:
        """T2-1: 绘制模型（供 FramePipeline.DrawProcessor 调用）。

        抽成单独方法以便管线化；内部仍用 try/except 降级，缺模型时透明不崩。
        """
        if model is None:
            return
        model.Draw()

    def _frame_update(self) -> None:
        """完整 Live2D 帧更新：直接驱动 C++ Model（绕过 wrapper 残缺 Update）。

        官方 LAppModel::Update 的标准序列（live2d-py 0.7.0.4 的 Python Update() 把它们全注释了）：
          Update(dt) → LoadParameters → UpdateMotion(dt) → [无 motion 时 UpdateBlink]
          → UpdateExpression → UpdateDrag → UpdateBreath → UpdatePhysics → UpdatePose
          → SaveParameters
        """
        mm = getattr(self._model, "_model", None)
        if mm is None:
            # 回退：wrapper 的 Update（虽然残缺，至少不崩）
            self._model.Update()
            return
        now = time.monotonic()
        dt = min(now - getattr(self, "_frame_last_t", now), 0.1)
        self._frame_last_t = now

        # 帧内各步骤：单个失败不拖垮其余步骤，但绝不静默。
        # 2026-09-10 修：原为 10 个独立 try/except + logger.debug。
        # 生产日志级别 INFO 下 → 每秒最多 600 次静默失败，用户看到的是
        # 「模型还在画，但不动了」——最难排查的一种失败形态。
        self._frame_step("Update", mm.Update, dt)
        self._frame_step("LoadParameters", mm.LoadParameters)
        motion_updated = bool(self._frame_step("UpdateMotion", mm.UpdateMotion, dt))
        if not motion_updated:
            self._frame_step("UpdateBlink", mm.UpdateBlink, dt)
        self._frame_step("UpdateExpression", mm.UpdateExpression, dt)
        # 水印抑制：模型自带 Param137(水印) 表情默认开，表情更新后强制置 0
        self._frame_step("SuppressWatermark", self._suppress_watermark, mm)
        self._frame_step("UpdateDrag", mm.UpdateDrag, dt)
        self._frame_step("UpdateBreath", mm.UpdateBreath, dt)
        self._frame_step("UpdatePhysics", mm.UpdatePhysics, dt)
        self._frame_step("UpdatePose", mm.UpdatePose, dt)
        self._frame_step("SaveParameters", mm.SaveParameters)

    def _note_frame_failure(self, name: str, e: Exception) -> None:
        """记一次帧内失败（去重 + 置降级状态）。

        帧内失败的处理原则（2026-09-10）：
          不静默（用户能看到「模型画着但不动」，日志却一无所有）
          不刷屏（60fps 下逐帧记日志会爆）
          可查询（frame_degraded / frame_failed_steps）
        所以同名只报一次，恢复时再报一次。
        """
        failed = getattr(self, "_frame_failed_steps", None)
        if failed is None:
            failed = self._frame_failed_steps = set()
        if name not in failed:
            failed.add(name)
            logger.warning(
                "Live2DRenderer: %s 失败，已降级（同类失败后续静音，恢复时会再报）: %s",
                name, e,
            )
        self._frame_degraded = True

    def _frame_step(self, name: str, fn, *args):
        """执行一个帧渲染步骤。失败 → 记一次 warning + 进降级状态，不中断其余步骤。

        失败状态可查：`frame_degraded` / `frame_failed_steps()`。
        """
        try:
            result = fn(*args)
        except Exception as e:
            self._note_frame_failure(name, e)
            return None
        failed = getattr(self, "_frame_failed_steps", None)
        if failed and name in failed:
            failed.discard(name)
            logger.info("Live2DRenderer: 帧步骤 %s 已恢复", name)
            if not failed:
                self._frame_degraded = False
        return result

    @property
    def frame_degraded(self) -> bool:
        """帧管线是否处于降级状态（有步骤持续失败）。供诊断 / UI 查询。"""
        return bool(getattr(self, "_frame_degraded", False))

    def frame_failed_steps(self) -> list[str]:
        """当前持续失败的帧步骤名（空 = 全部正常）。"""
        return sorted(getattr(self, "_frame_failed_steps", set()))

    # ── 内部：参数驱动 ──

    def _cache_watermark_index(self) -> None:
        """缓存水印部件索引。

        多数免费模型把作者版权水印做成独立部件：
        cdi3.json 里 Part 有 Id（如 Part18）与 Name（如 水印.psd）两个字段，
        GetPartIds() 只暴露 Id，中文名在 cdi3 里——所以先读 cdi3 做 Id→Name 映射，
        再匹配“水印/watermark/logo/版权”关键词，记录索引，每帧强制透明度 0。
        这是作者文档所说的“水印按键可在设置表情中关闭”的真正实现：
        水印不是一个参数，是一个可见部件。
        """
        self._watermark_part_idx: list[int] = []
        self._watermark_idx = -1   # 兼容旧字段（参数索引，已弃用）
        if not self._model:
            return
        try:
            parts = self._model.GetPartIds()
            if not parts:
                return
            # 从 model3.json 同目录的 cdi3.json 读 Id→Name
            wm_ids = set()
            import json as _json
            base = os.path.dirname(self._model_path or "")
            cdi3 = os.path.join(base, os.path.splitext(os.path.basename(self._model_path or ""))[0] + ".cdi3.json")
            if not os.path.exists(cdi3):
                # 回退：同目录里唯一的 cdi3
                _cands = [f for f in os.listdir(base) if f.endswith(".cdi3.json")] if base and os.path.isdir(base) else []
                if _cands:
                    cdi3 = os.path.join(base, _cands[0])
            if os.path.exists(cdi3):
                try:
                    _c = _json.load(open(cdi3, encoding="utf-8"))
                    wm_kw = ("水印", "watermark", "logo", "版权")
                    for _p in _c.get("Parts", []):
                        if any(k in str(_p.get("Name", "")).lower() for k in wm_kw):
                            wm_ids.add(_p.get("Id"))
                except Exception:
                    wm_ids = set()
            if wm_ids:
                self._watermark_part_idx = [i for i, p in enumerate(parts) if p in wm_ids]
                logger.info(
                    "Live2DRenderer: 检测到水印部件 %s，每帧强制隐藏",
                    [parts[i] for i in self._watermark_part_idx],
                )
        except Exception:
            self._watermark_part_idx = []

    def _suppress_watermark(self, mm=None) -> None:
        """每帧强制隐藏水印部件（Part 透明度=0）。

        作者版权水印默认显示；via 部件透明度直接隐藏，
        比参数方案可靠（水印部件不绑参数，Param137 只是装饰）。
        """
        idxs = getattr(self, "_watermark_part_idx", None)
        if not idxs:
            return
        target = mm or getattr(self._model, "_model", None) or self._model
        if target is None:
            return
        for i in idxs:
            try:
                target.SetPartOpacity(i, 0.0)
            except Exception as e:
                # 失败 = 模型作者版权水印直接显在屏幕上，用户能看到
                self._note_frame_failure("SuppressWatermarkPart", e)

    def _update_gaze_params(self) -> None:
        if not self._model:
            return
        P = self._live2d.StandardParams
        # 平滑插值
        s = 0.18
        self._gaze_cur_angle_x += (self._gaze_target_angle_x - self._gaze_cur_angle_x) * s
        self._gaze_cur_angle_y += (self._gaze_target_angle_y - self._gaze_cur_angle_y) * s
        self._gaze_cur_ball_x += (self._gaze_target_ball_x - self._gaze_cur_ball_x) * s
        self._gaze_cur_ball_y += (self._gaze_target_ball_y - self._gaze_cur_ball_y) * s
        try:
            # weight 用 1.0：之前 0.3 只有 30% 生效，head/eye 摆动微弱到用户看不到
            # （"设置面板有视线跟随但没效果"的根因之一）。
            self._model.SetParameterValue(P.ParamAngleX, self._gaze_cur_angle_x, 1.0)
            self._model.SetParameterValue(P.ParamAngleY, self._gaze_cur_angle_y, 1.0)
            self._model.SetParameterValue(P.ParamEyeBallX, self._gaze_cur_ball_x, 1.0)
            self._model.SetParameterValue(P.ParamEyeBallY, self._gaze_cur_ball_y, 1.0)
        except Exception as e:
            # 失败 = 视觉不跟随鼠标，用户 100% 能感知，不能静默
            self._note_frame_failure("GazeParams", e)

    def _slew_lip_open(self, target: float) -> float:
        """音素级开口度过一道起音/收音（对齐 AgentAtelierR 40ms / 90ms）。

        为什么只在这条路：`_update_mouth` 里振幅路径**本来就有**非对称包络
        （``k = 0.55 if level > env else 0.18``，起快落慢）；音素路径是裸阶梯，
        而且写入时 weight=1.0（SDK 不插值），所以 0.95 的 /a/ 接到 0.30 的 /i/
        会一帧硬切。

        与帧率无关（走 core.lip_sync.slew 的指数逆近）。首次调用直接采用目标值
        （不从 0 慢慢爬，避免开口迟一步）。
        """
        from core.lip_sync import slew

        now = time.monotonic()
        last = getattr(self, "_mouth_slew_at", 0.0)
        dt = 0.0 if last <= 0.0 else min(0.5, max(0.0, now - last))
        self._mouth_slew_at = now
        cur = getattr(self, "_mouth_slew_value", None)
        if cur is None:
            self._mouth_slew_value = float(target)
            return float(target)
        out = slew(cur, target, dt)
        self._mouth_slew_value = out
        return out

    def _update_mouth(self) -> None:
        """说话口型。三级优先（LIP-1 2026-09-14）：

        1. **音素级**：有口型时间轴 + 播放位置 → 嘴型跟**具体发音**
           （`/a/` 张大、`/i/` 咧嘴、`/m/` 闭唇）
        2. **振幅级**：有实时电平 → 嘴跟**音量**（重音/停顿能看出来）
        3. **正弦回退**：都没有 → 原行为（不会因为没接上就不动嘴）

        音素级与振幅级的关系：音素级定**嘴型**（open/form 的形状），
        振幅级定**幅度**（同样的形状，声大时更夸张）。
        两者叠加才有“既对音又跟声”的效果。
        """
        if not self._model:
            return
        P = self._live2d.StandardParams
        if not self._speaking:
            # 停止说话：包络归零，下次开口从闭合起算
            self._mouth_env = 0.0
            self._mouth_lip_open = 0.0
            self._mouth_lip_form = 0.0
            self._mouth_slew_value = 0.0      # 起音/收音重新起算
            self._mouth_slew_at = 0.0
            try:
                self._model.SetParameterValue(P.ParamMouthOpenY, 0.0, 1.0)
            except Exception as e:
                self._note_frame_failure("MouthOpen", e)
            return

        level = self._read_mouth_level()
        lip = self._read_lip_shape()   # (open, form) 或 None

        if lip is not None:
            # ── 音素级：嘴型由发音决定 ──
            lip_open, lip_form = lip
            # 起音/收音：时间轴给的是**阶梯**（每字保持 0.345s 后一帧跳变），
            # 这里磨成曲线。振幅那条路径本就有非对称包络，所以只补这条。
            lip_open = self._slew_lip_open(lip_open)
            self._mouth_lip_open = lip_open
            self._mouth_lip_form = lip_form
            # 振幅作为“音量缩放”：声大时形状更夸张，但不改变形状本身
            if level is not None:
                env = getattr(self, "_mouth_env", 0.0)
                k = 0.55 if level > env else 0.18
                self._mouth_env = env + (level - env) * k
                gain = 0.55 + 0.45 * min(1.0, math.sqrt(max(0.0, self._mouth_env)) * 1.6)
            else:
                gain = 1.0
            val = max(0.0, min(1.0, lip_open * gain))
            form = max(-1.0, min(1.0, lip_form))
        elif level is not None:
            # ── 振幅级（无音素时间轴）：跟音量 ──
            env = getattr(self, "_mouth_env", 0.0)
            k = 0.55 if level > env else 0.18
            env = env + (level - env) * k
            self._mouth_env = env
            # RMS 天然偏小，做一次非线性提亮；保留小底噪避免完全闭合的僵硬感
            val = 0.08 + 0.92 * min(1.0, math.sqrt(max(0.0, env)) * 1.6)
            form = getattr(self, "_mouth_lip_form", 0.0)  # 沿用上一次唇形（保持连贯）
        else:
            # ── 正弦回退（与 LIP-1 之前一致）──
            self._mouth_phase = getattr(self, "_mouth_phase", 0.0) + 0.35
            val = 0.5 + 0.5 * math.sin(self._mouth_phase * 3.0)
            val = 0.15 + val * 0.6
            form = 0.0

        # 开口度上限（审美旋钮，核心默认 1.0 = 不压；见 core/lip_sync.set_mouth_peak）
        from core.lip_sync import get_mouth_peak

        val = min(val, get_mouth_peak())

        try:
            self._model.SetParameterValue(P.ParamMouthOpenY, val, 1.0)
        except Exception as e:
            # 失败 = 说话时嘴不动（TTS 同步的核心），用户 100% 能看见
            self._note_frame_failure("MouthOpen", e)
        # 唇形（圆唇/扁唇）——非所有模型都有该参数，失败静默。
        # 注意：**无条件写入**（包括 0）——若只写非零值，标点/静音段会残留上一字的唇形。
        try:
            self._model.SetParameterValue(P.ParamMouthForm, form, 0.8)
        except Exception:
            logger.debug("Live2DRenderer: 模型无 MouthForm 参数", exc_info=True)

    # ── P4/P2-6: 程序化自主动作层（让 Live2D 真正"活"，不依赖 motion 文件）──

    @property
    def master_emotion(self) -> str:
        """当前主导情绪（master emotion）：程序化表情层的驱动源。

        与 pet.py 的情绪更新链路（_set_surface_emotion / _do_engine_reply_inner /
        _on_emotion_expired）通过 set_master_emotion 保持同步；读不到时回退
        _current_emotion（set_emotion / play_anim 也会更新它）。
        """
        return getattr(self, "_current_emotion", "neutral") or "neutral"

    def set_master_emotion(self, emotion: str) -> None:
        """P2-6: 推送主导情绪到程序化表情层（只同步表情，不触发 motion/手势）。

        与 set_emotion 的区别：set_emotion 可能播放情绪 motion（有手势冷却），
        这里只更新 _current_emotion/_emotion_target 并应用 Live2D Expression，
        供 pet.py 在情绪更新链路（对话/屏幕/过期回 neutral）里高频调用，
        程序化表情层据此做面部参数平滑过渡。
        """
        emotion = emotion or "neutral"
        # T10: 同步 V/A 目标坐标（与 set_emotion 保持一致）
        # 2026-09-10：如果 [feel:] 刚设过 VA，则保持它不被离散情绪覆盖。
        if time.monotonic() >= getattr(self, "_va_hold_until", 0.0):
            self._va_target = self._EMOTION_VA.get(emotion, (0.0, 0.0))
        else:
            logger.debug("set_master_emotion: VA 保持中，跳过覆盖（%s）", emotion)
        self._current_emotion = emotion
        self._emotion_target = emotion
        if self._model:
            try:
                self._apply_expression(emotion)
            except Exception:
                logger.debug("Live2DRenderer: 非致命异常(已静默吞掉)", exc_info=True)

    def set_va_target(self, valence: float, arousal: float,
                      hold_sec: float = 3.0) -> bool:
        """推送连续 VA 坐标（2026-09-20）。

        与 ``apply_action_intent`` 里的 ``intent["va"]`` 路径写的是同一组字段，
        但那个是「LLM 自报的 feel 坐标」（实测承载不了细粒度），
        本入口是「分类器从文本算出的 VAD」——更可靠的连续信号源。

        写入后设 ``_va_hold_until``，保持期内 ``set_master_emotion`` 不会用
        ``_EMOTION_VA`` 表覆盖它（表里只有 7 个情绪，而分类器有 14 类）。

        Returns:
            是否采纳（数值非法时返回 False）。
        """
        try:
            v = max(-1.0, min(1.0, float(valence)))
            a = max(-1.0, min(1.0, float(arousal)))
        except (TypeError, ValueError):
            logger.debug("set_va_target: 数值非法 %r %r", valence, arousal)
            return False
        self._va_target = (v, a)
        try:
            hold = float(hold_sec)
        except (TypeError, ValueError):
            hold = 3.0
        self._va_hold_until = time.monotonic() + max(0.0, hold)
        return True

    def set_procedural_smoothing(self, seconds: float) -> None:
        """P2-6: 配置面部参数插值时间常数（秒，可配）。

        例：set_procedural_smoothing(0.5) → 情绪切换约 0.5s 内平滑过渡到新表情；
        0.1 更跟手、1.0 更舒缓。无效/非正输入回退默认。
        """
        try:
            val = float(seconds)
        except (TypeError, ValueError):
            val = -1.0
        self._proc_smooth_tau = val if val > 0.0 else float(self.PROCEDURAL_SMOOTH_TAU)

    def set_emotion_intensity(self, intensity: float) -> bool:
        """设置情绪强度（缩放程序化表情的参数幅度）。

        ## 为什么单独一个入口

        `_emotion_intensity` 早就存在（每帧读它缩放参数，见
        `_update_procedural_emotion` 第 3 步），但**从未有人写过**
        —— 所有调用方都走 `set_emotion(emo)`，强度默认 1.0。
        于是「我有点累」和「我累死了」表现完全一样。

        ## 与 set_va_target 的分工

        - `set_va_target(v, a)` —— 情绪**方向**（哪一类）
        - `set_emotion_intensity(i)` —— 情绪**程度**（多强）

        两者独立：分类器同时给出 VAD 和 intensity，分别喂。

        Args:
            intensity: 0~1。0 = 几乎无表情，1 = 满幅。

        Returns:
            是否采纳（数值非法返回 False）。
        """
        try:
            val = float(intensity)
        except (TypeError, ValueError):
            return False
        self._emotion_intensity = max(0.0, min(1.0, val))
        return True

    def _update_procedural_emotion(self) -> None:
        """每帧程序化驱动情绪表情 + 眼神 + 呼吸（叠加在 motion 之上）。

        P2-6 升级（对比 P4 版）：
          - 由 master_emotion 驱动，参数从 3 个扩展到 12 个：
            眼睛开合(EyeLOpen/ROpen)、眯眼(EyeLSmile/RSmile)、
            眉毛(Angle/Form)、嘴型(MouthForm)、嘴张(MouthOpenY，说话时让位)、
            眼神方向(EyeBallX/Y，低权重叠加视线跟随)、头部转向(AngleX/Y，仅视线关闭)、
            呼吸节律(ParamBreath，幅度/频率随情绪变化)。
          - 平滑插值改为帧率无关的指数平滑：alpha = 1 - exp(-dt/tau)，
            tau 可配（PROCEDURAL_SMOOTH_TAU / set_procedural_smoothing）。
          - 每组参数独立 try/except：模型缺某个参数时该组跳过，绝不崩溃。
          - 约束：miku moc3 无手/臂/腿参数，只驱动面部/眼神/呼吸，不碰肢体。
        weight 保持 <1 与 motion 混合（motion 为主、此层为辅），避免打架。
        """
        if not self._model:
            return
        P = self._live2d.StandardParams
        # 手动演示锁定机制已随「动作/表情展示」面板移除；面部参数始终由情绪驱动。
        _override = False
        try:
            # T10: V/A 空间路径插值（离散情绪名 → V/A 坐标 → 参数目标值）
            # 1) 平滑过渡 V/A 坐标
            now = time.monotonic()
            dt = min(max(now - getattr(self, "_proc_last_t", now), 0.0), 0.1)
            self._proc_last_t = now
            tau = getattr(self, "_proc_smooth_tau", self.PROCEDURAL_SMOOTH_TAU) or self.PROCEDURAL_SMOOTH_TAU
            alpha = 1.0 - math.exp(-dt / tau) if dt > 0.0 else 1.0
            va_cur = getattr(self, "_va_cur", (0.0, 0.0))
            va_target = getattr(self, "_va_target", (0.0, 0.0))
            va_cur = (va_cur[0] + (va_target[0] - va_cur[0]) * alpha,
                      va_cur[1] + (va_target[1] - va_cur[1]) * alpha)
            self._va_cur = va_cur
            # 2) 从 V/A 坐标插值参数目标
            intensity = getattr(self, "_emotion_intensity", 1.0)
            targets = self._va_interpolate_targets(va_cur)
            # 3) intensity 缩放（只缩放非基准参数，breath_amp/rate 不受影响）
            if intensity != 1.0:
                scaled = {}
                for k, v in targets.items():
                    if k in ("breath_amp", "breath_rate"):
                        scaled[k] = v  # 呼吸不受强度影响
                    else:
                        scaled[k] = v * intensity
                targets = scaled
            # 动作叠加（motion 文件名关键词 → 叠加参数，如 waving→眯眼+脸红）。
            # 在情绪目标之上叠加（clamp 由各参数写入处负责），weight<1 混合不打架。
            overlay = getattr(self, "_action_overlay", None) or {}
            if overlay:
                targets = dict(targets)
                targets.update(overlay)
            # 表情序列覆盖（优先于情绪目标；返回 None 表示无序列，走正常情绪路径）。
            # 眨眼脉冲等快速参数经 seq_fast 在末尾直接写（跳过平滑，保证节奏）。
            seq_override, seq_fast = self._emote_seq_targets()
            if seq_override is not None:
                targets = dict(targets)
                targets.update(seq_override)
            self._emote_seq_fast = seq_fast or {}
            # 帧率无关指数平滑
            now = time.monotonic()
            dt = min(max(now - getattr(self, "_proc_last_t", now), 0.0), 0.1)
            self._proc_last_t = now
            tau = getattr(self, "_proc_smooth_tau", self.PROCEDURAL_SMOOTH_TAU) or self.PROCEDURAL_SMOOTH_TAU
            alpha = 1.0 - math.exp(-dt / tau) if dt > 0.0 else 1.0
            cur = getattr(self, "_proc_cur", {})
            for k in targets:
                cur[k] = cur.get(k, 0.0) + (targets[k] - cur.get(k, 0.0)) * alpha
            # blush 平滑归零兜底：动作叠加/表情序列结束后 blush 不在 targets 里，
            # 若不显式衰减会残留在 _proc_cur 导致脸红永不消退。
            cur["blush"] = cur.get("blush", 0.0) + (targets.get("blush", 0.0) - cur.get("blush", 0.0)) * alpha
            self._proc_cur = cur

            # 结构化动作意图（[action:{...}] 注入的直接参数目标）：复用同一帧率无关
            # 指数平滑，平滑过渡到目标值，避免瞬间跳变。每条独立 try/except 兜底。
            # W3：先做过期释放，避免 LLM 写过的参数永久卡住。
            self._expire_intent_params()
            intent_targets = getattr(self, "_param_intent", None) or {}
            if intent_targets:
                _pcur = getattr(self, "_param_cur", {}) or {}
                for _name, _tgt in intent_targets.items():
                    try:
                        _tgt_f = float(_tgt)
                    except (TypeError, ValueError):
                        continue
                    _pcur[_name] = _pcur.get(_name, 0.0) + (_tgt_f - _pcur.get(_name, 0.0)) * alpha
                self._param_cur = _pcur

            # 手动演示锁定期间：跳过眼/眉/嘴/脸红等会与手动表情/动作冲突的面部参数，
            # 只保留呼吸/眼神/头部/头发等底层自然律动，让手动选择完整显示不被冲淡。
            if not _override:
                # D1-lite 委托写入（T07）
                writer = getattr(self, "_param_writer", None) or ParamWriter(self._model, getattr(self, "_model_profile", DEFAULT_PROFILE))
                self._param_writer = writer
                writer.write_group("eyes_open", cur, gate=True)
                writer.write_group("eyes_smile", cur, gate=True)
                writer.write_group("eyebrows", cur, gate=True)
                writer.write_group("mouth_form", cur, gate=not getattr(self, "_speaking", False))
                writer.write_group("mouth_open", cur, gate=not getattr(self, "_speaking", False))
                writer.write_group("gaze", cur, gate=True)
                _apply_head = (not getattr(self, "_gaze_enabled", True)) or (getattr(self, "_emote_seq_active", False) and (("head_angle_x", "head_angle_y") & (self._emote_seq[self._emote_seq_idx].get("params") or {}).keys()))
                writer.write_group("head_angle", cur, gate=_apply_head)
                breath_val = (0.5 + 0.5 * math.sin(now * 1.6 * max(0.5, min(2.5, cur.get("breath_rate", 1.0))))) * 0.5 * max(0.0, min(2.0, cur.get("breath_amp", 1.0)))
                writer.write_derived("breath", {"ParamBreath": breath_val}, gate=True)
                hair_raw = math.sin(now * 0.8) * 3.0
                writer.write_derived("hair", {"ParamHairFront": hair_raw, "ParamHairSide": hair_raw}, gate=True)
                blush_val = max(0.0, min(1.0, cur.get("blush", 0.0) or 0.0))
                if blush_val > 0.01:
                    self._apply_blush(blush_val)
            # 表情序列快速参数（如眨眼脉冲）：跳过平滑直接写，保证眨眼节奏。
            # 眨眼脉冲 = eye_open 交替 0.1/1.0，若走指数平滑会被拉成"软眨眼"看不清。
            _fast = getattr(self, "_emote_seq_fast", None) or {}
            if _fast.get("eye_open") is not None:
                try:
                    _ev = max(0.0, min(1.0, float(_fast["eye_open"])))
                    self._model.SetParameterValue(P.ParamEyeLOpen, _ev, 0.9)
                    self._model.SetParameterValue(P.ParamEyeROpen, _ev, 0.9)
                except Exception as e:
                    self._note_frame_failure("ProceduralEyeOpen", e)
            # 单眼眨眼（wink）：左右眼分开写，保留另一只眼的情绪开合
            if _fast.get("eye_open_l") is not None:
                try:
                    _ev = max(0.0, min(1.0, float(_fast["eye_open_l"])))
                    self._model.SetParameterValue(P.ParamEyeLOpen, _ev, 0.9)
                except Exception as e:
                    self._note_frame_failure("ProceduralEyeOpenL", e)
            if _fast.get("eye_open_r") is not None:
                try:
                    _ev = max(0.0, min(1.0, float(_fast["eye_open_r"])))
                    self._model.SetParameterValue(P.ParamEyeROpen, _ev, 0.9)
                except Exception as e:
                    self._note_frame_failure("ProceduralEyeOpenR", e)
            # 写入结构化动作意图参数目标（已在上方按 alpha 平滑到 _param_cur）
            _intent_cur = getattr(self, "_param_cur", None) or {}
            for _name, _val in _intent_cur.items():
                try:
                    # 2026-09-06: 先查映射表，再查 StandardParams 属性，最后用原名兜底
                    _mapped = self._PARAM_NAME_MAP.get(_name, _name)
                    _pid = getattr(P, _mapped, _mapped)
                    # 白名单检查：先过跨模型允许集，再过“按模型实际参数”校验
                    # （W4 2026-09-14：单靠静态集合会在换模型时写出无效参数）
                    if _pid not in self._PARAM_WHITE_LIST:
                        continue
                    if _pid not in self._effective_whitelist():
                        continue
                    self._model.SetParameterValue(_pid, float(_val), 1.0)
                except Exception as e:
                    self._note_frame_failure("IntentParams", e)
        except Exception as e:
            self._note_frame_failure("ProceduralEmotion", e)

    # ── 动作优化层：idle 微摆动 / 动作叠加 / 表情序列 ──

    def _update_idle_sway(self) -> None:
        """idle 状态微摆动：2~3 个低频正弦叠加驱动头/眼微摆（参数随机化）。

        仅 ``_motion_is_idle`` 时生效（手势/情绪 motion 播放期间不抢）。
        在 ``_update_procedural_emotion`` 之后调用，直接写参数（低权重混合）。
        每 8~15 秒或每次进 idle 重新随机相位/幅度/频率，避免机械循环感。
        约束：miku moc3 无肢体参数，只驱动头部角度 + 眼神，绝不碰 ParamArm*/Leg*。
        """
        if not self._model or not self._live2d:
            return
        if not getattr(self, "_idle_sway_enabled", True):
            return
        if not getattr(self, "_motion_is_idle", True):
            return
        # 表情序列播放期间暂停微摆动：nod/head_shake/head_tilt 等头部预设
        # 需要独占头/眼参数，微摆动会跟它们打架（后写覆盖）。
        if getattr(self, "_emote_seq_active", False):
            return
        now = time.monotonic()
        if now >= getattr(self, "_idle_sway_next_random_at", 0.0) or not self._idle_sway_phase:
            self._randomize_idle_sway()
            self._idle_sway_next_random_at = now + random.uniform(
                self.IDLE_SWAY_RANDOM_MIN_S, self.IDLE_SWAY_RANDOM_MAX_S,
            )
        ph = self._idle_sway_phase
        t = now
        # 头部角度：两个低频正弦叠加（目标 ±1.5° 内；weight 取 0.25 既保留 75%
        # gaze 跟手，又叠加 ±0.4° 左右的"活着"微摆，避免压低视线跟随）
        head_x = (
            ph["head_x_amp"] * math.sin(t * math.tau * ph["head_x_freq"] + ph["head_x_phase"])
            + 0.5 * ph["head_x_amp"] * math.sin(t * math.tau * ph["head_x_freq"] * 0.5 + ph["head_x_phase"] * 1.7)
        )
        head_y = (
            ph["head_y_amp"] * math.sin(t * math.tau * ph["head_y_freq"] + ph["head_y_phase"])
            + 0.5 * ph["head_y_amp"] * math.sin(t * math.tau * ph["head_y_freq"] * 0.7 + ph["head_y_phase"] * 2.3)
        )
        # 眼神：低频小幅游移（目标 ±0.15 内，weight 0.2）
        ball_x = ph["ball_x_amp"] * math.sin(t * math.tau * ph["ball_x_freq"] + ph["ball_x_phase"])
        ball_y = ph["ball_y_amp"] * math.sin(t * math.tau * ph["ball_y_freq"] + ph["ball_y_phase"])
        P = self._live2d.StandardParams
        try:
            self._model.SetParameterValue(P.ParamAngleX, head_x, 0.25)
            self._model.SetParameterValue(P.ParamAngleY, head_y, 0.25)
        except Exception as e:
            self._note_frame_failure("IdleSwayHead", e)
        try:
            self._model.SetParameterValue(P.ParamEyeBallX, ball_x, 0.2)
            self._model.SetParameterValue(P.ParamEyeBallY, ball_y, 0.2)
        except Exception as e:
            self._note_frame_failure("IdleSwayGaze", e)

    def _randomize_idle_sway(self) -> None:
        """重新随机 idle 摆动参数（相位/幅度/频率），避免机械循环感。"""
        self._idle_sway_phase = {
            # 头部：慢（0.08~0.22 Hz）
            "head_x_amp": random.uniform(0.4, 1.5),
            "head_x_freq": random.uniform(0.10, 0.22),
            "head_x_phase": random.uniform(0.0, math.tau),
            "head_y_amp": random.uniform(0.3, 1.0),
            "head_y_freq": random.uniform(0.08, 0.18),
            "head_y_phase": random.uniform(0.0, math.tau),
            # 眼神：稍快、幅度小
            "ball_x_amp": random.uniform(0.05, 0.15),
            "ball_x_freq": random.uniform(0.25, 0.45),
            "ball_x_phase": random.uniform(0.0, math.tau),
            "ball_y_amp": random.uniform(0.04, 0.12),
            "ball_y_freq": random.uniform(0.20, 0.40),
            "ball_y_phase": random.uniform(0.0, math.tau),
        }

    def _apply_action_overlay(self, fname: str) -> None:
        """按 motion 文件名关键词设置动作叠加参数（未命中则清空）。"""
        low = str(fname or "").lower()
        overlay: dict[str, float] = {}
        for kw, params in self._ACTION_OVERLAYS.items():
            if kw in low:
                overlay = params
                break
        self._action_overlay = dict(overlay)
        if overlay:
            logger.debug("Live2DRenderer: 动作叠加 %s -> %s", fname, overlay)

    def play_emote_sequence(self, steps, name: str = "") -> bool:
        """播放表情序列（不播 motion，只做表情切换）。

        Args:
            steps: 步列表（每步 dict，见 _EMOTE_PRESETS 注释），或预设名
                   （20+ 预设：blush/blink3/gaze_shift/wink/blink_slow/
                   blush_shy/blush_deny/gaze_up/gaze_down/gaze_side/sneak_peek/
                   smile_soft/smile_bright/pout/angry_glare/sad_droop/
                   surprise_gasp/think_look/doubt/sigh/yawn/excited/grin/shy/
                   proud/sleepy/nod/head_shake/head_tilt/giggle/sneeze/
                   stretch_yawn 等）。
            name: 可选序列名（日志用；缺省用预设名）。

        Returns:
            True 表示序列已启动；False 表示参数非法/未知预设。
        """
        if isinstance(steps, str):
            preset = self._EMOTE_PRESETS.get(steps)
            if preset is None:
                logger.warning("Live2DRenderer: 未知表情序列预设 %s", steps)
                return False
            name = name or steps
            steps = preset
        if not steps or not isinstance(steps, list):
            return False
        # 拷贝一份，避免调用方后续修改影响播放中状态
        self._emote_seq = [dict(s) for s in steps if isinstance(s, dict)]
        if not self._emote_seq:
            return False
        self._emote_seq_idx = 0
        self._emote_seq_active = True
        self._emote_seq_step_started = time.monotonic()
        self._emote_seq_name = name
        self._emote_seq_fast = {}
        logger.info("Live2DRenderer: 播放表情序列 %s（%d 步）", name or "custom", len(self._emote_seq))
        return True

    def stop_emote_sequence(self) -> None:
        """立即停止表情序列（恢复情绪目标）。"""
        self._emote_seq = []
        self._emote_seq_idx = 0
        self._emote_seq_active = False
        self._emote_seq_step_started = 0.0
        self._emote_seq_fast = {}
        self._emote_seq_name = ""
        logger.info("Live2DRenderer: 表情序列已手动停止")

    def _emote_seq_targets(self):
        """返回当前表情序列步的目标 ``(override_targets, fast_params)``。

        - override_targets: dict 或 None（None=无序列/本步无覆盖 → 走情绪目标）
        - fast_params: dict（跳过平滑直接写的参数，如眨眼脉冲 eye_open）
        序列播完自动调用 _finish_emote_seq 恢复情绪目标。
        """
        if not getattr(self, "_emote_seq_active", False) or not self._emote_seq:
            return None, None
        now = time.monotonic()
        idx = self._emote_seq_idx
        if idx >= len(self._emote_seq):
            self._finish_emote_seq()
            return None, None
        step = self._emote_seq[idx]
        stype = step.get("type", "set")
        elapsed = now - self._emote_seq_step_started
        if stype == "clear":
            dur = max(0.05, float(step.get("duration", 1.0) or 1.0))
            if elapsed >= dur:
                self._advance_emote_seq(now)
                return None, None
            # 清空步：目标为空（回情绪目标），无快速参数
            return {}, None
        if stype == "blink":
            interval = max(0.2, float(step.get("interval", 0.6) or 0.6))
            times = int(step.get("times", 0) or 0)
            if times > 0:
                # 按次数计数（语义稳，B1 修复）：闭眼起点为 0/interval/2*interval…
                # 共 times 次，播满 times 次即结束——不会像按 duration 取模那样
                # 在边界出现第 times+1 次被截断的半眨眼（旧 preset duration=2.2
                # 时 2.1s 起第 4 次眨眼只剩 0.1s 闭眼）。
                blink_index = int(elapsed // interval)
                if blink_index >= times:
                    self._advance_emote_seq(now)
                    return None, None
                cycle = elapsed - blink_index * interval
            else:
                # 兼容：未给 times 时按 duration 循环（旧行为）
                dur = max(0.05, float(step.get("duration", 1.0) or 1.0))
                if elapsed >= dur:
                    self._advance_emote_seq(now)
                    return None, None
                cycle = elapsed % interval
            # 眨眼脉冲：闭 0.12s / 开 0.58s（交替），模拟一次眨眼
            eye_open = 0.1 if cycle < 0.12 else 1.0
            # side 支持单眼 wink（left/right）；缺省 both（双眼同眨）
            side = str(step.get("side") or "both").lower()
            fast: dict[str, float] = {}
            if side in ("left", "l"):
                fast["eye_open_l"] = eye_open
            elif side in ("right", "r"):
                fast["eye_open_r"] = eye_open
            else:
                fast["eye_open"] = eye_open
            return {}, fast
        # set：目标覆盖情绪目标（部分键覆盖，其余回情绪）
        dur = max(0.05, float(step.get("duration", 1.0) or 1.0))
        if elapsed >= dur:
            self._advance_emote_seq(now)
            return None, None
        params = dict(step.get("params") or {})
        return params, None

    def _advance_emote_seq(self, now: float) -> None:
        """推进表情序列到下一步；播完自动恢复。"""
        self._emote_seq_idx += 1
        if self._emote_seq_idx >= len(self._emote_seq):
            self._finish_emote_seq()
        else:
            self._emote_seq_step_started = now

    def _finish_emote_seq(self) -> None:
        """表情序列播完：清空状态，表情层自动回情绪目标（无需额外操作）。"""
        name = self._emote_seq_name or ""
        self._emote_seq = []
        self._emote_seq_idx = 0
        self._emote_seq_active = False
        self._emote_seq_step_started = 0.0
        self._emote_seq_name = ""
        self._emote_seq_fast = {}
        logger.info("Live2DRenderer: 表情序列播完%s，已恢复情绪表情",
                    f"（{name}）" if name else "")

    def _apply_blush(self, blush: float) -> None:
        """脸红：优先 ParamCheek 标准参数；模型没有则用 eye_smile + brow 组合模拟。

        所有调用独立 try/except，缺参数跳过，绝不崩溃。
        """
        if not self._model:
            return
        blush = max(0.0, min(1.0, blush))
        P = self._live2d.StandardParams
        # 1) 标准脸红参数（少数模型有 ParamCheek）
        cheek_param = getattr(P, "ParamCheek", None)
        if cheek_param is not None:
            try:
                self._model.SetParameterValue(cheek_param, blush * 0.6, 0.6)
                return
            except Exception as e:
                self._note_frame_failure("BlushCheek", e)
        # 2) 组合模拟：眯眼 + 眉毛形态微弯（害羞/脸颊微鼓的观感）
        try:
            self._model.SetParameterValue(P.ParamEyeLSmile, blush * 0.25, 0.5)
            self._model.SetParameterValue(P.ParamEyeRSmile, blush * 0.25, 0.5)
        except Exception as e:
            self._note_frame_failure("BlushEyes", e)
        try:
            self._model.SetParameterValue(P.ParamBrowLForm, blush * 0.15, 0.5)
            self._model.SetParameterValue(P.ParamBrowRForm, blush * 0.15, 0.5)
        except Exception as e:
            self._note_frame_failure("BlushBrow", e)

    def _note_motion_started(self, fname: str = "", is_idle: bool = False) -> None:
        """记录当前 motion 是否常态 idle 并重置计时（卡手势超时兜底用）。

        is_idle 由调用方显式传入（idle 动作=True，手势/随机动作=False），
        不再依赖文件名是否含 "idle" 猜测——避免某手势 motion 文件名恰含 "idle"
        被错判为 idle，导致 GESTURE_TIMEOUT 兜底永不触发、手势永久卡住（如比心）。

        动作优化：回 idle 时清除动作叠加（_action_overlay），
        非 idle 时按 motion 文件名关键词设置叠加（眯眼/脸红/视线偏移等）。
        """
        self._motion_is_idle = bool(is_idle)
        self._motion_started_at = time.monotonic()
        if is_idle:
            self._action_overlay = {}
        else:
            self._apply_action_overlay(fname)

    def _motion_weight(self, fname: str) -> float:
        """按 motion 文件名关键词匹配 pet.json 配置的权重（未配置默认 1.0）。

        P8: 每个模型可在 pet.json 的 animations 块里给动作配 weight——
        weight=0 表示不参与自动随机播放（如 touch 交互专属、idle 常驻）。
        文件名关键词与 animations 键名匹配（happy.motion3.json → "happy"）。
        """
        w = 1.0
        low = str(fname).lower()
        for kw, wt in getattr(self, "_motion_weights", {}).items():
            if kw in low:
                w = wt
                break
        return w

    def _pick_emote_preset(self) -> str:
        """从表情预设池按权重随机选一个预设名（weight<=0 不参与自动播放）。

        预设池扩充到 20+ 后，自动表情序列的选取从"硬编码 3 选 1"升级为
        "加权随机"：日常微妙表情（眨眼/微笑/视线/点头）权重高、戏剧性表情
        （惊讶/打喷嚏/伸懒腰）权重低，避免待机时频繁出现夸张脸。
        """
        names = [
            n for n in self._EMOTE_PRESETS
            if self._EMOTE_PRESET_WEIGHTS.get(n, 1.0) > 0
        ]
        if not names:
            return ""
        weights = [max(0.0, self._EMOTE_PRESET_WEIGHTS.get(n, 1.0)) for n in names]
        return random.choices(names, weights=weights, k=1)[0]

    def _tick_auto_motion(self) -> None:
        """周期 30~80s 随机播一次非 idle motion，避免"一直比心"的视觉疲劳。

        仅在 idle 时触发（手动播手势期间不打断）；首次触发是 _ready 后随机
        第一次。依赖 _motion_files / _start_motion_at 既有的播放栈，不修改
        GESTURE_TIMEOUT 兜底逻辑——播满 GESTURE_TIMEOUT 自然回 idle，配合
        本方法形成"随机播 ~idle 的循环"。
        """
        if not self._auto_motion_enabled or not self._model or not self._motion_files:
            return
        if len(self._motion_files) < 2:  # 只有一个 motion（或 idle）就不用调
            return
        # 仅在 idle 状态下触发随机调度
        try:
            if not getattr(self, "_motion_is_idle", True):
                return
        except Exception:
            return
        now = time.monotonic()
        if self._auto_motion_next_at == 0.0:
            # 首次随机化（首次触发时机设远一些，让用户先看清初始 idle）
            self._auto_motion_next_at = now + 45.0
            return
        if now < self._auto_motion_next_at:
            return
        # 动作优化：idle 到点后，10% 概率用表情序列替代随机 motion（纯参数驱动，
        # 不新增 motion 文件；序列 2~4s 自动结束，不影响 GESTURE_TIMEOUT 兜底）。
        # 2026-09-06 调低：从 25% 降到 10%，motion 播放更频繁，减少“摇头晃脑”观感。
        # 从 20+ 预设池按权重加权随机（_EMOTE_PRESET_WEIGHTS：日常微妙表情权重高，
        # 戏剧性表情权重低），避免待机时频繁出现夸张脸。
        try:
            if random.random() < 0.10:
                preset = self._pick_emote_preset()
                if preset and self.play_emote_sequence(preset):
                    logger.info("Live2DRenderer: 自动表情序列 %s（替代随机 motion）", preset)
                    self._auto_motion_next_at = now + random.uniform(
                        self._auto_motion_min_s, self._auto_motion_max_s
                    )
                    return
        except Exception as e:
            # 自动表情序列失败：原本只 debug —— 用户会看到「长时间不动」
            self._note_frame_failure("AutoEmotionSequence", e)
        # 到点：选一个非 idle 的 motion；按 pet.json 配置的 weight 加权随机。
        # P8 权重机制：weight=0（如 touch 交互专属 / idle）不参与自动播放；
        # 未配置 weight 的动作默认 1.0。每个模型在 pet.json 的 animations 块配。
        candidates = []  # (idx, weight)
        for i, f in enumerate(self._motion_files):
            low = str(f).lower()
            try:
                if "idle.motion3" in low:
                    continue
                w = self._motion_weight(f)
                if w <= 0:
                    continue
                candidates.append((i, w))
            except Exception:
                continue
        if not candidates:
            return
        chosen = None
        emo = getattr(self, "_emotion_target", "") or ""
        if emo and random.random() < 0.5:
            want = (getattr(self, "_emotion_anims", {}) or {}).get(emo, "")
            if want:
                for i, f in enumerate(self._motion_files):
                    if want in str(f).lower() and self._motion_weight(f) > 0:
                        chosen = i
                        break
        if chosen is None:
            # 加权随机：按 weight 分配概率
            total = sum(w for _, w in candidates)
            r = random.uniform(0, total)
            acc = 0.0
            for i, w in candidates:
                acc += w
                if r <= acc:
                    chosen = i
                    break
            if chosen is None:
                chosen = candidates[-1][0]
        try:
            # exclusive=True 清场：避免与未结束的手势/表情姿势叠加
            self._start_motion_at(chosen, None, exclusive=True)  # 默认 NORMAL 优先级
            logger.info(
                "Live2DRenderer: 自动随机动作 idx=%d（%s）",
                chosen, self._motion_files[chosen],
            )
        except Exception as e:
            self._note_frame_failure("AutoRandomMotion", e)
        # 安排下一次（30~80s 随机区间）
        try:
            self._auto_motion_next_at = now + random.uniform(
                self._auto_motion_min_s, self._auto_motion_max_s
            )
        except Exception:
            self._auto_motion_next_at = now + 45.0

    def _force_idle(self) -> None:
        """StopAllMotions 后用 FORCE 优先级重启 idle（最高优先级强制接管）。

        历史教训（三次修复）：
        - v1 用 IDLE 优先级 → 打不过正在播的 NORMAL 手势，接不上 → 比心死锁
        - v2 用 NORMAL 优先级 → 但手势 motion 也是 NORMAL，Live2D 同优先级
          不打断正在播的 motion，StartMotion(idle, NORMAL) 被静默拒绝 → 仍死锁
        - v3（当前）用 FORCE 优先级（最高）→ 强制替换任何正在播的 motion，
          再叠加双重 StopAllMotions 清理，确保 idle 一定能接管
          
        P1 修复：同时 ResetExpressions，清除前倾/脸红等贴图表情。
        旧实现只切 motion 不清表情，导致 motion 回 idle 后前倾贴图仍挂着。
        """
        # P1: 先重置表情，再切 motion（确保贴图清除优先）。
        # miku 的比心/葱等是 Param131-137 贴图开关，某些 wrapper/模型状态下
        # 单次 ResetExpressions 不能立即清掉，这里多重调用 + 日志便于排查。
        try:
            if hasattr(self._model, "ResetExpressions"):
                self._model.ResetExpressions()
                logger.info("Live2DRenderer: _force_idle 第一次 ResetExpressions 完成")
        except Exception as e:
            logger.warning("Live2DRenderer: _force_idle 第一次 ResetExpressions 失败: %s", e)
        # P2-9: 同步重置表情簿记，避免后续 _apply_expression 基于旧状态误判
        self._expression_active = False
        self._last_expression = ""
        self._expression_suppress_until = 0.0
        # 双重 StopAllMotions：某些 wrapper 实现需要两次才彻底清
        try:
            if hasattr(self._model, "StopAllMotions"):
                self._model.StopAllMotions()
        except Exception:
            logger.debug("Live2DRenderer: 非致命异常(已静默吞掉)", exc_info=True)
        try:
            if hasattr(self._model, "StopAllMotions"):
                self._model.StopAllMotions()
        except Exception:
            logger.debug("Live2DRenderer: 非致命异常(已静默吞掉)", exc_info=True)
        # 在 StopAllMotions 后再清一次表情：有些模型 motion 停止后才真正释放
        # 表情贴图，确保比心/葱等不会残留在 idle 上。
        try:
            if hasattr(self._model, "ResetExpressions"):
                self._model.ResetExpressions()
                logger.info("Live2DRenderer: _force_idle 第二次 ResetExpressions 完成")
        except Exception as e:
            logger.warning("Live2DRenderer: _force_idle 第二次 ResetExpressions 失败: %s", e)
        # 用 FORCE 优先级播 idle（最高，强制接管当前任何 motion）
        try:
            if not self._model:
                self._note_motion_started("force_idle_no_model", is_idle=True)
                return
            motions = self._model.GetMotions()
            if motions:
                group = next((g for g in motions if g), next(iter(motions), ""))
                motion_list = motions.get(group, [])
                if motion_list:
                    idx = 0
                    for i, m in enumerate(motion_list):
                        if isinstance(m, dict) and "idle" in m.get("File", "").lower():
                            idx = i
                            break
                    fname = (motion_list[idx].get("File", "")
                             if isinstance(motion_list[idx], dict) else "idle")
                    # 关键：FORCE 优先级，强制替换正在播的 happy/手势 motion
                    self._model.StartMotion(group, idx, self._live2d.MotionPriority.FORCE)
                    # 切到 idle motion 后再清一次表情：防止 motion 启动过程中又把
                    # 旧表情贴图带回来（miku 比心/葱残留问题）。
                    try:
                        if hasattr(self._model, "ResetExpressions"):
                            self._model.ResetExpressions()
                            logger.info("Live2DRenderer: _force_idle 第三次 ResetExpressions 完成")
                    except Exception as e:
                        logger.warning("Live2DRenderer: _force_idle 第三次 ResetExpressions 失败: %s", e)
                    self._note_motion_started(fname, is_idle=True)
                    logger.info("Live2DRenderer: _force_idle 成功播放 idle（idx=%d, FORCE 优先级）", idx)
                    # 重置后让自动随机动作从完整间隔重新计时：_tick_auto_motion 在手动
                    # 演示锁定期会直接 return 不更新 _auto_motion_next_at，导致该计时器
                    # 停在"过去"；一旦锁定解除（如本重置）就立即重播随机手势，视觉上像
                    # "重置没生效"。这里推到未来一个完整间隔，重置后桌宠真正静止休息。
                    try:
                        self._auto_motion_next_at = time.monotonic() + random.uniform(
                            self._auto_motion_min_s, self._auto_motion_max_s)
                    except Exception:
                        self._auto_motion_next_at = time.monotonic() + 45.0
                    return
        except Exception as e:
            logger.warning("Live2DRenderer: _force_idle 强切 idle 失败: %s", e)
        # 兜底：状态机切回 idle（即使没真播放，UI 状态对）
        try:
            if hasattr(self._model, "ResetExpressions"):
                self._model.ResetExpressions()
                logger.info("Live2DRenderer: _force_idle 兜底 ResetExpressions 完成")
        except Exception as e:
            logger.warning("Live2DRenderer: _force_idle 兜底 ResetExpressions 失败: %s", e)
        self._note_motion_started("force_idle_fallback", is_idle=True)
        # 同成功路径：重置自动随机动作计时器，避免重置后秒级重播手势。
        try:
            self._auto_motion_next_at = time.monotonic() + random.uniform(
                self._auto_motion_min_s, self._auto_motion_max_s)
        except Exception:
            self._auto_motion_next_at = time.monotonic() + 45.0

    # ── T08: MotionMixer 接口实现 ──

    def submit_motion_request(self, req: MotionRequest, fallback_motion: str = "") -> bool:
        """T08: 提交动作请求（经层优先级仲裁）。

        仲裁通过 → 播放 motion / expression / params；
        仲裁拒绝 → 返回 False。
        fallback_motion 非空时，motion_group 播放失败则自动回退。
        """
        if not self._mixer.submit(req):
            return False
        # 仲裁通过：播放
        played = False
        if req.motion_group:
            # from_arbiter=True：这是 mixer 刚批准的动作本人，不受仲裁保护拦截
            played = self._play_motion_kw(req.motion_group, from_arbiter=True)
            if not played:
                # 2026-09-11：motion 未找到但 params 可能生效——**必须可见**。
                # 原实现因为下面 `if req.params: played = True` 而把整条请求当成功，
                # 日志只说“已提交 MotionRequest”，而动作实际没播。
                # 实测：模型常自造 gesture（如 concern），既不是别名/预设也不是 motion 名。
                logger.info(
                    "MotionRequest 的动作 %r 未匹配到 motion，仅面部参数生效"
                    "（可用动作见 prompt 的 [do:] 选项）",
                    req.motion_group,
                )
            if not played and fallback_motion:
                played = self._play_motion_kw(fallback_motion, from_arbiter=True)
        if req.expression_name:
            self._apply_expression(req.expression_name)
            played = True
        if req.params:
            self._param_intent.update(req.params)
            played = True
        if not played:
            # 无 motion/expression/params 可播 → 仲裁回滚
            return False
        self._note_motion_started(
            req.motion_group or req.expression_name or req.name or "mixer",
            is_idle=(req.layer == Layer.IDLE),
        )
        return True

    def force_idle(self) -> None:
        """T08: 强制重置到 idle（替代外部直调 _force_idle）。

        先调 mixer.force_reset() 清除所有层 + 进入 3 秒冷却，
        再调 _force_idle() 做底层清场（三重 ResetExpressions + 双重 StopAllMotions）。

        ── 重复调用保护（2026-09-11）──
        `bubble_mixin` 在**每个** `state == "idle"` 事件都会调本方法，而 Hanako
        会在数秒内推多次 → 实测 3 秒内 4 次 force_idle，每次三重 ResetExpressions
        + 双重 StopAllMotions（很贵，且会打断正在演的东西）。

        不变量：**已经在目标状态时，force_idle 无事可做。**

        “目标状态” = 已 idle + 无活跃表情 + mixer 无活跃请求。
        此时 force_idle 唯一的效果是把正在loop的 idle 从头重播——
        一次可见的抽动，而这正是要避免的。

        不用时间窗：时间窗过了一定秒数又会放开，反而让 idle 每秒重播一次；
        而纯语义判断没有这个漏洞，也不需要霫数。

        保留强制能力：真有卡住的表情时 `_expression_active` 为真，本保护不拦。
        （但 idle motion 自身的循环由 IdleLoopProcessor 负责，不走这条路。）
        """
        already_idle = (
            getattr(self, "_motion_is_idle", False)
            and not getattr(self, "_expression_active", False)
            and self._mixer.is_idle()
        )
        if already_idle:
            return
        self._mixer.force_reset()
        self._force_idle()

    def get_motion_layer(self) -> Layer:
        """T08: 获取当前活跃动作层。"""
        return self._mixer.get_active_layer()

    def is_motion_idle(self) -> bool:
        """T08: 是否处于 idle 层。"""
        return self._mixer.is_idle()

    def _start_idle(self) -> None:
        # ── idle 重播节流（2026-09-11）──
        # IdleLoopProcessor 每帧检查 IsMotionFinished()，为真就调本方法。
        # 而本方法原本**无条件 StartMotion** —— 没有去重（对比
        # _start_motion_at 是有去重的）。
        #
        # 后果：若 idle motion 播完得异常快（或 IsMotionFinished 在某状态下
        # 持续为真），就会逐帧重播。实测日志：idle 一秒播 3 次、一晚 38 次，
        # 任何动作都活不过一秒——**桌宠在不停地打断自己**。
        #
        # 不变量：idle 重播至少间隔 IDLE_RESTART_MIN_INTERVAL 秒。
        # 正常 idle 长度（2-4s）远大于此值，不受影响；只在异常快速重播时生效。
        now = time.monotonic()
        if now - getattr(self, "_last_idle_start_at", 0.0) < self.IDLE_RESTART_MIN_INTERVAL:
            return
        if not self._model:
            return
        try:
            # 优先用 GetMotions() 取具体 motion 列表，按索引播放，避免 StartRandomMotion
            # 对空串组名可能静默失败的问题。
            motions = self._model.GetMotions()
            if motions:
                # 优先取空串组（此模型所有动作都在 "" 组），否则取第一个非空组
                group = next((g for g in motions if g), next(iter(motions), ""))
                motion_list = motions.get(group, [])
                if motion_list:
                    # 选包含 "idle" 的 motion（优先），否则播第 0 个
                    idx = 0
                    for i, m in enumerate(motion_list):
                        if isinstance(m, dict) and "idle" in m.get("File", "").lower():
                            idx = i
                            break
                    fname = (motion_list[idx].get("File", "")
                             if isinstance(motion_list[idx], dict) else "")
                    self._model.StartMotion(group, idx, self._live2d.MotionPriority.IDLE)
                    self._note_motion_started(fname, is_idle=True)
                    self._last_idle_start_at = now
                    return
            # fallback：StartRandomMotion（组名非空时有效）
            if self._motion_groups:
                group = next(iter(self._motion_groups))
                self._model.StartRandomMotion(group, self._live2d.MotionPriority.IDLE)
            else:
                self._model.StartRandomMotion(self._live2d.MotionGroup.IDLE,
                                              self._live2d.MotionPriority.IDLE)
            self._note_motion_started("idle", is_idle=True)  # fallback 视为 idle，不受超时限制
            self._last_idle_start_at = now
        except Exception as e:
            # 忽略 "motion priority is too low" 警告（正常行为，idle 被更高优先级 motion 打断）
            if "priority is too low" not in str(e):
                logger.warning("Live2DRenderer: 起始待机动作失败: %s", e)

    def _play_motion_kw(self, *groups, priority=None, from_arbiter: bool = False) -> bool:
        """按文件名关键词从 motion 列表找第一个匹配并播放。

        模型所有动作都在一个组（如空串 ""），组名匹配不上任何关键词，
        所以按 GetMotions() 的 File 文件名匹配（如 touch_head → "touch"+"head"）。
        支持多组备选关键词：每组内先 all 后 any 匹配，组间按顺序（前组优先）——
        兼容 lafei（main_1/2/3、home…）与 miku（waving/touch/thinking…）两种模型命名。

        Returns:
            True 表示找到了并播放；False 表示无匹配（调用方可回退）。
        """
        if not self._model or not self._motion_files:
            return False
        try:
            # 兼容旧式单组扁平调用（("main", "1")）→ 包成多组
            if groups and not isinstance(groups[0], (tuple, list)):
                groups = (groups,)
            for kws in groups:
                kws = [k.lower() for k in kws if k]
                if not kws:
                    continue
                # 严格：全部关键词命中
                for i, f in enumerate(self._motion_files):
                    low = f.lower()
                    if all(k in low for k in kws):
                        return self._start_motion_at(
                            i, priority, exclusive=True, from_arbiter=from_arbiter)
                # 宽松：任一关键词命中
                for i, f in enumerate(self._motion_files):
                    low = f.lower()
                    if any(k in low for k in kws):
                        return self._start_motion_at(
                            i, priority, exclusive=True, from_arbiter=from_arbiter)
            return False
        except Exception as e:
            # 忽略 "motion priority is too low" 警告（正常行为）
            if "priority is too low" not in str(e):
                logger.warning("Live2DRenderer._play_motion_kw 异常: %s", e)
            return False

    def _start_motion_at(
        self,
        idx: int,
        priority=None,
        force_restart: bool = False,
        exclusive: bool = False,
        from_arbiter: bool = False,
    ) -> bool:
        """按索引播 motion 并记录起始状态（卡手势超时兜底用）。

        去重：同一 motion 已在播（Loop=True 帧动画）时不重复 StartMotion、
        也不重置计时——否则 emotion 周期刷新（happy 每 3s 续期）会不断
        重置 _motion_started_at，卡手势超时兜底永不触发（比心/挥手持久）。

        force_restart=True 时跳过上述去重（即使同一 idx 也重新 StartMotion），
        供菜单手动播放使用——用户明确点同一个动作也应重新触发，而不是被
        "已在播"静默忽略。

        exclusive=True：启动前彻底清场（双重 StopAllMotions + ResetExpressions），
        确保新 motion 不会与旧 motion 或表情姿势（如"比心/葱"）叠加。
        用于菜单手动播放、自动随机动作、情绪触发动作等非 idle 场景。
        """
        try:
            fname = self._motion_files[idx] if idx < len(self._motion_files) else ""
            cur_idx = getattr(self, "_current_motion_idx", None)
            if not force_restart and idx == cur_idx and not getattr(self, "_motion_is_idle", False):
                if getattr(self, "_debug", False):
                    logger.debug("Live2DRenderer: 同一 motion 已在播(idx=%d)，去重跳过", idx)
                return True  # 继续播（Loop），不计时不受影响

            # ── 仲裁保护（2026-09-10 选项 A；2026-09-11 修正判据）──
            # AI 点名的动作在它自己声明的时长内，不被情绪/状态驱动的重播顶掉。
            #
            # ★ 判据用「有无时长」而不是「层≥DIALOG」。用层的写法会造成死锁：
            # behavior_mixin 提交主动挥手用的是 Layer.USER_INITIATED（=4）且
            # duration=0（永不过期），一旦触发就把 mixer 永久锁在层 4，
            # 之后所有 play_anim / 自动动作全被拦下。
            # 实测：15 分钟内 25 次随机动作只播了 2 次，且没有任何报错。
            #
            # from_arbiter=True 是动作本人（mixer 刚批准），放行；
            # force_restart=True 是用户手动点菜单，用户意图优先，也放行。
            if (exclusive and not force_restart and not from_arbiter
                    and self._mixer.has_bounded_active()):
                if getattr(self, "_debug", False):
                    logger.debug(
                        "Live2DRenderer: AI 有时长受限的动作在播，跳过重播 idx=%d", idx,
                    )
                return False
            prio = priority if priority is not None else self._live2d.MotionPriority.NORMAL
            if exclusive and self._model:
                # 关键：彻底清场，避免新旧动作/表情叠加
                # （2026-09-10: 原为 StopAllMotions 连调两次的重复块，去重）
                try:
                    if hasattr(self._model, "StopAllMotions"):
                        self._model.StopAllMotions()
                except Exception as e:
                    self._note_frame_failure("StopAllMotions", e)
                try:
                    if hasattr(self._model, "ResetExpressions"):
                        self._model.ResetExpressions()
                except Exception as e:
                    self._note_frame_failure("ResetExpressions", e)
                self._expression_active = False
                self._last_expression = ""
            self._model.StartMotion(self._motion_group_name, idx, prio)
            self._current_motion_idx = idx
            self._note_motion_started(fname, is_idle=False)
            # 2026-09-10：补上成功路径的可见性。
            # 原实现只在失败时报（而且大部分失败还静默），没播出来时无从判定到底
            # 是「没找到动作」还是「找到了但没画」—— 排查时只能靠猜。
            logger.info(
                "Live2DRenderer: 播放动作 idx=%d（%s）prio=%s group=%r",
                idx, fname or "?", prio, self._motion_group_name,
            )
            return True
        except Exception as e:
            logger.warning("Live2DRenderer.StartMotion 异常: %s", e)
            return False

    def _match_expression(self, emotion: str):
        """返回情绪对应的 Live2D 表情名（无匹配返回 None）。"""
        if emotion in ("neutral", ""):
            return None
        # pet.json 精确指定优先（emotions[emotion].expression，如 "脸红"）
        exact = getattr(self, "_emotion_exprs", {}).get(emotion)
        if exact:
            for name in self._expression_names:
                if str(name) == exact or exact.lower() in str(name).lower():
                    trace.record("expression", chosen=name, source=f"exact:{emotion}", note="pet.json精确")
                    return name
        kws = self._EMOTION_KEYWORDS.get(emotion, ())
        for name in self._expression_names:
            low = str(name).lower()
            if any(k in low for k in kws):
                trace.record("expression", chosen=name, source=f"kw:{emotion}", note=f"kws={list(kws)}")
                return name
        trace.record("expression", chosen="(none)", source=emotion, note="no-match")
        return None

    def _match_motion(self, emotion: str):
        """返回情绪对应的 motion 组名（无匹配返回 None）。"""
        groups = list(self._motion_groups.keys())
        kws = self._EMOTION_MOTION.get(emotion, ())
        for g in groups:
            low = str(g).lower()
            if any(k in low for k in kws):
                trace.record("motion", chosen=g, source=emotion, note=f"kws={list(kws)}")
                return g
        trace.record("motion", chosen="(none)", source=emotion, note="no-match")
        return None

    # ── 动画控制 ──

    # 精灵动画名/情绪 → Live2D motion 文件名关键词（模型动作全在空组，按文件名匹配）。
    # 值是多组备选关键词：每组按 all→any 匹配，组间按顺序（前组优先）。
    # 兼容两种模型命名：lafei（main_1/2/3、home、touch_head…）与 miku（happy/waving/touch…）
    _ANIM_TO_MOTION_KW = {
        "idle": (("idle",),),
        "waving": (("waving",), ("main", "1")),
        "happy": (("happy",), ("main", "1")),
        "walk": (("walk",), ("main", "2")),
        "sleep": (("sleep",), ("home",)),
        "working": (("working",), ("main", "3")),
        "thinking": (("thinking",), ("main", "3")),
        "failed": (("failed",), ("mission",)),
        "sad": (("sad",), ("mission",)),
        "surprised": (("surprised",), ("login",)),
        "angry": (("angry",), ("mission_complete",)),
        "touch": (("touch",), ("touch_head",)),
        "pat": (("touch",), ("touch_head",)),
        "stroke": (("stroke",), ("touch_body",)),
        "pet": (("stroke",), ("touch_body",)),
        "special": (("special",), ("touch_special",)),
        "wedding": (("wedding",),),
        "login": (("login",),),
        "mail": (("mail",),),
        "complete": (("complete",),),
    }

    def play_anim(self, anim: str, emotion: str = "", frame_range=None) -> bool:
        """播放动作。

        Returns:
            True 表示真的播了；False 表示无匹配（调用方不得声称已触发）。
            2026-09-10：原来返回 None，调用方无法区分「播了」与「没播」，
            导致 apply_action_intent 无条件打「已触发动作」。
        """
        # 2026-09-19 修：**先解析、成功了再改状态**。
        # 原来第一行就是 self._current_anim = anim，于是一个不存在的名字
        # （外部触发打错字 / MCP 传了没听过的名字）会把动画状态改坏：
        # 渲染循环随后去找一个不存在的序列 → **桌宠画面整体坏掉**
        # （实测帧差 204 = 画面完全变了，而日志里没有任何报错）。
        if emotion:
            # 缺陷② 修复：播放指定动作时，情绪只同步表情、不再重复播情绪 motion，
            # 否则“情绪 motion + 动作 motion”连着播放，出现「生气表情 + 唱歌动作」错位。
            self.set_emotion_expression_only(emotion)
        # Live2D：按精灵动画名映射到 motion 文件名播放（组名是空串匹配不上）
        kws = self._ANIM_TO_MOTION_KW.get(anim) or self._ANIM_TO_MOTION_KW.get(emotion)
        if kws and self._play_motion_kw(*kws):
            self._current_anim = anim
            return True
        # fallback：老逻辑（组名匹配）
        if self._model and anim in self._motion_groups:
            try:
                self._model.StartRandomMotion(anim, self._live2d.MotionPriority.NORMAL)
                self._note_motion_started("")  # 未知 motion → 按限时手势处理
                self._current_anim = anim
                return True
            except Exception:
                logger.debug("Live2DRenderer: 非致命异常(已静默吞掉)", exc_info=True)
        logger.warning(
            "Live2DRenderer.play_anim: 未知动作 %r，已忽略（不污染动画状态）可用: %s",
            anim, ", ".join(sorted(self._ANIM_TO_MOTION_KW)[:12]),
        )
        return False

    def set_emotion_expression_only(self, emotion: str) -> None:
        """仅同步情绪表情（不播动作）。给 play_anim 用，避免动作/表情错位。"""
        self._current_emotion = emotion
        self._emotion_target = emotion
        if not self._model:
            return
        self._apply_expression(emotion)

    def set_named_expression(self, name: str) -> bool:
        """**按表情名**直接设置（如「比心」/「唱歌」/「脸红」）。

        ## 为什么需要单独的入口（2026-09-20 真实 bug）

        `_apply_expression(emotion)` 收的是**情绪名**（happy/sad），
        内部走 `_match_expression(emotion)` —— 用**情绪关键词**去匹配表情。

        而 MCP 的 `pet_expression` 工具收的是**表情名**（比心/唱歌/葱）。
        它之前直接调 `_apply_expression(name)`，于是：

            传「比心」→ _match_expression("比心") → 找不到情绪关键词 → no-match

        实测（tools/verify_assets_via_mcp.py，真机 7 个表情）：
        **全部 no-match**，而工具还回「已派发」——静默失效。

        两个概念不同：情绪是「角色什么心情」，表情是「脸上贴哪个图」。
        这个入口直接按名字找 `_expression_names` 里的项。

        Returns:
            True 表示找到并设置；False 表示模型没有这个表情。
        """
        if not self._model or not name:
            return False
        # 精确匹配优先，其次大小写不敏感包含
        target = None
        for n in getattr(self, "_expression_names", []) or []:
            if str(n) == name:
                target = n
                break
        if target is None:
            low = name.lower()
            for n in getattr(self, "_expression_names", []) or []:
                if low in str(n).lower():
                    target = n
                    break
        if target is None:
            logger.warning(
                "Live2DRenderer.set_named_expression: 模型无此表情 %r，可用: %s",
                name, ", ".join(str(x) for x in (self._expression_names or [])) or "(无)",
            )
            return False
        try:
            # 与 _apply_expression 同样的防叠加：Set 前先 Reset
            self._model.ResetExpressions()
            self._model.SetExpression(target)
            self._expression_active = True
            self._expression_set_at = time.monotonic()
            self._last_expression = target
            logger.info("Live2DRenderer: 设置命名表情 %r", target)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("Live2DRenderer: 设置命名表情失败 %r: %s", target, e)
            return False

    def _apply_expression(self, emotion: str) -> None:
        """应用情绪对应的表情（不碰 motion）。

        P4-1 表情超时重置（根治“一直比心”）：
        - 模型如 miku 用 Param131-137 贴图开关做“比心/葱/唱歌/前倾”等手势表情。
        - 旧逻辑：happy 情绪每帧 set_emotion → SetExpression("比心") → 表情永不重置。
        - 修：同表情激活中不刷新超时（让手势自然过期）；超时后 ResetExpressions 回默认；
          重置后同表情进入冷却期，防止情绪持续时“3秒亮/3秒灭”闪烁。
          
        P1 修复：SetExpression 前先 ResetExpressions，杜绝多表情叠加（如“前倾+脸红”）。
        旧实现只 Set 不设 Reset，导致多个贴图表情同时激活。

        动作播放期间允许叠加表情：waving/happy 等肢体动作与比心/葱等脸部贴图表情
        共存是有价值的组合（用户夸桌宠时它正好在挥手 → 脸上也能同步比心）。
        冲突已由下面两道防线兜住：
          1) 同表情已激活时不刷新超时（不重复 Set）；
          2) Set 前先 ResetExpressions，确保新表情独占贴图参数、不叠加两个表情贴图。
        管线化集成（avatar/frame_pipeline.py）已把 idle/gesture 超时兜底拆成独立
        处理器，动作播放与表情叠加现在分属两个通道，可以并行。
        """
        expr = self._match_expression(emotion)
        try:
            if expr is None:
                # P2-9: 无论 _expression_active 真假，只要曾设过表情就重置
                if self._expression_active or self._last_expression:
                    self._model.ResetExpressions()
                # P2-10: 无条件重置，彻底清除贴图表情，不依赖簿记状态
                try:
                    self._model.ResetExpressions()
                except Exception as e:
                    self._note_frame_failure("ApplyExpressionReset", e)
                self._expression_active = False
                self._last_expression = ""
                return
            now = time.monotonic()
            # 同表情已激活：不重复设置、不刷新超时（让手势自然过期重置）
            if self._expression_active and expr == self._last_expression:
                return
            # 刚被超时重置的同表情，冷却期内不重播（防闪烁）
            if expr == self._last_expression and now < self._expression_suppress_until:
                return
            # P1: SetExpression 前先 Reset，确保新表情独占总有贴图参数
            self._model.ResetExpressions()
            # T2-2b: Blend 折算 — 检测模型是否支持 Blend 模式
            try:
                # 尝试获取 Blend 参数（Live2D SDK 可能不支持）
                if hasattr(self._model, 'GetExpressionBlendMode'):
                    blend_mode = self._model.GetExpressionBlendMode(expr)
                    if blend_mode == 'Add':
                        # Add 模式：叠加到当前表情
                        self._model.SetExpression(expr, weight=0.5)
                    elif blend_mode == 'Multiply':
                        # Multiply 模式：乘法混合
                        self._model.SetExpression(expr, weight=0.7)
                    else:
                        # Overwrite 模式（默认）：覆盖
                        self._model.SetExpression(expr)
                else:
                    # 不支持 Blend 检测，使用默认覆盖
                    self._model.SetExpression(expr)
            except Exception:
                # Blend 不支持，回退到默认覆盖
                self._model.SetExpression(expr)
            self._expression_active = True
            self._expression_set_at = now
            self._last_expression = expr
        except Exception as e:
            logger.warning("Live2DRenderer: 设置表情失败: %s", e)

    def _expire_expression_if_stale(self) -> None:
        """表情超时兜底：非中性表情设置超过 GESTURE_TIMEOUT 则自动重置回默认。

        与 motion 的 GESTURE_TIMEOUT 兜底对称——避免"比心/葱/唱歌"等贴图开关表情
        因情绪持续 happy 而永久显示（用户反复反馈的"一直比心"）。
        重置后进入冷却期（GESTURE_TIMEOUT），期间 _apply_expression 不会重播同表情，
        防止情绪持续时"3秒亮/3秒灭"的闪烁。
        """
        if not self._expression_active or not self._model:
            return
        now = time.monotonic()
        if now - self._expression_set_at > self.GESTURE_TIMEOUT:
            try:
                self._model.ResetExpressions()
            except Exception as e:
                # 失败 = 表情卡住不回落，与「已重置」的日志矛盾
                self._note_frame_failure("ExpireExpression", e)
            self._expression_active = False
            self._expression_suppress_until = now + self.GESTURE_TIMEOUT
            logger.info("Live2DRenderer: 表情超时(%ds)，已自动重置回默认，%s 秒冷却",
                        self.GESTURE_TIMEOUT, self.GESTURE_TIMEOUT)

    def set_emotion(self, emotion: str, intensity: float = 1.0) -> None:
        # T10: 同步 V/A 目标坐标（情绪切换变成 V/A 空间路径插值）
        # 2026-09-10：与 set_master_emotion 同理——[feel:] 保持期内不被覆盖。
        if time.monotonic() >= getattr(self, "_va_hold_until", 0.0):
            self._va_target = self._EMOTION_VA.get(emotion, (0.0, 0.0))
        self._emotion_intensity = max(0.0, min(1.0, intensity))
        # 同一 emotion 短时间内重复调用：直接同步表情，不重播 motion。
        # 真实场景：_unified_tick 每秒检查 emotion 并 set_emotion，
        # 若屏幕感知把 emotion 设为 happy，每秒都会触发 happy 调用，
        # 全局 cooling 3 秒挡不住这种"每秒一次"的节奏 → 比心永远切不回来。
        # 修：上次同 emotion 调用距今 < GESTURE_TIMEOUT 时，直接同步表情 return。
        # 防御：load() 失败的渲染器（_model 为 None，如占位角色无 live2d/ 目录）
        # 每帧仍被 tick 无条件调用本方法，必须在任何属性访问之前拦成安全 no-op，
        # 否则访问未初始化属性（self._emotion_motion_cooldown 等）抛 AttributeError。
        if not self._model:
            return
        now = time.monotonic()
        last_same_at = self._emotion_motion_cooldown.get(f"_lastcall:{emotion}", 0.0)
        if emotion == getattr(self, "_current_emotion", None) and now - last_same_at < self.GESTURE_TIMEOUT:
            self._apply_expression(emotion)
            return
        self._current_emotion = emotion
        self._emotion_target = emotion

        # 全局 gesture 冷却：任何非 idle motion 播放后，GESTURE_TIMEOUT 内不再播新 motion。
        # 这是防"比心/挥手持久卡死"的核心：屏幕感知、对话回复、鼠标交互可能高频推同一
        # 情绪，若每次都能重新触发 motion，就会不断重置计时、手势永不回 idle。
        last_gesture_at = getattr(self, "_last_gesture_at", 0.0)
        in_global_gesture_cooldown = (
            not getattr(self, "_motion_is_idle", True)
            and now - self._motion_started_at < self.GESTURE_TIMEOUT
        ) or (
            getattr(self, "_motion_is_idle", True)
            and now - last_gesture_at < self.GESTURE_TIMEOUT
        )
        if in_global_gesture_cooldown:
            logger.debug(
                "Live2DRenderer: 全局 gesture 冷却中（%.1fs/%.1fs），emotion=%s 只同步表情",
                now - max(self._motion_started_at, last_gesture_at),
                self.GESTURE_TIMEOUT, emotion,
            )
            self._emotion_motion_cooldown[f"_lastcall:{emotion}"] = now
            self._apply_expression(emotion)
            return

        # emotion 级冷却：同一情绪在 GESTURE_TIMEOUT 内不重播（即便已回 idle）。
        last_at = self._emotion_motion_cooldown.get(emotion, 0.0)
        in_emotion_cooldown = getattr(self, "_motion_is_idle", True) and now - last_at < self.GESTURE_TIMEOUT
        if in_emotion_cooldown:
            logger.debug("Live2DRenderer: emotion=%s 仍在 %.1fs 冷却期，只同步表情", emotion, self.GESTURE_TIMEOUT)
            self._emotion_motion_cooldown[f"_lastcall:{emotion}"] = now
            self._apply_expression(emotion)
            return

        # 优先：pet.json emotions[emotion].anim 精确指定 → motion 文件名关键词。
        # 例：miku 的 touched → "touch"（touch.motion3.json）、happy → "happy"。
        anim_name = getattr(self, "_emotion_anims", {}).get(emotion)
        kws = self._ANIM_TO_MOTION_KW.get(emotion)
        motion_played = False
        if anim_name:
            motion_played = self._play_motion_kw((anim_name,))
            if not motion_played and kws:
                motion_played = self._play_motion_kw(*kws)
        elif kws:
            motion_played = self._play_motion_kw(*kws)
        if not motion_played:
            # 回退：情绪 → motion 组
            motion = self._match_motion(emotion)
            if motion:
                try:
                    # 清场：避免情绪 motion 与旧手势/表情叠加
                    if self._model:
                        if hasattr(self._model, "StopAllMotions"):
                            try:
                                self._model.StopAllMotions()
                            except Exception as e:
                                self._note_frame_failure("EmotionStopMotions", e)
                        if hasattr(self._model, "ResetExpressions"):
                            try:
                                self._model.ResetExpressions()
                            except Exception as e:
                                self._note_frame_failure("EmotionResetExpressions", e)
                    self._expression_active = False
                    self._last_expression = ""
                    self._model.StartRandomMotion(motion, self._live2d.MotionPriority.FORCE)
                    motion_played = True
                except Exception as e:
                    # 切情绪但动作没换 —— 用户能看出「表情变了动作没变」
                    self._note_frame_failure("EmotionMotion", e)
        # 记录该情绪 motion 的播放时间，用于 emotion 级冷却
        if motion_played:
            self._emotion_motion_cooldown[emotion] = time.monotonic()
            self._last_gesture_at = time.monotonic()
        self._emotion_motion_cooldown[f"_lastcall:{emotion}"] = now
        # 表情
        self._apply_expression(emotion)

    # ── 结构化动作意图（[action:{...}]）─────────────────────

    def hit_detect(self, screen_x: int, screen_y: int) -> str | None:
        """P2: 双击部位检测 — 返回命中的区域名（head/body/touch 等）。

        使用 live2d-py 的 HitPart API 检测点击位置对应的模型部分。
        坐标需要转换为模型画布坐标（考虑缩放和偏移）。
        """
        if not self._model or not self._ready:
            return None
        try:
            # 获取模型画布尺寸和像素/单位比例
            canvas_w, canvas_h = self._model.GetCanvasSize()
            ppu = self._model.GetPixelsPerUnit()
            if canvas_w <= 0 or ppu <= 0:
                return None
            # 获取当前缩放和偏移
            fit = getattr(self, '_fit_scale', 1.0)
            offx = getattr(self, '_center_offset_x', 0.0)
            offy = getattr(self, '_offset_scale', (0.0, 0.0))[1]
            # 窗口像素 → 模型画布坐标
            win_w, win_h = self.char_label.size().width(), self.char_label.size().height()
            # 相对窗口位置（0-1）
            rx = screen_x / win_w if win_w > 0 else 0.5
            ry = screen_y / win_h if win_h > 0 else 0.5
            # 画布坐标（考虑居中偏移）
            cx = (rx - 0.5) * canvas_w * fit + canvas_w / 2 + offx
            cy = (ry - 0.5) * canvas_h * fit + canvas_h / 2 - offy
            # HitPart 返回命中的 part 列表
            parts = self._model.HitPart(cx, cy, topOnly=False)
            if not parts:
                return None
            # 尝试匹配常见部位名
            for p in parts:
                pname = p.lower() if isinstance(p, str) else str(p).lower()
                if 'head' in pname or 'face' in pname:
                    return 'head'
                elif 'body' in pname or 'torso' in pname:
                    return 'body'
                elif 'hair' in pname:
                    return 'hair'
                elif 'arm' in pname or 'hand' in pname:
                    return 'hand'
                elif 'leg' in pname or 'foot' in pname:
                    return 'foot'
                elif 'eye' in pname:
                    return 'eye'
            # 无匹配区域名，返回第一个 part 名作为 fallback
            return str(parts[0])[:20] if parts else None
        except Exception as e:
            logger.debug("hit_detect 失败: %s", e)
            return None

    def apply_action_intent(self, intent: dict) -> None:
        """应用结构化动作意图（任意动作；向后兼容 [emotion:xxx] 标签路径）。

        intent ∈ {"gesture": str, "intensity": float, "params": dict, "duration": float}：
        - params 非空 → 作为 Live2D 直接参数目标（如 smile/eye_smile/ParamAngleX），
          复用 _update_procedural_emotion 的每帧平滑插值过渡到目标（不跳变）。
        - gesture 非空 → 触发对应 motion/expression（情绪名走表情+对应 motion，
          否则当作 motion 组名尝试播放）。
        - duration > 0 → 通过 MotionMixer 提交，指定秒后自动回 idle。
        - va=[v,a] → 直接驱动连续 VA 坐标（_va_target），逐帧插值到面部参数。
        - 缺省/非法字段安全忽略（不抛异常）。
        """
        if not isinstance(intent, dict):
            return
        gesture = intent.get("gesture")
        params = intent.get("params")
        duration = intent.get("duration", 0.0)
        try:
            intensity = float(intent.get("intensity", 1.0))
        except (TypeError, ValueError):
            intensity = 1.0
        intensity = max(0.0, min(1.0, intensity))

        # ── [feel:v,a] 连续 VA 坐标（2026-09-10）──
        # 直驱渲染器已有的逐帧插值层：_va_target ──平滑──▶ _va_cur ──插值──▶ 面部参数。
        # 这是「AI 自己做细微动作」的入口：连续值 → 平滑逼近，而非 7 档跳变。
        _va = intent.get("va")
        if (isinstance(_va, (list, tuple)) and len(_va) == 2):
            try:
                self._va_target = (
                    max(-1.0, min(1.0, float(_va[0]))),
                    max(-1.0, min(1.0, float(_va[1]))),
                )
                # 保持期内不让离散情绪覆盖（默认与兜底 duration 一致：3s）
                _hold = float(duration) if duration and duration > 0 else 3.0
                self._va_hold_until = time.monotonic() + _hold
                logger.info(
                    "设定 VA 目标: %.2f, %.2f（保持 %.1fs）",
                    self._va_target[0], self._va_target[1], _hold,
                )
            except (TypeError, ValueError):
                logger.warning("apply_action_intent: va 数值非法，忽略: %r", _va)
        
        # 调试日志：记录动作意图
        logger.info("apply_action_intent: gesture=%s, intensity=%.1f, params=%s, duration=%.1f",
                    gesture, intensity, params, duration)

        # 如果有 duration，通过 MotionMixer 提交（带自动过期）
        if duration > 0 and params:
            req = MotionRequest(
                layer=Layer.DIALOG,
                motion_group=gesture,
                params=params,
                duration=duration,
                name="intent",
            )
            self.submit_motion_request(req)
            logger.info("已提交 MotionRequest: gesture=%s, duration=%.1f", gesture, duration)
            return

        # 无 duration：保持当前行为（直接设置参数，无自动过期）
        if isinstance(params, dict) and params:
            self._set_intent_params(params, intensity)
            logger.info("已设置表情参数: %s (intensity=%.1f)", params, intensity)
        if gesture:
            played = self._trigger_gesture(gesture, intensity)
            if played:
                logger.info("已触发动作: %s (intensity=%.1f)", gesture, intensity)
            elif self._mixer.has_bounded_active():
                # 被仲裁保护拦下：有时长受限的动作正在场，属正常情况（不是“没找到动作”）
                logger.info(
                    "动作 %s 未执行：当前有时长受限的动作在场（仲裁保护，待其到期后生效）",
                    gesture,
                )
            else:
                # 2026-09-10：原实现无条件打「已触发动作」，即使什么都没播。
                # 日志声称成功而实际无声，是排查时的最大干扰源。
                logger.warning(
                    "动作未生效: gesture=%s 未匹配到任何 motion（模型 motion 列表：%s）",
                    gesture, [f for f in self._motion_files] or "空",
                )

    def _set_intent_params(self, params: dict, intensity: float) -> None:
        """把 params 字典归一化为平滑目标值（按 intensity 缩放幅度）。

        只保留字符串键 + 数值值的合法项；其余忽略。新意图会重置目标集合
        （未提及的参数即视为“释放”，不再作为目标写入）。

        W3（2026-09-14）：同时记录设置时刻，供 `_expire_intent_params` 做超时释放。
        旧实现下 `_param_intent` **永不过期**——LLM 一次 `[action:{params:...}]`
        写入的嘴型/眉眼会一直挂着，直到下一次 intent 到来（可能永不）。
        这是用户反复反馈的“表情卡住”的另一条路径（与贴图表情超时同源）。
        """
        targets: dict[str, float] = {}
        for k, v in params.items():
            if not isinstance(k, str) or not k:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            targets[k] = fv * intensity
        self._param_intent = targets
        self._param_intent_set_at = time.monotonic()
        # 不清空 _param_cur：保留当前平滑值作为起点，继续平滑过渡。

    def _expire_intent_params(self) -> None:
        """动作意图参数超时释放（W3）。

        用户要求“情绪结束就收”，而非固定 N 秒。这里处理的是**显式参数意图**
        （LLM `[action:{params:...}]`）——它不由情绪驱动，无法“随情绪收回”，
        所以给它一个可配的保留时长（默认 6s）：到期后清空目标集，
        `_update_procedural_emotion` 会把参数平滑回情绪基准值（而非硬切）。

        到期释放而非永久保持，是旧实现下“嘴型/眉眼永久卡住”的根因修复。
        新 intent 到来时会重置计时（见 _set_intent_params）。
        """
        if not self._param_intent:
            return
        now = time.monotonic()
        ttl = getattr(self, "_param_intent_ttl", self.PARAM_INTENT_TTL)
        if ttl <= 0:
            return  # 配 0/负数 = 关闭超时（保持旧行为，便于对照排查）
        if now - getattr(self, "_param_intent_set_at", 0.0) > ttl:
            self._param_intent = {}
            logger.debug("动作意图参数超时释放（%.1fs）", ttl)

    def _trigger_gesture(self, gesture, intensity: float) -> bool:
        """gesture 名 → 触发对应 motion/expression。

        - 已知情绪名（config.EXPRESSION_MAP）→ 同步表情并播放对应 anim 的 motion。
        - 否则当作 motion 组名直接播放（play_anim 内部会按 _ANIM_TO_MOTION_KW /
          motion 组匹配，无匹配则安全忽略）。

        Returns:
            True 表示真的播了；False 表示无匹配（调用方不得声称已触发）。
        """
        if not gesture or not isinstance(gesture, str):
            return False
        g = gesture.strip().lower()
        if not g:
            return False

        # ── 0. 语义别名归一（2026-09-11）──
        # AI 输出的是「害羞/挥手」这类语义词；这里归一到具体预设/motion 名，
        # 后续三类查找逻辑不变。
        # 为何需要这层：直接把 66 个预设/motion 名给模型，实测 `[do:]`
        # 全历史 0 次使用（见 _AI_DO_ALIASES 注释）。
        #
        # ⚠️ 先记下别名表是否命中：快照标签层（0.5）只应在**未命中**时兵底，
        # 不能盖掉人工调的语义优先表（如「挥手」→ motion `waving`，
        # 比标签的 `arm_wave` 预设更符合直觉）。
        alias_hit = g in self._AI_DO_ALIASES
        g = self._AI_DO_ALIASES.get(g, g)

        # ── 0.5 快照中文标签归一（2026-09-20）──
        # 为什么需要：prompt 里给模型的候选是**中文语义标签**
        # （「灿烂笑容」「害羞脸红」「瞪大眼睛」——见 _AI_DO_PROMPT）。
        # 但 `_AI_DO_ALIASES` 只收了 69 条常用词，而快照有 53 个标签，
        # 两者不是同一个集合——实测 33 个标签（如「灿烂笑容」）过完别名表
        # 仍是中文，落到 play_anim 就变成「未知动作」→ **模型说对了却播不出来**。
        #
        # 这层把标签直接查回预设名，不再依赖别名表把每个标签都手抄一遍。
        # 标签表由 core/capability_snapshot.PRESET_LABELS 提供（单一一来源）。
        if not alias_hit and g not in self._EMOTE_PRESETS:
            try:
                from core.capability_snapshot import LABEL_TO_PRESET

                g = LABEL_TO_PRESET.get(g, g)
            except Exception:
                logger.debug("live2d_renderer: 标签归一失败（忽略）", exc_info=True)

        # ── 1. 表情预设（2026-09-11 接线）──
        # avatar/emote_presets.py 里有 53 个带步骤序列的预设（wink / blush_shy /
        # pout / head_tilt / gaze_shift…），比裸参数（smile=80）表现力强得多。
        # 但它们原本**只能被 _pick_emote_preset() 随机挑中**，AI 点不了名。
        #
        # 预设优先于 motion：它粒度更细，且不依赖模型是否有对应 motion 文件
        # （纯参数驱动，缺参数会被 ParamWriter 按白名单跳过）。
        if g in self._EMOTE_PRESETS:
            try:
                if self.play_emote_sequence(g):
                    logger.info("已播放表情预设: %s", g)
                    return True
            except Exception as e:
                self._note_frame_failure("EmotePreset", e)

        try:
            from config import EXPRESSION_MAP
            if g in EXPRESSION_MAP:
                anim = (EXPRESSION_MAP.get(g) or (None,))[0] or "idle"
                return bool(self.play_anim(anim, emotion=g))
        except Exception as e:
            self._note_frame_failure("GestureExpressionMap", e)
        try:
            return bool(self.play_anim(g))
        except Exception as e:
            # 失败 = 用户点击/触发手势，桌宠毫无反应
            self._note_frame_failure("GesturePlayAnim", e)
            return False

    # ── 视线 ──

    def look_at(self, x: int, y: int) -> None:
        if not self._gaze_enabled or not self._model:
            return
        # 鼠标全局坐标 -> 控件局部坐标
        pos = self.char_label.mapFromGlobal(QPoint(x, y))
        cx = self.char_label.width() / 2.0
        cy = self.char_label.height() / 2.0
        dx = pos.x() - cx
        dy = pos.y() - cy
        dist = math.hypot(dx, dy) or 1.0
        norm_x = dx / dist
        norm_y = dy / dist
        strength = min(1.0, dist / 300.0)
        self._gaze_target_angle_x = norm_x * 25.0 * strength
        self._gaze_target_angle_y = -norm_y * 20.0 * strength
        self._gaze_target_ball_x = norm_x * 1.0 * strength
        self._gaze_target_ball_y = -norm_y * 1.0 * strength

    def set_gaze_enabled(self, enabled: bool) -> None:
        self._gaze_enabled = enabled
        if not enabled:
            self._gaze_target_angle_x = self._gaze_target_angle_y = 0.0
            self._gaze_target_ball_x = self._gaze_target_ball_y = 0.0

    def update_gaze(self) -> None:
        """每帧调用（pet 的平滑定时器）；实际插值在 draw 中完成。"""
        pass

    def reset_gaze(self) -> None:
        self._gaze_target_angle_x = self._gaze_target_angle_y = 0.0
        self._gaze_target_ball_x = self._gaze_target_ball_y = 0.0

    def get_char_top_y(self) -> int:
        return self.char_label.y()

    # ── 说话（TTS 口型） ──

    def set_speaking(self, speaking: bool) -> None:
        self._speaking = bool(speaking)

    def set_mouth_level_source(self, fn) -> None:
        """接入实时音频电平源（返回 0.0~1.0 的 RMS；None/异常表示不可用）。

        由 PetWindow 在播放器就绪后调用，把 StreamingPcmPlayer.current_level
        接进来。未接入时 _update_mouth 自动回退正弦包络。
        """
        self._mouth_level_fn = fn if callable(fn) else (lambda: None)

    def set_lip_timeline(self, frames, clock_fn=None) -> None:
        """接入口型时间轴（LIP-1）。

        Args:
            frames: `core.lip_sync.LipFrame` 列表（按时间升序）。
                空列表/None = 清除，回退到振幅/正弦。
            clock_fn: 返回"当前播放位置（秒）"的可调用对象。
                未提供时用内部计时器（从 set_lip_timeline 起算）。

        由 PetWindow 在 TTS begin 时调用（那时文本已完整，可预生成时间轴）。
        """
        self._lip_frames = list(frames) if frames else []
        self._lip_clock_fn = clock_fn if callable(clock_fn) else None
        self._lip_started_at = time.monotonic()

    def clear_lip_timeline(self) -> None:
        """清除口型时间轴（TTS 结束时调用）。"""
        self._lip_frames = []
        self._lip_clock_fn = None
        self._mouth_lip_open = 0.0
        self._mouth_lip_form = 0.0

    def _read_lip_shape(self):
        """读当前应采用的 (开口度, 唇形)。无时间轴/超出范围返回 None。

        热路径（每帧调用）：时间轴为空时直接返回 None（零开销）。
        """
        frames = getattr(self, "_lip_frames", None)
        if not frames:
            return None
        try:
            from core.lip_sync import sample as _lip_sample
            if self._lip_clock_fn is not None:
                at = float(self._lip_clock_fn())
            else:
                at = time.monotonic() - getattr(self, "_lip_started_at", 0.0)
            return _lip_sample(frames, at)
        except Exception:
            logger.debug("Live2DRenderer: 口型时间轴采样失败（回退振幅）", exc_info=True)
            return None

    def _read_mouth_level(self):
        """读一次电平，失败一律返回 None（走正弦回退）。

        渲染帧循环不能被电平源拖崩：源可能跨线程、设备可能已释放。
        """
        fn = getattr(self, "_mouth_level_fn", None)
        if fn is None:
            return None
        try:
            level = fn()
        except Exception:
            logger.debug("Live2DRenderer: 读口型电平失败（回退正弦）", exc_info=True)
            return None
        if level is None:
            return None
        try:
            return max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            return None

    # ── 变换 ──

    def set_position(self, x: int, y: int) -> None:
        pass

    def get_size(self) -> tuple[int, int]:
        return (self.char_label.width(), self.char_label.height())

    def set_scale(self, scale: float) -> None:
        self._scale = scale
        w = int(220 * scale)
        h = int(260 * scale)
        self.char_label.setFixedSize(w, h)
        self._base_label_pos = QPoint(10, 0)
        self._recompute_fit()

    def get_scale(self) -> float:
        return self._scale

    def recalc_geometry(self, window_w: int, window_h: int) -> None:
        # 用窗口尺寸设置角色 label（不再用固定 220x260 基准——那比 200 宽的窗口还宽，
        # 导致角色宽度溢出被窗口裁切、显示不完整）。
        # 模型是正方形(1200px)，label 用窗口宽，角色按高度填满。
        # 注意：window_w/window_h 已是缩放后的最终尺寸（pet.py _recalc_geometry 传入），
        # 这里不再乘 _scale，避免双重缩放导致 label 尺寸异常（窗口框远超模型）。
        w = int(window_w)
        h = int(window_h)
        self.char_label.setFixedSize(w, h)
        self._base_label_pos = QPoint(0, 0)
        self._recompute_fit()

    def set_facing(self, right: bool) -> None:
        self._facing_right = right
        # Live2D 是 3D 立绘，保持正面：水平镜像（SetScaleX 负值）会把模型整体
        # 左右翻转，与画布偏右的居中补偿（SetOffsetX 左移）叠加，转向后模型
        # 明显偏左/偏右——用户反馈的“又偏左/偏右”就是镜像+居中的冲突。
        # 镜像朝向是精灵图桌宠的玩法，Live2D 不用。这里仅记录 facing 状态，
        # 供 walk 动画/交互语义使用，不改 model 矩阵。若将来要“转身”，
        # 应走 Live2D 参数（如 ParamAngleY 体感转向）而非负 scale。
        if self._model and getattr(self, "_mirror_facing_enabled", False):
            try:
                # 用像素画布尺寸计算缩放（与 _recompute_fit 一致，按高度缩放）
                cw_px, ch_px = self._model.GetCanvasSizePixel()
                if not cw_px or not ch_px:
                    ppu = self._model.GetPixelsPerUnit() or 1.0
                    cw_log, ch_log = self._model.GetCanvasSize()
                    cw_px, ch_px = cw_log * ppu, ch_log * ppu
                fit = abs(self._fit_scale)
                sx = abs(getattr(self, "_fit_scale_x", 1.0))
                self._model.SetScaleX(fit * sx * (1 if right else -1))
                self._model.SetScaleY(fit)
            except Exception:
                logger.debug("Live2DRenderer: 非致命异常(已静默吞掉)", exc_info=True)

    def get_facing(self) -> bool:
        return self._facing_right

    def set_label_base_pos(self, pos: QPoint) -> None:
        self._base_label_pos = pos

    # ── 透明度 ──

    def set_alpha(self, alpha: float) -> None:
        alpha = max(0.0, min(1.0, alpha))
        self._opacity = alpha
        try:
            self.char_label.setWindowOpacity(alpha)
        except Exception:
            logger.debug("Live2DRenderer: 非致命异常(已静默吞掉)", exc_info=True)

    def get_alpha(self) -> float:
        return self._opacity

    # ── 兼容接口 ──

    @property
    def label(self):
        return self.char_label

    @property
    def eye_overlay(self):
        return None

    def show_eyes(self):
        pass

    def hide_eyes(self):
        self.reset_gaze()

    def cleanup(self) -> None:
        # 注意：这里不调用 self._live2d.glRelease()。self._live2d 是进程级的 live2d.v3 模块，
        # 其 glRelease() 会释放全局 GL 状态。多宠场景下关闭某个 Live2D 宠就释放全局 GL，
        # 会导致其它仍在渲染的 Live2D 宠崩坏。GL 上下文由各自的 QOpenGLWidget 自行管理，
        # 进程退出时系统自动回收，因此单个 renderer 清理时不应释放全局 GL。
        try:
            if self._model is not None:
                self._model = None
            if self._live2d is not None:
                self._live2d = None
        except Exception:
            logger.debug("Live2DRenderer: 非致命异常(已静默吞掉)", exc_info=True)
