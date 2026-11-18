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
