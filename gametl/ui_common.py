# -*- coding: utf-8 -*-
"""两个界面都用到的小零件：自适应窗口、可滚动容器。

单独成文件是为了让「汉化安装器」不必把整个主界面模块拉进来 ——
安装器要能独立打包成一个很小的 exe。

tkinter 是**可选**依赖：``fit_size`` 是纯计算，没有 GUI 的环境（比如
只跑文本测试的精简 Python）也能拿它来验证布局算术，所以这里不能让它
因为 ``import tkinter`` 失败而整个模块炸掉。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk
    HAS_TK = True
except ImportError:                      # pragma: no cover - 无 GUI 环境
    tk = None                            # type: ignore[assignment]
    ttk = None                           # type: ignore[assignment]
    HAS_TK = False

#: 屏幕边缘最多留这么多像素给任务栏 / 标题栏 / 视觉呼吸
SCREEN_MARGIN_X = 80
SCREEN_MARGIN_Y = 96

def install_thread_hook() -> None:
    """让「窗口已经关了、工作线程才收尾」的那种回调安静地结束。

    工作线程回主线程靠 ``root.after()``；窗口一关，它会抛
    ``RuntimeError: main thread is not in main loop``。窗口都没了，这次回调
    本来也不该再跑，但默认的线程异常钩子会把它当「工作线程崩了」记一笔堆栈。

    只吞掉**这一种**，其余异常照旧交给默认钩子 —— 不能顺手把真 bug 也捂掉。
    """
    default = getattr(threading, "excepthook", None)
    if default is None:                       # Python 3.7 及更早：没有这个钩子
        return

    def _hook(args) -> None:
        exc = getattr(args, "exc_value", None)
        if isinstance(exc, RuntimeError) and \
                "main thread is not in main loop" in str(exc):
            return
        default(args)

    threading.excepthook = _hook              # type: ignore[assignment]


# ---------------------------------------------------------------- 分栏宽度
#: 左栏（设置区）与右栏（日志）的像素下限。
#: 为什么要有下限：ttk 的分栏**只认 weight、不认 minsize**（Tk 8.6 的
#: ttk::panedwindow 没有 -minsize 这个 pane 选项），用户可以把某一栏拖成
#: 1px 宽 —— 拖没了以后没有任何办法用鼠标拖回来，只能重启。所以在松手时
#: 夹一下。
SASH_MIN_LEFT = 340
SASH_MIN_RIGHT = 320
#: 首次打开时左栏占分栏宽度的比例。
#: 0.44 是量出来的：默认窗口 1180 宽 → 分栏 1144 → 左栏 503，
#: 正好装得下卡片（自然宽度 460）且留出余量；日志还剩 641，够看。
SASH_RATIO = 0.44
#: 大屏上左栏也不该无限变宽（卡片会被拉得很空）
SASH_MAX_LEFT = 580


def sash_target(span: int, saved=None, *,
                min_first: int = SASH_MIN_LEFT,
                min_second: int = SASH_MIN_RIGHT,
                ratio: float = SASH_RATIO,
                max_first: int = SASH_MAX_LEFT) -> int:
    """算出分隔条该落在哪个坐标（纯计算，无 GUI 依赖，好测）。

    横向分栏传「宽度」，纵向分栏传「高度」，同一套算法通用。

    Args:
        span: 分栏容器当前的宽度（横向）或高度（纵向）
        saved: 用户上次拖动后的位置；None = 从没拖过（用默认比例）
        min_first / min_second: 第一栏、第二栏的像素下限
        ratio: 首次打开时第一栏占的比例
        max_first: 第一栏的像素上限（大屏上别拉得太空）

    Returns:
        分隔条的目标坐标。容器还没布局出来（span <= 1）时返回 0。
    """
    try:
        span = int(span or 0)
    except (TypeError, ValueError):
        return 0
    if span <= 1:
        return 0
    # 第二栏必须先留够，剩下的才是第一栏能占的上限
    avail = max(0, span - min_second)
    lo = min(min_first, avail)              # 容器极窄时下限也要退让
    if saved is not None:
        try:
            v = int(saved)
        except (TypeError, ValueError):
            v = 0
        if v > 0:
            return max(lo, min(v, avail))
    want = int(span * ratio)
    return max(lo, min(want, min(max_first, avail)))


def clamp_sash(span: int, pos, *,
               min_first: int = SASH_MIN_LEFT,
               min_second: int = SASH_MIN_RIGHT) -> int:
    """把用户拖到的位置夹进合法区间（不改变「谁宽谁窄」的意图）。"""
    try:
        span = int(span or 0)
        pos = int(pos)
    except (TypeError, ValueError):
        return 0
    if span <= 1:
        return 0
    avail = max(0, span - min_second)
    return max(min(min_first, avail), min(pos, avail))


# ---------------------------------------------------------------- 界面偏好
def read_prefs(path) -> dict:
    """读界面偏好。文件不在、坏了、被占用了 —— 一律当作「没有偏好」。

    界面偏好丢了顶多回到默认布局，绝不能因此让软件起不来。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.loads(f.read())
        return data if isinstance(data, dict) else {}
    except Exception:                    # noqa: BLE001
        return {}


