# -*- coding: utf-8 -*-
"""A/B 实测：模型「部分上卡」到底损失多少速度。

同一模型、同一语料、同一并发，唯一变量是 ``num_gpu``（放到显卡上的层数）：
  A 组 28 层（改造前的实际状态）
  B 组 33 层（清理孤儿进程之后）

★ 语料是自己造的合成日语，不读取、不输出任何游戏文本。

    python bench_ab.py [条数]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_v15 import make_texts                          # noqa: E402
from gametl.profiles import Profile                       # noqa: E402
from gametl.runtime_manager import RuntimeManager         # noqa: E402

HOME = Path(r"D:\滚刀哥布林汉化椅")

HOST = "http://127.0.0.1:11435"
MODEL = "gametl-model"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 160
CONC = 4

SYSTEM = "你是日译中翻译器。只输出简体中文译文，不要解释。"
PROMPT_TPL = "把下面这句日语翻译成简体中文：\n{src}"


def loaded_vram() -> tuple:
    """回读 /api/ps：显存里的模型大小 vs 总大小。"""
    try:
        with urllib.request.urlopen(f"{HOST}/api/ps", timeout=10) as r:
            data = json.loads(r.read())
    except Exception:  # noqa: BLE001
        return (0.0, 0.0)
    for m in data.get("models", []):
        return ((m.get("size_vram") or 0) / 1048576, (m.get("size") or 0) / 1048576)
    return (0.0, 0.0)


def one(src: str, num_gpu: int) -> None:
    body = json.dumps({
        "model": MODEL,
        "system": SYSTEM,
        "prompt": PROMPT_TPL.format(src=src),
        "stream": False,
        "keep_alive": "30m",
        "options": {
            "temperature": 0.2, "top_p": 0.9, "num_ctx": 4096,
            "num_predict": 256, "num_gpu": num_gpu,
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{HOST}/api/generate", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            r.read()
    except Exception:  # noqa: BLE001
        pass


def run_arm(num_gpu: int, texts: list) -> dict:
    done = [0]
    stamps: list[float] = []
    t0 = time.time()

    def work(s: str) -> None:
        one(s, num_gpu)
        done[0] += 1
        stamps.append(time.time() - t0)

    with ThreadPoolExecutor(max_workers=CONC) as ex:
        list(ex.map(work, texts))
    total = time.time() - t0
    stamps.sort()
    cut = total * 0.25
    steady = [s for s in stamps if s > cut]
    span = (steady[-1] - cut) if steady else 0
    vram, size = loaded_vram()
    return {
        "layers": num_gpu, "total": total, "n": done[0],
        "avg": done[0] / total if total else 0,
        "steady": (len(steady) / span) if span > 0 else 0,
        "vram": vram, "size": size,
    }


def main() -> None:
    rm = RuntimeManager(HOME)
    rm.reap_orphan_runners()
    ok, _host, _model, msg = rm.prepare(progress=lambda m: print(f"   · {m}"),
                                        profile=Profile.TURBO)
    if not ok:
        print(f"引擎启动失败：{msg}")
        return

    texts = make_texts(N, long=True)
    print(f"语料 {len(texts)} 条 · 唯一 {len(set(texts))} · "
          f"均长 {sum(len(t) for t in texts) / len(texts):.1f} 字")
    print(f"并发 {CONC} · 模型 {MODEL}")
    print()

    arms = []
    for g in (28, 33):
        print(f"── 跑 {g} 层上卡 …")
        r = run_arm(g, texts)
        arms.append(r)
        print(f"   总耗时 {r['total']:.1f}s · 累计 {r['avg']:.1f} 条/秒 · "
              f"稳态 {r['steady']:.1f} 条/秒")
        print(f"   显存内 {r['vram']:.0f} / {r['size']:.0f} MB "
              f"（{r['vram'] / r['size'] * 100 if r['size'] else 0:.0f}% 在卡上）")
        print()

    a, b = arms[0], arms[1]
    print("=" * 62)
    print(f"A 组 {a['layers']} 层上卡 : 稳态 {a['steady']:.1f} 条/秒"
          f"（{a['vram'] / a['size'] * 100 if a['size'] else 0:.0f}% 在显存）")
    print(f"B 组 {b['layers']} 层上卡 : 稳态 {b['steady']:.1f} 条/秒"
          f"（{b['vram'] / b['size'] * 100 if b['size'] else 0:.0f}% 在显存）")
    if a["steady"] > 0:
        print(f"提速倍数 : {b['steady'] / a['steady']:.2f}x")
        print(f"损失比例 : {(1 - a['steady'] / b['steady']) * 100:.0f}% "
              f"（那 5 层跑在 CPU 上白白丢掉的）")


if __name__ == "__main__":
    main()
