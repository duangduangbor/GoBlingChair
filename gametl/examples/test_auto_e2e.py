"""端到端自动化闭环测试：造一个 KiriKiri 游戏样本，跑完整自动汉化流程。

验证：选文件夹 → 自动识别 → 自动解包 → 提取 → 翻译 → 回填 → 重打包
"""
import sys
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gametl.core.xp3 import XP3Archive
from gametl.auto import AutoConfig, AutoPipeline

# ---------- 1. 造一个模拟 KiriKiri 游戏 ----------
game = ROOT / "gametl" / "output" / "auto_test_game"
if game.exists():
    shutil.rmtree(game)
game.mkdir(parents=True)

# 构造 .ks 脚本（UTF-16LE）
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

files = [
    ("script.ks", ks.encode("utf-16-le")),
    ("readme.txt", "这是游戏说明文件。".encode("utf-16-le")),
]
XP3Archive.pack(files, game / "data.xp3")
# 模拟启动器与配置
(game / "game.eXe").write_bytes(b"MZ fake exe")
(game / "config.cf").write_text("dummy", encoding="utf-8")

print(f"[素材] 游戏目录: {game}")
print(f"       data.xp3: {(game / 'data.xp3').stat().st_size} 字节")
print()

# ---------- 2. 跑一键流程 ----------
out = ROOT / "gametl" / "output" / "auto_test_out"
if out.exists():
    shutil.rmtree(out)

print("=" * 56)
print("开始一键汉化流程")
print("=" * 56)


def on_progress(stage, cur, total, desc):
    print(f"  [{stage}] {cur}/{total} {desc}")


def on_log(msg):
    print(f"  {msg}")


cfg = AutoConfig(
    game_dir=game,
    out_dir=out,
    glossary_path=ROOT / "gametl" / "glossary.example.json",
)
pipe = AutoPipeline(cfg, on_progress=on_progress, on_log=on_log)
result = pipe.run()

print()
print("=" * 56)
print("结果")
print("=" * 56)
print(f"成功: {result.success}")
print(f"引擎: {result.engine}")
print(f"阶段: {result.stage_reached}")
print(f"文本: {result.total_units} 条，已译 {result.translated_units} 条")
print(f"输出: {result.output_dir}")
print(f"打包: {result.pack_files}")
if result.error:
    print(f"错误: {result.error}")
