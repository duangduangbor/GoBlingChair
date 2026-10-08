# -*- coding: utf-8 -*-
"""v2.6.3 回归：dfpf 第三条硬约束（usize 预算）+ 译文清理防线。

背景（2026-10-07/08 两次 CQ2 事故）
-----------------------------------
1. **usize 预算**：dfpf 资源「解压后的字节数」一旦超过原版，Buddha 引擎启动
   即死循环（CPU 空转、窗口关不掉）。实测边界精确落在原版 usize 上：
   ``stringtable/costumequest_usenglish`` 原版 388657 —— 388649 能进、
   388666 卡死。中译必然撞线（英文 1 字符 ≈ 1 字节，中文 UTF-8 每字
   3 字节：字数 −23%、字节 +1.3%），所以回填必须自己压。
   对策两道闸：① 三阶段全角标点 → 半角；② 仍超就**逐条淘汰**（丢掉净增
   字节最大的几条，其余照写），一条都塞不下才整张表保原版。

2. **换行丢失**：``_clean`` 早期把 ``\\n`` 压成空格，CQ2 里 18 条多行文本
   （服装说明）整段挤成一行。

3. **提示词回声**：小模型偶尔把自己的指令也写进 JSON 值，CQ2 实测 16 条。

跑法::

    python gametl/examples/test_v263_dfpf_budget.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from gametl.core.dfpf import DfpfPack, build_synthetic_v5  # noqa: E402
from gametl.core.models import EngineType, Project  # noqa: E402
from gametl.extractors.buddha import (  # noqa: E402
    BuddhaExtractor, _PUN_STAGES, _apply_pun_stage, emit_value, escape,
    utf8_len,
)
from gametl.translators.ollama_backend import (  # noqa: E402
    OllamaTranslator, looks_leaked,
)

FAILS: list[str] = []


def check(cond, msg):
    print(("  [OK] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


# ---------------------------------------------------------------- 合成素材

#: T1 特意很长（200 字节），这样"译文比原文长多少"可以精细扫描；
#: T3 留着多行值（引号内 ``\r\n`` 转义），用来验证换行不会丢。
_LONG = "A" * 200
ST_TEXT = (
    'StringTable{LineCodeData={'
    'T1=LineCodeData{Text="' + _LONG + '";VolumeDB=0;Character=Text;SoundCue=;};'
    'T2=LineCodeData{Text="Press /BUTTON_DPadUp/ to jump";'
    'VolumeDB=0;Character=Text;SoundCue=;};'
    'T3=LineCodeData{Text="Focus: Attack\\r\\nType: Melee";'
    'VolumeDB=0;Character=Text;SoundCue=;};'
    '};}'
)


def _fresh(tmp: Path) -> int:
    """造一个干净合成包，返回该表的「解压后大小」（= 预算）。"""
    build_synthetic_v5(tmp / "Test_Stuff.~h", tmp / "Test_Stuff.~p", [
        ("stringtable/test_enus", ST_TEXT.encode("utf-8"), 0, True),
    ])
    pk = DfpfPack.open(tmp / "Test_Stuff.~h")
    return pk.find("stringtable/test_enus").usize


def _run_one(tmp: Path, translated: str) -> tuple:
    """只翻 T1，返回 (stats, 输出包的解压后大小 or None)。"""
    ex = BuddhaExtractor(tmp)
    units = ex.extract()
    for u in units:
        if u.location["key"] == "T1":
            u.translated = translated
    proj = Project(root=tmp, engine=EngineType.BUDDHA, units=units)
    out = tmp / "out"
    stats = ex.write_back(proj, out)
    h = out / "Test_Stuff.~h"
    if not h.exists():
        return stats, None
    pk = DfpfPack.open(h)
    return stats, len(pk.read(pk.find("stringtable/test_enus")))


print("=" * 64)
print("1. 全角 → 半角：分阶段、按字节生效")
print("=" * 64)

check(len(_PUN_STAGES) == 3, f"标点压缩分 3 阶段（实际 {len(_PUN_STAGES)}）")
check("\u3002" not in _PUN_STAGES[0] and "\uff0c" not in _PUN_STAGES[0],
      "阶段 1 不碰逗号句号（最伤排版，留到最后）")
check("\uff01" in _PUN_STAGES[1] and "\uff1f" in _PUN_STAGES[1],
      "阶段 2 = 叹号 / 问号")
check("\uff0c" in _PUN_STAGES[2] and "\u3002" in _PUN_STAGES[2],
      "阶段 3 = 逗号 / 句号")

_s = '"你好，世界。"'
check(_apply_pun_stage(_s, _PUN_STAGES[0]) == '"你好，世界。"',
      "阶段 1 对逗号句号无影响")
check(_apply_pun_stage(_s, _PUN_STAGES[2]) == '"你好,世界."',
      "阶段 3 把全角逗号句号换成半角")
check(utf8_len("，") == 3 and utf8_len(",") == 1, "全角标点 3 字节 / 半角 1 字节")
check(utf8_len(_apply_pun_stage(_s, _PUN_STAGES[2])) < utf8_len(_s),
      "换半角后 UTF-8 字节数下降（这就是预算的来源）")

print()
print("=" * 64)
print("2. 换行必须原样保留（v2.6.3 换行丢失事故）")
print("=" * 64)

check(escape("a\r\nb") == "a\\r\\nb", "escape：\\r\\n 转成字面转义")
check(emit_value("Focus: Attack\r\nType: Melee", True)
      == '"Focus: Attack\\r\\nType: Melee"',
      "多行值加引号 + 转义（和原版写法一致）")

c = OllamaTranslator._clean("  专注：攻击\r\n类型：近战  ",
                            "Focus: Attack\r\nType: Melee")
check("\n" in c, f"_clean 保留换行（实际 {c!r}）")
check(c.count("\n") == 1, "换行数量正确（CRLF 归一为 LF，不多不少）")
check(c.startswith("专注") and c.endswith("近战"), "_clean 仍然去掉首尾空白")

print()
print("=" * 64)
print("3. 提示词回声防线（v2.6.3 泄漏事故）")
print("=" * 64)

LEAKS = [
    ("获得了南瓜服装。}``` 提示：仅输出 JSON 对象，键与输入完全一致，"
     "值为翻译结果。不要输出任何解释。``` 输出：{", "Acquired the Pumpkin Costume."),
    ("喵。”} 好的，我将按照您的要求翻译这些 JSON 值。不需要任何解释。", "Meow."),
    ("喜欢石头……”} 好的，我将按照您的要求翻译这些 JSON 值。", "Likes rocks..."),
    ("糖果玉米不想承担责任。}``` 提示：仅输出 JSON 对象。``` 输出：{",
     "Candy Corn doesn't want the responsibility."),
]
for bad, orig in LEAKS:
    check(bool(looks_leaked(bad, orig)), f"识别泄漏：{bad[:20]}…")

CLEAN = [
    ("获得了南瓜服装。", "Acquired the Pumpkin Costume."),
    ("喵。", "Meow."),
    ("糖果玉米不想承担责任。", "Candy Corn doesn't want the responsibility."),
    ("专注：攻击\r\n类型：近战", "Focus: Attack\r\nType: Melee"),
    ("按【0】使用基本攻击！", "Press the /BUTTON_X/ button to use your Basic Attack!"),
    ("", "Meow."),
    ("关于这个任务，我需要你帮助我完成它。", "About this quest, I need your help."),
]
for good, orig in CLEAN:
    check(not looks_leaked(good, orig), f"不误伤：{good[:20]!r}")

print()
print("=" * 64)
print("4. 预算不变量：落盘的 usize 永远 ≤ 原版")
print("=" * 64)

viol: list = []
written_ok: list = []
gave_up: list = []
for k in range(1, 80):                      # 3k 越过 200 字节后必超预算
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        u0 = _fresh(tmp)
        stats, new = _run_one(tmp, "中" * k)
        over = [s for s in stats.get("over_budget", []) if "test_enus" in s]
        if over:
            gave_up.append(k)
            if new is not None:
                viol.append(f"k={k} 已判定超预算却仍产出了文件")
        else:
            written_ok.append(k)
            if new is None:
                viol.append(f"k={k} 未判超预算却没有产出文件")
            elif new > u0:
                viol.append(f"k={k} 落盘 usize={new} 超过原版 {u0}")

check(not viol, f"80 档长度扫描无违例（违例 {viol[:3]}）")
check(bool(written_ok), f"有一批能成功写入（{len(written_ok)} 档，最大 k={max(written_ok)}）")
check(bool(gave_up), f"有一批超预算被放弃（{len(gave_up)} 档，最小 k={min(gave_up)}）")
check(max(written_ok) < min(gave_up), "分界清晰：能写的全部短于被放弃的")

print()
print("=" * 64)
print("4b. 逐条淘汰：只有装不下的那几条保留英文，其余照写（v2.6.4）")
print("=" * 64)

_L1, _L2, _L3 = "A" * 200, "B" * 200, "C" * 200
ST3 = (
    'StringTable{LineCodeData={'
    'T1=LineCodeData{Text="' + _L1 + '";VolumeDB=0;Character=Text;SoundCue=;};'
    'T2=LineCodeData{Text="' + _L2 + '";VolumeDB=0;Character=Text;SoundCue=;};'
    'T3=LineCodeData{Text="' + _L3 + '";VolumeDB=0;Character=Text;SoundCue=;};'
    '};}'
)

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    build_synthetic_v5(tmp / "Test_Stuff.~h", tmp / "Test_Stuff.~p", [
        ("stringtable/test_enus", ST3.encode("utf-8"), 0, True),
    ])
    u0 = DfpfPack.open(tmp / "Test_Stuff.~h").find("stringtable/test_enus").usize
    ex3 = BuddhaExtractor(tmp)
    units3 = ex3.extract()
    # 三条的"净增字节"刻意拉开：T1 最胀(+130)、T2 最省(−140)、T3 居中(+100)。
    # 合计 +90 > 预算 → 只需丢掉 T1 一条即可落地。
    plan = {"T1": 110, "T2": 20, "T3": 100}
    for u in units3:
        u.translated = "中" * plan[u.location["key"]]
    proj3 = Project(root=tmp, engine=EngineType.BUDDHA, units=units3)
    out3 = tmp / "out"
    st3 = ex3.write_back(proj3, out3)

    h3 = out3 / "Test_Stuff.~h"
    check(h3.exists(), "整体仍产出（不再整张表放弃）")
    check(not st3.get("over_budget"), "没有整表放弃")
    check(bool(st3.get("trimmed")), f"记进 trimmed（{st3.get('trimmed')}）")

    pk3 = DfpfPack.open(h3)
    body = pk3.read(pk3.find("stringtable/test_enus")).decode("utf-8")
    check(utf8_len(body) <= u0, f"落盘 {utf8_len(body)} 字节 ≤ 原版 {u0}")
    check(_L1 in body, "胀得最凶的 T1 保留英文（被淘汰）")
    check("中" * 20 in body, "省字节的 T2 保留中文")
    check("中" * 100 in body, "居中且装得下的 T3 保留中文")
    check(st3.get("fields_replaced") == 2,
          f"只替换 2 处（实际 {st3.get('fields_replaced')}）")

print()
print("=" * 64)
print("5. 端到端：超预算时全角标点被自动压成半角后才落地")
print("=" * 64)

half_converted: list = []
fit_kept_fullwidth: list = []
for k in range(25, 70):
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        u0 = _fresh(tmp)
        # 30 个全角逗号 → 最多能省 60 字节，制造出"全角超、半角够"的窄带
        stats, new = _run_one(tmp, "中" * k + "，" * 30)
        if new is None:
            continue
        h = tmp / "out" / "Test_Stuff.~h"
        txt = DfpfPack.open(h).read(
            DfpfPack.open(h).find("stringtable/test_enus")).decode("utf-8")
        if len(txt.encode("utf-8")) > u0:
            viol.append(f"k={k} 落盘超过预算")
        if "，" in txt:
            fit_kept_fullwidth.append(k)
        else:
            half_converted.append(k)

check(not viol, f"窄带扫描无预算违例（{viol[:3]}）")
check(bool(half_converted),
      f"存在『压成半角才塞得下』的档位（k={half_converted[:6]}…）")
check(bool(fit_kept_fullwidth),
      f"预算够的档位保留全角标点（k={fit_kept_fullwidth[:3]}…）")
check(not half_converted or not fit_kept_fullwidth
      or max(fit_kept_fullwidth) < min(half_converted),
      "预算越紧越可能压半角（单调）")

print()
print("=" * 64)
print("6. Double Fine 系部署：自动打开窗口化（黑屏事故的根因修复）")
print("=" * 64)

from gametl.core.patcher import _ensure_windowed, _restore_windowed  # noqa: E402

CFG_OFF = ("-- release settings\nUsePackfiles=true\n"
           "forceWindowedMode = false\nPauseOnLostFocus = true\n")


def _mk_cfg(tmp: Path, text: str) -> Path:
    d = tmp / "Data" / "Config"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "User.cfg"
    f.write_text(text, encoding="utf-8")
    return f


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    f = _mk_cfg(tmp, CFG_OFF)
    changed = _ensure_windowed(tmp, EngineType.BUDDHA)
    body = f.read_text(encoding="utf-8")
    check(changed, "原为 false → 判定需要修改")
    check("forceWindowedMode = true" in body, "已改成 true")
    check("UsePackfiles=true" in body and "PauseOnLostFocus = true" in body,
          "其它配置一个字没动")
    check((f.with_name("User.cfg.mt_orig")).is_file(), "改前另存了 .mt_orig 备份")

    check(not _ensure_windowed(tmp, EngineType.BUDDHA), "已经是 true → 幂等，不再改")
    _restore_windowed(tmp)
    check("forceWindowedMode = false" in f.read_text(encoding="utf-8"),
          "还原汉化时换回原设置")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    f = _mk_cfg(tmp, "forceWindowedMode = true\n")
    check(not _ensure_windowed(tmp, EngineType.BUDDHA), "本来就是 true → 不碰")
    check(not (f.with_name("User.cfg.mt_orig")).exists(), "没改动就不留备份")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    f = _mk_cfg(tmp, "-- 没有这一项的旧配置\nUsePackfiles=true\n")
    check(not _ensure_windowed(tmp, EngineType.BUDDHA), "配置里没这一项 → 不擅自新增")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)                        # 没有 User.cfg
    check(not _ensure_windowed(tmp, EngineType.BUDDHA), "没有 User.cfg → 静默跳过不报错")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    _mk_cfg(tmp, CFG_OFF)
    check(not _ensure_windowed(tmp, EngineType.RPGMAKER_MV),
          "非 Double Fine 系 → 不碰它的配置")

print()
print("=" * 64)
if FAILS:
    print(f"FAILED {len(FAILS)} 项：")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("ALL PASS — v2.6.3 dfpf 预算 + 清理防线")
sys.exit(0)
