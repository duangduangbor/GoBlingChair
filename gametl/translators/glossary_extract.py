# -*- coding: utf-8 -*-
"""自动术语抽取：从游戏文本里挖出需要「统一译法」的专有名词。

为什么需要它
------------
``Glossary`` 的**注入**机制早就做好了（``relevant_for()`` 只取文本里真出现
的术语，避免提示词膨胀），但**表从哪来**一直是空白 —— 只能手写 JSON，
没人写就等于没有一致性保障。本模块补的正是这一环：把表**自动造出来**。

分工（社区已收敛的共识）
------------------------
    统计层 ``collect_candidates()`` —— 负责**召回**（宁可多给，别漏）
    LLM 层  ``refine_with_llm()``   —— 负责**精度**（判真伪 + 定译名）

两者**解耦**：没有模型时只跑统计层也能出一份候选清单，人工过一遍就能用。

信号来源
--------
1. **拉丁专有名词**：首字母大写的词组，且**在句中位置出现过** ——
   句首大写只是英文语法，不算专名证据（这条判据砍掉了绝大多数噪声）。
2. **CJK 高频 n-gram**：2~4 字的连续串；含假名的阈值更低（明确是日文词）。
3. 提取器打了 ``kind=NAME`` 的条目 —— 引擎层面已知的名字，天然是好候选。

诚实边界
--------
n-gram 统计对**英文**（有空格 + 大写规则）最有效；日文无空格、中文需分词，
这一路只能粗筛，最终仍靠 LLM 判断。别指望统计层直接给出可用的术语表。
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Callable, Iterable, Optional

# ---------------------------------------------------------------- 模式

# 拉丁专名：首字母大写，可含 ' - 和最多两个后续大写词（"New York" / "O'Brien"）
_LATIN_PROPER = re.compile(r"[A-Z][A-Za-z'\-]*(?:\s+[A-Z][A-Za-z'\-]*){0,2}")
_CJK_RUN = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]+")
_KANA = re.compile(r"[\u3040-\u30ff]")
_NUM = re.compile(r"^\d+$")
# 英文缩写：I'm / It's / That's / Don't …（撇号 + 短后缀）
_CONTRACTION = re.compile(r"^([A-Za-z]+)['\u2019]([A-Za-z]+)$")
# 驼峰特征：小写字母紧跟大写（DPadRight / TeamMemberAction / SomeAsset）
_CAMEL_HINT = re.compile(r"[a-z][A-Z]")

# 句首大写但**不是**专名的常见词。主力过滤其实靠「句中位置」判据，
# 这张表只是兜底 —— 有些词也会出现在句中（"...told you" 里的小写不算）。
_STOP_LATIN = frozenset("""
the a an and but or if i you he she it we they this that these those
what where when who why how yes no ok oh hey well now then so just not
do did does can could will would should shall may might must is are was
were be been being am have has had having get got go going come came let
me my your his her its our their there here never ever always please
thank thanks hello hi bye goodbye sir ma am mr mrs ms dr
one two three four five six seven eight nine ten hundred thousand
day night morning evening time way thing man woman boy girl people
new old good bad big small long short right left next last first
all none found yeah yep nope wow ouch ugh huh ah eh um
anything something everything nothing someone anyone everyone nobody
maybe really very much many more most some any every each other another
such only also too still yet again once twice mine yours ours theirs
""".split())

# 多词候选的**前导语法词**：这些词不可能是一个专名短语的开头，
# 匹配到就剥掉 —— "Then Wren" → "Wren"。
# 刻意**不含** new / old / good / east 这类可作地名首词的形容词
# （New York / Old Town / East Side 都会被剥坏）。
_LEAD_STRIP = frozenset("""
the a an and but or if then so well now oh hey yes no ok just also
still yet please thanks hello hi bye
""".split())


def _form_rank(t: str) -> int:
    """同一术语有多种大小写时，哪个形态更该当代表。"""
    if len(t) > 1 and t[0].isupper() and not t[1:].isupper():
        return 3          # Title case 最优（Orel / Candy Corn）
    if t.isupper():
        return 2          # 全大写（BATTLE STAMPS / BUTTON）
    return 1              # 全小写 / 其他


def collect_candidates(texts: Iterable[str], *,
                       max_candidates: int = 300,
                       min_count: int = 2,
                       include_cjk: bool = True) -> list:
    """从文本集合里召回术语候选（纯统计，不碰模型）。

    Args:
        texts: 原文集合（可以带重复）
        max_candidates: 最多返回多少条
        min_count: 最少出现次数（拉丁词；CJK 纯汉字会用更高的下限）
        include_cjk: 是否处理中日韩文本（英文源可关掉省时间）

    Returns:
        ``[(候选, 出现次数, 分数)]``，按分数降序。
    """
    latin_all: Counter = Counter()
    latin_mid: Counter = Counter()      # 出现在「句中」的次数
    cjk_all: Counter = Counter()
    cjk_kana: Counter = Counter()

    for raw in texts:
        if not raw:
            continue
        t = str(raw).strip()
        if not t or len(t) > 400:
            continue

        for m in _LATIN_PROPER.finditer(t):
            term = m.group(0).strip()
            # 剥掉前导语法词：正则会把 "Then Wren" 整段吞下，
            # 但真正的专名是 "Wren" —— 不剥会让 Wren 的计数漏掉一大半。
            parts = term.split()
            while len(parts) > 1 and parts[0].lower() in _LEAD_STRIP:
                parts.pop(0)
            term = " ".join(parts)
            if len(term) < 3:
                continue
            latin_all[term] += 1
            head = t[:m.start()].rstrip()
            # 前面有内容、且上一个字符不是句末标点 → 视为句中位置
            if head and head[-1] not in ".!?\"'":
                latin_mid[term] += 1

        if include_cjk:
            for m in _CJK_RUN.finditer(t):
                run = m.group(0)
                for n in (2, 3, 4):
                    if len(run) < n:
                        continue
                    for i in range(len(run) - n + 1):
                        g = run[i:i + n]
                        cjk_all[g] += 1
                        if _KANA.search(g):
                            cjk_kana[g] += 1

    scored: dict = {}

    def is_noise(term: str) -> bool:
        if term.lower() in _STOP_LATIN or _NUM.match(term):
            return True
        m = _CONTRACTION.match(term)
        if m and m.group(1).lower() in _STOP_LATIN:
            return True                       # I'm / It's / That's / Let's …
        # 驼峰代码标识（TeamMemberAction / DPadRight）。加两个条件避免误伤
        # 正常的人名地名：需要有驼峰特征，且够长或有 ≥3 个大写字母。
        if (" " not in term and "'" not in term and "-" not in term
                and _CAMEL_HINT.search(term)
                and (len(term) > 10
                     or sum(1 for c in term if c.isupper()) >= 3)):
            return True
        return False

    def bump(term: str, score: float, count: int) -> None:
        old = scored.get(term)
        if old is None or score > old[2]:
            scored[term] = (term, count, score)

    for term, n in latin_all.items():
        if n < min_count or len(term) < 3:
            continue
        if is_noise(term):
            continue
        mid = latin_mid.get(term, 0)
        if mid <= 0:
            continue                       # 只在句首出现 → 语法大写，不是专名
        bump(term, n + mid * 3 + 0.3 * min(len(term), 20), n)

    for term, n in cjk_all.items():
        # 含假名 = 明确是日文词，门槛低；纯汉字串噪声大，门槛翻倍
        if cjk_kana.get(term):
            thr = min_count
        else:
            thr = max(min_count * 2, 4)
        if n < thr:
            continue
        bump(term, n + (2 if cjk_kana.get(term) else 0) + 0.3 * len(term), n)

    # 大小写归并：Candy / CANDY 归成一条（同一术语的两种写法）。
    # 代表形态取「最像专名」的那个，计数求和。
    by_key: dict = {}
    for term, cnt, sc in scored.values():
        key = term.lower()
        cur = by_key.get(key)
        if cur is None:
            by_key[key] = [term, cnt, sc]
            continue
        cur[1] += cnt
        if _form_rank(term) > _form_rank(cur[0]):
            cur[0] = term
        cur[2] = max(cur[2], sc)

    merged = sorted(by_key.values(), key=lambda kv: -kv[2])[:max_candidates]
    return [(t, int(c), float(s)) for t, c, s in merged]


# ---------------------------------------------------------------- LLM 精筛

_REFINE_PROMPT = """下面是从一个游戏的全部文本里自动挑出的候选词。
请从里面挑出**需要统一译法的专有名词 / 术语**，并给出{lang}译名。

