"""引擎识别：根据游戏目录的文件特征判断游戏引擎。

汉化的第一步。不同引擎的文本存储方式差异极大，
识别错误会导致后续提取全部跑偏。
"""
from __future__ import annotations

from pathlib import Path

from .models import EngineType
from .scan import find_by_suffix, iter_files

#: 明文文本类游戏的候选后缀（「通用明文适配器」用）
PLAINTEXT_SUFFIXES: set[str] = {
    ".txt", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml",
    ".po", ".srt", ".ass", ".lang", ".strings", ".loc", ".text",
}

#: 这些名字的文件是说明书/许可证，不是游戏内容，别当成可翻译文本
PLAINTEXT_SKIP_NAMES: set[str] = {
    "readme.txt", "readme.md", "license.txt", "licence.txt", "copying",
    "notice", "changelog.txt", "changes.txt", "version.txt", "eula.txt",
    "credits.txt", "thanks.txt", "install.txt", "uninstall.txt",
    # 运行时日志（游戏/引擎自己写的，不是剧本文本）
    "output_log.txt", "player.log", "player-prev.log", "error.log",
    # 汉化工具自己的说明书
    "使用说明.txt", "安装说明.txt",
}

#: 明文目录判定门槛：至少这么多个文本文件、总量过这么多字节才认
PLAINTEXT_MIN_FILES = 3
PLAINTEXT_MIN_BYTES = 1024


def _has_suffix(root: Path, suffixes: set[str], max_depth: int = 3) -> dict[str, Path]:
    """在目录树中查找指定后缀的文件，返回 {后缀: 首个命中路径}。"""
    found: dict[str, Path] = {}
    want = {s.lower() for s in suffixes}
    for p in iter_files(root, max_depth=max_depth):
        suf = p.suffix.lower()
        if suf in want and suf not in found:
            found[suf] = p
            if len(found) == len(want):
                break
    return found


def _safe_rel(p: Path, root: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def detect_engine(root: Path) -> tuple[EngineType, dict]:
    """识别游戏引擎，返回 (引擎类型, 证据字典)。"""
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"游戏目录不存在: {root}")

    evidence: dict = {}

    # --- KiriKiri: .xp3 封包，或 .eXe/.exe + data.xp3 ---
    xp3_files = list(root.glob("*.xp3"))
    if xp3_files:
        evidence["xp3_files"] = [p.name for p in xp3_files[:10]]
        evidence["xp3_count"] = len(xp3_files)
        # KiriKiri 启动器通常是 .eXe 或带 .cf 配置
        cf = list(root.glob("*.cf"))
        if cf:
            evidence["cf_config"] = [p.name for p in cf]
        return EngineType.KIRIKIRI, evidence

    # --- RPG Maker MV/MZ: www/data/*.json 或 data/*.json + js/rpg_core.js ---
    for candidate in (root / "www" / "data", root / "data"):
        if candidate.is_dir():
            jsons = list(candidate.glob("*.json"))
            if jsons:
                names = {p.stem for p in jsons}
                if {"System", "MapInfos"} & names or "CommonEvents" in names:
                    evidence["data_dir"] = str(candidate.relative_to(root))
                    evidence["json_files"] = sorted(names)[:20]
                    return EngineType.RPGMAKER_MV, evidence

    # --- Ren'Py: game/ 目录 + .rpy 脚本 + .rpa 归档 ---
    # 注意：限深度扫描。早期用 rglob("*.rpy") 全树递归，大目录会卡死。
    rpy = find_by_suffix(root, {".rpy", ".rpyc"}, max_depth=4)
    if rpy:
        evidence["script_files"] = [_safe_rel(p, root) for p in rpy[:10]]
        return EngineType.RENPY, evidence
    rpa = find_by_suffix(root, {".rpa"}, max_depth=4)
    if rpa:
        evidence["rpa_files"] = [p.name for p in rpa[:10]]
        return EngineType.RENPY, evidence

    # --- Unity: *_Data 目录 + Managed/Assembly-CSharp.dll，或 *.assets ---
    data_dirs = [d for d in root.iterdir() if d.is_dir() and d.name.endswith("_Data")]
    if data_dirs:
        evidence["data_dirs"] = [d.name for d in data_dirs]
        if (data_dirs[0] / "Managed").is_dir():
            evidence["managed_dir"] = True
        return EngineType.UNITY, evidence
    unity_player = list(root.glob("UnityPlayer.dll")) + list(root.glob("*/UnityPlayer.dll"))
    if unity_player:
        evidence["unity_player"] = str(unity_player[0])
        return EngineType.UNITY, evidence

    # --- Double Fine（Buddha/Moai/Remonkeyed）: Win/Packs/*.~h + *.~p ---
    # .~h 索引头部有 "dfpf" 魔数，只读 4 字节即可确认，代价极低。
    h_files = find_by_suffix(root, {".~h"}, max_depth=4)
    if h_files:
        from .dfpf import DfpfPack
        hits = [p for p in h_files[:12] if DfpfPack.looks_like(p)]
        if hits:
            evidence["dfpf_packs"] = [
                _safe_rel(p, root) for p in hits[:12]]
            evidence["dfpf_count"] = len(h_files)
            return EngineType.BUDDHA, evidence

    # --- 通用明文文本（兜底）：目录里有 exe，且散着一堆真·文本文件 ---
    plain = _looks_like_plaintext(root)
    if plain:
        evidence.update(plain)
        return EngineType.PLAINTEXT, evidence

    return EngineType.UNKNOWN, evidence


def _looks_like_plaintext(root: Path) -> dict:
    """目录里是否散着足够多的明文文本文件（判定为「无引擎特征的文本目录」）。

    门槛刻意定得保守：光有 exe 加一两个 readme 不算，必须至少有
    ``PLAINTEXT_MIN_FILES`` 个像样的文本文件、总量过 ``PLAINTEXT_MIN_BYTES``，
    免得把随便一个装了程序的目录当成游戏。
    """
    if not (any(root.glob("*.exe")) or any(root.glob("*/*.exe"))):
        return {}
    files: list[str] = []
    total = 0
    for p in iter_files(root, max_depth=4):
        if p.suffix.lower() not in PLAINTEXT_SUFFIXES:
            continue
        if p.name.lower() in PLAINTEXT_SKIP_NAMES:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size < 64:
            continue
        total += min(size, 8 * 1024 * 1024)
        files.append(_safe_rel(p, root))
        if len(files) >= 400:
            break
    if len(files) < PLAINTEXT_MIN_FILES or total < PLAINTEXT_MIN_BYTES:
        return {}
    return {"plaintext_files": len(files), "plaintext_bytes": total,
            "plaintext_sample": files[:8]}


# 各引擎的文本文件后缀，供提取器筛选
ENGINE_TEXT_SUFFIXES: dict[EngineType, set[str]] = {
    EngineType.KIRIKIRI: {".ks", ".scn", ".tjs", ".csv"},
    EngineType.RPGMAKER_MV: {".json"},
    EngineType.RENPY: {".rpy"},
    EngineType.UNITY: {".assets", ".txt", ".json", ".csv"},
    EngineType.BUDDHA: {".~h", ".~p"},
    EngineType.PLAINTEXT: PLAINTEXT_SUFFIXES,
    EngineType.UNKNOWN: set(),
}
