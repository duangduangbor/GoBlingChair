# -*- coding: utf-8 -*-
"""v2.5.0 回归：批量翻译的上下文注入（说话人 + 相邻句只读上文）。

背景
----
提取器一直在给每条文本打 ``context={"speaker": ...}``（KiriKiri / Ren'Py /
RPG Maker / Unity / Buddha 全都打了），但**批量模式**（默认）构造提示词时
只用了原文::

    for _i, text, _c in items:      # ← _c 拿到就扔掉了
        terms.update(glossary.relevant_for(text))

只有单条降级路径才写进 prompt。于是「说话人」在默认配置下一直是白提取的。
另外分块是按提取顺序切的，同一批基本是同一段对话的前后几句，却没声明
「这是连续对话」，上下文价值也浪费了一半。

本测试盯住：注入位置、开关行为、以及 hymt 专用模板不被污染。

跑法::

    python gametl/examples/test_v250_context.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gametl.core.models import TextUnit  # noqa: E402
from gametl.translators.glossary import Glossary  # noqa: E402
from gametl.translators.ollama_backend import (  # noqa: E402
    OllamaTranslator, TranslateOptions)

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


def _mk_units(n: int) -> list:
    return [
        TextUnit(uid=f"u{i:03d}", source_file="scene.yarn",
                 location={"line": i},
                 original=(f"This is dialogue line number {i}, "
                           "long enough to trigger batch mode."),
                 context={"speaker": "Alice" if i % 2 == 0 else "Bob"})
        for i in range(n)
    ]


def _run(n: int, overlap: int, inject: bool = True):
    """跑一次 translate_batch（HTTP 被替换掉），返回 (每个请求的 prompt, units)。"""
    caps: list = []

    def fake_post(self, payload):
        caps.append(payload["prompt"])
        m = re.search(r"输入：\n(\{.*?\})\n", payload["prompt"], re.S)
        obj = json.loads(m.group(1))
        return {"response": json.dumps(
            {k: "中文译文第" + k + "句" for k in obj}, ensure_ascii=False)}

    t = OllamaTranslator(model="fake", options=TranslateOptions(
        concurrency=1, batch_size=8, use_batch=True, batch_min_len=0,
        verify=False, inject_context=inject, context_overlap=overlap))
    t._post = fake_post.__get__(t)          # type: ignore[assignment]
    units = _mk_units(n)
    t.translate_batch(units, glossary=Glossary(), target_lang="简体中文")
    return caps, units


print("=" * 60)
print("1. 纯构造层：_build_batch_prompt 的注入内容")
print("=" * 60)

G = Glossary({"guild": "公会"})
ITEMS = [
    (1, "Hello, is this the guild?", {"speaker": "Alice"}),
    (2, "It is. What do you need?", {"speaker": "Bob"}),
    (3, "I want to register.", {}),
]
PRIOR = ["Who goes there?", "A traveler, from the north."]

t_on = OllamaTranslator(model="fake", options=TranslateOptions(
    inject_context=True, context_overlap=2))
p_on = t_on._build_batch_prompt(ITEMS, G, "简体中文", prior=PRIOR)
ck("1=Alice" in p_on, "开：说话人写成「编号=名字」对照行")
ck("2=Bob" in p_on, "开：多个说话人都在对照里")
ck("同一段连续对话" in p_on, "开：声明这是同一段连续对话")
ck("上文（前 2 条" in p_on, "开：带只读上文标题（前 2 条）")
ck("A traveler, from the north." in p_on, "开：上文内容确实进去了")
ck("guild" in p_on, "开：术语表照旧注入（relevant_for 命中）")
ck('"1"' in p_on and '"3"' in p_on, "开：待翻译 JSON 未被破坏")

t_off = OllamaTranslator(model="fake", options=TranslateOptions(
    inject_context=False))
p_off = t_off._build_batch_prompt(ITEMS, G, "简体中文", prior=PRIOR)
ck("1=Alice" not in p_off, "关：不注入说话人")
ck("A traveler" not in p_off, "关：不注入上文")
ck("guild" in p_off, "关：术语表仍然注入（与上下文无关）")

t_hy = OllamaTranslator(model="hy-mt-x", options=TranslateOptions(
    prompt_style="hymt"))
p_hy = t_hy._build_batch_prompt(ITEMS, G, "简体中文", prior=PRIOR)
ck("1=Alice" not in p_hy, "hymt 专用模板：不注入说话人结构")
ck("A traveler" not in p_hy, "hymt 专用模板：不注入上文")
ck("guild" in p_hy, "hymt 专用模板：术语表仍以简单形式注入")

# 没有说话人时不该凭空造出对照行
p_nospk = t_on._build_batch_prompt(
    [(1, "No speaker here.", {}), (2, "Nor here.", {})], G, "简体中文")
ck("说话人" not in p_nospk, "没有说话人时不产生空的对照行")


print()
print("=" * 60)
print("2. 流水线层：translate_batch 的分块与上文传递")
print("=" * 60)

caps, units = _run(20, overlap=2)
ck(len(caps) == 3, f"20 条 / 每批 8 → 3 批（实际 {len(caps)}）")
ck("上文（前 " not in caps[0], "第 1 批没有上文（前面确实没东西）")
n_ctx = sum(1 for p in caps if "上文（前 " in p)
ck(n_ctx == 2, f"第 2、3 批带上文（实际 {n_ctx} 批）")
n_spk = sum(1 for p in caps if "1=Alice" in p or "1=Bob" in p)
ck(n_spk == 3, f"每批都带说话人对照（实际 {n_spk} 批）")

m = re.search(r"上文（前 \d+ 条.*?：\n(.*?)\n\n输入：", caps[1], re.S)
prev = m.group(1) if m else ""
ck("line number 6" in prev and "line number 7" in prev,
   "第 2 批的上文 = 紧挨着的前 2 条原文（第 6、7 条）")
ck("line number 8" not in prev, "上文不含本批自己的条目")

missing = [u.uid for u in units if not u.translated]
ck(not missing, f"全部条目都拿到了译文（缺 {len(missing)} 条）")
ck(all(u.translated.startswith("中文译文") for u in units),
   "译文按编号正确分发（没有串位）")

caps0, _ = _run(20, overlap=0)
ck(all("上文（前 " not in p for p in caps0),
   "context_overlap=0 → 一批都不带上文")
ck(len(caps0) == 3, "关闭上文不影响分块本身")

caps_off, _ = _run(20, overlap=2, inject=False)
ck(all("上文（前 " not in p and "1=Alice" not in p for p in caps_off),
   "inject_context=False → 说话人与上文都不注入")
ck(len(caps_off) == 3, "关闭注入不影响分块与翻译")

# 单条批（batch_size=1）不该给 translate_one 传 prior（它没有该参数）
t1 = OllamaTranslator(model="fake", options=TranslateOptions(
    concurrency=1, batch_size=1, use_batch=True, batch_min_len=0,
    verify=False, inject_context=True, context_overlap=2))
calls: list = []


def fake_post1(self, payload):
    calls.append(payload)
    m = re.search(r"原文：\n(.*?)\n", payload["prompt"], re.S)
    return {"response": "单条译文" + str(len(calls))}


t1._post = fake_post1.__get__(t1)           # type: ignore[assignment]
u1 = _mk_units(3)
t1.translate_batch(u1, glossary=Glossary(), target_lang="简体中文")
ck(len(calls) == 3, f"单条模式逐条请求（实际 {len(calls)} 次）")
ck(all("上文" not in str(c.get("prompt", "")) for c in calls),
   "单条模式不带「上文」结构（translate_one 无该参数）")

print()
print("=" * 60)
print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
if FAIL:
    for f in FAIL:
        print(f"  - {f}")
print("=" * 60)
raise SystemExit(1 if FAIL else 0)
