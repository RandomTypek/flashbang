# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Flashbang.

Builds two one-file executables:

    Flashbang.exe        windowed GUI (no console)
    flashbang-cli.exe    console CLI

Build with:  pyinstaller --clean --noconfirm flashbang.spec
Output lands in dist/.
"""

import os

block_cipher = None
ICON = "flashbang.ico" if os.path.exists("flashbang.ico") else None

# Flashbang is stdlib-only. Excluding the heavy scientific stack keeps the
# binary small and stops PyInstaller pulling in whatever happens to be
# installed on the build machine.
EXCLUDES = [
    "numpy", "pandas", "matplotlib", "scipy", "PIL", "pytest",
    "setuptools", "pip", "test", "unittest", "pydoc_data",
]

gui_a = Analysis(
    ["flashbang_gui.py"],
    pathex=[],
    binaries=[],
    datas=[("flashbang.ico", ".")] if ICON else [],
    hiddenimports=["flashbang"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
gui_pyz = PYZ(gui_a.pure, gui_a.zipped_data, cipher=block_cipher)
gui_exe = EXE(
    gui_pyz,
    gui_a.scripts,
    gui_a.binaries,
    gui_a.zipfiles,
    gui_a.datas,
    [],
    name="Flashbang",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                 # windowed: no black console box
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
    version="version_info.txt" if os.path.exists("version_info.txt") else None,
)

cli_a = Analysis(
    ["flashbang.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES + ["tkinter"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
cli_pyz = PYZ(cli_a.pure, cli_a.zipped_data, cipher=block_cipher)
cli_exe = EXE(
    cli_pyz,
    cli_a.scripts,
    cli_a.binaries,
    cli_a.zipfiles,
    cli_a.datas,
    [],
    name="flashbang-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
    version="version_info.txt" if os.path.exists("version_info.txt") else None,
)
