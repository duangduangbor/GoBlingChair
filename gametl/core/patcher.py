# -*- coding: utf-8 -*-
"""可逆地把汉化装进游戏 —— 「汉化安装器」的核心逻辑。

和「导出完整版」的区别
----------------------
导出完整版会**复制一整份游戏**（几百 MB~几 GB），导错了重来一次也便宜。
安装器不复制游戏，它**就地覆盖**那几十个含文字的文本文件，代价是必须
先给自己留一条退路 —— 所以每次安装都会在游戏目录里留下一份原版备份:

    <游戏目录>/_汉化备份_原版/
        gametl_restore.json          ← 清单：改了哪些文件、什么时候、用的哪个包
        www/data/System.json.bak     ← 原文件的副本（**强制 .bak 后缀**）
        data.xp3.bak

两个设计细节
------------
1. **备份一律加 `.bak` 后缀**。不能沿用原扩展名 —— 否则备份目录里的
   ``System.json`` / ``data.xp3`` 会被引擎探测和资源包探测当成游戏本体
   扫到，轻则重复识别，重则把备份当成游戏去解包。
2. **备份只建一次，之后永不覆盖**。备份里存的是「第一次安装前的样子」，
   也就是真正的原版。哪怕用户装 → 还原 → 再装，原版备份依然有效。
   如果允许覆盖，第二次安装就会把「已经汉化过的文件」当成原版存下来，
   还原之后就再也回不到日文了。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from .archive import detect_archives, repack, unpack_all
from .detect import detect_engine
from .models import EngineType, Project
from .package import import_into, load_package, read_manifest
from ..extractors.base import wb_replaced

BACKUP_DIRNAME = "_汉化备份_原版"
MANIFEST_NAME = "gametl_restore.json"
RESTORE_FORMAT = "gametl-restore"
RESTORE_VERSION = 1
BAK_SUFFIX = ".bak"

#: 匹配率低于这个值就认为「包和游戏对不上」，宁可报错也不要写坏文件
MIN_MATCH_RATIO = 0.05

ProgressCb = Optional[Callable[[int, int, str], None]]
LogCb = Optional[Callable[[str], None]]


# ---------------------------------------------------------------- 工具

def _log(on_log: LogCb, msg: str) -> None:
    if on_log:
        try:
            on_log(msg)
        except Exception:  # noqa: BLE001
            pass


def _step(on_progress: ProgressCb, cur: int, total: int, desc: str) -> None:
    if on_progress:
        try:
            on_progress(cur, total, desc)
        except Exception:  # noqa: BLE001
            pass


def _walk_dirs_limited(root: Path, max_depth: int = 2,
                       max_dirs: int = 60) -> list[Path]:
    """逐层收集子目录，**总目录数封顶**。

    ``looks_like_game`` 会被「向上找游戏根目录」反复调用，用户随手选个
    ``D:\\`` 也不能卡住界面 —— 所以这里不能无限递归，也不能全量枚举。
    """
    seen = [root]
    level = [root]
    for _ in range(max_depth):
        nxt: list[Path] = []
        for d in level:
            try:
                for e in d.iterdir():
                    if e.is_dir():
                        nxt.append(e)
                    if len(seen) + len(nxt) >= max_dirs:
                        break
            except OSError:
                continue
            if len(seen) + len(nxt) >= max_dirs:
                break
        level = nxt[:max_dirs]
        seen.extend(level)
        if not level or len(seen) >= max_dirs:
            break
    return seen[:max_dirs]


def _has_pack_pair(root: Path, max_depth: int = 2) -> bool:
    """``root`` 往下 ``max_depth`` 层内，有没有**成对**的 ``.~h`` + ``.~p``。

    Costume Quest 2 把封包放在 ``Win/Packs/`` 里，只 glob 根目录会漏；
    但要往下看就得上限，否则大目录上会拖死界面。
    """
    for d in _walk_dirs_limited(root, max_depth=max_depth):
        try:
            for h in d.glob("*.~h"):
                if h.is_file() and h.with_suffix(".~p").is_file():
                    return True
        except OSError:
            continue
    return False


def looks_like_game(path: Path) -> bool:
    """这条路看着像不像一个游戏目录。"""
    p = Path(path)
    if not p.is_dir():
        return False
    if (p / "www" / "data").is_dir():                       # RPG Maker MV/MZ
        return True
    if (p / "data").is_dir() and (p / "js").is_dir():        # RPG Maker MV 变体
        return True
    if (p / "Game.ini").is_file() or (p / "game.ini").is_file():
        return True
    # Ren'Py：必须 renpy/ 和 game/ 同时存在。只看「有没有 game 子目录」
    # 会把一堆无关目录（甚至系统临时目录）误判成游戏 —— 向上找的时候
    # 那就变成随便什么路径都能「找到游戏」了。
    if (p / "renpy").is_dir() and (p / "game").is_dir():
        return True
    # KiriKiri / Ren'Py 的封包：光有一个 .xp3 说明不了什么 —— 临时目录、
    # 下载目录里经常躺着封包残片，认错了会让「向上找游戏」随便停在
    # 某个系统目录上。要求同层还有可执行文件才认。
    try:
        has_pack = any(p.glob("*.xp3")) or any(p.glob("*.rpa"))
        if has_pack and any(p.glob("*.exe")):
            return True
    except OSError:
        pass
    # Double Fine 系（Buddha/Moai）：.~h 索引 + .~p 数据成对出现，特征极强。
    # **要往下看两层** —— Costume Quest 2 把它们放在 Win/Packs/ 里，
    # 只 glob 根目录会漏，安装器就会把游戏根目录判成「不像游戏」。
    try:
        if _has_pack_pair(p, max_depth=2):
            return True
    except OSError:
        pass
    # Unity：`Xxx_Data/` 目录（同级还有可执行文件）或 UnityPlayer.dll。
    # 这条**曾经漏掉过** —— 结果安装器对着 Night in the Woods 这类 Unity
    # 游戏的根目录说「看着不太像游戏根目录」，用户只能点「是」硬闯。
    # 只认 `*_Data` 不够（随便一个目录都可能叫 foo_Data），所以要求
    # 同层同时存在 .exe 或 UnityPlayer.dll。
    try:
        if any(p.glob("UnityPlayer.dll")):
            return True
        # 只读一次目录、且封顶 4000 项：用户可能随手选了 `D:\` 这种大目录，
        # 全量 iterdir 会卡住界面。
        entries = []
        for i, e in enumerate(p.iterdir()):
            if i >= 4000:
                break
            entries.append(e)
        has_exe = any(e.is_file() and e.suffix.lower() == ".exe" for e in entries)
        if has_exe and any(e.is_dir() and e.name.endswith("_Data")
                           for e in entries):
            return True
    except OSError:
        pass
    return False


# ---------------------------------------------------------------- 游戏指纹

#: 每类特征最多记几个名字（比对够用即可，不必全存）
FP_MAX_NAMES = 12

#: 认得出引擎的关键子目录名（小写比对）
FP_INTERESTING_DIRS = ("www", "game", "renpy", "data", "js", "managed",
                       "streamingassets", "win", "packs", "resources")

#: 采集指纹时要排除的文件名 —— 汉化工具自己会被用户丢进游戏目录，
#: 要是它成了「共同特征」，两个毫不相干的游戏也会被判成「匹配」。
FP_SKIP_NAMES = {"汉化安装器.exe", "滚刀哥布林汉化椅.exe", "gametl.exe"}


def _fp_names(paths) -> list[str]:
    out: list[str] = []
    for p in sorted(paths, key=lambda q: q.name.lower()):
        if p.name in FP_SKIP_NAMES:
            continue
        out.append(p.name)
        if len(out) >= FP_MAX_NAMES:
            break
    return out


def _fp_overlap(a, b) -> bool:
    """两组名字有没有交集（**大小写不敏感** —— 换台机器/换个渠道，
    ``Win`` 写成 ``win``、``Data`` 写成 ``data`` 都不该影响判断）。"""
    return bool({str(x).lower() for x in a} & {str(x).lower() for x in b})


def game_fingerprint(path: Path) -> dict:
    """采集游戏根目录的「指纹」，用于确认「这个汉化包配的是这个游戏」。

    只记**名字**，不记内容、不记大小：exe 名、``*_Data`` 目录名、资源封包名、
    关键子目录名。这样既够用来揪出「选错文件夹 / 装到别的游戏上」，又不会
    因为同一个游戏不同渠道的版本差异（文件大小各异）而误报，也绝不泄露
    任何游戏内容。
    """
    p = Path(path)
    fp: dict = {}
    if not p.is_dir():
        return fp
    try:
        entries = list(p.iterdir())
    except OSError:
        return fp
    dirs = [d for d in entries if d.is_dir()]
    fp["dir"] = p.name
    fp["exe"] = _fp_names(x for x in entries if x.is_file() and x.suffix.lower() == ".exe")
    fp["data_dirs"] = _fp_names(d for d in dirs if d.name.endswith("_Data"))
    packs = []
    for pat in ("*.xp3", "*.rpa", "*.~h", "*.pck", "*.assets", "*.bundle"):
        try:
            packs.extend(x for x in p.glob(pat) if x.is_file())
        except OSError:
            pass
    fp["packs"] = _fp_names(packs)
    fp["dirs"] = _fp_names(
        d for d in dirs if d.name.lower() in FP_INTERESTING_DIRS)
    return {k: v for k, v in fp.items() if v}


def fingerprint_match(fp: dict, path: Path) -> tuple[str, str]:
    """拿包里记的指纹比对某个目录，返回 ``(结论, 人话说明)``。

    结论取值：``match`` / ``partial`` / ``mismatch`` / ``unknown``。

    ⚠️ 只用来**提示**，绝不阻止安装 —— 同一个游戏换个渠道下载，exe 名和
    目录名都可能不一样，不能因为名字对不上就把用户挡在门外。
    """
    if not fp:
        return "unknown", "翻译包里没记游戏特征（老版本导出的包）"
    cur = game_fingerprint(path)
    if not cur:
        return "unknown", "这个目录读不出特征"

    checks: list[tuple[str, bool]] = []
    if fp.get("exe") and cur.get("exe"):
        checks.append(("可执行文件", _fp_overlap(fp["exe"], cur["exe"])))
    if fp.get("data_dirs") and cur.get("data_dirs"):
        checks.append(("_Data 目录",
                       _fp_overlap(fp["data_dirs"], cur["data_dirs"])))
    if fp.get("packs") and cur.get("packs"):
        checks.append(("资源封包", _fp_overlap(fp["packs"], cur["packs"])))
    if fp.get("dirs") and cur.get("dirs"):
        checks.append(("目录结构", _fp_overlap(fp["dirs"], cur["dirs"])))
    if not checks:
        return "unknown", "两边没有可比对的特征"

    hit = sum(1 for _, ok in checks if ok)
    detail = "、".join(f"{n}{'✓' if ok else '✗'}" for n, ok in checks)
    if hit == len(checks):
        return "match", f"和翻译包完全吻合（{detail}）"
    if hit == 0:
        return "mismatch", f"和翻译包对不上（{detail}）"
    return "partial", f"和翻译包部分吻合（{detail}）"


def find_game_dir(start: Path, *, max_up: int = 3) -> Optional[Path]:
    """从 ``start`` 开始向上找游戏目录 —— 安装器被放进子文件夹也能认出来。"""
    try:
        cur = Path(start).resolve()
    except OSError:
        cur = Path(start)
    if cur.is_file():
        cur = cur.parent
    for _ in range(max_up + 1):
        if looks_like_game(cur):
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def make_extractor(engine: EngineType, decoded_dir: Path):
    """按引擎拿提取器。

    这里**故意不复用 ``pipeline.get_extractor``**：那个模块顶部会 import
    翻译后端（连带 requests 之类的重依赖），而安装器全程不需要模型 ——
    装上它们只会让独立的「汉化安装器.exe」白白胖几 MB。
    """
    from ..extractors.kirikiri import KiriKiriExtractor
    from ..extractors.renpy import RenPyExtractor
    from ..extractors.rpgmaker import RPGMakerExtractor

    if engine == EngineType.KIRIKIRI:
        return KiriKiriExtractor(decoded_dir)
    if engine == EngineType.RPGMAKER_MV:
        return RPGMakerExtractor(decoded_dir)
    if engine == EngineType.RENPY:
        return RenPyExtractor(decoded_dir)
    if engine == EngineType.UNITY:
        from ..extractors.unity import UnityExtractor
        return UnityExtractor(decoded_dir)
    if engine == EngineType.BUDDHA:
        from ..extractors.buddha import BuddhaExtractor
        return BuddhaExtractor(decoded_dir)
    if engine == EngineType.PLAINTEXT:
        from ..extractors.plaintext import PlainTextExtractor
        return PlainTextExtractor(decoded_dir)
    raise ValueError(f"暂不支持该引擎：{engine}")


def patch_status(game_dir: Path) -> dict:
    """这个游戏装过汉化没有。

    Returns:
        {"installed", "backup_dir", "files", "created_at", "package", "engine"}
    """
    game_dir = Path(game_dir)
    bd = game_dir / BACKUP_DIRNAME
    out = {"installed": False, "backup_dir": str(bd), "files": 0,
           "created_at": "", "package": "", "engine": ""}
    mf = bd / MANIFEST_NAME
    if not mf.is_file():
        return out
    try:
        man = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if man.get("format") != RESTORE_FORMAT:
        return out
    # 备份在、不代表汉化还装着 —— 用户可能已经点过「还原原版」。
    # 所以状态跟着清单里的 last_action 走，而不是「备份目录还在不在」。
    out.update({
        "installed": str(man.get("last_action") or "installed") == "installed",
        "files": len(man.get("files") or []),
        "created_at": str(man.get("created_at") or ""),
        "package": str(man.get("package") or ""),
        "engine": str(man.get("engine") or ""),
    })
    return out


def _set_action(backup: Path, action: str) -> None:
    """把「这次是装还是卸」记进清单（备份内容一个字节都不动）。"""
    mf = backup / MANIFEST_NAME
    try:
        man = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    man["last_action"] = action
    man["last_action_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        mf.write_text(json.dumps(man, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- 安装

def apply_patch(game_dir: Path, package: Path, *,
                on_log: LogCb = None,
                on_progress: ProgressCb = None,
                cancel_event=None) -> dict:
    """把翻译包装进游戏目录（就地覆盖 + 留原版备份）。

    Args:
        game_dir: 游戏根目录
        package: ``.gtpkg`` 翻译包
        on_log: 文字回调
        on_progress: (当前, 总数, 描述)
        cancel_event: threading.Event，置位后尽快中止

    Returns:
        {"files": 写入文件数, "filled": 填上的条目数, "by_uid", "by_memory",
         "total": 游戏内文本条数, "ratio", "backup": 备份目录, "engine",
         "repacked": 是否重打包过}
    """
    game_dir = Path(game_dir)
    package = Path(package)
    if not game_dir.is_dir():
        raise RuntimeError(f"游戏目录不存在：{game_dir}")
    if not package.is_file():
        raise RuntimeError(f"翻译包不存在：{package}")

    def ck() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("已取消")

    _log(on_log, f"读取翻译包：{package.name}")
    pman = read_manifest(package)
    _log(on_log, f"  包内 {pman.get('units_translated')} 条译文 · "
                 f"来自《{pman.get('game') or '未记录'}》"
                 f"· 引擎 {pman.get('engine') or '未知'}")
    ck()

    _step(on_progress, 0, 4, "识别游戏…")
    engine, _ev = detect_engine(game_dir)
    if engine == EngineType.UNKNOWN:
        engine, _ev = detect_engine(game_dir)
    archives = detect_archives(game_dir)
    packs = list(archives.get("xp3") or []) + list(archives.get("rpa") or [])
    need_repack = bool(packs)
    _log(on_log, f"引擎：{engine.value}"
                 + (f" · 发现 {len(packs)} 个资源包（需重打包）" if need_repack else ""))
    ck()

    with tempfile.TemporaryDirectory(prefix="gametl_patch_") as td:
        tmp = Path(td)
        decoded_dir = game_dir

        if need_repack:
            _step(on_progress, 1, 4, "解包资源…")
            _log(on_log, "先解包资源包…")
            decoded_dir = tmp / "decoded"
            ures = unpack_all(game_dir, decoded_dir)
            if not getattr(ures, "processed", 0):
                raise RuntimeError(
                    "资源包解不开，无法就地安装。请改用「导出完整版」。")
            _log(on_log, ures.summary())
            ck()

        use_engine = engine
        if use_engine == EngineType.UNKNOWN:
            use_engine, _ = detect_engine(decoded_dir)

        extractor = make_extractor(use_engine, decoded_dir)

        _step(on_progress, 2, 4, "扫描游戏文本…")
        units = extractor.extract()
        if not units:
            raise RuntimeError("没在游戏里找到可汉化的文本 —— "
                               "确认选的是游戏根目录。")
        _log(on_log, f"游戏内文本 {len(units)} 条，开始与翻译包比对…")
        ck()

        project = Project(root=decoded_dir, engine=use_engine, units=units)
        res = import_into(project, package)
        filled = res["by_uid"] + res["by_memory"]
        ratio = filled / max(1, len(units))
        _log(on_log, f"匹配结果：uid 精确 {res['by_uid']} 条 · "
                     f"原文兜底 {res['by_memory']} 条 · "
                     f"未匹配 {res['missed']} 条（{ratio * 100:.1f}%）")
        if filled == 0:
            raise RuntimeError(
                "翻译包和这个游戏对不上 —— 一条都没匹配上。\n"
                "请确认包和游戏是同一款作品。")
        if res["by_uid"] == 0 and ratio < MIN_MATCH_RATIO:
            raise RuntimeError(
                f"翻译包和这个游戏只有 {ratio * 100:.1f}% 能对上，"
                "多半不是同一款游戏，已中止以免写坏文件。")
        ck()

        # ---- 回填到临时目录，拿到「真正变了哪些文件」 ----
        _step(on_progress, 3, 4, "回填译文…")
        wb = tmp / "wb"
        stats = extractor.write_back(project, wb)
        changed = list(stats.get("changed") or [])
        _log(on_log, f"回填：{stats.get('files_written', 0)} 个文件有变化"
                     f"（{wb_replaced(stats)} 处译文）")
        ck()

        # ---- 备份原版（只建一次，之后永不覆盖） ----
        backup = game_dir / BACKUP_DIRNAME
        made_backup = False
        if (backup / MANIFEST_NAME).is_file():
            _log(on_log, f"沿用已有原版备份：{BACKUP_DIRNAME}/"
                         "（不会覆盖它，它存的才是真正的原版）")
            _set_action(backup, "installed")     # 备份不动，只更新状态
        else:
            backup.mkdir(parents=True, exist_ok=True)
            files: list[dict] = []
            if need_repack:
                for p in packs:
                    try:
                        rel = p.relative_to(game_dir).as_posix()
                    except ValueError:
                        continue
                    dst = backup / (rel + BAK_SUFFIX)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dst)
                    files.append({"rel": rel, "kind": "pack"})
            else:
                for rel in changed:
                    src = game_dir / rel
                    if not src.is_file():
                        continue
                    dst = backup / (rel + BAK_SUFFIX)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    files.append({"rel": rel, "kind": "file"})
            man = {
                "format": RESTORE_FORMAT,
                "version": RESTORE_VERSION,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "game_dir": str(game_dir),
                "engine": use_engine.value,
                "package": package.name,
                "package_units": pman.get("units_translated"),
                "files": files,
                "last_action": "installed",
                "last_action_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "note": "本目录是安装汉化前的原版文件副本。"
                        "用同一个安装器点「还原原版」即可恢复。",
            }
            try:
                (backup / MANIFEST_NAME).write_text(
                    json.dumps(man, ensure_ascii=False, indent=2),
                    encoding="utf-8")
            except OSError as e:
                raise RuntimeError(f"无法写入备份（游戏目录只读？）：{e}") from e
            made_backup = True
            _log(on_log, f"已备份原版 {len(files)} 个文件 → {BACKUP_DIRNAME}/")

        # ---- 覆盖 ----
        _step(on_progress, 4, 4, "写入游戏目录…")
        written = 0
        if need_repack:
            out = tmp / "repack"
            pstats = repack(decoded_dir, wb, out, source_game_dir=game_dir)
            packed = list(pstats.get("packed") or [])
            for name in packed:
                src = out / name
                if not src.is_file():
                    cands = [q for q in out.rglob(Path(name).name) if q.is_file()]
                    if not cands:
                        continue
                    src = cands[0]
                dst = game_dir / Path(name).name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                written += 1
            _log(on_log, f"重新打包 {written} 个资源包并覆盖")
        else:
            for rel in changed:
                src = wb / rel
                if not src.is_file():
                    continue
                dst = game_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, dst)
                    written += 1
                except OSError as e:
                    _log(on_log, f"  [警告] 写不进去：{rel}（{e}）")

    return {
        "files": written,
        "filled": filled,
        "by_uid": res["by_uid"],
        "by_memory": res["by_memory"],
        "missed": res["missed"],
        "total": len(units),
        "ratio": ratio,
        "backup": str(game_dir / BACKUP_DIRNAME),
        "made_backup": made_backup,
        "engine": use_engine.value,
        "repacked": need_repack,
    }


# ---------------------------------------------------------------- 还原

def revert_patch(game_dir: Path, *,
                 on_log: LogCb = None,
                 on_progress: ProgressCb = None,
                 remove_backup: bool = False) -> dict:
    """把游戏还原成安装汉化之前的样子。

    Args:
        remove_backup: True 则在还原成功后删掉备份目录。默认保留 ——
            保留着随时可以再装一次，也方便人工核对。

    Returns:
        {"restored", "missing", "backup_dir", "removed"}
    """
    game_dir = Path(game_dir)
    st = patch_status(game_dir)
    if not st["installed"]:
        raise RuntimeError(f"这个游戏没有安装记录：\n{game_dir}\n"
                           f"（找不到 {BACKUP_DIRNAME}/{MANIFEST_NAME}）")

    bd = Path(st["backup_dir"])
    try:
        man = json.loads((bd / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RuntimeError(f"备份清单读不出来：{e}") from e

    entries = list(man.get("files") or [])
    _step(on_progress, 0, len(entries) or 1, "还原原版文件…")
    restored = 0
    missing: list[str] = []
    for i, ent in enumerate(entries, 1):
        rel = str(ent.get("rel") or "")
        if not rel:
            continue
        src = bd / (rel + BAK_SUFFIX)
        if not src.is_file():
            missing.append(rel)
            continue
        dst = game_dir / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored += 1
        except OSError as e:
            missing.append(f"{rel}（{e}）")
        if i % 20 == 0 or i == len(entries):
            _step(on_progress, i, len(entries) or 1, f"还原 {i}/{len(entries)}")
    _log(on_log, f"已还原 {restored} 个文件"
                 + (f"，{len(missing)} 个失败" if missing else ""))

    # 备份留着（下次还能装），但状态要翻回「未安装」，
    # 否则界面会一直说「已装汉化」，用户会以为还原没生效。
    if restored and not missing:
        _set_action(bd, "reverted")

    removed = False
    if remove_backup:
        try:
            shutil.rmtree(bd, ignore_errors=True)
            removed = True
            _log(on_log, "备份目录已删除")
        except OSError:
            pass

    return {"restored": restored, "missing": missing,
            "backup_dir": str(bd), "removed": removed}
