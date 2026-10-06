# -*- coding: utf-8 -*-
"""v2.6.1 回归：App 真的能开机启动，而不只是「界面能搭出来」。

为什么单独有这个测试
--------------------
v2.6.0 首版翻车：``__init__`` 里挂了 ``self.root.after(260, self._lib_bootstrap)``
回调，可 ``_lib_bootstrap`` 这个方法压根没写 → 用户双击 exe，窗口还没出来就
``AttributeError: 'App' object has no attribute '_lib_bootstrap'``。

而 ``test_gui_smoke.py`` 为了不启引擎，用 ``App.__new__`` 手工复刻字段、
只调 ``_setup_ui()`` —— **绕过了 ``__init__``**，所以它全绿也拦不住。

本测试补上这个洞：真跑一遍 ``App.__init__``（引擎/硬件探测打桩），
让 Tk 把 ``after`` 队列跑起来，一直走到「开机自动扫描 → 表格填满 → 选中一行
→ 按钮文案跟着变」。任何回调抛异常 / 弹窗都会被记下来当失败。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# GAMETL_HOME 必须在构造 App 之前设好：App 用它当软件根目录（放工程/翻译包）
TMP = Path(tempfile.mkdtemp(prefix="gametl_boot_"))
GOODS = TMP / "common"          # 假装这是「自动探测到的游戏位置」
GOODS.mkdir(parents=True, exist_ok=True)
# 一个「探测得到、但里面没有游戏」的位置（用户机器上常见：空的 Steam 库）。
# 开机自动连扫必须先扫它、再换到能扫到游戏的 GOODS。
EMPTY = TMP / "empty_lib"
(EMPTY / "Steamworks Shared").mkdir(parents=True, exist_ok=True)
(EMPTY / "Steam Controller Configs").mkdir(parents=True, exist_ok=True)
os.environ["GAMETL_HOME"] = str(TMP)

import tkinter as tk  # noqa: E402

from gametl import gui as gui_mod  # noqa: E402
from gametl.core import library as lib  # noqa: E402
from gametl.runtime_manager import RuntimeManager  # noqa: E402

FAILS: list[str] = []
NOTES: list[str] = []


def check(ok, msg: str) -> bool:
    if not ok:
        FAILS.append(msg)
    return bool(ok)


def mk_rmmv(root: Path, name: str) -> Path:
    p = root / name
    (p / "www" / "data").mkdir(parents=True)
    (p / "js").mkdir(parents=True)
    (p / "www" / "data" / "System.json").write_text('{"a":1}', encoding="utf-8")
    (p / "www" / "data" / "MapInfos.json").write_text(
        '{"b":2}', encoding="utf-8")
    (p / "js" / "rpg_core.js").write_text("//x", encoding="utf-8")
    (p / "Game.exe").write_bytes(b"MZ")
    return p


class FakeMsgBox:
    """替掉 messagebox：**绝不弹真的模态框**（无人点它 → 测试会永远卡住）。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def _rec(self, kind, *a, **k):
        self.calls.append((kind, a[0] if a else "", a[1] if len(a) > 1 else ""))
        return False                       # askyesno 一律答「否」

    def showinfo(self, *a, **k):
        return self._rec("info", *a, **k)

    def showwarning(self, *a, **k):
        return self._rec("warning", *a, **k)

    def showerror(self, *a, **k):
        return self._rec("error", *a, **k)

    def askyesno(self, *a, **k):
        return self._rec("askyesno", *a, **k)


