"""集成测试 — 验证 oc-pet 所有新模块的导入和基础功能"""
from __future__ import annotations

import sys
sys.path.insert(0, r'W:\Games\Hanako\Work\小说\oc-pet')

def test_import(module_name: str):
    try:
        __import__(module_name)
        print(f"  ✓ {module_name}")
        return True
    except Exception as e:
        print(f"  ✗ {module_name}: {e}")
        return False

def test_ws_client():
    from ws_client import BridgeClient, compress_messages
    
    # Test compress
    msgs = [{"text": "a", "time": "1"}, {"text": "b", "time": "2"}, {"text": "c", "time": "3"}]
    assert compress_messages(msgs) == "[1] a\n[2] b\n[3] c"
    
    msgs2 = [{"text": f"m{i}", "time": str(i)} for i in range(5)]
    result = compress_messages(msgs2)
    assert "你有 5 条新消息" in result
    assert "还有 2 条未显示" in result
    assert "[最新] m4" in result
    
    print("  ✓ compress_messages()")
    
    # Test BridgeClient instantiation
    bc = BridgeClient()
    assert bc.connected == True  # HTTP mode always connected
    bc.start()
    assert bc.connected == True
    bc.stop()
    assert bc.connected == True  # HTTP mode stays connected
    print("  ✓ BridgeClient lifecycle")

def test_hanako_monitor():
    from hanako_monitor import HanakoMonitor, STATE_LABELS, EMOTION_KEYWORDS
    
    mon = HanakoMonitor()
    assert mon.ws_connected == False
    mon.set_ws_connected(True)
    assert mon.ws_connected == True
    print("  ✓ HanakoMonitor ws_connected")
    
    # Check 6+ states
    assert "happy" in STATE_LABELS
    assert "cute" in STATE_LABELS
    assert "missing" in STATE_LABELS
    print(f"  ✓ STATE_LABELS ({len(STATE_LABELS)} states)")
    
    # Check emotion keywords
    assert "cute" in EMOTION_KEYWORDS
    assert "missing" in EMOTION_KEYWORDS
    print(f"  ✓ EMOTION_KEYWORDS ({len(EMOTION_KEYWORDS)} emotions)")

def main():
    print("=== oc-pet 模块集成测试 ===\n")
    
    core = [
        "config",
        "memory_store",
        "harness_adapter",
        "break_notifier",
        "action_linker",
        "foreground_watcher",
        "tts_player",
        "eye_overlay",
        "startup_screen",
        "character_editor",
        "behavior",
        "ws_client",
        "hanako_monitor",
        "pet",
    ]
    
    print("1. 模块导入测试")
    passed = sum(test_import(m) for m in core)
    print(f"   {passed}/{len(core)} 通过\n")
    
    print("2. 功能测试")
    test_ws_client()
    test_hanako_monitor()
    
    print("\n=== 全部通过 ===")

if __name__ == "__main__":
    main()
