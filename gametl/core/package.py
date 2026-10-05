# -*- coding: utf-8 -*-
"""翻译包（``.gtpkg``）：把译文从一台机器搬到另一台。

设计要点：

1. **不含游戏文件**，只存「原文 -> 译文」。所以包很小（几 MB），
   也天然不涉及游戏本体的再分发。
2. 匹配首选 **uid**（= ``sha1(相对路径 + 位置 + 原文)``）。相对路径是
   ``www/data/Map001.json`` 这种，**不含游戏根目录名** —— 所以对方的
   文件夹叫什么都行，改名、换盘、换路径都不影响。
3. uid 不中时用「**原文全文**」兜底（翻译记忆）。对方若是另一个版本
   （地图增删、文件挪位），只要某句原文一模一样就能复用，不必重翻。
4. 包里没有任何模型依赖：对方装上工具、打开游戏、导入，即可完成
   回填与导出，全程不需要本地模型。
"""
from __future__ import annotations

import io
import json
import time
import zipfile
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

PACKAGE_FORMAT = "gametl-translation-package"
PACKAGE_VERSION = 1
PACKAGE_EXT = ".gtpkg"

TOOL_VERSION = "1.6"


class PackageError(Exception):
    """翻译包格式不对/损坏。"""


def export_package(project, dest: Path, *,
                   target_lang: str = "简体中文",
                   progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """把工程里已有的译文打包成 ``.gtpkg``。

    Args:
        project: 已加载的 Project
        dest: 输出文件路径（.gtpkg）
        progress: (已写条数, 总条数)

    Returns:
        {"path", "units", "bytes", "files"}
    """
    dest = Path(dest)
    if dest.suffix.lower() != PACKAGE_EXT:
        dest = dest.with_suffix(PACKAGE_EXT)
    dest.parent.mkdir(parents=True, exist_ok=True)

    filled = [u for u in project.units if u.translated]
    total = len(filled)

    buf = io.BytesIO()
    files = set()
    n = 0
    for u in filled:
        rec = {
            "uid": u.uid,
            "f": u.source_file,
            "o": u.original,
            "t": u.translated,
        }
        if u.extra.get("unverified"):
            rec["uv"] = 1
        buf.write(json.dumps(rec, ensure_ascii=False).encode("utf-8"))
        buf.write(b"\n")
        files.add(u.source_file)
        n += 1
        if progress and n % 2000 == 0:
            progress(n, total)
    if progress:
        progress(n, total)

    meta = project.meta or {}
    game_dir = Path(str(meta.get("game_dir") or project.root))
    # 函数内 import：patcher 反过来依赖本模块，顶层 import 会成环
    from .patcher import game_fingerprint
    manifest = {
        "format": PACKAGE_FORMAT,
        "version": PACKAGE_VERSION,
        "tool_version": TOOL_VERSION,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "engine": project.engine.value,
        "game": game_dir.name,
        # 游戏指纹：安装器据此确认「这个包配的就是这个游戏目录」。
        # 只存名字（exe / *_Data / 封包 / 目录结构），不含任何游戏内容。
        "fingerprint": game_fingerprint(game_dir),
        "target_lang": target_lang,
        "units_total": len(project.units),
        "units_translated": total,
        "unverified": len(project.unverified_uids()),
        "files": len(files),
        "note": "译文数据包，不含游戏文件。导入后由本工具重新回填，无需本地模型。",
    }

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("units.jsonl", buf.getvalue())

    return {"path": str(dest), "units": n,
            "bytes": dest.stat().st_size, "files": len(files)}


def read_manifest(path: Path) -> dict:
    """只读包的 manifest，不加载译文 —— 用来在导入前向用户交代内容。"""
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as zf:
            man = json.loads(zf.read("manifest.json").decode("utf-8"))
    except (OSError, KeyError, zipfile.BadZipFile, ValueError) as e:
        raise PackageError(f"不是有效的翻译包：{e}") from e
    if man.get("format") != PACKAGE_FORMAT:
        raise PackageError("这个文件不是本工具的翻译包。")
    return man


def load_package(path: Path, *,
                 progress: Optional[Callable[[int, int], None]] = None
                 ) -> tuple[dict, dict[str, str], dict[str, str]]:
    """读取翻译包。

    Returns:
        (manifest, uid -> 译文, 原文 -> 译文)
        第三个是**翻译记忆**兜底索引：同一个原文若有多条译文，
        取出现次数最多的那条。
    """
    path = Path(path)
    man = read_manifest(path)
    by_uid: dict[str, str] = {}
    text_index: dict[str, Counter] = {}
    n = 0
    try:
        with zipfile.ZipFile(path) as zf:
            with zf.open("units.jsonl") as fp:
                for line in io.TextIOWrapper(fp, encoding="utf-8"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    uid = rec.get("uid")
                    t = rec.get("t")
                    o = rec.get("o")
                    if not uid or t is None:
                        continue
                    by_uid[uid] = t
                    if isinstance(o, str) and o:
                        text_index.setdefault(o, Counter())[t] += 1
                    n += 1
                    if progress and n % 5000 == 0:
                        progress(n, man.get("units_translated") or n)
    except (OSError, zipfile.BadZipFile) as e:
        raise PackageError(f"读取翻译包失败：{e}") from e

    tm = {o: c.most_common(1)[0][0] for o, c in text_index.items()}
    if progress:
        progress(n, man.get("units_translated") or n)
    return man, by_uid, tm


def import_into(project, path: Path, *,
                use_memory: bool = True,
                progress: Optional[Callable[[int, int], None]] = None
                ) -> dict:
    """把翻译包里的译文灌进工程对象（不落盘，由调用方决定何时保存）。

    匹配两级：
    1. **uid 精确命中** —— 同一份游戏、同一个位置，最可靠
    2. **原文兜底**（翻译记忆）—— 跨版本、跨文件、甚至跨引擎都能用
       （``use_memory=False`` 可关掉）

    Returns:
        {"manifest", "by_uid", "by_memory", "skipped", "missed", "new_uids"}
        ``new_uids`` 是本轮真正被填上的条目 uid 列表，供调用方追加进增量日志。
    """
    man, by_uid, tm = load_package(path, progress=progress)
    hit_uid = 0
    hit_mem = 0
    skipped = 0        # 已经有译文，不必动
    missed = 0         # 两种方式都没匹配上
    new_uids: list[str] = []

    for u in project.units:
        if u.translated:
            skipped += 1
            continue
        t = by_uid.get(u.uid)
        if t is not None:
            u.translated = t
            u.extra["from_package"] = "uid"
            hit_uid += 1
            new_uids.append(u.uid)
            continue
        if use_memory:
            t = tm.get(u.original)
            if t is not None:
                u.translated = t
                u.extra["from_package"] = "memory"
                hit_mem += 1
                new_uids.append(u.uid)
                continue
        missed += 1

    return {
        "manifest": man,
        "by_uid": hit_uid,
        "by_memory": hit_mem,
        "skipped": skipped,
        "missed": missed,
        "new_uids": new_uids,
        "memory_size": len(tm),
    }


def export_file_patch(translated_dir: Path, dest: Path, game_name: str,
                      *, changed_only: Optional[list[str]] = None,
                      on_progress: Optional[Callable[[int, int, str], None]] = None
                      ) -> dict:
    """导出一个「汉化文件补丁」目录：只含**汉化后的文件**，不带游戏本体。

    适合两种场景：
    - 同一台机器上发现译文有问题，重新生成后只覆盖改动过的那几个文件
    - 对方已经有游戏，只要把补丁覆盖进去就能玩

    Args:
        changed_only: 只导出这批相对路径（增量）。None = 全部导出。
    """
    import shutil

    translated_dir = Path(translated_dir)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    if changed_only is not None:
        want = set(changed_only)
        plan = [translated_dir / r for r in sorted(want)]
        plan = [p for p in plan if p.is_file()]
        # 被删掉的旧译文文件无法从「变化清单」体现，调用方用全量重建即可
    else:
        plan = [p for p in translated_dir.rglob("*") if p.is_file()]

    n = len(plan)
    if on_progress:
        on_progress(0, n, f"准备导出 {n} 个文件")
    copied = 0
    total_bytes = 0
    for i, p in enumerate(plan, 1):
        try:
            rel = p.relative_to(translated_dir)
            dst = dest / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            copied += 1
            total_bytes += p.stat().st_size
        except (OSError, ValueError):
            continue
        if on_progress and (i % 100 == 0 or i == n):
            on_progress(i, n, f"复制 {i}/{n}")

    readme = dest / "安装说明.txt"
    readme.write_text(_patch_readme(game_name, copied, changed_only), encoding="utf-8")
    return {"files": copied, "bytes": total_bytes, "dest": str(dest)}


def _patch_readme(game_name: str, n: int, changed_only) -> str:
    scope = ("本次**只包含发生变化的文件**" if changed_only is not None
             else "本次包含**全部汉化后的文件**")
    return f"""《{game_name}》汉化补丁
================================

本补丁共 {n} 个文件，{scope}。

怎么用
------
1. 打开你的游戏文件夹（里面应有 www 目录）。
2. 把本文件夹里的内容**按原目录结构**复制进去，同名文件选择「替换」。
   例如本文件夹里的 www/data/System.json
   要覆盖到  <游戏目录>/www/data/System.json
3. 启动游戏即可。

说明
----
* 游戏本体不在补丁内，请确认你已有一份原版游戏。
* 覆盖前建议把原来的文件备份一份，想还原时替换回来即可。
* 如果游戏还有没翻译的地方，说明那部分文本位于图片里（游戏把字
  画成了图），需要用图像处理的方式替换，不属于本补丁的范围。
"""


def default_package_name(game_name: str) -> str:
    """给翻译包起个一眼能认出来的名字。"""
    safe = _safe_name(game_name)
    return f"{safe}_汉化翻译包{PACKAGE_EXT}"


def _safe_name(game_name: str) -> str:
    return "".join(c for c in str(game_name)
                   if c not in '\\/:*?"<>|').strip() or "game"


def default_bundle_name(game_name: str) -> str:
    """补丁包文件夹的名字。"""
    return f"{_safe_name(game_name)}_汉化补丁包"


INSTALLER_EXE_NAME = "汉化安装器.exe"


def export_patch_bundle(project, parent: Path, game_name: str, *,
                        installer_exe: Optional[Path] = None,
                        make_zip: bool = True,
                        on_progress: Optional[Callable[[int, int, str], None]] = None,
                        on_log: Optional[Callable[[str], None]] = None) -> dict:
    """生成一个**可以直接发给别人**的汉化补丁包。

    产物::

        <parent>/
          <游戏名>_汉化补丁包/
              汉化安装器.exe        ← 独立运行，不需要模型/联网
              <游戏名>_汉化翻译包.gtpkg
              安装说明.txt
          <游戏名>_汉化补丁包.zip     ← 直接发这个

    对方把文件夹解压到游戏目录（或随便哪儿），双击 exe，点「安装汉化」，
    装完还能点「还原原版」切回原语言。

    Args:
        installer_exe: 用来当安装器的 exe（通常是主程序自己）。
            None / 文件不存在时只生成包，并在说明里指路。

    Returns:
        {"dir", "zip", "pkg", "units", "bytes", "installer": bool}
    """
    import shutil

    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    folder = default_bundle_name(game_name)
    dest = parent / folder
    dest.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        if on_log:
            try:
                on_log(msg)
            except Exception:  # noqa: BLE001
                pass

    def step(cur: int, total: int, desc: str) -> None:
        if on_progress:
            try:
                on_progress(cur, total, desc)
            except Exception:  # noqa: BLE001
                pass

    # ---- 1. 译文数据 ----
    step(0, 3, "打包译文数据…")
    log("正在打包译文数据…")
    pkg_path = dest / default_package_name(game_name)
    pres = export_package(project, pkg_path)

    # ---- 2. 安装器 ----
    step(1, 3, "准备汉化安装器…")
    have_installer = False
    if installer_exe is not None:
        src = Path(installer_exe)
        try:
            if src.is_file():
                shutil.copy2(src, dest / INSTALLER_EXE_NAME)
                have_installer = True
                log(f"已放入安装器：{INSTALLER_EXE_NAME}"
                    f"（{src.stat().st_size / 1048576:.1f} MB）")
        except OSError as e:
            log(f"[警告] 安装器复制失败：{e}")

    # ---- 3. 说明 ----
    step(2, 3, "写安装说明…")
    readme = _bundle_readme(game_name, folder, pkg_path.name, pres, have_installer)
    (dest / "安装说明.txt").write_text(readme, encoding="utf-8")

    # ---- 4. 压成一个 zip，便于整包发出去 ----
    zip_path: Optional[Path] = None
    if make_zip:
        step(2, 3, "压缩成 zip…")
        log("正在压缩成 zip…")
        zip_path = parent / (folder + ".zip")
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                                 compresslevel=6) as zf:
                for p in sorted(dest.rglob("*")):
                    if p.is_file():
                        zf.write(p, f"{folder}/{p.relative_to(dest).as_posix()}")
        except OSError as e:
            log(f"[警告] 打 zip 失败：{e}")
            zip_path = None
        else:
            log(f"已生成：{zip_path.name}"
                f"（{zip_path.stat().st_size / 1048576:.1f} MB）")

    step(3, 3, "完成")
    return {
        "dir": str(dest),
        "zip": str(zip_path) if zip_path else "",
        "pkg": str(pkg_path),
        "units": pres["units"],
        "bytes": pres["bytes"],
        "installer": have_installer,
    }


def _bundle_readme(game_name: str, folder: str, pkg_name: str,
                   pres: dict, have_installer: bool) -> str:
    mb = pres["bytes"] / 1048576
    if have_installer:
        steps = f"""怎么用（三步）
--------------
1. 把「{folder}」整个文件夹解压到**游戏目录**里。
   游戏目录 = 能直接看到 Game.exe 或 www 文件夹的那一层。
   懒得找也没关系 —— 解压到哪儿都行，安装器会自己认。

2. 双击文件夹里的「{INSTALLER_EXE_NAME}」。
   它会自动认出游戏和翻译包，确认路径没错就点「安装汉化」。

3. 装完启动游戏。第一次安装会顺便备份原版，多等几秒是正常的。

想换回原来的语言
----------------
再双击一次「{INSTALLER_EXE_NAME}」，点「还原原版」。"""
    else:
        steps = f"""怎么用
------
1. 把「{folder}」文件夹解压到游戏目录旁边。
2. 打开汉化工具本体，选这个游戏的文件夹，
   再点「导入翻译包」，选中本文件夹里的
   {pkg_name}。工具会自动还原成中文版。

（本包内没带独立安装器，所以需要那台电脑上装有汉化工具。）"""

    return f"""《{game_name}》汉化补丁包
========================================

{steps}

说明
----
* 不需要联网、不需要安装任何翻译模型。
* 原版备份会放在游戏目录的「_汉化备份_原版」文件夹里。
  确认游戏一切正常后可以自行删除；删掉之后就不能再一键还原了。
* 如果装完发现某些地方还是原文，多半是那些文字被画成了图片，
  得改图才能解决，不属于本补丁的范围。

包内容
------
* {INSTALLER_EXE_NAME if have_installer else '（本包不含安装器）'}
* {pkg_name}
      译文 {pres['units']} 条 · {mb:.1f} MB（只有译文，不含游戏本体）
* 安装说明.txt（本文件）
"""


# ---------------------------------------------------------------- 包仓库

#: 软件自带的翻译包目录名。
#:
#: 把导出的 ``.gtpkg`` 丢进这里，主界面「一键汉化」就能直接把它作用到
#: 游戏上 —— 不需要模型、不需要联网、也不用把 exe 复制到包旁边。
#: 这是「导出的补丁包回流到软件自身」的落点。
REPO_DIRNAME = "packages"


def repo_dir(app_home) -> Path:
    """软件自带的翻译包目录（可能还不存在）。"""
    return Path(app_home) / REPO_DIRNAME


def _short_err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:160]