def main() -> int:
    games = [mk_rmmv(GOODS, "GameA"), mk_rmmv(GOODS, "GameB")]

    # ---- 打桩：不启引擎、不探显卡、不扫真实磁盘 ----
    # ⚠️ 两个桩都得**稍等一会儿**再返回：真实情况下引擎/硬件探测要花上秒级，
    #    主线程早就进 mainloop 了；这里秒回的话，工作线程会在 mainloop 起来
    #    之前调 root.after()，Tk 会甩 "main thread is not in main loop"。
    def _stub_prepare(self, *a, **k):
        time.sleep(0.4)
        return True, "127.0.0.1:1", "stub-model", "测试打桩：引擎未真启动"

    def _stub_hw(*a, **k):
        time.sleep(0.4)
        return None

    RuntimeManager.prepare = _stub_prepare          # type: ignore[assignment]
    RuntimeManager.shutdown = lambda self: None     # type: ignore[assignment]
    gui_mod.detect_hardware = _stub_hw
    gui_mod.common_roots = lambda: [EMPTY, GOODS]      # 第一个位置是空的
    mb = FakeMsgBox()
    gui_mod.messagebox = mb

    # 回调异常：默认会被 App 换成「弹窗」，那会卡死测试 —— 这里换成记账，
    # 而且必须在 App 构造之后替换（__init__ 末尾会重设它）。
    cb_errors: list[str] = []

    root = tk.Tk()
    root.withdraw()

    app = None
    try:
        app = gui_mod.App(root)            # ★ 真跑 __init__
    except Exception as e:                 # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(False, f"App.__init__ 抛异常：{type(e).__name__}: {e}")

    if app is None:
        try:
            root.destroy()
        except Exception:                  # noqa: BLE001
            pass
        shutil.rmtree(TMP, ignore_errors=True)
        print("[失败] 应用起不来：")
        for m in FAILS:
            print("  -", m)
        return 1

    root.report_callback_exception = (     # type: ignore[assignment]
        lambda exc, val, tb: cb_errors.append(f"{exc.__name__}: {val}"))
    NOTES.append("App.__init__ 正常返回")

    # ---- 跑真的 mainloop，把 after 链走完 ----
    # 必须是真的 mainloop：工作线程靠 root.after() 往主线程投递回调，而
    # `main thread is not in main loop` 时 Tk 是拒绝受理的。
    state = {"done": False, "timeout": False}
    t0 = time.time()

    def poll():
        if app._lib_entries and str(app.lib_scan_btn.cget("text")).startswith("🔍"):
            state["done"] = True               # 扫描收工（按钮文案复位）
            root.quit()
            return
        if time.time() - t0 > 40:
            state["timeout"] = True
            root.quit()
            return
        root.after(50, poll)

    root.after(50, poll)
    root.mainloop()

    check(not state["timeout"], "等扫描完成超时（40 秒）")
    check(not cb_errors, f"回调抛异常：{cb_errors}")
    check(app._lib_scanned_root == str(GOODS),
          f"开机该自动连扫：第一个位置没游戏就得换下一个，"
          f"最终停在「{app._lib_scanned_root}」")
    check(not app._lib_boot_mode and not app._lib_boot_queue,
          "扫到游戏后自动连扫该收工")
    check(len(app._lib_entries) == 2,
          f"应扫到 2 个游戏，实为 {len(app._lib_entries)}")
    rows = app.lib_tree.get_children()
    check(len(rows) == 2, f"表格应有 2 行，实为 {len(rows)}")

    # ---- 行 ↔ 数据的下标对应（iid 就是 _lib_entries 的下标）----
    if len(rows) == 2:
        check([app.lib_tree.item(r, "values")[0] for r in rows]
              == ["GameA", "GameB"], "表格里的游戏名/顺序不对")
        app.lib_tree.selection_set("1")     # 选中第 2 行
        root.update()
        sel = app._lib_selected()
        check(sel is not None and sel.name == "GameB",
              f"选中第 2 行应拿到 GameB，实为 {getattr(sel, 'name', None)}")
        check("GameB" in str(app.lib_detail.cget("text")),
              "选中后详情行没更新")
        check(app.lib_tree.item("1", "values")[1] == lib.ST_NEW,
              "没翻过的游戏状态该是「未翻译」")

    # ---- 选中后按钮可用 + 文案跟着状态走 ----
    check(str(app.lib_apply_btn.cget("state")) == "normal",
          "选中游戏后「汉化这个游戏」该可用")
    check("汉化这个游戏" in str(app.lib_apply_btn.cget("text")),
          f"按钮文案不对：{app.lib_apply_btn.cget('text')}")
    check(str(app.lib_revert_btn.cget("state")) == "disabled",
          "没装过汉化时「还原原版」该禁用")

    # ---- 傻瓜式：默认页就是游戏库 ----
    check(app.nb.index("current") == 0, "默认页不是第 1 页")
    check("游戏库" in str(app.nb.tab(0, "text")), "第 1 页标题不是「游戏库」")

    # ---- 扫描期间不许弹窗（弹了就是有东西没兜住）----
    silent = [c for c in mb.calls if c[0] in ("error", "warning")]
    check(not silent, f"启动/扫描过程中弹了框：{silent}")

    # ---- 关窗收尾 ----
    try:
        app.on_close()
    except Exception as e:                 # noqa: BLE001
        check(False, f"on_close 抛异常：{type(e).__name__}: {e}")
    check(app._closing, "on_close 之后 _closing 该为 True")

    try:
        root.destroy()
    except Exception:                      # noqa: BLE001
        pass
    shutil.rmtree(TMP, ignore_errors=True)

    if FAILS:
        print(f"[失败] {len(FAILS)} 项：")
        for m in FAILS:
            print("  -", m)
        return 1
    for n in NOTES:
        print("     ", n)
    print("v2.6.1 开机启动：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
