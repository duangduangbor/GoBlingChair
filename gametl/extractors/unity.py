"""Unity 文本提取器。

Unity 游戏的文本存储有多种形态，本提取器覆盖：
1. 独立的文本资源文件（.txt / .json / .csv）—— 最简单，直接当文本处理。
2. Localization 插件产物（I2 Localization 的 .csv，或 Unity Localization 的 .json）。
3. ``.assets`` / ``.bundle`` 里的 ``TextAsset`` —— 需要 UnityPy 解析。

   这是 Unity 游戏文本最常见的藏身处（如 Night in the Woods 的对话全部
   在 ``sharedassets0.assets`` 的 41 个 TextAsset 里），本提取器支持：

   - **精确解析 TextAsset 内的可翻译行**，两种主流形态：
     · Yarn Spinner 对话脚本（``Mae: 台词`` 行，跳过 title/->/<<...>>/[[...]]）
     · CSV 式 ``*_lines``（``LineCode,LineText,Comment`` 三列）
   - **回填**：译文写回 TextAsset 的 ``m_Script``，再用 UnityPy 重新序列化
     ``.assets``，产出真正的汉化资源包（而非只读不改）。

依赖 UnityPy（版本 1.10.x，兼容 Python 3.8；1.25.x 需 3.9+）。
未安装时退化为只处理纯文本资源并给出提示。
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from ..core.models import Project, TextKind, TextUnit
from ..core.protect import protect, restore
from ..core.scan import iter_files
from .base import BaseExtractor, wb_stats, write_back_file
from .yarn import (
    BRACKET_RE as YARN_BRACKET_CHOICE_RE,
    iter_statements as iter_yarn_statements,
    split_speaker as yarn_split_speaker,
    split_statement,
)

TEXT_SUFFIXES = {".txt", ".json", ".csv", ".tsv", ".xml", ".yaml", ".yml"}

# ---------------------------------------------------------------------------
# Unity 引擎 / 插件的「机器配置」文件 —— 里面的字符串是类型名、GUID、资源
# 路径、枚举值，**一个都不能翻**。
#
# 实测事故（IDJ，Addressables + EOS，2026-10-12）：汉化补丁覆盖后游戏启动
# 直接 Segmentation fault。逐项比对补丁与原版后定位到：
#   · settings.json 的 m_buildTarget "StandaloneWindows64" → "独立版 Windows 64 位"
#   · RuntimeInitializeOnLoads.json 的 nameSpace "BlendModes" → "混合模式"
#   · link.xml 的 <type fullname="CurrentSelectedItem" preserve="all"/>
#     —— fullname 与 preserve 双双被翻成中文
#   · eos_product_config.json 的 ProductName → 中文
#   · catalog.json 的 m_InternalIds 里
#     {UnityEngine.AddressableAssets.Addressables.RuntimePath} 占位符 → 中文
# 这些是「配置」不是「文本」。翻错任何一处轻则功能失效，重则启动即崩。
# ---------------------------------------------------------------------------
UNITY_MACHINE_FILES = {
    # Addressables 目录与设置
    "catalog.json", "settings.json",
    # Unity 运行时清单
    "runtimeinitializeonloads.json", "scriptingassemblies.json",
    "link.xml", "unityservicesprojectconfiguration.json",
    "google-services-desktop.json",
    # Epic Online Services
    "eos_android_config.json", "eos_ios_config.json", "eos_linux_config.json",
    "eos_macos_config.json", "eos_product_config.json", "eos_steam_config.json",
    "eos_windows_config.json", "epiconlineservicesconfig.json",
}

# 这些目录整棵子树都是机器配置（Addressables / EOS）
UNITY_MACHINE_PATH_PARTS = (
    "/streamingassets/aa/",
    "/streamingassets/eos/",
    "/addressableslink/",
)

# 许可证/法律文本：翻了没有任何玩家看得到，纯白烧算力
LICENSE_ASSET_NAMES = ("license", "licence", "notice", "copying", "third party",
                       "thirdparty", "credits", "changelog")


def _is_unity_machine_file(rel: str, name: str) -> bool:
    """这个文本文件是不是 Unity 机器配置（不该被翻译）。"""
    if name.lower() in UNITY_MACHINE_FILES:
        return True
    r = "/" + rel.lower().replace("\\", "/")
    return any(part in r for part in UNITY_MACHINE_PATH_PARTS)


def _is_license_asset(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in LICENSE_ASSET_NAMES)

# ---------------------------------------------------------------------------
# Yarn：语法判定已迁移到 ``gametl/extractors/yarn.py``
#
# 早期这里是「扁平正则扫描」：逐行套几条正则猜哪些行是台词。每遇到一种
# 新形态就得补一条正则，历史上因此返工两次（先漏 `->` 选项 797 条，再漏
# `[[文本|目标]]` 选项 262 条）。现在改为**按官方编译器语法解析**，见
# yarn.py 的模块文档。下面这些常量只为兼容早期调用方与测试保留。
# ---------------------------------------------------------------------------
YARN_LINE_RE = re.compile(r"^(\s*)([A-Za-z][\w ]*):\s*(.+)$")
# 早期「扁平扫描」用的跳过前缀；新解析器靠节点状态区分头部/正文，
# 不再依赖它，但自检脚本仍在引用，故保留。
YARN_SKIP_PREFIXES = ("title:", "tags:", "colorID:", "position:", "<<", "[[", "===")
YARN_SKIP_EXACT = {"---", ""}
# 选项行前缀：`-> 正文`（`->` 与正文之间可能没有空格）
YARN_CHOICE_PREFIX_RE = re.compile(r"^(\s*->\s*)")
# 选项行「尾部」要原样保留的记号：<<命令>> / #line:xx / #tag
YARN_TAIL_RE = re.compile(r"(\s*(?:<<[^>]*>>|#[\w:.\-]+))+\s*$")

# 受保护占位 token（【0】【1】…）
_PLACEHOLDER_RE = re.compile(r"\u3010\d+\u3011")


def _has_real_text(text: str) -> bool:
    """正文里除受保护片段外，是否还有真正需要翻译的文字。

    ``$grocery_box``、``{locator=Left}``、``%s`` 这种整行只剩变量/标签的
    行，保护后一个字母都不剩 —— 不该送去翻译（翻不出东西，还平白让回填
    多一次无谓改动）。
    """
    prot, _frags = protect(text)
    return _is_translatable(_PLACEHOLDER_RE.sub("", prot))

# *_lines 的 CSV 表头
LINES_HEADER = "LineCode"

# 吉他小游戏谱面数据资产（Bars/Notes/AnimationCues 是音符时间戳，不是文本）
# Lyrics 里虽有歌词文本，但混着时间戳行，需按行过滤（见 _is_lyric_line）
SPECTRAL_ASSET_SUFFIXES = ("Bars", "Notes", "AnimationCues", "Cues")
# 纯数字/版本号这类非文本资产名
NON_TEXT_ASSET_NAMES = {"buildnumber", "app.info"}
# 配置类资产名前缀（FMOD 事件清单等）
NON_TEXT_ASSET_PREFIXES = ("Events-",)
# 谱面/时间戳行形如 "00.000|8|0" 或 "00.000|Alcuni seguono|0"
TIMECODE_LINE_RE = re.compile(r"^\s*\d{1,2}:\d{2}\.\d{3}\s*\|")


def _is_translatable(text: str) -> bool:
    s = text.strip()
    if not s:
        return False
    if not any(c.isalnum() or c.isalpha() or ord(c) > 0x2E80 for c in s):
        return False
    return True


# ---------------------------------------------------------------------------
# MonoBehaviour 里的 UI 文本
#
# Unity 游戏的真文本常常不在 TextAsset 里，而在 MonoBehaviour 的字段里
# （I2 Localization 的 _text/drawText/hyphenedText/preParsedText，UGUI 的
# m_Text，TextMeshPro 的 m_text…）。但 MonoBehaviour 的 typetree 里同时混着
# 大量**机器字符串**：状态名、资源名、GUID、枚举、图层名 —— 翻坏一个状态名
# 就是游戏跑不起来。所以这里只认「字段名里带文本语义词」的字段，宁可少翻。
#
# 字段名先按驼峰/下划线切成词再比对，避免 "context" 里的 "text" 误命中
# （`_contextParam` 就是靠这条挡掉的）。
#
# ⚠️ 这份白名单收得很紧，是拿 IDJ 实测换来的。曾经加过
# label / title / description，结果：
#   · InControl 手柄插件的设备描述（'VKB-Sim Gladiator MKII…'、'PS3 Mode'）
#     —— 翻了会破坏手柄识别；
#   · 行为树/节点图的节点名（'Run in parallel'、'Change Character State'）
#     与字段名（'CustomStateName'、'SetActive1'）—— 翻了运行时按名字匹配就断。
# 这些字段名字都很「像 UI 文本」，但内容是机器标识。宁可少翻。
MB_TEXT_WORDS = frozenset((
    "text", "subtitle", "caption", "tooltip", "dialogue",
))
# 值是路径 / 术语 key / GUID / 占位符的一律跳过
_MB_HEX_RE = re.compile(r"^[0-9a-fA-F\-_]{8,}$")
_MB_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def _field_words(name: str) -> list[str]:
    """``drawText`` -> ['draw','text']；``_contextParam`` -> ['context','param']。"""
    return [w.lower() for w in _MB_WORD_RE.findall(str(name))]


def _mb_field_is_text(name: str) -> bool:
    """字段名看起来是不是「给人看的文本」。"""
    return any(w in MB_TEXT_WORDS for w in _field_words(name))


def _mb_text_ok(value: str) -> bool:
    """字段值是否值得翻译（保守判定：拿不准就不翻）。"""
    v = value.strip()
    if not v or len(v) > 400:
        return False
    # 至少 2 个字母 —— 挡掉 "R$ 14,99"、"01"、"--" 这类不该动的值
    if sum(1 for c in v if c.isalpha()) < 2:
        return False
    if _MB_HEX_RE.match(v):
        return False
    if "://" in v:
        return False
    # 路径 / 术语 key（I2 的 CORE_PROFILES/PROFILE_1）/ 资源名
    if "/" in v or "\\" in v:
        return False
    # 纯占位符，如 {0}、{PlayerName}
    if v.startswith("{") and v.endswith("}"):
        return False
    return True


def _walk_mb_strings(node, path: tuple = ()):
    """递归遍历 typetree，产出 ``(字段路径, 字符串值)``。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_mb_strings(v, path + (str(k),))
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            yield from _walk_mb_strings(v, path + (str(i),))
    elif isinstance(node, str):
        yield path, node


