# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：滚刀哥布林汉化椅。

生成单文件 exe（无控制台窗口），内置 gametl 包与术语表示例。

UnityPy 相关说明（踩过坑，别动）：
- UnityPy 是 Unity 引擎支持的全部底气所在（解析 .assets 里的 TextAsset，
  并把译文写回二进制封包）。它**在 import 阶段**就会拉进
  PIL / lz4 / brotli / etcpak / texture2ddecoder / tabulate / fsspec，
  所以 PIL **不能**放进 excludes —— 之前排除了 PIL，打包版一 import
  UnityPy 就炸，Unity 游戏只能退化成「按明文目录处理」。
- resources/uncompressed.tpk 是类型库（非 .py 数据文件），必须靠
  collect_all 收集，否则解 .assets 时找不到类型定义。
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import (collect_all, collect_data_files,
                                     collect_dynamic_libs,
                                     collect_submodules)

BASE = Path(os.getcwd())
GAMETL = BASE / "gametl"
ICON = BASE / "assets" / "GoBlingChair.ico"
AVATAR = BASE / "assets" / "GoBlingChair_128.png"

# ---- UnityPy：整包收集（子模块 + 数据文件 + 二进制扩展）----
UNITY_DATAS, UNITY_BINARIES, UNITY_HIDDEN = collect_all("UnityPy")
# UnityPy 的编译扩展是顶层 .pyd，靠模块名动态 import，静态分析看不见
UNITY_HIDDEN += ["UnityPyBoost"]
# etcpak（纹理压缩）带 avx2/avx512/sse41/none 四个 CPU 变体的 .pyd，
# 按 archspec 的检测结果挑一个 —— 全都要带上，否则在别的机器上挑不到
ETCPAK_BINARIES = collect_dynamic_libs("etcpak")
# ★ archspec 的 CPU 型号库是 JSON 数据文件，不收集的话
#   `from archspec.cpu import host` 直接 FileNotFoundError，
#   进而整个 import UnityPy 失败（打包版实测踩过）
ARCHSPEC_DATAS = collect_data_files("archspec")
# 这些是 UnityPy 一 import 就要用的第三方包，显式列出来更稳
for _pkg in ("lz4", "brotli", "etcpak", "texture2ddecoder", "tabulate",
             "fsspec", "PIL", "archspec"):
    try:
        UNITY_HIDDEN += collect_submodules(_pkg)
    except Exception:  # noqa: BLE001
        pass

a = Analysis(
    [str(GAMETL / "gui.py")],
    pathex=[str(BASE)],          # 让 PyInstaller 找到 gametl 包
    binaries=list(UNITY_BINARIES) + list(ETCPAK_BINARIES),
    datas=[
        (str(GAMETL / "glossary.example.json"), "gametl"),
        (str(ICON), "assets"),   # 窗口标题栏图标
        (str(AVATAR), "assets"), # 界面内嵌头像
    ] + list(UNITY_DATAS) + list(ARCHSPEC_DATAS),
    hiddenimports=[
        "gametl.auto",
        "gametl.pipeline",
        "gametl.profiles",
        "gametl.runtime_manager",
        "gametl.core.archive",
        "gametl.core.detect",
        "gametl.core.models",
        "gametl.core.dfpf",
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
        "gametl.extractors.buddha",
        "gametl.extractors.plaintext",
        "gametl.translators.glossary",
        "gametl.translators.ollama_backend",
        "gametl.translators.quality",
    ] + UNITY_HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # 注意：**不能**排除 PIL —— UnityPy 在 import 阶段就要用它
        "numpy", "matplotlib", "scipy", "pandas",
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
