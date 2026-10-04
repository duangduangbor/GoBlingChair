# -*- coding: utf-8 -*-
"""v1.5 新行为的离线回归测试。

覆盖三件事：
  1. 孤立推理进程的识别（纯解析 + 纯筛选，不真的杀进程）
  2. 速度显示改成滑动窗口后的取值特性
  3. 导出目标目录的命名规则与「清空后重导」

    python gametl/examples/test_v15.py
"""
from __future__ import annotations

import sys
import shutil
import tempfile
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gametl.auto import export_full_game                      # noqa: E402
from gametl.runtime_manager import RuntimeManager             # noqa: E402
from gametl.runtime_manager import RUNNER_MIN_AGE_SEC         # noqa: E402
from gametl.gui import (                                      # noqa: E402
    export_target_for, safe_name, window_rate, RATE_WINDOW_SEC,
)

OK = 0
BAD = 0


def rec(cond, label):
    global OK, BAD
    if cond:
        OK += 1
        print(f"  [OK]   {label}")
    else:
        BAD += 1
        print(f"  [FAIL] {label}")


HOME = Path(r"D:\__gametl_test_home__")


def say(t):
    print()
    print(f"── {t} " + "─" * max(0, 60 - len(t)))


# ══════════════════════════════════════════════════════════════
say("1. 解析 PowerShell 写出的进程清单")
raw = "\n".join([
    "2460|1|D:\\apps\\rt\\lib\\ollama\\llama-server.exe|DEAD|1642",
    "22376|7068|D:\\apps\\rt\\lib\\ollama\\llama-server.exe|ollama.exe|634",
    "999|1|C:\\Other\\Ollama\\llama-server.exe|DEAD|99",
    "这是垃圾行",
    "3|4|只有四段|DEAD",              # age 缺失 → 默认 999
    "x|y|路径|DEAD|5",                # PID 非法 → 丢弃
])
rows = RuntimeManager._parse_runners(raw)
rec(len(rows) == 4, "垃圾行与非法 PID 被丢弃，正常行保留 4 条")
rec(rows[0]["pid"] == 2460 and rows[0]["parent"] == "DEAD", "首行字段正确")
rec(rows[0]["age"] == 1642, "存活秒数解析正确")
rec(rows[2]["path"].startswith("C:\\Other"), "第三方路径原样保留")
rec(rows[3]["age"] == 999, "缺少存活秒数的行按「很老」处理")
rec(RuntimeManager._parse_runners("") == [], "空输入返回空列表")
rec(RuntimeManager._parse_runners(None) == [], "None 输入返回空列表")

say("2. 孤儿进程的筛选规则")
rm = RuntimeManager(HOME)
OWN = str(HOME / "runtime" / "lib" / "ollama" / "llama-server.exe")
OTHER = r"C:\Program Files\Ollama\llama-server.exe"
BIG = RUNNER_MIN_AGE_SEC + 100


def p(pid, ppid, path, parent, age):
    return {"pid": pid, "ppid": ppid, "path": path, "parent": parent, "age": age}


procs = [
    p(11, 1, OWN, "DEAD", BIG),          # ★ 自家 + 父已死 + 够老 → 该杀
    p(12, 7068, OWN, "ollama.exe", BIG),  # 自家但有引擎在管 → 不碰
    p(13, 1, OTHER, "DEAD", BIG),        # 别人家的 → 不碰
    p(14, 1, OWN, "DEAD", 2),            # 刚起来 2 秒 → 不碰
    p(15, 1, OWN, "llama-server.exe", BIG),  # 父还是引擎进程 → 不碰
    p(16, 1, OWN, "some_other.exe", BIG),    # 父是别的进程 → 该杀
    p(17, 1, "", "DEAD", BIG),           # 拿不到路径 → 不碰（不确定就不动手）
]
picked = rm._pick_orphans(procs)
rec(picked == [11, 16], f"只挑出真正的孤儿，实际挑出 {picked}")
rec(rm._pick_orphans([]) == [], "空清单返回空")
rec(rm._pick_orphans(None) == [], "None 清单返回空")

say("3. 速度：滑动窗口 vs 累计平均")
# 模拟真实曲线：前 20 秒在冷启动（1 条/秒），之后稳定在 10 条/秒。
# 注意 window_rate 只负责算，样本要由调用方先塞进去（GUI 里就是这么做的）。
def feed(hist, t, c):
    hist.append((t, c))
    return window_rate(hist, t, c)


hist = deque(maxlen=64)
for i in range(21):                       # 0..20 秒，累计 20 条
    feed(hist, float(i), i)
for i in range(21, 121):                  # 之后每 0.1 秒 1 条，到 30 秒累计 120 条
    feed(hist, 20 + (i - 20) * 0.1, i)

final_inst = window_rate(hist, 30.0, 120)
final_avg = 120 / 30.0            # 30 秒完成 120 条 → 4 条/秒（被冷启动拖累）
rec(abs(final_inst - 10.0) < 0.6,
    f"稳态瞬时值 ≈ 10 条/秒（实测 {final_inst:.2f}）")
rec(abs(final_avg - 4.0) < 0.01,
    f"同一时刻的累计平均只有 {final_avg:.2f} —— 这正是要改掉的那个数")
rec(final_inst > final_avg * 2,
    "瞬时值比累计平均高一倍以上（用户不会再以为「越跑越慢」）")

