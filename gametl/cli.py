"""gametl 命令行入口。

用法：
  python -m gametl detect   <游戏目录>
  python -m gametl extract  <解包目录> -o project.json [--engine kirikiri]
  python -m gametl translate <project.json> [--model qwen2.5:7b] [--glossary terms.json] [--limit N]
  python -m gametl writeback <project.json> -o <输出目录>
  python -m gametl stats    <project.json>
  python -m gametl glossary <project.json> -o glossary.json   # 自动生成术语表
  python -m gametl doctor           # 检查环境（Ollama/模型）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许直接 `python gametl/cli.py` 运行
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gametl.core.detect import detect_engine
from gametl.core.models import EngineType, Project
from gametl.pipeline import run_extract, run_translate, run_writeback
from gametl.translators.ollama_backend import OllamaTranslator


def cmd_detect(args) -> int:
    engine, evidence = detect_engine(Path(args.game_dir))
    print(f"引擎: {engine.value}")
    print("证据:")
    for k, v in evidence.items():
        print(f"  {k}: {v}")
    if engine == EngineType.KIRIKIRI:
        print("\n提示: KiriKiri 的 .xp3 需先用 GARbro 解包，"
              "再对解包目录执行 extract。")
    return 0


def cmd_extract(args) -> int:
    engine = EngineType(args.engine) if args.engine else None
    run_extract(Path(args.decoded_dir), Path(args.output), engine)
    return 0


def cmd_translate(args) -> int:
    run_translate(
        Path(args.project),
        model=args.model,
        glossary_path=Path(args.glossary) if args.glossary else None,
        host=args.host,
        limit=args.limit,
        target_lang=args.target_lang,
    )
    return 0


def cmd_writeback(args) -> int:
    run_writeback(Path(args.project), Path(args.output))
    return 0


def cmd_stats(args) -> int:
    p = Project.load(Path(args.project))
    s = p.stats()
    print(json.dumps(s, ensure_ascii=False, indent=2))
    # 列出未翻译条目示例
    untranslated = [u for u in p.units if not u.translated]
    if untranslated:
        print(f"\n未翻译示例（共 {len(untranslated)} 条）:")
        for u in untranslated[:5]:
            print(f"  [{u.source_file}] {u.original[:50]}")
    return 0


def cmd_glossary(args) -> int:
    """从工程自动生成术语表（统计召回 + 模型精筛）。"""
    from gametl.translators.glossary_extract import (
        build_glossary, save_glossary, can_refine)

    proj = Project.load(Path(args.project))
    print(f"工程载入：{len(proj.units)} 条原文")
    t = OllamaTranslator(model=args.model, host=args.host)
    if not t.health():
        print(f"[错误] 无法连接翻译服务 {args.host}（请先 ollama serve）",
              file=sys.stderr)
        return 1
    if not can_refine(t):
        print("[警告] 当前是翻译专用模型，术语判定可能不准（建议用通用模型）")

    gl = build_glossary(
        proj.units, t, target_lang=args.target_lang,
        on_log=lambda m: print(f"  {m}"),
        on_progress=lambda c, n, d: print(f"  {d}"))
    if not gl:
        print("没有筛出可用的术语。")
        return 0
    save_glossary(gl, Path(args.output))
    print(f"\n术语表已写入 {args.output}（{len(gl)} 条）")
    for k, v in list(gl.items())[:30]:
        print(f"  {k} => {v}")
    if len(gl) > 30:
        print(f"  … 其余 {len(gl) - 30} 条见文件")
    return 0


def cmd_doctor(args) -> int:
    print("=== 环境自检 ===")
    t = OllamaTranslator(host=args.host)
    ok = t.health()
    print(f"[{'OK' if ok else '!!'}] Ollama 服务 ({args.host}): "
          f"{'可用' if ok else '不可用 —— 请运行 ollama serve'}")
    if ok:
        models = t.list_models()
        print(f"[{'OK' if models else '..'}] 已安装模型: {models or '（无）'}")
        if args.model not in models and not any(m.startswith(args.model) for m in models):
            print(f"[!!] 目标模型 {args.model} 未安装。运行: ollama pull {args.model}")
        else:
            print(f"[OK] 目标模型 {args.model} 已就绪")

    try:
        import UnityPy  # noqa: F401
        print("[OK] UnityPy 已安装（支持 Unity 二进制资源解析）")
    except ImportError:
        print("[..] UnityPy 未安装（Unity 二进制资源解析不可用，"
              "纯文本资源仍可处理）。安装: pip install UnityPy")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gametl", description="本地游戏文本汉化工具链（Ollama 驱动）")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect", help="识别游戏引擎")
    d.add_argument("game_dir")
    d.set_defaults(func=cmd_detect)

    e = sub.add_parser("extract", help="从解包目录提取文本")
    e.add_argument("decoded_dir", help="已解包的资源目录")
    e.add_argument("-o", "--output", default="project.json")
    e.add_argument("--engine", choices=[x.value for x in EngineType if x != EngineType.UNKNOWN])
    e.set_defaults(func=cmd_extract)

    t = sub.add_parser("translate", help="用本地模型翻译")
    t.add_argument("project")
    t.add_argument("--model", default="qwen2.5:7b")
    t.add_argument("--glossary", default=None)
    t.add_argument("--host", default="http://127.0.0.1:11434")
    t.add_argument("--limit", type=int, default=None, help="仅翻译前 N 条（测试用）")
    t.add_argument("--target-lang", default="简体中文", dest="target_lang")
    t.set_defaults(func=cmd_translate)

    w = sub.add_parser("writeback", help="把译文写回文件副本")
    w.add_argument("project")
    w.add_argument("-o", "--output", default="translated")
    w.set_defaults(func=cmd_writeback)

    s = sub.add_parser("stats", help="查看项目翻译进度")
    s.add_argument("project")
    s.set_defaults(func=cmd_stats)

    doc = sub.add_parser("doctor", help="环境自检")
    doc.add_argument("--host", default="http://127.0.0.1:11434")
    doc.add_argument("--model", default="qwen2.5:7b")
    doc.set_defaults(func=cmd_doctor)

    gl = sub.add_parser("glossary",
                        help="从工程自动生成术语表（提升译名一致性）")
    gl.add_argument("project")
    gl.add_argument("-o", "--output", default="glossary.json")
    gl.add_argument("--model", default="qwen2.5:7b")
    gl.add_argument("--host", default="http://127.0.0.1:11434")
    gl.add_argument("--target-lang", default="简体中文", dest="target_lang")
    gl.set_defaults(func=cmd_glossary)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n[中断] 已停止。翻译进度已保存，可重新运行继续。")
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"[错误] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
