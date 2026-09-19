"""FrameBaker 集成 — 帧动画编辑器

FrameBaker 是一个 Bun 全栈应用，支持：
- 多源导入（GIF/MP4 帧提取、PNG 上传、CLI 生成）
- 内置 rembg 遮罩引擎切割背景
- PixiJS 洋葱皮画布编辑帧
- 时间线管理和spritesheet导出
- MCP 服务器（34 个 AI 工具）

集成方式：
1. 本地运行 FrameBaker 服务（bun dev）
2. 通过 MCP 协议控制（POST /mcp）
3. 导出 spritesheet 后由 SpriteRenderer 加载
"""
import os
import subprocess
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# FrameBaker 配置
FRAMEBAKER_PATH = os.environ.get("FRAMEBAKER_PATH", "")  # 用户安装路径
FRAMEBAKER_URL = "http://localhost:3000"  # 默认服务地址
FRAMEBAKER_MCP_ENDPOINT = f"{FRAMEBAKER_URL}/mcp"

# 本模块启动的 bun 子进程句柄（stop 时按 PID 定向终止，不误杀其他 bun）
_framebaker_proc: "Optional[subprocess.Popen]" = None

# 检查 FrameBaker 是否已安装
def is_framebaker_installed() -> bool:
    """检查 FrameBaker 是否已安装并可用"""
    if not FRAMEBAKER_PATH:
        return False
    try:
        # 检查 bun 是否可用
        result = subprocess.run(["bun", "--version"], capture_output=True, timeout=5)
        if result.returncode != 0:
            logger.warning("bun 未安装，FrameBaker 无法运行")
            return False
        # 检查项目目录
        package_json = Path(FRAMEBAKER_PATH) / "package.json"
        if not package_json.exists():
            logger.warning("FrameBaker 目录无 package.json: %s", FRAMEBAKER_PATH)
            return False
        return True
    except Exception as e:
        logger.warning("检查 FrameBaker 失败: %s", e)
        return False

# 启动 FrameBaker 服务
def start_framebaker(detach: bool = True) -> bool:
    """启动 FrameBaker 开发服务器

    Args:
        detach: True=后台启动，False=前台阻塞
    """
    global _framebaker_proc
    if not is_framebaker_installed():
        logger.error("FrameBaker 未安装，请先设置 FRAMEBAKER_PATH 环境变量")
        return False
    try:
        cmd = ["bun", "dev"]
        if detach:
            # 保存子进程句柄，stop 时按 PID 定向终止（B2-6）
            _framebaker_proc = subprocess.Popen(
                cmd,
                cwd=FRAMEBAKER_PATH,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.run(cmd, cwd=FRAMEBAKER_PATH, check=True)
        logger.info("FrameBaker 启动成功: %s", FRAMEBAKER_URL)
        return True
    except Exception as e:
        logger.error("FrameBaker 启动失败: %s", e)
        return False

# 停止 FrameBaker 服务
def stop_framebaker() -> bool:
    """停止 FrameBaker 服务（定向终止：优先按 PID，其次按 cwd/命令行匹配）

    原实现 `taskkill /F /IM bun.exe` 会杀掉机器上所有 bun 进程（B2-6）。
    现在优先终止本模块启动并仍存活的进程；未跟踪时按 FRAMEBAKER_PATH 匹配
    bun 进程的命令行，只停属于 FrameBaker 的那一个。
    """
    global _framebaker_proc
    try:
        # 1) 优先按 PID 终止本模块启动的进程
        proc = _framebaker_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    logger.debug("framebaker: 非致命异常(已静默吞掉)", exc_info=True)
            logger.info("FrameBaker 已停止 (pid=%s)", proc.pid)
            _framebaker_proc = None
            return True
        _framebaker_proc = None

        # 2) 兜底：按 FRAMEBAKER_PATH 匹配 bun 进程的命令行（定向，不误杀）
        if not FRAMEBAKER_PATH:
            logger.warning("停止 FrameBaker 失败：FRAMEBAKER_PATH 未设置")
            return False
        import platform
        if platform.system() == "Windows":
            # 参数化匹配：用 .Contains()（而非 -like）避免路径中的通配符多匹配；
            # 单引号翻倍转义，防止路径里的 ' 破坏字符串字面量语法。
            _escaped = FRAMEBAKER_PATH.replace("'", "''").lower()
            ps = (
                "Get-CimInstance Win32_Process | "
                f"Where-Object {{ $_.Name -eq 'bun.exe' -and $null -ne $_.CommandLine -and "
                f"$_.CommandLine.ToLower().Contains('{_escaped}') }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
        else:
            # macOS/Linux：pkill -f 匹配 bun 命令行中的 FRAMEBAKER_PATH
            subprocess.run(["pkill", "-f", f"bun.*{FRAMEBAKER_PATH}"], capture_output=True)
        logger.info("FrameBaker 已停止（按路径匹配）")
        return True
    except Exception as e:
        logger.warning("停止 FrameBaker 失败: %s", e)
        return False

# MCP 客户端（占位，**未实现**；本机未安装 FrameBaker，无调用方）
class FrameBakerMCP:
    """FrameBaker MCP 客户端（占位类，未实现）。

    ⚠️ 本类目前**没有任何调用方**（全库仅此定义）；本机也未检测到 FrameBaker
    实例。因此不凭空实现：方法一律抛 ``NotImplementedError``（而不是静默返回
    ``None``），避免将来误把它当成可用接口。

    设计意图（未经验证）：通过 HTTP POST ``{endpoint}/mcp`` 与 FrameBaker 通信。
    真要落地时，需先拿到 FrameBaker 的 MCP 协议规格 + 一个可用实例。
    """

    def __init__(self, endpoint: str = FRAMEBAKER_MCP_ENDPOINT):
        self.endpoint = endpoint
        self._session_id = None

    def initialize(self) -> bool:
        """初始化 MCP 连接（未实现）。"""
        raise NotImplementedError(
            "FrameBakerMCP.initialize 未实现：无 FrameBaker 实例与 MCP 协议规格")

    def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """调用 FrameBaker 工具（未实现）。"""
        raise NotImplementedError(
            "FrameBakerMCP.call_tool 未实现：无 FrameBaker 实例与 MCP 协议规格")

    def export_spritesheet(self, project_id: str, output_path: str) -> bool:
        """导出 spritesheet（未实现）。"""
        raise NotImplementedError(
            "FrameBakerMCP.export_spritesheet 未实现：无 FrameBaker 实例与 MCP 协议规格")

# 菜单集成（供 pet.py 调用）
def get_framebaker_menu_items():
    """返回 FrameBaker 菜单项"""
    items = []
    if is_framebaker_installed():
        items.append(("🎬 启动 FrameBaker", start_framebaker))
        items.append(("🛑 停止 FrameBaker", stop_framebaker))
    else:
        items.append(("⚙️ 配置 FrameBaker", open_settings))
    return items

def open_settings():
    """打开 FrameBaker 设置"""
    from PySide6.QtWidgets import QInputDialog
    path, ok = QInputDialog.getText(
        None, "FrameBaker 路径",
        "请输入 FrameBaker 安装路径（包含 package.json 的目录）:"
    )
    if ok and path:
        os.environ["FRAMEBAKER_PATH"] = path
        logger.info("FrameBaker 路径已设置: %s", path)
        # 持久化到配置
        try:
            from config import load_config, save_config
            cfg = load_config()
            cfg.setdefault("framebaker", {})["path"] = path
            save_config(cfg)
        except Exception as e:
            logger.warning("保存 FrameBaker 配置失败: %s", e)
