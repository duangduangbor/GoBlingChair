"""一键式自动化管线：从游戏目录到汉化补丁，全程无需人工干预。

流程：
   游戏目录
     │ 1. 引擎识别
     │ 2. 自动解包（.xp3/.rpa）
     │ 3. 提取文本
     │ 4. 本地模型翻译
     │ 5. 回填
     │ 6. 重打包
     ▼
   输出目录（含汉化后的封包/文件）

设计为可被 GUI 调用：所有耗时步骤通过 callback 汇报进度，
支持中途取消。
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from .core.archive import detect_archives, repack, unpack_all
from .core.detect import detect_engine
from .core.models import EngineType, Project
from .core.validate import audit_units
from .pipeline import get_extractor
from .profiles import PROFILES, Profile
from .translators.glossary import Glossary
from .translators.ollama_backend import OllamaTranslator, TranslateOptions


class Stage(str, Enum):
    DETECT = "识别引擎"
    UNPACK = "解包资源"
    EXTRACT = "提取文本"
    TRANSLATE = "翻译文本"
    AUDIT = "校对译文"
    WRITEBACK = "回填译文"
    REPACK = "重打包"
    DONE = "完成"


def options_from_profile(key, model_file: str = "") -> TranslateOptions:
    """把性能档位翻译成翻译后端的并发/批量参数。

    Args:
        key: Profile 枚举或档位字符串
        model_file: models/ 里的原始 .gguf 文件名。翻译专用模型
                    （混元 Hy-MT 等）需要换一套提示词，靠文件名判断。
    """
    if isinstance(key, Profile):
        spec = PROFILES[key]
    else:
        try:
            spec = PROFILES[Profile(str(key))]
        except (ValueError, KeyError):
            spec = PROFILES[Profile.TURBO]
    return TranslateOptions(
        concurrency=spec.num_parallel,
        batch_size=spec.batch_size,
        use_batch=spec.batch_size > 1,
        model_file=model_file,
    )


@dataclass
class AutoConfig:
    """一键流程的配置。"""
    game_dir: Path
    out_dir: Path
    model: str = "gametl-qwen"
    host: str = "http://127.0.0.1:11434"
    model_file: str = ""              # models/ 里的原始 .gguf 名（判断提示词风格）
    glossary_path: Optional[Path] = None
    target_lang: str = "简体中文"
    pack_output: bool = True          # 是否重打包
    keep_workdir: bool = True         # 保留中间文件
    limit: Optional[int] = None       # 限制翻译条数（测试用）
    work_root: Optional[Path] = None  # 中间产物根目录；None = 用 out_dir。
                                      # 指向软件盘可避开机械盘 —— 工程文件每次
                                      # 落盘都是几十 MB，放慢盘会明显拖慢翻译

    # --- 性能与质量 ---
    profile: str = "turbo"            # 性能档位（compat/normal/turbo/insane）
    options: Optional[TranslateOptions] = None   # 不传则按 profile 推导
    enable_audit: bool = True         # 完成后跑第二层规则校对（秒级、免费）
    deep_check_rate: float = 0.0      # 第三层模型抽检比例，0 = 不做
    reuse_translations: bool = True   # 复用输出目录里已有的译文（断点续传）
    skip_if_finished: bool = True     # 工程已 100% 且产物齐全时，跳过翻译/校对/回填
    # --- 翻译包 ---
    import_package: Optional[Path] = None   # 导入 .gtpkg（免模型补全译文）
    import_memory: bool = True              # 导入时用「原文」兜底匹配
    export_package: Optional[Path] = None   # 流程结束顺手导出一份 .gtpkg


@dataclass
class AutoResult:
    """一键流程的结果。"""
    success: bool = False
    engine: str = ""
    stage_reached: str = ""
    total_units: int = 0
    translated_units: int = 0
    output_dir: str = ""
    pack_files: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    error: str = ""
    # --- 质量与性能统计 ---
    audit_summary: str = ""
    audit_issues: int = 0
    audit_fixable: int = 0
    deep_flagged: int = 0
    speed_line: str = ""
    work_dir: str = ""                # 工程文件所在目录（可能不在输出目录里）
    # --- 工程进度 ---
    reused_units: int = 0             # 复用上一轮的译文条数
    filled_units: int = 0             # 工程中已有译文的条数
    already_done: bool = False        # 进来时就已经是 100%（本轮没重翻）
    finished: bool = False            # 流程走完后工程达到 100%
    unverified: int = 0               # 兜底保留（未过硬校验）的条数
    # --- 翻译包 ---
    package_imported: int = 0         # 从翻译包补上的条数
    package_exported: str = ""        # 导出的翻译包路径
    written_back: list = field(default_factory=list)   # 本轮实际重写的汉化文件


_SKIP_ON_EXPORT = {
    "_汉化输出", "_work", "output",   # 本工具自己的中间产物
    "__pycache__", ".git", ".svn",
}

#: 导出目录里的增量快照。记下每个文件的 (大小, 修改时间)，下次导出只动
#: 变动过的 —— 译文改几句就重建整个游戏目录，完全没有必要。
EXPORT_SNAPSHOT = ".gametl_export.json"


def _load_snapshot(dest: Path) -> Optional[dict]:
    try:
        raw = (Path(dest) / EXPORT_SNAPSHOT).read_text(encoding="utf-8")
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError):
        return None


def _save_snapshot(dest: Path, entries: dict) -> None:
    try:
        d = Path(dest)
        d.mkdir(parents=True, exist_ok=True)
        (d / EXPORT_SNAPSHOT).write_text(
            json.dumps({"version": 1,
                        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "files": entries},
                       ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        pass


def _sig(p: Path) -> Optional[tuple]:
    """文件的 (大小, 修改时间秒)。用于判断「和上次导出相比动没动」。"""
    try:
        st = p.stat()
        return (st.st_size, int(st.st_mtime))
    except OSError:
        return None


def export_full_game(game_dir: Path, work_root: Path, dest: Path,
                     translated_dir: Optional[Path] = None,
                     on_progress: Optional[Callable[[int, int, str], None]] = None,
                     cancel_event: Optional[threading.Event] = None,
                     clean: bool = False,
                     incremental: bool = True) -> dict:
    """导出一份「可直接开玩的完整汉化版」。

    做法是**复制游戏本体 + 覆盖回填后的文件**，而不是原地改动原游戏 ——
    原游戏保持原样，导错了随时能重来。

    ``incremental=True`` 时会在目标目录留一份快照：**第二次及以后导出
    只复制真正变动过的文件**。译文改几句就再拷一遍几个 GB，是纯粹的浪费。

    Args:
        game_dir: 原游戏目录
        work_root: 中间产物根目录（回填结果在这里找）
        dest: 目标目录（不能是游戏目录本身，也不能在游戏目录内部）
        translated_dir: 指定回填产物；None 则自动在 work_root 下探测
        on_progress: (当前, 总数, 描述)
        cancel_event: 设置后尽快中止
        clean: 目标目录已存在且有内容时，先清空再导。**调用方必须先跟用户
               确认过** —— 这里只是执行，不负责问。
        incremental: 目标目录已有上次的快照时，只复制变动过的文件

    Returns:
        {"files": 本轮实际复制数, "overwritten": 其中的汉化文件数,
         "skipped": 未变动而跳过的数, "bytes": 原游戏总字节,
         "translated_dir": 实际用到的回填产物目录, "incremental": 是否走了增量}
    """
    game_dir = Path(game_dir).resolve()
    dest = Path(dest)
    work_root = Path(work_root)

    if not game_dir.is_dir():
        raise RuntimeError(f"游戏目录不存在：{game_dir}")

    # 防止把目录拷进自己里 —— 那样会无限递归
    try:
        dest_r = dest.resolve()
    except OSError:
        dest_r = dest
    if dest_r == game_dir or game_dir in dest_r.parents:
        raise RuntimeError("目标目录不能是游戏目录本身，也不能位于游戏目录内部")

    prev = None
    if incremental and not clean:
        prev = _load_snapshot(dest)

    if clean and dest.exists():
        try:
            shutil.rmtree(str(dest), ignore_errors=True)
        except OSError:
            pass
        prev = None

    # 定位回填产物
    if translated_dir is None:
        for cand in (work_root / "_work" / "translated",
                     work_root / "_work" / "decoded_translated",
                     Path(game_dir) / "_汉化输出" / "汉化后文件"):
            if cand.is_dir() and any(cand.rglob("*")):
                translated_dir = cand
                break
    translated_dir = Path(translated_dir) if translated_dir else None

    # 先算出「最终应该长什么样」：游戏本体打底，回填产物盖在上面
    plan: dict[str, tuple[Path, str]] = {}
    total_bytes = 0
    for p in game_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(game_dir)
        if rel.parts and rel.parts[0] in _SKIP_ON_EXPORT:
            continue
        plan[str(rel)] = (p, "game")
        try:
            total_bytes += p.stat().st_size
        except OSError:
            pass

    if translated_dir is not None and translated_dir.is_dir():
        for p in translated_dir.rglob("*"):
            if not p.is_file():
                continue
            try:
                rel = str(p.relative_to(translated_dir))
            except ValueError:
                continue
            plan[rel] = (p, "translated")

    n = len(plan)
    if on_progress:
        on_progress(0, n, f"{'增量更新' if prev else '准备复制'} {n} 个文件")

    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0
    overwritten = 0
    snapshot: dict[str, dict] = {}

    for i, rel in enumerate(sorted(plan), 1):
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        src, origin = plan[rel]
        dst = dest / Path(rel)
        sig = _sig(src)
        if sig is None:
            continue
        snapshot[rel] = {"s": sig[0], "m": sig[1], "src": origin}
        if origin == "translated":
            overwritten += 1
        # 目标文件与源文件的大小、修改时间完全一致 → 内容必然相同，整段跳过。
        # 注意这里**不要求**存在上次的快照：copy2 会保留修改时间，所以
        # 用户第一次用新版导出（旧成品目录里还没有快照）也能享受到增量，
        # 不必白拷一遍几个 GB 再等下一次。
        if incremental and dst.is_file() and _sig(dst) == sig:
            skipped += 1
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        except OSError:
            continue                                  # 个别文件被占用就跳过
        if on_progress and (i % 200 == 0 or i == n):
            on_progress(i, n, f"复制 {i}/{n}")

    if on_progress:
        on_progress(n, n, "写入增量快照")
    _save_snapshot(dest, snapshot)

    return {"files": copied, "overwritten": overwritten, "skipped": skipped,
            "bytes": total_bytes, "translated_dir": str(translated_dir or ""),
            "incremental": bool(incremental),
            "had_snapshot": prev is not None}



class Cancelled(Exception):
    """用户取消。"""


class AutoPipeline:
    """一键式汉化管线。"""

    def __init__(self, config: AutoConfig,
                 on_progress: Optional[Callable[[str, int, int, str], None]] = None,
                 on_log: Optional[Callable[[str], None]] = None,
                 cancel_event: Optional[threading.Event] = None):
        """
        Args:
            on_progress: (阶段, 当前, 总数, 描述)
            on_log: 一行日志
            cancel_event: 设置后尽快中止
        """
        self.cfg = config
        self.on_progress = on_progress or (lambda *a: None)
        self.on_log = on_log or (lambda m: None)
        self.cancel_event = cancel_event or threading.Event()
        self.result = AutoResult()

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def _log(self, msg: str) -> None:
        self.on_log(msg)
        self.result.messages.append(msg)

    def run(self) -> AutoResult:
        cfg = self.cfg
        game_dir = Path(cfg.game_dir)
        out_dir = Path(cfg.out_dir)
        # 中间产物（解包结果、工程文件、增量日志）统一放 work_root。
        # 默认与输出同目录（旧行为）；显式指定软件盘后，解包与落盘都在快盘上完成，
        # 只有最终成品才写回游戏目录 —— 省掉「搬来搬去」的复制和中断风险。
        work_root = Path(cfg.work_root) if cfg.work_root else out_dir
        work_dir = work_root / "_work"
        self.result.work_dir = str(work_root)

        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            work_dir.mkdir(parents=True, exist_ok=True)

            # ---- 阶段 1：识别引擎 ----
            self._stage(Stage.DETECT, 0, 1, "分析游戏目录...")
            engine, evidence = detect_engine(game_dir)
            self.result.engine = engine.value
            self._log(f"引擎识别结果：{engine.value}")
            for k, v in evidence.items():
                self._log(f"  {k}: {v}")
            self._check_cancel()

            if engine == EngineType.UNKNOWN:
                # 也看看是不是已解包的目录
                self._log("未识别出已是完整游戏目录，尝试按已解包目录处理...")

            # ---- 阶段 2：解包 ----
            self._check_cancel()
            archives = detect_archives(game_dir)
            n_arch = len(archives["xp3"]) + len(archives["rpa"])
            decoded_dir = game_dir

            if n_arch:
                self._log(f"发现 {n_arch} 个资源包，开始解包...")
                self._stage(Stage.UNPACK, 0, n_arch, "解包中...")
                unpack_dest = work_dir / "decoded"

                def up_cb(cur, total, desc):
                    self._stage(Stage.UNPACK, cur, total, desc)

                ures = unpack_all(game_dir, unpack_dest, progress=up_cb)
                self._log(ures.summary())
                for name, reason in ures.failed:
                    self._log(f"  [警告] {name}: {reason}")

                if ures.processed:
                    decoded_dir = unpack_dest
                    # 同时把非封包文件（如 exe、配置）也复制过来，
                    # 便于重打包时保持完整
                else:
                    self._log("所有资源包都未能解包，改为直接扫描原始目录。")
            else:
                self._log("未发现资源包（可能是明文目录或已解包）。")

            self._check_cancel()

            # ---- 阶段 3：提取 ----
            self._stage(Stage.EXTRACT, 0, 1, "提取文本...")
            # 若是解包目录，引擎信息可能丢失，需用原引擎或按内容推断
            use_engine = engine
            if use_engine == EngineType.UNKNOWN:
                use_engine, _ = detect_engine(decoded_dir)

            extractor = get_extractor(use_engine, decoded_dir)
            units = extractor.extract()
            project = Project(root=decoded_dir, engine=use_engine, units=units)
            self.result.total_units = len(units)
            self._log(f"提取到 {len(units)} 条文本")
            if not units:
                self.result.error = "未提取到任何可翻译文本"
                self.result.stage_reached = Stage.EXTRACT.value
                return self.result

            project_path = work_root / "project.json"
            journal_path = work_root / "project.journal.jsonl"
            # 兼容旧版本：工程文件此前放在输出目录。发现就地沿用 ——
            # 老工程已经翻到一半，不该因为换了工作目录而白翻。
            _legacy = out_dir / "project.json"
            if not project_path.exists() and _legacy.exists():
                project_path = _legacy
                journal_path = out_dir / "project.journal.jsonl"
                self._log(f"沿用已有工程文件：{project_path}")

            # 复用上一次的译文 —— 既是断点续传，也让「只重翻可疑条目」成为可能。
            # 按 uid 匹配（uid 由 文件+位置+原文 派生，原文改过就自然失配，不会错用）。
            old_meta: dict = {}
            hit = 0
            if cfg.reuse_translations and project_path.exists():
                try:
                    old = Project.load(project_path, journal=journal_path)
                    old_meta = dict(old.meta)
                    cache = {u.uid: u for u in old.units if u.translated}
                    for u in project.units:
                        src_u = cache.get(u.uid)
                        if src_u is None:
                            continue
                        u.translated = src_u.translated
                        if src_u.extra.get("unverified"):
                            u.extra["unverified"] = True
                        hit += 1
                    if hit:
                        self._log(f"复用已有译文 {hit} 条"
                                  f"（断点续传 / 增量重翻，仅翻未完成的部分）")
                except Exception as e:  # noqa: BLE001
                    self._log(f"[警告] 读取已有项目失败，忽略：{e}")

            self.result.reused_units = hit

            # ---- 导入翻译包（免模型补全）----
            # 别的机器跑出来的译文包在这里灌进去。命中就不用再翻，
            # 于是「对方机器没装模型」也能拿到完整汉化版。
            if cfg.import_package:
                try:
                    from .core.package import import_into
                    ires = import_into(project, cfg.import_package,
                                       use_memory=cfg.import_memory)
                    man = ires["manifest"]
                    self._log(f"导入翻译包：{Path(cfg.import_package).name}"
                              f"（来自《{man.get('game') or '未知游戏'}》，"
                              f"{man.get('units_translated')} 条译文）")
                    self._log(f"  精确命中 {ires['by_uid']} 条 · 按原文复用 "
                              f"{ires['by_memory']} 条 · 工程已有 {ires['skipped']} 条"
                              f" · 仍未覆盖 {ires['missed']} 条")
                    if ires["new_uids"]:
                        want = set(ires["new_uids"])
                        Project.append_journal(
                            [(u.uid, u.translated,
                              bool(u.extra.get("unverified")))
                             for u in project.units if u.uid in want],
                            journal_path)
                    self.result.package_imported = (
                        ires["by_uid"] + ires["by_memory"])
                except Exception as e:  # noqa: BLE001
                    self._log(f"[警告] 导入翻译包失败，继续按正常流程翻译：{e}")

            filled, total_units = project.progress()
            self.result.filled_units = filled
            self._log(f"工程进度：{filled}/{total_units} 条"
                      f"（{filled / max(total_units, 1) * 100:.1f}%）")

            # ---- 「这个游戏已经翻完了」就别再翻一遍 ----
            # 光识别出已有译文只能省下推理；真正的大头是后面那些**全量重做**
            # 的阶段：校对要遍历几万条、回填要重写上百个数据文件、重打包要重新
            # 压一遍。这些都必须一并跳过，否则用户依然会觉得「程序在重复劳动」。
            already_done = project.is_complete()
            prev_finish = str(old_meta.get("finished_at") or "")
            if already_done:
                self.result.already_done = True
                when = f"，上次完成于 {prev_finish}" if prev_finish else ""
                self._log(f"✅ 该项目已全部翻完（{filled}/{total_units} 条）{when}")

            # 已经全翻完时不必再写一遍工程文件（没有任何新内容）
            if not already_done:
                project.save(project_path)
            self._check_cancel()

            # ---- 阶段 4：翻译 ----
            skip_translate = already_done and cfg.skip_if_finished
            opt = cfg.options or options_from_profile(cfg.profile,
                                                      cfg.model_file)
            glossary = Glossary.load(cfg.glossary_path)
            translator: Optional[OllamaTranslator] = None
            done = 0

            if skip_translate:
                self._log("跳过翻译阶段 —— 没有未完成的条目，直接用已有译文")
                self._stage(Stage.TRANSLATE, 1, 1, "已跳过（全部已译）")
            else:
                self._stage(Stage.TRANSLATE, 0, len(units), "准备翻译...")
                translator = OllamaTranslator(model=cfg.model, host=cfg.host,
                                              options=opt)
                if not translator.health():
                    self.result.error = (
                        f"无法连接本地翻译服务 ({cfg.host})。\n"
                        "请先启动 Ollama（运行 gametl/serve.sh 或启动Ollama.bat）。")
                    self.result.stage_reached = Stage.TRANSLATE.value
                    return self.result

                models = translator.list_models()
                if cfg.model not in models and not any(
                        m.startswith(cfg.model) for m in models):
                    self.result.error = (
                        f"翻译模型 {cfg.model} 未安装。已安装：{models or '（无）'}")
                    self.result.stage_reached = Stage.TRANSLATE.value
                    return self.result

                if len(glossary):
                    self._log(f"载入术语表 {len(glossary)} 条")

                todo = units if cfg.limit is None else units[:cfg.limit]
                total = len(todo)
                self._log(f"开始翻译 {total} 条（模型：{cfg.model}）")
                self._log(f"  并发 {opt.concurrency} 路 · 每批 {opt.batch_size} 条 · "
                          f"批量{'开' if opt.use_batch else '关'} · "
                          f"{'精简' if opt.slim_prompt else '完整'}提示词")

                def tr_cb(i, n, preview):
                    self._check_cancel()
                    self._stage(Stage.TRANSLATE, i, n, preview[:50])

                def save_proj(entries):
                    # 只追加新增译文（几十字节），不再把整个工程重写一遍
                    Project.append_journal(entries, journal_path)

                t0 = time.time()
                done = translator.translate_batch(
                    todo, glossary=glossary, target_lang=cfg.target_lang,
                    progress=tr_cb, save_every=10, on_save=save_proj,
                    options=opt,
                    cancel_check=lambda: self.cancel_event.is_set())
                elapsed = max(time.time() - t0, 0.001)

                self.result.translated_units = done
                rate = done / elapsed
                self.result.speed_line = f"{rate:.1f} 条/秒"
                self._log(f"翻译完成：{done}/{total} 条，耗时 {elapsed:.1f}s "
                          f"（{rate:.1f} 条/秒）")
                self._log(f"效率明细：{translator.stats_line()}")
                self._check_cancel()

            # ---- 阶段 5：校对（规则层，秒级、免费）----
            if cfg.enable_audit:
                self._stage(Stage.AUDIT, 0, 1, "校验译文...")
                rep = audit_units(project.units, glossary=glossary.mapping,
                                  target_lang=cfg.target_lang)
                self.result.audit_summary = rep.summary()
                self.result.audit_issues = len(rep.issues)
                self.result.audit_fixable = len(rep.fixable_uids())
                self._log(rep.summary())
                if rep.issues:
                    for code, n in sorted(rep.by_code().items(),
                                          key=lambda kv: -kv[1]):
                        self._log(f"    · {code}: {n} 处")
                # 校对只做判断、不改数据，所以**不需要**在这里再存一次工程
                # （那是白白多写几十 MB）
                self._check_cancel()

            # ---- 阶段 5.5：深度校对（模型抽检，可选）----
            if cfg.deep_check_rate > 0 and not skip_translate and translator:
                from .translators.quality import QualityChecker

                self._stage(Stage.AUDIT, 0, 1, "模型抽检中...")

                def q_cb(c, n, d):
                    self._check_cancel()
                    self._stage(Stage.AUDIT, c, n, d)

                qc = QualityChecker(translator, concurrency=opt.concurrency)
                rres = qc.review(
                    project.units, sample_rate=cfg.deep_check_rate,
                    progress=q_cb,
                    cancel_check=lambda: self.cancel_event.is_set())
                self.result.deep_flagged = len(rres.flagged)
                self._log(rres.summary())
                self._check_cancel()

            # ---- 合并增量日志 / 写完成标记 ----
            # 已经翻完、并且**已经**有完成标记的话，连这一次全量写都省掉 ——
            # 重跑一个完成的工程，磁盘上不该再出现任何几十 MB 的写入。
            need_compact = (not already_done) or not prev_finish
            if need_compact:
                try:
                    filled_now = sum(1 for u in project.units if u.translated)
                    meta_upd = None
                    if project.units and filled_now >= len(project.units):
                        meta_upd = {
                            "engine": project.engine.value,
                            "units": len(project.units),
                            "filled": filled_now,
                            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "unverified": len(project.unverified_uids()),
                            "game_dir": str(game_dir),
                            "out_dir": str(out_dir),
                        }
                        self.result.finished = True
                    merged = Project.compact(project_path, journal_path,
                                             meta_update=meta_upd)
                    self.result.filled_units = filled_now
                    self.result.unverified = len(merged.unverified_uids())
                    if meta_upd:
                        self._log(f"✅ 工程已 100% 完成（{filled_now} 条），"
                                  f"已写下完成标记")
                    if self.result.unverified:
                        self._log(f"注：{self.result.unverified} 条是兜底保留的"
                                  f"（未通过硬校验），建议点一次「快速校对」复核")
                except Exception as e:  # noqa: BLE001
                    self._log(f"[警告] 合并增量日志失败：{e}")
            else:
                self.result.finished = True
                self.result.unverified = len(project.unverified_uids())
                self._log("无需重写工程文件（内容无变化）")
            self._check_cancel()

            # ---- 阶段 6：回填 ----
            # 上一轮的产物还在，不代表它「够用」：只要这次提取到了新的条目
            # （比如新版工具多覆盖了 plugins.js），就必须重新回填。所以用一份
            # 状态文件把「上次回填的是什么」记下来，对不上就重做。
            translated_dir = work_dir / "translated"
            wb_state_path = work_dir / "writeback_state.json"
            filled_now = sum(1 for u in project.units if u.translated)
            wb_done = False
            if skip_translate and translated_dir.is_dir():
                try:
                    st = json.loads(wb_state_path.read_text(encoding="utf-8"))
                    files = st.get("files") or []
                    wb_done = (st.get("units") == len(project.units)
                               and st.get("translated") == filled_now
                               and bool(files)
                               and all((translated_dir / r).is_file()
                                       for r in files))
                except (OSError, ValueError):
                    wb_done = False
                if not wb_done:
                    self._log("上一轮的回填产物与新提取结果不一致，重新回填。")
            if wb_done:
                self._log(f"跳过回填阶段 —— 上一轮的回填产物仍然有效："
                          f"{translated_dir}")
                self._stage(Stage.WRITEBACK, 1, 1, "已跳过（产物已有）")
            else:
                self._stage(Stage.WRITEBACK, 0, 1, "回填译文...")
                stats = extractor.write_back(project, translated_dir)
                self._log(f"回填完成：写出 {stats['files_written']} 个文件"
                          f"（{stats['fields_replaced']} 处译文，"
                          f"{stats.get('unchanged', 0)} 个文件内容未变）")
                self.result.written_back = stats.get("changed") or []
                try:
                    wb_state_path.write_text(json.dumps(
                        {"units": len(project.units),
                         "translated": filled_now,
                         "files": sorted(stats.get("changed") or [])},
                        ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
            self._check_cancel()

            # ---- 阶段 7：重打包/输出 ----
            self._stage(Stage.REPACK, 0, 1, "生成补丁...")
            final_dir = out_dir / "汉化补丁"
            final_dir.mkdir(parents=True, exist_ok=True)

            if cfg.pack_output and n_arch:
                pstats = repack(unpack_dest, translated_dir, final_dir,
                                source_game_dir=game_dir)
                self.result.pack_files = pstats["packed"]
                self._log(f"重打包完成：{pstats['packed']}")
                # 同时输出明文修改版，便于用户手动覆盖
                plain_out = out_dir / "汉化后文件"
                if translated_dir.exists():
                    if plain_out.exists():
                        shutil.rmtree(plain_out, ignore_errors=True)
                    shutil.copytree(translated_dir, plain_out)
                    self._log(f"同时输出明文修改版到：{plain_out.name}/")
            else:
                # 无封包或不需要打包：直接输出文件
                if translated_dir.exists():
                    if final_dir.exists():
                        shutil.rmtree(final_dir, ignore_errors=True)
                    shutil.copytree(translated_dir, final_dir)
                    self._log(f"汉化后文件已输出到：{final_dir}")

            self.result.output_dir = str(final_dir)
            self.result.stage_reached = Stage.DONE.value
            self.result.success = True

            xf = sum(1 for u in project.units if u.translated)
            self._log(f"译文覆盖：{xf}/{len(project.units)} 条"
                      f"（复用 {hit} 条 · 本轮新译 {done} 条）")
            self._log(f"成品目录：{final_dir}")
            if self.result.already_done:
                self._log("本次没有重新翻译任何条目 —— 工程此前已完成。")

            # ---- 顺手导出翻译包 ----
            if cfg.export_package:
                try:
                    from .core.package import export_package
                    pres = export_package(project, cfg.export_package,
                                          target_lang=cfg.target_lang)
                    self._log(f"翻译包已导出：{pres['path']}"
                              f"（{pres['units']} 条 · "
                              f"{pres['bytes'] / 1048576:.1f} MB · "
                              f"可直接在别的电脑上导入，无需模型）")
                    self.result.package_exported = pres["path"]
                except Exception as e:  # noqa: BLE001
                    self._log(f"[警告] 导出翻译包失败：{e}")

            # 清理中间文件（可选）
            if not cfg.keep_workdir:
                shutil.rmtree(work_dir, ignore_errors=True)

            self._stage(Stage.DONE, 1, 1, "全部完成")
            return self.result

        except Cancelled:
            self.result.error = "用户已取消"
            self.result.stage_reached = "已取消"
            self._log("任务已取消。中间文件保留，可重新运行续传。")
            return self.result
        except Exception as e:  # noqa: BLE001
            import traceback
            self.result.error = f"{type(e).__name__}: {e}"
            self._log(f"[错误] {self.result.error}")
            self._log(traceback.format_exc())
            return self.result

    def _stage(self, stage: Stage, cur: int, total: int, desc: str) -> None:
        self.on_progress(stage.value, cur, total, desc)
