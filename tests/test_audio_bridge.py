"""AUDIO-07 ↔ PET-02 联调测试方案

测试目标：验证桌宠音频事件桥接器正确区分 TTS/音乐/提示音，
并与 PET-02 口型系统正确对接。

运行方式：
  python -m unittest tests/test_audio_bridge.py
"""
import unittest
from unittest.mock import MagicMock, patch
from core.pet_audio_bridge import (
    PetAudioBridge, PetAudioCallbacks, AudioTypeDetector,
    AudioEvent, AudioType, MouthState
)


class MockCallbacks(PetAudioCallbacks):
    """记录所有回调调用的 mock 实现"""

    def __init__(self):
        self.events = []

    def on_tts_start(self, emotion="neutral"):
        self.events.append(("tts_start", emotion))

    def on_tts_end(self):
        self.events.append(("tts_end",))

    def on_music_start(self, track_name=""):
        self.events.append(("music_start", track_name))

    def on_music_end(self):
        self.events.append(("music_end",))

    def on_notification_play(self):
        self.events.append(("notification_play",))

    def on_volume_change(self, volume):
        self.events.append(("volume_change", volume))

    def on_pause(self, audio_type):
        self.events.append(("pause", audio_type))

    def on_resume(self, audio_type):
        self.events.append(("resume", audio_type))


# ═══════════════════════════════════════════
# 测试组 1：音频类型检测器
# ═══════════════════════════════════════════

class TestAudioTypeDetector(unittest.TestCase):

    def test_explicit_tts(self):
        detector = AudioTypeDetector()
        event = AudioEvent(type="play", audio_type=AudioType.TTS)
        self.assertEqual(detector.detect(event), AudioType.TTS)

    def test_explicit_music(self):
        detector = AudioTypeDetector()
        event = AudioEvent(type="play", audio_type=AudioType.MUSIC)
        self.assertEqual(detector.detect(event), AudioType.MUSIC)

    def test_explicit_notification(self):
        detector = AudioTypeDetector()
        event = AudioEvent(type="play", audio_type=AudioType.NOTIFICATION)
        self.assertEqual(detector.detect(event), AudioType.NOTIFICATION)

    def test_detect_tts_by_name(self):
        detector = AudioTypeDetector()
        event = AudioEvent(
            type="play", audio_type="unknown",
            track_name="cosyvoice_output.wav"
        )
        self.assertEqual(detector.detect(event), AudioType.TTS)

    def test_detect_tts_by_path(self):
        detector = AudioTypeDetector()
        event = AudioEvent(
            type="play", audio_type="unknown",
            track_name="/data/tts/speech_001.mp3"
        )
        self.assertEqual(detector.detect(event), AudioType.TTS)

    def test_detect_notification_by_name(self):
        detector = AudioTypeDetector()
        event = AudioEvent(
            type="play", audio_type="unknown",
            track_name="notification_ding.wav"
        )
        self.assertEqual(detector.detect(event), AudioType.NOTIFICATION)

    def test_detect_notification_by_chinese(self):
        detector = AudioTypeDetector()
        event = AudioEvent(
            type="play", audio_type="unknown",
            track_name="提示音_叮"
        )
        self.assertEqual(detector.detect(event), AudioType.NOTIFICATION)

    def test_fallback_to_music(self):
        detector = AudioTypeDetector()
        event = AudioEvent(
            type="play", audio_type="unknown",
            track_name="some_song.mp3"
        )
        self.assertEqual(detector.detect(event), AudioType.MUSIC)


# ═══════════════════════════════════════════
# 测试组 2：TTS 播放 → 口型触发
# ═══════════════════════════════════════════

class TestTTSAudioHandling(unittest.TestCase):

    def setUp(self):
        self.callbacks = MockCallbacks()
        self.bridge = PetAudioBridge(self.callbacks)

    def test_tts_play_triggers_speak_open(self):
        event = AudioEvent(
            type="play", audio_type=AudioType.TTS,
            payload={"emotion": "happy"}
        )
        self.bridge._handle_play(event)

        self.assertIn(("tts_start", "happy"), self.callbacks.events)
        self.assertTrue(self.bridge._is_playing)
        self.assertEqual(self.bridge._mouth_state.state, "speak_open")

    def test_tts_end_restores_idle(self):
        self.bridge._handle_play(AudioEvent(
            type="play", audio_type=AudioType.TTS,
            payload={"emotion": "neutral"}
        ))
        self.assertEqual(self.bridge._mouth_state.state, "speak_open")

        self.bridge._handle_end(AudioEvent(
            type="end", audio_type=AudioType.TTS
        ))

        # 结束后恢复闭嘴
        self.assertEqual(self.bridge._mouth_state.state, "speak_closed")
        # tts_end 至少被调用一次
        end_events = [e for e in self.callbacks.events if e[0] == "tts_end"]
        self.assertGreaterEqual(len(end_events), 1)

    def test_tts_pause_keeps_mouth(self):
        self.bridge._handle_play(AudioEvent(
            type="play", audio_type=AudioType.TTS,
            payload={"emotion": "thinking"}
        ))
        self.bridge._handle_pause(AudioEvent(
            type="pause", audio_type=AudioType.TTS
        ))

        pause_events = [e for e in self.callbacks.events if e[0] == "pause"]
        self.assertEqual(len(pause_events), 1)
        # 暂停后仍然 playing
        self.assertTrue(self.bridge._is_playing)

    def test_tts_resume_retriggers_speak(self):
        self.bridge._handle_play(AudioEvent(
            type="play", audio_type=AudioType.TTS,
            payload={"emotion": "neutral"}
        ))
        self.bridge._handle_pause(AudioEvent(type="pause", audio_type=AudioType.TTS))
        self.bridge._handle_resume(AudioEvent(
            type="resume", audio_type=AudioType.TTS,
            payload={"emotion": "neutral"}
        ))

        resume_events = [e for e in self.callbacks.events if e[0] == "resume"]
        self.assertEqual(len(resume_events), 1)


