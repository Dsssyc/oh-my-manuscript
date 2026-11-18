# ADR-001：结构化源稿语法、显式版本同步、批注存储与多端编译契约

- **状态**：已采纳（Accepted）
- **日期**：2026-11-18

## 1. 背景与问题

`oh-my-manuscript`（CLI: `omm`）需要在纯文本 Git 工作流之上同时支撑五项核心能力：
1. 从 `0 → 1` 的**大纲块（Outline Block）**创制与 AI 扩写；
2. 内嵌于 IDE 侧栏或浏览器的工作台阅读视图（防编辑期半途抖动）与全元素**批注（Annotation）**；
3. **参考文献**作为独立顶层版面管理与审阅，不在源稿尾部手写维护冗余文献列表；
4. 基于 Git **审阅点（Checkpoint）**的三级差异对照；
5. 从同一棵语法树确定性编译 Word（Clean `.docx` + 严格符合 ECMA-376 的原生修订 Marked `.docx` + 自动反解回填真实页码行号的回复信）与标准 LaTeX（`.tex` + `latexdiff`）。

本 ADR 一次性锁定**手稿工作区（Manuscript Workspace）**清单契约、**结构化源稿（Structured Source）**语法、`(revision, block_id)` 寻址与前端同步模型、`.omm/annotations.json` 数据结构、独立参考文献编译归属以及数学公式转换链路，不留双轨兼容与隐式回退。

---

## 2. 决策内容

### 2.1 手稿工作区布局、投稿产物装配与 Git 引用模型

```text
<workspace>/
├── manuscript.toml          # 工作区唯一清单与投稿产物装配配置（Git 追踪）
├── references.bib           # 唯一 BibTeX 文献事实源（Git 追踪）
├── sections/                # 正文与附录结构化源稿（Git 追踪，顺序由 manuscript.toml 显式声明）
├── reviews/                 # 返修期审稿追踪项与回复信源稿（Git 追踪，可选）
├── figures/                 # 插图资产（Git 追踪）
└── .omm/
    ├── annotations.json     # 批注及冻结的处理历史快照（Git 追踪）
    └── cache/               # 无头排版位置缓存、工作区写锁与运行态临时文件（.gitignore 忽略）
```

1. **投稿产物装配声明（Deliverables）**：
   - `manuscript.toml` 显式声明每份输出产物（如主文 `manuscript`、独立补充材料 `supplementary`、回复信 `response_letter`）所包含的章节路径有序列表，以及所使用的期刊样式模板（决定章节编号规则、引文 CSL 样式，以及各产物文献表的独立或合并放置位置）。
2. **显式同步版本（`revision`）、阶段分支提交与三级审阅点（Checkpoint）统一复用 Git 对象**：
   - 不发明独立的整数版本号或外部快照库；`revision` 直接采用完整 40 位 Git Commit 对象 ID，实行“小修轻量同步 + 阶段完整提交”两层协同：
     - **每轮小修同步（`refs/omm/published`）**：由显式同步动作（`omm sync` 或工作台“同步”按钮）在全量语法与引用校验通过后，通过临时 Git 索引将受管工作区文件写入提交对象并以 CAS（compare-and-swap）快进更新 `refs/omm/published`，**不污染用户当前分支 `HEAD` 与 `.git/index` 暂存区**，支撑高频审阅与批注关联。
     - **阶段完整提交（分支 `git commit`）**：在一轮修缮定稿需要落入分支历史时，依据当前已校验发布的文稿版本执行完整分支提交（将结构化源稿、图表资产、`references.bib` 与 `.omm/annotations.json` 原子固化入分支历史，并使 `refs/omm/published` 与分支提交对齐）。
     - **上一审阅点（`Last Checkpoint`）**：自定义 Git 引用 `refs/omm/checkpoints/latest`，在用户确认一轮审阅时直接指向当时的已发布 `revision`。
     - **投稿里程碑（`Submission Milestone`）**：打在对应完整分支提交上的标准不可变 Git 标签（如 `refs/tags/v1-submitted`）。
     - **单条批注快照（`Resolution Snapshot`）**：记录于 `.omm/annotations.json` 的处理记录中（同时携带对应 `revision` 与冻结块源码文本，保证常规 `git push` 未推送 `refs/omm/*` 时快照依然独立可读）。