def _tree_step(node, key: str):
    """按一个路径片段往下走一层（dict 用键，list 用下标）。"""
    if isinstance(node, dict):
        return node.get(key)
    if isinstance(node, list):
        try:
            return node[int(key)]
        except (ValueError, IndexError):
            return None
    return None


def _set_mb_field(root, field_path, original: str, value: str) -> bool:
    """按字段路径把 typetree 里的字符串换成译文。

    只在**当前值仍等于原文**时才替换 —— 提取和回填是两次独立的解析，
    万一结构对不上，这里宁可放过也不能改错字段。
    """
    if not field_path:
        return False
    parent = root
    for key in field_path[:-1]:
        parent = _tree_step(parent, key)
        if parent is None:
            return False
    last = field_path[-1]
    if isinstance(parent, dict):
        if last not in parent:
            return False
        cur = parent[last]
        if isinstance(cur, str) and cur.strip() == original.strip():
            parent[last] = value
            return True
        return False
    if isinstance(parent, list):
        try:
            idx = int(last)
            cur = parent[idx]
        except (ValueError, IndexError):
            return False
        if isinstance(cur, str) and cur.strip() == original.strip():
            parent[idx] = value
            return True
    return False


def _split_choice_line(line: str) -> tuple[str, str, str] | None:
    """把一条 Yarn 选项行拆成 ``(前缀, 正文, 尾部)``。

    ``-> 正文 <<命令>> #line:xxx // 注释`` 拆成三块，只有「正文」是
    该翻译的，其余原样保留。返回 ``None`` 表示这行不是选项行。

    正文可以为空（纯命令/纯跳转选项），调用方自行判断要不要提取。
    """
    m = YARN_CHOICE_PREFIX_RE.match(line)
    if not m:
        return None
    prefix = m.group(1)
    rest = line[m.end():]

    # 先切出行尾注释（`//` 到行尾）—— 注释里也常有 #line:xxx
    comment = ""
    cm = re.search(r"\s//", rest)
    if cm:
        comment = rest[cm.start():]
        rest = rest[:cm.start()]

    # 再从尾部剥掉 <<命令>> / #line:xxx / #tag
    tail = ""
    tm = YARN_TAIL_RE.search(rest)
    if tm:
        tail = rest[tm.start():]
        rest = rest[:tm.start()]
    return prefix, rest, tail + comment