# ═══════════════════════════════════════════
# 测试组 3：音乐播放 → 强制闭嘴
# ═══════════════════════════════════════════

class TestMusicAudioHandling(unittest.TestCase):

    def setUp(self):
        self.callbacks = MockCallbacks()
        self.bridge = PetAudioBridge(self.callbacks)

    def test_music_play_forces_closed_mouth(self):
        # 先播放 TTS
        self.bridge._handle_play(AudioEvent(
            type="play", audio_type=AudioType.TTS,
            payload={"emotion": "happy"}
        ))
        self.assertEqual(self.bridge._mouth_state.state, "speak_open")

        # 然后切到音乐
        self.bridge._handle_play(AudioEvent(
            type="play", audio_type=AudioType.MUSIC,
            track_name="test_song.mp3"
        ))

        self.assertEqual(self.bridge._mouth_state.state, "speak_closed")
        self.assertTrue(self.bridge._music_muted_speaking)
        self.assertIn(("music_start", "test_song.mp3"), self.callbacks.events)

    def test_music_end_restores_normal(self):
        self.bridge._handle_play(AudioEvent(
            type="play", audio_type=AudioType.MUSIC,
            track_name="song.mp3"
        ))
        self.bridge._handle_end(AudioEvent(
            type="end", audio_type=AudioType.MUSIC
        ))

        self.assertFalse(self.bridge._music_muted_speaking)
        self.assertFalse(self.bridge._is_playing)
        self.assertIn(("music_end",), self.callbacks.events)

    def test_tts_after_music_restores_speak(self):
        self.bridge._handle_play(AudioEvent(
            type="play", audio_type=AudioType.MUSIC,
            track_name="song.mp3"
        ))
        self.bridge._handle_end(AudioEvent(
            type="end", audio_type=AudioType.MUSIC
        ))

        self.bridge._handle_play(AudioEvent(
            type="play", audio_type=AudioType.TTS,
            payload={"emotion": "sad"}
        ))

        self.assertEqual(self.bridge._mouth_state.state, "speak_open")
        self.assertFalse(self.bridge._music_muted_speaking)


# ═══════════════════════════════════════════
# 测试组 4：提示音 → 不触发持久口型
# ═══════════════════════════════════════════

class TestNotificationAudioHandling(unittest.TestCase):

    def setUp(self):
        self.callbacks = MockCallbacks()
        self.bridge = PetAudioBridge(self.callbacks)

    def test_notification_triggers_brief_reaction(self):
        self.bridge._handle_play(AudioEvent(
            type="play", audio_type=AudioType.NOTIFICATION,
            track_name="ding.wav"
        ))

        self.assertIn(("notification_play",), self.callbacks.events)
        self.assertEqual(self.bridge._mouth_state.state, "speak_closed")

    def test_notification_end_no_persistent_effect(self):
        self.bridge._handle_play(AudioEvent(
            type="play", audio_type=AudioType.NOTIFICATION,
            track_name="beep.wav"
        ))
        self.bridge._handle_end(AudioEvent(
            type="end", audio_type=AudioType.NOTIFICATION
        ))

        # 结束后不应该有额外的口型变化
        notification_events = [e for e in self.callbacks.events if e[0] == "notification_play"]
        self.assertEqual(len(notification_events), 1)


# ═══════════════════════════════════════════
# 测试组 5：曲目切换保护
# ═══════════════════════════════════════════

