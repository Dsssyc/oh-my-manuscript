# oh-my-manuscript (`omm`)

[English](./README.md) | 简体中文

**以作者为第一责任人的人机协同科学论文工作台与多端确定性编译器。**

`oh-my-manuscript`（CLI: `omm`）**绝不是**“一键水论文”的全自动生成器，也不是又一个富文本文字处理软件。论文作者始终是学术观点、实验事实与科研诚信的第一责任人；AI 智能体在作者的掌舵下协同完成大纲扩写、多轮文字精修、交叉核验与繁琐的期刊排版及返修编译，把研究者的时间与精力从机械细节中解放出来，还给真正的科学问题。

---

## 核心理念

1. **作者主导与学术诚实**：AI 基于作者给定的论点大纲与真实实验资产辅助成文、润色与核查，绝不替学者编造科研结论，也不引入生成式伪造科研插图。
2. **科学论文工作台，而非又一个 Word 编辑器**：专为在本地浏览器或内嵌网页视图中与编码智能体（CLI、桌面应用或 IDE）并排协同而生。人类作者通过**大纲块（Outline Block）**与全元素**批注（Annotation）**（覆盖文本选区、插图框选区域、公式与表格单元格）掌舵审阅；磁盘上的纯文本**结构化源稿（Structured Source）**与 Git 始终是唯一事实源。
3. **一源多端确定性编译**：以简洁 Markdown 正文 + 原生 LaTeX 数学公式 + 声明式图表结构块编写一次，确定性编译为实时五视图阅读工作台、期刊投稿级 Word 文档（Clean `.docx` + 严格符合 ECMA-376 的原生修订 Marked `.docx` + 自动回填页码行号的审稿回复信）以及标准 `.tex`（Clean + AST 原生 `latexdiff` 兼容修订稿）。

---

## 安装（`0.1.0.dev0`）

