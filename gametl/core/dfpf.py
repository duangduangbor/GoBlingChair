# -*- coding: utf-8 -*-
"""Double Fine 的 dfpf 包容器（``.~h`` 索引 + ``.~p`` 数据）读写。

Buddha / Moai / Remonkeyed 引擎（Costume Quest 1·2、Stacking、Brutal Legend、
Headlander、The Cave、Iron Brigade …）的资源都装在这种包里。

格式依据
--------
· watto.org 的 Game Extractor 插件规格 ``Archive_P_DFPF``（字段清单）
· bgbennyboy/DoubleFine-Explorer（MPL-2.0）``uDFExplorer_PAKManager.pas``
  （三种版本 v2 / v5 / v6 的**逐字段读取顺序**）

为什么不用手写位运算
--------------------
``.~h`` 每条文件记录是 16 字节的**非对齐位域**，三种版本的切法各不相同
（v5 里"文件偏移"占 29 位、"压缩长度"占 23 位，还跨字节交叉）。手写
pack/unpack 公式极易出错且无法验证。

这里改用**单比特探针**：给一条只有第 k 位为 1 的合成记录跑一遍官方读取
逻辑，看哪个字段被置上了 2 的几次方，于是自动得到"字段 ↔ 记录位"的映射。
回填时按这个映射把新值写回去即可 —— 与读取逻辑严格互逆，且天然支持三种
版本，将来遇到 v7 只要补一个 reader。
"""
from __future__ import annotations

import io
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

MAGIC = b"dfpf"

#: 压缩类型标记 → 是否 zlib 压缩（各版本取值不同，见 Pascal 的 case 分支）
COMP_NONE = "none"
COMP_ZLIB = "zlib"
COMP_XMEM = "xmem"          # Xbox XMemCompress，本工具只读不写

_RECORD_SIZE = 16


# --------------------------------------------------------------------- 读取游标

class _Cursor:
    """模拟 Delphi TExplorerFileStream（**大端**读），位运算语义与 Pascal 逐条对齐。"""

    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes, pos: int = 0):
        self.buf = buf
        self.pos = pos

    def seek(self, delta: int) -> None:
        self.pos += delta

    def read_byte(self) -> int:
        v = self.buf[self.pos]
        self.pos += 1
        return v

    def read_dword(self) -> int:
        v = struct.unpack_from(">I", self.buf, self.pos)[0]
        self.pos += 4
        return v

    def read_qword(self) -> int:
        v = struct.unpack_from(">Q", self.buf, self.pos)[0]
        self.pos += 8
        return v


def _shl(v: int, n: int) -> int:
    """Pascal 的 32 位 shl（会溢出截断）。"""
    return (v << n) & 0xFFFFFFFF


def _shl64(v: int, n: int) -> int:
    return (v << n) & 0xFFFFFFFFFFFFFFFF


# --------------------------------------------------- 三种版本的记录读取逻辑（忠实移植）

def _read_record_v2(c: _Cursor) -> dict:
    """版本 2：Costume Quest 1 / Stacking 早期。"""
    u = c.read_dword() >> 9
    c.seek(1)
    size = _shl(c.read_dword(), 1) >> 10
    c.seek(-2)
    offset = _shl64(c.read_qword(), 7) >> 34
    c.seek(-3)
    name_off = c.read_dword() >> 11
    c.seek(-2)
    type_idx = _shl(c.read_dword(), 5) >> 25
    c.seek(-3)
    comp = c.read_byte() & 15
    compressed = comp == 4
    return {"usize": u, "name_offset": name_off, "offset": offset,
            "size": size, "type_index": type_idx, "comp_raw": comp,
            "compressed": compressed,
            "comp": COMP_ZLIB if compressed else COMP_NONE}


def _read_record_v5(c: _Cursor) -> dict:
    """版本 5：Costume Quest 2 / Headlander 等（本工具主用）。"""
    u = c.read_dword() >> 8
    c.seek(-1)
    name_off = c.read_dword() >> 11
    c.seek(1)
    offset = c.read_dword() >> 3
    c.seek(-1)
    size = _shl(c.read_dword(), 5) >> 9
    c.seek(-1)
    type_idx = (_shl(c.read_dword(), 4) >> 24) >> 1
    c.seek(-3)
    comp = c.read_byte() & 15
    if comp == 4:
        kind = COMP_NONE
    elif comp == 8:
        kind = COMP_ZLIB
    elif comp == 12:
        kind = COMP_XMEM
    else:
        kind = COMP_NONE          # 未知：按不压缩处理（至少能原样取出）
    return {"usize": u, "name_offset": name_off, "offset": offset,
            "size": size, "type_index": type_idx, "comp_raw": comp,
            "compressed": kind == COMP_ZLIB, "comp": kind}


