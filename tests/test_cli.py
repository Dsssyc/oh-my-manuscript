"""Smoke tests for the `omm` CLI entry point and bilingual catalog."""

from __future__ import annotations

import pytest

from omm import __version__
from omm.cli import COMMANDS, main
from omm.i18n import MESSAGES


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