def _same_file(a: Path, b: Path) -> bool:
    """两个文件内容是否一致（翻译包只有几 MB，直接比摘要）。"""
    import hashlib

    def _h(p: Path) -> str:
        h = hashlib.sha1()
        with open(p, "rb") as fp:
            for chunk in iter(lambda: fp.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    try:
        return _h(a) == _h(b)
    except OSError:
        return False


def list_packages(app_home) -> list[dict]:
    """扫描软件自带的翻译包。

    两个位置都收：

    * ``<软件目录>/packages/*.gtpkg`` —— 推荐放这儿
    * 软件根目录下散落的 ``*.gtpkg`` —— 用户随手丢的也能认出来

    Returns:
        每项 ``{"path", "name", "game", "engine", "units", "units_total",
        "created_at", "size", "target_lang", "fingerprint", "error"}``，
        按创建时间倒序。

        **读不出来的包也保留在列表里**（``error`` 非空）—— 否则界面
        上它会凭空消失，用户只会觉得「我明明放进去了」。
    """
    home = Path(app_home)
    seen: set[str] = set()
    cands: list[Path] = []
    for d in (repo_dir(home), home):
        try:
            if not d.is_dir():
                continue
            for p in sorted(d.glob(f"*{PACKAGE_EXT}")):
                if not p.is_file():
                    continue
                key = str(p.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    cands.append(p)
        except OSError:
            continue

    out: list[dict] = []
    for p in cands:
        rec: dict = {
            "path": str(p), "name": p.name, "game": "", "engine": "",
            "units": 0, "units_total": 0, "created_at": "", "size": 0,
            "target_lang": "", "fingerprint": {}, "error": "",
        }
        try:
            rec["size"] = p.stat().st_size
        except OSError:
            pass
        try:
            man = read_manifest(p)
            rec.update({
                "game": str(man.get("game") or ""),
                "engine": str(man.get("engine") or ""),
                "units": int(man.get("units_translated") or 0),
                "units_total": int(man.get("units_total") or 0),
                "created_at": str(man.get("created_at") or ""),
                "target_lang": str(man.get("target_lang") or ""),
                "fingerprint": man.get("fingerprint") or {},
            })
        except PackageError as e:
            rec["error"] = str(e)
        except Exception as e:  # noqa: BLE001
            rec["error"] = _short_err(e)
        out.append(rec)

    out.sort(key=lambda r: (r["created_at"], r["name"]), reverse=True)
    return out


def add_to_repo(app_home, src) -> Path:
    """把一个 ``.gtpkg`` 收进「软件自带」的翻译包目录。

    重名**不覆盖** —— 自动加 ``-2``、``-3`` 后缀，免得手滑把好包冲掉。
    但如果目标文件与来源**内容完全相同**，就直接复用，不制造副本。
    """
    import shutil

    src = Path(src)
    if not src.is_file():
        raise PackageError(f"找不到文件：{src}")
    man = read_manifest(src)            # 先验一遍，垃圾不进仓库

    d = repo_dir(app_home)
    d.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(man.get("game") or src.stem)
    dst = d / f"{stem}_汉化翻译包{PACKAGE_EXT}"
    i = 2
    while dst.exists():
        if _same_file(dst, src):
            return dst                  # 收过一模一样的了，不再复制
        dst = d / f"{stem}_汉化翻译包-{i}{PACKAGE_EXT}"
        i += 1
    shutil.copy2(src, dst)
    return dst


#: 匹配结论的排序权重（越大越靠前）
_VERDICT_RANK = {"match": 3, "partial": 2, "unknown": 1, "mismatch": 0}

VERDICT_LABEL = {
    "match": "✔ 完全吻合",
    "partial": "△ 部分吻合",
    "unknown": "？ 无法判断",
    "mismatch": "✖ 对不上",
}


def rank_packages(pkgs: list[dict], game_dir) -> list[dict]:
    """给包列表打上「跟所选游戏配不配」的结论，并按贴合度排序。

    结论来自 :func:`patcher.fingerprint_match`，**只用于提示与排序** ——
    绝不用来阻止用户手动指定某个包。同一个游戏换个渠道下载，exe 名和
    目录名都可能不一样，替用户下结论不合适。

    Returns:
        新列表（不改原对象），每项多出 ``verdict`` 与 ``verdict_text``。
    """
    if game_dir is None:
        return [dict(r, verdict="unknown", verdict_text="还没选游戏")
                for r in pkgs]

    # 函数内 import：patcher 反过来依赖本模块，顶层 import 会成环
    from .patcher import fingerprint_match

    out: list[dict] = []
    for r in pkgs:
        rec = dict(r)
        if rec.get("error"):
            rec["verdict"] = "unknown"
            rec["verdict_text"] = "包读不出来"
        else:
            try:
                v, txt = fingerprint_match(rec.get("fingerprint") or {},
                                           Path(game_dir))
            except Exception as e:  # noqa: BLE001
                v, txt = "unknown", _short_err(e)
            rec["verdict"] = v
            rec["verdict_text"] = txt
        out.append(rec)

    # 稳定排序：同分的保持 list_packages 给的「新的在前」顺序
    out.sort(key=lambda r: _VERDICT_RANK.get(r["verdict"], 0), reverse=True)
    return out
