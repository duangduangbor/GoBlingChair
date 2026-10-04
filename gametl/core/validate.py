# -*- coding: utf-8 -*-
"""两层文本校验。

分层动机：机械性错误和语义问题是两回事，解法也不同。

第一层（即时硬校验）—— 翻译过程中每条返回后立刻跑，不花额外算力：
    译文为空、占位符丢失、日文残留、模型发散（长度暴涨）、清理残留前缀。
    不通过就**当场重发**，不需要用户介入，也永远不会污染最终结果。

第二层（全量规则校对）—— 用户点「校对」按钮后扫全部译文：
    漏译、术语违规、长度异常、不同原文译出同一结果（模型犯懒）等。
    只做判断不做修改，输出 Issue 列表，交给用户决定要不要「一键重翻」。

第三层（模型抽检）在 translators/quality.py，是选做项，成本较高。
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional


# ---------------------------------------------------------------- 字符集判定

# 假名：平假名 + 片假名（排除浊音符号、中点、长音符等中日共用/无意义字符）
_KANA = re.compile(r"[\u3041-\u3096\u30a1-\u30fa\u30fd\u30fe]")
_CJK = re.compile(r"[\u4e00-\u9fff]")
# 繁体中文专用字（台港常用，但简体字库也覆盖不到的个别字），用于区分简/繁
_TRAD_ONLY = re.compile(r"[\u4e00-\u9fff]")  # 占位，见 _detect_script 的分支
_LATIN = re.compile(r"[A-Za-z]")
_HANGUL = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]")

# 占位符（core/protect.py 生成的全角括号形式）
_TOKEN = re.compile(r"\u3010(\d+)\u3011")

# 模型常见的"没清理干净"的输出前缀
_BAD_PREFIXES = ("译文：", "译文:", "翻译：", "翻译:", "中文：", "中文:",
                 "Answer:", "Translation:", "输出：")


def has_kana(text: str) -> bool:
    """文本里是否含日文假名（判断「还是日文」的主信号）。

    汉字中日共用，不能作为判据；平/片假名才是可靠的日文标志。
    """
    return bool(_KANA.search(text or ""))


def is_mostly_latin(text: str) -> bool:
    """是否以拉丁字母为主（用于识别没被翻译的英文串）。"""
    s = text or ""
    if not s:
        return False
    latin = len(_LATIN.findall(s))
    return latin >= 4 and latin / max(len(s), 1) > 0.5


# ---------------------------------------------------------------- 目标语言判定
#
# 多语言翻译需要「已经翻成目标语言」的判断：原文若已经是目标语言，就没必要
# 再送进模型翻一遍。这里把「目标语言」和「字符集」对上号，供 should_skip
# （预过滤）和 check_immediate / audit_units（校验）共用，避免三处各写一套。

# 目标语言名称（或片段）→ 脚本类别。类别决定「已是目标语言」怎么判。
# key 全部小写匹配。
_LANG_SCRIPT = {
    # 简体中文 / 中文：出现汉字即算
    "简体中文": "hanzi",
    "中文": "hanzi",
    "简体": "hanzi",
    "chinese": "hanzi",
    "simplified": "hanzi",
    "zh": "hanzi",
    # 繁体中文：与简体共用汉字库，只能按「无假名 + 有汉字」判（见下）
    "繁体": "hanzi",
    "繁體": "hanzi",
    "traditional": "hanzi",
    "zh-tw": "hanzi",
    "zh-hant": "hanzi",
    # 日语：出现假名才算（汉字中日共用不能作判据）
    "日本語": "kana",
    "日语": "kana",
    "日文": "kana",
    "japanese": "kana",
    "ja": "kana",
    # 韩语：出现谚文才算
    "한국어": "hangul",
    "韩语": "hangul",
    "韓語": "hangul",
    "韩文": "hangul",
    "korean": "hangul",
    "ko": "hangul",
    # 英语 / 以拉丁字母为主的语言：出现字母即算
    "english": "latin",
    "英语": "latin",
    "英文": "latin",
    "en": "latin",
    "français": "latin",
    "francais": "latin",
    "french": "latin",
    "deutsch": "latin",
    "german": "latin",
    "español": "latin",
    "espanol": "latin",
    "spanish": "latin",
}


def target_script(target_lang: str) -> str:
    """把目标语言名归到脚本类别：hanzi / kana / hangul / latin / unknown。

    未知语言返回 "unknown"（调用方应保守处理：不跳过、照常翻译）。
    """
    low = (target_lang or "").strip().lower()
    if not low:
        return "hanzi"          # 缺省按简体中文处理（历史默认）
    for key, script in _LANG_SCRIPT.items():
        if key in low or low in key:
            return script
    return "unknown"


def already_in_target(text: str, target_lang: str) -> bool:
    """判断文本是否「已经是目标语言」——是的话无需再翻译。

    这是 should_skip 里「已是中文就跳过」的多语言泛化版：
    按目标语言脚本类别，检查文本是否已经主要由该脚本构成。

    保守原则：判不准就返回 False（宁可多翻，不可漏翻）。
    """
    s = (text or "").strip()
    if not s or len(s) <= 1:
        return True               # 空串/单字符照旧跳过（与 should_skip 一致）

    script = target_script(target_lang)

    if script == "hanzi":
        # 有汉字、且不含假名（假名说明还是日文）即算已是中文
        return bool(_CJK.search(s)) and not has_kana(s)
    if script == "kana":
        # 已是日语：出现假名即算（汉字+假名混合也算）
        return has_kana(s)
    if script == "hangul":
        return bool(_HANGUL.search(s))
    if script == "latin":
        # 以字母为主且不含 CJK 假名，即算已是英文等拉丁语言
        return bool(_LATIN.search(s)) and not has_kana(s) and not _CJK.search(s)
    return False                    # 未知语言：不跳过


# ---------------------------------------------------------------- 数据结构

class Level(str, Enum):
    BLOCK = "block"   # 必须处理（第一层用）
    WARN = "warn"     # 建议复核
    INFO = "info"     # 仅提示


# 问题代码 -> (等级, 中文名, 是否建议重翻)
ISSUE_META: dict[str, tuple[Level, str, bool]] = {
    "empty":         (Level.WARN,  "译文为空",        True),
    "placeholder":   (Level.WARN,  "占位符异常",      True),
    "untranslated":  (Level.WARN,  "疑似漏译",        True),
    "term_miss":     (Level.INFO,  "术语未命中",      True),
    "dup_target":    (Level.INFO,  "多条同译",        True),
    "too_short":     (Level.INFO,  "译文过短",        True),
    "too_long":      (Level.INFO,  "译文过长",        True),
    "prefix_residue": (Level.INFO, "残留前缀",        True),
    # identical 只在「原文含假名」时才会被报出来，所以它等价于「没翻译」，应当重翻
    "identical":     (Level.INFO,  "与原文相同",      True),
    # 第三层模型抽检判定出来的问题
    "model_flag":    (Level.WARN,  "模型判定有问题",  True),
}


@dataclass
class Issue:
    uid: str
    code: str
    message: str
    original: str = ""
    translated: str = ""
    source_file: str = ""

    @property
    def level(self) -> Level:
        return ISSUE_META.get(self.code, (Level.INFO, "", True))[0]

    @property
    def kind(self) -> str:
        return ISSUE_META.get(self.code, (Level.INFO, self.code, True))[1]

    @property
    def retranslatable(self) -> bool:
        return ISSUE_META.get(self.code, (Level.INFO, "", True))[2]


# ================================================================ 第一层

def _kana_should_be_checked(target_lang) -> bool:
    """假名残留检查是否适用：只有当目标语言**不是**日语时才查。

    参数既可以是目标语言名（如 "简体中文" / "日本語"），也可以是旧版的
    布尔值 source_lang_is_jp（True 表示原文是日文、目标非日文 → 要查；
    False 表示不查）。向后兼容。
    """
    if isinstance(target_lang, bool):
        return bool(target_lang)
    # 目标语言是日语 → 译文含假名是正常的，不查
    return target_script(target_lang) != "kana"


def check_immediate(original: str, translated: str,
                    glossary: Optional[dict] = None,
                    source_lang_is_jp: bool = True,
                    target_lang: Optional[str] = None) -> Optional[str]:
    """翻译过程中的即时硬校验。

    返回 `None` 表示通过；返回字符串表示失败原因（调用方应重发）。

    这一层必须**快且零误伤** —— 宁可放过，不可错杀，
    因为每次误判都会白白多跑一次推理。

    ``target_lang`` 若给出，则用它决定是否做「假名残留」检查
    （目标语言是日语时跳过该检查）；否则沿用 source_lang_is_jp。
    """
    if not translated or not translated.strip():
        return "译文为空"

    src = (original or "").strip()
    dst = translated.strip()

    # 1) 模型发散：短文译出超长文本（多半是开始自说自话）
    #    注意这里对超短原文（1-2 字）同样要拦 —— 「はい」译出 400 字绝对是发散
    if src and len(dst) > max(len(src) * 6, 120):
        return f"译文异常冗长（{len(dst)} 字 vs 原文 {len(src)} 字）"

    # 2) 占位符：原文里出现的每一个编号，译文必须原样保留且只出现一次
    src_tokens = _TOKEN.findall(src)
    if src_tokens:
        dst_tokens = _TOKEN.findall(dst)
        if sorted(src_tokens) != sorted(dst_tokens):
            missing = set(src_tokens) - set(dst_tokens)
            extra = set(dst_tokens) - set(src_tokens)
            detail = []
            if missing:
                detail.append("丢失 " + ",".join(sorted(missing)))
            if extra:
                detail.append("多出 " + ",".join(sorted(extra)))
            return "占位符异常（" + "；".join(detail) + "）"

    # 3) 漏译：原文是日文，译文里假名占比仍然很高
    #    （目标语言是日语时跳过 —— 翻成日语含假名是正常的）
    check_kana = _kana_should_be_checked(
        target_lang if target_lang is not None else source_lang_is_jp)
    if check_kana and has_kana(src):
        kana_n = len(_KANA.findall(dst))
        if kana_n and kana_n / max(len(dst), 1) > 0.5:
            return "疑似未翻译（译文仍为日文）"

    # 4) 残留前缀（正常应该在 _clean 里去掉，这里是兜底）
    for p in _BAD_PREFIXES:
        if dst.startswith(p):
            return f"残留前缀「{p}」"

    return None


# ================================================================ 第二层

@dataclass
class AuditReport:
    issues: list[Issue] = field(default_factory=list)
    total: int = 0

    def by_code(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for i in self.issues:
            out[i.code] += 1
        return dict(out)

    def fixable_uids(self) -> list[str]:
        """建议重翻的 uid（去重，保持顺序）。"""
        seen = set()
        out = []
        for i in self.issues:
            if i.retranslatable and i.uid not in seen:
                seen.add(i.uid)
                out.append(i.uid)
        return out

    def summary(self) -> str:
        if not self.issues:
            return f"校对完成：{self.total} 条全部通过，未发现问题。"
        parts = [f"{k} × {v}" for k, v in sorted(self.by_code().items(),
                                                 key=lambda kv: -kv[1])]
        n_fix = len(self.fixable_uids())
        return (f"校对完成：检查 {self.total} 条，发现 {len(self.issues)} 处问题"
                f"（涉及 {n_fix} 条文本）— " + "，".join(parts))


def _protected_of(unit) -> str:
    """取单位的「保护后原文」；没有就用原原文。"""
    extra = getattr(unit, "extra", None) or {}
    return extra.get("protected_form") or unit.original


def audit_units(units: Iterable, glossary: Optional[dict] = None,
                source_lang_is_jp: bool = True,
                target_lang: Optional[str] = None) -> AuditReport:
    """全量规则校对：扫一遍所有已翻译条目，输出问题清单。

    纯本地计算，不调用模型，所以是秒级的。
    """
    glossary = glossary or {}
    units = list(units)
    rep = AuditReport(total=len(units))
    check_kana = _kana_should_be_checked(
        target_lang if target_lang is not None else source_lang_is_jp)

    # 先做一次全局统计，供「多条同译」判定
    target_groups: dict[str, list] = defaultdict(list)
    for u in units:
        if u.translated:
            target_groups[u.translated.strip()].append(u)

    for u in units:
        dst = (u.translated or "").strip()
        src = _protected_of(u)

        if not dst:
            rep.issues.append(Issue(u.uid, "empty", "未产出译文",
                                    u.original, "", u.source_file))
            continue

        # --- 与原文完全相同（多是既定名词/符号，通常无需处理）---
        # 与「漏译」互斥：既然逐字相同，就没必要再报一次假名残留
        identical = (dst == src)
        if identical and has_kana(src):
            rep.issues.append(Issue(u.uid, "identical", "译文与原文完全相同",
                                    u.original, dst, u.source_file))
        elif check_kana and has_kana(src):
            # --- 漏译：原文是日文，译文还有大量假名 ---
            kana_n = len(_KANA.findall(dst))
            if kana_n / max(len(dst), 1) > 0.3:
                rep.issues.append(Issue(
                    u.uid, "untranslated",
                    f"译文仍含大量日文假名（{kana_n} 个）",
                    u.original, dst, u.source_file))

        # --- 占位符 ---
        src_tokens = sorted(_TOKEN.findall(src))
        if src_tokens:
            dst_tokens = sorted(_TOKEN.findall(dst))
            if src_tokens != dst_tokens:
                rep.issues.append(Issue(
                    u.uid, "placeholder",
                    f"占位符不匹配：原文 {src_tokens} vs 译文 {dst_tokens}",
                    u.original, dst, u.source_file))

        # --- 术语违规 ---
        # 注意：术语可能有同义译法，所以只报 INFO，不强制重翻
        for term, want in glossary.items():
            if term and term in src and want and want not in dst:
                rep.issues.append(Issue(
                    u.uid, "term_miss",
                    f"术语「{term}」应译作「{want}」，但译文中未出现",
                    u.original, dst, u.source_file))
                break   # 每条只报一次，避免刷屏

        # --- 长度异常 ---
        if len(src) >= 6:
            ratio = len(dst) / len(src)
            if ratio < 0.25:
                rep.issues.append(Issue(
                    u.uid, "too_short",
                    f"译文过短（原文 {len(src)} 字 → 译文 {len(dst)} 字）",
                    u.original, dst, u.source_file))
            elif ratio > 4:
                rep.issues.append(Issue(
                    u.uid, "too_long",
                    f"译文过长（原文 {len(src)} 字 → 译文 {len(dst)} 字）",
                    u.original, dst, u.source_file))

        # --- 残留前缀 ---
        for p in _BAD_PREFIXES:
            if dst.startswith(p):
                rep.issues.append(Issue(
                    u.uid, "prefix_residue", f"残留前缀「{p}」",
                    u.original, dst, u.source_file))
                break

    # --- 多条同译：不同原文译出同一结果（通常是模型偷懒）---
    # 例外：超短语气词（はい / うん / ええ → 嗯）在日语里本来就一对多，
    # 硬报出来只会刷屏。所以当「译文和所有原文都很短」时放过。
    for dst, group in target_groups.items():
        if len(group) < 2 or len(dst) < 2:
            continue
        origins = {_protected_of(u) for u in group}
        if len(origins) < 2:
            continue
        if len(dst) <= 3 and all(len(o) <= 3 for o in origins):
            continue
        for u in group:
            rep.issues.append(Issue(
                u.uid, "dup_target",
                f"有 {len(group)} 条不同原文都译成了「{dst[:20]}」",
                u.original, dst, u.source_file))

    return rep


def filter_fixable(report: AuditReport, units: Iterable) -> list:
    """按校对报告挑出建议重翻的 TextUnit 列表。"""
    want = set(report.fixable_uids())
    return [u for u in units if u.uid in want]


def reset_translations(units: list) -> int:
    """把给定条目的译文清空（用于重翻）。返回清空的条数。"""
    n = 0
    for u in units:
        if u.translated is not None:
            u.translated = None
            n += 1
    return n
