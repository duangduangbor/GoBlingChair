# -*- coding: utf-8 -*-
"""Addressables catalog 的 AssetBundle CRC 刷新。

Unity 的 Addressables 运行时会逐个校验 AssetBundle 的 CRC，校验值明文存在
catalog.json 的 ``m_ExtraDataString`` 里（base64 → UTF-16LE 编码的 JSON 文本）：

    {"m_Hash":"1f61aa3e1f50ac752bd0eee71b29e35c","m_Crc":4006512723,...}

汉化改写 bundle 字节之后这个旧值就失配，运行时直接拒绝加载：

    CRC Mismatch. Provided 6347575e, calculated 553a1d35 from data.
    Will not load AssetBundle 'aa\\StandaloneWindows64\\xxx.bundle'

表现是游戏永远卡在 Loading。Player.log 里还会跟着一串
``RemoteProviderException: Invalid path in AssetBundleProvider`` —— 那是 CRC
失败的连带报错，**不是**路径真的错，别被带偏。

修法：把被改写 bundle 的 ``m_Crc`` 置 0。Addressables 对 0 不做校验。
没有去复刻 Unity 的 CRC 实现，因为实测它存的不是标准 CRC-32 的任何常见变体
（反射/非反射 × init 0/0xFFFFFFFF × xorout 0/0xFFFFFFFF 全试过，整文件都试过，
都对不上），清零是唯一稳且可验证的路子。

硬约束：``m_ExtraDataString`` 解码后按「4 字节长度 + 载荷」顺序排布，条目字节
长度一旦变化，后面所有条目的偏移就全错。所以数字 ``0`` 之后用空格补到原来的
字符数 —— JSON 允许值后跟空白，语义不变，长度不变。

★★ 载荷是**两种编码交替**的（IDJ 实测，2026-10-12 更正）：``m_ExtraDataString``
base64 解码后不是纯 UTF-16 文本，而是::

    [ASCII 类型全名][UTF-16LE JSON 记录] [ASCII 类型全名][UTF-16LE JSON 记录] …

ASCII 段的字节数是**奇数**，于是每过一条记录，下一条 JSON 的起始偏移奇偶就
翻转一次。**所以绝不能把整块按 UTF-16LE 解码后再逐条 raw_decode** —— 那样
只有「偶数偏移」的那一半记录能解析出来（IDJ 的 212 条只啃出 106 条），另一半
整体错位成乱码、被静默跳过。

代价极大：IDJ 的 ``b62cbb0a``（26 MB 开场动画包，游戏启动必需）恰好落在被漏掉
的那一半里，于是它的 CRC 永远刷不掉 —— 游戏每次启动都
``CRC Mismatch. Provided d87a0010 ... Will not load AssetBundle`` 卡死在 Loading，
而我们「在整个游戏目录搜 d87a0010 都搜不到出处」（其实它以十进制 3631874064
明明白白写在载荷里）。这条弯路以前被误判成「Unity 用别处的校验值，无解」。

正确做法见 ``_iter_records``：在**字节层**找 ``{``（UTF-16LE 的 ``7b 00``），
从真实字符边界起解码再 raw_decode。这样 212 条一条不漏，**每个 bundle 的
CRC 都能被中和**。

所以：**catalog 里的包照常回填**（本来就没问题的包保持原样）。
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

# 每个条目形如 "m_Hash":"<md5>","m_Crc":<十进制>
_ENTRY_RE = re.compile(r'"m_Hash":"([0-9a-fA-F]+)","m_Crc":(\d+)')
# 备份/工具产物目录，找 catalog 时跳过
_SKIP_DIRS = ("_汉化备份", "_汉化输出", "_backup", "node_modules", "__pycache__")


def is_catalog_name(name: str) -> bool:
    """这个文件名像不像 Addressables catalog。"""
    return name.lower() == "catalog.json"


def find_catalogs(root: Path, max_depth: int = 6) -> list:
    """找出 root 下所有 Addressables catalog.json（有界深度，跳过备份目录）。"""
    root = Path(root)
    found: list = []

    def walk(d: Path, depth: int):
        if depth > max_depth:
            return
        try:
            entries = list(d.iterdir())
        except OSError:
            return
        for e in entries:
            try:
                if e.is_dir():
                    n = e.name
                    if n.lower() in _SKIP_DIRS or any(
                            n.startswith(p) for p in _SKIP_DIRS):
                        continue
                    walk(e, depth + 1)
                elif is_catalog_name(e.name):
                    found.append(e)
            except OSError:
                continue

    walk(root, 0)
    return sorted(found)


def _extra_raw(data: bytes) -> bytes:
    """catalog 字节 → ``m_ExtraDataString`` 的原始二进制。失败回空串。"""
    try:
        obj = json.loads(data.decode("utf-8"))
        return base64.b64decode(obj["m_ExtraDataString"])
    except (ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
        return b""


def _iter_records(raw: bytes):
    """按字节逐条吐出 ``m_ExtraDataString`` 里的 JSON 记录。

    ★★ 这里**不能**「把整块 raw 按 UTF-16LE 解码、再用 raw_decode 逐条啃」。

    实测 IDJ 的载荷是**两种编码交替排布**的::

        [ASCII 类型全名][UTF-16LE JSON 记录] [ASCII 类型全名][UTF-16LE JSON 记录] …

    而 ASCII 段的字节数是**奇数**，于是每过一条记录，下一条 JSON 的起始
    偏移奇偶就翻转一次。整块按 UTF-16LE 解码时，**有一半记录整体错位**，
    解出来全是乱码，raw_decode 直接跳过 —— IDJ 的 212 条里只能啃出 106 条。

    后果很严重：被漏掉的那一半里恰好有 ``b62cbb0a``，它的 CRC 永远刷不掉，
    游戏每次启动都 ``CRC Mismatch ... Will not load AssetBundle`` 卡在 Loading，
    而我们却「查不到这条记录的出处」。

    正确做法：直接在**字节层**找 ``{``（UTF-16LE 的 ``7b 00``），从这个字
    符边界起解码，raw_decode 返回的字符数 ×2 就是这条记录的字节长度。
    每个 ``{`` 都落在真实字符边界上，错位问题自然消失。

    Yields:
        ``(start, end, record)`` —— 记录在 raw 里的字节区间（左闭右开）与解析结果。
    """
    dec = json.JSONDecoder()
    pos = 0
    n = len(raw)
    mark = b"\x7b\x00"                     # "{" 的 UTF-16LE 编码
    while pos < n:
        i = raw.find(mark, pos)
        if i < 0:
            return
        try:
            chunk = raw[i:].decode("utf-16-le", "ignore")
        except Exception:
            pos = i + 2
            continue
        try:
            rec, end = dec.raw_decode(chunk)
        except ValueError:
            pos = i + 2
            continue
        if not isinstance(rec, dict):
            pos = i + 2
            continue
        end_byte = i + end * 2
        if end_byte <= i or end_byte > n:
            pos = i + 2
            continue
        yield i, end_byte, rec
        pos = end_byte


def entries(data: bytes) -> list:
    """列出 catalog 里所有 (hash, crc 原文) —— 仅用于自检/诊断。"""
    return [((rec.get("m_Hash") or ""), str(rec.get("m_Crc")))
            for _, _, rec in _iter_records(_extra_raw(data))]


def records(path: Path) -> list:
    """精确解析 m_ExtraDataString 里的所有 AssetBundleRequestOptions。

    每条长这样（字段随 Addressables 版本增减）::

        {"m_Hash": "<bundle 内容 hash>", "m_Crc": 1665619806, ...,
         "m_BundleName": "<AssetBundle 内部名>", "m_BundleSize": 2045764, ...}

    **不能被大括号配对糊弄** —— JSON 字符串里就带 ``{}``
    （``"{UnityEngine.AddressableAssets.Addressables.RuntimePath}"``），
    自己配对大括号会把相邻两条记录粘成一条，hash 与 BundleSize 就错位了。
    所以这里用 ``json.JSONDecoder.raw_decode`` 一条一条啃。
    """
    try:
        return records_bytes(Path(path).read_bytes())
    except OSError:
        return []


def records_bytes(data: bytes) -> list:
    """同 ``records()``，但直接吃 catalog 的字节（便于测试/内存里操作）。"""
    return [rec for _, _, rec in _iter_records(_extra_raw(data))]


def entry_hashes(path: Path) -> set:
    """catalog 里有 CRC 条目的 bundle 名集合（即 m_Hash 集合）。

    只有这个集合里的 bundle，我们才**有办法**中和它的校验。
    """
    return {(r.get("m_Hash") or "").lower() for r in records(path)
            if r.get("m_Hash")}


def verifiable_hashes(root: Path) -> set:
    """root 下所有 catalog 覆盖到的 bundle 名（并集）。"""
    out: set = set()
    for cat in find_catalogs(root):
        out |= entry_hashes(cat)
    return out


def is_verifiable(root: Path, bundle, known: set = None) -> bool:
    """这个 bundle 的 CRC 我们有没有办法中和？

    ``known`` 传 ``verifiable_hashes(root)`` 可以省掉重复解析 catalog。
    **没有 catalog 的游戏（不走 Addressables）一律返回 True** —— 那边根本
    不存在 CRC 校验，照常回填。
    """
    if known is None:
        known = verifiable_hashes(root)
    if not known:                  # 找不到 Addressables：不存在校验
        return True
    return bundle_stem(bundle).lower() in known


def zero_crc(data: bytes, hashes=None) -> tuple:
    """把指定 bundle（按 hash 匹配）的 m_Crc 置 0。

    Args:
        data: catalog.json 的原始字节
        hashes: bundle 文件名（不含 .bundle）的集合；None = 全部置 0

    Returns:
        (新字节, 改动条数)。新字节与输入**等长**。

    ★ 逐条按 ``_iter_records`` 给出的**字节区间**改写，不再对整块做
    ``decode("utf-16-le")`` 后正则扫 —— 那样只能改到偶数偏移的那一半记录
    （详见 ``_iter_records`` 的说明）。
    """
    obj = json.loads(data.decode("utf-8"))
    b64 = obj["m_ExtraDataString"]
    raw = base64.b64decode(b64)

    want = None
    if hashes is not None:
        want = {str(h).lower() for h in hashes}

    parts: list = []
    pos = 0
    count = 0
    for start, end, rec in _iter_records(raw):
        parts.append(raw[pos:start])
        seg = raw[start:end]
        h = (rec.get("m_Hash") or "").lower()
        crc = rec.get("m_Crc")
        if (want is None or h in want) and isinstance(crc, int) and crc != 0:
            # 只动这一条自己的字节；数字 0 后用空格补齐到原字符数 ——
            # JSON 允许值后跟空白，语义不变、长度不变。
            txt = seg.decode("utf-16-le")
            txt2, hit = re.subn(
                r'("m_Crc":)(\d+)',
                lambda m: m.group(1) + "0" + " " * (len(m.group(2)) - 1),
                txt, count=1)
            if hit:
                seg2 = txt2.encode("utf-16-le")
                if len(seg2) == len(seg):
                    seg = seg2
                    count += 1
        parts.append(seg)
        pos = end
    parts.append(raw[pos:])
    raw2 = b"".join(parts)

    if not count:
        return data, 0
    if len(raw2) != len(raw):
        raise ValueError("载荷长度变了 %d -> %d" % (len(raw), len(raw2)))
    b64_2 = base64.b64encode(raw2).decode("ascii")
    if len(b64_2) != len(b64):
        raise ValueError("base64 长度变了")
    old_b = b64.encode("ascii")
    if data.count(old_b) != 1:
        raise ValueError("m_ExtraDataString 在 catalog 里不唯一")
    out = data.replace(old_b, b64_2.encode("ascii"))
    if len(out) != len(data):
        raise ValueError("catalog 长度变了")
    return out, count


def bundle_stem(name: str) -> str:
    """``c1d099eb...bundle`` -> ``c1d099eb...``（catalog 里 m_Hash 就是它）。

    先把目录部分去掉 —— 传进来的可能是相对路径而不是裸文件名。
    """
    n = Path(str(name)).name
    low = n.lower()
    for suf in (".bundle", ".unity3d"):
        if low.endswith(suf):
            return n[: -len(suf)]
    return Path(n).stem


def _walk_packs(base: Path, max_depth: int = 8):
    """有界递归列举 base 下的 .bundle/.unity3d（跳过备份/工具目录）。"""
    stack = [(Path(base), 0)]
    while stack:
        d, depth = stack.pop()
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir():
                    n = e.name
                    nl = n.lower()
                    if nl in _SKIP_DIRS or any(n.startswith(p) for p in _SKIP_DIRS):
                        continue
                    if depth + 1 <= max_depth:
                        stack.append((e, depth + 1))
                elif e.is_file():
                    n = e.name
                    if n.lower().endswith((".bundle", ".unity3d")):
                        yield e
            except OSError:
                continue


def recorded_sizes(root: Path) -> dict:
    """catalog 记录的 ``hash -> m_BundleSize``（= **原版** 字节数）。"""
    out: dict = {}
    for cat in find_catalogs(root):
        for r in records(cat):
            h = (r.get("m_Hash") or "").lower()
            if h:
                out[h] = r.get("m_BundleSize")
    return out


def dirty_bundles(root: Path, max_depth: int = 8) -> set:
    """root 下**已经被改过**的 bundle —— 判据是文件大小与 catalog 记录不符。

    ★ 为什么必须单独找它们（v2.6.7，IDJ 实战暴露）：

    ``write_back`` 出补丁时，会把「产物与游戏里现有文件逐字节相同」的包判成
    unchanged、**不写进补丁目录**（这是 v2.6.6 的优化，本身没错）。可游戏目录里
    那一份本身可能就是**上一轮**（或旧版工具）改过的，而 catalog 里记的仍是原版
    CRC —— 于是这批包既不在 ``changed``、也不在产物目录，**两边都刷不到**。
    补丁一装，它们立刻被 Unity 判 ``CRC Mismatch``，游戏卡死 Loading。

    IDJ 实测：``a29f6ac8`` / ``aff1ec57`` 正是这样漏网的（catalog 记 8029404 /
    118150，磁盘上却是 8031992 / 119295），而 `b62cbb0a` 因为压根解析不出来
    更惨。两个坑一起修掉，这类游戏才算真的稳。

    判据用大小最省事：UnityPy 重新序列化后字节数几乎必变，而 catalog 里的
    ``m_BundleSize`` 记的是原版，一比即可，不需要读内容。
    """
    sizes = recorded_sizes(root)
    if not sizes:
        return set()
    dirty: set = set()
    for cat in find_catalogs(root):
        for p in _walk_packs(cat.parent, max_depth):
            h = bundle_stem(p.name).lower()
            want = sizes.get(h)
            if want is None:
                continue          # catalog 没记这个包，无从比对
            try:
                if p.stat().st_size != want:
                    dirty.add(h)
            except OSError:
                continue
    return dirty


def refresh(root: Path, bundles, out_dir: Path = None,
            verbose: bool = True) -> list:
    """按「被改写的 bundle」刷新 root 下所有 catalog 的 CRC。

    Args:
        root: 游戏/解码目录（用于找 catalog）
        bundles: 被改写的 bundle 文件名（或路径）集合
        out_dir: 不为 None 时把改好的 catalog 写到 out_dir 下的同名相对路径
                 （补丁安装器只覆盖 out_dir 里的文件，所以必须另存一份）；
                 为 None 时原地改 root 里的 catalog

    Returns:
        实际改动的 catalog 相对路径列表（相对 root）。
    """
    stems = {bundle_stem(b) for b in (bundles or [])}
    if not stems:
        return []
    root = Path(root)
    done: list = []
    for cat in find_catalogs(root):
        try:
            data = cat.read_bytes()
            new, n = zero_crc(data, stems)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        if not n:
            continue
        rel = str(cat.relative_to(root)).replace("\\", "/")
        try:
            if out_dir is None:
                cat.write_bytes(new)
            else:
                dst = Path(out_dir) / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(new)
        except OSError:
            continue
        if verbose:
            print("[Addressables] 刷新 CRC：%s（%d 个 bundle）" % (rel, n))
        done.append(rel)
    return done
