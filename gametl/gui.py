# -*- coding: utf-8 -*-
"""滚刀哥布林汉化椅 —— 图形界面主程序。

面向非技术用户：选文件夹 → 选档位 → 点开始 → 自动完成 → 想复核就点校对。

界面布局::

    ┌──────────────────────────────────────────────────┐
    │  滚刀哥布林汉化椅                                  │
    │  ──────────────────────────────────────────────  │
    │  🟢 翻译引擎就绪（端口 11435）· 模型 gametl-model  │
    │  ┌── 游戏文件夹 ──────────────────────────────┐  │
    │  │ [____________________] [选择文件夹]        │  │
    │  │ 识别引擎：RPG Maker MV / MZ · 未发现资源包  │  │
    │  └────────────────────────────────────────────┘  │
    │  ┌── 性能档位 ────────────────────────────────┐  │
    │  │ [🐢兼容] [🟢普通] [🔵强效] [🔴狂暴]         │  │
    │  │ RTX 3060 Ti · 显存 8.0 GB → 推荐：强效      │  │
    │  │ 并发 4 路 · 每批 8 条 · 预计占用 6.5 GB     │  │
    │  └────────────────────────────────────────────┘  │
    │  [▶ 开始汉化] [取消]  [校对] [深度校对] [输出]    │
    │  正在翻译... 420/77401      12.3 条/秒 · 剩 1.7h │
    │  [████████░░░░░░░░░░░░░░]  1%                    │
    │  运行日志                                         │
    │  ┌────────────────────────────────────────────┐  │
    │  └────────────────────────────────────────────┘  │
    └──────────────────────────────────────────────────┘
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
import queue
import subprocess
import time
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# 让脚本在打包后也能找到 gametl 包
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys._MEIPASS)  # PyInstaller 解压目录
    APP_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    APP_DIR = BASE_DIR

sys.path.insert(0, str(BASE_DIR))

from gametl.auto import (  # noqa: E402
    AutoConfig, AutoPipeline, Cancelled, export_full_game,
    options_from_profile,
)
from gametl.core.archive import detect_archives  # noqa: E402
from gametl.core.detect import detect_engine  # noqa: E402
from gametl.core.models import Project  # noqa: E402
from gametl.core.scan import ScanStats  # noqa: E402
from gametl.profiles import (  # noqa: E402
    PROFILE_ORDER, PROFILES, Profile, detect_hardware, is_translation_model,
    model_label, pick_model, recommend_profile,
)
from gametl.runtime_manager import RuntimeManager  # noqa: E402
from gametl.ui_common import (  # noqa: E402
    ButtonRow, VScroll, clamp_sash, fit_window, install_thread_hook,
    read_prefs, sash_target, wrap_to_parent, write_prefs,
)

#: 左右分栏的权重。**左栏不能是 0** —— ttk 只按权重分配空间，weight=0 的
#: 栏会永远停在「首次布局时的宽度」，而首次布局发生在窗口还没映射、子控件
#: 还没塞进去的时候，于是它停在 1px。表现出来就是「打开只有日志，设置区
#: 得自己拖出来」。4:6 约等于 40% / 60%。
PANE_WEIGHT_LEFT = 4
PANE_WEIGHT_RIGHT = 6
#: 窗口还没映射时 sashpos() 会抛错，所以要重试到「读回来的位置 == 目标」
SASH_RETRY_MS = 40
SASH_RETRY_MAX = 30

APP_NAME = "滚刀哥布林汉化椅"
APP_NAME_EN = "GoBlingChair"
APP_VERSION = "2.1.0"
INSTALLER_NAME = "汉化安装器.exe"

# LunaTranslator 运行时翻译（路线2整合）：候选安装路径（按顺序探测）。
# 首选 = 汉化椅目录内的便携副本（一体化结构）；其余为外置安装位置的兜底。
LUNA_PATHS = (
    "D:/滚刀哥布林汉化椅/LunaTranslator/LunaTranslator_x64/LunaTranslator.exe",
    "E:/Tools/LunaTranslator/LunaTranslator_x64/LunaTranslator.exe",
    "C:/Tools/LunaTranslator/LunaTranslator_x64/LunaTranslator.exe",
)

# 目标语言预置选项（多语言翻译）。(code, 显示名)
# code 既作按钮标识，也直接作为传给模型的 target_lang 值。
LANG_CHOICES = [
    ("简体中文", "简体中文"),
    ("繁體中文", "繁體中文"),
    ("English", "English"),
    ("日本語", "日本語"),
    ("한국어", "한국어"),
]

# 图标候选路径（打包后在 _MEIPASS/assets/，源码运行时在项目 assets/）
ICON_RELATIVE = ("assets/GoBlingChair.ico", "GoBlingChair.ico",
                 "assets/滚刀哥.ico", "滚刀哥.ico")


def _apply_icon(window) -> None:
    """给窗口设置标题栏/任务栏图标；找不到文件就静默跳过。"""
    for base in (BASE_DIR, APP_DIR):
        for name in ICON_RELATIVE:
            p = base / name
            if p.exists():
                try:
                    window.iconbitmap(default=str(p))
                    return
                except Exception:  # noqa: BLE001
                    pass


# 配色（浅色主题）
BG = "#f5f6f8"
CARD = "#ffffff"
ACCENT = "#2d7ff9"
ACCENT_DARK = "#1a6ae0"
TEXT = "#1f2328"
MUTED = "#6b7280"
OK_GREEN = "#16a34a"
WARN = "#d97706"
WARN_AMBER = "#b45309"
ERR_RED = "#dc2626"
IDLE_BTN = "#eef1f5"
IDLE_BTN_HOVER = "#e0e5ec"

ENGINE_NAMES = {
    "kirikiri": "KiriKiri（吉里吉里）",
    "rpgmaker_mv": "RPG Maker MV / MZ",
    "renpy": "Ren'Py",
    "unity": "Unity",
    "unknown": "未识别（可能是已解包目录）",
}

# 速度显示的滑动窗口长度（秒）。
#
# 为什么不用「累计完成 / 累计耗时」：那个数在数学上**必然**从开场值单调
# 收敛 —— 引擎冷启动（把模型加载进显存）那几十秒会被永久摊进分母，于是
# 前几分钟的读数低得离谱。实测同一轮里，界面显示 6.0 条/秒，而服务日志
# 逐秒统计是 8~11 个请求/秒。用户会因此以为「越跑越慢」，其实没有。
RATE_WINDOW_SEC = 8.0

# 导出成品时自动建出来的子文件夹后缀。
# 用户选的是「放哪儿」，软件负责建「<原游戏名>中文版」并把整份游戏放进去 ——
# 不然一堆文件散在用户选的目录里，想整个拷走或者删掉都很麻烦。
EXPORT_SUFFIX = "中文版"


def safe_name(name: str) -> str:
    """清掉文件名里的 Windows 非法字符与首尾空白/点。"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "")
    cleaned = cleaned.strip().strip(".")
    return cleaned or "游戏"


def export_target_for(game_name: str, parent: Path) -> Path:
    """由用户选的父目录，算出真正的导出文件夹。

    规则：``<父目录>/<原游戏名>中文版``。

    两种例外：
    * 用户**直接选中了**那个「…中文版」文件夹 —— 就地用，不再套一层，
      免得出现「XX中文版\\XX中文版」；
    * 游戏名里带非法字符 —— 替换掉再拼。
    """
    want = safe_name(game_name) + EXPORT_SUFFIX
    if parent.name == want:
        return parent
    return parent / want


