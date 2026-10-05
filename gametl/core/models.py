"""核心数据模型：贯穿「提取 -> 翻译 -> 回填」全链路。

设计原则：
1. 所有引擎的提取结果统一收敛为 TextUnit 列表，后续流程与引擎解耦。
2. TextUnit 必须携带足够信息以便无损回填（源文件、位置、原始文本、上下文）。
3. 翻译状态与文本分离，方便断点续传。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class EngineType(str, Enum):
    """支持的引擎类型。"""
    KIRIKIRI = "kirikiri"      # .xp3 封包（吉里吉里 / KAG）
    UNITY = "unity"            # Unity 引擎
    RPGMAKER_MV = "rpgmaker_mv"    # RPG Maker MV / MZ
    RENPY = "renpy"            # Ren'Py
    BUDDHA = "buddha"          # Double Fine 系（.~h/.~p 的 dfpf 包）
    PLAINTEXT = "plaintext"    # 无引擎特征的明文文本目录（通用兜底）
    UNKNOWN = "unknown"


class TextKind(str, Enum):
    """文本类型，影响翻译提示词与回填策略。"""
    DIALOGUE = "dialogue"      # 对话 / 旁白
    UI = "ui"                  # 界面文字、菜单项
    NAME = "name"              # 角色名 / 物品名
    DESCRIPTION = "description"  # 描述文本、帮助
    SYSTEM = "system"          # 系统提示
    OTHER = "other"


@dataclass
class TextUnit:
    """一条待翻译文本单元。

    Attributes:
        uid: 全局唯一 ID（基于文件+位置+原文生成，保证可复现）。
        source_file: 相对于游戏根目录的源文件路径。
        location: 引擎相关的定位信息（行号 / 键路径 / 字节偏移等）。
        original: 原始文本。
        translated: 译文；未翻译为 None。
        kind: 文本类型。
        context: 上下文信息（说话人、所在场景等），供翻译时参考。
        extra: 引擎特有的附加信息，回填时使用。
        protected: 不应翻译的占位/变量（如 [name]、%s、\\n），翻译时保持原样。
    """
    uid: str
    source_file: str
    location: dict[str, Any]
    original: str
    translated: Optional[str] = None
    kind: TextKind = TextKind.OTHER
    context: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    protected: list[str] = field(default_factory=list)

    @staticmethod
    def make_uid(source_file: str, location: dict[str, Any], original: str) -> str:
        raw = f"{source_file}|{json.dumps(location, sort_keys=True, ensure_ascii=False)}|{original}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TextUnit":
        d = dict(d)
        d["kind"] = TextKind(d.get("kind", "other"))
        return cls(**d)


@dataclass
class Project:
    """一次汉化项目。"""
    root: Path                       # 游戏根目录
    engine: EngineType
    units: list[TextUnit] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def stats(self) -> dict[str, Any]:
        total = len(self.units)
        done = sum(1 for u in self.units if u.translated)
        chars = sum(len(u.original) for u in self.units)
        return {
            "engine": self.engine.value,
            "total_units": total,
            "translated_units": done,
            "progress": f"{done / total * 100:.1f}%" if total else "0%",
            "total_chars": chars,
            "kinds": self._kind_breakdown(),
        }

    def _kind_breakdown(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for u in self.units:
            out[u.kind.value] = out.get(u.kind.value, 0) + 1
        return out

    # ---------------- 完成状态（避免重复劳动） ----------------

    def progress(self) -> tuple[int, int]:
        """返回 (已有译文的条数, 总条数)。"""
        total = len(self.units)
        filled = sum(1 for u in self.units if u.translated)
        return filled, total

    def is_complete(self) -> bool:
        """是否已经全部翻完。"""
        filled, total = self.progress()
        return total > 0 and filled >= total

    def unverified_uids(self) -> list:
        """没有通过第一层硬校验、被兜底保留下来的条目。

        这些条目**有译文**（所以不会再被重翻），但值得人工扫一眼。
        """
        return [u.uid for u in self.units
                if u.translated and u.extra.get("unverified")]

    def mark_finished(self, path: Path, *, game_dir: str = "",
                      out_dir: str = "") -> bool:
        """写下「这个工程已经翻完了」的标记，并全量保存。

        标记存在的意义：下次再选同一个游戏时，程序一眼就能判定
        「翻译 + 校对 + 回填」都不必重跑 —— 而不是把几万条文本、
        上百个数据文件重新写一遍。

        只有真的 100% 翻完才会写。返回是否写入成功。
        """
        filled, total = self.progress()
        if total <= 0 or filled < total:
            return False
        self.meta.update({
            "engine": self.engine.value,
            "units": total,
            "filled": filled,
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "unverified": len(self.unverified_uids()),
            "game_dir": game_dir or str(self.meta.get("game_dir") or ""),
            "out_dir": out_dir or str(self.meta.get("out_dir") or ""),
        })
        self.save(path)
        return True

    @staticmethod
    def read_meta(path: Path, head_bytes: int = 524288) -> dict:
        """只读工程文件开头的 ``meta``，不解析整个文件。

        7.7 万条的工程有 30MB 左右，完整解析要一两秒；而 `meta` 在
        `units` 之前，所以读开头几百 KB 就够 —— 选目录时做「这个游戏
        翻过没有」的判断，必须是**瞬间**的。解析失败一律返回空 dict。
        """
        path = Path(path)
        try:
            with open(path, "rb") as f:
                head = f.read(head_bytes).decode("utf-8", errors="ignore")
        except OSError:
            return {}
        key = '"meta"'
        i = head.find(key)
        if i < 0:
            return {}
        j = head.find("{", i + len(key))
        if j < 0:
            return {}
        depth = 0
        in_str = False
        esc = False
        for k in range(j, len(head)):
            c = head[k]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(head[j:k + 1])
                    except ValueError:
                        return {}
                    return obj if isinstance(obj, dict) else {}
        return {}

    def save(self, path: Path, *, pretty: bool = False) -> None:
        """全量保存整个工程。

        Args:
            pretty: 是否缩进美化。默认**不缩进** —— 7.7 万条的工程缩进后会从
                    约 30MB 膨胀到 41MB，序列化也明显更慢，而这份文件是给
                    程序读的，不需要好看的排版。
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "root": str(self.root),
            "engine": self.engine.value,
            "meta": self.meta,
            "units": [u.to_dict() for u in self.units],
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False,
                       indent=2 if pretty else None),
            encoding="utf-8")

    # ---------------- 增量落盘（翻译热路径） ----------------

    @staticmethod
    def append_journal(entries, path: Path) -> int:
        """把新产出的译文追加进日志文件（JSON Lines）。

        这是翻译过程中唯一的磁盘写操作。相比「每落一次盘就把整个工程
        重写一遍」，追加写只碰新产生的几十字节，**成本与原工程大小无关** ——
        把总落盘开销从 O(N²) 拉回 O(N)。

        Args:
            entries: ``[(uid, 译文)]`` 或 ``[(uid, 译文, 是否未通过校验)]``。
                     第三项为真时写 ``uv:1``，标记「译文是兜底留下来的，
                     有译文但没通过硬校验」。
            path: 日志文件路径（约定 *.journal.jsonl）

        Returns:
            实际写入行数
        """
        if not entries:
            return 0
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with open(path, "a", encoding="utf-8") as f:
            for item in entries:
                try:
                    uid, t = item[0], item[1]
                    uv = bool(item[2]) if len(item) > 2 else False
                except (TypeError, IndexError):
                    continue
                if t is None:
                    continue
                rec: dict[str, Any] = {"uid": uid, "t": t}
                if uv:
                    rec["uv"] = 1
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
        return n

    @staticmethod
    def apply_journal(project: "Project", path: Path) -> int:
        """把日志里的译文合并回工程对象（后写的记录覆盖先写的）。"""
        path = Path(path)
        if not path.exists():
            return 0
        by_uid = {u.uid: u for u in project.units}
        n = 0
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    u = by_uid.get(rec.get("uid"))
                    if u is not None and rec.get("t") is not None:
                        u.translated = rec["t"]
                        if rec.get("uv"):
                            u.extra["unverified"] = True
                        else:
                            u.extra.pop("unverified", None)
                        n += 1
        except OSError:
            return 0
        return n

    @classmethod
    def compact(cls, project_path: Path, journal_path: Optional[Path] = None,
                meta_update: Optional[dict] = None) -> "Project":
        """把增量日志合并写回工程文件，并删除日志。

        只在流程结束（或需要一份干净快照）时调用一次，成本 O(N) 可以接受。

        Args:
            meta_update: 顺便写进 ``meta`` 的键值（例如完成标记）。
        """
        project_path = Path(project_path)
        proj = cls.load(project_path, journal=journal_path)
        if meta_update:
            proj.meta.update(meta_update)
        proj.save(project_path)
        if journal_path is not None:
            try:
                Path(journal_path).unlink(missing_ok=True)
            except OSError:
                pass
        return proj

    @classmethod
    def load(cls, path: Path, journal: Optional[Path] = None) -> "Project":
        """读取工程。若给了 journal，则把增量译文一并应用上去。"""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        proj = cls(
            root=Path(payload["root"]),
            engine=EngineType(payload["engine"]),
            units=[TextUnit.from_dict(u) for u in payload["units"]],
            meta=payload.get("meta", {}),
        )
        if journal is not None:
            cls.apply_journal(proj, journal)
        return proj
