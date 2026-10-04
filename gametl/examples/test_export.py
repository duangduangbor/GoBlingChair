# -*- coding: utf-8 -*-
"""导出完整汉化版：功能测试。

用法：
    env -u PYTHONPATH PYTHONIOENCODING=utf-8 python -u gametl/examples/test_export.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from gametl.auto import export_full_game  # noqa: E402

FAIL: list = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  [{'OK' if ok else 'FAIL'}] {label}: {got!r}")
    if not ok:
        FAIL.append(label)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gametl_ex_"))
    game = tmp / "Game"
    (game / "www" / "data").mkdir(parents=True)
    (game / "Game.exe").write_text("exe", encoding="utf-8")
    (game / "www" / "data" / "Map001.json").write_text('{"a":"原文"}',
                                                      encoding="utf-8")
    (game / "www" / "data" / "Other.json").write_text('{"b":"keep"}',
                                                     encoding="utf-8")
    (game / "_汉化输出").mkdir()
    (game / "_汉化输出" / "junk.txt").write_text("x", encoding="utf-8")

    work = tmp / "work"
    tr = work / "_work" / "translated"
    (tr / "www" / "data").mkdir(parents=True)
    (tr / "www" / "data" / "Map001.json").write_text('{"a":"译文"}',
                                                    encoding="utf-8")

    print("-- 正常导出 --")
    dest = tmp / "out"
    res = export_full_game(game, work, dest)
    check("Game.exe 已复制", (dest / "Game.exe").exists(), True)
    check("未被汉化的文件保留", (dest / "www" / "data" / "Other.json").exists(), True)
    check("汉化文件被覆盖",
          (dest / "www" / "data" / "Map001.json").read_text(encoding="utf-8"),
          '{"a":"译文"}')
    check("中间产物不导出", (dest / "_汉化输出").exists(), False)
    check("复制文件数", res["files"], 3)
    check("覆盖文件数", res["overwritten"], 1)
    check("原游戏未被改动",
          (game / "www" / "data" / "Map001.json").read_text(encoding="utf-8"),
          '{"a":"原文"}')

    print("-- 目标在游戏目录内部：必须拒绝 --")
    try:
        export_full_game(game, work, game / "sub")
        check("抛异常", "没抛", "RuntimeError")
    except RuntimeError:
        check("已拒绝", True, True)
    except Exception as e:  # noqa: BLE001
        check("异常类型", type(e).__name__, "RuntimeError")

    print("-- 目标就是游戏目录本身：必须拒绝 --")
    try:
        export_full_game(game, work, game)
        check("抛异常", "没抛", "RuntimeError")
    except RuntimeError:
        check("已拒绝", True, True)

    print("-- 找不到回填产物时：仍能导出原始游戏 --")
    dest2 = tmp / "out2"
    res2 = export_full_game(game, tmp / "empty_work", dest2)
    check("文件仍在", (dest2 / "Game.exe").exists(), True)
    check("覆盖数为 0", res2["overwritten"], 0)

    print()
    if FAIL:
        print(f"[FAIL] {len(FAIL)} 项未通过: {FAIL}")
        return 1
    print("[OK] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
