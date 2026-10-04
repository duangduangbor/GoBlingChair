"""KiriKiri（吉里吉里 / KAG）文本提取器。

KiriKiri 的剧本是 .ks 文件，KAG 标记语言格式，典型结构：

    [name="勇者"]
    这是一句对话。[r]
    @color=0xFFFFFF
    另一句话。

需要翻译的只有「对话文本」——即标签 [xxx] 之外、@指令之外的可见文字。
回填时保持所有标签与指令原样。

注意：本提取器工作在「已用 GARbro 等工具解包后的目录」上，
不负责 .xp3 解包本身（那部分依赖外部工具，见 README）。
"""
from __future__ import annotations

import re
from pathlib import Path

from ..core.models import Project, TextKind, TextUnit
from ..core.protect import protect
from ..core.scan import find_by_suffix
from .base import BaseExtractor

# KAG 行内变量引用：[pcname]、[name]、[f.xxx] —— 属于文本内容，应保留在文本中
INLINE_VAR_RE = re.compile(r"\[[a-zA-Z_][\w\.]*\]")
# 真正的 KAG 标签：[tag attr=val]、[tag]、[/tag]、[tag ...]
# 特征：包含 = 号、/ 开头、或是已知标签名、或含空格参数
KNOWN_TAGS = {
    "cm", "r", "l", "p", "br", "er", "hr", "wait", "nowait",
    "link", "endlink", "return", "jump", "call", "if", "else", "endif",
    "bg", "fg", "image", "position", "quake", "waitbgm", "playbgm", "stopbgm",
    "fadein", "fadeout", "h", "reset", "graph", "locate", "mov", "vibrate",
    "name", "clear", "ct", "endindent", "indent", "globals", "macro",
    "eval", "emb", "endemb", "iscript", "endscript", "loadplugin", "button",
    "endbutton", "select", "endselect", "store", "history",
}
# 标签形如 [tagname] 或 [tagname ...] 或 [/tagname] 或 [tagname=...]
TAG_RE = re.compile(r"\[/?(?:[a-zA-Z_][\w]*)(?:\s[^\[\]]*|=[^\[\]]*)?\]")
# 行首标签（整行都是标签/指令的行，不翻译）
LEADING_TAG_RE = re.compile(r"^\s*(?:\[[^\[\]]*\]|@\w+|\*[^\s]*|;.*)\s*$")
# 行首 @ 指令：@command param
CMD_RE = re.compile(r"^\s*@\w+.*$")
# KAG 注释：; 开头
COMMENT_RE = re.compile(r"^\s*;.*$")
# 脚本标签行：*label
LABEL_RE = re.compile(r"^\s*\*[\w]*")
# 显式脚本块标记（不应翻译的内容）
SCRIPT_BLOCK_RE = re.compile(r"^\s*\[iscript\]|^\s*\[endscript\]")

