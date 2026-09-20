# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — OC Desktop Pet (onedir / windowed)
#
# 构建命令 / Build:
#   pyinstaller oc_pet.spec
# 或 / or:
#   pyi-makespec --onedir --windowed --name oc_pet --icon resources/icon.ico main.py
#
# 说明 / Notes:
#   - 单目录模式 (onedir): 产物在 dist/oc_pet/，exe 与依赖同目录
#   - 隐藏控制台 (windowed): console=False，运行时不弹黑窗
#   - 图标: 仅当 resources/icon.ico 存在时启用（如不存在则忽略）
#   - 数据目录: 打包 characters/（内置角色精灵）。assets/、config/ 当前仓库不存在，
#     若你创建后需打包，取消对应 datas 行的注释即可。

import os
import re

block_cipher = None

# ── 入口脚本 / Entry point ──
entry = 'main.py'

# ── 图标（如存在）/ Icon (if present) ──
icon_path = 'resources/icon.ico'
icon = icon_path if os.path.exists(icon_path) else None

# ── 数据文件 / Data files (src, dst) ──
# onedir 模式下 dst 为相对于产物根目录 (dist/oc_pet/) 的路径
#
# ⚠️ 2026-09-20：**不打包 Live2D 模型文件**。
#
# 原写法 `datas = [('characters', 'characters')]` 会把整个目录打进去——
# 实测带出 47.9MB 模型（kurisu 2.5 + miku 34.6 + Rory 10.8）。
# 这与 README 的承诺相矛盾（“不随仓库分发任何 Live2D 模型文件”，
# 见 .gitignore 的 `characters/*/live2d/*`），尤其 Rory 是**第三方模型**。
#
# 现在按文件粒度收集：
#   ✓ 收：pet.json / manifest.json / profile.json / 角色说明等**配置**
#   ✗ 不收：.moc3 / .model3.json / .model.json / 贴图 / 物理 / 表情 等**模型素材**
#
# 用户首次启动时按 README「第 3 步」自备模型（或跑
# tools/fetch_free_live2d_sample.py）。缺模型时桌宠会提示“需下载模型”，
# 不会白屏或崩溃。

# 模型素材后缀（不打包）
#
# 注意 `.moc.json`（Cubism 2 的模型文件，kurisu 那个 93KB）与
# `.model.json`（Cubism 2 清单）必须列在 `.json` 之前——本函数用
# endswith 判断，顺序不重要，但两者都要在。
# 另：`.bak` / `.orig` / `.tmp` 是本地备份，也不该随包分发。
_MODEL_EXTS = (
    '.moc3', '.moc', '.moc.json',        # 模型本体（Cubism 3 / 2）
    '.model3.json', '.model.json',        # 模型清单
    '.physics3.json', '.physics.json',    # 物理
    '.cdi3.json', '.cdi.json',            # 参数显示信息
    '.pose3.json', '.pose.json',          # 姿势
    '.exp3.json', '.exp.json',            # 表情
    '.motion3.json', '.mtn',              # 动作
    '.png', '.jpg', '.jpeg', '.webp',     # 贴图（也含预览图）
    # 本地备份（不是可分发内容）
    '.bak', '.orig', '.tmp', '.old',
)

# 模型目录名（不整目录跳过——live2d/ 下的 profile.json 要保留，
# 它是随仓库分发的参数映射配置）
_MODEL_DIR_HINTS = ()


def _collect_character_configs(root: str = 'characters'):
    """收集 characters/ 下的**配置**文件，跳过模型素材。

    按**文件后缀**过滤（而不是整目录跳过）——因为 `live2d/` 下混着
    两种东西：
      - profile.json（参数映射配置，随仓库分发）→ 要收
      - *.moc3 / 贴图 / *.model3.json（模型素材）→ 不收

    Returns:
        list[(src, dst_rel)]，可直接喂给 PyInstaller 的 datas。
    """
    out = []
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        parts = [] if rel_dir == '.' else rel_dir.split(os.sep)
        if any(p.lower() in _MODEL_DIR_HINTS for p in parts):
            dirnames[:] = []
            continue
        # 模型子目录通常形如 "Roxy_V1.8192" / "kurisu.2048"（带尺寸后缀）
        dirnames[:] = [d for d in dirnames
                       if not re.search(r'\.\d{3,4}$', d)]
        for fn in filenames:
            low = fn.lower()
            if any(low.endswith(ext) for ext in _MODEL_EXTS):
                continue
            src = os.path.join(dirpath, fn)
            # 注意：PyInstaller 的 datas 元组是 (src_file, dst_DIR)。
            # dst 是**目标目录**，文件名由 PyInstaller 自己取——
            # 把文件名也拼进去会变成 `x.json/x.json`（多一层）。
            dst_dir = os.path.join('characters', rel_dir) if rel_dir != '.' \
                else 'characters'
            out.append((src, dst_dir))
    return out


datas = _collect_character_configs('characters')
# 若目录不存在（干净 checkout），退回一个空列表而不是报错
datas = datas or []

