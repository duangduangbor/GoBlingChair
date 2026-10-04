"""管线编排：把「识别 -> 提取 -> 翻译 -> 回填」串成一条流水线。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .core.detect import detect_engine
from .core.models import EngineType, Project
from .translators.glossary import Glossary
from .translators.ollama_backend import OllamaTranslator


def get_extractor(engine: EngineType, decoded_dir: Path):
    """按引擎类型返回对应提取器实例。"""
    from .extractors.kirikiri import KiriKiriExtractor
    from .extractors.rpgmaker import RPGMakerExtractor
    from .extractors.renpy import RenPyExtractor

    if engine == EngineType.KIRIKIRI:
        return KiriKiriExtractor(decoded_dir)
    if engine == EngineType.RPGMAKER_MV:
        return RPGMakerExtractor(decoded_dir)
    if engine == EngineType.RENPY:
        return RenPyExtractor(decoded_dir)
    if engine == EngineType.UNITY:
        from .extractors.unity import UnityExtractor
        return UnityExtractor(decoded_dir)
    raise ValueError(f"暂不支持该引擎的提取: {engine}")


def run_extract(decoded_dir: Path, out_project: Path,
                engine: Optional[EngineType] = None) -> Project:
    """提取阶段：识别引擎并抽取全部文本，保存为项目文件。"""
    decoded_dir = Path(decoded_dir)
    if engine is None:
        engine, evidence = detect_engine(decoded_dir)
        print(f"[识别] 引擎 = {engine.value}")
        for k, v in evidence.items():
            print(f"       {k}: {v}")
        if engine == EngineType.UNKNOWN:
            raise RuntimeError(
                "无法识别引擎。请确认目录内容，或手动指定引擎。\n"
                "提示：KiriKiri 需要先用 GARbro 解包 .xp3，再对解包目录运行提取。")

    extractor = get_extractor(engine, decoded_dir)
    units = extractor.extract()
    project = Project(root=decoded_dir, engine=engine, units=units)
    project.meta["extractor"] = type(extractor).__name__
    project.save(out_project)

    s = project.stats()
    print(f"[提取] 共 {s['total_units']} 条文本，{s['total_chars']} 字")
    print(f"       类型分布: {s['kinds']}")
    print(f"       项目文件: {out_project}")
    return project


def run_translate(project_path: Path, model: str = "qwen2.5:7b",
                  glossary_path: Optional[Path] = None,
                  host: str = "http://127.0.0.1:11434",
                  limit: Optional[int] = None,
                  target_lang: str = "简体中文") -> Project:
    """翻译阶段：调用本地模型翻译项目中的全部文本，实时落盘。"""
    project_path = Path(project_path)
    journal_path = project_path.with_suffix(".journal.jsonl")
    project = Project.load(project_path, journal=journal_path)
    glossary = Glossary.load(glossary_path)

    translator = OllamaTranslator(model=model, host=host)
    if not translator.health():
        raise RuntimeError(
            f"无法连接 Ollama 服务 ({host})。请先启动：ollama serve")
    models = translator.list_models()
    if model not in models and not any(m.startswith(model) for m in models):
        raise RuntimeError(
            f"模型 {model} 未安装。已安装: {models}\n"
            f"请先拉取：ollama pull {model}")

    units = project.units if limit is None else project.units[:limit]
    total = len(units)
    print(f"[翻译] 模型={model} 待处理={total} 条 术语表={len(glossary)} 条")
    print("       （首次调用需加载模型，可能等待数十秒）")

    import time
    t0 = time.time()

    def progress(i: int, n: int, preview: str) -> None:
        pv = preview[:40].replace("\n", " ")
        print(f"\r  [{i}/{n}] {pv:<44}", end="", flush=True)

    def save(entries) -> None:
        # 增量追加，避免每次落盘都重写整个工程
        Project.append_journal(entries, journal_path)

    done = translator.translate_batch(
        units, glossary=glossary, target_lang=target_lang,
        progress=progress, save_every=10, on_save=save)

    # 合并增量日志，交还给调用方一个完整工程
    try:
        project = Project.compact(project_path, journal_path)
    except Exception:  # noqa: BLE001
        pass

    elapsed = time.time() - t0
    print()
    print(f"[完成] 翻译 {done}/{total} 条，耗时 {elapsed:.1f}s "
          f"（{elapsed / max(done, 1):.1f}s/条）")
    return project


def run_writeback(project_path: Path, out_dir: Path) -> dict:
    """回填阶段：把译文写回文件副本，准备重新打包。"""
    project = Project.load(project_path)
    extractor = get_extractor(project.engine, project.root)
    stats = extractor.write_back(project, Path(out_dir))
    print(f"[回填] {stats}")
    return stats
