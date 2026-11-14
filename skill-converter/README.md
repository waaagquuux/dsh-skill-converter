# DSH 外部技能极速转换器

把网上找来的技能包（Claude Skills、提示词包、别人的 GitHub 仓库）一键变成 DSH 能用的技能，**顺手拍快照，随时能反悔**。

- 一个文件 `convert.py`，只用 Python 标准库，不联网装依赖
- 只读不执行：绝不运行包里的任何脚本，可执行文件一律不落地
- 动手前先备份到 `_backup_<时间戳>/`，`--undo` 一键回滚
- 每次跑完生成一份单文件离线报告（`report.html`），双击就能看

> 当前是第二阶段：命令行 + 静态报告 + 双击即用的 exe + 省 Token 的压缩版。批量转换、网页版增强还在后面。

---

## 双击就能用（不想碰 Python 的人看这里）

找到 `dist\DSH技能转换器.exe`，**双击**。会弹出一个黑色窗口，按提示做：

1. 把 GitHub 链接、zip 文件或文件夹**拖进窗口**，回车
2. 选 `1` 先演习（什么都不写，最安全）→ 回车
3. 看完结果，再双击一次，这次选 `2` 装进 DSH 技能目录
4. 想反悔：双击同一个 exe，选完来源后……不，撤销更简单 —— 在窗口里输入完来源之前，
   直接在 exe 旁边开个命令行敲：

   ```bat
   "DSH技能转换器.exe" --undo
   ```

   或者让 AI 帮你按。

exe 自带 Python（约 8 MB），**不需要你装任何环境**，也不需要联网装依赖。

> 想把它放到桌面？直接把 `DSH技能转换器.exe` 拖到桌面就行。`output/`、`_backup_<时间戳>/`、`report.html` 都会生成在 exe 旁边那个文件夹里。

---

## 快速开始

本机推荐用 DSH 自带的 Python（不用自己装）：

```bat
set PY=C:\Users\Lenovo\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe

:: 1) 先预演，什么都不写
%PY% convert.py https://github.com/mattpocock/skills --name grilling --dry-run

:: 2) 看着没问题，正式装进 ~/.dsh/skills
%PY% convert.py https://github.com/mattpocock/skills --name grilling --install
```

装完等一两秒，DSH 会自动刷新技能目录。

## 三种来源

| 来源 | 写法 |
|---|---|
| GitHub 仓库 | `https://github.com/<owner>/<repo>`（可带 `/tree/<分支>`） |
| 本地压缩包 | `convert.py D:\下载\some-skill.zip` |
| 本地文件夹 | `convert.py D:\下载\some-skill\`（文件夹里要有 `<名字>/SKILL.md`） |

GitHub 走整仓 tarball 下载（比逐文件抓快几十倍），默认分支 `main`，仓库用 `master` 的话会自动回退。

## 一次转多个

两个维度都支持，可以一起用：

```bat
:: 一个仓库里有 39 个技能，全转（每个都单独出报告、单独备份）
convert.py https://github.com/mattpocock/skills --all --install