# ── 隐藏导入 / Hidden imports ──
# PySide6 的子模块在部分使用场景下不会被自动探测，显式声明以避免运行时
# "ImportError: cannot import name ..." 或 Qt 插件缺失。
hiddenimports = [
    'PySide6.QtCore',
    'PySide6.QtGui',
    'PySide6.QtWidgets',
    'PySide6.QtSvg',
    'PySide6.QtMultimedia',
    'PySide6.QtNetwork',
    'PySide6.QtXml',
    'pkg_resources',   # 部分依赖（如 openai 等）需要
    # 音频解码（口型能量分段 / 音视频转换）：
    # pydub 在 import 时才探测 ffmpeg，imageio_ffmpeg 带二进制且靠运行时
    # 定位——两者都容易被 PyInstaller 漏掉，显式声明。
    'pydub',
    'pydub.utils',
    'imageio_ffmpeg',
    'soundfile',
    'numpy',
]

# ── 排除模块 / Excluded modules ──
# main.py 在 `--sandbox` 分支中条件导入 sandbox_runner，但该文件在本仓库中
# 不存在（已被 .gitignore 忽略），打包时显式排除避免 missing-module 警告与误打包。
#
# ── Qt 模块裁剪（2026-09-16）──
# 背景：PySide6 全家桶装了一百多个 Qt DLL（共 628MB），而本程序只用 6 个模块：
#     QtCore / QtGui / QtWidgets / QtMultimedia / QtOpenGLWidgets / QtSvg
# 其中 Qt6WebEngineCore.dll 单文件 195MB，完全用不到。
#
# 原则：只排**明确无关**的——即代码里零 import、且不被已用模块间接依赖的。
# 已用模块的依赖链（不能排）：
#     QtWidgets → QtGui → QtCore
#     QtMultimedia → QtNetwork / ffmpeg(avcodec 等)
#     QtOpenGLWidgets → QtOpenGL / QtGui
# 保守起见不碰 QtQuick/Qml（QtMultimedia 的 QML 后端可能间接引用）。
excludes = [
    'sandbox_runner',
    # ── 浏览器引擎（195MB，零使用）──
    'PySide6.QtWebEngineCore',
    'PySide6.QtWebEngineWidgets',
    'PySide6.QtWebEngineQuick',
    'PySide6.QtWebChannel',
    'PySide6.QtWebView',
    'PySide6.QtWebSockets',
    # ── 3D / 图表 / 数据可视化（零使用）──
    'PySide6.Qt3DCore',
    'PySide6.Qt3DRender',
    'PySide6.Qt3DInput',
    'PySide6.Qt3DLogic',
    'PySide6.Qt3DAnimation',
    'PySide6.Qt3DExtras',
    'PySide6.QtCharts',
    'PySide6.QtDataVisualization',
    'PySide6.QtGraphs',
    'PySide6.QtGraphsWidgets',
    # ── 设计器 / 文档 / 打印（零使用）──
    'PySide6.QtDesigner',
    'PySide6.QtUiTools',
    'PySide6.QtPdf',
    'PySide6.QtPdfWidgets',
    'PySide6.QtHelp',
    'PySide6.QtPrintSupport',
    # ── 杂项（零使用）──
    'PySide6.QtBluetooth',
    'PySide6.QtNfc',
    'PySide6.QtPositioning',
    'PySide6.QtLocation',
    'PySide6.QtSensors',
    'PySide6.QtSerialPort',
    'PySide6.QtSerialBus',
    'PySide6.QtRemoteObjects',
    'PySide6.QtScxml',
    'PySide6.QtStateMachine',
    'PySide6.QtSql',
    'PySide6.QtTest',
    'PySide6.QtTextToSpeech',
    'PySide6.QtVirtualKeyboard',
    'PySide6.QtSpatialAudio',
    'PySide6.QtHttpServer',
    'PySide6.QtNetworkAuth',
    'PySide6.QtConcurrent',
    'PySide6.QtDBus',
    # ── 开发/测试工具（零使用）──
    'PySide6.scripts',
    'shiboken6_generator',
    # ── 被 requirements-build.txt 砍掉的可选件（保留排除防误打包）──
    'torch',
    'whisper',
    'onnxruntime',
    'transformers',
    'funasr',
    'modelscope',
    'cosyvoice',
    'matplotlib',
    'tkinter',
    'IPython',
    'jupyter',
    'pandas',
    'pytest',
    # ── 本机环境残留（2026-09-20）──
    # 这几个在开发机上装了、但**代码零 import**、也不在任何 requirements
    # 清单里。PyInstaller 的传递依赖分析会把它们拉进产物：
    #   cv2 (98MB, rapidocr 的依赖) / pyarrow (39MB, 孤儿包)
    # 实测不加排除时产物 1.03GB，加后显著缩小。
    'cv2',
    'rapidocr',
    'pyarrow',
    'torchvision',
    'sympy',
    'networkx',
]

a = Analysis(
    [entry],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ⚠️ 2026-09-20 修复：onedir 模式的 EXE **只拿 pyz + scripts**。
#
# 原写法把 `a.binaries` / `a.datas` 也传给了 EXE（那是 **onefile** 模式的写法），
# 后果：同一批文件被装两遍——
#   exe 内部一份（实测 262MB）
#   COLLECT 又复制一份到 _internal/（实测 566MB）
# 产物 828MB，其中约 260MB 是纯重复。
#
# onedir 的正确分工：EXE 只含 bootloader + PYZ；
# 所有 DLL/数据交给 COLLECT 落进 _internal/。
exe = EXE(
    pyz,
    a.scripts,
    [],              # binaries：交给 COLLECT（onedir）
    exclude_binaries=True,
    name='oc_pet',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 隐藏控制台窗口 / hide console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)

# ── 单目录收集 / onedir collect ──
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='oc_pet',
)
