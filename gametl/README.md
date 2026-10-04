# gametl —— 本地游戏文本汉化工具链

用**本地大模型**（Ollama）驱动的游戏文本汉化流水线。不依赖任何在线翻译服务，
数据不出本机。支持 KiriKiri、RPG Maker MV/MZ、Ren'Py、Unity（文本资源）四类引擎。

> **法律提示**：本工具仅用于处理你**合法拥有**的游戏。逆向、修改、分发商业游戏的
> 汉化补丁涉及版权问题，请自行确认合规性后再使用。

---

## 一、它解决什么问题

汉化的本质是「**找出游戏里的文本 → 翻译 → 写回去**」。本工具把这条链路工程化：

```
游戏目录
   │
   ├─[1] detect   ── 识别引擎（看文件特征）
   │
   ├─[2] 解包     ── .xp3/.assets 等封包需外部工具解开（见 §四）
   │
   ├─[3] extract  ── 从解包目录抽取全部可翻译文本 → project.json
   │
   ├─[4] translate── 调用本地 Ollama 模型批量翻译（带术语表、上下文、续传）
   │
   ├─[5] writeback── 把译文写回文件副本（保留标签/变量/编码）
   │
   └─[6] 重打包   ── 用外部工具打回封包
```

**核心设计**：`TextUnit` 数据模型统一承载「原文 + 位置 + 译文 + 保护片段」，
翻译与引擎解耦——所有引擎走同一条翻译/回填管线。

---

## 二、快速开始

### 0. 环境准备

```bash
# 已为你装好的部分：
#  - Ollama      C:\Users\h\Tools\ollama\ollama.exe
#  - 模型存储     D:\ollama-models
#  - 模型         qwen2.5:7b-instruct (GGUF, Q4_K_M)

# 启动 Ollama 服务（模型目录指向 D 盘）：
export OLLAMA_MODELS="D:/ollama-models"
"C:/Users/h/Tools/ollama/ollama.exe" serve
```

### 1. 自检

```bash
python -m gametl doctor
```

### 2. 识别引擎

```bash
python -m gametl detect "Z:/path/to/游戏目录"
```

### 3. 提取文本

```bash
# 自动识别引擎
python -m gametl extract "解包目录" -o project.json

# 或显式指定（解包目录往往已无引擎特征）
python -m gametl extract "解包目录" -o project.json --engine kirikiri
```

### 4. 翻译

```bash
python -m gametl translate project.json \
    --model qwen2.5:7b-instruct \
    --glossary glossary.example.json
```

- **断点续传**：每 10 条自动落盘，`Ctrl+C` 中断后重跑即可继续。
- **先试水**：加 `--limit 20` 只翻前 20 条，看效果再全量。
- 已有译文的条目默认跳过。

### 5. 回填

```bash
python -m gametl writeback project.json -o translated/
```

### 6. 查看进度

```bash
python -m gametl stats project.json
```

---

## 三、命令速查

| 命令 | 作用 | 关键参数 |
|---|---|---|
| `detect` | 识别游戏引擎 | `<游戏目录>` |
| `extract` | 抽取文本 | `-o` 输出项目文件、`--engine` 指定引擎 |
| `translate` | 本地模型翻译 | `--model` `--glossary` `--limit` `--host` |
| `writeback` | 写回译文 | `-o` 输出目录 |
| `stats` | 查看进度 | — |
| `doctor` | 环境自检 | `--model` |

---

## 四、各引擎的完整流程

### KiriKiri（.xp3）

KiriKiri 的 `.xp3` 是**加密/混淆封包**，本工具**不负责解包**。步骤：

