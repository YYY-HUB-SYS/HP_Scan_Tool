# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['hp_scan_gui_pyside.py'],
    pathex=[],
    binaries=[],
    datas=[('resources', 'resources')],
    hiddenimports=['escl_engine', 'wia_engine', 'wsd_engine', 'listen_engine', 'scan_coordinator', 'cache_manager', 'history_manager', 'profile_manager', 'requests', 'urllib3', 'zeroconf', 'PIL', 'PIL.Image', 'xml.etree.ElementTree', 'wsdiscovery', 'wsdiscovery.discovery', 'wsdiscovery.scope', 'wsdiscovery.service', 'wsdiscovery.types', 'win32com', 'win32com.client', 'ifaddr', 'asyncio', 'selectors', 'logging.config'],
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
    [],
    exclude_binaries=True,
    name='HP_Scan_Tool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['resources\\icons\\app.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='HP_Scan_Tool',
)
