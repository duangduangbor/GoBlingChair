"""v1.4 新增行为回归测试（全程离线，翻译器用桩替换）。

覆盖：
  1. 工程进度 / 完成标记 / 只读头部 meta
  2. 增量日志的 uv（兜底保留）标记往返
  3. compact(meta_update=...) 把完成标记和日志合并进同一次写入
  4. 失败条目兜底：重试耗尽不再抛错、而是保留最后一次输出
  5. 服务日志轮转
  6. 「这个标签到底是从哪个 gguf 建的」—— tag_source 读清单
  7. 端到端：同一个游戏跑两遍，第二遍必须**跳过翻译与回填**，且不再写大文件
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import gametl.auto as auto_mod                                    # noqa: E402
from gametl.auto import AutoConfig, AutoPipeline                   # noqa: E402
from gametl.core.models import EngineType, Project, TextUnit       # noqa: E402
from gametl.runtime_manager import RuntimeManager                  # noqa: E402
from gametl.translators.ollama_backend import (                    # noqa: E402
    OllamaTranslator, TranslateOptions)

TMP = Path(tempfile.mkdtemp(prefix="gametl_v14_"))
_ok = 0
_bad = 0


def rec(cond, msg):
    global _ok, _bad
    if cond:
        _ok += 1
        print(f"[OK]   {msg}")
    else:
        _bad += 1
        print(f"[FAIL] {msg}")


# ================================================================ 1 + 2 + 3
print("=" * 60)
print("1. 工程进度 / 完成标记 / 日志标记")
print("=" * 60)
try:
    pp = TMP / "p1" / "project.json"
    pp.parent.mkdir(parents=True, exist_ok=True)
    units = [
        TextUnit(uid="u1", source_file="a.json", location={"i": 0}, original="あ"),
        TextUnit(uid="u2", source_file="a.json", location={"i": 1}, original="い"),
        TextUnit(uid="u3", source_file="a.json", location={"i": 2}, original="う"),
    ]
    proj = Project(root=TMP, engine=EngineType.RPGMAKER_MV, units=units)
    rec(proj.progress() == (0, 3), "空工程 progress=(0,3)")
    rec(not proj.is_complete(), "空工程不算完成")
    proj.save(pp)

    jp = TMP / "p1" / "project.journal.jsonl"
    Project.append_journal([("u1", "阿"), ("u2", "伊", 1)], jp)
    rec(sum(1 for _ in open(jp, encoding="utf-8")) == 2, "日志写入 2 行")
    rec("\"uv\": 1" in jp.read_text(encoding="utf-8") or
        "\"uv\":1" in jp.read_text(encoding="utf-8"), "日志带 uv 标记")

    back = Project.compact(pp, jp, meta_update={"units": 3, "filled": 2})
    rec(not jp.exists(), "compact 后日志被删除")
    rec(back.meta.get("filled") == 2, "meta_update 生效")
    rec(back.units[1].extra.get("unverified") is True, "uv 回放到 extra")
    rec(back.units[0].extra.get("unverified") is None, "普通条目不带 uv")
    rec(back.progress() == (2, 3), "合并后 progress=(2,3)")
    rec(back.unverified_uids() == ["u2"], "unverified_uids 只列兜底条目")

    m = Project.read_meta(pp)
    rec(m.get("units") == 3 and m.get("filled") == 2, "read_meta 读到 meta")
    rec(Project.read_meta(TMP / "不存在.json") == {}, "read_meta 缺文件返回空")
    # 只读头部必须真的只读头部：截断到顶层 units 之前
    # （注意用 rfind —— meta 里也有一个 "units" 键）
    raw = pp.read_text(encoding="utf-8")
    head_only = raw[: raw.rfind('"units"')]
    tiny = TMP / "p1" / "tiny.json"
    tiny.write_text(head_only + "}", encoding="utf-8")
    rec(Project.read_meta(tiny).get("filled") == 2, "read_meta 不依赖完整 JSON")

    # 完成标记
    back.units[2].translated = "宇"
    rec(back.is_complete(), "补齐后 is_complete")
    wrote = back.mark_finished(pp, game_dir=str(TMP), out_dir=str(TMP / "o"))
    rec(wrote, "mark_finished 返回 True")
    m2 = Project.read_meta(pp)
    rec(bool(m2.get("finished_at")), "完成时间已写入 meta")
    rec(m2.get("unverified") == 1, "完成标记里带兜底条数")
    p2 = Project(root=TMP, engine=EngineType.RPGMAKER_MV,
                 units=[TextUnit(uid="z", source_file="a", location={},
                                 original="あ")])
    rec(not p2.mark_finished(TMP / "p2.json"), "没翻完时不写完成标记")
except Exception as e:  # noqa: BLE001
    rec(False, f"进度/标记 异常：{e!r}")


# ================================================================ 4
print()
print("=" * 60)
print("2. 失败条目兜底（重试耗尽不再丢条目）")
print("=" * 60)


class _AlwaysBad(OllamaTranslator):
    """永远返回「仍是日文」的译文，必然过不了第一层硬校验。"""

    def _post(self, payload):
        self.stats["requests"] += 1
        return {"response": "こんにちは"}


class _AlwaysErr(OllamaTranslator):
    def _post(self, payload):
        raise OSError("网络不通")


try:
    opt = TranslateOptions(max_retries=2, verify=True)
    t = _AlwaysBad(model="x", host="http://127.0.0.1:1", options=opt)
    raised = False
    try:
        t.translate_one("こんにちは")
    except RuntimeError:
        raised = True
    rec(raised, "不给 allow_partial 时照旧抛错（行为不变）")

    t2 = _AlwaysBad(model="x", host="http://127.0.0.1:1", options=opt)
    out = t2.translate_one("こんにちは", allow_partial=True)
    rec(out == "こんにちは", "allow_partial 返回最后一次的输出")
    rec(t2.stats["unverified"] == 1, "stats 记了一次兜底")

    t3 = _AlwaysErr(model="x", host="http://127.0.0.1:1", options=opt)
    raised = False
    try:
        t3.translate_one("こんにちは", allow_partial=True)
    except RuntimeError:
        raised = True
    rec(raised, "纯网络故障仍然抛错（不兜底空译文）")

    # 正常译文不受影响
    t4 = OllamaTranslator(model="x", host="http://127.0.0.1:1", options=opt)
    t4._post = lambda payload: {"response": "  \"你好\"  "}
    rec(t4.translate_one("こんにちは") == "你好", "正常译文照旧（去引号+trim）")
except Exception as e:  # noqa: BLE001
    rec(False, f"兜底 异常：{e!r}")


# ================================================================ 5
print()
print("=" * 60)
print("3. 服务日志轮转")
print("=" * 60)
try:
    lp = TMP / "server.log"
    lp.write_bytes(b"x" * (3 * 1024 * 1024))
    freed = RuntimeManager.rotate_log(lp, max_mb=1)
    rec(freed == 3 * 1024 * 1024, "超限日志被轮转，返回释放字节数")
    rec((not lp.exists()) or lp.stat().st_size == 0, "当前日志已让位（改名或清空）")
    rec(lp.with_suffix(".log.1").exists(), "滚动到 .1")
    rec(lp.with_suffix(".log.1").stat().st_size == 3 * 1024 * 1024,
        ".1 里是滚走的那份，没丢内容")
    rec(RuntimeManager.rotate_log(lp, max_mb=1) == 0, "未超限时不动")
    lp.write_bytes(b"y" * (2 * 1024 * 1024))
    RuntimeManager.rotate_log(lp, max_mb=1)
    rec(lp.with_suffix(".log.1").stat().st_size == 2 * 1024 * 1024,
        "第二次轮转覆盖旧 .1（只保留最近一份）")
    rec(RuntimeManager.rotate_log(lp, max_mb=1) == 0, "第二次轮转不重复触发")

    # 大得离谱时直接截断（改名只是换个名字继续占磁盘）
    big = TMP / "huge.log"
    big.write_bytes(b"z" * (16 * 1024 * 1024))
    freed3 = RuntimeManager.rotate_log(big, max_mb=1)
    rec(freed3 == 16 * 1024 * 1024, "超大日志返回释放字节数")
    rec(big.exists() and big.stat().st_size == 0, "超大日志就地截断（真释放空间）")
    rec(not big.with_suffix(".log.1").exists(), "超大日志不留下 .1 副本")
except Exception as e:  # noqa: BLE001
    rec(False, f"日志轮转 异常：{e!r}")


# ================================================================ 6
print()
print("=" * 60)
print("4. tag_source：标签到底是从哪个 gguf 建的")
print("=" * 60)
try:
    app = TMP / "app"
    (app / "runtime").mkdir(parents=True, exist_ok=True)
    (app / "models").mkdir(parents=True, exist_ok=True)
    (app / "runtime" / "ollama.exe").write_bytes(b"\x00")
    rm = RuntimeManager(app)
    rec(rm.tag_source() == "", "没有清单时返回空串")

    mp = rm.manifest_path()
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps({
        "layers": [
            {"mediaType": "config", "digest": "sha256:aa", "size": 1},
            {"mediaType": "model", "digest": "sha256:bb", "size": 9,
             "from": "Qwen2.5-7B-Instruct-Q4_K_M.gguf"},
        ]}), encoding="utf-8")
    rec(rm.tag_source() == "Qwen2.5-7B-Instruct-Q4_K_M.gguf",
        "从清单读到真实来源")

    mp.write_text(json.dumps({
        "layers": [{"mediaType": "model", "digest": "sha256:cc",
                    "from": "D:/x/y/Hy-MT2-1.8B-Q4_K_M.gguf"}]}),
        encoding="utf-8")
    rec(rm.tag_source() == "Hy-MT2-1.8B-Q4_K_M.gguf", "路径形式只取文件名")

    mp.write_text("{ 坏掉的 json", encoding="utf-8")
    rec(rm.tag_source() == "", "清单损坏时返回空串（不抛错）")
    rec(rm.loaded_info() == [], "引擎未运行时 loaded_info 返回空表（不抛错）")
except Exception as e:  # noqa: BLE001
    rec(False, f"tag_source 异常：{e!r}")


# ================================================================ 7
print()
print("=" * 60)
print("5. 端到端：同一个游戏跑两遍")
print("=" * 60)


class _StubTranslator:
    """离线桩：把每条原文翻成 "译<原文>"，直接写入 translated。

    每次 translate_batch 的实际产出记进 per_call，用来验证「只重翻缺的那部分」。
    """

    calls = 0
    per_call: list = []

    def __init__(self, model="", host="", options=None):
        pass

    def health(self):
        return True

    def list_models(self):
        return ["gametl-model"]

    def translate_batch(self, units, glossary=None, target_lang="",
                        progress=None, save_every=10, on_save=None,
                        skip_translated=True, options=None,
                        cancel_check=None):
        done = 0
        batch = []
        for i, u in enumerate(units, 1):
            if skip_translated and u.translated:
                continue
            u.translated = "译" + u.original
            # 每个偶数条目标成「兜底保留」，验证 uv 一路走到工程文件
            uv = (done % 2 == 1)
            if uv:
                u.extra["unverified"] = True
            else:
                u.extra.pop("unverified", None)
            batch.append((u.uid, u.translated, 1 if uv else 0))
            done += 1
            if progress:
                progress(i, len(units), "stub")
            if on_save and len(batch) >= save_every:
                on_save(batch)
                batch = []
        if on_save and batch:
            on_save(batch)
        type(self).calls += 1
        type(self).per_call.append(done)
        return done

    def stats_line(self):
        return "桩翻译器"


_real = auto_mod.OllamaTranslator
auto_mod.OllamaTranslator = _StubTranslator
try:
    src = ROOT / "gametl" / "examples" / "mock_rpgmaker_example"
    game = TMP / "game"
    shutil.copytree(src, game)
    out = TMP / "out"
    work = TMP / "work"

    cfg = AutoConfig(game_dir=game, out_dir=out, work_root=work,
                     model="gametl-model", host="http://127.0.0.1:1",
                     glossary_path=None)
    logs: list = []
    r1 = AutoPipeline(cfg, on_log=logs.append).run()
    rec(r1.success, f"第一遍成功（{r1.error}）")
    rec(_StubTranslator.calls == 1, "第一遍调用了翻译器")
    rec(r1.total_units > 0 and r1.translated_units == r1.total_units,
        f"第一遍翻完全部 {r1.total_units} 条")
    rec(r1.finished and not r1.already_done, "第一遍标记完成、不是「已完成」进入")
    rec(r1.unverified > 0, f"兜底条目被统计（{r1.unverified} 条）")

    pj = work / "project.json"
    rec(pj.exists(), "工程文件写在 work_root 上")
    meta = Project.read_meta(pj)
    rec(bool(meta.get("finished_at")), "第一遍写下了完成时间")
    rec(meta.get("unverified") == r1.unverified, "完成标记里的兜底条数一致")
    rec(not (out / "project.json").exists(), "旧位置不再放工程文件")
    wb1 = work / "_work" / "translated"
    rec(wb1.is_dir() and any(wb1.rglob("*")), "回填产物已生成")

    # ---- 第二遍：必须跳过翻译、跳过回填，工程文件不再被重写 ----
    mtime1 = pj.stat().st_mtime_ns
    size1 = pj.stat().st_size
    logs2: list = []
    r2 = AutoPipeline(cfg, on_log=logs2.append).run()
    rec(r2.success, f"第二遍成功（{r2.error}）")
    rec(_StubTranslator.calls == 1, "第二遍没有再调用翻译器")
    rec(r2.already_done, "第二遍识别出「已经翻完」")
    rec(r2.translated_units == 0, "第二遍新译 0 条")
    rec(any("跳过翻译阶段" in m for m in logs2), "日志明确写「跳过翻译阶段」")
    rec(any("已全部翻完" in m for m in logs2), "日志明确写「已全部翻完」")
    rec(pj.stat().st_mtime_ns == mtime1 and pj.stat().st_size == size1,
        "第二遍没有重写工程文件（零大写入）")
    rec(any("跳过回填阶段" in m for m in logs2), "日志明确写「跳过回填阶段」")

    # ---- 第三遍：中途被打断（只翻了一半），必须续传 ----
    _StubTranslator.calls = 0
    _StubTranslator.per_call.clear()
    p3 = TMP / "game3"
    shutil.copytree(src, p3)
    out3, work3 = TMP / "out3", TMP / "work3"
    cfg3 = AutoConfig(game_dir=p3, out_dir=out3, work_root=work3,
                      model="gametl-model", host="http://127.0.0.1:1")
    AutoPipeline(cfg3, on_log=lambda m: None).run()
    # 人为清掉一半译文，模拟「上次没翻完」
    pr = Project.load(work3 / "project.json")
    _cleared = len(pr.units) // 2
    for u in pr.units[:_cleared]:
        u.translated = None
    pr.meta.pop("finished_at", None)
    pr.save(work3 / "project.json")
    logs4: list = []
    r4 = AutoPipeline(cfg3, on_log=logs4.append).run()
    rec(r4.success and not r4.already_done, "未完成的工程不会被当成已完成")
    rec(len(_StubTranslator.per_call) == 2, "未完成时又调了一次翻译器")
    # 断言按**实际清掉的条数**算，不依赖「总数是偶数、且切分点正好落在
    # 文件边界上」这种巧合 —— 提取范围一变（v1.6 多覆盖了
    # terms.messages / gameTitle 等），条数奇偶就会变，旧写法会假失败。
    _expect_new = _cleared
    _expect_reuse = r4.total_units - _cleared
    rec(_StubTranslator.per_call[-1] == _expect_new,
        f"只重翻缺的那 {_expect_new} 条（不是全量 {r4.total_units} 条）")
    rec(r4.reused_units == _expect_reuse,
        f"复用已译的 {_expect_reuse} 条（实际 {r4.reused_units}）")
    rec(r4.finished, "续传后达到 100% 并写下完成标记")
finally:
    auto_mod.OllamaTranslator = _real


# ================================================================ 收尾
print()
print("=" * 60)
print(f"结果：{_ok} 通过 / {_bad} 失败")
print("=" * 60)
try:
    shutil.rmtree(TMP, ignore_errors=True)
except OSError:
    pass
sys.exit(1 if _bad else 0)
