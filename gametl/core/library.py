# -*- coding: utf-8 -*-
"""游戏库：指一个盘 / 文件夹，软件自己把里面的游戏列成一张表。

为什么要有这个模块
------------------
给「傻瓜式」主界面用。普通用户不该知道引擎、模型、翻译包、术语表这些
内部概念，他只需要回答一个问题：**我游戏装在哪儿**。剩下的：

    扫 →  一张表（游戏 / 状态 / 类型 / 位置）  →  选中一个 → 一键

全部由本模块 + 界面完成。

这里只干「找游戏」和「读状态」两件事，不碰翻译、不碰模型，也不依赖
``gametl.gui``（否则会和界面互相 import）。工程进度这类信息由界面
通过 ``project_state`` 回调/参数喂进来。
"""
from __future__ import annotations

import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .detect import detect_engine
from .models import EngineType
from .scan import DEFAULT_SKIP_DIRS, is_tool_dir

# 引擎的人话名（和界面共用同一套措辞，避免两处各写一份）
ENGINE_LABEL: dict[str, str] = {
    "kirikiri": "KiriKiri（吉里吉里）",
    "rpgmaker_mv": "RPG Maker MV / MZ",
    "renpy": "Ren'Py",
    "unity": "Unity",
    "buddha": "Double Fine（Buddha / Moai）",
    "plaintext": "通用明文文本",
    "unknown": "认不出引擎",
}

# 扫描时的上限（沿用全项目的性能红线：任何遍历游戏目录的代码都必须限界）
DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_DIRS = 6000
DEFAULT_TIME_BUDGET = 25.0
DEFAULT_LIMIT = 300

# 从根目录起最多往下找几层。Steam 的游戏在
# ``<库>\steamapps\common\<游戏名>`` = 深度 3，正好覆盖。
SCAN_MAX_DEPTH = 3

#: 明显不可能是游戏、又特别能拖慢扫描的目录
_EXTRA_SKIP = {
    "steamapps", "downloading", "temp", "shadercache", "workshop",
    "windows", "program files", "program files (x86)", "programdata",
    "users", "system32", "$recycle.bin", "recovery", "perflogs",
    "appdata", "node_modules", "vendor", "obj", "bin", "build",
}

#: 已知的「游戏库根」名字：在这些目录下一层通常就是游戏本体
_COMMON_ROOT_NAMES = ("steamapps/common", "steamapps", "games", "game",
                      "游戏", "单机游戏", "steamlibrary", "gog games",
                      "epic games")

ProgressCb = Optional[Callable[[int, int, str], None]]


# ---------------------------------------------------------------- 常见游戏位置

def _drives() -> list[Path]:
    """当前存在的盘符根目录。"""
    out: list[Path] = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        p = Path(f"{letter}:/")
        try:
            if p.exists():
                out.append(p)
        except OSError:
            continue
    return out


def _steam_from_registry() -> list[Path]:
    """从注册表读 Steam 安装位置（读不到就返回空，不报错）。

    ⚠️ 只用 ``winreg``，**不要**去 shell out ``reg.exe``（本机安全策略拦它）。
    """
    out: list[Path] = []
    try:
        import winreg  # type: ignore
    except ImportError:
        return out
    keys = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam",
         "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    )
    for hive, sub, name in keys:
        try:
            with winreg.OpenKey(hive, sub) as k:
                val, _ = winreg.QueryValueEx(k, name)
            if val:
                out.append(Path(str(val)))
        except OSError:
            continue
    return out


def _steam_libraries(steam_root: Path) -> list[Path]:
    """解析 ``steamapps/libraryfolders.vdf``，拿到所有库。

    vdf 是 Valve 的私有格式，但结构极简：库的路径都在 ``"path"  "..."``
    这一行。不做完整解析，按行正则提取即可 —— 要的只是路径字符串。
    """
    out: list[Path] = []
    vdf = Path(steam_root) / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    for m in re.finditer(r'"path"\s+"([^"]+)"', text):
        raw = m.group(1).replace("\\\\", "\\")
        out.append(Path(raw))
    return out


