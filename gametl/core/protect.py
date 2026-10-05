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
    # 转义序列：Yarn 里 `\:` 是字面冒号（不代表说话人）、`\[` `\]` 是字面
    # 方括号（不是 markup）、`\{` 是字面花括号（不是表达式）。必须排在所有
    # 「方括号 / 花括号」模式**之前**，否则 `\[wave\]` 会被当成标签保护掉，
    # 语义虽等价（还原后仍是原文），但会掩盖真实意图、也更容易被模型改写。
    ("escape_seq", re.compile(r"\\[\[\]{}<>#/:\\]")),
    # Yarn 的**任意** hashtag：`#line:xxx`、`#tone:sarcastic`、`#lastline`、
    # `#duplicate`。词法规定 hashtag 是 `#` 后接 `~[ \t\r\n#$<]+`，且不显示
    # 给玩家。早期只认 `#line:`，于是 `Homer: Hi. #tone:sarcastic` 里的
    # `#tone:sarcastic` 会裸露给模型 —— 翻掉或删掉，回填后行尾多出垃圾。
    # 放在 renpy_var 之后：`#` 不会出现在 `[name]` 里的合法位置。
    ("yarn_hashtag", re.compile(r"#[^\s#$<]{1,64}")),
    # 方括号标签的**闭合/带参**写法：[/wave]、[color=999999]、[/all]。
    # renpy_var 只认 `[字母]` 这种纯名字，认不出 `/` 开头和 `=` 带参的，
    # 于是 NITW 的 `[color=999999]…[/all]` 会漏保护、被模型吃掉或改写，
    # 回填后游戏显示错乱。
    # 前面那个 `(?<!\[)` 很关键：没有它，正则会在 `[[Node]]` 的**第二个**
    # `[` 处匹配出 `[Node]`，把本该由 yarn_jump 整体保护的节点引用切碎。
    ("square_tag", re.compile(r"(?<!\[)\[/?[A-Za-z][\w\-]*(?:\s*=\s*[^\]\s]*)?\]")),
    # 引擎标签 @name、\\n 转义、$var
    ("escape", re.compile(r"\\[nrt\"'\\]")),
    # 花括号占位 {0}、{name}。上限从 40 放宽到 120：Unity 的属性块会很长，
    # 例如 `{align=middle,locator=MaeFenceTalk,width=2}` 正好 40 字符，
    # 再多一个属性就漏保护了。
    ("brace", re.compile(r"\{[^{}]{0,120}\}")),
    # 百分号占位 %s %d %1$s %%
    ("percent", re.compile(r"%\d*\$?[sdifxXeEgc%]")),
    # Unity TextMeshPro 富文本 <color=...> </color> <b> 等
    ("unity_rich", re.compile(r"</?[a-zA-Z][\w\-]*(?:\s*=\s*[\"'][^\"']*[\"'])?\s*/?>")),
    # RPG Maker 的 \V[1] \N[1] \C[1] \I[1] 控制码
    ("rpgmaker_ctrl", re.compile(r"\\[VNCIEX]{1,2}\[\d+\]")),
    # 美元符号变量 $gameVariables...
    ("dollar_var", re.compile(r"\$[A-Za-z_][\w\.\[\]]*")),
    # Yarn Spinner 的 #line:xxxxxx 行注释标记（Unity 对话脚本）
    ("yarn_line_tag", re.compile(r"#line:[0-9a-fA-F]+\b")),
    # Yarn Spinner 的变量/逻辑标签 <<set $x>> <<if $x is 0>> 等。
    # ⚠️ 必须用**非贪婪** `.*?` 而不是 `[^>]*`：Yarn 的条件里会出现 `>` 或
    # `>=`（`<<if $x > 90>>`、`<<if $count >= 3>>`），用「不含右尖括号」
    # 的写法整条都匹配不上 —— 于是这类命令会裸露给模型（旧版实测
    # BandPractice.yarn 有上百条），被翻掉就写坏脚本。命令在 `>>` 处结束，
    # 条件里不可能出现 `>>`，所以非贪婪是安全的。
    ("yarn_cmd", re.compile(r"<<.*?>>")),
    # Yarn Spinner 的跳转/节点引用 [[NodeName]] 与 === 分隔
    ("yarn_jump", re.compile(r"\[\[[^\]]+\]\]")),
]

TOKEN_FMT = "\u3010{index}\u3011"  # 【0】【1】这种全角占位，模型很少改动


def protect(text: str) -> tuple[str, list[str]]:
    """把受保护片段替换为占位符。

    Returns:
        (处理后的文本, 被保护片段列表)
    """
    protected: list[str] = []

    token_re = re.compile(r"\u3010(\d+)\u3011")

    def _repl(match: "re.Match[str]") -> str:
        frag = match.group(0)
        # **嵌套展开**：后一个模式匹配到的范围可能横跨前一个模式留下的
        # token（典型：`[[Node]]` 整体保护时里面还嵌着 `[name]` 的 token，
        # `<<set $x to 1>>` 里嵌着 `$x` 的 token）。若原样存进列表，
        # 还原时就再也没机会把内层 token 展开，游戏文本里会留下 `【1】`
        # 这种字面量。这里先用已收集的片段把内层 token 还原成原文再存。
        if "\u3010" in frag:
            frag = token_re.sub(
                lambda m: (protected[int(m.group(1))]
                           if 0 <= int(m.group(1)) < len(protected)
                           else m.group(0)),
                frag)
        token = TOKEN_FMT.format(index=len(protected))
        protected.append(frag)
        return token

    result = text
    # 依次应用所有模式；每轮替换后 token 本身不会再被后续模式匹配（全角括号安全）
    for _, pattern in PATTERNS:
        result = pattern.sub(_repl, result)
    return result, protected


def restore(text: str, protected: list[str]) -> str:
    """把占位符还原为原始受保护片段。

    ⚠️ **必须单遍扫描替换**，不能像早期那样 `str.replace` 循环。原因：
    一个受保护片段内部可能含另一个 token（例如 ``[[Node]]`` 整体被保护，
    而它里面又含 ``[name]`` 的 token）。用 replace 逐个换的话，先换完的
    片段里那些 token 就再也不会被处理，还原结果会残留 ``【1】`` 这种
    字面量，直接写进游戏文本里。
    """
    # 先把模型可能写歪的变体归一化回标准 token
    for i in range(len(protected)):
        token = TOKEN_FMT.format(index=i)
        for variant in (f"[{i}]", f"({i})", f"【 {i} 】", f"【{i} 】", f"【 {i}】"):
            if variant != token:
                text = text.replace(variant, token)

    def _sub(m: "re.Match[str]") -> str:
        i = int(m.group(1))
        return protected[i] if 0 <= i < len(protected) else m.group(0)

    # 单遍替换：re.sub 不会重扫刚插入的内容，嵌套片段因此能正确展开
    return re.sub(r"\u3010(\d+)\u3011", _sub, text)


def has_protected(text: str) -> bool:
    """快速判断文本中是否存在受保护片段。"""
    return any(p.search(text) for _, p in PATTERNS)