:: 多个来源一起转（zip、文件夹、链接混着给都行）
convert.py "D:\下载\a.zip" "D:\下载\b\" https://github.com/x/y --all --dry-run
```

- 批量时**单个技能有问题只跳过它**，其余照常转；跳过的会列在报告「跳过」区块和命令行尾部，原因写清楚（比如「含可执行文件」）。
- 带 `.sh/.bat/.ps1` 辅助脚本的技能默认被跳过；确实要装就加 `--allow-scripts`——脚本原样复制，工具绝不执行，报告里单独列一节、命令行也会提示。
- `.py/.js` 这类辅助代码不算脚本、会照常复制（官方 pdf/docx 技能全靠它们干活），但报告里会一条条点名，并写明「本工具没有运行它们」。
- 多个来源会按顺序各跑一遍，报告编号 `report.html`、`report-2.html`…，**每批一个 `_backup_`，`--undo` 撤最新一批**（连按几次就逐批回退）。

## 报告怎么用

报告是单个 HTML 文件，双击就能开，不联网、不加载任何外部脚本。技能多的时候：

- 顶部有搜索框，输技能名、描述、路径都行，按 `/` 直接聚焦、`Esc` 清空。
- 筛选按钮：全部 / 仅手动 / 可自动调用 / 省 ≥5%（按钮按当前这批技能的实际情况出现，没有就不显示）。
- 每个技能是一张可折叠的卡片：点标题展开，能看到描述、正文行数、安装路径，以及这个技能的压缩/附件数据；技能 ≤ 5 个默认全展开，多了默认收起。
- 右下角有「回到顶部」，长报告不用一路滚。

## 命令行参数

| 参数 | 作用 |
|---|---|
| `--dry-run` | 预演：只检查、不写文件，生成 `report-dry-run.html` |
| `--install` | 装进 DSH 技能目录；不加就只输出到 `output/` |
| `--all` | 包里多个技能时一次全转（不加则拒绝并让你用 `--name` 挑一个） |
| `--allow-scripts` | 允许复制技能里的脚本（`.sh/.bat/.ps1` 等）。**只复制，绝不执行**；`.exe/.dll` 这类二进制永远拒绝 |
| `--name <名字>` | 从包里挑一个技能，或覆盖推断的名字 |
| `--skills-dir <路径>` | 临时换技能目录（测试用，不碰真实目录） |
| `--local` | 装到程序旁边的 `local-skills/`（Agent 被限制只能写工作区时用） |
| `--output-dir <路径>` | 不装技能目录，只把产物放到指定目录 |
| `--self-install` | 把本工具自己装成一个 DSH 技能（引擎 `convert.py` 跟着技能目录走） |
| `--no-report` | 不生成报告 |
| `--open` | 跑完自动打开报告 |
| `--undo` | 回滚最近一次转换 |
| `--force` | 配合 `--undo`：检测到冲突也继续（会先把当前版本另存到 `output/_undo_keep_*/`） |
| `--list-backups` | 列出所有快照 |
| `--compress` | 生成省 Token 的压缩版，另存为 `SKILL.compact.md`；`refs/` 等目录里的 `.md` 附件也会各生成一份 `<原名>.compact.md`（原文件都不动） |
| `--aggressive` | 规则压缩改用有损模式（删整节/截超长示例）；过不了结构初筛就不会写进技能目录 |
| `--no-attach` | 只压 `SKILL.md`，附件不碰 |
| `--compact-file <路径>` | 用你或 AI 改写好的版本当压缩版；先过结构初筛，改坏了直接拒绝 |
| `--use-compact` | 把压缩版当 `SKILL.md` 装进去（配合上面两个之一） |

## 省 Token：压缩版怎么用

技能正文每次都要进 AI 的上下文，越长越花钱。这个工具给你两条路，**都不动原文**：

```bat
:: 路线 1：规则压缩（离线、免费、保守）——只删重复段落/多余空行/徽章图，默认不改写正文
%PY% convert.py 来源 --compress --dry-run

:: 想更激进（会删整节、截超长示例）：加 --aggressive；过不了结构初筛就不会落地
%PY% convert.py 来源 --compress --aggressive --dry-run