def _steam_roots() -> list[Path]:
    roots = list(_steam_from_registry())
    for base in (Path("C:/Program Files (x86)/Steam"),
                 Path("C:/Program Files/Steam")):
        roots.append(base)
    for d in _drives():
        roots.append(d / "Steam")
        roots.append(d / "SteamLibrary")
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        try:
            key = str(r.resolve()).lower()
        except OSError:
            key = str(r).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def common_roots() -> list[Path]:
    """探测本机「最可能装着游戏」的根目录，**只返回真实存在的**。

    顺序 = 优先级：Steam 的 common 目录在前，然后各盘的通用游戏目录。
    界面拿它做「打开软件就自动扫一遍」的默认目标。
    """
    cands: list[Path] = []
    for sr in _steam_roots():
        if not sr.exists():
            continue
        for lib in _steam_libraries(sr) or [sr]:
            cands.append(lib / "steamapps" / "common")
        cands.append(sr / "steamapps" / "common")
    for d in _drives():
        for name in ("Games", "Game", "游戏", "单机游戏", "SteamLibrary"
                                                           "/steamapps/common",
                     "steamapps/common", "GOG Games", "Epic Games"):
            cands.append(d / name)

    out: list[Path] = []
    seen: set[str] = set()
    for c in cands:
        try:
            if not c.is_dir():
                continue
            key = str(c.resolve()).lower()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def game_like_roots(roots: Iterable[Path]) -> list[Path]:
    """把候选根目录过滤成「真的像装着游戏」的那些（给界面做快捷按钮）。"""
    out: list[Path] = []
    for r in roots:
        try:
            if not Path(r).is_dir():
                continue
        except OSError:
            continue
        out.append(Path(r))
    return out


# ---------------------------------------------------------------- 扫描

def looks_like_library_root(path: Path) -> bool:
    """这个目录本身像不像「一堆游戏躺在里面」的库根。

    用于扫描时的加速：库根（如 ``steamapps/common``）下面才值得继续挖，
    普通目录（如 ``C:\\Windows``）直接放弃。
    """
    p = Path(path)
    if p.name.lower() in _COMMON_ROOT_NAMES:
        return True
    if (p / "steamapps" / "common").is_dir():
        return True
    return False


def iter_candidate_dirs(root: Path, *,
                        max_depth: int = SCAN_MAX_DEPTH,
                        max_dirs: int = DEFAULT_MAX_DIRS,
                        time_budget: float = DEFAULT_TIME_BUDGET,
                        cancel_event=None,
                        on_progress: ProgressCb = None) -> Iterable[Path]:
    """广度优先产出「像游戏」的目录。

    三条硬约束（全部为了不卡界面）：目录数封顶、耗时封顶、可取消。
    命中一个游戏后**不再往它内部挖** —— 游戏目录里不会有第二个游戏。
    """
    from .patcher import looks_like_game      # 函数内 import：patcher 依赖本包

    root = Path(root)
    try:
        if not root.is_dir():
            return
    except OSError:
        return

    t0 = time.monotonic()
    seen = 0
    queue: deque[tuple[Path, int]] = deque([(root, 0)])

    while queue:
        if cancel_event is not None and cancel_event.is_set():
            return
        if seen >= max_dirs or time.monotonic() - t0 > time_budget:
            return
        d, depth = queue.popleft()
        seen += 1
        if on_progress is not None and seen % 10 == 0:
            try:
                on_progress(seen, 0, str(d))
            except Exception:          # noqa: BLE001
                pass

        try:
            if looks_like_game(d):
                yield d
                continue               # 是游戏就别往里挖了
        except OSError:
            pass

        if depth >= max_depth:
            continue
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if not e.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    name = e.name
                    low = name.lower()
                    if (low in DEFAULT_SKIP_DIRS or is_tool_dir(name)
                            or low in _EXTRA_SKIP
                            or name.startswith("$") or name.startswith(".")):
                        continue
                    queue.append((Path(e.path), depth + 1))
        except (PermissionError, OSError):
            continue


