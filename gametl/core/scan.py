# -*- coding: utf-8 -*-
"""有界目录扫描工具。

游戏目录动辄数万个文件（素材、音频、多语言资源），
无限制的 ``rglob("*")`` 在机械盘上会把界面卡死（实测 Z 盘 3.7T 机械盘）。

本模块提供「限深度 + 限数量 + 限时间」的安全遍历：
- 广度优先，保证浅层的封包/脚本先被找到
- 到达任一上限即停止，并通过 ScanStats 告知调用方「结果可能不完整」
- 跳过明显无关的目录（.git / node_modules / 输出目录等）
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Iterator, Optional, Set

# 这些目录不会有游戏文本/封包，直接跳过
DEFAULT_SKIP_DIRS: Set[str] = {
    "__pycache__", ".git", ".svn", ".hg", "node_modules",
    "$RECYCLE.BIN", "System Volume Information",
    "_work", "_汉化输出", "_hanhua_output",
}

# 经验值：KiriKiri 的 .xp3 多在根目录，Ren'Py 的 .rpa 在 game/ 下
DEFAULT_MAX_DEPTH = 4
DEFAULT_MAX_FILES = 40000
DEFAULT_TIME_BUDGET = 20.0  # 秒


class ScanStats:
    """扫描过程统计，用于向用户解释「为什么没找到某些文件」。"""

    def __init__(self) -> None:
        self.files = 0
        self.dirs = 0
        self.truncated = False
        self.reason = ""
        self.elapsed = 0.0

    def note(self) -> str:
        if not self.truncated:
            return f"扫描 {self.files} 个文件（用时 {self.elapsed:.1f}s）"
        return (f"扫描提前结束（{self.reason}），"
                f"已检查 {self.files} 个文件（用时 {self.elapsed:.1f}s）")


def iter_files(root: Path,
               *,
               max_depth: int = DEFAULT_MAX_DEPTH,
               max_files: int = DEFAULT_MAX_FILES,
               time_budget: float = DEFAULT_TIME_BUDGET,
               skip_dirs: Optional[Set[str]] = None,
               stats: Optional[ScanStats] = None) -> Iterator[Path]:
    """广度优先遍历文件，带深度/数量/时间三重上限。

    Args:
        root: 起始目录
        max_depth: 最大递归深度（root 自身为 0）
        max_files: 最多产出多少个文件
        time_budget: 最长耗时（秒）
        skip_dirs: 额外要跳过的目录名
        stats: 传入 ScanStats 以获取统计信息

    Yields:
        文件路径
    """
    root = Path(root)
    skip = DEFAULT_SKIP_DIRS | set(skip_dirs or ())
    st = stats or ScanStats()

    t0 = time.monotonic()
    queue: list[tuple[Path, int]] = [(root, 0)]
    head = 0
    stop = False

    while head < len(queue) and not stop:
        d, depth = queue[head]
        head += 1

        if time.monotonic() - t0 > time_budget:
            st.truncated = True
            st.reason = f"超过 {time_budget:g} 秒时间上限"
            break

        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if depth + 1 > max_depth or e.name in skip:
                                continue
                            st.dirs += 1
                            queue.append((Path(e.path), depth + 1))
                        elif e.is_file(follow_symlinks=False):
                            if st.files >= max_files:
                                st.truncated = True
                                st.reason = f"超过 {max_files} 个文件上限"
                                stop = True
                                break
                            st.files += 1
                            yield Path(e.path)
                    except OSError:
                        continue
        except (PermissionError, OSError):
            continue

    st.elapsed = time.monotonic() - t0


def find_by_suffix(root: Path, suffixes: Set[str], **kw) -> list[Path]:
    """按后缀收集文件（后缀需小写、含点）。"""
    want = {s.lower() for s in suffixes}
    return [p for p in iter_files(root, **kw) if p.suffix.lower() in want]


def count_files(root: Path, **kw) -> int:
    """快速统计文件数量（受同样上限约束）。"""
    return sum(1 for _ in iter_files(root, **kw))
