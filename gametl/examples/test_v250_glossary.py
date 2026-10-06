# -*- coding: utf-8 -*-
"""v2.5.0 回归：自动术语抽取（统计召回 + LLM 精筛）。

背景
----
``Glossary`` 的**注入**机制早就做好了（``relevant_for()`` 只取文本里真出现
的术语，避免提示词膨胀），但**表从哪来**一直是空白 —— 只能手写 JSON，
没人写就等于没有一致性保障。``glossary_extract`` 补的就是这一环。

本测试盯住三层：
1. 统计层 ``collect_candidates()`` —— 召回 + 噪声过滤 + 大小写归并
2. LLM 层 ``refine_with_llm()`` —— 解析、防模型乱造、容错
3. 串起来 ``build_glossary()`` / ``save_glossary()`` —— 落盘与回读

跑法::

    python gametl/examples/test_v250_glossary.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gametl.core.models import TextKind, TextUnit  # noqa: E402
from gametl.translators.glossary import Glossary  # noqa: E402
from gametl.translators.glossary_extract import (  # noqa: E402
    build_glossary, can_refine, collect_candidates, refine_with_llm,
    save_glossary)

PASS = 0
FAIL: list = []


def ck(ok: bool, msg: str) -> None:
    global PASS
    if ok:
        PASS += 1
        print(f"[OK] {msg}")
    else:
        FAIL.append(msg)
        print(f"[FAIL] {msg}")


def terms_of(cands) -> set:
    return {c[0] for c in cands}


# ============================================================ 1. 统计层

print("=" * 60)
print("1. 统计层：召回 / 过滤 / 归并")
print("=" * 60)

SAMPLE = [
    "Wren went to the Candy Corn shop.",
    "Then Wren met Everett at the Candy Corn shop.",
    "Everett said the Candy Corn was stale.",
    "I'm not sure about that, said Wren.",
    "It's not my problem, Everett replied.",
    "TeamMemberAction fired for Wren.",
    "DPadRight was pressed near the Candy Corn stall.",
]
cand = collect_candidates(SAMPLE, max_candidates=50)
names = terms_of(cand)

ck("Wren" in names, "召回人名 Wren（句中出现过）")
ck("Everett" in names, "召回人名 Everett")
ck("Candy Corn" in names, "召回多词术语 Candy Corn")
ck(not any(t.startswith("I'") for t in names),
   "过滤掉 I'm 这类缩写（撇号前是停用词）")
ck(not any(t.startswith("It'") for t in names), "过滤掉 It's 这类缩写")
ck("TeamMemberAction" not in names, "过滤掉驼峰代码标识 TeamMemberAction")
ck("DPadRight" not in names, "过滤掉驼峰宏名 DPadRight")
ck(all(isinstance(c, tuple) and len(c) == 3 for c in cand),
   "返回结构是 (候选, 次数, 分数)")
ck(all(cand[i][2] >= cand[i + 1][2] for i in range(len(cand) - 1)),
   "按分数降序排列")

# 只在句首出现的词不算专名（句首大写只是英文语法）
sentence_start = ["Apple is red.", "Apple is green.", "Apple is round."]
ck("Apple" not in terms_of(collect_candidates(sentence_start, min_count=2)),
   "只在句首出现的大写词不算专名（如句首的 Apple）")
mixed_case = ["I ate an Apple today.", "The Apple fell down."]
ck("Apple" in terms_of(collect_candidates(mixed_case, min_count=2)),
   "出现在句中时才算专名")

# 大小写归并
merge_src = ["Candy is here.", "I like Candy.", "Then Candy again.",
             "CANDY for all!", "CANDY is great.", "Give me CANDY."]
mc = collect_candidates(merge_src, max_candidates=50)
candy = [c for c in mc if c[0].lower() == "candy"]
ck(len(candy) == 1, f"Candy / CANDY 归并成一条（实际 {len(candy)} 条）")
ck(candy and candy[0][0] == "Candy",
   f"归并后代表形态取 Title case（实际 {candy[0][0] if candy else '—'}）")
ck(candy and candy[0][1] == 6, f"归并后计数求和 = 6（实际 {candy[0][1] if candy else 0}）")

# CJK 候选（日文源：含假名的词门槛更低）
jp = ["ギルドにようこそ。", "ギルドの受付です。",
      "ギルドへ行こう。", "冒険者ギルドはこちら。"]
jnames = terms_of(collect_candidates(jp, max_candidates=80))
ck("ギルド" in jnames, "CJK：召回合假名的日文词「ギルド」")

ck(collect_candidates([]) == [], "空输入返回空列表")
ck(collect_candidates(["", "   "]) == [], "空白输入返回空列表")


# ============================================================ 2. LLM 层

print()
print("=" * 60)
print("2. LLM 层：解析 / 防乱造 / 容错")
print("=" * 60)


class FakeGen:
    """假模型：只把指定的几个候选判为术语。"""
    prompt_style = "generic"

    def __init__(self, keep=("Wren", "Everett")):
        self.keep = set(keep)
        self.calls = 0
        self.prompts: list = []

    def generate(self, prompt, system="", format_json=False, **kw):
        self.calls += 1
        self.prompts.append(prompt)
        got = re.findall(r"^\s*\d+\.\s*(.+)$", prompt, re.M)
        return json.dumps({c: "译:" + c for c in got if c in self.keep},
                          ensure_ascii=False)


fg = FakeGen()
res = refine_with_llm(fg, cand, target_lang="简体中文", batch=10)
ck(res.get("Wren") == "译:Wren", "精筛保留了 Wren 并给出译名")
ck("Everett" in res, "精筛保留了 Everett")
ck(not any(k in ("Candy Corn", "TeamMemberAction") for k in res),
   "精筛丢掉了模型没选的候选")
ck(fg.calls >= 1, f"确实调用了模型（{fg.calls} 次）")
ck("术语" in fg.prompts[0], "提示词里含术语判定要求")


class LiarGen:
    """模型乱造：返回了根本不在候选里的键。"""
    prompt_style = "generic"

    def generate(self, *a, **kw):
        return json.dumps({"NotACandidate": "哈哈", "Wren": "蕾恩"},
                          ensure_ascii=False)


lr = refine_with_llm(LiarGen(), [("Wren", 3, 5.0), ("Everett", 2, 4.0)])
ck("NotACandidate" not in lr, "模型自造的词被丢弃（只接受候选表里的键）")
ck(lr.get("Wren") == "蕾恩", "候选表里真有的键被接受")


class BadGen:
    prompt_style = "generic"

    def generate(self, *a, **kw):
        return "这不是 JSON {{{"


ck(refine_with_llm(BadGen(), [("Wren", 2, 3.0)]) == {},
   "模型返回垃圾时不崩、结果为空")


class BoomGen:
    prompt_style = "generic"

    def generate(self, *a, **kw):
        raise RuntimeError("连接断了")


ck(refine_with_llm(BoomGen(), [("Wren", 2, 3.0)]) == {},
   "模型调用抛异常时不崩、结果为空")

ck(refine_with_llm(FakeGen(), []) == {}, "候选为空时直接返回空")


class HymtGen:
    prompt_style = "hymt"


ck(can_refine(FakeGen()) is True, "通用模型可用于精筛")
ck(can_refine(HymtGen()) is False, "翻译专用模型不宣称为可精筛")


# ============================================================ 3. 串起来

print()
print("=" * 60)
print("3. build_glossary / save_glossary")
print("=" * 60)


def mk_units(texts, kind=TextKind.DIALOGUE):
    return [TextUnit(uid=f"u{i}", source_file="a.txt", location={"i": i},
                     original=t, kind=kind)
            for i, t in enumerate(texts)]


units = mk_units(SAMPLE)
ck(build_glossary(units, None) == {}, "没有模型时返回空表（不生成假译名）")

gl = build_glossary(units, FakeGen(), target_lang="简体中文",
                    on_log=lambda _m: None)
ck(gl.get("Wren") == "译:Wren", "有模型时正常产出术语表")

# kind=NAME 的条目会被补进候选
name_units = mk_units(["Zzzq"]) + [
    TextUnit(uid="n1", source_file="b.txt", location={"i": 0},
             original="Quibble", kind=TextKind.NAME)]
seen: list = []


class RecordGen(FakeGen):
    def generate(self, prompt, **kw):
        seen.extend(re.findall(r"^\s*\d+\.\s*(.+)$", prompt, re.M))
        return json.dumps({}, ensure_ascii=False)


build_glossary(name_units, RecordGen(), on_log=lambda _m: None)
ck("Quibble" in seen, "kind=NAME 的条目被补进候选（引擎已知的名字）")

with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "glossary.json"
    save_glossary({"Wren": "蕾恩", "Everett": "埃弗雷特"}, p)
    ck(p.is_file(), "术语表写盘成功")
    raw = json.loads(p.read_text(encoding="utf-8"))
    ck(raw.get("Wren") == "蕾恩", "存盘内容是 {原文: 译名}")
    back = Glossary.load(p)
    ck(len(back) == 2, "Glossary 能读回术语表")
    ck(back.relevant_for("Wren is here.") == {"Wren": "蕾恩"},
       "读回后能按「文本中真出现」注入")
    ck(back.relevant_for("nobody") == {}, "没命中的术语不注入")

print()
print("=" * 60)
print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
if FAIL:
    for f in FAIL:
        print(f"  - {f}")
print("=" * 60)
raise SystemExit(1 if FAIL else 0)
