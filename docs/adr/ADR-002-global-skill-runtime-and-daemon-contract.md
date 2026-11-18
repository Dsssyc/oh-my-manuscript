# ADR-002：跨宿主共享 Skill 安装、稳定运行时与单实例工作台服务契约

- **状态**：已采纳（Accepted）
- **日期**：2026-11-18

## 1. 背景与问题

`oh-my-manuscript`（`omm`）面向任意 AI 编码宿主（Antigravity、Claude Code、Codex 等）协同使用：Agent 在终端调用 CLI，作者在宿主内嵌网页侧栏或浏览器中审阅批注。若沿用传统的“在每篇论文目录下前台运行 `omm serve`、随机分配端口或要求用户手动维护全局 `PATH`”模式，会产生四个问题：
1. 不同 AI 宿主无法零配置发现同一套操作规范与可执行入口；
2. 同时处理多篇手稿或终端关闭重启时，监听端口漂移导致侧栏内嵌地址频繁失效；
3. 源码开发改动或依赖升级会直接干扰正在运行的工作台服务；
4. 在临时 Git 工作树（worktree）中执行检查时容易误覆盖主工作区记录。

本 ADR 借鉴 `hey-my-buddy` 的跨宿主分发与单实例回环服务模式，结合学术手稿场景裁剪掉不必要的多子进程调度复杂度，一次性锁定全局安装布局、稳定运行时、固定端口守护服务与工作区注册契约，并彻底移除原规划中的 `omm serve`。

---

## 2. 决策内容

### 2.1 全局统一安装位置与多宿主自动链接

1. **发行包唯一安装入口**：
   - Python 发行包仅暴露安装动作（发布版通过 `uvx oh-my-manuscript@<version> install`，本地检出构建 wheel 后通过 `uvx --from /abs/path/oh_my_manuscript-<version>-py3-none-any.whl omm install`），在用户明确授权后于宿主沙盒外执行；无需 `sudo`，**绝不修改系统 `PATH`、shell 启动文件或任何宿主自身的模型配置**。
2. **唯一共享 Skill 真身**：
   - 固定安装于 `~/.agents/skills/oh-my-manuscript`，包含：
     - `SKILL.md` 与 `references/`：面向所有编码智能体的完整工作流、语法契约与排障文档；
     - `skill.json`：记录版本号、契约版本与源码提交指纹；
     - `scripts/omm`（macOS / Linux）与 `scripts/omm.cmd`（Windows）：**日常唯一 CLI 启动入口**。
3. **多宿主目录链接（零副本冗余）**：
   - 安装器检测本机已存在的宿主目录，仅为已安装的宿主建立指向 `~/.agents/skills/oh-my-manuscript` 的目录链接（macOS / Linux 使用符号链接 symlink，Windows 使用无需管理员权限的 NTFS 目录联接 junction，不做文件拷贝回退）：
     - `~/.claude` 存在时：`~/.claude/skills/oh-my-manuscript`
     - `~/.gemini/antigravity` 或 `~/.gemini/antigravity-cli` 存在时：`~/.gemini/config/skills/oh-my-manuscript`
   - 若目标位置已被非本项目的目录或链接占用，立即以 `SKILL_TARGET_CONFLICT` 拒绝，绝不覆盖用户资产。

---

### 2.2 内容寻址稳定运行时与全局私有数据根（`OMM_HOME`）

