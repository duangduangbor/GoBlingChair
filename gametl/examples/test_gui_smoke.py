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

    from gametl.runtime_manager import RuntimeManager

    app.game_dir = None
    app.worker = None
    app.cancel_event = threading.Event()
    app.log_queue = queue.Queue()
    app.hw = None
    app._t0 = 0.0
    app.app_home = Path(os.environ["GAMETL_HOME"])
    app.runtime = RuntimeManager(app.app_home,
                                 on_log=lambda m: None)
    app.model_host = ""
    app.model_name = ""
    app._engine_ready = False
    app.model_pref = "auto"

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
