"""Bilingual (en / zh-CN) CLI message catalog."""

from __future__ import annotations

import os
from typing import Literal

Locale = Literal["en", "zh-CN"]

MESSAGES: dict[Locale, dict[str, str]] = {
    "en": {
        "cli.description": (
            "oh-my-manuscript (omm): Author-first, AI-collaborative scientific manuscript workbench."
        ),
        "cli.lang_help": "Interface language (en or zh-CN; defaults to OMM_LANG or system locale).",
        "cmd.install.help": "Install or repair the global shared Agent Skill and stable runtime.",
        "cmd.init.help": "Initialize and register a Git-backed Manuscript Workspace.",
        "cmd.register.help": "Register an existing Manuscript Workspace in the global registry.",
        "cmd.relocate.help": "Update the path of a moved or renamed Manuscript Workspace.",
        "cmd.stamp.help": "Assign persistent {#block_id} markers to unlabelled blocks.",
        "cmd.sync.help": "Validate sources and publish a new Git revision to the workbench.",
        "cmd.check.help": "Validate Outline Blocks, open Annotations, and reference integrity.",
        "cmd.checkpoint.help": "Advance the Last Checkpoint baseline to the published revision.",
        "cmd.annotate.help": "List, resolve, or reopen workspace Annotations.",
        "cmd.build.help": "Compile Structured Source into .docx or .tex submission packages.",
        "cmd.console.help": "Ensure the single-instance workbench daemon is active and print its URL.",
        "cmd.health.help": "Inspect daemon, contract, and runtime integrity.",
        "cmd.runtime.help": "Inspect active runtime identity and source-leak diagnostics.",
        "cmd.paths.help": "Report global Skill, launcher, runtime, and state directories.",
        "cmd.restart.help": "Restart the background workbench daemon.",
        "cmd.stop.help": "Stop the background workbench daemon.",
        "err.not_implemented": "ERR_NOT_IMPLEMENTED: '{command}' is scheduled for 0.1 implementation.",
    },
    "zh-CN": {
        "cli.description": "oh-my-manuscript (omm)：以作者为第一责任人的人机协同科学论文工作台。",
        "cli.lang_help": "界面语言（en 或 zh-CN；默认读取 OMM_LANG 或系统语言环境）。",
        "cmd.install.help": "安装或修复全局共享 Agent Skill 与稳定运行时。",
        "cmd.init.help": "初始化并登记基于 Git 的手稿工作区（Manuscript Workspace）。",
        "cmd.register.help": "将已有的手稿工作区登记至全局工作区注册表。",
        "cmd.relocate.help": "在手稿工作区目录移动或重命名后更新登记路径。",
        "cmd.stamp.help": "显式为未标记裸块补齐持久 {#block_id} 标识。",
        "cmd.sync.help": "全量校验源稿并发布新的 Git revision 至工作台。",
        "cmd.check.help": "静态校验未扩写的大纲块、待处理批注与引用完整性。",
        "cmd.checkpoint.help": "将上一审阅点（Last Checkpoint）基准推进至当前已发布版本。",
        "cmd.annotate.help": "查看、解决或重新打开手稿批注。",
        "cmd.build.help": "调用目标编译器生成 .docx 或 .tex 投稿产物。",
        "cmd.console.help": "确保本地单实例工作台守护服务就绪并输出固定回环地址。",
        "cmd.health.help": "读取守护服务、契约版本与运行时完整性诊断。",
        "cmd.runtime.help": "查看当前激活运行时身份与源码泄漏检查结果。",
        "cmd.paths.help": "输出全局共享 Skill、启动器、运行时与状态目录路径。",
        "cmd.restart.help": "重启后台单实例工作台守护服务。",
        "cmd.stop.help": "停止后台单实例工作台守护服务。",
        "err.not_implemented": "ERR_NOT_IMPLEMENTED：子命令 '{command}' 待在 0.1 阶段实现。",
    },
}


def detect_locale(explicit: str | None = None) -> Locale:
    raw = (explicit or os.environ.get("OMM_LANG") or os.environ.get("LANG") or "en").lower()
    if raw.startswith("zh"):
        return "zh-CN"
    if raw in ("en", "en-us", "en_us", "en-gb", "en_gb", "c", "c.utf-8", "posix"):
        return "en"
    return "en"


