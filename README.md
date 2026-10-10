# 滚刀哥布林汉化椅（GoBlingChair）

**本地大模型驱动的游戏汉化工具**。翻译跑在自己电脑的模型上，不联网、不上传、不花钱——
选一个游戏文件夹 → 点「开始汉化」→ 拿到一份可以直接玩的汉化版。

- ✅ 全流程自动：识别引擎 → 解包 → 提取文本 → 翻译 → 回填 → 重打包
- ✅ 断点续传：翻译结果边跑边存，中断后接着跑，不白翻
- ✅ 图形界面：为非技术用户设计，3 步出结果
- ✅ 术语表：人名、专有名词按你指定的译法翻译
- ✅ 三层译文校对：即时校验 → 规则校对 → 模型抽检
- ✅ 可逆安装：就地安装前自动备份，一键还原原版
- ✅ 翻译包：把翻译成果打包分享给别的电脑，对方**不需要装模型**
- ✅ 运行时实时翻译：一键调用 [LunaTranslator](https://github.com/HIllya51/LunaTranslator)（需单独下载），玩什么翻什么

## 支持的引擎

| 引擎 | 支持程度 | 说明 |
|---|---|---|
| RPG Maker MV / MZ | ✅ 完整 | 明文 JSON 直接读写，含插件 JS 界面文本 |
| KiriKiri / 吉里吉里 | ✅ 完整 | `.xp3` 解包/回填/重打包（配合 GARbro），自动处理 UTF-16 编码与字体问题 |
| Ren'Py | ✅ 完整 | `.rpa` 解包，`.rpy` 读写 |
| Unity | ✅ 完整 | 解析 `*_Data/*.assets` 里的 TextAsset（Yarn Spinner 对话脚本、CSV 文本表），译文写回二进制封包，产出独立汉化版 |
| Double Fine 系 | ✅ 完整 | Buddha / Moai 引擎的 `.~h` / `.~p` 封包（Costume Quest 1·2、Stacking、Headlander 等）：自动解包、回填、原位替换优先重打包，无需外部工具 |
| 通用明文 | ✅ 兜底 | 没有封包的小游戏：散落的 `.txt` / `.csv` / `.json` / `.xml` / `.srt` / `.ass` / `.po`，按格式逐类处理（只碰能读成 UTF-8 的纯文本） |

## 更新日志

### v2.6.5

- **点「汉化这个游戏」会自动跳到「📊 进度与设置」页**。以前进度条和运行日志都长在第 2 页，
  而用户在游戏库点完汉化之后界面毫无动静（只在按钮文案上变一下），**很多人以为按钮没生效，
  其实翻译正在跑**。现在软件在**真正开跑**的那一刻切过去，进度一目了然；还原原版同理。
- **第 2 页改名：「⚙ 高级设置」→「📊 进度与设置」**。一个叫「高级设置」的页签会让人
  以为自己点错了地方；改名之后它同时也承担「翻译进度在这里」的指路作用。
  页签名抽成了**单一来源常量**，界面上所有「去那一页看看」的提示文案都引用它，
  以后再改名不会再漏掉某处把用户指错方向。
- **同页重排卡片，让「选模型」不用找**：左栏卡片按「用户多久改一次」重排，
  **「翻译模型」提到第一张**（进去就是它，不用滚动），「游戏文件夹」降级为
  「自动扫描没找到 / 我想直接指一个目录」的兜底入口。
- 修掉三个顺带查出来的真问题：忙时按钮文案写着「进度见下方」（切页之后那一页就不在下方了，
  等于把用户指错地方）；模型卡片前置后一处读取了尚未创建的变量，会**静默显示一个选错的模型名**；
  切性能档位时模型说明不刷新。
- 回归新增页签/切页断言；`test_v17_layout_gui` 修掉一个**陈旧前提**（分栏从 v2.6.0 起搬进
  第 2 页，而该页默认未选中——未映射控件的宽度恒为 1，导致这个测试一直在量空气），
  并把它和 `test_v17_installer_gui` 一起收进批量回归（现在共 27 个用例）。

### v2.6.4

- **预算不足时不再「整张表放弃」**：dfpf 封包资源的解压后字节数有硬上限（超了游戏启动
  即死循环），而中文一个字 UTF-8 占 3 字节，中译必然撞线。以前是**一条**译文塞不下就
  丢掉**整张表**的译文；现在按条计算「净增字节」，**只丢掉真正塞不下的那几条**，其余照写。
  安全性完全相同（总量仍 ≤ 上限），但不会再出现「一整块文字变回英文」。
  - 实测 Costume Quest 2 真包（7 张表 13435 条）：真实译文下两版行为一致（全角压缩已够用）；
    一旦译文更膨胀（×1.2），旧逻辑 **0 条落地 / 7 张表全灭** → 新逻辑 **7663 条落地 / 0 张全灭**。
- 回归新增 `test_v263_dfpf_budget.py` 第 4b 节；自检新增「逐条淘汰」正向断言。

### v2.6.3

针对 **Double Fine 系（Buddha/dfpf）封包**的三处实战修复，全部来自 Costume Quest 2 的真实事故：

- **修「汉化后游戏启动即卡死、窗口关不掉」**：封包资源解压后的字节数不能超过原版，
  而中文一个字 UTF-8 占 3 字节，中译必然撞线。现在回填时先做三阶段全角→半角压缩，
  仍超预算就放弃该表、保留原版 —— 宁可少翻，也不让游戏起不来。
  （v2.6.4 起这条降级为「只放弃塞不下的那几条」，见上。）
- **修「多行文本挤成一行」**：服装说明这类多行文本原版存的是引号内 `\r\n` 转义，
  早期实现把换行当噪声压成空格，落盘后整段并成一行。现在换行原样保留。
- **修「模型把提示词也写进译文」**：新增提示词回声防线，命中即整批作废重试。
- 新增回归测试 `test_v263_dfpf_budget.py`（六节：全角压缩分阶段 / 换行保留 /
  提示词回声 / 预算不变量 / 端到端窄带 / 窗口化修复）。

### v2.6.0 – v2.6.2

- **v2.6.0 / v2.6.1 傻瓜式「游戏库」**：打开就是一张扫好的游戏表，选中 → 点一下即可。
  术语表 / 模型 / 性能档位等专业概念收进第 2 个页签（功能一个没删，默认看不见）。
  （该页签 v2.6.0 起叫「⚙ 高级设置」，**v2.6.5 起改名「📊 进度与设置」**。）
- **v2.6.2 术语表全自动 + 进度搬到游戏库页**：不再需要手动点「生成术语表」，
  进度直接显示在游戏库页的状态行与进度条上。

## 快速开始

### 方式一：下载便携版（推荐普通用户）

1. 到 [Releases](../../releases) 下载最新的 exe（单文件，约 16.7 MB，免安装）
   > Release 上的资产名形如 `GoBlingChair-vX.Y.Z.exe`（GitHub 不接受中文资产名），
   > 下载后可以随意改名，叫 `滚刀哥布林汉化椅.exe` 也行 —— 程序本身不依赖文件名。
2. 把它放进一个文件夹，双击运行

> **首次使用需要翻译引擎和模型**。软件是纯离线翻译，靠本地模型干活，
> 需要一个 Ollama 便携引擎（`runtime\`）和一个 `.gguf` 模型（`models\`）。
> 把 gguf 文件丢进 `models\` 文件夹，软件会自动识别并在界面上列出。
> 若没有本地模型，也可以先按 [方式二](#方式二从源码运行) 装一个 Ollama 系统服务，指向它翻译。

```
软件目录/
  ├─ 滚刀哥布林汉化椅.exe   ← 主程序
  ├─ runtime/               ← 内置 Ollama（ollama.exe + lib/）
  ├─ models/                ← ★ 丢 .gguf 进来即可换模型
  └─ data/                  ← 自动生成，可再生
```

### 方式二：从源码运行

```bash
# 需要 Python 3.8+（GUI 需要 tkinter）与 Ollama
python -m gametl doctor          # 环境自检
python -m gametl.gui             # 启动图形界面
```

命令行方式：

```bash
python -m gametl detect  "游戏目录"            # 识别引擎
python -m gametl extract "游戏目录" -o p.json  # 提取文本
python -m gametl translate p.json --model qwen2.5:7b-instruct
python -m gametl writeback p.json -o translated/
```

## 翻译模型

任何 Ollama 兼容模型都能用。把 `.gguf` 文件丢进 `models/` 文件夹，
下次启动软件会自动识别，界面上直接选即可。

| 显存 | 推荐 |
|---|---|
| ≥ 6 GB | 通用 7B（Qwen2.5-7B-Instruct Q4_K_M，质量好） |
| ≥ 2 GB | 翻译专用 1.8B（如 Hunyuan-MT-1.8B，速度快、专为翻译训练） |

软件会自动检测显存并推荐运行档位（并发 / 上下文 / KV 精度成套调优）。

## 运行时实时翻译

对于加密封包、DRM 校验、联机游戏等**改文件不好使**的场景，软件可以一键启动
[LunaTranslator](https://github.com/HIllya51/LunaTranslator)（开源、免费），
在游戏运行时抓取文本实时翻译。把它解压到软件目录的
`LunaTranslator\` 下即可被自动识别，翻译后端会自动指向本软件的本地模型。

## 项目结构

```
gametl/                  ← 主源码包
  ├─ gui.py              ← 图形界面（Tkinter）
  ├─ auto.py             ← 全自动管线
  ├─ core/               ← 引擎识别 / 封包读写 / 大目录有界扫描
  ├─ extractors/         ← 各引擎文本提取器
  ├─ translators/        ← Ollama 后端 / 术语表 / 质量抽检
  ├─ installer.py        ← 补丁包安装器（备份 / 还原）
  ├─ runtime_manager.py  ← 便携运行时管理（端口隔离 / 模型指纹 / 孤儿进程回收）
  └─ profiles.py         ← 性能档位（按显存自动推荐）
gametl/README.md         ← 开发者技术文档（二十九章实战经验，强烈推荐）
build_exe.spec           ← PyInstaller 打包配置
```

---

## English

**GoBlingChair** is a **fully offline game-translation tool for Windows** that turns a game folder
into a playable Simplified-Chinese version using a **local LLM** — no network, no uploads, no API
keys, no cost.

Pick a game folder → click translate → get a translated build. The tool detects the engine, unpacks
the archives, extracts the text, translates it with an Ollama model running on your own PC, writes
the result back, and repacks — end to end.

### Features

- 🔌 **100% offline and private** — translation runs on a local GGUF model through a bundled
  portable Ollama runtime. Nothing ever leaves your machine.
- 🧩 **Six engines** (table below), from `.xp3` / `.rpa` archives to binary Unity `.assets`.
- ⏸️ **Resumable** — results are checkpointed as they go, so you can stop and continue later.
- 📖 **Glossary support** — force your own wording for character names and proper nouns so they stay
  consistent across the whole game.
- 🧪 **Three-layer QA** — instant validation → rule-based proofreading → model spot-checks.
- ↩️ **Reversible install** — the originals are backed up before patching; one click restores them.
- 📦 **Shareable translation packs** — ship a finished translation as one file; the recipient
  installs it **without needing a model**.
- 🎮 **Runtime translation** — launches [LunaTranslator](https://github.com/HIllya51/LunaTranslator)
  (downloaded separately) pointed at the local model, for games you can't patch.

### Supported engines

| Engine | Support | Notes |
|---|---|---|
| RPG Maker MV / MZ | ✅ Full | Plain-text JSON read/write, including plugin JS UI strings |
| KiriKiri | ✅ Full | `.xp3` unpack / write-back / repack (works alongside GARbro); handles UTF-16 and font issues |
| Ren'Py | ✅ Full | `.rpa` unpack, `.rpy` read/write |
| Unity | ✅ Full | TextAssets inside `*_Data/*.assets` (Yarn Spinner dialogue, CSV text tables), written back into the binary |
| Double Fine (Buddha / Moai) | ✅ Full | `.~h` / `.~p` packs (Costume Quest 1·2, Stacking, Headlander…); in-place replacement preferred, no external tools needed |
| Generic plain text | ✅ Fallback | Unpacked games: `.txt` / `.csv` / `.json` / `.xml` / `.srt` / `.ass` / `.po`, UTF-8 only |

### Quick start

1. Download the latest exe from [Releases](../../releases) — a single file, ~16.7 MB, no installer.
2. Drop it into a folder and run it. First use also needs an Ollama runtime (`runtime\`) and a
   `.gguf` model in `models\` — put any Ollama-compatible model there and the app picks it up.
3. Open the app, pick your game from the scanned library, click translate, and watch the progress.

Or from source (Python 3.8+ with tkinter):

```bash
python -m gametl doctor            # environment check
python -m gametl.gui               # launch the GUI
python -m gametl detect  "GAME_DIR"
python -m gametl extract "GAME_DIR" -o project.json
python -m gametl translate project.json --model qwen2.5:7b-instruct
python -m gametl writeback project.json -o out/
```

### Notes

- The UI is Chinese-only — the tool is built for translating **into** Chinese.
- Recommended models: a general **Qwen2.5-7B-Instruct** (~Q4_K_M, better quality, ≥6 GB VRAM) or a
  translation-tuned **Hunyuan-MT-1.8B** (faster, ≥2 GB VRAM). The app detects your VRAM and
  recommends a tuning profile automatically.
- This tool is intended for translating **games you legally own**, for personal study and research.
  Do not use it to distribute pirated software or to circumvent copy protection.

## 合规声明

本工具仅供汉化**你合法拥有的**游戏、个人学习研究使用。请勿用于传播盗版
或规避版权保护。逆向、修改、分发商业游戏的汉化补丁涉及版权问题，请自行
确认合规性后再使用。

## License

[MIT](LICENSE)