def write_prefs(path, data: dict) -> bool:
    """原子写界面偏好（先写 .tmp 再替换，避免断电留下半个文件）。"""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(str(tmp), str(p))
        return True
    except Exception:                    # noqa: BLE001
        return False


def fit_size(sw: int, sh: int, want_w: int, want_h: int, *,
             min_w: int = 720, min_h: int = 540,
             margin_x: int = SCREEN_MARGIN_X,
             margin_y: int = SCREEN_MARGIN_Y) -> tuple[int, int, int, int]:
    """算出「装得下、又不至于太小」的窗口尺寸与位置（纯计算，好测）。

    Returns:
        (宽, 高, x, y)
    """
    if sw <= 1 or sh <= 1:
        sw, sh = 1920, 1080
    w = max(min_w, min(want_w, sw - margin_x))
    h = max(min_h, min(want_h, sh - margin_y))
    w = min(w, sw)                 # 屏幕比 min 还小时，至少别超出屏幕
    h = min(h, sh)
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 3)      # 略偏上，任务栏不会压住底部按钮
    return w, h, x, y


def fit_window(root, want_w: int, want_h: int, *,
               min_w: int = 720, min_h: int = 540,
               margin_x: int = SCREEN_MARGIN_X,
               margin_y: int = SCREEN_MARGIN_Y) -> tuple[int, int]:
    """把窗口调到「装得下、又不至于太小」的尺寸，然后居中偏上。

    为什么不能写死尺寸：14 寸笔记本在 150% 缩放下逻辑分辨率只有
    1280×720，一个 880 高的窗口会被任务栏和标题栏切掉一大截，底部
    的日志区首当其冲。这里按**逻辑屏幕尺寸**取 min，小屏自动缩，
    大屏也不会撑成一片空白。
    """
    try:
        sw = int(root.winfo_screenwidth())
        sh = int(root.winfo_screenheight())
    except Exception:                     # noqa: BLE001
        sw, sh = 1920, 1080
    w, h, x, y = fit_size(sw, sh, want_w, want_h, min_w=min_w, min_h=min_h,
                          margin_x=margin_x, margin_y=margin_y)
    try:
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.minsize(min(min_w, sw), min(min_h, sh))
    except Exception:                     # noqa: BLE001
        pass
    return w, h


def button_cols(widths, avail: int, *, gap: int = 6) -> int:
    """一横排按钮该排几列：排得下就一行，排不下就折行（纯计算，好测）。

    为什么不直接用 pack：**Tk 的 pack 永远不会折行**，容器一窄它就把最后
    一颗按钮切掉一半。而这个界面的左栏宽度是可变的（还能被拖），按钮必须
    会折。

    选择顺序刻意是「先整除、再按列数从多到少」——4 颗按钮在 3 列下会变成
    3+1（最后一行孤零零一颗），不如 2+2 整齐。

    注意判据里必须排除 ``c == 1``：**1 能整除任何数**，不排除的话「排成一列」
    会被当成整齐排法而优先于两列，三四颗按钮一窄就直接变竖排。

    Args:
        widths: 每颗按钮的自然宽度
        avail: 容器可用宽度
        gap: 按钮之间的水平间距

    Returns:
        列数（1..len(widths)）
    """
    n = len(widths)
    if n <= 1:
        return max(1, n)
    try:
        avail = int(avail or 0)
    except (TypeError, ValueError):
        avail = 0
    if avail <= 1:
        return n                      # 容器还没布局：先按一行摆，等 Configure

    def _pref(k: int) -> int:
        return 0 if (k > 1 and n % k == 0) else 1

    for c in sorted(range(1, n + 1), key=lambda k: (_pref(k), -k)):
        widest = 0
        for r in range(0, n, c):
            row = widths[r:r + c]
            widest = max(widest, sum(row) + gap * (len(row) - 1))
        if widest <= avail:
            return c
    return 1


