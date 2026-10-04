# -*- coding: utf-8 -*-
"""验证四档模式定义与硬件自动检测。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gametl.profiles import (  # noqa: E402
    PROFILES, PROFILE_ORDER, classify_model, detect_hardware,
    is_translation_model, model_label, pick_model, recommend_profile,
)

FAILED = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  [{'OK' if ok else '失败'}] {name}: {got!r}"
          + ("" if ok else f"（期望 {want!r}）"))
    if not ok:
        FAILED.append(name)


def main() -> int:
    hw = detect_hardware()
    print("=" * 60)
    print("硬件检测")
    print("=" * 60)
    print(f"  显卡   : {hw.summary()}")
    print(f"  内存   : 总 {hw.ram_total_mb/1024:.1f} GB"
          f" / 可用 {hw.ram_avail_mb/1024:.1f} GB")
    print(f"  CUDA   : {hw.has_cuda}")
    if hw.error:
        print(f"  错误   : {hw.error}")

    rec = recommend_profile(hw)
    spec = PROFILES[rec]
    print(f"  推荐档 : {spec.emoji} {spec.name}（{spec.tagline}）")

    print()
    print("=" * 60)
    print("四档参数")
    print("=" * 60)
    for p in PROFILE_ORDER:
        s = PROFILES[p]
        mark = " ← 推荐" if p == rec else ""
        print(f"{s.emoji} {s.name}：并发{s.num_parallel} 槽 / 上下文{s.num_ctx}"
              f" / KV {s.kv_cache_type} / 批{s.batch_size} 条"
              f" / {s.speed_hint} / {s.vram_hint}{mark}")

    print()
    print("=" * 60)
    print("模型挑选测试")
    print("=" * 60)

    # 用真实文件（大小不同）测 quality / speed，纯名字的用假路径测 auto
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="gametl_profiles_"))
    specs = [
        ("Qwen2.5-7B-Instruct-Q4_K_M.gguf", 4096),
        ("HY-MT2-1.8B-Q4_K_M.gguf", 1024),
        ("Hunyuan-MT-7B-Q4_K_M.gguf", 3072),
    ]
    real = []
    for nm, kb in specs:
        p = tmp / nm
        p.write_bytes(b"\0" * (kb * 1024))
        real.append(p)

    for p in real:
        print(f"  {p.name:38s} -> 档位 {classify_model(p.name):7s}"
              f" · 翻译专用 {is_translation_model(p.name)}")
    print()

    check("auto + 大模型倾向（强效档）",
          pick_model(real, "auto", prefer_small=False).name,
          "Hunyuan-MT-7B-Q4_K_M.gguf")
    check("auto + 小模型倾向（狂暴档）",
          pick_model(real, "auto", prefer_small=True).name,
          "HY-MT2-1.8B-Q4_K_M.gguf")
    check("quality（挑最大）",
          pick_model(real, "quality").name,
          "Qwen2.5-7B-Instruct-Q4_K_M.gguf")
    check("speed（挑最小）",
          pick_model(real, "speed").name,
          "HY-MT2-1.8B-Q4_K_M.gguf")
    check("显式指定文件名",
          pick_model(real, "Hunyuan-MT-7B-Q4_K_M.gguf").name,
          "Hunyuan-MT-7B-Q4_K_M.gguf")
    check("文件名片段也能匹配",
          pick_model(real, "hy-mt2").name,
          "HY-MT2-1.8B-Q4_K_M.gguf")
    check("名字写错时退回 auto 不报错",
          pick_model(real, "不存在的模型").name,
          "Hunyuan-MT-7B-Q4_K_M.gguf")
    check("空列表返回 None", pick_model([]), None)
    check("标签压缩", model_label("Hy-MT2-1.8B-Q4_K_M.gguf"), "Hy-MT2-1.8B")

    try:
        for p in real:
            p.unlink()
        tmp.rmdir()
    except OSError:
        pass

    print()
    if FAILED:
        print(f"[失败] {len(FAILED)} 项未通过：{', '.join(FAILED)}")
        return 1
    print("[OK] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
