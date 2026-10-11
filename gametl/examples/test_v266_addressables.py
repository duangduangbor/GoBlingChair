# -*- coding: utf-8 -*-
"""Addressables CRC 刷新模块测试。

覆盖：等长改写、只改指定 bundle、解析回来确实是 0、找不到 hash 时不乱改、
bundle_stem 归一化、find_catalogs 跳过备份目录。
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from gametl.core import addressables as A

H1 = "1f61aa3e1f50ac752bd0eee71b29e35c"
H2 = "354e22c87aa74a501f538605e6b181ba"


def _mk_catalog(entries) -> bytes:
    """entries: [(hash, crc_int)] -> 一个最小可用的 catalog.json 字节。"""
    payload = ""
    for h, c in entries:
        payload += json.dumps(
            {"m_Hash": h, "m_Crc": c, "m_Timeout": 0,
             "m_ChunkedTransfer": False, "m_RedirectLimit": 0},
            separators=(",", ":"), ensure_ascii=False)
        payload += "\n"
    b64 = base64.b64encode(payload.encode("utf-16-le")).decode("ascii")
    obj = {"m_LocatorId": "AddressablesMainContentCatalog",
           "m_InternalIds": ["a.bundle", "b.bundle"],
           "m_ExtraDataString": b64}
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def main():
    recs = []

    def rec(ok, name):
        recs.append((bool(ok), name))

    data = _mk_catalog([(H1, 4006512723), (H2, 1665619806)])

    # ---- 1. 只改指定 bundle，且长度完全不变 ----
    new, n = A.zero_crc(data, [H1])
    rec(n == 1, "只改 1 条（实际 %d）" % n)
    rec(len(new) == len(data), "catalog 长度不变（%d -> %d）" % (len(data), len(new)))

    got = dict(A.entries(new))
    rec(got[H1].strip() == "0", "H1 的 m_Crc 变成 0（实际 %r）" % got[H1])
    rec(got[H2].strip() == "1665619806",
        "H2 不受影响（实际 %r）" % got[H2])

    # ---- 2. 改写后 JSON 仍然可解析，且 base64 可解码 ----
    obj = json.loads(new.decode("utf-8"))
    raw = base64.b64decode(obj["m_ExtraDataString"])
    rec(raw.decode("utf-16-le").count('"m_Crc":0') == 1, "载荷里只有 1 个 m_Crc:0")
    rec(obj["m_InternalIds"] == ["a.bundle", "b.bundle"], "catalog 其它字段原样保留")

    # ---- 3. 幂等：已经 0 了再改不算改动 ----
    new2, n2 = A.zero_crc(new, [H1])
    rec(n2 == 0 and new2 == new, "已清零的条目不再改动（幂等）")

    # ---- 4. 没有匹配的 hash：原样返回，不乱改 ----
    new3, n3 = A.zero_crc(data, ["deadbeef"])
    rec(n3 == 0 and new3 == data, "没有匹配时不改动")

    # ---- 5. 全部清零 ----
    new4, n4 = A.zero_crc(data, None)
    rec(n4 == 2, "hashes=None 时全部清零（实际 %d）" % n4)
    rec(all(v.strip() == "0" for v in dict(A.entries(new4)).values()),
        "全部条目都变成 0")

    # ---- 6. bundle_stem ----
    rec(A.bundle_stem(H1 + ".bundle") == H1, ".bundle 剥掉后缀")
    rec(A.bundle_stem(H1 + ".unity3d") == H1, ".unity3d 剥掉后缀")
    rec(A.bundle_stem("abc.BUNDLE") == "abc", "大小写不敏感")
    rec(A.bundle_stem("/x/y/zz.bundle") == "zz", "带路径也能取到名字")

    # ---- 7. find_catalogs 跳过备份目录 ----
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        good = root / "Game_Data" / "StreamingAssets" / "aa" / "catalog.json"
        good.parent.mkdir(parents=True)
        good.write_bytes(data)
        bad = root / "_汉化备份_原版" / "aa" / "catalog.json"
        bad.parent.mkdir(parents=True)
        bad.write_bytes(data)
        found = A.find_catalogs(root)
        rec(found == [good], "找到 1 个 catalog 且跳过备份目录（实际 %r）"
            % [str(p.relative_to(root)) for p in found])

        # ---- 8. refresh 写盘：out_dir 另存一份，原文件不动 ----
        out = root / "patch"
        rels = A.refresh(root, [H1 + ".bundle"], out_dir=out, verbose=False)
        rec(rels == ["Game_Data/StreamingAssets/aa/catalog.json"],
            "refresh 返回改动的相对路径（实际 %r）" % rels)
        rec(good.read_bytes() == data, "原 catalog 保持不变")
        rec((out / rels[0]).read_bytes() == new, "out_dir 里的 catalog 已刷新 CRC")

        # ---- 9. 没改任何 bundle 时不产出 ----
        rec(A.refresh(root, [], out_dir=out, verbose=False) == [],
            "没有改 bundle 时不产出 catalog")

    # ---- 10. records()：JSON 字符串里带 {} 也不能把两条记录粘一起 ----
    braces = json.dumps(
        {"m_Hash": H2, "m_Crc": 123, "m_BundleName": "x",
         "m_InternalId": "{UnityEngine.AddressableAssets.Addressables"
                         ".RuntimePath}/StandaloneWindows64/aa.bundle",
         "m_BundleSize": 1000, "m_Timeout": 0},
        separators=(",", ":"), ensure_ascii=False)
    braces2 = json.dumps(
        {"m_Hash": "cafebabe", "m_Crc": 456, "m_BundleName": "y",
         "m_BundleSize": 2000, "m_Timeout": 0},
        separators=(",", ":"), ensure_ascii=False)
    payload2 = braces + braces2
    cat2 = json.dumps(
        {"m_ExtraDataString":
         base64.b64encode(payload2.encode("utf-16-le")).decode("ascii")},
        separators=(",", ":")).encode("utf-8")
    r2 = A.records_bytes(cat2)
    rec(len(r2) == 2, "含 {} 的字符串不会粘记录（切出 %d 条）" % len(r2))
    rec([x.get("m_Hash") for x in r2] == [H2, "cafebabe"],
        "记录顺序与 m_Hash 正确（%r）" % [x.get("m_Hash") for x in r2])
    rec([x.get("m_BundleSize") for x in r2] == [1000, 2000],
        "m_BundleSize 没有错位（%r）" % [x.get("m_BundleSize") for x in r2])

    # ---- 11. is_verifiable：没记录 = 不许动 ----
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cat = root / "Game_Data" / "StreamingAssets" / "aa" / "catalog.json"
        cat.parent.mkdir(parents=True)
        cat.write_bytes(_mk_catalog([(H2, 1665619806)]))
        known = A.verifiable_hashes(root)
        rec(known == {H2}, "verifiable_hashes 只含记录里的 hash")
        rec(A.is_verifiable(root, H2 + ".bundle", known) is True,
            "有记录的 bundle 可改")
        rec(A.is_verifiable(root, H1 + ".bundle", known) is False,
            "★ 没记录的 bundle 不许改（改了游戏进不去）")
        rec(A.is_verifiable(root, "/x/" + H2 + ".bundle", known) is True,
            "带路径也能判对")

    # ---- 12. 没有 Addressables 的游戏：不受影响，照常改 ----
    with tempfile.TemporaryDirectory() as td:
        rec(A.verifiable_hashes(Path(td)) == set(),
            "没有 catalog 时 verifiable_hashes 为空")
        rec(A.is_verifiable(Path(td), "whatever.bundle") is True,
            "没有 Addressables 时不限制（其它引擎照常回填）")

    # ---- 13. ★★ 载荷是「ASCII 类型名 + UTF-16LE JSON」交替排布 ----
    # IDJ 实测（2026-10-12）：真实载荷并不是纯 UTF-16 文本，而是
    #     [ASCII 类型全名][UTF-16LE JSON 记录] [ASCII 类型全名][UTF-16LE JSON] …
    # ASCII 段字节数是奇数 → 每过一条记录，下一条 JSON 的起始偏移奇偶就翻转。
    # 若把整块按 UTF-16LE 解码后再逐条 raw_decode，**只有一半记录能解析出来**，
    # 另一半整体错位成乱码被静默跳过。IDJ 的 212 条只啃出 106 条，而被漏掉的
    # 那一半里恰好有 b62cbb0a（26 MB 开场动画包），后果是它的 CRC 永远刷不掉、
    # 游戏每次启动都 CRC Mismatch 卡 Loading，日志却像「查不到这个包的记录」。
    def _mixed(items):
        out = b""
        for i, r in enumerate(items):
            nm = "Type" + "X" * (i % 2)          # 长度交替奇偶，复现错位
            out += nm.encode("ascii")
            out += json.dumps(r, separators=(",", ":"),
                              ensure_ascii=False).encode("utf-16-le")
        return out

    raw_recs = [{"m_Hash": "%032x" % (0xA0 + i), "m_Crc": 1000 + i,
                 "m_BundleName": "n%d" % i, "m_BundleSize": 10 * i}
                for i in range(6)]
    mixed = _mixed(raw_recs)
    got = [r for _, _, r in A._iter_records(mixed)]
    rec(len(got) == len(raw_recs),
        "交替编码载荷每条都读到（%d/%d）" % (len(got), len(raw_recs)))
    rec([r.get("m_Hash") for r in got] == [r["m_Hash"] for r in raw_recs],
        "交替编码下记录顺序正确")

    catm = json.dumps(
        {"m_ExtraDataString": base64.b64encode(mixed).decode("ascii")},
        separators=(",", ":")).encode("utf-8")
    want_last = raw_recs[-1]["m_Hash"]
    newm, nm = A.zero_crc(catm, {want_last})
    rec(nm == 1, "交替编码下能改到末尾那条（改了 %d 条）" % nm)
    rec(len(newm) == len(catm), "交替编码改完长度不变")
    after = [r for r in A.records_bytes(newm) if r.get("m_Hash") == want_last]
    rec(after and after[0].get("m_Crc") == 0, "目标那条 CRC 已置 0")

    # ---- 14. ★ dirty_bundles：大小与 catalog 记录不符 = 已被改过 ----
    # 这批包必须一起刷 CRC（它们既不在 changed、也不在产物目录里，
    # 以前两边都刷不到 → 补丁一装就 CRC Mismatch）。IDJ 的 a29f6ac8/aff1ec57。
    def _mk_cat_sizes(items):
        payload = ""
        for h, c, s in items:
            payload += json.dumps(
                {"m_Hash": h, "m_Crc": c, "m_BundleSize": s},
                separators=(",", ":")) + "\n"
        b64 = base64.b64encode(payload.encode("utf-16-le")).decode("ascii")
        return json.dumps({"m_ExtraDataString": b64},
                          separators=(",", ":")).encode("utf-8")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        packs = (root / "Game_Data" / "StreamingAssets" / "aa"
                 / "StandaloneWindows64")
        packs.mkdir(parents=True)
        (packs / (H1 + ".bundle")).write_bytes(b"x" * 100)    # 与记录一致
        (packs / (H2 + ".bundle")).write_bytes(b"y" * 250)    # 与记录不符
        (root / "Game_Data" / "StreamingAssets" / "aa" / "catalog.json"
         ).write_bytes(_mk_cat_sizes([(H1, 11, 100), (H2, 22, 200)]))
        d = A.dirty_bundles(root)
        rec(d == {H2}, "dirty_bundles 只挑 size 不符的（%r）" % sorted(d))
        rec(A.recorded_sizes(root).get(H2) == 200, "recorded_sizes 读到原版大小")

    ok = sum(1 for r, _ in recs if r)
    for r, name in recs:
        print("[%s] %s" % ("OK" if r else "FAIL", name))
    print()
    print("结果：%d 通过 / %d 失败" % (ok, len(recs) - ok))
    return 0 if ok == len(recs) else 1


if __name__ == "__main__":
    sys.exit(main())
