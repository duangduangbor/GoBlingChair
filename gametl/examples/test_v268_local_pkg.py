# -*- coding: utf-8 -*-
"""v2.6.8 回归：「一键汉化」卡片要认得**当前游戏自己的译文**。

用户实测反馈（原话）
--------------------
「这里一键汉化没能检测当前汉化的文件是哪个，甚至还停留在老游戏这里。
  无法直接点击就汉化」

翻完之后他打开「一键汉化」卡片，想点一下就装进游戏。结果：
* 列表里唯一的东西是**上一款游戏**留下的包，标着「✖ 对不上」；
* 没有任何一条会被自动选中 → 「✔ 安装到所选游戏」是灰的 → 点不了；
* 文案里那个「这个游戏」，界面上压根没写它是谁。

根因：这张卡当时只认 ``packages/`` 里的 ``.gtpkg``。**自己刚翻好的译文
不在里面** —— 它在 ``data/work/<游戏名>/project.json``，所以卡片对当前
游戏完全无能。

本测试盯两件事：
1. 「本机译文」这条候选的身份、排序、默认选中（纯逻辑，不碰界面）；
2. 它真的能一路走通：工程 → 临时包 → ``apply_patch`` → 游戏里出现中文，
   而且还能原样还原回去（端到端，全程在临时目录里，不碰任何真实游戏）。

跑法::

    python gametl/examples/test_v268_local_pkg.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gametl.core.models import EngineType, Project          # noqa: E402
from gametl.core.package import (                            # noqa: E402
    LOCAL_KEY, cand_key, default_pick, export_package, local_candidate,
    merge_candidates)
from gametl.core.patcher import apply_patch, patch_status, revert_patch  # noqa: E402
from gametl.extractors.rpgmaker import RPGMakerExtractor     # noqa: E402

PASS = 0
FAIL: list = []


def ck(ok: bool, msg: str) -> None:
    global PASS
    if ok:
        PASS += 1
    else:
        FAIL.append(msg)


MOCK = ROOT / "gametl" / "examples" / "mock_rpgmaker_example"

# 造译文的方式：加个前缀，便于在游戏文件里搜出来
TAG = "【译】"


# ---------------------------------------------------------------- 1. 纯逻辑
def test_pure():
    print("[1] 候选的身份 / 排序 / 默认选中")

    loc = local_candidate("IDJ", units=4336, units_total=4336,
                          target_lang="简体中文",
                          finished_at="2026-10-12 19:07:43",
                          proj_path="D:/app/data/work/IDJ/project.json")
    ck(loc.get("local") is True, "本机译文候选带 local 标记")
    ck(loc["units"] == 4336, f"带上译文条数（{loc['units']}）")
    ck(loc["units_total"] == 4336, "带上总数")
    ck("IDJ" in loc["name"] and "本机译文" in loc["game"],
       f"名字里写明是它自己的译文：{loc['game']!r}")
    ck(loc["proj"].endswith("project.json"), "记住了工程文件在哪")
    ck(loc["path"] == "" and loc["error"] == "",
       "它不是磁盘上的文件，但不该带 error（否则界面会当坏包）")

    ck(cand_key(loc) == LOCAL_KEY, "身份键 = 本机译文哨兵值")
    ck(cand_key({"path": "D:/x/a.gtpkg"}) == "D:/x/a.gtpkg",
       "仓库包的身份键 = 文件路径")
    ck(cand_key(loc) != cand_key({"path": ""}), "两者不会撞键")

    repo = [
        {"path": "a.gtpkg", "game": "AAA", "verdict": "match"},
        {"path": "b.gtpkg", "game": "BBB", "verdict": "mismatch"},
    ]
    merged = merge_candidates(repo, loc)
    ck(len(merged) == 3, f"合并后 3 条（实际 {len(merged)}）")
    ck(merged[0].get("local") is True, "本机译文排在最前面")
    ck(merged[0]["verdict"] == "match",
       f"本机译文直接给 match（{merged[0]['verdict']}）")
    ck([r.get("game") for r in merged[1:]] == ["AAA", "BBB"],
       "仓库包原来的顺序没被打乱")
    ck(repo[0].get("verdict") == "match" and len(repo) == 2,
       "合并不会改动传进来的原列表（浅拷贝）")
    ck(merge_candidates(repo, None) == repo, "没有本机译文时原样返回")

    # ★ 这条是「上一款游戏的包被自动选中」的守卫
    ck(default_pick([{"path": "b.gtpkg", "verdict": "mismatch"},
                     {"path": "c.gtpkg", "verdict": "unknown"}]) is None,
       "全是「对不上 / 说不准」时，一条都不默认选中")
    ck(default_pick([{"verdict": "mismatch"}, {"verdict": "match"}]) == 1,
       "有吻合的才选它（跳过前面那条对不上的）")
    ck(default_pick([dict(loc, verdict="match")]) == 0,
       "只有本机译文时，默认选中它")
    ck(default_pick([{"path": "x", "error": "坏了", "verdict": "match"}]) is None,
       "读不出来的包不会被默认选中")

    ck(local_candidate("", units=0)["game"].startswith("当前游戏"),
       "名字缺失时兜底成「当前游戏…」，不产出 None 或空串")


# ------------------------------------------------- 2. 端到端：真装进游戏
def test_end_to_end():
    print("[2] 端到端：工程 → 临时包 → 装进游戏 → 还原")

    if not MOCK.is_dir():
        FAIL.append(f"找不到 mock 游戏目录：{MOCK}")
        return

    tmp = Path(tempfile.mkdtemp(prefix="v268_"))
    try:
        game = tmp / "MyGame"
        shutil.copytree(MOCK, game)
        before = (game / "www" / "data" / "Actors.json").read_bytes()
        ck(TAG.encode("utf-8") not in before, "原版里当然没有我们的标记")

        # ---- 当作「汉化椅刚翻完这个游戏」：工程落盘到 data/work/<名字>/ ----
        work = tmp / "app" / "data" / "work" / "MyGame"
        work.mkdir(parents=True)
        proj_path = work / "project.json"

        units = RPGMakerExtractor(game).extract()
        ck(len(units) > 0, f"从 mock 游戏提出 {len(units)} 条文本")
        for u in units:
            u.translated = TAG + (u.original or "")[:6]
        project = Project(root=game, engine=EngineType.RPGMAKER_MV, units=units)
        project.mark_finished(proj_path, game_dir=str(game),
                              out_dir=str(tmp / "out"))
        ck(proj_path.is_file(), "工程写进了 data/work/MyGame/")

        # ---- 模拟 _local_candidate：只读 meta 就能拿到条数 ----
        meta = Project.read_meta(proj_path)
        ck(int(meta.get("filled") or 0) == len(units),
           f"工程 meta 记着已译 {meta.get('filled')} 条")
        rec = local_candidate(game.name, units=int(meta["filled"]),
                              units_total=int(meta["units"]),
                              target_lang="简体中文",
                              proj_path=str(proj_path))
        ck(rec["units"] == len(units) and rec["proj"] == str(proj_path),
           "本机译文候选指向的就是这个工程")

        # ---- 模拟 _apply_local_worker：读工程 → 临时包 → apply_patch ----
        loaded = Project.load(proj_path)
        filled, total = loaded.progress()
        ck(filled == total == len(units), f"读回工程 {filled}/{total} 条")

        pkg = tmp / "tmp_pkg" / "MyGame.gtpkg"
        pres = export_package(loaded, pkg, target_lang="简体中文")
        ck(pres["units"] == len(units), f"临时翻译包 {pres['units']} 条")
        man = json.loads(zipfile.ZipFile(pkg)
                         .read("manifest.json").decode("utf-8"))
        ck(man.get("fingerprint", {}).get("dir") == "MyGame",
           "临时包里的指纹指向当前游戏（不是别的游戏）")

        res = apply_patch(game, pkg)
        ck(res["filled"] == len(units),
           f"安装命中 {res['filled']}/{len(units)} 条")
        ck(res["files"] > 0, f"改动了 {res['files']} 个文件")

        after = (game / "www" / "data" / "Actors.json").read_bytes()
        ck(TAG.encode("utf-8") in after, "游戏文件里真的出现中文了")
        ck(after != before, "文件内容确实变了")

        st = patch_status(game)
        ck(st.get("installed") is True, "装完之后状态是「已汉化」")
        ck(st.get("package") == "MyGame.gtpkg",
           f"记账里记着用的哪个包（{st.get('package')!r}）")

        # ---- 可逆：用户点「↩ 还原原版」必须回到原样 ----
        rres = revert_patch(game)
        ck((game / "www" / "data" / "Actors.json").read_bytes() == before,
           "还原之后文件与原版逐字节一致")
        ck(rres.get("restored"), f"还原了 {rres.get('restored')} 个文件")
        ck(patch_status(game).get("installed") is False,
           "还原之后状态回到「未汉化」")

        # ---- 装过汉化的游戏再硬装 → 一条都对不上（所以界面要先拦） ----
        apply_patch(game, pkg)
        try:
            apply_patch(game, pkg)
            ck(False, "装过汉化的游戏再装一次不该静默成功")
        except RuntimeError as e:
            ck("对不上" in str(e) or "没匹配" in str(e),
               f"重复硬装会报错（界面据此提示「先还原再装」）："
               f"{str(e).splitlines()[0][:30]}…")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------- 3. 静态：界面真的接上了
def test_wiring():
    print("[3] 静态检查：卡片真的接上了这条新路")
    src = (ROOT / "gametl" / "gui.py").read_text(encoding="utf-8")

    for name in ("_local_candidate", "_start_apply_local",
                 "_apply_local_worker", "_current_repo_key", "_index_of_key"):
        ck(f"def {name}" in src, f"gui.py 里有 {name}")

    ck("merge_candidates" in src and "default_pick" in src,
       "包列表用 merge_candidates / default_pick（本机译文插队 + 默认选中守卫）")
    ck("cand_key" in src,
       "选中状态按身份键恢复（不是按下标 —— 本机译文插队会让下标平移）")
    ck("local_candidate" in src and "export_package" in src,
       "本机译文这条路：先打成临时包，再交安装器")
    ck("apply_patch" in src and "revert_patch" in src,
       "底层仍是已测过的 apply_patch / revert_patch")

    # 卡片文案必须说出「当前游戏是谁」
    ck("当前游戏：《" in src,
       "卡片文案点名当前游戏（原来只说「这个游戏」，用户无从判断）")
    ck("已经装过汉化了" in src, "已装汉化时文案会说明")
    ck("要重新装一套译文" in src or "先把它**还原成原版**" in src,
       "装过汉化的游戏会先提示还原，不硬装")

    # 本机译文没有磁盘文件，按钮的可用性判断不能去看 path 是否存在
    ck("def revert_game(self, confirm: bool = True)" in src,
       "还原支持 confirm=False（「先还原再装」时不再问第二遍）")

    # 回归红线：不许在卡片里再长出一颗干同样事的按钮
    seg = src[src.find("def apply_package_to_game"):]
    seg = seg[:seg.find("def revert_game")]
    ck(seg.count("command=") == 0, "分流函数里没有偷偷加按钮")


if __name__ == "__main__":
    print("v2.6.8 本机译文候选 / 一键汉化：")
    test_pure()
    test_end_to_end()
    test_wiring()
    print(f"\n通过 {PASS} 项，失败 {len(FAIL)} 项")
    for m in FAIL:
        print(f"  [FAIL] {m}")
    raise SystemExit(1 if FAIL else 0)
