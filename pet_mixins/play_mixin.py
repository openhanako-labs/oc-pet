"""PlayMixin — P2 互动层接线（小游戏邀请 / 音乐推荐 / 休息提醒）

由 PetWindow 多重继承（pet.py 类定义中加入）。方法体内访问
``self._show_bubble`` / ``self._chat_panel`` / ``self._companion_memory`` /
``self._engine`` 等均由 PetWindow 提供（鸭子类型）。

设计原则：
- **全部防御式**：任一线（小游戏/音乐/休息）失败只记日志，绝不影响既有功能。
- **主线程约束**：本 mixin 的方法只允许在 Qt 主线程调用（QTimer 回调、
  按钮点击、聊天面板提交均在主线程）；跨线程入口（如后台事件回调）必须先
  经 Qt Signal 绕回主线程。
- **复用投递机制**：卡片经 ``_show_interaction_card``（桌宠旁浮动卡 +
  聊天面板同步），文案经 ``_show_bubble``，事件经 ``record_interaction_event``
  （EventBus + EventStream）。
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


class PlayMixin:
    """P2 互动层：初始化 + 聊天分发 + 工作休息提醒 + 卡片动作。"""

    # ── 初始化（pet.py __init__ 调用）──

    def _init_play_layer(self) -> None:
        """初始化互动层（小游戏/音乐/休息提醒）；失败仅禁用互动，不影响主功能。"""
        try:
            from PySide6.QtCore import Qt as _Qt
            from core.play import (
                InteractionIntentDetector,
                MusicRecommender,
                WorkReminderTracker,
                load_work_reminder_config,
            )
            from ui.interaction_card import InteractionCard

            self._interaction_detector = InteractionIntentDetector()
            self._music_recommender = MusicRecommender()
            self._work_reminder = WorkReminderTracker(
                settings=load_work_reminder_config()
            )
            self._music_exclude: list[str] = []
            self._music_dir: str | None = None
            try:
                from pathlib import Path
                home_music = Path.home() / "Music"
                if home_music.exists():
                    self._music_dir = str(home_music)
            except Exception:
                logger.debug("play_mixin: 非致命异常(已静默吞掉)", exc_info=True)

            # 桌宠旁浮动互动卡（可点气泡卡）
            theme = getattr(self, "_ui_theme", "dark") or "dark"
            self._interaction_card = InteractionCard(
                theme=theme if theme in ("light", "dark") else "dark",
                parent=None,
            )
            self._interaction_card.setWindowFlags(
                self._interaction_card.windowFlags() | _Qt.Tool | _Qt.FramelessWindowHint
            )
            self._interaction_card.setMaximumWidth(320)
            self._interaction_card.action_clicked.connect(
                self._on_interaction_card_action
            )
            self._interaction_card.dismiss_requested.connect(
                lambda _k: self._hide_interaction_card()
            )
            self._interaction_card.hide()

            self._mini_game_windows: dict[str, object] = {}

            # 聊天面板内卡片动作转发（面板在 T05 已创建）
            panel = getattr(self, "_chat_panel", None)
            if panel is not None:
                try:
                    panel.card_action.connect(self._on_interaction_card_action)
                except Exception:
                    logger.debug("play_mixin: 非致命异常(已静默吞掉)", exc_info=True)

            logger.info(
                "P2 interaction layer ready | work_reminder enabled=%s",
                getattr(self._work_reminder, "enabled", False),
            )
        except Exception as exc:
            logger.warning("P2 interaction layer 初始化失败（互动功能禁用）: %s", exc)
            self._interaction_detector = None
            self._music_recommender = None
            self._work_reminder = None
            self._interaction_card = None
            self._mini_game_windows = {}

    # ── 聊天分发（P2-3/P2-4 聊天触发）──

    def _dispatch_chat_interaction(self, text: str) -> None:
        """用户消息 → 交互意图 → 邀请卡/推荐卡/休息卡（防御式）。"""
        try:
            detector = getattr(self, "_interaction_detector", None)
            if detector is None or not text or not text.strip():
                return
            intent = detector.detect(text)
            if intent is None:
                return
            kind = intent.kind
            if kind == "game":
                self._offer_game()
            elif kind == "music":
                self._offer_music()
            elif kind == "rest":
                self._offer_rest(source="chat")
        except Exception as exc:
            logger.debug("_dispatch_chat_interaction failed: %s", exc)

    # ── 邀请/推荐 ──

    def _offer_game(self) -> None:
        """弹小游戏邀请卡 + 气泡。"""
        try:
            from core.play import build_game_invite_card
            self._show_interaction_card(build_game_invite_card())
            self._show_bubble("一起玩个小游戏吧？🎮", emotion="happy")
            self._record_interaction_event(
                category="gaming", scenario="mini_game", intent="invite",
                topic="小游戏邀请", source="interaction",
                event_bus_event="interaction_game_invited",
            )
        except Exception as exc:
            logger.debug("_offer_game failed: %s", exc)

    def _offer_music(self) -> None:
        """按场景/情绪/时段推荐一首歌 → 推荐卡 + 气泡。"""
        try:
            from core.play import build_music_card
            recommender = getattr(self, "_music_recommender", None)
            if recommender is None:
                return
            exclude = list(getattr(self, "_music_exclude", []) or [])
            track = recommender.pick_for_context(
                self._music_context(), exclude_ids=exclude
            )
            if track is None:
                self._show_bubble("我还没找到合适的歌……下次再推荐给你～", emotion="neutral")
                return
            self._music_exclude = (exclude + [track.id])[-8:]
            self._show_interaction_card(build_music_card(track))
            self._show_bubble(f"给你推荐一首《{track.title}》～", emotion="happy")
            self._record_interaction_event(
                category="music", scenario="music_recommend", intent="recommend",
                topic=f"推荐音乐:{track.title}", emotion="happy",
                source="interaction", event_bus_event="interaction_music_recommended",
            )
        except Exception as exc:
            logger.debug("_offer_music failed: %s", exc)

    def _music_context(self) -> dict:
        """构造音乐推荐上下文（场景/情绪/时段）。"""
        ctx: dict = {"scene": "", "emotion": "", "hour": None}
        try:
            import datetime
            ctx["hour"] = datetime.datetime.now().hour
        except Exception:
            logger.debug("play_mixin: 非致命异常(已静默吞掉)", exc_info=True)
        try:
            category = ""
            fw = getattr(self, "_foreground_watcher", None)
            if fw is not None:
                category = getattr(fw, "last_category", "") or ""
            if category in ("development", "writing", "study", "research"):
                ctx["scene"] = "deep_work"
            elif category == "gaming":
                ctx["scene"] = "gaming"
            elif category == "entertainment":
                ctx["scene"] = "entertainment"
        except Exception:
            logger.debug("play_mixin: 非致命异常(已静默吞掉)", exc_info=True)
        try:
            emo = getattr(self, "_current_emotion", "") or ""
            if emo and emo != "neutral":
                ctx["emotion"] = emo
        except Exception:
            logger.debug("play_mixin: 非致命异常(已静默吞掉)", exc_info=True)
        return ctx

    def _offer_rest(self, source: str = "timer",
                    info: dict | None = None) -> None:
        """弹休息建议卡 + 气泡（source=timer 由工作提醒驱动，chat 由聊天触发）。"""
        try:
            from core.play import build_rest_card
            tr = getattr(self, "_work_reminder", None)
            if tr is None:
                return
            suggestion = tr.pick_suggestion()
            info = info or tr.should_remind()
            card_data = build_rest_card(
                suggestion,
                threshold_min=float(info.get("threshold_min", 90)),
                late_night=bool(info.get("late_night")),
            )
            self._show_interaction_card(card_data)
            if info.get("late_night"):
                self._show_bubble("夜深了……记得抽空休息一下哦", emotion="neutral")
            else:
                self._show_bubble(
                    f"{suggestion.icon} 休息一下？{suggestion.desc}",
                    emotion="happy",
                )
            if source == "timer":
                tr.mark_reminded()
            self._record_interaction_event(
                category="rest", scenario="break_reminder", intent="remind",
                topic=f"休息建议:{suggestion.title}", emotion="neutral",
                source="interaction", event_bus_event="interaction_rest_reminded",
            )
            self._try_speak_reminder(f"工作好久了，{suggestion.desc}")
        except Exception as exc:
            logger.debug("_offer_rest failed: %s", exc)

    # ── 休息提醒 tick（behavior_mixin._break_check_inner 调用）──

    def _work_reminder_tick(self) -> None:
        """每 30 秒推进工作计时；到点且非深夜 → 休息提醒（联动专注模式）。"""
        try:
            tr = getattr(self, "_work_reminder", None)
            if tr is None or not tr.enabled:
                return
            tr.update(self._is_working_context())
            # 已显示休息卡时不再重复弹
            card = getattr(self, "_interaction_card", None)
            if card is not None and card.isVisible() and card.kind == "rest":
                return
            info = tr.should_remind()
            if info.get("due"):
                self._offer_rest(source="timer", info=info)
        except Exception as exc:
            logger.debug("_work_reminder_tick failed: %s", exc)

    def _is_working_context(self) -> bool:
        """工作上下文：专注模式激活 或 前台分类属于工作类。"""
        try:
            focus = getattr(self, "_focus_manager", None)
            if focus is not None and getattr(focus, "active", False):
                return True
        except Exception:
            logger.debug("play_mixin: 非致命异常(已静默吞掉)", exc_info=True)
        try:
            from core.play.break_reminder import WORK_CATEGORIES
            fw = getattr(self, "_foreground_watcher", None)
            category = getattr(fw, "last_category", "") or ""
            return category in WORK_CATEGORIES
        except Exception:
            return False

    # ── 卡片动作 ──

    def _on_interaction_card_action(self, kind: str, action_id: str) -> None:
        """统一处理卡片按钮（游戏/音乐/休息）。"""
        try:
            aid = str(action_id or "")
            if aid.startswith("open_game:"):
                self._open_mini_game(aid.split(":", 1)[1])
            elif aid.startswith("music_play:"):
                self._play_music(aid.split(":", 1)[1])
            elif aid == "music_open_folder":
                self._open_music_folder_action()
            elif aid == "rest_ack":
                self._acknowledge_rest()
            elif aid == "rest_snooze":
                self._snooze_rest()
            self._hide_interaction_card()
        except Exception as exc:
            logger.debug("_on_interaction_card_action failed: %s", exc)

    def _show_interaction_card(self, card_data: dict) -> None:
        """显示互动卡：桌宠旁浮动卡 + 聊天面板同步（若打开）。"""
        try:
            card = getattr(self, "_interaction_card", None)
            if card is None:
                return
            card.set_content(
                kind=str(card_data.get("kind") or ""),
                title=str(card_data.get("title") or ""),
                body=str(card_data.get("body") or ""),
                actions=card_data.get("actions") or [],
            )
            card.adjustSize()
            geo = self.geometry()
            x = geo.center().x() - card.width() // 2
            y = geo.top() - card.height() - 12
            try:
                from PySide6.QtGui import QGuiApplication
                scr = QGuiApplication.primaryScreen()
                if scr is not None:
                    ag = scr.availableGeometry()
                    x = max(ag.left() + 8, min(x, ag.right() - card.width() - 8))
                    y = max(ag.top() + 8, min(y, ag.bottom() - card.height() - 8))
            except Exception:
                logger.debug("play_mixin: 非致命异常(已静默吞掉)", exc_info=True)
            card.move(x, y)
            card.show()
            card.raise_()
            # 聊天面板同步（若已打开）
            panel = getattr(self, "_chat_panel", None)
            if panel is not None and panel.isVisible():
                try:
                    panel.add_card(
                        str(card_data.get("kind") or ""),
                        str(card_data.get("title") or ""),
                        str(card_data.get("body") or ""),
                        card_data.get("actions") or [],
                    )
                except Exception as exc:
                    logger.debug("chat panel card 同步失败: %s", exc)
        except Exception as exc:
            logger.debug("_show_interaction_card failed: %s", exc)

    def _hide_interaction_card(self) -> None:
        card = getattr(self, "_interaction_card", None)
        if card is not None:
            try:
                card.hide()
            except Exception:
                logger.debug("play_mixin: 非致命异常(已静默吞掉)", exc_info=True)

    # ── 小游戏 ──

    def _open_mini_game(self, kind: str) -> None:
        """打开/复用一个小游戏窗口。"""
        try:
            from PySide6.QtCore import Qt as _Qt
            from ui.mini_game_window import create_game_window
            windows = getattr(self, "_mini_game_windows", None)
            if windows is None:
                windows = {}
                self._mini_game_windows = windows
            if kind not in windows:
                theme = getattr(self, "_ui_theme", "dark") or "dark"
                win = create_game_window(
                    kind, theme=theme if theme in ("light", "dark") else "dark",
                    parent=None,
                )
                if win is None:
                    self._show_bubble("这个游戏还没准备好～", emotion="neutral")
                    return
                win.game_finished.connect(self._on_mini_game_finished)
                win.close_requested.connect(lambda w=win: w.hide())
                win.setWindowFlags(win.windowFlags() | _Qt.Tool)
                windows[kind] = win
            win = windows[kind]
            win.show()
            win.raise_()
            if hasattr(win, "_center_on_screen"):
                win._center_on_screen()
            self._record_interaction_event(
                category="gaming", scenario="mini_game", intent="open",
                topic=f"打开小游戏:{kind}", source="interaction",
                event_bus_event="interaction_game_opened",
            )
        except Exception as exc:
            logger.debug("_open_mini_game failed: %s", exc)

    def _on_mini_game_finished(self, result: dict) -> None:
        """小游戏对局结束：气泡 + 事件日志（复用 EventStream）。"""
        try:
            game = str(result.get("game") or "")
            won = bool(result.get("won"))
            detail = str(result.get("detail") or "")
            if won:
                self._show_bubble(f"🎉 {detail}", emotion="happy")
            else:
                self._show_bubble(f"😿 {detail}", emotion="sad")
            self._record_interaction_event(
                category="gaming", scenario="mini_game", intent="finished",
                topic=f"{game}:{'win' if won else 'lose'}",
                emotion="happy" if won else "sad",
                intensity=0.7 if won else 0.4, source="interaction",
                event_bus_event="mini_game_finished",
            )
        except Exception as exc:
            logger.debug("_on_mini_game_finished failed: %s", exc)

    # ── 音乐播放 ──

    def _play_music(self, track_id: str) -> None:
        """用系统默认播放器打开播放目标（本地文件/URL/音乐文件夹兜底）。"""
        try:
            from core.play.music_recommender import (
                open_play_target,
                resolve_play_target,
            )
            recommender = getattr(self, "_music_recommender", None)
            track = recommender.get(track_id) if recommender is not None else None
            kind, target = resolve_play_target(
                track, getattr(self, "_music_dir", None) or None
            )
            ok = open_play_target(target, kind)
            title = track.title if track is not None else track_id
            if ok:
                self._show_bubble(f"♪ 已打开《{title}》", emotion="happy")
            else:
                self._show_bubble("打开播放器失败……", emotion="sad")
            self._record_interaction_event(
                category="music", scenario="music_play", intent="play",
                topic=f"播放:{title}", source="interaction",
                event_bus_event="interaction_music_played",
            )
        except Exception as exc:
            logger.debug("_play_music failed: %s", exc)

    def _open_music_folder_action(self) -> None:
        try:
            from core.play.music_recommender import open_music_folder
            ok = open_music_folder(getattr(self, "_music_dir", None) or None)
            self._show_bubble(
                "已打开音乐文件夹～" if ok else "打开音乐文件夹失败……",
                emotion="happy" if ok else "sad",
            )
        except Exception as exc:
            logger.debug("_open_music_folder_action failed: %s", exc)

    # ── 休息确认/稍后 ──

    def _acknowledge_rest(self) -> None:
        """确认休息：重置计时 + 进入冷却。"""
        try:
            tr = getattr(self, "_work_reminder", None)
            if tr is not None:
                tr.acknowledge()
            self._show_bubble("好呀，休息一下～回来再战！", emotion="happy")
            self._record_interaction_event(
                category="rest", scenario="break_reminder", intent="ack",
                topic="确认休息", emotion="happy", source="interaction",
                event_bus_event="interaction_rest_acknowledged",
            )
        except Exception as exc:
            logger.debug("_acknowledge_rest failed: %s", exc)

    def _snooze_rest(self) -> None:
        try:
            tr = getattr(self, "_work_reminder", None)
            if tr is not None:
                tr.snooze()
            self._show_bubble("好，那我 10 分钟后再提醒你～", emotion="neutral")
        except Exception as exc:
            logger.debug("_snooze_rest failed: %s", exc)

    # ── 事件记录 / 可选 TTS ──

    def _record_interaction_event(self, *, category: str = "", scenario: str = "",
                                  intent: str = "", topic: str = "",
                                  emotion: str = "", intensity: float = 0.0,
                                  source: str = "interaction",
                                  event_bus_event: str = "") -> None:
        """写入事件日志（EventStream，经 CompanionMemory 或直接 EventStream）。"""
        try:
            from core.play.interaction_dispatcher import record_interaction_event
            rec = None
            mem = getattr(self, "_companion_memory", None)
            if mem is not None and hasattr(mem, "record_event"):
                rec = mem.record_event
            else:
                try:
                    from core.event_stream import EventStream
                    es = EventStream(getattr(self, "_agent_id", "default"))
                    rec = lambda **kw: es.append(dict(kw))  # noqa: E731
                except Exception:
                    rec = None
            record_interaction_event(
                category=category, scenario=scenario, intent=intent,
                topic=topic, emotion=emotion, intensity=intensity,
                source=source, record_event=rec,
                event_bus_event=event_bus_event,
            )
        except Exception as exc:
            logger.debug("_record_interaction_event failed: %s", exc)

    def _try_speak_reminder(self, text: str) -> None:
        """可选 TTS 语音提醒：探查 tts_provider 能力，有则用、无则跳过。"""
        try:
            if not text or not getattr(self, "_work_reminder", None):
                return
            if not self._work_reminder.tts_enabled:
                return
            tts_cfg = self.config.get("tts", {}) or {}
            if not tts_cfg.get("enabled", True):
                return
            engine = getattr(self, "_engine", None)
            provider = getattr(engine, "_tts", None) if engine is not None else None
            if provider is None or not getattr(provider, "is_ready", False):
                logger.debug("TTS provider 不可用，跳过休息语音提醒")
                return
            provider_ref = provider
            char = getattr(self, "_current_char", "") or ""
            signal = getattr(self, "tts_celebration_signal", None)
            if signal is None:
                return

            def _synth() -> None:
                try:
                    path = provider_ref.synthesize(
                        text, character_id=char, instruct="温柔"
                    ) or ""
                    if path:
                        signal.emit(path)
                except Exception as exc:
                    logger.debug("休息语音合成失败: %s", exc)

            threading.Thread(target=_synth, daemon=True).start()
        except Exception as exc:
            logger.debug("_try_speak_reminder failed: %s", exc)


__all__ = ["PlayMixin"]