---

### 2.2 `(revision, block_id)` 寻址与版本门控渲染机制

1. **为什么必须同时具备持久 `block_id` 与显式 `revision`**：
   - 纯外部位置/指纹索引在“段落跨章节剪切移动且大幅改写”时无法区分原地重写与移位重写，会导致 `Last Checkpoint` 块配对与回复信 `[#loc:...]` 断链；因此每个可寻址块在源稿内携带持久 `{#block_id}`。
   - 渲染与批注寻址统一以 `(revision, block_id)` 二元组定位某一确定版本下的块状态。
2. **写时赋 ID，发布纯只读（彻底消除后台改写竞态）**：
   - **唯一语法**：所有可寻址块统一用 `{#<block_id>}` 声明标识（标题、普通段落、列表块与行间公式置于块尾，指令块置于起始栅栏头），匹配 `^[A-Za-z][A-Za-z0-9_-]{1,63}$`，全**手稿工作区**（跨正文、附录、回复信）全局唯一；已删除块的 ID 不复用于无关新块。
   - **职责边界**：新增或拆分块时，由写稿方（作者、AI Agent 或带乐观锁的大纲卡片保存接口）直接写出 `{#block_id}`（亦可在编辑完成后显式调用辅助命令 `omm stamp` 一次性为裸块补齐 ID）。**`omm serve` 与 `omm sync` 对磁盘 `.md` 严格只读，绝不在发布或监听过程中反向改写源稿**；存在裸块、重复 ID 或断链时，`omm sync` / `omm check` / `omm build` 直接失败并报告精确文件与行号。
3. **前端版本门控重载（消除 AI 连续编辑期的页面抖动）**：
   - 工作台前端严格绑定单一已发布 `revision` 渲染整套页面（`正文` / `附录` / `审稿追踪项` / `图表总览` / `参考文献`）。
   - AI 在磁盘上连续执行多次局部编辑期间，`omm serve` 仅推送轻量“磁盘有未同步修改”状态提示，**绝不半途重刷 DOM**。
   - 仅当 `omm sync` 校验通过并推进 `refs/omm/published` 至新 `revision` 时，服务端通知前端新 `revision`；前端预取就绪后整版原子切换（若用户正处于划词或填写批注状态，则暂缓切换直至提交或取消，切换后按仍存活的 `block_id` 保持阅读滚动锚点）。
   - **跨版本安全提交批注**：若用户在浏览 `C1` 页面期间后台已发布 `C2`，批注请求携带页面实际所属的 `base_rev = C1`；服务端从 Git 对象库读取 `C1` 中的目标块核验选区并安全落库，绝不串位或丢弃。

---

### 2.3 结构化源稿（Structured Source）语法契约

解析底座采用 `markdown-it-py` 搭配受控语法扩展，构建强类型 AST。

#### （1）交叉引用、文献引用与引用标蓝契约（严格一符一义）
- **工作区块引用（`#`）**：
  - `[#fig-domain]`、`[#tbl-metrics]`、`[#eq-boussinesq]`、`[#sec-method]`：编译为期刊样式逻辑编号（如 `Fig. 1`、`Table 2`、`Eq. (3)`、`Section 2.1`）。禁止引用会被投稿剥离的 `:::{outline}` 块。
  - `[#loc:p-4f9a2c]` 或同一产物内连续块区间 `[#loc:p-4f9a2c..p-8b1e2d]`：专用于回复信请求回填物理位置；在工作台中渲染为分屏联动跳转锚点，在 `omm build` 时必须在包含最终文献表与图表的目标投稿文件（如 Clean 主文或独立附录）完成无头排版后读取真实页码与行号并标明所属文件。若目标块在 Word 排版规则下无独立行号（如浮动插图或表格内部），编译边界直接报错提示改为引用所在段落或逻辑编号，严禁借用相邻块或 `.md` 源码行号充数。
