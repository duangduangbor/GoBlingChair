"""测试 XP3 打包/解包闭环。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gametl.core.xp3 import XP3Archive, is_xp3

out = ROOT / "gametl" / "output" / "xp3_test"
out.mkdir(parents=True, exist_ok=True)

# 1. 打包
files = [
    ("script.ks", "テスト台本です。".encode("utf-16-le")),
    ("sub/data.txt", b"hello xp3 world"),
    ("image/bg.bin", bytes(range(256)) * 4),
]
pack_path = out / "test.xp3"
XP3Archive.pack(files, pack_path)
print(f"[打包] {pack_path} ({pack_path.stat().st_size} 字节)")
print(f"       魔数校验: {is_xp3(pack_path)}")

# 2. 解包
arc = XP3Archive(pack_path)
entries = arc.read_index()
print(f"[索引] 解析出 {len(entries)} 个条目:")
for e in entries:
    print(f"       {e.name} ({e.size} 字节, protected={e.protected})")

dest = out / "unpacked"
stats = arc.extract_all(dest)
print(f"[解包] {stats}")

# 3. 校验内容一致
ok = True
for name, content in files:
    got = (dest / name).read_bytes()
    if got != content:
        print(f"       [不一致] {name}")
        ok = False
print(f"[校验] 内容比对: {'全部一致 ✓' if ok else '有不一致 ✗'}")
