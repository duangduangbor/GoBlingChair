# -*- coding: utf-8 -*-
"""验证两层校验规则。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gametl.core.models import TextKind, TextUnit  # noqa: E402
from gametl.core.validate import (  # noqa: E402
    audit_units, check_immediate, filter_fixable,
)

PASS = 0
FAIL = 0


def expect(label, got, want):
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
        print(f"  [OK]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}\n         期望={want!r}\n         实际={got!r}")


def expect_true(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}")


def tu(uid, orig, trans, protected=None, kind=TextKind.DIALOGUE):
    u = TextUnit(uid=uid, source_file="a.ks", location={}, original=orig,
                 translated=trans, kind=kind)
    if protected:
        u.extra["protected_form"] = protected
    return u


def main() -> int:
    print("=" * 62)
    print("第一层：即时硬校验（返回 None = 通过）")
    print("=" * 62)
    expect("正常翻译", check_immediate("こんにちは", "你好"), None)
    expect("空译文", check_immediate("こんにちは", ""), "译文为空")
    expect("纯空白", check_immediate("こんにちは", "   "), "译文为空")
    expect("占位符保留正确", check_immediate("【0】さん、こんにちは",
                                            "【0】先生，你好"), None)
    expect_true("占位符丢失",
                (check_immediate("【0】さん、こんにちは",
                                 "先生，你好") or "").startswith("占位符异常"))
    expect_true("占位符编号错乱",
                (check_immediate("【0】と【1】",
                                 "【1】和【2】") or "").startswith("占位符异常"))
    expect_true("未翻译（仍为日文）",
                "未翻译" in (check_immediate("こんにちは", "こんにちは") or ""))
    expect_true("残留前缀",
                "前缀" in (check_immediate("おはよう", "译文：早上好") or ""))
    expect_true("模型发散（超长）",
                "冗长" in (check_immediate("はい",
                                          "好的" * 200) or ""))
    expect("英文原文不误判", check_immediate("Potion", "药水"), None)

    print()
    print("=" * 62)
    print("第二层：全量规则校对")
    print("=" * 62)

    units = [
        tu("u1", "こんにちは", "你好"),
        tu("u2", "おはようございます", ""),                       # 空
        tu("u3", "ギルドへ行こう", "让我们去公会吧"),               # 正常（术语命中）
        tu("u4", "ギルドの受付", "ギルドの受付"),                   # 与原文相同（未翻译）
        tu("u5", "ありがとう", "谢谢"),
        tu("u6", "どういたしまして", "谢谢"),                      # 与 u5 撞车
        tu("u7", "これはとても長い日本語の文章でございまして",
           "短"),                                                 # 过短
        tu("u8", "こんにちは、元気ですか", "你好，元気ですか"),      # 部分漏译
    ]
    rep = audit_units(units, glossary={"ギルド": "公会"})
    codes = rep.by_code()
    print(f"  统计：{rep.summary()}")
    print(f"  按代码：{codes}")

    expect_true("检出空译文", "empty" in codes)
    expect_true("检出部分漏译", "untranslated" in codes)
    expect_true("检出与原文相同（未翻译）", "identical" in codes)
    expect_true("检出多条同译", "dup_target" in codes)
    expect_true("检出过短", "too_short" in codes)

    fixable = rep.fixable_uids()
    print(f"  建议重翻：{fixable}")
    expect_true("建议重翻包含 u2/u4/u8",
                "u2" in fixable and "u4" in fixable and "u8" in fixable)
    expect_true("u5/u6 撞车应被标记", "u5" in fixable and "u6" in fixable)
    expect_true("u1 正常不应被标记", "u1" not in fixable)
    expect_true("u3 正常不应被标记", "u3" not in fixable)

    print()
    print("=" * 62)
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    print("=" * 62)

    # 附带展示 filter_fixable
    picked = filter_fixable(rep, units)
    print(f"filter_fixable 挑出 {len(picked)} 条：{[u.uid for u in picked]}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
