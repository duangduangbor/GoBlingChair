# -*- coding: utf-8 -*-
"""v2.6.2 回归：dfpf 回填必须守住两条铁律。

背景（用户实测事故）
--------------------
用 CQ2 翻译包「一键汉化」后，游戏**启动就卡死、窗口都关不掉**。

复盘原版包（14 个 ``.~p``、30 万个资源，无一例外）得到两条硬约束：

1. ``.~p`` 里每个资源的起始位置都按 **2048 字节** 对齐；
2. ``.~h`` 偏移 48 处的 8 字节字段 = **数据区结束位置**（最后一个资源
   结束位置向上取整到 2048），它之后到文件末尾是预分配填充（0x7A）。

旧的「整包重建」两条都破坏了：新数据被压成连续排布（偏移不再对齐），
而索引里那个长度字段压根没更新 —— 游戏按旧长度认定数据区，新资源
直接落在它的视野之外，于是卡死。

这个测试既覆盖合成包（快、可控），也拿本机真实的 CQ2 原版包做结构
体检（慢、但那是出过事的那一档）。
"""
from __future__ import annotations

import shutil
import struct
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from gametl.core.dfpf import (  # noqa: E402
    DATA_END_OFF, SECTOR, DfpfPack, build_synthetic_v5,
)

FAILS: list[str] = []
NOTES: list[str] = []


