#!/usr/bin/env python3
"""沙盒运行器 - 在隔离环境中启动桌宠，不调用任何 API。

用法：
    python sandbox_runner.py                    # 交互模式（手动输入测试）
    python sandbox_runner.py --scenario basic_chat  # 自动化场景测试
    python sandbox_runner.py --list             # 列出可用场景
    python sandbox_runner.py --latency 2.0      # 设置模拟延迟（秒）

沙盒做了什么：
    1. LLM -> MockLLMAdapter（预设回复，不调 API）
    2. TTS -> MockTTSProvider（跳过合成，不发声）
    3. ASR -> 跳过
    4. 配置 -> sandbox/config.sandbox.json（不碰真实 config.json）
    5. 屏幕感知 -> 关闭
    6. 主动对话 -> 关闭
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

# ── 切换到项目目录 ──
PROJECT_DIR = Path(__file__).parent
os.chdir(PROJECT_DIR)
sys.path.insert(0, str(PROJECT_DIR))

# ── 沙盒配置路径 ──
SANDBOX_CONFIG_PATH = PROJECT_DIR / "sandbox" / "config.sandbox.json"


def load_sandbox_config() -> dict:
    """加载沙盒配置"""
    if SANDBOX_CONFIG_PATH.exists():
        return json.loads(SANDBOX_CONFIG_PATH.read_text("utf-8"))
    return {}


def save_sandbox_config(cfg: dict):
    """保存沙盒配置"""
    SANDBOX_CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8"
    )


def apply_patches(latency_scale: float = 1.0):
    """注入所有 mock 替换

    在 import PetWindow/PetManager 之前调用。
    """
    from sandbox.mock_llm import MockLLMAdapter
    from sandbox.mock_tts import MockTTSProvider

    # ── 1. 替换 HanakoPetAdapter ──
    import core.harness_adapter as adapter_mod
    _original_adapter = adapter_mod.HanakoPetAdapter

    def _make_mock_adapter(agent_id="ophelia", builtin=False):
        m = MockLLMAdapter(agent_id=agent_id, builtin=builtin)
        return m

    adapter_mod.HanakoPetAdapter = _make_mock_adapter
    logging.info("[Sandbox] LLM -> MockLLMAdapter")

    # 还要替换 conversation_engine 里已经 import 的引用
    import core.conversation_engine as engine_mod
    engine_mod.HanakoPetAdapter = _make_mock_adapter
    logging.info("[Sandbox] conversation_engine.HanakoPetAdapter patched")

    # ── 2. 替换 config 的 load/save ──
    import config as config_mod
    _original_load = config_mod.load_config
    _original_save = config_mod.save_config

    def _sandbox_load_config():
        cfg = load_sandbox_config()
        logging.info("[Sandbox] config loaded from sandbox/config.sandbox.json")
        return cfg

    def _sandbox_save_config(cfg):
        save_sandbox_config(cfg)
        logging.info("[Sandbox] config saved to sandbox/config.sandbox.json")

    config_mod.load_config = _sandbox_load_config
    config_mod.save_config = _sandbox_save_config
    logging.info("[Sandbox] config load/save patched")

    # ── 3. 替换 PetWindow._create_tts_provider 和 _create_asr_provider ──
    import pet as pet_mod

    _original_create_tts = pet_mod.PetWindow._create_tts_provider
    _original_create_asr = pet_mod.PetWindow._create_asr_provider

    def _sandbox_create_tts(self):
        provider_name = self.config.get("tts", {}).get("provider", "cosyvoice")
        if provider_name == "mock":
            logging.info("[Sandbox] TTS -> MockTTSProvider")
            return MockTTSProvider()
        return _original_create_tts(self)

    def _sandbox_create_asr(self):
        provider_name = self.config.get("asr", {}).get("provider", "whisper_local")
        if provider_name == "mock":
            logging.info("[Sandbox] ASR -> skip (None)")
            return None
        return _original_create_asr(self)

    pet_mod.PetWindow._create_tts_provider = _sandbox_create_tts
    pet_mod.PetWindow._create_asr_provider = _sandbox_create_asr
    logging.info("[Sandbox] PetWindow._create_tts_provider / _create_asr_provider patched")

    # ── 4. 替换 PetManager._load_config / _save_config ──
    import pet_manager as pm_mod
    _original_pm_load = pm_mod.PetManager._load_config
    _original_pm_save = pm_mod.PetManager._save_config

    def _sandbox_pm_load(self):
        cfg = load_sandbox_config()
        return cfg

    def _sandbox_pm_save(self):
        save_sandbox_config(self._config)

    pm_mod.PetManager._load_config = _sandbox_pm_load
    pm_mod.PetManager._save_config = _sandbox_pm_save
    logging.info("[Sandbox] PetManager._load_config / _save_config patched")


def run_interactive():
    """交互模式：启动桌宠，手动测试"""
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont

    app = QApplication(sys.argv)
    app.setApplicationName("OC Desktop Pet (Sandbox)")
    font = QFont("Microsoft YaHei UI", 10)
    app.setFont(font)

    from pet_manager import PetManager

    manager = PetManager()

    # 首次运行自动添加 ophelia
    if not manager.agents:
        manager._config.setdefault("agents", []).append({
            "id": "ophelia",
            "enabled": True,
            "builtin": False,
        })
        manager._save_config()

    manager.launch_all()

    window_count = len(manager.windows)
    print(f"\n{'='*50}")
    print(f"  🧪 沙盒模式已启动")
    print(f"  桌宠数量: {window_count}")
    print(f"  LLM: Mock（不调 API）")
    print(f"  TTS: Mock（不发声）")
    print(f"  配置: sandbox/config.sandbox.json")
    print(f"{'='*50}")
    print(f"  右键桌宠 -> 💬对话 -> 输入消息测试")
    print(f"  关闭窗口或 Ctrl+C 退出")
    print(f"{'='*50}\n")

    sys.exit(app.exec())


def run_scenario(scenario_name: str):
    """自动化场景测试"""
    from sandbox.scenarios import get_scenario
    from sandbox.mock_llm import MockLLMAdapter

    scenario = get_scenario(scenario_name)
    if not scenario:
        print(f"场景 '{scenario_name}' 不存在。可用场景：")
        from sandbox.scenarios import list_scenarios
        for name in list_scenarios():
            print(f"  - {name}")
        return

    print(f"\n{'='*50}")
    print(f"  🧪 场景测试: {scenario['name']}")
    print(f"  {scenario['description']}")
    print(f"  步骤: {len(scenario['steps'])}")
    print(f"{'='*50}\n")

    # 创建 mock adapter 直接测试（不启动 GUI）
    adapter = MockLLMAdapter(agent_id="ophelia")

    # 如果场景有预设脚本
    if "script" in scenario:
        adapter.set_script(scenario["script"])
        print(f"  📝 脚本模式: {len(scenario['script'])} 条预设回复")

    passed = 0
    failed = 0

    for i, step in enumerate(scenario["steps"], 1):
        send_text = step["send"]
        expect_emotion = step.get("expect_emotion")
        expect_contains = step.get("expect_contains")

        print(f"  [{i}/{len(scenario['steps'])}] 发送: {send_text}")

        reply, emotion = adapter.chat(send_text)

        print(f"         回复: {reply}")
        print(f"         情绪: {emotion}")

        errors = []
        if expect_emotion and emotion != expect_emotion:
            errors.append(f"情绪不符 (期望 {expect_emotion}, 实际 {emotion})")
        if expect_contains and expect_contains not in reply:
            errors.append(f"回复不含 '{expect_contains}'")

        if errors:
            print(f"         ❌ FAIL: {'; '.join(errors)}")
            failed += 1
        else:
            print(f"         ✅ PASS")
            passed += 1
        print()

    # 统计
    stats = adapter.stats
    print(f"{'='*50}")
    print(f"  结果: {passed} passed, {failed} failed")
    print(f"  LLM 调用: {stats['calls']} 次")
    print(f"  平均延迟: {stats['avg_latency_ms']}ms")
    print(f"  历史长度: {stats['history_length']}")
    print(f"{'='*50}")

    if failed > 0:
        sys.exit(1)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="桌宠沙盒测试环境",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python sandbox_runner.py                        # 交互模式
  python sandbox_runner.py --scenario basic_chat  # 场景测试
  python sandbox_runner.py --list                 # 列出场景
  python sandbox_runner.py --latency 2.0          # 加大模拟延迟
"""
    )
    parser.add_argument("--scenario", "-s", help="运行指定场景测试")
    parser.add_argument("--list", "-l", action="store_true", help="列出可用场景")
    parser.add_argument("--latency", type=float, default=1.0,
                        help="延迟倍率（1.0=正常, 2.0=双倍延迟）")

    args = parser.parse_args()

    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='[%(levelname)s] %(name)s: %(message)s'
    )

    if args.list:
        from sandbox.scenarios import list_scenarios, SCENARIOS
        print("\n可用场景：\n")
        for s in SCENARIOS:
            print(f"  {s['name']:20s} - {s['description']}")
            print(f"  {'':20s}   步骤: {len(s['steps'])}")
        print()
        return

    if args.scenario:
        # 纯逻辑测试，不需要 GUI
        run_scenario(args.scenario)
        return

    # 交互模式：注入 mock 后启动 GUI
    apply_patches(latency_scale=args.latency)
    run_interactive()


if __name__ == "__main__":
    main()
