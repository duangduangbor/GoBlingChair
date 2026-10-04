"""测试 RPA 打包/解包闭环。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from gametl.core.rpa import RPAArchive, is_rpa

out = ROOT / "gametl" / "output" / "rpa_test"
out.mkdir(parents=True, exist_ok=True)

files = [
    ("game/script.rpy", "label start:\n    \"テスト\"\n".encode("utf-8")),
    ("game/gui/logo.png", bytes(range(256)) * 8),
]
pack_path = out / "archive.rpa"
RPAArchive.pack(files, pack_path)
print(f"[打包] {pack_path} ({pack_path.stat().st_size} 字节)")
print(f"       RPA校验: {is_rpa(pack_path)}")

arc = RPAArchive(pack_path)
idx = arc.read_index()
print(f"[索引] 版本={arc.version} 密钥={hex(arc.key)} 条目={len(idx)}")
for k in idx:
    print(f"       {k}")

dest = out / "unpacked"
stats = arc.extract_all(dest)
print(f"[解包] {stats}")

ok = True
for name, content in files:
    p = dest / name
    if not p.exists() or p.read_bytes() != content:
        print(f"       [不一致] {name}")
        ok = False
print(f"[校验] 内容比对: {'全部一致 ✓' if ok else '有不一致 ✗'}")
