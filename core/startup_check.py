"""启动自检工具 — 复用 crash_collector 范式，反向做启动前检查

检查项：
    - Hanako 可连？（~/.hanako/ 存在 + 可写）
    - 角色目录存在？（characters/ 存在 + 至少一个角色包）
    - 模型可读？（live2d-py 可 import + 模型路径有效）
    - TTS 可用？（TTS 引擎可导入）

输出：把 WARNING 翻成一句人话 + 一行修复命令
"""
from __future__ import annotations

import os
import sys
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── 检查结果 ──

class CheckResult:
    """单项检查结果"""
    def __init__(self, name: str, ok: bool, message: str, fix: str = ""):
        self.name = name
        self.ok = ok
        self.message = message
        self.fix = fix

    def __str__(self) -> str:
        icon = "✅" if self.ok else "❌"
        s = f"{icon} {self.name}: {self.message}"
        if not self.ok and self.fix:
            s += f"\n   修复: {self.fix}"
        return s


class StartupReport:
    """启动自检报告"""
    def __init__(self):
        self.results: list[CheckResult] = []
        self.all_ok = True

    def add(self, result: CheckResult):
        self.results.append(result)
        if not result.ok:
            self.all_ok = False

    def print(self) -> None:
        """打印报告"""
        if not self.results:
            return
        print("\n" + "=" * 60)
        print("  oc-pet 启动自检")
        print("=" * 60)
        for r in self.results:
            print(r)
        print("=" * 60)
        if not self.all_ok:
            print("⚠️  部分检查未通过，请修复后重启。")
            print("   详细日志见 logs/oc_pet.log\n")
        else:
            print("✅ 全部检查通过，桌宠可以正常启动。\n")

    def to_markdown(self) -> str:
        """生成 Markdown 格式报告"""
        lines = ["# oc-pet 启动自检报告", "", "| 检查项 | 状态 | 说明 | 修复命令 |", "|---|---|---|---|"]
        for r in self.results:
            icon = "✅" if r.ok else "❌"
            fix = r.fix or "—"
            lines.append(f"| {r.name} | {icon} | {r.message} | `{fix}` |")
        lines.append("")
        if not self.all_ok:
            lines.append("⚠️ 部分检查未通过，请修复后重启。")
        else:
            lines.append("✅ 全部检查通过。")
        return "\n".join(lines)


# ── 检查函数 ──

def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def check_hanako_home() -> CheckResult:
    """检查 Hanako 是否可连"""
    hanako_home = Path.home() / ".hanako"
    if not hanako_home.exists():
        return CheckResult(
            "Hanako 可连？",
            False,
            f"~/.hanako/ 目录不存在（{hanako_home}）",
            "安装 Hanako: git clone https://github.com/liliMozi/openhanako && cd openhanako && python main.py"
        )
    if not hanako_home.is_dir():
        return CheckResult("Hanako 可连？", False, "路径存在但不是目录", "检查 ~/.hanako 是否正确")
    if not os.access(hanako_home, os.W_OK):
        return CheckResult(
            "Hanako 可连？",
            False,
            f"~/.hanako/ 不可写（{hanako_home}）",
            "修复权限: chmod +w ~/.hanako"
        )
    # 检查 agents 目录
    agents_dir = hanako_home / "agents"
    if agents_dir.exists() and agents_dir.is_dir():
        agents = list(agents_dir.iterdir())
        if not agents:
            return CheckResult(
                "Hanako 可连？",
                False,
                "~/.hanako/agents/ 为空，没有已配置的 agent",
                "在 Hanako 里创建 agent 或复制已有 agent 到 ~/.hanako/agents/"
            )
    return CheckResult("Hanako 可连？", True, f"~/.hanako/ 存在且可写，{len(agents) if agents_dir.exists() else 0} 个 agent")


