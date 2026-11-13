# DSH 外部技能极速转换器

> 把别人写的技能包（Claude Skills / 提示词包 / GitHub 仓库 / zip）**一条命令**变成 DSH 技能：自动压缩省 Token、装之前先快照、出问题一键回滚。
>
> 小白不用懂 Python，也不用装环境——**在 DSH 里说一句"帮我把这个转成技能"就行**。

---

## 下载

| 拿到什么 | 去哪拿 |
|---|---|
| `DSH-Skill-Converter.exe`（Windows，约 9 MB，自带 Python 运行时） | 👉 **[直接下载最新版](https://github.com/waaagquuux/dsh-skill-converter/releases/latest/download/DSH-Skill-Converter.exe)**（旁边有同名 `.sha256`）；也可以进 [Releases](https://github.com/waaagquuux/dsh-skill-converter/releases/latest) 挑带版本号的旧版 |
| 源码（自己跑 / 改） | `git clone` 本仓库，或下载源码 zip |

> **exe 没有代码签名**。所以 Windows 可能弹「Windows 已保护你的电脑」——点「更多信息」→「仍要运行」即可。校验方式：把 Release 里的 `.sha256` 和本地文件的 SHA256 比一下（`Get-FileHash .\DSH-Skill-Converter.exe -Algorithm SHA256`）。有签名之后会更新这条说明。
>
> 名字说明：Release 里有两个名字：`DSH-Skill-Converter.exe`（永远指向最新版）和 `DSH-Skill-Converter-v版本号.exe`（钉死某一版）——GitHub 附件不接受中文文件名，你自己用 `scripts\build.ps1` 构建出来的叫 `dist\DSH技能转换器.exe`——**同一个程序**，只是文件名叫法不同。

---

## 它解决三个痛点

| 痛点 | 这个工具怎么处理 |
|---|---|
| 技能包装不上、格式不对（frontmatter、目录层级、编码） | 自动识别技能根、校验 `name`/`description`、拍平目录层级、给出中文检查清单 |
| 提示词又臭又长，装进去把上下文吃光 | 规则压缩 + AI 改写两条路，压完还做六项红线校验（命令、参数、硬性要求、步骤一个都不能少） |
| 手一抖装错、覆盖了原来的技能，环境崩了 | 每次落地前自动快照 `_backup_<时间戳>/`，`--undo` 一键回滚，有清单可查 |

---

## 三种用法（**不用点 exe**）

### ① 在 DSH 里说一句话 —— 推荐

装了 `skill-converter` 技能之后，直接跟我说：

> 帮我把 `https://github.com/anthropics/skills` 这个包装上

我会自动：预演（不写任何文件）→ 按固定格式汇报「技能名 / 包含的工具和功能 / 行数 / 装到哪 / 报告在哪」→ **停下来问你要不要装** → 你点头才落盘。

这条路走的是 `convert.py`，用 DSH 自带的 Python，比 exe 快（exe 每次启动要解包 8 MB，等 1~2 秒）。

### ② 双击 exe —— 给没有 DSH、没装 Python 的人

`DSH-Skill-Converter.exe`（从 Release 下）或自己构建的 `dist\DSH技能转换器.exe`（约 9.1 MB，单文件，**自带 Python 运行时**）。

双击后是问答模式：把 zip / 文件夹**拖进窗口**或粘 GitHub 链接 → 选「演习 / 装进技能目录 / 只输出」→ 结束按回车关闭。

把 exe 发给朋友，他不需要装 Python、不需要命令行。

### ③ 命令行 —— 给想批量/自动化的人

```bat
DSH-Skill-Converter.exe "https://github.com/anthropics/skills" --all --dry-run
DSH-Skill-Converter.exe "C:\path\my-skill.zip" --name my-skill --install
DSH-Skill-Converter.exe --undo
DSH-Skill-Converter.exe --list-backups
```

---

## 装它自己（30 秒）

1. 确认 DSH 技能目录：Windows 是 `C:\Users\<你>\.dsh\skills`，Linux/macOS 是 `~/.dsh/skills`。
2. 把本仓库的 `skill-converter\` 目录里的 `SKILL.md` + `README.md` 复制进 `<技能目录>\skill-converter\`。
3. 打开 DSH，在对话里说"帮我转一个技能包"——技能清单会**秒级热加载**，不用重启。

> 想让它自己装自己？先在本仓库根目录跑 `python skill-converter\convert.py . --name skill-converter --install`。

---

## 实测数据（不是宣传口径）

| 用例 | 结果 |
|---|---|
| `anthropics/skills`（官方技能库，16 个技能） | 16 个通过、2 个按规则跳过（含 shell 脚本 / frontmatter 名字对不上） |
| `mattpocock/skills`（29 个技能） | 26 个通过，3 个因 `.sh` 脚本被拦（加 `--allow-scripts` 才能装） |
| 冗余技能包压缩（含目录、徽章图、重复段落、致谢） | 省 62.9% Token |
| 已经很紧凑的技能包 | 规则压缩只有 0~4%，**工具会如实告诉你"没得省"**，不编数字 |
| AI 改写 `grilling` | 543 → 430 Token（省 20.8%），六项红线校验全过 |
| 附件（`refs/` 里的 .md） | 262 → 95 Token（省 63.7%） |

---

## 安全边界（说清楚，不吹）

- **绝不执行**包里的任何东西：不跑脚本、不装依赖、不联网执行。
- **二进制永远拒绝**：`.exe/.dll/.msi/.sys/...` 一个都不复制。
- **shell 脚本默认不复制**（`.sh/.bat/.ps1`），要用得显式加 `--allow-scripts`，且只复制不执行。
- **Python/JS 辅助代码会原样复制**（很多技能靠它干活），但报告里会逐个点名、并说明"本工具没有运行它们"。
- **防路径穿越**：zip 先读中央目录再限量解压，`../`、绝对路径、符号链接一律不落地。
- **限额**：压缩包 ≤ 5 MB、解压后 ≤ 20 MB、文件数 ≤ 500。
- **只写两个地方**：技能目录，或你指定的 `output/`。不写系统目录、不改 PATH、不碰注册表。

---

## 后悔药

每次安装前自动写 `_backup_<时间戳>/`（含 `manifest.json`）：

```bat
DSH-Skill-Converter.exe --undo            :: 撤最近一批
DSH-Skill-Converter.exe --list-backups    :: 看有哪些批次、各撤什么
```

覆盖同名技能 → `--undo` 还原旧版本；新增技能 → `--undo` 把它删掉。两条路径都实测过。

---

## 报告（本地 HTML，不联网）

每次运行生成 `report.html`（预演是 `report-dry-run.html`，被拒是 `report-rejected.html`）：

- 结论条（做了什么 / 没做什么）
- 检查清单（体积、文件数、路径安全、脚本、辅助代码、frontmatter）
- 技能卡片：**可搜索（按 `/`）、可筛选、可折叠**
- 包结构树、变更清单、**一键复制的撤销命令**

单文件、纯静态、不联网、不执行任何脚本，可以直接发给别人看。

---

## 仓库结构

```
README.md                 你正在看的这个
LICENSE                   MIT
skill-converter/
  convert.py              引擎（单文件，只用标准库）
  SKILL.md                装进 DSH 技能目录的入口
  README.md               详细用法 / 参数表
scripts/
  build.ps1               一条命令打包（打 exe、打印 SHA256）
  version.txt             exe 版本信息资源
assets/
  icon.ico                exe 图标
```

自己构建：

```bat
pip install pyinstaller
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

产物在 `dist\DSH技能转换器.exe`。不装 PyInstaller 也能用：直接 `python skill-converter\convert.py ...`（只需 Python 3.10+，无第三方依赖）。

---

## 常见错误对照

| 报错 | 原因 | 怎么办 |
|---|---|---|
| `这个包里包含多个技能，需要指定一个或全部转换` | 仓库里有多个 `SKILL.md` | 加 `--name 技能名` 或 `--all` |
| `frontmatter 里的 name 与目录名不一致` | 作者改了目录名 | 让作者改，或用 `--name` 指定 |
| `含脚本文件（默认不复制）` | 技能带 `.sh/.ps1` | 确认来源可信后加 `--allow-scripts` |
| `含可执行文件` | 技能带 `.exe/.dll` | 无解，**不要装** |
| `压缩版改坏了关键内容，拒绝使用` | AI 改写时删了命令/参数/硬要求 | 重新改写，校验必须全过 |

更多细节见 [skill-converter/README.md](skill-converter/README.md)。

---

## 免责

这是个**格式转换 + 压缩**工具，不能判断技能内容安不安全、会不会骗 AI 说错话。装来源不明的技能包，风险由你承担。只处理 `.md` 文本，不改代码逻辑，不保证压缩后语义 100% 等价——所以压缩版一律**另存一份**给你对比，不偷偷替换原文。
