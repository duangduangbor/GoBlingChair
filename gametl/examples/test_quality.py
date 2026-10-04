# -*- coding: utf-8 -*-
"""验证模型抽检模块的解析与抽样逻辑（不需要真实模型）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gametl.core.models import TextKind, TextUnit  # noqa: E402
from gametl.translators.quality import (  # noqa: E402
    QualityChecker, ReviewResult, sample_deterministic,
)

PASS = 0
FAIL = 0


def expect(label, got, want):
    global PASS, FAIL
    if got == want:
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


def main() -> int:
    print("=" * 62)
    print("模型输出解析（_parse_flags）")
    print("=" * 62)
    p = QualityChecker._parse_flags
    expect("标准数组", p("[1,2,3]"), [1, 2, 3])
    expect("空数组（全部合格）", p("[]"), [])
    expect("代码块包裹", p("```json\n[2]\n```"), [2])
    expect("对象包装 problems", p('{"problems": [4,5]}'), [4, 5])
    expect("对象包装 issues", p('{"issues": [7]}'), [7])
    expect("键值对形式", p('{"1": true, "2": false, "5": true}'), [1, 5])
    expect("前后有噪声", p("好的，问题是 [3] 这些"), [3])
    expect("非法内容", p("完全没问题"), None)
    expect("空串", p(""), None)

    print()
    print("=" * 62)
    print("确定性抽样（sample_deterministic）")
    print("=" * 62)

    units = [TextUnit(uid=f"u{i}", source_file="a.ks", location={"i": i},
                      original=f"文本{i}", translated=f"译文{i}",
                      kind=TextKind.DIALOGUE)
             for i in range(100)]

    s10 = sample_deterministic(units, 0.1)
    expect("10% 抽样条数", len(s10), 10)
    expect("同样输入两次抽样结果一致",
           [u.uid for u in sample_deterministic(units, 0.1)],
           [u.uid for u in s10])

    s100 = sample_deterministic(units, 1.0)
    expect("100% 抽样等于全量", len(s100), 100)

    s0 = sample_deterministic(units, 0.001)
    expect_true("极小比例至少抽 1 条", len(s0) >= 1)

    expect("空列表", sample_deterministic([], 0.1), [])

    print()
    print("=" * 62)
    print("ReviewResult 汇总文案")
    print("=" * 62)
    r = ReviewResult()
    expect_true("零检查时的文案", "没有可检查" in r.summary())

    r2 = ReviewResult(checked=100, batches=5)
    expect_true("无问题时的文案", "未发现问题" in r2.summary())

    from gametl.core.validate import Issue
    r3 = ReviewResult(checked=100, batches=5, failed_batches=1)
    r3.flagged.append(Issue("u1", "model_flag", "存疑", "原文", "译文", "a.ks"))
    s = r3.summary()
    expect_true("有问题时的文案含数量", "1 条存疑" in s)
    expect_true("有问题时的文案提示失败批次", "1 批请求失败" in s)

    print()
    print("=" * 62)
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
