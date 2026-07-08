# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for OC Desktop Pet — standalone Windows exe"""

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent

a = Analysis(
    ['main.py'],
    pathex=[str(PROJECT_DIR)],
    binaries=[],
    datas=[
        ('characters', 'characters'),
        ('skills', 'skills'),
        ('config.json', '.'),
    ],
    hiddenimports=[
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        'requests',
        'PIL',
        'json',
        'math',
        'random',
        'time',
        'pathlib',
        'config',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='oc-desktop-pet',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)