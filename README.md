# 滚刀哥布林汉化椅（GoBlingChair）

**本地大模型驱动的游戏汉化工具**。翻译跑在自己电脑的模型上，不联网、不上传、不花钱——
选一个游戏文件夹 → 点「开始汉化」→ 拿到一份可以直接玩的汉化版。

- ✅ 全流程自动：识别引擎 → 解包 → 提取文本 → 翻译 → 回填 → 重打包
- ✅ 断点续传：翻译结果边跑边存，中断后接着跑，不白翻
- ✅ 图形界面：为非技术用户设计，3 步出结果
- ✅ 术语表：人名、专有名词按你指定的译法翻译
- ✅ 三层译文校对：即时校验 → 规则校对 → 模型抽检
- ✅ 可逆安装：就地安装前自动备份，一键还原原版
- ✅ 一键汉化：翻完**不用导出**，直接把这个游戏自己的译文就地写进游戏（也是零模型、几秒）
- ✅ 翻译包：把翻译成果打包分享给别的电脑，对方**不需要装模型**
- ✅ 运行时实时翻译：一键调用 [LunaTranslator](https://github.com/HIllya51/LunaTranslator)（需单独下载），玩什么翻什么

## 支持的引擎

| 引擎 | 支持程度 | 说明 |
|---|---|---|
| RPG Maker MV / MZ | ✅ 完整 | 明文 JSON 直接读写，含插件 JS 界面文本 |
| KiriKiri / 吉里吉里 | ✅ 完整 | `.xp3` 解包/回填/重打包（配合 GARbro），自动处理 UTF-16 编码与字体问题 |
| Ren'Py | ✅ 完整 | `.rpa` 解包，`.rpy` 读写 |
| Unity | ✅ 完整 | 解析 `*_Data/*.assets` 与 Addressables `.bundle`：TextAsset（Yarn Spinner 对话脚本、CSV 文本表）+ **MonoBehaviour 里的 UI 文本**（I2 Localization、UGUI、TextMeshPro）。译文写回二进制封包并自动刷新 Addressables 的 bundle CRC，产出独立汉化版 |
| Double Fine 系 | ✅ 完整 | Buddha / Moai 引擎的 `.~h` / `.~p` 封包（Costume Quest 1·2、Stacking、Headlander 等）：自动解包、回填、原位替换优先重打包，无需外部工具 |
| 通用明文 | ✅ 兜底 | 没有封包的小游戏：散落的 `.txt` / `.csv` / `.json` / `.xml` / `.srt` / `.ass` / `.po`，按格式逐类处理（只碰能读成 UTF-8 的纯文本） |

## 更新日志

### v2.6.8

- **★「一键汉化」卡片现在认得当前游戏，翻完就能一键装进游戏。**
  以前这张卡只认软件自带 `packages/` 目录里的 `.gtpkg` 翻译包 —— **自己刚翻好的译文不在里面**
  （它在 `data/work/<游戏名>/project.json`）。于是翻完一款游戏回到这里，列表里只剩**上一款游戏**
  留下的包、还标着「✖ 对不上」，没有任何一条会被自动选中，「✔ 安装到所选游戏」是灰的 ——
  用户的原话是「没能检测当前汉化的文件是哪个，甚至还停留在老游戏这里，无法直接点击就汉化」。
  现在列表第一条就是**当前游戏自己的译文**（`✔ 完全吻合　<游戏名>（本机译文）· N 条`），
  默认选中，点一下「安装到所选游戏」即可就地装进游戏（现场打一个临时包，走的是同一套已测过的
  `apply_patch`：自动备份原版、可一键还原）。不用再绕「导出补丁包 → 收进软件 → 安装」三跳。
- **卡片文案现在会点名当前游戏。** 原来只说「都不太像这个游戏」，可界面上压根没写「这个游戏」是谁，
  列表里唯一的游戏名又是旧包里的旧游戏名 —— 用户只能理解为「它卡在老游戏那儿了」。
  现在顶部直接写 `当前游戏：《XXX》`，并按情况说清该点哪个按钮。
- **列表选中改成按「身份」恢复，不再按下标。** 本机译文是插在队首的，下标会整体平移，
  按下标恢复会把用户刚选的那条选错成隔壁。
- **装过汉化的游戏不会硬装。** 汉化安装是拿**包里的原文**去游戏里找地方填的，而装过汉化的游戏里
  那些原文已经是中文了 —— 硬装下去一条都对不上，用户只会收到一句「翻译包和这个游戏对不上」
  的红字，完全看不出真实原因。现在直接拦下来并说清「要先还原成原版」，问一句就顺手帮你还原。
- 新增回归 `gametl/examples/test_v268_local_pkg.py`（52 项）：含**端到端**——工程 → 临时包 →
  真装进游戏（文件里出现中文）→ 还原后与原版**逐字节一致**。

### v2.6.7

- **★★ 找到了 Addressables 校验值「刷不干净」的真凶：catalog 载荷解析器只认了一半记录。**
  v2.6.6 曾把 `b62cbb0a`（26MB 开场动画包）判成「catalog 里查无此包、校验值无解、只能放弃」。
  **那个结论是错的** —— 它的 CRC `0xd87a0010`（十进制 3631874064）明明白白写在 catalog 里，
  只是被解析器漏掉了。真因：`m_ExtraDataString` 解码后**不是纯 UTF-16 文本**，而是
  `[ASCII 类型全名][UTF-16LE JSON 记录]` 这样**两种编码交替**排布的；ASCII 段的字节数是**奇数**，
  于是每过一条记录，下一条 JSON 的起始偏移奇偶就翻转一次。旧实现把整块按 UTF-16 解码后再逐条啃，
  **只有偶数偏移的那一半能解析出来** ——《Irmão do Jorel》212 条只啃出 106 条，`b62cbb0a`
  正好落在被静默跳过的另一半里。后果是它的 CRC 永远刷不掉，游戏每次启动都
  `CRC Mismatch. Provided d87a0010 … Will not load AssetBundle` 卡死在 Loading，
  而排查时「把 `d87a0010` 在游戏目录里翻遍五种编码都搜不到出处」。
  现在改为在**字节层**定位每条记录的 `{`（UTF-16LE 的 `7b 00`）、从真实字符边界起解析，212 条一条不漏。
  **修后实测：同一份补丁，`CRC Mismatch` 从 1 条降到 0 条，游戏正常进到语言选择界面。**
- **★ 补上一类「已经改过、却不在本轮产物里」的包也要刷 CRC。**
  v2.6.6 起内容没变化的包不再写进补丁目录（补丁更小、要动的文件更少），但**游戏目录里那一份本身
  可能已经是上一轮改过的** —— 它既不在「本轮改动」里、也不在产物目录里，两处都刷不到，
  补丁一装立刻 Mismatch。《Irmão do Jorel》的 `a29f6ac8` / `aff1ec57` 就是这么漏的
  （catalog 记 8029404 / 118150，磁盘上却是 8031992 / 119295）。现在以「文件大小与 catalog
  记录不符」为准，把这批包一并刷掉。
- 上面两条合起来，《Irmão do Jorel》从「启动即卡死」变成**正常游玩**，汉化文案也已就位
  （开场提示「点击任意按钮开始」「轻触以开始」等）。

### v2.6.6

- **修掉一个「汉化完游戏反而打不开」的致命问题：Unity 的 Addressables 分包游戏，改了资源包却不更新校验值。**
  Addressables 会给每个 `.bundle` 存一个 CRC 并在运行时逐个校验；汉化改写了包体、catalog 里的旧值就失配，
  运行时**直接拒绝加载**。表现是游戏永远卡在 Loading，`Player.log` 里是一串
  `CRC Mismatch … Will not load AssetBundle` 带 `RemoteProviderException: Invalid path`（后者是连带报错，
  路径其实没错）。现在回填后会自动把被改写 bundle 的 CRC 刷新掉。
  实测《Irmão do Jorel》：同一份补丁，修前卡死（内存停在 474MB），修后正常进游戏（558MB，日志里加载完 52 个序列化文件）。
- **修掉 Unity 游戏被「翻坏配置」的问题。** 提取器以前会把引擎/插件配置当正文翻：
  `settings.json` 的 `StandaloneWindows64`、`RuntimeInitializeOnLoads.json` 的 `BlendModes`、
  `link.xml` 的属性名、EOS 配置的 `ProductName`，以及 Addressables 的 `catalog.json`
  （19.9MB 被写成 5.35MB，路径占位符 `{Addressables.RuntimePath}` 被翻译 → 运行时 `Invalid path`）。
  这些值的本质是类型名 / GUID / 路径 / 枚举，翻一个就崩。现在按「文件名名单 + 路径」双重过滤，
  同时不再提取许可证 / 法律文本（Apache LICENSE 之类，玩家根本看不到）。
- **Unity 游戏的真文本终于翻得到了：支持 MonoBehaviour 里的 UI 文本。**
  以前只认 TextAsset，而很多 Unity 游戏（尤其用 Addressables 分包的）文本全在 MonoBehaviour 里 ——
  同一款《Irmão do Jorel》，只看 TextAsset 只能提取 523 条、而且翻的是 Apache 许可证；
  现在提取 4728 条真 UI 文本（I2 Localization 的 `_text` / `drawText` / `hyphenedText` / `preParsedText`，
  UGUI / TextMeshPro 的 `m_Text` / `m_text`）。
  ⚠️ 字段白名单收得很紧，只认字段名里带 `text` 等文本语义词的字段。实测放手去认
  `label` / `title` / `description` 会翻到 InControl 手柄插件的设备描述（`VKB-Sim Gladiator MKII…`）
  和行为树节点名（`Run in parallel`、`Change Character State`）——那些是机器标识，翻了必崩。
  宁可少翻：**字段名白名单是拿真实游戏换来的，加词之前先想清楚它的值可能是机器字符串。**
- 二进制回填改用 `packer="original"` 保存，保住原包体的压缩标志（原版 `dataflags=0x243`，
  默认保存会掉成 `0x40`，丢掉 `BlockInfoNeedPaddingAtStart`），产物加载行为与原版一致。
- **★ 只回填「校验值能中和」的资源包，其余一律原样保留。**
  catalog 只覆盖**一部分** bundle ——《Irmão do Jorel》212 个 `.bundle` 里当时只读出 106 个有 CRC 记录。
  对不上的包按名字比对后跳过，并在日志里说明「哪些包没汉化、少翻了多少条」。
  没有 catalog 的游戏（不走 Addressables）完全不受影响。
  ⚠️ **这一段当时还带了个错结论**：说 `b62cbb0a`（26MB）「catalog 里查无此包、校验值无解、
  改了必崩且无能为力」。**其实是解析器只啃出了一半记录**（详见 v2.6.7）—— 它的 CRC 一直都在
  catalog 里，所谓「放弃 16 条译文」是白放弃的。
- **补丁里只放「真的会改变」的文件。** 断点续传重跑时，某个包可能算出来「内容和现在一样」，
  以前照样写进补丁目录、还得为它去刷 CRC；现在直接不产出，补丁更小、要动的文件更少。
  同时会把产物目录里**上一轮遗留**的、现在判定为不可汉化的资源包清掉（它们一旦被补丁盖进游戏
  同样会让游戏进不去）。
- **★ 原版备份改成按文件惰性补齐。** 以前备份目录「只建一次、之后永不覆盖」——
  好处是里面那份一定是真原版，坏处是**第二次安装碰到上次没碰过的文件时不会给它留备份**，
  那些文件被覆盖后就再也还原不回去了（本次实测事故：5 个 `.bundle` 被覆盖且没进备份）。
  现在每次安装前都会检查「本轮要覆盖的文件、备份里有没有」，缺的立刻补上；
  已存在的绝不覆盖（先存下的那份才是真原版）。

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
- **修掉一个更严重的：「▶ 汉化这个游戏」按钮在默认窗口尺寸下根本没画出来。**
  游戏库页那张卡片的控件是**从上往下**排的，而 tk 的 pack 在容器不够高时会把
  **最后排的那个**挤成 0 高 —— 窗口 1080×756（很常见的笔记本分辨率）时卡片需要 591px
  只拿到 302px，于是「汉化这个游戏 / 还原原版 / 打开文件夹」整排高度 1px、完全不可见；
  连软件自己的默认尺寸 1180×840 也一样（426 < 591）。**用户从装机起就没见过这颗按钮**，
  只能去点底部那条「▶ 开始汉化」，还因此觉得「点了没反应」。
  现在这几块改成 `side="bottom"` **从下往上排**，先把高度占住，让表格去承担挤压。
  回归测试里加了一条真开窗口、量控件 `winfo_ismapped()` 的断言，谁再改回从上往下立刻红。
- **撤掉游戏库页那排「📁 D:\SteamLibrary\steamapps\common」快捷按钮**（开机探测到的常见位置）。
  真正让用户一眼看到游戏的是下面那张表，那排按钮只是把同一件事再说一遍，还把本来就矮的
  「选位置」区域挤得更满。探测本身保留 —— 开机仍然自动扫一遍，打开就有表。
- **同一个页面上不再有两颗干同一件事的按钮。** 游戏库页原先同时长着
  ①「🔍 扫描游戏」和「🔄 重新扫描」（都调 `lib_scan`）；②「▶ 汉化这个游戏」和底部那条
  「▶ 开始汉化」。②尤其糟：两颗按钮走的**不是同一条路** —— 「汉化这个游戏」会智能路由
  （软件里有配得上的翻译包就直接装，几秒完事），「开始汉化」只会老老实实跑模型，
  **点哪颗结果都不一样**。现在删掉重复的扫描按钮，底部操作栏（开始汉化 / 取消 /
  打开输出目录）整条搬进「📊 进度与设置」页 —— 它本来就该长在进度和日志底下。
- 顺带把「一键汉化」卡里那颗按钮从「✔ 汉化到所选游戏」改成 **「✔ 安装到所选游戏」**：
  它和底部的「▶ 开始汉化」是两条不同的路（装现成包 vs 跑模型翻新的），
  但都写「汉化…」容易被当成同一件事的两个入口，改字眼把两者分开。
- 回归新增页签/切页断言 + 一条**通用**守卫（AST 扫 `__init__` / `_build_library_page`，
  同一个函数体里两颗 Button 的 `command` 完全相同即失败 —— 以后再加重复按钮一样跑不掉）；
  `test_v17_layout_gui` 修掉一个**陈旧前提**（分栏从 v2.6.0 起搬进
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
