"""术语表管理：保证同一术语在全项目翻译一致。

例如 "ギルド" 必须统一译为 "公会"，不能一处 "公会" 一处 "行会"。
术语表是 dict: {原文术语: 译文术语}，翻译时注入提示词。
"""
from __future__ import annotations

import json
from pathlib import Path


class Glossary:
    def __init__(self, mapping: dict[str, str] | None = None):
        self.mapping: dict[str, str] = dict(mapping or {})

    @classmethod
    def load(cls, path: Path | None) -> "Glossary":
        if path and Path(path).exists():
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls(data)
        return cls()

    def save(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(self.mapping, ensure_ascii=False, indent=2), encoding="utf-8")

    def relevant_for(self, text: str, limit: int = 20) -> dict[str, str]:
        """返回文本中实际出现的术语（避免提示词过长）。"""
        hit = {k: v for k, v in self.mapping.items() if k in text}
        if len(hit) > limit:
            # 按术语长度降序，优先保留长术语
            hit = dict(sorted(hit.items(), key=lambda kv: -len(kv[0]))[:limit])
        return hit

    def __len__(self) -> int:
        return len(self.mapping)
