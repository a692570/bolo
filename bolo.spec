# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['bolo.py'],
    pathex=['/Users/abhisheksharma/bolo'],
    binaries=[],
    datas=[
        ('icon_idle.png', '.'),
        ('icon_recording.png', '.'),
    ],
    hiddenimports=[
        'rumps',
        'sounddevice', 'numpy', 'requests', 'websockets',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Bolo',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Bolo',
)

app = BUNDLE(
    coll,
    name='Bolo.app',
    icon=None,
    bundle_identifier='com.abhishek.bolo',
    info_plist={
        'NSMicrophoneUsageDescription': 'Bolo needs mic access to transcribe your voice.',
        'NSAccessibilityUsageDescription': 'Bolo needs accessibility access to inject text.',
        'LSUIElement': True,  # menubar only, no Dock icon
        'CFBundleShortVersionString': '1.2.0-alpha.2',
    },
)
