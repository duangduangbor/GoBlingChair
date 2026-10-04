"""封包自动处理：识别并解包/重打包游戏资源包。

支持：
- KiriKiri `.xp3`（未加密/非压缩，覆盖多数同人与部分商业作品）
- Ren'Py `.rpa`（2.0/3.0）
- RPG Maker：无需处理（明文 JSON）

对加密封包会明确报错并给出提示，不静默失败。
"""
from __future__ import annotations

from pathlib import Path

from .rpa import RPAArchive, is_rpa
from .scan import ScanStats, iter_files
from .xp3 import XP3Archive, is_xp3


class UnpackResult:
    def __init__(self):
        self.processed: list[str] = []       # 成功解包/处理的包
        self.failed: list[tuple[str, str]] = []  # (文件名, 原因)
        self.total_extracted = 0

    def summary(self) -> str:
        lines = []
        if self.processed:
            lines.append(f"成功解包 {len(self.processed)} 个包，"
                         f"共 {self.total_extracted} 个文件")
        if self.failed:
            lines.append(f"{len(self.failed)} 个包未能处理：")
            for name, reason in self.failed:
                lines.append(f"  - {name}: {reason}")
        return "\n".join(lines)


def detect_archives(game_dir: Path,
                    *,
                    max_depth: int = 4,
                    max_files: int = 40000,
                    time_budget: float = 20.0,
                    stats: ScanStats | None = None) -> dict[str, list[Path]]:
    """扫描游戏目录，找出各类封包文件。

    性能要点（曾经导致界面卡死）：
    - 遍历必须**限深度/限数量/限时间**，游戏目录动辄数万文件；
    - 只按**后缀**筛选，仅对「无后缀」文件做魔数探测。
      早期实现对每个文件都调用 is_xp3()（open + 读 10 字节），
      在机械盘上对数万文件逐个 open 会让程序无响应。
    """
    game_dir = Path(game_dir)
    found: dict[str, list[Path]] = {"xp3": [], "rpa": []}

    for p in iter_files(game_dir, max_depth=max_depth, max_files=max_files,
                        time_budget=time_budget, stats=stats):
        suf = p.suffix.lower()
        if suf == ".xp3":
            found["xp3"].append(p)
        elif suf == ".rpa":
            found["rpa"].append(p)
        elif suf == "":
            # 兜底：极少数封包没有扩展名，才值得 open 一次
            try:
                if is_xp3(p):
                    found["xp3"].append(p)
                elif is_rpa(p):
                    found["rpa"].append(p)
            except OSError:
                continue

    return found


def unpack_all(game_dir: Path, out_dir: Path,
               progress=None) -> UnpackResult:
    """解包游戏目录下所有可识别的封包到 out_dir。

    目录结构保留为 out_dir/<包名>/<内部路径>，
    便于后续重打包时对应回去。
    """
    game_dir = Path(game_dir)
    out_dir = Path(out_dir)
    result = UnpackResult()
    found = detect_archives(game_dir)

    total = len(found["xp3"]) + len(found["rpa"])
    done = 0

    for path in found["xp3"]:
        done += 1
        if progress:
            progress(done, total, f"解包 {path.name}")
        try:
            arc = XP3Archive(path)
            entries = arc.read_index()
            protected = sum(1 for e in entries if e.protected)
            if protected == len(entries) and entries:
                result.failed.append((path.name, "全部条目加密（需密钥）"))
                continue
            stats = arc.extract_all(out_dir / path.stem)
            result.processed.append(path.name)
            result.total_extracted += stats["extracted"]
            if protected:
                result.failed.append(
                    (path.name, f"{protected}/{len(entries)} 个加密条目被跳过"))
        except Exception as e:  # noqa: BLE001
            result.failed.append((path.name, str(e)))

    for path in found["rpa"]:
        done += 1
        if progress:
            progress(done, total, f"解包 {path.name}")
        try:
            arc = RPAArchive(path)
            arc.read_index()
            stats = arc.extract_all(out_dir / path.stem)
            result.processed.append(path.name)
            result.total_extracted += stats["extracted"]
        except Exception as e:  # noqa: BLE001
            result.failed.append((path.name, str(e)))

    return result


def repack(original_dir: Path, translated_dir: Path, out_dir: Path,
           source_game_dir: Path | None = None) -> dict:
    """把 translated_dir 里修改过的文件，重新打包回与 original_dir 结构对应的封包。

    Args:
        original_dir: 解包产物目录（每个子目录对应一个原封包）
        translated_dir: 回填后的目录（结构同上）
        out_dir: 输出目录
        source_game_dir: 原始游戏目录（用于查找原封包的扩展名与名称）

    Returns:
        {"packed": [生成的封包名或目录名]}
    """
    from .rpa import RPAArchive
    from .xp3 import XP3Archive

    original_dir = Path(original_dir)
    translated_dir = Path(translated_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 在游戏目录里建立 {包名: 包路径} 的映射
    pack_map: dict[str, Path] = {}
    search_roots = [source_game_dir] if source_game_dir else []
    search_roots.append(original_dir.parent)
    for root in search_roots:
        if not root or not Path(root).is_dir():
            continue
        # 同样要限界：这里扫的是原始游戏目录
        for p in iter_files(Path(root), max_depth=4, max_files=40000,
                            time_budget=15.0):
            if p.suffix.lower() in (".xp3", ".rpa"):
                if p.stem not in pack_map:
                    pack_map[p.stem] = p

    packed = []
    for sub in sorted(original_dir.iterdir()):
        if not sub.is_dir():
            continue
        trans_sub = translated_dir / sub.name
        if not trans_sub.is_dir():
            continue

        # 收集该包的全部文件（优先用译文版）
        files: list[tuple[str, bytes]] = []
        for f in sorted(sub.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(sub)
            trans_f = trans_sub / rel
            src_f = trans_f if trans_f.exists() else f
            files.append((str(rel).replace("\\", "/"), src_f.read_bytes()))

        if not files:
            continue

        original_pack = pack_map.get(sub.name)

        if original_pack and original_pack.suffix.lower() == ".xp3":
            out_pack = out_dir / original_pack.name
            XP3Archive.pack(files, out_pack)
            packed.append(out_pack.name)
        elif original_pack and original_pack.suffix.lower() == ".rpa":
            out_pack = out_dir / original_pack.name
            RPAArchive.pack(files, out_pack)
            packed.append(out_pack.name)
        else:
            # 未知类型：原样复制文件树
            for rel, content in files:
                dst = out_dir / sub.name / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(content)
            packed.append(f"{sub.name}/(目录)")

    return {"packed": packed}
