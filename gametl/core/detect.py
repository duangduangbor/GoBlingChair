"""引擎识别：根据游戏目录的文件特征判断游戏引擎。

汉化的第一步。不同引擎的文本存储方式差异极大，
识别错误会导致后续提取全部跑偏。
"""
from __future__ import annotations

from pathlib import Path

from .models import EngineType
from .scan import find_by_suffix, iter_files


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

    return EngineType.UNKNOWN, evidence


# 各引擎的文本文件后缀，供提取器筛选
ENGINE_TEXT_SUFFIXES: dict[EngineType, set[str]] = {
    EngineType.KIRIKIRI: {".ks", ".scn", ".tjs", ".csv"},
    EngineType.RPGMAKER_MV: {".json"},
    EngineType.RENPY: {".rpy"},
    EngineType.UNITY: {".assets", ".txt", ".json", ".csv"},
    EngineType.UNKNOWN: set(),
}
