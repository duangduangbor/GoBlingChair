# -*- coding: utf-8 -*-
"""安装器界面的冒烟测试：把补丁包文件夹放进游戏里，看它能不能自动认出来。

需要 tkinter；不弹任何窗口（建完就销毁）。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import tkinter as tk                                     # noqa: E402

import gametl.installer as I                             # noqa: E402
from gametl.core import patcher                          # noqa: E402
from gametl.core.detect import detect_engine             # noqa: E402
from gametl.core.models import Project                   # noqa: E402
from gametl.core.package import (default_bundle_name,    # noqa: E402
                                 default_package_name,
                                 export_patch_bundle)

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


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="instgui17_"))
    try:
        game = tmp / "我的游戏"
        shutil.copytree(ROOT / "gametl/examples/mock_rpgmaker_example", game)

        # 造一个真实补丁包，放进游戏目录里的子文件夹（模拟「解压到游戏目录」）
        eng, _ = detect_engine(game)
        ex = patcher.make_extractor(eng, game)
        units = ex.extract()
        proj = Project(root=game, engine=eng, units=units)
        for u in units:
            u.translated = "译" + u.original
        bundle = export_patch_bundle(proj, tmp / "out", game.name,
                                     installer_exe=None)
        pkg = Path(bundle["pkg"])

        drop = game / default_bundle_name(game.name)
        drop.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pkg, drop / pkg.name)
        rec((drop / pkg.name).is_file(), "补丁包已放进游戏目录的子文件夹")

        # 让安装器以为自己正躺在这个子文件夹里运行
        I.exe_dir = lambda: drop          # type: ignore[assignment]

        root = tk.Tk()
        try:
            app = I.InstallerApp(root)
            root.update_idletasks()

            rec(app.base == drop, "安装器以自身所在目录为起点")
            rec(app.game_dir == game.resolve(),
                f"向上找到游戏根：{app.game_dir.name}")
            rec(app.pkg is not None and app.pkg.name == pkg.name,
                f"自动选中翻译包：{app.pkg.name if app.pkg else '无'}")
            rec(app.install_btn.cget("state") == "normal", "「安装汉化」可用")
            rec(app.revert_btn.cget("state") == "disabled",
                "没装过时「还原原版」是灰的")
            rec("还没装过汉化" in app.state_label.cget("text"),
                "状态条提示「还没装过汉化」")
            rec(app.install_btn.cget("text").startswith("✔"),
                "按钮文字是「安装汉化」")

            # 装一次，再看界面是否切成「已安装」
            patcher.apply_patch(game, pkg)
            app.status = patcher.patch_status(game)
            app._render_state()
            root.update_idletasks()
            rec(app.revert_btn.cget("state") == "normal",
                "装完之后「还原原版」亮了")
            rec("已经装了汉化" in app.state_label.cget("text"),
                "状态条变成「已经装了汉化」")
            rec(app.install_btn.cget("text").startswith("↻"),
                "安装按钮改成「重新安装汉化」")

            # 还原，界面应回到未安装
            patcher.revert_patch(game)
            app.status = patcher.patch_status(game)
            app._render_state()
            root.update_idletasks()
            rec(app.revert_btn.cget("state") == "disabled",
                "还原之后「还原原版」重新变灰")

            # 窗口尺寸：14 寸逻辑屏也不越界
            w, h = root.winfo_width(), root.winfo_height()
            rec(w > 100 and h > 100, f"窗口实际尺寸 {w}×{h}")

            # ---- 上下分栏：上栏（设置区）必须真的看得见 ----
            # 同一类「打开只有日志、设置区要自己拖」的坑，安装器是上下分栏。
            t0 = time.time()
            while time.time() - t0 < 1.5:
                root.update()
                time.sleep(0.02)
            top = app._pane.winfo_children()[0]
            sash = app._pane.sashpos(0)
            rec(top.winfo_height() > 1,
                f"上栏（设置区）可见，高 {top.winfo_height()}px")
            rec(sash > 0, f"分隔条在 {sash}，不是 0")
            rec(app._pane.winfo_height() - sash >= 100,
                f"下栏（日志）还剩 {app._pane.winfo_height() - sash}px")
            rec(top.winfo_height() >= I.SASH_MIN_TOP,
                f"上栏 {top.winfo_height()} ≥ {I.SASH_MIN_TOP}（卡片放得下）")

            app._pane.sashpos(0, 5)          # 人为拖到最上
            app._on_sash_release()
            root.update()
            rec(app._pane.sashpos(0) >= I.SASH_MIN_TOP,
                f"拖到底后被夹回 {app._pane.sashpos(0)} ≥ {I.SASH_MIN_TOP}")

            # 主按钮必须固定在底部，不能躺在滚动区里（卡片一多就要滚动才够得着）
            rec(app.install_btn.master.master is root,
                "「安装汉化」在底部固定栏，不在设置区的滚动容器里")
            y = app.install_btn.master.winfo_y()
            rec(y + app.install_btn.master.winfo_height() <= h,
                f"底部按钮栏（y={y}）没有掉出窗口（{h}）")
        finally:
            root.destroy()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结果：{_ok} 通过 / {_bad} 失败")
    return 1 if _bad else 0


if __name__ == "__main__":
    import os
    code = main()
    sys.stdout.flush()
    # tkinter 退出后解释器有时不肯收尾（残留的 after 回调），直接送走
    os._exit(code)
