"""avatar/motion_mixer.py — 层优先级仲裁的统一混流层（T08 · D2）

替换现有 5 道互斥补丁（emotion_motion_cooldown / _motion_is_idle /
_emote_seq_active / GESTURE_TIMEOUT 超时回 idle / b1f543c 非 idle 禁叠加），
用显式层优先级仲裁统一处理「谁占哪条通道、谁能打断谁」。

层优先级（高→低）：
    USER_INITIATED (4) — 用户触发动作（鼠标点击/拖拽/waving）
    DIALOG         (3) — 对话驱动表情（气泡对话、回复态）
    SCREEN         (2) — 屏幕感知/情绪触发动作
    IDLE           (1) — 待机微摆动

仲裁规则：
    - 高优先级可打断低优先级
    - 同优先级不打断（first-come-first-served）
    - 强制重置（force_reset）清除所有层，进入 3 秒防闪烁冷却

线程安全：非线程安全；由 Live2DRenderer 在主线程 tick 中串行调用。

动作过渡（easing）
    Live2D 的 motion 切换默认硬切（StopAllMotions 后 StartMotion），视觉生硬。
    mixer 层记录每次成功仲裁切换的「切换起始时间 + 前一活跃层」，配合
    Live2DRenderer 里对 SetExpression/ProgramParam 的 weight ramp 实现
    ~0.3s 的 easeOut 平滑过渡；motion 文件本身的 blend 目前受限于
    live2d-py wrapper 未暴露 motion weight API，仍为硬切，但表情/参数层
    已具备平滑基础（后续拿到 motion weight 接口可继续下沉）。
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Callable


# ── Easing 曲线（动作/表情过渡的缓动函数库）──
#
# 签名：(t: float in [0,1]) -> float in [0,1]。
# 约定：t 为归一化时间（0=切换起点、1=过渡结束），返回值作为新旧状态的插值系数。
# Live2DRenderer 在每帧 tick 中用 get_transition() 拿 t，再调下面任一曲线得到 w，
# 用 w 做 SetExpression 权重/参数目标值插值，实现 0.3s 平滑过渡。

def linear(t: float) -> float:
    """线性：无缓动（保留给调试或强制硬过渡）。"""
    return max(0.0, min(1.0, t))


def easeIn(t: float) -> float:
    """easeIn (t*t)：慢启动、快收尾。动作加速起势用。"""
    t = max(0.0, min(1.0, t))
    return t * t


def easeOut(t: float) -> float:
    """easeOut (1-(1-t)^2)：快启动、慢收尾。动作/表情默认曲线——手感最自然。"""
    t = max(0.0, min(1.0, t))
    return 1.0 - (1.0 - t) * (1.0 - t)


def easeInOut(t: float) -> float:
    """easeInOut（抛物线两段拼接）：中点最大速度，对称柔滑。适合大幅度动作。"""
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        return 2.0 * t * t
    return 1.0 - (-2.0 * t + 2.0) ** 2 / 2.0


_EASINGS: dict[str, Callable[[float], float]] = {
    "linear": linear,
    "easeIn": easeIn,
    "easeOut": easeOut,
    "easeInOut": easeInOut,
}


def get_easing(name: str) -> Callable[[float], float]:
    """按名称取缓动函数；未知名称回退到 easeOut（永不抛异常）。"""
    if not isinstance(name, str):
        return easeOut
    return _EASINGS.get(name, easeOut)


# ── 层优先级 ──

class Layer(IntEnum):
    """动作来源层。值越大优先级越高。"""
    IDLE = 1
    SCREEN = 2
    DIALOG = 3
    USER_INITIATED = 4

    @classmethod
    def from_str(cls, name: str) -> "Layer":
        mapping = {
            "idle": cls.IDLE,
            "screen": cls.SCREEN,
            "dialog": cls.DIALOG,
            "user": cls.USER_INITIATED,
            "user_initiated": cls.USER_INITIATED,
        }
        return mapping.get(name.lower(), cls.IDLE)


# ── 混流请求 ──

@dataclass
class MotionRequest:
    """一次动作/表情提交请求。"""
    layer: Layer
    motion_group: Optional[str] = None      # motion 组名（"happy"/"waving"等）
    expression_name: Optional[str] = None   # 表情名（SetExpression 用）
    params: Optional[dict] = None           # 直接参数目标（走程序化表情层）
    duration: float = 0.0                  # 0=直到被更高优先级替换；>0=自动过期
    can_interrupt: bool = True              # 同优先级是否允许打断
    name: str = ""                          # 日志用名称


# ── 当前活跃状态快照 ──

@dataclass
class ActiveState:
    """当前混流层仲裁结果（供 render 读取）。"""
    motion_group: Optional[str] = None
    expression_name: Optional[str] = None
    params: Optional[dict] = None
    layer: Layer = Layer.IDLE
    name: str = ""


# ── 混流器 ──

class MotionMixer:
    """统一混流：层优先级仲裁，替换 5 道互斥补丁。

    设计原则：
    - 仲裁只在上层做（mixer 层）；底层不假设 wrapper 优雅
    - 强制重置（force_reset）保留为独立接口，供 _force_idle 调用
    - 3 秒防闪烁冷却在 force_reset 后生效
    """

    # 强制重置后的防闪烁冷却秒数（沿用 3be390a）
    RESET_COOLDOWN_S: float = 3.0
    # 动作/表情切换的默认过渡时长（秒）。0 表示硬切（旧行为）。
    TRANSITION_S: float = 0.3
    # 默认缓动曲线（easeOut）：起手快、收势柔——符合「刚接上→自然放松」的动作语义。
    DEFAULT_EASING: str = "easeOut"

    def __init__(self) -> None:
        self._requests: list[MotionRequest] = []
        self._active_idx: int = -1
        self._active_since: float = 0.0
        self._last_reset_at: float = 0.0
        self._next_id: int = 0
        # 动作过渡状态（easing 基础设施）：
        #   _transition_started_at: 最近一次成功切换的起始时刻（monotonic）
        #   _transition_from:       切换前的活跃请求（None=首次/刚 reset）
        #   _transition_duration_s: 过渡总时长（<=0 表示硬切）
        #   _transition_easing:     缓动曲线名称
        # 供 Live2DRenderer 每帧查询 _transition_progress() 得到 0~1 缓动值，
        # 用于对 SetExpression/程序化参数做 weight ramp（motion 文件本身仍硬切）。
        self._transition_started_at: float = 0.0
        self._transition_from: Optional[MotionRequest] = None
        self._transition_duration_s: float = self.TRANSITION_S
        self._transition_easing: str = self.DEFAULT_EASING

    # ── 提交 ──

    def submit(self, req: MotionRequest) -> bool:
        """提交动作请求。返回 True 表示请求被接受（可能替换当前活跃请求）。

        仲裁规则：
        1. 防闪烁冷却期内，非 force 请求一律拒绝
        2. 高优先级可打断低优先级 → 替换
        3. 同优先级：can_interrupt=True 才替换，否则拒绝
        4. 低优先级一律拒绝
        """
        if self._in_reset_cooldown():
            return False

        now = _time.monotonic()
        if not self._has_active():
            self._requests.append(req)
            self._active_idx = len(self._requests) - 1
            self._active_since = now
            # 首次进入活跃：从「无」过渡到 req，也记录一个切换起点
            # （Live2D 首次播放也应有淡入感；无前一状态时 duration 由 renderer 侧兜底）。
            self._transition_started_at = now
            self._transition_from = None
            return True

        current = self._active()
        if req.layer > current.layer:
            self._active_idx = len(self._requests)
            self._requests.append(req)
            self._active_since = now
            self._record_transition(current, req, now)
            return True

        if req.layer < current.layer:
            return False

        # 同优先级
        if req.can_interrupt and current.can_interrupt:
            self._active_idx = len(self._requests)
            self._requests.append(req)
            self._active_since = now
            self._record_transition(current, req, now)
            return True
        return False

    def _record_transition(
        self, from_req: MotionRequest, to_req: MotionRequest, now: float
    ) -> None:
        """记录一次成功的层切换（供 Live2DRenderer 查询 easing 进度）。"""
        self._transition_started_at = now
        self._transition_from = from_req

    # ── 查询 ──

    def get_active(self) -> Optional[ActiveState]:
        """获取当前仲裁结果。过期请求自动清理。"""
        now = _time.monotonic()
        if self._has_active():
            active = self._active()
            if active.duration > 0 and (now - self._active_since) >= active.duration:
                self._active_idx = -1
                return None
        if not self._has_active():
            return None
        active = self._active()
        return ActiveState(
            motion_group=active.motion_group,
            expression_name=active.expression_name,
            params=active.params,
            layer=active.layer,
            name=active.name,
        )

    def get_active_layer(self) -> Layer:
        """获取当前活跃层。无活跃请求时返回 IDLE。"""
        state = self.get_active()
        return state.layer if state else Layer.IDLE

    def is_idle(self) -> bool:
        """是否处于 idle 层（无高优先级动作在播）。"""
        return self.get_active_layer() == Layer.IDLE

    def has_bounded_active(self) -> bool:
        """是否存在「声明了时长且尚未到期」的活跃请求。

        2026-09-11 新增。用途：让「AI 点名的动作在它自己声明的时长内不被
        顶掉」这条保护有一个精确判据。

        **为什么不能用「层」当判据**（这是上一版的 bug）：
        `duration <= 0` 的语义是「直到被更高优先级替换」（见 MotionRequest
        字段注释），它永远不会过期。而 `behavior_mixin` 提交主动挥手时用的是
        `Layer.USER_INITIATED`（=4，最高）且未给 duration——于是**一次主动挥手
        就把 mixer 永久锁在层 4**。任何用「层 >= DIALOG」当保护条件的写法，
        都会在那之后拦下所有动作（实测：15 分钟内 25 次随机动作只播了 2 次）。

        真正值得保护的是**时间受限**的请求：它承载 AI 明确声明的 duration。
        无时长的请求本意就是「随时可被替换」，拦它反而造成本 bug。
        """
        if not self._has_active():
            return False
        active = self._active()
        if active.duration <= 0:
            return False
        return (_time.monotonic() - self._active_since) < active.duration

    def active_name(self) -> str:
        """当前活跃请求的名称（日志用）。"""
        state = self.get_active()
        if state is None:
            return "idle"
        return state.name or state.motion_group or "idle"

    # ── 强制重置 ──

    def force_reset(self) -> None:
        """强制重置：清除所有层，进入 3 秒防闪烁冷却。

        对应 Live2DRenderer._force_idle() 的仲裁层调用。
        调用后任何 submit() 都会被拒绝直到冷却结束。
        """
        self._requests.clear()
        self._active_idx = -1
        self._active_since = 0.0
        self._last_reset_at = _time.monotonic()
        # 强制重置 = 立即结束任何进行中的过渡（视觉硬回到 idle）
        self._transition_started_at = 0.0
        self._transition_from = None

    # ── 动作过渡（easing）查询 ──

    def get_transition(self) -> Optional[tuple["MotionRequest", float]]:
        """返回 (前一活跃请求, 缓动进度 0.0~1.0)。无过渡时返回 None。

        - 首次从 idle 进入活跃时，前一请求为 None，进度会正常推进。
        - force_reset 后清空过渡起点，返回 None。
        - _transition_duration_s <= 0 时视为关闭平滑（返回 None）。
        """
        if self._transition_duration_s is None or self._transition_duration_s <= 0:
            return None
        if self._transition_started_at <= 0.0:
            return None
        elapsed = _time.monotonic() - self._transition_started_at
        if elapsed < 0:
            elapsed = 0.0
        t = elapsed / self._transition_duration_s
        if t >= 1.0:
            return None  # 过渡结束
        eased = self._evaluate_easing(min(t, 1.0))
        return (self._transition_from, eased)

    def is_transition_active(self) -> bool:
        """是否仍处于过渡窗口内（用于诊断/日志）。"""
        return self.get_transition() is not None

    def _evaluate_easing(self, t: float) -> float:
        """按 _transition_easing 计算缓动值。未知曲线回退到 easeOut。"""
        return _EASINGS.get(self._transition_easing, _EASINGS["easeOut"])(t)

    def set_transition_config(self, duration_s: float, easing: str) -> None:
        """设置过渡时长与缓动曲线。duration<=0 关闭平滑；easing 未知回退 easeOut。"""
        if not isinstance(duration_s, (int, float)) or duration_s < 0:
            return
        if not isinstance(easing, str) or easing not in _EASINGS:
            return
        self._transition_duration_s = float(duration_s)
        self._transition_easing = easing

    def is_in_reset_cooldown(self) -> bool:
        """是否在强制重置后的防闪烁冷却期内。"""
        return self._in_reset_cooldown()

    # ── 内部 ──

    def _has_active(self) -> bool:
        return self._active_idx >= 0 and self._active_idx < len(self._requests)

    def _active(self) -> MotionRequest:
        return self._requests[self._active_idx]

    def _in_reset_cooldown(self) -> bool:
        if self._last_reset_at == 0.0:
            return False
        return (_time.monotonic() - self._last_reset_at) < self.RESET_COOLDOWN_S