"""VRMRenderer - 3D VRM 角色渲染器（QWebEngineView + three.js + three-vrm）

架构
====
    Python (本文件)                     浏览器侧（avatar/vrm_view/index.html）
    ─────────────────                   ─────────────────────────────────────
    QWebEngineView  ──── 加载页面 ────▶  three.js 场景
      │  runJavaScript("window.VRM…")    │  three-vrm 载入 .vrm
      │  QWebChannel(bridge)  ◀── 上报 ──┤  expression / lookAt / springbone
      │  QTimer 轮询 status()  ◀── 后备 ─┤  window.VRM.status()
      ▼                                  ▼
    口型 / 情绪 / 视线 / 缩放            每帧渲染（透明背景）

为什么是 WebEngine 而不是自写 OpenGL：three.js + @pixiv/three-vrm 是 VRM 事实标准，
VRM 1.0 的 springbone / MToon / humanoid 归一化骨骼都在其中。自写管线等于重做一遍
且永远落后于规范。内存代价 150~300MB（比 Live2D 重），因此本渲染器只在角色
pet.json 声明 "format": "vrm" 时才会被工厂选中。

依赖
====
- `PySide6-Addons` 提供 QtWebEngineWidgets / QtWebChannel（已在 requirements 里）。
- `avatar/vrm_view/vendor/` 为本地 vendored 三方 JS（three + three-vrm + GLTFLoader），
  离线可用；版本与刷新方式见该目录 README.md。

不可用时的降级
==============
- WebEngine 导入失败 → `unsupported=True` + 明确原因，显示提示 label（不崩）。
- 角色目录下找不到 .vrm → `load()` 返回 False，原因写进 `unsupported_reason`。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, QUrl, QUrlQuery, Signal, Slot
from PySide6.QtWidgets import QLabel, QWidget

from avatar.base import AvatarRenderer

logger = logging.getLogger(__name__)

# ── 路径 ──────────────────────────────────────────────────────────────

_MODULE_DIR = Path(__file__).resolve().parent
VIEW_DIR = _MODULE_DIR / "vrm_view"
VIEW_INDEX = VIEW_DIR / "index.html"
VENDOR_DIR = VIEW_DIR / "vendor"

# 等待 JS 上报 ready/error 的默认上限（秒）
DEFAULT_READY_TIMEOUT = 30.0
# 「模块未执行」的宽限期（秒）：页面要先解析约 1.7MB 的三方 JS
# （three + three-vrm），启动瞬间 window.VRM 还是前置层预置的那个对象，
# 此时 bootstrapped=False 属于正常中间态，不能当失败。
MODULE_GRACE_S = 25.0
# 未收到 QWebChannel 时，轮询 window.VRM.status() 的间隔（毫秒）
STATUS_POLL_MS = 400


# ── 纯函数（无 Qt 依赖，便于单测）─────────────────────────────────────


def find_vrm_model(char_dir: str | os.PathLike | None) -> str | None:
    """在角色目录下找 .vrm 模型（优先根目录，其次 vrm/ 子目录，最后递归）。

    返回绝对路径；找不到返回 None。
    """
    if not char_dir:
        return None
    root = Path(char_dir)
    if not root.is_dir():
        return None
    # 1) 根目录下的 .vrm（按文件名排序，结果稳定）
    hits = sorted(p for p in root.glob("*.vrm") if p.is_file())
    if hits:
        return str(hits[0])
    # 2) vrm/ 子目录
    sub = root / "vrm"
    if sub.is_dir():
        hits = sorted(p for p in sub.glob("*.vrm") if p.is_file())
        if hits:
            return str(hits[0])
    # 3) 递归兜底
    hits = sorted(p for p in root.rglob("*.vrm") if p.is_file())
    return str(hits[0]) if hits else None


# Live2D 参数名 → VRM 表情名的映射（VRM 没有"任意参数"，只有预设表情 + 自定义表情）
_PARAM_TO_EXPRESSION = {
    "parammouthopeny": "aa",
    "parammouthform": "ou",
    "parameyelopen": "blink",
    "parameyeropen": "blink",
}


def intent_to_commands(intent: dict) -> list[dict]:
    """把结构化动作意图翻译成渲染器无关的 JS 调用列表（纯函数）。

    返回 ``[{"fn": "setEmotion", "args": [...]}, ...]``；非法输入 → 空列表。

    映射规则：
    - ``gesture`` 命中情绪名（happy/angry/sad/relaxed/surprised/neutral）→ 设表情；
      命中自定义表情名 → setExpression；否则忽略（VRM 无 motion 文件可播）。
    - ``params``：Live2D 参数名按 ``_PARAM_TO_EXPRESSION`` 映射为 VRM 表情，
      值取绝对值（Live2D 用负值表示"闭眼"，VRM 用 0 表示）。
    - ``va``（连续 VA 坐标）→ 拆成两个自定义表情通道 ``va_x`` / ``va_y``
      （模型若无这两个自定义表情则 JS 侧安全忽略）。
    """
    if not isinstance(intent, dict):
        return []

    cmds: list[dict] = []
    try:
        intensity = float(intent.get("intensity", 1.0))
    except (TypeError, ValueError):
        intensity = 1.0
    intensity = max(0.0, min(1.0, intensity))

    emotion_presets = {"happy", "angry", "sad", "relaxed", "surprised", "neutral"}
    gesture = intent.get("gesture")
    if isinstance(gesture, str) and gesture:
        name = gesture.strip()
        low = name.lower()
        if low in emotion_presets:
            cmds.append({"fn": "setEmotion", "args": [low, intensity]})
        elif name:
            cmds.append({"fn": "setExpression", "args": [name, intensity]})

    params = intent.get("params")
    if isinstance(params, dict):
        for key, value in params.items():
            if not isinstance(key, str):
                continue
            expr = _PARAM_TO_EXPRESSION.get(key.strip().lower())
            if not expr:
                continue
            try:
                num = abs(float(value))
            except (TypeError, ValueError):
                continue
            # Live2D 的 -1~1 与 VRM 的 0~1 量纲接近但不完全一致，夹紧后直用
            cmds.append({"fn": "setExpression", "args": [expr, min(1.0, num)]})

    va = intent.get("va")
    if isinstance(va, (list, tuple)) and len(va) == 2:
        try:
            x = max(-1.0, min(1.0, float(va[0])))
            y = max(-1.0, min(1.0, float(va[1])))
            cmds.append({"fn": "setExpression", "args": ["va_x", abs(x)]})
            cmds.append({"fn": "setExpression", "args": ["va_y", abs(y)]})
        except (TypeError, ValueError):
            pass

    return cmds


def normalize_to_view(x: float, y: float, view_w: int, view_h: int) -> tuple[float, float]:
    """屏幕坐标 → 视图内归一化坐标（-1~1，左下为 (-1,1)）。"""
    w = max(1, int(view_w))
    h = max(1, int(view_h))
    nx = (float(x) / w) * 2.0 - 1.0
    ny = 1.0 - (float(y) / h) * 2.0
    return (max(-1.0, min(1.0, nx)), max(-1.0, min(1.0, ny)))


# ── Qt 桥（Python ⇄ JS）──────────────────────────────────────────────


class VrmBridge(QObject):
    """QWebChannel 对象：页面侧上报 ready / error / log。"""

    ready = Signal(str)
    failed = Signal(str)
    logged = Signal(str)

    @Slot(str)
    def reportReady(self, text: str) -> None:  # noqa: N802（JS 侧方法名）
        logger.info("VRMRenderer 就绪: %s", text)
        self.ready.emit(str(text))

    @Slot(str)
    def reportError(self, text: str) -> None:  # noqa: N802
        logger.warning("VRMRenderer 页面报错: %s", text)
        self.failed.emit(str(text))

    @Slot(str)
    def log(self, text: str) -> None:  # noqa: N802
        self.logged.emit(str(text))


def _import_webengine():
    """惰性导入 QtWebEngine（须在 QApplication 之后导入时也应可用）。

    返回 (QWebEngineView, QWebEnginePage, QWebEngineSettings, QWebChannel)；
    不可用抛 ImportError。
    """
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebChannel import QWebChannel

    return QWebEngineView, QWebEnginePage, QWebEngineSettings, QWebChannel


def _make_logging_page_class(QWebEnginePage):
    """包装 QWebEnginePage，把页面控制台接进 Python 日志。

    这一步不是为了好看：**ES 模块导入失败的报错只会出现在浏览器控制台**
    （模块内的错误监听器根本没机会安装），没有它就会表现为「白屏且零线索」。
    """

    class _LoggingPage(QWebEnginePage):
        def javaScriptConsoleMessage(self, level, message, line_number, source_id):  # noqa: N802
            try:
                src = str(source_id or "").rsplit("/", 1)[-1]
                logger.warning("VRM 页面[%s] %s:%s — %s", str(level), src, line_number, message)
            except Exception:
                pass
            super().javaScriptConsoleMessage(level, message, line_number, source_id)

    return _LoggingPage


# ── 渲染器 ────────────────────────────────────────────────────────────


class VRMRenderer(AvatarRenderer):
    """3D VRM 渲染器（QWebEngineView + three.js + three-vrm）。"""

    def __init__(self, parent: QWidget, char_dir: str | os.PathLike | None = None):
        super().__init__()
        self._parent = parent
        self._char_dir = Path(char_dir) if char_dir else None
        self._scale = 1.0
        self._facing_right = True
        self._base_label_pos = QPoint(0, 0)
        self._alpha = 1.0

        self._view = None
        self._channel = None
        self._bridge: VrmBridge | None = None
        self._poll_timer: QTimer | None = None
        self._poll_count = 0
        self._load_started_at = 0.0
        self._model_path: str | None = None
        self._ready = False
        self._page_error = ""
        self._ready_hooks: list[Callable[[bool, str], None]] = []

        # 兼容属性（pet.py 等会直接访问；缺失会 AttributeError）
        self.unsupported = False
        self.unsupported_reason = ""
        self._frames: dict = {}
        self._frame_tops: dict = {}
        self._anim_timer = None
        self._anim_seq = "idle"
        self._anim_idx = 0
        self._anim_range = (None, None)
        self._opacity_effect: Optional[object] = None
        self._gaze_enabled = True
        self._gaze_offset_x = 0.0
        self._gaze_offset_y = 0.0

        # 占位 label：仅当 WebEngine 不可用/未加载时可见（降级用）
        self.char_label = QLabel(parent)
        self.char_label.setAlignment(Qt.AlignCenter)
        self.char_label.setFixedSize(192, 208)
        self.char_label.move(10, 0)
        self.char_label.lower()
        self.char_label.hide()

    # ── 视图装配 ──

    def _ensure_view(self) -> bool:
        """创建 QWebEngineView + QWebChannel。失败 → 降级并返回 False。"""
        if self._view is not None:
            return True
        try:
            QWebEngineView, QWebEnginePage, QWebEngineSettings, QWebChannel = _import_webengine()
        except Exception as e:
            self.unsupported = True
            self.unsupported_reason = f"QtWebEngine 不可用（{type(e).__name__}）"
            logger.warning("VRMRenderer: %s，降级为占位", self.unsupported_reason)
            self._show_placeholder(self.unsupported_reason)
            return False

        view = QWebEngineView(self._parent)
        # 用带日志的页面类（模块导入失败只能从控制台看到）
        try:
            view.setPage(_make_logging_page_class(QWebEnginePage)(view))
        except Exception:
            logger.debug("vrm_renderer: 自定义页面类装配失败（用默认页）", exc_info=True)
        # 透明背景：桌宠窗口本身是透明的
        try:
            view.page().setBackgroundColor(Qt.transparent)
        except Exception:
            logger.debug("vrm_renderer: 透明背景设置失败（非致命）", exc_info=True)
        # 允许 file:// 页面读取本地模型/JS（不放开远程）
        try:
            s = view.settings()
            s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
            s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
            s.setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)
        except Exception:
            logger.debug("vrm_renderer: WebEngine 设置写入失败（非致命）", exc_info=True)

        # QWebChannel（页面里若拿不到 qwebchannel.js 会自动退化为轮询 status()）
        self._bridge = VrmBridge()
        self._bridge.ready.connect(self._on_ready)
        self._bridge.failed.connect(self._on_failed)
        self._bridge.logged.connect(lambda t: logger.debug("VRM 页面: %s", t))
        try:
            channel = QWebChannel(view.page())
            channel.registerObject("bridge", self._bridge)
            view.page().setWebChannel(channel)
            self._channel = channel
        except Exception as e:
            logger.debug("VRMRenderer: QWebChannel 装配失败（改用轮询）: %s", e)

        view.hide()
        # 载入生命周期接入日志（诊断时非常关键：模块导入失败只能从控制台看到）
        try:
            view.loadFinished.connect(
                lambda ok: logger.info("VRM 视图载入%s", "完成" if ok else "失败"))
            view.renderProcessTerminated.connect(
                lambda st, code: logger.warning("VRM 渲染进程终止: status=%s code=%s", st, code))
        except Exception:
            logger.debug("vrm_renderer: 载入事件接入失败（非致命）", exc_info=True)
        self._view = view
        return True

    def _show_placeholder(self, text: str) -> None:
        self.char_label.setText(f"[VRM 未启用]\n{text}")
        self.char_label.setStyleSheet("color: #e6e6f0; font-size: 13px;")
        self.char_label.show()

    def _start_poll(self) -> None:
        """轮询 window.VRM.status()（QWebChannel 不可用时的后备通道）。"""
        if self._view is None:
            return
        if self._poll_timer is None:
            self._poll_timer = QTimer()
            self._poll_timer.setInterval(STATUS_POLL_MS)
            self._poll_timer.timeout.connect(self._poll_status)
        if not self._poll_timer.isActive():
            self._poll_timer.start()

    def _poll_status(self) -> None:
        if self._view is None:
            return
        if self._ready:
            if self._poll_timer is not None:
                self._poll_timer.stop()
            return
        self._poll_count += 1
        try:
            # 回调必须是**被强引用的绑定方法**。用局部闭包（lambda / 内嵌 def）在
            # PySide 下可能于 JS 真正执行前就被回收，表现为「runJavaScript
            # 永远不回调」——这是本模块踩过的坑。
            # 另：页面侧一律 JSON.stringify——PySide 对 JS 对象/数组的转换
            # 不可靠（会回 None），字符串/数字才稳。
            self._view.page().runJavaScript(
                "window.VRM ? JSON.stringify(window.VRM.status()) : null",
                self._on_poll_result)
        except Exception:
            logger.debug("vrm_renderer: 状态轮询失败（非致命）", exc_info=True)

    def _on_poll_result(self, value) -> None:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                return
        if not isinstance(value, dict):
            return
        err = value.get("error") or ""
        if err:
            self._on_failed(str(err))
        elif value.get("ready"):
            self._on_ready(f"{value.get('name') or 'VRM'} (VRM{value.get('version') or '?'})")
        elif value.get("bootstrapped") is False or (
                value.get("bootstrapped") is None and not self._ready):
            # 模块从没执行（导入失败）——但必须先过宽限期：页面解析三方 JS
            # 需要几秒，启动瞬间 bootstrapped=False 是正常中间态。
            if (time.monotonic() - self._load_started_at) < MODULE_GRACE_S:
                return
            evs = value.get("events") or []
            self._on_failed("ES 模块未执行"
                            + (f"：{'; '.join(map(str, evs[-3:]))}" if evs else "（无错误事件）"))

    def _on_ready(self, text: str) -> None:
        if self._ready:
            return
        self._ready = True
        self._page_error = ""
        if self._poll_timer is not None:
            self._poll_timer.stop()
        logger.info("VRMRenderer 已加载模型: %s", text)
        for hook in list(self._ready_hooks):
            try:
                hook(True, text)
            except Exception:
                logger.debug("vrm_renderer: ready 回调异常（非致命）", exc_info=True)

    def _on_failed(self, text: str) -> None:
        if self._page_error:
            return
        self._page_error = str(text)
        self.unsupported = True
        self.unsupported_reason = f"VRM 页面加载失败: {self._page_error}"
        logger.warning("VRMRenderer 加载失败: %s", self._page_error)
        for hook in list(self._ready_hooks):
            try:
                hook(False, self._page_error)
            except Exception:
                logger.debug("vrm_renderer: failed 回调异常（非致命）", exc_info=True)

    def on_ready(self, hook: Callable[[bool, str], None]) -> None:
        """注册就绪/失败回调 ``hook(ok: bool, info: str)``；已就绪则立即调用。"""
        self._ready_hooks.append(hook)
        if self._ready:
            hook(True, "ready")
        elif self._page_error:
            hook(False, self._page_error)

    def wait_until_ready(self, timeout: float = DEFAULT_READY_TIMEOUT) -> bool:
        """自旋事件循环等待 JS 上报（供测试/诊断用，不建议在生产启动路径调用）。"""
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        deadline = time.monotonic() + max(0.0, float(timeout))
        while not (self._ready or self._page_error):
            if time.monotonic() > deadline:
                return False
            if app is not None:
                app.processEvents()
            else:
                time.sleep(0.02)
            time.sleep(0.01)
        return self._ready

    # ── JS 调用 ──

    def _js(self, expr: str) -> None:
        if self._view is None:
            return
        try:
            self._view.page().runJavaScript(expr)
        except Exception:
            logger.debug("vrm_renderer: runJavaScript 失败（非致命）: %s", expr[:80], exc_info=True)

    def _js_args(self, fn: str, *args) -> None:
        """调用 window.VRM.<fn>(<json 参数…>)。"""
        payload = ", ".join(json.dumps(a, ensure_ascii=False) for a in args)
        self._js(f"window.VRM && window.VRM.{fn} && window.VRM.{fn}({payload})")

    # ── 生命周期 ──

    def load(self, character_id: str, sprite_dir: str = None) -> bool:
        self._character_id = character_id
        char_dir = self._char_dir or (
            Path(sprite_dir) if sprite_dir
            else _MODULE_DIR.parent / "characters" / character_id
        )
        model = find_vrm_model(char_dir)
        if not model:
            self.unsupported = True
            self.unsupported_reason = f"未找到 .vrm 模型（{char_dir}）"
            logger.warning("VRMRenderer.load('%s'): %s", character_id, self.unsupported_reason)
            self._show_placeholder(self.unsupported_reason)
            return False
        if not VIEW_INDEX.is_file():
            self.unsupported = True
            self.unsupported_reason = f"视图页缺失: {VIEW_INDEX}"
            logger.error("VRMRenderer: %s", self.unsupported_reason)
            self._show_placeholder(self.unsupported_reason)
            return False

        if not self._ensure_view():
            return False

        self._model_path = model
        logger.info("VRMRenderer.load('%s'): 模型=%s", character_id, model)

        url = QUrl.fromLocalFile(str(VIEW_INDEX))
        query = QUrlQuery()
        query.addQueryItem("model", QUrl.fromLocalFile(model).toString())
        url.setQuery(query)

        self._view.setUrl(url)
        self._view.show()
        self._view.lower()  # 与占位 label 一致：低于气泡等上层控件
        self._load_started_at = time.monotonic()
        # 视图尺寸按角色基准尺寸（192×208 × scale）定，与 Sprite/Live2D 一致；
        # 不设的话 QWebEngineView 会保持默认 640×480 的宽屏比例，取景会发扁。
        self.recalc_geometry(*self._parent_size())
        self._start_poll()
        return True

    def cleanup(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer = None
        if self._view is not None:
            try:
                self._js("window.VRM && window.VRM.reset && window.VRM.reset()")
                self._view.setUrl(QUrl("about:blank"))
                self._view.deleteLater()
            except Exception:
                logger.debug("vrm_renderer: 视图销毁异常（非致命）", exc_info=True)
            self._view = None
        self._ready = False

    # ── 动画 / 情绪 / 动作 ──

    def play_anim(self, anim: str, emotion: str = "", frame_range=None) -> None:
        self._current_anim = anim
        if emotion:
            self.set_emotion(emotion)

    def set_emotion(self, emotion: str, intensity: float = 1.0) -> None:
        self._current_emotion = emotion or "neutral"
        self._js_args("setEmotion", self._current_emotion, max(0.0, min(1.0, float(intensity))))

    def set_master_emotion(self, emotion: str) -> None:
        """主导情绪推送：VRM 直接映射到预设表情（不触发动作）。"""
        self.set_emotion(emotion or "neutral", 1.0)

    def set_va_target(self, valence: float, arousal: float,
                      hold_sec: float = 3.0) -> bool:
        """连续 VA 坐标 → VRM 表情参数（2026-09-20）。

        VRM 侧已有 ``intent_to_commands`` 处理 ``{"va": [v, a]}``，
        这里复用它，不另开一套映射。
        """
        try:
            v = max(-1.0, min(1.0, float(valence)))
            a = max(-1.0, min(1.0, float(arousal)))
        except (TypeError, ValueError):
            return False
        try:
            self.apply_action_intent({"va": [v, a]})
        except Exception:
            logger.debug("VRMRenderer: 非致命异常(已静默吞掉)", exc_info=True)
            return False
        return True

    def apply_action_intent(self, intent: dict) -> None:
        cmds = intent_to_commands(intent)
        if not cmds:
            return
        for cmd in cmds:
            fn = cmd.get("fn")
            if not isinstance(fn, str):
                continue
            self._js_args(fn, *cmd.get("args", []))
        logger.debug("VRMRenderer.apply_action_intent → %s", cmds)

    def set_procedural_smoothing(self, seconds: float) -> None:
        """VRM 的平滑由 JS 侧每帧插值完成，这里只记录（接口对齐）。"""
        self._smoothing_s = max(0.0, float(seconds or 0.0))

    def submit_motion_request(self, req) -> bool:
        """把 MotionMixer 的请求映射成 VRM 表情/参数动作。

        VRM 侧没有骨骼动画剪辑（motion 文件），所以不能真「播动作」；但至少要让
        同一套 MotionRequest 在 VRM 上也有反应，否则所有经 mixar 下发的表演在
        VRM 角色上都是静默无效的（与 Live2D 行为不一致）。

        映射：``motion_group`` 当 gesture（命中情绪名→预设表情）；``params`` 里的
        Live2D 参数名按 ``_PARAM_TO_EXPRESSION`` 转成 VRM 表情。

        Returns: True 表示至少执行了一个命令。
        """
        if req is None:
            return False
        intent = {
            "gesture": getattr(req, "motion_group", "") or "",
            "params": getattr(req, "params", None) or None,
            "intensity": getattr(req, "intensity", 1.0) or 1.0,
        }
        cmds = intent_to_commands(intent)
        if not cmds:
            return False
        for cmd in cmds:
            fn = cmd.get("fn")
            if isinstance(fn, str):
                self._js_args(fn, *cmd.get("args", []))
        logger.debug("VRMRenderer.submit_motion_request → %s", cmds)
        return True

    # ── 视线 ──

    def look_at(self, x: int, y: int) -> None:
        if not self._gaze_enabled:
            return
        w, h = self.get_size()
        nx, ny = normalize_to_view(x, y, w, h)
        self._gaze_offset_x, self._gaze_offset_y = nx, ny
        self._js_args("lookAt", nx, ny)

    def set_gaze_enabled(self, enabled: bool) -> None:
        self._gaze_enabled = bool(enabled)
        if not self._gaze_enabled:
            self._js_args("lookAt", 0.0, 0.0)

    def update_gaze(self) -> None:
        """VRM 视线在 JS 主循环里每帧更新；此方法保留仅为接口对齐。"""
        return None

    def reset_gaze(self) -> None:
        self._gaze_offset_x = self._gaze_offset_y = 0.0
        self._js_args("lookAt", 0.0, 0.0)

    def get_char_top_y(self) -> int:
        if self._view is not None:
            return self._view.y()
        return self.char_label.y()

    # ── 变换 ──

    def set_position(self, x: int, y: int) -> None:
        if self._view is not None:
            self._view.move(int(x), int(y))

    def get_size(self) -> tuple[int, int]:
        if self._view is not None:
            return (self._view.width(), self._view.height())
        return (self.char_label.width(), self.char_label.height())

    def set_scale(self, scale: float) -> None:
        self._scale = max(0.05, float(scale or 1.0))
        self.recalc_geometry(*self._parent_size())

    def get_scale(self) -> float:
        return self._scale

    def _parent_size(self) -> tuple[int, int]:
        if self._parent is not None:
            try:
                return (self._parent.width(), self._parent.height())
            except Exception:
                pass
        return (192, 208)

    def recalc_geometry(self, window_w: int, window_h: int) -> None:
        """按窗口尺寸重算视图几何。

        约定对齐 Live2DRenderer：``window_w/window_h`` 已经是**缩放后的最终尺寸**
        （pet.py 算好再传进来），所以直接用，不再乘 ``_scale``（否则双重缩放）。
        旧实现把两个参数丢掉、固定回 192×208，导致窗口变大后角色在下半段被硬切。
        取景本身在页面里做（按头胯距算），所以视图多大都能自适应。
        """
        w = max(48, int(window_w or 0) or 192)
        h = max(48, int(window_h or 0) or 208)
        if self._view is not None:
            self._view.setFixedSize(w, h)
            self._view.move(0, 0)
        self.char_label.setFixedSize(w, h)

    def set_facing(self, right: bool) -> None:
        self._facing_right = bool(right)
        self._js_args("setFacing", self._facing_right)

    def get_facing(self) -> bool:
        return self._facing_right

    def set_label_base_pos(self, pos: QPoint) -> None:
        self._base_label_pos = QPoint(pos)

    # ── 透明度（走页面 CSS，QWebEngineView 不支持子控件 windowOpacity）──

    def set_alpha(self, alpha: float) -> None:
        self._alpha = max(0.0, min(1.0, float(alpha)))
        self._js_args("setAlpha", self._alpha)

    def get_alpha(self) -> float:
        return self._alpha

    # ── 兼容接口 ──

    @property
    def label(self):
        return self._view if self._view is not None else self.char_label

    @property
    def eye_overlay(self):
        return None

    def show_eyes(self):
        return None

    def hide_eyes(self):
        return None

    @property
    def view(self):
        """底层 QWebEngineView（诊断/测试用；未装配时为 None）。"""
        return self._view

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def model_path(self) -> str | None:
        return self._model_path

    @property
    def page_error(self) -> str:
        return self._page_error
