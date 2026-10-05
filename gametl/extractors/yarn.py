"""Yarn Spinner 脚本语法解析（结构感知、语法完整）。

## 为什么单独成模块

早期实现是「扁平正则扫描」：逐行套几条正则，猜哪些行是台词。这种写法
每遇到一种新形态就要补一条正则 —— 于是出现了两次返工（先漏 `->` 选项，
再漏 `[[文本|目标]]` 选项）。本模块改成**按官方编译器语法解析**，一次把
「哪些行是玩家可见文本」这件事定义清楚，后续新形态不再需要逐个打补丁。

## 语法依据

- Yarn Spinner 官方文档（v1.x 语言参考 + 3.x Scripting Fundamentals）
- 编译器 ANTLR 语法：``YarnSpinner.Compiler/Grammars/YarnSpinnerParser.g4``
  与 ``YarnSpinnerLexer.g4``（YarnSpinnerTool/YarnSpinner, main 分支）

关键结论（决定实现）：

1. ``node : (header|when_header|title_header)+ BODY_START body BODY_END``
   —— **每个节点必须以 ``---`` 开始正文、以 ``===`` 结束**。因此只要跟踪
   这两个分隔符，就能准确区分「头部区」和「正文区」。头部区里的
   ``key: value`` 是 header，绝不是台词；正文区里的 ``key: value`` 才是
   「说话人: 台词」。扁平扫描无法区分这两者，会把自定义 header
   （如 ``background: cabin``）当成台词翻掉，直接写坏脚本。
2. ``line_statement : line_formatted_text line_condition? hashtag* NEWLINE``
   —— 「台词」**不要求有说话人前缀**。``Empty Text #line:22a818``、
   ``1 - Succotash #line:3751d9`` 都是合法台词。旧实现强制
   ``名字: 正文``，把这类整类漏掉。
3. ``shortcut_option : '->' line_statement (INDENT statement* DEDENT)?``
   —— ``->`` 后面接的是**完整 line_statement**，所以选项正文可以带说话人
   前缀（``-> Captain: Let's go!``）、可以带 ``<<if>>`` 条件、可以带 hashtag。
4. ``line_group_item : '=>' line_statement (INDENT statement* DEDENT)?``
   —— Yarn 3.x 的「行组」（line group）用 ``=>``，同样是玩家可见文本。
5. ``line_condition : '<<' 'if' expr '>>' | '<<' 'once' ('if' expr)? '>>'``
   —— 「行内条件」写在**同一行**末尾，属结构、不属正文。
6. ``hashtag : '#' HASHTAG_TEXT``，``HASHTAG_TEXT: ~[ \\t\\r\\n#$<]+``
   —— 任意 ``#tag``（不只有 ``#line:``）都是元数据，不显示给玩家。
7. 词法里的转义：``\\:`` 是字面冒号（不代表说话人）、``\\[`` ``\\]`` 是字面
   方括号（不是 markup）、``\\{`` 是字面花括号（不是表达式）。
8. ``[[文本|节点]]``（Yarn 1.x 的 shortcut option，本分支语法已移除但
   大量老游戏仍在使用）：**有竖线才是选项**，``[[节点]]`` 是纯跳转。
"""

from __future__ import annotations

import re
from typing import Iterator, NamedTuple

# ---------------------------------------------------------------------------
# 结构标记
# ---------------------------------------------------------------------------

BODY_START = "---"          # node 头部 / 正文的分界
BODY_END = "==="             # node 结束

# 头部区里出现这些键的行一律是 header（不是台词）。
# 前四个是 NITW / 官方编辑器自动生成的；when 属于 node group；
# 其余是社区常见写法。未知键在**正文区**仍按「说话人: 台词」处理。
KNOWN_HEADER_KEYS = frozenset({
    "title", "tags", "colorid", "position", "when", "file", "style",
    "tracking", "subtitle", "line_group", "language", "lineType",
})

HEADER_KEY_RE = re.compile(r"^([A-Za-z_][\w\- ]{0,24})\s*:")

