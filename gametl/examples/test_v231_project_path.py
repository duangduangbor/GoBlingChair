# -*- coding: utf-8 -*-
"""v2.3.1 回归：工程文件路径解析。

背景：从 v1.3 起工程文件搬到了 `<软件目录>/data/work/<游戏名>/project.json`，
但「快速校对 / 深度校对 / 重翻可疑条目」三处仍在拼老路径
`<游戏目录>/_汉化输出/project.json` —— 于是全部误报「还没有翻译结果」，
用户翻译完 13437 条却点不动校对。

跑法（**必须用带 tkinter 的解释器**）：
    python gametl/examples/test_v231_project_path.py
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gametl.core.models import EngineType, Project, TextUnit  # noqa: E402
from gametl.gui import (  # noqa: E402
    App, project_path_candidates, resolve_project_path, work_root_path,
)

PASS = 0
FAIL: list = []


def ck(ok: bool, msg: str):
    global PASS
    if ok:
        PASS += 1
    else:
        FAIL.append(msg)


# ---------- 1. 新版位置优先 ----------
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    app_home = td / "app"
    game = td / "MyGame"
    game.mkdir(parents=True)
    legacy = game / "_汉化输出" / "project.json"

    workp = work_root_path(app_home, game)
    workp.mkdir(parents=True)
    (workp / "project.json").write_text("{}", encoding="utf-8")

    ck(resolve_project_path(app_home, game) == workp / "project.json",
       "新版位置（软件盘 data/work）能被找到")
    ck(resolve_project_path(app_home, game) != legacy,
       "不再误认 _汉化输出/project.json")
    ck(workp == app_home / "data" / "work" / "MyGame",
       "work_root 命名规则 = <app_home>/data/work/<游戏名>")

    # ---------- 2. 旧版位置兜底 ----------
    (workp / "project.json").unlink()
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}", encoding="utf-8")
    ck(resolve_project_path(app_home, game) == legacy,
       "旧版 _汉化输出/project.json 仍能兜底")

    # ---------- 3. 都没有 → None（不能瞎报「有」）----------
    legacy.unlink()
    ck(resolve_project_path(app_home, game) is None,
       "两处都没有时返回 None")
    ck(len(project_path_candidates(app_home, game)) == 2,
       "候选路径恰有 2 个（新版 + 旧版）")

# ---------- 4. 游戏名清洗（非法字符 → 下划线，且截断）----------
# work_root_path 只做字符串计算、不碰磁盘，所以这里不需要真的建目录
# （Windows 本来也建不出带 : ? * 的目录）
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    name = work_root_path(td / "app", td / "My:Game?*<>|").name
    ck(not re.search(r'[<>:"/\\|?*]', name), f"游戏名里的非法字符被清掉：{name!r}")
    ck(len(work_root_path(td / "app", td / ("L" * 200)).name) == 60,
       "超长游戏名被截到 60 字符")
    ck(work_root_path(td / "app", td / "..").name == "game",
       "空名兜底成 game")

# ---------- 5. 增量日志与工程文件同目录同名 ----------
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    j = App._journal_of(td / "project.json")
    ck(j.name == "project.journal.jsonl", "增量日志名 = project.journal.jsonl")
    ck(j.parent == td, "增量日志与工程文件同目录")

# ---------- 6. 读工程必须连日志一起读 ----------
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    pp = td / "project.json"
    jp = td / "project.journal.jsonl"
    u1 = TextUnit(uid="a", source_file="x.json", location={"i": 1},
                  original="あ", translated="啊")
    u2 = TextUnit(uid="b", source_file="x.json", location={"i": 2},
                  original="い")
    Project(root=td, engine=EngineType.RPGMAKER_MV, units=[u1, u2]).save(pp)
    Project.append_journal([("b", "以")], jp)

    app = App.__new__(App)
    merged = App._load_project(app, pp)
    ck(merged.units[1].translated == "以",
       "只读 project.json 会漏掉日志里的译文（已改为一起读）")
    ck(Project.load(pp).units[1].translated is None,
       "对照组：不带日志读确实读不到（说明这条测试有效）")

    # 清空 + 存盘 + 删日志：日志不能把清掉的译文盖回来
    merged.units[1].translated = None
    merged.save(pp)
    App._drop_journal(app, pp)
    ck(not jp.exists(), "清空译文后日志被删除")
    ck(Project.load(pp, journal=jp if jp.exists() else None)
       .units[1].translated is None,
       "重开工程时被清掉的译文不会自己长回来")

# ---------- 7. 静态检查：源码里不许再手拼老路径 ----------
# 只匹配真正的代码写法（前面带 game_dir / self.game_dir 标识符），
# 避免误伤文档里对这种错误写法的说明。
src = (ROOT / "gametl" / "gui.py").read_text(encoding="utf-8")
bad = re.findall(
    r'(?:self\.)?game_dir\s*/\s*"_汉化输出"\s*/\s*"project\.json"', src)
ck(not bad, f'gui.py 里还有手拼 _汉化输出 工程路径的写法（{len(bad)} 处）')
ck(src.count("resolve_project_path(self.app_home") >= 2,
   "校对与重翻都走了统一的 resolve_project_path")

print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
for m in FAIL:
    print(f"  [FAIL] {m}")
sys.exit(1 if FAIL else 0)
