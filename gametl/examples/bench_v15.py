# -*- coding: utf-8 -*-
"""端到端吞吐实测：重启引擎后，模型是否全量上卡、真实速度多少。

★ 只测**自己造的合成语料**（日语假名+汉字的组合），不读取任何游戏文本、
  不输出任何原文内容。语料保证两两不同 —— 去重分发会把「条/秒」放大，
  这一点以前踩过坑。

    python bench_v15.py [条数]
"""
from __future__ import annotations

import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gametl.auto import options_from_profile                # noqa: E402
from gametl.profiles import Profile                         # noqa: E402
from gametl.runtime_manager import RuntimeManager           # noqa: E402
from gametl.translators.ollama_backend import OllamaTranslator, should_skip  # noqa: E402

HOME = Path(r"D:\滚刀哥布林汉化椅")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 150

ADJ = ["美しい", "静かな", "古い", "小さな", "眩しい", "冷たい", "柔らかな",
       "重い", "暗い", "若い", "遠い", "甘い"]
NOUN = ["森", "城", "剣", "魔法", "少女", "騎士", "塔", "泉", "書物", "扉",
        "灯り", "旗"]
PART = ["を", "が", "に", "は", "も", "と"]
VERB = ["見つけた", "守っている", "忘れてしまった", "探し続けた",
        "静かに抱えた", "そっと置いた", "呼び戻した", "封じた",
        "確かめていた", "取り替えた", "数えていた", "隠していた"]


def make_texts(n: int, long: bool = False) -> list[str]:
    """造 n 条**互不相同**的日语短句。

    long=False → 单句，均长约 10 字；
    long=True  → 两个分句，均长约 20~22 字（贴近 RPG Maker 里的对话/描述）。
    """
    out, seen = [], set()
    i = 0
    while len(out) < n:
        a = ADJ[i % len(ADJ)]
        b = NOUN[(i // len(ADJ)) % len(NOUN)]
        c = PART[(i // 7) % len(PART)]
        d = VERB[(i // 13) % len(VERB)]
        s = f"{a}{b}{c}{d}"
        if long:
            a2 = ADJ[(i // 3) % len(ADJ)]
            b2 = NOUN[(i // 5) % len(NOUN)]
            c2 = PART[(i // 11) % len(PART)]
            d2 = VERB[(i // 17) % len(VERB)]
            s = f"{s}、{a2}{b2}{c2}{d2}"
        i += 1
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def main() -> None:
    rm = RuntimeManager(HOME)
    long_mode = "long" in sys.argv

    print("=" * 62)
    print("① 清理遗留进程")
    killed = rm.reap_orphan_runners()
    print(f"   清理了 {killed or '（无）'}")

    print()
    print("② 启动引擎（强效档 + 当前模型）")
    ok, host, model, msg = rm.prepare(progress=lambda m: print(f"   · {m}"),
                                      profile=Profile.TURBO,
                                      model_pref=rm.model_pref)
    if not ok:
        print(f"   失败：{msg}")
        return
    print(f"   host={host}  model={model}")

    texts = make_texts(N, long=long_mode)
    uniq = len(set(texts))
    mean_len = sum(len(t) for t in texts) / len(texts)
    skipped = sum(1 for t in texts if should_skip(t))
    print()
    print(f"③ 语料：{len(texts)} 条 · 唯一 {uniq} 条 · 均长 {mean_len:.1f} 字 · "
          f"会被预过滤的 {skipped} 条")
    if uniq != len(texts):
        print("   [警告] 语料有重复，吞吐会被放大，结果不可信")

    opt = options_from_profile("turbo", rm.model_file or "")
    print(f"   并发 {opt.concurrency} · 每批 {opt.batch_size} · "
          f"批量{'开' if opt.use_batch else '关'} · "
          f"{'精简' if opt.slim_prompt else '完整'}提示词")

    tr = OllamaTranslator(model=model, host=host, options=opt)
    print(f"   健康检查：{tr.health()}")

    done = [0]
    stamps: list[float] = []
    t0 = time.time()

    def work(t: str) -> None:
        try:
            tr.translate_one(t)
        except Exception:  # noqa: BLE001
            pass
        done[0] += 1
        stamps.append(time.time() - t0)

    print()
    print("④ 开始测量")
    with ThreadPoolExecutor(max_workers=opt.concurrency) as ex:
        list(ex.map(lambda t: work(t), texts))
    total = time.time() - t0

    stamps.sort()
    print()
    print("=" * 62)
    print(f"总耗时           : {total:.1f} s")
    print(f"累计平均         : {done[0] / total:.1f} 条/秒  ← 含引擎冷启动")
    # 稳态：跳过最前面 25% 的时间（模型加载 + 预热）
    warm_cut = total * 0.25
    steady = [s for s in stamps if s > warm_cut]
    if steady:
        span = steady[-1] - warm_cut
        print(f"稳态（后 75% 时间）: {len(steady) / span:.1f} 条/秒  ← 真实速度")
    if stamps:
        print(f"首条返回         : {stamps[0]:.1f} s（含模型加载进显存）")
        print(f"末条返回         : {stamps[-1]:.1f} s")
    print()
    print(f"效率明细：{tr.stats_line()}")


if __name__ == "__main__":
    main()