1. **解包**：用 [GARbro](https://github.com/morkt/GARbro)（图形界面，拖入 `.xp3` 即可）。
   - 若报错「未知密钥」，说明是加密包，需要对应的 `.cf` 配置或社区已知密钥。
   - GARbro 解出的 `.ks` 是 UTF-16，可直接被本工具读取。
2. **提取**：`python -m gametl extract <解包目录> -o p.json --engine kirikiri`
3. **翻译 / 回填**（同上）
4. **重打包**：GARbro 支持重新打包为 `.xp3`；或用 KiriKiri 官方的 `krkrrel`。
   - **重要**：回填后的 `.ks` 必须保持原编码（UTF-16），工具已自动处理。
5. **字体**：日文游戏通常缺中文字库，**必须替换字体**，否则中文显示为方块。
   这是 KiriKiri 汉化最容易漏掉的一步（见 §六）。

### RPG Maker MV / MZ

最简单，无需解包（数据就是明文 JSON）。

```bash
python -m gametl extract "游戏目录/www" -o p.json
python -m gametl translate p.json
python -m gametl writeback p.json -o translated/
# 把 translated/ 覆盖回 游戏目录/www/data/ 即可
```

覆盖的文本包括：`Actors/Items/Skills/...` 的 name/description、
事件中的 401 对话指令、`System.json` 的术语表。

> 若游戏用了 `.rpgmvp` 加密素材或数据，需先用 [RPG-Maker-MV-Decrypter](https://github.com/Petschko/RPG-Maker-MV-Decrypter) 解密。

### Ren'Py

1. **解包**（若脚本在 `.rpa` 里）：用 [unrpa](https://github.com/Lattyware/unrpa)
   `unrpa -mp out game.rpa`
2. **提取**：`python -m gametl extract "游戏目录" -o p.json`
3. **翻译 / 回填**
4. **替代方案**：Ren'Py 官方支持翻译文件机制，可让本工具的输出转换为
   `game/tl/chinese/*.rpy`（后续版本支持）。当前 patch 模式直接改 `game/*.rpy` 也可用。

### Unity

**两种路线**：

- **A. 文本资源路线（本工具支持）**：若游戏把文本放在 `.txt/.json/.csv` 里，
  直接提取。`.assets` 中的 TextAsset 需安装 `UnityPy`：
  `env -u PYTHONPATH <venv>/Scripts/python.exe -m pip install UnityPy`
- **B. 运行时路线（推荐用于复杂 Unity 游戏）**：
  用 [XUnity.AutoTranslator](https://github.com/bbepis/XUnity.AutoTranslator)
  + BepInEx，游戏运行时抓取文本并实时翻译，**不改动游戏文件**，最稳。
  可将其翻译后端指向本地 Ollama（配置见 XUnity 的 `AutoTranslatorConfig.ini`：
  `Endpoint=OllamaTranslate`、`OllamaEndpoint=http://127.0.0.1:11434`）。

---

## 五、目录结构

```
gametl/
├─ cli.py                    # 命令行入口
├─ pipeline.py               # 管线编排（extract/translate/writeback）
├─ core/
│  ├─ models.py              # TextUnit / Project 数据模型
│  ├─ detect.py              # 引擎识别
│  └─ protect.py             # 保护片段（变量/标签/转义符）
├─ extractors/
│  ├─ base.py                # 提取器基类
│  ├─ kirikiri.py            # KiriKiri (.ks)
│  ├─ rpgmaker.py            # RPG Maker MV/MZ (JSON)
│  ├─ renpy.py               # Ren'Py (.rpy)
│  └─ unity.py               # Unity（文本资源 + 可选 UnityPy）
├─ translators/
│  ├─ glossary.py            # 术语表
│  └─ ollama_backend.py      # Ollama 翻译后端
├─ glossary.example.json     # 术语表示例
└─ examples/                 # 三个引擎的模拟测试工程
```

---

## 六、常见坑（重要）

| 坑 | 表现 | 解法 |
|---|---|---|
| **字体缺失** | 中文显示为方块/问号 | 替换游戏字体图集，或注入中文 TTF。KiriKiri 常用 `glyph` 机制；Unity 需改 TMP 字体资源 |
| **文本溢出** | 中文比日文长，撑破对话框 | 缩小字号、调整 UI、精简译文。可让模型「尽量简短」 |
| **编码错误** | 回填后游戏乱码/崩溃 | 工具已自动保持原编码。若手工改，务必确认是 UTF-16 还是 Shift-JIS |
| **变量被翻译** | `[pcname]` 变成 `[玩家名]`，游戏报错 | 工具已保护；若模型仍出错，检查 `protect.py` 的模式覆盖 |
| **加密封包** | 解包工具报「unknown key」 | 需找对应密钥；这是逆向工作，超出本工具范围 |
| **DRM/校验** | 改文件后游戏拒绝启动 | 走运行时翻译路线（XUnity），不改文件 |

---

## 七、模型选择建议

本机 **RTX 3060 Ti 8GB**，推荐配置：

| 模型 | 显存占用 | 中文质量 | 速度 | 说明 |
|---|---|---|---|---|
| `qwen2.5:7b-instruct` (Q4_K_M) | ~5.5GB | 好 | 快 | **默认，推荐** |
| `qwen2.5:14b-instruct` (Q4_K_M) | ~9GB | 更好 | 慢 | 会溢出到内存，速度大降 |
| `qwen2.5:3b` (Q4) | ~2.5GB | 一般 | 很快 | 低配备选 |
| `glm4:9b` / `yi:9b` | ~6GB | 好 | 中 | 备选 |

> 8GB 显存下，7B 是质量与速度的最佳平衡点。

---

## 八、测试

三个模拟工程位于 `examples/`，可完整验证链路：

```bash
# 提取
python -m gametl extract examples/mock_rpgmaker_example -o out/rpg.json
python -m gametl extract examples/mock_renpy_example -o out/renpy.json
python -m gametl extract examples/mock_kirikiri_example/decoded -o out/kirikiri.json --engine kirikiri

# 回填测试（用模拟译文，不需模型）
python examples/test_writeback.py
```

---

## 九、傻瓜式图形界面（推荐给非技术用户）

源码：`gametl/gui.py`，用 Tkinter 实现，功能是「选文件夹 → 一键汉化」。

```bash
# 源码方式运行
python -m gametl.gui
```

界面流程：

```
选择游戏文件夹 → 点「开始汉化」
    ↓
[1/6] 识别引擎（kirikiri / rpgmaker / renpy / unity）
[2/6] 解包（.xp3 / .rpa → 临时目录）
[3/6] 提取文本（对话 / 人名 / 菜单）
[4/6] 调用 Ollama 翻译（带断点续传 + 术语表）
[5/6] 回填译文（严格还原占位符）
[6/6] 重新打包 → 输出 <游戏名>_汉化版\
```

支持「取消」按钮，已翻译内容保留（断点续传）。

---

## 十、打包为独立 exe

用 PyInstaller 打成单文件、无控制台窗口的 exe：

```bash
# 需用系统 Python（带 tkinter）：
"C:/Users/h/AppData/Local/Programs/Python/Python38/python.exe" \
    -m PyInstaller --clean --noconfirm build_exe.spec
```

产物：`dist/滚刀哥布林汉化椅.exe`（约 9.6MB，单文件，双击即用）。

打包要点：
- `console=False` → GUI 程序，不弹黑窗
- `hiddenimports` 显式列出 gametl 全部子模块（PyInstaller 静态分析漏掉动态导入）
- `datas` 带上 `glossary.example.json`
- 内置自检：`滚刀哥布林汉化椅.exe --selfcheck`，会写一份 `selfcheck.txt`
  逐项验证依赖导入 + XP3 打包往返，用于确认打包完整性。

---

## 十一、打包后运行（终端用户）

直接分发 `dist/` 目录即可：

```
dist/
  ├─ 滚刀哥布林汉化椅.exe   ← 主程序
  ├─ 启动.bat                ← 推荐入口（自动拉起 Ollama 服务）
  └─ 使用说明.txt            ← 面向用户的说明
```

用户只需：双击 `启动.bat` → 选文件夹 → 点开始。

---

## 十二、大目录性能（重要）

游戏目录动辄数万个文件。早期实现用 `rglob("*")` 全树递归，**并且对每个文件都 `open` 读 10 字节魔数**判断是否封包 —— 在机械盘上会把 UI 线程占满，Windows 直接给窗口打上「未响应」。

新增 `core/scan.py` 提供三重有界遍历：

| 限制 | 默认值 | 作用 |
|---|---|---|
| `max_depth` | 4 | 广度优先，剪掉深层素材目录 |
| `max_files` | 40000 | 防止极端目录无限产出 |
| `time_budget` | 20 秒 | 兜底，超时即停并标记 `truncated` |

改造点：
- `archive.detect_archives()` — 只按**后缀**筛，仅对无后缀文件做魔数兜底
- `archive.repack()` — 查找原封包时同样限界
- `detect.detect_engine()` / `_has_suffix()` — 改用 `find_by_suffix()`
- `extractors/*` — kirikiri / renpy / unity 的扫描全部限界
- `gui.py` — 「选择文件夹」后的分析**移到后台线程**（这是界面卡死的直接原因）

实测（3402 文件样本）：**89.6x 加速**，读盘次数 3400 → 0。

---

## 十三、便携运行时（模型跟着软件走）

`runtime_manager.py` 让翻译引擎与模型完全自包含，用户拷贝文件夹即可运行，无需单独安装 Ollama。

目录约定：

```
软件目录/
  ├─ runtime/           内置 Ollama（ollama.exe + lib/）
  ├─ models/            ★ 用户可替换：丢 .gguf 进来即可
  └─ data/              自动生成（Ollama 模型库 + 状态文件）
```

关键设计：
- **独立端口 11435**（自动向后探测 20 个），避免与用户已装的系统版 Ollama（11434）冲突
- `OLLAMA_MODELS` 指向 `data/models`，与系统 Ollama 完全隔离
- 模型用「文件名 + 大小 + mtime」指纹判断是否需要重新导入
- 服务生命周期由 GUI 托管，窗口关闭时 `shutdown()`

```python
from gametl.runtime_manager import RuntimeManager

rm = RuntimeManager(app_dir, on_log=print)
ok, host, model, msg = rm.prepare()   # 启动服务 + 导入模型
# host -> http://127.0.0.1:11435 , model -> gametl-model
```

换模型：把新 `.gguf` 放进 `models/`（**可以放多个**），在界面上点选即可 ——
详见第十五章。
导入时 Ollama 会把它复制进 `data/models/blobs/`，所以 `data/` 是**可再生数据** ——
分发给别人时只需带 `runtime/` + `models/`，对方首次启动自动重建 `data/`。

---

## 十四、测试

```bash
PY="C:/Users/h/AppData/Local/Programs/Python/Python38/python.exe"

# 硬件检测 + 四档参数 + 模型挑选
env -u PYTHONPATH "$PY" gametl/examples/test_profiles.py

# 两层校验规则（19 项断言）
env -u PYTHONPATH "$PY" gametl/examples/test_validate.py

# 第三层模型抽检（解析/抽样，18 项断言）
env -u PYTHONPATH "$PY" gametl/examples/test_quality.py

# 预过滤规则（英文不能被误判成「已是中文」，~40 项断言）
env -u PYTHONPATH "$PY" gametl/examples/test_prefilter.py

# 扫描性能对比（旧实现 vs 新实现）
env -u PYTHONPATH "$PY" gametl/examples/test_scan_perf.py

# 性能实测（两阶段：通用 7B vs 翻译专用 1.8B）
env -u PYTHONPATH "$PY" gametl/examples/test_speed_bench.py

# 便携运行时（启动引擎 + 导入模型 + 真实翻译）
env -u PYTHONPATH "$PY" gametl/examples/test_portable_runtime.py

# 端到端汉化（识别→解包→翻译→回填→重打包）
env -u PYTHONPATH "$PY" gametl/examples/test_portable_e2e.py

# 不用开界面也能体检（结果写入 selfcheck.txt）
GAMETL_SELFCHECK=selfcheck.txt "$PY" gametl/gui.py --selfcheck
```

> 注意：本机 python 需加 `env -u PYTHONPATH` 绕过沙箱 shim。

---

## 十五、性能档位与模型选择（v1.2）

### 15.1 为什么会有这一章

用户实测反馈：「几秒钟 10 条，完全没有达到软件该有的机能」。
查下来瓶颈**不在模型算力**，而在三个地方：

1. Ollama 默认 `OLLAMA_NUM_PARALLEL=1` —— 请求只能排队，GPU 大量空转；
2. 每请求都带完整的 system prompt + 模板 + 术语表（约 260 token），
   而真正的原文只有 15-60 token，**prefill 占了整次请求的 85% 以上**；
3. 用 7B 通用模型做翻译，杀鸡用牛刀 —— 翻译专用模型 1.8B 就够。

### 15.2 四档模式（`profiles.py`）

档位只管**资源参数**（并发槽 / 上下文 / KV 精度 / 批大小），
引擎参数必须**在启动时**写入环境变量才生效 —— 所以切档位要重启引擎。

| 档位 | 并发槽 | 上下文 | KV | 客户端并发 | 显存 |
|---|---|---|---|---|---|
| 🐢 兼容 | 1 | 2048 | f16 | 1 | ~1.5 GB（可走内存） |
| 🟢 普通 | 2 | 2048 | f16 | 2 | ~3 GB |
| 🔵 强效 | 4 | 4096 | q8_0 | 4 | ~6.5 GB |
| 🔴 狂暴 | 6 | 2048 | q8_0 | 6 | ~7.5 GB+ |

**为什么并发和上下文要成套调**（Ollama 官方口径）：

```
KV 缓存显存 ≈ num_ctx × num_parallel × 每 token KV 大小
```

单独把并发拉高，会连乘 KV 显存，把模型挤出显存掉到 CPU 上 —— 反而更慢。

硬件自动检测（`nvidia-smi` + `GlobalMemoryStatusEx`）按显存推荐档位：
`<3000MB → 兼容`、`<5500 → 普通`、`<10500 → 强效`、`否则 狂暴`。

### 15.3 模型选择（与档位解耦，重要）

> **踩过的坑**：最初把「选哪个模型」绑进档位（狂暴档自动选小模型）。
> 结果用户切到狂暴档，模型被偷偷换成 1.8B，**质量掉了却毫不知情**
> （实测把「けん」译成「勇气」而不是「剑」）。
> 档位只该改资源占用，不该改产出质量 —— 于是把两者彻底解耦。

界面上「翻译模型」是**独立一行按钮**，按 `models/` 里实际的 `.gguf` 动态生成：

| 按钮 | 行为 |
|---|---|
| 自动 | 优先翻译专用模型；再由档位决定大小倾向 |
| *每个 .gguf 文件名* | 用户显式锁定该模型 |

`pick_model(files, mode, prefer_small, prefer_translation)` 支持
`auto` / `quality`（挑最大）/ `speed`（挑最小）/ 具体文件名（支持片段匹配，
写错时退回 `auto` 而不报错）。

### 15.4 实测数据（RTX 3060 Ti 8GB · 120 条短文本）

| 配置 | 条/秒 | 相对提速 | 7.7 万条耗时 |
|---|---|---|---|
| 通用 7B · 并发1（改版前行为） | 8.49 | 1.0x | 2.5 小时 |
| 通用 7B · 精简提示词 · 并发1 | 8.14 | 1.0x | 2.6 小时 |
| 通用 7B · 并发4 | 12.38 | 1.5x | 1.7 小时 |
| 专用 1.8B · 并发1 | 6.62 | 0.8x | 3.2 小时 |
| 专用 1.8B · 并发4 | 23.03 | 2.7x | 56 分钟 |
| 专用 1.8B · 并发6 | 24.24 | 2.9x | 53 分钟 |

三条结论：

1. **并发是主杠杆，且小模型受益远大于大模型**。
   7B 并发 1→4 只涨 1.5x（算力已饱和）；
   1.8B 并发 1→4 涨 3.5x —— 小模型算得飞快，瓶颈是**每请求的固定开销**
   （HTTP 往返、调度、prefill），并行把它藏掉了。
   1.8B 单并发 6.6 条/秒甚至比 7B 的 8.5 还慢，就是这个原因。
2. **精简提示词对大模型没用**（8.49 → 8.14，噪声范围内）。
   能省的是 prefill，但 7B 的瓶颈在 decode，省前缀换不来吞吐。
3. **换模型比调档位见效**：1.8B 专用模型是 7B 通用模型的近 2 倍。
   但质量有短板，所以选择权交给用户（见 15.3）。

### 15.5 批量翻译为什么默认关着

`TranslateOptions.batch_min_len = 24.0`：平均原文短于 24 字时**自动关闭批量**。

原因：批量靠「一个请求译多条、共享固定前缀」省 prefill，
但 JSON 结构开销约 **8 token/条**，比短译文本身还长 ——
生成量翻几倍，吞吐反而下降。实测批量 11.1 条/秒 < 单条并发 13.5 条/秒。
只有平均原文够长（对话/旁白，prefill 占比高）时批量才划算。

---

## 十六、三层译文校对（v1.2）

翻译完不代表翻对了。三层防护，成本从低到高：

| 层 | 位置 | 成本 | 默认 | 作用 |
|---|---|---|---|---|
| 1 即时校验 | `translators/ollama_backend.py` | 免费 | 开 | 每条译文当场体检，不过关立刻重试 |
| 2 规则校对 | `core/validate.py` | 秒级 | 开 | 全量扫描，交叉比对找系统性问题 |
| 3 模型抽检 | `translators/quality.py` | 占显卡 | 手动按钮 | 让模型读「原文+译文」判断对错 |

### 16.1 第一层：即时校验（`check_immediate`）

单条译文立刻过 5 条硬规则，返回 `None` 表示通过：

- 空译文
- **发散**：`len(译文) > max(len(原文)*6, 120)` —— 对超短原文也生效
  （「はい」译出 400 字绝对是发散）
- 占位符 `【0】【1】` 的编号集合必须与原文完全一致
- 假名占比 > 0.5 → 判定「没翻译」
- 残留前缀（模型把 `译文：` 一起吐出来）

不过关就带 0.4s/0.8s 退避重试，共 `max_retries` 次。

### 16.2 第二层：规则校对（`audit_units`）

9 种问题代码，全量扫描 + 全局交叉比对：

| 代码 | 级别 | 含义 |
|---|---|---|
| `empty` | WARN | 译文为空 |
| `placeholder` | WARN | 占位符异常 |
| `untranslated` | WARN | 疑似漏译（含假名却与原文相同/仍是日文） |
| `model_flag` | WARN | 模型判定有问题（第三层回传） |
| `term_miss` | INFO | 术语表里的词没按指定译法出现 |
| `dup_target` | INFO | 多条原文翻成了同一句 |
| `too_short` / `too_long` | INFO | 译文长度异常 |
| `prefix_residue` / `identical` | INFO | 残留前缀 / 与原文逐字相同 |

几个刻意的设计：

- `identical` 与 `untranslated` **互斥**（`if identical and has_kana: ... elif ...`），
  否则同一条会被报两遍刷屏；
- `dup_target` 有例外：译文 ≤3 字且所有原文也 ≤3 字时不算问题
  （「谢谢」这种短词撞车是正常的）;
- 每条 `Issue` 带 `retranslatable` 标志，只有可重翻的才进「重翻队列」。

### 16.3 第三层：模型抽检（`QualityChecker`）

让翻译模型自己当评委，输入「编号 + 原文 + 译文」，只让它输出**有问题的编号**。
提示词里明确写了「人名/术语译法差异不算问题、轻微润色差异不算问题、宁可少报」，
避免它为了凑数乱报。

- `sample_deterministic()` 固定步长抽样，**可复现**（同一批数据每次抽到同一批）
- 解析器容忍 `[1,2]` / `{"problems":[...]}` / `{"1":true}` / 代码块包裹 / 前后噪声
- `work()` 失败返回 `(None, batch)`，与「本批全部合格」返回 `([], batch)` 严格区分 ——
  否则会把「请求挂了」统计成「这批没问题」

### 16.4 GUI 上的两个按钮

- **快速校对**：跑第二层，秒级、免费。跑完弹窗问「要不要重翻这 N 条可疑的？」
- **深度校对**：再叠第三层，默认抽 10%，占显卡几分钟。

点「是」之后只会清空**可疑条目**的译文，其余靠断点续传原样保留 ——
不会白跑一遍全量。

---

## 十七、已知短板与后续方向

1. **1.8B 专用模型有词义短板**。实测「この けんを もっている かぎり」
   把「けん（剣）」译成「勇气」。专用模型的强项是句式与流畅度，
   生僻多义词不如大模型稳。缓解手段：术语表 + 快速校对。
2. **7B 在 8GB 显存上并发收益有限**（1.5x）。若要进一步提速，
   得上 llama.cpp 的 `--cont-batching` 或更大的显存。
3. **术语表命中率**取决于用户有没有先跑一小段把专有名词捞出来。
   目前靠 `glossary.json` 手工维护。

---

## 十八、落盘架构与工作目录（v1.3）

### 为什么工程文件不放在输出目录

实测（RTX 3060 Ti · 1.8B 模型 · 400 条唯一语料 · 均长 21.7 字 · 并发4）：

| 场景 | 条/秒 | 7.7 万条 |
|---|---|---|
| 单条 · **不落盘** | **22.67** | 57 分钟 |
| 单条 · **每 10 条全量落盘** | **10.96** | 2.0 小时 |

**落盘吃掉 52% 的速度。** `Project.save()` 每次都要 `to_dict()` 全部 unit
＋ `json.dumps()` ＋ 重写整个 46MB 文件，单次实测 7.10 秒
（序列化 2.95s 占 GIL、写盘 3.93s）。而 `save_every=10` 意味着每 10 条
就付一次 —— 保存次数 ∝ 总量、文件大小 ∝ 进度，**总成本 O(N²)**。

### v1.3 的解法

1. **JSONL 增量日志**：翻译过程中只 append 一行 `{"uid": ..., "t": ...}`，
   成本与原文件大小无关。
   - `Project.append_journal(entries, path)` —— 热路径上唯一的磁盘写
   - `Project.load(path, journal=)` —— 读取时自动回放日志
   - `Project.compact(path, journal)` —— 结束时合并成完整 json 并删日志
   - `translate_batch` 的 `on_save` 回调现在**接收** `[(uid, 译文), ...]`
2. **去掉 `indent=2`**：46MB → 33MB，序列化也更快。
3. **工作目录放软件盘**：`<软件目录>/data/work/<游戏名>/`。解包产物直接
   输出到那里，**不做「搬来搬回」** —— 零拷贝、没有中断风险。成品仍写到
   游戏目录旁的 `_汉化输出`。
4. **兼容老工程**：新路径不存在但 `_汉化输出/project.json` 存在时就地沿用，
   已翻到一半的工程不会白翻。

> 删掉 `data/work/` 等于放弃断点续传，下次要重头翻。

## 十九、导出完整汉化版（v1.3）

```python
from gametl.auto import export_full_game
res = export_full_game(game_dir, work_root, dest)
# -> {"files": 复制文件数, "overwritten": 覆盖数, "bytes": ..., "translated_dir": ...}
```

复制游戏本体 → 用回填产物覆盖 → 得到一份可直接开玩的汉化版。**原游戏不动。**

- 拒绝目标是游戏目录本身或其子目录（否则递归拷贝）
- 跳过 `_汉化输出` / `_work` 等中间产物
- 找不到回填产物时仍能导出原始游戏（`overwritten=0`）
- 个别文件被占用会跳过，不中断整体
- GUI 入口：绿色的「导出完整版」按钮

## 二十、关闭后临时目录残留（v1.3）

**现象**：关闭软件后弹 `Failed to remove temporary directory: ...\_MEI000055e02`。

**根因**：PyInstaller onefile 模式启动时把自身解压到 `%TEMP%\_MEIxxxxxx`，
退出时由 bootloader 删除；tkinter 的 tcl/tk DLL 往往还没释放，于是删不掉。
**与翻译引擎无关**（`ollama.exe` 能正常清理）。

**修法**：`_cleanup_stale_mei()` 在 `main()` 最早期清理**陈旧**残留：
仅 `_MEI*` 前缀 / 排除当前实例 / 只删 30 分钟以上没被动过的 /
`ignore_errors`。文件被占用时 Windows 会拒绝删除，天然保护。
另外 `on_close()` 改为「先 shutdown 引擎 → quit/destroy → gc + 0.4s 停顿」。

实测：启动 exe 一次，残留从 9 个降到 4 个。

## 二十一、引擎模型实况校验（v1.4）

**现象**：界面写着「当前使用：Hy-MT2-1.8B（手动指定）」，而实际吞吐只有
**5.7 条/秒** —— 1.8B 翻译专用模型在强效档下应该跑 20 条/秒上下。

**根因**：`gametl-model` 这个 Ollama 标签**指向的是 7B**，1.8B 从来没被注册过。
三条独立证据：

```
GET /api/ps            →  gametl-model:latest  7.6B  Q4_K_M  4.67 GB  (qwen2)
manifests/.../latest   →  layers[].from = "Qwen2.5-7B-Instruct-Q4_K_M.gguf"
blobs/                 →  只有一个模型 blob，4,683,074,240 字节
server.log 里的 /api/create 只有 1 次（15:31:10），且全文没有 "hy-mt" 出现
```

**为什么会走成这样**：`ensure_model()` 判断「要不要重新导入」只看一个
指纹（`文件名|大小|mtime`）+ 标签是否存在。指纹匹配只说明*上次导入的就是它*，
**不能说明这个标签现在指向谁**。于是只要有另一条路径把标签建成了 7B
（比如启动时那次 `prepare()` 没带 model_pref，走 `auto`；而强效档的
`prefer_small_model=False` 一定挑中 7B），指纹照样匹配，界面显示的却是
用户手选那个 —— 静默错位，且**速度差 3~4 倍，用户完全看不出来**。

**修法（四层）**：

1. `RuntimeManager.tag_source()` 读清单里的 `from` 字段，回答「这个标签
   到底是从哪个 gguf 建的」；`loaded_info()` 读 `/api/ps` 回答「此刻显存里
   是哪一档参数量」。
2. `ensure_model()` 的「已就绪」判定加上 `tag_source() == 目标文件名`；
   对不上就强制重导。
3. `prepare()` 收口核对：导完之后标签必须真指向所选模型，否则**当场报错**，
   而不是让用户以为在用 A、实际在跑 B。
4. `prepare()` 全程加 `RLock` —— 启动时的自动准备与「开始汉化」触发的准备
   是两条线程，并发 `ollama create` 会互相覆盖，最后注册的是哪个全看运气。
   顺手把模型选择持久化进 `runtime_state.json`，避免每次开软件先按 auto
   导一遍大模型、用户一点开始又换一遍。

GUI 里 `_sync_engine_model()` 在引擎就绪后回读实际模型，直接显示
「引擎已加载：X（7B Q4_K_M · 显存 4.67 GB）」，与所选不一致时改显警告句。

## 二十二、工程完成标记与「不再重复劳动」（v1.4）

**现象**：一个上次已经翻完的游戏，再选一次，程序看起来像从头再来一遍。

**真相**：复用其实**是生效的**（实测 75 291/77 401 = 97.3% 直接复用），
只是界面上完全没有体现，所以用户只能靠猜。真正被重复执行的是后面三段
**全量重做**的阶段：校对要遍历几万条、回填要重写上百个数据文件、
重打包要重新压一遍。

**修法**：

- `Project.progress()` / `is_complete()` / `mark_finished()`，把
  `finished_at` / `units` / `filled` / `unverified` 写进工程 `meta`；
- `Project.read_meta(path)` **只读文件头 512KB** 就能拿到 `meta`（`meta`
  在 `units` 之前），所以「这个游戏翻过没有」是瞬间的回答 —— 选目录时
  不能去解析 30MB 的 JSON；
- `AutoConfig.skip_if_finished=True`：已 100% 且回填产物还在 → 跳过翻译、
  校对、回填，只重新打包；并且**连一次全量写都不做**
  （`need_compact = not already_done or not prev_finish`）；
- GUI 在选择目录后显示 `✅ 这个游戏已经翻完了（77401/77401 条，完成于 …）`，
  主按钮文案变成「重新生成汉化版」；未完成则显示 `⏳ 上次翻到 x/y（z%）`
  和「继续汉化」。

顺手删掉了一处纯浪费：校对阶段之后原本会 `project.save()` 一次 ——
但校对**只做判断、不改数据**，那是一次毫无意义的几十 MB 写入。

## 二十三、失败条目兜底保留（v1.4）

实测某工程有 2 110 条（2.7%）上一轮翻译失败、没拿到译文。逐条重新请求
模型后 **12/12 全部通过校验** —— 说明它们是**瞬态失败**（重试耗尽、
或者上一轮被用户取消时正在飞行中的那一批），不是"翻不动"。

问题在于原来的行为：重试 3 次仍不过第一层硬校验就**丢弃**该条 →
工程永远到不了 100% → 每次重跑都要把这 2 110 条再失败一遍（3 倍推理量）
→ 用户看到的就是"重复劳动"。

**修法**：`translate_one(..., allow_partial=True)` 在重试耗尽且校验仍不过时
返回**最后一次的输出**，调用方再判一次校验，没过就标 `extra["unverified"]`
并写进增量日志的 `uv` 字段。

- 纯网络故障（没有任何输出）仍然抛错，不会兜底出空译文；
- 不传 `allow_partial` 的老调用点行为完全不变；
- 工程能真正走到 100%，同时留下一份「值得人工扫一眼」的清单，
  `result.unverified` 与 `meta.unverified` 都会报出来。

## 二十四、服务日志无上限增长（v1.4）

**现象**：`data/server.log` 达到 **23.5 GB**，占满 D 盘。Ollama 的 serve
日志既不轮转也没有上限，而且会打印**每个请求的逐 token 计时**，翻译跑几小时
就是几十 GB。

**修法**：`RuntimeManager.rotate_log()` 在**每次启动引擎之前**跑一次：

- 没超阈值（默认 32MB）→ 不动；
- 超阈值但还「有机会看」→ 改名成 `.1`（旧的 `.1` 先删，只留最近一份）；
- 大得离谱（> 阈值 × 8）→ **就地截断**。这种情况下留一份二十多 GB 的
  历史毫无意义，而改名只是换个名字继续占磁盘，一点空间都不释放。

实测清掉一次：**释放 23.5 GB**。

## 二十五、孤立推理进程与显存（v1.5）

### 现象：小模型跑出了大模型的速度

用户报告「翻译只有 5~7 条/秒，是不是慢」。查下来界面显示与实际一致
（`/api/ps` 确认显存里就是 1.8B，`tag_source()` 也指向 `Hy-MT2-1.8B`），
但服务日志里赫然写着：

```
load_tensors: offloaded 28/33 layers to GPU
load_tensors:        CUDA0 model buffer size =   932.87 MiB
load_tensors:    CUDA_Host model buffer size =   336.44 MiB   ← 5 层还在 CPU
llama_context: n_ctx                 = 16384                  ← 4096 × 4 槽
```

**33 层只有 28 层上了显卡**，剩下 5 层退回 CPU。每个 token 都要跑一遍
CPU 层、再跨一次 PCIe 同步 —— Ollama 对此**不报错、不提示**，只是慢。

### 根因：孤儿推理进程把显存卡住了

`nvidia-smi` 显示 8 GB 卡已用 7767 MB、只剩 248 MB。列进程才发现：

| PID | 启动时间 | 内存 | 性质 |
|---|---|---|---|
| 2460 | 12:29:31 | 140 MB | 孤儿（父进程早没了） |
| 20788 | 13:33:46 | 101 MB | 孤儿 |
| **9408** | **15:31:28** | **1836 MB** | **上一次 7B 的推理进程，还占着 4.7 GB** |
| 22376 | 16:03:57 | 1132 MB | 当前 1.8B |

Ollama 的模型跑在**子进程** `llama-server.exe` 里。当 `ollama.exe` 被结束
（换档位 / 换模型 / 关软件）时，这些子进程**不保证跟着退出** —— 它们变成
孤儿，但**显存不释放**。清理前后：

```
清理前: 7777 MiB 已用 / 248 MiB 可用
清理后: 2500 MiB 已用 / 5525 MiB 可用      ← 释放 5.15 GB
```

这个 bug 是**可复现**的：写完修复、关一次软件，再看就又多了一个孤儿
（`ollama` 主进程已退，它的 `llama-server` 还在，占着 1.8B 的 1.6 GB）。

### 代价有多大：一次严格 A/B

同一模型、同一语料（160 条唯一合成日语，均长 21.3 字）、同一并发（4），
唯一变量是 `num_gpu`：

| | 上卡层数 | 显存占用 | 稳态吞吐 |
|---|---|---|---|
| A 组 | 28 / 33 | 1500 / 1948 MB（77%） | **6.7 条/秒** |
| B 组 | 33 / 33 | 1724 / 1724 MB（100%） | **12.0 条/秒** |

**提速 1.78x；那 5 层跑在 CPU 上，白白丢掉 44%。**

注意 A 组的 6.7 条/秒 —— 与用户界面上看到的 6.0 条/秒几乎完全一致，
这条线就此闭合。

### 修法

`RuntimeManager` 新增两条纯函数 + 一个动作，接在三个位置：

- `list_runners()` —— 借一次系统进程查询拿「PID / 父 PID / 可执行文件
  路径 / 父进程是否还活着」；
- `_pick_orphans()` —— 三条判定，缺一不可：
  1. 可执行文件必须在**本软件 `runtime\` 目录下**（用户自己装的 Ollama
     不在管辖范围）；
  2. 父进程已经不在（`DEAD`）或不像引擎进程 —— 父进程还活着说明有引擎
     正在管它，杀了会打断人家跑着的翻译；
  3. 存活超过 `RUNNER_MIN_AGE_SEC`（5 秒）。
- `reap_orphan_runners()` —— 执行强杀，返回清掉的 PID。

调用点：`_prepare_locked()` 开头（复用引擎的场景）、`_spawn()` 之前、
`shutdown()` 之后（关软件时最需要）。20 秒内只跑一次，免得重复开销。

### 两个坑

**① 中文路径经控制台往返会成乱码。** 第一版把外部命令的 stdout 直接用
UTF-8 解码，路径变成 `D:\?????粼?ֺ?????\runtime\...`。于是「是不是本软件
的进程」永远比对不成功，判定全是「他方进程，不碰」，**一个都没清掉，
而且不报错**。改成让查询进程自己按 UTF-8 写临时文件、Python 再读，全程
不经过控制台。

**② 「能拿到进程列表」不等于「能安全判断」。** 拿不到信息时（查询被策略
挡住、非 Windows）一律返回空列表 —— 宁可不清，也不要乱杀。

### 界面上的兜底提醒

光修不清理还不够 —— 用户机器上可能还有别的吃显存的程序。所以
`_offload_tip()` 会在引擎就绪后比对 `/api/ps` 的 `size` 与 `size_vram`，
差额超过 3% 就主动说：

> ⚠ 显存不够，模型只有约 77% 在显卡上（1.43 / 1.85 GB），其余部分在内存里跑，
> 速度会明显变慢。关掉一些吃显存的程序（浏览器、微信、远程串流/投屏），
> 或者换用更小的模型后重启软件，通常能快一倍以上。

## 二十六、速度显示改成滑动窗口（v1.5）

用户看到「6.0 条/秒」，而同一轮服务日志逐秒统计是 **8~11 个请求/秒**。

差在哪：旧公式是 `速度 = 累计完成 / 累计耗时`，而 `耗时` 从**点下按钮**
那一刻起算 —— 引擎启动、模型加载进显存（几十秒）全被永久摊进分母。这个数
在数学上**必然**从开场值单调收敛，看起来就像「越跑越慢」。

改成两条线：

- **瞬时**：最近 `RATE_WINDOW_SEC = 8` 秒的增量 / 时间差。取窗口内最早的那个
  样本做基准，窗口一满就向前滑，读数反映的是「眼下这一会儿有多快」；
- **累计平均**：起点改到**翻译阶段真正开始**那一刻（`_tr_t0`，进度条第一次
  跳动时记录），不再把引擎准备算进去。只在两者差距超过 20% 时才一并显示，
  免得数字打架。

显示成：`当前 8.9 条/秒 · 平均 8.2 · 预计剩余 3 分`。

附加两个保护：窗口不足 1 秒时不给读数（否则数字乱跳）；进度计数回退
（重跑 / 重置）时丢掉旧窗口。

## 二十七、导出自动建「中文版」文件夹（v1.5）

原来用户选一个目录，整个游戏就**散落**在那个目录里 —— 想整个拷走、或者
导错了想删掉，都很麻烦。

改成：用户选的目录只是**父目录**，软件在其下自动建
`<原游戏名>中文版`，再把整份游戏放进去。

- 游戏名里的 Windows 非法字符（`< > : " / \ | ? *`）替换成 `_`，首尾空白
  与点也清掉，空名字兜底成「游戏」；
- 用户**直接选中了**那个「…中文版」目录时，就地使用，不再套一层（否则会
  变成 `XX中文版\XX中文版`）；
- 目标已存在且非空 → 弹窗明确列出**完整路径**，警告会先清空，默认按钮是
  「否」。用户点「是」之后才由 `export_full_game(clean=True)` 执行清理；
- 归纳出的命名函数 `export_target_for()` / `safe_name()` 是模块级纯函数，
  可以离线回归测试。

## 二十八、v1.5 的测试

- 自检 **67/67**（v1.4 是 52，新增 15 项），源码与打包 exe 各跑一遍；
- 新增 `test_v15.py` **40/40**，覆盖进程清单解析、孤儿判定四条件、滑动窗口
  取值特性、导出命名三种情形、`clean=True` 清空重导、两条安全边界；
- 新增 `diag_reap.py`（进程枚举诊断，`--kill` 才动手，默认只看）；
- 新增 `bench_v15.py`（端到端吞吐实测，合成唯一语料，不读任何游戏文本）；
- 回归全绿：v14 61 / v15 40 / journal / export / prefilter / validate 19:0 /
  quality 18:0 / profiles / xp3 / rpa / writeback / verify_pack / scan_perf /
  gui_smoke。

## 二十九、为什么「对话都翻了，菜单还是日文」（v1.6）

用户反馈：正文全中文了，但标题画面（ニューゲーム/コンティニュー/回想モード）
和游戏内菜单（アイテム/スキル/装備/ステータス/サブステータス/クエスト確認）
还是日文。实测复查后确认**三个独立原因叠在一起**：

**① 回填路径写错，术语一条都没落地（致命）**

`System.json` 的界面文字在 `terms` 之下：

```
System.json
  └─ terms
       ├─ basic     ["レベル", "ＨＰ", ...]      ← 数组
       ├─ commands  ["戦う", "逃げる", ...]      ← 数组
       ├─ params    ["最大ＨＰ", ...]            ← 数组
       └─ messages  {actionFailure: "...", ...}  ← **对象**
```

而 `_set_value()` 里写的是 `data[loc["group"]][loc["index"]] = value`，**漏了
`terms` 这一层**。于是每次都 `KeyError`，又被

```python
except (KeyError, IndexError, TypeError):
    return False
```

静默吞掉 —— 提取了、翻译了、写进工程了，就是没落进文件。

实测证据（真实工程 77401 条）：

| 检查项 | 结果 |
|---|---|
| 工程里 System.json 已译条目 | 43 条（basic 9 / commands 24 / params 10） |
| 回填前 `terms.commands` | 26 项全是日文 |
| 回填后 `terms.commands` | **17 项变成中文** |

修法：先定位到 `terms` 再取组，并同时兼容 `key`（对象）与 `index`（数组）
两种形态，旧工程里已有的 loc 继续可用。

**② 有一批界面字段从来没被提取过**

`terms.messages` 是**对象**不是数组，而提取端只处理 `isinstance(arr, list)`，
整个 51 条战斗提示被静默跳过。同样漏掉的还有 `System.json` 顶层的
`armorTypes` / `weaponTypes` / `skillTypes` / `elements` / `gameTitle` /
`currencyUnit` —— 装备类型、武器类型、技能分类、属性名、货币单位，
全是玩家直接看得到的文字。

**③ `js/` 目录完全不在扫描范围内**

标题菜单里的「回想モード」、菜单里的「サブステータス」「クエスト確認」
既不在 `data/` 里也不在图片里，而是来自插件：

- `www/js/plugins.js` —— 106 个插件的参数。注意参数值常常是**嵌套 JSON
  字符串**：

  ```js
  "baseItems": "[{\"name\":\"サブステータス\",\"command\":\"...\"}]"
  ```

  只当普通字符串翻译，得到的是被转义的一团乱码；必须解析开、逐项下钻、
  再整体序列化回去。实测该文件贡献 248 条界面词。

- `www/js/plugins/*.js` —— 插件源码里硬编码的字面量。46 个插件文件共
  336 条日文。

## 三十、JS 层提取的判据与两条安全线（v1.6）

插件源码是**可执行代码**，往里塞译文必须极小心。判据只有一条：

> 字符串里含假名 / 汉字 / 全角字符，且不是纯 ASCII 标识符或路径。

挡住：`"customstatus"`、`"img/pictures/a.png"`、`"true"`、`"12"`、
`"---General---"`（插件参数的分组标题）。
留下：`'クエスト確認'`、`"回想モード"`、`"ウィンドウ透明度"`。

**安全线一：单字符必须挡下。**

第一版没设长度下限，实测立刻出事 —— 扫 `rpg_windows.js` 得到 236 条，
全是 `'あ','い','う','え','お','が',...`；扫 `rpg_objects.js` 得到 26 条
`'Ａ','Ｂ','Ｃ',...`。那是**名称输入界面的五十音表与全角字母表**，
翻掉之后玩家就没法给角色起名了。加 `JS_TEXT_MIN_LEN = 2` 后两者归零。

**安全线二：引擎核心 `rpg_*.js` 默认不碰。**

那些文件里的日文几乎全是上述字符表一类的**数据**，而引擎自带的界面词
本来就走 `System.json` 的 `terms`。所以 `enable_engine_js` 默认 `False`，
只处理 `js/plugins/` —— 那里才是作者手写的界面文字。

最终（真实游戏）JS 层提取 825 条，引擎核心残留 0、单字符残留 0。

回填插件源码用**字符偏移倒序替换**，替换前逐条校验
`text[off:off+len] == original`：文件若被改过，位置对不上就跳过那一条，
宁可漏译也不写坏代码。

## 三十一、增量导出与汉化文件补丁（v1.6）

**问题**：已经导出过一份完整版，后来发现几句译文有问题，改完再点导出，
又要把几个 GB 原封不动地复制一遍。

**做法两层**：

1. `write_back()` 改成**按需写入** —— 内容与目标文件一致就不落盘。
   这不只是省时间：回填产物的 mtime 稳定之后，「这次导出哪些文件真的
   变了」才能靠 `(大小, 修改时间)` 一眼看出来，增量才成立。
2. `export_full_game(incremental=True)` 每次都逐文件比对**源与目标的
   `(大小, 修改时间)`**，一致就连 `copy2` 都不调用，同时在目标目录留下
   一份 `.gametl_export.json` 快照备查。

   注意这里**不要求**存在上次的快照：`copy2` 会保留修改时间，所以用户
   第一次用新版导出（旧成品目录里还没有快照）也能直接享受增量，
   不必白拷一遍几个 GB 再等下一次。`incremental=False` 可强制全量重建。

   实测（合成用例）：首次 3 个文件全拷；再导一次 `files == 0 / skipped == 3`；
   改一句译文后 `files == 1 / skipped == 2`。

返回里多了 `skipped`，界面会如实报告「本次跳过 N 个未变动的文件」。

另外提供 `export_file_patch()`：只导出**汉化后的文件**（不带游戏本体），
可只导 `changed` 清单里的那几个文件，并附一份《安装说明.txt》，
对方直接覆盖进游戏目录即可，连工具都不用装。

## 三十二、翻译包：把译文搬到别的电脑（v1.6）

**需求**：翻译成果能打包给别的设备用，那边**不需要装任何模型**；
而且对方的游戏文件夹名字不一定和自己一样。

**格式**：`<游戏名>_汉化翻译包.gtpkg`（其实是个 zip，压缩后约
**2.7 MB / 77387 条**，导出耗时 0.5 秒）。

```
manifest.json   {"format":"gametl-translation-package","engine":...,
                 "game":..., "units_total":..., "units_translated":...}
units.jsonl     每行 {"uid","f"(相对路径),"o"(原文),"t"(译文)}
```

**为什么对方改名也能用**：匹配首选 `uid`，而

```python
uid = sha1(f"{source_file}|{json.dumps(location)}|{original}")[:16]
```

`source_file` 是 `www/data/Map001.json` 这样的**相对路径**，
**不含游戏根目录名**。所以对方把文件夹改成什么都不影响。

**为什么换个版本也常常能用**：uid 不中时用「**原文全文**」兜底
（翻译记忆）。对方若是另一个版本 —— 地图增删、文件挪位 —— 只要某句原文
一字不差就能复用。同一个原文有多条译文时取出现次数最多的那条。
可以用 `use_memory=False` 关掉。

**为什么不需要模型**：导入只是「填译文」，不产生任何推理。包覆盖不到的
条目才会走模型，而那种情况界面会如实说明。

流程上，导入插在「复用旧工程译文」之后、「判定是否已完成」之前，
所以导入后一旦 100%，`already_done` 会自然成立，
`skip_translate` 随即跳过整个翻译阶段 —— 连 Ollama 的连接都不建立。
端到端测试里特意把 `host` 指向 `http://127.0.0.1:1`（一个不存在的服务）
来证明这条路真的不碰模型。

**顺带修掉的一个隐患**：原先判断「要不要跳过回填」只看
`translated/r` 目录非空。这在 v1.6 会出错 —— 新版多提取了
`plugins.js`，可老产物还在，于是直接跳过回填，新增的译文永远落不了地。
现在改看一份 `writeback_state.json`：记录了上次回填时的条目数、
已译数与文件清单，只要和新提取结果对不上就重新回填。

## 三十三、v1.6 的测试

`examples/test_v16.py`，**86 / 86 通过**，覆盖：

- System.json 的 terms 三组（含对象型 `messages`）提取与回填；
- 顶层 `armorTypes` / `weaponTypes` / `skillTypes` / `elements` /
  `gameTitle` / `currencyUnit`；
- 嵌套 JSON 参数下钻（字符串形态与真对象形态）、日文键改写；
- 机器串必须被挡下（路径 / 布尔 / 数字 / 英文标识符 / 分组标题）；
- 插件源码偏移替换，且注释、路径、标识符原样不动；
- 按需写入（重跑一次 `files_written == 0`、mtime 不变）；
- 旧格式 loc 兼容（v1.6 之前的工程条目继续能回填）；
- 翻译包：导出 / uid 命中 / 原文兜底 / 关掉兜底 / 损坏包被拒绝；
- 文件补丁的清单模式与目录结构；
- **免模型端到端**：`AutoPipeline` + 翻译包，`translated_units == 0`。

配套：`selfcheck` 扩到 92 项（含新增的界面文本与翻译包断言），
`gui_smoke` 全绿。



## 三十四、可逆安装：备份是唯一的退路（v1.7）

「导出完整版」可以随便重来（复制出来的东西，错了删掉就是）。
**就地安装不行** —— 它改的是用户的游戏本体，必须留退路。三条不变量：

1. **备份一律加 `.bak` 后缀。** 备份目录里若留着 `www/data/System.json`、
   `data.xp3` 这种原扩展名，引擎探测（`iter_files` 找 `.json`/`.js`）和
   资源包探测（找 `.xp3`/`.rpa`）会把备份当成游戏本体再扫一遍 ——
   轻则词条出现双份、uid 全乱，重则去解包一个备份文件。
2. **备份只建一次，之后永不覆盖。** 存的是「第一次安装之前的样子」，
   也就是真正的原版。若允许覆盖，「装 → 卸 → 再装」的第二步会把
   **已经汉化的文件**当成原版存下来，用户就再也回不到日文了。
   这条有专门的反向断言守着（`test_v17` 第 5 节）。
3. **状态跟着清单里的 `last_action` 走，而不是「备份目录在不在」。**
   还原之后备份要留着（方便再装），此时若按「目录存在」判断，界面会
   一直显示「已装汉化」，用户会以为还原没生效。

安装流程刻意是「**先在临时目录回填、再覆盖**」而不是直接写游戏目录：
`write_back` 的返回值里有 `changed` 清单，只有真变了的文件才值得备份和
覆盖。实测真实游戏：351 个文件、7.8 万条译文、19 秒。

## 三十五、安装器就是主程序自己（v1.7）

没有做第二个 exe。`want_installer()` 三条判据：

1. 命令行带 `--installer` / `--patch` / `--install`；
2. `sys.executable` 的文件名里带「安装器」（导出补丁包时就是这么命名的）；
3. 不在软件目录里（同级没有 `runtime/` 或 `models/`）而身边躺着 `.gtpkg`。

为什么复用主程序：

- 单独打包一份「安装器」，两份代码迟早各自漂移 —— 改了一边忘另一边；
- 主程序本来就带着 extractor（安装器需要它重新定位文本、生成 uid），
  复用它不额外增重；
- 代价是补丁包里的 exe 和主程序一样大（约 10 MB）。对总量 11 MB 左右的
  补丁包来说可以接受。

**判据 3 只在 frozen 下生效**：源码调试时项目根目录经常躺着测试用的
`.gtpkg`，不限制的话跑一次测试就会把主界面顶成安装器界面。

配对地，`installer.py` 与 `ui_common.py` 都对 tkinter 做了条件导入 ——
`want_installer` / `exe_dir` / `fit_size` 是纯逻辑，不能在无 GUI 的
Python 里连导入都失败（否则启动分流和布局测试都会被卡住）。

## 三十六、界面为什么拆成左右两栏（v1.7）

问题：功能越加越多，卡片一路往下堆，而窗口是固定 `880x880`。卡片占满后
`pack` 会把底部的日志区压到 0 高 —— 界面上按钮都在，**运行日志没了**。
而日志恰恰是出问题时唯一能看的东西。

三条改动：

1. **底部操作栏先 pack（`side="bottom"`）。** 主按钮和进度条永远贴着
   窗口底部，上面塞多少卡片都顶不掉它。
2. **左右分栏**（`ttk.PanedWindow`）：左边设置区，右边日志。日志拿到
   `weight=1`，永远撑满剩余高度；分隔条可拖。
3. **左边设置区可滚动**（`VScroll`），装不下时出现滚动条而不是把别的
   区域挤扁。滚动条只在真装不下时才显示（大屏不该白占一条竖线）。

窗口尺寸改为按屏幕算：

```
fit_size(1280, 720, 1180, 840, min_w=880, min_h=560)   # → 1180×624
```

14 寸笔记本 1920×1080 在 150% 缩放下，tkinter 看到的**逻辑分辨率只有
1280×720**，固定 880 高的窗口会被任务栏和标题栏切掉一截。`fit_size`
按逻辑屏幕尺寸取 min：小屏自动缩、大屏保持设计尺寸。

`fit_size` 刻意写成纯函数（不 import tkinter），没有 GUI 的精简 Python
也能跑布局算术的测试。

## 三十七、「点了没效果」的根因与根治（v1.7）

用户报「导出翻译包点了没效果」。实测点击链路（mock 掉文件对话框）发现
**功能本身是好的**：对话框弹出、包正常生成 2.85 MB。

真问题是交互设计与静默失败：

- 它弹的是**「保存文件」**对话框。用户在对话框里一取消，`if not dest:
  return` 就什么都不做 —— 没有任何反馈；
- 保存成功后只弹一句 `messagebox`，而日志区（见三十六）此时已被挤没；
- 更根本的：tkinter 回调里抛的异常**默认只写 stderr**。打包成窗口程序后
  根本没有 stderr，异常就此蒸发 —— 用户点任何按钮都可能「毫无反应」，
  看起来就是按钮坏了。

三条修法：

1. 改成**选文件夹 → 自动建 `<游戏名>_汉化补丁包/` → 自动打 zip**，
   每一步都写日志，完成时明确弹窗告知路径；
2. `root.report_callback_exception` 接管：任何回调异常都进日志 + 弹窗
   （同一异常去重，避免刷屏）；
3. 弹对话框前 `lift()` + `focus_force()`，避免对话框被主窗口压在后面
   而看起来像没反应。

## 三十八、v1.7 的测试

`examples/test_v17.py` —— **55 / 55 通过**（纯逻辑，无 GUI 依赖）：

- `fit_size` 在 1280×720 / 1366×768 / 2560×1440 / 800×600 / 取不到屏幕时
  的结果都落在屏内且不低于下限；
- `looks_like_game` / `find_game_dir`：游戏内两层子目录也能向上找到根；
  无关目录返回 None（这条曾经的失败暴露了「只看有没有 `game/` 子目录」
  和「光有一个 `.xp3` 就算游戏」两个过宽判据，已收紧）；
- 补丁包：目录结构、zip 单层顶层、说明文案、包内条数；
- 安装：uid 全命中、`System.json` 真被改写、备份内容与安装前逐字符一致、
  备份文件全部带 `.bak`；
- **重复安装不污染备份**（反向断言）；
- 还原：与原始 mock 游戏逐字节一致、状态回到未安装、还原后能再装；
- 拒绝：对不上的包、不存在的目录、没有备份就还原，都给出明确错误。

`examples/test_v17_installer_gui.py` —— **13 / 13 通过**（需要 tkinter）：
把补丁包文件夹塞进游戏目录的子文件夹，验证安装器自动认出游戏根与翻译包、
按钮状态随安装/还原正确翻转。

真实游戏端到端（`verify_v17_real.py`，副本上跑，不动原游戏）：

| 指标 | 数值 |
|---|---|
| 写入文件 | 351 个 |
| 覆盖文本 | 78060 / 78319（99.67%） |
| uid 精确命中 | 78052 · 原文兜底 8 · 未匹配 259 |
| 菜单术语 | 17/26 项变中文，假名占比 59% → 0% |
| `terms.messages` | 假名占比 16% → 0% |
| 安装耗时 | 19.4 s |
| 还原 | 351 个文件，**与原版逐字节 0 处不一致** |

回归：v14 61 / v15 40 / v16 97 / v17 55 / installer_gui 13 + export /
journal / prefilter / rpa / quality / gui_smoke 全绿；
`--selfcheck` 扩到 **105 项**（打包部署后 0 失败）。

## 三十九、打开就是两栏：ttk 分栏的 weight 陷阱（v1.8）

用户反馈：**关掉软件再打开，左边设置区不见了，只剩日志区**，得自己把分隔条
拖出来才看得见选项。这是最典型的那种「能用但反直觉」。

### 根因：weight=0 的那一栏会停在「初次布局时的尺寸」

`ttk.PanedWindow` 分配空间靠 **weight**。`weight=0` 的栏不参与分配，它永远
停在**首次布局那一刻**的尺寸。而首次布局发生在：

- 窗口刚建出来、**还没映射**；
- 左栏的 `lholder` 还是个空 Frame，子控件（滚动容器、卡片）**还没塞进去**。

于是左栏停在 1px。更糟的是旧代码的落位逻辑只有一次机会：

```python
r.after(100, self._place_sash)                  # 只试一次
...
    self._pane.sashpos(0, ...)                  # 窗口没映射时抛 TclError
except (tk.TclError, AttributeError):
    pass                                        # 吞掉 → 永久失效
```

100ms 时窗口还没映射（实测：分栏宽 = 1），`sashpos` 抛错被吞，之后再没有任何
补救 —— 用户看到的就是「打开只有日志」。

### 三层修法（任何一层单独都够，但都留着）

1. **两栏都给非 0 权重**（4:6）。这一层是根治：Tk 从第一次布局就按权重分配，
   不依赖任何时序。实测 `weight=4:6` → 900px 分栏直接得到 360/535。
2. **落位重试到「读回来 == 目标」**。不再「设一次就算数」，而是每 40ms 试一次、
   最多 30 次；`sashpos` 设完还要回读核对（Tk 可能取整或拒绝）。判据是回读值，
   不是「没抛异常」。
3. **ttk 没有 minsize，就在松手时夹**。Tk 8.6 的 `ttk::panedwindow` 只认
   `-weight`，**没有 `-minsize`**（`add(..., minsize=380)` 会报
   `unknown option "-minsize"`，那是经典 `tk::panedwindow` 才有的）。所以
   `clamp_sash()` 在 `<ButtonRelease-1>` 时把位置夹回
   `[SASH_MIN_LEFT, 宽 - SASH_MIN_RIGHT]` —— 不夹的话用户能把某一栏拖成 1px，
   而且**拖没了就没法用鼠标拖回来**，只能重启。

### 顺带：记住用户拖过的位置

`data/ui_prefs.json` 存 `{"sash": 640}`。带保存值启动时用保存值（并夹进合法
区间），没有才用默认比例 0.44。窗口变小后保存值会超界 —— 所以保存值也要夹。
偏好文件坏了 / 是数组 / 值是字符串，一律当作「没有偏好」：**界面不能被偏好文件
拖垮**。

### 真实窗口的验证（不是算术，是真开一次）

`examples/test_v17_layout_gui.py`（56 项）真开两个窗口：

- 第一个窗口：左栏宽 **503px**（不是 1）、右栏 641、卡片自然宽度 474 ≤ 503；
- 人为把分隔条拖到 300px：档位按钮**自动折成 2 行**；松手后被夹回 340；
- 关闭 → 重开：第二个窗口**直接落在 340**，不需要用户再拖一次。

## 四十、默认宽度是量出来的，不是拍的（v1.8）

左栏给多少像素？靠猜必然出错。做法是**量卡片的自然宽度**：

```
body.winfo_reqwidth() = 474   ← 由「四颗档位按钮 + 间距 + 内边距」决定
```

于是 0.44 的默认比例（1180 宽窗口 → 左栏 503）刚好装得下并留 29px 余量。
这个数字变了就该重新量 —— `test_v17_layout_gui` 里有一条
「卡片需要 474 ≤ 左栏 503」的断言守着。

### 顺带修掉切字：Label 默认不换行

Tk 的 `Label` **不自动换行**，文字比容器宽就直接截断，用户永远看不到后半句。
而这些说明文字所在的容器宽度是可变的（还能被拖），所以不能写死
`wraplength`。`wrap_to_parent()` 让 Label 跟着容器宽度换行，并且**只在宽度真
变了时才 configure**（换行会改变控件高度、进而触发容器的 `<Configure>`，
不做这个判断就是自激循环）。

### 按钮行：pack 永远不会折行

`pack(side="left")` 的按钮排永远不会折行，容器一窄就把**最后一颗切掉一半**。
`ButtonRow` 在 `<Configure>` 时按 `button_cols()` 重排：排得下就一行，排不下
就折行，且**先挑整除的列数**（4 颗 → 优先 2+2 而不是 3+1）。

判据里必须排除 `c == 1`：**1 能整除任何数**，不排除的话「排成一列」会被判成
整齐排法而优先于两列 —— 这个 bug 是测试当场抓出来的（`[150,160,130]` 在 340px
下返回 1 而不是 2）。

## 四十一、安装器：同一类坑 + 按钮不许躺在滚动区里（v1.8）

安装器是**上下**分栏（设置区在上、日志在下），上栏原本也是 `weight=0` ——
同一个塌成 1px 的问题。一并改成 3:2 + 重试落位 + 松手夹紧（下限 210 / 130）。

另外把「安装汉化 / 还原原版」从**设置区的滚动容器里**挪到窗口底部固定栏：
它们原本是 `btns = ttk.Frame(body)`，`body` 是那个可滚动区域 —— 卡片一多，
用户就得先滚动才够得着整块屏幕的主角按钮。测试里有一条守着：
`install_btn.master.master is root`。

## 四十二、关窗时的两条收尾（v1.8）

1. **日志泵的 after 链必须掐掉。** 它每 120ms 自我续期，窗口销毁后 Tk 找不到
   那个回调命令，会往 stderr 丢 `invalid command name "..._pump_log"`。窗口
   程序看不见，但那是实打实的异常噪音。现在 `on_close()` 先
   `after_cancel` 再销毁。
2. **工作线程收尾时窗口可能已经关了。** 线程回主线程靠 `root.after()`，窗口
   一关就抛 `RuntimeError: main thread is not in main loop`，被默认线程异常
   钩子记一笔堆栈。`install_thread_hook()` 只吞掉**这一种**（按消息匹配），
   其余照旧交给默认钩子 —— 不能顺手把真 bug 也捂掉。
