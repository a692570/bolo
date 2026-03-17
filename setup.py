from setuptools import setup

APP = ['bolo.py']
DATA_FILES = [
    ('', ['icon_idle.png', 'icon_recording.png']),
]
OPTIONS = {
    'argv_emulation': False,
    'no_zip': True,
    'plist': {
        'CFBundleIdentifier': 'com.abhishek.bolo',
        'CFBundleName': 'Bolo',
        'CFBundleShortVersionString': '1.2.0-alpha.1',
        'LSUIElement': True,
        'NSMicrophoneUsageDescription': 'Bolo needs mic access to transcribe your voice.',
        'NSAccessibilityUsageDescription': 'Bolo needs accessibility to inject text into apps.',
    },
    'packages': [
        'rumps', 'sounddevice', '_sounddevice_data', 'numpy',
        'requests', 'certifi', 'websockets',
    ],
    'includes': [
        'Quartz',
        'HIServices',
    ],
    'excludes': ['matplotlib', 'PyInstaller', 'scipy', 'tkinter'],
}

setup(
    app=APP,
    name='Bolo',
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
