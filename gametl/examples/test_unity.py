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
    rec(len(us) == 4, f".yarn 提取 4 条（3 台词 + 1 选项，实际 {len(us)}）")
    rec(bodies[0].startswith("Hey it's the Harleys"), ".yarn 台词正文正确")
    rec(us[0].context.get("speaker") == "Mae", ".yarn 说话人识别")
    rec(bodies[2] == "Do you kids know what DNA is?", ".yarn 选项行正文被提取")
    rec(us[2].location.get("yarn_choice") is True, ".yarn 选项行标记 yarn_choice")
    rec(all("<<" not in b and "->" not in b and "[[" not in b for b in bodies),
        ".yarn 结构行被跳过（选项行的 -> 前缀也不带进正文）")

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

    # ---- 4b. Yarn 选项行：玩家可见文本，曾经被当结构标记整行丢掉 ----
    # Yarn 的 `-> 正文` 既是二选一的选项，也常被拿来做「点击推进」的分句
    # 演出（Night in the Woods 的开场诗整段都是 `-> 一句`）。全库曾因此
    # 漏掉 800 多条玩家可见文本，一条都没翻。
    from gametl.extractors.unity import (
        _split_choice_line, _replace_yarn_choice_text,
    )
    for src, want in [
        ("-> hello #line:abc123", "hello"),
        ("->hello #line:abc123", "hello"),
        ("    -> indented text <<set $x to 1>> #line:abc123", "indented text"),
        ('-> "They went looking for the gods, #line:55770c',
         '"They went looking for the gods,'),
        ("-> text // 注释 #line:zzz", "text"),
    ]:
        parts = _split_choice_line(src)
        rec(parts is not None and parts[1].strip() == want,
            f"选项行拆分 {src[:34]!r} -> {want!r}")
    rec(_split_choice_line("-> ")[1].strip() == "", "空选项正文为空（不提取）")
    rec(_split_choice_line("-> <<jump Foo>>")[1].strip() == "",
        "纯跳转选项正文为空（不提取）")
    rec(_split_choice_line("Mae: 不是选项行") is None, "普通台词不误判成选项行")

    cu = _unit({"yarn_choice": True}, "Do you kids know what DNA is?")
    cu.translated = "你们小孩知道DNA是啥吗？"
    new, ok = _replace_yarn_choice_text(
        "-> Do you kids know what DNA is? #line:2d8d78", cu, cu.translated)
    rec(ok, "选项行回填成功")
    rec(new == "-> 你们小孩知道DNA是啥吗？ #line:2d8d78",
        f"选项行回填保留前缀与 #line（实际 {new!r}）")
    _, ok_bad = _replace_yarn_choice_text(
        "-> 完全不同的正文 #line:2d8d78", cu, cu.translated)
    rec(not ok_bad, "选项行正文对不上就不动（防错位写坏脚本）")
    cu2 = _unit({"yarn_choice": True}, "indented text")
    new3, ok3 = _replace_yarn_choice_text(
        "    -> indented text <<set $x to 1>> #line:abc123", cu2, "缩进正文")
    rec(ok3 and new3 == "    -> 缩进正文 <<set $x to 1>> #line:abc123",
        f"选项行回填保留缩进与内联命令（实际 {new3!r}）")

    # ---- 4c. Yarn 的另一种选项写法：`[[显示文本|跳转目标]]` ----
    # 与 `->` 并列的第二种选项语法。竖线**左边**才是玩家看到的文字，
    # 右边是节点跳转名，一个字都不能动。而 `[[NodeName]]`（无竖线）
    # 是纯跳转，不是选项，绝不能提取。全库曾因此漏掉 262 条。
    from gametl.extractors.unity import (  # noqa: E402
        YARN_BRACKET_CHOICE_RE, _replace_yarn_bracket_text,
    )
    for src, want in [
        ("[[Yes|Bed]] #line:0633e1", "Yes"),
        ("    [[Your blooooood.|YourBlood]] #line:dc9b81", "Your blooooood."),
        ('[[{locator=Left}People don\'t like you. Clearly.|PeopleDontLikeYou]]',
         "{locator=Left}People don't like you. Clearly."),
        # 正文里带 BBCode 标签：不能用「不含右方括号」的写法去切
        ('[[{locator=Right}[wave]"Hi!"[/wave]|HowsItGoing]] #line:2b55d3',
         '{locator=Right}[wave]"Hi!"[/wave]'),
        ("[[Isn't there supposed to be someone at the desk?|someone]]",
         "Isn't there supposed to be someone at the desk?"),
    ]:
        m = YARN_BRACKET_CHOICE_RE.match(src)
        rec(m is not None and m.group("body").strip() == want,
            f"方括号选项拆分 {src[:38]!r} -> {want[:30]!r}")
    rec(YARN_BRACKET_CHOICE_RE.match("        [[Harleys_00_00]]") is None,
        "纯跳转 [[Node]] 不算选项（不提取）")
    rec(YARN_BRACKET_CHOICE_RE.match("Mae: 普通台词") is None,
        "普通台词不误判成方括号选项")

    bu = _unit({"yarn_bracket": True},
               "Isn't there supposed to be someone at the desk?")
    bu.translated = "难道柜台不该有个人吗？"
    line_in = "[[Isn't there supposed to be someone at the desk?|someone]] #line:b64380"
    newb, okb = _replace_yarn_bracket_text(line_in, bu, bu.translated)
    rec(okb, "方括号选项回填成功")
    rec(newb == "[[难道柜台不该有个人吗？|someone]] #line:b64380",
        f"方括号选项回填保留 |目标 与 #line（实际 {newb!r}）")
    newb2, okb2 = _replace_yarn_bracket_text(
        "    [[  spaced out  |Target]] #line:aaa", 
        _unit({"yarn_bracket": True}, "spaced out"), "有缩进")
    # ⚠️ 断言在 v2.3 改过：旧实现会顺手吃掉正文字尾的两个空格
    # （`[[  spaced out  |X]]` → `[[  有缩进|X]]`）。那是**无谓的字节改动**：
    # 玩家看到的选项文本是 ASCII 诗的场合（NITW 大量使用），多改一个空格
    # 都可能改变排版。现在回填严格只替换正文那一段，其余字节原样保留。
    rec(okb2 and newb2 == "    [[  有缩进  |Target]] #line:aaa",
        f"方括号选项回填保留缩进与正文字尾空白（实际 {newb2!r}）")
    _, okb3 = _replace_yarn_bracket_text(
        "[[完全不同的正文|someone]] #line:b64380", bu, bu.translated)
    rec(not okb3, "方括号选项正文对不上就不动")

    # ---- 4e. 语法完整性：按官方编译器语法覆盖全部「玩家可见文本」写法 ----
    # 背景：早期是「扁平正则扫描」，每遇到一种新形态就补一条正则，因此
    # 返工过两次（先漏 `->` 选项，再漏 `[[文本|目标]]`）。v2.3 改为按
    # YarnSpinnerParser.g4 的语法解析（见 gametl/extractors/yarn.py），
    # 下面把所有会「显示给玩家」的形态一次钉死。
    from gametl.extractors.unity import (  # noqa: E402
        _replace_yarn_group_text, _has_real_text,
    )
    from gametl.extractors.yarn import (  # noqa: E402
        iter_statements, split_statement, split_speaker,
    )

    _full = (
        "title: Demo\n"
        "tags: \n"
        "colorID: 1\n"
        "position: 10,20\n"
        "background: cabin\n"          # 自定义 header：绝不能被当台词
        "---\n"
        "Narrator: Hello there.\n"
        "Empty Text #line:22a818\n"     # ← 裸台词（没有说话人）
        "1 - Succotash #line:3751d9\n"  # ← 裸台词 + 数字序号
        "$grocery_box\n"                # ← 只有变量，不该提取
        "-> Captain: Let's go!\n"       # ← 选项带说话人前缀
        "=> A line group entry\n"       # ← Yarn 3.x 行组
        "    =>   Indented group item\n"
        "[[Yes|Bed]] #line:0633e1\n"
        "[[Harleys_00_00]]\n"           # ← 纯跳转，不是选项
        "Mae: Cold <<if $cold>>\n"      # ← 行内条件
        "Homer: Hi. #tone:sarcastic\n"  # ← 非 #line 的 hashtag
        "\\: not a speaker\n"           # ← 转义冒号，没有说话人
        "<<close>>\n"                   # ← 纯命令
        "<<wait 1>> #line:7bcefc\n"     # ← 命令 + hashtag
        "<wait 3>> #line:42b866\n"      # ← 游戏脚本里的残缺命令
        "===\n"
    )
    _us = ext._extract_yarn("f", {"form": "unitypy", "asset": "D.yarn"}, _full)
    _texts = [u.original for u in _us]
    _kinds = [("group" if u.location.get("yarn_group") else
               "bracket" if u.location.get("yarn_bracket") else
               "option" if u.location.get("yarn_choice") else "line")
              for u in _us]

    rec("Hello there." in _texts, "header 区的自定义键没有被当台词")
    rec(not any("cabin" in t for t in _texts), "自定义 header 的值未被提取")
    rec("Empty Text #line:22a818" in _texts, "裸台词（无说话人）被提取")
    rec("1 - Succotash #line:3751d9" in _texts, "裸台词 + 数字序号被提取")
    rec(not any("grocery_box" in t for t in _texts),
        "整行只剩变量的行不提取（保护后没有真文字）")
    rec(not _has_real_text("$grocery_box") and _has_real_text("Empty Text"),
        "_has_real_text 判定正确")
    rec("Let's go!" in _texts, "选项带说话人前缀：只取正文")
    rec(any(u.location.get("speaker") == "Captain" for u in _us),
        "选项的说话人被识别")
    rec("A line group entry" in _texts, "Yarn 3.x 行组 `=>` 被提取")
    rec("Indented group item" in _texts, "缩进行组 `=>` 被提取且去掉缩进")
    rec("group" in _kinds, "行组被标记 yarn_group")
    rec("Yes" in _texts, "方括号选项被提取")
    rec(not any("Harleys_00_00" in t for t in _texts), "纯跳转 [[Node]] 不提取")
    rec("Cold <<if $cold>>" in _texts, "行内条件随原文保留")
    rec("Hi. #tone:sarcastic" in _texts, "非 #line 的 hashtag 原样保留在原文里")
    rec(any(t == "\\: not a speaker" for t in _texts),
        "转义冒号 `\\:` 不产生说话人（整行都是正文）")
    rec(not any("close" in t for t in _texts), "纯命令行不提取")
    rec(not any(t.startswith("<wait 3") for t in _texts), "残缺命令 `<wait 3>>` 不提取")

    # 4f. 回填不变式：`prefix + text + suffix == 原行`
    # 这是「只替换正文、其余字节不动」这条铁律的可执行定义。
    import io as _io
    _bad = []
    for _line in ("Mae: hi #line:a", "    -> opt <<set $x to 1>> #line:b",
                  "=> grp #line:c", "[[  txt  |Node]] #line:d",
                  "Empty Text #line:e", "\\: escaped", "   indented bare"):
        _st = split_statement(_line)
        if _st is None:
            continue
        if _st.prefix + _st.text + _st.suffix != _line:
            _bad.append((_line, _st.prefix + _st.text + _st.suffix))
    rec(not _bad, f"拆分不变式 prefix+text+suffix==原行（违例 {_bad[:2]}）")

    # 4g. 行组 `=>` 回填
    _gu = _unit({"yarn_group": True}, "A line group entry")
    _gu.translated = "一条行组文本"
    _ng, _okg = _replace_yarn_group_text(
        "=> A line group entry #line:abc123", _gu, _gu.translated)
    rec(_okg and _ng == "=> 一条行组文本 #line:abc123",
        f"行组回填保留 `=>` 与 #line（实际 {_ng!r}）")
    _, _okg2 = _replace_yarn_group_text(
        "-> A line group entry #line:abc123", _gu, _gu.translated)
    rec(not _okg2, "行组回填不认错类型（`->` 行不当作行组）")

    # 4h. 裸台词回填（没有说话人前缀也要能拼回）
    _bu2 = _unit({"yarn": True, "speaker": ""}, "Empty Text #line:22a818")
    _bu2.translated = "空文本 #line:22a818"
    _nb2, _okb4 = _replace_yarn_line_text("Empty Text #line:22a818",
                                          _bu2, _bu2.translated)
    rec(_okb4 and _nb2 == "空文本 #line:22a818",
        f"裸台词回填（实际 {_nb2!r}）")
    _nb3, _okb5 = _replace_yarn_line_text("    1 - Succotash",
                                          _unit({"yarn": True, "speaker": ""},
                                                "1 - Succotash"), "1 - 青豆")
    rec(_okb5 and _nb3 == "    1 - 青豆", f"缩进裸台词回填（实际 {_nb3!r}）")

    # 4i. 选项带说话人前缀的回填：说话人原样保留
    _cu3 = _unit({"yarn_choice": True, "speaker": "Captain"}, "Let's go!")
    _cu3.translated = "走吧！"
    _nc3, _okc3 = _replace_yarn_choice_text(
        "-> Captain: Let's go! <<jump Ship>> #line:zz", _cu3, _cu3.translated)
    rec(_okc3 and _nc3 == "-> Captain: 走吧！ <<jump Ship>> #line:zz",
        f"选项说话人前缀原样保留（实际 {_nc3!r}）")

    # 4j. 转义冒号不被当成说话人
    _sp, _, _bd = split_speaker("\\: hello")
    rec(_sp == "" and _bd == "\\: hello", "split_speaker 不把 `\\:` 当说话人")

    # 4k. 头部区/正文区的区分（同一个 `key: value` 在头部是 header、
    #     在正文才是「说话人: 台词」）
    _st_kinds = [s.kind for s in iter_statements(
        "title: X\nbackground: cabin\n---\nbackground: cabin\n===\n")]
    rec(len(_st_kinds) == 1 and _st_kinds[0] == "line",
        f"头部区的 `key: value` 不算台词、正文区的才算（实际 {_st_kinds}）")


    # ---- 4d. 工具自己产出的目录/日志不该被当成游戏文本 ----
    from gametl.core.scan import is_tool_dir  # noqa: E402
    rec(is_tool_dir("NightInTheWoods_汉化补丁包"), "补丁包目录被识别为工具产物")
    rec(is_tool_dir("_汉化备份_原版"), "原版备份目录被识别为工具产物")
    rec(is_tool_dir("_汉化输出"), "输出目录被识别为工具产物")
    rec(not is_tool_dir("data"), "普通游戏目录不误判")
    rec(not is_tool_dir("Night in the Woods_Data"), "_Data 目录不误判")

    # ---- 5. _apply_textasset 批量替换 ----
    text = ("Mae: A #line:111\n"
            "Mae: B #line:222\n"
            "-> Choice one <<set $x to 1>> #line:333\n"
            "[[Choice two|Target]] #line:444\n")
    u1 = _unit({"yarn": True, "line": 1, "speaker": "Mae"}, "A #line:111")
    u2 = _unit({"yarn": True, "line": 2, "speaker": "Mae"}, "B #line:222")
    u3 = _unit({"yarn_choice": True, "line": 3}, "Choice one")
    u4 = _unit({"yarn_bracket": True, "line": 4}, "Choice two")
    u1.translated = restore("甲" + protect("A #line:111")[0], u1.protected)
    u2.translated = restore("乙" + protect("B #line:222")[0], u2.protected)
    u3.translated = "选项一"
    u4.translated = "选项二"
    new_text, cnt = ext._apply_textasset(text, [u1, u2, u3, u4])
    rec(cnt == 4, f"_apply_textasset 替换 4 条（实际 {cnt}）")
    rec("Mae: 甲" in new_text and "Mae: 乙" in new_text, "_apply_textasset 译文落地")
    rec("-> 选项一 <<set $x to 1>> #line:333" in new_text,
        "_apply_textasset 选项行落地且保留内联命令")
    rec("[[选项二|Target]] #line:444" in new_text,
        "_apply_textasset 方括号选项落地且保留 |目标")

    # ---- 备份目录 / .bak 不该被当成游戏内容扫到 ----
    # 用户「装完汉化补丁 → 又跑一次汉化椅」是最常见的操作顺序。安装器会把
    # 原版备份到 <游戏>/_汉化备份_原版/ 且强制 .bak 后缀。这两者若被提取器
    # 吃进去，轻则词条翻倍、uid 全乱（白翻一遍），重则回填**改写原版备份**，
    # 用户再也还原不回原语言。
    import tempfile

    from gametl.core.scan import find_by_suffix, iter_files

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "_Data").mkdir()
        (td / "_Data" / "sharedassets0.assets").write_bytes(b"x")
        # 安装器建的备份目录 + .bak 后缀
        bak = td / "_汉化备份_原版" / "_Data"
        bak.mkdir(parents=True)
        (bak / "sharedassets0.assets.bak").write_bytes(b"x")
        # 散落的 .bak / .old / .orig
        (td / "save.txt.bak").write_text("old", encoding="utf-8")
        (td / "cfg.json.orig").write_text("{}", encoding="utf-8")

        seen = {p.name for p in iter_files(td)}
        rec(seen == {"sharedassets0.assets"},
            f"有界扫描只看到真正的资源（实际 {sorted(seen)}）")
        rec(find_by_suffix(td, {".assets"}) != [] and
            all("_汉化备份" not in str(p) for p in find_by_suffix(td, {".assets"})),
            "find_by_suffix 不会捞到备份目录里的 .assets")
        rec(not list(find_by_suffix(td, {".bak"})), "按 .bak 后缀也捞不到（已统一跳过）")

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