# ``-> 正文``：箭头与其后的水平空白都算前缀
ARROW_RE = re.compile(r"^([ \t]*->[ \t]*)")
# ``=> 正文``：Yarn 3.x 的行组
GROUP_RE = re.compile(r"^([ \t]*=>[ \t]*)")

# ``[[文本|目标]]``（Yarn 1.x shortcut option）。
# 正文用**非贪婪**匹配到第一个 ``|``：正文里可以带 BBCode
# （``[[{locator=Right}[wave]"Hi!"[/wave]|HowsItGoing]]``），用
# 「不含右方括号」的写法一遇 ``[/wave]`` 就断。
BRACKET_RE = re.compile(
    r"^([ \t]*)\[\[(?P<body>.+?)\|(?P<target>[^\]]*)\]\]")

# 行尾需要原样保留的结构尾巴：``<<if $x>>``/``<<once>>``/``<<任意命令>>``
# 与 ``#line:xxx``/``#tag``。它们都不显示给玩家。
#
# ⚠️ 命令用**非贪婪** ``<<.*?>>``：Yarn 的条件里会出现 ``>`` / ``>=``
# （``<<if $x > 90>>``），用 ``[^>]*`` 会整条匹配不上，导致这类命令行被
# 当成台词提取（旧版实测 NITW 有 107 条 ``<<if $x > 90>>`` 被误提取）。
# 命令在第一个 ``>>`` 处结束，条件里不可能出现 ``>>``，非贪婪是安全的。
#
# 依据词法：``HASHTAG_TEXT: ~[ \t\r\n#$<]+``，所以 ``#`` 后面必须紧跟
# 至少一个非空白/非 #$< 的字符；``Mae: C# is great`` 这种 ``#`` 后接空格
# 的不算 hashtag，会原样留在正文里。
TAIL_RE = re.compile(r"((?:[ \t]*(?:<<.*?>>|#[^\s#$<]+))+[ \t]*)$")

# 行尾注释 ``// ...``（需前置空白，避免吃掉 ``http://``）
COMMENT_RE = re.compile(r"[ \t]//")

# 残缺命令：``<wait 3>>`` / ``<wait 3>> #line:xx``（本该是 ``<<wait 3>>``）。
# 按「首个词」识别，因为后面可能还跟 hashtag。
STRAY_COMMAND_RE = re.compile(r"^<[A-Za-z_][^<>]*>>([ \t]|$)")

# ``说话人: 正文``。冒号不能是被 ``\`` 转义的（``\:`` 是字面冒号）。
# 取**第一个**冒号，与 Yarn 自己的 character 探测规则一致
# （"everything from the start of the line up to the first ':'"）。
SPEAKER_RE = re.compile(r"^([A-Za-z][\w '\-\.]{0,40}?)(?<!\\):[ \t]*(.*)$")

# 说话人探测里被 ``\`` 转义过的行不该被当成说话人
ESCAPED_COLON = "\\:"


class Statement(NamedTuple):
    """一条「正文语句」的拆分结果。

    不变式：``prefix + text + suffix`` 恒等于原始行（未含换行）。

    - ``kind``：``line`` / ``option`` / ``group`` / ``bracket``
    - ``text``：玩家可见的正文（已 strip）
    - ``speaker``：说话人名（``""`` 表示没有）
    - ``tail``：正文之后的结构尾巴（行内条件 / hashtag / 行尾注释），
      **原样保留前导空白**，因为普通台词的 ``original`` 是「冒号后原文」，
      必须能把尾巴原封不动拼回去（翻译记忆按该字符串命中）。
    """

    line_no: int
    kind: str
    prefix: str
    text: str
    suffix: str
    speaker: str
    tail: str


def _strip_tail(rest: str) -> tuple[str, str]:
    """从 ``rest`` 尾部剥掉注释与结构尾巴，返回 ``(去掉尾巴的正文, 尾巴)``。"""
    comment = ""
    cm = COMMENT_RE.search(rest)
    if cm:
        comment = rest[cm.start():]
        rest = rest[:cm.start()]
    tail = ""
    tm = TAIL_RE.search(rest)
    if tm:
        tail = rest[tm.start():]
        rest = rest[:tm.start()]
    return rest, tail + comment


