"""Detached workbench entry point and local lifecycle control."""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

import portalocker

from omm.errors import OmmError
from omm.runtime import (
    configured_port,
    expected_runtime,
    inspect_runtime,
    read_json_object,
    state_directory,
    write_json_object,
)
from omm.server import CONTRACT_VERSION, LOOPBACK, WorkbenchServer
from omm.workspace import load_workspace

_LOCK_ERRORS = (OSError, portalocker.exceptions.LockException)
_RECORD_KEYS = {
    "schema_version",
    "pid",
    "port",
    "runtime_id",
    "contract_version",
    "token",
    "started_at",
}
_IDENTITY_KEYS = ("pid", "port", "runtime_id", "contract_version")
_HTTP_TIMEOUT = 0.25


@contextmanager
def _try_lock(state: Path, *, create: bool = True) -> Iterator[bool | None]:
    """Yield None when a read-only check finds no lock file."""
    lock = portalocker.Lock(
        state / "daemon.lock",
        mode="ab" if create else "r+b",
        timeout=0,
        fail_when_locked=True,
        flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
    )
    try:
        lock.acquire()
    except FileNotFoundError:
        if create:
            raise
        yield None
        return
    except portalocker.exceptions.AlreadyLocked:
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


def _record(state: Path) -> dict | None:
    path = state / "daemon.json"
    record = read_json_object(path)
    if record is None and not os.path.lexists(path):
        return None
    if (
        record is None
        or set(record) != _RECORD_KEYS
        or type(record["schema_version"]) is not int
        or record["schema_version"] != 1
        or type(record["pid"]) is not int
        or not 0 < record["pid"] <= (0xFFFFFFFF if os.name == "nt" else 0x7FFFFFFF)
        or type(record["port"]) is not int
        or not 1 <= record["port"] <= 65535
        or any(
            not isinstance(record[key], str)
            for key in ("runtime_id", "contract_version", "token", "started_at")
        )
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", record["runtime_id"])
        or not record["contract_version"]
        or not re.fullmatch(r"[A-Za-z0-9_-]+", record["token"])
        or datetime.fromisoformat(record["started_at"]).utcoffset() != timedelta(0)
    ):
        raise ValueError("invalid daemon metadata")
    return record


def _probe(port: int, timeout: float = _HTTP_TIMEOUT) -> dict | None:
    """Return None for connection refusal and an empty dict for an unverified listener."""
    connection = HTTPConnection(LOOPBACK, port, timeout=timeout)
    try:
        try:
            connection.connect()
        except ConnectionRefusedError:
            return None
        connection.request("GET", "/api/health", headers={"Host": f"{LOOPBACK}:{port}"})
        with connection.getresponse() as response:
            if response.status == 200:
                data = json.loads(response.read(16385))
                if (
                    isinstance(data, dict)
                    and data.get("status") == "ok"
                    and type(data.get("pid")) is int
                    and type(data.get("port")) is int
                    and data["port"] == port
                    and isinstance(data.get("runtime_id"), str)
                    and isinstance(data.get("contract_version"), str)
                ):
                    return data
    except (OSError, HTTPException, ValueError, RecursionError):
        pass
    finally:
        connection.close()
    return {}


def _matches(health: dict, record: dict | None) -> bool:
    return record is not None and all(health.get(key) == record[key] for key in _IDENTITY_KEYS)


def _post(record: dict, route: str, body: dict | None = None) -> None:
    port = record["port"]
    connection = HTTPConnection(LOOPBACK, port, timeout=_HTTP_TIMEOUT)
    try:
        connection.request(
            "POST",
            route,
            body=json.dumps(body) if body is not None else None,
            headers={
                "Host": f"{LOOPBACK}:{port}",
                "Authorization": f"Bearer {record['token']}",
                "Content-Type": "application/json",
            },
        )
        with connection.getresponse() as response:
            response.read(16385)
    except (OSError, HTTPException):
        pass
    finally:
        connection.close()


def _public_status(record: dict, **extra: object) -> dict:
    return {
        **{key: record[key] for key in _IDENTITY_KEYS},
        "url": f"http://{LOOPBACK}:{record['port']}/",
        **extra,
    }


def _running(state: Path, port: int, runtime_id: str, health: dict) -> dict:
    try:
        record = _record(state)
    except (OSError, TypeError, ValueError, RecursionError):
        raise OmmError("ERR_PORT_IN_USE", port=port) from None
    if not _matches(health, record):
        raise OmmError("ERR_PORT_IN_USE", port=port)
    if record["runtime_id"] != runtime_id:
        raise OmmError(
            "ERR_DAEMON_RUNTIME_MISMATCH",
            port=port,
            active=runtime_id,
            running=record["runtime_id"],
        )
    if record["contract_version"] != CONTRACT_VERSION:
        raise OmmError("ERR_PORT_IN_USE", port=port)
    return record


