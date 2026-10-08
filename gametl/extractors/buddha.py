# -*- coding: utf-8 -*-
"""Buddha / Moai / Remonkeyed 引擎文本提取器（Double Fine 系游戏）。

代表作品：Costume Quest 1·2、Stacking、Brutal Legend、Headlander、The Cave、
Iron Brigade、Massive Chalice …

文本藏身之处
------------
游戏把资源装进 ``Win/Packs/*.~h`` + ``*.~p``（dfpf 容器，见
:mod:`gametl.core.dfpf`）。**对话与界面文本集中在 ``StringTable`` 资源里**，
格式是一段自描述的明文 DSL：

.. code-block:: text

    StringTable{LineCodeData={
        PWCH001LUCY=LineCodeData{Text=Fascinating...;VolumeDB=0;Character=Lucy;SoundCue=;};
        PWCH002EVER=LineCodeData{Text="More portals?! ...";VolumeDB=0;Character=Everett;SoundCue=;};
    };}

按语言分表（``cq2_enus`` / ``cq2_frfr`` / …），所以在英语表上做汉化，
就是把 ``Text=...`` 的值换成中文 —— 结构一个字节都不用动。

回填策略
--------
**不重新序列化整张表**，而是"原串 + 精确区间替换"：解析时记下每个 ``Text``
值在原文里的起止位置，回填时只把这几段换掉再拼回去。实测对 CQ2 的全部
英语表都能做到逐字节无损往返（见 ``test_buddha.py`` 的 round-trip 用例），
因此不可能因为格式微秒差异把游戏改坏。

写回包时优先走 ``DfpfPack.write`` 的"就地替换"（新数据不比原槽位大就只改
那一段），装不下才整包重建。
"""
from __future__ import annotations

import re
from pathlib import Path

from ..core.dfpf import COMP_XMEM, DfpfPack, DfpfPackError, find_packs
from ..core.models import Project, TextKind, TextUnit
from ..core.protect import PATTERNS, TOKEN_FMT, restore
from .base import BaseExtractor, wb_stats, write_back_file

# ------------------------------------------------------------------ DSL 常量

TABLE_HEAD = "StringTable{"
ENTRY_RE = re.compile(r"([A-Za-z0-9_]+)=LineCodeData\{")
VALUE_MARK = "Text="
CHAR_MARK = "Character="
SOUND_MARK = "SoundCue="

#: 不该翻译的键前缀（CMAP008TEXT 是"字体覆盖字符表"，逐字列出全部字形）
SKIP_KEY_PREFIXES = ("CMAP", "FONT")

#: 名字里带这些词 = 其它语言，跳过（只在英语表上做汉化）
NON_ENGLISH_TOKENS = (
    "french", "german", "italian", "spanish", "portuguese", "polish",
    "russian", "japanese", "korean", "chinese", "tchinese", "schinese",
    "leet",                      # 1337 火星文：是梗，翻了就毁了
    "dede", "frfr", "itit", "eses", "ptbr", "ruru", "jajp", "kokr",
    "zhcn", "zhtw",
)
#: 名字里带这些词 = 英语表，要做
ENGLISH_TOKENS = ("usenglish", "ukenglish", "english", "enus", "engb")

#: Buddha 专有的占位标记：``/BUTTON_DPadUp/`` ``/KEY_LEFT/`` 这类按键提示
DFS_MACRO_RE = re.compile(r"/[A-Z][A-Za-z0-9_]{1,30}/")


def _protect(text: str) -> tuple[str, list[str]]:
    """保护不该被翻译的片段（按键宏 + 通用模式：``{0}`` ``%s`` ``<tag>`` …）。"""
    frags: list[str] = []

    def _repl(m: "re.Match[str]") -> str:
        token = TOKEN_FMT.format(index=len(frags))
        frags.append(m.group(0))
        return token

    out = text
    for pattern in (DFS_MACRO_RE,) + tuple(p for _n, p in PATTERNS):
        out = pattern.sub(_repl, out)
    return out, frags


def _is_translatable(text: str) -> bool:
    s = text.strip()
    if not s:
        return False
    return any(c.isalpha() for c in s)


# ------------------------------------------------------------------ 值读写

