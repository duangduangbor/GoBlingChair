"""提取器基类。所有引擎提取器实现统一接口。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..core.models import Project, TextUnit


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
            统计信息字典（写回文件数、替换条数等）。
        """
        raise NotImplementedError