def check_characters_dir() -> CheckResult:
    """检查角色目录"""
    root = _project_root()
    chars_dir = root / "characters"
    if not chars_dir.exists():
        return CheckResult(
            "角色目录存在？",
            False,
            f"characters/ 目录不存在（{chars_dir}）",
            "git clone 仓库后自动包含，或手动创建"
        )
    if not chars_dir.is_dir():
        return CheckResult("角色目录存在？", False, "路径存在但不是目录", "检查 characters 目录")
    # 检查至少一个角色包
    packages = [p for p in chars_dir.iterdir() if p.is_dir() and (p / "pet.json").exists()]
    if not packages:
        return CheckResult(
            "角色目录存在？",
            False,
            "characters/ 下没有有效角色包（缺少 pet.json）",
            "运行 python tools/fetch_free_live2d_sample.py haru 下载示例角色"
        )
    return CheckResult("角色目录存在？", True, f"找到 {len(packages)} 个角色包: {', '.join(p.name for p in packages)}")


def check_live2d_import() -> CheckResult:
    """检查 live2d-py 是否可导入"""
    try:
        import live2d.v3
        return CheckResult("live2d-py 可导入？", True, "live2d.v3 导入成功")
    except ImportError as e:
        return CheckResult(
            "live2d-py 可导入？",
            False,
            f"live2d-py 未安装或导入失败: {e}",
            "pip install live2d-py"
        )
    except Exception as e:
        return CheckResult(
            "live2d-py 可导入？",
            False,
            f"live2d-py 导入异常: {e}",
            "pip install --force-reinstall live2d-py"
        )


def check_tts_import() -> CheckResult:
    """检查 TTS 引擎是否可导入"""
    # 检查 edge-tts（默认免费引擎）
    try:
        import edge_tts
        return CheckResult("TTS 可用？", True, "edge-tts 已安装（免费引擎可用）")
    except ImportError:
        log.debug("startup_check: 非致命异常(已静默吞掉)", exc_info=True)
    # 检查 CosyVoice（本地引擎）
    try:
        import sys
        sys.path.insert(0, str(_project_root() / "tts_provider"))
        from cosyvoice_provider import CosyVoiceProvider
        return CheckResult("TTS 可用？", True, "CosyVoice 本地引擎可用")
    except Exception:
        log.debug("startup_check: 非致命异常(已静默吞掉)", exc_info=True)
    return CheckResult(
        "TTS 可用？",
        False,
        "没有可用的 TTS 引擎（edge-tts 和 CosyVoice 都不可用）",
        "pip install edge-tts 或配置本地 CosyVoice"
    )


def check_model_path(model_path: Optional[str] = None) -> CheckResult:
    """检查模型路径（可选）"""
    if not model_path:
        return CheckResult("模型可读？", True, "未指定模型路径，跳过检查")
    path = Path(model_path)
    if not path.exists():
        return CheckResult(
            "模型可读？",
            False,
            f"模型文件不存在: {path}",
            "检查 config.json 的 character 字段，或运行 python tools/fetch_free_live2d_sample.py haru"
        )
    if not path.is_file():
        return CheckResult("模型可读？", False, f"路径存在但不是文件: {path}", "检查模型路径")
    return CheckResult("模型可读？", True, f"模型文件存在: {path}")


# ── 主函数 ──

def run_startup_check(model_path: Optional[str] = None) -> StartupReport:
    """运行启动自检，返回报告"""
    report = StartupReport()
    report.add(check_hanako_home())
    report.add(check_characters_dir())
    report.add(check_live2d_import())
    report.add(check_tts_import())
    report.add(check_model_path(model_path))
    return report


def print_startup_check(model_path: Optional[str] = None) -> None:
    """运行启动自检并打印"""
    report = run_startup_check(model_path)
    report.print()
    log.info("启动自检完成: %d/%d 通过", sum(1 for r in report.results if r.ok), len(report.results))


# ── CLI 入口 ──

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="oc-pet 启动自检")
    parser.add_argument("--model", type=str, help="模型路径（可选）")
    parser.add_argument("--markdown", action="store_true", help="输出 Markdown 格式")
    args = parser.parse_args()

    report = run_startup_check(args.model)
    if args.markdown:
        print(report.to_markdown())
    else:
        report.print()

    sys.exit(0 if report.all_ok else 1)