- **文献引用（`@`）**：
  - `[@beven1979; @kirchner2009]` 为括号引文，`@beven1979` 为叙述引文（仅在空白或行首/标点后触发，排除邮箱 `a@b.org`、代码跨度与数学公式内的 `@`）。缺失文献键或 `.bib` 重复键在解析边界报错。
- **结构化引用默认标蓝（渲染与多端导出一致）**：
  - 凡由上述结构化引用语法生成的章节、插图（含子图）、表格、公式交叉引用文字、回复信位置锚点以及文献引用文字（含括号引文与叙述引文），在 **Web 工作台渲染、Word `.docx` 导出（携带内部书签超链接及运行颜色属性；Marked `.docx` 接受全部修订后与 Clean 版字节级一致保留该颜色）与 LaTeX `.tex` 导出（`hyperref` 配色）中默认统一标蓝（默认学术蓝 `#0070C0`）**。
  - 颜色严格由 AST 引用节点驱动，**绝不用正则猜测纯文本中的 `"Figure 1"` 等字面量染蓝**；色值通过 `manuscript.toml` 期刊样式配置的 `link_color` 字段暴露（默认 `"#0070C0"`），若特定期刊要求纯黑白投稿（如设为 `"#000000"`），仅需改一处配置即可全局生效，不污染源稿。

#### （2）独立参考文献版面与原生修订生成
- 源稿 `.md` 中**不手写**文末参考文献章节。
- **工作台顶层「参考文献」版面**：基于 `references.bib` 与当前 `revision` 全量引用图，实时展示格式化条目、在各投稿产物（正文/附录）中的引用频次与反向跳转链接、未被引用的冗余条目及断链告警。
- **编译归属与 Marked `.docx` 双向还原契约**：
  - `omm build` 按 `manuscript.toml` 的产物装配范围收集该产物实际输出块（含正文、图题、表注、表格单元格）引用的键集合，按期刊模板置于指定位置。
  - 生成返修 Marked `.docx` 时，编译器分别对 `Submission Milestone` 与当前 `revision` 完整执行引文与文献表格式化（覆盖作者-年份制的 `2020 → 2020a` 消歧变化与数字制的顺延重排），在同一文献表内按稳定 `citekey` 求确定性保序最长公共子序列（LCS）：新增条目整段 `<w:ins>`、删除条目整段 `<w:del>`、换序条目旧位删新位插、保留条目逐词比对正文与文内引文（同步维护段落标记修订与文献表标题显隐），保证 Word 中“接受全部修订 = 新版 Clean，拒绝全部修订 = 投稿里程碑版”。

#### （3）四类声明式指令块（`:::{kind}`）
单独成行的 `:::` 关闭最近一层打开的指令块（代码跨度、代码块与数学公式内文本不参与配对）。

1. **大纲块（Outline Block）——存在即待扩写，无额外状态字段**
   ```markdown
   :::{outline} {#out-intro-gap}
   - 核心论点：现有网格前处理无法兼顾河网拓扑连通性与局部加密质量。
   - 拟引支撑：[@beven1979]，关联 [#fig-mesh-compare]。
   :::
   ```
   不设 `state` 属性：源稿中只要存在 `:::{outline}` 即视为尚未完成扩写（`omm check` 对此告警，`omm build` 编译投稿产物时一律剥离）；扩写定稿后直接从源稿删除，历史大纲随时通过 Git 历史或审阅点查阅。

2. **插图块（Figure Block，含多子图）**
   ```markdown
   :::{figure} {#fig-panels cols=2}
   ![Terrain elevation](figures/fig02a.png){#fig-panels-a}
   ![Stream network](figures/fig02b.png){#fig-panels-b}

   Catchment overview: [#fig-panels-a] elevation; [#fig-panels-b] extracted channel network.
   :::
   ```
   子图编号 `(a)`、`(b)` 按出现顺序自动生成；多子图各自拥有独立 `block_id`，可直接作为交叉引用与矩形框选批注目标。

