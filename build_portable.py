# -*- coding: utf-8 -*-
"""把便携运行时（Ollama + 模型）复制进软件目录。

结构：
    D:\\滚刀哥布林汉化椅\\
      ├─ runtime\\   ollama.exe + lib/
      └─ models\\    *.gguf（用户可替换）
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

SRC_LLAMA = Path(r"C:\Users\h\Tools\ollama")
GGUF_SRC = Path(r"D:\ollama-models\gguf\Qwen2.5-7B-Instruct-Q4_K_M.gguf")
DST = Path(r"D:\滚刀哥布林汉化椅")
RUNTIME = DST / "runtime"
MODELS = DST / "models"

done_bytes = [0]
t0 = time.monotonic()


def report(name: str, size: int) -> None:
    done_bytes[0] += size
    mb = done_bytes[0] / 1024 / 1024
    el = time.monotonic() - t0
    print(f"  [{mb:8.1f} MB | {el:6.1f}s] {name}", flush=True)


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    report(src.name, src.stat().st_size)


def copy_tree(src: Path, dst: Path) -> int:
    n = 0
    for p in src.rglob("*"):
        if p.is_file():
            rel = p.relative_to(src)
            copy_file(p, dst / rel)
            n += 1
    return n


def main() -> int:
    print("=" * 60)
    print("构建便携运行时")
    print("=" * 60)
    print(f"目标: {DST}")
    print()

    # --- 1. runtime ---
    print("[1/3] 复制 Ollama 运行时…")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    copy_file(SRC_LLAMA / "ollama.exe", RUNTIME / "ollama.exe")
    n = copy_tree(SRC_LLAMA / "lib", RUNTIME / "lib")
    print(f"      lib/ 共 {n} 个文件")
    print()

    # --- 2. models ---
    print("[2/3] 复制模型（约 4.7GB，请稍候）…")
    MODELS.mkdir(parents=True, exist_ok=True)
    if not GGUF_SRC.exists():
        print(f"  [错误] 找不到模型源文件: {GGUF_SRC}")
        return 1
    dst_gguf = MODELS / GGUF_SRC.name
    if dst_gguf.exists() and dst_gguf.stat().st_size == GGUF_SRC.stat().st_size:
        print(f"  已存在且大小一致，跳过：{dst_gguf.name}")
    else:
        copy_file(GGUF_SRC, dst_gguf)
    print()

    # --- 3. 校验 ---
    print("[3/3] 校验…")
    a = GGUF_SRC.stat().st_size
    b = dst_gguf.stat().st_size
    print(f"  源大小:   {a:,} 字节")
    print(f"  副本大小: {b:,} 字节")
    print(f"  一致: {'✓' if a == b else '✗ 不一致！'}")
    ok_exe = (RUNTIME / "ollama.exe").exists()
    print(f"  ollama.exe: {'✓' if ok_exe else '✗'}")

    total = sum(p.stat().st_size for p in DST.rglob("*") if p.is_file())
    print()
    print(f"完成，软件目录当前共 {total/1024/1024/1024:.2f} GB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
