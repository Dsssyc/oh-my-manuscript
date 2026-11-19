"""Global paths, installed runtime identity, and offline Skill installation."""

from __future__ import annotations

import ast
import base64
import csv
import hashlib
import io
import json
import operator
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import venv
from importlib import metadata
from pathlib import Path
from uuid import uuid4

import portalocker
from platformdirs import user_data_path

import omm
from omm.errors import OmmError
from omm.server import CONTRACT_VERSION, DEFAULT_PORT
from omm.workspace import atomic_write_text, fs_path, resolve_path

_NAME = "oh-my-manuscript"
_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_REQUIREMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_MARKER_OPERATIONS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda left, right: left in right,
    ast.NotIn: lambda left, right: left not in right,
}
_SELF_CHECK = """
from pathlib import Path
import sys
import omm
import lxml.etree
import latex2mathml.converter
import markdown_it
import platformdirs
import portalocker
import pypdf
from omm.workspace import resolve_path

root = resolve_path(Path(sys.argv[1]))
assert resolve_path(Path(sys.prefix)) == root / "venv"
assert root in resolve_path(Path(omm.__file__)).parents
"""


def _path_override(name: str) -> Path | None:
    value = os.environ.get(name)
    return resolve_path(Path(value)) if value else None


def host_home() -> Path:
    return _path_override("OMM_HOST_HOME") or resolve_path(Path.home())


def omm_home() -> Path:
    if override := _path_override("OMM_HOME"):
        return override
    if sys.platform == "darwin" or os.environ.get("OMM_HOST_HOME"):
        relative = Path("AppData", "Local") if os.name == "nt" else Path(".local", "share")
        return resolve_path(host_home() / relative / _NAME)
    return resolve_path(user_data_path(_NAME, appauthor=False))


