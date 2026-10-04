# -*- coding: utf-8 -*-
"""端到端测试便携运行时：启动内置 serve + 导入模型 + 真实翻译。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gametl.runtime_manager import RuntimeManager
from gametl.translators.glossary import Glossary
from gametl.translators.ollama_backend import OllamaTranslator

HOME = Path(r"D:/滚刀哥布林汉化椅")

print("=" * 64)
print("便携运行时端到端测试")
print("=" * 64)
print(f"软件目录: {HOME}")
print()

rm = RuntimeManager(HOME, on_log=lambda m: print(f"  [引擎] {m}", flush=True))

t0 = time.monotonic()
ok, host, model, msg = rm.prepare()
elapsed = time.monotonic() - t0

print()
print(f"  结果:     {'成功 ✓' if ok else '失败 ✗'}")
print(f"  host:     {host}")
print(f"  模型:     {model}")
print(f"  消息:     {msg}")
print(f"  耗时:     {elapsed:.1f}s")
print()

if not ok:
    print("测试中止")
    rm.shutdown()
    sys.exit(1)

# ---- 真实翻译 ----
print("=" * 64)
print("真实翻译测试")
print("=" * 64)

tr = OllamaTranslator(model=model, host=host, timeout=180)
print(f"  health:  {tr.health()}")
print(f"  models:  {tr.list_models()}")
print()

gl = Glossary({
    "アレン": "艾伦",
    "エレン": "爱莲",
    "ギルド": "公会",
})

samples = [
    ("アレン", "おはよう、エレン。今日もいい天気だね。"),
    ("エレン", "そうね。ギルドに行く前に朝ごはんを食べましょう。"),
    ("", "【0】を持って【1】へ行け。"),
]

for speaker, text in samples:
    t1 = time.monotonic()
    out = tr.translate_one(text, glossary=gl,
                           context={"speaker": speaker} if speaker else {})
    dt = time.monotonic() - t1
    tag = f"{speaker}: " if speaker else ""
    print(f"  {tag}{text}")
    print(f"    -> {out}   ({dt:.2f}s)")
    print()

# ---- 显存/资源占用 ----
print("=" * 64)
print("资源占用")
print("=" * 64)
data_dir = HOME / "data"
total = sum(p.stat().st_size for p in data_dir.rglob("*") if p.is_file())
print(f"  data\\ 目录: {total/1024/1024/1024:.2f} GB")
whole = sum(p.stat().st_size for p in HOME.rglob("*") if p.is_file())
print(f"  软件总大小: {whole/1024/1024/1024:.2f} GB")

print()
print("测试完成，关闭内置服务…")
rm.shutdown()
print("已关闭")
