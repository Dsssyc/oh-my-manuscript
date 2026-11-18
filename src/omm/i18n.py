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
        "cmd.init.help": "Initialize a Git-backed Manuscript Workspace.",
        "cmd.serve.help": "Start the local live-reloading manuscript workbench.",
        "cmd.check.help": "Validate Outline Blocks, open Annotations, and reference integrity.",
        "cmd.build.help": "Compile Structured Source into .docx or .tex submission packages.",
        "err.not_implemented": "ERR_NOT_IMPLEMENTED: '{command}' is scheduled for 0.1 implementation.",
    },
    "zh-CN": {
        "cli.description": "oh-my-manuscript (omm)：以作者为第一责任人的人机协同科学论文工作台。",
        "cli.lang_help": "界面语言（en 或 zh-CN；默认读取 OMM_LANG 或系统语言环境）。",
        "cmd.init.help": "初始化基于 Git 的手稿工作区（Manuscript Workspace）。",
        "cmd.serve.help": "启动本地热同步科学论文工作台。",
        "cmd.check.help": "校验未扩写的大纲块、待处理批注与引用完整性。",
        "cmd.build.help": "调用目标编译器生成 .docx 或 .tex 投稿产物。",
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
