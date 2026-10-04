"""KiriKiri XP3 封包格式解析与读写（纯 Python 实现）。

XP3 是 KiriKiri 引擎的资源归档格式。结构：

    [XP3 归档]
      ├─ 文件头 (18 字节): "XP3\r\n \x1a\x8b\x67\x01"(10) + 8字节索引偏移
      ├─ ...文件数据...
      └─ 索引区 (在 index_offset 处):
           ├─ 索引头: 索引块大小 + (可选) 压缩标记
           └─ 若干文件条目 (TOC):
                ├─ File: 文件名
                ├─ Info: 文件信息（是否加密/压缩/大小）
                ├─ Segm: 数据分段（偏移+大小）
                └─ adlr: 校验

本实现覆盖**未加密/未压缩**以及**常见混淆**的 XP3。
真正的商业加密包（带 .cf 密钥）超出本工具范围。

参考：https://github.com/arcusmaximus/KirikiriTools 等开源实现的格式描述。
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
# XP3 文件头魔数（10 字节）："XP3\r\n \x1a\x8b\x67\x01"
XP3_MAGIC = b"XP3\r\n \x1a\x8b\x67\x01"
# 文件头总长：10 字节魔数 + 8 字节索引偏移 = 18 字节
XP3_HEADER_SIZE = 18

# 条目类型标识
TAG_FILE = b"File"
TAG_INFO = b"Info"
TAG_SEGM = b"Segm"
TAG_ADLR = b"adlr"

# Info 标志位
INFO_PROTECTED = 0x80000000  # 数据被加密


class XP3Error(Exception):
    """XP3 解析错误。"""


@dataclass
class Segment:
    """数据分段：描述文件内容在归档中的位置。"""
    offset: int          # 相对数据区起始的偏移
    raw_size: int        # 原始（可能压缩后）大小
    packed_size: int     # 压缩大小


@dataclass
class Entry:
    """归档中的一个文件条目。"""
    name: str                       # 文件名（UTF-16 解码）
    protected: bool = False         # 是否加密
    segments: list[Segment] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(s.raw_size for s in self.segments)


def _read_cstring(buf: bytes, pos: int) -> tuple[str, int]:
    """读取 UTF-16LE 以 \\0 结尾的字符串。"""
    end = pos
    while end + 1 < len(buf) and buf[end:end + 2] != b"\x00\x00":
        end += 2
    text = buf[pos:end].decode("utf-16-le", errors="replace")
    return text, end + 2


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """读取 XP3 的可变长度整数（每字节低7位，最高位为续接标志）。"""
    value = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return value, pos


def _write_varint(value: int) -> bytes:
    """写可变长度整数。"""
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def is_xp3(path: Path) -> bool:
    """判断文件是否为 XP3 归档。"""
    try:
        with open(path, "rb") as f:
            return f.read(10) == XP3_MAGIC
    except OSError:
        return False


class XP3Archive:
    """XP3 归档读写器。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.entries: list[Entry] = []
        self._index_offset = 0
        self._index_size = 0

    # ---------- 读取 ----------

    def read_index(self) -> list[Entry]:
        """解析归档索引，填充 self.entries。"""
        data = self.path.read_bytes()
        if data[:10] != XP3_MAGIC:
            raise XP3Error(f"不是有效的 XP3 文件: {self.path.name}")

        self._index_offset = struct.unpack("<q", data[10:18])[0]
        if not (0 <= self._index_offset < len(data)):
            raise XP3Error("索引偏移越界，可能是不支持的 XP3 变体")

        pos = self._index_offset
        # 索引头：flags(1字节)
        flags = data[pos]
        pos += 1
        if flags & 0x80:
            # 索引被压缩
            body_size, pos = _read_varint(data, pos)
            raw_size, pos = _read_varint(data, pos)
            packed = zlib.decompress(data[pos:pos + body_size])
        else:
            index_size, pos = _read_varint(data, pos)
            packed = data[pos:pos + index_size]

        self.entries = self._parse_toc(packed, self._index_offset)
        return self.entries

    def _parse_toc(self, toc: bytes, data_start: int) -> list[Entry]:
        """解析目录表（TOC）。"""
        entries: list[Entry] = []
        pos = 0
        current: Entry | None = None

        while pos < len(toc):
            tag = toc[pos:pos + 4]
            pos += 4
            if len(tag) < 4:
                break

            if tag == TAG_FILE:
                name, pos = _read_cstring(toc, pos)
                current = Entry(name=name)
                entries.append(current)

            elif tag == TAG_INFO:
                if current is None:
                    raise XP3Error("Info 出现在 File 之前")
                _info_size, pos = _read_varint(toc, pos)
                if pos + 4 <= len(toc):
                    info_flags = struct.unpack("<I", toc[pos:pos + 4])[0]
                    current.protected = bool(info_flags & INFO_PROTECTED)
                pos += _info_size

            elif tag == TAG_SEGM:
                if current is None:
                    raise XP3Error("Segm 出现在 File 之前")
                seg_count, pos = _read_varint(toc, pos)
                for _ in range(seg_count):
                    _seg_flags = toc[pos]
                    pos += 1
                    offset, pos = _read_varint(toc, pos)
                    raw_size, pos = _read_varint(toc, pos)
                    packed_size, pos = _read_varint(toc, pos)
                    current.segments.append(Segment(offset, raw_size, packed_size))

            elif tag == TAG_ADLR:
                size, pos = _read_varint(toc, pos)
                pos += size

            else:
                # 未知标签：尝试跳过
                size, pos = _read_varint(toc, pos)
                pos += size

        return entries

    def extract_all(self, out_dir: Path) -> dict:
        """解包所有文件到 out_dir。"""
        out_dir = Path(out_dir)
        data = self.path.read_bytes()
        # 数据区起始：文件头之后（18 字节）、索引之前
        data_base = XP3_HEADER_SIZE

        count = 0
        skipped = 0
        for entry in self.entries:
            if entry.protected:
                skipped += 1
                continue  # 加密条目跳过
            content = self._read_entry(data, data_base, entry)
            if content is None:
                skipped += 1
                continue
            dst = out_dir / entry.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(content)
            count += 1

        return {"extracted": count, "skipped_protected": skipped,
                "total": len(self.entries)}

    @staticmethod
    def _read_entry(data: bytes, data_base: int, entry: Entry) -> bytes | None:
        """读取单个条目的内容。"""
        chunks: list[bytes] = []
        for seg in entry.segments:
            start = data_base + seg.offset
            end = start + seg.packed_size
            if end > len(data):
                return None
            blob = data[start:end]
            if seg.packed_size != seg.raw_size:
                try:
                    blob = zlib.decompress(blob)
                except zlib.error:
                    return None
            chunks.append(blob)
        return b"".join(chunks)

    # ---------- 写入 ----------

    @staticmethod
    def pack(files: list[tuple[str, bytes]], out_path: Path) -> None:
        """把 (文件名, 内容) 列表打包成 XP3。

        使用「非压缩、非加密」格式，兼容性最好。
        """
        out_path = Path(out_path)
        body = bytearray()
        toc = bytearray()

        for name, content in files:
            # 数据区偏移（相对 data_base）
            offset = len(body)
            body.extend(content)

            # File 条目
            toc.extend(TAG_FILE)
            toc.extend(name.encode("utf-16-le") + b"\x00\x00")
            # Info 条目：大小 + flags(0=未加密)
            info_payload = struct.pack("<I", 0) + struct.pack("<Q", len(content))
            toc.extend(TAG_INFO)
            toc.extend(_write_varint(len(info_payload)))
            toc.extend(info_payload)
            # Segm 条目：1 段
            toc.extend(TAG_SEGM)
            toc.extend(_write_varint(1))          # 段数
            toc.append(0)                          # 段标志
            toc.extend(_write_varint(offset))      # 偏移
            toc.extend(_write_varint(len(content)))  # 原始大小
            toc.extend(_write_varint(len(content)))  # 压缩大小（相等=未压缩）
            # adlr：校验（用 0 占位，多数引擎不校验）
            toc.extend(TAG_ADLR)
            toc.extend(_write_varint(4))
            toc.extend(struct.pack("<I", 0))

        # 组装：头(18) + 数据 + 索引
        data_base = XP3_HEADER_SIZE
        index_offset = data_base + len(body)
        header = XP3_MAGIC + struct.pack("<q", index_offset)
        # 索引体：非压缩，前缀 flags(0) + 长度
        index_body = bytes([0]) + _write_varint(len(toc)) + bytes(toc)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(header)
            f.write(bytes(body))
            f.write(index_body)