判定标准（满足其一即可）：
- 人名、地名、组织 / 阵营名、种族名
- 技能 / 道具 / 装备 / 怪物名
- 界面固定词（菜单项、系统提示里的专有叫法）
- 世界观专有概念

不要收录：
- 普通名词、动词、形容词、代词、虚词、疑问词、感叹词
- 你觉得不能确定含义的 —— **宁可不收，不要猜**

输出要求：
1. 只输出 JSON 对象，键是候选原文，值是{lang}译名。
2. 不是术语的直接不要出现在结果里。
3. 译名要简短自然，符合该词在游戏里的常见译法。

候选词：
{items}
"""


def refine_with_llm(translator, candidates, target_lang: str = "简体中文",
                    batch: int = 40,
                    on_progress: Optional[Callable[[int, int, str], None]] = None,
                    cancel_check: Optional[Callable[[], bool]] = None) -> dict:
    """把统计候选交给模型：判真伪 + 定译名。

    Args:
        translator: 需要提供 ``generate(prompt, format_json=...)``
                    （``OllamaTranslator`` 已具备）
        candidates: ``collect_candidates()`` 的返回值，或纯字符串列表

    Returns:
        ``{原文: 译名}``。模型漏答/答错的一律丢弃 —— 宁可少，不可错。
    """
    terms: list = []
    for c in candidates or []:
        if isinstance(c, (tuple, list)) and c:
            terms.append(str(c[0]))
        elif c:
            terms.append(str(c))
    if not terms:
        return {}

    from .ollama_backend import OllamaTranslator     # 复用它的 JSON 容错解析

    out: dict = {}
    total = len(terms)
    done = 0
    for i in range(0, total, batch):
        if cancel_check and cancel_check():
            break
        chunk = terms[i:i + batch]
        items = "\n".join(f"{j}. {t}" for j, t in enumerate(chunk, 1))
        prompt = _REFINE_PROMPT.format(lang=target_lang, items=items)
        try:
            raw = translator.generate(prompt, format_json=True,
                                      num_predict=2048)
        except Exception:  # noqa: BLE001
            raw = ""
        got = OllamaTranslator._parse_json_map(raw) or {}
        for k, v in got.items():
            k2, v2 = str(k).strip(), str(v).strip()
            # 只接受**确实在候选里**的键 —— 模型偶尔会自己造词
            if k2 and v2 and k2 in chunk:
                out[k2] = v2
        done += len(chunk)
        if on_progress:
            on_progress(done, total, f"精筛 {done}/{total}")

    return out


def can_refine(translator) -> bool:
    """翻译专用模型（Hy-MT 等）只会翻译，不适合做术语判定。"""
    return getattr(translator, "prompt_style", "generic") != "hymt"


def build_glossary(units, translator, target_lang: str = "简体中文", *,
                   max_candidates: int = 300,
                   min_count: int = 2,
                   on_progress: Optional[Callable[[int, int, str], None]] = None,
                   on_log: Optional[Callable[[str], None]] = None,
                   cancel_check: Optional[Callable[[], bool]] = None) -> dict:
    """从工程条目生成术语表。返回 ``{原文: 译名}``。

    无模型（``translator is None``）时只跑统计层并返回 ``{}`` ——
    术语判定**必须**有模型，用空译名凑数只会把提示词带偏。
    """
    log = on_log or (lambda _m: None)
    texts = [getattr(u, "original", "") for u in units]
    if not texts:
        return {}

    cand = collect_candidates(texts, max_candidates=max_candidates,
                              min_count=min_count)
    log(f"统计层召回候选 {len(cand)} 条")

    # 提取器标了 kind=NAME 的条目直接补进去（它们天然是名字，权重拉满）
    known = {str(getattr(u, "original", "")).strip() for u in units
             if "name" in str(getattr(u, "kind", "")).lower()}
    known = {k for k in known if k and len(k) <= 40}
    merged: list = [c[0] for c in cand]
    for k in sorted(known):
        if k not in merged:
            merged.append(k)
    if known:
        log(f"其中引擎已标注的名字 {len(known)} 条")

    if translator is None:
        log("未提供模型：只给候选清单，不生成术语表")
        return {}

    if not can_refine(translator):
        log("[警告] 当前是翻译专用模型，术语判定可能不准；建议换通用模型")

    refined = refine_with_llm(
        translator,
        [(m, 0, 0.0) for m in merged],
        target_lang=target_lang, on_progress=on_progress,
        cancel_check=cancel_check)
    log(f"模型精筛后保留术语 {len(refined)} 条")
    return refined


def save_glossary(mapping: dict, path) -> None:
    """写成 ``Glossary`` 认识的 JSON。"""
    from .glossary import Glossary
    Glossary(dict(mapping)).save(Path(path))
