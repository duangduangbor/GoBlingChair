# -*- coding: utf-8 -*-
"""预过滤规则回归测试。

背景：曾经 `should_skip("Potion")` 返回 True —— 纯 ASCII 字母串被
`_ALREADY_CJK` 的字符集范围（含 A-Za-z）判成「已是中文」而整条跳过，
造成**英文原文漏翻**。这里把边界情况全部钉死。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gametl.translators.ollama_backend import should_skip  # noqa: E402

FAILED: list = []


def check(name: str, got: bool, want: bool) -> None:
    ok = got == want
    print(f"  [{'OK' if ok else '失败'}] {name}: {got}"
          + ("" if ok else f"（期望 {want}）"))
    if not ok:
        FAILED.append(name)


def main() -> int:
    print("=" * 62)
    print("预过滤规则（应跳过 = True，需要翻译 = False）")
    print("=" * 62)

    print("\n-- 必须跳过 --")
    for t in ["", "   ", "\n", "…", "——", "※", "12", "3.14", "1,234",
              "１００", "（注）", "？", "A"]:
        check(f"跳过 {t!r}", should_skip(t), True)

    print("\n-- 已是中文：跳过 --")
    for t in ["你好", "勇者大人，欢迎回来。", "公会接待处",
              "药水 x3", "第 12 章", "设置"]:
        check(f"跳过 {t!r}", should_skip(t), True)

    print("\n-- 英文：必须翻译（曾经的 bug 就在这里）--")
    for t in ["Potion", "Guild", "Save Game", "New Game", "HP", "Yes",
              "No", "Loading...", "Chapter 12", "Item Box", "HP:100"]:
        check(f"翻 {t!r}", should_skip(t), False)

    print("\n-- 日文：必须翻译 --")
    for t in ["ポーション", "ギルドの受付", "セーブ", "はい", "いいえ",
              "つづける", "こんにちは", "アイテム"]:
        check(f"翻 {t!r}", should_skip(t), False)

    print("\n-- 混合 / 边界 --")
    check("英中混合 'HP已满'", should_skip("HP已满"), True)
    check("日中混合 '剣を持て'", should_skip("剣を持て"), False)
    check("汉字+假名 '薬草です'", should_skip("薬草です"), False)
    check("单汉字 '剣'", should_skip("剣"), True)      # 长度 1，跳过
    check("两汉字 '剣士'", should_skip("剣士"), True)   # 已是中文语境
    check("英文带数字 'X2'", should_skip("X2"), False)

    print()
    if FAILED:
        print(f"[失败] {len(FAILED)} 项未通过：{', '.join(FAILED)}")
        return 1
    print("[OK] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
