# -*- coding: utf-8 -*-
"""GUI 冒烟测试：真正构造一次界面再销毁，确保布局代码不报错。

只搭界面 + 点一遍档位/模型按钮，**不启动引擎**（避免拖慢/占显存）。
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("GAMETL_HOME", r"D:/滚刀哥布林汉化椅")

import tkinter as tk  # noqa: E402

from gametl.gui import App  # noqa: E402
from gametl.profiles import PROFILE_ORDER, Profile  # noqa: E402

FAILED: list = []


def main() -> int:
    root = tk.Tk()
    # 不启动后台线程/引擎：直接构造 UI 部分
    app = App.__new__(App)
    app.root = root

    # 复刻 __init__ 里 _setup_ui 之前的必要字段
    import queue
    import threading
    from collections import deque

    from gametl.runtime_manager import RuntimeManager

    app.game_dir = None
    app.worker = None
    app.cancel_event = threading.Event()
    app.log_queue = queue.Queue()
    app.hw = None
    app._t0 = 0.0
    app._tr_t0 = 0.0
    app._rate_hist = deque(maxlen=64)
    app._last_err_ui = ""
    app.app_home = Path(os.environ["GAMETL_HOME"])
    app.runtime = RuntimeManager(app.app_home,
                                 on_log=lambda m: None)
    app.model_host = ""
    app.model_name = ""
    app._engine_ready = False
    app._loaded_info = []
    app.model_pref = "auto"
    # 多语言（v1.9）：_setup_ui 会读它来恢复上次选的语言
    app.target_lang = str(
        app.runtime._read_state().get("target_lang") or "简体中文")
    # 分栏记忆 / 关闭态（_setup_ui 会读）
    app._sash_saved = app._load_sash()
    app._closing = False
    app._pump_id = None
    # 一键汉化（v2.4.0）：翻译包仓库 + 独立任务线程
    app.pkg_thread = None
    app._repo_pkgs = []

    try:
        app._setup_ui()
    except Exception:
        print("[失败] _setup_ui 抛异常：")
        traceback.print_exc()
        return 1

    print("[OK] 界面构造成功")
    print(f"     档位按钮: {[k.value for k in app._profile_btns]}")
    print(f"     模型按钮: {list(app._model_btns.keys())}")

    if len(app._profile_btns) != len(PROFILE_ORDER):
        FAILED.append("档位按钮数量")
    if "auto" not in app._model_btns:
        FAILED.append("缺少「自动」模型按钮")
    # 有几个 gguf 就应该有 自动 + N 个按钮
    n_gguf = len(app.runtime.list_gguf())
    if len(app._model_btns) != n_gguf + 1:
        FAILED.append(f"模型按钮数量（期望 {n_gguf + 1}）")

    # 点一遍所有档位
    for k in PROFILE_ORDER:
        try:
            app._set_profile(k)
        except Exception:
            traceback.print_exc()
            FAILED.append(f"切档位 {k.value}")
    print("[OK] 四档切换正常")

    # 点一遍所有模型
    for pref in list(app._model_btns.keys()):
        try:
            app._set_model(pref)
        except Exception:
            traceback.print_exc()
            FAILED.append(f"切模型 {pref}")
    print("[OK] 模型切换正常")

    # 校验按钮存在
    for attr in ("start_btn", "cancel_btn", "audit_btn", "deep_btn",
                 "open_out_btn", "choose_btn", "progress", "log"):
        if not hasattr(app, attr):
            FAILED.append(f"缺少控件 {attr}")
    print("[OK] 关键控件齐全")

    # ---- 术语表（v2.6.2）：改成全自动，界面上**不许**再有手工按钮 ----
    if not hasattr(app, "gloss_hint"):
        FAILED.append("缺少术语表说明控件 gloss_hint")
    if hasattr(app, "gloss_btn"):
        FAILED.append("术语表按钮应当删掉（已改为翻译前自动生成）")
    if not hasattr(app, "build_glossary_clicked"):
        FAILED.append("缺少 build_glossary_clicked 方法（留作内部能力）")
    print("[OK] 术语表已改为全自动（无手工按钮）")

    # ---- 游戏库页的进度反馈（v2.6.2）：这一页也摆一份进度，
    #      v2.6.5 起点汉化还会自动切到「📊 进度与设置」看主进度 ----
    for attr in ("lib_stage", "lib_progress", "lib_status"):
        if not hasattr(app, attr):
            FAILED.append(f"缺少游戏库进度控件 {attr}")
    try:
        app._lib_task("测试中…", pct=42, kind="info")
        if app.lib_stage.cget("text") != "测试中…":
            FAILED.append("_lib_task 没把文案写到 lib_stage")
        if int(float(app.lib_progress.cget("value"))) != 42:
            FAILED.append("_lib_task 没把进度写到 lib_progress")
        app._lib_progress_reset()
    except Exception:
        traceback.print_exc()
        FAILED.append("_lib_task / _lib_progress_reset 抛异常")
    print("[OK] 游戏库页有醒目进度行 + 进度条")

    # ---- 一键汉化（v2.4.0）：卡片、按钮、刷新逻辑 ----
    for attr in ("repo_list", "repo_hint", "pkg_apply_btn", "pkg_revert_btn",
                 "pkg_add_btn", "pkg_open_btn"):
        if not hasattr(app, attr):
            FAILED.append(f"缺少「一键汉化」控件 {attr}")
    print("[OK] 一键汉化卡片控件齐全")

    try:
        app._refresh_repo()
        print(f"[OK] 翻译包仓库刷新正常（当前扫到 {len(app._repo_pkgs)} 个包）")
    except Exception:
        traceback.print_exc()
        FAILED.append("_refresh_repo 抛异常")

    # 没选游戏 + 没选包 → 「汉化到所选游戏」必须是禁用态
    if str(app.pkg_apply_btn.cget("state")) != "disabled":
        FAILED.append("没选游戏时「汉化到所选游戏」应为禁用")
    if str(app.pkg_revert_btn.cget("state")) != "disabled":
        FAILED.append("没装汉化时「还原原版」应为禁用")
    # 这两个不该依赖游戏/包，永远可用
    for attr in ("pkg_add_btn", "pkg_open_btn"):
        if str(getattr(app, attr).cget("state")) != "normal":
            FAILED.append(f"{attr} 应始终可用")
    print("[OK] 一键汉化按钮初始状态正确")

    # ---- 游戏库（v2.6.0）：主界面默认页 ----
    for attr in ("lib_tree", "lib_root_entry", "lib_scan_btn",
                 "lib_choose_btn", "lib_apply_btn", "lib_revert_btn",
                 "lib_open_btn", "lib_summary", "lib_status", "lib_progress",
                 "lib_detail", "lib_rescan_btn", "nb"):
        if not hasattr(app, attr):
            FAILED.append(f"缺少游戏库控件 {attr}")
    if not hasattr(app, "_lib_entries"):
        FAILED.append("缺少 _lib_entries")
    # 默认停在第 1 页（游戏库）—— 傻瓜式的前提
    try:
        if app.nb.index("current") != 0:
            FAILED.append("默认页不是「游戏库」")
        if "游戏库" not in str(app.nb.tab(0, "text")):
            FAILED.append("第 1 页标题不是「游戏库」")
        if len(app.nb.tabs()) < 2:
            FAILED.append("应该有两个页签（游戏库 / 进度与设置）")
        # v2.6.5：第 2 页不叫「高级设置」—— 用户点完汉化会被自动送进去看进度，
        # 一个叫「高级设置」的页签会让他以为自己点错了地方
        tabs = [str(app.nb.tab(t, "text")).strip() for t in app.nb.tabs()]
        if len(tabs) >= 2 and tabs[1] != "📊 进度与设置":
            FAILED.append(f"第 2 页标题应为「📊 进度与设置」，实际是「{tabs[1]}」")
    except Exception:
        traceback.print_exc()
        FAILED.append("读 Notebook 页失败")

    # ---- v2.6.5：点了汉化要把界面切到进度页 ----
    #      事故：翻译确实在跑，但进度条和日志都在第 2 页，第 1 页毫无动静，
    #      用户以为按钮坏了。
    if not hasattr(app, "_goto_progress"):
        FAILED.append("缺少 _goto_progress 方法")
    else:
        try:
            app.nb.select(0)
            app._goto_progress()
            if int(app.nb.index("current")) != 1:
                FAILED.append(
                    f"_goto_progress 没把界面切到第 2 页（当前第 "
                    f"{int(app.nb.index('current')) + 1} 页）")
            app.nb.select(0)
        except Exception:
            traceback.print_exc()
            FAILED.append("_goto_progress 抛异常")
    print("[OK] 点汉化会自动切到「进度与设置」页")
    # 一个游戏都没选 → 三个操作按钮都该是禁用
    for attr in ("lib_apply_btn", "lib_revert_btn", "lib_open_btn"):
        if str(getattr(app, attr).cget("state")) != "disabled":
            FAILED.append(f"没选游戏时 {attr} 应为禁用")
    print("[OK] 游戏库控件齐全（默认页 = 游戏库）")

    app._set_profile(Profile.TURBO)
    app._set_model("auto")
    print()
    print("  档位说明：")
    print("   " + app.profile_detail.cget("text").replace("\n", "\n   "))
    print("  模型说明：")
    print("   " + app.model_detail.cget("text").replace("\n", "\n   "))

    root.after(10, root.destroy)
    root.mainloop()

    if FAILED:
        print(f"\n[失败] {len(FAILED)} 项：{', '.join(FAILED)}")
        return 1
    print("\n[OK] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
