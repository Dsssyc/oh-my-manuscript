# oh-my-manuscript (`omm`)

[English](./README.md) | 简体中文

**以作者为第一责任人的人机协同科学论文工作台与多端确定性编译器。**

`oh-my-manuscript`（CLI: `omm`）**绝不是**“一键水论文”的全自动生成器，也不是又一个富文本文字处理软件。论文作者始终是学术观点、实验事实与科研诚信的第一责任人；AI 智能体在作者的掌舵下协同完成大纲扩写、多轮文字精修、交叉核验与繁琐的期刊排版及返修编译，把研究者的时间与精力从无关机械细节中解放出来，还给真正的科学问题。

---

## 核心理念

1. **作者主导与学术诚实**：AI 基于作者给定的论点大纲与真实实验资产辅助成文、润色与核查，绝不替学者编造科研结论，也不引入生成式伪造科研插图。
2. **科学论文工作台，而非又一个 Word 编辑器**：专为内嵌在 AI IDE 侧栏网页视图或本地浏览器中协同而生。人类作者通过**大纲块（Outline Block）**与全元素**批注（Annotation）**（覆盖文本选区、插图框选区域、公式与表格单元格）掌舵审阅；磁盘上的纯文本**结构化源稿（Structured Source）**与 Git 始终是唯一事实源。
3. **一源多端确定性编译**：以简洁 Markdown 正文 + 原生 LaTeX 数学公式 + 声明式图表结构块编写一次，确定性编译为实时网页阅读视图、期刊投稿级 Word 文档（Clean `.docx` + 严格符合 ECMA-376 的原生修订 Marked `.docx` + 自动回填页码行号的审稿回复信）以及标准 `.tex`（含 `latexdiff`）。

---

## `0.1` 核心工作流规划

`oh-my-manuscript` 一次安装到全局统一的共享 Agent Skill 目录（`~/.agents/skills/oh-my-manuscript`，自动为已安装的编码智能体建立目录链接，并由单实例守护服务固定监听 `127.0.0.1:49638`），同时让每篇论文的**手稿工作区**原地自包含在各自的课题 Git 仓库中：

- `uvx oh-my-manuscript@<version> install` — 物化独立稳定运行时与全局共享 Skill（唯一日常入口 `~/.agents/skills/oh-my-manuscript/scripts/omm`）。
- `omm init` / `omm register` / `omm relocate` — 初始化、登记或重定位基于 Git 的**手稿工作区**（`manuscript.toml`、分章**结构化源稿**、`references.bib` 与 `.omm/` 状态目录）。
- `omm console` — 确保本地单实例工作台守护服务就绪并返回固定回环地址（支持正文 / 附录 / 审稿追踪项 / 图表总览 / 参考文献五大版面与分屏联动、三级**审阅点**差异对比，以及文字与图表全元素**批注**）。
- `omm stamp` / `omm sync` — 为新增裸块补齐持久 `{#block_id}`，并在全量语法校验通过后发布新的 Git `revision` 触发工作台整版平滑切换（消除 AI 连续编辑期的半途抖动）。
- `omm check` — 静态校验未扩写的**大纲块**、待处理**批注**、断链的 `block_id` 或文献引用，以及期刊格式约束。
- `omm build` — 调用**目标编译器**（结合可选的**投稿里程碑** Git 标签），确定性构建 `.docx` 或 `.tex` 投稿包并自动反解回填真实页码与行号。

核心双语概念定义见 [docs/GLOSSARY.md](./docs/GLOSSARY.md)，核心架构契约见 [ADR-001](./docs/adr/ADR-001-core-representation-and-contracts.md) 与 [ADR-002](./docs/adr/ADR-002-global-skill-runtime-and-daemon-contract.md)。

---

## 当前进度与验证边界

> **当前状态（`0.1.0-dev` —— 核心契约已定稿，编码实现待启动）**：
> - **已实现并验证**：Python 包与 `omm` 命令行外壳（`uv` + `pytest`）、中英双语（`zh-CN` / `en`）i18n 词条机制、五版面 Vite + React + TypeScript 前端工作台外壳（`tsc` 类型检查与生产构建），以及两份基础架构契约（`ADR-001` 与 `ADR-002`）。
> - **尚未开始（`0.1` 待建范围）**：全局共享 Skill 安装器与单实例固定回环守护服务、结构化源稿 AST 解析器与 `(revision, block_id)` 同步引擎、`.omm/annotations.json` 批注存储与 Git 审阅点三级 Diff 引擎，以及 Word（`.docx` OOXML/OMML/原生修订）与 LaTeX（`.tex`）目标编译器。外部旧 `.docx` 逆向拆包导入（`ingest`）留待 `0.2`。

---

## 本地开发快速开始

环境要求：Python `>= 3.12`（使用 [`uv`](https://docs.astral.sh/uv/) 管理）与 Node.js `>= 22`。

```sh
# 运行 CLI 与后端单元测试
uv run omm --help
uv run pytest

# 前端类型检查与构建
npm --prefix web install
npm --prefix web run typecheck
npm --prefix web run build
```