def _read_record_v6(c: _Cursor) -> dict:
    """版本 6：Brutal Legend / Massive Chalice 等。"""
    u = c.read_dword() >> 5
    c.seek(-2)
    name_off = _shl(c.read_dword(), 13) >> 13
    c.seek(2)
    offset = c.read_dword() >> 1
    c.seek(-1)
    size = _shl(c.read_dword(), 7) >> 7
    type_idx = c.read_byte() >> 2
    c.seek(-1)
    comp = (c.read_byte() << 6 & 0xFF) >> 6
    if comp == 1:
        kind = COMP_NONE
    elif comp == 2:
        kind = COMP_ZLIB
    else:
        kind = COMP_NONE
    return {"usize": u, "name_offset": name_off, "offset": offset,
            "size": size, "type_index": type_idx, "comp_raw": comp,
            "compressed": kind == COMP_ZLIB, "comp": kind}


_READERS: Dict[int, Callable[[_Cursor], dict]] = {
    2: _read_record_v2,
    5: _read_record_v5,
    6: _read_record_v6,
}

#: 每个字段占据「16 字节记录」里的哪些位。键 = 字段名，值 = [(字节下标, 该字段的权重位)]
_BitMap = List[tuple]


def _derive_bit_layout(reader: Callable[[_Cursor], dict]) -> Dict[str, _BitMap]:
    """用单比特探针反推字段 ↔ 记录位 的映射。

    对记录里的第 k 个比特单独置 1，跑一遍官方读取逻辑；
    哪个字段非零，就说明该字段吃到了这个比特，且权重是 2^log2(值)。
    """
    layout: Dict[str, _BitMap] = {}
    for k in range(_RECORD_SIZE * 8):
        probe = bytearray(_RECORD_SIZE)
        probe[k // 8] = 1 << (7 - (k % 8))      # 大端序：字节内高位在前
        vals = reader(_Cursor(bytes(probe) + b"\x00" * 16))
        for name, v in vals.items():
            if isinstance(v, bool) or not isinstance(v, int) or v == 0:
                continue
            layout.setdefault(name, []).append((k, v.bit_length() - 1))
    return layout


_LAYOUTS: Dict[int, Dict[str, _BitMap]] = {}


def _layout(version: int) -> Dict[str, _BitMap]:
    if version not in _LAYOUTS:
        _LAYOUTS[version] = _derive_bit_layout(_READERS[version])
    return _LAYOUTS[version]


def _patch_record(rec: bytearray, base: int, version: int, **values) -> None:
    """按推导出的位映射，把新值写回 ``rec[base:base+16]``（只动相关比特）。

    注意 ``rec`` 必须是**整个索引字节串**（bytearray），``base`` 是记录起点 ——
    传切片会得到副本、改了个寂寞。
    """
    lay = _layout(version)
    for fname, val in values.items():
        for bit, weight in lay.get(fname, ()):
            i = base + bit // 8
            mask = 1 << (7 - (bit % 8))
            if (int(val) >> weight) & 1:
                rec[i] |= mask
            else:
                rec[i] &= ~mask & 0xFF


# ------------------------------------------------------- 合成包（测试 / 自检用）

#: 合成包默认的「资源类型」表（下标即记录的 type_index）
SYNTHETIC_TYPES = ["StringTable", "Blob"]


def build_synthetic_v5(h_path, p_path, resources,
                       types: Optional[List[str]] = None) -> None:
    """现造一个 v5 的 ``.~h``/``.~p`` 包（不依赖任何真实游戏）。

    Args:
        h_path / p_path: 输出的索引 / 数据文件路径。
        resources: ``[(名字, 未压缩内容 bytes, 类型下标, 是否 zlib 压缩)]``。
        types: 资源类型表，默认 :data:`SYNTHETIC_TYPES`。

    布局刻意贴近真实包（2048 对齐、名字目录 + 尾部填充），
    好让解析器走的是和生产环境一样的代码路径。
    """
    h_path, p_path = Path(h_path), Path(p_path)
    types = list(types or SYNTHETIC_TYPES)

    blob = bytearray()
    recs = []
    for name, payload, tidx, comp in resources:
        raw = zlib.compress(payload, 6) if comp else payload
        off = len(blob)
        blob += raw
        blob += b"\x00" * ((-len(blob)) % 2048)      # 跟真实包一样 2048 对齐
        recs.append((name, len(payload), off, len(raw), tidx, 8 if comp else 4))
    p_path.write_bytes(bytes(blob))

    names = bytearray()
    name_offsets = []
    for r in recs:
        name_offsets.append(len(names))
        names += r[0].encode("latin-1") + b"\x00"
    while len(names) % 4:
        names.append(0xCC)

    type_off = 2048
    tb = bytearray()
    for t in types:
        enc = t.encode() + b"\x00"
        tb += struct.pack(">I", len(enc)) + enc + b"\x00" * 12
    while len(tb) % 4:
        tb.append(0xCC)

    details_off = type_off + len(tb)
    name_dir_off = details_off + _RECORD_SIZE * len(recs)

    head = bytearray(b"\x00" * 2048)
    head[0:4] = MAGIC
    head[4] = 5
    struct.pack_into(">Q", head, 8, type_off)
    struct.pack_into(">Q", head, 16, name_dir_off)
    struct.pack_into(">I", head, 24, len(types))
    struct.pack_into(">I", head, 28, len(names))
    struct.pack_into(">I", head, 32, len(recs))
    struct.pack_into(">I", head, 36, 0x23A1CEAB)
    struct.pack_into(">Q", head, 56, details_off)
    struct.pack_into(">I", head, 80, 1)
    struct.pack_into(">I", head, 84, 0x23A1CEAB)

    details = bytearray(_RECORD_SIZE * len(recs))
    for i, (name, usize, off, size, tidx, comp) in enumerate(recs):
        _patch_record(details, _RECORD_SIZE * i, 5, usize=usize,
                      name_offset=name_offsets[i], offset=off, size=size,
                      type_index=tidx, comp_raw=comp)

    body = bytes(tb) + bytes(details) + bytes(names) + b"\x00" * 16
    struct.pack_into(">I", head, 76, len(head) + len(body))
    h_path.write_bytes(bytes(head) + body + b"\x00" * 16)


# --------------------------------------------------------------------- 数据模型

@dataclass
class DfpfEntry:
    name: str
    usize: int              # 解压后大小
    size: int               # 在 .~p 里的原始（可能是压缩的）字节数
    offset: int             # 在 .~p 里的起始偏移
    type_index: int = 0
    comp: str = COMP_NONE
    ext: str = ""
    record_offset: int = 0  # 该记录在 .~h 里的偏移（回填时定位）

    @property
    def compressed(self) -> bool:
        return self.comp == COMP_ZLIB

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"<DfpfEntry {self.name!r} off={self.offset} "
                f"size={self.size} usize={self.usize} {self.comp}>")


