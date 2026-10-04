"""Unity 文本提取器。

Unity 游戏的文本存储有多种形态，本提取器覆盖最常见的三种：
1. 独立的文本资源文件（.txt / .json / .csv）—— 最简单，直接当文本处理。
2. Localization 插件产物（I2 Localization 的 .csv，或 Unity Localization 的 .json）。
3. .assets / bundle 中的 MonoBehaviour 文本 —— 需要 UnityPy 解析（可选依赖）。

说明：完整解析 Unity 二进制资源依赖 UnityPy（需额外安装）。
本提取器在未安装 UnityPy 时，退化为只处理纯文本资源，
并给出明确提示，引导用户安装或改用 XUnity.AutoTranslator 运行时方案。
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from ..core.models import Project, TextKind, TextUnit
from ..core.protect import protect
from ..core.scan import iter_files
from .base import BaseExtractor

TEXT_SUFFIXES = {".txt", ".json", ".csv", ".tsv", ".xml", ".yaml", ".yml"}


def _is_translatable(text: str) -> bool:
    s = text.strip()
    if not s:
        return False
    if not any(c.isalnum() or c.isalpha() or ord(c) > 0x2E80 for c in s):
        return False
    return True


def _has_unitypy() -> bool:
    try:
        import UnityPy  # noqa: F401
        return True
    except ImportError:
        return False


class UnityExtractor(BaseExtractor):
    """Unity 提取器（文本资源模式 + 可选 UnityPy 模式）。"""

    def __init__(self, decoded_dir: Path, use_unitypy: bool = True):
        super().__init__(decoded_dir)
        self.use_unitypy = use_unitypy and _has_unitypy()
        self._text_units: list[TextUnit] = []

    def extract(self) -> list[TextUnit]:
        units: list[TextUnit] = []

        # --- 模式 1：纯文本资源 ---
        text_files = [p for p in iter_files(self.decoded_dir)
                      if p.suffix.lower() in TEXT_SUFFIXES]
        # 过滤掉明显的配置/代码文件
        skip_names = {"package.json", "manifest.json", "ProjectVersion.txt",
                      "boot.config", "app.info"}
        for path in text_files:
            if path.name in skip_names:
                continue
            rel = str(path.relative_to(self.decoded_dir)).replace("\\", "/")
            suf = path.suffix.lower()
            try:
                if suf == ".json":
                    units.extend(self._extract_json(path, rel))
                elif suf in (".csv", ".tsv"):
                    units.extend(self._extract_csv(path, rel))
                else:
                    units.extend(self._extract_plain(path, rel))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, csv.Error):
                continue

        # --- 模式 2：UnityPy 解析二进制资源 ---
        if self.use_unitypy:
            units.extend(self._extract_binary())
        else:
            assets = [p for p in iter_files(self.decoded_dir)
                      if p.suffix.lower() == ".assets"]
            if assets:
                print(f"[提示] 发现 {len(assets)} 个 .assets 文件，但未安装 UnityPy，"
                      f"跳过二进制资源解析。")
                print("       安装方法: pip install UnityPy")

        self._text_units = units
        return units

    def _mk(self, rel: str, loc: dict, text: str, kind: TextKind) -> TextUnit:
        prot, frags = protect(text)
        return TextUnit(
            uid=TextUnit.make_uid(rel, loc, text),
            source_file=rel, location=loc, original=text, kind=kind,
            protected=frags, extra={"protected_form": prot},
        )

    def _extract_plain(self, path: Path, rel: str) -> list[TextUnit]:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        # 整文件就是一个大文本块的情况（对话脚本）
        if len(lines) > 1:
            units = []
            for i, line in enumerate(lines, 1):
                if _is_translatable(line):
                    units.append(self._mk(rel, {"line": i, "form": "line"}, line.strip(),
                                          TextKind.DIALOGUE))
            return units
        if _is_translatable(text):
            return [self._mk(rel, {"form": "whole"}, text.strip(), TextKind.OTHER)]
        return []

    def _extract_json(self, path: Path, rel: str) -> list[TextUnit]:
        data = json.loads(path.read_text(encoding="utf-8"))
        units: list[TextUnit] = []

        def walk(node, key_path: list):
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, key_path + [k])
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, key_path + [i])
            elif isinstance(node, str) and _is_translatable(node):
                units.append(self._mk(rel, {"key_path": key_path, "form": "json"},
                                      node, TextKind.OTHER))

        walk(data, [])
        return units

    def _extract_csv(self, path: Path, rel: str) -> list[TextUnit]:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",\t")
        except csv.Error:
            dialect = csv.excel
            if path.suffix.lower() == ".tsv":
                dialect = csv.excel_tab
        reader = csv.reader(io.StringIO(raw), dialect)
        units: list[TextUnit] = []
        for row_i, row in enumerate(reader):
            for col_i, cell in enumerate(row):
                if _is_translatable(cell):
                    units.append(self._mk(
                        rel, {"row": row_i, "col": col_i, "form": "csv"},
                        cell, TextKind.OTHER))
        return units

    def _extract_binary(self) -> list[TextUnit]:
        """用 UnityPy 从 .assets/.bundle 中提取 TextAsset 与 MonoBehaviour 文本。"""
        units: list[TextUnit] = []
        try:
            import UnityPy
        except ImportError:
            return units

        targets = [p for p in iter_files(self.decoded_dir)
                   if p.suffix.lower() in (".assets", ".bundle", ".unity3d")
                   or ".assets" in p.name]
        for path in targets:
            rel = str(path.relative_to(self.decoded_dir)).replace("\\", "/")
            try:
                env = UnityPy.load(str(path))
            except Exception:
                continue
            for obj in env.objects:
                if obj.type.name != "TextAsset":
                    continue
                try:
                    data = obj.read()
                    name = getattr(data, "m_Name", "TextAsset")
                    script = bytes(data.m_Script)
                    try:
                        text = script.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                except Exception:
                    continue
                if not _is_translatable(text):
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if _is_translatable(line):
                        units.append(self._mk(
                            rel, {"asset": name, "line": i, "form": "unitypy"},
                            line.strip(), TextKind.OTHER))
        return units

    def write_back(self, project: Project, out_dir: Path) -> dict:
        """回填：文本资源文件直接改；二进制资源回写需 UnityPy（此处仅处理文本）。"""
        from ..core.protect import restore

        out_dir = Path(out_dir)
        by_file: dict[str, list[TextUnit]] = {}
        for u in project.units:
            if u.translated:
                by_file.setdefault(u.source_file, []).append(u)

        written = 0
        replaced = 0
        skipped_binary = 0
        for rel, us in by_file.items():
            src = self.decoded_dir / rel
            if not src.exists():
                continue
            suf = src.suffix.lower()
            if "form" in us[0].location and us[0].location["form"] == "unitypy":
                skipped_binary += 1
                continue  # 二进制回写需专门处理，见 README

            if suf == ".json":
                replaced += self._write_json(src, out_dir / rel, us)
            elif suf in (".csv", ".tsv"):
                replaced += self._write_csv(src, out_dir / rel, us)
            else:
                replaced += self._write_plain(src, out_dir / rel, us)
            written += 1

        return {"files_written": written, "segments_replaced": replaced,
                "binary_skipped": skipped_binary}

    def _write_plain(self, src: Path, dst: Path, us: list[TextUnit]) -> int:
        from ..core.protect import restore
        text = src.read_text(encoding="utf-8", errors="replace")
        n = 0
        for u in us:
            val = restore(u.translated, u.protected)
            # 用原文精确替换首次出现
            if u.original in text:
                text = text.replace(u.original, val, 1)
                n += 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")
        return n

    def _write_json(self, src: Path, dst: Path, us: list[TextUnit]) -> int:
        from ..core.protect import restore
        data = json.loads(src.read_text(encoding="utf-8"))
        n = 0
        for u in us:
            kp = u.location.get("key_path", [])
            node = data
            try:
                for k in kp[:-1]:
                    node = node[k]
                node[kp[-1]] = restore(u.translated, u.protected)
                n += 1
            except (KeyError, IndexError, TypeError):
                continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return n

    def _write_csv(self, src: Path, dst: Path, us: list[TextUnit]) -> int:
        from ..core.protect import restore
        raw = src.read_text(encoding="utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",\t")
        except csv.Error:
            dialect = csv.excel_tab if src.suffix.lower() == ".tsv" else csv.excel
        rows = [r for r in csv.reader(io.StringIO(raw), dialect)]
        n = 0
        for u in us:
            r, c = u.location.get("row"), u.location.get("col")
            try:
                rows[r][c] = restore(u.translated, u.protected)
                n += 1
            except (IndexError, TypeError):
                continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        with open(dst, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f, dialect).writerows(rows)
        return n