3. **三线表块（Table Block）——等列数管道表与矩形唯一合并拼法**
   ```markdown
   :::{table} {#tbl-metrics}
   Model performance across calibration and validation periods.

   | Basin | Calibration (2010–2015) | > | Validation (2016–2020) | > |
   | ^ | $\mathrm{NSE}$ | $\mathrm{KGE}$ | $\mathrm{NSE}$ | $\mathrm{KGE}$ |
   |:---|:---:|:---:|:---:|:---:|
   | Upper | **0.87** | 0.84 | **0.83** | 0.81 |
   | Lower | 0.79 | **0.82** | 0.76 | **0.78** |

   Note: Bold values denote best performance ($p < 0.01$).
   :::
   ```
   - **词法切分**：先识别行内公式（如 `$|x|$`）、代码跨度与转义 `\|`，再切分管道符 `|`；每行逻辑列数必须严格相等。
   - **矩形合并唯一拼法**：任意 `R × C` 合并矩形，**仅允许首行在左上主格右侧连续写 `>`，其余覆盖行在该宽度内全部写 `^`**（字面量写为 `\>`、`\^`）；越界、L 形非矩形合并、跨细横线的纵向合并或其他等价拼法一律在解析边界报错。
   - **结构映射**：表体前唯一段落为表题（Caption），表体后零至多个段落按序作为表注（Table Note）；表头区跨列单元格下方自动生成局部组线（LaTeX `\cmidrule(lr)` / Word 局部底边线）；表体内部全格为 `---` 的行生成组间细横线且不占逻辑数据行号。

4. **审稿追踪项块（Review Item Block）**
   ```markdown
   :::{review-item} {#R1-C3 reviewer="Reviewer 1"}
   :::{comment}
   Why is specific yield $S_y$ treated as constant during peak storm events?
   :::

   We thank the reviewer for raising this point. We added a transient sensitivity analysis in [#sec-discussion] (revised manuscript, [#loc:p-4f9a2c..p-8b1e2d] and [#fig-hydrograph]). {#p-r1c3-reply1}
   :::
   ```
   反向索引严格只从 `:::{comment}` 之后的**作者回复体**中提取 `[#...]`（逻辑关联）与 `[#loc:...]`（物理行号回填），审稿人原话中的引用不计入修改目标；纯解释型回复允许关联零个正文目标块。

---

### 2.4 `.omm/annotations.json` 数据结构与并发锁契约

所有批注读写经同一进程内/文件级工作区互斥锁串行化（重读 → 校验目标项预期状态 → 临时文件原子替换），防止多标签页并发写入互相覆盖：

```json
{
  "schema_version": 1,
  "items": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "block_id": "p-4f9a2c",
      "base_rev": "9c4e1a2b3d4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b",
      "selector": {
        "kind": "text",
        "start": 12,
        "end": 43,
        "quote": "constant specific yield $S_y$"
      },
      "comment": "补充说明该假设仅在基流退水期成立，暴雨期见第 4.2 节敏感性分析。",
      "created_at": "2026-11-18T06:15:00Z",
      "before_block_md": "We assume a constant specific yield $S_y$ across the catchment.",
      "resolutions": [
        {
          "resolved_at": "2026-11-18T06:42:10Z",
          "after_rev": "3f8b7c6d5e4a3b2c1d0e9f8a7b6c5d4e3f2a1b0c",
          "after_block_md": "During baseflow recession, we assume a constant specific yield $S_y$ (storm-event sensitivity is evaluated in [#sec-discussion]).",
          "reopened_at": null
        }
      ]
    }
  ]
}
```