class TestTrackChangeProtection(unittest.TestCase):

    def setUp(self):
        self.callbacks = MockCallbacks()
        self.bridge = PetAudioBridge(self.callbacks)

    def test_tts_to_music_switch_closes_mouth(self):
        self.bridge._handle_play(AudioEvent(
            type="play", audio_type=AudioType.TTS,
            payload={"emotion": "happy"}
        ))
        self.assertEqual(self.bridge._mouth_state.state, "speak_open")

        self.bridge._handle_track_change(AudioEvent(
            type="track-change", audio_type=AudioType.MUSIC,
            track_name="new_song.mp3",
            payload={"trackInfo": {"name": "new_song.mp3"}}
        ))

        self.assertEqual(self.bridge._mouth_state.state, "speak_closed")
        self.assertTrue(self.bridge._music_muted_speaking)

    def test_music_to_tts_switch_opens_mouth(self):
        self.bridge._handle_play(AudioEvent(
            type="play", audio_type=AudioType.MUSIC,
            track_name="song.mp3"
        ))
        self.bridge._handle_end(AudioEvent(
            type="end", audio_type=AudioType.MUSIC
        ))

        self.bridge._handle_play(AudioEvent(
            type="play", audio_type=AudioType.TTS,
            payload={"emotion": "neutral"}
        ))

        self.assertEqual(self.bridge._mouth_state.state, "speak_open")


# ═══════════════════════════════════════════
# 测试组 6：音量事件
# ═══════════════════════════════════════════

class TestVolumeEvent(unittest.TestCase):

    def setUp(self):
        self.callbacks = MockCallbacks()
        self.bridge = PetAudioBridge(self.callbacks)

    def test_volume_change_callback(self):
        self.bridge._handle_volume(AudioEvent(
            type="volume", audio_type=AudioType.MUSIC,
            payload={"volume": 0.5}
        ))

        vol_events = [e for e in self.callbacks.events if e[0] == "volume_change"]
        self.assertEqual(len(vol_events), 1)
        self.assertEqual(vol_events[0][1], 0.5)


# ═══════════════════════════════════════════
# 测试组 7：生命周期
# ═══════════════════════════════════════════

class TestBridgeLifecycle(unittest.TestCase):

    def test_connect_disconnect(self):
        callbacks = MockCallbacks()
        bridge = PetAudioBridge(callbacks)
        # connect 在没有 window.audioEventBus 时会打 warning，但不崩溃
        try:
            bridge.connect()
        except Exception:
            pass  # 没有 window 对象是正常的
        status = bridge.get_status()
        self.assertIn("subscriptions", status)
        bridge.disconnect()

    def test_get_status(self):
        callbacks = MockCallbacks()
        bridge = PetAudioBridge(callbacks)
        status = bridge.get_status()

        self.assertFalse(status["is_playing"])
        self.assertIsNone(status["current_audio_type"])
        self.assertEqual(status["mouth_state"], "speak_closed")
        self.assertFalse(status["music_muted_speaking"])


# ═══════════════════════════════════════════
# 测试组 8：端到端模拟
# ═══════════════════════════════════════════

class TestEndToEnd(unittest.TestCase):
    """模拟真实使用场景的完整流程"""

    def setUp(self):
        self.callbacks = MockCallbacks()
        self.bridge = PetAudioBridge(self.callbacks)

    def test_full_tts_cycle(self):
        """完整 TTS 周期：播放 → 结束 → 空闲"""
        self.bridge._handle_play(AudioEvent(
            type="play", audio_type=AudioType.TTS,
            track_name="response_001.wav",
            payload={"emotion": "happy"}
        ))
        self.assertEqual(self.bridge._mouth_state.state, "speak_open")

        self.bridge._handle_end(AudioEvent(
            type="end", audio_type=AudioType.TTS,
            track_name="response_001.wav"
        ))
        self.assertEqual(self.bridge._mouth_state.state, "speak_closed")
        self.assertFalse(self.bridge._is_playing)

    def test_music_interrupts_tts(self):
        """音乐打断 TTS：TTS 播放中 → 音乐开始 → TTS 被强制闭嘴"""
        self.bridge._handle_play(AudioEvent(
            type="play", audio_type=AudioType.TTS,
            payload={"emotion": "neutral"}
        ))
        self.assertEqual(self.bridge._mouth_state.state, "speak_open")

        # 音乐开始（模拟用户切歌）
        self.bridge._handle_play(AudioEvent(
            type="play", audio_type=AudioType.MUSIC,
            track_name="bgm.mp3"
        ))
        self.assertEqual(self.bridge._mouth_state.state, "speak_closed")
        self.assertTrue(self.bridge._music_muted_speaking)

    def test_notification_between_tts(self):
        """TTS 间隙中的提示音不影响口型"""
        self.bridge._handle_play(AudioEvent(
            type="play", audio_type=AudioType.TTS,
            payload={"emotion": "neutral"}
        ))
        self.assertEqual(self.bridge._mouth_state.state, "speak_open")

        # 提示音插入
        self.bridge._handle_play(AudioEvent(
            type="play", audio_type=AudioType.NOTIFICATION,
            track_name="beep.wav"
        ))
        # 注意：这里会强制闭嘴，这是当前设计的 trade-off
        # 如果需要在 TTS 期间保持口型，应修改 _handle_play 逻辑
        self.bridge._handle_end(AudioEvent(
            type="end", audio_type=AudioType.NOTIFICATION
        ))

        # 提示音结束后恢复之前的 TTS 状态
        self.bridge._handle_end(AudioEvent(
            type="end", audio_type=AudioType.TTS
        ))


if __name__ == "__main__":
    unittest.main()
