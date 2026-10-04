# -*- coding: utf-8 -*-
"""便携式翻译运行时管理。

让「翻译引擎 + 模型」完全跟着软件文件夹走，用户拷贝整个文件夹即可运行，
不需要单独安装 Ollama。

目录约定（都相对于软件所在目录）::

    滚刀哥布林汉化椅/
      ├─ 滚刀哥布林汉化椅.exe
      ├─ runtime/            <- 内置 Ollama 运行时（可整体替换）
      │    ├─ ollama.exe
      │    └─ lib/ollama/...
      ├─ models/             <- ★ 用户可替换：把手里的 .gguf 丢进来即可
      │    └─ xxx.gguf
      └─ data/               <- 自动生成，模型注册数据

设计选择：
- 使用**独立端口**（默认 11435），避免与用户已装的系统版 Ollama（11434）冲突；
- 模型用「指纹」（文件名+大小+mtime）判断是否需要重新导入；
- 只依赖标准库，不引入额外依赖。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from .profiles import PROFILES, Profile, ProfileSpec, pick_model

# 注册到 Ollama 的模型名（固定，便于内部引用）
MODEL_NAME = "gametl-model"
BASE_PORT = 11435
PORT_RANGE = 20

# 服务日志上限。Ollama 的 serve 日志是**无上限追加**的，且会打印每个请求的
# 逐 token 计时 —— 实测跑几个小时的翻译能把它撑到二十多 GB，直接把工作盘吃满。
# 所以每次启动前轮转一次。
SERVER_LOG_MAX_MB = 32

# 导入模型时使用的默认推理参数
MODELFILE_TEMPLATE = """FROM {gguf}