def scan_games(root: Path, *,
               limit: int = DEFAULT_LIMIT,
               max_depth: int = SCAN_MAX_DEPTH,
               max_dirs: int = DEFAULT_MAX_DIRS,
               time_budget: float = DEFAULT_TIME_BUDGET,
               cancel_event=None,
               on_progress: ProgressCb = None) -> list[Path]:
    """扫 ``root`` 找到的游戏目录（最多 ``limit`` 个）。

    ``root`` 自己就是一个游戏目录时，直接返回它 —— 用户把某个游戏文件夹
    拖进来也该能用。
    """
    from .patcher import looks_like_game

    root = Path(root)
    out: list[Path] = []
    try:
        if looks_like_game(root):
            return [root]
    except OSError:
        pass

    for d in iter_candidate_dirs(root, max_depth=max_depth, max_dirs=max_dirs,
                                 time_budget=time_budget,
                                 cancel_event=cancel_event,
                                 on_progress=on_progress):
        out.append(d)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 单个游戏状态

# 状态标签（界面直接显示这几个字，所以措辞要面向普通用户）
ST_PATCHED = "已汉化"
ST_READY = "可一键汉化"
ST_PROGRESS = "翻译中"
ST_TRANSLATED = "译文已备好"
ST_NEW = "未翻译"
ST_UNKNOWN = "认不出引擎"

#: 状态对应的语义（界面据此上色）
KIND_OK = "ok"          # 已完成 / 可一键
KIND_INFO = "info"      # 正常待办
KIND_WARN = "warn"      # 需要人工看看
KIND_MUTED = "muted"    # 说不清


@dataclass
class GameEntry:
    """表格里的一行 = 一个游戏。"""

    path: str
    name: str
    engine: str = "unknown"
    engine_label: str = ""
    status: str = ST_NEW
    kind: str = KIND_INFO
    note: str = ""
    # 汉化安装状态
    patched: bool = False
    patch_files: int = 0
    patch_package: str = ""
    patch_at: str = ""
    # 最贴合的翻译包（软件自带仓库里的）
    pkg_name: str = ""
    pkg_path: str = ""
    pkg_units: int = 0
    pkg_verdict: str = ""
    # 工程进度
    units: int = 0
    done: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def ratio(self) -> float:
        return self.done / self.units if self.units else 0.0


#: 翻译包贴合度排序权重
_VERDICT_RANK = {"match": 3, "partial": 2, "unknown": 1, "mismatch": 0}


def pick_best_package(game_dir: Path, pkgs: list[dict],
                      cur_fp: dict | None = None) -> Optional[dict]:
    """从软件自带的翻译包里挑一个最配这个游戏的（含 verdict 字段）。

    结论只用于**排序与提示**，绝不阻止用户手动指定别的包 —— 同一个游戏
    换个渠道下载，exe 名就可能不一样。
    """
    from .patcher import fingerprint_match, game_fingerprint

    if not pkgs:
        return None
    if cur_fp is None:
        try:
            cur_fp = game_fingerprint(game_dir)
        except OSError:
            cur_fp = {}
    best: Optional[dict] = None
    best_rank = -1
    for r in pkgs:
        if r.get("error"):
            continue
        try:
            v, txt = fingerprint_match(r.get("fingerprint") or {}, game_dir,
                                       cur_fp=cur_fp)
        except TypeError:              # 老版本 patcher 没有 cur_fp 参数
            try:
                v, txt = fingerprint_match(r.get("fingerprint") or {}, game_dir)
            except Exception:          # noqa: BLE001
                continue
        except Exception:              # noqa: BLE001
            continue
        rank = _VERDICT_RANK.get(v, 0)
        if rank > best_rank:
            best_rank = rank
            best = dict(r, verdict=v, verdict_text=txt)
    return best


