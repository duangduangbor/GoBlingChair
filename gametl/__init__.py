"""gametl —— 本地游戏文本汉化工具链。

用法示例：
    from gametl.pipeline import run_extract, run_translate, run_writeback

    run_extract("game_decoded/", "project.json")
    run_translate("project.json", model="qwen2.5:7b")
    run_writeback("project.json", "translated/")
"""
from .core.models import EngineType, Project, TextKind, TextUnit

__version__ = "0.1.0"
__all__ = ["EngineType", "Project", "TextKind", "TextUnit", "__version__"]
