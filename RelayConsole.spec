# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('D:/ittle soft/real_console/relay-console/relay/static', 'relay/static'), ('D:/ittle soft/real_console/relay-console/config.example.yaml', '.')]
binaries = []
hiddenimports = ['uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto', 'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan', 'uvicorn.lifespan.on', 'relay.app']
tmp_ret = collect_all('playwright')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('keyring')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['D:/ittle soft/real_console/relay-console/run_console.py'],
    pathex=['D:/ittle soft/real_console/relay-console'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name='RelayConsole',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='RelayConsole',
)
