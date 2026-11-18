"""Smoke tests for the `omm` CLI entry point and bilingual catalog."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from omm import __version__
from omm.cli import COMMANDS, main
from omm.i18n import MESSAGES
from omm.workspace import load_workspace


def test_i18n_catalog_key_parity() -> None:
    assert set(MESSAGES["en"].keys()) == set(MESSAGES["zh-CN"].keys())
    for cmd in COMMANDS:
        assert f"cmd.{cmd}.help" in MESSAGES["en"]


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
