"""Smoke tests for the `omm` CLI entry point and bilingual catalog."""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path

import pytest

from omm import __version__
from omm.cli import COMMANDS, _print_diagnostic, main
from omm.errors import OmmError
from omm.git_revs import LATEST_CHECKPOINT_REF, PUBLISHED_REF
from omm.i18n import MESSAGES, Locale
from omm.workspace import ANNOTATIONS, load_workspace


def test_i18n_catalog_key_parity() -> None:
    assert set(MESSAGES["en"].keys()) == set(MESSAGES["zh-CN"].keys())
    for cmd in COMMANDS:
        assert f"cmd.{cmd}.help" in MESSAGES["en"]


def test_i18n_catalog_covers_source_diagnostics() -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "omm"
    codes = set()
    for path in source.rglob("*.py"):
        if path == source / "i18n.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # Error codes also reach diagnostics through helpers and named constants.
        codes.update(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and re.fullmatch(r"ERR_[A-Z0-9_]+", node.value)
        )
    for locale, messages in MESSAGES.items():
        assert codes <= messages.keys(), locale


@pytest.mark.parametrize("locale", ["en", "zh-CN"])
@pytest.mark.parametrize("code", ["ERR_FILE_READ", "ERR_GIT_COMMAND"])
@pytest.mark.parametrize("params", [{}, {"revision": "a" * 40}])
def test_cli_diagnostic_without_optional_detail(
    code: str,
    locale: Locale,
    params: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _print_diagnostic(OmmError(code, path=Path("manuscript.toml"), **params).diagnostic, locale)
    output = capsys.readouterr().err
    assert code in output
    assert "{detail}" not in output
    assert not output.endswith(" \n")


def test_cli_help_en_and_zh(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--lang", "en"]) == 0
    out_en = capsys.readouterr().out
    assert "Author-first" in out_en
    assert "console" in out_en
    assert "serve" not in out_en

    assert main(["--lang", "zh-CN"]) == 0
    out_zh = capsys.readouterr().out
    assert "第一责任人" in out_zh
    assert "console" in out_zh


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_unimplemented_subcommand_returns_error_code(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--lang", "en", "console"])
    assert rc == 2
    assert "ERR_NOT_IMPLEMENTED" in capsys.readouterr().err


def test_removed_serve_subcommand_is_rejected() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["serve"])
    assert exc.value.code == 2


def test_workspace_lifecycle_and_read_only_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "我的 论文"
    state = tmp_path / "state"
    monkeypatch.setenv("OMM_STATE_DIR", str(state))
    monkeypatch.setenv("OMM_DEV_SOURCE", "1")
    assert main(["--cwd", str(root), "init", "--title", "新手稿", "--lang", "zh-CN"]) == 0
    assert "已初始化手稿工作区" in capsys.readouterr().out
    assert (root / ".git").is_dir()
    assert "/.omm/cache/" in (root / ".gitignore").read_text(encoding="utf-8")
    assert (root / "figures").is_dir()
    assert (root / "references.bib").is_file()
    registry = state / "workspaces.json"
    original_registry = registry.read_bytes()

    assert main(["--lang", "en", "check", "--cwd", str(root)]) == 1
    assert "warning ERR_OUTLINE_UNEXPANDED" in capsys.readouterr().err
    assert registry.read_bytes() == original_registry

    section = root / "sections" / "01-introduction.md"
    section.write_bytes(
        "# 已编号 {#sec-introduction}\r\n\r\n😀 𝑄 and $x$.\r\n\r\n- One\r\n- Two\r\n".encode()
    )
    assert main(["stamp", "--cwd", str(root), "--lang", "en"]) == 0
    assert "Assigned 2 block IDs." in capsys.readouterr().out
    stamped = section.read_bytes()
    assert b"\r" not in stamped
    assert b"{#sec-introduction}" in stamped
    assert re.search(rb"\{#p-[a-f0-9]{6}\}", stamped)
    assert re.search(rb"\{#li-[a-f0-9]{6}\}", stamped)
    assert main(["stamp", "--cwd", str(root)]) == 0
    assert section.read_bytes() == stamped
    assert main(["check", "--cwd", str(root), "--lang", "zh-CN"]) == 0
    assert "手稿工作区检查通过" in capsys.readouterr().out
    assert registry.read_bytes() == original_registry

    new_root = root.rename(tmp_path / "重命名 论文")
    monkeypatch.chdir(new_root)
    assert main(["check"]) == 0
    assert main(["register"]) == 1
    assert "ERR_WORKSPACE_ALREADY_REGISTERED" in capsys.readouterr().err
    assert registry.read_bytes() == original_registry
    assert main(["relocate"]) == 0
    workspace = load_workspace(new_root)
    assert json.loads(registry.read_text(encoding="utf-8"))["workspaces"] == {
        workspace.workspace_id: {"path": str(new_root.resolve())}
    }
    assert main(["register"]) == 0


def test_relocate_requires_explicit_replacement_if_old_path_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = tmp_path / "original"
    alternate = tmp_path / "alternate"
    monkeypatch.setenv("OMM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMM_DEV_SOURCE", "1")
    assert main(["init", "--cwd", str(original)]) == 0
    original.rename(alternate)
    original.mkdir()
    assert main(["relocate", "--cwd", str(alternate)]) == 1
    assert "ERR_WORKSPACE_ALREADY_REGISTERED" in capsys.readouterr().err
    assert main(["relocate", "--cwd", str(alternate), "--replace"]) == 0
    registry = tmp_path / "state" / "workspaces.json"
    workspace = load_workspace(alternate)
    assert json.loads(registry.read_text(encoding="utf-8"))["workspaces"] == {
        workspace.workspace_id: {"path": str(alternate.resolve())}
    }


@pytest.fixture
def cli_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OMM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMM_DEV_SOURCE", "1")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty.gitconfig"))
    monkeypatch.setenv("GIT_DEFAULT_HASH", "sha1")
    return tmp_path / "我的 论文"


