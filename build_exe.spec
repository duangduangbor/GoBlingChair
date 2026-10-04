# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：滚刀哥布林汉化椅。

生成单文件 exe（无控制台窗口），内置 gametl 包与术语表示例。
"""
import os
from pathlib import Path

BASE = Path(os.getcwd())
GAMETL = BASE / "gametl"
ICON = BASE / "assets" / "GoBlingChair.ico"
AVATAR = BASE / "assets" / "GoBlingChair_128.png"

a = Analysis(
    [str(GAMETL / "gui.py")],
    pathex=[str(BASE)],          # 让 PyInstaller 找到 gametl 包
    binaries=[],
    datas=[
        (str(GAMETL / "glossary.example.json"), "gametl"),
        (str(ICON), "assets"),   # 窗口标题栏图标
        (str(AVATAR), "assets"), # 界面内嵌头像
    ],
    hiddenimports=[
        "gametl.auto",
        "gametl.pipeline",
        "gametl.profiles",
        "gametl.runtime_manager",
        "gametl.core.archive",
        "gametl.core.detect",
        "gametl.core.models",
        "gametl.core.package",
        "gametl.core.patcher",
        "gametl.core.protect",
        "gametl.core.scan",
        "gametl.core.validate",
        "gametl.ui_common",
        "gametl.installer",
        "gametl.core.xp3",
        "gametl.core.rpa",
        "gametl.extractors.base",
        "gametl.extractors.kirikiri",
        "gametl.extractors.renpy",
        "gametl.extractors.rpgmaker",
        "gametl.extractors.unity",
        "gametl.translators.glossary",
        "gametl.translators.ollama_backend",
        "gametl.translators.quality",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "numpy", "PIL", "matplotlib", "scipy", "pandas",
        "PyQt5", "PySide2", "IPython", "unittest", "pydoc",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="滚刀哥布林汉化椅",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 无控制台窗口（GUI 程序）
    disable_windowed_traceback=False,
    icon=str(ICON),         # GoBlingChair LOGO
)