# 仅包含标点/空白/深度的行不翻译
def _is_translatable(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    # 纯符号
    if not any(c.isalnum() or c.isalpha() for c in stripped):
        return False
    # 纯数字/坐标之类
    if re.fullmatch(r"[\d\s\.,:;_\-\*/\\\(\)\[\]<>=\+\|@#$%^&!?~`]+", stripped):
        return False
    return True


def _split_line(line: str) -> list[tuple[str, str]]:
    """把一行 KAG 文本拆成 [(类型, 内容)]，类型为 'text' 或 'tag'。

    关键：区分「行内变量引用」与「KAG 标签」。
    - `[pcname]`（纯标识符）→ 视为文本，保留在对话内容中
    - `[r]`、`[bg storage=x]`、`[/link]`（已知标签/带参数）→ 视为标签，不翻译
    """
    parts: list[tuple[str, str]] = []
    pos = 0
    for m in TAG_RE.finditer(line):
        token = m.group(0)
        inner = token[1:-1].strip()  # 去掉 []
        inner_name = inner.split()[0].split("=")[0].lower() if inner else ""
        # 判定优先级：已知标签名 > 带参数/闭合标签 > 纯标识符（变量）
        if inner_name in KNOWN_TAGS or inner_name.startswith("end"):
            is_tag = True
        elif token.startswith("[/") or "=" in inner or " " in inner:
            is_tag = True
        else:
            # 纯标识符：可能是行内变量（保留在文本中）
            is_tag = not bool(INLINE_VAR_RE.fullmatch(token))
        if not is_tag:
            continue
        if m.start() > pos:
            parts.append(("text", line[pos:m.start()]))
        parts.append(("tag", token))
        pos = m.end()
    if pos < len(line):
        parts.append(("text", line[pos:]))
    return parts


class KiriKiriExtractor(BaseExtractor):
    """KiriKiri 剧本提取器。"""

    def __init__(self, decoded_dir: Path, encoding: str = "utf-16"):
        """
        Args:
            decoded_dir: 解包后的目录。
            encoding: .ks 文件编码。KiriKiri 常见 UTF-16LE（带 BOM）；
                      部分老游戏为 Shift-JIS。自动探测失败时用此值。
        """
        super().__init__(decoded_dir)
        self.encoding = encoding
        self._file_cache: dict[str, str] = {}

    def _read(self, path: Path) -> tuple[str, str]:
        """读取文件，自动探测编码。返回 (文本, 实际编码)。"""
        raw = path.read_bytes()
        if raw.startswith(b"\xff\xfe"):
            return raw.decode("utf-16-le"), "utf-16-le"
        if raw.startswith(b"\xfe\xff"):
            return raw.decode("utf-16-be"), "utf-16-be"
        if raw.startswith(b"\xef\xbb\xbf"):
            return raw.decode("utf-8-sig"), "utf-8-sig"
        # 无 BOM：先试 UTF-8，再试 Shift-JIS，最后用指定编码
        for enc in ("utf-8", "shift_jis", self.encoding):
            try:
                return raw.decode(enc), enc
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode(self.encoding, errors="replace"), self.encoding

    def extract(self) -> list[TextUnit]:
        units: list[TextUnit] = []
        ks_files = find_by_suffix(self.decoded_dir, {".ks", ".scn"}, max_depth=8)
        ks_files.sort()

        for path in ks_files:
            rel = str(path.relative_to(self.decoded_dir)).replace("\\", "/")
            try:
                text, enc = self._read(path)
            except OSError:
                continue
            self._file_cache[rel] = enc

            speaker: str | None = None
            in_script = False
            for lineno, line in enumerate(text.splitlines(), start=1):
                if SCRIPT_BLOCK_RE.search(line):
                    in_script = "[iscript]" in line
                    continue
                if in_script:
                    continue
                if COMMENT_RE.match(line) or CMD_RE.match(line) or LABEL_RE.match(line):
                    continue

                # 先提取说话人（[name="xxx"] 可能单独成行），再决定是否跳过该行
                name_match = re.search(r'\[name\s*=\s*"([^"]*)"', line)
                if name_match:
                    speaker = name_match.group(1)

                # 整行都是标签/指令的行，跳过（但说话人状态已更新）
                if LEADING_TAG_RE.match(line):
                    continue

                # 收集本行所有文本片段，合并为一个待翻译单元
                text_parts = [c for k, c in _split_line(line) if k == "text"]
                merged = "".join(text_parts)
                if not _is_translatable(merged):
                    continue
                prot, frags = protect(merged)
                uid = TextUnit.make_uid(rel, {"line": lineno}, merged)
                units.append(TextUnit(
                    uid=uid,
                    source_file=rel,
                    location={"line": lineno, "encoding": enc},
                    original=merged,
                    kind=TextKind.DIALOGUE,
                    context={"speaker": speaker} if speaker else {},
                    protected=frags,
                    extra={"protected_form": prot},
                ))
        return units

    def write_back(self, project: Project, out_dir: Path) -> dict:
        """按行号回填译文，输出修改后的 .ks 文件。"""
        from ..core.protect import restore

        out_dir = Path(out_dir)
        # 按文件分组：{相对路径: {行号: 译文}}
        by_file: dict[str, dict[int, str]] = {}
        for u in project.units:
            if not u.translated:
                continue
            ln = u.location.get("line")
            if ln is None:
                continue
            by_file.setdefault(u.source_file, {})[int(ln)] = restore(u.translated, u.protected)

        written = 0
        replaced = 0
        for rel, line_map in by_file.items():
            src = self.decoded_dir / rel
            if not src.exists():
                continue
            enc = self._file_cache.get(rel) or self._read(src)[1]
            text, _ = self._read(src)
            lines = text.splitlines(keepends=True)
            for lineno, translated in line_map.items():
                idx = lineno - 1
                if not (0 <= idx < len(lines)):
                    continue
                orig_line = lines[idx]
                nl = "\n" if orig_line.endswith("\n") else ""
                # 只替换文本片段，保留标签结构
                new_line = self._rebuild_line(orig_line.rstrip("\r\n"), translated)
                lines[idx] = new_line + nl
                replaced += 1

            dst = out_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            # 保持原编码写回
            out_text = "".join(lines)
            if enc.startswith("utf-16"):
                dst.write_bytes(out_text.encode(enc))
            elif enc == "utf-8-sig":
                dst.write_bytes(out_text.encode("utf-8-sig"))
            else:
                dst.write_bytes(out_text.encode(enc, errors="xmlcharrefreplace"))
            written += 1

        return {"files_written": written, "lines_replaced": replaced}

    @staticmethod
    def _rebuild_line(orig_line: str, translated: str) -> str:
        """把原行中的文本片段替换为译文，保留所有标签。

        若原行有多个文本片段，译文整体放入第一个片段位置，
        其余片段留空（KAG 一行通常只有一个语义片段）。
        """
        parts = _split_line(orig_line)
        out: list[str] = []
        placed = False
        for kind, content in parts:
            if kind == "tag":
                out.append(content)
            else:
                if not placed and _is_translatable(content):
                    out.append(translated)
                    placed = True
                else:
                    out.append(content)
        return "".join(out)
