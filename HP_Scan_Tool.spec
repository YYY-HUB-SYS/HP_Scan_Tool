# -*- mode: python ; coding: utf-8 -*-
# SOP 2.2 Spec 文件规范 / 7.5.5 字体嵌入方案A

import os

a = Analysis(
    ['hp_scan_gui.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('fonts/NotoSansSC-Regular.ttf', 'fonts'),
        ('app.ico', '.'),
        ('icon_32.png', '.'),
        ('icon_64.png', '.'),
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
        # 项目内模块
        'escl_engine', 'wia_engine', 'wsd_engine',
        'scan_coordinator',
        'cache_manager', 'history_manager',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter.test',
        'unittest',
        'pydoc',
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

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
    icon='app.ico',
)
