"""把示例 KiriKiri 脚本转为 UTF-16LE 编码的 .ks 文件，模拟真实游戏格式。"""
import sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])

text = src.read_text(encoding="utf-8")
dst.parent.mkdir(parents=True, exist_ok=True)
# KiriKiri 常见编码：UTF-16LE 带 BOM
dst.write_bytes(text.encode("utf-16-le"))
print(f"已写入: {dst} ({dst.stat().st_size} 字节)")
