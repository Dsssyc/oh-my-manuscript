"""Installed CLI and detached workbench integration tests in isolated directories."""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import shutil
import socket
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http.client import HTTPConnection
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

import omm
from omm import runtime, workspace
from omm.daemon import ensure_daemon, stop_daemon
from omm.errors import OmmError
from omm.runtime import configured_port, install_omm, state_directory, write_json_object
from omm.server import CONTRACT_VERSION, DEFAULT_PORT, LOOPBACK
from omm.workspace import fs_path, load_workspace, resolve_path

_SKILL_DIGEST = "8e8a4819bd105459cad2d48c494de71f5acb899c0e85e2bfe408e7d07fb3fd6a"
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _ephemeral_socket() -> socket.socket:
    while True:
        listener = socket.socket()
        listener.bind((LOOPBACK, 0))
        if listener.getsockname()[1] != DEFAULT_PORT:
            return listener
        listener.close()


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "Host Home"
    home.mkdir()
    monkeypatch.setenv("OMM_HOST_HOME", str(home))
    monkeypatch.setenv("OMM_HOME", str(tmp_path / "omm"))
    monkeypatch.setenv("OMM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OMM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.delenv("OMM_SKILL_ROOT", raising=False)
    monkeypatch.delenv("_OMM_ACTIVE_EXEC", raising=False)
    monkeypatch.setenv("OMM_DEV_SOURCE", "1")
    with _ephemeral_socket() as listener:
        monkeypatch.setenv("OMM_CONSOLE_PORT", str(listener.getsockname()[1]))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty.gitconfig"))
    monkeypatch.setenv("GIT_DEFAULT_HASH", "sha1")
    return resolve_path(tmp_path)


def _cli(
    *args: str, launcher: Path | None = None, code: int = 0, stdout: str | None = None
) -> dict:
    if launcher is None:
        command = [sys.executable, "-m", "omm.cli", *args]
    elif os.name == "nt":
        arguments = subprocess.list2cmdline([str(launcher), *args])
        command = f'cmd.exe /d /s /v:off /c "{arguments}"'
    else:
        command = [str(launcher), *args]
    result = subprocess.run(
        command,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=60,
        creationflags=_NO_WINDOW,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == code, (result.stdout, result.stderr)
    if stdout is not None:
        assert result.stdout == stdout
    return json.loads(result.stdout) if result.stdout.startswith("{") else {}


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _launcher(skill: Path) -> Path:
    return skill / "scripts" / ("omm.cmd" if os.name == "nt" else "omm")


def test_install_and_launch_stable_runtime(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = isolated / "Host Home"
    (host / ".claude").mkdir()
    (host / ".gemini" / "antigravity").mkdir(parents=True)
    monkeypatch.delenv("OMM_DEV_SOURCE")
    installed = _cli("install", "--json")
    assert installed["created"]
    runtime = isolated / "runtime" / "runtimes" / installed["runtime_id"]
    ready = _read(runtime / "READY.json")
    assert _read(isolated / "runtime" / "active-runtime.json") == ready
    assert ready["schema_version"] == 1
    assert ready["runtime_id"] == installed["runtime_id"]
    assert Path(ready["web_dist"]).is_dir()

    skill = host / ".agents" / "skills" / "oh-my-manuscript"
    assert hashlib.sha256((skill / "SKILL.md").read_bytes()).hexdigest() == _SKILL_DIGEST
    assert (skill / "references" / "syntax-and-cli.md").is_file()
    assert _read(skill / "skill.json") == {
        "schema_version": 1,
        "runtime_id": ready["runtime_id"],
        "name": "oh-my-manuscript",
        "version": omm.__version__,
        "contract_version": CONTRACT_VERSION,
    }
    assert (skill / "scripts" / "omm").read_bytes() == (
        f'#!/bin/sh\nexec "{ready["python"].replace(chr(92), chr(92) * 2)}" -m omm.cli "$@"\n'
    ).encode()
    assert (skill / "scripts" / "omm.cmd").read_bytes() == (
        b'@echo off\r\n"%~dp0.runtime\\venv\\Scripts\\python.exe" -X utf8 -m omm.cli %*\r\n'
        b"exit /b %ERRORLEVEL%\r\n"
    )
    runtime_link = skill / "scripts" / ".runtime"
    assert runtime_link.is_junction() if os.name == "nt" else runtime_link.is_symlink()
    assert resolve_path(runtime_link) == runtime
    if os.name != "nt":
        assert stat.S_IMODE((skill / "scripts" / "omm").stat().st_mode) == 0o755
    for link in (host / ".claude" / "skills", host / ".gemini" / "config" / "skills"):
        target = link / "oh-my-manuscript"
        assert target.is_junction() if os.name == "nt" else target.is_symlink()
        assert resolve_path(target) == skill

    launcher = _launcher(skill)
    inspected = _cli("runtime", "--lang", "zh-CN", "--json", launcher=launcher)
    assert inspected["source_leak"] is False
    assert inspected["dev_source"] is False
    package = Path(inspected["omm_file"]).parent
    assert package.is_relative_to(runtime)
    assert not list(package.parent.glob("*editable*"))
    assert not (package.parent / "pytest").exists()
    assert (package.parent / "mdurl").is_dir()
    assert _cli("paths", "--json", launcher=launcher)["skill_root"] == str(skill)
    health = _cli("health", "--json", launcher=launcher)
    assert health["status"] == "unavailable" and health["source_leak"] is False
    assert not state_directory().exists()
    with monkeypatch.context() as forwarded:
        forwarded.setenv("_OMM_ACTIVE_EXEC", "1")
        leak = _cli("runtime", "--json", code=1)
    assert leak["code"] == "ERR_RUNTIME_SOURCE_LEAK" and leak["source_leak"] is True

    ready_stat = (runtime / "READY.json").stat()
    repeated = _cli("install", "--json")
    assert not repeated["created"]
    assert repeated["runtime_id"] == installed["runtime_id"]
    assert (runtime / "READY.json").stat().st_mtime_ns == ready_stat.st_mtime_ns


def test_install_and_reuse_deep_runtime_with_utf8(isolated, monkeypatch):
    padding = "运行时-" + "x" * (190 - len(str(isolated)))
    runtime_root = isolated / padding
    monkeypatch.setenv("OMM_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("PYTHONUTF8", "1")
    monkeypatch.delenv("OMM_DEV_SOURCE")
    (isolated / "Host Home" / ".claude").mkdir()

    try:
        installed = _cli("install", "--json")
        assert installed["created"]
        directory = runtime_root / "runtimes" / installed["runtime_id"]
        launcher = _launcher(Path(installed["skill_root"]))
        inspected = _cli("runtime", "--json", launcher=launcher)
        assert inspected["runtime_id"] == installed["runtime_id"]
        assert inspected["python"] == installed["python"]
        assert inspected["dev_source"] is False and inspected["source_leak"] is False
        assert len(inspected["python"]) < 250
        assert len(inspected["omm_file"]) > 260
        assert Path(inspected["omm_file"]).is_relative_to(directory)
        for output in (installed, inspected):
            assert all("\\\\?\\" not in path for path in (
                output[key] for key in ("python", "web_dist", "skill_root", "omm_file")
                if key in output
            ))

        resource = Path(installed["web_dist"]).parents[1].joinpath(
            "lxml", "isoschematron", "resources", "xsl", "iso-schematron-xslt1",
            "iso_dsdl_include.xsl",
        )
        assert len(str(resource)) > 280 and fs_path(resource).is_file()
        ready_file = fs_path(directory / "READY.json")
        ready = _read(ready_file)
        assert _read(fs_path(runtime_root / "active-runtime.json")) == ready
        assert ready["python"] == installed["python"] and ready["web_dist"] == installed["web_dist"]
        ready_mtime = ready_file.stat().st_mtime_ns
        assert _cli("install", "--json") == {**installed, "created": False}
        assert ready_file.stat().st_mtime_ns == ready_mtime
    finally:
        if fs_path(runtime_root).exists():
            shutil.rmtree(fs_path(runtime_root))


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (r"C:\outer\..\here", r"\\?\C:\here"),
        (r"\\server\share\run", r"\\?\UNC\server\share\run"),
        (r"\\?\C:\already", r"\\?\C:\already"),
    ],
)
def test_fs_path_uses_extended_windows_drive_and_unc_paths(monkeypatch, path, expected):
    monkeypatch.setattr(workspace, "_WINDOWS_PATHS", True)
    monkeypatch.setattr(workspace, "abspath", ntpath.abspath)
    monkeypatch.setattr(workspace, "Path", PureWindowsPath)
    assert str(fs_path(PureWindowsPath(path))) == expected


def test_windows_junction_commands_capture_localized_output_as_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="nt", path=os.path, environ=os.environ))
    monkeypatch.setattr(runtime.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    calls = []

    def run(command, **kwargs):
        assert kwargs["capture_output"]
        assert not kwargs.get("text")
        calls.append(kwargs["env"]["OMM_LINK"])
        return subprocess.CompletedProcess(command, 0, "已创建联接".encode("gbk"), b"")

    monkeypatch.setattr(runtime.subprocess, "run", run)
    runtime._link_skill(tmp_path / "skill-link", tmp_path / "skill")
    runtime._link_runtime(tmp_path, tmp_path / "runtime")
    assert calls == [str(tmp_path / name) for name in ("skill-link", ".runtime")]


@pytest.mark.parametrize("kind", ["directory", "foreign_link", "dangling_link"])
def test_host_target_conflict_preserves_assets(isolated: Path, kind: str) -> None:
    path = isolated / "Host Home" / ".claude" / "skills" / "oh-my-manuscript"
    path.parent.mkdir(parents=True)
    asset = isolated / "foreign"
    if kind == "directory":
        asset = path
    asset.mkdir()
    if kind != "dangling_link":
        (asset / "owner.txt").write_text("Keep this file.", encoding="utf-8")
    if kind != "directory":
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/d", "/c", "mklink", "/J", str(path), str(asset)],
                capture_output=True,
                check=True,
                creationflags=_NO_WINDOW,
            )
        else:
            path.symlink_to(asset, target_is_directory=True)
    if kind == "dangling_link":
        asset.rmdir()

    assert _cli("install", "--json", code=1)["code"] == "ERR_SKILL_TARGET_CONFLICT"
    assert os.path.lexists(path) or path.is_junction()
    if kind != "dangling_link":
        assert (path / "owner.txt").read_text(encoding="utf-8") == "Keep this file."
    assert not (isolated / "runtime" / "active-runtime.json").exists()


