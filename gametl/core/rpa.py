"""Ren'Py RPA 封包格式解析与读写（纯 Python 实现）。

RPA 是 Ren'Py 的资源归档格式，比 XP3 简单得多。

RPA 2.0 / 3.0 结构：
    "RPA-3.0 " + 十六进制索引偏移 + " " + 十六进制密钥 + "\\n"
    索引区在 index_offset 处（zlib 压缩的 pickle 或 明文 pickle）

RPA 3.2：用了不同的路径编码。
本实现覆盖 RPA 2.0 / 3.0（最常见）。

索引是一个 dict: {路径: [(偏移, 长度, 前缀), ...]}
读取时用 密钥 对偏移做 XOR 混淆。
"""
from __future__ import annotations

import pickle
import struct
import zlib
from pathlib import Path


class RPAError(Exception):
    """RPA 解析错误。"""


MAGIC_20 = b"RPA-2.0 "
MAGIC_30 = b"RPA-3.0 "
MAGIC_32 = b"RPA-3.2 "


def is_rpa(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(8)
        return head.startswith((MAGIC_20, MAGIC_30, MAGIC_32))
    except OSError:
        return False


class RPAArchive:
    """RPA 归档读取器。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.index: dict = {}
        self.version = ""
        self.key = 0
        self.index_offset = 0

    def read_index(self) -> dict:
        data = self.path.read_bytes()
        header = data[:8]
        if header.startswith(MAGIC_32):
            self.version = "3.2"
            magic_len = 8
        elif header.startswith(MAGIC_30):
            self.version = "3.0"
            magic_len = 8
        elif header.startswith(MAGIC_20):
            self.version = "2.0"
            magic_len = 8
        else:
            raise RPAError(f"不是有效的 RPA 文件: {self.path.name}")

        # 头部：magic + "offset key\n"（十六进制）
        line_end = data.index(b"\n", magic_len)
        spec = data[magic_len:line_end].decode("ascii").strip()
        parts = spec.split()
        self.index_offset = int(parts[0], 16)
        if len(parts) > 1:
            self.key = int(parts[1], 16)

        raw_index = data[self.index_offset:]
        try:
            index = zlib.decompress(raw_index)
        except zlib.error:
            index = raw_index

        self.index = pickle.loads(index)
        return self.index

    def extract_all(self, out_dir: Path) -> dict:
        """解包所有文件。"""
        out_dir = Path(out_dir)
        if not self.index:
            self.read_index()

        data = self.path.read_bytes()
        # 数据区起点 = header 结束处（magic 之后到第一个 \n）
        header_end = data.index(b"\n", 8) + 1
        count = 0
        failed = 0

        for name, value in self.index.items():
            if isinstance(value, list) and value:
                first = value[0]
                offset = first[0] ^ self.key
                length = first[1] ^ self.key
                # 偏移是相对数据区起点的，需加上 header 长度
                start = header_end + offset
                try:
                    content = data[start:start + length]
                    if len(content) != length:
                        failed += 1
                        continue
                    clean_name = self._clean_name(name)
                    dst = out_dir / clean_name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(content)
                    count += 1
                except OSError:
                    failed += 1
            else:
                failed += 1

        return {"extracted": count, "failed": failed, "total": len(self.index)}

    @staticmethod
    def _clean_name(name: str) -> str:
        """清理路径（处理 Ren'Py 的特殊前缀）。"""
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        return name.lstrip("/").replace("\\", "/")

    @staticmethod
    def pack(files: list[tuple[str, bytes]], out_path: Path) -> None:
        """打包为 RPA-3.0（标准格式）。

        密钥固定为 0x42424242（常见做法），偏移与长度做 XOR。
        """
        key = 0x42424242
        body = bytearray()
        index: dict[str, list[tuple[int, int, bytes]]] = {}

        for name, content in files:
            offset = len(body)
            body.extend(content)
            index[name] = [(offset ^ key, len(content) ^ key, b"")]

        pickled = pickle.dumps(index, protocol=2)
        compressed = zlib.compress(pickled)

        # header 形如："RPA-3.0 " + "016x偏移" + " " + "08x密钥" + "\n"
        # 先算出 header 长度，再确定索引的绝对偏移
        dummy_header = MAGIC_30 + b"%016x %08x\n" % (0, key)
        header_len = len(dummy_header)
        index_offset = header_len + len(body)
        header = MAGIC_30 + b"%016x %08x\n" % (index_offset, key)

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(header)
            f.write(bytes(body))
            f.write(compressed)
