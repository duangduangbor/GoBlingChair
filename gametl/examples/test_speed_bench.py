# -*- coding: utf-8 -*-
"""性能实测 —— 拆解「精简提示词 / 并发 / 模型大小」各自的贡献。

分两个阶段：

  阶段一  通用 7B 模型（Qwen2.5-7B）
          A 基线      并发1 · 单条 · 完整提示词   ← 等价于改版前的行为
          B 精简提示  并发1 · 单条 · 精简提示词
          C 并发      并发4 · 单条 · 精简提示词

  阶段二  翻译专用 1.8B 模型（混元 Hy-MT2）
          D/E/F      并发 1 / 4 / 6

用法::

    env -u PYTHONPATH PYTHONIOENCODING=utf-8 <py> gametl/examples/test_speed_bench.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gametl.core.models import TextKind, TextUnit  # noqa: E402
from gametl.profiles import Profile  # noqa: E402
from gametl.runtime_manager import RuntimeManager  # noqa: E402
from gametl.translators.glossary import Glossary  # noqa: E402
from gametl.translators.ollama_backend import (  # noqa: E402
    OllamaTranslator, TranslateOptions,
)

APP_HOME = Path(os.environ.get("GAMETL_HOME", r"D:/滚刀哥布林汉化椅"))

# 按 7.7 万条折算用
FULL_SCALE = 77401

NOUNS = [
    "ポーション", "エーテル", "やくそう", "てつのつるぎ", "はがねのよろい",
    "ほのおのたま", "かみなりのやり", "こおりのつえ", "せいなるしるし",
    "まほうのゆびわ", "りゅうのきば", "ぎんのゆびわ", "たいようのけん",
    "つきのかがみ", "いのちのみず", "ちからのたね", "まもりのふだ",
    "しあわせのすず", "ギルド", "ぼうけんしゃ", "まじゅつし", "せんし",
    "そうりょ", "とうぞく", "ゆうしゃ", "まおう", "りゅうおう",
]

PATTERNS = [
    "{}を ひとつ ください。",
    "この {} は とても やくに たつ。",
    "{} を つかって みよう。",
    "{} の こうかが きれて しまった。",
    "{} を てに いれた！",
    "{} は もう もって いる。",
    "{} を そうび した。",
    "{} の のこりが すくない。",
    "{} は どこで てに いれられますか。",
    "{} について おしえて ください。",
]

LONG = [
    "むらの ちかくに ある もりには、むかしから りゅうが すんで いるという "
    "いいつたえが あります。",
    "あなたが たすけて くれた おかげで、わたしは ぶじに いえへ かえれました。",
    "この けんを もっている かぎり、どんな てきにも まける ことは ありません。",
    "まちの きたがわに ある しんでんでは、ちいさな こどもたちが "
    "まいにち いのりを ささげて います。",
    "そらの いろが あかく そまって きたら、もうすぐ あらしが くる しょうこです。",
    "わたしたちの ぎるどでは、あたらしい ぼうけんしゃを "
    "いつでも かんげい して いますよ。",
    "ゆうしゃさま、こんやは ゆっくり おやすみ ください。"
    "あしたも たいへんな たたかいが まって いますから。",
    "その つるぎは はるか むかしに つくられた もので、"
    "いまでは もう つくる ことが できないと いわれて います。",
]


def make_texts(n: int = 120) -> list:
    texts = list(NOUNS)
    i = 0
    while len(texts) < n - len(LONG):
        texts.append(PATTERNS[i % len(PATTERNS)].format(
            NOUNS[(i * 7) % len(NOUNS)]))
        i += 1
    texts.extend(LONG)
    return texts[:n]


def run_case(host: str, model: str, label: str, opt: TranslateOptions,
             texts: list, glossary: Glossary) -> tuple:
    tr = OllamaTranslator(model=model, host=host, options=opt)

    # 预热：把模型加载时间排除在计时之外
    tr.translate_batch(
        [TextUnit(uid="warmup", source_file="w.ks", location={},
                  original="こんにちは")],
        glossary=glossary, skip_translated=False)

    units = [
        TextUnit(uid=f"b{i:04d}", source_file="b.ks", location={"i": i},
                 original=t, kind=TextKind.DIALOGUE)
        for i, t in enumerate(texts)
    ]
    tr.stats = {k: 0 for k in tr.stats}

    t0 = time.time()
    tr.translate_batch(units, glossary=glossary, skip_translated=False)
    elapsed = time.time() - t0

    filled = sum(1 for u in units if u.translated)
    sample = next((u.translated for u in units if u.translated), "")
    print(f"  {label}")
    print(f"      {filled}/{len(texts)} 条 · {elapsed:.1f}s"
          f" → {filled/elapsed:.2f} 条/秒")
    print(f"      明细: {tr.stats_line()}")
    print(f"      样例: {sample[:44]}")
    return filled / max(elapsed, 0.001)


def main() -> int:
    print("=" * 72)
    print("滚刀哥布林汉化椅 · 性能实测（拆解各因素贡献）")
    print("=" * 72)
    if not APP_HOME.is_dir():
        print(f"[错误] 找不到软件目录：{APP_HOME}")
        return 1

    rm = RuntimeManager(APP_HOME, on_log=lambda m: print("      " + m))
    ggufs = rm.list_gguf()
    print(f"models\\ 里的模型：")
    for g in ggufs:
        print(f"    {g.name}  ({g.stat().st_size/1024/1024/1024:.2f} GB)")
    if not ggufs:
        print("[错误] models\\ 里没有 .gguf")
        return 1

    glossary = Glossary({"ギルド": "公会", "ポーション": "药水",
                         "エーテル": "以太", "ゆうしゃ": "勇者"})
    texts = make_texts(120)
    avg = sum(len(t) for t in texts) / len(texts)
    print(f"测试语料：{len(texts)} 条，平均 {avg:.1f} 字/条")

    small = [g for g in ggufs if any(h in g.name.lower() for h in
                                     ("1.8b", "1.5b", "2b", "3b"))]
    big = [g for g in ggufs if g not in small]

    results = []

    # ---------------- 阶段一：通用 7B ----------------
    if big:
        print()
        print("─" * 72)
        print("阶段一 · 通用 7B 模型")
        print("─" * 72)
        ok, host, model, msg = rm.prepare(profile=Profile.TURBO)
        if not ok:
            print(f"[错误] 引擎启动失败：{msg}")
            return 1
        print(f"  引擎就绪 {host} · 档位 强效（并发 4 槽 / ctx 4096 / KV q8_0）")
        print(f"  实际载入模型：{rm.model_file}")

        cases = [
            ("A 基线（并发1 · 单条 · 完整提示词）",
             TranslateOptions(concurrency=1, batch_size=1, use_batch=False,
                              slim_prompt=False)),
            ("B 精简提示词（并发1 · 单条）",
             TranslateOptions(concurrency=1, batch_size=1, use_batch=False,
                              slim_prompt=True)),
            ("C 并发 4（单条 · 精简提示词）",
             TranslateOptions(concurrency=4, batch_size=1, use_batch=False,
                              slim_prompt=True)),
        ]
        for label, opt in cases:
            r = run_case(host, model, label, opt, texts, glossary)
            results.append((label, r))

    # ---------------- 阶段二：翻译专用 1.8B ----------------
    if small:
        print()
        print("─" * 72)
        print("阶段二 · 翻译专用 1.8B 模型（混元 Hy-MT2）")
        print("─" * 72)
        # 狂暴档 prefer_small_model=True，会挑中小模型
        ok, host, model, msg = rm.prepare(profile=Profile.INSANE)
        if not ok:
            print(f"[错误] 引擎启动失败：{msg}")
            return 1
        print(f"  引擎就绪 {host} · 档位 狂暴（并发 6 槽 / ctx 2048 / KV q8_0）")
        print(f"  实际载入模型：{rm.model_file}")

        for conc in (1, 4, 6):
            label = f"D{conc} 并发 {conc}（单条）" if conc > 1 else "D1 并发 1（单条）"
            opt = TranslateOptions(concurrency=conc, batch_size=1,
                                   use_batch=False, slim_prompt=True,
                                   model_file=rm.model_file)
            r = run_case(host, model, label, opt, texts, glossary)
            results.append((label, r))
    else:
        print()
        print("[提示] models\\ 里没有小模型，跳过阶段二。")

    # ---------------- 汇总 ----------------
    print()
    print("=" * 72)
    print("汇总")
    print("=" * 72)
    base = results[0][1] if results else 1
    print(f"{'配置':<36}{'条/秒':>10}{'提速':>9}{'7.7万条':>12}")
    print("-" * 72)
    for label, rate in results:
        minutes = FULL_SCALE / (rate * 60)
        dur = f"{minutes:.0f} 分钟" if minutes < 90 else f"{minutes/60:.1f} 小时"
        print(f"{label:<36}{rate:>10.2f}{rate/base:>8.1f}x{dur:>12}")
    print("-" * 72)
    print("说明：语料以短文本为主（模拟 RPG Maker 的数据库条目）。")
    print("      批量翻译已在代码里自动关闭 —— 实测短文本用批量反而更慢。")

    try:
        rm.shutdown()
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
