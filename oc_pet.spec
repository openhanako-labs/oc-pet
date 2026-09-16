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

block_cipher = None

# ── 入口脚本 / Entry point ──
entry = 'main.py'

# ── 图标（如存在）/ Icon (if present) ──
icon_path = 'resources/icon.ico'
icon = icon_path if os.path.exists(icon_path) else None

# ── 数据文件 / Data files (src, dst) ──
# onedir 模式下 dst 为相对于产物根目录 (dist/oc_pet/) 的路径
datas = [
    ('characters', 'characters'),   # 内置角色精灵 / built-in character sprites
    # ('assets', 'assets'),        # 取消注释以打包 assets/（如创建）
    # ('config', 'config'),        # 取消注释以打包 config/（如创建）
]

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
excludes = [
    'sandbox_runner',
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

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
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