def _check_available_port(port: int) -> None:
    # A bound socket may refuse HTTP connections before it begins listening.
    with socket.socket() as listener:
        if os.name == "nt":
            option = getattr(socket, "SO_EXCLUSIVEADDRUSE", ~socket.SO_REUSEADDR)
            listener.setsockopt(socket.SOL_SOCKET, option, 1)
        elif WorkbenchServer.allow_reuse_address:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((LOOPBACK, port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) in {10013, 10048}:
                raise OmmError("ERR_PORT_IN_USE", port=port) from exc
            raise


def _clear_stale_record(state: Path, checked_port: int) -> None:
    """Clean up a crashed daemon while holding its lock and after a refused probe."""
    try:
        record = _record(state)
    except (OSError, TypeError, ValueError, RecursionError) as exc:
        raise OmmError("ERR_DAEMON_STOP_UNCONFIRMED", pid="unknown") from exc
    if record is None:
        return
    pid, port = record["pid"], record["port"]
    if _process_exited(pid) and (port == checked_port or _probe(port) is None):
        (state / "daemon.json").unlink()
    else:
        raise OmmError("ERR_DAEMON_STOP_UNCONFIRMED", pid=pid)


def _health_after_refusal(state: Path, port: int) -> dict | None:
    """Allow a daemon that is publishing its record a short interval to come up."""
    deadline = monotonic() + _HTTP_TIMEOUT
    pid: int | str = "unknown"
    try:
        while True:
            with _try_lock(state, create=False) as acquired:
                record = _record(state)
                pid = record["pid"] if record is not None else "unknown"
                if acquired is not False and (record is None or _process_exited(record["pid"])):
                    return None
            if monotonic() >= deadline:
                break
            sleep(0.05)
            if health := _probe(port):
                return health
    except (*_LOCK_ERRORS, TypeError, ValueError, RecursionError) as exc:
        raise OmmError("ERR_DAEMON_STOP_UNCONFIRMED", pid=pid) from exc
    raise OmmError("ERR_DAEMON_STOP_UNCONFIRMED", pid=pid)


def daemon_health() -> dict:
    runtime = inspect_runtime()
    state = state_directory()
    port = configured_port(state)
    health = _probe(port)
    if health is None:
        health = _health_after_refusal(state, port)
    integrity = {key: runtime[key] for key in ("dev_source", "source_leak")}
    if health is None:
        return {
            "status": "unavailable",
            "port": port,
            "runtime_id": runtime["runtime_id"],
            "contract_version": CONTRACT_VERSION,
            **integrity,
        }
    return _public_status(
        _running(state, port, runtime["runtime_id"], health),
        status="ok",
        **integrity,
    )


def _spawn(python: Path, state: Path, port: int, token: str, runtime_id: str) -> None:
    options = (
        {
            "creationflags": subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
        }
        if os.name == "nt"
        else {"start_new_session": True}
    )
    command = [str(python), "-X", "utf8"]
    if runtime_id != "development":
        command.append("-I")
    process = subprocess.Popen(
        [*command, "-m", "omm.daemon"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            **os.environ,
            "OMM_STATE_DIR": str(state),
            "OMM_CONSOLE_PORT": str(port),
            "_OMM_DAEMON_TOKEN": token,
        },
        **options,
    )
    # Also reap children when the CLI is embedded in a long-lived Python process.
    Thread(target=process.wait, daemon=True).start()


def ensure_daemon(timeout: float = 5.0) -> dict:
    runtime_id, python = expected_runtime()
    state = state_directory()
    port = configured_port(state)
    deadline = monotonic() + timeout
    health = _probe(port)
    if health:
        return _public_status(_running(state, port, runtime_id, health), started=False)

    token = secrets.token_urlsafe(32)
    launched = False
    while True:
        # The same lock protects stale metadata cleanup and publication by the daemon.
        if not launched:
            try:
                state.mkdir(mode=0o700, parents=True, exist_ok=True)
                with _try_lock(state) as acquired:
                    if acquired:
                        health = _probe(port)
                        if health is not None:
                            return _public_status(
                                _running(state, port, runtime_id, health),
                                started=False,
                            )
                        _check_available_port(port)
                        _clear_stale_record(state, port)
                if acquired:
                    _spawn(python, state, port, token, runtime_id)
                    launched = True
            except _LOCK_ERRORS as exc:
                raise OmmError("ERR_DAEMON_START_FAILED", port=port, detail=str(exc)) from exc

        health = _probe(port)
        if health:
            record = _running(state, port, runtime_id, health)
            return _public_status(record, started=record["token"] == token)
        if monotonic() >= deadline:
            if health is not None:
                raise OmmError("ERR_PORT_IN_USE", port=port)
            try:
                with _try_lock(state) as acquired:
                    if acquired:
                        _check_available_port(port)
            except _LOCK_ERRORS as exc:
                raise OmmError("ERR_DAEMON_START_FAILED", port=port, detail=str(exc)) from exc
            raise OmmError("ERR_DAEMON_START_FAILED", port=port)
        # A newly bound socket can accept TCP connections before HTTP is ready.
        # If no process holds the state lock, such a listener belongs elsewhere.
        if health is not None:
            try:
                with _try_lock(state) as acquired:
                    if acquired:
                        health = _probe(port)
                        if health is not None:
                            raise OmmError("ERR_PORT_IN_USE", port=port)
            except _LOCK_ERRORS as exc:
                raise OmmError("ERR_DAEMON_START_FAILED", port=port, detail=str(exc)) from exc
        sleep(0.05)


def _process_exited(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError as exc:
            return exc.errno == errno.ESRCH
        return False

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    synchronize = 0x00100000
    query_limited_information = 0x1000
    handle = kernel32.OpenProcess(synchronize | query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER
    try:
        wait_object_0 = 0
        still_active = 259
        code = wintypes.DWORD()
        return (
            kernel32.WaitForSingleObject(handle, 0) == wait_object_0
            and bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code)))
            and code.value != still_active
        )
    finally:
        kernel32.CloseHandle(handle)


