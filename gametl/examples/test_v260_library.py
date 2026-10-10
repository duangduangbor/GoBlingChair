# -*- coding: utf-8 -*-
"""v2.6.0 回归：游戏库（傻瓜式主界面）。

覆盖三件事：
1. 扫描：指一个目录，能不能把里面的游戏找出来（假游戏目录是现造的）
2. 状态：每个游戏「已汉化 / 可一键汉化 / 翻译中 / 未翻译」判得准不准
3. 接线：主界面真的把游戏库当成默认页，且 library 不反向依赖 gui
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gametl.core import library as lib  # noqa: E402
from gametl.core.patcher import BACKUP_DIRNAME, game_fingerprint  # noqa: E402

FAILS: list[str] = []


def check(ok, msg: str) -> bool:
    if not ok:
        FAILS.append(msg)
    return bool(ok)


def mk_rmmv(root: Path) -> Path:
    """造一个假的 RPG Maker MV 游戏目录。"""
    (root / "www" / "data").mkdir(parents=True)
    (root / "js").mkdir(parents=True)
    (root / "www" / "data" / "System.json").write_text(
        '{"a":1}', encoding="utf-8")
    (root / "www" / "data" / "MapInfos.json").write_text(
        '{"b":2}', encoding="utf-8")
    (root / "js" / "rpg_core.js").write_text("//x", encoding="utf-8")
    (root / "Game.exe").write_bytes(b"MZ")
    return root


def mk_unity(root: Path) -> Path:
    """造一个假的 Unity 游戏目录（exe + Xxx_Data）。"""
    data = root / f"{root.name}_Data"
    (data / "Managed").mkdir(parents=True)
    (root / f"{root.name}.exe").write_bytes(b"MZ")
    (data / "Managed" / "Assembly-CSharp.dll").write_bytes(b"MZ")
    (data / "level0").write_bytes(b"\x00" * 32)
    return root


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gametl_lib_"))
    try:
        a = mk_rmmv(tmp / "GameA")
        b = mk_unity(tmp / "GameB")
        (tmp / "NotAGame").mkdir()
        (tmp / "NotAGame" / "notes.txt").write_text("hi", encoding="utf-8")
        (tmp / "NotAGame" / "inner").mkdir()
        (tmp / "NotAGame" / "inner" / "data.bin").write_bytes(b"\x00" * 16)

        # ---- 1) 扫描 ----
        found = {p.name for p in lib.scan_games(tmp)}
        check(found == {"GameA", "GameB"},
              f"扫描结果应为 GameA/GameB，实为 {found}")

        # ---- 2) root 本身就是游戏 ----
        only = lib.scan_games(b)
        check(len(only) == 1 and only[0] == b,
              "root 本身是游戏目录时应直接返回它")

        # ---- 3) 引擎与状态 ----
        ea = lib.describe_game(a)
        check(ea.engine == "rpgmaker_mv",
              f"GameA 引擎应为 rpgmaker_mv，实为 {ea.engine}")
        check(ea.status == lib.ST_NEW,
              f"没翻过应为「{lib.ST_NEW}」，实为「{ea.status}」")
        check(bool(ea.engine_label), "engine_label 不该为空")

        eb = lib.describe_game(b)
        check(eb.engine == "unity", f"GameB 引擎应为 unity，实为 {eb.engine}")

        # ---- 4) 有匹配的翻译包 → 可一键汉化 ----
        fp = game_fingerprint(b)
        pkgs = [{"path": str(tmp / "x.gtpkg"), "name": "x.gtpkg",
                 "game": "GameB", "units": 100, "fingerprint": fp,
                 "error": ""}]
        e2 = lib.describe_game(b, pkgs=pkgs)
        check(e2.status == lib.ST_READY,
              f"有匹配包应为「{lib.ST_READY}」，实为「{e2.status}」")
        check(e2.pkg_name == "x.gtpkg", "应挑中那个包")

        # 包读不出来时不能当成可用
        bad = lib.describe_game(b, pkgs=[dict(pkgs[0], error="坏了")])
        check(bad.status == lib.ST_NEW, "坏包不该被判成「可一键汉化」")

        # ---- 5) 装过汉化 ----
        bd = b / BACKUP_DIRNAME
        bd.mkdir(parents=True, exist_ok=True)
        (bd / "gametl_restore.json").write_text(json.dumps({
            "format": "gametl-restore", "version": 1,
            "files": [{"rel": "a", "kind": "file"}],
            "last_action": "installed", "package": "test.gtpkg",
            "created_at": "2026-01-01 00:00:00",
        }, ensure_ascii=False), encoding="utf-8")
        e3 = lib.describe_game(b)
        check(e3.patched and e3.status == lib.ST_PATCHED,
              f"装过汉化应为「{lib.ST_PATCHED}」，实为「{e3.status}」")

        # 还原过（last_action=reverted）就不该再说「已汉化」
        (bd / "gametl_restore.json").write_text(json.dumps({
            "format": "gametl-restore", "version": 1,
            "files": [{"rel": "a", "kind": "file"}],
            "last_action": "reverted",
        }, ensure_ascii=False), encoding="utf-8")
        check(not lib.describe_game(b).patched, "还原过之后不该还显示「已汉化」")

        # ---- 6) 工程进度 ----
        e4 = lib.describe_game(
            a, project_state={"exists": True, "units": 10, "filled": 4})
        check(e4.status == lib.ST_PROGRESS,
              f"翻了 4/10 应为「{lib.ST_PROGRESS}」，实为「{e4.status}」")
        check(abs(e4.ratio - 0.4) < 1e-6, f"ratio 应为 0.4，实为 {e4.ratio}")

        e5 = lib.describe_game(
            a, project_state={"exists": True, "units": 10, "filled": 10})
        check(e5.status == lib.ST_TRANSLATED,
              f"翻完应为「{lib.ST_TRANSLATED}」，实为「{e5.status}」")

        # 日志里还没并进工程文件的条数也要算上
        e6 = lib.describe_game(
            a, project_state={"exists": True, "units": 10, "filled": 8,
                              "journal": 2})
        check(e6.status == lib.ST_TRANSLATED, "journal 里的译文也要计入")

        # ---- 7) 统计 ----
        s = lib.summarize([ea, eb, e3])
        check(s["total"] == 3 and s["patched"] == 1, f"统计不对：{s}")
        txt = lib.summary_text(s)
        check("3 个游戏" in txt, f"summary_text 应含总数：{txt}")
        check("本次扫到" not in lib.summary_text({"total": 0}),
              "空结果也该有人话说明")

        # ---- 8) 常见位置探测（只要求不崩 + 都真实存在） ----
        roots = lib.common_roots()
        check(isinstance(roots, list), "common_roots 应返回 list")
        check(all(Path(r).is_dir() for r in roots),
              "common_roots 只该返回存在的目录")

        # ---- 9) 静态接线：界面真的用了游戏库 ----
        gui_src = (ROOT / "gametl" / "gui.py").read_text(encoding="utf-8")
        for token in ("_build_library_page", "def lib_scan", "def lib_apply",
                      "def _start_apply_package", "def lib_choose_dir",
                      "def _lib_sync_after_change",
                      "from gametl.core.library import"):
            check(token in gui_src, f"gui.py 缺少 {token}")
        check("nb.add(lib_page" in gui_src and "nb.add(adv_page" in gui_src,
              "gui.py 没有把游戏库 / 进度与设置做成两个页签")
        check(gui_src.index("nb.add(lib_page") < gui_src.index("nb.add(adv_page"),
              "游戏库页签必须排在第 2 页之前（默认页）")
        # 傻瓜式：主界面不该再把「术语表」当卡片标题摆出来
        check("术语表（让专有名词全篇译法统一）" not in gui_src
              or gui_src.index("术语表（让专有名词全篇译法统一）")
              > gui_src.index("nb.add(adv_page"),
              "专业概念（术语表）必须待在第 2 页（📊 进度与设置）里")
        # 两个入口共用一条安装通道
        check("def _start_apply_package" in gui_src
              and gui_src.count("_start_apply_package") >= 3,
              "两个入口应共用 _start_apply_package")

        # ---- 9b) v2.6.5：页签名不能退回「高级设置」+ 点汉化必须切页 ----
        # 事故：用户在游戏库点「汉化这个游戏」，翻译确实在跑，可进度条和
        # 运行日志都在第 2 页 —— 那一页毫无动静，用户以为按钮是坏的。
        check("⚙ 高级设置" not in gui_src,
              "页签标题不该再叫「高级设置」（用户点完汉化会被自动送进去，"
              "名字得像「进度」而不是「高级」）")
        check("ADV_TAB_TEXT" in gui_src
              and "nb.add(adv_page, text=ADV_TAB_TEXT" in gui_src,
              "第 2 页标题应走 ADV_TAB_TEXT 常量（改名时不会漏掉提示文案）")
        check("def _goto_progress" in gui_src,
              "gui.py 缺少 _goto_progress（点汉化切到进度页）")
        # 两个「从游戏库发起」的入口都必须切页：汉化、还原
        for fn in ("def lib_apply", "def lib_revert"):
            seg = gui_src[gui_src.index(fn):]
            nxt = seg.find("\n    def ", 1)
            body = seg if nxt < 0 else seg[:nxt]
            check("_goto_progress()" in body,
                  f"{fn.split()[-1]} 里没调 _goto_progress()，"
                  "用户在游戏库点完看不到进度")
        # 「进度见下方」是错文案：切页之后那一页就不在下方了。
        # 用完整旧字面量匹配（注释里也会引用这半句话做说明，别误伤）。
        check("⏳ 正在处理，进度见下方" not in gui_src,
              "按钮文案不能写「进度见下方」—— 点完会切到第 2 页，不在下方")
        # 正向：忙的时候按钮必须把用户指到第 2 页去
        _rb = gui_src[gui_src.index("def _lib_refresh_buttons"):]
        _rb = _rb[: _rb.find("\n    def ", 1)]
        check("ADV_TAB_NAME" in _rb,
              "忙的时候按钮文案没指向第 2 页（应该拼 ADV_TAB_NAME）")
        # ★ 底部那条「▶ 开始汉化」（start()）也是同一个入口，也必须切页。
        #   漏它的代价实测过：用户在游戏库点了底部按钮，界面毫无动静。
        _st = gui_src[gui_src.index("def start(self)"):]
        _st = _st[: _st.find("\n    def ", 1)]
        check("_goto_progress()" in _st,
              "start() 里没调 _goto_progress() —— 点底部的「▶ 开始汉化」"
              "看不到进度页（这是用户实际点的那颗，表格里的按钮曾被挤没了）")

        # ---- 9c) v2.6.5 修：游戏库页「从下往上 pack」，按钮不能被挤没 ----
        # 事故：窗口 1080×756（用户截图那个尺寸）时 mid 卡片需要 591px 只拿到
        # 302px，「▶ 汉化这个游戏 / ↩ 还原原版 / 📂 打开文件夹」整排 h=1、
        # mapped=0 —— 屏幕上一个像素都没有，用户根本点不到。默认 1180×840 也一样。
        # 根因：这几块按「从上往下」的顺序 pack，容器不够高时 pack 把**最后
        # pack 的那个**挤成 0 高。修法：底部几块改 side="bottom" 从下往上 pack，
        # 表格（expand=True）让位。
        check('act.pack(side="bottom"' in gui_src,
              '游戏库底部按钮排要 side="bottom"'
              "（否则窗口一矮就先把它挤成 0 高）")
        for _w in ("self.lib_status", "self.lib_progress", "self.lib_stage",
                   "self.lib_detail"):
            check(f'{_w}.pack(side="bottom"' in gui_src,
                  f"{_w} 也要 side=\"bottom\"（同上，且顺序在按钮之上）")
        check('tree_wrap.pack(side="top", fill="both", expand=True' in gui_src,
              "表格要最后 pack 且 expand=True —— 高度不够时由它来让步")

        # ---- 9d) v2.6.5：撤掉「自动找到的游戏位置」那排快捷按钮 ----
        check("lib_quick_row" not in gui_src,
              "那排「📁 D:\\SteamLibrary…」快捷按钮应已撤掉（用户要求去掉；"
              "真正有用的信号是下面那张表）")
        check("lib_hint" not in gui_src,
              "快捷按钮下面那行提示要一并撤掉 —— 否则是一句指不到东西的空话")
        check("_lib_quick_roots" in gui_src and "_lib_boot_next" in gui_src,
              "撤的只是界面上那排按钮：开机自动探测 + 逐个位置试扫必须留着，"
              "否则打开软件是一张空表")

        # ---- 10) library 不许反向依赖 gui（会成环） ----
        lib_src = (ROOT / "gametl" / "core" / "library.py").read_text(
            encoding="utf-8")
        check("import gui" not in lib_src and "from gametl.gui" not in lib_src,
              "library.py 不能 import gui")

        # ---- 11) 指纹比对支持复用已采的指纹（批量扫描提速） ----
        pat_src = (ROOT / "gametl" / "core" / "patcher.py").read_text(
            encoding="utf-8")
        check("cur_fp" in pat_src, "fingerprint_match 应支持 cur_fp 参数")

        # ---- 12) 静态体检：不许有「引用了但没定义」的 self.xxx ----
        # 线上事故（v2.6.0 首版）：__init__ 里挂了 self._lib_bootstrap 回调，
        # 方法却根本没写 → 窗口一打开就 AttributeError。冒烟测试用
        # App.__new__ 手工搭界面、绕过了 __init__，所以没拦住。
        # 这里用 AST 把整份 gui.py 扫一遍：凡是 self.X 既不是方法、
        # 也没在任何地方 `self.X = ...` 赋值过，就是悬空引用。
        import ast
        tree = ast.parse(gui_src, filename="gui.py")
        app = next((n for n in tree.body
                    if isinstance(n, ast.ClassDef) and n.name == "App"), None)
        check(app is not None, "gui.py 里找不到 class App")
        if app is not None:
            methods = {n.name for n in app.body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            assigned: set = set()
            reads: dict = {}
            for node in ast.walk(app):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "self"):
                    if isinstance(node.ctx, ast.Store):
                        assigned.add(node.attr)
                    elif isinstance(node.ctx, ast.Load):
                        reads.setdefault(node.attr, node.lineno)
            dangling = {k: v for k, v in reads.items()
                        if k not in assigned and k not in methods}
            check(not dangling,
                  f"gui.py 引用了没定义的 self 属性：{dangling}")

        # ---- 13) 启动回调 / 表格数据一致性 ----
        for cb in ("_lib_bootstrap", "_lib_fill_quick", "_lib_use_root",
                   "_refresh_repo"):
            check(f"def {cb}" in gui_src, f"缺少启动相关方法 {cb}")
        check("self._lib_entries.append(ent)" in gui_src,
              "_lib_insert 必须把条目追加进 _lib_entries"
              "（行的 iid 就是它的下标，选中靠它反查）")
        check("self._lib_entries = list(entries)" in gui_src,
              "_lib_scan_done 必须无条件刷新 _lib_entries"
              "（否则空结果会留着上一轮的幽灵行）")
        check("self.after(" not in gui_src,
              "App 没有 after 方法，回调一律走 self.root.after")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if FAILS:
        print(f"[失败] {len(FAILS)} 项：")
        for m in FAILS:
            print("  -", m)
        return 1
    print("v2.6.0 游戏库：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