def check(cond, msg):
    if cond:
        print(f"  [OK] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        FAILS.append(msg)


def _struct_ok(tag: str, h: Path, p: Path, *, expect_data_end=None) -> None:
    """体检一个包：对齐 + 数据区长度 + 可读性。"""
    pk = DfpfPack.open(h)
    ents = [e for e in pk.entries if e.size]
    psz = p.stat().st_size

    bad_off = [e.name for e in ents if e.offset % SECTOR]
    check(not bad_off,
          f"{tag}：{len(ents)} 个资源偏移全按 {SECTOR} 对齐"
          f"（违规 {len(bad_off)}）")

    max_end = max((e.offset + e.size) for e in ents)
    want = -(-max_end // SECTOR) * SECTOR
    check(pk.data_end == want,
          f"{tag}：索引里的数据区结束位置 = {pk.data_end}（应为 {want}）")
    if expect_data_end is not None:
        check(pk.data_end == expect_data_end,
              f"{tag}：数据区结束位置符合预期 {expect_data_end}")

    oob = [e.name for e in ents if e.offset + e.size > psz]
    check(not oob, f"{tag}：没有资源越出 .~p 尾部（越界 {len(oob)}）")

    dups = len(ents) - len({e.offset for e in ents})
    check(dups == 0, f"{tag}：资源起始位置无重叠（重复 {dups}）")

    # 逐个解出来，确认不是"索引自洽但数据是垃圾"
    bad = []
    for e in ents:
        try:
            pk.read(e)
        except Exception as ex:                       # noqa: BLE001
            bad.append(f"{e.name}: {ex}")
    check(not bad, f"{tag}：全部资源都能解出来（失败 {len(bad)}）{bad[:2]}")


ST = ('StringTable{LineCodeData={'
      'A=LineCodeData{Text="Hello world";VolumeDB=0;Character=Narrator;'
      'SoundCue=;};'
      'B=LineCodeData{Text="Press /BUTTON_DPadUp/ to jump";'
      'VolumeDB=0;Character=Text;SoundCue=;};'
      '};}')


def _mk(tmp: Path):
    h = tmp / "T_Stuff.~h"
    p = tmp / "T_Stuff.~p"
    build_synthetic_v5(h, p, [
        ("stringtable/test_enus", ST.encode("utf-8"), 0, True),
        ("data/thing", bytes(range(256)) * 16, 1, False),
        ("data/big", b"x" * 5000, 1, False),
    ])
    return h, p


def test_synthetic():
    print("\n[1] 合成包：原地替换 / 末尾追加 / 往返")
    tmp = Path(tempfile.mkdtemp(prefix="dfpf_syn_"))
    try:
        # ---- 1a) 短内容 → 走原地，数据区长度不动 ----
        h, p = _mk(tmp)
        pk = DfpfPack.open(h)
        orig_p = p.read_bytes()
        blob = bytes(range(256)) * 16
        res = pk.write({"data/thing": b"SHORT"}, tmp / "A.~h", tmp / "A.~p")
        check(res["mode"] == "inplace", f"短内容走原地替换（实际 {res['mode']}）")
        check((tmp / "A.~p").stat().st_size == len(orig_p),
              "原地替换不改 .~p 大小")
        pk2 = DfpfPack.open(tmp / "A.~h")
        check(pk2.read(pk2.find("data/thing")) == b"SHORT",
              "改过的资源能读出 NEW 内容")
        check(pk2.read(pk2.find("stringtable/test_enus")).decode("utf-8") == ST,
              "没动的资源内容一字不差")
        _struct_ok("合成包/原地", tmp / "A.~h", tmp / "A.~p")

        # ---- 1b) 超长内容 → 搬到数据区末尾 ----
        h, p = _mk(tmp)
        pk = DfpfPack.open(h)
        old_end = pk.data_end
        big = ("StringTable{LineCodeData={" + "".join(
            f'K{i}=LineCodeData{{Text="中文很长的一段话第{i}条";'
            f'VolumeDB=0;Character=Text;SoundCue=;}};' for i in range(400))
            + "};}").encode("utf-8")
        res = pk.write({"stringtable/test_enus": big},
                       tmp / "B.~h", tmp / "B.~p")
        check(res["mode"] in ("append", "mixed"),
              f"超长内容搬到末尾（实际 {res['mode']}）")
        pk3 = DfpfPack.open(tmp / "B.~h")
        ent = pk3.find("stringtable/test_enus")
        check(ent.offset >= old_end,
              f"搬走的资源落在旧数据区之后（{ent.offset} ≥ {old_end}）")
        check(ent.offset % SECTOR == 0, "搬走的资源仍按 2048 对齐")
        check(pk3.data_end > old_end,
              f"数据区结束位置跟着变大（{old_end} → {pk3.data_end}）")
        check(pk3.read(ent) == big, "搬走之后内容仍然正确")
        check(pk3.read(pk3.find("data/big")) == b"x" * 5000,
              "同一包里没动的资源没被牵连")
        _struct_ok("合成包/追加", tmp / "B.~h", tmp / "B.~p")

        # ---- 1c) 往返：内容没变就不该动结构 ----
        h, p = _mk(tmp)
        pk = DfpfPack.open(h)
        same = {e.name: pk.read(e) for e in pk.entries if e.size}
        pk.write(same, tmp / "C.~h", tmp / "C.~p")
        pk4 = DfpfPack.open(tmp / "C.~h")
        ok = all(pk4.read(pk4.find(n)) == v for n, v in same.items())
        check(ok, "往返一圈内容全对")
        _struct_ok("合成包/往返", tmp / "C.~h", tmp / "C.~p")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_real():
    """拿本机真实 CQ2 原版包做体检。没有就跳过。"""
    print("\n[2] 真实 CQ2 包（.~p 最大 57 MB）")
    bak = Path(r"E:\游戏\CostumeQuest2\_汉化备份_原版\Win\Packs")
    if not (bak / "RgB_Stuff.~h.bak").is_file():
        NOTES.append("本机没有 CQ2 原版备份，跳过真实包测试")
        print("  [SKIP] 找不到 CQ2 原版备份")
        return
    tmp = Path(tempfile.mkdtemp(prefix="dfpf_real_"))
    try:
        for name in ("DLC1_Stuff", "RgB_Stuff"):
            shutil.copyfile(bak / f"{name}.~h.bak", tmp / f"{name}.~h")
            shutil.copyfile(bak / f"{name}.~p.bak", tmp / f"{name}.~p")
            h = tmp / f"{name}.~h"
            p = tmp / f"{name}.~p"
            pk = DfpfPack.open(h)
            ents = [e for e in pk.entries if e.size]

            # 原版本身就该满足两条铁律 —— 这是我们的"事实依据"
            check(all(e.offset % SECTOR == 0 for e in ents),
                  f"{name} 原版：资源偏移全部 2048 对齐（{len(ents)} 个）")
            check(pk.data_end == -(-max(e.offset + e.size for e in ents)
                                   // SECTOR) * SECTOR,
                  f"{name} 原版：数据区结束位置 == 最后资源结束对齐值")

            # 挑几个字符串表，塞中文（真实场景：压缩后通常变大）
            targets = [e for e in ents
                       if e.name.lower().startswith("stringtable/")
                       and "usenglish" in e.name.lower()][:3]
            if not targets:
                targets = [e for e in ents if e.comp == "zlib"][:3]
            check(bool(targets), f"{name}：找到可改的字符串表")
            if not targets:
                continue

            old_raw = {}
            changes = {}
            for e in targets:
                text = pk.read(e).decode("utf-8")
                old_raw[e.name] = (e.offset, e.size, pk.read(e))
                changes[e.name] = text.replace(
                    "Text=", "Text=【中文】").encode("utf-8")

            oh, op = tmp / f"{name}_out.~h", tmp / f"{name}_out.~p"
            res = pk.write(changes, oh, op)
            check(set(res["updated"]) == set(changes),
                  f"{name}：写入报告的资源与请求一致")
            print(f"       mode={res['mode']} 原地={res['inplace']} "
                  f"追加={res['appended']} data_end={res['data_end']}")
            _struct_ok(f"{name}/汉化后", oh, op)

            pk2 = DfpfPack.open(oh)
            for n, newv in changes.items():
                e2 = pk2.find(n)
                check(e2 is not None and pk2.read(e2) == newv,
                      f"{name}：{n} 译文可正确读出")

            # 未改动的资源：按「记录位置」精确比对内容（资源名可能重复，
            # 光靠名字会认错对象）
            by_rec = {e.record_offset: e for e in pk2.entries}
            sample = [e for e in ents if e.name not in changes][:300]
            diff = []
            for e in sample:
                e2 = by_rec.get(e.record_offset)
                if e2 is None or pk2.read(e2) != pk.read(e):
                    diff.append(e.name)
            check(not diff,
                  f"{name}：抽查 {len(sample)} 个未改动资源内容一致"
                  f"（不一致 {len(diff)}）{diff[:2]}")

            # 同名资源必须各归各（DLC1 的 ..._leet 有两条）
            dup_names = {e.name for e in ents
                         if len([x for x in ents if x.name == e.name]) > 1}
            if dup_names:
                ok_dup = True
                for nm in dup_names:
                    for e in pk.find_all(nm):
                        e2 = pk2.find_all(nm)
                        match = [x for x in e2
                                 if x.record_offset == e.record_offset]
                        if not match or pk2.read(match[0]) != pk.read(e):
                            ok_dup = False
                check(ok_dup,
                      f"{name}：{len(dup_names)} 个重名资源各归各没串"
                      f"（{sorted(dup_names)[:2]}）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_end_to_end():
    """把「用户点一键汉化」真正做的事完整跑一遍（临时目录，不碰游戏）。

    这是最有说服力的一段：同一份原版包 + 同一个翻译包，走
    extract → import_into → write_back 全流程，再对产出做结构体检。
    用户上次就是死在这一步之后。
    """
    print("\n[4] 端到端：真翻译包 → 真回填（临时目录）")
    bak = Path(r"E:\游戏\CostumeQuest2\_汉化备份_原版\Win\Packs")
    pkg = Path(r"D:\滚刀哥布林汉化椅\packages\CostumeQuest2_汉化翻译包.gtpkg")
    if not (bak / "RgB_Stuff.~h.bak").is_file():
        NOTES.append("没有 CQ2 原版备份，跳过端到端")
        print("  [SKIP] 找不到 CQ2 原版备份")
        return
    if not pkg.is_file():
        NOTES.append(f"没找到翻译包 {pkg}，跳过端到端")
        print("  [SKIP] 找不到翻译包")
        return

    tmp = Path(tempfile.mkdtemp(prefix="dfpf_e2e_"))
    try:
        game = tmp / "game"
        (game / "Win" / "Packs").mkdir(parents=True)
        for name in ("DLC1_Stuff", "RgB_Stuff"):
            shutil.copyfile(bak / f"{name}.~h.bak",
                            game / "Win" / "Packs" / f"{name}.~h")
            shutil.copyfile(bak / f"{name}.~p.bak",
                            game / "Win" / "Packs" / f"{name}.~p")

        from gametl.core.models import EngineType, Project
        from gametl.core.package import import_into
        from gametl.extractors.buddha import BuddhaExtractor

        ex = BuddhaExtractor(game)
        units = ex.extract()
        check(len(units) > 5000, f"从原版包里提出 {len(units)} 条原文")

        project = Project(root=game, engine=EngineType.BUDDHA, units=units)
        imp = import_into(project, pkg)
        filled = imp["by_uid"] + imp["by_memory"]
        print(f"       翻译包命中 {filled} 条（uid {imp['by_uid']} + "
              f"原文兜底 {imp['by_memory']}），未匹配 {imp['missed']}")
        check(filled > 5000, f"翻译包命中 {filled} 条译文")

        out = tmp / "out"
        stats = ex.write_back(project, out)
        changed = list(stats.get("changed") or [])
        from gametl.extractors.base import wb_replaced
        print(f"       回填：文件 {stats.get('files_written')} 个 · "
              f"替换 {wb_replaced(stats)} 处")
        check(wb_replaced(stats) > 5000,
              f"回填统计正确记录了替换数（{wb_replaced(stats)}）")
        check(bool(changed), "回填报告了发生变化的文件")

        for rel in changed:
            f = out / rel
            check(f.is_file(), f"产出 {rel} 存在（{f.stat().st_size} 字节）")
            if rel.endswith(".~h"):
                _struct_ok(f"端到端/{rel}", f, f.with_suffix(".~p"))

        # 译文真的写进去了吗（不是"结构对但内容空"）
        pk = DfpfPack.open(out / "Win" / "Packs" / "RgB_Stuff.~h")
        hit = 0
        for e in pk.entries:
            if not e.name.lower().startswith("stringtable/"):
                continue
            if "usenglish" not in e.name.lower():
                continue
            txt = pk.read(e).decode("utf-8", "replace")
            if any("\u4e00" <= c <= "\u9fff" for c in txt):
                hit += 1
        check(hit > 0, f"回填后的英语表里确实出现了中文（{hit} 张表）")

        # 重名资源（..._leet 有两条）都要各归各
        orig = DfpfPack.open(game / "Win" / "Packs" / "DLC1_Stuff.~h")
        new = DfpfPack.open(out / "Win" / "Packs" / "DLC1_Stuff.~h")
        leet = orig.find_all("stringtable/costumequestdlc1_leet")
        check(len(leet) >= 2,
              f"CQ2 DLC1 里确实有 {len(leet)} 条同名 ..._leet（回归前提）")
        for e in leet:
            e2 = [x for x in new.find_all(e.name)
                  if x.record_offset == e.record_offset]
            if not e2:
                check(False, f"重名资源丢了（rec@{e.record_offset}）")
                continue
            same = new.read(e2[0]) == orig.read(e)
            note = "未改动" if same else "被改（有译文）"
            print(f"       {e.name} rec@{e.record_offset} "
                  f"off={e.offset} size={e.size} → {note}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_static():
    print("\n[3] 静态检查：回填逻辑不许再走整包重建")
    src = (ROOT / "gametl" / "core" / "dfpf.py").read_text(encoding="utf-8")
    check("DATA_END_OFF" in src,
          "写回逻辑引用了「数据区结束位置」字段")
    check("SECTOR" in src, "写回逻辑引用了扇区对齐常量")
    head = src.split("def write(")[1].split("\n    def ")[0]
    check("DATA_END_OFF" in head,
          "write() 内部真的会更新数据区结束位置")
    check("_align(" in head, "write() 内部真的会做对齐")
    check("mode = \"rebuild\"" not in head,
          "旧的「整包重建」路径已删除")


def main() -> int:
    print("=" * 68)
    print("dfpf 回填结构回归（v2.6.2）")
    print("=" * 68)
    test_synthetic()
    test_real()
    test_end_to_end()
    test_static()
    print("\n" + "=" * 68)
    if NOTES:
        for n in NOTES:
            print("  [NOTE]", n)
    if FAILS:
        print(f"[失败] {len(FAILS)} 项：")
        for m in FAILS:
            print("  -", m)
        return 1
    print("[全部通过] dfpf 回填守住 2048 对齐 + 数据区长度同步")
    return 0


if __name__ == "__main__":
    sys.exit(main())