def _next_event(response, expected: str) -> dict:
    for _ in range(40):
        line = response.readline()
        assert line, "event stream closed"
        if line.startswith(b"data: "):
            event = json.loads(line[6:])
            if event["type"] == expected:
                return event
    pytest.fail(f"SSE did not broadcast {expected}")


def test_installed_daemon_lifecycle_and_cli_events(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMM_DEV_SOURCE")
    installed = _cli("install", "--json")
    launcher = _launcher(Path(installed["skill_root"]))
    if os.name == "nt":
        # omm.cmd is pure ASCII and resolves the live runtime through the
        # co-located .runtime link, so a pre-update copy is identical to it.
        old_launcher = launcher
    else:
        old_launcher = isolated / launcher.name
        shutil.copy2(launcher, old_launcher)
    workspace = isolated / "paper"
    with monkeypatch.context() as offline:
        # Keep the workspace outside the daemon registry to exclude polling as the event source.
        offline.setenv("OMM_STATE_DIR", str(isolated / "offline-state"))
        _cli("init", "--cwd", str(workspace), launcher=launcher)
    workspace_id = load_workspace(workspace).workspace_id
    (workspace / "sections" / "01-introduction.md").write_text(
        "# Introduction {#sec-introduction}\n\nThe manuscript text. {#p-intro}\n", encoding="utf-8"
    )
    port = int(os.environ["OMM_CONSOLE_PORT"])
    metadata_path = isolated / "state" / "daemon.json"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            calls = [
                executor.submit(
                    _cli, "console", "--cwd", str(workspace), "--json", launcher=launcher
                )
                for _ in range(2)
            ]
            results = [call.result() for call in calls]
        assert sorted(result["started"] for result in results) == [False, True]
        assert results[0]["pid"] == results[1]["pid"]
        assert all(result["workspace_id"] == workspace_id for result in results)
        assert results[0]["url"] == f"http://{LOOPBACK}:{port}/#workspaces/{workspace_id}"
        base = _cli("console", "--cwd", str(isolated), "--json", launcher=launcher)
        assert base["workspace_id"] is None
        assert base["url"] == f"http://{LOOPBACK}:{port}/"
        assert _cli("health", "--json", launcher=launcher)["pid"] == results[0]["pid"]
        if os.name != "nt":
            assert stat.S_IMODE(metadata_path.stat().st_mode) == 0o600
            assert stat.S_IMODE(metadata_path.parent.stat().st_mode) == 0o700

        connection = HTTPConnection(LOOPBACK, port, timeout=3)
        try:
            connection.request("GET", "/api/events")
            with connection.getresponse() as events:
                assert events.status == 200 and events.readline() == b": connected\n"
                common = ("--cwd", str(workspace))
                synced = _cli(*common, "sync", "--json", launcher=launcher)
                assert synced["changed"]
                event = _next_event(events, "revision_published")
                assert event["revision"] == synced["revision"]
                assert event["workspace_id"] == workspace_id
                checkpoint = _cli(*common, "checkpoint", "--json", launcher=launcher)
                assert (
                    _next_event(events, "checkpoint_updated")["checkpoint"]
                    == checkpoint["checkpoint"]
                )
                _cli(
                    *common,
                    "annotate",
                    "create",
                    "--block-id",
                    "p-intro",
                    "--comment",
                    "Expand.",
                    "--json",
                    launcher=launcher,
                )
                assert _next_event(events, "annotations_changed")["workspace_id"] == workspace_id
        finally:
            connection.close()

        updated_source = isolated / "source" / "omm"
        shutil.copytree(Path(omm.__file__).parent, updated_source)
        web = updated_source / "_web_dist"
        web.mkdir(exist_ok=True)
        (web / "new-asset.txt").write_text("New build content.", encoding="utf-8")
        updated_cli = updated_source / "cli.py"
        updated_cli.write_text(
            updated_cli.read_text(encoding="utf-8").replace(
                'version=f"omm {__version__}"', 'version=f"omm {__version__} (updated)"'
            ),
            encoding="utf-8",
        )
        with monkeypatch.context() as update:
            update.setattr(omm, "__file__", str(updated_source / "__init__.py"))
            updated = install_omm()
        assert updated["runtime_id"] != installed["runtime_id"]
        assert (Path(updated["web_dist"]) / "new-asset.txt").is_file()
        assert (
            _cli("runtime", "--json", launcher=old_launcher)["runtime_id"] == updated["runtime_id"]
        )
        _cli("--version", launcher=old_launcher, stdout=f"omm {omm.__version__} (updated)\n")
        mismatch = _cli("console", "--json", launcher=old_launcher, code=1)
        assert mismatch["code"] == "ERR_DAEMON_RUNTIME_MISMATCH"
        assert mismatch["running"] == installed["runtime_id"]
        assert mismatch["active"] == updated["runtime_id"]
        restarted = _cli("restart", "--json", launcher=old_launcher)
        assert restarted["stopped"] and restarted["started"]
        assert restarted["runtime_id"] == updated["runtime_id"]
        assert restarted["pid"] != results[0]["pid"]
        assert _cli("stop", "--json", launcher=old_launcher) == {"stopped": True}
        assert not metadata_path.exists()
        assert _cli("stop", "--json", launcher=old_launcher) == {"stopped": False}
    finally:
        if metadata_path.exists():
            stop_daemon()


@pytest.mark.parametrize("listening", [False, True])
def test_foreign_listener_is_rejected(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
    listening: bool,
) -> None:
    with _ephemeral_socket() as listener:
        if listening:
            listener.listen()
        monkeypatch.setenv("OMM_CONSOLE_PORT", str(listener.getsockname()[1]))
        assert _cli("console", "--json", code=1)["code"] == "ERR_PORT_IN_USE"
    assert not (isolated / "state" / "daemon.json").exists()


@pytest.mark.parametrize("malformed", [False, True])
def test_held_lock_without_valid_metadata_is_unconfirmed(isolated: Path, malformed: bool) -> None:
    state = state_directory()
    state.mkdir()
    record = state / "daemon.json"
    if malformed:
        record.write_bytes(b"{")
    script = """
import sys
import portalocker
with portalocker.Lock(sys.argv[1], mode="ab"):
    print("locked", flush=True)
    sys.stdin.buffer.read(1)
"""
    with subprocess.Popen(
        [sys.executable, "-c", script, str(state / "daemon.lock")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        creationflags=_NO_WINDOW,
    ) as holder:
        try:
            assert holder.stdout.readline().strip() == b"locked"
            assert _cli("stop", "--json", code=1)["code"] == "ERR_DAEMON_STOP_UNCONFIRMED"
            assert _cli("health", "--json", code=1)["code"] == "ERR_DAEMON_STOP_UNCONFIRMED"
            assert holder.poll() is None
            if malformed:
                assert record.read_bytes() == b"{"
            else:
                assert not record.exists()
        finally:
            holder.stdin.close()
            holder.wait(timeout=5)


@pytest.mark.parametrize("contents", [b"{", b'{"schema_version": 1}'])
def test_health_rejects_invalid_record_when_port_is_closed(isolated: Path, contents: bytes) -> None:
    state = state_directory()
    state.mkdir()
    record = state / "daemon.json"
    record.write_bytes(contents)

    assert _cli("health", "--json", code=1)["code"] == "ERR_DAEMON_STOP_UNCONFIRMED"
    assert record.read_bytes() == contents
    assert not (state / "daemon.lock").exists()


def test_rejected_shutdown_keeps_daemon_record(isolated: Path) -> None:
    assert ensure_daemon()["started"]
    state = state_directory()
    path = state / "daemon.json"
    record = _read(path)
    try:
        write_json_object(path, {**record, "token": "incorrect-token"})
        original = path.read_bytes()
        connection = HTTPConnection(LOOPBACK, record["port"], timeout=1)
        try:
            connection.request(
                "POST", "/api/shutdown", headers={"Authorization": "Bearer incorrect-token"}
            )
            with connection.getresponse() as response:
                assert response.status == 403
        finally:
            connection.close()
        with pytest.raises(OmmError, match="ERR_DAEMON_STOP_UNCONFIRMED"):
            stop_daemon(timeout=0.4)
        assert path.read_bytes() == original
        assert _cli("health", "--json")["pid"] == record["pid"]
    finally:
        write_json_object(path, record)
        assert stop_daemon() == {"stopped": True}


def test_stale_record_is_cleaned_only_after_exit(isolated: Path) -> None:
    state = state_directory()
    state.mkdir()
    path = state / "daemon.json"
    with subprocess.Popen([sys.executable, "-c", "pass"], creationflags=_NO_WINDOW) as exited:
        exited.wait(timeout=5)
        record = {
            "schema_version": 1,
            "pid": exited.pid,
            "port": configured_port(state),
            "runtime_id": "development",
            "contract_version": CONTRACT_VERSION,
            "token": "stale-token",
            "started_at": datetime.now(UTC).isoformat(),
        }
    write_json_object(path, {**record, "pid": os.getpid()})
    with pytest.raises(OmmError, match="ERR_DAEMON_STOP_UNCONFIRMED"):
        stop_daemon(timeout=0.4)
    with pytest.raises(OmmError, match="ERR_DAEMON_STOP_UNCONFIRMED"):
        ensure_daemon()
    assert path.exists()

    write_json_object(path, record)
    assert stop_daemon() == {"stopped": False}
    assert not path.exists()
    write_json_object(path, record)
    try:
        recovered = ensure_daemon()
        assert recovered["started"] and recovered["pid"] != record["pid"]
    finally:
        if path.exists():
            assert stop_daemon() == {"stopped": True}


def test_console_settings_precedence_and_schema(
    isolated: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = state_directory()
    state.mkdir()
    path = state / "console-settings.json"
    with _ephemeral_socket() as listener:
        saved = listener.getsockname()[1]
    write_json_object(
        path, {"schema_version": 1, "locale": "zh-CN", "theme": "dark", "port": saved}
    )
    monkeypatch.delenv("OMM_CONSOLE_PORT")
    assert configured_port(state) == saved
    monkeypatch.setenv("OMM_CONSOLE_PORT", str(saved + 1 if saved < 65535 else saved - 1))
    assert configured_port(state) == int(os.environ["OMM_CONSOLE_PORT"])
    write_json_object(path, {"schema_version": 1, "port": True})
    with pytest.raises(OmmError, match="ERR_PORT_INVALID"):
        configured_port(state)
    write_json_object(path, {"schema_version": 1, "theme": "invalid"})
    with pytest.raises(OmmError, match="ERR_CONSOLE_SETTINGS_INVALID"):
        configured_port(state)
