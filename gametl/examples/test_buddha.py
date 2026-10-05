# -*- coding: utf-8 -*-
"""Buddha（Double Fine dfpf）提取器单元测试。

不依赖真实游戏：用合成数据现造一个 v5 的 ``.~h``/``.~p`` 包，
走完整的「解析 → 提取 → 回填 → 重新解包」闭环，并验证
就地替换 / 整包重建两条路都不会伤到其它资源。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from gametl.core.dfpf import (
    DfpfPack, build_synthetic_v5, find_packs,
)
from gametl.core.models import Project
from gametl.extractors.buddha import (
    BuddhaExtractor, emit_value, escape, parse_entries, rebuild, unescape,
)

ST_TEXT = (
    'StringTable{LineCodeData={'
    'TEST001=LineCodeData{Text="Hello world";VolumeDB=0;Character=Narrator;SoundCue=;};'
    'TEST002=LineCodeData{Text=Bye;VolumeDB=0;Character=Text;SoundCue=;};'
    'TEST003=LineCodeData{Text="Press /BUTTON_DPadUp/ to jump";'
    'VolumeDB=0;Character=Text;SoundCue=;};'
    'CMAP001TEXT=LineCodeData{Text="A B C";VolumeDB=0;Character=Text;SoundCue=;};'
    '};}'
)


def _make_pack(tmp: Path):
    stb = ST_TEXT.encode("utf-8")
    blob = bytes(range(256)) * 8
    build_synthetic_v5(tmp / "Test_Stuff.~h", tmp / "Test_Stuff.~p", [
        ("stringtable/test_enus", stb, 0, True),
        ("data/thing", blob, 1, False),
    ])
    return stb, blob


def main() -> int:
    recs = []

    def rec(ok, name):
        recs.append((bool(ok), name))

    # ---- 1. DSL 值转义往返 ----
    for raw, want in (("Hello world", "Hello world"),
                      ('"Hello world"', "Hello world"),
                      (r'"a \"b\" c"', 'a "b" c'),
                      (r'"tab\there"', "tab\there"),
                      ("Bye", "Bye")):
        rec(unescape(raw) == want, f"unescape {raw!r}")
    rec(escape('a "b" \\ c') == 'a \\"b\\" \\\\ c', "escape 反斜杠与引号")
    rec(emit_value("Bye", False) == "Bye", "纯标识符保持裸写")
    rec(emit_value("再见", False) == '"再见"', "中文自动加引号")
    rec(emit_value('a;b', True) == '"a;b"', "含分号保留引号")
    rec(emit_value("Teddy", False) == "Teddy", "名字不加多余引号")

    # ---- 2. StringTable 解析 ----
    ents = parse_entries(ST_TEXT)
    rec(len(ents) == 4, f"解析出 4 个条目（实际 {len(ents)}）")
    rec(ents[0]["key"] == "TEST001" and ents[0]["value"] == "Hello world",
        "键与正文解析正确")
    rec(ents[1]["value"] == "Bye" and ents[1]["char"] == "Text",
        "裸值与 Character 解析正确")
    rec(ents[0]["char"] == "Narrator", "引号值后的 Character 解析正确")
    rec(ST_TEXT[ents[2]["start"]:ents[2]["end"]]
        == '"Press /BUTTON_DPadUp/ to jump"', "Text 区间定位精确")

    # ---- 3. 无损重排 ----
    same, n = rebuild(ST_TEXT, ents, {i: e["raw"] for i, e in enumerate(ents)})
    rec(same == ST_TEXT and n == 4, "原值回填 = 逐字节无损往返")

    # ---- 4. 精确替换只动目标一段 ----
    new, n = rebuild(ST_TEXT, ents, {0: '"你好，世界"'})
    rec(n == 1 and new.startswith('StringTable{LineCodeData={TEST001='
                                  'LineCodeData{Text="你好，世界";'),
        "精确替换：只改第一条")
    rec(new.count("Hello world") == 0 and 'Text=Bye;' in new, "其余条目原样保留")

    # ---- 5. 包解析 ----
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        stb, blob = _make_pack(tmp)
        pk = DfpfPack.open(tmp / "Test_Stuff.~h")
        rec(pk.version == 5, "版本识别 = 5")
        rec(len(pk.entries) == 2, f"解析出 2 个资源（实际 {len(pk.entries)}）")
        e0 = pk.find("stringtable/test_enus")
        e1 = pk.find("data/thing")
        rec(e0 is not None and e1 is not None, "两个资源都找得到")
        rec(e0 is not None and e0.compressed and e0.usize == len(stb),
            "压缩资源：usize / 压缩标记正确")
        rec(e1 is not None and not e1.compressed and e1.usize == len(blob),
            "未压缩资源：usize 正确")
        rec(e0.type_index == 0 and pk.type_names[0] == "StringTable",
            "类型表解析正确（StringTable 在下标 0）")
        rec(pk.read(e0) == stb and pk.read(e1) == blob, "两个资源都能正确取出")

        rec(len(find_packs(tmp)) == 1, "find_packs 能扫到这个包")

        # ---- 6. 提取（含 CMAP 过滤与按键宏保护）----
        ex = BuddhaExtractor(tmp)
        units = ex.extract()
        keys = [u.location["key"] for u in units]
        rec(len(units) == 3, f"提取 3 条（跳过 CMAP）（实际 {len(units)}）")
        rec("CMAP001TEXT" not in keys, "字体覆盖表 CMAP 被跳过")
        u3 = [u for u in units if u.location["key"] == "TEST003"][0]
        rec(u3.protected and "/BUTTON_DPadUp/" in u3.protected,
            "按键宏 /BUTTON_DPadUp/ 被保护")
        rec(u3.original == "Press /BUTTON_DPadUp/ to jump", "原文保留宏本身")

        # ---- 7. 回填（译文更短 → 就地替换）----
        for u in units:
            u.translated = "短"
        from gametl.core.models import EngineType
        proj = Project(root=tmp, engine=EngineType.BUDDHA, units=units)
        out = tmp / "out1"
        stats = ex.write_back(proj, out)
        rec(stats["fields_replaced"] == 3,
            f"回填 3 处（实际 {stats['fields_replaced']}）")
        rec(sorted(stats["changed"]) == ["Test_Stuff.~h", "Test_Stuff.~p"],
            f"索引与数据都进了补丁清单（{stats['changed']}）")

        pk2 = DfpfPack.open(out / "Test_Stuff.~h")
        rec(pk2.p_path.stat().st_size == pk.p_path.stat().st_size,
            "就地替换：.~p 大小不变")
        idx_diff = [i for i, (a, b) in
                    enumerate(zip((tmp / "Test_Stuff.~h").read_bytes(),
                                  (out / "Test_Stuff.~h").read_bytes()))
                    if a != b]
        rec(len(idx_diff) <= 6,
            f"就地替换：索引只动了『解压后大小』那几位（差异 {len(idx_diff)} 字节）")
        t2 = pk2.read(pk2.find("stringtable/test_enus")).decode("utf-8")
        rec("短" in t2 and "Hello world" not in t2, "译文落地")
        rec(pk2.read(pk2.find("data/thing")) == blob,
            "就地替换：其它资源逐字节不变")

        # ---- 8. 回填（译文超长 → 整包重建）----
        long_txt = "这是一段相当长的译文" * 40
        for u in units:
            u.translated = long_txt
        out2 = tmp / "out2"
        ex.write_back(proj, out2)
        pk3 = DfpfPack.open(out2 / "Test_Stuff.~h")
        rec(len(pk3.entries) == 2, "重建后条目数不变")
        rec(pk3.read(pk3.find("data/thing")) == blob,
            "整包重建：其它资源逐字节不变")
        oob = [e.name for e in pk3.entries
               if e.offset + e.size > pk3.p_path.stat().st_size]
        rec(not oob, "整包重建：没有越界的偏移")
        t3 = pk3.read(pk3.find("stringtable/test_enus")).decode("utf-8")
        rec(long_txt in t3, "整包重建：长译文落地")
        rec(pk3.read(pk3.find("data/thing")) == blob, "整包重建：Blob 仍然可读")

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
