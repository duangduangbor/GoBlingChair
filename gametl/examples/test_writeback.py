"""回填测试：给项目文件填入模拟译文，验证回填后文件格式与结构正确。

不依赖模型，用于在翻译前验证整条回填链路。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gametl.core.models import Project, TextKind
from gametl.core.protect import restore
from gametl.pipeline import get_extractor, run_writeback

# 简单的中日对照词典（模拟翻译结果，仅为验证回填）
FAKE = {
    "アレン": "艾伦", "ミア": "米娅", "ゴブリン": "哥布林",
    "エレン": "艾莲",
    "剣士の青年。正義感が強い。": "剑士青年。正义感很强。",
    "回復魔法を操る少女。": "操纵回复魔法的少女。",
    "森に棲む下級の魔物。": "栖息在森林里的低级魔物。",
    "ポーション": "药水", "マナポーション": "魔力药水", "エリクサー": "万能药",
    "HPを50回復する。": "回复 50 点 HP。",
    "MPを30回復する。": "回复 30 点 MP。",
    "HPとMPを全回復する伝説の薬。": "能完全恢复 HP 与 MP 的传说之药。",
    "小さな村の朝。鳥のさえずりが聞こえる。": "小村庄的清晨。能听见鸟儿的鸣叫。",
    "お姉ちゃん、早くして！市場が始まっちゃうよ。": "姐姐，快点啦！集市要开始了哦。",
    "おはよう、[pcname]さん。よく眠れた？": "早上好，[pcname]。睡得好吗？",
    "よし、それじゃあ市場に行こうか。何かいいものが見つかるといいね。":
        "好，那我们去集市吧。希望能找到点好东西呢。",
    "市場へ行く": "去集市", "家に残る": "留在家中",
    "あっ、あそこに武器屋さんがあるよ！": "啊，那边有家武器店哦！",
    "二人は市場の人混みの中へ消えていった。": "两人消失在集市的人潮之中。",
    "今日は家でゆっくり休もう。": "今天就待在家里好好休息吧。",
    "えー、つまらないの！": "诶——好无聊哦！",
}


def fake_translate(project_path: Path) -> None:
    p = Project.load(project_path)
    n = 0
    for u in p.units:
        if u.original in FAKE:
            u.translated = FAKE[u.original]
            n += 1
        elif "—" not in u.original and len(u.original) < 30:
            u.translated = "[未译]" + u.original  # 占位
    p.save(project_path)
    print(f"  {project_path.name}: 填入 {n} 条真实模拟译文，其余占位")


if __name__ == "__main__":
    print("=== 填入模拟译文 ===")
    for name in ("rpg", "renpy", "kirikiri"):
        fake_translate(ROOT / "gametl" / "output" / f"{name}.json")

    print("\n=== 执行回填 ===")
    for name, subdir in (("rpg", "mock_rpgmaker_example"),
                         ("renpy", "mock_renpy_example"),
                         ("kirikiri", "mock_kirikiri_example")):
        proj = ROOT / "gametl" / "output" / f"{name}.json"
        out = ROOT / "gametl" / "output" / f"translated_{name}"
        stats = run_writeback(proj, out)
        print(f"  [{name}] {stats}")