:: 路线 2：让 AI 改写一版，再交给工具校验（省得更多）
%PY% convert.py 来源 --compact-file 我改好的.md --dry-run
:: 校验通过后装上压缩版
%PY% convert.py 来源 --compact-file 我改好的.md --install --use-compact
```

**附件也压**：技能目录里的 `refs/notes.md` 这类 `.md` 附件，同样会生成 `refs/notes.compact.md` 并排放在旁边（原文件一个字节都没改）。图片、csv、pdf 一律不碰；单个附件超过 256 KB 不碰；压完省不到 3% 就不生成。附件同样过一遍结构初筛，没过的不落盘。`--no-attach` 可以关掉这一步。

注意：`SKILL.md` 里如果写死了 `refs/notes.md` 这个文件名，AI 读到的还是原版；想让它读压缩版，你自己把引用改成 `.compact.md`，或者用 `--use-compact`（只影响 `SKILL.md` 本身，附件的替换由你决定）。

**结构初筛**（落地前必过）会逐项核对：frontmatter 全字段（`name`/`description`/`disable-model-invocation` 等一个不改）、所有 `--参数` 按完整 token 比对、`MUST/NEVER/必须/不要` 这类硬要求数量不少、编号步骤数量不少、每段代码块逐字节相同。任何一项不过就退出码 3，**不会装进去**。

说清楚它的能力边界：它是**结构遗漏初筛**，不是语义等价证明——它挡的是"删掉一条硬要求""截断一段代码""把 `--name` 错认成 `--namespace`"这类结构和字面问题；一段话被改写成同义但不同词，它看不出来。所以路线 2 的改写仍然要你自己（或 AI）负责，工具只保证"不该动的地方没动"。

说实话：网上那些写得紧凑的包（比如 mattpocock/skills），规则压缩只能省 0～4%，因为它们本来就没废话；这种包要真省 Token 只能靠改写。规则压缩的收益主要在"小红书/公众号卖的提示词包"——目录、徽章、重复段落一大堆，实测能省 50% 以上。一个字都没省下来时，工具**不会**生成一份内容一样的 `SKILL.compact.md`。


## 它做了什么

1. **取包**：下载 / 解压到临时目录，先读中央目录再限量解压（防 zip 炸弹：包 ≤ 5 MB、解压 ≤ 20 MB、文件 ≤ 500）
2. **定位技能根**：找出所有 `<名字>/SKILL.md`。DSH 只认顶层这种结构，更深的目录会被拍平
3. **安全检查**：拒绝 `../` 穿越、绝对路径、加密条目；符号链接不落地不跟随；只扫"被选中的技能目录"里的可执行文件（仓库别处的脚本不复制、无所谓）
4. **校验 frontmatter**：必须有 `---` 包裹的 YAML，里面要有 `name` 和 `description`；`name` 只用小写字母/数字/`-`。目录名和 `name` 不一致时，单个技能会按 `name` 归一化（会提示），批量时直接拒绝
5. **落地**：先建这一批的备份目录 `_backup_<时间戳>/`（排他创建，绝不覆盖别的批次），备份旧目录，再在**同一个盘**写临时目录、原子替换；清单在动手之前就写好
6. **写报告 + manifest**：报告给人看，manifest（含安装后文件树指纹）给 `--undo` 看

## 怎么撤销

```bat
%PY% convert.py --list-backups
%PY% convert.py --undo
```

撤销**先核对、后动手**：把这一批的每一项先检查一遍——备份还在不在、目标有没有被人改过（manifest 里存了安装后的文件树指纹）。只要有一项对不上，就**一个文件都不动**，退出码 4，把冲突列出来给你看。

想强行撤：加 `--force`。它会先把当前版本整个另存到 `output/_undo_keep_<时间戳>/`，再还原旧版本——你的修改不会凭空消失。

新增的技能 → 删掉；覆盖过的 → 把备份还原回去。连按两次就退两步。

## Windows 与受限环境（Agent 只能写工作区时）

有些宿主会把 Agent 关在"只能写这个工作区"的沙箱里，`~/.dsh/skills` 写了就报错。这时候**别硬顶**，三条路任选：

```bat
:: 1) 只装到工作区里的一个目录（推荐）
%PY% convert.py 来源 --install --skills-dir "%CD%\.dsh\skills"

:: 2) 装到程序旁边的 local-skills/（不用敲路径）
%PY% convert.py 来源 --install --local

