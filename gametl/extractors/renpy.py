"""Ren'Py 文本提取器。

Ren'Py 的剧本是 .rpy 文件，Python 风格语法。典型结构：

    label start:
        "这是一句旁白。"
        e "Ellen 的对话内容。"
        menu:
            "选项 A":
                pass

需要翻译的是「字符串字面量」——尤其是作为台词/旁白的独立字符串、
menu 选项文字、以及 name 定义（角色名）。

注意：Ren'Py 官方支持 `translate` 语句，理想做法是生成
`game/tl/chinese/*.rpy` 翻译文件而非直接改原文。本工具提供两种模式：
- patch 模式：直接改 .rpy 字面量（简单直接，适用于无官方 tl 的游戏）
- 默认 patch 模式。
"""
from __future__ import annotations

import re
from pathlib import Path

from ..core.models import Project, TextKind, TextUnit
from ..core.protect import protect
from ..core.scan import find_by_suffix
from .base import BaseExtractor

# 匹配双引号字符串（支持转义）
STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
# define 角色名：define e = Character("Ellen", color="#fff")
CHARACTER_RE = re.compile(r'Character\s*\(\s*"((?:[^"\\]|\\.)*)"')
# 该行是否只是纯字符串台词行（缩进 + "..."）
SIMPLE_LINE_RE = re.compile(r'^\s*"[^"]*"\s*(?:with\s+\w+)?\s*$')
# menu 选项行："选项文本":
MENU_OPTION_RE = re.compile(r'^\s*"((?:[^"\\]|\\.)*)"\s*:\s*$')
# 角色对话行：e "台词"
CHAR_LINE_RE = re.compile(r'^\s*([A-Za-z_]\w*)\s+"((?:[^"\\]|\\.)*)"\s*(?:with\s+\w+)?\s*$')
# 关键字行（不应翻译）
KEYWORD_RE = re.compile(
    r"^\s*(label|jump|call|menu|if|elif|else|while|for|return|pass|scene|show|hide|"
    r"play|stop|queue|pause|with|init|define|default|python|image|transform|style|"
    r"screen|translate|voice|window|nvl|from|import|class|def|set|at|\$)\b"
)


def _is_translatable(text: str) -> bool:
    s = text.strip()
    if not s:
        return False
    if not any(c.isalnum() or c.isalpha() or ord(c) > 0x2E80 for c in s):
        return False
    return True


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class RenPyExtractor(BaseExtractor):
    """Ren'Py 提取器。"""

    def __init__(self, decoded_dir: Path):
        super().__init__(decoded_dir)
        self.game_dir = decoded_dir / "game" if (decoded_dir / "game").is_dir() else decoded_dir

    def extract(self) -> list[TextUnit]:
        units: list[TextUnit] = []
        # 限界扫描：Ren'Py 脚本一般在 game/ 下，深度不会太大
        rpy_files = find_by_suffix(self.game_dir, {".rpy"}, max_depth=6)
        rpy_files.sort()
        # 排除已有翻译目录，避免把译文当原文
        rpy_files = [p for p in rpy_files if "tl" not in p.relative_to(self.game_dir).parts]

        for path in rpy_files:
            rel = str(path.relative_to(self.decoded_dir)).replace("\\", "/")
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (UnicodeDecodeError, OSError):
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue

            for lineno, line in enumerate(text.splitlines(), start=1):
                # 跳过关键字/控制行
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue

                # 1. Character 定义中的角色名
                cm = CHARACTER_RE.search(line)
                if cm:
                    name = _unescape(cm.group(1))
                    if _is_translatable(name):
                        units.append(self._mk(rel, lineno, name, TextKind.NAME,
                                              {"pattern": "character", "col": cm.start(1)}))
                    continue

                # 2. menu 选项
                om = MENU_OPTION_RE.match(line)
                if om:
                    opt = _unescape(om.group(1))
                    if _is_translatable(opt):
                        units.append(self._mk(rel, lineno, opt, TextKind.UI,
                                              {"pattern": "menu_option", "col": om.start(1)}))
                    continue

                # 3. 角色对话行 e "台词"
                dm = CHAR_LINE_RE.match(line)
                if dm:
                    body = _unescape(dm.group(2))
                    if _is_translatable(body):
                        units.append(self._mk(rel, lineno, body, TextKind.DIALOGUE,
                                              {"pattern": "dialogue", "speaker": dm.group(1),
                                               "col": dm.start(2)}))
                    continue

                # 4. 纯旁白行 "文本"
                if SIMPLE_LINE_RE.match(line) and not KEYWORD_RE.match(line):
                    sm = STRING_RE.search(line)
                    if sm:
                        body = _unescape(sm.group(1))
                        if _is_translatable(body):
                            units.append(self._mk(rel, lineno, body, TextKind.DIALOGUE,
                                                  {"pattern": "narration", "col": sm.start(1)}))
        return units

    def _mk(self, rel: str, lineno: int, text: str, kind: TextKind,
            ctx: dict) -> TextUnit:
        prot, frags = protect(text)
        loc = {"line": lineno, **ctx}
        return TextUnit(
            uid=TextUnit.make_uid(rel, loc, text),
            source_file=rel, location=loc, original=text, kind=kind,
            context={"speaker": ctx.get("speaker", "")} if ctx.get("speaker") else {},
            protected=frags, extra={"protected_form": prot},
        )

    def write_back(self, project: Project, out_dir: Path) -> dict:
        from ..core.protect import restore

        by_file: dict[str, list[TextUnit]] = {}
        for u in project.units:
            if u.translated:
                by_file.setdefault(u.source_file, []).append(u)

        written = 0
        replaced = 0
        for rel, us in by_file.items():
            src = self.decoded_dir / rel
            if not src.exists():
                continue
            text = src.read_text(encoding="utf-8-sig")
            lines = text.splitlines(keepends=True)

            # 同一行可能有多条（极少），按列号处理时以行为单位替换全部字符串
            by_line: dict[int, list[TextUnit]] = {}
            for u in us:
                by_line.setdefault(int(u.location["line"]), []).append(u)

            for lineno, line_units in by_line.items():
                idx = lineno - 1
                if not (0 <= idx < len(lines)):
                    continue
                orig = lines[idx].rstrip("\r\n")
                nl = "\n" if lines[idx].endswith("\n") else ""
                new_line = orig
                for u in line_units:
                    translated = restore(u.translated, u.protected)
                    new_line = self._replace_string(new_line, u, translated)
                lines[idx] = new_line + nl
                replaced += 1

            dst = Path(out_dir) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text("".join(lines), encoding="utf-8")
            written += 1
        return {"files_written": written, "lines_replaced": replaced}

    @staticmethod
    def _replace_string(line: str, u: TextUnit, translated: str) -> str:
        """把行内指定位置的字符串替换为译文。"""
        pattern = u.location.get("pattern")
        target = _escape(translated)

        # 按 pattern 精确定位
        if pattern == "character":
            m = CHARACTER_RE.search(line)
            if m:
                return line[:m.start(1)] + target + line[m.end(1):]
        elif pattern == "menu_option":
            m = MENU_OPTION_RE.match(line)
            if m:
                return line[:m.start(1)] + target + line[m.end(1):]
        elif pattern == "dialogue":
            m = CHAR_LINE_RE.match(line)
            if m:
                return line[:m.start(2)] + target + line[m.end(2):]
        elif pattern == "narration":
            m = STRING_RE.search(line)
            if m:
                return line[:m.start(1)] + target + line[m.end(1):]
        return line