def state_directory(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return resolve_path(explicit)
    return _path_override("OMM_STATE_DIR") or resolve_path(omm_home() / "state")


def runtime_root() -> Path:
    return _path_override("OMM_RUNTIME_ROOT") or omm_home()


def skill_root() -> Path:
    return _path_override("OMM_SKILL_ROOT") or resolve_path(
        host_home() / ".agents" / "skills" / _NAME
    )


def read_json_object(path: Path) -> dict | None:
    """Return None only when the file is absent."""
    try:
        data = json.loads(fs_path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(data, dict):
        raise TypeError("expected a JSON object")
    return data


def write_json_object(path: Path, data: dict, *, mode: int | None = None) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", mode=mode)


def configured_port(state_dir: Path) -> int:
    path = resolve_path(state_dir) / "console-settings.json"
    try:
        settings = read_json_object(path)
        if settings is not None and (
            set(settings) - {"schema_version", "port", "locale", "theme"}
            or type(settings.get("schema_version")) is not int
            or settings["schema_version"] != 1
            or settings.get("locale", "en") not in ("en", "zh-CN")
            or settings.get("theme", "system") not in ("light", "dark", "system")
        ):
            raise ValueError("invalid console settings")
    except (OSError, TypeError, ValueError, RecursionError) as exc:
        raise OmmError("ERR_CONSOLE_SETTINGS_INVALID", path=path) from exc

    saved_port = settings.get("port", DEFAULT_PORT) if settings is not None else DEFAULT_PORT
    if type(saved_port) is not int or not 1 <= saved_port <= 65535:
        raise OmmError("ERR_PORT_INVALID", value=str(saved_port))
    override = os.environ.get("OMM_CONSOLE_PORT")
    if override is None:
        return saved_port
    try:
        port = int(override, 10)
        if 1 <= port <= 65535:
            return port
    except ValueError:
        pass
    raise OmmError("ERR_PORT_INVALID", value=override)


def _python_path(path: Path) -> Path:
    # Resolving the executable symlink itself would bypass pyvenv.cfg on POSIX.
    return resolve_path(path.parent) / path.name


def _venv_python(directory: Path) -> Path:
    return directory / (Path("Scripts", "python.exe") if os.name == "nt" else Path("bin", "python"))


def _venv_site(directory: Path) -> Path:
    variables = {"base": str(directory), "platbase": str(directory)}
    return Path(sysconfig.get_path("purelib", scheme="venv", vars=variables))


def active_runtime() -> dict | None:
    path = runtime_root() / "active-runtime.json"
    try:
        data = read_json_object(path)
        if data is None:
            return None
        runtime_id = data.get("runtime_id")
        executable = data.get("python")
        if (
            type(data.get("schema_version")) is not int
            or data["schema_version"] != 1
            or not isinstance(runtime_id, str)
            or not _RUNTIME_ID.fullmatch(runtime_id)
            or not isinstance(executable, str)
            or not Path(executable).is_absolute()
        ):
            raise ValueError("invalid active runtime")
        python = _python_path(Path(executable))
        executable_path = fs_path(python)
        if not executable_path.is_file() or not os.access(executable_path, os.X_OK):
            raise ValueError("runtime interpreter is unavailable")
        return {**data, "python": str(python)}
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise OmmError("ERR_RUNTIME_INVALID", path=path, detail=str(exc)) from exc


def expected_runtime() -> tuple[str, Path]:
    if active := active_runtime():
        return active["runtime_id"], Path(active["python"])
    if os.environ.get("OMM_DEV_SOURCE") == "1":
        return "development", _python_path(Path(sys.executable))
    raise OmmError("ERR_RUNTIME_NOT_INSTALLED")


def inspect_runtime() -> dict:
    runtime_id, python = expected_runtime()
    source = resolve_path(Path(omm.__file__))
    dev_source = os.environ.get("OMM_DEV_SOURCE") == "1"
    if not dev_source and runtime_root() / "runtimes" / runtime_id not in source.parents:
        raise OmmError("ERR_RUNTIME_SOURCE_LEAK", path=source, source_leak=True)
    return {
        "runtime_id": runtime_id,
        "python": str(python),
        "omm_file": str(source),
        "version": omm.__version__,
        "contract_version": CONTRACT_VERSION,
        "dev_source": dev_source,
        "source_leak": False,
    }


def inspect_paths() -> dict:
    skill = skill_root()
    root = runtime_root()
    return {
        "omm_home": str(omm_home()),
        "state_dir": str(state_directory()),
        "runtime_root": str(root),
        "active_runtime": str(root / "active-runtime.json"),
        "skill_root": str(skill),
        "launcher": str(skill / "scripts" / ("omm.cmd" if os.name == "nt" else "omm")),
    }


def dispatch_active(command: str | None, argv: list[str]) -> int | None:
    if (
        command == "install"
        or os.environ.get("OMM_DEV_SOURCE") == "1"
        or os.environ.get("_OMM_ACTIVE_EXEC") == "1"
    ):
        return None
    active = active_runtime()
    if active is None:
        return None
    python = Path(active["python"])
    current = Path(sys.executable)
    if resolve_path(current) == resolve_path(python) and (
        _python_path(current) == python or resolve_path(Path(sys.prefix)) == python.parent.parent
    ):
        return None
    try:
        return subprocess.run(
            [str(python), "-I", "-X", "utf8", "-m", "omm.cli", *argv],
            env={**os.environ, "_OMM_ACTIVE_EXEC": "1"},
            check=False,
        ).returncode
    except OSError as exc:
        raise OmmError("ERR_RUNTIME_INVALID", path=python, detail=str(exc)) from exc


def _marker_matches(marker: str) -> bool:
    # Evaluate the comparisons and boolean groups in runtime Requires-Dist markers.
    # The empty extra denotes the base distribution.
    environment = {
        "extra": "",
        "implementation_name": sys.implementation.name,
        "implementation_version": platform.python_version(),
        "os_name": os.name,
        "platform_machine": platform.machine(),
        "platform_python_implementation": platform.python_implementation(),
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "python_full_version": platform.python_version(),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "sys_platform": sys.platform,
    }
    version_fields = {"implementation_version", "python_version", "python_full_version"}

    def value(node: ast.expr) -> str:
        if isinstance(node, ast.Name) and node.id in environment:
            return environment[node.id]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        raise ValueError(f"invalid runtime dependency marker: {marker}")

    def matches(node: ast.expr) -> bool:
        if isinstance(node, ast.BoolOp):
            results = (matches(item) for item in node.values)
            return all(results) if isinstance(node.op, ast.And) else any(results)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = node.left, node.comparators[0]
            operation = _MARKER_OPERATIONS.get(type(node.ops[0]))
            if operation is not None:
                lhs, rhs = value(left), value(right)
                if isinstance(node.ops[0], (ast.In, ast.NotIn)):
                    return operation(lhs, rhs)
                if any(
                    isinstance(side, ast.Name) and side.id in version_fields
                    for side in (left, right)
                ):

                    def version(raw: str) -> tuple[int, ...]:
                        parts = tuple(map(int, raw.split(".")))
                        return parts + (0,) * max(0, 3 - len(parts))

                    return operation(version(lhs), version(rhs))
                return operation(lhs, rhs)
        raise ValueError(f"invalid runtime dependency marker: {marker}")

    return matches(ast.parse(marker, mode="eval").body)


def _distributions(project: metadata.Distribution) -> dict[str, metadata.Distribution]:
    def normalize(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).lower()

    visited = {_NAME}
    dependencies: dict[str, metadata.Distribution] = {}

    def collect(distribution: metadata.Distribution) -> None:
        for requirement in distribution.requires or ():
            spec, _, marker = requirement.partition(";")
            if marker and not _marker_matches(marker.strip()):
                continue
            name = _REQUIREMENT_NAME.match(spec)
            if name is None:
                raise ValueError(f"invalid runtime dependency: {requirement}")
            key = normalize(name[0])
            if key not in visited:
                visited.add(key)
                dependencies[key] = metadata.distribution(key)
                collect(dependencies[key])

    collect(project)
    return dependencies


def _copyable(relative: Path) -> bool:
    return "__pycache__" not in relative.parts and relative.suffix not in (".pyc", ".pyo")


def _copy_distribution(distribution: metadata.Distribution, target: Path) -> None:
    if distribution.files is None:
        raise ValueError(f"{distribution.name} has no installed file list")
    source = resolve_path(Path(distribution.locate_file("")))
    for entry in distribution.files:
        relative = Path(*entry.parts)
        path = resolve_path(Path(distribution.locate_file(entry)))
        if relative.is_absolute() or ".." in relative.parts or not path.is_relative_to(source):
            continue
        if (
            not _copyable(relative)
            or relative.name == "direct_url.json"
            or relative.name.startswith(("_virtualenv.", "__editable__", "_editable_"))
        ):
            continue
        source_file = fs_path(path)
        if path.suffix == ".pth":
            lines = source_file.read_text(encoding="utf-8").splitlines()
            if any(
                not resolve_path(source / line.strip()).is_relative_to(source)
                or Path(line.strip()).is_absolute()
                for line in lines
                if line.strip() and not line.startswith(("#", "import ", "import\t"))
            ):
                continue
        destination = fs_path(target / relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination)


def _content_id(
    source: Path, files: list[Path], dependencies: dict[str, metadata.Distribution]
) -> str:
    base = resolve_path(Path(getattr(sys, "_base_executable", sys.executable)))
    entries = [(str(base), tuple(sys.version_info))]
    entries.extend(
        (relative.as_posix(), hashlib.sha256(fs_path(source / relative).read_bytes()).hexdigest())
        for relative in files
    )
    entries.extend(
        (name, dist.version, hashlib.sha256((dist.read_text("RECORD") or "").encode()).hexdigest())
        for name, dist in sorted(dependencies.items())
    )
    return "rt-" + hashlib.sha256(json.dumps(entries).encode()).hexdigest()[:16]


def _write_project_metadata(site: Path, project: metadata.Distribution, files: list[Path]) -> None:
    info = site / f"oh_my_manuscript-{project.version}.dist-info"
    fs_path(info).mkdir()
    atomic_write_text(info / "METADATA", project.metadata.as_string())
    atomic_write_text(info / "INSTALLER", "omm\n")
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for relative in [
        *(Path("omm") / p for p in files),
        info.relative_to(site) / "METADATA",
        info.relative_to(site) / "INSTALLER",
    ]:
        data = fs_path(site / relative).read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        writer.writerow((relative.as_posix(), f"sha256={digest}", len(data)))
    writer.writerow(((info.relative_to(site) / "RECORD").as_posix(), "", ""))
    atomic_write_text(info / "RECORD", record.getvalue())


def _check_python(root: Path, python: Path) -> None:
    result = subprocess.run(
        [str(python), "-I", "-X", "utf8", "-B", "-c", _SELF_CHECK, str(root)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip())


def _materialize(
    root: Path,
    ready: dict,
    source: Path,
    files: list[Path],
    project: metadata.Distribution,
    dependencies: dict[str, metadata.Distribution],
) -> bool:
    python = Path(ready["python"])
    root_fs = fs_path(root)
    if root_fs.is_symlink() or root_fs.is_junction():
        raise ValueError("runtime directory is a link")
    try:
        if read_json_object(root / "READY.json") == ready:
            _check_python(root, python)
            return False
    except (OSError, TypeError, ValueError, RecursionError, subprocess.SubprocessError):
        pass

    transaction = uuid4()
    staging = root.parent / f".staging-{transaction}"
    staging_fs = fs_path(staging)
    previous_fs = fs_path(root.parent / f".replaced-{transaction}")
    staging_fs.mkdir(mode=0o700)
    promoted = False
    try:
        environment = staging / "venv"
        venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(fs_path(environment))
        site = _venv_site(environment)
        for dist in dependencies.values():
            _copy_distribution(dist, site)
        fs_path(site / "omm" / "_web_dist").mkdir(parents=True, exist_ok=True)
        for relative in files:
            destination = fs_path(site / "omm" / relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fs_path(source / relative), destination)
        _write_project_metadata(site, project, files)
        if root_fs.exists():
            root_fs.rename(previous_fs)
        staging_fs.rename(root_fs)
        promoted = True
        _check_python(root, python)
        write_json_object(root / "READY.json", ready)
    except BaseException:
        if promoted:
            shutil.rmtree(root_fs)
        if previous_fs.exists():
            previous_fs.rename(root_fs)
        raise
    finally:
        if staging_fs.exists():
            shutil.rmtree(staging_fs)
    if previous_fs.exists():
        shutil.rmtree(previous_fs)
    return True


def _host_links(skill: Path) -> list[Path]:
    home = host_home()
    hosts = []
    if fs_path(home / ".claude").is_dir():
        hosts.append(home / ".claude" / "skills")
    gemini = home / ".gemini"
    if fs_path(gemini / "antigravity").is_dir() or fs_path(gemini / "antigravity-cli").is_dir():
        hosts.append(gemini / "config" / "skills")
    return list(
        dict.fromkeys(
            resolve_path(directory) / _NAME
            for directory in hosts
            if resolve_path(directory) / _NAME != skill
        )
    )


def _link_exists(path: Path, skill: Path) -> bool:
    link = fs_path(path)
    if not (os.path.lexists(link) or link.is_junction()):
        return False
    if (link.is_symlink() or link.is_junction()) and link.exists() and resolve_path(link) == skill:
        return True
    raise OmmError("ERR_SKILL_TARGET_CONFLICT", path=path)


def _link_skill(path: Path, skill: Path) -> None:
    if _link_exists(path, skill):
        return
    try:
        fs_path(path.parent).mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            # Quoted environment expansions avoid interpreting path characters as cmd syntax.
            subprocess.run(
                'cmd.exe /d /v:off /s /c "mklink /J "%OMM_LINK%" "%OMM_LINK_TARGET%""',
                env={**os.environ, "OMM_LINK": str(path), "OMM_LINK_TARGET": str(skill)},
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=True,
            )
        else:
            path.symlink_to(skill, target_is_directory=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        if _link_exists(path, skill):
            return
        raise OmmError("ERR_FILE_WRITE", path=path, detail=str(exc)) from exc


def _link_runtime(scripts: Path, runtime: Path) -> None:
    """Point scripts/.runtime at the active runtime directory."""
    link = scripts / ".runtime"
    link_fs = fs_path(link)
    if os.path.lexists(link_fs) or link_fs.is_junction():
        if not (link_fs.is_symlink() or link_fs.is_junction()):
            raise OmmError("ERR_FILE_WRITE", path=link, detail="not a runtime link")
        # Replace the previous runtime link; a junction needs rmdir, not unlink.
        if link_fs.is_junction():
            link_fs.rmdir()
        else:
            link_fs.unlink()
    try:
        if os.name == "nt":
            # Quoted environment expansions avoid interpreting path characters as cmd syntax.
            subprocess.run(
                'cmd.exe /d /v:off /s /c "mklink /J "%OMM_LINK%" "%OMM_LINK_TARGET%""',
                env={**os.environ, "OMM_LINK": str(link), "OMM_LINK_TARGET": str(runtime)},
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=True,
            )
        else:
            os.symlink(runtime, link, target_is_directory=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise OmmError("ERR_FILE_WRITE", path=link, detail=str(exc)) from exc


def _write_skill(skill: Path, source: Path, ready: dict, links: list[Path]) -> None:
    for relative in (Path("SKILL.md"), Path("references", "syntax-and-cli.md")):
        path = skill / relative
        fs_path(path.parent).mkdir(parents=True, exist_ok=True)
        bundled = fs_path(source / "skill_bundle" / relative)
        atomic_write_text(path, bundled.read_bytes().decode("utf-8"))
    scripts = skill / "scripts"
    fs_path(scripts).mkdir(exist_ok=True)
    # cmd.exe decodes .cmd files with the console code page, so omm.cmd must
    # stay pure 7-bit ASCII: %~dp0 expands to its own Unicode directory and
    # reaches the interpreter through the co-located .runtime link.
    _link_runtime(scripts, runtime_root() / "runtimes" / ready["runtime_id"])
    python = ready["python"]
    shell_python = re.sub(r'([\\`"$])', r"\\\1", python)
    atomic_write_text(
        scripts / "omm", f'#!/bin/sh\nexec "{shell_python}" -m omm.cli "$@"\n', mode=0o755
    )
    atomic_write_text(
        scripts / "omm.cmd",
        '@echo off\r\n"%~dp0.runtime\\venv\\Scripts\\python.exe" -X utf8 -m omm.cli %*\r\n'
        "exit /b %ERRORLEVEL%\r\n",
    )
    write_json_object(
        skill / "skill.json",
        {
            "schema_version": 1,
            "name": _NAME,
            "version": omm.__version__,
            "contract_version": CONTRACT_VERSION,
            "runtime_id": ready["runtime_id"],
        },
    )
    for path in links:
        _link_skill(path, skill)


def install_omm() -> dict:
    root = runtime_root()
    runtimes = root / "runtimes"
    skill = skill_root()
    try:
        fs_path(runtimes).mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(fs_path(runtimes / "install.lock"), mode="ab", timeout=30):
            links = _host_links(skill)
            for path in links:
                _link_exists(path, skill)
            project = metadata.distribution(_NAME)
            dependencies = _distributions(project)
            source = resolve_path(Path(omm.__file__)).parent
            source_fs = fs_path(source)
            files = sorted(
                relative
                for path in source_fs.rglob("*")
                if path.is_file() and _copyable(relative := path.relative_to(source_fs))
            )
            runtime_id = _content_id(source, files, dependencies)
            directory = runtimes / runtime_id
            python = _venv_python(directory / "venv")
            ready = {
                "schema_version": 1,
                "runtime_id": runtime_id,
                "version": omm.__version__,
                "contract_version": CONTRACT_VERSION,
                "python": str(python),
                "web_dist": str(_venv_site(directory / "venv") / "omm" / "_web_dist"),
            }
            created = _materialize(directory, ready, source, files, project, dependencies)
            _write_skill(skill, source, ready, links)
            write_json_object(root / "active-runtime.json", ready)
    except (
        OSError,
        ValueError,
        SyntaxError,
        subprocess.SubprocessError,
        portalocker.exceptions.LockException,
    ) as exc:
        raise OmmError("ERR_RUNTIME_INVALID", path=runtimes, detail=str(exc)) from exc
    return {**ready, "created": created, "skill_root": str(skill), "links": list(map(str, links))}
