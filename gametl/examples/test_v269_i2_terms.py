#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v2.6.9：Unity 分组子目录扫描 + I2 Localization 语言表。

覆盖两处「静默漏翻」：

1. ``iter_files`` 的默认深度 4 会把 Addressables 的**分组子目录**
   （``…/StandaloneWindows64/chapter1/*.bundle``，正好第 5 层）整棵剪掉。
   IDJ 的全部对话/字幕都在 chapter1/2/3 里 → 一条都提不到，界面上还没报错。
2. I2 的**语言表**（``mSource.mTerms[i].Languages[j]``）字段路径最后一段是
   语言下标数字，靠「文本语义词」白名单永远认不出来。

需要真机样本才跑得动第 E 段；样本不在就明说并跳过（不影响退出码）：
    GAMETL_I2_SAMPLE=<某个 chapter 包或任何含 I2 语言表的 .bundle>
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from gametl.core.protect import protect, restore          # noqa: E402
from gametl.extractors.unity import (                      # noqa: E402
    PACK_SCAN_MAX_DEPTH,
    _find_i2_tables,
    _i2_real_locales,
    _i2_source_locale,
    _i2_text_ok,
)

#: 真机样本：优先环境变量，其次 IDJ 的 chapter1 包。
SAMPLE = Path(os.environ.get(
    "GAMETL_I2_SAMPLE",
    r"E:\游戏\IDJ\IDJ_Data\StreamingAssets\aa\StandaloneWindows64\chapter1"
    r"\5dd6287e2deff0d82dc60db5abfb667c.bundle"))
#: 同一款游戏里另一句台词的所在包（用来确认「不止一个包被扫到」）。
SAMPLE2 = Path(os.environ.get(
    "GAMETL_I2_SAMPLE2",
    r"E:\游戏\IDJ\IDJ_Data\StreamingAssets\aa\StandaloneWindows64\chapter1"
    r"\c54149b748fb05d365e4b8e94b121dca.bundle"))

_ok = 0
_bad = 0
_FAILS: list = []


def ck(cond, label):
    """和 `_runall_lite.ps1` 的约定一致：逐条 [OK] / [FAIL]。"""
    global _ok, _bad
    if cond:
        _ok += 1
        print("[OK] %s" % label)
    else:
        _bad += 1
        _FAILS.append(label)
        print("[FAIL] %s" % label)


# ---------------------------------------------------------------- A 语言表识别
def test_find_tables():
    print("\n[A] I2 语言表识别")
    tt = {
        "m_Name": "",
        # ⚠️ 顶层这份是**空占位**（真实 IDJ 就是这样）：只看键名会永远拿到空表
        "mTerms": [],
        "mLanguages": [],
        "mSource": {
            "mLanguages": [
                {"Name": "English (United States)", "Code": "en-US"},
                {"Name": "Portuguese (Brazil)", "Code": "pt-BR"},
                {"Name": "Spanish (LatAm)", "Code": "es-US"},
                {"Name": "IdString", "Code": ""},
                {"Name": "Type", "Code": ""},
            ],
            "mTerms": [
                {"Languages": ["Jorel's Brother", "Irmão do Jorel",
                               "Hermano de Jorel", "IDJ_NAME", "TEXT"]},
                {"Languages": ["PLAY", "JOGAR", "JUGAR", "IDJ_PLAY", "BUTTON"]},
            ],
        },
    }
    tables = _find_i2_tables(tt)
    ck(len(tables) == 1, "只找到 1 张表（顶层空占位被排除）")
    ck(len(tables[0][1]["mTerms"]) == 2, "拿到的是非空表")
    ck(tables[0][0] == ("mSource",), "表路径 = ('mSource',)")

    # 表挂在顶层的情况（有些游戏没有 mSource 这一层）
    flat = {"mLanguages": [{"Name": "English", "Code": "en-US"}],
            "mTerms": [{"Languages": ["Hi", "Oi"]}]}
    t2 = _find_i2_tables(flat)
    ck(len(t2) == 1 and t2[0][0] == (), "表挂顶层时路径为空元组")


# ------------------------------------------------------------ B 语言槽的选择
def test_locale_pick():
    print("\n[B] 原文槽选择")
    langs = [{"Name": "English (United States)", "Code": "en-US"},
             {"Name": "Portuguese (Brazil)", "Code": "pt-BR"},
             {"Name": "Spanish (LatAm)", "Code": "es-US"},
             {"Name": "IdString", "Code": ""},
             {"Name": "Type", "Code": ""}]
    ck(_i2_real_locales(langs) == [(0, "en-US"), (1, "pt-BR"), (2, "es-US")],
       "元数据列（Code 为空）不算语言")
    ck(_i2_source_locale(langs) == 0, "有 en-US → 选 0")
    ck(_i2_source_locale([{"Name": "Portuguese", "Code": "pt-BR"}]) == 0,
       "没有英文 → 退回第一个真实语言")
    ck(_i2_source_locale([{"Name": "IdString", "Code": ""}]) is None,
       "全是元数据列 → None（宁可漏也不误翻）")
    ck(_i2_source_locale([]) is None, "空表 → None")


# ------------------------------------------------------------ C 正文值过滤
def test_value_filter():
    print("\n[C] 正文值过滤")
    ck(_i2_text_ok("Especially when the owners get mad."), "普通句子通过")
    ck(not _i2_text_ok("   "), "空白拒绝")
    ck(not _i2_text_ok("1 / 2"), "无字母拒绝")
    ck(not _i2_text_ok("http://x.com"), "URL 拒绝")
    ck(_i2_text_ok("A/B 选项"), "带斜杠保留（正文里斜杠是正常标点）")
    ck(_i2_text_ok("x" * 900) and not _i2_text_ok("x" * 1500),
       "长度上限 1000（对话整段不该被 400 卡掉）")


# -------------------------------------------------------- D 富文本标签保护
def test_protect_tags():
    print("\n[D] I2 富文本标签保护")
    cases = [
        "<e=ChangeReaction,Jorel,Conversando,Risadinha> Hehehe.",
        "A misterious videogame appeared <j>OUT OF THE BLUE</j> in every home.",
        "<c=#00c13c>Granny Juju</c>",
        "<w=sassy>never</w>",
        "<size=36>Chapter 2</size>",
        "<s=0.7>slow</s>",
    ]
    for s in cases:
        p, frags = protect(s)
        ck("【" in p, "被保护 → %s" % p[:46])
        ck(restore(p, frags) == s, "还原逐字符一致 → %s" % s[:40])


# ----------------------------------------------- E 真机：深度 + 提取 + 回填
def test_real_bundle():
    print("\n[E] 真机样本：分组子目录 + I2 提取 + 回填往返")
    if not SAMPLE.is_file():
        print("  SKIP  样本不在：%s" % SAMPLE)
        print("        （设 GAMETL_I2_SAMPLE 指向任意含 I2 语言表的 .bundle）")
        return
    tmp = Path(tempfile.mkdtemp(prefix="t269_"))
    try:
        base = (tmp / "IDJ_Data" / "StreamingAssets" / "aa"
                / "StandaloneWindows64")
        # 真实深度：IDJ_Data(1)/StandaloneAssets(2…)/aa(3)/StandaloneWindows64(4)
        # /chapter1(5) —— **默认深度 4 会把 chapter1 整棵剪掉**
        (base / "chapter1").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(SAMPLE), str(base / "chapter1" / SAMPLE.name))
        if SAMPLE2.is_file():
            shutil.copyfile(str(SAMPLE2), str(base / "chapter1" / SAMPLE2.name))
        # 工具产物目录必须被跳过（否则会把备份当游戏内容再翻一遍）
        (tmp / "IDJ_Data" / "chapter1" / "_汉化备份_原版").mkdir(
            parents=True, exist_ok=True)

        ck(PACK_SCAN_MAX_DEPTH >= 6,
           "PACK_SCAN_MAX_DEPTH=%d 能覆盖第 5 层的分组目录" % PACK_SCAN_MAX_DEPTH)

        from gametl.extractors.unity import UnityExtractor
        ex = UnityExtractor(tmp)
        units = ex.extract()
        print("  提取条数: %d" % len(units))
        ck(len(units) > 500, "提到内容（>500 条）")

        i2 = [u for u in units if (u.location or {}).get("i2_term")]
        ck(len(i2) > 500, "I2 词条 %d 条" % len(i2))
        ck(bool(i2), "回填路径结构 → %s"
           % (i2[0].location["mb_field_path"] if i2 else None))
        ck(all(u.location["mb_field_path"][-4:-2] == ["mTerms", u.location["mb_field_path"][-3]]
               for u in i2), "路径尾部是 mTerms/<i>/Languages/<j>")
        ck(all(str(u.location["mb_field_path"][-1]) == "0" for u in i2),
           "只提原文槽（下标 0）")

        texts = [u.original for u in units]
        ck(any("Especially when the owners get mad" in t for t in texts),
           "跨子目录的台词已提到（Especially when the owners get mad.）")
        if SAMPLE2.is_file():
            ck(any("eeevery home in town" in t for t in texts),
               "第二个包也被扫到（eeevery home in town）")

        # ---- 回填往返：改 3 条，重开文件核对 ----
        probes = [u for u in i2 if len(u.original) > 12][:3]
        ck(len(probes) == 3, "取到 3 条做回填探针")
        for u in probes:
            u.translated = "\u2605中\u6587\u2605" + u.original[:6]
        src = base / "chapter1" / SAMPLE.name
        dst = src.with_suffix(".patched")
        # 与 write_back 一致：只把**有译文**的单元交给回填
        filled = [u for u in units if u.translated]
        ck(len(filled) == 3, "只有 3 条带译文（与 write_back 的过滤一致）")
        n, differs = ex._write_binary_assets(src, dst, filled)
        ck(n >= 3 and differs, "回填替换 %d 条、产物与原件不同" % n)

        import UnityPy
        want = {tuple(u.location["mb_field_path"]): u.translated for u in probes}
        hit = 0
        for obj in UnityPy.load(str(dst)).objects:
            if obj.type.name != "MonoBehaviour":
                continue
            try:
                tt = obj.read_typetree()
            except Exception:
                continue
            for path, val in want.items():
                node = tt
                for step in path:
                    node = (node.get(step) if isinstance(node, dict)
                            else (node[int(step)]
                                  if isinstance(node, list) and int(step) < len(node)
                                  else None))
                    if node is None:
                        break
                if node == val:
                    hit += 1
        ck(hit == len(want), "回填 3 条全部在产物里读到（实得 %d）" % hit)

        # pt-BR 槽（下标 1）绝不能被写
        touched_other = [u for u in i2 if str(u.location["mb_field_path"][-1]) != "0"]
        ck(not touched_other, "pt-BR / es-US 槽一条都没碰")
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


def main():
    print("=" * 62)
    print("test_v269_i2_terms — Unity 分组子目录 + I2 语言表")
    print("=" * 62)
    test_find_tables()
    test_locale_pick()
    test_value_filter()
    test_protect_tags()
    test_real_bundle()
    print("")
    if _bad:
        print("[FAIL] %d 项未通过: %s" % (_bad, _FAILS))
    else:
        print("[OK] 全部通过（%d 项）" % _ok)
    return 1 if _bad else 0


if __name__ == "__main__":
    sys.exit(main())
