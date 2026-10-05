"""Unity 文本提取器。

Unity 游戏的文本存储有多种形态，本提取器覆盖：
1. 独立的文本资源文件（.txt / .json / .csv）—— 最简单，直接当文本处理。
2. Localization 插件产物（I2 Localization 的 .csv，或 Unity Localization 的 .json）。
3. ``.assets`` / ``.bundle`` 里的 ``TextAsset`` —— 需要 UnityPy 解析。

   这是 Unity 游戏文本最常见的藏身处（如 Night in the Woods 的对话全部
   在 ``sharedassets0.assets`` 的 41 个 TextAsset 里），本提取器支持：

   - **精确解析 TextAsset 内的可翻译行**，两种主流形态：
     · Yarn Spinner 对话脚本（``Mae: 台词`` 行，跳过 title/->/<<...>>/[[...]]）
     · CSV 式 ``*_lines``（``LineCode,LineText,Comment`` 三列）
   - **回填**：译文写回 TextAsset 的 ``m_Script``，再用 UnityPy 重新序列化
     ``.assets``，产出真正的汉化资源包（而非只读不改）。

依赖 UnityPy（版本 1.10.x，兼容 Python 3.8；1.25.x 需 3.9+）。
未安装时退化为只处理纯文本资源并给出提示。
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from ..core.models import Project, TextKind, TextUnit
from ..core.protect import protect, restore
from ..core.scan import iter_files
from .base import BaseExtractor, wb_stats, write_back_file

TEXT_SUFFIXES = {".txt", ".json", ".csv", ".tsv", ".xml", ".yaml", ".yml"}

# Yarn Spinner 对话行：`说话人: 台词`（可带缩进）。说话人后紧跟冒号+空格。
YARN_LINE_RE = re.compile(r"^(\s*)([A-Za-z][\w ]*):\s*(.+)$")
# 需要整体跳过的 Yarn 行类型：元数据 / 选项 / 逻辑 / 跳转 / 分隔
YARN_SKIP_PREFIXES = ("title:", "tags:", "colorID:", "position:", "->", "<<", "[[", "===")
YARN_SKIP_EXACT = {"---", ""}

# *_lines 的 CSV 表头
LINES_HEADER = "LineCode"

# 吉他小游戏谱面数据资产（Bars/Notes/AnimationCues 是音符时间戳，不是文本）
# Lyrics 里虽有歌词文本，但混着时间戳行，需按行过滤（见 _is_lyric_line）
SPECTRAL_ASSET_SUFFIXES = ("Bars", "Notes", "AnimationCues", "Cues")
# 纯数字/版本号这类非文本资产名
NON_TEXT_ASSET_NAMES = {"buildnumber", "app.info"}
# 配置类资产名前缀（FMOD 事件清单等）
NON_TEXT_ASSET_PREFIXES = ("Events-",)
# 谱面/时间戳行形如 "00.000|8|0" 或 "00.000|Alcuni seguono|0"
TIMECODE_LINE_RE = re.compile(r"^\s*\d{1,2}:\d{2}\.\d{3}\s*\|")


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
        # 过滤掉明显的配置/代码文件 + 许可证/法律文本
        skip_names = {"package.json", "manifest.json", "ProjectVersion.txt",
                      "boot.config", "app.info", "Legal.txt", "license.txt",
                      "LICENSE", "COPYING", "NOTICE", "README.txt"}
        skip_parts = ("/Legal", "/Licenses", "/legal")
        for path in text_files:
            if path.name in skip_names:
                continue
            rel = str(path.relative_to(self.decoded_dir)).replace("\\", "/")
            if any(part in rel for part in skip_parts):
                continue
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

    def _mk(self, rel: str, loc: dict, text: str, kind: TextKind,
            context: dict | None = None) -> TextUnit:
        prot, frags = protect(text)
        return TextUnit(
            uid=TextUnit.make_uid(rel, loc, text),
            source_file=rel, location=loc, original=text, kind=kind,
            context=context or {},
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
        """用 UnityPy 从 .assets/.bundle 提取 TextAsset 里的可翻译文本。

        对每个 TextAsset，根据其内容形态（.yarn 或 *_lines）精确解析，
        只提取真正该翻的「台词/正文」行，跳过结构标记与元数据。
        """
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
                if not text.strip():
                    continue
                # 记录该 TextAsset 的原始字节，供回填时精确替换
                units.extend(self._extract_textasset(rel, name, text, obj))
        return units

    def _extract_textasset(self, rel: str, name: str, text: str,
                           obj) -> list[TextUnit]:
        """解析单个 TextAsset 内容，产出 TextUnit。"""
        units: list[TextUnit] = []
        loc_base = {"asset": name, "form": "unitypy"}
        if name.endswith(".yarn") or "\n---\n" in text:
            units.extend(self._extract_yarn(rel, loc_base, text))
        elif text.lstrip().startswith(LINES_HEADER):
            units.extend(self._extract_lines_csv(rel, loc_base, text))
        elif (name.endswith(SPECTRAL_ASSET_SUFFIXES)
              or name in NON_TEXT_ASSET_NAMES
              or name.startswith(NON_TEXT_ASSET_PREFIXES)):
            # 纯音符谱面 / 版本号 / 事件清单，不含可翻译文本，跳过
            pass
        else:
            # 兜底：整文件逐行（含 Lyrics 等混排文本，跳过时间戳行）
            for i, line in enumerate(text.splitlines(), 1):
                s = line.strip()
                if TIMECODE_LINE_RE.match(s):
                    continue
                if _is_translatable(s):
                    units.append(self._mk(
                        rel, {**loc_base, "line": i, "raw": True},
                        s, TextKind.OTHER))
        return units

    def _extract_yarn(self, rel: str, loc_base: dict,
                      text: str) -> list[TextUnit]:
        """解析 Yarn Spinner 脚本：只提取 `说话人: 台词` 行。

        跳过：title/tags/colorID/position 元数据、-> 选项、<<逻辑>>、
        [[跳转]]、=== 分隔、空行。台词行尾的 #line:xxxx 由 protect 保护。
        """
        units: list[TextUnit] = []
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped in YARN_SKIP_EXACT or not stripped:
                continue
            if stripped.startswith(YARN_SKIP_PREFIXES):
                continue
            m = YARN_LINE_RE.match(line)
            if not m:
                continue
            body = m.group(3).strip()
            if not _is_translatable(body):
                continue
            speaker = m.group(2).strip()
            units.append(self._mk(
                rel, {**loc_base, "line": i, "yarn": True, "speaker": speaker},
                body, TextKind.DIALOGUE,
                context={"speaker": speaker} if speaker else None))
        return units

    def _extract_lines_csv(self, rel: str, loc_base: dict,
                           text: str) -> list[TextUnit]:
        """解析 *_lines 的 CSV（LineCode,LineText,Comment）。

        LineText 里常嵌 ``说话人: 台词`` 前缀（如 ``Mae: =_=``），
        前缀本身可能是人名（保留），台词主体才翻译。
        """
        units: list[TextUnit] = []
        raw = text.replace("\r\n", "\n")
        lines = raw.split("\n")
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if not s or s.startswith(LINES_HEADER):
                continue
            # 结构：line:code,LineText,Comment（LineText 可能带引号）
            try:
                row = next(csv.reader([s]))
            except (csv.Error, StopIteration):
                continue
            if len(row) < 2:
                continue
            code, text_col = row[0], row[1]
            if not code.startswith("line:"):
                continue
            body = text_col.strip()
            if not _is_translatable(body):
                continue
            # 剥离「说话人: 」前缀，前缀作为 context（人名通常不必翻，
            # 但台词主体要翻）。前缀用 protect 保护，避免模型乱译人名。
            speaker = ""
            m = re.match(r"^([A-Za-z][\w ]*):\s*(.+)$", body)
            if m:
                speaker = m.group(1).strip()
                body = m.group(2).strip()
                if not _is_translatable(body):
                    continue
            units.append(self._mk(
                rel, {**loc_base, "row": i, "lines_csv": True,
                      "speaker": speaker, "code": code},
                body, TextKind.DIALOGUE,
                context={"speaker": speaker} if speaker else None))
        return units

    def write_back(self, project: Project, out_dir: Path) -> dict:
        """回填：文本资源文件直接改；TextAsset 用 UnityPy 写回 .assets。

        二进制回填流程（真正产出汉化资源，而非跳过）：
        1. 按 source_file（.assets 路径）分组，收集已译的 TextAsset 单元；
        2. 对每个 TextAsset，按行号/CSV 行把译文替换回文本；
        3. 用 UnityPy 把改后的 TextAsset 重新序列化，写出新的 .assets 文件。
        """
        out_dir = Path(out_dir)
        by_file: dict[str, list[TextUnit]] = {}
        for u in project.units:
            if u.translated:
                by_file.setdefault(u.source_file, []).append(u)

        written = 0
        unchanged = 0
        replaced = 0
        binary_replaced = 0
        binary_files = 0
        changed: list[str] = []
        for rel, us in by_file.items():
            src = self.decoded_dir / rel
            if not src.exists():
                continue
            is_binary = any(u.location.get("form") == "unitypy" for u in us)
            if is_binary:
                r, differs = self._write_binary_assets(src, out_dir / rel, us)
                if r:
                    replaced += r
                    binary_replaced += r
                    binary_files += 1
                    written += 1
                    if differs:
                        changed.append(rel)
                    else:
                        unchanged += 1
                continue

            suf = src.suffix.lower()
            if suf == ".json":
                n, differs = self._write_json(src, out_dir / rel, us)
            elif suf in (".csv", ".tsv"):
                n, differs = self._write_csv(src, out_dir / rel, us)
            else:
                n, differs = self._write_plain(src, out_dir / rel, us)
            replaced += n
            if n:
                written += 1
                if differs:
                    changed.append(rel)
                else:
                    unchanged += 1

        return wb_stats(files_written=written, replaced=replaced,
                        unchanged=unchanged, changed=changed,
                        binary_files=binary_files,
                        binary_replaced=binary_replaced)

    def _write_binary_assets(self, src: Path, dst: Path,
                             us: list[TextUnit]) -> tuple[int, bool]:
        """把译文写回 .assets 里的 TextAsset，重新序列化整包。

        按 TextAsset 名分组 → 重建每个 TextAsset 的文本 → 写回 m_Script
        → UnityPy 重新保存整个 .assets 文件。

        Returns:
            (替换条数, 内容是否与原 .assets 不同)。替换条数为 0 时不落盘。
        """
        try:
            import UnityPy
        except ImportError:
            return 0, False

        try:
            env = UnityPy.load(str(src))
        except Exception:
            return 0, False

        # 按 (asset 名) 分组，组内再按行/CSV 行定位
        by_asset: dict[str, list[TextUnit]] = {}
        for u in us:
            by_asset.setdefault(u.location.get("asset", ""), []).append(u)

        n = 0
        for obj in env.objects:
            if obj.type.name != "TextAsset":
                continue
            try:
                data = obj.read()
                name = getattr(data, "m_Name", "")
            except Exception:
                continue
            group = by_asset.get(name)
            if not group:
                continue
            try:
                text = bytes(data.m_Script).decode("utf-8")
            except (UnicodeDecodeError, AttributeError):
                continue
            new_text, cnt = self._apply_textasset(text, group)
            if cnt:
                # 写回 m_Script 并触发序列化（TextAsset.save() 会 set_raw_data）；
                # 少了这一步 env.file.save() 会原样吐回旧字节，改动静默丢失。
                data.m_Script = new_text.encode("utf-8")
                data.save()
                n += cnt

        if not n:
            return 0, False
        try:
            payload = env.file.save()
        except Exception:
            return n, False
        if isinstance(payload, dict):      # 多文件 AssetBundle 的返回值
            payload = next(iter(payload.values()), b"")
        if not isinstance(payload, (bytes, bytearray)):
            return n, False
        _wrote, differs = write_back_file(src, dst, bytes(payload))
        return n, differs

    def _apply_textasset(self, text: str, group: list[TextUnit]) -> tuple[str, int]:
        """把一组译文替换回 TextAsset 的文本，返回 (新文本, 替换条数)。"""
        lines = text.splitlines()
        # 原样保留换行风格（CSV 多为 CRLF，改成 LF 属于无谓的额外改动）
        nl = "\r\n" if "\r\n" in text else "\n"
        n = 0
        # 按行号分组
        by_line = {}
        for u in group:
            by_line.setdefault(u.location.get("line", u.location.get("row")), []).append(u)

        for lineno, us in sorted(by_line.items(), key=lambda kv: kv[0] if kv[0] else 0):
            if not isinstance(lineno, int) or lineno < 1 or lineno > len(lines):
                continue
            line = lines[lineno - 1]
            for u in us:
                val = restore(u.translated, u.protected)
                if u.location.get("lines_csv"):
                    # CSV 行：替换「说话人: 台词」里的台词部分（说话人前缀保留）
                    line, ok = _replace_csv_line_text(line, u, val)
                    if ok:
                        n += 1
                elif u.location.get("yarn"):
                    # Yarn 行：`说话人: 台词 #line:xxx`，只替换台词
                    line, ok = _replace_yarn_line_text(line, u, val)
                    if ok:
                        n += 1
                elif u.location.get("raw"):
                    if u.original in line:
                        line = line.replace(u.original, val, 1)
                        n += 1
                lines[lineno - 1] = line
        return nl.join(lines), n

    def _write_plain(self, src: Path, dst: Path, us: list[TextUnit]) -> tuple[int, bool]:
        from ..core.protect import restore
        text = src.read_text(encoding="utf-8", errors="replace")
        n = 0
        for u in us:
            val = restore(u.translated, u.protected)
            # 用原文精确替换首次出现
            if u.original in text:
                text = text.replace(u.original, val, 1)
                n += 1
        if not n:
            return 0, False
        return n, write_back_file(src, dst, text.encode("utf-8"))[1]

    def _write_json(self, src: Path, dst: Path, us: list[TextUnit]) -> tuple[int, bool]:
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
        if not n:
            return 0, False
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        return n, write_back_file(src, dst, payload)[1]

    def _write_csv(self, src: Path, dst: Path, us: list[TextUnit]) -> tuple[int, bool]:
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
        if not n:
            return 0, False
        buf = io.StringIO(newline="")
        csv.writer(buf, dialect).writerows(rows)
        payload = buf.getvalue().encode("utf-8-sig")
        return n, write_back_file(src, dst, payload)[1]


def _replace_yarn_line_text(line: str, u: TextUnit, val: str) -> tuple[str, bool]:
    """替换 Yarn 行 `说话人: 台词 #line:xxx` 里的台词部分。

    只替换冒号后的正文，保留说话人前缀与缩进。``val`` 已经过 protect/restore
    还原，本身含完整的正文（含 #line 标签），故直接整体替换、不再追加标签。
    """
    m = YARN_LINE_RE.match(line)
    if not m:
        return line, False
    speaker_part = m.group(2)
    rest = m.group(3)
    # 校验正文一致才替换：去掉行尾 #line 标签后与原文比对
    body = rest.rstrip()
    mm = re.search(r"(#line:[0-9a-fA-F]+)\s*$", body)
    if mm:
        body = body[:mm.start()].rstrip()
    orig_body = u.original
    om = re.search(r"(#line:[0-9a-fA-F]+)\s*$", orig_body)
    if om:
        orig_body = orig_body[:om.start()].rstrip()
    if body != orig_body.strip():
        return line, False
    return m.group(1) + speaker_part + ": " + val, True


def _replace_csv_line_text(line: str, u: TextUnit, val: str) -> tuple[str, bool]:
    """替换 *_lines 的 CSV 行 `line:code,说话人: 台词,注释` 里的台词。

    台词可能带双引号包裹（含逗号时）。只改台词，保留 line code、说话人前缀、注释。
    """
    try:
        row = next(csv.reader([line]))
    except (csv.Error, StopIteration):
        return line, False
    if len(row) < 2:
        return line, False
    text_col = row[1]
    speaker = u.location.get("speaker", "")
    # 重建台词列：有说话人则保留前缀
    if speaker:
        new_text_col = f"{speaker}: {val}"
    else:
        new_text_col = val
    row[1] = new_text_col
    # 重新序列化为 CSV 行（与原文风格尽量一致：quote 由 csv 自动处理）
    out = io.StringIO()
    csv.writer(out, lineterminator="").writerow(row)
    return out.getvalue(), True
