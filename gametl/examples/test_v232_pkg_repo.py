# -*- coding: utf-8 -*-
"""v2.4.0 回归：翻译包仓库（软件自带 ``packages/`` 目录）。

背景
----
以前「把翻译包作用到游戏上」只有两条路：

1. 主界面「导入翻译包」—— 它其实是**把译文预填进工程**，然后照样跑一遍
   完整流程（含模型补漏），产出的是「工程 + _汉化输出目录」。
2. 把 exe 复制到包旁边，触发「安装器模式」（``installer.want_installer``）。

用户要的是第三条、也是最直接的：**软件本身**选中游戏 + 选中包，直接把
汉化写进游戏文件。这条路的底层能力一直在 ``patcher.apply_patch()`` 里
（就地在游戏目录覆盖 + 留原版备份 + 可一键还原，且零模型依赖），只是
主界面没有入口。

本测试盯住「仓库」这一层：目录约定、扫描、收入、匹配排序、坏包容错。

跑法::

    python gametl/examples/test_v232_pkg_repo.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gametl.core.models import EngineType, Project  # noqa: E402
from gametl.core.package import (  # noqa: E402
    REPO_DIRNAME, PackageError, add_to_repo, export_package, list_packages,
    rank_packages, read_manifest, repo_dir,
)
from gametl.extractors.rpgmaker import RPGMakerExtractor  # noqa: E402

PASS = 0
FAIL: list = []


def ck(ok: bool, msg: str):
    global PASS
    if ok:
        PASS += 1
    else:
        FAIL.append(msg)


MOCK = ROOT / "gametl" / "examples" / "mock_rpgmaker_example"


def _make_pkg(dest: Path, game_root: Path, tag: str = "") -> Path:
    """从 ``game_root`` 导出一个翻译包（译文是假数据，只为跑通链路）。"""
    us = RPGMakerExtractor(game_root).extract()
    for u in us:
        u.translated = (tag or "译") + (u.original or "")[:6]
    proj = Project(root=game_root, engine=EngineType.RPGMAKER_MV, units=us)
    export_package(proj, dest)
    return dest


if not MOCK.is_dir():
    FAIL.append(f"找不到 mock 游戏目录：{MOCK}")
else:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        app = td / "软件目录"
        app.mkdir()
        game = td / "MyGame"
        shutil.copytree(MOCK, game)

        # ---------- 1. 目录约定 ----------
        ck(repo_dir(app) == app / REPO_DIRNAME,
           f"仓库目录 = <软件目录>/{REPO_DIRNAME}")
        ck(list_packages(app) == [], "还没有包时返回空列表（不报错）")

        # ---------- 2. 收包进仓库 ----------
        src = _make_pkg(td / "out" / "a.gtpkg", game)
        dst = add_to_repo(app, src)
        ck(dst.parent == repo_dir(app), "收进来的包落在 packages/ 里")
        ck(dst.is_file() and dst.suffix == ".gtpkg", f"文件已就位：{dst.name}")
        ck("汉化翻译包" in dst.name, "包名是给人看的（含「汉化翻译包」）")
        ck((repo_dir(app) / dst.name).is_file(), "包确实写进了仓库目录")

        # ---------- 3. 重复收同一个包：复用，不制造副本 ----------
        again = add_to_repo(app, src)
        ck(again == dst, "内容相同的包重复收入时复用原文件")
        ck(len(list(repo_dir(app).glob("*.gtpkg"))) == 1,
           "仓库里仍然只有 1 个包")

        # ---------- 4. 同名但内容不同：另存，不覆盖 ----------
        src2 = _make_pkg(td / "out" / "b.gtpkg", game, tag="别的译文")
        dst2 = add_to_repo(app, src2)
        ck(dst2 != dst, "同名但内容不同的包另存一份，不覆盖")
        ck("-2" in dst2.name, f"重名自动加序号：{dst2.name}")
        ck(len(list(repo_dir(app).glob("*.gtpkg"))) == 2,
           "仓库里现在有 2 个包")

        # ---------- 5. 扫描读出的字段 ----------
        pkgs = list_packages(app)
        ck(len(pkgs) == 2, f"扫到 2 个包（实际 {len(pkgs)}）")
        r0 = pkgs[0]
        ck(r0["game"] == "MyGame", f"读到包内游戏名：{r0['game']!r}")
        ck(r0["engine"] == "rpgmaker_mv", f"读到引擎：{r0['engine']!r}")
        ck(r0["units"] > 0, f"读到译文条数：{r0['units']}")
        ck(r0["size"] > 0, "读到文件大小")
        ck(bool(r0["created_at"]), f"读到生成时间：{r0['created_at']!r}")
        ck(bool(r0["target_lang"]), f"读到目标语言：{r0['target_lang']!r}")
        ck(not r0["error"], "正常包不带 error")
        ck(bool(r0["fingerprint"]), "读到游戏指纹（供匹配用）")

        # ---------- 6. 软件根目录散落的包也能认 ----------
        stray = app / "随手丢的包.gtpkg"
        shutil.copy2(dst, stray)
        pkgs2 = list_packages(app)
        ck(any(Path(r["path"]) == stray for r in pkgs2),
           "软件根目录散落的 .gtpkg 也能扫到")
        ck(len(pkgs2) == 3, f"总计 3 个（实际 {len(pkgs2)}）")
        stray.unlink()

        # ---------- 7. 坏包容错：留在列表里并说明原因 ----------
        bad = repo_dir(app) / "坏掉了.gtpkg"
        bad.write_bytes(b"not a zip at all")
        rb = [r for r in list_packages(app) if Path(r["path"]) == bad]
        ck(len(rb) == 1, "坏包仍留在列表里（不会被静默忽略）")
        ck(bool(rb and rb[0]["error"]),
           f"坏包带 error 说明：{(rb[0]['error'][:40] if rb else '')!r}")
        try:
            add_to_repo(app, bad)
            ck(False, "坏包不该被收进仓库")
        except PackageError:
            ck(True, "坏包被拒绝收入仓库")
        bad.unlink()

        # ---------- 8. 匹配排序：跟游戏吻合的排前面 ----------
        other = td / "别家的游戏"
        other.mkdir()
        (other / "Other.exe").write_bytes(b"MZ")
        (other / "Other_Data").mkdir()
        pb = td / "out" / "other.gtpkg"
        export_package(Project(root=other, engine=EngineType.UNKNOWN,
                               units=[]), pb)
        shutil.copy2(pb, repo_dir(app) / "别家的包.gtpkg")

        ranked = rank_packages(list_packages(app), game)
        ck(ranked[0]["game"] == "MyGame",
           f"与所选游戏吻合的包排第一（实为 {ranked[0]['game']!r}）")
        ck(ranked[0]["verdict"] == "match",
           f"结论为完全吻合（{ranked[0]['verdict']}：{ranked[0]['verdict_text']}）")
        ck(any(r["verdict"] != "match" for r in ranked[1:]),
           "对不上的包没被排到第一位")
        ck(all("verdict" in r and "verdict_text" in r for r in ranked),
           "每项都带 verdict / verdict_text")

        # ---------- 9. 没选游戏时一律 unknown（不瞎猜） ----------
        rn = rank_packages(list_packages(app), None)
        ck(all(r["verdict"] == "unknown" for r in rn),
           "没选游戏时全部标 unknown")

        # ---------- 10. 包内指纹确实能认出游戏 ----------
        man = read_manifest(dst)
        fp = man.get("fingerprint") or {}
        ck(fp.get("dir") == "MyGame", f"指纹记了目录名：{fp.get('dir')!r}")
        ck(bool(fp.get("exe")) or bool(fp.get("dirs")) or bool(fp.get("packs")),
           "指纹里有可用于比对的项")

# ---------- 11. 静态检查：主界面确实接上了「直接汉化」 ----------
src = (ROOT / "gametl" / "gui.py").read_text(encoding="utf-8")
ck("apply_patch" in src, "主界面用上了 patcher.apply_patch（直接改游戏文件）")
ck("revert_patch" in src, "主界面用上了 patcher.revert_patch（一键还原）")
ck("list_packages" in src, "主界面用上了包仓库扫描")
ck("add_to_repo" in src, "主界面支持把包收进软件")

# patcher 必须保持「零模型依赖」—— 它是「不联网、不装模型也能汉化」的底气
psrc = (ROOT / "gametl" / "core" / "patcher.py").read_text(encoding="utf-8")
ck("translators" not in psrc,
   "patcher.py 不依赖任何翻译后端（免模型汉化成立）")

print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
for m in FAIL:
    print(f"  [FAIL] {m}")
sys.exit(1 if FAIL else 0)