def test_annotate_without_action_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["annotate", "--lang", "zh-CN"]) == 2
    output = capsys.readouterr()
    assert "用法：" in output.out
    assert "omm annotate" in output.out
    assert "{list,create,resolve,reopen,delete}" in output.out
    assert not output.err


def test_cli_requires_published_revision_for_defaults(
    cli_workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    common = ["--cwd", str(cli_workspace), "--lang", "en"]
    assert main([*common, "init"]) == 0
    capsys.readouterr()
    for command in (
        ["checkpoint", "--json"],
        ["annotate", "create", "--block-id", "sec-introduction", "--comment", "Review.", "--json"],
        ["annotate", "resolve", "550e8400-e29b-41d4-a716-446655440000", "--json"],
    ):
        assert main([*common, *command]) == 1
        output = capsys.readouterr()
        assert "ERR_NO_PUBLISHED_REVISION" in output.err
        assert not output.out


def test_cli_revision_and_annotation_lifecycle(
    cli_workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = cli_workspace

    def run(*command: str, expected: int = 0) -> pytest.CaptureResult[str]:
        code = main(["--cwd", str(root), "--lang", "en", *command])
        output = capsys.readouterr()
        assert code == expected, output
        return output

    run("init")
    section = root / "sections" / "01-introduction.md"
    heading = "sec-introduction"
    quote = "😀 流域"
    before = f"{quote}数据支持原结论。"
    after = "更新后的数据支持修订结论。"
    section.write_text(
        f"# Introduction {{#{heading}}}\n\n{before}\n", encoding="utf-8", newline="\n"
    )
    run("stamp")
    stamped = section.read_text(encoding="utf-8")
    match = re.search(r"\{#(p-[a-f0-9]{6})\}", stamped)
    assert match is not None
    paragraph = match[1]

    first_sync = json.loads(run("sync", "--json").out)
    first = first_sync["revision"]
    assert re.fullmatch(r"[a-f0-9]{40}", first)
    assert first_sync["changed"] is True
    assert json.loads(run("sync", "--expected-rev", first, "--json").out) == {
        "revision": first,
        "changed": False,
    }
    assert main(["sync", "--cwd", str(root), "--lang", "zh-CN"]) == 0
    assert f"当前版本仍为 {first}" in capsys.readouterr().out
    assert json.loads(run("checkpoint", "--json").out) == {"checkpoint": first}
    assert "No Annotations found." in run("annotate", "list").out

    create = ("annotate", "create")
    note_id = "550e8400-e29b-41d4-a716-446655440000"
    whole = json.loads(
        run(
            *create, "--block-id", heading, "--comment", "Review title.", "--id", note_id, "--json"
        ).out
    )
    assert whole["id"] == note_id
    assert whole["base_rev"] == first
    assert whole["selector"] == {"kind": "block"}
    assert whole["status"] == "open"

    selector = {"kind": "text", "start": 0, "end": len(quote), "quote": quote}
    text_note = (*create, "--block-id", paragraph, "--comment", "检查这段文字。")
    for invalid in ("{", "[]"):
        output = run(*text_note, "--selector-json", invalid, expected=1)
        assert f"ERR_ANNOTATION_SELECTOR_MISMATCH [{paragraph}]" in output.err
    text_note += ("--selector-json", json.dumps(selector, ensure_ascii=False), "--json")
    selected = json.loads(run(*text_note).out)
    assert selected["selector"] == selector
    assert selected["before_block_md"] == before
    warning_output = run("check", expected=1).err
    assert warning_output.count("warning ERR_ANNOTATION_OPEN") == 2
    assert whole["id"] in warning_output and selected["id"] in warning_output

    section.write_text(stamped.replace(before, after), encoding="utf-8", newline="\n")
    second_sync = json.loads(run("sync", "--expected-rev", first, "--json").out)
    second = second_sync["revision"]
    assert second_sync["changed"] is True
    assert second != first
    late = json.loads(run(*text_note, "--base-rev", LATEST_CHECKPOINT_REF).out)
    assert late["base_rev"] == first
    assert late["before_block_md"] == before
    assert late["selector"] == selector
    assert json.loads(run("checkpoint", "--revision", PUBLISHED_REF, "--json").out) == {
        "checkpoint": second,
    }

    open_notes = json.loads(
        run("annotate", "list", "--block-id", paragraph, "--status", "open", "--json").out
    )
    assert open_notes == [selected, late]
    assert main(["annotate", "list", "--cwd", str(root), "--lang", "zh-CN"]) == 0
    assert f"{late['id']}［未处理］{paragraph}：检查这段文字。" in capsys.readouterr().out

    unpublished = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test Author",
            "-c",
            "user.email=author@example.com",
            "commit-tree",
            "--no-gpg-sign",
            f"{second}^{{tree}}",
            "-p",
            second,
            "-m",
            "Unpublished",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        encoding="utf-8",
    ).stdout.strip()
    output = run("annotate", "resolve", note_id, "--after-rev", unpublished, expected=1)
    assert "ERR_REVISION_NOT_PUBLISHED" in output.err
    for note in (whole, selected, late):
        revision = ("--after-rev", PUBLISHED_REF) if note is late else ()
        resolved = json.loads(run("annotate", "resolve", note["id"], *revision, "--json").out)
        assert resolved["status"] == "resolved"
        (snapshot,) = resolved["resolutions"]
        assert snapshot["after_rev"] == second
        expected_snapshot = "# Introduction" if note is whole else after
        assert snapshot["after_block_md"] == expected_snapshot
        assert snapshot["reopened_at"] is None
    assert "Workspace checks passed." in run("check").out

    reopened = json.loads(run("annotate", "reopen", late["id"], "--json").out)
    assert reopened["status"] == "open"
    (snapshot,) = reopened["resolutions"]
    assert snapshot["reopened_at"] is not None
    assert snapshot["after_block_md"] == after
    assert json.loads(run("annotate", "delete", late["id"], "--json").out) == {"id": late["id"]}
    assert json.loads(run("annotate", "list", "--status", "open", "--json").out) == []
    store = root / ANNOTATIONS
    stored = json.loads(store.read_text(encoding="utf-8"))
    assert {item["id"] for item in stored["items"]} == {whole["id"], selected["id"]}
    assert all("status" not in item for item in stored["items"])

    stored["items"][0]["id"] = "invalid-uuid"
    store.write_text(json.dumps(stored) + "\n", encoding="utf-8", newline="\n")
    assert "error ERR_ANNOTATIONS_INVALID" in run("check", expected=1).err
    assert "error ERR_ANNOTATIONS_INVALID" in run("sync", expected=1).err
