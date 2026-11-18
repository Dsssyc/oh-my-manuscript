# 术语表 / Glossary

本表定义 `oh-my-manuscript`（CLI: `omm`）的核心领域概念。代码标识符、配置字段、文档与多语言界面（i18n）均严格对齐本表，不引入同义词。

| 中文术语 | English Term | 精确定义与不变量 |
| :--- | :--- | :--- |
| **手稿工作区** | **Manuscript Workspace** | 以单个 Git 仓库承载的一篇学术论文全生命周期目录，包含 `manuscript.toml`、结构化源稿、参考文献库（`references.bib`）、图表资产及 `.omm/` 状态目录。 |
| **结构化源稿** | **Structured Source** | 工作区内的唯一正文事实源（UTF-8 `.md`），采用 **Markdown 正文 + 原生 LaTeX 数学公式 + 声明式结构块**，不存储任何冗余排版缓存。 |
| **块与块标识** | **Block & Block ID** | 语法树的最小可寻址与可批注单元（节、段落、行间公式、表格、插图），拥有跨章节重排保持稳定的 `block_id`，取代易变的整数段号或行号。 |
| **大纲块** | **Outline Block** | 直接写在结构化源稿中的 `:::{outline}` 指令块，记录本节或本段的核心论点与拟引图表/文献：网页端渲染为可编辑大纲卡片，随源稿在 Git 中原子同步，投稿编译时自动剥离（未扩写完则由 `omm check` 告警）。 |
| **批注** | **Annotation** | 挂载在任意 `block_id` 及其局部选择器（文本选区、插图归一化矩形框 `(x, y, w, h)`、表格单元格）上的审阅指令，状态仅分 `open` 与 `resolved`；标为 `resolved` 时冻结**修改前后块快照（Resolution Snapshot）**。 |
| **审阅点** | **Checkpoint** | 基于 Git 轻量引用打下的版本对比基准点，支撑工作台三级差异切换：上一审阅点（`Last Checkpoint`）、单条批注快照（`Resolution Snapshot`）、投稿里程碑（`Submission Milestone`，如 `v1-submitted`）。 |
| **审稿追踪项** | **Review Item** | 同行评议返修阶段的单条审稿人或编辑意见（如 `R1-C3`），将回复信段落与零个或多个目标 `block_id` 双向关联；凡请求物理位置回填的目标块，编译时经无头排版自动反解并回填真实页码与行号。 |
| **目标编译器** | **Target Compiler** | 从同一棵语法树确定性生成投稿产物的后端。`0.1` 规划包含 **Word 编译器**（产出 Clean `.docx`、符合 ECMA-376 的原生修订 Marked `.docx`、回复信与独立切片）与 **LaTeX 编译器**（产出标准 `.tex` 与 `latexdiff`）。 |
