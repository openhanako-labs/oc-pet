"""computer_use_bridge — 桌宠的「手」：把本机 cua-driver 的 computer-use 能力接进桌宠。

设计（2026-09-20）
------------------
桌宠是 Hana 的翻译层（嘴 + 眼 + 表演者），**脑在 Hana**。要让桌宠能操作电脑，
需要一条「Hana → 桌宠 MCP(8979) → 本机 computer-use」的通路。本模块提供执行侧：
调用本机已安装的 cua-driver（trycua/cua，MIT），把它的工具代理成桌宠能力，
再由 `core/mcp_server.py` 暴露给 Hana。

为什么走 CLI 而不是 MCP 客户端
------------------------------
cua-driver 有 stdio MCP（`cua-driver mcp`）与 CLI（`cua-driver call`）两个入口。
桌宠已有 MCP 客户端（`skyrim_bridge`），但那是常驻会话模型；这里的调用是
一次性的、无状态的取数/动作，CLI 子进程更简单、隔离更好，也不占用 MCP 会话。

安全
----
- 默认关（config `computer_use.enabled=false`）：零行为、不探测、不占端口。
- 读工具（status/apps/windows/window_state）无副作用，默认允许。
- 写工具（launch/click/type/key）另有 `allow_actions` 开关，默认 False。
- 所有子进程带超时；异常一律降级为错误 dict，**绝不让 MCP 线程崩**。
- 不自动拉起 daemon，除非 config `auto_start_daemon=true`（会新增一个后台进程）。

配置（config.json 的 `computer_use` 块）
---------------------------------------
    {
      "enabled": false,
      "driver_path": "",            // 留空 = 自动探测
      "allow_actions": false,
      "auto_start_daemon": false,
      "timeout_s": 30
    }
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 已知安装位置（与 cua 官方 Windows 安装脚本一致）
DEFAULT_DRIVER_CANDIDATES = (
    os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Programs", "Cua", "cua-driver", "bin", "cua-driver.exe",
    ),
    os.path.join(
        os.environ.get("USERPROFILE", ""), ".cua-driver", "packages", "current", "cua-driver.exe",
    ),
)

DEFAULT_TIMEOUT_S = 30

# 代理给桌宠 MCP 的工具名 → cua-driver 工具名
READ_TOOLS = {
    "status": "health_report",
    "apps": "list_apps",
    "windows": "list_windows",
    "window_state": "get_window_state",
}
ACTION_TOOLS = {
    "launch": "launch_app",
    "click": "click",
    "type": "type_text",
    "key": "press_key",
}


def resolve_driver_path(config: Optional[dict] = None) -> Optional[str]:
    """按 config → 环境变量 → PATH → 已知位置 的顺序解析 cua-driver 可执行文件。"""
    cfg = config or {}
    explicit = str(cfg.get("driver_path") or "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    env_path = str(os.environ.get("CUA_DRIVER_PATH") or "").strip()
    if env_path and os.path.isfile(env_path):
        return env_path
    found = shutil.which("cua-driver")
    if found:
        return found
    for cand in DEFAULT_DRIVER_CANDIDATES:
        if cand and os.path.isfile(cand):
            return cand
    return None


class ComputerUseBridge:
    """cua-driver 的薄封装：可探测、可读、可写（写受 allow_actions 门控）。"""

    def __init__(self, config: Optional[dict] = None):
        cfg = dict(config or {})
        self._cfg = cfg
        self._driver = resolve_driver_path(cfg)
        self._allow_actions = bool(cfg.get("allow_actions", False))
        self._auto_start = bool(cfg.get("auto_start_daemon", False))
        try:
            self._timeout = float(cfg.get("timeout_s") or DEFAULT_TIMEOUT_S)
        except (TypeError, ValueError):
            self._timeout = float(DEFAULT_TIMEOUT_S)

    # ── 属性 ──

    @property
    def driver_path(self) -> Optional[str]:
        return self._driver

    @property
    def available(self) -> bool:
        return bool(self._driver)

    @property
    def allow_actions(self) -> bool:
        return self._allow_actions

    # ── 底层：子进程调用 ──

    def _run(self, args: list[str], timeout: Optional[float] = None) -> dict:
        """跑一次 cua-driver 子进程，返回 {ok, code, stdout, stderr}。绝不抛。"""
        if not self._driver:
            return {"ok": False, "error": "未找到 cua-driver（配置 computer_use.driver_path 或安装后置入 PATH）"}
        try:
            proc = subprocess.run(
                [self._driver, *args],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout or self._timeout,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"cua-driver 超时（{timeout or self._timeout}s）"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"cua-driver 调用失败: {e}"}
        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
        }

    def _call_json(self, tool: str, args: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
        """`cua-driver call <tool> <json>` → 解析 JSON；失败降级为 {error}。"""
        r = self._run(["call", tool, json.dumps(args or {}, ensure_ascii=False)], timeout=timeout)
        if not r.get("ok"):
            msg = (r.get("error") or r.get("stderr") or r.get("stdout") or "").strip()
            return {"error": msg[:400] or "cua-driver call 失败"}
        out = (r.get("stdout") or "").strip()
        try:
            return json.loads(out)
        except Exception:  # noqa: BLE001
            return {"error": f"cua-driver 输出非 JSON: {out[:200]}"}

    # ── 读：状态探测 ──

    def status(self) -> dict:
        """返回驱动可用性 / 版本 / daemon 状态。只读。"""
        info: dict[str, Any] = {
            "available": self.available,
            "driver_path": self._driver,
            "allow_actions": self._allow_actions,
        }
        if not self.available:
            return info
        ver = self._run(["--version"], timeout=10)
        info["version"] = (ver.get("stdout") or "").strip() if ver.get("ok") else None
        st = self._run(["status"], timeout=10)
        text = (st.get("stdout") or "") + (st.get("stderr") or "")
        info["daemon_running"] = "daemon is running" in text.lower()
        if not info["daemon_running"] and self._auto_start:
            started = self.ensure_daemon()
            info["daemon_auto_started"] = started
            info["daemon_running"] = bool(started)
        return info

    def ensure_daemon(self) -> bool:
        """按需后台拉起 `cua-driver serve`。仅在 auto_start_daemon=true 时被调用。"""
        if not self.available:
            return False
        try:
            flags = 0x00000008  # DETACHED_PROCESS
            if sys.platform == "win32":
                flags |= 0x08000000  # CREATE_NO_WINDOW
                subprocess.Popen(
                    [self._driver, "serve"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                    creationflags=flags, close_fds=True,
                )
            else:
                subprocess.Popen(
                    [self._driver, "serve"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                    start_new_session=True, close_fds=True,
                )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("cua-driver serve 启动失败: %s", e)
            return False

    # ── 读工具 ──

    def list_apps(self) -> dict:
        return self._call_json(READ_TOOLS["apps"])

    def list_windows(self, pid: Optional[int] = None) -> dict:
        args = {"pid": int(pid)} if pid else {}
        return self._call_json(READ_TOOLS["windows"], args)

    def window_state(self, pid: int, window_id: int) -> dict:
        return self._call_json(READ_TOOLS["window_state"], {"pid": int(pid), "window_id": int(window_id)})

    # ── 写工具（受 allow_actions 门控）──

    def _deny_write(self) -> dict:
        return {"error": "computer_use 写操作已禁用（config computer_use.allow_actions=false）"}

    def launch(self, *, aumid: str = "", name: str = "", path: str = "") -> dict:
        if not self._allow_actions:
            return self._deny_write()
        args: dict = {}
        if aumid:
            args["aumid"] = aumid
        if name:
            args["name"] = name
        if path:
            args["path"] = path
        if not args:
            return {"error": "launch 需要 aumid / name / path 之一"}
        return self._call_json(ACTION_TOOLS["launch"], args)

    def click(self, *, pid: int, window_id: Optional[int] = None,
              element_token: str = "", x: Optional[int] = None, y: Optional[int] = None) -> dict:
        if not self._allow_actions:
            return self._deny_write()
        args: dict = {"pid": int(pid)}
        if window_id is not None:
            args["window_id"] = int(window_id)
        if element_token:
            args["element_token"] = str(element_token)
        elif x is not None and y is not None:
            args["x"] = int(x)
            args["y"] = int(y)
        else:
            return {"error": "click 需要 element_token 或 (x, y)"}
        return self._call_json(ACTION_TOOLS["click"], args)

    def type_text(self, *, text: str, pid: Optional[int] = None) -> dict:
        if not self._allow_actions:
            return self._deny_write()
        args: dict = {"text": str(text)}
        if pid is not None:
            args["pid"] = int(pid)
        return self._call_json(ACTION_TOOLS["type"], args)

    def press_key(self, *, key: str, pid: Optional[int] = None) -> dict:
        if not self._allow_actions:
            return self._deny_write()
        args: dict = {"key": str(key)}
        if pid is not None:
            args["pid"] = int(pid)
        return self._call_json(ACTION_TOOLS["key"], args)


def build_bridge(config: Optional[dict] = None) -> Optional[ComputerUseBridge]:
    """按 config 的 `computer_use` 块构建 bridge；未启用时返回 None。"""
    cfg = (config or {}).get("computer_use") or {}
    if not bool(cfg.get("enabled", False)):
        return None
    return ComputerUseBridge(cfg)