`oh-my-manuscript` 当前处于 `0.1.0.dev0`，**尚未发布至 PyPI**。请直接从源码安装（需 Python `>= 3.12` 与 [`uv`](https://docs.astral.sh/uv/)、Node.js `>= 22` 以及 Git）：

```sh
git clone https://github.com/Dsssyc/oh-my-manuscript.git
cd oh-my-manuscript

# 1. 构建本地 React 工作台前端静态产物
npm --prefix web install
npm --prefix web run build

# 2. 同步 Python 依赖并物化隔离稳定运行时与全局共享 Skill
uv sync
uv run omm install
```

`omm install` 会一次性物化内容寻址的独立稳定运行时与全局共享 Agent Skill：

- **规范 Skill 目录与唯一日常启动器**：
  - macOS / Linux：`~/.agents/skills/oh-my-manuscript/scripts/omm`
  - Windows：`%USERPROFILE%\.agents\skills\oh-my-manuscript\scripts\omm.cmd`
- **编码智能体自动链接**（POSIX 使用符号链接，Windows 经 `mklink /J` 建立 NTFS 目录联接）：若宿主目录存在，自动链接至 `~/.claude/skills/oh-my-manuscript` 与 `~/.gemini/config/skills/oh-my-manuscript`，遇到已有冲突资产时原样保留并报错，绝不覆盖。

待 `0.1` 正式发布至 PyPI 后，即可直接通过 `uvx oh-my-manuscript@<version> install` 一键安装。

---

## `0.1` 核心工作流与 CLI

每篇论文的**手稿工作区**原地自包含在各自的 Git 仓库中，所有工作区共用固定监听 `127.0.0.1:49638` 的单实例回环工作台守护服务。全部命令支持 `--lang en|zh-CN`；工作区命令支持 `--cwd PATH`，并按需提供 `--json` 机器可读输出。

```sh
# 初始化手稿工作区并拉起五视图工作台
omm init --title "My Paper"
omm stamp
omm check
omm sync
omm console

# 标记投稿里程碑、返修同步并编译 Clean + Marked 投稿产物
git tag v1-submitted refs/omm/published
# ... 编辑结构化源稿与审稿回复信 ...
omm sync
omm build
```

### 手稿工作区命令

| 命令 | 说明 |
| :--- | :--- |
| `omm init [--title TITLE]` | 创建并登记基于 Git 的**手稿工作区**（`manuscript.toml`、`sections/`、`references.bib`、`.omm/`）。 |
| `omm register` / `omm relocate [--replace]` | 登记已有工作区检出，或更新目录移动后的登记路径。 |
| `omm stamp` | 为新增的可寻址块补齐持久 `{#block_id}` 标识。 |
| `omm check` | 校验**结构化源稿**、未扩写的**大纲块**、**批注**、断链的 `block_id`/文献引用及插图资产。 |
| `omm sync [--expected-rev REV] [--json]` | 全量校验通过后向 `refs/omm/published` 发布新版本快照，触发工作台整版原子切换（消除 AI 编辑中途抖动）。 |
| `omm checkpoint [--revision REV] [--json]` | 推进 `Last Checkpoint` 引用，支撑三级**审阅点**差异对比。 |
| `omm annotate <list\|create\|resolve\|reopen\|delete>` | 管理本地**批注**（支持整块、文本选区、插图框选区域、表格单元格），标记处理时冻结**修改前后块快照**。 |
| `omm build [--target docx\|latex\|all] ...` | 调用**目标编译器**确定性构建 Clean 与 Marked `.docx` / `.tex` 投稿包，并反解回填 `[#loc:...]` 物理位置。 |

### 运行时与工作台守护命令

| 命令 | 说明 |
| :--- | :--- |
| `omm install` | 物化独立稳定运行时、启动脚本与全局共享 Skill 链接。 |
| `omm console [--cwd PATH]` | 确保无窗口单实例工作台守护服务（`127.0.0.1:49638`）就绪并输出固定本地地址（正文 / 附录 / 审稿追踪项 / 图表总览 / 参考文献五大视图）。 |
| `omm health` / `omm runtime` / `omm paths` | 检查守护服务健康状态、激活运行时隔离情况（`source_leak: false`）与全局路径布局。 |
| `omm restart` / `omm stop` | 重启守护服务切换至最新激活运行时，或安全停止守护服务。 |

---

## 手稿工作区与结构化源稿

每个**手稿工作区**包含 `manuscript.toml`、UTF-8 Markdown **结构化源稿**、`references.bib`、插图资产与 `.omm/`（存放本地批注与默认 `.omm/build/` 输出）。

| 结构 | 语法约定 |
| :--- | :--- |
| 可寻址块 | 在标题、段落、列表或行间公式末尾标注 `{#block-id}`（行间公式写作 `$$ ... $$ {#eq-id}` 或 `{#eq-id unnumbered}`）。 |
| 指令块 | `:::{outline} {#out-id}`、`:::{figure} {#fig-id cols=2}`、`:::{table} {#tbl-id}`（标准三线表，支持 `>` / `^` 单元格合并与 `---` 中线），均以 `:::` 闭合。 |
| **审稿追踪项** | `:::{review-item} {#R1-C1 reviewer="Reviewer 1"}`，以 `:::{comment} ... :::` 开头，随后接作者带块标识的回复段落。 |
| 交叉引用与文献 | 逻辑编号引用 `[#block-id]`、回复信物理页行引用 `[#loc:p-id]` 或 `[#loc:p-start..p-end]`、BibTeX 引用 `[@key1; @key2]` 或叙事引用 `@key`。 |

完整 CLI 选项与错误码见 [src/omm/skill_bundle/references/syntax-and-cli.md](./src/omm/skill_bundle/references/syntax-and-cli.md)，规范双语术语见 [docs/GLOSSARY.md](./docs/GLOSSARY.md)，架构契约见 [ADR-001](./docs/adr/ADR-001-core-representation-and-contracts.md) 与 [ADR-002](./docs/adr/ADR-002-global-skill-runtime-and-daemon-contract.md)。

---

## 投稿构建（`omm build`）

- **Clean 与 Marked 产物**：默认 `--target all`（同时构建 `docx` 与 `latex`）并写入 `.omm/build/`（工作区内的自定义 `--out-dir` 必须位于 `.omm/<subdir>` 下以保护源稿）。默认读取 `refs/omm/published`（要求源稿已同步，或显式传入 `--revision REV`），并自动选取目标版本之前的最新祖先 `v*-submitted` **投稿里程碑**标签（可用 `--milestone REV` 显式指定），生成 `*.clean.*` 与 `*.marked.*`。
- **物理页码与行号回填（`[#loc:...]`）**：
  - 当回复信使用 `[#loc:...]` 时，可传入对应 Clean 产物导出的带页边行号 PDF。使用默认 `--target all` 时需按目标格式加前缀：
    ```sh
    omm build \
      --layout-pdf docx:manuscript=word-numbered.pdf \
      --layout-pdf latex:manuscript=tex-numbered.pdf
    ```
    仅编译单一目标（`--target docx` 或 `--target latex`）时可省略前缀（`--layout-pdf manuscript=numbered.pdf`）。
  - 未为被引用的产物提供 `--layout-pdf` 时，Word 目标调用无头 LibreOffice（`soffice`），LaTeX 目标调用无头 TeX 引擎读取 `lineno` `.aux` 标签。若二者均不可用，或目标块在排版下无独立行号，编译边界会直接报错，绝不估算行号。
  - 不含 `[#loc:...]` 的工作区无需安装 LibreOffice 或 TeX。

---

## 当前进度与验证边界

`0.1` 全部四个阶段均已在 `main` 分支完成实现（`0.1.0.dev0`）：覆盖结构化源稿解析/校验/打标、显式 `omm sync`、三级审阅点 Diff、本地批注、五视图 React 工作台与无窗口回环守护服务、全局共享 Skill 安装，以及 Clean/Marked Word `.docx` + LaTeX `.tex` 编译与 `[#loc:...]` 反解。

**已完成自动化与实机验证的范围：**

- **macOS（Python 3.13, Node.js 22）**：`uv run ruff check` 零告警；`uv run pytest -q` 通过（`505 passed, 1 skipped`，唯一跳过项为 Windows 专属 NTFS Junction 用例）；前端 `npm --prefix web run typecheck` 与 `vite build` 通过；隔离空 `HOME` 离线 `.whl` 安装与 `scripts/omm build` 全链路通过。
- **Windows 11 x64 NTFS（Python 3.12，默认 `LongPathsEnabled=0`）**：实机跑通 `uv run ruff check` 与 `uv run pytest -q`（`0 failed, 0 skipped`，覆盖 `>260` 字符扩展长路径运行时安装与 `_omm.pth` 原生扩展导入），并在含中文与空格路径下通过离线 `omm.cmd install`（`chcp 1252`、`PYTHONUTF8=1`）、NTFS 目录联接及 `omm.cmd build` 端到端验证。
- **Word OOXML 与修订痕迹**：生成的每份 `.docx` 均经内置严格 OPC/WML 校验器（`verify_docx_package`）检查，验收样稿 XML 部件通过官方 [ECMA-376](https://ecma-international.org/publications-and-standards/standards/ecma-376/) XSD 校验，自动化测试验证了 Marked `.docx` 接受/拒绝全部修订后与对应 Clean 文档双向等价。

**明确的未验边界与后续规划：**

- **分发渠道**：尚未发布至 PyPI，现阶段需从源码安装。
- **平台覆盖**：尚未在原生 Linux 环境下执行实机测试。
- **外部桌面排版软件**：位置反解测试基于真实定位 PDF 字节流与仿真引擎；自动化检查未运行桌面版 Microsoft Word、LibreOffice 或完整 TeX Live。正式投稿前请在桌面 Word 或你的 TeX 工具链中复核字体、分页与修订显示效果。
- **`0.2` 规划**：旧版 `.docx` 手稿导入（`omm ingest`）。

---

## 本地开发

环境要求：Python `>= 3.12`（`uv`）、Node.js `>= 22` 与 Git。

```sh
# 安装依赖并运行 Python 静态检查与测试
uv sync
uv run ruff check
uv run pytest

# 直接从当前源码检出运行 CLI（跳过已安装的激活运行时分发）
OMM_DEV_SOURCE=1 uv run omm --help

# 前端类型检查与构建（输出至 src/omm/_web_dist/）
npm --prefix web install
npm --prefix web run typecheck
npm --prefix web run build
```
