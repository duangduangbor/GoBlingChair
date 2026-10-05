# -*- coding: utf-8 -*-
"""通用明文适配器单元测试：各格式的提取与精确回填。

用临时目录现造各种文本文件，走「提取 → 回填 → 读回」闭环，
重点验证**格式没被破坏**（换行风格、BOM、CSV 引号、JSON 缩进、.ass 特效码）。
"""
from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from gametl.core.detect import detect_engine
from gametl.core.models import EngineType, Project
from gametl.extractors.plaintext import PlainTextExtractor


def _run(ex, work: Path, trans="【译】"):
    units = ex.extract()
    for u in units:
        u.translated = trans + u.original
    proj = Project(root=work, engine=EngineType.PLAINTEXT, units=units)
    out = work / "_out"
    stats = ex.write_back(proj, out)
    return units, stats, out


def main() -> int:
    recs = []

    def rec(ok, name):
        recs.append((bool(ok), name))

    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "game"
        (work / "data").mkdir(parents=True)
        (work / "game.exe").write_bytes(b"MZ" + b"\x00" * 4096)

        # ---- 1. 纯文本行 ----
        story = ("Hello there.\n\n"
                 "12345\n"
                 "   Indented line\n"
                 "Second line of dialogue that is long enough to matter.\n"
                 "The road ahead is long and full of danger, but you look ready.\n"
                 "Take this sword; you will need it more than I do today.\n"
                 "May the light guide your steps through the dark forest.\n"
                 "Return here when your journey is done, and tell me your tale.\n"
                 "There is much to be done and precious little time to do it.\n"
                 "Farewell, and good luck to you, brave adventurer of the north.\n"
                 "Remember: the mountain pass is closed in winter, so go south.\n"
                 "Once you reach the river, follow it east until you see towers.\n")
        (work / "data" / "story.txt").write_text(story, encoding="utf-8")

        # ---- 2. CSV（含带逗号的引号单元格）----
        (work / "data" / "items.csv").write_text(
            "id,name,desc\n"
            '1,Sword,"A blade, sharp and true."\n'
            '2,Shield,"Light, but weak"\n', encoding="utf-8")

        # ---- 3. JSON（缩进要保住）----
        (work / "data" / "ui.json").write_text(
            json.dumps({"menu": {"start": "Start Game"},
                        "hint": "Use arrows."}, ensure_ascii=False, indent=2),
            encoding="utf-8")

        # ---- 4. SRT ----
        (work / "data" / "sub.srt").write_text(
            "1\n00:00:01,000 --> 00:00:03,000\nWhere did everybody go?\n\n"
            "2\n00:00:03,500 --> 00:00:05,000\nI think we are lost.\n",
            encoding="utf-8")

        # ---- 5. ASS（特效码必须留在原位）----
        (work / "data" / "sub.ass").write_text(
            "[Events]\nFormat: Layer, Start, End, Style, Name, "
            "MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,"
            "{\\an8}Watch out!\n", encoding="utf-8")

        # ---- 6. XML ----
        (work / "data" / "strings.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<resources>\n  <string name="app">My Great Game</string>\n'
            '</resources>\n', encoding="utf-8")

        # ---- 7. YAML ----
        (work / "data" / "labels.yaml").write_text(
            "menu:\n  play: Play Now\n  empty: ''\n", encoding="utf-8")

        # ---- 8. PO ----
        (work / "data" / "locale.po").write_text(
            'msgid "Continue"\nmsgstr "Continue"\n', encoding="utf-8")

        # ---- 9. CRLF + BOM 的 txt ----
        (work / "data" / "crlf.txt").write_bytes(
            "\ufeffLine one is English.\r\nLine two is also English.\r\n"
            .encode("utf-8"))

        # ---- 10. 说明书应当被跳过 ----
        (work / "readme.txt").write_text("Not game content.\n" * 200,
                                         encoding="utf-8")

        eng, ev = detect_engine(work)
        rec(eng == EngineType.PLAINTEXT, f"识别为 plaintext（实际 {eng.value}）")

        ex = PlainTextExtractor(work)
        units, stats, out = _run(ex, work)

        rels = {u.source_file for u in units}
        rec("data/story.txt" in rels and "data/items.csv" in rels
            and "data/ui.json" in rels, "各类文件都提取到")
        rec("readme.txt" not in rels, "readme.txt 被跳过")
        rec(all(u.original != "12345" for u in units), "纯数字行被跳过")
        rec(any(u.original == "Indented line" for u in units),
            "缩进行取正文（不含缩进）")

        # ---- 回填后逐项校验格式 ----
        txt = (out / "data" / "story.txt").read_text(encoding="utf-8")
        rec("\n\n" in txt, "txt：空行保留")
        rec("12345" in txt, "txt：数字行原样")
        rec("【译】Hello there." in txt, "txt：译文落地")
        rec(txt.startswith("【译】Hello there."), "txt：首行正确")

        csv_txt = (out / "data" / "items.csv").read_text(encoding="utf-8")
        rows = list(csv.reader(io.StringIO(csv_txt)))
        rec(len(rows[0]) == 3, "csv：列数（结构）不变")
        rec(rows[1][0] == "1", "csv：纯数字单元格不动")
        rec(rows[1][2] == "【译】A blade, sharp and true.",
            f"csv：带逗号的引号单元格还原正确（{rows[1][2]!r}）")
        rec(rows[2][2] == "【译】Light, but weak",
            f"csv：第二个引号单元格正确（{rows[2][2]!r}）")

        json_txt = (out / "data" / "ui.json").read_text(encoding="utf-8")
        rec(json_txt.count("\n") >= 5 and '  "menu"' in json_txt,
            "json：缩进保留")
        rec(json.loads(json_txt)["menu"]["start"] == "【译】Start Game",
            "json：译文落地")

        srt = (out / "data" / "sub.srt").read_text(encoding="utf-8")
        rec("00:00:01,000 --> 00:00:03,000" in srt, "srt：时间轴未被动")
        rec("1\n00:00:01,000" in srt.replace("\r\n", "\n"), "srt：序号未被动")
        rec("【译】Where did everybody go?" in srt, "srt：台词已翻译")

        ass = (out / "data" / "sub.ass").read_text(encoding="utf-8")
        rec("{\\an8}【译】Watch out!" in ass, "ass：特效码留在原位")
        rec(ass.splitlines()[0] == "[Events]", "ass：段头未被动")

        xml = (out / "data" / "strings.xml").read_text(encoding="utf-8")
        rec('name="app"' in xml and "【译】My Great Game" in xml,
            "xml：属性与文本都对")

        yml = (out / "data" / "labels.yaml").read_text(encoding="utf-8")
        rec("  play: 【译】Play Now" in yml, "yaml：键不动、值翻译")
        rec("  empty: ''" in yml, "yaml：空值原样")

        po = (out / "data" / "locale.po").read_text(encoding="utf-8")
        rec('msgstr "【译】Continue"' in po and 'msgid "Continue"' in po,
            "po：只改 msgstr，msgid 不动")

        raw = (out / "data" / "crlf.txt").read_bytes()
        rec(raw.startswith(b"\xef\xbb\xbf"), "CRLF 文件：BOM 保留")
        rec(raw.count(b"\r\n") == raw.count(b"\n") and b"\r\n" in raw,
            "CRLF 文件：换行风格保留（每个 \\n 前都有 \\r）")
        rec("【译】Line one is English.".encode() in raw, "CRLF 文件：译文落地")

        # ---- 识别门槛：目录里只有 exe + readme，不该认成游戏 ----
        bare = Path(td) / "bare"
        bare.mkdir()
        (bare / "x.exe").write_bytes(b"MZ")
        (bare / "readme.txt").write_text("hello\n" * 200, encoding="utf-8")
        eng2, _ = detect_engine(bare)
        rec(eng2 == EngineType.UNKNOWN, f"只有 exe+readme → unknown（实际 {eng2.value}）")

    ok = sum(1 for o, _ in recs if o)
    total = len(recs)
    print("=" * 60)
    print(f"通过 {ok} / {total}")
    for o, name in recs:
        if not o:
            print(f"  失败: {name}")
    print("=" * 60)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
