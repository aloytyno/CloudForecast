# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_data_files

# Collect all data files (themes, images, etc.) from these packages
datas = []
datas += collect_data_files("customtkinter", include_py_files=False)
datas += collect_data_files("tkintermapview",  include_py_files=False)

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "customtkinter",
        "tkintermapview",
        "PIL._tkinter_finder",
        "scipy.io",
        "scipy.io.netcdf",
        "scipy._lib.messagestream",
        "astral",
        "astral.sun",
        "zoneinfo",
        "tzdata",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PilviEnnuste",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=False,      # no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
