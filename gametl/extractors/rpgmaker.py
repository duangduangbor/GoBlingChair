"""RPG Maker MV / MZ 文本提取器。

RPG Maker MV/MZ 的文本分三个层次，缺一层就会出现「对话翻了、菜单还是日文」：

1. ``www/data/*.json`` —— 数据表
   - Actors / Enemies / Items / Skills ...：name / description / message1-4
   - CommonEvents.json / Map*.json：事件指令，401 = 显示文字，405 = 续行
   - **System.json**：这是界面文字的大本营
     · ``terms.basic`` / ``terms.commands`` / ``terms.params``（数组）
     · ``terms.messages``（**对象**，51 项战斗提示 —— 早期版本按数组处理，
       整个字段被静默跳过）
     · ``armorTypes`` / ``weaponTypes`` / ``skillTypes`` / ``elements``（顶层数组）
     · ``gameTitle`` / ``currencyUnit``（顶层字符串）
2. ``www/js/plugins.js`` —— 插件参数。作者专门留给使用者填界面文字的地方，
   典型形态是**嵌套 JSON 字符串**，例如::

       "parameters": {"baseItems": "[{\"name\":\"サブステータス\",\"command\":...}]"}

   只当普通字符串翻译，得到的是一堆被转义的乱码；必须递归下钻。
3. ``www/js/plugins/*.js`` —— 插件源码里硬编码的界面字面量。
   只挑**含假名/汉字/全角**的短字符串，纯 ASCII 的标识符一律不碰。

回填时按位置信息写回，可无损还原。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..core.models import Project, TextKind, TextUnit
from ..core.protect import protect
from .base import BaseExtractor, wb_stats

# 指令码 -> 语义：401/405 是显示文字
CODE_MESSAGE = 401
CODE_MESSAGE_CONT = 405

# 其他需要翻译的字段
FIELD_KINDS: dict[str, TextKind] = {
    "name": TextKind.NAME,
    "description": TextKind.DESCRIPTION,
    "message1": TextKind.SYSTEM,
    "message2": TextKind.SYSTEM,
    "message3": TextKind.SYSTEM,
    "message4": TextKind.SYSTEM,
    "note": TextKind.OTHER,
    "title": TextKind.UI,
    "profile": TextKind.DESCRIPTION,
}

# System.json 里以字符串数组形式存在的界面词
SYS_ARRAY_FIELDS = (
    "armorTypes", "weaponTypes", "skillTypes", "elements",
)
# System.json 里以单个字符串存在的界面词
SYS_TEXT_FIELDS = (
    "gameTitle", "currencyUnit",
)

# 需要跳过翻译的 JSON 文件（数据表，无文本或不该翻）
SKIP_FILES = {
    "MapInfos.json", "Tilesets.json", "Animations.json",
    "TilesetFlags.json", "PluginConfig.json", "Pictures.json",
}

# ---- JS 层的字符判定 ----------------------------------------------------

# 假名（平/片假名 + 半角片假名）
KANA_RE = re.compile(r"[\u3041-\u309f\u30a0-\u30ff\uff66-\uff9f]")
# 假名 + 汉字 + 全角字母数字 —— 「像是给人看的日文」的粗判
JP_RE = re.compile(
    r"[\u3041-\u309f\u30a0-\u30ff\uff66-\uff9f"
    r"\u4e00-\u9fff\uff21-\uff3a\uff41-\uff5a\uff10-\uff19]")
# 纯 ASCII 标识符 / 路径（没有空格、没有日文）—— 一律不当文本
IDENT_RE = re.compile(r"^[A-Za-z0-9_./\\:\-+@#%*()\[\]{}<>$,;=~^|!?'\" ]+$")

# JS 里的单/双引号字符串（不跨行）
JS_STR_RE = re.compile(r"(['\"])((?:\\.|(?!\1)[^\\\n])*)\1")

# 插件源码里字符串长度上限 —— 超过多半是数据/脚本，不是界面文字
JS_TEXT_MAX_LEN = 120
# 长度下限。单字符必须挡下：RPG Maker 的名称输入界面在
# rpg_windows.js / rpg_objects.js 里硬编码了五十音表与全角字母表
# （'あ','い',... 'Ａ','Ｂ',...），一旦被当成界面文字翻掉，
# 玩家就没法给角色起名了。
JS_TEXT_MIN_LEN = 2


def _is_translatable(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not s:
        return False
    if not any(c.isalnum() or c.isalpha() or ord(c) > 0x2E80 for c in s):
        return False
    return True


def _js_text_ok(v: Any) -> bool:
    """JS 里的字符串值是否像「给玩家看的界面文字」。

    判据：必须含假名/汉字/全角字符，且不是纯 ASCII 标识符或路径。
    这条规则能把 ``"customstatus"``、``"img/pictures/a.png"``、``"true"``
    之类的机器串全部挡在外面，只留下真正的界面词。
    """
    if not isinstance(v, str):
        return False
    s = v.strip()
    if not s or len(s) > JS_TEXT_MAX_LEN or len(s) < JS_TEXT_MIN_LEN:
        return False
    if not JP_RE.search(s):
        return False
    if IDENT_RE.match(s):
        return False
    if s.startswith(("http://", "https://", "data:", "file:")):
        return False
    return True


class RPGMakerExtractor(BaseExtractor):
    """RPG Maker MV/MZ 提取器。"""

    def __init__(self, decoded_dir: Path, *, enable_js: bool = True,
                 enable_plugin_source: bool = True,
                 enable_engine_js: bool = False):
        """
        Args:
            decoded_dir: 游戏（或 www 的父级）目录
            enable_js: 是否处理 ``www/js/plugins.js`` 的插件参数
            enable_plugin_source: 是否处理 ``www/js/plugins/*.js``
                里硬编码的界面字面量（实验性，可用界面开关关掉）
            enable_engine_js: 是否连引擎核心 ``www/js/rpg_*.js`` 一起改。
                **默认关闭**：实测这些文件里的日文几乎全是**名称输入界面的
                五十音表/字母表**那类数据，翻掉会直接废掉起名功能，
                而引擎自带的界面词本来就由 System.json 的 terms 负责。
        """
        super().__init__(decoded_dir)
        self.enable_js = enable_js
        self.enable_plugin_source = enable_plugin_source
        self.enable_engine_js = enable_engine_js
        # 定位 data 目录
        self.data_dir = None
        for cand in (self.decoded_dir / "www" / "data",
                     self.decoded_dir / "data", self.decoded_dir):
            if cand.is_dir() and list(cand.glob("*.json")):
                self.data_dir = cand
                break
        # 定位 js 目录：优先与 data 同级（www/js）
        self.js_dir = None
        if self.data_dir is not None:
            for cand in (self.data_dir.parent / "js",
                         self.decoded_dir / "js"):
                if cand.is_dir():
                    self.js_dir = cand
                    break
        # 各来源的提取条数，供界面说明「为什么还是日文」
        self.stats: dict[str, int] = {}

    # ---------------- 提取 ----------------

    def extract(self) -> list[TextUnit]:
        if self.data_dir is None:
            raise FileNotFoundError("未找到 RPG Maker 的 data 目录")

        units: list[TextUnit] = []
        n_data = 0
        for path in sorted(self.data_dir.glob("*.json")):
            if path.name in SKIP_FILES:
                continue
            rel = self._rel(path)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                continue

            before = len(units)
            if path.name.startswith("CommonEvents") or path.name.startswith("Map"):
                units.extend(self._extract_events(data, rel))
            else:
                units.extend(self._extract_objects(data, rel))
                if path.name == "System.json":
                    units.extend(self._extract_system_extra(data, rel))
            n_data += len(units) - before
        self.stats["data"] = n_data

        n_js = 0
        if self.enable_js:
            n_js += self._extract_plugins_js(units)
        if self.enable_plugin_source:
            n_js += self._extract_plugin_sources(units)
        self.stats["js"] = n_js
        return units

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.decoded_dir)).replace("\\", "/")
        except ValueError:
            return path.name

    def _mk(self, rel: str, loc: dict, text: str, kind: TextKind,
            context: dict | None = None) -> TextUnit:
        prot, frags = protect(text)
        return TextUnit(
            uid=TextUnit.make_uid(rel, loc, text),
            source_file=rel,
            location=loc,
            original=text,
            kind=kind,
            context=context or {},
            protected=frags,
            extra={"protected_form": prot},
        )

    # ---- data/*.json ----

    def _extract_objects(self, data: Any, rel: str) -> list[TextUnit]:
        """处理对象数组型文件（Actors/Items/Skills...）。"""
        units: list[TextUnit] = []
        # System.json 是单对象；其余多为 [null, {...}, ...]
        objects: list[tuple[int, dict]] = []
        if isinstance(data, list):
            for i, obj in enumerate(data):
                if isinstance(obj, dict):
                    objects.append((i, obj))
        elif isinstance(data, dict):
            objects = [(-1, data)]

        for idx, obj in objects:
            for field, kind in FIELD_KINDS.items():
                val = obj.get(field)
                if field == "note":
                    continue  # note 常含插件指令，默认跳过
                if _is_translatable(val):
                    loc = {"index": idx, "field": field} if idx >= 0 else {"field": field}
                    units.append(self._mk(rel, loc, val, kind))
            # System.json 的 terms：数组组 + 对象组都要处理
            terms = obj.get("terms")
            if isinstance(terms, dict):
                base = {"field": "terms"} if idx < 0 else {"index": idx, "field": "terms"}
                for grp, val in terms.items():
                    if isinstance(val, list):
                        for i, t in enumerate(val):
                            if _is_translatable(t):
                                units.append(self._mk(
                                    rel, {**base, "group": grp, "index": i},
                                    t, TextKind.UI))
                    elif isinstance(val, dict):
                        # terms.messages 是**对象**不是数组 ——
                        # 早期版本只认 list，整个字段被静默跳过
                        for key, t in val.items():
                            if _is_translatable(t):
                                units.append(self._mk(
                                    rel, {**base, "group": grp, "key": key},
                                    t, TextKind.SYSTEM))
        return units

    def _extract_system_extra(self, data: Any, rel: str) -> list[TextUnit]:
        """System.json 里除 terms 之外的界面词。"""
        units: list[TextUnit] = []
        obj = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else data
        if not isinstance(obj, dict):
            return units
        idx = 0 if isinstance(data, list) else -1
        for field in SYS_ARRAY_FIELDS:
            arr = obj.get(field)
            if not isinstance(arr, list):
                continue
            for i, t in enumerate(arr):
                if _is_translatable(t):
                    units.append(self._mk(
                        rel, {"top": field, "index": i}, t,
                        TextKind.UI if field != "elements" else TextKind.NAME))
        for field in SYS_TEXT_FIELDS:
            v = obj.get(field)
            if _is_translatable(v):
                units.append(self._mk(rel, {"top": field}, v, TextKind.UI))
        return units

    def _extract_events(self, data: Any, rel: str) -> list[TextUnit]:
        """处理事件型文件（CommonEvents / Map）。递归查找 401/405 指令。"""
        units: list[TextUnit] = []

        def walk(node: Any, path: list) -> None:
            if isinstance(node, dict):
                # 事件指令形如 {"code": 401, "parameters": ["勇者", "你好。"]}
                if node.get("code") == CODE_MESSAGE:
                    params = node.get("parameters") or []
                    speaker = params[0] if len(params) > 0 and isinstance(params[0], str) else ""
                    body = params[1] if len(params) > 1 else ""
                    if _is_translatable(body):
                        units.append(self._mk(
                            rel, {"event_path": path, "code": 401}, body,
                            TextKind.DIALOGUE,
                            context={"speaker": speaker} if speaker else None))
                    # 说话人也翻（角色名）
                    if _is_translatable(speaker):
                        units.append(self._mk(
                            rel, {"event_path": path, "code": 401, "part": "speaker"},
                            speaker, TextKind.NAME))
                for k, v in node.items():
                    if k == "parameters" and node.get("code") in (CODE_MESSAGE, CODE_MESSAGE_CONT):
                        continue
                    walk(v, path + [k])
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, path + [i])

        walk(data, [])
        return units

    # ---- js/plugins.js ----

    def _extract_plugins_js(self, units: list[TextUnit]) -> int:
        """插件参数。**嵌套 JSON 字符串要递归下钻**，否则拿到的是转义乱码。"""
        if self.js_dir is None:
            return 0
        path = self.js_dir / "plugins.js"
        if not path.is_file():
            return 0
        arr = self._read_plugins_js(path)
        if arr is None:
            return 0
        rel = self._rel(path)
        before = len(units)
        for plugin in arr:
            if not isinstance(plugin, dict):
                continue
            params = plugin.get("parameters")
            if not isinstance(params, dict):
                continue
            pname = str(plugin.get("name") or "")
            for key, val in params.items():
                loc = {"js": "plugins", "plugin": pname, "param": str(key)}
                if isinstance(val, str):
                    if not _is_translatable(val):
                        continue
                    self._walk_param(val, rel, loc, units)
                elif isinstance(val, (dict, list)):
                    # 参数值也可能是**真对象**而不是序列化字符串
                    # （例如 {"最遅":-2,"標準":0}）—— 同样要递归下钻
                    self._walk_param(json.dumps(val, ensure_ascii=False),
                                     rel, loc, units)
        return len(units) - before

    def _walk_param(self, text: str, rel: str, loc: dict,
                    units: list[TextUnit], depth: int = 0, path: list | None = None) -> None:
        """参数值可能是纯文字，也可能是**序列化后的 JSON 字符串**。

        后者（``"[{\\"name\\":\\"サブステータス\\"}]"``）必须解析开、逐项下钻，
        再整体序列化回去；只当字符串翻会得到一堆转义乱码。
        """
        path = path or []
        loc = dict(loc)
        loc["path"] = path
        s = text.strip()
        if depth < 6 and s[:1] in ("{", "["):
            try:
                obj = json.loads(s)
            except (json.JSONDecodeError, ValueError):
                obj = None
            if obj is not None:
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if isinstance(k, str) and _js_text_ok(k):
                            units.append(self._mk(
                                rel, {**loc, "path": path + [k], "as": "key"},
                                k, TextKind.UI))
                        self._walk_param(v if isinstance(v, str) else json.dumps(
                            v, ensure_ascii=False), rel, loc, units,
                            depth + 1, path + [k])
                elif isinstance(obj, list):
                    for i, v in enumerate(obj):
                        self._walk_param(v if isinstance(v, str) else json.dumps(
                            v, ensure_ascii=False), rel, loc, units,
                            depth + 1, path + [i])
                return
        if _js_text_ok(text):
            units.append(self._mk(rel, loc, text, TextKind.UI))

    @staticmethod
    def _read_plugins_js(path: Path) -> list | None:
        """把 ``plugins.js`` 还原成可解析的 JSON 数组。

        RPG Maker 生成的格式很固定::

            // Generated by RPG Maker.
            // Do not edit this file directly.
            var $plugins =
            [
            {"name":"..."},
            {"name":"..."}
            ];
        """
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        raw = raw.lstrip("\ufeff")
        lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("//")]
        body = "\n".join(lines)
        m = re.search(r"var\s+\$plugins\s*=\s*", body)
        if m:
            body = body[m.end():]
        body = body.strip().rstrip(";").strip()
        try:
            arr = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return None
        return arr if isinstance(arr, list) else None

    # ---- js/plugins/*.js ----

    def _extract_plugin_sources(self, units: list[TextUnit]) -> int:
        """插件源码里硬编码的界面字面量。

        只认**含假名/汉字/全角**的短字符串 —— 纯 ASCII 的标识符、路径、
        布尔值一律不碰。这样 ``"customstatus"`` 会被挡下，``'クエスト確認'``
        会被留下。单字符也要挡下（那是名称输入的五十音表）。
        """
        if self.js_dir is None:
            return 0
        before = len(units)
        targets: list[Path] = []
        pdir = self.js_dir / "plugins"
        if pdir.is_dir():
            targets.extend(sorted(p for p in pdir.glob("*.js") if p.is_file()))
        if self.enable_engine_js:
            targets.extend(sorted(p for p in self.js_dir.glob("rpg_*.js")
                                  if p.is_file()))
        for path in targets:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = self._rel(path)
            for m in JS_STR_RE.finditer(text):
                s = m.group(2)
                if not _js_text_ok(s):
                    continue
                units.append(self._mk(
                    rel, {"js": "src", "off": m.start(2), "len": len(s)},
                    s, TextKind.UI))
        return len(units) - before

    # ---------------- 回填 ----------------

    def write_back(self, project: Project, out_dir: Path) -> dict:
        """把译文写回文件。**只写内容真的变了的文件**。

        「按需写入」不只是省时间：回填产物的 mtime 稳定之后，「这次导出
        哪些文件变了」才能靠 (size, mtime) 一眼看出来，增量导出才成立。
        """
        by_file: dict[str, list[TextUnit]] = {}
        for u in project.units:
            if u.translated:
                by_file.setdefault(u.source_file, []).append(u)

        out_dir = Path(out_dir)
        written = 0
        unchanged = 0
        replaced = 0
        changed: list[str] = []
        for rel, us in sorted(by_file.items()):
            src = self.decoded_dir / rel
            if not src.is_file():
                continue
            if rel.endswith("plugins.js") and any(
                    u.location.get("js") == "plugins" for u in us):
                stats = self._write_plugins_js(src, out_dir, rel, us)
            elif rel.endswith(".js"):
                stats = self._write_js_spans(src, out_dir, rel, us)
            else:
                stats = self._write_json(src, out_dir, rel, us)
            replaced += stats["replaced"]
            if stats["written"]:
                written += 1
            if stats.get("differs"):
                changed.append(rel)
            else:
                unchanged += 1
        return wb_stats(files_written=written, replaced=replaced,
                        unchanged=unchanged, changed=changed)

    def _put(self, src: Path, dst: Path, payload: bytes) -> tuple[bool, bool]:
        """落盘一个回填产物，返回 (是否真的写了, 是否与原文件不同)。

        第一个值用于保持 mtime 稳定（增量导出的前提）；第二个值用于告诉
        补丁安装器「这个文件真的被汉化改动了，要覆盖进游戏目录」。
        """
        try:
            differs = not src.is_file() or src.read_bytes() != payload
        except OSError:
            differs = True
        try:
            if dst.is_file() and dst.read_bytes() == payload:
                return False, differs
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(payload)
            return True, differs
        except OSError:
            return False, differs

    def _write_json(self, src: Path, out_dir: Path, rel: str,
                    us: list[TextUnit]) -> dict:
        from ..core.protect import restore
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return {"written": 0, "replaced": 0, "differs": False}
        n = 0
        for u in us:
            val = restore(u.translated, u.protected)
            if self._set_value(data, u, val):
                n += 1
        payload = json.dumps(data, ensure_ascii=False, indent=None,
                             separators=(",", ":")).encode("utf-8")
        wrote, differs = self._put(src, Path(out_dir) / rel, payload)
        return {"written": 1 if wrote else 0, "replaced": n,
                "differs": differs}

    def _write_plugins_js(self, src: Path, out_dir: Path, rel: str,
                          us: list[TextUnit]) -> dict:
        from ..core.protect import restore
        arr = self._read_plugins_js(src)
        if arr is None:
            return {"written": 0, "replaced": 0, "differs": False}
        by_plugin: dict[str, dict[str, list[TextUnit]]] = {}
        for u in us:
            by_plugin.setdefault(u.location.get("plugin", ""), {}) \
                .setdefault(u.location.get("param", ""), []).append(u)
        n = 0
        for plugin in arr:
            if not isinstance(plugin, dict):
                continue
            pname = str(plugin.get("name") or "")
            ploc = by_plugin.get(pname)
            if not ploc:
                continue
            params = plugin.get("parameters")
            if not isinstance(params, dict):
                continue
            for key, items in ploc.items():
                if key not in params:
                    continue
                new_val, cnt = self._apply_param(
                    params[key], items, restore)
                if cnt:
                    params[key] = new_val
                    n += cnt
        # 复刻 RPG Maker 自身的写法：每行一个插件对象
        header = ("// Generated by RPG Maker.\n"
                  "// Do not edit this file directly.\n"
                  "var $plugins =\n[\n")
        body = ",\n".join(json.dumps(o, ensure_ascii=False,
                                     separators=(",", ":")) for o in arr)
        payload = (header + body + "\n];\n").encode("utf-8")
        wrote, differs = self._put(src, Path(out_dir) / rel, payload)
        return {"written": 1 if wrote else 0, "replaced": n,
                "differs": differs}

    def _apply_param(self, value: Any, items: list[TextUnit],
                     restore) -> tuple[Any, int]:
        """把一个插件参数里的译文写回去（必要时重建嵌套 JSON 字符串）。"""
        # 先分出「本层直接改」与「要下钻」的
        direct = [u for u in items if not u.location.get("path")]
        deep = [u for u in items if u.location.get("path")]
        n = 0
        for u in direct:
            if isinstance(value, str):
                value = restore(u.translated, u.protected)
                n += 1
        if not deep:
            return value, n
        # 参数值可能是「序列化后的 JSON 字符串」，也可能本来就是真对象
        nested_str = isinstance(value, str)
        if nested_str:
            s = value.strip()
            if s[:1] not in ("{", "["):
                return value, n
            try:
                obj = json.loads(s)
            except (json.JSONDecodeError, ValueError):
                return value, n
        elif isinstance(value, (dict, list)):
            obj = value
        else:
            return value, n
        for u in deep:
            tgt = obj
            ok = True
            for k in u.location["path"][:-1]:
                try:
                    tgt = tgt[k]
                except (KeyError, IndexError, TypeError):
                    ok = False
                    break
            if not ok:
                continue
            last = u.location["path"][-1]
            txt = restore(u.translated, u.protected)
            if u.location.get("as") == "key":
                # 翻译 JSON 的键（例如 {"最遅": -2} 里的选项名）
                if isinstance(tgt, dict) and last in tgt:
                    newd = {}
                    for k, v in tgt.items():
                        newd[txt if k == last else k] = v
                    tgt.clear()
                    tgt.update(newd)
                    n += 1
                continue
            try:
                tgt[last] = txt
                n += 1
            except (KeyError, IndexError, TypeError):
                continue
        if nested_str:
            return json.dumps(obj, ensure_ascii=False), n
        return obj, n

    def _write_js_spans(self, src: Path, out_dir: Path, rel: str,
                        us: list[TextUnit]) -> dict:
        """插件源码：按字符偏移**倒序**替换，前面的偏移就不会被打乱。"""
        from ..core.protect import restore
        try:
            text = src.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return {"written": 0, "replaced": 0, "differs": False}
        spans = sorted(
            (u for u in us if u.location.get("js") == "src"),
            key=lambda u: u.location.get("off", 0), reverse=True)
        n = 0
        for u in spans:
            off = u.location.get("off")
            ln = u.location.get("len")
            if not isinstance(off, int) or not isinstance(ln, int):
                continue
            if text[off:off + ln] != u.original:
                continue          # 文件被改过，位置对不上 —— 宁可不动
            text = text[:off] + restore(u.translated, u.protected) + text[off + ln:]
            n += 1
        payload = text.encode("utf-8")
        wrote, differs = self._put(src, Path(out_dir) / rel, payload)
        return {"written": 1 if wrote else 0, "replaced": n,
                "differs": differs}

    @staticmethod
    def _set_value(data: Any, u: TextUnit, value: str) -> bool:
        """按 location 定位并写入译文。"""
        loc = u.location
        try:
            # System.json 的顶层字段（gameTitle / armorTypes ...）
            if "top" in loc:
                top = loc["top"]
                if "index" in loc:
                    data[top][loc["index"]] = value
                else:
                    data[top] = value
                return True

            if "event_path" in loc:
                node = data
                for key in loc["event_path"]:
                    node = node[key]
                # node 是 code=401 的指令 dict
                if loc.get("part") == "speaker":
                    node["parameters"][0] = value
                else:
                    node["parameters"][1] = value
                return True

            if loc.get("field") == "terms":
                # 定位到 terms 这一层 —— 早先写成 data[group][index]，
                # 而 System.json 里组名在 terms 之下，于是每次都 KeyError，
                # 被 except 静默吞掉，菜单术语一条都没落地。
                if isinstance(data, list):
                    base = data[loc["index"]] if "index" in loc else data[0]
                else:
                    base = data
                terms = base["terms"]
                grp = terms[loc["group"]]
                if "key" in loc:
                    grp[loc["key"]] = value
                else:
                    grp[loc["index"]] = value
                return True

            idx = loc.get("index", -1)
            target = data[idx] if idx >= 0 else data
            target[loc["field"]] = value
            return True
        except (KeyError, IndexError, TypeError):
            return False
