# -*- coding: utf-8 -*-
"""四档性能模式 + 硬件自动检测。

设计目标：让不懂硬件的用户也能拿到接近自己机器上限的速度，
同时保留手动覆盖的余地。

核心约束（Ollama 官方口径）：

    KV 缓存显存 ≈ num_ctx × num_parallel × 每 token KV 大小

所以「并发」和「上下文」是互相挤占显存的，**必须成套调整**，
不能单独把并发拉高 —— 盲目拉高会把模型挤到 CPU 上，反而更慢。

四档定位：

    COMPAT  无独显 / 老机器           —— 只求能跑，不挑机器
    NORMAL  4-6GB 显存                —— 稳，兼容性优先
    TURBO   6-10GB 显存               —— 甜点位，质量与速度兼顾
    INSANE  12GB+ / 愿意风扇狂转      —— 极限吞吐

实测（RTX 3060 Ti 8GB · 短文本语料 · 120 条）：

    通用 7B · 并发1（旧版行为）   8.5 条/秒   ← 用户抱怨的「几秒 10 条」
    通用 7B · 并发4（强效档）    12.4 条/秒   1.5x
    专用 1.8B · 并发1             6.6 条/秒   ← 单并发被请求开销拖住
    专用 1.8B · 并发4            23.0 条/秒   2.7x
    专用 1.8B · 并发6（狂暴档）  24.2 条/秒   2.9x

结论：
  1. **并发**是主要杠杆，且小模型受益远大于大模型
     （小模型算得飞快，瓶颈是请求往返开销，并行能把它藏掉）。
  2. **换模型**比调档位更见效 —— 1.8B 翻译专用模型是 7B 通用模型的近 2 倍。
  3. 但 1.8B 质量有短板（曾把「けん」译成「勇气」而不是「剑」），
     所以模型选择必须**交给用户**，不能藏在档位里偷偷换。

因此档位（本文件）只管**资源参数**；选哪个模型由用户在界面上单独决定
（见 pick_model 的 mode 参数）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Profile(str, Enum):
    COMPAT = "compat"      # 兼容
    NORMAL = "normal"      # 普通
    TURBO = "turbo"        # 强效
    INSANE = "insane"      # 狂暴


@dataclass(frozen=True)
class ProfileSpec:
    """一档模式的完整参数集合。

    Attributes:
        num_parallel:     Ollama 服务端并行槽位（环境变量 OLLAMA_NUM_PARALLEL）。
                          每个槽位持有独立 KV 缓存，是显存的主要消费者之一。
        num_ctx:          每个槽位的上下文窗口（OLLAMA_CONTEXT_LENGTH）。
                          批量翻译时一个请求塞多条文本，需要相应加大。
        kv_cache_type:    KV 缓存精度，f16 / q8_0 / q4_0。q8_0 约省一半显存。
        num_batch:        单次前向的批大小（OLLAMA_NUM_BATCH），影响 prefill 速度。
        concurrency:      客户端并发线程数。应 ≈ num_parallel，多了只会排队。
        batch_size:       单个请求里一次翻译几条文本。靠共享固定前缀来提速。
        prefer_small_model: 是否优先挑选 models/ 里的「小模型」（1.8B 级别）。
        vram_hint:        界面展示用的显存占用估算。
        speed_hint:       界面展示用的相对速度（以现状为 1x）。
    """
    key: Profile
    name: str
    emoji: str
    tagline: str
    num_parallel: int
    num_ctx: int
    kv_cache_type: str
    num_batch: int
    concurrency: int
    batch_size: int
    prefer_small_model: bool
    vram_hint: str
    speed_hint: str
    detail: str


PROFILES: dict[Profile, ProfileSpec] = {
    Profile.COMPAT: ProfileSpec(
        key=Profile.COMPAT,
        name="兼容模式",
        emoji="🐢",
        tagline="老机器 / 无独显也能跑",
        num_parallel=1,
        num_ctx=2048,
        kv_cache_type="f16",
        num_batch=256,
        concurrency=1,
        batch_size=4,
        prefer_small_model=True,
        vram_hint="约 1.5 GB（可走内存）",
        speed_hint="约 1x",
        detail="不挑机器，核显或纯 CPU 也能跑完。牺牲速度换兼容性。",
    ),
    Profile.NORMAL: ProfileSpec(
        key=Profile.NORMAL,
        name="普通模式",
        emoji="🟢",
        tagline="办公本 / 入门独显",
        num_parallel=2,
        num_ctx=2048,
        kv_cache_type="f16",
        num_batch=512,
        concurrency=2,
        batch_size=6,
        prefer_small_model=False,
        vram_hint="约 3 GB",
        speed_hint="约 1.3x",
        detail="稳定优先，几乎不会让机器变卡。日常办公机可用。",
    ),
    Profile.TURBO: ProfileSpec(
        key=Profile.TURBO,
        name="强效模式",
        emoji="🔵",
        tagline="主推 · 质量与速度平衡",
        num_parallel=4,
        num_ctx=4096,
        kv_cache_type="q8_0",
        num_batch=1024,
        concurrency=4,
        batch_size=8,
        prefer_small_model=False,
        vram_hint="约 6.5 GB",
        speed_hint="约 1.5x",
        detail="6-10GB 显存的甜点位。翻译质量不变，速度提升。",
    ),
    Profile.INSANE: ProfileSpec(
        key=Profile.INSANE,
        name="狂暴模式",
        emoji="🔴",
        tagline="吃满显卡 · 风扇会起飞",
        num_parallel=6,
        num_ctx=2048,
        kv_cache_type="q8_0",
        num_batch=1024,
        concurrency=6,
        batch_size=12,
        prefer_small_model=True,
        vram_hint="约 7.5 GB 起",
        speed_hint="约 2.9x",
        detail="把显存吃满、并发拉满。笔记本会烫、风扇狂转、"
               "其他程序可能卡顿 — 但翻译速度最快。",
    ),
}


PROFILE_ORDER: list[Profile] = [
    Profile.COMPAT, Profile.NORMAL, Profile.TURBO, Profile.INSANE,
]


# ---------------------------------------------------------------- 硬件检测

@dataclass
class HardwareInfo:
    gpus: list[dict] = field(default_factory=list)
    ram_total_mb: int = 0
    ram_avail_mb: int = 0
    error: str = ""

    @property
    def vram_total_mb(self) -> int:
        return max((g.get("vram_mb", 0) for g in self.gpus), default=0)

    @property
    def gpu_name(self) -> str:
        return self.gpus[0].get("name", "") if self.gpus else ""

    @property
    def has_cuda(self) -> bool:
        return bool(self.gpus) and self.vram_total_mb >= 2000

    def summary(self) -> str:
        if self.gpus:
            g = self.gpus[0]
            return (f"{g.get('name', '未知显卡')} · "
                    f"显存 {g.get('vram_mb', 0) / 1024:.1f} GB "
                    f"(可用 {g.get('vram_free_mb', 0) / 1024:.1f} GB)")
        return "未检测到 NVIDIA 独显（将使用 CPU 推理）"


def _nvidia_smi() -> list[dict]:
    """通过 nvidia-smi 查询显卡。失败时返回空列表（不抛异常）。"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        for cand in (r"C:\Windows\System32\nvidia-smi.exe",
                     r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
            if Path(cand).exists():
                exe = cand
                break
    if not exe:
        return []

    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        res = subprocess.run(
            [exe,
             "--query-gpu=name,memory.total,memory.free,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
            creationflags=flags,
        )
    except Exception:  # noqa: BLE001
        return []

    out: list[dict] = []
    for line in (res.stdout or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            out.append({
                "name": parts[0],
                "vram_mb": int(float(parts[1])),
                "vram_free_mb": int(float(parts[2])),
                "driver": parts[3],
            })
        except ValueError:
            continue
    return out


def _ram_info() -> tuple[int, int]:
    """返回 (总内存 MB, 可用内存 MB)。失败返回 (0, 0)。"""
    if sys.platform == "win32":
        try:
            import ctypes

            class _MSX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            st = _MSX()
            st.dwLength = ctypes.sizeof(_MSX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            mb = 1024 * 1024
            return int(st.ullTotalPhys / mb), int(st.ullAvailPhys / mb)
        except Exception:  # noqa: BLE001
            return 0, 0
    try:
        page = os.sysconf("SC_PAGE_SIZE")
        total = os.sysconf("SC_PHYS_PAGES")
        avail = os.sysconf("SC_AVPHYS_PAGES")
        return int(page * total / 1024 / 1024), int(page * avail / 1024 / 1024)
    except Exception:  # noqa: BLE001
        return 0, 0


def detect_hardware() -> HardwareInfo:
    """探测本机硬件。任何一步失败都不应影响整体。"""
    hw = HardwareInfo()
    try:
        hw.gpus = _nvidia_smi()
    except Exception as e:  # noqa: BLE001
        hw.error = f"显卡探测失败：{e}"
    try:
        hw.ram_total_mb, hw.ram_avail_mb = _ram_info()
    except Exception:  # noqa: BLE001
        pass
    return hw


def recommend_profile(hw: HardwareInfo) -> Profile:
    """按显存推荐档位。

    阈值是按「跑 7B Q4 需要多少显存」倒推的：
        模型本身 ~4.7GB + 每槽 KV ~0.11GB(ctx2048) + CUDA 开销 ~0.6GB
    """
    vram = hw.vram_total_mb
    if not hw.gpus or vram < 3000:
        return Profile.COMPAT
    if vram < 5500:
        return Profile.NORMAL
    return Profile.TURBO if vram < 10500 else Profile.INSANE


# ---------------------------------------------------------------- 模型挑选

_SMALL_HINTS = ("1.5b", "1.8b", "2b", "3b", "0.5b", "mt-1.8", "mt1.5")
_LARGE_HINTS = ("7b", "8b", "9b", "12b", "13b", "14b")

# 翻译专用模型的名字特征（优先于通用模型的挑选）
TRANSLATION_MODEL_HINTS = ("hunyuan-mt", "hymt", "hy-mt", "translategemma",
                           "mt-", "-mt", "translate")


def classify_model(name: str) -> str:
    """把模型文件名粗分成 tiny / medium / unknown。"""
    low = name.lower()
    if any(h in low for h in _SMALL_HINTS):
        return "tiny"
    if any(h in low for h in _LARGE_HINTS):
        return "medium"
    return "unknown"


def is_translation_model(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in TRANSLATION_MODEL_HINTS)


def pick_model(files: list, mode: str = "auto",
               prefer_small: bool = False, prefer_translation: bool = True):
    """从候选模型文件里挑一个最合适的。

    Args:
        files: Path 列表（.gguf）
        mode:
            "auto"      按档位自动挑（prefer_small 决定大小档）；
                        同档位内优先翻译专用模型
            "quality"   挑最大的（质量优先）
            "speed"     挑最小的（速度优先）
            其它字符串   当作**文件名**（或文件名片段）精确指定
        prefer_small: mode="auto" 时是否偏好小模型
        prefer_translation: mode="auto" 时是否偏好翻译专用模型

    Returns:
        选中的 Path；列表为空时返回 None；mode 指定了文件但匹配不到时
        退回 "auto" 的结果（不会因为用户手滑而彻底失败）。
    """
    if not files:
        return None

    def size_of(p) -> int:
        try:
            return p.stat().st_size
        except OSError:
            return 0

    def name_of(p) -> str:
        return p.name if hasattr(p, "name") else str(p)

    # ---- 显式指定文件名 ----
    if mode not in ("auto", "quality", "speed"):
        low = str(mode).lower()
        for p in files:
            if name_of(p).lower() == low:
                return p
        for p in files:
            if low in name_of(p).lower():
                return p
        # 匹配不到就退回自动，不要因为名字写错就罢工
        mode = "auto"

    if mode == "quality":
        return max(files, key=lambda p: (size_of(p), name_of(p)))
    if mode == "speed":
        return min(files, key=lambda p: (size_of(p), name_of(p)))

    # ---- auto：先按档位分大小档，同档内再偏好翻译专用模型 ----
    def sort_key(p):
        name = name_of(p)
        tier = classify_model(name)
        if prefer_small:
            tier_rank = {"tiny": 0, "medium": 1, "unknown": 2}[tier]
        else:
            tier_rank = {"medium": 0, "tiny": 1, "unknown": 2}[tier]
        trans_rank = 0 if (prefer_translation and is_translation_model(name)) else 1
        return (tier_rank, trans_rank, name)

    return sorted(files, key=sort_key)[0]


def model_label(name: str) -> str:
    """把 .gguf 文件名压成界面用的短标签。"""
    s = name
    for ext in (".gguf", ".GGUF"):
        if s.endswith(ext):
            s = s[: -len(ext)]
    for junk in ("-Q4_K_M", "-q4_k_m", "-Q4_K_S", "-Q5_K_M", "-Q8_0",
                 "-Q4_0", "-Q6_K", "-IQ4_XS", "-Instruct", "-instruct"):
        s = s.replace(junk, "")
    s = s.replace("--", "-").strip("-")
    return s or name