- **`selector`（局部选择器，带标签联合类型）**：
  - `{"kind": "block"}`：整块批注（行间公式块、大纲块、整段/整图/整表）。
  - `{"kind": "text", "start": int, "end": int, "quote": str}`：针对 `base_rev` 中目标块剔除尾部 `{#block_id}` 及前导分隔空白、换行归一为 LF 后的 `.md` 源码，按 **Unicode 码点**计半开区间 `[start, end)`；创建时强制校验 `source[start:end] == quote`，跨块选区或公式局部字形选区在边界直接拒绝（前端将行内公式、引文与交叉引用按原子源码区间对齐）。
  - `{"kind": "rect", "x": float, "y": float, "w": float, "h": float, "asset_sha256": str}`：针对插图或子图持久 `block_id` 实际图像内容区（不含容器留白）的归一化坐标（`0 <= x < 1`、`0 <= y < 1`、`0 < w <= 1 - x`、`0 < h <= 1 - y`），并冻结当时的图像 `asset_sha256`；底图同名替换导致哈希变化时，工作台将该批注标记为“底图已变更待复核”。
  - `{"kind": "cell", "row": int, "col": int}`：`base_rev` 表格逻辑数据网格（不含 `---` 细横线分隔行）的 `0` 基左上主格坐标。
- **派生状态与 `reopen` 历史不变量**：
  - 当前状态无需冗余字段：`resolutions` 为空数组或末项 `reopened_at != null` 时状态即为 `open`，末项 `reopened_at == null` 时状态即为 `resolved`（对外 API 与 CLI 过滤仍只暴露 `open` / `resolved` 二态，严格对齐术语表）。
  - 解决批注时校验 `after_rev` 为已发布 `revision` 并追加记录（若目标块在新版已被删除，`after_block_md` 记为 `null` 以显式表达“块已删除”，删除块本身不会自动将未处理批注标为 `resolved`）；重新打开（reopen）时仅在末项填入 `reopened_at` 时间戳，不擦除历史快照。

---

### 2.5 LaTeX 数学公式编译链路

1. **编号与语法边界**：
   - 行内公式 `$...$` 与行间公式块 `$$...$$` 书写标准 LaTeX 数学语法；`0.1` 约定单个 `$$...$$` 块至多拥有一个外部块级编号（带 `unnumbered` 属性则不编号），需要逐行独立引用的公式拆为多个独立 `$$...$$` 块，公式内部使用 `\tag` 在解析边界直接报错。
2. **三端确定性编译**：
   - **Web 工作台**：`KaTeX` 渲染并携带源码区间属性。
   - **LaTeX 编译器**：输出标准 `\begin{equation}` / `\[...\]` 环境。
   - **Word 编译器**：锁定 `latex2mathml` 版本并在前置识别层保留多行环境语义（将 `aligned` 映射为带对齐点的 `<m:eqArr>`、`cases` 映射为单侧左大括号 `<m:d>` + `<m:m>`，避免退化为无语义普通 `<mtable>` 或产生重复编号列），再由内置 `lxml` 转换器输出 `<m:oMath>` / `<m:oMathPara>`；任何未知命令或被吞标记按 `block_id` 报错。
   - **原生修订模式（Marked `.docx`）**：以单个 `<m:oMath>` 为最小修订原子，整式替换为同级 `<w:del>`（旧公式）紧接 `<w:ins>`（新公式），不在公式内部拆分字符级修订。

---

## 3. 端到端验收契约（Acceptance Bar）

在宣称核心链路跑通前，必须通过一条覆盖五部分联动的端到端测试与桌面实机核验：
1. 在 `C1` 版页面对某段划词批注；AI 将该段跨章剪切重写、在独立附录新增一篇同作者同年文献（触发 `2020a/b` 消歧）、并替换一条 `cases` 分段公式；
2. 未执行 `omm sync` 前前端稳定停留在 `C1`（且此时仍可在 `C1` 页面安全提交批注）；执行 `omm sync` 发布 `C2` 后整版切换，批注可精确回溯快照，`Last Checkpoint` 准确配对跨章移动的块，回复信 `[#loc:...]` 准确回填目标 Clean 文件排版后的页码与行号；
3. 主文与独立附录按模板生成各自准确的文献表；桌面版 Microsoft Word 打开 Marked `.docx` 无损坏告警，公式可原生二次编辑，**“接受所有修订”严格还原 `C2` Clean 版正文、公式与文献次序，“拒绝所有修订”严格还原投稿里程碑版**。