if HAS_TK:

    class ButtonRow(tk.Frame):
        """一排按钮，容器变窄时自动折行。

        用法：先建 ``ButtonRow``，再用它当父控件建按钮、``add()`` 进去。
        """

        def __init__(self, master, *, gap: int = 6, bg: str = "#ffffff"):
            super().__init__(master, bg=bg)
            self._btns: list = []
            self._gap = int(gap)
            self._cols = 0
            self.bind("<Configure>", self._on_conf, add="+")

        def add(self, btn) -> None:
            self._btns.append(btn)
            self._cols = 0                            # 强制重排
            try:
                self._relayout(self.winfo_width())
            except tk.TclError:                       # pragma: no cover
                pass

        def clear(self) -> None:
            """清空按钮（模型列表会随 models\\ 里的文件变化重建）。"""
            for b in self._btns:
                try:
                    b.destroy()
                except tk.TclError:                   # pragma: no cover
                    pass
            self._btns = []
            self._cols = 0

        def _on_conf(self, e) -> None:
            self._relayout(e.width)

        def _relayout(self, width: int) -> None:
            if not self._btns:
                return
            widths = [b.winfo_reqwidth() for b in self._btns]
            cols = button_cols(widths, width, gap=self._gap)
            if cols == self._cols:
                return                                # 没变就别折腾布局
            self._cols = cols
            n = len(self._btns)
            last_row = (n - 1) // cols
            for i, b in enumerate(self._btns):
                b.grid_forget()
            for i, b in enumerate(self._btns):
                r, c = divmod(i, cols)
                b.grid(row=r, column=c, sticky="w",
                       padx=(0, self._gap),
                       pady=(0, 0 if r == last_row else self._gap))

    def wrap_to_parent(label, parent=None, *, pad: int = 6,
                       minimum: int = 140) -> None:
        """让 Label 的文字**跟着容器宽度换行**，而不是被切掉。

        Tk 的 Label 默认不换行：文字比容器宽就直接截断，用户永远看不到
        后半句。而分栏宽度是可变的（还能拖），所以不能写死 ``wraplength``。

        只在宽度真的变了时才 configure —— 换行会改变控件高度、进而触发
        容器的 ``<Configure>``，不做这个判断就会变成自激循环。
        """
        holder = parent if parent is not None else label.master
        box = {"last": -1}

        def _apply(e=None):
            try:
                width = int(e.width) if e is not None else int(holder.winfo_width())
            except (AttributeError, tk.TclError):
                return
            if width <= 1:
                return
            want = max(minimum, width - pad)
            if want == box["last"]:
                return
            box["last"] = want
            try:
                label.configure(wraplength=want)
            except tk.TclError:
                pass

        try:
            holder.bind("<Configure>", _apply, add="+")
            _apply()
        except tk.TclError:               # pragma: no cover - 控件已销毁
            pass

    class VScroll(tk.Frame):
        """纵向滚动容器。把内容塞进 ``.body`` 就行。

        内容装得下时滚动条自动隐藏 —— 大屏上不该因为「可能有滚动条」
        而白占一条竖线。
        """

        def __init__(self, master, *, bg: str = "#ffffff", **kw):
            super().__init__(master, bg=bg, **kw)
            self._bg = bg
            self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0,
                                    takefocus=0)
            self.vbar = ttk.Scrollbar(self, orient="vertical",
                                      command=self.canvas.yview)
            self.canvas.configure(yscrollcommand=self.vbar.set)
            self.canvas.pack(side="left", fill="both", expand=True)

            self.body = tk.Frame(self.canvas, bg=bg)
            self._win = self.canvas.create_window((0, 0), window=self.body,
                                                  anchor="nw")
            self._bar_visible = False
            self.body.bind("<Configure>", self._on_body)
            self.canvas.bind("<Configure>", self._on_canvas)
            self.canvas.bind("<Enter>", self._wheel_on)
            self.canvas.bind("<Leave>", self._wheel_off)
            self.body.bind("<Enter>", self._wheel_on)
            self.body.bind("<Leave>", self._wheel_off)

        def _wheel_on(self, _e=None) -> None:
            self.canvas.bind_all("<MouseWheel>", self._wheel)

        def _wheel_off(self, _e=None) -> None:
            self.canvas.unbind_all("<MouseWheel>")

        def _wheel(self, e) -> None:
            if not self._bar_visible:
                return
            try:
                self.canvas.yview_scroll(int(-e.delta / 120), "units")
            except tk.TclError:
                pass

        def _sync_bar(self) -> None:
            box = self.canvas.bbox("all")
            need = box is not None and box[3] > self.canvas.winfo_height()
            if need and not self._bar_visible:
                self.vbar.pack(side="right", fill="y")
                self._bar_visible = True
            elif not need and self._bar_visible:
                self.vbar.pack_forget()
                self._bar_visible = False

        def _on_body(self, _e=None) -> None:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
            self._sync_bar()

        def _on_canvas(self, e) -> None:
            self.canvas.itemconfigure(self._win, width=e.width)
            self._on_body()

else:                                     # pragma: no cover - 无 GUI 环境

    def wrap_to_parent(label, parent=None, **kw) -> None:
        """没有 tkinter 时的占位：什么都不用做。"""
        return None

    class ButtonRow:                      # type: ignore[no-redef]
        """没有 tkinter 时的占位。"""

        def __init__(self, *a, **kw):
            raise RuntimeError("当前 Python 没有 tkinter，无法使用界面组件")

    class VScroll:                        # type: ignore[no-redef]
        """没有 tkinter 时的占位：导入不报错，真要用才炸。"""

        def __init__(self, *a, **kw):
            raise RuntimeError("当前 Python 没有 tkinter，无法使用界面组件")
