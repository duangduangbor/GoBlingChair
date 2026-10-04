# -*- coding: utf-8 -*-
"""把 Q 版像素风格头像转成多尺寸 .ico（GoBlingChair 窗口图标）。"""
from PIL import Image
from pathlib import Path

OUT = Path(r"C:/Users/h/WorkBuddy/2026-10-04-00-02-16/assets")
OUT.mkdir(parents=True, exist_ok=True)

SRC = OUT / "Q版像素风格头像.jpg"
im = Image.open(SRC).convert("RGBA")
W, H = im.size
print("源图:", W, "x", H)

# 正方形头像直接等比缩放为多尺寸 ico
SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
img256 = im.resize((256, 256), Image.LANCZOS)
img256.save(OUT / "GoBlingChair.ico", format="ICO", sizes=SIZES)

# 也存一份 PNG 预览 + 缩小的头像（供界面内嵌显示用）
img256.save(OUT / "GoBlingChair.png")
img256.resize((128, 128), Image.LANCZOS).save(OUT / "GoBlingChair_128.png")

print("已生成:")
for name in ("GoBlingChair.ico", "GoBlingChair.png", "GoBlingChair_128.png"):
    print("  ", name)