# --------------------------------------------------------------------- 包对象

class DfpfPack:
    """一个 ``.~h`` + ``.~p`` 组成的资源包。"""

    def __init__(self, h_path: Path, version: int, header: bytes,
                 entries: List[DfpfEntry], type_names: List[str],
                 records_offset: int, file_size: int):
        self.h_path = Path(h_path)
        self.p_path = Path(h_path).with_suffix(".~p")
        self.version = version
        self.header = header
        self.entries = entries
        self.type_names = type_names
        self.records_offset = records_offset
        self.file_size = file_size

    # -------------------------------------------------- 打开 / 解析

    @staticmethod
    def looks_like(path: Path) -> bool:
        """快速判断一个文件是不是 dfpf 索引（只读 4 字节）。"""
        p = Path(path)
        if p.suffix.lower() != ".~h":
            return False
        try:
            with open(p, "rb") as f:
                return f.read(4) == MAGIC
        except OSError:
            return False

    @classmethod
    def open(cls, h_path: Path) -> "DfpfPack":
        h_path = Path(h_path)
        try:
            head = h_path.read_bytes()          # 索引文件很小（几十 KB ~ 1 MB）
        except OSError as e:
            raise DfpfPackError(f"读不到索引文件：{e}") from e
        if len(head) < 2048 or head[:4] != MAGIC:
            raise DfpfPackError(f"不是 dfpf 包（缺少 dfpf 魔数）：{h_path.name}")

        version = head[4]                       # 版本是单字节，不是 dword
        reader = _READERS.get(version)
        if reader is None:
            raise DfpfPackError(
                f"暂不支持的 dfpf 版本 {version}（{h_path.name}）。"
                f"已知版本：{sorted(_READERS)}")

        type_off = struct.unpack_from(">Q", head, 8)[0]
        name_dir_off = struct.unpack_from(">Q", head, 16)[0]
        type_count = struct.unpack_from(">I", head, 24)[0]
        num_files = struct.unpack_from(">I", head, 32)[0]
        records_off = struct.unpack_from(">Q", head, 56)[0]

        if not (0 < type_count < 20000 and 0 < num_files < 200000):
            raise DfpfPackError(f"索引数字不合理，格式判断可能有误：{h_path.name}")

        # 类型表：DWord 长度 + 名字（含结尾 NUL）+ 12 字节未知
        type_names: List[str] = []
        pos = type_off
        for _ in range(type_count):
            n = struct.unpack_from(">I", head, pos)[0]
            pos += 4
            if n <= 0 or n > 512 or pos + n > len(head):
                break
            raw = head[pos:pos + n]
            pos += n
            name = raw.split(b"\x00", 1)[0].decode("latin-1").strip()
            type_names.append(name)
            pos += 12                            # unknown(4) + hash(4) + null(4)

        entries: List[DfpfEntry] = []
        for i in range(num_files):
            rec_off = records_off + _RECORD_SIZE * i
            if rec_off + _RECORD_SIZE > len(head):
                break
            vals = reader(_Cursor(head, rec_off))
            npos = name_dir_off + vals["name_offset"]
            if npos >= len(head):
                continue
            end = head.find(b"\x00", npos)
            if end < 0:
                continue
            name = head[npos:end].decode("latin-1")
            if not name:
                continue
            t_idx = vals["type_index"]
            ext = (type_names[t_idx].lower().lstrip(".")
                   if 0 <= t_idx < len(type_names) else "")
            entries.append(DfpfEntry(
                name=name, usize=vals["usize"], size=vals["size"],
                offset=vals["offset"], type_index=t_idx,
                comp=vals["comp"], ext=ext, record_offset=rec_off))

        return cls(h_path=h_path, version=version, header=head,
                   entries=entries, type_names=type_names,
                   records_offset=records_off, file_size=len(head))

    # -------------------------------------------------- 取数据

    def read(self, entry: DfpfEntry) -> bytes:
        """取出并（必要时）解压一个资源。"""
        try:
            with open(self.p_path, "rb") as f:
                f.seek(entry.offset)
                raw = f.read(entry.size)
        except OSError as e:
            raise DfpfPackError(f"读 {self.p_path.name} 失败：{e}") from e
        if len(raw) < entry.size:
            raise DfpfPackError(
                f"{self.p_path.name} 里 {entry.name} 数据不完整"
                f"（要 {entry.size} 字节，只剩 {len(raw)}）")
        if entry.comp == COMP_ZLIB:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                # 少数包会把少量未压缩数据填在这里；退化为原样返回
                return raw
        if entry.comp == COMP_XMEM:
            raise DfpfPackError(
                f"{entry.name} 用的是 Xbox XMemCompress，本工具暂不支持解压")
        return raw

    def find(self, name: str) -> Optional[DfpfEntry]:
        for e in self.entries:
            if e.name == name:
                return e
        return None

    def by_ext(self, *exts: str) -> List[DfpfEntry]:
        want = {e.lower().lstrip(".") for e in exts}
        return [e for e in self.entries if e.ext in want]

    # -------------------------------------------------- 回填

    def write(self, changes: Dict[str, bytes], out_h: Path,
              out_p: Path) -> dict:
        """把 ``{资源名: 新内容(未压缩字节)}`` 写进包副本。

        两条路，优先走省事的那条：
        1. **就地替换**：新压缩数据不比原槽位大 → 只改 ``.~p`` 里那一段
           （后面用 0 补齐）并把索引里的"解压后大小"改掉。其余字节一个不动，
           产出最小、最安全。
        2. **整包重建**：装不下时按原顺序重排所有资源、重算全部偏移，
           并更新索引里每条记录。

        Args:
            changes: 资源名 → 新内容（未压缩）。名字必须在包内存在。
            out_h / out_p: 产出的索引与数据文件路径。

        Returns:
            {"updated": 改了哪些资源, "mode": "inplace"|"rebuild",
             "bytes_in": 新数据总长, "key": 关键结果字典（供上层统计）}
        """
        out_h, out_p = Path(out_h), Path(out_p)
        todo: List[tuple] = []
        for name, payload in changes.items():
            ent = self.find(name)
            if ent is None:
                continue
            if ent.comp == COMP_XMEM:
                continue
            new_raw = (zlib.compress(payload, 9) if ent.comp == COMP_ZLIB
                       else bytes(payload))
            todo.append((ent, payload, new_raw))
        if not todo:
            return {"updated": [], "mode": "none", "bytes_in": 0}

        inplace = all(len(raw) <= ent.size for ent, _p, raw in todo)
        out_h.parent.mkdir(parents=True, exist_ok=True)

        rec_bytes = bytearray(self.header)          # 索引改动都在这份副本上

        if inplace:
            # ---- 路 1：只动 .~p 里那几段 + 索引里的"解压后大小" ----
            import shutil
            shutil.copyfile(self.p_path, out_p)
            with open(out_p, "r+b") as f:
                for ent, payload, raw in todo:
                    f.seek(ent.offset)
                    pad = ent.size - len(raw)
                    f.write(raw + b"\x00" * pad)
                    _patch_record(rec_bytes, ent.record_offset,
                                  self.version, usize=len(payload))
            mode = "inplace"
        else:
            # ---- 路 2：整包重建 ----
            old = self.p_path.read_bytes()
            ordered = sorted(self.entries, key=lambda e: (e.offset, e.name))
            change_map = {id(ent): raw for ent, _p, raw in todo}

            buf = bytearray()
            first = ordered[0].offset if ordered else 0
            buf += old[:first]                       # 首个资源之前的头/填充
            cursor = len(buf)
            ends = []                                # (旧区间, 新区间)
            for i, ent in enumerate(ordered):
                raw = change_map.get(id(ent))
                if raw is None:
                    raw = old[ent.offset:ent.offset + ent.size]
                nxt = (ordered[i + 1].offset if i + 1 < len(ordered)
                       else len(old))
                gap = max(0, nxt - (ent.offset + ent.size))
                new_off = cursor
                buf += raw
                buf += old[ent.offset + ent.size:
                           ent.offset + ent.size + gap]   # 原样保留对齐填充
                cursor = len(buf)
                ends.append((ent, new_off, len(raw)))
            old_end = ordered[-1].offset + ordered[-1].size if ordered else 0
            buf += old[old_end:]

            payload = bytes(buf)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_bytes(payload)

            for ent, new_off, raw_len in ends:
                ent_usize = (len(changes[ent.name]) if ent.name in changes
                             else ent.usize)
                _patch_record(rec_bytes, ent.record_offset, self.version,
                              usize=ent_usize, offset=new_off, size=raw_len)
            mode = "rebuild"

        out_h.write_bytes(bytes(rec_bytes))
        return {"updated": [e.name for e, _p, _r in todo], "mode": mode,
                "bytes_in": sum(len(p) for _e, p, _r in todo)}


class DfpfPackError(RuntimeError):
    """dfpf 包格式/IO 异常。"""


# --------------------------------------------------------------------- 便捷函数

def find_packs(root: Path, max_depth: int = 5) -> List[DfpfPack]:
    """在游戏目录里找出所有能打开的 dfpf 包（按名字排序，跳过打不开的）。"""
    from .scan import iter_files
    packs: List[DfpfPack] = []
    for p in iter_files(root, max_depth=max_depth):
        if p.suffix.lower() != ".~h":
            continue
        if not DfpfPack.looks_like(p):
            continue
        if not p.with_suffix(".~p").is_file():
            continue
        try:
            packs.append(DfpfPack.open(p))
        except (DfpfPackError, OSError):
            continue
    packs.sort(key=lambda pk: pk.h_path.name.lower())
    return packs