:: 3) 不碰技能目录，只把产物放到指定目录，自己再拷过去
%PY% convert.py 来源 --output-dir "%CD%\output"
```

还有个更省事的办法：设一个环境变量，让程序把**所有会写的东西**（产物、备份、报告、撤销时另存的副本）都放在工作区里：

```bat
set DSH_SKILL_HOME=%CD%\.dsh-skill-home
%PY% convert.py 来源 --install
```

写不进去时程序不会半路崩：它会在动手前先确认目标目录可写，不可写就按退出码 3 停下并给出上面这几条建议；万一真在写入中途被系统拒绝（占位、杀软、权限），已经写进去的部分也能用 `--undo` 回滚——每批的清单在动手之前就落盘了。

其他 Windows 相关的注意点：

- 装到**同一个盘**才能原子替换；程序会把临时目录建在目标目录旁边，避免跨盘 move 变成"复制一半"
- 路径里有中文、空格都没问题（内部全部用 Python 的 Path 处理）
- 杀软偶发锁定刚写完的文件：重试一次即可；持续失败就换 `--output-dir` 先落到工作区

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功（安装 / 输出 / 预演） |
| 2 | 用法错误（参数不对、来源不存在） |
| 3 | 拒绝转换（多个技能、越界路径、有可执行文件、frontmatter 不合法） |
| 4 | 撤销失败 |

## 常见报错

| 报错 | 怎么办 |
|---|---|
| 这个包里包含多个技能，需要指定一个 | 输出里会列候选：加 `--name <名字>` 挑一个，或 `--all` 全转 |
| 压缩包含越界路径 | 这个包可能有问题，别用 |
| 技能 X 里含可执行文件 | 本工具不复制 `.exe/.bat/.ps1/.sh`；把脚本移走再转，或批量时让它被跳过 |
| frontmatter 不合法 | `SKILL.md` 开头少了 `---` 包裹的 YAML |
| name 与目录名不一致 | 用 `--name` 指定目录名 |
| 压缩版文件只能配一个技能用 | `--compact-file` 只针对单个技能：加 `--name` 挑一个，或改用 `--compress` |
| 写不进 DSH 技能目录 | 见上面「Windows 与受限环境」：`--skills-dir` / `--local` / `--output-dir` / `DSH_SKILL_HOME` |
| 有同名技能，装进去会互相覆盖 | 先只装其中一个（`--name`），或让作者把 `name` 改成不同值 |
| 撤销被拒绝（目标被改过 / 备份缺失） | 看完冲突清单再决定：加 `--force` 会先把当前版本另存，再撤销 |

## 安全声明

- 本工具**不会执行**技能包里的任何东西；`.exe/.dll` 这类二进制**永远不复制**，`.sh/.bat/.ps1` 默认也不复制（加 `--allow-scripts` 才复制，且只复制不执行）
- `.py/.js/.ts` 等**辅助代码会原样复制**（很多技能离开它们就干不了活，比如官方 pdf 技能的 `scripts/*.py`），但报告里会逐个点名、命令行也会提示"没有运行它们"——要装之前请确认来源可信
- 只写两处：DSH 技能目录（`--install`）或本目录的 `output/`；每次动手前必然先写 `_backup_<时间戳>/`
- 报告是纯静态 HTML，无外链、无脚本、不联网，只用于展示
- 不做的事：不改你的技能正文、不删章节、不"顺手优化"、不擅自改 `disable-model-invocation`

## 已知限制

- 批量时一个技能出错不会拖垮整批：它会被跳过并在报告里写明原因，其余照常装
- `--allow-scripts` 只是"照拷不跑"，脚本进了技能目录之后 AI 是否会调用，本工具管不了——来源不可信就别加这个开关
- 压缩版的"改写"要靠 AI 或你自己写，工具只负责结构初筛和安装（工具本身不调模型、不联网）。**结构初筛不等于语义等价证明**：它保证没删硬要求、没截代码、没丢参数，但同义改写它看不出来
- 附件只压 `.md`：图片、csv、pdf、json 之类不碰（它们大多是素材，压了会坏）；`SKILL.md` 里写死的附件文件名不会自动改指向
- exe 是单文件、每次启动要解包，第一次启动慢 1～2 秒（正常现象）
- 只在 Windows 上测过（引擎是纯标准库，Linux/macOS 理论上能跑，但没做端到端验证）

## 图标与版本信息（打包相关）

`assets/icon.ico` 已随仓库提交（256/128/64/48/32/16 六个尺寸，手写 ICO 容器：**16–128 用 32bpp BMP 帧 + AND 掩码，只有 256 用 PNG 帧**——全用 PNG 帧时资源管理器的大图标视图会退回通用图标）。
`scripts/version.txt` 提供 exe 的版本信息（FileVersion / FileDescription）。

打包（仓库根目录一条命令，打完打印 SHA256）：

```bat
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

手工等价命令：

```bat
pyinstaller --onefile --console --name "DSH技能转换器" ^
  --icon assets\icon.ico --version-file scripts\version.txt ^
  --distpath dist --workpath .build --specpath .build skill-converter\convert.py
```

> 踩坑记录：
> - **`--icon` / `--version-file` 要写绝对路径**：PyInstaller 以 `--specpath` 为基准解析相对路径，写 `assets\icon.ico` 会变成 `.build\assets\icon.ico`（找不到）。
> - **`scripts\build.ps1` 要存成 UTF-8 带 BOM**：Windows PowerShell 5.1 读无 BOM 的 UTF-8 会按 GBK 解，中文字符串乱码、甚至报 `TerminatorExpectedAtEndOfString`。
> - **换图标后大图标视图还是旧图标**：不是 exe 没写进去，是 Windows 按尺寸分库缓存了旧图（`%LOCALAPPDATA%\Microsoft\Windows\Explorer\iconcache_96.db`、`iconcache_256.db` 等）。`ie4uinit.exe -show` 常常不够，要结束 explorer、删掉 `iconcache_*.db` 和 `thumbcache_*.db`、再启动 explorer 才会重建。

## 免责

这是本地工具，按"现状"提供。技能包内容来自第三方，装之前请自己确认来源可信。因为误操作导致的文件变化，优先用 `--undo` 回滚；回滚不了再手动处理 `_backup_<时间戳>/` 里的备份。