1. **全局根目录**：
   - macOS / Linux：`~/.local/share/oh-my-manuscript/`
   - Windows：`%LOCALAPPDATA%\oh-my-manuscript\`
   - 支持通过 `OMM_STATE_DIR` 与 `OMM_RUNTIME_ROOT` 将状态与运行时完全隔离至测试私有目录；源码开发测试显式设置 `OMM_DEV_SOURCE=1`，严禁测试触碰日常运行中的服务与数据。
2. **稳定运行时（`runtimes/<content_id>/` + `active-runtime.json`）**：
   - `install` 将锁定版本的 Python 运行环境与打包好的前端静态产物 `_web_dist/` 物化至内容寻址目录，校验完成后写入 `READY.json` 并原子切换 `active-runtime.json`。
   - 日常 `scripts/omm` 直接调用 `active-runtime.json` 指向的运行时解释器，无需每次经过 `uv`。
3. **全局状态目录（`state/`，权限 `0700`）**：
   - `workspaces.json`：全局**手稿工作区**注册表（见 `2.3`）。
   - `console-settings.json`：工作台监听端口（默认固定端口 `49638`，亦可通过 `OMM_CONSOLE_PORT` 指定）、界面语言与主题设置。
   - `daemon.json`（权限 `0600`）：当前守护进程实例信息（`pid`、`port`、`runtime_id` 与同用户本地 CLI 认证令牌）。

---

### 2.3 手稿工作区注册表与防工作树劫持契约

1. **论文数据原地自包含**：
   - 每篇论文的 `manuscript.toml`、`sections/`、`reviews/`、`figures/`、`references.bib` 与 `.omm/annotations.json` **始终留在各自的课题 Git 仓库内**（支持位于仓库根目录或如 `<repo>/paper/` 的子目录；同一 Git 仓库内允许存在多个独立手稿子目录）。
   - `manuscript.toml` 包含由 `init` 生成的随机 UUIDv4 `workspace_id`（不含任何主机名、路径或硬件隐私信息），作为跨目录移动的唯一持久身份标识。
2. **显式注册与重定位（防临时 Worktree 污染）**：
   - 仅显式的 `init`、`register` 与 `relocate` 命令允许写入 `state/workspaces.json`：
     - `relocate` 在用户移动或重命名论文目录后，核对新目录 `manuscript.toml` 的 `workspace_id` 与已注册记录一致（且旧路径已不存在或显式确认替换）后更新路径；
     - 在临时 Git worktree 中运行 `check`、`stamp`、`sync`、`annotate` 或指定输出目录的 `build` 时，直接作用于目标路径，**绝不修改 `workspaces.json` 中已注册的主工作区路径**。

---

### 2.4 单实例固定回环守护服务与安全边界（废除 `omm serve`）

1. **单一通信与固定端口模型**：
   - 后台守护进程仅监听数值本地回环 `127.0.0.1:<port>`（默认 `49638`）。**若端口已被占用，直接报错退出，绝不静默切换至随机端口**，确保所有 IDE 侧栏与浏览器书签地址恒定（`http://127.0.0.1:49638/`，通过 `#workspaces/<workspace_id>` 路由切换或直达指定手稿工作区）。
   - **严禁自动弹窗抢焦点**：守护进程启动与 `console` 命令仅确保监听就绪并返回固定 URL，**服务端与 CLI 默认绝不调用系统命令自动弹出浏览器窗口**。
2. **统一 HTTP/JSON + SSE 本地边界（零额外 IPC 依赖）**：
   - 网页端与本地 CLI 共用该单实例回环服务（守护进程统一持有工作区写锁、只读磁盘变更提示监听与 SSE 广播通道，使 `scripts/omm sync` 完成后毫秒级通知已打开的侧栏视图）：
     - **浏览器请求边界**：默认免登录本地访问；强制校验 `Host == 127.0.0.1:<port>`，不返回任何 CORS 响应头；所有写请求强制校验精确 `Origin == http://127.0.0.1:<port>`、`Sec-Fetch-Site` 为 `same-origin` 或未发送，并核验注入页面的内存级 CSRF 令牌。
     - **本地 CLI 请求边界**：`scripts/omm`（尚未启动服务时按需无窗口自启守护进程）读取 `0600` 的 `daemon.json` 携带实例令牌并校验服务端 `runtime_id` 与当前 `active-runtime.json` 一致，防止误连非本运行时的残留进程。

---

### 2.5 统一 CLI 命令集（清理全部 `omm serve` 残留）

- **发行包安装入口**：`omm install`
- **共享 Skill 日常入口（`~/.agents/skills/oh-my-manuscript/scripts/omm`）**：
  - **手稿工作流命令**：`init`、`register`、`relocate`、`stamp`、`sync`、`check`、`checkpoint`、`annotate`、`build`
  - **工作台与受控运维命令**：`console`（确保后台监听就绪并输出固定工作台 URL，或关闭监听）、`health` / `runtime` / `paths`（只读诊断与源码泄漏核查）、`restart` / `stop`（需用户当次明确授权）

---

## 3. 联动清理清单

本 ADR 采纳后，在同一特性分支内一次性将仓库中所有旧 `omm serve` 表述同步替换为单实例 `console` / 全局 Skill 模式（覆盖 `ADR-001`、`README.md`、`README.zh-CN.md`、`src/omm/cli.py`、`src/omm/i18n.py`、`web/src/App.tsx`、`web/src/i18n.ts` 与 `tests/test_cli.py`），不留新旧两套说法。
