"""Command-line entry point (`omm`)."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict
from functools import partial
from pathlib import Path

from omm import __version__
from omm.annotations import (
    Annotation,
    BlockContext,
    create_annotation,
    delete_annotation,
    list_annotations,
    reopen_annotation,
    resolve_annotation,
)
from omm.builder import build_workspace
from omm.checker import check_workspace
from omm.daemon import daemon_health, ensure_daemon, notify_daemon, restart_daemon, stop_daemon
from omm.errors import Diagnostic, OmmError
from omm.git_revs import (
    PUBLISHED_REF,
    _Repository,
    lookup_block_at_revision,
    mark_checkpoint,
    resolve_revision,
    sync_workspace,
)
from omm.i18n import Locale, detect_locale, t
from omm.runtime import dispatch_active, inspect_paths, inspect_runtime, install_omm
from omm.stamp import stamp_workspace
from omm.workspace import (
    MANIFEST,
    init_workspace,
    load_workspace,
    register_workspace,
    relocate_workspace,
    resolve_path,
)

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

_WORKSPACE_COMMANDS = {
    "init",
    "register",
    "relocate",
    "stamp",
    "sync",
    "check",
    "checkpoint",
    "annotate",
    "build",
}

_RUNTIME_COMMANDS = {
    "install": install_omm,
    "health": daemon_health,
    "runtime": inspect_runtime,
    "paths": inspect_paths,
    "restart": restart_daemon,
    "stop": stop_daemon,
}


def _leading_command(argv: Sequence[str]) -> str | None:
    """Recognize install before asking the active CLI to parse the other commands."""
    tokens = iter(argv)
    for token in tokens:
        if token in ("--cwd", "--lang"):
            next(tokens, None)
        elif not token.startswith(("--cwd=", "--lang=")):
            return token
    return None


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


def _add_subcommand_options(
    parser: argparse.ArgumentParser, locale: Locale, *, workspace: bool
) -> None:
    parser.add_argument(
        "--lang",
        choices=("en", "zh-CN"),
        default=argparse.SUPPRESS,
        help=t("cli.lang_help", locale),
    )
    if workspace:
        parser.add_argument(
            "--cwd", type=Path, default=argparse.SUPPRESS, help=t("cli.cwd_help", locale)
        )


def _add_json_option(parser: argparse.ArgumentParser, locale: Locale) -> None:
    parser.add_argument("--json", action="store_true", help=t("cli.json_help", locale))


def _add_annotation_actions(parser: argparse.ArgumentParser, locale: Locale) -> None:
    parser.set_defaults(annotate_parser=parser)
    actions = parser.add_subparsers(
        dest="action", parser_class=partial(_LocalizedParser, locale=locale)
    )
    for action in ("list", "create", "resolve", "reopen", "delete"):
        help_text = t(f"cmd.annotate.{action}.help", locale)
        subparser = actions.add_parser(action, help=help_text, description=help_text)
        _add_subcommand_options(subparser, locale, workspace=True)
        _add_json_option(subparser, locale)
        if action == "list":
            subparser.add_argument(
                "--status",
                choices=("open", "resolved"),
                help=t("cli.annotation_status_help", locale),
            )
            subparser.add_argument("--block-id", help=t("cli.annotation_block_id_help", locale))
        elif action == "create":
            subparser.add_argument(
                "--block-id", required=True, help=t("cli.annotation_block_id_help", locale)
            )
            subparser.add_argument(
                "--comment", required=True, help=t("cli.annotation_comment_help", locale)
            )
            subparser.add_argument("--base-rev", help=t("cli.annotation_base_rev_help", locale))
            subparser.add_argument(
                "--selector-json", help=t("cli.annotation_selector_help", locale)
            )
            subparser.add_argument("--id", help=t("cli.annotation_id_help", locale))
        else:
            subparser.add_argument("id", help=t("cli.annotation_id_help", locale))
            if action == "resolve":
                subparser.add_argument(
                    "--after-rev", help=t("cli.annotation_after_rev_help", locale)
                )


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
        _add_subcommand_options(
            subparser, locale, workspace=command in _WORKSPACE_COMMANDS or command == "console"
        )
        if command in _RUNTIME_COMMANDS or command == "console":
            _add_json_option(subparser, locale)
        if command == "init":
            subparser.add_argument(
                "--title", default=t("cli.default_title", locale), help=t("cli.title_help", locale)
            )
        if command == "relocate":
            subparser.add_argument(
                "--replace", action="store_true", help=t("cli.replace_help", locale)
            )
        if command == "sync":
            subparser.add_argument("--expected-rev", help=t("cli.expected_rev_help", locale))
            _add_json_option(subparser, locale)
        if command == "checkpoint":
            subparser.add_argument("--revision", help=t("cli.checkpoint_revision_help", locale))
            _add_json_option(subparser, locale)
        if command == "annotate":
            _add_annotation_actions(subparser, locale)
        if command == "build":
            subparser.add_argument(
                "--target", choices=("docx", "latex", "all"), default="all",
                help=t("cli.build_target_help", locale),
            )
            subparser.add_argument(
                "--out-dir", type=Path, metavar="PATH", help=t("cli.build_out_dir_help", locale)
            )
            subparser.add_argument(
                "--revision", metavar="REV", help=t("cli.build_revision_help", locale)
            )
            subparser.add_argument(
                "--milestone", metavar="REV", help=t("cli.build_milestone_help", locale)
            )
            subparser.add_argument(
                "--layout-pdf", action="append", default=[], metavar="[TARGET:]DELIVERABLE=PATH",
                help=t("cli.build_layout_pdf_help", locale),
            )
            _add_json_option(subparser, locale)
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


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False))


def _run_build(root: Path, args: argparse.Namespace, locale: Locale) -> None:
    layout_pdfs = {}
    for value in args.layout_pdf:
        key, sep, path = value.partition("=")
        target, qualified, name = key.partition(":")
        if not qualified:
            target, name = args.target, key
        if not sep or not name.strip() or not path:
            raise OmmError("ERR_BAD_REQUEST")
        if target not in {"docx", "latex"} or args.target not in {target, "all"}:
            raise OmmError("ERR_BAD_REQUEST")
        layout_pdfs.setdefault(target, {})[name] = Path(path)
    result = build_workspace(
        root, target=args.target, out_dir=args.out_dir, revision=args.revision,
        milestone=args.milestone, layout_pdfs=layout_pdfs,
    )
    if args.json:
        _print_json(result)
    else:
        print(t("cli.build_done", locale, **result))


def _console(root: Path) -> dict:
    workspace_id = None
    try:
        if (root / MANIFEST).is_file():
            workspace_id = load_workspace(root).workspace_id
    except OmmError:
        pass
    result = ensure_daemon()
    result["workspace_id"] = workspace_id
    if workspace_id is not None:
        result["url"] += f"#workspaces/{workspace_id}"
    return result


def _print_runtime_result(command: str, result: dict, locale: Locale, json_output: bool) -> None:
    if json_output:
        _print_json(result)
    elif command in ("console", "restart"):
        print(result["url"])
    elif command == "stop":
        print(t("cli.daemon_stopped" if result["stopped"] else "cli.daemon_absent", locale))
    elif command == "install":
        print(t("cli.installed", locale, **result))
    else:
        for key, value in result.items():
            print(f"{key}={value}")


def _annotation_json(annotation: Annotation) -> dict[str, object]:
    return {**asdict(annotation), "status": annotation.status}


def _annotation_revision(root: Path, rev_or_ref: str | None) -> str:
    if rev_or_ref is not None:
        return resolve_revision(root, rev_or_ref)
    try:
        return resolve_revision(root, PUBLISHED_REF)
    except OmmError as exc:
        if exc.code == "ERR_REVISION_NOT_FOUND":
            raise OmmError("ERR_NO_PUBLISHED_REVISION", path=root) from exc
        raise


def _annotation_selector(raw: str | None, block_id: str) -> dict[str, object]:
    if raw is None:
        return {"kind": "block"}
    try:
        selector = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise OmmError("ERR_ANNOTATION_SELECTOR_MISMATCH", block_id=block_id) from exc
    if not isinstance(selector, dict):
        raise OmmError("ERR_ANNOTATION_SELECTOR_MISMATCH", block_id=block_id)
    return selector


def _run_annotate(root: Path, args: argparse.Namespace, locale: Locale) -> int:
    def lookup(revision: str, block_id: str) -> BlockContext | None:
        entry = lookup_block_at_revision(root, revision, block_id)
        return None if entry is None else BlockContext(entry.block, entry.asset_sha256)

    match args.action:
        case None:
            args.annotate_parser.print_help()
            return 2
        case "list":
            annotations = list_annotations(root, status=args.status, block_id=args.block_id)
            if args.json:
                _print_json([_annotation_json(ann) for ann in annotations])
            elif not annotations:
                print(t("cli.annotation_empty", locale))
            else:
                for ann in annotations:
                    status = t(f"cli.annotation_status.{ann.status}", locale)
                    print(
                        t(
                            "cli.annotation_row",
                            locale,
                            id=ann.id,
                            block_id=ann.block_id,
                            status=status,
                            comment=ann.comment,
                        )
                    )
            return 0
        case "create":
            annotation = create_annotation(
                root,
                block_id=args.block_id,
                base_rev=_annotation_revision(root, args.base_rev),
                selector=_annotation_selector(args.selector_json, args.block_id),
                comment=args.comment,
                lookup=lookup,
                annotation_id=args.id,
            )
        case "resolve":
            after_rev = _annotation_revision(root, args.after_rev)
            if args.after_rev is not None:
                published = _annotation_revision(root, None)
                if not _Repository(root).is_ancestor(after_rev, published):
                    raise OmmError("ERR_REVISION_NOT_PUBLISHED", path=root, revision=after_rev)
            annotation = resolve_annotation(root, args.id, after_rev=after_rev, lookup=lookup)
        case "reopen":
            annotation = reopen_annotation(root, args.id)
        case "delete":
            delete_annotation(root, args.id)
            notify_daemon("annotations_changed", root)
            if args.json:
                _print_json({"id": args.id})
            else:
                print(t("cli.annotation_deleted", locale, id=args.id))
            return 0

    notify_daemon("annotations_changed", root)
    if args.json:
        _print_json(_annotation_json(annotation))
    else:
        print(t(f"cli.annotation_{args.action}_ok", locale, id=annotation.id))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure) and getattr(stream, "encoding", None) != "utf-8":
            with suppress(OSError, ValueError):
                reconfigure(encoding="utf-8")
    args_list = list(sys.argv[1:] if argv is None else argv)
    locale = _preparse_locale(args_list)
    command = _leading_command(args_list)
    args = None
    try:
        forwarded = dispatch_active(command, args_list)
        if forwarded is not None:
            return forwarded
        parser = build_parser(locale)
        args = parser.parse_args(args_list)
        command, locale = args.command, args.lang
        if args.command is None:
            parser.print_help()
            return 0
        root = args.cwd if args.cwd is not None else Path.cwd()
        if args.command == "console" or args.command in _RUNTIME_COMMANDS:
            result = (
                _console(resolve_path(root)) if args.command == "console"
                else _RUNTIME_COMMANDS[args.command]()
            )
            _print_runtime_result(args.command, result, locale, args.json)
            return 0
        match args.command:
            case "init":
                workspace = init_workspace(root, args.title)
                notify_daemon("workspaces_changed", workspace.workspace_id)
                print(t("cli.initialized", locale, path=workspace.root))
            case "register":
                workspace = register_workspace(root)
                notify_daemon("workspaces_changed", workspace.workspace_id)
                print(t("cli.registered", locale, path=workspace.root))
            case "relocate":
                workspace = relocate_workspace(root, replace=args.replace)
                notify_daemon("workspaces_changed", workspace.workspace_id)
                print(t("cli.relocated", locale, path=workspace.root))
            case "stamp":
                print(t("cli.stamped", locale, count=stamp_workspace(root)))
            case "sync":
                result = sync_workspace(root, expected_old_rev=args.expected_rev)
                if result.changed:
                    notify_daemon("revision_published", root, revision=result.revision)
                if args.json:
                    _print_json({"revision": result.revision, "changed": result.changed})
                else:
                    message = "cli.sync_changed" if result.changed else "cli.sync_unchanged"
                    print(t(message, locale, revision=result.revision))
            case "check":
                result = check_workspace(root)
                for diagnostic in result.diagnostics:
                    _print_diagnostic(diagnostic, locale)
                if not result.ok:
                    return 1
                print(t("cli.check_ok", locale))
            case "checkpoint":
                checkpoint = mark_checkpoint(root, revision=args.revision)
                notify_daemon("checkpoint_updated", root, checkpoint=checkpoint)
                if args.json:
                    _print_json({"checkpoint": checkpoint})
                else:
                    print(t("cli.checkpoint_set", locale, checkpoint=checkpoint))
            case "annotate":
                return _run_annotate(root, args, locale)
            case "build":
                _run_build(root, args, locale)
    except OmmError as exc:
        _print_diagnostic(exc.diagnostic, locale)
        json_output = "--json" in args_list if args is None else getattr(args, "json", False)
        if (command == "console" or command in _RUNTIME_COMMANDS) and json_output:
            _print_json({
                "code": exc.code,
                "message": t(exc.code, locale, **dict(exc.diagnostic.params)),
                **exc.diagnostic.params,
            })
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
