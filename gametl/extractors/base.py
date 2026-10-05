"""提取器基类。所有引擎提取器实现统一接口。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..core.models import Project, TextUnit

# --------------------------------------------------------------- 回填统计规范
#
# 回填的返回值**必须字段统一**，因为有两个下游按它办事：
#   · auto.py         —— 打日志（"写出 N 个文件（M 处译文）"）
#   · core/patcher.py —— 按 `changed` 决定**哪些文件要覆盖进游戏目录**
#
# 历史坑：各引擎各写各的字段名（KiriKiri/Ren'Py 用 lines_replaced、
# Unity 用 segments_replaced、只有 RPG Maker 用 fields_replaced），于是
#   1) 主流程 `stats['fields_replaced']` 直接 KeyError 崩掉（Unity 首次跑就炸）；
#   2) KiriKiri / Ren'Py / Unity 的 `changed` 一直是空的 —— 补丁安装器
#      「装完了」但一个文件都没写进去。
#
# 新增引擎请一律用 wb_stats() 构造返回值。

#: 「替换了多少处译文」的历史字段名，按兼容顺序读取
_REPLACED_KEYS = ("fields_replaced", "lines_replaced", "segments_replaced",
                  "replaced")


def wb_stats(files_written: int = 0, replaced: int = 0, unchanged: int = 0,
             changed=None, **extra) -> dict:
    """构造标准的回填统计字典。

    Args:
        files_written: 产出到 out_dir 的文件数
        replaced: 替换掉的文本片段数
        unchanged: 内容与原文件相同、无实质变化的文件数
        changed: 内容确实变了的文件（相对路径列表）—— 补丁安装器要用它
        **extra: 引擎专属附加信息（如 Unity 的 binary_files）
    """
    stats = {
        "files_written": int(files_written),
        "fields_replaced": int(replaced),
        "unchanged": int(unchanged),
        "changed": [str(c) for c in (changed or [])],
    }
    stats.update(extra)
    return stats


def wb_replaced(stats: dict) -> int:
    """兼容读取「替换了多少处译文」——老的 lines_replaced 等写法也能读。"""
    for key in _REPLACED_KEYS:
        if key in stats:
            try:
                return int(stats[key] or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def write_back_file(src: Path, dst: Path, payload: bytes) -> tuple[bool, bool]:
    """落盘一个回填产物，返回 (是否真的写了, 内容是否和原文件不同)。

    两个返回值各有用处，别混：
    · 第一个 —— 内容与**上一次的产物**相同就不落盘。保持 mtime 稳定，
      「这次导出哪些文件变了」才能靠 (大小, mtime) 一眼看出来，增量导出才成立。
    · 第二个 —— 内容与**原游戏文件**不同才算「汉化改动了它」，补丁安装器
      只覆盖这一批文件。
    """
    src, dst = Path(src), Path(dst)
    payload = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
    try:
        differs_from_source = not src.is_file() or src.read_bytes() != payload
    except OSError:
        differs_from_source = True
    try:
        if dst.is_file() and dst.read_bytes() == payload:
            return False, differs_from_source
    except OSError:
        pass
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(payload)
    return True, differs_from_source


class BaseExtractor(ABC):
    """引擎文本提取器接口。

    子类需实现 extract()：从解包后的目录中提取文本单元。
    write_back()：把译文写回副本，产出可打包的修改版文件。
    """

    #: 该提取器负责的引擎
    engine = None

    def __init__(self, decoded_dir: Path):
        """
        Args:
            decoded_dir: 已解包的资源目录（各引擎解包工具产物）。
        """
        self.decoded_dir = Path(decoded_dir)

    @abstractmethod
    def extract(self) -> list[TextUnit]:
        """提取所有待翻译文本单元。"""
        raise NotImplementedError

    @abstractmethod
    def write_back(self, project: Project, out_dir: Path) -> dict:
        """把译文回填，输出到 out_dir。

        Returns:
            用 wb_stats() 构造的标准统计字典。**不要自己拼字段名** ——
            files_written / fields_replaced / unchanged / changed 这四个键
            是下游（主流程、补丁安装器）依赖的契约。
        """
        raise NotImplementedError