def _split_text(raw: str, tail: str) -> tuple[str, str]:
    """把 ``raw`` 的两端空白归入 prefix / suffix，让 text 是 strip 过的。"""
    core = raw.strip()
    if not core:
        return raw, ""
    lead = raw[:len(raw) - len(raw.lstrip())]
    trail = raw[len(raw.rstrip()):]
    return lead, trail + tail


def split_statement(line: str) -> Statement | None:
    """把一行正文区内容拆成正文语句；返回 ``None`` 表示它是结构行。

    只处理**正文区**的行。头部区（header）、``<<命令>>``、``===``、注释、
    空行一律返回 ``None``。
    """
    raw = line.rstrip("\r\n")
    s = raw.strip()
    if not s:
        return None
    if s.startswith("//"):
        return None
    # 正文区里 ``===`` 表示节点结束（由调用方处理状态切换），不可能是台词
    if s.startswith(BODY_END):
        return None
    # ``---`` 在正文区是异常残留（如 ``--- #line:e7643f``），不翻译
    if s.startswith(BODY_START):
        return None
    # 纯 hashtag 行（`` #line:e56b4a`` —— 上一行译文里带了换行导致标签被挤下来）
    if s.startswith("#"):
        return None
    # 残缺命令：游戏脚本里的笔误（NITW GermHouse.yarn 的 ``<wait 3>> #line:42b866``，
    # 本该写 ``<<wait 3>>``）。词法上 ``<`` 是 TEXT 不是命令，但这种行
    # 绝不是台词，翻它只会写坏脚本。注意要按「首个词」判断，
    # 因为行尾还跟着 hashtag（``endswith(">>")`` 会漏判）。
    if STRAY_COMMAND_RE.match(s):
        return None

    # ---- Yarn 1.x：``[[文本|目标]]`` 快捷选项 ----
    bm = BRACKET_RE.match(raw)
    if bm is not None:
        body_raw = bm.group("body")
        text = body_raw.strip()
        # 正文两端的空白分别归入 prefix / suffix，保证
        # ``prefix + text + suffix == line`` —— 回填只替换 text 那一段，
        # 一个字节都不多改。（旧实现会把 ``[[  spaced out  |X]]`` 里
        # 正文字尾的空格吃掉，属于无谓改动。）
        if text:
            lead = body_raw[:len(body_raw) - len(body_raw.lstrip())]
            trail = body_raw[len(body_raw.rstrip()):]
        else:
            lead, trail = body_raw, ""
        prefix = raw[:bm.start("body")] + lead
        suffix = trail + raw[bm.end("body"):]
        return Statement(0, "bracket", prefix, text, suffix, "", "")

    # ---- 箭头类：``->`` 选项 与 ``=>`` 行组 ----
    for kind, rx in (("option", ARROW_RE), ("group", GROUP_RE)):
        m = rx.match(raw)
        if m is None:
            continue
        rest, tail = _strip_tail(raw[m.end():])
        lead, suffix = _split_text(rest, tail)
        text = rest.strip()
        speaker = _detect_speaker(text)
        return Statement(0, kind, m.group(1) + lead, text, suffix,
                         speaker, tail)

    # ---- 普通台词行 ----
    # 先把行首的 ``<<命令>>`` 串剥进前缀：``<<close>> Mae: hi`` 这类
    # 「命令 + 台词」在同一行的写法，命令不是正文，也不该挡住说话人探测。
    lead_cmd = ""
    rest_all = raw
    while True:
        cm = re.match(r"^[ \t]*<<.*?>>[ \t]*", rest_all)
        if cm is None:
            break
        lead_cmd += rest_all[:cm.end()]
        rest_all = rest_all[cm.end():]
    if lead_cmd and not rest_all.strip():
        return None                      # 整行就是命令（``<<close>>``）
    # 命令之后的 ``//`` 是注释，不是台词。
    # 例（NITW MansionExterior.yarn）：
    #   ``<<if $did_germ_friendship_quest_2>> //or $did_gregg_friendship_quest_3>>``
    # 开发者想注释掉条件的后一半，词法上 ``//`` 到行尾都是 COMMENT。
    if rest_all.lstrip().startswith("//"):
        return None

    rest, tail = _strip_tail(rest_all)
    lead, suffix = _split_text(rest, tail)
    text = rest.strip()
    speaker = _detect_speaker(text)
    return Statement(0, "line", lead_cmd + lead, text, suffix,
                     speaker, tail)


