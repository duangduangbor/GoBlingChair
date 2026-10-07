# -*- coding: utf-8 -*-
"""v1.7 专项测试：可逆汉化安装器 + 汉化补丁包导出 + 自适应布局。

不需要模型、不需要联网 —— 全程用合成游戏 + 合成的「译文」。
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

from gametl.core import patcher                                   # noqa: E402
from gametl.core.detect import detect_engine                      # noqa: E402
from gametl.core.models import Project                            # noqa: E402
from gametl.core.package import (default_bundle_name,             # noqa: E402
                                 default_package_name, export_patch_bundle,
                                 read_manifest)
from gametl.ui_common import fit_size                             # noqa: E402

_ok = 0
_bad = 0


def rec(ok: bool, msg: str) -> None:
    global _ok, _bad
    if ok:
        _ok += 1
        print(f"[OK] {msg}")
    else:
        _bad += 1
        print(f"[!!] {msg}")


def walk(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)).replace("\\", "/")
                  for p in root.rglob("*") if p.is_file())


def section(name: str) -> None:
    print(f"\n{'=' * 62}\n{name}\n{'=' * 62}")


# ---------------------------------------------------------------- [1] 布局
def test_layout() -> None:
    section("[1] 窗口自适应（含 14 寸笔记本）")
    # 1920x1080 @150% → tkinter 看到的是 1280x720
    w, h, x, y = fit_size(1280, 720, 1180, 840, min_w=880, min_h=560)
    rec(h <= 720, f"1280×720 屏：窗口高 {h} ≤ 720（日志不会被切掉）")
    rec(w <= 1280, f"1280×720 屏：窗口宽 {w} ≤ 1280")
    rec(h >= 560, f"1280×720 屏：高度不低于最矮可用值（{h} ≥ 560）")

    w, h, _, _ = fit_size(1366, 768, 1180, 840, min_w=880, min_h=560)
    rec(w <= 1366 and h <= 768, f"1366×768 屏：{w}×{h} 完全在屏内")

    w, h, _, _ = fit_size(2560, 1440, 1180, 840, min_w=880, min_h=560)
    rec((w, h) == (1180, 840), f"大屏：保持设计尺寸 {w}×{h}（不无谓撑满）")

    # 极端小屏也不能算出负数或超出
    w, h, x, y = fit_size(800, 600, 1180, 840, min_w=880, min_h=560)
    rec(w <= 800 and h <= 600 and x >= 0 and y >= 0,
        f"极小屏兜底：{w}×{h} @ ({x},{y}) 不越界")

    w, h, _, _ = fit_size(0, 0, 1180, 840, min_w=880, min_h=560)
    rec((w, h) == (880, 560) or w >= 880, f"拿不到屏幕尺寸时回落到默认（{w}×{h}）")


# ---------------------------------------------------------------- [2] 定位
def test_locate(tmp: Path) -> None:
    section("[2] 游戏目录定位与安装状态")
    game = tmp / "我的游戏"
    shutil.copytree(ROOT / "gametl/examples/mock_rpgmaker_example", game)

    rec(patcher.looks_like_game(game), "www/data 结构被认成游戏")
    rec(not patcher.looks_like_game(tmp), "空目录不被认成游戏")

    sub = game / "补丁包文件夹" / "更里面"
    sub.mkdir(parents=True)
    rec(patcher.find_game_dir(sub) == game.resolve(),
        "从游戏内两层子目录也能向上找到游戏根（补丁包丢进游戏目录也能用）")
    rec(patcher.find_game_dir(tmp / "别处") is None, "无关目录返回 None")

    # ---- 各引擎的根目录特征 ----
    # 这里曾经漏了 Unity：`looks_like_game` 认 RPG Maker / Ren'Py / KiriKiri /
    # Buddha，就是不认 `*_Data` —— 于是「汉化安装器」对着 Night in the Woods
    # 的根目录弹「这个文件夹看着不太像游戏根目录」。
    uni = tmp / "Unity游戏"
    (uni / "Unity游戏_Data").mkdir(parents=True)
    (uni / "Unity游戏.exe").write_bytes(b"MZ")
    rec(patcher.looks_like_game(uni), "Unity（exe + *_Data）被认成游戏根")

    df = tmp / "DF游戏"
    (df / "Win" / "Packs").mkdir(parents=True)
    (df / "DF游戏.exe").write_bytes(b"MZ")
    (df / "Win" / "Packs" / "Stuff.~h").write_bytes(b"dfpf")
    (df / "Win" / "Packs" / "Stuff.~p").write_bytes(b"xx")
    rec(patcher.looks_like_game(df),
        "Buddha（封包在 Win/Packs 子目录里）被认成游戏根")

    rec(patcher.find_game_dir(uni / "Unity游戏_Data") == uni.resolve(),
        "从 Unity 的 *_Data 子目录能向上找到游戏根")

    # ---- 游戏指纹：说清「是不是**这个**游戏」，而不只是「像个游戏」----
    fp = patcher.game_fingerprint(uni)
    rec(fp.get("data_dirs") == ["Unity游戏_Data"], "指纹记下 *_Data 目录名")
    rec(patcher.fingerprint_match(fp, uni)[0] == "match", "指纹自比 = match")
    # 注意目录名别和后面 test_refuse 用的 "别的游戏" 撞（同一个 tmp 下）
    other = tmp / "另一个游戏"
    (other / "另一个游戏_Data").mkdir(parents=True)
    (other / "另一个游戏.exe").write_bytes(b"MZ")
    rec(patcher.fingerprint_match(fp, other)[0] == "mismatch",
        "指纹比对另一个游戏 = mismatch")
    rec(patcher.fingerprint_match({}, uni)[0] == "unknown",
        "没指纹的老包 = unknown（只提示，不阻断安装）")
    # 汉化工具自己的 exe 必须排除，否则两个不相干的游戏会因为
    # 「都放了个汉化安装器.exe」而被判成「匹配」
    (uni / "汉化安装器.exe").write_bytes(b"MZ")
    rec("汉化安装器.exe" not in patcher.game_fingerprint(uni).get("exe", []),
        "指纹排除汉化工具自己的 exe")
    # GitHub Release 上的资产名是 GoBlingChair-v2.6.3.exe（GitHub 不收中文名），
    # 用户常常不改名就丢进游戏目录 —— 按**前缀**也要认出来，且大小写不敏感
    (uni / "GoBlingChair-v2.6.3.exe").write_bytes(b"MZ")
    _fpe = patcher.game_fingerprint(uni).get("exe", [])
    rec("GoBlingChair-v2.6.3.exe" not in _fpe,
        "指纹排除 Release 资产名（带版本号前缀）")

    st = patcher.patch_status(game)
    rec(st["installed"] is False, "没装过 → installed=False")
    rec(str(game) in st["backup_dir"], "状态里给出备份目录位置")


# ---------------------------------------------------------------- [3] 导出
def test_bundle(tmp: Path) -> Project:
    section("[3] 导出汉化补丁包（含 zip）")
    game = tmp / "我的游戏"
    engine, _ = detect_engine(game)
    rec(engine.value == "rpgmaker_mv", f"引擎识别 = {engine.value}")

    ex = patcher.make_extractor(engine, game)
    units = ex.extract()
    rec(len(units) > 0, f"提取到 {len(units)} 条文本")
    proj = Project(root=game, engine=engine, units=units)
    for u in units:
        u.translated = "译" + u.original

    parent = tmp / "导出"
    res = export_patch_bundle(proj, parent, game.name, installer_exe=None)
    folder = Path(res["dir"])
    rec(folder.name == default_bundle_name(game.name),
        f"文件夹名带「汉化补丁包」：{folder.name}")
    rec((folder / default_package_name(game.name)).is_file(), "翻译包在其中")
    rec((folder / "安装说明.txt").is_file(), "安装说明在其中")
    rec(res["installer"] is False, "没给安装器时如实记录 installer=False")

    readme = (folder / "安装说明.txt").read_text(encoding="utf-8")
    rec("还原" in readme, "说明里讲了怎么还原")
    rec("_汉化备份_原版" in readme, "说明里交代了备份去哪儿找")

    zp = Path(res["zip"])
    rec(zp.is_file(), f"同时生成了 zip：{zp.name}")
    with zipfile.ZipFile(zp) as zf:
        names = zf.namelist()
    top = {n.split("/")[0] for n in names}
    rec(top == {folder.name}, "zip 里只有一层顶层目录（解压不会散落一地）")
    rec(any(n.endswith(".gtpkg") for n in names), "zip 内含翻译包")
    rec(any(n.endswith("安装说明.txt") for n in names), "zip 内含安装说明")

    man = read_manifest(folder / default_package_name(game.name))
    rec(man["units_translated"] == len(units),
        f"包内译文条数与工程一致（{man['units_translated']}）")
    return proj


# ---------------------------------------------------------------- [4] 安装
def test_apply(tmp: Path) -> None:
    section("[4] 就地安装汉化（带原版备份）")
    game = (tmp / "我的游戏").resolve()
    bundle = Path(tmp / "导出" / default_bundle_name(game.name))
    pkg = bundle / default_package_name(game.name)

    before = {rel: (game / rel).read_bytes() for rel in walk(game)}
    json_before = json.loads((game / "www/data/System.json")
                             .read_text(encoding="utf-8"))

    res = patcher.apply_patch(game, pkg)
    rec(res["files"] > 0, f"写入了 {res['files']} 个文件")
    rec(res["filled"] == res["total"],
        f"全部文本都填上了（{res['filled']}/{res['total']}）")
    rec(res["by_uid"] == res["total"], "匹配全部靠 uid 精确命中")
    rec(res["ratio"] == 1.0, "匹配率 100%")

    txt = (game / "www/data/System.json").read_text(encoding="utf-8")
    rec("译" in txt, "System.json 里确实出现了译文")

    bak = game / patcher.BACKUP_DIRNAME
    rec((bak / patcher.MANIFEST_NAME).is_file(), "生成了还原清单")
    man = json.loads((bak / patcher.MANIFEST_NAME).read_text(encoding="utf-8"))
    rec(man["format"] == patcher.RESTORE_FORMAT, "清单格式标记正确")
    rec(len(man["files"]) == res["files"],
        f"清单记录了 {len(man['files'])} 个被改文件")

    # 备份里必须是**原版**，而且一律 .bak 后缀
    rec((bak / "www/data/System.json.bak").is_file(),
        "原文件副本存在且加了 .bak 后缀")
    old = json.loads((bak / "www/data/System.json.bak").read_text(encoding="utf-8"))
    rec(old == json_before, "备份内容与安装前逐字符一致（是原版）")
    backups = [p for p in bak.rglob("*")
               if p.is_file() and p.name != patcher.MANIFEST_NAME]
    rec(bool(backups) and all(p.name.endswith(patcher.BAK_SUFFIX)
                              for p in backups),
        f"备份里 {len(backups)} 个文件全都带 .bak 后缀"
        "（不会被引擎/资源探测误当成游戏本体）")

    st = patcher.patch_status(game)
    rec(st["installed"] is True, "状态变为已安装")
    rec(st["files"] == res["files"], "状态里的文件数与实际一致")
    rec(st["package"] == pkg.name, "状态里记下了用的哪个包")


# ---------------------------------------------------------------- [5] 幂等
def test_idempotent(tmp: Path) -> None:
    section("[5] 重复安装不会污染备份")
    game = (tmp / "我的游戏").resolve()
    bundle = Path(tmp / "导出" / default_bundle_name(game.name))
    pkg = bundle / default_package_name(game.name)
    bak = game / patcher.BACKUP_DIRNAME

    first = (bak / patcher.MANIFEST_NAME).read_bytes()
    bak_copy = (bak / "www/data/System.json.bak").read_bytes()

    try:
        patcher.apply_patch(game, pkg)
        rec(False, "已汉化的游戏重复安装应当明确报错，而不是静默乱写")
    except RuntimeError as e:
        rec("对不上" in str(e) or "没在游戏里找到" in str(e),
            f"已汉化状态再次安装会明确报错：{str(e)[:28]}…")

    rec((bak / patcher.MANIFEST_NAME).read_bytes() == first,
        "备份清单没有被第二次安装改写")
    rec((bak / "www/data/System.json.bak").read_bytes() == bak_copy,
        "★ 备份里的原版依然是原版（没被『已汉化的内容』覆盖）")


# ---------------------------------------------------------------- [6] 还原
def test_revert(tmp: Path) -> None:
    section("[6] 一键还原原版")
    game = (tmp / "我的游戏").resolve()
    # 和**原始 mock 游戏**逐字节比对 —— 这才是「安装前的样子」
    ref = ROOT / "gametl/examples/mock_rpgmaker_example"

    res = patcher.revert_patch(game)
    rec(res["restored"] > 0, f"还原了 {res['restored']} 个文件")
    rec(not res["missing"], "没有还原失败的文件")

    bad = []
    for rel in walk(ref):
        if (game / rel).read_bytes() != (ref / rel).read_bytes():
            bad.append(rel)
    rec(not bad, "所有文件逐字节回到安装前"
                 + (f"（异常: {','.join(bad[:2])}）" if bad else ""))

    st = patcher.patch_status(game)
    rec(st["installed"] is False, "还原后状态回到「未安装」")
    rec((game / patcher.BACKUP_DIRNAME).is_dir(), "备份默认保留（可以再装）")

    # 还原之后应该能顺利再装一次
    bundle = Path(tmp / "导出" / default_bundle_name(game.name))
    pkg = bundle / default_package_name(game.name)
    again = patcher.apply_patch(game, pkg)
    rec(again["filled"] == again["total"], "还原后可以正常再装一次")


# ---------------------------------------------------------------- [7] 拒绝
def test_refuse(tmp: Path) -> None:
    section("[7] 装不进去的情况要说清楚")
    other = tmp / "别的游戏"
    shutil.copytree(ROOT / "gametl/examples/mock_rpgmaker_example", other)
    for p in (other / "www/data").glob("*.json"):
        d = json.loads(p.read_text(encoding="utf-8"))
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    # 把这个游戏的文本全部改成纯 ASCII，让包里一条都命中不了
    eng, _ = detect_engine(other)
    ex = patcher.make_extractor(eng, other)
    units = ex.extract()
    proj = Project(root=other, engine=eng, units=units)
    for u in units:
        u.translated = "ENGLISH " + u.original
    bundle = Path(tmp / "别的导出")
    res = export_patch_bundle(proj, bundle, other.name, installer_exe=None,
                              make_zip=False)
    pkg = Path(res["pkg"])

    try:
        patcher.apply_patch(tmp / "我的游戏", pkg)
        rec(False, "对不上的包应当报错")
    except RuntimeError as e:
        rec(True, f"对不上的包被拒绝：{str(e).splitlines()[0][:34]}…")

    try:
        patcher.apply_patch(tmp / "空的", pkg)
        rec(False, "不存在的游戏目录应当报错")
    except RuntimeError as e:
        rec("不存在" in str(e), f"游戏目录不存在时报错：{str(e)[:24]}")

    try:
        patcher.revert_patch(tmp / "空目录2")
        rec(False, "没备份时还原应当报错")
    except RuntimeError as e:
        rec("没有安装记录" in str(e), f"没备份就还原会明说：{str(e)[:20]}…")


# ---------------------------------------------------------------- [8] 安装器判定
def test_installer_mode() -> None:
    section("[8] 安装器模式的启动判定")
    from gametl.installer import want_installer
    rec(want_installer(["--installer"]) is True, "--installer 参数进安装器模式")
    rec(want_installer(["--patch"]) is True, "--patch 参数进安装器模式")
    rec(want_installer([]) is False,
        "源码运行且没有参数时**不**进安装器模式（不会被测试包顶掉主界面）")


# ---------------------------------------------------------------- main
def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gmttest17_"))
    try:
        test_layout()
        test_locate(tmp)
        test_bundle(tmp)
        test_apply(tmp)
        test_idempotent(tmp)
        test_revert(tmp)
        test_refuse(tmp)
        test_installer_mode()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'=' * 62}")
    print(f"结果：{_ok} 通过 / {_bad} 失败")
    print("=" * 62)
    return 1 if _bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
