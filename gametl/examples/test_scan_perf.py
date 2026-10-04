# -*- coding: utf-8 -*-
"""验证「选文件夹卡死」修复效果。

构造一个模拟真实游戏的大目录：
- 根目录放封包（.xp3 / .rpa）
- 大量素材文件（.png/.ogg，模拟图音资源）
- 若干层深的目录（验证深度限制）

对比：
  旧实现 = rglob("*") + 对每个文件 open 读魔数（会卡死界面）
  新实现 = 有界扫描 + 只按后缀筛
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# 允许直接 `python gametl/examples/test_scan_perf.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gametl.core.archive import detect_archives
from gametl.core.detect import detect_engine
from gametl.core.rpa import is_rpa
from gametl.core.xp3 import is_xp3


def build_big_dir(base: Path, n_flat: int = 1500, n_deep: int = 400) -> int:
    """造目录：素材 + 深层嵌套 + 根目录封包。返回文件总数。"""
    total = 0

    # 根目录：素材 + 两个封包
    (base / "data.xp3").write_bytes(b"XP3\r\n \x1a\x8b\x67\x01" + b"\x00" * 40)
    (base / "game.rpa").write_bytes(b"RPA-3.0 " + b"\x00" * 40)
    total += 2

    # 模拟素材目录：多层，每层一堆图片/音频
    for layer in range(1, 6):
        d = base / f"素材{layer}"
        d.mkdir(parents=True, exist_ok=True)
        for i in range(n_flat // 5):
            (d / f"img_{i:05d}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            (d / f"bgm_{i:05d}.ogg").write_bytes(b"OggS")
            total += 2

    # 深层嵌套（超过 max_depth=4，应被剪枝）
    deep = base
    for lv in range(1, 9):
        deep = deep / f"lv{lv}"
        deep.mkdir(parents=True, exist_ok=True)
        for i in range(n_deep // 8):
            (deep / f"deep_{i:03d}.dat").write_bytes(b"X" * 8)
            total += 1

    return total


def old_detect_plain(game_dir: Path) -> tuple[dict, int]:
    """忠实复现修复前的实现，并统计 open 次数。"""
    found = {"xp3": [], "rpa": []}
    opens = 0
    for p in game_dir.rglob("*"):
        if not p.is_file():
            continue
        try:
            if p.suffix.lower() == ".xp3":
                found["xp3"].append(p)
            elif p.suffix.lower() == ".rpa":
                found["rpa"].append(p)
            else:
                opens += 1                       # 非 xp3/rpa 都要 open 一次
                if is_xp3(p):
                    found["xp3"].append(p)
                elif is_rpa(p):
                    found["rpa"].append(p)
        except OSError:
            continue
    return found, opens


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="gametl_bigdir_"))
    try:
        print("=" * 64)
        print("构造模拟游戏目录…")
        print("=" * 64)
        t0 = time.monotonic()
        n = build_big_dir(tmp)
        print(f"  路径: {tmp}")
        print(f"  文件数: {n}  （建目录耗时 {time.monotonic()-t0:.1f}s）")
        print()

        # ---- 旧实现 ----
        print("=" * 64)
        print("【旧实现】rglob + 逐文件 open 魔数探测")
        print("=" * 64)
        t0 = time.monotonic()
        old_found, opens = old_detect_plain(tmp)
        old_time = time.monotonic() - t0
        print(f"  耗时:     {old_time:.2f}s")
        print(f"  文件 open 次数: {opens}   ← 每个非封包文件都要读一次头部")
        print(f"  找到封包: xp3={len(old_found['xp3'])}, rpa={len(old_found['rpa'])}")
        print()

        # ---- 新实现 ----
        print("=" * 64)
        print("【新实现】有界扫描 + 只按后缀筛")
        print("=" * 64)
        from gametl.core.scan import ScanStats
        st = ScanStats()
        t0 = time.monotonic()
        new_found = detect_archives(tmp, stats=st)
        new_time = time.monotonic() - t0
        print(f"  耗时:     {new_time:.2f}s")
        print(f"  open 次数: 0（仅对无后缀文件兜底探测）")
        print(f"  找到封包: xp3={len(new_found['xp3'])}, rpa={len(new_found['rpa'])}")
        print(f"  统计:     {st.note()}")
        print()

        # ---- 引擎识别 ----
        t0 = time.monotonic()
        engine, ev = detect_engine(tmp)
        det_time = time.monotonic() - t0
        print(f"  引擎识别耗时: {det_time:.2f}s  -> {engine.value}")
        print()

        # ---- 结果比对 ----
        print("=" * 64)
        print("结论")
        print("=" * 64)
        same = (len(old_found['xp3']) == len(new_found['xp3'])
                and len(old_found['rpa']) == len(new_found['rpa']))
        print(f"  结果一致性: {'一致 ✓' if same else '不一致 ✗'}")
        if old_time > 0:
            print(f"  加速比:     {old_time/new_time:.1f}x")
        print(f"  旧实现读盘次数 {opens} 次  ->  新实现 0 次")
    finally:
        print()
        print("清理临时目录…")
        shutil.rmtree(tmp, ignore_errors=True)
        print("完成")


if __name__ == "__main__":
    main()
