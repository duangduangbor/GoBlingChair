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


_BARE_OK = re.compile(r"[A-Za-z0-9_]+")


def emit_value(text: str, was_quoted: bool) -> str:
    """按 DSL 规矩产出一个值。

    · 原来没引号、新文本仍是纯 ASCII 标识符 → 保持裸写（不改无谓的字节）
    · 其余一律加引号 —— 中文、空格、分号、引号都必须靠引号包住才合法
    """
    if not was_quoted and _BARE_OK.fullmatch(text):
        return text
    return '"' + escape(text) + '"'


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

            changes: dict[str, bytes] = {}
            n_here = 0
            for res_name, rus in by_res.items():
                ent = pack.find(res_name)
                if ent is None or ent.comp == COMP_XMEM:
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
                    raw_new = emit_value(restore(u.translated, u.protected),
                                         entries[i]["raw"].startswith('"'))
                    if raw_new == entries[i]["raw"]:
                        continue
                    new_values[i] = raw_new
                if not new_values:
                    continue
                new_text, n = rebuild(text, entries, new_values)
                if not n:
                    continue
                changes[res_name] = new_text.encode("utf-8")
                n_here += n

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
                        unchanged=unchanged, changed=changed)


# 延后绑定引擎枚举，避免 models → extractors 的循环引用
def _bind_engine() -> None:
    try:
        from ..core.models import EngineType
        BuddhaExtractor.engine = EngineType.BUDDHA
    except Exception:      # noqa: BLE001 - 极端情况（被打包裁剪）下不影响功能
        pass


_bind_engine()
