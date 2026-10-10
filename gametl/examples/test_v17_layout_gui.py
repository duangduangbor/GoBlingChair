# -*- coding: utf-8 -*-
"""v1.7 布局专项测试：分栏默认位置、按钮折行、偏好记忆。

需要 tkinter（真开窗口），但**不需要模型 / 不需要联网**。

最重要的一条是「打开就是两栏」的回归守卫 —— 用户反馈过「打开只有日志，
设置区要自己拖出来」。根因是 ttk 分栏按 weight 分配空间，weight=0 的那一栏
会永远停在首次布局时的尺寸（此时窗口还没映射，是 1px）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="gtl17_"))
os.environ["GAMETL_HOME"] = str(TMP)      # 别碰真实安装目录

from gametl.ui_common import (                                    # noqa: E402
    SASH_MIN_LEFT, SASH_MIN_RIGHT, button_cols, clamp_sash, fit_size,
    install_thread_hook, read_prefs, sash_target, write_prefs,
)

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


def section(name: str) -> None:
    print(f"\n===== {name} =====")


# ---------------------------------------------------------------- 纯计算
def test_sash_math() -> None:
    section("[1] 分栏位置的算术（不依赖 GUI）")

    # 默认窗口 1180 → 分栏 1144
    pos = sash_target(1144)
    rec(pos >= 460, f"默认分栏宽 1144 → 左栏 {pos} ≥ 460"
                    "（卡片自然宽度约 460，这是「打开即两栏且不切字」的下限）")
    rec(pos > 0, "**左栏宽度不为 0**（曾经的 bug：打开只有日志）")
    rec(pos + SASH_MIN_RIGHT <= 1144, f"右栏还剩 {1144 - pos} ≥ {SASH_MIN_RIGHT}"
                                      "（日志看得见）")

    rec(sash_target(1) == 0, "容器还没布局（宽 1）时返回 0，不瞎猜")
    rec(sash_target(0) == 0, "宽 0 时返回 0")
    rec(sash_target(None) == 0, "拿不到宽度时返回 0")

    # 14 寸笔记本 1280×720：fit_size 给出 1180 宽 → 同样的分栏
    w, h, _, _ = fit_size(1280, 720, 1180, 840, min_w=880, min_h=560)
    p = sash_target(w - 36)                 # 减掉左右各 18 的边距
    rec(p >= SASH_MIN_LEFT, f"14 寸笔记本（窗口 {w}）左栏 {p} ≥ {SASH_MIN_LEFT}")

    # 窄容器：右栏必须先留够
    for span in (500, 600, 700, 844):
        p = sash_target(span)
        rec(p >= 0 and span - p >= min(SASH_MIN_RIGHT, span),
            f"窄容器 {span}：左 {p} + 右 {span - p} 不越界")

    # 记住用户拖的位置
    rec(sash_target(1144, saved=700) == 700, "有保存值时用保存值（700）")
    rec(sash_target(1144, saved=9999) <= 1144 - SASH_MIN_RIGHT,
        "保存值过大时被夹回（窗口变小后不能把右栏挤没）")
    rec(sash_target(1144, saved=-5) == sash_target(1144),
        "保存值非法（负数）时回落到默认比例")
    rec(sash_target(1144, saved="abc") == sash_target(1144),
        "保存值不是数字时回落到默认比例")

    # 夹紧
    rec(clamp_sash(1144, 0) >= SASH_MIN_LEFT, "拖到 0 会被夹回最小宽度")
    rec(clamp_sash(1144, 2000) <= 1144 - SASH_MIN_RIGHT, "拖过头会被夹回")
    rec(clamp_sash(1144, 600) == 600, "合法位置不动它")


def test_button_cols() -> None:
    section("[2] 按钮折行（窄栏不切掉最后一颗）")

    four = [110, 110, 110, 110]
    rec(button_cols(four, 1144) == 4, "宽栏：4 颗按钮一行排开")
    rec(button_cols(four, 466) == 4, "默认左栏 466：仍然一行")
    rec(button_cols(four, 350) == 2, "窄栏 350：折成 2+2（而不是 3+1）")
    rec(button_cols(four, 200) == 1, "极窄 200：一列")
    rec(button_cols(four, 1) == 4, "容器还没布局时先按一行摆")

    three = [150, 160, 130]
    rec(button_cols(three, 466) == 3, "3 颗按钮在 466 宽下一行")
    # 316 = 150+160+6，正好是 2 列需要的宽度
    rec(button_cols(three, 340) == 2, "3 颗按钮在 340 宽下折成 2+1")
    rec(button_cols(three, 300) == 1, "3 颗按钮在 300 宽下一列（真的排不下）")
    rec(button_cols([], 500) == 0 or button_cols([], 500) == 1,
        "空列表不炸")
    rec(button_cols([120], 50) == 1, "单颗按钮永远是 1 列")

    # 折行后的总宽度必须真的装得下（不是「折了个寂寞」）
    for avail in (200, 300, 350, 466, 600):
        c = button_cols(four, avail)
        widest = 0
        for r in range(0, 4, c):
            row = four[r:r + c]
            widest = max(widest, sum(row) + 6 * (len(row) - 1))
        rec(widest <= avail or c == 1,
            f"按 {c} 列排好后最宽一行 {widest} ≤ 容器 {avail}")


def test_prefs() -> None:
    section("[3] 界面偏好（记住用户拖的分栏位置）")

    p = TMP / "prefs_probe" / "ui_prefs.json"    # 别用界面真正会读的那个路径
    rec(read_prefs(p) == {}, "文件不存在时返回空 dict（不抛异常）")
    rec(write_prefs(p, {"sash": 640}) is True, "写入成功")
    rec(p.is_file(), f"文件真的落盘：{p.name}")
    rec(read_prefs(p).get("sash") == 640, "读回来是 640")
    rec(write_prefs(p, {"sash": 700, "x": 1}) is True, "覆盖写成功")
    rec(read_prefs(p) == {"sash": 700, "x": 1}, "覆盖后内容正确")

    p.write_text("{ 这不是 json", encoding="utf-8")
    rec(read_prefs(p) == {}, "文件损坏时返回空 dict（界面不能被偏好拖垮）")
    p.write_text("[1,2,3]", encoding="utf-8")
    rec(read_prefs(p) == {}, "内容是数组（不是对象）时也返回空 dict")
    p.write_text('{"sash": "640"}', encoding="utf-8")
    rec(read_prefs(p).get("sash") == "640", "字符串值原样返回（由调用方转换）")


# ---------------------------------------------------------------- 真开窗口
def test_real_window() -> None:
    section("[4] 真开一次主界面：打开就该是两栏")

    try:
        import tkinter as tk
    except ImportError:
        rec(False, "当前 Python 没有 tkinter，跳过真窗口检查")
        return

    install_thread_hook()
    import gametl.gui as G

    root = tk.Tk()
    app = G.App(root)

    def pump(seconds: float) -> None:
        t0 = time.time()
        while time.time() - t0 < seconds:
            root.update()
            time.sleep(0.02)

    pump(1.5)                                # 让窗口完成映射与落位

    # ⚠️ 必须先切到分栏所在的那一页，否则量的是空气。
    #    分栏从 v2.6.0 起搬进了 Notebook 的第 2 页（当时叫「高级设置」，
    #    v2.6.5 起叫「📊 进度与设置」），而默认停在「🎮 游戏库」。
    #    **没被选中的 Notebook 页是未映射的 —— 控件的 winfo_width() 恒为 1。**
    #    这个测试从那一刻起就没再量到过真东西，偏偏它又没被 _runall.ps1 收进去，
    #    于是烂了很久没人发现（实测：不切页 → pane 宽 1px；切过去 → 1140px）。
    app.nb.select(1)
    pump(0.6)

    rec(app._pane.winfo_ismapped(),
        "第 2 页的分栏已被映射（不是量一个没显示的控件）")

    pane_w = app._pane.winfo_width()
    sash = app._pane.sashpos(0)
    left = app.settings.winfo_width()
    right = pane_w - sash

    rec(left > 1, f"**左栏可见（宽 {left}px）** —— 头号回归：不再需要用户自己拖")
    rec(sash > 0, f"分隔条在 {sash}，不是 0")
    rec(app._sash_saved is None, "首次打开没有历史位置，用的是默认比例")
    rec(left >= min(SASH_MIN_LEFT, pane_w), f"左栏 {left} ≥ {SASH_MIN_LEFT}")
    rec(right >= min(SASH_MIN_RIGHT, pane_w), f"右栏 {right} ≥ {SASH_MIN_RIGHT}")
    rec(app._sash_locked, "落位成功后锁定，不再和用户抢分隔条")

    # 卡片不能被切：正文容器的自然宽度要装得进左栏
    body_need = app.settings.body.winfo_reqwidth()
    rec(body_need <= left,
        f"左栏装得下卡片（卡片需要 {body_need} ≤ 左栏 {left}）")

    # 折行：窄栏时档位按钮应折成两行
    tier = None
    for b in app._profile_btns.values():
        tier = b.master
        break
    if tier is not None:
        rows4 = len({b.grid_info().get("row") for b in app._profile_btns.values()})
        rec(rows4 == 1, f"宽栏下 4 颗档位按钮排 1 行（实际 {rows4} 行）")
        app._pane.sashpos(0, 300)            # 人为挤窄
        root.update()
        pump(0.3)
        rows_narrow = len({b.grid_info().get("row")
                           for b in app._profile_btns.values()})
        rec(rows_narrow == 2, f"挤到 300px 时自动折成 2 行（实际 {rows_narrow} 行）")
        app._on_sash_release()               # 模拟松手
        root.update()
        rec(app._pane.sashpos(0) >= min(SASH_MIN_LEFT, pane_w),
            f"松手后被夹回 {app._pane.sashpos(0)} ≥ {SASH_MIN_LEFT}"
            "（拖没了拖不回来是最讨厌的）")
        saved = read_prefs(TMP / "data" / "ui_prefs.json").get("sash")
        rec(saved is not None, f"拖动位置被记住（sash={saved}）")

    # 关窗再开一次：位置应当沿用（而不是又回到默认）
    pump(0.4)
    app.on_close()                           # 走真实关窗路径（顺带验证不留 Tcl 报错）
    rec(app._closing and app._pump_id is None,
        "关窗后日志泵已停止（否则 Tk 会报 invalid command name）")

    root2 = tk.Tk()
    app2 = G.App(root2)
    t0 = time.time()
    while time.time() - t0 < 1.2:
        root2.update()
        time.sleep(0.02)
    # 同上：分栏在第 2 页，得先切过去才量得到
    app2.nb.select(1)
    t0 = time.time()
    while time.time() - t0 < 0.6:
        root2.update()
        time.sleep(0.02)
    rec(app2._sash_saved is not None,
        f"第二次打开读到了上次的位置（{app2._sash_saved}）")
    rec(abs(app2._pane.sashpos(0) - app2._sash_saved) <= 2,
        f"第二次打开直接落在 {app2._pane.sashpos(0)}，不需要用户再拖一次")
    app2.on_close()


# ---------------------------------------------------------------- main
def main() -> int:
    try:
        test_sash_math()
        test_button_cols()
        test_prefs()
        test_real_window()
    finally:
        import shutil
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{'=' * 62}")
    print(f"结果：{_ok} 通过 / {_bad} 失败")
    print("=" * 62)
    return 1 if _bad else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    # tkinter 退出后解释器有时不肯收尾（残留的 after 回调 / Tcl 异步处理器），
    # 表现为「结果明明是 0 失败，退出码却是 1」——批量回归会把它误判成失败。
    # 和 test_v17_installer_gui 一样，直接送走。
    os._exit(code)