def _detect_speaker(text: str) -> str:
    """从正文里探测说话人前缀。

    ``\\:`` 是转义冒号，不代表说话人；此时整行都是正文。
    """
    if not text or ESCAPED_COLON in text.split(" ")[0]:
        return ""
    m = SPEAKER_RE.match(text)
    if not m:
        return ""
    name = m.group(1).strip()
    return name


def split_speaker(text: str) -> tuple[str, str, str]:
    """把 ``说话人: 正文`` 拆成 ``(说话人名, 原文里的说话人前缀, 正文)``。

    ``说话人前缀`` 原样保留（含冒号与其后的空白），回填时直接拼回，
    一个字都不改动脚本。无说话人时返回 ``("", "", text)``。

    转义冒号（``\\:``）不算说话人 —— 词法里 ``\\:`` 是字面冒号。
    """
    if not text:
        return "", "", text
    # 转义冒号出现在第一个「词」里时，整行都不是说话人
    if ESCAPED_COLON in text.split(" ")[0]:
        return "", "", text
    m = SPEAKER_RE.match(text)
    if not m:
        return "", "", text
    # ⚠️ 必须用 group(2) 的**起点**来切，不能用 m.end()：正则末尾是
    # ``(.*)``，m.end() 落在整行末尾，会把正文整段吞成空串。
    return m.group(1).strip(), text[:m.start(2)], m.group(2)


def speaker_body(text: str) -> tuple[str, str]:
    """把 ``说话人: 正文`` 拆成 ``(说话人, 正文)``；无说话人则 ``("", text)``。"""
    name, _prefix, body = split_speaker(text)
    return name, body


def iter_statements(text: str) -> Iterator[Statement]:
    """按节点结构遍历脚本，产出所有正文语句。

    状态机：``===`` 之后是头部区（header，不是台词），``---`` 之后是正文区。
    若整个文件里没有 ``---``（极少数被改写过的脚本），退化为「全文按正文
    处理」，并靠 ``KNOWN_HEADER_KEYS`` 兜住头部行。
    """
    lines = text.splitlines()
    has_separator = any(l.strip().startswith(BODY_START) for l in lines)
    in_body = not has_separator

    for i, line in enumerate(lines, 1):
        s = line.strip()
        if not s:
            continue
        if s.startswith(BODY_END):
            in_body = False
            continue
        if s.startswith(BODY_START):
            in_body = True
            continue
        if s.startswith("//"):
            continue
        if not in_body:
            continue
        # 头部兜底（文件缺 ``---`` 时）：已知 header 键不是台词
        if HEADER_KEY_RE.match(s):
            key = s.split(":", 1)[0].strip().lower().replace(" ", "")
            if key in KNOWN_HEADER_KEYS:
                continue
        st = split_statement(line)
        if st is None:
            continue
        if not st.text:
            continue
        yield st._replace(line_no=i)


def is_header_line(line: str) -> bool:
    """该行是否是（已知或未知的）头部区 header。

    供分析/自检使用。
    """
    return HEADER_KEY_RE.match(line.strip()) is not None


def has_format_function(text: str) -> bool:
    """是否含 Yarn 的「格式函数」标记。

    形如 ``[plural value={$n} one="pie" other="pies" /]``、
    ``[select {$gender} m="he" f="she" /]``。**标记参数里含玩家可见英文**
    （``pie`` / ``pies`` / ``he`` / ``she``），本工具目前把整个标记当结构保护，
    因此这部分文字不会被翻译 —— 属已知限制（样本中尚未出现）。
    """
    return bool(re.search(r"\[(plural|select|ordinal)\b", text))
