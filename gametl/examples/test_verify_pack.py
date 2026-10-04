"""终极验证：解包自动生成的汉化 xp3，确认中文正确写入且标签完好。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gametl.core.xp3 import XP3Archive

pack = ROOT / "gametl" / "output" / "auto_test_out" / "汉化补丁" / "data.xp3"
print(f"汉化包: {pack}")
print(f"大小: {pack.stat().st_size} 字节")
print()

arc = XP3Archive(pack)
entries = arc.read_index()
print(f"条目: {[e.name for e in entries]}")
print()

dest = ROOT / "gametl" / "output" / "auto_test_out" / "_verify"
arc.extract_all(dest)

ks = dest / "script.ks"
print("=" * 56)
print("汉化后 script.ks 内容")
print("=" * 56)
text = ks.read_bytes().decode("utf-16-le")
for line in text.splitlines():
    if line.strip():
        print(f"  {line}")

print()
print("=" * 56)
print("关键检查")
print("=" * 56)
checks = {
    "含中文译文": "姐姐" in text or "市场" in text,
    "KAG标签保留": "[bg storage=" in text and "[r]" in text,
    "endlink保留": "[endlink]" in text,
    "脚本标签保留": "*start" in text and "*market" in text,
    "行内变量保留": "[pcname]" in text,
    "无日文残留（主体）": "おはよう" not in text,
}
for name, ok in checks.items():
    print(f"  {'✓' if ok else '✗'} {name}")
