# -*- coding: utf-8 -*-
"""通用明文文本适配器 —— 不依赖任何引擎特征。

很多小体量游戏（Godot 导出的外置文本、自研框架、被解包后的资源目录）并不
属于任何一个已知引擎，但它们的文本就是**明文的文件**：``.txt`` / ``.csv`` /
``.json`` / ``.xml`` / ``.yaml`` / ``.po`` / ``.srt`` / ``.ass`` …

这个适配器就是为它们准备的：把目录里所有像文本的文件扫一遍，逐行/逐格/逐键
地挑出该翻的内容，回填时**只替换那一段**，格式、缩进、换行风格、BOM 全部
原样保留。

支持的形态
----------
============  ==========================================================
后缀          解析方式
============  ==========================================================
.txt/.text    按行取正文
.lang/.strings/.loc  同上（本地化字符串文件常见后缀）
.srt          只取字幕正文行，跳过序号与时间轴
.ass/.ssa     只取 ``Dialogue:`` 行末尾的台词，跳过样式与注释
.csv/.tsv     逐单元格（跳过纯数字/布尔）
.json         递归取字符串值（按 key 路径定位，回填精确写回）
.xml          取标签之间的文本
.yaml/.yml    ``key: value`` 的 value 部分
.po           ``msgstr "…"`` 的值
============  ==========================================================

安全边界：只处理**能按 UTF-8 严格解码**的文件，且单个文件不超过 8 MB ——
解不开的一律跳过，宁可少翻也不改坏。
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from ..core.detect import PLAINTEXT_SKIP_NAMES, PLAINTEXT_SUFFIXES
from ..core.models import Project, TextKind, TextUnit
from ..core.protect import protect, restore
from ..core.scan import iter_files
from .base import BaseExtractor, wb_stats, write_back_file

#: 单个文件最大处理体积（超过就跳过，避免把时间耗在日志/存档上）
MAX_FILE_BYTES = 8 * 1024 * 1024
#: 最多处理多少个文件
MAX_FILES = 400

#: 结构型/配置文件，不翻
SKIP_NAMES = set(PLAINTEXT_SKIP_NAMES) | {
    "package.json", "manifest.json", "projectversion.txt", "boot.config",
    "app.info", "tsconfig.json", "package-lock.json", "yarn.lock",
    "launch.json", "settings.json", "editorconfig",
}
SKIP_SUFFIXES = (".min.json", ".min.js", ".map")

LINE_SUFFIXES = {".txt", ".text", ".lang", ".strings", ".loc", ".md"}
CSV_SUFFIXES = {".csv", ".tsv"}
JSON_SUFFIXES = {".json"}
XML_SUFFIXES = {".xml"}
YAML_SUFFIXES = {".yaml", ".yml"}
PO_SUFFIXES = {".po"}

#: .srt 的序号行 / 时间轴行
SRT_TIME_RE = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->")
SRT_INDEX_RE = re.compile(r"^\s*\d{1,3}\s*$")
#: .ass 的 Dialogue 行：``Dialogue: 0,0:00:01.00,0:00:02.00,Style,,0,0,0,,台词``
ASS_DIALOGUE_RE = re.compile(r"^(\s*Dialogue\s*:\s*)(.*)$", re.IGNORECASE)
ASS_FIELDS = 9
#: .yaml 的 ``key: value``
YAML_KV_RE = re.compile(r"^(\s*(?:-\s+)?[\w\-.$]+)\s*:\s*(\S.*)$")
#: .po 的 ``msgstr "…"``
PO_MSGSTR_RE = re.compile(r'^\s*msgstr\s+(".*")\s*$')


def _is_translatable(text: str) -> bool:
    s = text.strip()
    if not s:
        return False
    return any(c.isalpha() for c in s)


def _try_read(path: Path) -> tuple[bytes, str, bool, str]:
    """读取文件，返回 (原始字节, 文本, 是否有 BOM, 换行风格)。解不开抛异常。"""
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig") if bom else raw.decode("utf-8")
    nl = "\r\n" if "\r\n" in text else "\n"
    return raw, text, bom, nl


def _join(text_lines: list[str], bom: bool, nl: str, trailing: bool) -> bytes:
    s = nl.join(text_lines)
    if trailing:
        s += nl
    b = s.encode("utf-8")
    return (b"\xef\xbb\xbf" + b) if bom else b


class PlainTextExtractor(BaseExtractor):
    """明文文本提取器（无引擎特征时的通用兜底）。"""

    def __init__(self, decoded_dir: Path):
        super().__init__(decoded_dir)
        self.skipped: list[str] = []

    # -------------------------------------------------- 文件筛选

    def _candidates(self) -> list[Path]:
        out: list[Path] = []
        for p in iter_files(self.decoded_dir, max_depth=5):
            if len(out) >= MAX_FILES:
                break
            suf = p.suffix.lower()
            if suf not in PLAINTEXT_SUFFIXES:
                continue
            name = p.name.lower()
            if name in SKIP_NAMES or any(name.endswith(s) for s in SKIP_SUFFIXES):
                continue
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    self.skipped.append(f"{p.name}（超过 8MB）")
                    continue
            except OSError:
                continue
            out.append(p)
        out.sort(key=lambda q: str(q).lower())
        return out

    # -------------------------------------------------- 提取

    def extract(self) -> list[TextUnit]:
        units: list[TextUnit] = []
        for path in self._candidates():
            rel = str(path.relative_to(self.decoded_dir)).replace("\\", "/")
            try:
                _raw, text, _bom, _nl = _try_read(path)
            except (OSError, UnicodeDecodeError):
                self.skipped.append(f"{rel}（不是 UTF-8 文本）")
                continue
            suf = path.suffix.lower()
            try:
                if suf in CSV_SUFFIXES:
                    units.extend(self._extract_csv(rel, text))
                elif suf in JSON_SUFFIXES:
                    units.extend(self._extract_json(rel, text))
                elif suf in PO_SUFFIXES:
                    units.extend(self._extract_po(rel, text))
                elif suf == ".srt":
                    units.extend(self._extract_srt(rel, text))
                elif suf in (".ass", ".ssa"):
                    units.extend(self._extract_ass(rel, text))
                elif suf in YAML_SUFFIXES:
                    units.extend(self._extract_yaml(rel, text))
                elif suf in XML_SUFFIXES:
                    units.extend(self._extract_xml(rel, text))
                else:
                    units.extend(self._extract_lines(rel, text))
            except (ValueError, csv.Error, json.JSONDecodeError):
                continue
        return units

    def _mk(self, rel: str, loc: dict, text: str,
            kind: TextKind = TextKind.OTHER) -> TextUnit:
        prot, frags = protect(text)
        return TextUnit(
            uid=TextUnit.make_uid(rel, loc, text),
            source_file=rel, location=loc, original=text, kind=kind,
            protected=frags, extra={"protected_form": prot},
        )

    # ---- 逐行 / 字幕 ----

    def _extract_lines(self, rel: str, text: str) -> list[TextUnit]:
        units = []
        for i, line in enumerate(text.splitlines()):
            s = line.strip()
            if not _is_translatable(s):
                continue
            a = len(line) - len(line.lstrip())
            units.append(self._mk(rel, {"form": "line", "line": i + 1,
                                        "span": [a, a + len(s)]},
                                  s, TextKind.DIALOGUE))
        return units

    def _extract_srt(self, rel: str, text: str) -> list[TextUnit]:
        units = []
        for i, line in enumerate(text.splitlines()):
            s = line.strip()
            if not s or SRT_INDEX_RE.match(s) or SRT_TIME_RE.match(s):
                continue
            if not _is_translatable(s):
                continue
            a = len(line) - len(line.lstrip())
            units.append(self._mk(rel, {"form": "line", "line": i + 1,
                                        "span": [a, a + len(s)]},
                                  s, TextKind.DIALOGUE))
        return units

    def _extract_ass(self, rel: str, text: str) -> list[TextUnit]:
        units = []
        for i, line in enumerate(text.splitlines()):
            m = ASS_DIALOGUE_RE.match(line)
            if not m:
                continue
            body = m.group(2)
            parts = body.split(",", ASS_FIELDS)
            if len(parts) <= ASS_FIELDS:
                continue
            payload = parts[ASS_FIELDS]
            payload = re.sub(r"\{[^{}]*\}", "", payload)      # 去掉 {\an8} 这类特效码
            s = payload.strip()
            if not _is_translatable(s):
                continue
            a = line.find(s)
            if a < 0:
                continue
            units.append(self._mk(rel, {"form": "line", "line": i + 1,
                                        "span": [a, a + len(s)]},
                                  s, TextKind.DIALOGUE))
        return units

    def _extract_yaml(self, rel: str, text: str) -> list[TextUnit]:
        units = []
        for i, line in enumerate(text.splitlines()):
            m = YAML_KV_RE.match(line)
            if not m:
                continue
            val = m.group(2).strip()
            if val.startswith(("|", ">", "*", "&")) or val in ("~", "null"):
                continue
            val = val.strip('"').strip("'")
            if not _is_translatable(val):
                continue
            a = line.rfind(val)
            if a < 0:
                continue
            units.append(self._mk(rel, {"form": "line", "line": i + 1,
                                        "span": [a, a + len(val)]},
                                  val))
        return units

    def _extract_po(self, rel: str, text: str) -> list[TextUnit]:
        units = []
        for i, line in enumerate(text.splitlines()):
            m = PO_MSGSTR_RE.match(line)
            if not m:
                continue
            quoted = m.group(1)                 # 含首尾双引号
            val = quoted[1:-1]
            if not _is_translatable(val):
                continue
            a = line.find(quoted)
            if a < 0:
                continue
            # 只替换引号**里面**那一段，引号本身留着
            units.append(self._mk(rel, {"form": "line", "line": i + 1,
                                        "span": [a + 1, a + 1 + len(val)]},
                                  val))
        return units

    def _extract_xml(self, rel: str, text: str) -> list[TextUnit]:
        units = []
        for i, line in enumerate(text.splitlines()):
            for m in re.finditer(r">([^<>]+)<", line):
                s = m.group(1).strip()
                if not _is_translatable(s):
                    continue
                a = m.start(1) + len(m.group(1)) - len(m.group(1).lstrip())
                units.append(self._mk(rel, {"form": "line", "line": i + 1,
                                            "span": [a, a + len(s)]}, s))
        return units

    # ---- 结构型 ----

    def _extract_csv(self, rel: str, text: str) -> list[TextUnit]:
        dialect = csv.excel_tab if rel.lower().endswith(".tsv") else csv.excel
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;")
        except csv.Error:
            pass
        units = []
        for r, row in enumerate(csv.reader(io.StringIO(text), dialect)):
            for c, cell in enumerate(row):
                if not _is_translatable(cell):
                    continue
                units.append(self._mk(rel, {"form": "csv", "row": r, "col": c},
                                      cell))
        return units

    def _extract_json(self, rel: str, text: str) -> list[TextUnit]:
        data = json.loads(text)
        units: list[TextUnit] = []

        def walk(node, key_path: list) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, key_path + [k])
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, key_path + [i])
            elif isinstance(node, str) and _is_translatable(node):
                units.append(self._mk(rel, {"form": "json", "key_path": key_path},
                                      node))

        walk(data, [])
        return units

    # -------------------------------------------------- 回填

    def write_back(self, project: Project, out_dir: Path) -> dict:
        out_dir = Path(out_dir)
        by_file: dict[str, list[TextUnit]] = {}
        for u in project.units:
            if u.translated:
                by_file.setdefault(u.source_file, []).append(u)

        written = replaced = unchanged = 0
        changed: list[str] = []
        for rel, us in by_file.items():
            src = self.decoded_dir / rel
            if not src.is_file():
                continue
            try:
                _raw, text, bom, nl = _try_read(src)
            except (OSError, UnicodeDecodeError):
                continue
            forms = {u.location.get("form") for u in us}
            trailing = text.endswith("\n")
            try:
                if "json" in forms:
                    n, payload = self._write_json(text, us)
                elif "csv" in forms:
                    n, payload = self._write_csv(rel, text, us)
                else:
                    n, payload = self._write_lines(text, us, bom, nl, trailing)
            except (ValueError, csv.Error, json.JSONDecodeError, IndexError):
                continue
            if not n:
                continue
            wrote, differs = write_back_file(src, out_dir / rel, payload)
            replaced += n
            written += 1
            if differs:
                changed.append(rel)
            else:
                unchanged += 1

        return wb_stats(files_written=written, replaced=replaced,
                        unchanged=unchanged, changed=changed)

    @staticmethod
    def _write_lines(text: str, us: list[TextUnit], bom: bool, nl: str,
                     trailing: bool) -> tuple[int, bytes]:
        """按「行号 + 列区间」精确替换，其余字节不动。"""
        lines = text.splitlines()
        n = 0
        for u in sorted(us, key=lambda x: x.location.get("line", 0), reverse=True):
            ln = u.location.get("line")
            span = u.location.get("span")
            if not isinstance(ln, int) or not isinstance(span, list):
                continue
            if not (1 <= ln <= len(lines)):
                continue
            cur = lines[ln - 1]
            a, b = int(span[0]), int(span[1])
            if a < 0 or b > len(cur) or cur[a:b] != u.original:
                continue
            lines[ln - 1] = cur[:a] + restore(u.translated, u.protected) + cur[b:]
            n += 1
        if not n:
            return 0, b""
        return n, _join(lines, bom, nl, trailing)

    @staticmethod
    def _write_csv(rel: str, text: str, us: list[TextUnit]) -> tuple[int, bytes]:
        dialect = csv.excel_tab if rel.lower().endswith(".tsv") else csv.excel
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;")
        except csv.Error:
            pass
        rows = [r for r in csv.reader(io.StringIO(text), dialect)]
        n = 0
        for u in us:
            r, c = u.location.get("row"), u.location.get("col")
            try:
                if rows[r][c] != u.original:
                    continue
                rows[r][c] = restore(u.translated, u.protected)
                n += 1
            except (IndexError, TypeError):
                continue
        if not n:
            return 0, b""
        buf = io.StringIO(newline="")
        csv.writer(buf, dialect=dialect).writerows(rows)
        out = buf.getvalue()
        nl = "\r\n" if "\r\n" in text else "\n"
        if nl != "\n":
            out = out.replace("\r\n", "\n").replace("\n", "\r\n")
        return n, out.encode("utf-8")

    @staticmethod
    def _write_json(text: str, us: list[TextUnit]) -> tuple[int, bytes]:
        data = json.loads(text)
        indent = 2 if re.search(r"\n\s+\S", text) else None
        n = 0
        for u in us:
            kp = u.location.get("key_path") or []
            node = data
            try:
                for k in kp[:-1]:
                    node = node[k]
                if node[kp[-1]] != u.original:
                    continue
                node[kp[-1]] = restore(u.translated, u.protected)
                n += 1
            except (KeyError, IndexError, TypeError):
                continue
        if not n:
            return 0, b""
        return n, json.dumps(data, ensure_ascii=False,
                             indent=indent).encode("utf-8")
