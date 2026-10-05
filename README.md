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

## 快速开始

### 方式一：下载便携版（推荐普通用户）

1. 到 [Releases](../../releases) 下载最新的 `滚刀哥布林汉化椅.exe`（单文件，约 16MB，免安装）
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

## 合规声明

本工具仅供汉化**你合法拥有的**游戏、个人学习研究使用。请勿用于传播盗版
或规避版权保护。逆向、修改、分发商业游戏的汉化补丁涉及版权问题，请自行
确认合规性后再使用。

## License

[MIT](LICENSE)
