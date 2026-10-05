# -*- coding: utf-8 -*-
"""Unity 提取器单元测试：覆盖 .yarn 与 *_lines 的提取/回填纯逻辑。

不依赖真实游戏与 UnityPy —— 用合成文本直接测 _extract_yarn、
_extract_lines_csv、_apply_textasset 与两个行替换辅助函数。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from gametl.extractors.unity import (
    UnityExtractor, _replace_yarn_line_text, _replace_csv_line_text,
)
from gametl.core.models import TextUnit, TextKind
from gametl.core.protect import protect, restore


def _unit(loc: dict, original: str) -> TextUnit:
    prot, frags = protect(original)
    return TextUnit(
        uid=TextUnit.make_uid("f", loc, original),
        source_file="f", location=loc, original=original, kind=TextKind.DIALOGUE,
        protected=frags, extra={"protected_form": prot},
    )


def main():
    recs = []
    def rec(ok, name):
        recs.append((ok, name))

    ext = UnityExtractor(Path("."))

    # ---- 1. .yarn 提取：只取「说话人: 台词」，跳过结构行 ----
    yarn = (
        "title: Harleys\n"
        "tags: \n"
        "colorID: 2\n"
        "position: 516,878\n"
        "---\n"
        "Mae: Hey it's the Harleys! #line:1519db\n"
        "Harley2: Little Joe you're under arrest #line:2afbec\n"
        "-> Do you kids know what DNA is? #line:2d8d78\n"
        "    Harley3: Yeah it's stuff #line:07ddfb\n"
        "<<if $harleys is 0>>\n"
        "    <<set $harleys += 1>>\n"
        "[[Harleys_00_00]]\n"
        "===\n"
    )
    us = ext._extract_yarn("f", {"form": "unitypy", "asset": "X.yarn"}, yarn)
    bodies = [u.original for u in us]
    rec(len(us) == 3, f".yarn 提取 3 条对话（实际 {len(us)}）")
    rec(bodies[0].startswith("Hey it's the Harleys"), ".yarn 台词正文正确")
    rec(us[0].context.get("speaker") == "Mae", ".yarn 说话人识别")
    rec(all("<<" not in b and "->" not in b and "[[" not in b for b in bodies),
        ".yarn 结构行被跳过")

    # ---- 2. *_lines CSV 提取 ----
    lines = (
        "LineCode,LineText,Comment\r\n"
        "line:a91c46,Mae: =_= ,\r\n"          # 颜文字，应被 _is_translatable 跳过
        "line:7d2e41,Mom: Ehi tesoro!,\r\n"
        'line:421ca7,"Mom: Notte movimentata, eh?",\r\n'
        "line:3839c1,\"No, sono solo stanca.\",\r\n"
    )
    us = ext._extract_lines_csv("f", {"form": "unitypy", "asset": "M_lines"}, lines)
    rec(len(us) == 3, f"*_lines 提取 3 条（颜文字被跳过，实际 {len(us)}）")
    rec(us[0].original == "Ehi tesoro!", f"*_lines 剥离说话人前缀（实际 {us[0].original!r}）")
    rec(us[0].context.get("speaker") == "Mom", "*_lines 说话人识别")
    rec(us[1].original == "Notte movimentata, eh?", "*_lines 引号包裹行解析")
    rec(us[2].context.get("speaker") in (None, ""), "*_lines 无说话人行")

    # ---- 3. .yarn 回填：保留说话人与 #line 标签 ----
    orig = "Hey it's the Harleys! What are you doing? #line:1519db"
    u = _unit({"yarn": True, "speaker": "Mae"}, orig)
    u.translated = restore("【译】" + protect(orig)[0], u.protected)
    line = "Mae: Hey it's the Harleys! What are you doing? #line:1519db"
    new, ok = _replace_yarn_line_text(line, u, u.translated)
    rec(ok, ".yarn 回填成功")
    rec(new.startswith("Mae: 【译】"), ".yarn 保留说话人前缀")
    rec(new.endswith("#line:1519db"), ".yarn 保留 #line 标签")

    # ---- 4. *_lines CSV 回填：保留 line code 与说话人 ----
    orig = "Ehi tesoro!"
    u = _unit({"lines_csv": True, "speaker": "Mom", "code": "line:7d2e41"}, orig)
    u.translated = "【译】" + orig
    line = "line:7d2e41,Mom: Ehi tesoro!,"
    new, ok = _replace_csv_line_text(line, u, u.translated)
    rec(ok, "*_lines 回填成功")
    rec(new.startswith("line:7d2e41,"), "*_lines 保留 line code")
    rec("Mom: 【译】Ehi tesoro!" in new, "*_lines 保留说话人+译文")

    # ---- 5. _apply_textasset 批量替换 ----
    text = "Mae: A #line:111\nMae: B #line:222\n"
    u1 = _unit({"yarn": True, "line": 1, "speaker": "Mae"}, "A #line:111")
    u2 = _unit({"yarn": True, "line": 2, "speaker": "Mae"}, "B #line:222")
    u1.translated = restore("甲" + protect("A #line:111")[0], u1.protected)
    u2.translated = restore("乙" + protect("B #line:222")[0], u2.protected)
    new_text, cnt = ext._apply_textasset(text, [u1, u2])
    rec(cnt == 2, f"_apply_textasset 替换 2 条（实际 {cnt}）")
    rec("Mae: 甲" in new_text and "Mae: 乙" in new_text, "_apply_textasset 译文落地")

    # ---- 汇总 ----
    ok = sum(1 for o, _ in recs if o)
    total = len(recs)
    print("=" * 60)
    print(f"通过 {ok} / {total}")
    for o, name in recs:
        if not o:
            print(f"  失败: {name}")
    print("=" * 60)
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
