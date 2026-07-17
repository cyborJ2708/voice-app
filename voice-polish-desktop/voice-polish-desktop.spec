# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for voice-polish-desktop.

Build with:
    .venv\\Scripts\\pyinstaller.exe voice-polish-desktop.spec

Single-file, windowed (no console — this is a tray app), custom icon.
Explicitly bundles sounddevice's data files (its prebuilt PortAudio DLL)
via collect_data_files rather than relying on PyInstaller's built-in hook
picking it up automatically — cheap insurance, and makes the dependency
explicit here rather than implicit in PyInstaller's hook directory.

Also explicitly bundles src/voice_polish_desktop/assets/ (the tray/app
icon) — this is a plain runtime file path read via
`Path(__file__).parent / "assets" / "icon.ico"` in tray.py, not a Python
import, so PyInstaller's static Analysis has no way to discover it on its
own. Without this entry the built exe still runs, but silently shows a
blank tray icon (QIcon just fails to load a nonexistent path — no crash,
no error, easy to miss). Confirmed missing and fixed during development.

comtypes (focus_detect.py's UI Automation calls) and sounddevice both
needed zero extra hiddenimports beyond what's here — verified directly by
running the frozen exe's actual UIA/audio code paths, not assumed; see
README's Stage 1 verification notes.

Expected output size: ~50MB in practice (PySide6 pulls in only
QtCore/QtGui/QtWidgets here, not QtWebEngine/QtQuick/etc., which is what
drives typical PySide6 builds into the 150-250MB range).

`excludes` below trims pure-Python modules we don't touch — a small win.
The bigger size cost is Qt6Qml/Quick/Pdf/Svg/VirtualKeyboard/OpenGL/Network
DLLs (several MB each), which PySide6's PyInstaller hook bundles
proactively regardless of Python-level `excludes` (those only stop
*Python import* discovery, not the hook's own binary collection).
Confirmed via `pefile` that our actual dependency chain — Qt6Widgets.dll
-> Qt6Gui.dll -> Qt6Core.dll — has NO binary import-table dependency on any
of them, so filtering them out of `a.binaries` below is safe; re-verified
with a full app smoke test after removal (this app only ever touches
QtCore/QtGui/QtWidgets/QtTest).
"""
from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("sounddevice")
datas += [("src/voice_polish_desktop/assets", "voice_polish_desktop/assets")]

excludes = [
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtVirtualKeyboard",
    "PySide6.QtNetwork",
    "PySide6.QtDBus",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.Qt3DCore",
]

# Binary-level exclusion — see docstring above for why Python-level
# `excludes` alone doesn't remove these DLLs/plugins.
_UNUSED_BINARY_PATTERNS = [
    "qt6qml", "qt6quick", "qt6pdf", "qt6svg", "qt6virtualkeyboard", "qt6opengl",
    "qt6network", "qt6multimedia", "qt63d",
    "plugins\\tls\\", "plugins\\networkinformation\\",
    "plugins\\platforminputcontexts\\qtvirtualkeyboardplugin",
]

a = Analysis(
    ["run.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

a.binaries = [
    entry for entry in a.binaries
    if not any(pattern in entry[0].lower() for pattern in _UNUSED_BINARY_PATTERNS)
]

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="voice-polish-desktop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="src/voice_polish_desktop/assets/icon.ico",
)