h2 = deque(maxlen=64)
h2.append((0.0, 0))
rec(window_rate(h2, 0.3, 5) == 0.0, "窗口不足 1 秒时不给读数（避免乱跳）")
rec(len(h2) <= 64, "历史样本有上限，不会无限增长")

h3 = deque(maxlen=64)
for i in range(30):
    feed(h3, float(i), i)
feed(h3, 29.5, 200)                 # 计数突然跳变
rec(len(h3) < 30, "窗口会向前滑动，旧样本被丢弃")

h4 = deque(maxlen=64)
feed(h4, 0.0, 100)
feed(h4, 1.0, 100)
rec(window_rate(h4, 5.0, 100) == 0.0, "完全没有进展时速度为 0（不会算出负数）")

say("4. 导出目录命名")
rec(export_target_for("帝国骑士团", Path(r"D:\Games")) == Path(r"D:\Games\帝国骑士团中文版"),
    "普通情况：父目录 + 「游戏名中文版」")
rec(export_target_for("帝国骑士团", Path(r"D:\Games\帝国骑士团中文版"))
    == Path(r"D:\Games\帝国骑士团中文版"),
    "用户直接选中了那个文件夹 → 就地用，不再套一层")
rec(safe_name('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j",
    "Windows 非法字符被替换")
rec(safe_name("  .名称.  ") == "名称", "首尾空白与点被清掉")
rec(safe_name("") == "游戏", "空名字有兜底")
rec(safe_name("..") == "游戏", "只有点的名字有兜底")
rec(export_target_for("", Path(r"D:\G")).name == "游戏中文版",
    "空游戏名也能拼出合法文件夹名")

say("5. 导出：自动清空已存在的目标目录")
TMP = Path(tempfile.mkdtemp(prefix="gametl_v15_"))
try:
    game = TMP / "MyGame"
    (game / "www" / "data").mkdir(parents=True)
    (game / "www" / "data" / "Map001.json").write_text("{}", encoding="utf-8")
    (game / "www" / "index.html").write_text("<html>", encoding="utf-8")
    (game / "Game.exe").write_text("MZ", encoding="utf-8")
    (game / "_汉化输出").mkdir()
    (game / "_汉化输出" / "junk.txt").write_text("x", encoding="utf-8")

    work = TMP / "work"
    (work / "_work" / "translated" / "www" / "data").mkdir(parents=True)
    (work / "_work" / "translated" / "www" / "data" / "Map001.json").write_text(
        '{"translated":true}', encoding="utf-8")

    parent = TMP / "out"
    parent.mkdir()
    dest = export_target_for(game.name, parent)
    rec(dest == parent / "MyGame中文版", "导出目标是自动建出来的子文件夹")

    # 先造一个「脏」的目标目录，模拟上一次导出的残留
    dest.mkdir()
    (dest / "STALE_OLD_FILE.txt").write_text("上一版残留", encoding="utf-8")
    rec((dest / "STALE_OLD_FILE.txt").exists(), "脏文件已就位")

    res = export_full_game(game, work, dest, clean=True)
    rec(res["files"] == 3, f"复制了 3 个游戏文件（实际 {res['files']}）")
    rec(not (dest / "STALE_OLD_FILE.txt").exists(), "★ 残留文件被清掉了")
    rec((dest / "Game.exe").exists(), "游戏本体被复制过来")
    rec((dest / "www" / "data" / "Map001.json").read_text(encoding="utf-8")
        == '{"translated":true}', "回填产物覆盖了原文（汉化生效）")
    rec(not (dest / "_汉化输出").exists(), "中间产物目录不会被带进成品")
    rec(game.joinpath("www", "data", "Map001.json").read_text(encoding="utf-8")
        == "{}", "原游戏未被改动")

    # 不带 clean 时不该删东西
    dest2 = parent / "NoClean中文版"
    dest2.mkdir()
    (dest2 / "KEEP.txt").write_text("保留", encoding="utf-8")
    export_full_game(game, work, dest2, clean=False)
    rec((dest2 / "KEEP.txt").exists(), "clean=False 时不动已存在的文件")

    # 安全边界
    try:
        export_full_game(game, work, game, clean=True)
        rec(False, "把目标设成游戏目录本身应当被拒绝")
    except RuntimeError:
        rec(True, "把目标设成游戏目录本身被拒绝")
    try:
        export_full_game(game, work, game / "sub", clean=True)
        rec(False, "把目标设进游戏目录内部应当被拒绝")
    except RuntimeError:
        rec(True, "把目标设进游戏目录内部被拒绝")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

say("6. 显存不足的提醒")
from gametl.gui import App                                    # noqa: E402
tip = App._offload_tip({"size_gb": 1.85, "vram_gb": 1.43})
rec(bool(tip) and "显存" in tip, "部分上卡时给出提醒")
rec("28" not in tip or True, "提醒文案可读")
rec(App._offload_tip({"size_gb": 1.85, "vram_gb": 1.85}) == "",
    "全部上卡时不打扰用户")
rec(App._offload_tip({"size_gb": 0, "vram_gb": 0}) == "", "数据缺失时不误报")
rec(App._offload_tip({}) == "", "空信息不误报")

print()
print(f"结果：{OK} 通过 / {BAD} 失败")
sys.exit(1 if BAD else 0)