PARAMETER temperature 0.2
PARAMETER top_p 0.9
PARAMETER repeat_penalty 1.05
PARAMETER num_ctx 4096
"""

# 列出所有 llama-server 进程（含父进程名与存活秒数），结果写进临时文件。
#
# 为什么必须借 PowerShell：Windows 上只用标准库拿不到「某个 PID 的可执行文件
# 路径」和「父进程是否还活着」这两件事，而清理孤儿进程恰恰只能靠它们来判断。
#
# 为什么不直接读 stdout：中文路径在 Windows 控制台默认代码页（GBK）与 Python
# 的 UTF-8 之间转一道就成乱码了，路径一乱码，比对就永远失败（实测踩过：
# 明明是自家引擎的进程，被判成「他方进程，不碰」，一个都没清掉）。
# 所以让 PowerShell 自己按 UTF-8 写文件，Python 再读 —— 全程不经过控制台。
#
# 输出格式：pid|ppid|可执行文件路径|父进程名(DEAD=父已死)|存活秒数
_PS_LIST_RUNNERS = """
$ErrorActionPreference = 'SilentlyContinue'
$all = @{}
Get-CimInstance Win32_Process | ForEach-Object { $all[[int]$_.ProcessId] = $_.Name }
$lines = Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" | ForEach-Object {
  $pn = $all[[int]$_.ParentProcessId]
  if (-not $pn) { $pn = 'DEAD' }
  $age = [int]((Get-Date) - $_.CreationDate).TotalSeconds
  "$($_.ProcessId)|$($_.ParentProcessId)|$($_.ExecutablePath)|$pn|$age"
}
$lines | Out-File -LiteralPath '__OUT__' -Encoding utf8
"""

# 刚起来的 runner 不动 —— 避免和正在加载模型的流程抢
RUNNER_MIN_AGE_SEC = 5


class RuntimeManager:
    """管理内置 Ollama 服务与 models/ 目录里的 GGUF 模型。"""

    def __init__(self, app_dir: Path,
                 on_log: Optional[Callable[[str], None]] = None,
                 profile: Optional[Profile] = None,
                 model_pref: Optional[str] = None):
        self.app_dir = Path(app_dir)
        self.runtime_dir = self.app_dir / "runtime"
        self.models_dir = self.app_dir / "models"
        self.data_dir = self.app_dir / "data"
        self.on_log = on_log or (lambda m: None)

        # 性能档位决定引擎的启动参数（并发槽 / 上下文 / KV 精度 / 批大小）
        self.profile: Profile = profile or Profile.TURBO
        self.spec: ProfileSpec = PROFILES[self.profile]

        self.port: int = 0
        self.model_file: str = ""      # **引擎实际加载**的 .gguf 文件名
        self._proc: Optional[subprocess.Popen] = None
        self._state_file = self.data_dir / "runtime_state.json"

        # 模型选择：auto / quality / speed / 具体文件名
        # 与档位**解耦** —— 档位只该改资源占用，不该偷偷换掉模型。
        # 不显式指定时沿用上次的选择：否则每次开软件都会先按 auto 导一遍大模型，
        # 用户点开始又得换一遍 —— 白导两次，还可能让人以为「选了没用」。
        if model_pref is None:
            model_pref = str(self._read_state().get("model_pref") or "") or "auto"
        self.model_pref: str = model_pref

        # 启动时的自动准备与「开始汉化」时的准备是两条线程，可能同时进来
        # （用户手快，在引擎还没就绪时就把流程点起来了）。没有这把锁，
        # 两个 ollama create 会互相覆盖，最后注册进 Ollama 的模型是哪个
        # 全看运气 —— 界面显示的和实际跑的就会对不上。
        self._lock = threading.RLock()
        self._last_reap: float = 0.0

    # ---------------- 日志 ----------------

    def _log(self, msg: str) -> None:
        self.on_log(msg)

    def _reap_and_log(self) -> list[int]:
        """清掉孤立的推理子进程，并把过程写进日志。

        20 秒内只做一次 —— 一次查询要拉一个 PowerShell，而启动流程里可能
        连着进来两次（启动时的自动准备 + 用户点的「开始汉化」）。
        """
        now = time.time()
        if now - self._last_reap < 20:
            return []
        self._last_reap = now
        killed = self.reap_orphan_runners()
        if killed:
            self._log(f"已清理 {len(killed)} 个遗留的推理进程"
                      f"（PID {', '.join(str(p) for p in killed)}）——"
                      f"它们此前一直占着显存不放")
        return killed

    # ---------------- 路径与探测 ----------------

    @property
    def host(self) -> str:
        return f"http://127.0.0.1:{self.port}" if self.port else ""

    def exe_path(self) -> Optional[Path]:
        """找到内置的 ollama.exe。"""
        for cand in (self.runtime_dir / "ollama.exe",
                     self.runtime_dir / "ollama",
                     self.app_dir / "runtime" / "bin" / "ollama.exe"):
            if cand.exists():
                return cand
        return None

    def list_gguf(self) -> list[Path]:
        """列出 models/ 里可用的模型（按文件名排序）。"""
        if not self.models_dir.is_dir():
            return []
        return sorted(
            [p for p in self.models_dir.iterdir()
             if p.is_file() and p.suffix.lower() == ".gguf"],
            key=lambda p: p.name,
        )

    @staticmethod
    def _port_free(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex(("127.0.0.1", port)) != 0

    @staticmethod
    def _is_ollama(port: int) -> bool:
        """该端口上是否跑着 Ollama。"""
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/version", timeout=2) as r:
                if r.status != 200:
                    return False
                data = json.loads(r.read())
                return "version" in data
        except Exception:
            return False

    def _tags(self) -> list[str]:
        try:
            with urllib.request.urlopen(
                    f"{self.host}/api/tags", timeout=8) as r:
                data = json.loads(r.read())
                return [m.get("name", "") for m in data.get("models", [])]
        except Exception:
            return []

    # ---------------- 模型实况（唯一的真相来源） ----------------

    def manifest_path(self) -> Path:
        """Ollama 为 ``gametl-model`` 写的清单文件。"""
        return (self.data_dir / "models" / "manifests"
                / "registry.ollama.ai" / "library" / MODEL_NAME / "latest")

    def tag_source(self) -> str:
        """``gametl-model`` 这个标签，**究竟**是从哪个 .gguf 建的。

        这是唯一能证伪「界面说是 A、引擎跑的是 B」的东西 —— 清单里的
        ``from`` 字段是 ``ollama create`` 当时写进去的，改不了口供。
        读不到时返回空串（由调用方决定要不要采信别的依据）。
        """
        try:
            data = json.loads(self.manifest_path().read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return ""
        for layer in data.get("layers", []) or []:
            src = layer.get("from")
            if src:
                return Path(str(src).replace("/", "\\")).name
        return ""

    def loaded_info(self) -> list:
        """问 Ollama 现在**真的**把什么模型放在显存里（``/api/ps``）。

        Returns:
            [{"name", "size_gb", "vram_gb", "params", "quant", "ctx"}, ...]
        """
        try:
            with urllib.request.urlopen(f"{self.host}/api/ps", timeout=8) as r:
                data = json.loads(r.read())
        except Exception:  # noqa: BLE001
            return []
        out = []
        for m in data.get("models", []) or []:
            d = m.get("details", {}) or {}
            out.append({
                "name": m.get("name", ""),
                "size_gb": (m.get("size") or 0) / 1024 / 1024 / 1024,
                "vram_gb": (m.get("size_vram") or 0) / 1024 / 1024 / 1024,
                "params": d.get("parameter_size", ""),
                "quant": d.get("quantization_level", ""),
                "ctx": m.get("context_length", 0),
            })
        return out

    @staticmethod
    def rotate_log(log_path: Path, max_mb: int = SERVER_LOG_MAX_MB) -> int:
        """把过大的服务日志滚走，返回释放的字节数。

        Ollama 的日志没有大小上限，也没有轮转 —— 它会打印**每个请求的逐
        token 计时**，跑几小时的翻译就能涨到几十 GB，直接把工作盘吃满
        （实测遇到过一个 23.5 GB 的 server.log）。

        处理策略（每次启动引擎之前跑一次）：

        * 没超阈值 —— 什么都不做；
        * 超阈值但还「有机会看」 —— 改名成 ``.1``（旧的那份直接删掉，
          相当于只保留最近一个窗口）；
        * 大得离谱（超过阈值 8 倍）—— **直接截断**。这种情况下留一份
          二十多 GB 的历史毫无意义，而改名等于换个名字继续占着磁盘，
          一点空间都释放不出来。
        """
        released = 0
        try:
            if not log_path.exists():
                return 0
            size = log_path.stat().st_size
            if size <= max_mb * 1024 * 1024:
                return 0
            if size > max_mb * 1024 * 1024 * 8:
                # 就地截断：进程还握着追加句柄也能安全截断（下一次写回到 0 处）
                with open(log_path, "wb"):
                    pass
                return size
            old = log_path.with_suffix(log_path.suffix + ".1")
            try:
                old.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                log_path.replace(old)
                released = size
            except OSError:
                # 改名失败（句柄被占）就退而求其次：截断
                try:
                    with open(log_path, "wb"):
                        pass
                    released = size
                except OSError:
                    return 0
        except OSError:
            return 0
        return released

    # ---------------- 清理孤立的推理子进程 ----------------

    @staticmethod
    def list_runners() -> list[dict]:
        """列出所有 llama-server 进程。

        Returns:
            [{"pid", "ppid", "path", "parent", "age"}, ...]
            ``parent`` 为 ``"DEAD"`` 表示父进程已经不存在了（= 孤儿）。
            查询失败返回空列表 —— 调用方把「查不到」当成「不确定」。
        """
        if sys.platform != "win32":
            return []
        tmp = Path(tempfile.gettempdir()) / f"gametl_runners_{os.getpid()}.txt"
        script = _PS_LIST_RUNNERS.replace(
            "__OUT__", str(tmp).replace("'", "''"))
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", script],
                capture_output=True, timeout=30,
                creationflags=RuntimeManager._flags())
            raw = tmp.read_text(encoding="utf-8-sig", errors="replace")
        except Exception:  # noqa: BLE001
            return []
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

        return RuntimeManager._parse_runners(raw)

    @staticmethod
    def _parse_runners(raw: str) -> list[dict]:
        """解析 PowerShell 写出来的进程清单。抽出来是为了能离线测。"""
        out: list[dict] = []
        for line in (raw or "").splitlines():
            parts = line.strip().split("|")
            if len(parts) < 4:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
            except ValueError:
                continue
            try:
                age = int(parts[4]) if len(parts) > 4 else 999
            except ValueError:
                age = 999
            out.append({"pid": pid, "ppid": ppid, "path": parts[2],
                        "parent": parts[3], "age": age})
        return out

    def _pick_orphans(self, procs: list) -> list[int]:
        """从进程清单里挑出该清理的 PID。**纯函数，不动手**，便于测试。

        判定为孤儿必须同时满足：
        1. 可执行文件在本软件 ``runtime\\`` 目录下（否则是别人家的引擎）；
        2. 父进程已经不在了（``parent == "DEAD"``）或不像引擎进程 ——
           父进程还活着说明有引擎正在管它，杀了会把人家跑着的翻译打断；
        3. 已经存活超过 ``RUNNER_MIN_AGE_SEC`` 秒。
        """
        try:
            here = str(self.runtime_dir.resolve()).lower()
        except OSError:
            here = str(self.runtime_dir).lower()

        pids: list[int] = []
        for info in procs or []:
            path = (info.get("path") or "").lower()
            if not path or here not in path:
                continue                     # 不是本软件的引擎
            parent = (info.get("parent") or "").lower()
            if parent.startswith("ollama") or parent.startswith("llama"):
                continue                     # 还有活着的引擎在管它
            if info.get("age", 0) < RUNNER_MIN_AGE_SEC:
                continue
            pids.append(int(info["pid"]))
        return pids

    def reap_orphan_runners(self) -> list[int]:
        """结束孤立的推理子进程，返回被结束的 PID 列表。

        ★ 为什么必须做这件事：模型跑在**子进程** llama-server.exe 里，而
        ollama.exe 被结束时，这些子进程**不保证跟着退出**。实测在一台机器上
        残留了 3 个，最老的那个还稳稳占着 4.7 GB 显存 —— 那是上一次加载的
        7B 模型。显存被这么卡住之后，重新加载的小模型只能**部分上卡**：::

            load_tensors: offloaded 28/33 layers to GPU
            load_tensors:    CUDA_Host model buffer size = 336.44 MiB   ← 5 层在 CPU
            llama_context: n_ctx = 16384

        剩下 5 层退回 CPU，每个 token 都要跨 PCIe 同步一次，速度就上不去。
        实测清理前后可用显存：**248 MB → 5525 MB**；关软件那一次又复现了
        一回（ollama 主进程已退、推理子进程还在占 1.8B 的 1.6 GB）。

        安全性：见 :meth:`_pick_orphans` 的三条判定；查询失败什么都不做。
        """
        pids = self._pick_orphans(self.list_runners())
        if not pids:
            return []
        killed: list[int] = []
        for pid in pids:
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/F"],
                        capture_output=True, timeout=15,
                        creationflags=self._flags())
                else:
                    import signal
                    os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            except Exception:  # noqa: BLE001
                continue
        return killed

    # ---------------- 状态持久化 ----------------

    def _read_state(self) -> dict:
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_state(self, **kw) -> None:
        st = self._read_state()
        st.update(kw)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(
                json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ---------------- 启动服务 ----------------

    @staticmethod
    def _flags() -> int:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0) \
            if sys.platform == "win32" else 0

    def _env(self) -> dict:
        """按当前档位构造引擎环境变量。

        这几个变量必须在**启动服务时**就位 —— Ollama 只在启动时读取它们，
        改完不重启是不生效的（所以切档位需要重启引擎）。
        """
        spec = self.spec
        env = os.environ.copy()
        env["OLLAMA_MODELS"] = str(self.data_dir / "models")
        env["OLLAMA_HOST"] = f"127.0.0.1:{self.port}"
        env["OLLAMA_KEEP_ALIVE"] = "30m"        # 别让模型刚热起来就被卸载
        env["OLLAMA_NUM_PARALLEL"] = str(spec.num_parallel)
        env["OLLAMA_CONTEXT_LENGTH"] = str(spec.num_ctx)
        env["OLLAMA_NUM_BATCH"] = str(spec.num_batch)
        if spec.kv_cache_type and spec.kv_cache_type != "f16":
            env["OLLAMA_KV_CACHE_TYPE"] = spec.kv_cache_type
        else:
            env.pop("OLLAMA_KV_CACHE_TYPE", None)
        # 静默日志（注意：OLLAMA_DEBUG 是布尔语义，设 "0" 反而会被当成 true）
        env.pop("OLLAMA_DEBUG", None)
        return env

    def _spawn(self, port: int) -> bool:
        """在指定端口启动内置 ollama serve，等待就绪。"""
        exe = self.exe_path()
        if exe is None:
            return False
        self.port = port
        flags = self._flags()

        # 启动前先把上一次遗留的推理子进程清掉 —— 它们会一直占着显存不放，
        # 新模型因此只能部分上卡，速度直接掉一半以上。
        try:
            self._reap_and_log()
        except Exception:  # noqa: BLE001
            pass

        log_path = self.data_dir / "server.log"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            freed = self.rotate_log(log_path)
            if freed:
                self._log(f"服务日志已轮转（释放 {freed / 1024 / 1024:.0f} MB）")
        except Exception:  # noqa: BLE001
            pass
        try:
            logf = open(log_path, "ab", buffering=0)
        except OSError:
            logf = subprocess.DEVNULL

        try:
            self._proc = subprocess.Popen(
                [str(exe), "serve"],
                env=self._env(),
                stdout=logf,
                stderr=logf,
                stdin=subprocess.DEVNULL,
                cwd=str(self.runtime_dir),
                creationflags=flags,
            )
        except OSError as e:
            self._log(f"[错误] 启动翻译引擎失败：{e}")
            return False

        # 最多等 30 秒
        for _ in range(60):
            if self._is_ollama(port):
                self._log(f"翻译引擎已启动（端口 {port}）")
                self._write_state(port=port, pid=self._proc.pid)
                return True
            if self._proc.poll() is not None:
                self._log("[错误] 翻译引擎进程异常退出，详见 data/server.log")
                return False
            time.sleep(0.5)
        self._log("[错误] 翻译引擎启动超时")
        return False

    def ensure_server(self, force_new: bool = False) -> tuple[bool, str]:
        """确保翻译服务在运行。返回 (成功, 消息)。

        Args:
            force_new: 忽略已有服务，强行用当前档位的参数启动新实例。
                       切换性能档位时用 —— Ollama 只在启动时读环境变量。
        """
        st = {} if force_new else self._read_state()

        # 1) 上次用过的端口还在跑？
        prev = int(st.get("port") or 0)
        if prev and self._is_ollama(prev):
            self.port = prev
            self._log(f"复用已在运行的翻译引擎（端口 {prev}）")
            return True, f"端口 {prev}"

        # 2) 从 BASE_PORT 起找位置
        for p in range(BASE_PORT, BASE_PORT + PORT_RANGE):
            if not force_new and self._is_ollama(p):
                self.port = p
                self._write_state(port=p)
                self._log(f"发现已有翻译引擎（端口 {p}）")
                return True, f"端口 {p}"
            if self._port_free(p):
                self._log(f"正在启动内置翻译引擎（端口 {p}）…")
                if self._spawn(p):
                    return True, f"端口 {p}"
                return False, "翻译引擎启动失败"

        return False, f"端口 {BASE_PORT}-{BASE_PORT+PORT_RANGE-1} 全被占用"

    # ---------------- 档位切换 ----------------

    def _stop_known_server(self) -> bool:
        """停掉此前由本程序启动的引擎（按 state 里记录的 PID）。

        只处理「自己启动的」进程 —— 用户自己装的 Ollama 不在管辖范围内。
        """
        st = self._read_state()
        pid = int(st.get("pid") or 0)
        self._write_state(pid=0, port=0)

        stopped = False
        if pid:
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                   capture_output=True, timeout=15,
                                   creationflags=self._flags())
                else:
                    import signal
                    os.kill(pid, signal.SIGTERM)
                stopped = True
            except Exception as e:  # noqa: BLE001
                self._log(f"[警告] 停止旧引擎失败：{e}")
        elif self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=8)
                stopped = True
            except Exception:  # noqa: BLE001
                pass

        if self._proc:
            try:
                self._proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            self._proc = None
        self.port = 0
        return stopped

    def apply_profile(self, profile: Profile) -> tuple[bool, str]:
        """切换性能档位（如需要则停掉旧引擎，由后续 ensure_server 重启）。

        Returns:
            (是否需要重启引擎, 人类可读说明)
        """
        spec = PROFILES[profile]
        if profile == self.profile:
            self.spec = spec
            return False, f"性能档位保持「{spec.name}」"

        old = self.spec.name
        self.profile = profile
        self.spec = spec
        stopped = self._stop_known_server()

        msg = f"性能档位：{old} → {spec.name}"
        if stopped:
            msg += "；旧引擎已停止，将按新参数重启"
        self._log(msg)
        return True, msg

    # ---------------- 模型 ----------------

    def resolve_model(self) -> Optional[Path]:
        """按当前 model_pref 从 models/ 里定出要用哪个 .gguf。"""
        return pick_model(self.list_gguf(), mode=self.model_pref,
                          prefer_small=self.spec.prefer_small_model)

    def apply_model_pref(self, model_pref: str) -> tuple[bool, str]:
        """切换模型选择。若最终选中的文件变了，需要重启引擎（腾干净显存）。

        Returns:
            (是否需要重启引擎, 人类可读说明)
        """
        if not model_pref or model_pref == self.model_pref:
            self.model_pref = model_pref or self.model_pref
            return False, ""

        old_pref = self.model_pref
        self.model_pref = model_pref
        self._write_state(model_pref=model_pref)   # 记住选择，下次开软件直接用

        ggufs = self.list_gguf()
        new_pick = pick_model(ggufs, mode=model_pref,
                              prefer_small=self.spec.prefer_small_model)
        old_name = self.model_file
        new_name = new_pick.name if new_pick else ""

        if not new_name or new_name == old_name or not old_name:
            self._log(f"模型选择已更新为「{model_pref}」"
                      f"（使用 {new_name or old_name or '—'}）")
            return False, ""

        stopped = self._stop_known_server()
        msg = f"模型切换：{old_name or old_pref} → {new_name}"
        if stopped:
            msg += "；旧引擎已停止，将按新模型重启"
        self._log(msg)
        return True, msg

    @staticmethod
    def fingerprint(gguf: Path) -> str:
        st = gguf.stat()
        return f"{gguf.name}|{st.st_size}|{int(st.st_mtime)}"

    def ensure_model(self,
                     progress: Optional[Callable[[str], None]] = None
                     ) -> tuple[bool, str, str]:
        """确保 models/ 里的模型已注册可用。

        Returns:
            (成功, 模型名, 消息)
        """
        def p(msg: str) -> None:
            self._log(msg)
            if progress:
                progress(msg)

        ggufs = self.list_gguf()
        if not ggufs:
            return (False, "", "models\\ 目录里没有 .gguf 模型文件。\n"
                               "请把模型文件放进软件目录的 models\\ 文件夹。")

        # 挑模型：默认按 model_pref 决定（auto 时受档位的 prefer_small_model 影响）
        gguf = pick_model(ggufs, mode=self.model_pref,
                          prefer_small=self.spec.prefer_small_model)
        self.model_file = gguf.name
        if len(ggufs) > 1:
            others = [g.name for g in ggufs if g != gguf]
            p(f"models\\ 里有 {len(ggufs)} 个模型，本次选用：{gguf.name}"
              f"（其余：{', '.join(others)}）")

        fp = self.fingerprint(gguf)
        st = self._read_state()
        tags = self._tags()

        already = (st.get("fingerprint") == fp
                   and any(t == MODEL_NAME or t.startswith(MODEL_NAME + ":")
                           for t in tags))
        if already:
            # ★ 光看「文件没变过」还不够 —— 必须核对这个标签**当前指向**哪个
            #   gguf。指纹匹配只说明「上次导入的就是它」，可要是中间有别的
            #   流程（并发准备、别的版本、用户手动 ollama create）把标签改掉
            #   了，指纹照样匹配，而引擎跑的已经是另一个模型了。实测踩过：
            #   界面写着 1.8B，Ollama 里跑的是 7B，速度直接差三四倍。
            src = self.tag_source()
            if src and src != gguf.name:
                self._log(f"[警告] 已注册的模型是 {src}，"
                          f"与所选 {gguf.name} 不一致，重新导入")
                already = False
        if already:
            p(f"翻译模型已就绪：{gguf.name}")
            return True, MODEL_NAME, gguf.name

        size_gb = gguf.stat().st_size / 1024 / 1024 / 1024
        p(f"正在导入模型 {gguf.name}（{size_gb:.2f} GB），首次需要 1-3 分钟，请稍候…")

        ok = self._create_model(gguf, p)
        if not ok:
            return False, "", f"模型导入失败，请查看 data\\server.log"

        self._write_state(fingerprint=fp, model_file=str(gguf), model=MODEL_NAME)
        p(f"模型导入完成：{MODEL_NAME} ← {gguf.name}")
        return True, MODEL_NAME, gguf.name

    def _create_model(self, gguf: Path,
                      log: Callable[[str], None]) -> bool:
        exe = self.exe_path()
        if exe is None:
            return False

        self.data_dir.mkdir(parents=True, exist_ok=True)
        modelfile = self.data_dir / "Modelfile.auto"
        try:
            modelfile.write_text(
                MODELFILE_TEMPLATE.format(gguf=str(gguf)), encoding="utf-8")
        except OSError as e:
            log(f"[错误] 无法写入 Modelfile：{e}")
            return False

        flags = self._flags()

        try:
            res = subprocess.run(
                [str(exe), "create", MODEL_NAME, "-f", str(modelfile)],
                env=self._env(),
                cwd=str(self.runtime_dir),
                capture_output=True,
                creationflags=flags,
                timeout=1800,       # 大模型可能较慢
            )
        except subprocess.TimeoutExpired:
            log("[错误] 模型导入超时（超过 30 分钟）")
            return False
        except OSError as e:
            log(f"[错误] 无法执行 ollama create：{e}")
            return False

        if res.returncode != 0:
            err = (res.stderr or b"").decode("utf-8", errors="replace")[-400:]
            log(f"[错误] ollama create 返回 {res.returncode}：{err}")
            return False

        # 让服务重新读取模型列表
        time.sleep(0.5)
        return True

    # ---------------- 一站式准备 ----------------

    def prepare(self,
                progress: Optional[Callable[[str], None]] = None,
                profile: Optional[Profile] = None,
                model_pref: Optional[str] = None,
                ) -> tuple[bool, str, str, str]:
        """完整准备流程：切换档位/模型（如需）→ 启动服务 → 注册模型。

        Args:
            profile:    目标性能档位；None 表示沿用当前
            model_pref: 模型选择（auto / quality / speed / 文件名）；
                        None 表示沿用当前

        Returns:
            (成功, host, 模型名, 消息)
        """
        # 串行化：启动时的后台准备与「开始汉化」触发的准备不能同时跑
        with self._lock:
            return self._prepare_locked(progress, profile, model_pref)

    def _prepare_locked(self, progress, profile, model_pref):
        if self.exe_path() is None:
            return (False, "", "",
                    "未找到内置翻译引擎。\n"
                    "请确认软件目录下存在 runtime\\ollama.exe\n"
                    f"（查找路径：{self.runtime_dir}）")

        force_new = False

        # 1) 性能档位 —— 它决定引擎的启动参数，必须先定下来
        if profile is not None:
            need_restart, _msg = self.apply_profile(profile)
            force_new = force_new or need_restart

        # 2) 模型选择 —— 与档位解耦，可单独指定
        if model_pref is not None and model_pref != self.model_pref:
            need_restart, _msg = self.apply_model_pref(model_pref)
            force_new = force_new or need_restart
        elif profile is not None:
            # auto 模式下，档位变了可能连带选中不同的模型，核对一次
            new_pick = self.resolve_model()
            if (new_pick and self.model_file
                    and new_pick.name != self.model_file):
                self._log(f"随档位调整为使用模型：{new_pick.name}")
                self._stop_known_server()
                force_new = True

        # 0) 每次准备引擎前，先把上一轮遗留的推理子进程清干净。它们会一直
        #    占着显存（实测最夸张的一次卡了 4.7 GB），导致新模型只能部分
        #    上卡、5 层退回 CPU 跑，速度掉一半以上。
        try:
            self._reap_and_log()
        except Exception:  # noqa: BLE001
            pass

        ok, msg = self.ensure_server(force_new=force_new)
        if not ok:
            return False, "", "", f"翻译服务启动失败：{msg}"

        ok2, model, msg2 = self.ensure_model(progress)
        if not ok2:
            return False, self.host, "", msg2

        # 3) 收口核对：导完之后标签必须真的指向所选模型。
        #    万一这里都对不上，宁可当场报错，也不要让用户以为在用 A、实际在跑 B。
        src = self.tag_source()
        if src and self.model_file and src != self.model_file:
            return (False, self.host, model,
                    f"模型注册异常：标签 {MODEL_NAME} 指向 {src}，"
                    f"与所选 {self.model_file} 不一致。\n"
                    f"请检查 data\\Modelfile.auto 或查看 data\\server.log。")

        return True, self.host, model, msg2

    def shutdown(self) -> None:
        """关闭由本管理器启动的服务，并回收它的推理子进程。

        ★ 收尾这一步不能省：ollama.exe 被结束后，它派生的 llama-server.exe
        **不保证跟着退出**。实测关掉软件后残留过 3 个，其中一个还占着
        4.7 GB 显存 —— 用户下次开软件时会发现「明明用的是小模型，却慢得
        离谱」，原因就在这里。
        """
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=8)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

        # 主进程已经走了，它的子进程这时才真正变成孤儿 —— 再扫一遍
        self._write_state(pid=0, port=0)
        try:
            self._last_reap = 0.0        # 关软件时不受节流限制
            self._reap_and_log()
        except Exception:  # noqa: BLE001
            pass
