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
import shutil
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

#: ``.~p`` 里每个资源的起始位置必须按这个粒度对齐。
#:
#: 实测 CQ2 全部 14 个 ``.~p``（合计 30 万个资源）**无一例外**：
#: 每个资源的 offset 都是 2048 的整数倍。这不是巧合，是打包器的分配
#: 粒度 —— 我们回填时必须照做，否则游戏按自己的规则定位会读到碎片。
SECTOR = 2048

#: ``.~h`` 偏移 48 处 8 字节字段 = **数据区结束位置**。
#:
#: 14 个包里 13 个精确等于「最后一个资源的结束位置向上取整到 2048」，
#: 且它之后到文件末尾是预分配的填充区（CQ2 用 0x7A 填满）。
#: 回填时把资源搬到数据区末尾，这个字段必须同步改 —— 不改的话游戏
#: 按旧长度认数据区，新资源直接落在它的视野之外（实测症状：游戏启动
#: 后卡死、窗口关不掉）。
DATA_END_OFF = 48

#: 探不到填充字节时的兜底值（CQ2 实际用的就是 0x7A）。
DEFAULT_PAD_BYTE = 0x7A


def _align(n: int, unit: int = SECTOR) -> int:
    return ((n + unit - 1) // unit) * unit


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


def _field_max(version: int, fname: str) -> Optional[int]:
    """某个字段在记录里最多能表示到几（位宽决定）。

    位域字段写超了会被静默截断 —— 那就是"写进去一个偏移、游戏读到另一个
    偏移"，属于最难查的一类损坏。回填前先问一句，宁可报错也别写坏。
    返回 ``None`` = 推导不出（字段不存在），由调用方决定是否放行。
    """
    bits = _layout(version).get(fname) or ()
    if not bits:
        return None
    return (1 << (max(w for _b, w in bits) + 1)) - 1


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
    data_end = len(blob)
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
    struct.pack_into(">Q", head, 48, data_end)      # 数据区结束位置
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

@dataclass(eq=False)
class DfpfEntry:
    """包内一条资源记录。

    ``eq=False`` 是故意的：资源名**可以重复**（CQ2 的 DLC1_Stuff 里
    ``..._leet`` 就有两条），回填时得按"哪一条对象"来指定目标，
    所以按对象身份比较/哈希，不能按字段值。
    """
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
                 records_offset: int, file_size: int, data_end: int = 0):
        self.h_path = Path(h_path)
        self.p_path = Path(h_path).with_suffix(".~p")
        self.version = version
        self.header = header
        self.entries = entries
        self.type_names = type_names
        self.records_offset = records_offset
        self.file_size = file_size
        #: ``.~p`` 里"资源数据区"的结束位置（见 DATA_END_OFF）。0 = 包里没记。
        self.data_end = data_end

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

        # 「数据区结束位置」：CQ2 实测 13/14 个包等于最后一个资源结束位置
        # 向上取整到 2048。读不到 / 明显荒谬就当没记（回填时按资源推断）。
        data_end = 0
        if len(head) >= DATA_END_OFF + 8:
            cand = struct.unpack_from(">Q", head, DATA_END_OFF)[0]
            if 0 < cand < (1 << 40):
                data_end = cand

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
                   records_offset=records_off, file_size=len(head),
                   data_end=data_end)

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

    def find_all(self, name: str) -> List[DfpfEntry]:
        """按名字找出**所有**同名资源。

        CQ2 的 ``DLC1_Stuff`` 里 ``stringtable/costumequestdlc1_leet`` 就
        出现了两次（一条未压缩的短表 + 一条 zlib 的长表）。只认第一个的话，
        译文会写到错的对象上，而真正那张台词表永远轮不到 —— 回填时用
        「原文能不能对上」来认领，就能自动挑对。
        """
        return [e for e in self.entries if e.name == name]

    def by_ext(self, *exts: str) -> List[DfpfEntry]:
        want = {e.lower().lstrip(".") for e in exts}
        return [e for e in self.entries if e.ext in want]

    # -------------------------------------------------- 回填

    @property
    def pad_byte(self) -> int:
        """数据区之后的"预分配填充"用的是哪个字节。

        CQ2 的 ``.~p`` 用 ``0x7A``（``'z'``）把数据区后面填满。补新数据
        时沿用同一个值，免得在文件里留下两种风格的空白。
        """
        try:
            with open(self.p_path, "rb") as f:
                f.seek(self.data_end if self.data_end > 0 else 0)
                chunk = f.read(4096)
        except OSError:
            return DEFAULT_PAD_BYTE
        for b in chunk:
            if b:
                return b
        return 0

    def _data_end(self) -> int:
        """数据区结束位置；包里没记就按最后一个资源的结束位置推断。"""
        if self.data_end > 0:
            return self.data_end
        ends = [e.offset + e.size for e in self.entries if e.size]
        return _align(max(ends)) if ends else 0

    def write(self, changes: Dict[str, bytes], out_h: Path,
              out_p: Path) -> dict:
        """把 ``{资源名: 新内容(未压缩字节)}`` 写进包副本。

        两条路，优先走副作用小的那条：

        1. **原地替换** —— 新压缩数据塞得进原槽位（到下一条资源的距离）
           就只改 ``.~p`` 里那一段，并把记录里的长度字段改掉。**数据区
           布局一个字节不动**，索引里的"数据区结束位置"也不用动。
        2. **末尾追加** —— 塞不下的搬到数据区末尾（**按 2048 对齐**），
           更新这条记录的偏移，并把索引里的"数据区结束位置"一并改掉。

        ⚠️ 两条硬约束（实测 CQ2 全部 14 个 ``.~p``、30 万个资源得到）：

        · 每个资源在 ``.~p`` 里的起始位置都按 :data:`SECTOR` 对齐；
        · ``.~h`` 的 :data:`DATA_END_OFF` 处记着数据区结束位置，之后是
          预分配填充。**搬动任何资源都必须同步更新它** —— 否则游戏按
          旧长度认定数据区，新资源落在视野之外（实测症状：启动卡死）。

        Args:
            changes: 资源名 → 新内容（未压缩）。名字必须在包内存在。
            out_h / out_p: 产出的索引与数据文件路径。

        Returns:
            {"updated": 改了哪些资源, "mode": "inplace"|"append"|"mixed",
             "bytes_in": 新数据总长, "data_end": 新的数据区结束位置}
        """
        out_h, out_p = Path(out_h), Path(out_p)
        todo: List[tuple] = []
        for key, payload in changes.items():
            # key 既可以是资源名（老写法，取第一个同名的），也可以直接是
            # DfpfEntry —— 同名资源（见 find_all）必须用后者才指得准。
            ent = key if isinstance(key, DfpfEntry) else self.find(key)
            if ent is None or ent.comp == COMP_XMEM or ent.size <= 0:
                continue
            new_raw = (zlib.compress(payload, 9) if ent.comp == COMP_ZLIB
                       else bytes(payload))
            todo.append((ent, payload, new_raw))
        if not todo:
            return {"updated": [], "mode": "none", "bytes_in": 0,
                    "data_end": self._data_end()}

        ordered = sorted([e for e in self.entries if e.size],
                         key=lambda e: e.offset)
        slot_of: Dict[int, int] = {}
        for i, e in enumerate(ordered):
            if i + 1 < len(ordered):
                slot_of[id(e)] = max(0, ordered[i + 1].offset - e.offset)

        data_end = self._data_end()
        if data_end <= 0:
            raise DfpfPackError(f"{self.h_path.name} 里找不到数据区，拒绝回填")

        off_max = _field_max(self.version, "offset")
        size_max = _field_max(self.version, "size")

        rec_bytes = bytearray(self.header)
        out_h.parent.mkdir(parents=True, exist_ok=True)
        # 磁盘到磁盘复制：57 MB / 295 MB 的包也不会把内存吃穿
        shutil.copyfile(self.p_path, out_p)

        # ---- 分派：能塞进原槽位的原地改，塞不下的排队追加 ----
        plan: List[tuple] = []          # (ent, payload, raw, keep 或 None)
        append_items: List[tuple] = []
        for ent, payload, raw in todo:
            slot = slot_of.get(id(ent), max(0, data_end - ent.offset))
            # 未压缩资源的 size 必须**精确等于**数据长度（游戏照它读多少）；
            # zlib 的要保证整条压缩流都在，多出来的位置清零即可 ——
            # 解压时尾部的多余字节会被忽略。
            keep = (len(raw) if ent.comp != COMP_ZLIB
                    else max(len(raw), ent.size))
            if len(raw) <= slot and ent.offset + keep <= data_end:
                plan.append((ent, payload, raw, keep))
            else:
                append_items.append((ent, payload, raw))

        if append_items and off_max is not None:
            need = data_end + sum(
                _align(len(r)) for _e, _p, r in append_items)
            if need > off_max:
                raise DfpfPackError(
                    f"{self.h_path.name} 的索引只能表示 {off_max} 字节内的"
                    f"偏移，回填后需要 {need} —— 这个包太大了，暂不支持回填")

        names_inplace: List[str] = []
        names_appended: List[str] = []
        with open(out_p, "r+b") as f:
            # ---- 路 1：原地替换（补齐到原长度，多出来的位置清零） ----
            for ent, payload, raw, keep in plan:
                f.seek(ent.offset)
                f.write(raw + b"\x00" * (keep - len(raw)))
                vals = {"usize": len(payload), "size": keep}
                if size_max is not None and keep > size_max:
                    raise DfpfPackError(
                        f"{ent.name} 压缩后有 {keep} 字节，超出索引字段上限")
                _patch_record(rec_bytes, ent.record_offset, self.version, **vals)
                names_inplace.append(ent.name)

            # ---- 路 2：搬到数据区末尾（每个都 2048 对齐） ----
            new_data_end = data_end
            if append_items:
                cur = data_end
                for ent, payload, raw in append_items:
                    cur = _align(cur)
                    f.seek(cur)
                    f.write(raw)
                    _patch_record(rec_bytes, ent.record_offset, self.version,
                                  usize=len(payload), size=len(raw), offset=cur)
                    names_appended.append(ent.name)
                    cur += len(raw)
                new_data_end = _align(cur)
                if new_data_end > cur:                 # 数据区尾部补齐
                    f.seek(cur)
                    f.write(bytes([self.pad_byte]) * (new_data_end - cur))
                if new_data_end != data_end:
                    # ★ 关键：告诉游戏"数据区到这儿为止"
                    struct.pack_into(">Q", rec_bytes, DATA_END_OFF, new_data_end)

        out_h.write_bytes(bytes(rec_bytes))
        if names_inplace and names_appended:
            mode = "mixed"
        elif names_appended:
            mode = "append"
        else:
            mode = "inplace"
        return {"updated": [e.name for e, _p, _r in todo], "mode": mode,
                "bytes_in": sum(len(p) for _e, p, _r in todo),
                "data_end": new_data_end if append_items else data_end,
                "inplace": len(names_inplace), "appended": len(names_appended)}


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
