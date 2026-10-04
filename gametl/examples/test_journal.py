# -*- coding: utf-8 -*-
"""增量落盘（JSONL journal）往返测试。

用法：
    env -u PYTHONPATH PYTHONIOENCODING=utf-8 python -u gametl/examples/test_journal.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from gametl.core.models import EngineType, Project, TextKind, TextUnit  # noqa: E402

FAIL: list = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  [{'OK' if ok else 'FAIL'}] {label}: {got!r}")
    if not ok:
        FAIL.append(label)


def mk(n: int = 5) -> Project:
    return Project(
        root=Path("X:/g"), engine=EngineType.RPGMAKER_MV,
        units=[TextUnit(uid=f"u{i}", source_file="a.json",
                        location={"i": i}, original=f"原文{i}",
                        kind=TextKind.DIALOGUE) for i in range(n)],
        meta={"k": 1})


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gametl_j_"))
    pj = tmp / "project.json"
    jn = tmp / "project.journal.jsonl"

    print("-- 全量保存 / 读取 --")
    mk().save(pj)
    raw = pj.read_text(encoding="utf-8")
    check("默认不缩进", "\n  " not in raw, True)
    b = Project.load(pj)
    check("条数", len(b.units), 5)
    check("初始无译文", b.units[0].translated, None)

    print("-- 增量追加 --")
    n = Project.append_journal([("u0", "译文0"), ("u2", "译文2")], jn)
    check("写入行数", n, 2)
    check("日志物理行数",
          len([x for x in jn.read_text(encoding="utf-8").splitlines() if x]), 2)
    b2 = Project.load(pj, journal=jn)
    check("u0 生效", b2.units[0].translated, "译文0")
    check("u1 仍空", b2.units[1].translated, None)
    check("u2 生效", b2.units[2].translated, "译文2")

    print("-- 同一 uid 重复写：后写覆盖 --")
    Project.append_journal([("u0", "新译文0")], jn)
    check("覆盖生效", Project.load(pj, journal=jn).units[0].translated, "新译文0")

    print("-- 脏行 / 未知 uid 被安全忽略 --")
    with open(jn, "a", encoding="utf-8") as f:
        f.write("这不是json\n")
        f.write(json.dumps({"uid": "不存在", "t": "x"}) + "\n")
    b4 = Project.load(pj, journal=jn)
    check("有效记录仍读得到", b4.units[2].translated, "译文2")

    print("-- 空 entries 不产生写入 --")
    before = jn.stat().st_size
    check("返回 0", Project.append_journal([], jn), 0)
    check("文件未变", jn.stat().st_size, before)

    print("-- compact：合并进工程并删除日志 --")
    m = Project.compact(pj, jn)
    check("合并后条数", len(m.units), 5)
    check("日志已删除", jn.exists(), False)
    check("已写回工程", Project.load(pj).units[0].translated, "新译文0")

    print("-- 向后兼容：旧工程（无 journal）照常读 --")
    p2 = tmp / "legacy.json"
    legacy = mk(3)
    p2.write_text(json.dumps({
        "root": str(legacy.root), "engine": legacy.engine.value,
        "meta": {}, "units": [u.to_dict() for u in legacy.units],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    check("带缩进的旧文件也能读", len(Project.load(p2).units), 3)

    print()
    if FAIL:
        print(f"[FAIL] {len(FAIL)} 项未通过: {FAIL}")
        return 1
    print("[OK] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
