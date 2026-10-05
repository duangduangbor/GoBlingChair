# -*- coding: utf-8 -*-
"""汉化安装器 —— 独立运行的「一键装汉化 / 一键还原原版」。

它和主程序是同一个 exe。主程序启动时若发现:

* 命令行带了 ``--installer`` / ``--patch``，或
* 自己的文件名里有「安装器」三个字（导出补丁包时就是这么命名的），或
* 自己不在软件目录里、而同目录又躺着 ``.gtpkg``

就切到这个界面。

别人的电脑上**不需要装模型、不需要联网、不需要装这个软件** ——
把补丁包文件夹解压到游戏目录旁边，双击里面的 exe 就行。
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
from pathlib import Path

# tkinter 只在实际开界面时才是必需的。``want_installer`` / ``exe_dir``
# 是纯逻辑（启动分流要靠它们），所以不能因为环境没 GUI 就让整个模块
# 导入失败。
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
    HAS_TK = True
except ImportError:                      # pragma: no cover - 无 GUI 环境
    tk = ttk = filedialog = messagebox = scrolledtext = None   # type: ignore
    HAS_TK = False

from .core import patcher
from .ui_common import (
    VScroll, clamp_sash, fit_window, install_thread_hook, sash_target,
    wrap_to_parent,
)

APP_NAME = "汉化安装器"

# 上下分栏的权重与尺寸约束。
# **上栏不能是 weight=0** —— ttk 只按权重分配空间，weight=0 的栏会永远停在
# 「首次布局时的尺寸」，而首次布局发生在窗口还没映射、子控件还没塞进容器的
# 时候，于是它停在 1px。表现出来就是「打开只有日志，设置区得自己拖出来」。
PANE_WEIGHT_TOP = 3
PANE_WEIGHT_BOTTOM = 2
SASH_MIN_TOP = 210          # 上面至少要放得下「游戏文件夹 / 翻译包」两张卡片
SASH_MIN_BOTTOM = 130       # 下面至少要看得见几行日志
SASH_RATIO_TOP = 0.60
SASH_MAX_TOP = 520
SASH_RETRY_MS = 40
SASH_RETRY_MAX = 30

BG = "#f5f6f8"
CARD = "#ffffff"
TEXT = "#1f2328"
MUTED = "#6b7280"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
OK_GREEN = "#16a34a"
WARN_AMBER = "#b45309"
ERR_RED = "#dc2626"
IDLE_BTN = "#e5e7eb"
IDLE_BTN_HOVER = "#d1d5db"


# ---------------------------------------------------------------- 环境

def exe_dir() -> Path:
    """这个程序自己躺在哪个目录。"""
    if getattr(sys, "frozen", False):
        try:
            return Path(sys.executable).resolve().parent
        except OSError:
            return Path.cwd()
    return Path(__file__).resolve().parent.parent


def want_installer(argv: list[str] | None = None) -> bool:
    """这次启动该不该进安装器界面。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("--installer", "--patch", "--install") for a in args):
        return True
    for a in args:
        if a.startswith("--"):                 # 其它参数说明是别的方式启动
            break
    d = exe_dir()
    try:
        stem = Path(sys.executable).stem
    except (OSError, ValueError):
        stem = ""
    if "安装器" in stem:
        return True
    # 兜底：不在软件目录（没有 runtime/）而身边有翻译包 —— 像补丁包。
    # 只在打包成 exe 后才启用：源码调试时项目根目录常躺着测试用的
    # .gtpkg，不该因此把主界面顶掉。
    has_runtime = (d / "runtime").is_dir() or (d / "models").is_dir()
    if not has_runtime and getattr(sys, "frozen", False):
        try:
            if any(d.glob("*.gtpkg")) or any(d.parent.glob("*.gtpkg")):
                return True
        except OSError:
            pass
    return False


# ---------------------------------------------------------------- 界面

class InstallerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.base = exe_dir()
        self.game_dir: Path | None = None
        self.pkg: Path | None = None
        self.pkg_meta: dict | None = None     # 翻译包的 manifest（含游戏指纹）
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.logq: queue.Queue = queue.Queue()
        self.status = patcher.patch_status(self.base)

        self._setup_ui()
        self._pump()
        self._autodetect()

    # ---- 布局 ----

    def _setup_ui(self) -> None:
        r = self.root
        r.title(APP_NAME)
        r.configure(bg=BG)
        fit_window(r, 780, 640, min_w=660, min_h=520)

        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=TEXT,
                     font=("Microsoft YaHei UI", 10))
        st.configure("Title.TLabel", background=BG, foreground=TEXT,
                     font=("Microsoft YaHei UI", 16, "bold"))
        st.configure("Sub.TLabel", background=BG, foreground=MUTED,
                     font=("Microsoft YaHei UI", 9))
        st.configure("TLabelframe", background=BG)
        st.configure("TLabelframe.Label", background=BG, foreground=MUTED,
                     font=("Microsoft YaHei UI", 9, "bold"))
        st.configure("Horizontal.TProgressbar", troughcolor="#e5e7eb",
                     background=ACCENT, thickness=16)

        # ---- 标题（固定） ----
        head = ttk.Frame(r)
        head.pack(fill="x", padx=18, pady=(14, 6))
        ttk.Label(head, text=f"🎮 {APP_NAME}", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            head,
            text="把汉化装进游戏，想换回原语言随时按「还原原版」",
            style="Sub.TLabel").pack(anchor="w", pady=(2, 0))

        # ---- 大状态条（固定） ----
        self.state_card = tk.Frame(r, bg=CARD, highlightbackground="#e5e7eb",
                                   highlightthickness=1)
        self.state_card.pack(fill="x", padx=18, pady=(6, 0))
        self.state_label = tk.Label(
            self.state_card, text="", bg=CARD, fg=TEXT, anchor="w",
            justify="left",
            font=("Microsoft YaHei UI", 11, "bold"))
        self.state_label.pack(fill="x", padx=14, pady=10)
        # 状态行可能很长（「已装汉化 · 315 个文件 · 2026-10-04 19:20」之类），
        # 写死 wraplength 在窄屏上会被切掉，交给容器宽度决定
        wrap_to_parent(self.state_label, self.state_card, pad=28)

        # ---- 中间：设置区（可滚动）+ 日志 ----
        # 上栏必须给非 0 权重：ttk 只按权重分配空间，weight=0 的栏会永远
        # 停在「首次布局时的尺寸」—— 而此时窗口还没映射、子控件还没塞进去，
        # 于是它停在 1px，用户看到的是「只有日志，设置区得自己拖出来」。
        mid = ttk.PanedWindow(r, orient="vertical")
        mid.pack(fill="both", expand=True, padx=18, pady=(8, 4))
        self._pane = mid

        top = ttk.Frame(mid)
        mid.add(top, weight=PANE_WEIGHT_TOP)

        sc = VScroll(top, bg=BG)
        sc.pack(fill="both", expand=True)
        body = sc.body

        # 游戏目录
        c1 = tk.Frame(body, bg=CARD, highlightbackground="#e5e7eb",
                      highlightthickness=1)
        c1.pack(fill="x", pady=(0, 8))
        i1 = tk.Frame(c1, bg=CARD)
        i1.pack(fill="x", padx=14, pady=10)
        tk.Label(i1, text="游戏文件夹", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        row1 = tk.Frame(i1, bg=CARD)
        row1.pack(fill="x", pady=(6, 0))
        self.game_var = tk.StringVar(value="（正在查找…）")
        tk.Entry(row1, textvariable=self.game_var, state="readonly",
                 readonlybackground="#f9fafb", relief="solid", bd=1,
                 font=("Microsoft YaHei UI", 10)).pack(
            side="left", fill="x", expand=True, ipady=5)
        tk.Button(row1, text="选择…", command=self.pick_game, bg=ACCENT,
                  fg="white", activebackground=ACCENT_DARK,
                  activeforeground="white", relief="flat", cursor="hand2",
                  font=("Microsoft YaHei UI", 10, "bold"),
                  padx=12, pady=5).pack(side="left", padx=(8, 0))
        # 指纹比对结论：光说「像个游戏目录」不够，得说清「是不是**这个**游戏」
        self.match_label = tk.Label(
            i1, text="", bg=CARD, fg=MUTED, anchor="w", justify="left",
            font=("Microsoft YaHei UI", 9))
        self.match_label.pack(fill="x", pady=(6, 0))
        wrap_to_parent(self.match_label, i1, pad=16)

        # 翻译包
        c2 = tk.Frame(body, bg=CARD, highlightbackground="#e5e7eb",
                      highlightthickness=1)
        c2.pack(fill="x", pady=(0, 8))
        i2 = tk.Frame(c2, bg=CARD)
        i2.pack(fill="x", padx=14, pady=10)
        tk.Label(i2, text="汉化翻译包（.gtpkg）", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        row2 = tk.Frame(i2, bg=CARD)
        row2.pack(fill="x", pady=(6, 0))
        self.pkg_var = tk.StringVar(value="（正在查找…）")
        tk.Entry(row2, textvariable=self.pkg_var, state="readonly",
                 readonlybackground="#f9fafb", relief="solid", bd=1,
                 font=("Microsoft YaHei UI", 10)).pack(
            side="left", fill="x", expand=True, ipady=5)
        tk.Button(row2, text="选择…", command=self.pick_pkg, bg=ACCENT,
                  fg="white", activebackground=ACCENT_DARK,
                  activeforeground="white", relief="flat", cursor="hand2",
                  font=("Microsoft YaHei UI", 10, "bold"),
                  padx=12, pady=5).pack(side="left", padx=(8, 0))

        # 按钮固定在窗口底部 —— 不能放进上面那个滚动区：卡片一多，用户就得
        # 先滚动才够得着「安装汉化」，而这个按钮恰恰是整块屏幕的主角。
        btns = ttk.Frame(r)
        btns.pack(side="bottom", fill="x", padx=18, pady=(2, 12))
        self.install_btn = tk.Button(
            btns, text="✔  安装汉化", command=self.install, bg=OK_GREEN,
            fg="white", activebackground="#128a3e", activeforeground="white",
            relief="flat", cursor="hand2",
            font=("Microsoft YaHei UI", 12, "bold"), padx=22, pady=9)
        self.install_btn.pack(side="left")
        self.revert_btn = tk.Button(
            btns, text="↺  还原原版", command=self.revert, bg="#fef3c7",
            fg=WARN_AMBER, activebackground="#fde68a", relief="flat",
            cursor="hand2", font=("Microsoft YaHei UI", 11), padx=18, pady=9,
            state="disabled")
        self.revert_btn.pack(side="left", padx=(8, 0))
        tk.Button(btns, text="打开游戏目录", command=self.open_game,
                  bg=IDLE_BTN, fg=TEXT, activebackground=IDLE_BTN_HOVER,
                  relief="flat", cursor="hand2",
                  font=("Microsoft YaHei UI", 10), padx=14, pady=9).pack(
            side="right")

        # ---- 日志 ----
        bot = ttk.Frame(mid)
        mid.add(bot, weight=PANE_WEIGHT_BOTTOM)
        logf = ttk.LabelFrame(bot, text="运行日志")
        logf.pack(fill="both", expand=True)
        self.log = scrolledtext.ScrolledText(
            logf, height=7, wrap="word", font=("Consolas", 9),
            bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
            relief="flat", bd=0)
        self.log.pack(fill="both", expand=True, padx=6, pady=6)
        for tag, color in (("info", "#d4d4d4"), ("ok", "#4ade80"),
                           ("warn", "#fbbf24"), ("err", "#f87171")):
            self.log.tag_config(tag, foreground=color)
        self.log.configure(state="disabled")

        # ---- 底部进度（固定） ----
        self.progress = ttk.Progressbar(r, style="Horizontal.TProgressbar",
                                        maximum=100, value=0)
        self.progress.pack(fill="x", padx=18, pady=(0, 12))

        self._log("把本文件夹和游戏放在一起，然后点「安装汉化」。", "info")
        self._log("安装前会自动备份原版；想换回原语言，点「还原原版」。", "info")

        # 分栏落位（窗口映射后才有真实高度，所以要重试，见 _place_sash）
        self._sash_locked = False
        mid.bind("<ButtonRelease-1>", self._on_sash_release, add="+")
        r.after(SASH_RETRY_MS, self._place_sash)

    # ---------------- 上下分栏 ----------------

    def _place_sash(self, tries: int = 0) -> None:
        """把分隔条落到设计位置（上面看得见卡片、下面留得住日志）。

        必须**重试**：窗口还没映射时 ``sashpos()`` 会抛 ``TclError``，而
        布局尺寸只有映射之后才算得出来。只设一次 + 吞异常，就会永久停在
        失效的那一次，上栏宽度 0 且再无补救。
        """
        if getattr(self, "_sash_locked", False):
            return
        try:
            ph = self._pane.winfo_height()
        except tk.TclError:
            return
        if ph > 1:
            want = sash_target(ph, None, min_first=SASH_MIN_TOP,
                               min_second=SASH_MIN_BOTTOM,
                               ratio=SASH_RATIO_TOP, max_first=SASH_MAX_TOP)
            if want > 0:
                try:
                    self._pane.sashpos(0, want)
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
        """松手时夹一下：ttk 分栏没有 minsize，不夹能被拖成 1px 且拖不回来。"""
        try:
            ph = self._pane.winfo_height()
            cur = int(self._pane.sashpos(0))
        except tk.TclError:
            return
        if ph <= 1:
            return
        want = clamp_sash(ph, cur, min_first=SASH_MIN_TOP,
                          min_second=SASH_MIN_BOTTOM)
        if want != cur:
            try:
                self._pane.sashpos(0, want)
            except tk.TclError:
                return
        self._sash_locked = True

    # ---- 自动发现 ----

    def _autodetect(self) -> None:
        d = self.base
        # 游戏目录：先看自己这层，再往上看
        g = patcher.find_game_dir(d)
        if g is None and d.parent != d:
            g = patcher.find_game_dir(d.parent)
        if g is not None:
            self._set_game(g)
            self._log(f"自动找到了游戏目录：{g}", "ok")
        else:
            self.game_var.set("（没自动找到，请手动选择游戏文件夹）")
            self._log("没能自动认出游戏目录，请点「选择…」手动指一下。"
                      "（把本程序放在游戏根目录、或游戏根目录的子文件夹里，"
                      "再双击它，通常就能自动找到。）", "warn")

        # 翻译包：自己这层 → 上一层
        found: list[Path] = []
        for where in (d, d.parent):
            try:
                found = sorted(where.glob("*.gtpkg"))
            except OSError:
                found = []
            if found:
                break
        if len(found) == 1:
            self._set_pkg(found[0])
        elif len(found) > 1:
            self._set_pkg(found[0])
            self._log(f"同目录有 {len(found)} 个翻译包，默认选了 "
                      f"{found[0].name}；不对就点「选择…」换一个。", "warn")
        else:
            self.pkg_var.set("（没找到 .gtpkg，请手动选择）")

    def _set_game(self, p: Path) -> None:
        self.game_dir = Path(p)
        self.game_var.set(str(self.game_dir))
        self.status = patcher.patch_status(self.game_dir)
        self._render_state()

    def _set_pkg(self, p: Path) -> None:
        self.pkg = Path(p)
        self.pkg_var.set(str(self.pkg))
        self.pkg_meta = self._read_pkg_meta(self.pkg)
        self._render_state()

    @staticmethod
    def _read_pkg_meta(p: Path) -> dict | None:
        """读翻译包的 manifest（含游戏指纹）。读不出就当没有，不打断用户。"""
        try:
            from .core.package import read_manifest
            return read_manifest(p)
        except Exception:  # noqa: BLE001
            return None

    def _verify_root(self, p: Path) -> tuple[str, str]:
        """拿翻译包的指纹比对目录，返回 ``(结论, 说明)``。"""
        fp = (self.pkg_meta or {}).get("fingerprint") or {}
        return patcher.fingerprint_match(fp, p)

    def _render_state(self) -> None:
        if self.game_dir is None:
            self.state_label.configure(
                text="先告诉我要装在哪个游戏里。", fg=MUTED)
            self.match_label.configure(text="", fg=MUTED)
            self.revert_btn.configure(state="disabled")
            return
        # 指纹结论：光「像个游戏目录」不够，要说清「是不是**这个**游戏」
        if self.pkg_meta is None:
            self.match_label.configure(
                text=("（翻译包选了之后，会自动核对是不是同一个游戏）"
                      if self.pkg is None else ""), fg=MUTED)
        else:
            verdict, why = self._verify_root(self.game_dir)
            if verdict == "match":
                self.match_label.configure(text=f"✅ 与翻译包匹配：{why}",
                                           fg=OK_GREEN)
            elif verdict == "partial":
                self.match_label.configure(
                    text=f"⚠️ {why} —— 版本可能不同，能装但留个心眼", fg=WARN_AMBER)
            elif verdict == "mismatch":
                self.match_label.configure(
                    text=f"❌ {why}。是不是选错文件夹，或者包不是这个游戏的？",
                    fg=ERR_RED)
            else:
                self.match_label.configure(text=why, fg=MUTED)
        if self.status.get("installed"):
            self.state_label.configure(
                text=(f"✅ 这个游戏已经装了汉化（{self.status['files']} 个文件，"
                      f"装于 {self.status['created_at']}）\n"
                      f"想换回原来的语言，点下面「还原原版」就行。"),
                fg=OK_GREEN)
            self.revert_btn.configure(state="normal")
            self.install_btn.configure(text="↻  重新安装汉化")
        else:
            ok = self.pkg is not None
            self.state_label.configure(
                text=("这个游戏还没装过汉化。点「安装汉化」就开始 —— "
                      "装之前会自动把原版备份下来。" if ok else
                      "还没找到翻译包，先选一个 .gtpkg 文件。"),
                fg=TEXT if ok else MUTED)
            self.revert_btn.configure(state="disabled")
            self.install_btn.configure(text="✔  安装汉化")

    # ---- 选择 ----

    def pick_game(self) -> None:
        d = filedialog.askdirectory(
            title="选择游戏文件夹（exe 和游戏数据所在的那一层）",
            initialdir=str(self.game_dir or self.base))
        if not d:
            return
        p = Path(d)
        if not patcher.looks_like_game(p):
            g = patcher.find_game_dir(p, max_up=2)
            if g is not None:
                p = g
            else:
                # 引擎特征认不出来 ≠ 选错了。先用翻译包的指纹对一次：
                # 对得上就静默接受（比如老 Unity / 冷门引擎的目录），
                # 免得用户被一句「不太像游戏目录」反复劝退。
                verdict, why = self._verify_root(p)
                if verdict == "match":
                    self._log(f"目录特征不明显，但它和翻译包吻合（{why}），用它。",
                              "ok")
                elif not messagebox.askyesno(
                        APP_NAME,
                        f"这个文件夹看着不太像游戏根目录：\n{p}\n\n"
                        "仍然使用它吗？"):
                    return
        self._set_game(p)

    def pick_pkg(self) -> None:
        f = filedialog.askopenfilename(
            title="选择汉化翻译包",
            initialdir=str(self.pkg.parent if self.pkg else self.base),
            filetypes=[("翻译包", "*.gtpkg"), ("所有文件", "*.*")])
        if f:
            self._set_pkg(Path(f))

    def open_game(self) -> None:
        p = self.game_dir or self.base
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except OSError:
            pass

    # ---- 日志 ----

    def _log(self, msg: str, level: str = "info") -> None:
        self.logq.put((msg, level))

    def _pump(self) -> None:
        try:
            while True:
                msg, level = self.logq.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", msg + "\n", level)
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self._pump)

    # ---- 干活 ----

    def _busy(self, on: bool) -> None:
        st = "disabled" if on else "normal"
        self.install_btn.configure(state=st)
        self.revert_btn.configure(
            state=("disabled" if on or not self.status.get("installed")
                   else "normal"))

    def install(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if self.game_dir is None:
            messagebox.showwarning(APP_NAME, "请先选择游戏文件夹。")
            return
        if self.pkg is None:
            messagebox.showwarning(APP_NAME, "请先选择翻译包（.gtpkg）。")
            return

        already = bool(self.status.get("installed"))
        if already and not messagebox.askyesno(
                APP_NAME,
                "这个游戏已经装过汉化了。\n\n"
                "重装会先还原成原版，再装一次新包 —— 这样之后还能正常还原。\n"
                "现在重装吗？"):
            return
        if not already and not messagebox.askyesno(
                APP_NAME,
                f"即将把汉化装进：\n{self.game_dir}\n\n"
                f"用的包：{self.pkg.name}\n\n"
                "安装前会把原版文件备份到游戏目录下的 "
                f"「{patcher.BACKUP_DIRNAME}」文件夹，随时可以还原。\n\n"
                "开始安装吗？"):
            return

        self.cancel_event.clear()
        self._busy(True)
        self.progress.configure(value=0)
        self._log("═" * 46, "info")
        self.worker = threading.Thread(
            target=self._install_worker, args=(already,), daemon=True)
        self.worker.start()

    def _install_worker(self, need_revert: bool) -> None:
        try:
            if need_revert:
                self._log("先还原成原版…", "info")
                patcher.revert_patch(
                    self.game_dir, on_log=lambda m: self._log(m),
                    on_progress=lambda c, n, d: self.root.after(
                        0, self._progress, c, n, d))
            res = patcher.apply_patch(
                self.game_dir, self.pkg,
                on_log=lambda m: self._log(m),
                on_progress=lambda c, n, d: self.root.after(
                    0, self._progress, c, n, d),
                cancel_event=self.cancel_event)
            self.root.after(0, self._install_done, res)
        except Exception as e:  # noqa: BLE001
            self.root.after(0, self._fail, str(e))

    def _progress(self, cur: int, total: int, desc: str) -> None:
        pct = (cur / total * 100) if total else 0
        self.progress.configure(value=min(100, pct))
        if desc:
            self._log(f"  {desc}")

    def _install_done(self, res: dict) -> None:
        self._busy(False)
        self.progress.configure(value=100)
        self.status = patcher.patch_status(self.game_dir)
        self._render_state()
        self._log(f"✅ 安装完成：写入 {res['files']} 个文件，"
                  f"覆盖 {res['filled']}/{res['total']} 条文本"
                  f"（{res['ratio'] * 100:.1f}%）", "ok")
        self._log(f"   原版备份在：{res['backup']}", "info")
        self._log("   想换回原来的语言，点「还原原版」即可。", "info")
        messagebox.showinfo(
            APP_NAME,
            f"汉化安装完成！\n\n"
            f"写入文件：{res['files']} 个\n"
            f"覆盖文本：{res['filled']}/{res['total']} 条"
            f"（{res['ratio'] * 100:.1f}%）\n\n"
            f"原版已备份到游戏目录的「{patcher.BACKUP_DIRNAME}」里，\n"
            "想换回原来的语言，回到这个窗口点「还原原版」。\n\n"
            "现在可以启动游戏看看了。")

    def revert(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if self.game_dir is None or not self.status.get("installed"):
            messagebox.showwarning(APP_NAME, "这个游戏没有可还原的汉化记录。")
            return
        if not messagebox.askyesno(
                APP_NAME,
                f"会把游戏还原成安装汉化之前的样子：\n{self.game_dir}\n\n"
                "（备份会保留着，想再装一次也不用重新找包）\n\n继续吗？"):
            return
        self.cancel_event.clear()
        self._busy(True)
        self.progress.configure(value=0)
        self._log("═" * 46, "info")
        self.worker = threading.Thread(target=self._revert_worker, daemon=True)
        self.worker.start()

    def _revert_worker(self) -> None:
        try:
            res = patcher.revert_patch(
                self.game_dir, on_log=lambda m: self._log(m),
                on_progress=lambda c, n, d: self.root.after(
                    0, self._progress, c, n, d))
            self.root.after(0, self._revert_done, res)
        except Exception as e:  # noqa: BLE001
            self.root.after(0, self._fail, str(e))

    def _revert_done(self, res: dict) -> None:
        self._busy(False)
        self.progress.configure(value=100)
        self.status = patcher.patch_status(self.game_dir)
        self._render_state()
        self._log(f"✅ 已还原 {res['restored']} 个文件，游戏回到原版。", "ok")
        warn = ""
        if res.get("missing"):
            warn = f"\n\n有 {len(res['missing'])} 个文件没能还原（见日志）。"
        messagebox.showinfo(APP_NAME, f"已经还原成原版了。\n\n"
                                      f"还原文件：{res['restored']} 个{warn}")

    def _fail(self, err: str) -> None:
        self._busy(False)
        self._log(f"✖ 失败：{err}", "err")
        messagebox.showerror(APP_NAME, err)


# ---------------------------------------------------------------- 入口

def main(argv: list[str] | None = None) -> int:
    if not HAS_TK:
        print("当前 Python 没有 tkinter，无法启动安装器界面。")
        return 1
    install_thread_hook()
    root = tk.Tk()
    try:
        app = InstallerApp(root)          # noqa: F841
    except Exception as e:  # noqa: BLE001
        try:
            messagebox.showerror(APP_NAME, f"启动失败：{e}")
        except Exception:  # noqa: BLE001
            print(f"启动失败：{e}")
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