def t(key: str, locale: Locale = "en", **kwargs: object) -> str:
    template = MESSAGES[locale][key]
    return template.format(**kwargs) if kwargs else template


_DIAGNOSTICS = {
    "ERR_ANNOTATIONS_INVALID": (
        "The Annotation file has an invalid structure.",
        "批注文件结构无效。",
    ),
    "ERR_ANNOTATION_OPEN": ("Annotation {id} is open.", "批注 {id} 尚未处理。"),
    "ERR_BIB_DUPLICATE_KEY": ("Duplicate BibTeX key {key}.", "BibTeX 键 {key} 重复。"),
    "ERR_BIB_INVALID": ("Invalid BibTeX entry or field.", "BibTeX 条目或字段无效。"),
    "ERR_BLOCK_ATTRIBUTES_INVALID": ("Invalid block attributes.", "块属性无效。"),
    "ERR_BLOCK_ID_INVALID": ("Invalid block ID.", "块标识无效。"),
    "ERR_BLOCK_ID_MISSING": ("This block needs a block ID.", "此块缺少块标识。"),
    "ERR_CITATION_MISSING": ("No BibTeX entry for {key}.", "BibTeX 中缺少条目 {key}。"),
    "ERR_DIRECTIVE_INVALID": ("Invalid {kind} directive.", "{kind} 指令块无效。"),
    "ERR_DIRECTIVE_UNCLOSED": ("Directive is missing its closing fence.", "指令块缺少闭合栅栏。"),
    "ERR_DIRECTIVE_UNEXPECTED_CLOSE": (
        "Closing fence has no matching directive.",
        "闭合栅栏缺少对应的指令块。",
    ),
    "ERR_DUPLICATE_BLOCK_ID": ("Duplicate block ID.", "块标识重复。"),
    "ERR_FIGURE_ASSET_MISSING": ("Figure asset {asset} was not found.", "找不到插图资产 {asset}。"),
    "ERR_FIGURE_ASSET_UNREADABLE": (
        "Cannot read figure asset {asset}.",
        "无法读取插图资产 {asset}。",
    ),
    "ERR_FIGURE_INVALID": (
        "A Figure Block needs image lines followed by one Caption.",
        "插图块须先列出插图，再写一段图题。",
    ),
    "ERR_FILE_READ": ("Could not read the file. {detail}", "无法读取文件。{detail}"),
    "ERR_FILE_WRITE": ("Could not write the file. {detail}", "无法写入文件。{detail}"),
    "ERR_GIT_INIT": ("Could not initialize the Git repository.", "无法初始化 Git 仓库。"),
    "ERR_LOCATION_CONTEXT": (
        "Use location references in the response letter.",
        "请在回复信中使用物理位置引用。",
    ),
    "ERR_LOCATION_RANGE_AMBIGUOUS": (
        "Range {start}..{end} appears in multiple deliverables.",
        "区间 {start}..{end} 出现在多个投稿产物中。",
    ),
    "ERR_LOCATION_RANGE_DELIVERABLE": (
        "Range {start}..{end} must stay within one deliverable.",
        "区间 {start}..{end} 须位于同一投稿产物中。",
    ),
    "ERR_LOCATION_RANGE_ORDER": (
        "Range {start}..{end} is in reverse document order.",
        "区间 {start}..{end} 的端点顺序颠倒。",
    ),
    "ERR_LOCATION_UNAVAILABLE": (
        "Block {target} has no independent line number.",
        "块 {target} 没有独立行号。",
    ),
    "ERR_MANIFEST_INVALID": ("Invalid manifest field {field}.", "清单字段 {field} 无效。"),
    "ERR_MATH_TAG_FORBIDDEN": (
        r"Remove the \tag command from the formula.",
        r"请移除公式中的 \tag 命令。",
    ),
    "ERR_MATH_UNCLOSED": ("Display math is missing its closing $$.", "行间公式缺少闭合的 $$。"),
    "ERR_OUTLINE_UNEXPANDED": ("Outline Block is awaiting expansion.", "大纲块尚未扩写。"),
    "ERR_REFERENCE_INVALID": ("Invalid block reference {value}.", "块引用 {value} 无效。"),
    "ERR_REFERENCE_MISSING": (
        "Referenced block {target} was not found.",
        "找不到引用的块 {target}。",
    ),
    "ERR_REFERENCE_OUTLINE": (
        "Block {target} is an Outline Block and is stripped from deliverables.",
        "引用的块 {target} 是大纲块，生成投稿产物时会被剥离。",
    ),
    "ERR_REGISTRY_INVALID": ("Invalid Workspace registry.", "手稿工作区注册表无效。"),
    "ERR_REGISTRY_LOCK": (
        "Could not acquire the Workspace registry lock.",
        "无法锁定手稿工作区注册表。",
    ),
    "ERR_REVIEW_COMMENT_REQUIRED": (
        "A Review Item must begin with a comment directive.",
        "审稿追踪项须以 comment 指令块开头。",
    ),
    "ERR_SOURCE_INVALID": (
        "Structured Source contains a NUL character.",
        "结构化源稿含有 NUL 字符。",
    ),
    "ERR_SOURCE_MAPPING": ("Could not map markup to the source.", "无法定位标记对应的源码区间。"),
    "ERR_STAMP_INVALID": (
        "The block ID could not be applied safely.",
        "无法安全地为此块补齐标识。",
    ),
    "ERR_TABLE_COLUMNS_INVALID": (
        "Expected {expected} cells in this row, found {actual}.",
        "本行应有 {expected} 格，实际有 {actual} 格。",
    ),
    "ERR_TABLE_INVALID": ("Invalid Table Block structure.", "三线表块结构无效。"),
    "ERR_TABLE_SPAN_INVALID": ("Invalid rectangular table span.", "表格单元格的矩形合并无效。"),
    "ERR_UTF8_INVALID": ("File is not valid UTF-8.", "文件不是有效的 UTF-8 文本。"),
    "ERR_WORKSPACE_ALREADY_REGISTERED": (
        "Workspace {workspace_id} is already registered at {old_path}.",
        "工作区 {workspace_id} 已登记在 {old_path}。",
    ),
    "ERR_WORKSPACE_EXISTS": ("A Workspace manifest already exists.", "此处已有手稿工作区清单。"),
    "ERR_WORKSPACE_NOT_REGISTERED": (
        "Workspace {workspace_id} has no registration to relocate.",
        "工作区 {workspace_id} 尚无可重定位的登记。",
    ),
    "ERR_WORKSPACE_PATH_INVALID": (
        "Invalid path relative to the workspace root: {value}",
        "相对于工作区根目录的路径无效：{value}",
    ),
    "ERR_WORKSPACE_PATH_REGISTERED": (
        "This path is already registered to another Workspace.",
        "此路径已登记给另一手稿工作区。",
    ),
}

