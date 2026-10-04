# -*- coding: utf-8 -*-
"""端到端：便携运行时 + 完整汉化流程（模拟用户点「开始汉化」）。

流程：造 KiriKiri 游戏样本 → 启动内置引擎 → 识别 → 解包 → 提取
      → 翻译 → 回填 → 重打包 → 校验产物
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from gametl.auto import AutoConfig, AutoPipeline
from gametl.core.xp3 import XP3Archive
from gametl.runtime_manager import RuntimeManager

HOME = Path(r"D:/滚刀哥布林汉化椅")

# ---------- 1. 造模拟 KiriKiri 游戏 ----------
work = ROOT / "gametl" / "output" / "portable_e2e"
if work.exists():
    shutil.rmtree(work)
game = work / "游戏目录"
game.mkdir(parents=True)

ks = """*start
[cm]
[bg storage="town.jpg" time=500]
小さな村の朝。鳥のさえずりが聞こえる。[r]

[name="エレン"]
おはよう、[pcname]さん。よく眠れた？[r]

[name="ミア"]
お姉ちゃん、早くして！市場が始まっちゃうよ。[r]

[link target=*market]市場へ行く[endlink][r]
[link target=*stay]家に残る[endlink][r]

*market
[cm]
[name="ミア"]
あっ、あそこに武器屋さんがあるよ！[r]
二人は市場の人混みの中へ消えていった。[r]
[return]

*stay
[cm]
[name="エレン"]
今日は家でゆっくり休もう。[r]
[return]
"""

XP3Archive.pack(
    [("script.ks", ks.encode("utf-16-le")),
     ("readme.txt", "这是游戏说明文件。".encode("utf-16-le"))],
    game / "data.xp3")
(game / "game.eXe").write_bytes(b"MZ fake exe")
(game / "config.cf").write_text("dummy", encoding="utf-8")

print("=" * 64)
print("样本游戏已生成")
print("=" * 64)
print(f"  {game}")
print(f"  data.xp3 = {(game / 'data.xp3').stat().st_size} 字节")
print()

# ---------- 2. 准备内置引擎 ----------
print("=" * 64)
print("准备便携翻译引擎")
print("=" * 64)
rm = RuntimeManager(HOME, on_log=lambda m: print(f"  [引擎] {m}", flush=True))
ok, host, model, msg = rm.prepare()
print(f"  -> {'成功' if ok else '失败'}  host={host}  model={model}")
if not ok:
    print(msg)
    sys.exit(1)
print()

# ---------- 3. 跑一键流程 ----------
print("=" * 64)
print("开始一键汉化（模拟用户点「开始汉化」）")
print("=" * 64)

cfg = AutoConfig(
    game_dir=game,
    out_dir=game / "_汉化输出",
    model=model,
    host=host,
    glossary_path=ROOT / "gametl" / "glossary.example.json",
)

pipe = AutoPipeline(
    cfg,
    on_progress=lambda st, c, t, d: print(f"  [{st}] {c}/{t} {d[:60]}"),
    on_log=lambda m: print(f"  {m}"),
)
result = pipe.run()

print()
print("=" * 64)
print("结果")
print("=" * 64)
print(f"  成功:      {result.success}")
print(f"  引擎:      {result.engine}")
print(f"  到达阶段:  {result.stage_reached}")
print(f"  文本:      {result.total_units} 条，已译 {result.translated_units} 条")
print(f"  输出:      {result.output_dir}")
print(f"  重打包:    {result.pack_files}")
if result.error:
    print(f"  错误:      {result.error}")
print()

# ---------- 4. 校验产物 ----------
print("=" * 64)
print("校验汉化产物")
print("=" * 64)

patch_dir = Path(result.output_dir)
xp3s = list(patch_dir.glob("*.xp3")) if patch_dir.exists() else []

if xp3s:
    out_xp3 = xp3s[0]
    print(f"  重打包封包: {out_xp3.name}（{out_xp3.stat().st_size} 字节）")
    # 解回去看内容
    tmp = work / "_verify"
    arc = XP3Archive(out_xp3)
    entries = arc.read_index()
    print(f"  封包内条目: {[e.name for e in entries]}")
    arc.extract_all(tmp)
    ks_out = None
    for cand in tmp.rglob("*.ks"):
        ks_out = cand
        break
    if ks_out:
        text = ks_out.read_bytes().decode("utf-16-le", errors="replace")
        print()
        print("  ── 汉化后剧本（前 20 行）──")
        for line in text.splitlines()[:20]:
            print(f"    {line}")

        # 关键检查
        print()
        print("  ── 关键检查 ──")
        checks = [
            ("含中文译文", any("\u4e00" <= ch <= "\u9fff" for ch in text)),
            ("KAG 标签保留", "[bg storage=" in text and "[cm]" in text),
            ("endlink 保留", "[endlink]" in text),
            ("link 标签保留", "[link target=" in text),
            ("行内变量保留", "[pcname]" in text),
            ("占位符无残留", "【0】" not in text and "【1】" not in text),
            ("无日文假名残留", not any("\u3040" <= ch <= "\u309f"
                                        for ch in text.replace("[pcname]", ""))),
        ]
        for name, good in checks:
            print(f"    {'✓' if good else '✗'} {name}")
else:
    print("  未找到重打包产物，检查明文输出…")
    for p in patch_dir.rglob("*") if patch_dir.exists() else []:
        print(f"    {p.relative_to(patch_dir)}")

print()
rm.shutdown()
print("测试结束，内置服务已关闭")
