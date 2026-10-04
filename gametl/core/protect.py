"""保护片段：识别文本中不应被翻译的部分（变量、标签、转义符）。

动机：翻译模型看到 [name]、%s、\\n、<color=#fff> 这类片段时，
经常擅自翻译、删除或改变它们，导致回填后游戏崩溃或显示错乱。
策略：翻译前把受保护片段替换为占位 token，翻译后还原。
"""
from __future__ import annotations

import re

# 按优先级排列的保护模式。每个模式必须能在原文与译文中保持一一对应。
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Ren'Py 变量插值 [name]、[player.name]
    ("renpy_var", re.compile(r"\[[A-Za-z_][\w\.\[\]'\"]*\]")),
    # 引擎标签 @name、\\n 转义、$var
    ("escape", re.compile(r"\\[nrt\"'\\]")),
    # 花括号占位 {0}、{name}
    ("brace", re.compile(r"\{[^{}]{0,40}\}")),
    # 百分号占位 %s %d %1$s %%
    ("percent", re.compile(r"%\d*\$?[sdifxXeEgc%]")),
    # Unity TextMeshPro 富文本 <color=...> </color> <b> 等
    ("unity_rich", re.compile(r"</?[a-zA-Z][\w\-]*(?:\s*=\s*[\"'][^\"']*[\"'])?\s*/?>")),
    # RPG Maker 的 \V[1] \N[1] \C[1] \I[1] 控制码
    ("rpgmaker_ctrl", re.compile(r"\\[VNCIEX]{1,2}\[\d+\]")),
    # 美元符号变量 $gameVariables...
    ("dollar_var", re.compile(r"\$[A-Za-z_][\w\.\[\]]*")),
]

TOKEN_FMT = "\u3010{index}\u3011"  # 【0】【1】这种全角占位，模型很少改动


def protect(text: str) -> tuple[str, list[str]]:
    """把受保护片段替换为占位符。

    Returns:
        (处理后的文本, 被保护片段列表)
    """
    protected: list[str] = []

    def _repl(match: re.Match[str]) -> str:
        token = TOKEN_FMT.format(index=len(protected))
        protected.append(match.group(0))
        return token

    result = text
    # 依次应用所有模式；每轮替换后 token 本身不会再被后续模式匹配（全角括号安全）
    for _, pattern in PATTERNS:
        result = pattern.sub(_repl, result)
    return result, protected


def restore(text: str, protected: list[str]) -> str:
    """把占位符还原为原始受保护片段。"""
    result = text
    for i, frag in enumerate(protected):
        token = TOKEN_FMT.format(index=i)
        result = result.replace(token, frag)
        # 容错：模型偶尔把【】写成 [] 或 () 或加上空格
        for variant in (f"[{i}]", f"({i})", f"【 {i} 】", f"【{i} 】", f"【 {i}】"):
            result = result.replace(variant, frag)
    return result


def has_protected(text: str) -> bool:
    """快速判断文本中是否存在受保护片段。"""
    return any(p.search(text) for _, p in PATTERNS)