_CLI_MESSAGES = {
    "cli.cwd_help": (
        "Workspace directory (default: current directory).",
        "手稿工作区目录（默认当前目录）。",
    ),
    "cli.title_help": ("Title for the new Manuscript Workspace.", "新手稿工作区的标题。"),
    "cli.replace_help": (
        "Replace the registered path even if the old directory still exists.",
        "即使原目录仍存在，也替换已登记的路径。",
    ),
    "cli.help_help": ("Show this help message and exit.", "显示帮助后退出。"),
    "cli.usage": ("usage: ", "用法："),
    "cli.options": ("options", "选项"),
    "cli.commands": ("commands", "子命令"),
    "cli.arguments_error": (
        "ERR_CLI_ARGUMENTS: {detail}",
        "ERR_CLI_ARGUMENTS：命令行参数无效。请使用 --help 查看用法。",
    ),
    "cli.initialized": ("Initialized Workspace {path}", "已初始化手稿工作区 {path}"),
    "cli.registered": ("Registered Workspace {path}", "已登记手稿工作区 {path}"),
    "cli.relocated": ("Updated Workspace path to {path}", "已更新手稿工作区路径为 {path}"),
    "cli.stamped": ("Assigned {count} block IDs.", "已分配 {count} 个块标识。"),
    "cli.check_ok": ("Workspace checks passed.", "手稿工作区检查通过。"),
    "cli.error": ("error", "错误"),
    "cli.warning": ("warning", "警告"),
    "cli.default_title": ("Untitled Manuscript", "未命名手稿"),
}

for _index, _locale in enumerate(("en", "zh-CN")):
    MESSAGES[_locale].update({key: values[_index] for key, values in _CLI_MESSAGES.items()})
    MESSAGES[_locale].update({key: values[_index] for key, values in _DIAGNOSTICS.items()})
