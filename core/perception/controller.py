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

from .time import TimePerception
from .emotion import EmotionStateMachine
from .schedule import SchedulePerception
from .flags import PetPermissions
from .screen import ScreenPerception
from .screen_types import ScreenEvent, ActivityEvent
from .proactive import ProactiveScheduler

logger = logging.getLogger(__name__)


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

    def __init__(self, character_id: str = "yuexinmiao"):
        self._character_id = character_id
        self._time = TimePerception()
        self._emotion = EmotionStateMachine()
        self._schedule = SchedulePerception()
        self._screen = ScreenPerception()
        self._proactive: ProactiveScheduler | None = None
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
    def screen(self) -> ScreenPerception:
        return self._screen

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
        self._screen._interval = interval
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
            from .hanako_context import HanakoContext
            ctx = HanakoContext(self._character_id)
            return ctx.read_current_session()
        except Exception as e:
            logger.debug("Failed to read session: %s", e)
            return {}

    def get_session_context(self) -> str:
        """获取 Session 摘要文本（注入 LLM prompt 用）"""
        try:
            from .hanako_context import HanakoContext
            ctx = HanakoContext(self._character_id)
            return ctx.get_session_summary()
        except Exception:
            return ""

    def list_other_sessions(self, max_count: int = 10) -> list[dict]:
        """列出其他 Session（只读摘要）"""
        if not self._permissions.cross_session_enabled:
            return []
        try:
            from .hanako_context import HanakoContext
            ctx = HanakoContext(self._character_id)
            return ctx.list_sessions(max_count)
        except Exception:
            return []

    def get_cross_session_context(self) -> str:
        """获取跨 Session 摘要文本（注入 LLM prompt 用）"""
        try:
            from .hanako_context import HanakoContext
            ctx = HanakoContext(self._character_id)
            return ctx.get_cross_session_summary()
        except Exception:
            return ""

    # ── 日报生成 ──

    def generate_daily_diary(self, output_dir: str = "", preview_only: bool = False) -> str | None:
        """从活动事件生成日报 Markdown

        Args:
            output_dir: Obsidian 日记目录，默认 W:/Games/Obsidian/Work/无极限/03-日记/日常
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
            output_dir = "W:/Games/Obsidian/Work/无极限/03-日记/日常"
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

    # ── 统一 tick（每 30 秒）──

    def tick(self):
        """每 30 秒调用，驱动情绪衰减 + 主动对话检查 + 日程刷新 + M2 环境扫描"""
        self._emotion.tick()
        if self._proactive:
            self._proactive.tick()
        now = time.time()
        if now - self._last_schedule_refresh > 600:
            self._schedule.refresh()
            self._last_schedule_refresh = now

        # ── M2: 定期刷新环境扫描快照 ──
        if self._env_scanner and self._env_scanner_enabled:
            try:
                self._scan_environment()
            except Exception as e:
                logger.debug("M2 env scan tick failed: %s", e)

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

    def build_context(self) -> str:
        """组合所有感知信息为 prompt 上下文"""
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
                logger.debug("M2 build_context observation failed: %s", e)

        # ── 手机活动感知 ──
        if self._phone_activity and self._phone_enabled:
            try:
                phone_ctx = self._phone_activity.format_for_prompt()
                if phone_ctx:
                    parts.append(phone_ctx)
            except Exception as e:
                logger.debug("Phone activity build_context failed: %s", e)

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