def read_value(s: str, i: int) -> tuple[str, int]:
    """从 ``s[i]`` 读一个 DSL 值，返回 (原始片段, 结束下标)。

    带引号的值读到配对的（未被转义的）双引号；裸值读到下一个分号为止。
    """
    if i < len(s) and s[i] == '"':
        j = i + 1
        while j < len(s):
            if s[j] == "\\":
                j += 2
                continue
            if s[j] == '"':
                return s[i:j + 1], j + 1
            j += 1
        return s[i:], len(s)
    j = s.find(";", i)
    if j < 0:
        return s[i:], len(s)
    return s[i:j], j


#: 反斜杠转义的还原表
_UNESC = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "'": "'"}


def unescape(raw: str) -> str:
    """把 DSL 里的引号包裹与转义还原成真正的文本。"""
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    out: list[str] = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            out.append(_UNESC.get(nxt, "\\" + nxt))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def escape(text: str) -> str:
    """把文本转成可以放进 DSL 双引号里的形式。"""
    return (text.replace("\\", "\\\\").replace('"', '\\"')
                .replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t"))


#: StringTable 是**靠括号配平**的自描述 DSL：``{`` ``}`` ``[`` ``]`` 都是结构
#: 字符。可模型偶尔会把生成 JSON 时用的收尾符号（``}`` / ``]``）写进字符串值
#: 里 —— 实测 CQ2 的 5 张英语表里混进了 216 处。落在 ``Text`` 值内的结构符号
#: 会让整张表的配平错位，游戏解析时**启动即卡死或直接崩**（实测 0xC0000005）。
#: 这类污染不改变长度以外的任何可见内容，用"比原文多就删"就能安全清掉。
_BRACKETS = "{}[]"


def strip_stray_brackets(new: str, old: str) -> str:
    """删掉译文里**比原文凭空多出来**的结构符号。

    只在"原文没有、译文才有"时动手，所以原文里合法的 ``{0}`` ``[name]``
    之类一个都不会碰（它们的数量在两边相等）。多余符号优先从末尾删 ——
    实测污染几乎都出现在值尾；末尾删不干净再从后往前删。
    """
    for ch in _BRACKETS:
        extra = new.count(ch) - old.count(ch)
        while extra > 0 and new.endswith(ch):
            new = new[:-1]
            extra -= 1
        while extra > 0:
            i = new.rfind(ch)
            if i < 0:
                break
            new = new[:i] + new[i + 1:]
            extra -= 1
    return new


def canonical_value(text: str, original: str) -> str:
    """译文与原文**完全一样**时，直接沿用原文那一段字节（连引号风格都不动）。

    这是 CQ2 事故（2026-10-07）的核心防线。详见 ``emit_value`` 的长注释。
    """
    return original


# ============================================================ 解压后大小预算
#
# ★★★ dfpf 第三条硬约束：**资源「解压后的字节数」不能超过原版**。
#
# 2026-10-07 在 CQ2（`E:\游戏\CostumeQuest2`）上用**真机启动游戏**逐项排除：
#   · 把资源搬到数据区末尾（offset 变）              → 正常
#   · 压缩后大小变大（size 变）                      → 正常
#   · 只改 data_end / 只改 .~p 文件大小              → 正常
#   · 破坏记录顺序                                   → 正常
#   · **usize（解压后字节数）变大**                  → 启动即死循环 / 0xC0000005
#
# 边界精确落在「原版 usize」上 —— ``stringtable/costumequest_usenglish``
# 原版 usize=388657 字节：
#   · 前 12 条译文  usize=388649  → 启动正常
#   · 第 13 条译文  usize=388666  → 启动卡死（CPU 100% 空转、窗口关不掉）
#   注意第 13 条那组的**压缩后**尺寸（82356）比"正常"那组（82579）还**小**，
#   所以 size 不是判据 —— 只有 usize 能解释全部 12 组对照。
#
# 为什么中译一定会撞线：英文 1 字符 ≈ 1 字节，中文 UTF-8 **每字 3 字节**。
# 全表字数从 388651 掉到 299538（−23%），字节数反而从 388657 涨到 393599
# （+1.3%）。也就是说**译得再好也会超**，必须在回填时把它压回去。
#
# 社区佐证：DoubleFineTool（解包/重打包 dfpf 的工具）作者在 Brutal Legend
# 上记录过同一现象 —— "adding bytes ... 10 bytes causes the game to
# completely lock up"；同引擎（Buddha）、同症状。
#
# 对策（两道闸）：
#  ① 按阶段把**全角标点换成半角**（每个字符 3 字节 → 1 字节，省 2 字节），
#     只压到刚好够用就停，尽量保住中文排版。实测单这一项就能省 1 万字节左右。
#  ② 仍然超 → **逐条淘汰**（``_trim_to_budget``）：丢掉"净增字节"最大的几条，
#     其余照写。v2.6.4 起替代了原来的「一超就整张表放弃」—— 预算是按整张表
#     算的，表内条目能互相补贴，为 1.3% 的差额扔掉 3000 条译文太亏。
#     一条都塞不下时才保原版（写坏了游戏直接起不来，宁可少翻也不能让用户
#     打不开游戏）。
#
#: 全角 → 半角。分阶段是为了"能不换就不换"：先动观感影响最小的符号，
#: 最后才动逗号句号。
_PUN_STAGES: tuple = (
    # 阶段 1：符号类，对中文观感影响最小
    {"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
     "\uff08": "(", "\uff09": ")", "\u300a": "<", "\u300b": ">",
     "\u3010": "[", "\u3011": "]", "\u3001": ",", "\uff1a": ":", "\uff1b": ";"},
    # 阶段 2：叹号 / 问号
    {"\uff01": "!", "\uff1f": "?"},
    # 阶段 3：逗号 / 句号（最伤排版，最后才动）
    {"\uff0c": ",", "\u3002": "."},
)


def _apply_pun_stage(text: str, stage: dict) -> str:
    for k, v in stage.items():
        if k in text:
            text = text.replace(k, v)
    return text


def utf8_len(text: str) -> int:
    """文本的 UTF-8 字节数 —— dfpf 预算按字节算，**不是**字符数。"""
    return len(text.encode("utf-8"))


#: 裸写值里**不能出现**的字符：``;`` 是值的终止符，``"`` ``\\`` 会改变词法，
#: ``{`` ``}`` ``[`` ``]`` 是表结构符号，控制字符会打断行/表结构。
_BARE_BAD = set(';"\\{}[]\r\n\t')


def emit_value(text: str, was_quoted: bool) -> str:
    """按 DSL 规矩产出一个值。

    ★★ 铁律：**原文怎么写，就怎么写回去**（2026-10-07 CQ2 事故换来的）。

    CQ2 的 StringTable 里大量值是**裸写**的，而且裸写值里允许出现
    ``:`` ``-`` ``!`` ``.`` 这类"看起来该加引号"的字符 —— 原版里就有
    ``Text=HP:`` / ``Text=AP:`` / ``Text=BOO!`` / ``Text=T.P.`` /
    ``Text=Raz-Ums`` / ``Text=AAAAAAAAAAWWWW-`` 这样的写法。

    早期实现用 ``[A-Za-z0-9_]+`` 判"能不能裸写"，把这些值统统改写成了
    ``Text="HP:"``。**实测后果极其严重**：只把这 11 条加引号，游戏启动后
    就**死循环** —— 窗口弹出但不响应、任务管理器里一个核跑满、CPU 时间
    线性增长（15s→30s→45s→60s 完全不收敛）、点关闭毫无反应、只能重启
    机器。单条加引号没事，5 条以上必挂；把压缩流补长到原长度也照样挂，
    所以不是长度/对齐问题，就是引号本身。

    结论：判据只能是"**裸写放不放得下**"。只要新文本里没有裸写放不下的
    字符（分号 / 引号 / 反斜杠 / 结构符号 / 控制符），就保持裸写；否则
    才退化成加引号。宁可少加引号，也不要多改一个字节。
    """
    if not was_quoted and not (_BARE_BAD & set(text)):
        return text
    return '"' + escape(text) + '"'


def _trim_to_budget(entries: list[dict], plain: dict, text: str,
                    budget: int) -> tuple[dict, set]:
    """逐条淘汰：丢掉"净增字节"最大的几条，直到整表塞得进预算。

    ★ v2.6.4 起替代原来的「一超就整张表放弃」。理由：预算是**按整张表**算的
    （``usize`` 是这一个资源解压后的大小），所以表内条目**可以互相补贴** ——
    这条译文短一点，就够那条长一点。实测 CQ2 的 ``costumequest_usenglish``
    只超 1.3%（388657 → 393599，+4942 字节），为这点差额扔掉整张表 3000+
    条的译文太亏；``ukenglish`` 那张更只超 1634 字节。

    丢的顺序 = 净增字节**降序**：丢一条顶几条，**保留的中文条数最多**。
    ``delta ≤ 0``（省字节或持平）的条目永远不丢 —— 丢掉它们总量反而更大。

    Args:
        entries: ``parse_entries`` 的解析结果（要用 ``raw`` 算原始字节）
        plain: ``{下标: 纯文本译文}``
        text: 原表整文本（算"不翻是多少字节"的基数）
        budget: 目标上限（原版 ``usize``）

    Returns:
        ``(保留的 {下标: 纯文本}, 被丢弃的下标集合)``
    """
    delta: dict[int, int] = {}
    for i, v in plain.items():
        raw_new = emit_value(v, entries[i]["raw"].startswith('"'))
        if raw_new == entries[i]["raw"]:
            continue                     # 一字不动 → 对总账零贡献
        delta[i] = utf8_len(raw_new) - utf8_len(entries[i]["raw"])

    total = utf8_len(text) + sum(delta.values())
    keep = dict(plain)
    dropped: set = set()
    if total > budget:
        for i in sorted(delta, key=lambda k: delta[k], reverse=True):
            if total <= budget:
                break
            if delta[i] <= 0:
                break                    # 后面全是省字节的，再丢只会更糟
            total -= delta[i]
            dropped.add(i)
            keep.pop(i, None)
    return keep, dropped


# ------------------------------------------------------------------ 解析

def parse_entries(s: str) -> list[dict]:
    """把一张 StringTable 解析成条目列表。

    Returns:
        ``[{"key", "raw", "value", "char", "start", "end"}, …]``
        ``start``/``end`` 是 ``Text`` 值在原文中的区间，回填时用它做精确替换。
    """
    out: list[dict] = []
    pos = 0
    while True:
        m = ENTRY_RE.search(s, pos)
        if not m:
            break
        key = m.group(1)
        body_start = m.end()
        t = s.find(VALUE_MARK, body_start)
        if t < 0 or t - body_start > 80:
            pos = body_start
            continue
        vs = t + len(VALUE_MARK)
        raw, endv = read_value(s, vs)

        char = ""
        cpos = s.find(CHAR_MARK, endv)
        if 0 <= cpos - endv < 80:
            cend = s.find(";", cpos + len(CHAR_MARK))
            if cend > 0:
                char = s[cpos + len(CHAR_MARK):cend].strip().strip('"')

        out.append({"key": key, "raw": raw, "value": unescape(raw),
                    "char": char, "start": vs, "end": endv})
        pos = endv
    return out


def rebuild(s: str, entries: list[dict],
            new_values: dict[int, str]) -> tuple[str, int]:
    """用 ``{条目下标: 新原始值}`` 做精确替换，返回 (新文本, 替换数)。

    没有出现在 ``new_values`` 里的条目**原样保留** —— 这是无损往返的关键。
    """
    parts: list[str] = []
    last = 0
    n = 0
    for i, e in enumerate(entries):
        nv = new_values.get(i)
        if nv is None:
            continue
        parts.append(s[last:e["start"]])
        parts.append(nv)
        last = e["end"]
        n += 1
    parts.append(s[last:])
    return "".join(parts), n


# ------------------------------------------------------------------ 提取器

class BuddhaExtractor(BaseExtractor):
    """Double Fine（Buddha/Moai/Remonkeyed）引擎提取器。"""

    engine = None            # 由 models.EngineType 注入，避免循环 import

    def __init__(self, decoded_dir: Path):
        super().__init__(decoded_dir)
        self.notes: list[str] = []

    # -------------------------------------------------- 找文本表

    def _string_table_candidates(self):
        """产出 (pack, entry) —— 名字/类型像 StringTable 的资源。"""
        for pack in find_packs(self.decoded_dir):
            for e in pack.entries:
                name = e.name.lower()
                type_name = ""
                if 0 <= e.type_index < len(pack.type_names):
                    type_name = pack.type_names[e.type_index]
                if not (name.startswith("stringtable/")
                        or type_name == "StringTable"):
                    continue
                yield pack, e

    @staticmethod
    def _lang_ok(name: str) -> tuple[bool, str]:
        """这个名字的表该不该翻。返回 (是否翻, 原因)。"""
        low = name.lower()
        for tok in NON_ENGLISH_TOKENS:
            if tok in low:
                return False, f"非英语表（含 {tok!r}）"
        for tok in ENGLISH_TOKENS:
            if tok in low:
                return True, ""
        return False, "名字里没有语言标记，无法确认是英语表"

    # -------------------------------------------------- 提取

    def extract(self) -> list[TextUnit]:
        units: list[TextUnit] = []
        skipped: list[str] = []
        for pack, e in self._string_table_candidates():
            ok, why = self._lang_ok(e.name)
            if not ok:
                skipped.append(f"{pack.h_path.stem}::{e.name} —— {why}")
                continue
            if e.comp == COMP_XMEM:
                skipped.append(f"{pack.h_path.stem}::{e.name} —— Xbox 压缩，不支持")
                continue
            try:
                data = pack.read(e)
            except DfpfPackError as ex:
                skipped.append(f"{pack.h_path.stem}::{e.name} —— {ex}")
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                skipped.append(f"{pack.h_path.stem}::{e.name} —— 不是 UTF-8 文本")
                continue
            if not text.lstrip().startswith(TABLE_HEAD):
                skipped.append(f"{pack.h_path.stem}::{e.name} —— 内容不是对话表")
                continue

            rel = self._rel_h(pack)
            for ent in parse_entries(text):
                if ent["key"].upper().startswith(SKIP_KEY_PREFIXES):
                    continue
                value = ent["value"]
                if not _is_translatable(value):
                    continue
                prot, frags = _protect(value)
                loc = {"pack": pack.h_path.stem, "resource": e.name,
                       "key": ent["key"], "form": "dfs"}
                units.append(TextUnit(
                    uid=TextUnit.make_uid(rel, loc, value),
                    source_file=rel, location=loc, original=value,
                    kind=TextKind.DIALOGUE,
                    context=({"speaker": ent["char"]}
                             if ent["char"] and ent["char"] != "Text" else {}),
                    protected=frags, extra={"protected_form": prot},
                ))
        self.notes = skipped
        return units

    def _rel_h(self, pack: DfpfPack) -> str:
        """索引文件相对游戏根目录的路径 —— 回填时用它定位回哪个包。"""
        try:
            return str(pack.h_path.relative_to(self.decoded_dir)).replace("\\", "/")
        except ValueError:
            return pack.h_path.name

    # -------------------------------------------------- 回填

    def write_back(self, project: Project, out_dir: Path) -> dict:
        out_dir = Path(out_dir)
        by_pack: dict[str, list[TextUnit]] = {}
        for u in project.units:
            if u.translated and u.location.get("form") == "dfs":
                by_pack.setdefault(u.source_file, []).append(u)

        written = replaced = unchanged = 0
        changed: list[str] = []
        over_budget: list[str] = []
        trimmed: list[str] = []
        for rel_h, units in by_pack.items():
            src_h = self.decoded_dir / rel_h
            if not src_h.is_file():
                continue
            try:
                pack = DfpfPack.open(src_h)
            except (DfpfPackError, OSError):
                continue

            by_res: dict[str, list[TextUnit]] = {}
            for u in units:
                by_res.setdefault(u.location.get("resource", ""), []).append(u)

            changes: dict = {}
            n_here = 0
            for res_name, rus in by_res.items():
                # ⚠ 同一个包里的资源名**可以重复**（CQ2 的 DLC1_Stuff 里
                # stringtable/costumequestdlc1_leet 就有两条：一条未压缩的
                # 短表 + 一条 zlib 的长表）。按名字取第一个会把译文写到错的
                # 对象上，而真正那张表永远轮不到 —— 所以逐个候选"认领"：
                # 谁的原文能和我们的 original 对上，谁就是目标。
                for ent in pack.find_all(res_name):
                    if ent.comp == COMP_XMEM:
                        continue
                    try:
                        text = pack.read(ent).decode("utf-8")
                    except (DfpfPackError, OSError, UnicodeDecodeError):
                        continue
                    entries = parse_entries(text)
                    index = {e["key"]: i for i, e in enumerate(entries)}

                    new_values: dict[int, str] = {}
                    for u in rus:
                        i = index.get(u.location.get("key", ""))
                        if i is None:
                            continue
                        if entries[i]["value"] != u.original:
                            # 原文对不上 = 这张表不是我们提取的那一张
                            # （重名资源 / 版本不同），写了也是错的。
                            continue
                        new_text = strip_stray_brackets(
                            restore(u.translated, u.protected),
                            entries[i]["value"])
                        # ★ 译文与原文**一字不差** → 原文那一段一个字节都不碰。
                        #   曾经这里会跟着 emit_value 把原本裸写的值改写成
                        #   带引号，实测让 CQ2（2026-10-07）启动即死循环。
                        #   详见 emit_value 的长注释与回归 test_v263_bare_value。
                        if new_text == entries[i]["value"]:
                            continue
                        new_values[i] = new_text
                    if not new_values:
                        continue

                    def _assemble(plain: dict) -> tuple:
                        """把「纯文本译文」按原文的引号风格产出成整表 DSL。"""
                        raws = {}
                        for i, v in plain.items():
                            raw_new = emit_value(
                                v, entries[i]["raw"].startswith('"'))
                            if raw_new != entries[i]["raw"]:
                                raws[i] = raw_new
                        return rebuild(text, entries, raws)

                    new_text, n = _assemble(new_values)
                    if not n:
                        continue

                    # ★★ 解压后字节数必须 ≤ 原版（见 _PUN_STAGES 上方的长注释）。
                    # 超了就把全角标点逐阶段换半角，够用即止。
                    budget = int(ent.usize or 0)
                    payload = new_text.encode("utf-8")
                    if budget and len(payload) > budget:
                        for stage in _PUN_STAGES:
                            new_values = {i: _apply_pun_stage(v, stage)
                                          for i, v in new_values.items()}
                            new_text, n = _assemble(new_values)
                            payload = new_text.encode("utf-8")
                            if len(payload) <= budget:
                                break
                    if budget and len(payload) > budget:
                        # 压标点还不够 → **逐条淘汰**，而不是整张表放弃。
                        # 预算按整张表算，表内条目能互相补贴，所以只有真正
                        # 装不下的那几条保留英文。全被丢掉时等价于「保原版」。
                        kept, dropped = _trim_to_budget(
                            entries, new_values, text, budget)
                        new_text, n = _assemble(kept)
                        payload = new_text.encode("utf-8")
                        if not n or len(payload) > budget:
                            # 一条都塞不下（或算术与实际不符）→ 保原版。
                            # 写坏了游戏直接起不来（实测「黑屏 + 窗口关不掉 +
                            # CPU 空转」），宁可少翻也不能让用户打不开游戏。
                            over_budget.append(f"{rel_h}::{res_name}")
                            continue
                        trimmed.append(f"{rel_h}::{res_name}::{len(dropped)}")

                    changes[ent] = payload
                    n_here += n
                    break          # 这张表收工，同名的其余候选不用再看

            if not changes:
                continue

            dst_h = out_dir / rel_h
            dst_p = dst_h.with_suffix(".~p")
            try:
                res = pack.write(changes, dst_h, dst_p)
            except (DfpfPackError, OSError):
                continue

            replaced += n_here
            written += 2                       # 索引 + 数据各算一个产出文件
            # 索引与数据都相对原文件变了，都要进补丁清单
            rel_p = str(Path(rel_h).with_suffix(".~p")).replace("\\", "/")
            changed.extend([rel_h, rel_p])
            if res.get("mode") == "none":
                unchanged += 2

        return wb_stats(files_written=written, replaced=replaced,
                        unchanged=unchanged, changed=changed,
                        over_budget=over_budget, trimmed=trimmed)


# 延后绑定引擎枚举，避免 models → extractors 的循环引用
def _bind_engine() -> None:
    try:
        from ..core.models import EngineType
        BuddhaExtractor.engine = EngineType.BUDDHA
    except Exception:      # noqa: BLE001 - 极端情况（被打包裁剪）下不影响功能
        pass


_bind_engine()