def _has_unitypy() -> bool:
    try:
        import UnityPy  # noqa: F401
        return True
    except ImportError:
        return False


class UnityExtractor(BaseExtractor):
    """Unity 提取器（文本资源模式 + 可选 UnityPy 模式）。"""

    def __init__(self, decoded_dir: Path, use_unitypy: bool = True):
        super().__init__(decoded_dir)
        self.use_unitypy = use_unitypy and _has_unitypy()
        self._text_units: list[TextUnit] = []
        # Addressables 覆盖到的 bundle 名（懒解析，见 _addressables_hashes）。
        # None = 还没算过；set() = 算过、确认这个游戏没有 Addressables。
        self._addr_known = None

    def extract(self) -> list[TextUnit]:
        units: list[TextUnit] = []

        # --- 模式 1：纯文本资源 ---
        text_files = [p for p in iter_files(self.decoded_dir)
                      if p.suffix.lower() in TEXT_SUFFIXES]
        # 过滤掉明显的配置/代码文件 + 许可证/法律文本 + 运行时日志。
        # 全部小写比对（Windows 文件名大小写随意，`Readme.txt` 也得认出来）。
        skip_names = {"package.json", "manifest.json", "projectversion.txt",
                      "boot.config", "app.info", "legal.txt", "license.txt",
                      "licence.txt", "copying", "notice", "readme.txt",
                      # Unity 运行时自己写的日志，不是游戏内容。
                      # NITW 的 output_log.txt 有 2262 行，会被逐行当成
                      # 对话文本提取 —— 而且它随每次运行游戏不断变长，
                      # 让「提取条数」变成一个会漂移的数字。
                      "output_log.txt", "player.log", "player-prev.log",
                      "error.log", "crash.dmp",
                      # 汉化工具自己的说明书（常被用户连着 exe 一起放进游戏目录）
                      "使用说明.txt", "安装说明.txt",
                      "启动.bat"}
        skip_parts = ("/legal", "/licenses")
        for path in text_files:
            if path.name.lower() in skip_names:
                continue
            rel = str(path.relative_to(self.decoded_dir)).replace("\\", "/")
            if any(part in rel.lower() for part in skip_parts):
                continue
            # Unity 引擎/插件配置：值是类型名/GUID/路径/枚举，翻了必崩
            if _is_unity_machine_file(rel, path.name):
                continue
            suf = path.suffix.lower()
            try:
                if suf == ".json":
                    units.extend(self._extract_json(path, rel))
                elif suf in (".csv", ".tsv"):
                    units.extend(self._extract_csv(path, rel))
                else:
                    units.extend(self._extract_plain(path, rel))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, csv.Error):
                continue

        # --- 模式 2：UnityPy 解析二进制资源 ---
        if self.use_unitypy:
            units.extend(self._extract_binary())
        else:
            assets = [p for p in iter_files(self.decoded_dir)
                      if p.suffix.lower() == ".assets"]
            if assets:
                print(f"[提示] 发现 {len(assets)} 个 .assets 文件，但未安装 UnityPy，"
                      f"跳过二进制资源解析。")
                print("       安装方法: pip install UnityPy")

        self._text_units = units
        return units

    def _mk(self, rel: str, loc: dict, text: str, kind: TextKind,
            context: dict | None = None) -> TextUnit:
        prot, frags = protect(text)
        return TextUnit(
            uid=TextUnit.make_uid(rel, loc, text),
            source_file=rel, location=loc, original=text, kind=kind,
            context=context or {},
            protected=frags, extra={"protected_form": prot},
        )

    def _extract_plain(self, path: Path, rel: str) -> list[TextUnit]:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        # 整文件就是一个大文本块的情况（对话脚本）
        if len(lines) > 1:
            units = []
            for i, line in enumerate(lines, 1):
                if _is_translatable(line):
                    units.append(self._mk(rel, {"line": i, "form": "line"}, line.strip(),
                                          TextKind.DIALOGUE))
            return units
        if _is_translatable(text):
            return [self._mk(rel, {"form": "whole"}, text.strip(), TextKind.OTHER)]
        return []

    def _extract_json(self, path: Path, rel: str) -> list[TextUnit]:
        data = json.loads(path.read_text(encoding="utf-8"))
        units: list[TextUnit] = []

        def walk(node, key_path: list):
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, key_path + [k])
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, key_path + [i])
            elif isinstance(node, str) and _is_translatable(node):
                units.append(self._mk(rel, {"key_path": key_path, "form": "json"},
                                      node, TextKind.OTHER))

        walk(data, [])
        return units

    def _extract_csv(self, path: Path, rel: str) -> list[TextUnit]:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",\t")
        except csv.Error:
            dialect = csv.excel
            if path.suffix.lower() == ".tsv":
                dialect = csv.excel_tab
        reader = csv.reader(io.StringIO(raw), dialect)
        units: list[TextUnit] = []
        for row_i, row in enumerate(reader):
            for col_i, cell in enumerate(row):
                if _is_translatable(cell):
                    units.append(self._mk(
                        rel, {"row": row_i, "col": col_i, "form": "csv"},
                        cell, TextKind.OTHER))
        return units

    def _extract_binary(self) -> list[TextUnit]:
        """用 UnityPy 从 .assets/.bundle 提取可翻译文本。

        两类来源：
        · **TextAsset** —— 按内容形态（.yarn / *_lines）精确解析台词正文；
        · **MonoBehaviour** —— UI 文本（I2 Localization 的 _text 等）。
          很多 Unity 游戏（含 Addressables 分包的游戏）根本不把文本放进
          TextAsset，只看 TextAsset 会把整款游戏漏成 0 条。
        """
        units: list[TextUnit] = []
        try:
            import UnityPy
        except ImportError:
            return units

        # 注意：`.assets` 的松散匹配是为了兼容 split 出来的同名前缀文件；
        # 备份产物（*.assets.bak 等）已由 core.scan.iter_files 统一剔除。
        targets = [p for p in iter_files(self.decoded_dir)
                   if p.suffix.lower() in (".assets", ".bundle", ".unity3d")
                   or ".assets" in p.name]
        for path in targets:
            rel = str(path.relative_to(self.decoded_dir)).replace("\\", "/")
            try:
                env = UnityPy.load(str(path))
            except Exception:
                continue
            for obj in env.objects:
                kind = obj.type.name
                if kind == "MonoBehaviour":
                    units.extend(self._extract_monobehaviour(rel, obj))
                    continue
                if kind != "TextAsset":
                    continue
                try:
                    data = obj.read()
                    name = getattr(data, "m_Name", "TextAsset")
                    script = bytes(data.m_Script)
                    try:
                        text = script.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                except Exception:
                    continue
                if not text.strip():
                    continue
                # 许可证/法律文本（Apache LICENSE 等）：玩家看不到，翻了纯烧算力
                if _is_license_asset(name):
                    continue
                # 记录该 TextAsset 的原始字节，供回填时精确替换
                units.extend(self._extract_textasset(rel, name, text, obj))
        return units

    def _extract_monobehaviour(self, rel: str, obj) -> list[TextUnit]:
        """从一个 MonoBehaviour 里提取「给人看的」字符串字段。

        只认字段名带文本语义词的字段（见 ``_mb_field_is_text``），
        并且跳过路径/key/GUID 形态的值 —— MonoBehaviour 里机器字符串太多，
        误翻一个状态名就会让游戏跑不起来。
        """
        try:
            tt = obj.read_typetree()
        except Exception:
            return []
        if not isinstance(tt, dict):
            return []
        name = tt.get("m_Name") or ""
        try:
            path_id = int(obj.path_id)
        except (TypeError, ValueError):
            return []
        out: list[TextUnit] = []
        for field_path, value in _walk_mb_strings(tt):
            if not field_path:
                continue
            if not _mb_field_is_text(field_path[-1]):
                continue
            if not _mb_text_ok(value):
                continue
            out.append(self._mk(
                rel,
                {"asset": name, "form": "unitypy", "path_id": path_id,
                 "mb_field_path": list(field_path)},
                value.strip(), TextKind.OTHER))
        return out

    def _extract_textasset(self, rel: str, name: str, text: str,
                           obj) -> list[TextUnit]:
        """解析单个 TextAsset 内容，产出 TextUnit。"""
        units: list[TextUnit] = []
        loc_base = {"asset": name, "form": "unitypy"}
        if name.endswith(".yarn") or "\n---\n" in text:
            units.extend(self._extract_yarn(rel, loc_base, text))
        elif text.lstrip().startswith(LINES_HEADER):
            units.extend(self._extract_lines_csv(rel, loc_base, text))
        elif (name.endswith(SPECTRAL_ASSET_SUFFIXES)
              or name in NON_TEXT_ASSET_NAMES
              or name.startswith(NON_TEXT_ASSET_PREFIXES)):
            # 纯音符谱面 / 版本号 / 事件清单，不含可翻译文本，跳过
            pass
        else:
            # 兜底：整文件逐行（含 Lyrics 等混排文本，跳过时间戳行）
            for i, line in enumerate(text.splitlines(), 1):
                s = line.strip()
                if TIMECODE_LINE_RE.match(s):
                    continue
                if _is_translatable(s):
                    units.append(self._mk(
                        rel, {**loc_base, "line": i, "raw": True},
                        s, TextKind.OTHER))
        return units

    def _extract_yarn(self, rel: str, loc_base: dict,
                      text: str) -> list[TextUnit]:
        """解析 Yarn Spinner 脚本，提取**全部**玩家可见文本。

        判定逻辑全部在 ``gametl/extractors/yarn.py``（按官方编译器语法 +
        节点状态机），本方法只负责把结果包装成 TextUnit。覆盖四种写法：

        1. ``说话人: 台词`` / ``无说话人的裸台词`` —— 普通对话
           （``Empty Text #line:22a818``、``1 - Succotash #line:3751d9``
           都是合法台词，早期实现强制要求有说话人，把这一类整类漏掉）
        2. ``-> 正文``    —— 箭头式选项（也常被拿来做「点击推进」的分句演出，
           Night in the Woods 的开场诗整段都是这个形式）
        3. ``=> 正文``    —— Yarn 3.x 的行组（line group），同样是玩家可见文本
        4. ``[[正文|目标]]`` —— 方括号式选项（只有带竖线的才是选项，
           ``[[NodeName]]`` 是纯节点跳转，必须跳过）

        跳过：头部区 header（``title:``/``tags:``/``colorID:``/``position:``
        以及任意自定义 header）、``<<命令>>``、``===``/``---`` 分隔、注释、
        空行、以及正文里只剩变量/标签的行。行尾的 ``#line:xxxx``、内联
        ``<<命令>>`` 与正文里的 ``{属性}``/``[标签]`` 由 protect 保护，
        回填时原样拼回。
        """
        units: list[TextUnit] = []
        for st in iter_yarn_statements(text):
            speaker, _sp_prefix, body = yarn_split_speaker(st.text)
            # 兼容既有的 uid 约定（翻译记忆按 original 字符串命中，
            # 改动它会让大家已有的译文全部失效）：
            #   · 普通台词：original = 冒号后的原文（**含**行尾 #line 等）
            #   · 选项/行组/方括号选项：original = 纯正文
            original = (body + (st.tail if st.kind == "line" else "")).strip()
            if not _has_real_text(original):
                continue

            loc = dict(loc_base)
            loc["line"] = st.line_no
            if st.kind == "line":
                loc["yarn"] = True
                # 注：普通台词**始终**带 speaker 键（可能为空串），
                # 与历史 uid 保持一致
                loc["speaker"] = speaker
            elif st.kind == "option":
                loc["yarn_choice"] = True
                if speaker:
                    loc["speaker"] = speaker
            elif st.kind == "group":
                loc["yarn_group"] = True
                if speaker:
                    loc["speaker"] = speaker
            else:                                  # bracket
                loc["yarn_bracket"] = True
                if speaker:
                    loc["speaker"] = speaker

            units.append(self._mk(
                rel, loc, original, TextKind.DIALOGUE,
                context={"speaker": speaker} if speaker else None))
        return units

    def _extract_lines_csv(self, rel: str, loc_base: dict,
                           text: str) -> list[TextUnit]:
        """解析 *_lines 的 CSV（LineCode,LineText,Comment）。

        LineText 里常嵌 ``说话人: 台词`` 前缀（如 ``Mae: =_=``），
        前缀本身可能是人名（保留），台词主体才翻译。
        """
        units: list[TextUnit] = []
        raw = text.replace("\r\n", "\n")
        lines = raw.split("\n")
        for i, line in enumerate(lines, 1):
            s = line.strip()
            if not s or s.startswith(LINES_HEADER):
                continue
            # 结构：line:code,LineText,Comment（LineText 可能带引号）
            try:
                row = next(csv.reader([s]))
            except (csv.Error, StopIteration):
                continue
            if len(row) < 2:
                continue
            code, text_col = row[0], row[1]
            if not code.startswith("line:"):
                continue
            body = text_col.strip()
            if not _is_translatable(body):
                continue
            # 剥离「说话人: 」前缀，前缀作为 context（人名通常不必翻，
            # 但台词主体要翻）。前缀用 protect 保护，避免模型乱译人名。
            speaker = ""
            m = re.match(r"^([A-Za-z][\w ]*):\s*(.+)$", body)
            if m:
                speaker = m.group(1).strip()
                body = m.group(2).strip()
                if not _is_translatable(body):
                    continue
            units.append(self._mk(
                rel, {**loc_base, "row": i, "lines_csv": True,
                      "speaker": speaker, "code": code},
                body, TextKind.DIALOGUE,
                context={"speaker": speaker} if speaker else None))
        return units

    def write_back(self, project: Project, out_dir: Path) -> dict:
        """回填：文本资源文件直接改；TextAsset/MonoBehaviour 用 UnityPy 写回。

        二进制回填流程（真正产出汉化资源，而非跳过）：
        1. 按 source_file（.assets/.bundle 路径）分组，收集已译的单元；
        2. TextAsset 按行号/CSV 行替换、MonoBehaviour 按字段路径替换；
        3. 用 UnityPy 重新序列化整包，写出新文件；
        4. **刷新 Addressables catalog 的 bundle CRC** —— 不刷新的话
           Addressables 会以「CRC Mismatch」拒绝加载，游戏卡在 Loading。
        """
        out_dir = Path(out_dir)
        known = self._addressables_hashes()
        by_file: dict[str, list[TextUnit]] = {}
        for u in project.units:
            if u.translated:
                by_file.setdefault(u.source_file, []).append(u)

        written = 0
        unchanged = 0
        replaced = 0
        binary_replaced = 0
        binary_files = 0
        changed: list[str] = []
        per_file_n: dict[str, int] = {}
        skipped: list[str] = []
        for rel, us in by_file.items():
            src = self.decoded_dir / rel
            if not src.exists():
                continue
            # ★ 资源包先过「CRC 能不能中和」这一关。过不了就一个字都不改：
            #   Addressables 会拿 catalog 里的旧 CRC 校验，改了必然卡 Loading，
            #   而有些包的期望值根本找不到出处（改不动、也没法中和）。
            if self._is_pack(rel) and not self._crc_ok(rel, known):
                skipped.append(rel)
                continue
            is_binary = any(u.location.get("form") == "unitypy" for u in us)
            if is_binary:
                r, differs = self._write_binary_assets(src, out_dir / rel, us)
                if r and not differs:
                    # 产物和游戏里现有的这份**逐字节一样**：留着它只会让补丁变胖、
                    # 还得为它去刷 CRC（而刷 CRC 又要进游戏目录动手脚）。
                    # 干脆不产出 —— 补丁里只放**真的会改变**的文件。
                    try:
                        (out_dir / rel).unlink()
                    except OSError:
                        pass
                    per_file_n[rel] = r
                    unchanged += 1
                    continue
                if r:
                    per_file_n[rel] = r
                    replaced += r
                    binary_replaced += r
                    binary_files += 1
                    written += 1
                    changed.append(rel)
                continue

            suf = src.suffix.lower()
            if suf == ".json":
                n, differs = self._write_json(src, out_dir / rel, us)
            elif suf in (".csv", ".tsv"):
                n, differs = self._write_csv(src, out_dir / rel, us)
            else:
                n, differs = self._write_plain(src, out_dir / rel, us)
            replaced += n
            if n:
                per_file_n[rel] = n
                if not differs:
                    try:
                        (out_dir / rel).unlink()
                    except OSError:
                        pass
                    unchanged += 1
                    continue
                written += 1
                changed.append(rel)

        if skipped:
            n_left = sum(per_file_n.get(r, 0) for r in skipped)
            print(f"[Unity] {len(skipped)} 个资源包的 Addressables 校验值没法中和，"
                  f"保持原样、不汉化（{n_left} 条译文未使用）：")
            for rel in skipped:
                print(f"        {rel}（{per_file_n.get(rel, 0)} 条）")

        # 兜底：产物目录里可能躺着**上一轮**留下的、现在判定为不可改的包
        # （断点续传 / 增量重跑时 write_back 的输出目录是复用的）。
        # 那些包一旦被补丁盖进游戏，同样会让游戏进不去 —— 一并清掉。
        stale = self._purge_stale_bundles(out_dir, known)
        for rel in stale:
            if rel in changed:
                changed.remove(rel)
                written -= 1
                replaced -= per_file_n.get(rel, 0)
                binary_replaced -= per_file_n.get(rel, 0)
                binary_files -= 1
        if stale:
            print(f"[Unity] 产物目录里清掉 {len(stale)} 个历史遗留的不可汉化资源包：")
            for rel in stale:
                print(f"        {rel}")

        crc_files = self._refresh_addressables_crc(changed, out_dir)
        for rel in crc_files:
            if rel not in changed:
                changed.append(rel)
                written += 1
        return wb_stats(files_written=written, replaced=replaced,
                        unchanged=unchanged, changed=changed,
                        binary_files=binary_files,
                        binary_replaced=binary_replaced,
                        addressables_crc=len(crc_files),
                        skipped_bundles=sorted(set(skipped) | set(stale)))

    # ---- Addressables / 资源包安全闸 ----

    @staticmethod
    def _is_pack(rel: str) -> bool:
        return str(rel).lower().endswith((".bundle", ".unity3d"))

    def _addressables_hashes(self) -> set:
        """本项目 catalog 覆盖到的 bundle 名集合（只算一次，缓存到实例上）。"""
        if self._addr_known is not None:
            return self._addr_known
        known: set = set()
        try:
            from ..core import addressables
            known = addressables.verifiable_hashes(self.decoded_dir)
        except Exception:
            known = set()
        self._addr_known = known
        return known

    def _crc_ok(self, rel: str, known: set) -> bool:
        """这个资源包的 CRC 有没有办法中和？没有 catalog 就当作「没有校验」。"""
        if not known:                  # 不是 Addressables 游戏
            return True
        try:
            from ..core import addressables
            return addressables.bundle_stem(rel).lower() in known
        except Exception:
            return True

    def _purge_stale_bundles(self, out_dir: Path, known: set) -> list[str]:
        """删掉产物目录里所有「不可汉化」的资源包（含历史遗留）。

        只删产物（out_dir）里的副本，游戏目录里的原文件一个都不碰。
        """
        if not known:
            return []
        out_dir = Path(out_dir)
        if not out_dir.is_dir():
            return []
        killed: list[str] = []
        for p in sorted(out_dir.rglob("*")):
            if not p.is_file() or not self._is_pack(p.name):
                continue
            rel = str(p.relative_to(out_dir)).replace("\\", "/")
            if self._crc_ok(rel, known):
                continue
            try:
                p.unlink()
            except OSError:
                continue
            killed.append(rel)
        return killed

    def _dirty_bundles(self) -> set:
        """游戏目录里「已被改过」的资源包（大小与 catalog 记录不符）。

        ★ v2.6.7：这些包**必须**一起刷 CRC，否则补丁一装就 CRC Mismatch。
        详见 ``addressables.dirty_bundles`` 的说明（IDJ 的 a29f6ac8 / aff1ec57）。
        """
        try:
            from ..core import addressables
            return addressables.dirty_bundles(self.decoded_dir)
        except Exception:
            return set()

    def _refresh_addressables_crc(self, changed: list[str],
                                  out_dir: Path) -> list[str]:
        """凡是**会随补丁一起进游戏**的 AssetBundle，都要同步刷新 catalog 的 CRC。

        否则 Addressables 运行时校验不过：
        ``CRC Mismatch ... Will not load AssetBundle`` → 游戏永远卡在 Loading。

        口径是「**最终游戏目录里会有哪些被改过的包**」，三路并集：

        1. 这一轮真改了的（``changed``）；
        2. 产物目录里躺着的（断点续传时某个包这轮算出来「没变」—— 相对的是
           **已被改过**的游戏文件 —— 但它照样会被装进去）；
        3. ★ 游戏目录里**已经被改过**、却既不在 changed 也不在产物里的
           （``_dirty_bundles``，判据是大小与 catalog 记录不符）。

        第 3 条是 v2.6.7 补的坑：漏了它，残留包会在新补丁装好后立刻 Mismatch。
        宁可多刷几个（刷多无害，CRC=0 只是不校验），绝不能漏。
        """
        want = {r for r in changed if self._is_pack(r)}
        out_dir = Path(out_dir)
        if out_dir.is_dir():
            for p in out_dir.rglob("*"):
                if p.is_file() and self._is_pack(p.name):
                    want.add(str(p.relative_to(out_dir)).replace("\\", "/"))
        for h in self._dirty_bundles():
            want.add(h + ".bundle")
        bundles = sorted(want)
        if not bundles:
            return []
        try:
            from ..core import addressables
            return addressables.refresh(self.decoded_dir, bundles,
                                        out_dir=out_dir)
        except Exception:
            return []

    def _write_binary_assets(self, src: Path, dst: Path,
                             us: list[TextUnit]) -> tuple[int, bool]:
        """把译文写回 .assets/.bundle，重新序列化整包。

        两类目标，一次保存：
        · **TextAsset** —— 按 asset 名分组，重建文本后写回 ``m_Script``；
        · **MonoBehaviour** —— 按 ``path_id`` 精确定位对象，读 typetree
          → 改字段 → ``save_typetree`` 写回。

        保存优先用 ``packer="original"``：默认保存会把 bundle 的 dataflags
        抹掉（原版 0x243 → 0x40），丢掉 ``BlockInfoNeedPaddingAtStart`` 等标志，
        产物和原版的加载行为不一致。

        Returns:
            (替换条数, 内容是否与原文件不同)。替换条数为 0 时不落盘。
        """
        try:
            import UnityPy
        except ImportError:
            return 0, False

        try:
            env = UnityPy.load(str(src))
        except Exception:
            return 0, False

        n = self._apply_textassets(env, us)
        n += self._apply_monobehaviours(env, us)
        if not n:
            return 0, False

        payload = None
        try:
            payload = env.file.save(packer="original")
        except Exception:
            payload = None
        if payload is None:
            try:
                payload = env.file.save()
            except Exception:
                return n, False
        if isinstance(payload, dict):      # 多文件 AssetBundle 的返回值
            payload = next(iter(payload.values()), b"")
        if not isinstance(payload, (bytes, bytearray)):
            return n, False
        _wrote, differs = write_back_file(src, dst, bytes(payload))
        return n, differs

    def _apply_textassets(self, env, us: list[TextUnit]) -> int:
        """TextAsset 分支：按 asset 名分组，用行号/CSV 行定位替换。"""
        by_asset: dict[str, list[TextUnit]] = {}
        for u in us:
            if "mb_field_path" in u.location:
                continue
            by_asset.setdefault(u.location.get("asset", ""), []).append(u)
        if not by_asset:
            return 0

        n = 0
        for obj in env.objects:
            if obj.type.name != "TextAsset":
                continue
            try:
                data = obj.read()
                name = getattr(data, "m_Name", "")
            except Exception:
                continue
            group = by_asset.get(name)
            if not group:
                continue
            try:
                text = bytes(data.m_Script).decode("utf-8")
            except (UnicodeDecodeError, AttributeError):
                continue
            new_text, cnt = self._apply_textasset(text, group)
            if cnt:
                # 写回 m_Script 并触发序列化（TextAsset.save() 会 set_raw_data）；
                # 少了这一步 env.file.save() 会原样吐回旧字节，改动静默丢失。
                data.m_Script = new_text.encode("utf-8")
                data.save()
                n += cnt
        return n

    def _apply_monobehaviours(self, env, us: list[TextUnit]) -> int:
        """MonoBehaviour 分支：按 path_id 定位对象，改 typetree 字段后写回。"""
        by_obj: dict[int, list[TextUnit]] = {}
        for u in us:
            pid = u.location.get("path_id")
            if pid is None:
                continue
            try:
                by_obj.setdefault(int(pid), []).append(u)
            except (TypeError, ValueError):
                continue
        if not by_obj:
            return 0

        n = 0
        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            try:
                group = by_obj.get(int(obj.path_id))
            except (TypeError, ValueError):
                continue
            if not group:
                continue
            try:
                tt = obj.read_typetree()
            except Exception:
                continue
            if not isinstance(tt, dict):
                continue
            cnt = 0
            for u in group:
                if _set_mb_field(tt, u.location.get("mb_field_path") or [],
                                 u.original, restore(u.translated, u.protected)):
                    cnt += 1
            if cnt:
                try:
                    obj.save_typetree(tt)
                except Exception:
                    continue
                n += cnt
        return n

    def _apply_textasset(self, text: str, group: list[TextUnit]) -> tuple[str, int]:
        """把一组译文替换回 TextAsset 的文本，返回 (新文本, 替换条数)。"""
        lines = text.splitlines()
        # 原样保留换行风格（CSV 多为 CRLF，改成 LF 属于无谓的额外改动）
        nl = "\r\n" if "\r\n" in text else "\n"
        n = 0
        # 按行号分组
        by_line = {}
        for u in group:
            by_line.setdefault(u.location.get("line", u.location.get("row")), []).append(u)

        for lineno, us in sorted(by_line.items(), key=lambda kv: kv[0] if kv[0] else 0):
            if not isinstance(lineno, int) or lineno < 1 or lineno > len(lines):
                continue
            line = lines[lineno - 1]
            for u in us:
                val = restore(u.translated, u.protected)
                if u.location.get("lines_csv"):
                    # CSV 行：替换「说话人: 台词」里的台词部分（说话人前缀保留）
                    line, ok = _replace_csv_line_text(line, u, val)
                    if ok:
                        n += 1
                elif u.location.get("yarn_bracket"):
                    # Yarn 选项行：`[[正文|目标]] #line:xxx`
                    line, ok = _replace_yarn_bracket_text(line, u, val)
                    if ok:
                        n += 1
                elif u.location.get("yarn_choice"):
                    # Yarn 选项行：`-> 正文 <<命令>> #line:xxx`
                    line, ok = _replace_yarn_choice_text(line, u, val)
                    if ok:
                        n += 1
                elif u.location.get("yarn_group"):
                    # Yarn 3.x 行组：`=> 正文 #line:xxx`
                    line, ok = _replace_yarn_group_text(line, u, val)
                    if ok:
                        n += 1
                elif u.location.get("yarn"):
                    # Yarn 行：`说话人: 台词 #line:xxx`，只替换台词
                    line, ok = _replace_yarn_line_text(line, u, val)
                    if ok:
                        n += 1
                elif u.location.get("raw"):
                    if u.original in line:
                        line = line.replace(u.original, val, 1)
                        n += 1
                lines[lineno - 1] = line
        return nl.join(lines), n

    def _write_plain(self, src: Path, dst: Path, us: list[TextUnit]) -> tuple[int, bool]:
        from ..core.protect import restore
        text = src.read_text(encoding="utf-8", errors="replace")
        n = 0
        for u in us:
            val = restore(u.translated, u.protected)
            # 用原文精确替换首次出现
            if u.original in text:
                text = text.replace(u.original, val, 1)
                n += 1
        if not n:
            return 0, False
        return n, write_back_file(src, dst, text.encode("utf-8"))[1]

    def _write_json(self, src: Path, dst: Path, us: list[TextUnit]) -> tuple[int, bool]:
        from ..core.protect import restore
        data = json.loads(src.read_text(encoding="utf-8"))
        n = 0
        for u in us:
            kp = u.location.get("key_path", [])
            node = data
            try:
                for k in kp[:-1]:
                    node = node[k]
                node[kp[-1]] = restore(u.translated, u.protected)
                n += 1
            except (KeyError, IndexError, TypeError):
                continue
        if not n:
            return 0, False
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        return n, write_back_file(src, dst, payload)[1]

    def _write_csv(self, src: Path, dst: Path, us: list[TextUnit]) -> tuple[int, bool]:
        from ..core.protect import restore
        raw = src.read_text(encoding="utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",\t")
        except csv.Error:
            dialect = csv.excel_tab if src.suffix.lower() == ".tsv" else csv.excel
        rows = [r for r in csv.reader(io.StringIO(raw), dialect)]
        n = 0
        for u in us:
            r, c = u.location.get("row"), u.location.get("col")
            try:
                rows[r][c] = restore(u.translated, u.protected)
                n += 1
            except (IndexError, TypeError):
                continue
        if not n:
            return 0, False
        buf = io.StringIO(newline="")
        csv.writer(buf, dialect).writerows(rows)
        payload = buf.getvalue().encode("utf-8-sig")
        return n, write_back_file(src, dst, payload)[1]


def _splice_yarn_line(line: str, u: TextUnit, val: str,
                      expect: str) -> tuple[str, bool]:
    """回填一条 Yarn 语句：只替换正文，其余一个字节都不动。

    用与提取**同一套**语法解析重新切开当前行（``split_statement``），
    校验正文与原文一致后才替换；对不上就整行不动 —— 宁可漏翻一条，
    也绝不把脚本写坏。

    ``expect`` 是期望的语句类型（``line`` / ``option`` / ``group`` /
    ``bracket``），类型不符也一律不动。

    替换区间按「该类型 original 的语义」决定：

    - ``line``：译文里**含**行尾 ``#line``/``<<命令>>``（由 protect 保护、
      restore 还原），所以替换到行尾；
    - 其余：译文是纯正文，行尾结构尾巴（``|目标]]``/``<<命令>>``/``#line``）
      必须原样保留，故只替换正文那一段。
    """
    st = split_statement(line)
    if st is None or st.kind != expect:
        return line, False
    _name, sp_prefix, body = yarn_split_speaker(st.text)
    check = (body + (st.tail if expect == "line" else "")).strip()
    if check != (u.original or "").strip():
        return line, False
    body_start = len(st.prefix) + len(sp_prefix)
    if expect == "line":
        return line[:body_start] + val, True
    body_end = len(st.prefix) + len(st.text)
    return line[:body_start] + val + line[body_end:], True


def _replace_yarn_bracket_text(line: str, u: TextUnit, val: str) -> tuple[str, bool]:
    """替换 Yarn 1.x 快捷选项 ``[[正文|目标]] #line:xxx`` 里的正文。

    只动竖线左边那一段：缩进、``[[``、``|目标]]``、行尾 ``#line`` 全部
    原样拼回。正文里的 ``{locator=..}`` 属性与 ``[wave]..[/wave]`` 标签由
    protect 保护，``val`` 已是还原后的完整正文，直接整体替换即可。
    """
    return _splice_yarn_line(line, u, val, "bracket")


def _replace_yarn_line_text(line: str, u: TextUnit, val: str) -> tuple[str, bool]:
    """替换 Yarn 普通的「台词行」正文（**允许没有说话人前缀**）。

    只替换说话人之后的正文，保留说话人前缀与缩进。``val`` 已经过
    protect/restore 还原，本身含完整的正文（含 #line 标签），故直接
    整体替换、不再追加标签。
    """
    return _splice_yarn_line(line, u, val, "line")


def _replace_yarn_choice_text(line: str, u: TextUnit, val: str) -> tuple[str, bool]:
    """替换 Yarn 选项行 ``-> 正文 <<命令>> #line:xxx`` 里的正文。

    只动「正文」这一段，``->`` 前缀、行尾 ``#line``/``<<命令>>``/``// 注释``
    与正文前导空白全部原样拼回 —— 少改一个字节就少一分写坏脚本的风险。
    选项正文带说话人前缀时（``-> Captain: 台词``，Yarn 3.x 允许），
    说话人同样原样保留。
    """
    return _splice_yarn_line(line, u, val, "option")


def _replace_yarn_group_text(line: str, u: TextUnit, val: str) -> tuple[str, bool]:
    """替换 Yarn 3.x 行组 ``=> 正文 #line:xxx`` 里的正文。"""
    return _splice_yarn_line(line, u, val, "group")


def _replace_csv_line_text(line: str, u: TextUnit, val: str) -> tuple[str, bool]:
    """替换 *_lines 的 CSV 行 `line:code,说话人: 台词,注释` 里的台词。

    台词可能带双引号包裹（含逗号时）。只改台词，保留 line code、说话人前缀、注释。
    """
    try:
        row = next(csv.reader([line]))
    except (csv.Error, StopIteration):
        return line, False
    if len(row) < 2:
        return line, False
    text_col = row[1]
    speaker = u.location.get("speaker", "")
    # 重建台词列：有说话人则保留前缀
    if speaker:
        new_text_col = f"{speaker}: {val}"
    else:
        new_text_col = val
    row[1] = new_text_col
    # 重新序列化为 CSV 行（与原文风格尽量一致：quote 由 csv 自动处理）
    out = io.StringIO()
    csv.writer(out, lineterminator="").writerow(row)
    return out.getvalue(), True
