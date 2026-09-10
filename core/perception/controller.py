"""统一感知控制器 — 整合所有子模块

对外暴露：
  - build_context()  -> 注入 LLM prompt 的感知上下文
  - tick()           -> 每 30 秒调用，驱动情绪衰减 + 屏幕分析 + 主动对话
  - trigger_emotion() -> 触发情绪状态
  - get_screen_context() -> 屏幕感知结果
  - check_proactive()   -> 主动对话触发检查
  - generate_daily_diary() -> 日报 Markdown 生成

子模块依赖：
  - TimePerception   (time.py)
  - EmotionStateMachine (emotion.py)
  - SchedulePerception (schedule.py)
  - PetPermissions   (flags.py)
  - ScreenPerception + ScreenEvent + ActivityEvent (screen.py / screen_types.py)
  - ProactiveScheduler (proactive.py)

M2 增强：
  - EnhancedEnvironmentScanner：窗口标题/屏幕描述 → 结构化快照 → 观察文本
  - PhoneActivityReceiver / PhoneActivityPerception：手机 HTTP 上报
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path

from config import load_config

from .time import TimePerception
from .emotion import EmotionStateMachine
from .schedule import SchedulePerception
from .inspection import InspectionPerception
from .flags import PetPermissions
from .screen import ScreenPerception
from .screen_types import ScreenEvent, ActivityEvent
from .proactive import ProactiveScheduler
from .media import MediaPerception, MediaEvent
from .screen_observer_process import ScreenObserverProcess
from core.service_health import get_health_monitor, HealthMonitor
from core.autonomy_panel import get_autonomy_panel

logger = logging.getLogger(__name__)

# A：Obsidian 日记输出目录回退默认路径（config.perception.obsidian_diary_dir
# 与环境变量 OC_PET_OBSIDIAN_DIR 均缺失时使用）。
DEFAULT_OBSIDIAN_DIARY_DIR = "W:/Games/Obsidian/Work/无极限/03-日记/日常"


class PerceptionController:
    """统一感知控制器 - 整合时间/情绪/日程/屏幕/主动对话 + M2 增强环境扫描

    用法:
        ctrl = PerceptionController(character_id="yuexinmiao")
        ctrl.start_screen(interval=120)
        ctrl.set_proactive(foreground_watcher=watcher, on_proactive=callback)
        ctrl.load_proactive_config(config)

        # 每 30 秒
        ctrl.tick()

        # 注入 LLM prompt
        context = ctrl.build_context()

        # 触发情绪
        ctrl.trigger_emotion("happy")
    """

    def __init__(self, character_id: str = "yuexinmiao", config: dict | None = None, agent_id: str | None = None):
        self._character_id = character_id
        # Hanako 读取用的 agent（定时/巡检）：默认与显示角色一致，可被对话后端
        # agent 覆盖。桌宠显示角色（如 miku）在 ~/.hanako/agents/ 下往往无目录，
        # 必须绑定到真实的 Hanako 助手 agent（如 ophelia）才能读到 desk/cron-jobs.json。
        self._hanako_agent = (agent_id or character_id).strip()
        # A：感知层配置（缺省回退到磁盘 config.json）。
        self._config = config if config is not None else load_config()
        self._time = TimePerception()
        self._emotion = EmotionStateMachine()
        # BugFix #5-C：SchedulePerception 绑定 agent_id，读
        # ~/.hanako/agents/<agent_id>/desk/cron-jobs.json（原 automation*.json 不存在）
        self._schedule = SchedulePerception(agent_id=self._hanako_agent)
        # BugFix #5-D：Hanako 任务巡检（每 5 分钟观察者轮询）
        self._inspection = InspectionPerception(self._schedule)
        self._inspection_callback = None  # 巡检命中回调（pet.py 注入 _on_proactive_trigger）
        self._screen = ScreenPerception()
        self._media = MediaPerception()
        self._screen_process = ScreenObserverProcess()
        self._autonomy = get_autonomy_panel()
        self._health = get_health_monitor()
        self._proactive: ProactiveScheduler | None = None
        self._scene_memory = None  # C 场景记忆（收盘聚类透传；由 pet.py 注入）
        self._last_schedule_refresh = 0.0
        self._permissions = PetPermissions()  # 权限开关

        # ── M2: 增强环境扫描器 ──
        self._env_scanner = None
        self._env_scanner_enabled = True
        try:
            from core.enhanced_environment import EnhancedEnvironmentScanner
            self._env_scanner = EnhancedEnvironmentScanner()
            logger.info("EnhancedEnvironmentScanner initialized for %s", character_id)
        except Exception as e:
            logger.warning("Failed to init EnhancedEnvironmentScanner: %s", e)

        # ── 手机活动感知（MacroDroid HTTP 上报） ──
        self._phone_activity = None
        self._phone_receiver = None
        self._phone_enabled = True
        try:
            from core.phone_activity import PhoneActivityPerception
            from core.phone_receiver import PhoneActivityReceiver
            self._phone_activity = PhoneActivityPerception()
            auth_token = os.environ.get('PHONE_AUTH_TOKEN', '')
            self._phone_receiver = PhoneActivityReceiver(self._phone_activity, auth_token=auth_token)
            self._phone_receiver.start()
            logger.info("PhoneActivityReceiver started on port %d", self._phone_receiver.port)
        except Exception as e:
            logger.warning("Failed to init PhoneActivityReceiver: %s", e)

    @property
    def time(self) -> TimePerception:
        return self._time

    @property
    def emotion(self) -> EmotionStateMachine:
        return self._emotion

    @property
    def schedule(self) -> SchedulePerception:
        return self._schedule

    @property
    def inspection(self) -> InspectionPerception:
        """Hanako 任务巡检（BugFix #5-D）。"""
        return self._inspection

    def set_inspection_callback(self, callback) -> None:
        """注入巡检命中回调（命中文案 → 主动汇报链路）。"""
        self._inspection_callback = callback

    @property
    def screen(self) -> ScreenPerception:
        return self._screen

    @property
    def media(self) -> MediaPerception:
        return self._media

    @property
    def screen_process(self) -> ScreenObserverProcess:
        return self._screen_process

    @property
    def autonomy(self) -> AutonomyPanel:
        return self._autonomy

    @property
    def health(self) -> HealthMonitor:
        return self._health

    @property
    def proactive(self) -> ProactiveScheduler | None:
        return self._proactive

    @property
    def env_scanner(self):
        """M2: 暴露环境扫描器引用"""
        return self._env_scanner

    @property
    def phone_activity(self):
        """手机活动感知层（MacroDroid 上报）"""
        return self._phone_activity

    @property
    def phone_receiver(self):
        """手机活动 HTTP 接收器"""
        return self._phone_receiver

    @property
    def permissions(self) -> PetPermissions:
        """权限开关"""
        return self._permissions

    # ── 屏幕 ──

    def start_screen(self, interval: int = 120):
        if not self._permissions.screenshot_enabled:
            logger.info("Screen disabled by permissions")
            return
        # 基准间隔 + 随机浮动范围（默认 ±30%，即 84~156s）
        self._screen.set_interval(interval)
        self._screen.start()

    def stop_screen(self):
        self._screen.stop()

    def get_screen_context(self) -> str:
        return self._screen.get_context()

    # ── Session ──

    def get_current_session(self) -> dict:
        """获取当前 Session 摘要（不加载完整历史）"""
        if not self._permissions.session_read_enabled:
            return {}
        try:
            from core.hanako_context import HanakoContext
            ctx = HanakoContext(self._character_id)
            return ctx.read_current_session()
        except Exception as e:
            logger.debug("Failed to read session: %s", e)
            return {}

    def _note_degraded(self, name: str, e: Exception) -> None:
        """记一次感知降级（去重 + 可见）。

        2026-09-10：本类原有多处 `except Exception: return ""` 完全无日志，
        表现为「跨会话上下文静默为空 / 会话列表静默为空」——用户以为正常，
        实际是功能没生效。同名只报一次，避免每轮对话刷屏。
        """
        seen = getattr(self, "_degraded_reported", None)
        if seen is None:
            seen = self._degraded_reported = set()
        if name not in seen:
            seen.add(name)
            logger.warning("Perception 降级 [%s]: %s（同名后续静音）", name, e)

    def get_session_context(self) -> str:
        """获取 Session 摘要文本（注入 LLM prompt 用）"""
        try:
            from core.hanako_context import HanakoContext
            ctx = HanakoContext(self._character_id)
            return ctx.get_session_summary()
        except Exception as e:
            self._note_degraded("get_session_context", e)
            return ""

    def list_other_sessions(self, max_count: int = 10) -> list[dict]:
        """列出其他 Session（只读摘要）"""
        if not self._permissions.cross_session_enabled:
            return []
        try:
            from core.hanako_context import HanakoContext
            ctx = HanakoContext(self._character_id)
            return ctx.list_sessions(max_count)
        except Exception as e:
            self._note_degraded("list_other_sessions", e)
            return []

    def get_cross_session_context(self) -> str:
        """获取跨 Session 摘要文本（注入 LLM prompt 用）"""
        try:
            from core.hanako_context import HanakoContext
            ctx = HanakoContext(self._character_id)
            return ctx.get_cross_session_summary()
        except Exception as e:
            self._note_degraded("get_cross_session_context", e)
            return ""

    # ── 日报生成 ──

    def _resolve_obsidian_diary_dir(self, output_dir: str = "") -> str:
        """A：解析 Obsidian 日记输出目录。

        优先级：
          1. 显式传入的 output_dir（最高优先）
          2. config.perception.obsidian_diary_dir（非空）
          3. 环境变量 OC_PET_OBSIDIAN_DIR
          4. 内置默认路径 DEFAULT_OBSIDIAN_DIARY_DIR
        任一环节缺失/为空都安全回退到下一优先级，绝不抛异常。
        """
        if output_dir:
            return output_dir
        cfg = self._config or {}
        cfg_dir = (cfg.get("perception") or {}).get("obsidian_diary_dir") or ""
        if cfg_dir:
            return cfg_dir
        env_dir = os.environ.get("OC_PET_OBSIDIAN_DIR", "") or ""
        if env_dir:
            return env_dir
        return DEFAULT_OBSIDIAN_DIARY_DIR

    def generate_daily_diary(self, output_dir: str = "", preview_only: bool = False) -> str | None:
        """从活动事件生成日报 Markdown

        Args:
            output_dir: Obsidian 日记目录（缺省按配置/环境变量/内置默认解析，
                见 _resolve_obsidian_diary_dir）
            preview_only: True 则只返回 Markdown 内容，不写文件

        Returns:
            preview_only=True: Markdown 内容
            preview_only=False: 写入的文件路径
        """
        if not self._permissions.diary_enabled and not preview_only:
            logger.info("Diary disabled by permissions")
            return None
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M")

        # 获取今日活动（从 00:00 开始）
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self._screen._lock:
            today_activities = [
                e for e in self._screen._activity_history
                if e.start_time >= midnight
            ]

        if not today_activities:
            return None if not preview_only else "（今日无活动记录）"

        # 按分类分组
        categories = {
            'work': ('💼 工作', []),
            'learn': ('📚 学习', []),
            'entertainment': ('🎮 娱乐', []),
            'communication': ('💬 交流', []),
            'other': ('📌 其他', []),
        }
        for event in today_activities:
            cat = event.category if event.category in categories else 'other'
            categories[cat][1].append(event)

        # 生成 Markdown
        lines = [
            f"---",
            f"title: 桌宠日报 {date_str}",
            f"date: {date_str}",
            f"tags: [日报, 桌宠]",
            f"---",
            f"",
            f"# 桌宠日报 {date_str}",
            f"",
            f"生成时间：{time_str}",
            f"活动事件数：{len(today_activities)}",
            f"",
        ]

        for cat_key, (cat_label, events) in categories.items():
            if not events:
                continue
            lines.append(f"## {cat_label}")
            lines.append("")
            for e in events:
                start = datetime.fromtimestamp(e.start_time).strftime("%H:%M")
                confidence_mark = "" if e.confidence >= 0.7 else " ⚠️ 低置信度"
                duration = f" ({e.duration_minutes:.0f}分钟)" if e.duration_minutes > 0 else ""
                lines.append(f"- **{start}** {e.summary}{duration}{confidence_mark}")
                if e.app:
                    lines.append(f"  - 应用：{e.app}")
            lines.append("")

        # 时间缺口检测
        if len(today_activities) > 1:
            gaps = []
            for i in range(1, len(today_activities)):
                prev_end = today_activities[i-1].end_time or today_activities[i-1].start_time
                curr_start = today_activities[i].start_time
                gap_min = (curr_start - prev_end) / 60.0
                if gap_min > 30:  # 超过 30 分钟的缺口
                    gap_start = datetime.fromtimestamp(prev_end).strftime("%H:%M")
                    gap_end = datetime.fromtimestamp(curr_start).strftime("%H:%M")
                    gaps.append(f"{gap_start} ~ {gap_end}（{gap_min:.0f}分钟）")
            if gaps:
                lines.append("## ⏳ 时间缺口")
                lines.append("")
                for g in gaps:
                    lines.append(f"- {g}")
                lines.append("")

        lines.append(f"---")
        lines.append(f"*由桌宠自动生成*")
        content = "\n".join(lines)

        if preview_only:
            return content

        # 写入文件
        if not output_dir:
            output_dir = self._resolve_obsidian_diary_dir()
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        filename = f"{date_str}-桌宠日报.md"
        filepath = output_path / filename
        filepath.write_text(content, encoding="utf-8")
        logger.info("Daily diary written: %s", filepath)
        return str(filepath)

    # ── 情绪 ──

    def trigger_emotion(self, emotion: str, intensity: float = 1.0):
        self._emotion.trigger(emotion, intensity)

    def reset_emotion(self):
        self._emotion.reset()

    # ── 主动对话 ──

    def set_proactive(self, foreground_watcher=None, on_proactive: callable = None):
        self._proactive = ProactiveScheduler(foreground_watcher=foreground_watcher, on_proactive=on_proactive)

    def load_proactive_config(self, config: dict):
        if self._proactive:
            self._proactive.load_config(config)
        else:
            self.set_proactive()
            self._proactive.load_config(config)

    # ── C 场景记忆（收盘聚类透传；不主动定时，避免高频 IO）──

    def set_scene_memory(self, scene_memory) -> None:
        """注入 SceneMemory（D 回忆 + E 联想检索端，由 pet.py 传入）。"""
        self._scene_memory = scene_memory

    def rebuild_scenes(self, events: list[dict]) -> int:
        """收盘聚类透传：events → SceneMemory.rebuild（幂等）。

        Args:
            events: 事件流（mem.read_events(30) 的输出）

        Returns:
            重建后的场景条数；未注入 SceneMemory 返回 0。
        """
        scene_memory = getattr(self, "_scene_memory", None)
        if scene_memory is None:
            return 0
        try:
            return scene_memory.rebuild(events or [])
        except Exception as e:
            logger.warning("rebuild_scenes failed: %s", e)
            return 0

    # ── 统一 tick（每 30 秒）──

    def tick(self):
        """每 30 秒调用，驱动情绪衰减 + 主动对话检查 + 日程刷新 + M2 环境扫描 + 任务巡检"""
        self._emotion.tick()
        if self._proactive:
            self._proactive.tick()
        now = time.time()
        if now - self._last_schedule_refresh > 600:
            self._schedule.refresh()
            self._last_schedule_refresh = now

        # ── BugFix #5-D: Hanako 任务巡检（内部 5 分钟节流）──
        self._tick_inspection()

        # ── M2: 定期刷新环境扫描快照 ──
        if self._env_scanner and self._env_scanner_enabled:
            try:
                self._scan_environment()
            except Exception as e:
                # 失败 = 环境扫描停摆（窗口互动/黑名单判断基础缺失）
                self._note_degraded("env_scan_tick", e)

    def _tick_inspection(self):
        """D: 巡检命中 → 回调主动汇报（复用 proactive 触发链路）。

        Stagger 派发（5 秒一条）：避免启动时一次性 15 条 bubble 炸出来。
        每次 tick 最多立即派发 1 条，剩余通过 threading.Timer 逐个入队。
        """
        if self._inspection is None:
            return
        try:
            triggers = self._inspection.tick()
        except Exception as e:
            # 失败 = 本次巡检提醒全部不触发
            self._note_degraded("inspection_tick", e)
            return
        if not triggers or self._inspection_callback is None:
            return
        self._dispatch_inspection_staggered(triggers)

    def _dispatch_inspection_staggered(self, triggers: list, interval: float = 5.0) -> None:
        """stagger 派发巡检触发（默认 5 秒一条），避免启动时一次性堆炸。

        使用 threading.Timer 递归派发；每个 Timer 是守护线程，进程退出自动清理。
        单次 tick 最多 15 条 → 75 秒内依次播报，节奏自然不堆撞。
        """
        import threading

        def _dispatch_next():
            if not triggers:
                return
            try:
                self._inspection_callback(triggers.pop(0))
            except Exception as e:
                logger.debug("Inspection callback failed: %s", e)
            if triggers:
                t = threading.Timer(interval, _dispatch_next)
                t.daemon = True
                t.start()

        threading.Timer(0, _dispatch_next).start()

    def _scan_environment(self):
        """M2: 扫描当前环境并更新上下文

        从 ForegroundWatcher 获取窗口标题，通过 EnhancedEnvironmentScanner
        解析为结构化快照，注入到 ScreenPerception 的 on_update 回调中。
        """
        try:
            # 尝试从前景窗口检测器获取最新标题
            fg_title = ""
            if hasattr(self, '_foreground_watcher') and self._foreground_watcher:
                fg_title = getattr(self._foreground_watcher, 'last_title', '') or ''
            elif hasattr(self._screen, '_foreground_watcher'):
                fw = self._screen._foreground_watcher
                if fw:
                    fg_title = getattr(fw, 'last_title', '') or ''
        except Exception:
            fg_title = ""

        # 时间上下文
        time_ctx = self._time.get_context()

        # 屏幕描述
        screen_desc = self._screen.last_description if self._screen else ""

        # 执行扫描
        snapshot = self._env_scanner.scan(
            window_title=fg_title,
            screen_description=screen_desc,
            time_context=time_ctx,
        )
        logger.debug("M2 env scan: app=%s cat=%s files=%s",
                     snapshot.foreground_app, snapshot.category, snapshot.detected_files)
        return snapshot

    def get_recent_activity_events(self, minutes: int = 60) -> list[ActivityEvent]:
        """透传获取最近的 ActivityEvent 列表（用于活动流 UI 组件）"""
        return self._screen.get_recent_activity_events(minutes)

    # ── 构建 LLM 上下文 ──

    def build_context(self, source: str = "user") -> str:
        """组合所有感知信息为 prompt 上下文
        
        Args:
            source: 消息来源 (user/proactive/idle/screen_enrich 等)
                   user: 用户主动对话，不注入屏幕感知信息
                   proactive: 桌宠主动搭话，注入屏幕感知信息
        """
        parts = []
        time_ctx = self._time.format_for_prompt()
        if time_ctx:
            parts.append(time_ctx)
        emotion_ctx = self._emotion.format_for_prompt()
        if emotion_ctx:
            parts.append(emotion_ctx)
        schedule_ctx = self._schedule.format_for_prompt()
        if schedule_ctx:
            parts.append(schedule_ctx)
        # ── BugFix #5-D: 巡检命中注入 prompt（无命中返回空串）──
        inspection_ctx = self._inspection.format_for_prompt()
        if inspection_ctx:
            parts.append(inspection_ctx)
        
        # ── 2026-09-09: 区分来源，用户主动对话不注入屏幕感知信息 ──
        # 屏幕感知信息只用于 proactive 场景，避免混入用户对话回复
        if source in ("proactive", "idle", "screen_enrich"):
            screen_ctx = self._screen.get_context()
            if screen_ctx:
                parts.append(screen_ctx)

        # ── M2: 注入环境扫描观察 ──
        if self._env_scanner and self._env_scanner_enabled:
            try:
                # 从 ScreenPerception 获取最新的窗口标题
                fg_title = ""
                if hasattr(self._screen, '_foreground_watcher'):
                    fw = self._screen._foreground_watcher
                    if fw:
                        fg_title = getattr(fw, 'last_title', '') or ''
                if fg_title:
                    snapshot = self._env_scanner.scan(
                        window_title=fg_title,
                        screen_description=self._screen.last_description,
                        time_context=self._time.get_context(),
                    )
                    obs = self._env_scanner.get_observation(snapshot)
                    if obs:
                        parts.append(f"[环境观察] {obs}")
            except Exception as e:
                self._note_degraded("build_context_observation", e)

        # ── 子服务健康状态 ──
        if self._health:
            try:
                health_ctx = self._health.format_for_prompt()
                if health_ctx:
                    parts.append(health_ctx)
            except Exception as e:
                self._note_degraded("build_context_health", e)

        # ── 媒体播放感知（SMTC）──
        if self._media:
            try:
                current = self._media.get_current()
                if current and current.state == "playing":
                    media_ctx = f"[正在听] {current.artist} - {current.title}"
                    if current.album:
                        media_ctx += f" ({current.album})"
                    parts.append(media_ctx)
            except Exception as e:
                self._note_degraded("build_context_media", e)

        # ── 手机活动感知 ──
        if self._phone_activity and self._phone_enabled:
            try:
                phone_ctx = self._phone_activity.format_for_prompt()
                if phone_ctx:
                    parts.append(phone_ctx)
            except Exception as e:
                self._note_degraded("build_context_phone", e)

        return "\n".join(parts) if parts else ""

    def get_perception_status(self) -> dict:
        """获取当前感知状态全貌（用于设置面板展示）"""
        return {
            "permissions": self._permissions.to_dict(),
            "screen": {
                "enabled": self._permissions.screenshot_enabled,
                "running": self._screen._running if self._screen else False,
                "last_description": self._screen.last_description[:50] if self._screen else "",
                "last_activity": self._screen._last_activity.to_dict() if self._screen and self._screen._last_activity else None,
            },
            "session": {
                "read_enabled": self._permissions.session_read_enabled,
                "cross_session_enabled": self._permissions.cross_session_enabled,
            },
            "emotion": {
                "current": self._emotion.current,
                "intensity": round(self._emotion.intensity, 2),
            },
            "diary": {
                "enabled": self._permissions.diary_enabled,
                "activity_count": len(self._screen._activity_history) if self._screen else 0,
            },
        }
