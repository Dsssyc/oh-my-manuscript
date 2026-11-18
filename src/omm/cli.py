"""Command-line entry point (`omm`)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from omm import __version__
from omm.i18n import Locale, detect_locale, t

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


def _preparse_locale(argv: Sequence[str]) -> Locale:
    for idx, token in enumerate(argv):
        if token == "--lang" and idx + 1 < len(argv):
            val = argv[idx + 1]
            if val in ("en", "zh-CN"):
                return val
        elif token.startswith("--lang="):
            val = token.split("=", 1)[1]
            if val in ("en", "zh-CN"):
                return val
    return detect_locale()


def build_parser(locale: Locale) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omm",
        description=t("cli.description", locale),
    )
    parser.add_argument("--version", action="version", version=f"omm {__version__}")
    parser.add_argument(
        "--lang",
        choices=("en", "zh-CN"),
        default=locale,
        help=t("cli.lang_help", locale),
    )

    subparsers = parser.add_subparsers(dest="command")
    for cmd in COMMANDS:
        subparsers.add_parser(cmd, help=t(f"cmd.{cmd}.help", locale))

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    locale = _preparse_locale(args_list)
    parser = build_parser(locale)
    ns = parser.parse_args(args_list)

    if ns.command is None:
        parser.print_help()
        return 0

    print(t("err.not_implemented", ns.lang, command=ns.command), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
