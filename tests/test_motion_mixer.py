"""T08 MotionMixer 单元测试 — 层优先级仲裁逻辑。"""
import time

from avatar.motion_mixer import MotionMixer, MotionRequest, Layer


def test_priority_high_overrides_low():
    m = MotionMixer()
    # IDLE 层先入
    assert m.submit(MotionRequest(layer=Layer.IDLE, name="idle"))
    assert m.get_active_layer() == Layer.IDLE
    # USER_INITIATED 打断
    assert m.submit(MotionRequest(layer=Layer.USER_INITIATED, name="user"))
    assert m.get_active_layer() == Layer.USER_INITIATED


def test_priority_low_cannot_override_high():
    m = MotionMixer()
    assert m.submit(MotionRequest(layer=Layer.USER_INITIATED, name="user"))
    # SCREEN 层无法打断 USER_INITIATED
    assert not m.submit(MotionRequest(layer=Layer.SCREEN, name="screen"))
    assert m.get_active_layer() == Layer.USER_INITIATED


def test_same_priority_first_come_first_served():
    m = MotionMixer()
    assert m.submit(MotionRequest(layer=Layer.DIALOG, name="dialog1"))
    # 同优先级 can_interrupt=False → 不打断
    assert not m.submit(MotionRequest(layer=Layer.DIALOG, name="dialog2", can_interrupt=False))
    assert m.active_name() == "dialog1"


def test_same_priority_can_interrupt():
    m = MotionMixer()
    assert m.submit(MotionRequest(layer=Layer.DIALOG, name="dialog1"))
    # 同优先级 can_interrupt=True → 可打断
    assert m.submit(MotionRequest(layer=Layer.DIALOG, name="dialog2", can_interrupt=True))
    assert m.active_name() == "dialog2"


def test_force_reset_clears_all():
    m = MotionMixer()
    m.submit(MotionRequest(layer=Layer.USER_INITIATED, name="user"))
    m.submit(MotionRequest(layer=Layer.DIALOG, name="dialog"))
    m.force_reset()
    assert m.get_active_layer() == Layer.IDLE
    assert m.is_in_reset_cooldown()


def test_reset_cooldown_blocks_submits():
    m = MotionMixer()
    m.force_reset()
    # 冷却期内提交被拒绝
    assert not m.submit(MotionRequest(layer=Layer.USER_INITIATED, name="user"))
    assert m.get_active_layer() == Layer.IDLE


def test_reset_cooldown_expires():
    m = MotionMixer()
    m.RESET_COOLDOWN_S = 0.05
    m.force_reset()
    assert m.is_in_reset_cooldown()
    time.sleep(0.06)
    assert not m.is_in_reset_cooldown()
    # 冷却期后可正常提交
    assert m.submit(MotionRequest(layer=Layer.IDLE, name="idle"))
    assert m.get_active_layer() == Layer.IDLE


def test_duration_expiry():
    m = MotionMixer()
    assert m.submit(MotionRequest(layer=Layer.SCREEN, duration=0.05, name="screen"))
    assert m.get_active_layer() == Layer.SCREEN
    time.sleep(0.06)
    assert m.get_active_layer() == Layer.IDLE
    assert m.get_active() is None


def test_is_idle():
    m = MotionMixer()
    assert m.is_idle()
    m.submit(MotionRequest(layer=Layer.DIALOG, name="dialog"))
    assert not m.is_idle()
    m.force_reset()
    time.sleep(m.RESET_COOLDOWN_S + 0.01)
    assert m.is_idle()


def test_layer_from_str():
    assert Layer.from_str("idle") == Layer.IDLE
    assert Layer.from_str("screen") == Layer.SCREEN
    assert Layer.from_str("dialog") == Layer.DIALOG
    assert Layer.from_str("user") == Layer.USER_INITIATED
    assert Layer.from_str("user_initiated") == Layer.USER_INITIATED
    assert Layer.from_str("unknown") == Layer.IDLE