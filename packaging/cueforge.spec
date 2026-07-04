# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


SPEC_DIR = Path(SPECPATH).parent
ROOT = SPEC_DIR.parent if SPEC_DIR.name == "packaging" else SPEC_DIR
datas = collect_data_files("ytmusicapi", includes=["locales/**/*"])
oauth_client_config = ROOT / "config" / "google_oauth_client.json"
if oauth_client_config.exists():
    datas.append((str(oauth_client_config), "config"))
icon_path = ROOT / "src" / "cueforge" / "assets" / "cueforge.ico"
if icon_path.exists():
    datas.append((str(icon_path), "cueforge/assets"))
binaries = []
hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
]


a = Analysis(
    [str(ROOT / "src" / "cueforge" / "app.py")],
    pathex=[str(ROOT / "src")],
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
    name="CueForge",
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
    icon=str(icon_path) if icon_path.exists() else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="CueForge",
)