def describe_game(path, *, pkgs: list[dict] | None = None,
                  project_state: dict | None = None,
                  cur_fp: dict | None = None) -> GameEntry:
    """读一个游戏目录，产出一行表格数据。

    Args:
        path: 游戏根目录
        pkgs: ``core.package.list_packages()`` 的结果（软件自带的翻译包）
        project_state: 该游戏的工程进度，字段见
            ``gui.App._read_project_state``（``exists / units / filled /
            journal``）；由界面喂进来，避免本模块反向依赖界面。
    """
    from .patcher import patch_status

    p = Path(path)
    ent = GameEntry(path=str(p), name=p.name)

    try:
        engine, _ev = detect_engine(p)
    except Exception:                  # noqa: BLE001
        engine = EngineType.UNKNOWN
    ent.engine = engine.value
    ent.engine_label = ENGINE_LABEL.get(engine.value, engine.value)

    # 装过汉化没有
    try:
        st = patch_status(p)
    except Exception:                  # noqa: BLE001
        st = {}
    if st:
        ent.patched = bool(st.get("installed"))
        ent.patch_files = int(st.get("files") or 0)
        ent.patch_package = str(st.get("package") or "")
        ent.patch_at = str(st.get("created_at") or "")

    # 工程进度
    ps = project_state or {}
    if ps:
        try:
            ent.units = int(ps.get("units") or 0)
        except (TypeError, ValueError):
            ent.units = 0
        try:
            ent.done = int(ps.get("filled") or 0) + int(ps.get("journal") or 0)
        except (TypeError, ValueError):
            ent.done = 0

    # 最配的翻译包
    best = pick_best_package(p, pkgs or [], cur_fp=cur_fp)
    if best is not None:
        ent.pkg_name = str(best.get("name") or "")
        ent.pkg_path = str(best.get("path") or "")
        ent.pkg_units = int(best.get("units") or 0)
        ent.pkg_verdict = str(best.get("verdict") or "")

    _decide_status(ent)
    return ent


def _decide_status(ent: GameEntry) -> None:
    """按优先级定「这一行该显示什么状态」，并写好人话补充。"""
    # 1) 已经装过汉化 —— 最高优先，用户最需要知道
    if ent.patched:
        ent.status = ST_PATCHED
        ent.kind = KIND_OK
        bits = []
        if ent.patch_files:
            bits.append(f"改了 {ent.patch_files} 个文件")
        if ent.patch_package:
            bits.append(f"用的「{ent.patch_package}」")
        if ent.patch_at:
            bits.append(ent.patch_at)
        ent.note = " · ".join(bits)
        return

    # 2) 软件里有配得上的翻译包 → 不用跑模型，几秒装好
    if ent.pkg_path and ent.pkg_verdict in ("match", "partial"):
        ent.status = ST_READY
        ent.kind = KIND_OK
        ent.note = (f"用「{ent.pkg_name}」直接装（{ent.pkg_units} 条译文，"
                    "不用等翻译）")
        return

    # 3) 已经有译文/工程
    if ent.units and ent.done >= ent.units:
        ent.status = ST_TRANSLATED
        ent.kind = KIND_OK
        ent.note = f"已有 {ent.units} 条译文，可直接打包"
        return
    if ent.units and ent.done:
        ent.status = ST_PROGRESS
        ent.kind = KIND_INFO
        ent.note = f"{ent.done}/{ent.units} 条（接着翻剩下的）"
        return

    # 4) 引擎认不出来：多半不是游戏，或暂不支持
    if ent.engine == "unknown":
        ent.status = ST_UNKNOWN
        ent.kind = KIND_MUTED
        ent.note = "可能不是游戏目录，或暂不支持这种引擎"
        return

    # 5) 老老实实没翻过
    ent.status = ST_NEW
    ent.kind = KIND_INFO
    ent.note = "还没翻译过，需要跑一遍翻译"


def summarize(entries: list[GameEntry]) -> dict:
    """一句话统计，用来在表格上方告诉用户「扫到了什么」。"""
    total = len(entries)
    return {
        "total": total,
        "patched": sum(1 for e in entries if e.patched),
        "ready": sum(1 for e in entries
                     if not e.patched and e.status == ST_READY),
        "progress": sum(1 for e in entries
                        if e.status in (ST_PROGRESS, ST_TRANSLATED)),
        "new": sum(1 for e in entries if e.status == ST_NEW),
        "unknown": sum(1 for e in entries if e.status == ST_UNKNOWN),
    }


def summary_text(s: dict) -> str:
    """把 :func:`summarize` 的统计翻成给用户看的一句话。"""
    if not s.get("total"):
        return "没扫到游戏。换个装游戏的盘 / 文件夹试试。"
    parts = [f"扫到 {s['total']} 个游戏"]
    if s.get("patched"):
        parts.append(f"{s['patched']} 个已汉化")
    if s.get("ready"):
        parts.append(f"{s['ready']} 个可一键汉化")
    if s.get("progress"):
        parts.append(f"{s['progress']} 个有译文")
    if s.get("new"):
        parts.append(f"{s['new']} 个待翻译")
    if s.get("unknown"):
        parts.append(f"{s['unknown']} 个认不出")
    return " · ".join(parts)