def window_rate(hist: deque, now: float, cur: int,
                window: float = RATE_WINDOW_SEC) -> float:
    """最近 ``window`` 秒的瞬时吞吐（条/秒）。样本不足返回 0。

    取窗口内**最早的那个样本**做基准，所以窗口一满就自动向前滑动，读数反映
    的是「眼下这一会儿有多快」，而不是开跑以来的平均。
    """
    while len(hist) > 1 and now - hist[0][0] > window:
        hist.popleft()
    if len(hist) < 2:
        return 0.0
    t0, c0 = hist[0]
    dt = now - t0
    if dt < 1.0:                     # 窗口太短，读数会乱跳
        return 0.0
    return max(0.0, (cur - c0) / dt)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.game_dir: Path | None = None
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.log_queue: queue.Queue = queue.Queue()
        self.hw = None
        self._t0: float = 0.0                 # 整条流水线的起点
        self._tr_t0: float = 0.0              # 翻译阶段起点（速度只看这一段）
        self._rate_hist: deque = deque(maxlen=64)   # [(时刻, 已完成条数)]
        self._last_err_ui: str = ""                # 界面异常的弹窗去重

        # 翻译引擎（内置便携运行时；可用 GAMETL_HOME 覆盖软件根目录）
        self.app_home = Path(os.environ.get("GAMETL_HOME", APP_DIR))
        self.runtime = RuntimeManager(
            self.app_home, on_log=lambda m: self._log(m, "info"))
        self.model_host = ""
        self.model_name = ""
        self._engine_ready = False
        self._loaded_info: list = []      # /api/ps 回读的引擎实况
        # 用户选定的模型（auto / quality / speed / 具体文件名）。
        # 初值来自运行时持久化的选择 —— 界面和引擎必须从同一个值出发，
        # 否则一开软件就会「界面高亮自动、引擎其实在跑上次选的模型」。
        self.model_pref: str = self.runtime.model_pref or "auto"

        # 目标语言（多语言翻译）。默认简体中文，可从持久化状态恢复。
        self.target_lang: str = str(
            self.runtime._read_state().get("target_lang") or "简体中文")

        # 上次拖到的左右分栏位置（没有就 None，用默认比例）
        self._sash_saved = self._load_sash()
        self._closing = False                 # 关窗后别再续 after 链
        self._pump_id = None
        self._setup_ui()
        self._pump_log()

        # 界面回调里抛的异常必须先被「看见」。默认行为是写 stderr ——
        # 打包成窗口程序后没有控制台，异常就此人间蒸发，用户点了按钮
        # 毫无反应，只会觉得「这按钮是坏的」。这里改成：进日志 + 弹窗。
        root.report_callback_exception = self._on_tk_error

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        threading.Thread(target=self._prepare_engine, daemon=True).start()
        threading.Thread(target=self._detect_hw, daemon=True).start()

    # ---------------- 翻译引擎准备 ----------------

    def _prepare_engine(self):
        """后台启动内置翻译服务并导入模型（首次较慢）。"""
        try:
            ok, host, model, msg = self.runtime.prepare()
        except Exception as e:  # noqa: BLE001
            ok, host, model, msg = False, "", "", f"引擎准备异常：{e}"
        self.root.after(0, self._engine_done, ok, host, model, msg)

    def _engine_done(self, ok: bool, host: str, model: str, msg: str):
        if ok:
            self.model_host = host
            self.model_name = model
            self._engine_ready = True
            port = host.rsplit(":", 1)[-1] if host else "?"
            self.engine_var.set(f"🟢 翻译引擎就绪（端口 {port}）· 模型 {model}")
            self.engine_label.configure(fg=OK_GREEN)
            self._log(msg, "ok")
            # ★ 引擎就绪 ≠ 界面说的就对。必须回头问一句「你到底加载了哪个模型」，
            #   否则会出现「界面写 1.8B、实际跑 7B」这种静默错位（实测踩过，
            #   速度差三四倍，而且用户完全看不出来）。
            self._sync_engine_model()
        else:
            self.engine_var.set("🔴 翻译引擎未就绪 — 详见下方日志")
            self.engine_label.configure(fg=ERR_RED)
            for line in msg.splitlines():
                if line.strip():
                    self._log(line, "err")

    def _sync_engine_model(self):
        """把「引擎实际加载的模型」回读出来，作为唯一可信的事实。

        两条独立线索互相印证：
          · ``tag_source()`` 读 Ollama 清单里 ``from`` 字段 —— 注册时写死的，
            能证明这个标签是从哪个 .gguf 建的；
          · ``loaded_info()`` 读 ``/api/ps`` —— 能证明此刻显存里是哪一档
            参数量、多大、上下文多长。
        """
        try:
            src = self.runtime.tag_source()
        except Exception:  # noqa: BLE001
            src = ""
        if src and src != self.runtime.model_file:
            self._log(f"[警告] 引擎实际加载的是 {src}，"
                      f"与所选 {self.runtime.model_file or '（未选）'} 不一致；"
                      f"已按实际加载的模型显示", "warn")
            self.runtime.model_file = src
        try:
            info = self.runtime.loaded_info()
        except Exception:  # noqa: BLE001
            info = []
        self._loaded_info = info
        if info:
            m = info[0]
            tip = (f"　·　显存内 {m['params']} {m['quant']} "
                   f"{m['vram_gb']:.2f} GB · 上下文 {m['ctx']}")
            self.engine_var.set(self.engine_var.get() + tip)
            offload = self._offload_tip(m)
            if offload:
                self._log(offload, "warn")
        try:
            self._render_model_detail()
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _offload_tip(m: dict) -> str:
        """模型没全量上显卡时的提醒。返回空串表示一切正常。

        Ollama 在显存不够时不会报错，它会**悄悄**把一部分层留在内存里跑 ——
        每个 token 都要跨 PCIe 同步一次，速度能掉一半以上。日志里的表现是::

            load_tensors: offloaded 28/33 layers to GPU
            load_tensors:    CUDA_Host model buffer size = 336.44 MiB

        用户看不到这些日志，只会觉得「怎么这么慢」。所以这里主动说出来。
        """
        total = float(m.get("size_gb") or 0)
        vram = float(m.get("vram_gb") or 0)
        if total <= 0 or vram <= 0 or vram >= total * 0.97:
            return ""
        return (f"⚠ 显存不够，模型只有约 {vram / total * 100:.0f}% 在显卡上"
                f"（{vram:.2f} / {total:.2f} GB），其余部分在内存里跑，"
                f"速度会明显变慢。\n"
                f"   关掉一些吃显存的程序（浏览器、微信、远程串流/投屏），"
                f"或者换用更小的模型后重启软件，通常能快一倍以上。")

    # ---------------- 硬件检测 ----------------

    def _detect_hw(self):
        try:
            hw = detect_hardware()
        except Exception:  # noqa: BLE001
            hw = None
        self.root.after(0, self._hw_done, hw)

    def _hw_done(self, hw):
        self.hw = hw
        if hw is None:
            self._render_profile_detail(self.profile_var.get() or "turbo")
            return
        rec = recommend_profile(hw)
        self._log(f"硬件检测：{hw.summary()}", "info")
        self._log(f"推荐性能档位：{PROFILES[rec].emoji} {PROFILES[rec].name}", "info")
        self.profile_var.set("")          # 触发重绘
        self._set_profile(rec)

    # ---------------- 收尾 ----------------

    def on_close(self):
        # 0) 先把日志泵的 after 链掐掉，否则窗口销毁后 Tk 还会去调它
        self._closing = True
        if self._pump_id is not None:
            try:
                self.root.after_cancel(self._pump_id)
            except Exception:  # noqa: BLE001
                pass
            self._pump_id = None
        # 1) 先停翻译引擎，否则残留的 ollama.exe 会占住文件导致临时目录删不掉
        try:
            self.runtime.shutdown()
        except Exception:  # noqa: BLE001
            pass
        # 2) 收起主循环并销毁窗口，让 tcl/tk 交出 DLL 句柄
        for step in (self.root.quit, self.root.destroy):
            try:
                step()
            except Exception:  # noqa: BLE001
                pass
        # 3) 触发回收并稍作停顿，提高 bootloader 清掉 _MEI 的成功率
        try:
            import gc
            gc.collect()
            time.sleep(0.4)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 界面构建 ----------------

    def _setup_ui(self):
        r = self.root
        r.title(f"{APP_NAME_EN} · {APP_NAME} v{APP_VERSION}")
        _apply_icon(r)
        r.configure(bg=BG)
        # 尺寸按屏幕算：14 寸笔记本（尤其 150% 缩放 → 逻辑 1280×720）
        # 也保证整个界面连日志都装得下。
        fit_window(r, 1180, 840, min_w=880, min_h=560)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT,
                        font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", background=BG, foreground=TEXT,
                        font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED,
                        font=("Microsoft YaHei UI", 9))
        style.configure("TLabelframe", background=BG)
        style.configure("TLabelframe.Label", background=BG, foreground=MUTED,
                        font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Horizontal.TProgressbar", troughcolor="#e5e7eb",
                        background=ACCENT, thickness=18)
        style.configure("TPanedwindow", background=BG)
        style.configure("Sash", sashthickness=7, gripcount=0, background=BG)

        # ---- 标题 ----
        head = ttk.Frame(r)
        head.pack(fill="x", padx=18, pady=(14, 0))
        hleft = ttk.Frame(head)
        hleft.pack(side="left", anchor="w")

        # 头像（Q版像素风）；找不到就静默跳过，不挡界面。
        # 用 tk.PhotoImage 直接加载 PNG（不依赖被 excludes 掉的 PIL）。
        self.avatar_photo = None
        for base in (BASE_DIR, APP_DIR):
            for name in ("assets/GoBlingChair_128.png",
                         "assets/GoBlingChair.png"):
                p = base / name
                if p.exists():
                    try:
                        self.avatar_photo = tk.PhotoImage(file=str(p))
                    except Exception:  # noqa: BLE001
                        self.avatar_photo = None
                    break
            if self.avatar_photo is not None:
                break

        title_row = ttk.Frame(hleft)
        title_row.pack(anchor="w")
        if self.avatar_photo is not None:
            # 128px 头像缩到约 42px 作标题图标（subsample 返回新对象，需保引用）
            try:
                av = self.avatar_photo.subsample(3, 3)
            except Exception:  # noqa: BLE001
                av = self.avatar_photo
            self.avatar_small = av
            tk.Label(title_row, image=self.avatar_small, bg=BG,
                     borderwidth=0).pack(side="left", padx=(0, 10))
        tcol = ttk.Frame(title_row)
        tcol.pack(side="left", anchor="w")
        ttk.Label(tcol, text=f"{APP_NAME}",
                  style="Title.TLabel").pack(anchor="w")
        ttk.Label(tcol, text=f"{APP_NAME_EN}  ·  "
                             "选择游戏文件夹，一键完成汉化补丁制作",
                  style="Sub.TLabel").pack(anchor="w", pady=(2, 0))

        # 引擎状态放右上角：它只是一行提示，不该独占一整行把日志往下挤
        self.engine_var = tk.StringVar(value="⏳ 正在准备翻译引擎…")
        self.engine_label = tk.Label(
            head, textvariable=self.engine_var, bg=BG, fg=MUTED,
            font=("Microsoft YaHei UI", 9), anchor="e", justify="right",
            wraplength=560)
        self.engine_label.pack(side="right", anchor="ne", pady=(4, 0))

        # ---- 底部操作栏 ----
        # 先 pack side="bottom"：主按钮和进度条**永远**留在窗口底部，
        # 不管上面塞了多少卡片都不会把它顶出可视区。
        bar = ttk.Frame(r)
        bar.pack(side="bottom", fill="x", padx=18, pady=(0, 14))

        self.start_btn = tk.Button(
            bar, text="▶  开始汉化", command=self.start,
            bg=OK_GREEN, fg="white", activebackground="#128a3e",
            activeforeground="white", relief="flat", cursor="hand2",
            font=("Microsoft YaHei UI", 13, "bold"), padx=26, pady=9,
            state="disabled")
        self.start_btn.pack(side="left")
        self.cancel_btn = tk.Button(
            bar, text="取消", command=self.cancel,
            bg="#e5e7eb", fg=TEXT, activebackground="#d1d5db",
            relief="flat", cursor="hand2",
            font=("Microsoft YaHei UI", 10), padx=16, pady=9,
            state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))

        self.open_out_btn = tk.Button(
            bar, text="打开输出目录", command=self.open_output,
            bg="#e5e7eb", fg=TEXT, activebackground="#d1d5db",
            relief="flat", cursor="hand2",
            font=("Microsoft YaHei UI", 10), padx=14, pady=9,
            state="disabled")
        self.open_out_btn.pack(side="right")

        # ---- 中间：左右分栏（左设置 / 右日志，可拖动） ----
        # 两栏都给非 0 权重：这是「打开就只见日志、设置区得手动拖出来」
        # 的根治办法（见文件头 PANE_WEIGHT_LEFT 的注释）。
        pane = ttk.PanedWindow(r, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=18, pady=(10, 4))
        self._pane = pane

        lholder = ttk.Frame(pane)
        pane.add(lholder, weight=PANE_WEIGHT_LEFT)
        self.settings = VScroll(lholder, bg=BG)
        self.settings.pack(fill="both", expand=True)
        col = self.settings.body

        # ---- 卡片 1：游戏文件夹 ----
        card = tk.Frame(col, bg=CARD, highlightbackground="#e5e7eb",
                        highlightthickness=1)
        card.pack(fill="x", pady=(0, 8))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=16, pady=12)

        tk.Label(inner, text="游戏文件夹", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        row = tk.Frame(inner, bg=CARD)
        row.pack(fill="x", pady=(6, 0))
        self.path_var = tk.StringVar(value="（尚未选择）")
        self.path_entry = tk.Entry(row, textvariable=self.path_var,
                                   font=("Microsoft YaHei UI", 10),
                                   state="readonly", bd=1, relief="solid",
                                   readonlybackground="#f9fafb")
        self.path_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.choose_btn = tk.Button(
            row, text="选择文件夹", command=self.choose_dir,
            bg=ACCENT, fg="white", activebackground=ACCENT_DARK,
            activeforeground="white", relief="flat", cursor="hand2",
            font=("Microsoft YaHei UI", 10, "bold"), padx=14, pady=6)
        self.choose_btn.pack(side="left", padx=(8, 0))

        self.detect_label = tk.Label(
            inner, text="", bg=CARD, fg=MUTED,
            font=("Microsoft YaHei UI", 9), anchor="w", justify="left")
        self.detect_label.pack(fill="x", pady=(8, 0))
        wrap_to_parent(self.detect_label, inner)

        # 这个游戏**上次翻到哪了** —— 让「已经翻完」这件事在界面上一眼可见，
        # 而不是让用户看着进度条重新跑一遍再来判断程序是不是在做无用功。
        self.state_label = tk.Label(
            inner, text="", bg=CARD, fg=MUTED,
            font=("Microsoft YaHei UI", 9), anchor="w", justify="left")
        self.state_label.pack(fill="x", pady=(2, 0))
        wrap_to_parent(self.state_label, inner)
        self._project_state: dict = {}
        # 待导入的翻译包（用户在「导入翻译包」里选好，下一次开跑时消费掉）
        self._import_pkg = None

        # ---- 卡片 2：性能档位 ----
        card2 = tk.Frame(col, bg=CARD, highlightbackground="#e5e7eb",
                         highlightthickness=1)
        card2.pack(fill="x", pady=(0, 8))
        inner2 = tk.Frame(card2, bg=CARD)
        inner2.pack(fill="x", padx=16, pady=12)

        tk.Label(inner2, text="性能档位", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")

        self.profile_var = tk.StringVar(value="turbo")
        self._profile_btns: dict = {}
        p_row = ButtonRow(inner2, bg=CARD)
        p_row.pack(fill="x", pady=(6, 0))
        for key in PROFILE_ORDER:
            spec = PROFILES[key]
            b = tk.Button(
                p_row, text=f"{spec.emoji} {spec.name}",
                command=lambda k=key: self._set_profile(k),
                relief="flat", cursor="hand2", bd=0,
                font=("Microsoft YaHei UI", 10), padx=12, pady=7,
                bg=IDLE_BTN, fg=TEXT,
                activebackground=IDLE_BTN_HOVER, activeforeground=TEXT)
            p_row.add(b)
            self._profile_btns[key] = b

        self.profile_detail = tk.Label(
            inner2, text="", bg=CARD, fg=MUTED,
            font=("Microsoft YaHei UI", 9), anchor="w", justify="left")
        self.profile_detail.pack(fill="x", pady=(8, 0))
        wrap_to_parent(self.profile_detail, inner2)
        self._set_profile(Profile.TURBO)

        # ---- 卡片 3：翻译模型（与档位解耦，可单独选） ----
        card3 = tk.Frame(col, bg=CARD, highlightbackground="#e5e7eb",
                         highlightthickness=1)
        card3.pack(fill="x", pady=(0, 8))
        inner3 = tk.Frame(card3, bg=CARD)
        inner3.pack(fill="x", padx=16, pady=12)

        tk.Label(inner3, text="翻译模型", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")

        self._model_btns: dict = {}
        self.model_row = ButtonRow(inner3, bg=CARD)
        self.model_row.pack(fill="x", pady=(6, 0))

        self.model_detail = tk.Label(
            inner3, text="", bg=CARD, fg=MUTED,
            font=("Microsoft YaHei UI", 9), anchor="w", justify="left")
        self.model_detail.pack(fill="x", pady=(8, 0))
        wrap_to_parent(self.model_detail, inner3)
        self._build_model_buttons()

        # 自定义模型：从任意位置选一个 .gguf 复制进 models\
        self.pick_model_btn = tk.Button(
            inner3, text="📂 选择模型文件…", command=self.pick_model_file,
            bg="#f3f4f6", fg="#374151", activebackground="#e5e7eb",
            relief="flat", cursor="hand2",
            font=("Microsoft YaHei UI", 9), padx=12, pady=6)
        self.pick_model_btn.pack(fill="x", pady=(8, 0))

        # ---- 卡片 3.5：目标语言（多语言翻译） ----
        card35 = tk.Frame(col, bg=CARD, highlightbackground="#e5e7eb",
                          highlightthickness=1)
        card35.pack(fill="x", pady=(0, 8))
        inner35 = tk.Frame(card35, bg=CARD)
        inner35.pack(fill="x", padx=16, pady=12)

        tk.Label(inner35, text="目标语言", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")

        self.lang_row = ButtonRow(inner35, bg=CARD)
        self.lang_row.pack(fill="x", pady=(6, 0))
        self._lang_btns: dict = {}
        for code, label in LANG_CHOICES:
            b = tk.Button(
                self.lang_row, text=label,
                command=lambda c=code: self._set_lang(c),
                relief="flat", cursor="hand2", bd=0,
                font=("Microsoft YaHei UI", 10), padx=12, pady=7,
                bg=IDLE_BTN, fg=TEXT,
                activebackground=IDLE_BTN_HOVER, activeforeground=TEXT)
            self.lang_row.add(b)
            self._lang_btns[code] = b

        self.custom_lang_var = tk.StringVar()
        self.custom_lang_entry = tk.Entry(
            inner35, textvariable=self.custom_lang_var,
            font=("Microsoft YaHei UI", 10), relief="solid", bd=1)
        self.custom_lang_entry.pack(fill="x", pady=(8, 0))
        self.custom_lang_entry.bind(
            "<Return>", lambda e: self._apply_custom_lang())
        self.custom_lang_entry.bind(
            "<FocusOut>", lambda e: self._apply_custom_lang())
        self.lang_hint = tk.Label(
            inner35, text="", bg=CARD, fg=MUTED,
            font=("Microsoft YaHei UI", 8), anchor="w", justify="left")
        self.lang_hint.pack(fill="x", pady=(6, 0))
        wrap_to_parent(self.lang_hint, inner35)
        self._set_lang(self.target_lang)

        # ---- 卡片 4：校对与交付 ----
        card4 = tk.Frame(col, bg=CARD, highlightbackground="#e5e7eb",
                         highlightthickness=1)
        card4.pack(fill="x", pady=(0, 8))
        inner4 = tk.Frame(card4, bg=CARD)
        inner4.pack(fill="x", padx=16, pady=12)

        tk.Label(inner4, text="校对与交付", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")

        r1 = ButtonRow(inner4, bg=CARD)
        r1.pack(fill="x", pady=(6, 0))
        self.audit_btn = tk.Button(
            r1, text="快速校对", command=lambda: self.run_audit(deep=False),
            bg="#dbeafe", fg="#1e40af", activebackground="#bfdbfe",
            relief="flat", cursor="hand2",
            font=("Microsoft YaHei UI", 10), padx=14, pady=8)
        r1.add(self.audit_btn)
        self.deep_btn = tk.Button(
            r1, text="深度校对", command=lambda: self.run_audit(deep=True),
            bg="#fef3c7", fg="#92400e", activebackground="#fde68a",
            relief="flat", cursor="hand2",
            font=("Microsoft YaHei UI", 10), padx=14, pady=8)
        r1.add(self.deep_btn)

        r2 = tk.Frame(inner4, bg=CARD)
        r2.pack(fill="x", pady=(6, 0))
        self.export_btn = tk.Button(
            r2, text="导出完整版", command=self.export_game,
            bg="#dcfce7", fg="#166534", activebackground="#bbf7d0",
            relief="flat", cursor="hand2",
            font=("Microsoft YaHei UI", 10), padx=14, pady=8,
            state="disabled")
        self.export_btn.pack(side="left")
        self.pkg_in_btn = tk.Button(
            r2, text="导入翻译包", command=self.import_pkg,
            bg="#ede9fe", fg="#5b21b6", activebackground="#ddd6fe",
            relief="flat", cursor="hand2",
            font=("Microsoft YaHei UI", 10), padx=14, pady=8)
        self.pkg_in_btn.pack(side="left", padx=(6, 0))

        r3 = tk.Frame(inner4, bg=CARD)
        r3.pack(fill="x", pady=(6, 0))
        self.pkg_out_btn = tk.Button(
            r3, text="📦 导出汉化补丁包（可直接发给别人）",
            command=self.export_pkg,
            bg="#dbeafe", fg="#1e40af", activebackground="#bfdbfe",
            relief="flat", cursor="hand2", anchor="w",
            font=("Microsoft YaHei UI", 10, "bold"), padx=14, pady=9,
            state="disabled")
        self.pkg_out_btn.pack(fill="x")

        self.pkg_hint = tk.Label(
            inner4,
            text="补丁包里带着独立的安装器：对方解压到游戏目录、双击即可，\n"
                 "不用装模型也不用联网；装完想换回日文，再点一下还原。",
            bg=CARD, fg=MUTED, justify="left", anchor="w",
            font=("Microsoft YaHei UI", 8))
        self.pkg_hint.pack(fill="x", pady=(6, 0))
        wrap_to_parent(self.pkg_hint, inner4)

        # ---- 卡片 5：运行时翻译（整合 LunaTranslator） ----
        card5 = tk.Frame(col, bg=CARD, highlightbackground="#e5e7eb",
                         highlightthickness=1)
        card5.pack(fill="x", pady=(0, 8))
        inner5 = tk.Frame(card5, bg=CARD)
        inner5.pack(fill="x", padx=16, pady=12)

        tk.Label(inner5, text="运行时翻译（难搞游戏救星）",
                 bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")

        self.luna_btn = tk.Button(
            inner5, text="🚀 启动运行时翻译",
            command=self.launch_runtime_translate,
            bg="#ede9fe", fg="#5b21b6", activebackground="#ddd6fe",
            relief="flat", cursor="hand2", anchor="w",
            font=("Microsoft YaHei UI", 10, "bold"), padx=14, pady=9)
        self.luna_btn.pack(fill="x", pady=(6, 0))

        self.luna_hint = tk.Label(
            inner5,
            text="对魔改 XP3、自研引擎、cocos2d-x 等「静态提取不了」的游戏，\n"
                 "用 hook 方式直接翻译游戏画面文字，无需解包。启动后点\n"
                 "「绑定窗口」选游戏即可，翻译用的是本机内置模型，不联网。\n"
                 "实时翻译组件：LunaTranslator（GPLv3 开源项目）。",
            bg=CARD, fg=MUTED, justify="left", anchor="w",
            font=("Microsoft YaHei UI", 8))
        self.luna_hint.pack(fill="x", pady=(6, 0))
        wrap_to_parent(self.luna_hint, inner5)

        # ---- 右栏：进度 + 日志（日志永远是这里的主角） ----
        rholder = ttk.Frame(pane)
        pane.add(rholder, weight=PANE_WEIGHT_RIGHT)

        prog = tk.Frame(rholder, bg=BG)
        prog.pack(fill="x", pady=(0, 2))
        st_row = tk.Frame(prog, bg=BG)
        st_row.pack(fill="x")
        self.stage_var = tk.StringVar(value="就绪")
        tk.Label(st_row, textvariable=self.stage_var, bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", 10, "bold"),
                 anchor="w").pack(side="left")
        self.speed_var = tk.StringVar(value="")
        tk.Label(st_row, textvariable=self.speed_var, bg=BG, fg=ACCENT,
                 font=("Microsoft YaHei UI", 10), anchor="e").pack(side="right")

        self.progress = ttk.Progressbar(prog, style="Horizontal.TProgressbar",
                                        maximum=100, value=0)
        self.progress.pack(fill="x", pady=(6, 0))

        logf = ttk.LabelFrame(rholder, text="运行日志")
        logf.pack(fill="both", expand=True, pady=(8, 0))
        self.log = scrolledtext.ScrolledText(
            logf, height=8, wrap="word",
            font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="#d4d4d4", relief="flat", bd=0)
        self.log.pack(fill="both", expand=True, padx=6, pady=6)
        self.log.tag_config("info", foreground="#d4d4d4")
        self.log.tag_config("ok", foreground="#4ade80")
        self.log.tag_config("warn", foreground="#fbbf24")
        self.log.tag_config("err", foreground="#f87171")
        self.log.tag_config("time", foreground="#6b7280")
        self.log.configure(state="disabled")

        # 分栏落位：窗口映射后才有真实宽度，所以要重试（见 _place_sash）
        self._sash_locked = False         # 落位成功后就不再自动动它
        pane.bind("<ButtonRelease-1>", self._on_sash_release, add="+")
        pane.bind("<Map>", lambda _e: self.root.after(SASH_RETRY_MS,
                                                      self._place_sash), add="+")
        r.after(SASH_RETRY_MS, self._place_sash)

        self._log("欢迎使用！先选游戏文件夹，再挑性能档位和翻译模型。", "info")
        self._log("档位越高越快、越吃显存；模型决定速度与质量的取舍。", "info")

    # ---------------- 分栏（左设置 / 右日志） ----------------

    def _sash_prefs_path(self) -> Path:
        return Path(self.app_home) / "data" / "ui_prefs.json"

    def _load_sash(self):
        """读上次拖到的分隔条位置；没有 / 坏了都返回 None（走默认比例）。"""
        v = read_prefs(self._sash_prefs_path()).get("sash")
        try:
            v = int(v)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    def _save_sash(self, pos: int) -> None:
        prefs = read_prefs(self._sash_prefs_path())
        prefs["sash"] = int(pos)
        write_prefs(self._sash_prefs_path(), prefs)

    def _place_sash(self, tries: int = 0) -> None:
        """把分隔条落到设计位置（左栏有内容、右栏留得住日志）。

        这里**必须重试**，只设一次是不够的：窗口还没映射时
        ``sashpos()`` 会抛 ``TclError``，而布局宽度只有在窗口映射之后
        才算得出来。旧版就是「设一次 + 把异常吞掉」，于是永远停在失效
        的那一次，左栏宽度 0 且再无补救 —— 用户只能自己拖。
        """
        if getattr(self, "_sash_locked", False):
            return
        try:
            pw = self._pane.winfo_width()
        except tk.TclError:
            return
        if pw > 1:
            want = sash_target(pw, getattr(self, "_sash_saved", None))
            if want > 0:
                try:
                    self._pane.sashpos(0, want)
                    # 设完读回来核对：ttk 可能做了取整或拒绝
                    if abs(int(self._pane.sashpos(0)) - want) <= 2:
                        self._sash_locked = True
                        return
                except tk.TclError:
                    pass
        if tries < SASH_RETRY_MAX:
            try:
                self.root.after(SASH_RETRY_MS,
                                lambda: self._place_sash(tries + 1))
            except tk.TclError:
                pass

    def _on_sash_release(self, _e=None) -> None:
        """用户松手：夹一下宽度，并记住这个位置。

        ttk 的分栏只认 weight，没有 minsize —— 不夹的话用户能把某一栏
        拖成 1px，而且拖没了就没法用鼠标拖回来。拖动是右栏日志「看得见」
        的保证，不该被一次误操作永久破坏。
        """
        try:
            pw = self._pane.winfo_width()
            cur = int(self._pane.sashpos(0))
        except tk.TclError:
            return
        if pw <= 1:
            return
        want = clamp_sash(pw, cur)
        if want != cur:
            try:
                self._pane.sashpos(0, want)
            except tk.TclError:
                return
        self._sash_locked = True
        try:
            self._save_sash(int(self._pane.sashpos(0)))
        except tk.TclError:
            pass

    # ---------------- 性能档位 ----------------

    def _set_profile(self, key):
        """选中某档位（只记录选择 —— 引擎参数在点「开始汉化」时才重启生效）。"""
        if isinstance(key, str):
            try:
                key = Profile(key)
            except ValueError:
                key = Profile.TURBO
        self.profile_var.set(key.value)

        for k, b in self._profile_btns.items():
            sel = (k == key)
            b.configure(
                bg=ACCENT if sel else IDLE_BTN,
                fg="white" if sel else TEXT,
                activebackground=ACCENT_DARK if sel else IDLE_BTN_HOVER,
                activeforeground="white" if sel else TEXT)
        self._render_profile_detail(key.value)

    def _render_profile_detail(self, key_str: str):
        try:
            key = Profile(key_str)
        except ValueError:
            key = Profile.TURBO
        spec = PROFILES[key]

        lines = []
        if self.hw is not None:
            rec = recommend_profile(self.hw)
            mark = "（本机推荐）" if rec == key else ""
            lines.append(f"{self.hw.summary()} → 推荐：{PROFILES[rec].name}{mark}")
        else:
            lines.append("⏳ 正在检测显卡…")

        lines.append(
            f"{spec.tagline}　并发 {spec.num_parallel} 路 · 每批 {spec.batch_size} 条"
            f" · 上下文 {spec.num_ctx} · {spec.vram_hint} · 相对速度 {spec.speed_hint}")
        lines.append(spec.detail)

        pending = ""
        if self._engine_ready and self.runtime.profile.value != key.value:
            pending = "　⚠ 与当前引擎设置不同，将在开始汉化时重启引擎生效"
        self.profile_detail.configure(text="\n".join(lines) + pending)
        self._render_model_detail()

    # ---------------- 翻译模型 ----------------

    def _build_model_buttons(self):
        """按 models/ 里实际的 .gguf 生成按钮。换模型就是换个文件夹。"""
        self.model_row.clear()
        self._model_btns = {}

        try:
            ggufs = self.runtime.list_gguf()
        except Exception:  # noqa: BLE001
            ggufs = []

        entries = [("auto", "自动")]
        for g in ggufs:
            entries.append((g.name, model_label(g.name)))

        for pref, label in entries:
            b = tk.Button(
                self.model_row, text=label,
                command=lambda p=pref: self._set_model(p),
                relief="flat", cursor="hand2", bd=0,
                font=("Microsoft YaHei UI", 10), padx=12, pady=7,
                bg=IDLE_BTN, fg=TEXT,
                activebackground=IDLE_BTN_HOVER, activeforeground=TEXT)
            self.model_row.add(b)
            self._model_btns[pref] = b

        if not ggufs:
            # 注意：ButtonRow 内部用 grid 排版，这里也必须 grid（不能 pack），
            # 而且要走 add() 登记 —— 否则下次重建时它不会被清掉。
            self.model_row.add(tk.Label(
                self.model_row, text="（models\\ 里还没有 .gguf 模型）",
                bg=CARD, fg=ERR_RED, font=("Microsoft YaHei UI", 9)))
        self._set_model(self.model_pref)

    def _set_model(self, pref: str):
        """选中某个模型（只记录选择 —— 引擎在点「开始汉化」时按需重启）。"""
        self.model_pref = pref
        for p, b in self._model_btns.items():
            sel = (p == pref)
            b.configure(
                bg=ACCENT if sel else IDLE_BTN,
                fg="white" if sel else TEXT,
                activebackground=ACCENT_DARK if sel else IDLE_BTN_HOVER,
                activeforeground="white" if sel else TEXT)
        self._render_model_detail()

    def _render_model_detail(self):
        if not hasattr(self, "model_detail"):
            return
        try:
            ggufs = self.runtime.list_gguf()
        except Exception:  # noqa: BLE001
            ggufs = []
        if not ggufs:
            self.model_detail.configure(
                text="把 .gguf 模型文件放进软件目录的 models\\ 文件夹，"
                     "重启软件即可（可放多个，这里会都列出来）。")
            return

        try:
            picked = pick_model(
                ggufs, mode=self.model_pref,
                prefer_small=PROFILES[Profile(self.profile_var.get() or "turbo")]
                .prefer_small_model)
        except Exception:  # noqa: BLE001
            picked = ggufs[0]

        gb = picked.stat().st_size / 1024 / 1024 / 1024
        kind = "翻译专用模型" if is_translation_model(picked.name) else "通用模型"
        note = ""
        if self.model_pref == "auto":
            note = "（自动：先按档位定大小档，同档内优先翻译专用模型）"
        elif self.model_pref == "quality":
            note = "（手动指定：质量优先）"
        elif self.model_pref == "speed":
            note = "（手动指定：速度优先）"
        else:
            note = "（手动指定）"

        lines = [f"当前使用：{model_label(picked.name)} · {kind} · {gb:.2f} GB {note}"]
        if self.model_pref == "auto":
            trans = [g for g in ggufs if is_translation_model(g.name)]
            if trans and not is_translation_model(picked.name):
                names = "、".join(model_label(g.name) for g in trans)
                lines.append(
                    f"注：models\\ 里的翻译专用模型（{names}）属于另一个大小档，"
                    f"当前档位没用它。想用就直接点它的按钮。")
        if kind == "翻译专用模型":
            lines.append("专用模型更快，但小尺寸的专用模型在个别词义上"
                         "不如大通用模型稳；翻完建议跑一次「快速校对」。")
        # 引擎**实际**加载了什么 —— 这一行是防「界面说 A、实际跑 B」的关键
        if self._engine_ready and self.runtime.model_file:
            real = model_label(self.runtime.model_file)
            info = (self._loaded_info or [{}])[0]
            tip = ""
            if info:
                tip = (f"（{info.get('params', '?')} {info.get('quant', '')}"
                       f" · 显存 {info.get('vram_gb', 0):.2f} GB）")
            if self.runtime.model_file != picked.name:
                lines.append(f"⚠ 引擎目前加载的是 {real}{tip} —— "
                             f"点「开始汉化」时会自动切换并重启引擎")
            else:
                lines.append(f"引擎已加载：{real}{tip} ✅")
        self.model_detail.configure(text="\n".join(lines))

    # ---------------- 目标语言 / 自定义模型 ----------------

    def _set_lang(self, code: str):
        """选中一个预置目标语言（或匹配已有值）。"""
        self.target_lang = code
        self.runtime._write_state(target_lang=code)
        for c, b in self._lang_btns.items():
            sel = (c == code)
            b.configure(
                bg=ACCENT if sel else IDLE_BTN,
                fg="white" if sel else TEXT,
                activebackground=ACCENT_DARK if sel else IDLE_BTN_HOVER,
                activeforeground="white" if sel else TEXT)
        self._render_lang_detail()

    def _apply_custom_lang(self):
        """自定义语言输入框失焦/回车时，把填写的语言作为目标语言。"""
        val = self.custom_lang_var.get().strip()
        if not val:
            return
        # 命中预置就切到对应按钮，否则当作自定义语言
        for code, _label in LANG_CHOICES:
            if val == code or val.lower() == code.lower():
                self._set_lang(code)
                return
        self.target_lang = val
        self.runtime._write_state(target_lang=val)
        for c, b in self._lang_btns.items():
            b.configure(bg=IDLE_BTN, fg=TEXT,
                        activebackground=IDLE_BTN_HOVER, activeforeground=TEXT)
        self._render_lang_detail()

    def _render_lang_detail(self):
        if not hasattr(self, "lang_hint"):
            return
        from gametl.core.validate import target_script
        script = target_script(self.target_lang)
        names = {
            "hanzi": "中文（含简/繁）",
            "kana": "日语",
            "hangul": "韩语",
            "latin": "拉丁字母语言（英/法/德/西等）",
            "unknown": "未识别（照常翻译，不跳过）",
        }
        self.lang_hint.configure(
            text=f"当前目标语言：{self.target_lang}（{names.get(script, '?')}）\n"
                 f"原文自动识别，翻译成上面选定的语言；"
                 f"也可在上方输入框直接填其他语言名，如 Français、Deutsch、русский…")

    def pick_model_file(self):
        """从任意位置选一个 .gguf，复制进 models\\（自定义模型）。"""
        path = filedialog.askopenfilename(
            title="选择 GGUF 模型文件",
            filetypes=[("GGUF 模型", "*.gguf"), ("所有文件", "*.*")])
        if not path:
            return
        src = Path(path)
        if src.suffix.lower() != ".gguf":
            messagebox.showerror(
                APP_NAME, "请选择 .gguf 格式的模型文件。")
            return
        models_dir = self.runtime.models_dir
        try:
            models_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"无法创建 models\\ 目录：\n{e}")
            return

        dst = models_dir / src.name
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            messagebox.showinfo(
                APP_NAME, f"该模型已经在 models\\ 里了：\n{src.name}")
        else:
            try:
                import shutil
                shutil.copy2(src, dst)
            except OSError as e:
                messagebox.showerror(APP_NAME, f"复制模型失败：\n{e}")
                return
            self._log(f"已把模型复制进 models\\：{src.name}", "ok")

        # 刷新模型按钮列表，并选中刚放进来的这个
        self.model_pref = src.name
        self._build_model_buttons()
        self._set_model(src.name)
        self._log(f"已切换到模型：{src.name}（点「开始汉化」时生效）", "info")
        messagebox.showinfo(
            APP_NAME,
            f"模型已就位：{src.name}\n\n"
            "点「开始汉化」时软件会自动导入并加载它。")

    # ---------------- 日志 ----------------

    def _log(self, msg: str, level: str = "info"):
        """线程安全地写日志（入队，由主线程消费）。"""
        self.log_queue.put((msg, level))

    def _pump_log(self):
        """把日志队列里的内容刷到界面；自己续下一次（120ms 一次）。

        这条 ``after`` 链**必须在关窗时掐掉**：窗口销毁后 Tk 找不到
        那个回调命令，会往 stderr 丢一句 ``invalid command name ..._pump_log``。
        打包成窗口程序虽然看不见，但那是实打实的异常噪音。
        """
        if getattr(self, "_closing", False):
            return
        try:
            while True:
                msg, level = self.log_queue.get_nowait()
                self.log.configure(state="normal")
                ts = datetime.now().strftime("%H:%M:%S")
                self.log.insert("end", f"[{ts}] ", "time")
                self.log.insert("end", msg + "\n", level)
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        try:
            self._pump_id = self.root.after(120, self._pump_log)
        except tk.TclError:                       # pragma: no cover - 已销毁
            self._pump_id = None

    # ---------------- 选择目录 ----------------

    def choose_dir(self):
        d = filedialog.askdirectory(title="选择游戏文件夹")
        if not d:
            return
        self.game_dir = Path(d)
        self.path_var.set(d)
        self._start_analyze(d)

    def _start_analyze(self, d: str):
        """选完目录后**在后台线程**分析。

        ⚠️ 曾经这里是同步调用，在 Z 盘那种大目录上会把 UI 线程占满，
        Windows 直接给窗口打上「未响应」。任何涉及磁盘遍历的操作
        都必须挪出主线程。
        """
        self.detect_label.configure(text="⏳ 正在分析目录结构…", fg=MUTED)
        self.start_btn.configure(state="disabled")
        self.choose_btn.configure(state="disabled")
        self.stage_var.set("正在分析…")
        self._log(f"已选择：{d}", "info")
        threading.Thread(target=self._analyze_worker, args=(d,),
                         daemon=True).start()

    def _analyze_worker(self, d: str):
        try:
            stats = ScanStats()
            engine, _evidence = detect_engine(Path(d))
            archives = detect_archives(Path(d), stats=stats)
            try:
                state = self._read_project_state(Path(d))
            except Exception:  # noqa: BLE001
                state = {}
            self.root.after(0, self._analyze_ok, d, engine.value,
                            archives, stats.note(), state)
        except Exception as e:  # noqa: BLE001
            self.root.after(0, self._analyze_fail, str(e))

    def _analyze_ok(self, d: str, engine_value: str, archives: dict,
                    note: str, state: dict = None):
        n = len(archives["xp3"]) + len(archives["rpa"])
        name = ENGINE_NAMES.get(engine_value, engine_value)
        parts = [f"识别引擎：{name}"]
        if n:
            parts.append(f"发现 {n} 个资源包（将自动解包）")
        else:
            parts.append("未发现资源包（按明文目录处理）")
        self.detect_label.configure(text="  •  ".join(parts), fg=OK_GREEN)
        self._log(f"引擎：{engine_value}；资源包：{n} 个", "info")
        self._log(note, "info")
        self._refresh_project_state(state or {})
        st = state or {}
        if st.get("exists"):
            self._log(f"发现已有工程文件：{st.get('path')}", "info")
        self.start_btn.configure(state="normal")
        self.choose_btn.configure(state="normal")
        self.stage_var.set("就绪")

    def _analyze_fail(self, err: str):
        self.detect_label.configure(text=f"分析失败：{err}", fg=ERR_RED)
        self.start_btn.configure(state="normal")
        self.choose_btn.configure(state="normal")
        self.stage_var.set("就绪")

    # ---------------- 主流程 ----------------

    def start(self):
        if not self.game_dir:
            messagebox.showwarning(APP_NAME, "请先选择游戏文件夹")
            return
        if self.worker and self.worker.is_alive():
            return

        out_dir = self.game_dir / "_汉化输出"
        self.cancel_event.clear()
        self.progress.configure(value=0)
        self.stage_var.set("准备中...")
        self.speed_var.set("")
        self.start_btn.configure(state="disabled")
        self.choose_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.open_out_btn.configure(state="disabled")
        self.export_btn.configure(state="disabled")
        self.pkg_out_btn.configure(state="disabled")
        self.audit_btn.configure(state="disabled")
        self.deep_btn.configure(state="disabled")
        self._t0 = time.time()
        self._tr_t0 = 0.0            # 翻译阶段起点，等真的开始翻再记
        self._rate_hist.clear()
        self._log("═" * 50, "info")
        self._log("开始一键汉化流程", "ok")

        profile_key = self.profile_var.get() or "turbo"
        spec = PROFILES[Profile(profile_key)]
        self._log(f"性能档位：{spec.emoji} {spec.name}（{spec.tagline}）", "info")
        try:
            _picked = self.runtime.resolve_model()
            if _picked:
                self._log(f"翻译模型：{_picked.name}"
                          f"（{self.model_pref if self.model_pref != 'auto' else '自动'}）",
                          "info")
        except Exception:  # noqa: BLE001
            pass

        work_root = self._work_root_for(self.game_dir)
        if work_root != out_dir:
            self._log(f"工作目录：{work_root}", "info")
            self._log("（工程文件放软件盘 —— 实测放游戏盘会把翻译速度砍掉一半）",
                      "info")

        st = self._project_state or {}
        # 取出待导入的翻译包（一次性消费，避免下次开跑又莫名其妙地导入）
        pkg_path = getattr(self, "_import_pkg", None)
        self._import_pkg = None
        if pkg_path:
            self._log(f"本次会先导入翻译包：{Path(pkg_path).name}", "ok")
            self._log("  命中的条目直接复用，不再调用模型。", "info")
        if st.get("exists") and st.get("units"):
            dn = (st.get("filled") or 0) + (st.get("journal") or 0)
            if dn >= st["units"]:
                self._log(f"工程已是完成状态（{st['units']} 条全部已译），"
                          f"本次将跳过翻译、校对与回填，只重新打包。", "ok")
            else:
                self._log(f"工程已有 {dn}/{st['units']} 条译文，"
                          f"本次只翻剩下的 {st['units'] - dn} 条。", "info")
        elif st.get("exists"):
            self._log("发现旧版工程文件：已译条目会被复用，未记录的进度需重新统计。",
                      "info")

        cfg = AutoConfig(
            game_dir=self.game_dir,
            out_dir=out_dir,
            work_root=work_root,
            model=self.model_name or "gametl-model",
            host=self.model_host or "http://127.0.0.1:11435",
            model_file=self.runtime.model_file,
            glossary_path=self._find_glossary(),
            profile=profile_key,
            target_lang=self.target_lang,
            enable_audit=True,
            deep_check_rate=0.0,      # 深度校对走按钮，不自动跑
            skip_if_finished=True,    # 翻完的工程不再重复劳动
            import_package=pkg_path,  # 导入翻译包（免模型）
        )

        self.worker = threading.Thread(
            target=self._run_pipeline, args=(cfg,), daemon=True)
        self.worker.start()

    def _work_root_path(self, game_dir: Path) -> Path:
        """中间产物默认落哪（纯计算，不碰磁盘）。"""
        name = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", game_dir.name)
        name = (name or "game")[:60]
        return self.app_home / "data" / "work" / name

    def _work_root_for(self, game_dir: Path) -> Path:
        """中间产物（工程文件 / 增量日志 / 解包结果）放哪儿。

        实测数据：46MB 的工程文件在机械盘上每落一次盘要 5~7 秒，能把翻译
        吞吐从 22.7 条/秒直接砸到 11 条/秒。所以默认落到**软件所在的盘**
        （通常是 SSD），只把最终成品写回游戏目录。

        软件盘不可写时退回输出目录，不让流程失败。
        """
        root = self._work_root_path(game_dir)
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".writable"
            probe.write_text("1", encoding="utf-8")
            probe.unlink()
            return root
        except Exception:  # noqa: BLE001
            return game_dir / "_汉化输出"

    def _find_project_files(self, game_dir: Path) -> list:
        """找出这个游戏可能存在的工程文件（新版位置 + 旧版位置）。"""
        out = []
        for p in (self._work_root_path(game_dir) / "project.json",
                  game_dir / "_汉化输出" / "project.json"):
            if p.exists():
                out.append(p)
        return out

    def _read_project_state(self, game_dir: Path) -> dict:
        """快速判断「这个游戏上次翻到哪了」。**只读文件头**，不动全量解析。

        Returns:
            {"path", "exists", "units", "filled", "finished_at", "journal"}
        """
        st: dict = {"path": None, "exists": False, "units": 0,
                    "filled": 0, "finished_at": "", "journal": 0}
        files = self._find_project_files(game_dir)
        if not files:
            return st
        p = files[0]
        st["path"] = str(p)
        st["exists"] = True
        meta = Project.read_meta(p)
        try:
            st["units"] = int(meta.get("units") or 0)
            st["filled"] = int(meta.get("filled") or 0)
        except (TypeError, ValueError):
            pass
        st["finished_at"] = str(meta.get("finished_at") or "")
        # 增量日志里的条数还没并进工程文件，界面上要一并算进去才准
        j = p.with_name("project.journal.jsonl")
        if j.exists():
            try:
                with open(j, encoding="utf-8") as f:
                    st["journal"] = sum(1 for line in f if line.strip())
            except OSError:
                pass
        return st

    def _refresh_project_state(self, state: dict):
        """把工程进度画到界面上，并据此调整主按钮的说法。"""
        self._project_state = state or {}
        # 只要工程里有译文，就允许导出翻译包 —— 不必先跑一遍流程
        if state and state.get("exists") and (state.get("filled") or 0) > 0:
            self._enable_btn("pkg_out_btn")
        if self.game_dir and (self.game_dir / "_汉化输出").exists():
            self._enable_btn("export_btn")
        if not hasattr(self, "state_label"):
            return
        if not state or not state.get("exists"):
            self.state_label.configure(text="", fg=MUTED)
            self.start_btn.configure(text="▶  开始汉化")
            return

        units = state.get("units") or 0
        filled = state.get("filled") or 0
        extra = state.get("journal") or 0
        total_done = filled + extra
        when = state.get("finished_at") or ""

        if units and total_done >= units:
            txt = (f"✅ 这个游戏已经翻完了（{units}/{units} 条"
                   f"{'，完成于 ' + when if when else ''}）"
                   f"　→ 再点开始会跳过翻译与回填，只重新打包")
            self.state_label.configure(text=txt, fg=OK_GREEN)
            self.start_btn.configure(text="▶  重新生成汉化版")
        elif units:
            pct = total_done / units * 100
            txt = (f"⏳ 上次翻到 {total_done}/{units} 条（{pct:.1f}%"
                   f"{'，含未合并的 ' + str(extra) + ' 条' if extra else ''}）"
                   f"　→ 会从断点续传，已译部分不重翻")
            self.state_label.configure(text=txt, fg=WARN_AMBER)
            self.start_btn.configure(text="▶  继续汉化")
        else:
            txt = ("📄 发现已有的工程文件（旧版本，没有记录进度）"
                   "　→ 已译条目仍会被复用")
            self.state_label.configure(text=txt, fg=MUTED)
            self.start_btn.configure(text="▶  开始汉化")

    def _enable_btn(self, name: str) -> None:
        """把某个按钮置为可用（界面还没构造完/已销毁时不至于报错）。"""
        b = getattr(self, name, None)
        if b is None:
            return
        try:
            b.configure(state="normal")
        except tk.TclError:
            pass

    def _on_tk_error(self, exc, val, tb) -> None:
        """任何界面回调里逃出来的异常都走这里。

        这是「点了没效果」的根治手段：以前回调抛异常只是静默写 stderr，
        打包后连 stderr 都没有。现在至少会弹个框、留一行日志。
        """
        text = "".join(traceback.format_exception(exc, val, tb))
        self._log(f"界面操作出错：{val}", "err")
        for line in text.strip().splitlines()[-8:]:
            self._log("   " + line, "err")
        key = f"{type(val).__name__}:{val}"
        if key == self._last_err_ui:
            return
        self._last_err_ui = key
        try:
            messagebox.showerror(
                APP_NAME,
                f"操作失败：\n{val}\n\n"
                "（详细信息已经写进右边的运行日志）")
        except Exception:  # noqa: BLE001
            pass

    def _pick_folder(self, title: str, initial) -> Path | None:
        """选文件夹 —— 统一把主窗口提到前面，免得对话框被压在后面像没反应。"""
        try:
            self.root.lift()
            self.root.focus_force()
            self.root.update_idletasks()
        except tk.TclError:
            pass
        d = filedialog.askdirectory(
            title=title, initialdir=str(initial) if initial else None)
        return Path(d) if d else None

    def _find_glossary(self) -> Path | None:
        """查找术语表（优先软件目录下用户自己的，其次内置示例）。"""
        for cand in (self.app_home / "glossary.json",
                     APP_DIR / "glossary.json",
                     self.app_home / "gametl" / "glossary.example.json",
                     BASE_DIR / "gametl" / "glossary.example.json"):
            if cand.exists():
                return cand
        return None

    def _run_pipeline(self, cfg: AutoConfig):
        # ---- 档位 / 模型若被改过，先让引擎按新设置重启 ----
        want = Profile(cfg.profile)
        want_model = self.model_pref

        dirty = (self.runtime.profile != want
                 or self.runtime.model_pref != want_model)
        if not dirty:
            # auto 模式下档位没变 → 模型一般也不变；再核一次，
            # 以防用户中途往 models\ 里放了新文件。
            try:
                picked = self.runtime.resolve_model()
                if (picked and self.runtime.model_file
                        and picked.name != self.runtime.model_file):
                    dirty = True
            except Exception:  # noqa: BLE001
                pass

        # 引擎还没就绪时不要直接报错 —— 启动时的自动准备可能仍在跑。
        # prepare() 内部有锁，这里等它跑完即可（不会重复导入模型）。
        if dirty or not self._engine_ready:
            if dirty:
                self._log(f"正在按「{PROFILES[want].name}」+ 所选模型"
                          f"准备翻译引擎，请稍候…", "warn")
            else:
                self._log("等待翻译引擎准备完成…", "warn")
            try:
                ok, host, model, msg = self.runtime.prepare(
                    progress=lambda m: self._log(m, "info"),
                    profile=want, model_pref=want_model)
            except Exception as e:  # noqa: BLE001
                ok, host, model, msg = False, "", "", f"引擎重启异常：{e}"
            self.root.after(0, self._engine_done, ok, host, model, msg)
            if not ok:
                self.root.after(0, self._finish, _FailResult(msg))
                return
            cfg.host = host
            cfg.model = model
            cfg.model_file = self.runtime.model_file
            self._log(f"引擎已就绪（{PROFILES[want].name} · "
                      f"{self.runtime.model_file or model}）", "ok")
        else:
            cfg.model_file = self.runtime.model_file

        def on_progress(stage: str, cur: int, total: int, desc: str):
            self.root.after(0, self._update_progress, stage, cur, total, desc)

        def on_log(msg: str):
            level = "info"
            if "[警告]" in msg or "警告" in msg:
                level = "warn"
            if "[错误]" in msg or "Traceback" in msg:
                level = "err"
            self._log(msg, level)

        pipe = AutoPipeline(cfg, on_progress=on_progress, on_log=on_log,
                            cancel_event=self.cancel_event)
        result = pipe.run()
        self.root.after(0, self._finish, result)

    def _update_progress(self, stage: str, cur: int, total: int, desc: str):
        pct = 0 if total <= 0 else int(cur / total * 100)
        weights = {"识别引擎": (0, 3), "解包资源": (3, 15), "提取文本": (15, 20),
                   "翻译文本": (20, 80), "校对译文": (80, 85),
                   "回填译文": (85, 92), "重打包": (92, 99),
                   "完成": (100, 100)}
        lo, hi = weights.get(stage, (0, 100))
        overall = lo + (hi - lo) * (pct / 100)
        self.progress.configure(value=overall)

        if stage == "翻译文本":
            self.stage_var.set(f"正在翻译... {cur}/{total}")
            now = time.time()
            if not self._tr_t0:
                # 进度条一动，翻译才算真的开始 —— 之前那几十秒（启动引擎、
                # 把模型加载进显存）不该算进速度里。
                self._tr_t0 = now
                self._rate_hist.clear()
            if self._rate_hist and cur < self._rate_hist[-1][1]:
                self._rate_hist.clear()      # 计数回退说明重来了，丢掉旧窗口
            self._rate_hist.append((now, cur))

            span = now - self._tr_t0
            avg = cur / span if span > 1.0 and cur > 0 else 0.0
            inst = window_rate(self._rate_hist, now, cur)
            use = inst or avg
            if use > 0:
                remain = (total - cur) / use
                bits = [f"当前 {use:.1f} 条/秒"]
                # 只在两者差距明显时才把平均值也摆出来，免得数字打架
                if avg > 0 and (inst <= 0 or abs(inst - avg) > 0.2 * avg):
                    bits.append(f"平均 {avg:.1f}")
                bits.append(f"预计剩余 {self._fmt_dur(remain)}")
                self.speed_var.set(" · ".join(bits))
        elif stage == "校对译文":
            self.speed_var.set("")
            self.stage_var.set(f"校对中... {desc}" if desc else "校对中...")
        else:
            self.speed_var.set("")
            self.stage_var.set(f"{stage}：{desc}" if desc else stage)

    @staticmethod
    def _fmt_dur(sec: float) -> str:
        s = int(max(0, sec))
        if s < 60:
            return f"{s} 秒"
        if s < 3600:
            return f"{s // 60} 分"
        return f"{s // 3600} 小时 {s % 3600 // 60} 分"

    def _finish(self, result):
        self.start_btn.configure(state="normal")
        self.choose_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.audit_btn.configure(state="normal")
        self.deep_btn.configure(state="normal")
        self.speed_var.set("")

        if result.success:
            self.progress.configure(value=100)
            head = "✅ 已是最新" if getattr(result, "already_done", False) \
                else f"✅ 汉化完成（{result.speed_line}）"
            self.stage_var.set(head)
            self._log("═" * 50, "ok")
            if getattr(result, "already_done", False):
                self._log("✅ 完成！这个游戏此前已经翻完，本次没有重新翻译，"
                          "只重新生成了一遍成品。", "ok")
            else:
                self._log(f"✅ 完成！翻译 {result.translated_units} 条文本"
                          f"，平均 {result.speed_line}", "ok")
            if getattr(result, "unverified", 0):
                self._log(f"⚠ 其中 {result.unverified} 条是兜底保留的"
                          f"（未通过硬校验），建议点「快速校对」复核。", "warn")
            if result.audit_summary:
                self._log(result.audit_summary,
                          "warn" if result.audit_issues else "ok")
            self._log(f"输出目录：{result.output_dir}", "ok")
            self.open_out_btn.configure(state="normal")
            self.export_btn.configure(state="normal")
            self.pkg_out_btn.configure(state="normal")
            if getattr(result, "package_imported", 0):
                self._log(f"本次有 {result.package_imported} 条译文来自导入的"
                          f"翻译包（没有占用模型推理）。", "ok")
            # 刷新工程进度条幅：翻完之后应该立刻显示「已完成」
            try:
                if self.game_dir:
                    self._refresh_project_state(
                        self._read_project_state(self.game_dir))
            except Exception:  # noqa: BLE001
                pass

            if getattr(result, "already_done", False):
                msg = ("这个游戏上次就已经翻完了，本次没有重翻任何条目。\n"
                       f"已重新生成成品。\n\n输出位置：\n{result.output_dir}")
            else:
                msg = (f"汉化完成！\n\n翻译了 {result.translated_units} 条文本\n"
                       f"平均速度 {result.speed_line}\n")
                if getattr(result, "filled_units", 0) and result.total_units:
                    msg += (f"工程覆盖 {result.filled_units}/"
                            f"{result.total_units} 条\n")
                if result.audit_issues:
                    msg += (f"\n自动校对发现 {result.audit_issues} 处可疑"
                            f"（涉及 {result.audit_fixable} 条）。\n"
                            f"可以点「快速校对」查看详情并一键重翻。")
                msg += f"\n输出位置：\n{result.output_dir}"
            messagebox.showinfo(APP_NAME, msg)
        elif getattr(result, "stage_reached", "") == "已取消":
            self.stage_var.set("已取消")
            self._log("任务已取消。已翻译的部分会保留，重新开始即续传。", "warn")
        else:
            self.stage_var.set("❌ 出现错误")
            self._log(f"❌ {result.error}", "err")
            messagebox.showerror(APP_NAME, f"处理失败：\n\n{result.error}")

    def cancel(self):
        self.cancel_event.set()
        self._log("正在取消...", "warn")
        self.stage_var.set("正在取消...")

    def open_output(self):
        if not self.game_dir:
            return
        d = self.game_dir / "_汉化输出"
        if d.exists():
            try:
                os.startfile(d)  # Windows
            except AttributeError:
                subprocess.Popen(["xdg-open", str(d)])

    # ---------------- 运行时翻译（LunaTranslator 整合） ----------------

    def _find_luna(self) -> str:
        """按候选路径探测 LunaTranslator.exe；找不到返回空串。"""
        for p in LUNA_PATHS:
            if Path(p).exists():
                return p
        return ""

    def _luna_running(self) -> bool:
        """检测 LunaTranslator 是否已在运行（避免重复拉起）。"""
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq LunaTranslator.exe"],
                capture_output=True, text=True, timeout=10).stdout or ""
            return "LunaTranslator.exe" in out
        except Exception:  # noqa: BLE001
            return False

    def launch_runtime_translate(self):
        """一键拉起 LunaTranslator（运行时翻译）。

        前提：LunaTranslator 已预配置好指向内置引擎（11435 的 gametl-model），
        且配置文件随汉化椅分发（LunaTranslator 目录内的 userconfig/）。
        这里做三件事：① 防重复；② 确保内置引擎在跑；③ 调起 LunaTranslator。
        """
        luna = self._find_luna()
        if not luna:
            messagebox.showwarning(
                APP_NAME_EN,
                "没找到实时翻译组件（LunaTranslator）。\n\n"
                "正常安装的便携版自带该组件，位于：\n"
                "  D:\\滚刀哥布林汉化椅\\LunaTranslator\\LunaTranslator_x64\\\n\n"
                "如果目录缺失，请重新解压完整安装包。")
            self._log("运行时翻译：未找到 LunaTranslator 组件", "warn")
            return

        # ① 防重复：已在运行就不拉第二个（LunaTranslator 自身也会弹确认框）
        if self._luna_running():
            self._log("运行时翻译：实时翻译已在运行，无需重复启动。", "info")
            messagebox.showinfo(
                APP_NAME_EN,
                "实时翻译已经在运行了。\n\n"
                "在屏幕右下角托盘区找到 LunaTranslator 图标即可操作。")
            return

        # ② 确保内置引擎在跑（正常情况下启动时已就绪）
        if not self._engine_ready:
            self._log("运行时翻译：翻译引擎尚未就绪，正在等待…", "info")
            self._prepare_engine()

        # ③ 调起（cwd=LunaTranslator 目录 → 它会读取我们预置的 userconfig）
        try:
            subprocess.Popen([luna],
                             cwd=str(Path(luna).parent),
                             creationflags=getattr(
                                 subprocess, "CREATE_NO_WINDOW", 0)
                             if os.name == "nt" else 0)
            self._log(f"运行时翻译：已启动实时翻译组件（{luna}）", "ok")
            self._log("运行时翻译：在 LunaTranslator 里点「绑定窗口」选游戏即可，"
                      "翻译走本机内置模型，不联网。", "info")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME_EN, f"启动实时翻译组件失败：\n{e}")
            self._log(f"运行时翻译：启动失败：{e}", "error")

    # ---------------- 导出完整汉化版 ----------------

    def export_game(self):
        """把「游戏本体 + 汉化后的文件」合并导出成一份可直接开玩的汉化版。

        采用**复制**而不是原地改动 —— 原游戏保持原样，导错了随时能重来。
        """
        if not self.game_dir:
            messagebox.showwarning(APP_NAME, "请先选择游戏文件夹")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, "当前有任务在跑，请等它结束或先取消。")
            return
        if not (self.game_dir / "_汉化输出").exists():
            messagebox.showwarning(APP_NAME, "还没生成汉化结果，请先完成一次汉化。")
            return

        dest = filedialog.askdirectory(
            title="选择导出位置（软件会在里面自动建一个「…中文版」文件夹）")
        if not dest:
            return
        parent = Path(dest)

        try:
            g = self.game_dir.resolve()
            p = parent.resolve()
            if p == g or g in p.parents:
                messagebox.showerror(
                    APP_NAME,
                    "目标位置不能是游戏目录本身，也不能在它里面。\n请另选位置。")
                return
        except Exception:  # noqa: BLE001
            pass

        # 用户选的是**父目录**，真正的成品装进自动建出来的子文件夹里 ——
        # 不然整个游戏会散落在用户选的目录里，想整个拷走或删掉都很麻烦。
        target = self._export_target(parent)
        try:
            t = target.resolve()
            g2 = self.game_dir.resolve()
            if t == g2 or g2 in t.parents:
                messagebox.showerror(
                    APP_NAME,
                    "算出来的导出文件夹落在游戏目录里了，请换一个位置。")
                return
        except Exception:  # noqa: BLE001
            pass

        if target.exists():
            try:
                occupied = any(target.iterdir())
            except OSError:
                occupied = True
            if occupied:
                if not messagebox.askyesno(
                        APP_NAME,
                        f"这个文件夹已经存在，而且里面有东西：\n{target}\n\n"
                        "继续的话会**先把它清空**，再重新导出。\n"
                        "如果里面的东西还有用，请选「否」再换个位置。\n\n"
                        "确定要清空它吗？",
                        icon="warning", default="no"):
                    return

        if not messagebox.askyesno(
                APP_NAME,
                f"将把整个游戏复制到：\n{target}\n\n"
                "请确认目标盘剩余空间足够放下整个游戏。\n"
                "原游戏不会被改动。是否继续？"):
            return

        self.cancel_event.clear()
        self.export_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress.configure(value=0)
        self._log("═" * 50, "info")
        self._log(f"开始导出完整汉化版 → {target}", "ok")
        threading.Thread(target=self._export_worker, args=(target,),
                         daemon=True).start()

    def _export_target(self, parent: Path) -> Path:
        """由用户选的父目录，算出真正的导出文件夹（见 :func:`export_target_for`）。"""
        return export_target_for(self.game_dir.name, parent)

    def _export_worker(self, dest: Path):
        try:
            work_root = self._work_root_for(self.game_dir)
            res = export_full_game(
                self.game_dir, work_root, dest,
                on_progress=lambda c, n, d: self.root.after(
                    0, self._export_progress, c, n, d),
                cancel_event=self.cancel_event,
                clean=True)      # 目标若已存在，上面已经问过用户了
            self.root.after(0, self._export_done, res, dest)
        except Cancelled:
            self.root.after(0, self._export_done, None, dest)
        except Exception as e:  # noqa: BLE001
            self.root.after(0, self._export_fail, str(e))

    def _export_progress(self, cur: int, total: int, desc: str):
        self.stage_var.set(f"导出中... {desc}")
        if total > 0:
            self.progress.configure(value=int(cur / total * 100))

    def _export_done(self, res, dest: Path):
        self.export_btn.configure(state="normal")
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        if res is None:
            self._log("导出已取消（已复制到目标目录的文件可直接删掉）。", "warn")
            messagebox.showinfo(APP_NAME, "导出已取消。")
            return
        self.progress.configure(value=100)
        self.stage_var.set("导出完成")
        if res.get("skipped"):
            self._log(f"导出完成：复制 {res['files']} 个文件，"
                      f"跳过 {res['skipped']} 个和上次一模一样的文件（增量），"
                      f"其中 {res['overwritten']} 个是汉化文件", "ok")
        else:
            self._log(f"导出完成：复制 {res['files']} 个文件，"
                      f"其中覆盖 {res['overwritten']} 个汉化文件", "ok")
        self._log(f"位置：{dest}", "ok")
        if messagebox.askyesno(
                APP_NAME, f"导出完成！\n\n{dest}\n\n现在打开看看吗？"):
            try:
                os.startfile(dest)
            except Exception:  # noqa: BLE001
                pass

    def _export_fail(self, err: str):
        self.export_btn.configure(state="normal")
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self._log(f"导出失败：{err}", "err")
        messagebox.showerror(APP_NAME, f"导出失败：\n{err}")

    # ---------------- 汉化补丁包（可独立运行、可分发） ----------------

    def _installer_template(self) -> Path | None:
        """找用来当「汉化安装器」的 exe。

        优先用软件目录里那份独立文件（用户想单独更新它时方便），
        没有就把**主程序自己**复制一份过去 —— 主程序启动时发现
        自己的名字里带「安装器」，就会切到安装器界面。
        """
        cand = self.app_home / INSTALLER_NAME
        try:
            if cand.is_file():
                return cand
        except OSError:
            pass
        if getattr(sys, "frozen", False):
            try:
                exe = Path(sys.executable)
                if exe.is_file():
                    return exe
            except OSError:
                pass
        return None

    def export_pkg(self):
        """生成一个能直接发给别人的「汉化补丁包」。

        产物是一个文件夹（外加同名 zip）::

            <游戏名>_汉化补丁包/
                汉化安装器.exe      ← 双击就装，装完还能一键还原
                <游戏名>_汉化翻译包.gtpkg
                安装说明.txt

        以前这里是弹「保存文件」对话框存一个 .gtpkg，用户在对话框里
        一取消就什么都没有，界面上毫无反馈 —— 现在改成选文件夹、
        自动建目录、自动打 zip，每一步都写进日志。
        """
        if not self.game_dir:
            messagebox.showwarning(APP_NAME, "请先选择游戏文件夹")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, "当前有任务在跑，请等它结束或先取消。")
            return
        files = self._find_project_files(self.game_dir)
        if not files:
            messagebox.showwarning(
                APP_NAME,
                "这个游戏还没有工程文件，没什么可打包的。\n\n"
                "先点一次「开始汉化」把它翻出来，再回来导出。")
            return

        desktop = Path.home() / "Desktop"
        parent = self._pick_folder(
            "选择「汉化补丁包」放在哪个文件夹（建议放桌面）",
            desktop if desktop.is_dir() else self.game_dir)
        if parent is None:
            self._log("已取消导出（没有选择文件夹）。", "info")
            return

        self.pkg_out_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self._log("═" * 50, "info")
        self._log(f"开始导出汉化补丁包 → {parent}", "info")
        threading.Thread(target=self._export_bundle_worker,
                         args=(parent, files[0]), daemon=True).start()

    def _export_bundle_worker(self, parent: Path, project_path: Path):
        try:
            # 注意：本文件在打包后的 exe 里是「顶层脚本」，__package__ 为空，
            # 这里必须用绝对导入 —— 相对导入会报
            # "attempted relative import with no known parent package"。
            from gametl.core.models import Project
            from gametl.core.package import export_patch_bundle
            self._log("正在读取工程…", "info")
            proj = Project.load(project_path)
            filled, total = proj.progress()
            if not filled:
                raise RuntimeError("工程里还没有任何译文。")
            self._log(f"工程 {filled}/{total} 条已译，开始打包…", "info")
            inst = self._installer_template()
            if inst is None:
                self._log("[提示] 当前是源码运行、没有可复制的 exe，"
                          "本次只生成翻译包本身；打包成 exe 之后就会"
                          "自带独立安装器。", "warn")
            res = export_patch_bundle(
                proj, parent, self.game_dir.name,
                installer_exe=inst,
                on_progress=lambda c, n, d: self.root.after(
                    0, self._export_progress, c, n, d),
                on_log=lambda m: self._log(m, "info"))
            self.root.after(0, self._bundle_done, res, total)
        except Exception as e:  # noqa: BLE001
            self.root.after(0, self._pkg_fail, str(e), True)

    def _bundle_done(self, res: dict, total: int):
        self.pkg_out_btn.configure(state="normal")
        self.start_btn.configure(state="normal")
        self.progress.configure(value=100)
        mb = res["bytes"] / 1048576
        self._log(f"✅ 汉化补丁包已生成：{res['dir']}", "ok")
        self._log(f"   译文 {res['units']} 条（工程共 {total} 条）· "
                  f"{mb:.1f} MB", "ok")
        if res.get("zip"):
            self._log(f"   打包好的压缩包：{res['zip']}", "ok")
        else:
            self._log("   （zip 没打成，直接把文件夹发出去也一样）", "warn")
        if not res.get("installer"):
            self._log("   注意：本次没有放入独立安装器。", "warn")

        open_dir = Path(res["zip"]) if res.get("zip") else Path(res["dir"])
        if messagebox.askyesno(
                APP_NAME,
                "汉化补丁包做好了！\n\n"
                f"文件夹：\n{res['dir']}\n\n"
                + (f"压缩包：\n{res['zip']}\n\n" if res.get("zip") else "")
                + f"译文 {res['units']} 条 · {mb:.1f} MB\n\n"
                "拿到别的电脑后：解压到游戏目录 → 双击「汉化安装器.exe」\n"
                "→ 点「安装汉化」。不用装模型、不用联网；\n"
                "想换回原来的语言，再点一下「还原原版」。\n\n"
                "现在打开它所在的文件夹吗？"):
            self._open_path(open_dir)

    def _open_path(self, p: Path) -> None:
        try:
            p = Path(p)
            if p.is_file():
                p = p.parent
            if sys.platform.startswith("win") and hasattr(os, "startfile"):
                os.startfile(str(p))                    # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except Exception as e:  # noqa: BLE001
            self._log(f"打开文件夹失败：{e}", "warn")

    def import_pkg(self):
        """导入翻译包：不跑模型，直接把译文灌进工程再回填导出。"""
        if not self.game_dir:
            messagebox.showwarning(APP_NAME, "请先选择游戏文件夹")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, "当前有任务在跑，请等它结束或先取消。")
            return
        path = filedialog.askopenfilename(
            title="选择翻译包",
            filetypes=[("翻译包", "*.gtpkg"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            from gametl.core.package import read_manifest
            man = read_manifest(Path(path))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"这个文件读不出来：\n{e}")
            return

        try:
            mb = Path(path).stat().st_size / 1048576
        except OSError:
            mb = 0.0
        if not messagebox.askyesno(
                APP_NAME,
                f"翻译包内容：\n\n"
                f"  游戏：{man.get('game') or '（未记录）'}\n"
                f"  引擎：{man.get('engine') or '未知'}\n"
                f"  译文：{man.get('units_translated')} 条"
                f"（源工程共 {man.get('units_total')} 条）\n"
                f"  大小：{mb:.1f} MB\n\n"
                "导入后会自动完成回填与导出，**全程不需要模型**。\n"
                "包覆盖不到的条目，才会用模型补翻。\n\n"
                "现在开始导入吗？"):
            return
        self._import_pkg = Path(path)
        self._log("═" * 50, "info")
        self._log(f"准备导入翻译包：{Path(path).name}", "ok")
        self.start()

    def _pkg_fail(self, err: str, exporting: bool):
        self.pkg_out_btn.configure(state="normal")
        self.start_btn.configure(state="normal")
        self._log(f"翻译包操作失败：{err}", "err")
        messagebox.showerror(APP_NAME, f"翻译包操作失败：\n{err}")

    # ---------------- 校对 ----------------

    def run_audit(self, deep: bool = False):
        if not self.game_dir:
            messagebox.showwarning(APP_NAME, "请先选择游戏文件夹")
            return
        proj = self.game_dir / "_汉化输出" / "project.json"
        if not proj.exists():
            messagebox.showwarning(
                APP_NAME, "还没有翻译结果。\n\n请先完成一次汉化，再回头校对。")
            return
        if self.worker and self.worker.is_alive():
            return
        if deep and not self._engine_ready:
            messagebox.showwarning(
                APP_NAME, "深度校对需要翻译引擎，但它还没就绪。\n请查看日志区。")
            return

        self.audit_btn.configure(state="disabled")
        self.deep_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self.cancel_event.clear()
        self.cancel_btn.configure(state="normal")
        self.stage_var.set("校对中..." if not deep else "深度校对中...")
        self.speed_var.set("")
        self._log("─" * 50, "info")
        self._log("深度校对：模型抽检" if deep else "快速校对：规则检查", "ok")
        threading.Thread(target=self._audit_worker, args=(proj, deep),
                         daemon=True).start()

    def _audit_worker(self, proj_path: Path, deep: bool):
        try:
            from gametl.core.models import Project
            from gametl.core.validate import audit_units
            from gametl.translators.glossary import Glossary

            project = Project.load(proj_path)
            gl = Glossary.load(self._find_glossary())
            rep = audit_units(project.units, glossary=gl.mapping,
                              target_lang=self.target_lang)
            self._log(rep.summary(), "warn" if rep.issues else "ok")

            flagged = list(rep.fixable_uids())

            if deep:
                from gametl.translators.ollama_backend import OllamaTranslator
                from gametl.translators.quality import QualityChecker

                opt = options_from_profile(self.profile_var.get() or "turbo",
                                           self.runtime.model_file)
                tr = OllamaTranslator(model=self.model_name, host=self.model_host,
                                      options=opt)

                def q_cb(c, n, d):
                    self._log(f"  抽检进度 {c}/{n}", "info")

                qc = QualityChecker(tr, concurrency=opt.concurrency)
                rres = qc.review(project.units, sample_rate=0.1,
                                 progress=q_cb,
                                 cancel_check=lambda: self.cancel_event.is_set())
                self._log(rres.summary(), "warn" if rres.flagged else "ok")
                flagged += [i.uid for i in rres.flagged]
                for issue in rres.flagged[:20]:
                    self._log(f"  [抽检] {issue.original[:24]} → "
                              f"{issue.translated[:24]}", "warn")

            self.root.after(0, self._audit_done, rep, sorted(set(flagged)))
        except Exception as e:  # noqa: BLE001
            self.root.after(0, self._audit_fail, str(e))

    def _audit_done(self, rep, uids):
        self.audit_btn.configure(state="normal")
        self.deep_btn.configure(state="normal")
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.stage_var.set("校对完成")

        for issue in rep.issues[:40]:
            self._log(f"  [{issue.kind}] {issue.message}", "warn")
        if len(rep.issues) > 40:
            self._log(f"  …另有 {len(rep.issues) - 40} 条未列出", "info")

        if not uids:
            messagebox.showinfo(
                APP_NAME,
                rep.summary() + "\n\n没有需要重翻的条目。")
            return

        ans = messagebox.askyesno(
            APP_NAME,
            f"{rep.summary()}\n\n"
            f"是否重新翻译这 {len(uids)} 条可疑文本？\n\n"
            f"（已有译文会保留，只会重翻这些条目）")
        if ans:
            self._retranslate(uids)

    def _audit_fail(self, err: str):
        self.audit_btn.configure(state="normal")
        self.deep_btn.configure(state="normal")
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        self.stage_var.set("校对失败")
        self._log(f"❌ 校对失败：{err}", "err")
        messagebox.showerror(APP_NAME, f"校对失败：\n\n{err}")

    def _retranslate(self, uids):
        """清空可疑条目的译文，再跑一次流程 —— 其余条目会被自动复用。"""
        proj = self.game_dir / "_汉化输出" / "project.json"
        try:
            from gametl.core.models import Project

            project = Project.load(proj)
            want = set(uids)
            n = 0
            for u in project.units:
                if u.uid in want and u.translated:
                    u.translated = None
                    n += 1
            project.save(proj)
            self._log(f"已清空 {n} 条译文，开始重翻（其余保留）", "warn")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"准备重翻失败：\n\n{e}")
            return
        self.cancel_event.clear()
        self.start()


class _FailResult:
    """给 _finish 用的最小失败结果对象。"""

    success = False
    stage_reached = ""
    error = ""
    translated_units = 0
    speed_line = ""
    audit_summary = ""
    audit_issues = 0
    audit_fixable = 0
    output_dir = ""

    def __init__(self, msg: str):
        self.error = msg


def _cleanup_stale_mei(max_age_sec: int = 1800) -> int:
    """清理 PyInstaller onefile 遗留的 _MEI 临时目录。

    背景：onefile 模式启动时会把自身解压到 %TEMP%\\_MEIxxxxxx，退出时由
    bootloader 负责删除。若退出时 tcl/tk 的 DLL 尚未释放，删除就会失败并
    弹出 "Failed to remove temporary directory"。这里在**下次启动**时兜底清掉。

    安全约束：
      · 只认 `_MEI*` 前缀（PyInstaller 专用命名，不会误伤其它软件）
      · 跳过当前实例正在使用的解压目录
      · 只删 30 分钟以上没被动过的（活跃实例的文件会被持续访问）
      · 全程 ignore_errors —— 文件被占用时 Windows 会拒绝删除，天然保护

    Returns:
        清理掉的目录数
    """
    if not getattr(sys, "frozen", False):
        return 0
    try:
        import shutil
        import tempfile
        cur = Path(getattr(sys, "_MEIPASS", "") or "").resolve()
        base = Path(tempfile.gettempdir())
        now = time.time()
        removed = 0
        for d in base.glob("_MEI*"):
            try:
                if not d.is_dir():
                    continue
                if cur and d.resolve() == cur:
                    continue
                if now - d.stat().st_mtime < max_age_sec:
                    continue
                shutil.rmtree(str(d), ignore_errors=True)
                if not d.exists():
                    removed += 1
            except Exception:  # noqa: BLE001
                continue
        return removed
    except Exception:  # noqa: BLE001
        return 0


def main():
    # 兜底清掉上次遗留的临时解压目录（用户看不到，也不占 C 盘）
    try:
        _cleanup_stale_mei()
    except Exception:  # noqa: BLE001
        pass

    install_thread_hook()

    # 自检模式：验证打包后的依赖完整性（不启动界面）
    if "--selfcheck" in sys.argv:
        _selfcheck()
        return

    # 安装器模式：这个 exe 被当成「汉化安装器」发出去时走这里
    try:
        from gametl.installer import main as installer_main, want_installer
        if want_installer():
            raise SystemExit(installer_main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        pass          # 判断失败就当普通主程序启动，别把用户挡在门外

    root = tk.Tk()
    App(root)
    root.mainloop()


def _selfcheck():
    """打包自检：验证所有依赖可导入，把结果写入文件（GUI 程序无法用 stdout）。"""
    out = Path(os.environ.get("GAMETL_SELFCHECK", "selfcheck.txt"))
    lines = [f"### {APP_NAME} v{APP_VERSION} 自检"]

    def rec(ok: bool, msg: str):
        lines.append(f"[{'OK' if ok else 'FAIL'}] {msg}")

    rec(True, f"frozen={getattr(sys, 'frozen', False)}")
    rec(True, f"BASE_DIR={BASE_DIR}")
    rec(True, f"APP_DIR={APP_DIR}")

    # ---- 增量落盘（JSONL journal）----
    try:
        from gametl.core.models import Project as _Project
        rec(callable(getattr(_Project, "append_journal", None)),
            "增量落盘 append_journal")
        rec(callable(getattr(_Project, "apply_journal", None)),
            "日志回放 apply_journal")
        rec(callable(getattr(_Project, "compact", None)), "日志合并 compact")
    except Exception as e:  # noqa: BLE001
        rec(False, f"增量落盘: {e}")

    # ---- 完成标记 / 工程进度 ----
    try:
        import tempfile
        from gametl.core.models import (Project as _P, TextUnit as _TU,
                                        EngineType as _ET)
        rec(callable(getattr(_P, "progress", None)), "工程进度 progress")
        rec(callable(getattr(_P, "is_complete", None)), "完成判定 is_complete")
        rec(callable(getattr(_P, "mark_finished", None)), "完成标记 mark_finished")
        rec(callable(getattr(_P, "read_meta", None)), "只读头部 read_meta")
        rec(callable(getattr(_P, "unverified_uids", None)), "兜底清单 unverified")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            pp = td / "project.json"
            u1 = _TU(uid="a", source_file="x.json", location={"i": 1},
                     original="あ", translated="啊")
            u2 = _TU(uid="b", source_file="x.json", location={"i": 2},
                     original="い")
            proj = _P(root=td, engine=_ET.RPGMAKER_MV, units=[u1, u2])
            rec(proj.progress() == (1, 2) and not proj.is_complete(),
                "progress 计数正确")
            proj.save(pp)
            jp = td / "project.journal.jsonl"
            _P.append_journal([("b", "以"), ("b", "已", 1)], jp)
            _P.compact(pp, jp, meta_update={"k": "v"})
            rec(not jp.exists(), "compact 后删除日志")
            back = _P.load(pp)
            rec(back.is_complete() and back.meta.get("k") == "v",
                "日志带 uv 与 meta_update 往返一致")
            rec(back.units[1].translated == "已"
                and back.units[1].extra.get("unverified"),
                "uv 标记被回放")
            rec(back.unverified_uids() == ["b"], "unverified_uids 正确")
            m = _P.read_meta(pp)
            rec(m.get("k") == "v", "read_meta 只读头部取到 meta")
            rec(_P.read_meta(td / "nope.json") == {}, "read_meta 缺文件返回空")
    except Exception as e:  # noqa: BLE001
        rec(False, f"完成标记: {e}")

    # ---- 引擎模型实况 ----
    try:
        from gametl.runtime_manager import RuntimeManager as _RM
        rec(callable(getattr(_RM, "tag_source", None)), "模型来源 tag_source")
        rec(callable(getattr(_RM, "loaded_info", None)), "引擎实况 loaded_info")
        rec(callable(getattr(_RM, "rotate_log", None)), "日志轮转 rotate_log")
        with tempfile.TemporaryDirectory() as td:
            lp = Path(td) / "server.log"
            lp.write_bytes(b"x" * (2 * 1024 * 1024))
            freed = _RM.rotate_log(lp, max_mb=1)
            rec(freed == 2 * 1024 * 1024
                and ((not lp.exists()) or lp.stat().st_size == 0)
                and lp.with_suffix(".log.1").exists(),
                "日志超限被轮转")
            freed2 = _RM.rotate_log(lp, max_mb=1)
            rec(freed2 == 0, "未超限时不动日志")
    except Exception as e:  # noqa: BLE001
        rec(False, f"引擎模型实况: {e}")

    # ---- 失败条目兜底 ----
    try:
        from gametl.translators.ollama_backend import OllamaTranslator as _OT
        import inspect as _ins
        sig = _ins.signature(_OT.translate_one)
        rec("allow_partial" in sig.parameters, "单条兜底 allow_partial")
    except Exception as e:  # noqa: BLE001
        rec(False, f"失败条目兜底: {e}")

    # ---- 导出完整汉化版 ----
    try:
        from gametl.auto import Cancelled as _C          # noqa: F401
        from gametl.auto import export_full_game as _efg
        import inspect as _ins0
        rec(callable(_efg), "导出完整汉化版")
        rec("clean" in _ins0.signature(_efg).parameters,
            "导出支持清空已存在的目标目录")
    except Exception as e:  # noqa: BLE001
        rec(False, f"导出完整汉化版: {e}")

    # ---- 导出目录自动命名 ----
    try:
        _d1 = export_target_for("某某游戏", Path(r"D:\G"))
        _d2 = export_target_for("某某游戏", Path(r"D:\G\某某游戏中文版"))
        rec(_d1.name == "某某游戏中文版", "导出目录自动加「中文版」后缀")
        rec(_d1.parent == Path(r"D:\G"), "导出的父目录就是用户选的位置")
        rec(_d2.parent == Path(r"D:\G"), "选中已是中文版目录时不再套一层")
        rec(safe_name('a/b:c*d') == "a_b_c_d", "游戏名非法字符被替换")
    except Exception as e:  # noqa: BLE001
        rec(False, f"导出目录命名: {e}")

    # ---- RPG Maker 界面文本：System.json / plugins.js / 插件源码 ----
    try:
        import inspect
        from gametl.extractors.rpgmaker import (JS_TEXT_MIN_LEN,  # noqa: F401
                                                RPGMakerExtractor,
                                                _js_text_ok)
        rec(_js_text_ok("クエスト確認"), "JS 层：日文界面词会被提取")
        rec(_js_text_ok("サブステータス"), "JS 层：全角界面词会被提取")
        rec(not _js_text_ok("customstatus"), "JS 层：英文标识符不会被提取")
        rec(not _js_text_ok("img/pictures/a.png"), "JS 层：路径不会被提取")
        rec(not _js_text_ok("true"), "JS 层：布尔字面量不会被提取")
        rec(not _js_text_ok("あ"),
            "JS 层：单字符被挡下（名称输入五十音表）")
        rec(JS_TEXT_MIN_LEN >= 2, "JS 层：最小长度下限生效")
        _sig = inspect.signature(RPGMakerExtractor.__init__)
        rec("enable_js" in _sig.parameters, "可开关插件参数提取")
        rec("enable_plugin_source" in _sig.parameters, "可开关插件源码提取")
        rec(_sig.parameters["enable_engine_js"].default is False,
            "引擎核心 rpg_*.js 默认不改")
        rec(callable(getattr(RPGMakerExtractor, "_read_plugins_js", None)),
            "plugins.js 可解析")
        rec(callable(getattr(RPGMakerExtractor, "_apply_param", None)),
            "嵌套 JSON 参数可回填")
        rec(callable(getattr(RPGMakerExtractor, "_extract_system_extra", None)),
            "System.json 顶层界面词（gameTitle/armorTypes 等）有提取")
    except Exception as e:  # noqa: BLE001
        rec(False, f"RPG Maker 界面文本: {e}")

    # ---- 翻译包（.gtpkg） ----
    try:
        import tempfile
        from gametl.core.models import EngineType as _ET2
        from gametl.core.models import Project as _P2
        from gametl.core.models import TextUnit as _TU2
        from gametl.core.package import (PACKAGE_EXT, PackageError,  # noqa: F401
                                         default_package_name,
                                         export_file_patch, export_package,
                                         import_into, read_manifest)
        rec(PACKAGE_EXT == ".gtpkg", "翻译包扩展名 .gtpkg")
        rec(callable(export_package) and callable(import_into),
            "翻译包导出/导入接口齐备")
        rec(callable(export_file_patch), "汉化文件补丁接口齐备")
        rec(default_package_name("测试").endswith(".gtpkg"), "默认包名带扩展名")
        rec("/" not in default_package_name("a/b:c"),
            "包名里的非法字符被清理")
        _u1 = _TU2("u1", "www/data/Map001.json", {"index": 1}, "こんにちは")
        _u1.translated = "你好"
        _proj = _P2(Path("."), _ET2.RPGMAKER_MV, [_u1])
        with tempfile.TemporaryDirectory() as _td:
            _pkg = Path(_td) / "t.gtpkg"
            _res = export_package(_proj, _pkg)
            rec(_res["units"] == 1 and _pkg.is_file(), "能打出真实的翻译包")
            rec(read_manifest(_pkg)["units_translated"] == 1,
                "manifest 记录条数正确")
            # 换一个 uid（模拟对方游戏版本不同），只能靠原文兜底
            _u2 = _TU2("u9", "www/data/Map001.json", {"index": 1}, "こんにちは")
            _proj2 = _P2(Path("."), _ET2.RPGMAKER_MV, [_u2])
            _imp = import_into(_proj2, _pkg)
            rec(_imp["by_uid"] == 0 and _imp["by_memory"] == 1
                and _u2.translated == "你好", "uid 失配时按原文兜底救回")
            rec(import_into(_P2(Path("."), _ET2.RPGMAKER_MV, [
                _TU2("u9", "f", {}, "こんにちは")]), _pkg,
                use_memory=False)["missed"] == 1, "可关掉原文兜底")
            _bad = Path(_td) / "bad.gtpkg"
            _bad.write_bytes(b"not a zip")
            try:
                read_manifest(_bad)
                rec(False, "损坏的翻译包应当报错")
            except PackageError:
                rec(True, "损坏的翻译包被正确拒绝")
    except Exception as e:  # noqa: BLE001
        rec(False, f"翻译包: {e}")

    # ---- 增量导出 ----
    try:
        from gametl.auto import EXPORT_SNAPSHOT
        import inspect as _ins2
        from gametl.auto import export_full_game as _efg2
        rec(EXPORT_SNAPSHOT.startswith("."), "导出快照文件名不污染游戏目录")
        rec("incremental" in _ins2.signature(_efg2).parameters,
            "导出支持增量模式")
    except Exception as e:  # noqa: BLE001
        rec(False, f"增量导出: {e}")

    # ---- 可逆安装器 / 汉化补丁包（v1.7） ----
    try:
        from gametl.core import patcher as _pt
        from gametl.core.package import (INSTALLER_EXE_NAME as _IEXE,
                                         _bundle_readme, export_patch_bundle)
        from gametl.installer import exe_dir as _exedir
        from gametl.installer import want_installer as _want
        from gametl.ui_common import VScroll as _VS  # noqa: F401
        from gametl.ui_common import fit_size as _fs

        rec(_pt.BACKUP_DIRNAME == "_汉化备份_原版", "原版备份目录名固定")
        rec(_pt.BAK_SUFFIX == ".bak",
            "备份一律 .bak 后缀（不会被当游戏本体再扫一遍）")
        rec(_IEXE == "汉化安装器.exe", "安装器文件名固定")
        rec(callable(_bundle_readme) and callable(_exedir), "补丁包说明/路径工具")
        rec(_want(["--installer"]) and not _want([]),
            "安装器模式按参数+文件名判定（源码运行不会误进）")

        _w, _h, _x, _y = _fs(1280, 720, 1180, 840, min_w=880, min_h=560)
        rec(_h <= 720 and _w <= 1280 and _y >= 0,
            f"14 寸笔记本（逻辑 1280×720）整窗放得下：{_w}×{_h}")

        # 端到端：合成小游戏 → 出包 → 安装 → 还原 → 逐字节比对
        import json as _json
        import tempfile as _tf
        import zipfile as _zf
        with _tf.TemporaryDirectory(prefix="sc17_") as _td:
            _g = Path(_td) / "sc_game"
            (_g / "www" / "data").mkdir(parents=True)
            (_g / "www" / "data" / "System.json").write_text(_json.dumps({
                "gameTitle": "テストゲーム",
                "terms": {"commands": ["攻撃", "防御", "アイテム"],
                          "messages": {"actionFailure": "できない！"}},
                "currencyUnit": "ゴールド",
            }, ensure_ascii=False), encoding="utf-8")
            _eng, _ = detect_engine(_g)
            _ex = _pt.make_extractor(_eng, _g)
            _us = _ex.extract()
            rec(len(_us) > 0, f"合成游戏提取到 {len(_us)} 条")
            _pj = Project(root=_g, engine=_eng, units=_us)
            for _u in _us:
                _u.translated = "译" + _u.original
            _br = export_patch_bundle(_pj, Path(_td) / "out", "合成游戏",
                                      installer_exe=None)
            _before = (_g / "www/data/System.json").read_bytes()
            _ap = _pt.apply_patch(_g, Path(_br["pkg"]))
            rec(_ap["files"] > 0 and _ap["by_uid"] == _ap["total"],
                f"安装写入 {_ap['files']} 个文件、{_ap['filled']}/"
                f"{_ap['total']} 条全靠 uid 命中")
            _now = _json.loads((_g / "www/data/System.json")
                               .read_text(encoding="utf-8"))
            rec(_now["terms"]["commands"][0] == "译攻撃",
                "菜单术语真的写进 System.json 了")
            rec(_pt.patch_status(_g)["installed"] is True, "安装后状态 = 已安装")
            _rv = _pt.revert_patch(_g)
            rec((_g / "www/data/System.json").read_bytes() == _before,
                "还原后与原文件逐字节一致")
            rec(_pt.patch_status(_g)["installed"] is False,
                "还原后状态 = 未安装（备份仍在，可以再装）")
            _zp = Path(_br["zip"])
            with _zf.ZipFile(_zp) as _z:
                _names = _z.namelist()
            rec(any(n.endswith("安装说明.txt") for n in _names),
                "补丁包 zip 内含安装说明")
    except Exception as e:  # noqa: BLE001
        rec(False, f"可逆安装器/补丁包: {e}")

    # ---- 孤立推理进程的清理 ----
    try:
        from gametl.runtime_manager import RuntimeManager as _RMx
        from gametl.runtime_manager import RUNNER_MIN_AGE_SEC as _AGE
        rec(callable(getattr(_RMx, "list_runners", None)), "进程枚举 list_runners")
        rec(callable(getattr(_RMx, "reap_orphan_runners", None)),
            "孤儿进程清理 reap_orphan_runners")
        rec(callable(getattr(_RMx, "_pick_orphans", None)), "孤儿判定 _pick_orphans")
        _raw = ("11|1|D:\\x\\runtime\\lib\\ollama\\llama-server.exe|DEAD|900\n"
                "12|9|D:\\x\\runtime\\lib\\ollama\\llama-server.exe|ollama.exe|900\n"
                "垃圾行\n")
        _rows = _RMx._parse_runners(_raw)
        rec(len(_rows) == 2 and _rows[0]["parent"] == "DEAD",
            "进程清单解析（含坏行丢弃）")
        _rt = _RMx(Path(r"D:\x"))
        _fake = [
            {"pid": 1, "ppid": 0, "parent": "DEAD", "age": _AGE + 10,
             "path": r"D:\x\runtime\lib\ollama\llama-server.exe"},
            {"pid": 2, "ppid": 9, "parent": "ollama.exe", "age": _AGE + 10,
             "path": r"D:\x\runtime\lib\ollama\llama-server.exe"},
            {"pid": 3, "ppid": 0, "parent": "DEAD", "age": _AGE + 10,
             "path": r"C:\Other\llama-server.exe"},
            {"pid": 4, "ppid": 0, "parent": "DEAD", "age": 1,
             "path": r"D:\x\runtime\lib\ollama\llama-server.exe"},
        ]
        rec(_rt._pick_orphans(_fake) == [1],
            "只有「自家 + 父已死 + 够老」的才算孤儿")
    except Exception as e:  # noqa: BLE001
        rec(False, f"孤立推理进程清理: {e}")

    # ---- 速度显示（滑动窗口）----
    try:
        from collections import deque as _dq
        _h = _dq(maxlen=64)
        for _i in range(21):
            _h.append((float(_i), _i))
            window_rate(_h, float(_i), _i)
        for _i in range(21, 121):
            _t = 20 + (_i - 20) * 0.1
            _h.append((_t, _i))
            window_rate(_h, _t, _i)
        _inst = window_rate(_h, 30.0, 120)
        rec(abs(_inst - 10.0) < 0.6,
            f"稳态瞬时速度正确（{_inst:.1f} 条/秒，累计平均只有 4.0）")
        _h2 = _dq(maxlen=64)
        _h2.append((0.0, 0))
        rec(window_rate(_h2, 0.3, 5) == 0.0, "窗口过短时不给读数")
    except Exception as e:  # noqa: BLE001
        rec(False, f"滑动窗口测速: {e}")

    # ---- 分栏默认位置（「打开只有日志」的回归守卫）----
    try:
        from gametl.ui_common import (
            SASH_MIN_LEFT as _SML, SASH_MIN_RIGHT as _SMR,
            clamp_sash as _cs, button_cols as _bc, sash_target as _st,
        )
        _p = _st(1144)
        rec(_p >= 460,
            f"默认分栏：左栏 {_p}px（卡片约需 460，且**不是 0**）")
        rec(1144 - _p >= _SMR, f"默认分栏：右栏还剩 {1144 - _p}px 给日志")
        rec(_st(1) == 0 and _st(0) == 0, "容器未布局时不瞎猜（返回 0）")
        rec(_st(1144, 700) == 700, "记住用户拖过的位置")
        rec(_st(1144, 9999) <= 1144 - _SMR, "保存值过大时夹回（右栏不许被挤没）")
        rec(_cs(1144, 0) >= _SML, "拖到 0 会被夹回最小宽度")
        rec(_bc([110] * 4, 466) == 4 and _bc([110] * 4, 350) == 2,
            "按钮行窄了会折行（4 → 2+2），不会切掉最后一颗")
        rec(_bc([150, 160, 130], 340) == 2, "3 颗按钮 340px 下折成 2+1")
        _pp = Path(tempfile.gettempdir()) / "gt_selfcheck_prefs.json"
        rec(write_prefs(_pp, {"sash": 512}) and read_prefs(_pp).get("sash") == 512,
            "界面偏好能存能读（记住分栏位置）")
        _pp.write_text("{坏 json", encoding="utf-8")
        rec(read_prefs(_pp) == {}, "偏好文件损坏时不抛异常（界面不能被它拖垮）")
        try:
            _pp.unlink()
        except OSError:
            pass
        rec(callable(install_thread_hook), "线程异常钩子可安装（关窗后不留堆栈）")
    except Exception as e:  # noqa: BLE001
        rec(False, f"分栏布局: {e}")

    # ---- 安装器/界面的分栏参数 ----
    try:
        from gametl import installer as _ins
        rec(_ins.PANE_WEIGHT_TOP > 0,
            "安装器上栏权重 > 0（weight=0 会让它塌成 1px）")
        rec(_ins.SASH_MIN_TOP >= 180 and _ins.SASH_MIN_BOTTOM >= 100,
            f"安装器上下栏下限合理（{_ins.SASH_MIN_TOP}/{_ins.SASH_MIN_BOTTOM}）")
    except Exception as e:  # noqa: BLE001
        rec(False, f"安装器分栏参数: {e}")

    # ---- 显存不足提醒 ----
    try:
        _tip = App._offload_tip({"size_gb": 1.85, "vram_gb": 1.43})
        rec(bool(_tip) and "显存" in _tip, "部分上卡时给出显存提醒")
        rec(App._offload_tip({"size_gb": 1.85, "vram_gb": 1.85}) == "",
            "全部上卡时不打扰用户")
        rec(App._offload_tip({}) == "", "引擎信息缺失时不误报")
    except Exception as e:  # noqa: BLE001
        rec(False, f"显存提醒: {e}")

    # ---- 核心依赖 ----
    for mod, label in [
        ("gametl.core.xp3", "XP3 封包读写"),
        ("gametl.core.rpa", "RPA 封包读写"),
        ("gametl.core.archive", "封包统一入口"),
        ("gametl.core.detect", "引擎识别"),
        ("gametl.core.scan", "有界扫描"),
        ("gametl.core.validate", "两层校验"),
        ("gametl.profiles", "性能档位"),
        ("gametl.runtime_manager", "便携运行时"),
        ("gametl.translators.ollama_backend", "翻译后端"),
        ("gametl.translators.quality", "模型抽检"),
        ("gametl.auto", "自动管线"),
    ]:
        try:
            __import__(mod)
            rec(True, f"导入 {label} ({mod})")
        except Exception as e:  # noqa: BLE001
            rec(False, f"导入 {label} ({mod}): {e}")

    try:
        from gametl.extractors.kirikiri import KiriKiriExtractor      # noqa: F401
        from gametl.extractors.renpy import RenPyExtractor            # noqa: F401
        from gametl.extractors.rpgmaker import RPGMakerExtractor      # noqa: F401
        rec(True, "导入三个提取器")
    except Exception as e:  # noqa: BLE001
        rec(False, f"导入提取器: {e}")

    # ---- 性能档位与硬件 ----
    try:
        from gametl.profiles import (
            PROFILES, detect_hardware, is_translation_model, model_label,
            pick_model, recommend_profile,
        )
        hw = detect_hardware()
        rec(True, f"硬件检测: {hw.summary()}")
        rec(True, f"推荐档位: {PROFILES[recommend_profile(hw)].name}")
        rec(len(PROFILES) == 4, f"四档已定义: {len(PROFILES)} 档")
        _fake = [Path("Qwen2.5-7B-Instruct-Q4_K_M.gguf"),
                 Path("HY-MT2-1.8B-Q4_K_M.gguf")]
        rec(pick_model(_fake, "auto", prefer_small=True).name
            == "HY-MT2-1.8B-Q4_K_M.gguf",
            "模型自动挑选（小模型倾向）")
        rec(is_translation_model("HY-MT2-1.8B-Q4_K_M.gguf")
            and not is_translation_model("Qwen2.5-7B-Instruct-Q4_K_M.gguf"),
            "翻译专用模型识别")
        rec(model_label("Hy-MT2-1.8B-Q4_K_M.gguf") == "Hy-MT2-1.8B",
            "模型名压缩为界面短标签")
    except Exception as e:  # noqa: BLE001
        rec(False, f"性能档位/硬件检测: {e}")

    # ---- 校验规则 ----
    try:
        from gametl.core.validate import audit_units, check_immediate
        assert check_immediate("こんにちは", "你好") is None
        assert check_immediate("テスト", "") == "译文为空"
        rep = audit_units([], glossary={})
        rec(True, f"校验规则可用（空集合审计: {rep.summary()}）")
    except Exception as e:  # noqa: BLE001
        rec(False, f"校验规则: {e}")

    # ---- 预过滤 / 去重 ----
    try:
        from gametl.translators.ollama_backend import should_skip
        rec(should_skip("") and should_skip("12.3") and should_skip("你好")
            and not should_skip("こんにちは") and not should_skip("Potion"),
            "预过滤规则（跳空/数字/中文，不跳日文/英文）")
    except Exception as e:  # noqa: BLE001
        rec(False, f"预过滤: {e}")

    # ---- 便携运行时 ----
    app_home = Path(os.environ.get("GAMETL_HOME", APP_DIR))
    try:
        from gametl.runtime_manager import RuntimeManager
        rm = RuntimeManager(app_home)
        rec(rm.exe_path() is not None, f"内置翻译引擎: {rm.exe_path() or '未找到'}")
        ggufs = rm.list_gguf()
        rec(bool(ggufs), f"models\\ 内模型: {[g.name for g in ggufs] or '未找到'}")
        rec(True, f"当前档位: {rm.spec.name}（并发 {rm.spec.num_parallel}）")
        _pick = rm.resolve_model()
        rec(True, f"模型选择 {rm.model_pref} → "
                  f"{_pick.name if _pick else '（无可用模型）'}")
    except Exception as e:  # noqa: BLE001
        rec(False, f"便携运行时: {e}")

    # ---- XP3 往返 ----
    try:
        import tempfile

        from gametl.core.xp3 import XP3Archive
        tmp = Path(tempfile.gettempdir()) / "gametl_selfcheck.xp3"
        XP3Archive.pack([("t.ks", "テスト".encode("utf-16-le"))], tmp)
        arc = XP3Archive(tmp)
        arc.read_index()
        d = tmp.parent / "gametl_selfcheck_out"
        arc.extract_all(d)
        got = (d / "t.ks").read_bytes().decode("utf-16-le")
        rec(got == "テスト", f"XP3 打包/解包往返测试 (读到: {got})")
    except Exception as e:  # noqa: BLE001
        rec(False, f"XP3 往返测试: {e}")

    gl = BASE_DIR / "gametl" / "glossary.example.json"
    rec(gl.exists(), f"内置术语表: {gl}")

    try:
        import tkinter
        rec(True, f"tkinter {tkinter.TkVersion}")
    except Exception as e:  # noqa: BLE001
        rec(False, f"tkinter: {e}")

    # ---- Unity 引擎（.assets 里 TextAsset 的提取 + 回填）----
    try:
        import UnityPy as _UP
        rec(True, f"UnityPy {getattr(_UP, '__version__', '?')}")
        _pkg = Path(_UP.__file__).parent
        rec((_pkg / "resources" / "uncompressed.tpk").is_file(),
            "UnityPy 类型库 uncompressed.tpk 已随包")
        for _m in ("lz4", "brotli", "etcpak", "texture2ddecoder", "tabulate",
                   "fsspec", "PIL"):
            try:
                __import__(_m)
                rec(True, f"UnityPy 依赖 {_m}")
            except Exception as _me:  # noqa: BLE001
                rec(False, f"UnityPy 依赖 {_m}: {_me}")
        rec(callable(getattr(_UP, "load", None)), "UnityPy.load 可用")
        from gametl.extractors.unity import UnityExtractor as _UE
        rec(callable(getattr(_UE, "extract", None)), "Unity 提取器可用")
        from gametl.extractors.base import wb_replaced as _wr, wb_stats as _ws
        _st = _ws(files_written=1, replaced=2, changed=["a"])
        rec(_wr(_st) == 2 and _wr({"lines_replaced": 3}) == 3,
            "回填统计字段统一（兼容旧字段名）")
    except Exception as e:  # noqa: BLE001
        rec(False, f"UnityPy: {e}")

    # 真机验证通道：给一个真实 Unity 游戏目录就顺手跑一遍提取 + 回填
    _sample = os.environ.get("GAMETL_UNITY_SAMPLE")
    if _sample:
        import shutil as _sh
        _td = tempfile.mkdtemp(prefix="gt_unity_sc_")
        try:
            from gametl.extractors.base import wb_replaced as _wr
            from gametl.extractors.unity import UnityExtractor as _UE2
            from gametl.core.models import Project as _P2, EngineType as _ET2
            _ex = _UE2(Path(_sample))
            _units = _ex.extract()
            rec(len(_units) > 0, f"Unity 真机提取 {len(_units)} 条文本")
            if _units:
                _head = _units[:20]
                for _u in _head:
                    if _u.location.get("form") == "unitypy":
                        _u.translated = "【自检】" + _u.original
                _proj = _P2(root=Path(_sample), engine=_ET2.UNITY, units=_head)
                _stats = _ex.write_back(_proj, Path(_td))
                rec(_stats.get("files_written", 0) > 0,
                    f"Unity 真机回填 {_stats.get('files_written')} 个文件"
                    f"（{_wr(_stats)} 处）")
                _hit = 0
                for _p in Path(_td).rglob("*.assets"):
                    _env = _UP.load(str(_p))
                    for _o in _env.objects:
                        if _o.type.name != "TextAsset":
                            continue
                        _d = _o.read()
                        if "【自检】" in bytes(_d.m_Script).decode(
                                "utf-8", "ignore"):
                            _hit += 1
                rec(_hit > 0, f"Unity 真机回填后重新解包命中 {_hit} 个资产")
        except Exception as e:  # noqa: BLE001
            rec(False, f"Unity 真机验证: {e}")
        finally:
            # UnityPy 会持有文件句柄，清理失败无所谓，别把它算进自检结果
            _sh.rmtree(_td, ignore_errors=True)

    lines.append("=== 自检完成 ===")
    try:
        Path(out).write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        pass


if __name__ == "__main__":
    main()
