"""Command-line entry point (`omm`)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from functools import partial
from pathlib import Path

from omm import __version__
from omm.checker import check_workspace
from omm.errors import Diagnostic, OmmError
from omm.i18n import Locale, detect_locale, t
from omm.stamp import stamp_workspace
from omm.workspace import init_workspace, register_workspace, relocate_workspace

COMMANDS: tuple[str, ...] = (
    "install",
    "init",
    "register",
    "relocate",
    "stamp",
    "sync",
    "check",
    "checkpoint",
    "annotate",
    "build",
    "console",
    "health",
    "runtime",
    "paths",
    "restart",
    "stop",
)

_WORKSPACE_COMMANDS = {"init", "register", "relocate", "stamp", "check"}


def _preparse_locale(argv: Sequence[str]) -> Locale:
    locale = detect_locale()
    for idx, token in enumerate(argv):
        if token == "--lang" and idx + 1 < len(argv):
            val = argv[idx + 1]
        elif token.startswith("--lang="):
            val = token.split("=", 1)[1]
        else:
            continue
        if val in ("en", "zh-CN"):
            locale = val
    return locale


class _LocalizedParser(argparse.ArgumentParser):
    def __init__(self, *, locale: Locale, **kwargs: object) -> None:
        super().__init__(add_help=False, allow_abbrev=False, **kwargs)
        self.locale = locale
        self._positionals.title = t("cli.commands", locale)
        self._optionals.title = t("cli.options", locale)
        self.add_argument("-h", "--help", action="help", help=t("cli.help_help", locale))

    def _get_formatter(self) -> argparse.HelpFormatter:
        formatter = super()._get_formatter()
        add_usage = formatter.add_usage

        def localized_usage(usage, actions, groups, prefix=None):
            return add_usage(usage, actions, groups, t("cli.usage", self.locale))

        formatter.add_usage = localized_usage
        return formatter

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, t("cli.arguments_error", self.locale, detail=message) + "\n")


def build_parser(locale: Locale) -> argparse.ArgumentParser:
    parser = _LocalizedParser(
        locale=locale,
        prog="omm",
        description=t("cli.description", locale),
    )
    parser.add_argument("--version", action="version", version=f"omm {__version__}")
    parser.add_argument(
        "--lang", choices=("en", "zh-CN"), default=locale, help=t("cli.lang_help", locale)
    )
    parser.add_argument("--cwd", type=Path, help=t("cli.cwd_help", locale))

    subparsers = parser.add_subparsers(
        dest="command", parser_class=partial(_LocalizedParser, locale=locale)
    )
    for command in COMMANDS:
        help_text = t(f"cmd.{command}.help", locale)
        subparser = subparsers.add_parser(command, help=help_text, description=help_text)
        subparser.add_argument(
            "--lang",
            choices=("en", "zh-CN"),
            default=argparse.SUPPRESS,
            help=t("cli.lang_help", locale),
        )
        if command in _WORKSPACE_COMMANDS:
            subparser.add_argument(
                "--cwd", type=Path, default=argparse.SUPPRESS, help=t("cli.cwd_help", locale)
            )
        if command == "init":
            subparser.add_argument(
                "--title", default=t("cli.default_title", locale), help=t("cli.title_help", locale)
            )
        if command == "relocate":
            subparser.add_argument(
                "--replace", action="store_true", help=t("cli.replace_help", locale)
            )
    return parser


def _print_diagnostic(diagnostic: Diagnostic, locale: Locale) -> None:
    location = ""
    if diagnostic.path is not None:
        location = str(diagnostic.path)
        if diagnostic.line is not None:
            location += f":{diagnostic.line}"
        location += ": "
    block_id = f" [{diagnostic.block_id}]" if diagnostic.block_id is not None else ""
    severity = t(f"cli.{diagnostic.severity}", locale)
    message = t(diagnostic.code, locale, **dict(diagnostic.params))
    print(f"{location}{severity} {diagnostic.code}{block_id}: {message}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser(_preparse_locale(args_list))
    args = parser.parse_args(args_list)
    locale = args.lang
    if args.command is None:
        parser.print_help()
        return 0
    if args.command not in _WORKSPACE_COMMANDS:
        print(t("err.not_implemented", locale, command=args.command), file=sys.stderr)
        return 2

    root = args.cwd if args.cwd is not None else Path.cwd()
    try:
        match args.command:
            case "init":
                workspace = init_workspace(root, args.title)
                print(t("cli.initialized", locale, path=workspace.root))
            case "register":
                workspace = register_workspace(root)
                print(t("cli.registered", locale, path=workspace.root))
            case "relocate":
                workspace = relocate_workspace(root, replace=args.replace)
                print(t("cli.relocated", locale, path=workspace.root))
            case "stamp":
                print(t("cli.stamped", locale, count=stamp_workspace(root)))
            case "check":
                result = check_workspace(root)
                for diagnostic in result.diagnostics:
                    _print_diagnostic(diagnostic, locale)
                if not result.ok:
                    return 1
                print(t("cli.check_ok", locale))
    except OmmError as exc:
        _print_diagnostic(exc.diagnostic, locale)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
