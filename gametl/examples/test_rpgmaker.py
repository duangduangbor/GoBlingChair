# -*- coding: utf-8 -*-
"""RPG Maker 提取/回填的形状测试。

动机：RPG Maker 的「玩家可见文本」散在**多个指令码**里，且部分指令码的
参数形状有不止一种写法。早期实现只认 401/405 且写死「两元素参数」，
于是：

1. ``102 显示选项`` 的选项文本（玩家逐条点选读到的内容，和 Yarn 的
   ``-> 选项`` 完全同类）**一条都没提取过**；
2. ``401`` 只写了 ``[说话人, 正文]`` 一种形状，而原版 RPG Maker MV/MZ
   的真实形状是 ``[正文]``（说话人来自 101 的脸图设置）—— 单元素时正文
   会算成空串，「碰巧」被 params[0] 兜住才没漏翻，但条目标成了 name。

不依赖真实游戏与网络：直接构造事件指令 dict 走 _extract_events / _set_value。
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from gametl.extractors.rpgmaker import RPGMakerExtractor  # noqa: E402


def main():
    recs = []

    def rec(ok, name):
        recs.append((ok, name))

    ext = RPGMakerExtractor.__new__(RPGMakerExtractor)

    # ---------------------------------------------------------------- 提取
    data = [
        None,
        {
            "id": 1,
            "list": [
                {"code": 101, "indent": 0, "parameters": ["", 0, 0, 2]},
                # 单元素 401（原版 MV/MZ 形状）
                {"code": 401, "indent": 0, "parameters": ["おはよう。"]},
                # 两元素 401（本项目夹具 / 部分汉化约定）
                {"code": 401, "indent": 0, "parameters": ["アレン", "行こう。"]},
                # 显示选项
                {"code": 102, "indent": 0,
                 "parameters": [["はい", "いいえ"], 0, 0, 2, 0]},
                # 「当[选项]」分支
                {"code": 402, "indent": 1, "parameters": [0, "はい"]},
                {"code": 402, "indent": 1, "parameters": [1, "いいえ"]},
                # 其它指令码里的字符串**不该**被当成文本
                {"code": 117, "indent": 0, "parameters": ["CommonEvent", 3]},
            ],
        },
    ]
    units = ext._extract_events(data, "CommonEvents.json")
    by = {}
    for u in units:
        loc = u.location
        key = (loc.get("code"), loc.get("part"), loc.get("choice"))
        by[key] = u

    bodies401 = [u for u in units if u.location.get("code") == 401
                 and not u.location.get("part")]
    single = [u for u in bodies401 if u.original == "おはよう。"]
    two = [u for u in bodies401 if u.original == "行こう。"]

    rec(len(single) == 1, "401 单元素形状：正文被提取")
    rec(bool(single) and not (single[0].context or {}).get("speaker"),
        "401 单元素形状：不误判说话人")
    rec(len(two) == 1 and (two[0].context or {}).get("speaker") == "アレン",
        "401 两元素形状：正文被提取且说话人识别正确")
    rec(any(u.location.get("part") == "speaker" and u.original == "アレン"
            for u in units), "401 两元素形状：说话人被提取")

    ch = {u.location.get("choice"): u.original for u in units
          if u.location.get("code") == 102}
    rec(ch == {0: "はい", 1: "いいえ"},
        f"102 显示选项：选项文本被提取（实际 {ch}）")
    wh = sorted(u.original for u in units if u.location.get("code") == 402)
    rec(wh == ["いいえ", "はい"],
        f"402 当[选项]：选项文本副本被提取（实际 {wh}）")
    rec(not any("CommonEvent" in u.original for u in units),
        "其它指令码的参数不被当文本")

    # 不可翻译的选项（纯符号）要跳过
    u2 = ext._extract_events(
        [None, {"list": [{"code": 102, "indent": 0,
                          "parameters": [["はい", "...", "!!!"], 0, 0, 2, 0]}]}],
        "Map001.json")
    rec([x.original for x in u2] == ["はい"],
        f"102 里纯符号选项被跳过（实际 {[x.original for x in u2]}）")

    # ---------------------------------------------------------------- 回填
    def roundtrip(orig, mutate):
        d = copy.deepcopy(orig)
        us = ext._extract_events(d, "CommonEvents.json")
        for u in us:
            u.translated = "\u3010T\u3011" + u.original
        n = 0
        for u in us:
            if ext._set_value(d, u, u.translated):
                n += 1
        return d, us, n

    d2, us2, n2 = roundtrip(data, None)
    rec(n2 == len(us2), f"回填全部落地（{n2}/{len(us2)}）")
    lst = d2[1]["list"]
    rec(lst[1]["parameters"] == ["\u3010T\u3011おはよう。"],
        "401 单元素回填写回 params[0]（不撑出第二个元素）")
    rec(lst[2]["parameters"] == ["\u3010T\u3011アレン", "\u3010T\u3011行こう。"],
        "401 两元素回填：说话人与正文分别落位")
    rec(lst[3]["parameters"][0] == ["\u3010T\u3011はい", "\u3010T\u3011いいえ"],
        "102 回填：选项数组按下标替换")
    rec(lst[3]["parameters"][1:] == orig102_tail(),
        "102 回填：选项数组之外的参数原样不动")
    rec(lst[4]["parameters"][1] == "\u3010T\u3011はい"
        and lst[5]["parameters"][1] == "\u3010T\u3011いいえ",
        "402 回填：分支文本落地")
    rec(lst[6]["parameters"] == ["CommonEvent", 3],
        "其它指令码未被改动")

    # 位置对不上时不动
    d3 = copy.deepcopy(data)
    bad = ext._extract_events(d3, "x")[0]
    bad.location = {"event_path": [1, "list", 999], "code": 401}
    rec(ext._set_value(d3, bad, "X") is False, "路径不存在时拒绝写入")

    # ---------------------------------------------------------------- 汇总
    ok = sum(1 for o, _ in recs if o)
    total = len(recs)
    print("=" * 60)
    print(f"通过 {ok} / {total}")
    for o, name in recs:
        if not o:
            print(f"  失败: {name}")
    print("=" * 60)
    return 0 if ok == total else 1


def orig102_tail():
    return [0, 0, 2, 0]


if __name__ == "__main__":
    sys.exit(main())
