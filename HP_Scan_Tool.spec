# -*- mode: python ; coding: utf-8 -*-

import os
block_cipher = None

a = Analysis(
    ['hp_scan_gui.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('fonts/NotoSansSC-Regular.ttf', 'fonts'),
    ],
    hiddenimports=[
        'requests', 'urllib3',
        'zeroconf',
        'wsdiscovery',
        'PIL', 'PIL.Image', 'PIL.ImageTk',
        'customtkinter',
        'xml.etree.ElementTree',
        'json', 'threading', 'io', 'copy', 'shutil',
        'tkinter', 'tkinter.ttk', 'tkinter.filedialog', 'tkinter.messagebox', 'tkinter.simpledialog',
        # 项目内模块（PyInstaller 通常能自动发现，显式声明保险）
        'escl_engine', 'wia_engine', 'wsd_engine',
        'exposure', 'scan_coordinator',
        'cache_manager', 'preset_manager', 'history_manager',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='HP_Scan_Tool',
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