def stop_daemon(timeout: float = 5.0) -> dict:
    state = state_directory()
    port = configured_port(state)
    path = state / "daemon.json"
    pid: int | str = "unknown"
    try:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with _try_lock(state) as acquired:
            record = _record(state)
            if acquired and record is None and _probe(port) is None:
                return {"stopped": False}
            if record is None:
                raise OmmError("ERR_DAEMON_STOP_UNCONFIRMED", pid=pid)
            pid, port = record["pid"], record["port"]
            health = _probe(port)
            if acquired and health is None and _process_exited(pid):
                path.unlink()
                return {"stopped": False}
            if acquired and not _matches(health or {}, record):
                raise OmmError("ERR_DAEMON_STOP_UNCONFIRMED", pid=pid)

        deadline = monotonic() + timeout
        if _matches(health or {}, record):
            _post(record, "/api/shutdown")
        while monotonic() < deadline:
            with _try_lock(state) as acquired:
                if acquired and _process_exited(pid) and _probe(port) is None:
                    remaining = _record(state)
                    if remaining is None or remaining == record:
                        path.unlink(missing_ok=True)
                        return {"stopped": True}
                    break
            sleep(0.05)
    except (*_LOCK_ERRORS, TypeError, ValueError, RecursionError) as exc:
        raise OmmError("ERR_DAEMON_STOP_UNCONFIRMED", pid=pid) from exc
    raise OmmError("ERR_DAEMON_STOP_UNCONFIRMED", pid=pid)


def restart_daemon() -> dict:
    stopped = stop_daemon()
    return {**ensure_daemon(), **stopped}


def notify_daemon(event: str, workspace: Path | str, **fields: object) -> None:
    try:
        record = _record(state_directory())
        if record is None:
            return
        runtime_id, _ = expected_runtime()
        if record["runtime_id"] == runtime_id and record["contract_version"] == CONTRACT_VERSION:
            workspace_id = (
                load_workspace(workspace).workspace_id if isinstance(workspace, Path) else workspace
            )
            _post(
                record, "/api/cli/notify", {"type": event, "workspace_id": workspace_id, **fields}
            )
    except (OmmError, OSError, TypeError, ValueError, RuntimeError):
        pass


def main() -> int:
    try:
        runtime = inspect_runtime()
        state = state_directory()
        port = configured_port(state)
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with _try_lock(state) as acquired:
            if not acquired:
                return 1
            if os.name != "nt":
                state.chmod(0o700)
            if os.path.lexists(state / "daemon.json"):
                if _probe(port) is not None:
                    raise OmmError("ERR_PORT_IN_USE", port=port)
                _clear_stale_record(state, port)
            path = state / "daemon.json"
            server = WorkbenchServer(
                state_dir=state,
                port=port,
                runtime_id=runtime["runtime_id"],
                daemon_token=os.environ.pop("_OMM_DAEMON_TOKEN", None),
            )

            def shutdown(signum, frame):
                Thread(target=server.shutdown, daemon=True).start()

            try:
                signal.signal(signal.SIGTERM, shutdown)
                signal.signal(signal.SIGINT, shutdown)
                write_json_object(
                    path,
                    {
                        "schema_version": 1,
                        "pid": os.getpid(),
                        "port": server.port,
                        "runtime_id": server.runtime_id,
                        "contract_version": CONTRACT_VERSION,
                        "token": server.daemon_token,
                        "started_at": datetime.now(UTC).isoformat(),
                    },
                    mode=0o600,
                )
                server.serve_forever()
            finally:
                server.server_close()
                try:
                    record = _record(state)
                    if record is not None and record["pid"] == os.getpid():
                        path.unlink()
                except (OSError, TypeError, ValueError, RecursionError):
                    pass
    except OmmError as exc:
        print(exc.code, file=sys.stderr)
        return 1
    except _LOCK_ERRORS:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
