"""Manuscript Workspace manifests, initialization, and local registration."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from os.path import abspath
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import NamedTemporaryFile
from typing import Literal, cast
from uuid import RFC_4122, UUID, uuid4

import portalocker

from omm.errors import OmmError

DeliverableKind = Literal["manuscript", "supplementary", "response_letter"]
MANIFEST = "manuscript.toml"
ANNOTATIONS = Path(".omm", "annotations.json")
DEFAULT_TITLE = "Untitled Manuscript"
_WINDOWS_PATHS = os.name == "nt"


@dataclass(frozen=True, slots=True)
class Author:
    name: str
    affiliation: str | None = None
    email: str | None = None
    orcid: str | None = None


@dataclass(frozen=True, slots=True)
class Style:
    journal: str = "academic-default"
    citation_style: Literal["author-year", "numeric"] = "author-year"
    link_color: str = "#0070C0"
    bibliography_placement: Literal["per-deliverable", "merged-in-main"] = "per-deliverable"


@dataclass(frozen=True, slots=True)
class Deliverable:
    kind: DeliverableKind
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Workspace:
    root: Path
    workspace_id: str
    title: str
    authors: str | tuple[Author, ...]
    bibliography: str
    style: Style
    deliverables: dict[str, Deliverable]

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                source
                for deliverable in self.deliverables.values()
                for source in deliverable.sources
            )
        )

    def resolve(self, relative: str) -> Path:
        return workspace_path(self.root, relative, self.root / MANIFEST)


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_text(path: Path) -> str:
    try:
        return normalize_newlines(path.read_text(encoding="utf-8"))
    except UnicodeError as exc:
        raise OmmError("ERR_UTF8_INVALID", path=path) from exc
    except OSError as exc:
        raise OmmError("ERR_FILE_READ", path=path, detail=str(exc)) from exc


def _replace_file(temporary: Path, path: Path) -> None:
    temporary = fs_path(temporary)
    path = fs_path(path)
    if os.name != "nt":
        os.replace(temporary, path)
        return

    # Windows scanners can briefly hold a file open without delete sharing.
    for delay in (0.005, 0.01, 0.02, 0.04, 0.08, 0.16):
        try:
            os.replace(temporary, path)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in (5, 32) or path.is_dir():
                raise
        time.sleep(delay)
    os.replace(temporary, path)


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    temporary = None
    try:
        destination = fs_path(path)
        with NamedTemporaryFile(
            mode="w", dir=destination.parent, encoding="utf-8", newline="\n", delete=False
        ) as stream:
            temporary = fs_path(Path(stream.name))
            stream.write(text)
            stream.flush()
        if mode is not None:
            temporary.chmod(mode)
        elif destination.exists():
            temporary.chmod(destination.stat().st_mode & 0o777)
        _replace_file(temporary, path)
        temporary = None
    except OSError as exc:
        raise OmmError("ERR_FILE_WRITE", path=path, detail=str(exc)) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def normalize_relative_path(value: str, *, path: Path | None = None) -> str:
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value.replace("\\", "/"))
    if windows.drive or windows.root or posix.is_absolute() or "\0" in value:
        raise OmmError("ERR_WORKSPACE_PATH_INVALID", path=path, value=value)
    parts: list[str] = []
    for part in posix.parts:
        # Colons can denote drive-relative paths or alternate data streams on Windows.
        if part == ".." and parts:
            parts.pop()
        elif part == ".." or ":" in part:
            raise OmmError("ERR_WORKSPACE_PATH_INVALID", path=path, value=value)
        else:
            parts.append(part)
    if not parts:
        raise OmmError("ERR_WORKSPACE_PATH_INVALID", path=path, value=value)
    return PurePosixPath(*parts).as_posix()


def _strip_windows_verbatim_prefix(raw: str) -> str:
    if raw.startswith("\\\\?\\"):
        if raw[4:8].upper() == "UNC\\":
            return "\\\\" + raw[8:]
        if len(raw) >= 6 and raw[4].isascii() and raw[4].isalpha() and raw[5] == ":":
            return raw[4:]
    return raw


def resolve_path(path: Path) -> Path:
    """Resolve a path with a consistent Windows drive representation."""
    resolved = path.resolve()
    if os.name == "nt":
        return Path(_strip_windows_verbatim_prefix(str(resolved)))
    return resolved


def fs_path(path: Path) -> Path:
    """Use Windows extended-length paths for I/O without following links."""
    if not _WINDOWS_PATHS:
        return path
    absolute = abspath(path)
    if absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)


def workspace_path(root: Path, relative: str, path: Path | None = None) -> Path:
    try:
        root = resolve_path(root)
        target = root / relative
        resolved = resolve_path(target)
    except (OSError, RuntimeError) as exc:
        raise OmmError("ERR_WORKSPACE_PATH_INVALID", path=path, value=relative) from exc
    if not resolved.is_relative_to(root):
        raise OmmError("ERR_WORKSPACE_PATH_INVALID", path=path, value=relative)
    return target


def _uuid4(value: object) -> str:
    if isinstance(value, str):
        identifier = UUID(value)
        if (
            identifier.version == 4
            and identifier.variant == RFC_4122
            and value.lower() == str(identifier)
        ):
            return str(identifier)
    raise ValueError("workspace_id")


def load_workspace(root: Path) -> Workspace:
    root = resolve_path(root)
    path = root / MANIFEST
    source = read_text(path)
    try:
        data = tomllib.loads(source)
    except tomllib.TOMLDecodeError as exc:
        raise OmmError("ERR_MANIFEST_INVALID", path=path, field=MANIFEST) from exc

    def table(value: object, field: str, allowed: set[str]) -> dict:
        if not isinstance(value, dict):
            raise OmmError("ERR_MANIFEST_INVALID", path=path, field=field)
        for key in value:
            if key not in allowed:
                raise OmmError(
                    "ERR_MANIFEST_INVALID", path=path, field=f"{field}.{key}" if field else key
                )
        return value

    def string(value: object, field: str, choices: tuple[str, ...] = ()) -> str:
        if not isinstance(value, str) or not value.strip() or (choices and value not in choices):
            raise OmmError("ERR_MANIFEST_INVALID", path=path, field=field)
        return value

    table(data, "", {"workspace_id", "title", "authors", "bibliography", "style", "deliverables"})
    try:
        workspace_id = _uuid4(data.get("workspace_id"))
    except ValueError as exc:
        raise OmmError("ERR_MANIFEST_INVALID", path=path, field="workspace_id") from exc
    title = string(data.get("title"), "title")
    authors = data.get("authors")
    if isinstance(authors, list):
        author_list = []
        for entry in authors:
            author = table(entry, "authors", {"name", "affiliation", "email", "orcid"})
            string(author.get("name"), "authors.name")
            for key, value in author.items():
                if key != "name":
                    string(value, f"authors.{key}")
            author_list.append(Author(**author))
        authors = tuple(author_list)
    elif not isinstance(authors, str):
        raise OmmError("ERR_MANIFEST_INVALID", path=path, field="authors")
    bibliography = normalize_relative_path(
        string(data.get("bibliography", "references.bib"), "bibliography"), path=path
    )
    defaults = Style()
    style_fields = table(
        data.get("style", {}),
        "style",
        {
            "journal",
            "citation_style",
            "link_color",
            "bibliography_placement",
        },
    )
    journal = string(style_fields.get("journal", defaults.journal), "style.journal")
    link_color = string(style_fields.get("link_color", defaults.link_color), "style.link_color")
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", link_color) is None:
        raise OmmError("ERR_MANIFEST_INVALID", path=path, field="style.link_color")
    citation = string(
        style_fields.get("citation_style", defaults.citation_style),
        "style.citation_style",
        ("author-year", "numeric"),
    )
    placement = string(
        style_fields.get("bibliography_placement", defaults.bibliography_placement),
        "style.bibliography_placement",
        ("per-deliverable", "merged-in-main"),
    )
    style = Style(
        journal,
        cast(Literal["author-year", "numeric"], citation),
        link_color,
        cast(Literal["per-deliverable", "merged-in-main"], placement),
    )
    raw_deliverables = data.get("deliverables")
    if not isinstance(raw_deliverables, dict) or not raw_deliverables:
        raise OmmError("ERR_MANIFEST_INVALID", path=path, field="deliverables")
    deliverables = {}
    names = set()
    for name, value in raw_deliverables.items():
        field = f"deliverables.{name}"
        string(name, "deliverables")
        folded = name.casefold()
        device = folded.split(".", 1)[0].rstrip(" ")
        if (
            folded in names
            or any(part in name for part in ("/", "\\", "..", ":"))
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in name)
            or name.endswith((".", " "))
            or re.fullmatch(r"con|prn|aux|nul|(?:com|lpt)[1-9]", device)
        ):
            raise OmmError("ERR_MANIFEST_INVALID", path=path, field=field)
        names.add(folded)
        entry = table(value, field, {"kind", "sources"})
        kind = string(
            entry.get("kind"), f"{field}.kind", ("manuscript", "supplementary", "response_letter")
        )
        sources = entry.get("sources")
        if not isinstance(sources, list) or not sources:
            raise OmmError("ERR_MANIFEST_INVALID", path=path, field=f"{field}.sources")
        normalized = tuple(
            normalize_relative_path(string(source, f"{field}.sources"), path=path)
            for source in sources
        )
        if len(set(normalized)) != len(normalized):
            raise OmmError("ERR_MANIFEST_INVALID", path=path, field=f"{field}.sources")
        deliverables[name] = Deliverable(cast(DeliverableKind, kind), normalized)
    return Workspace(root, workspace_id, title, authors, bibliography, style, deliverables)


def _state_directory(state_dir: Path | None) -> Path:
    from omm.runtime import state_directory

    return state_directory(state_dir)


def _update_registry(directory: Path, operation: Callable[[dict[str, str]], bool]) -> None:
    path = directory / "workspaces.json"
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with portalocker.Lock(directory / "workspaces.lock", mode="ab", timeout=5):
            records: dict[str, str] = {}
            if path.exists():
                try:
                    data = json.loads(read_text(path))
                    if (
                        not isinstance(data, dict)
                        or set(data) != {"schema_version", "workspaces"}
                        or type(data["schema_version"]) is not int
                        or data["schema_version"] != 1
                        or not isinstance(data["workspaces"], dict)
                    ):
                        raise ValueError
                    for key, record in data["workspaces"].items():
                        if (
                            _uuid4(key) != key
                            or not isinstance(record, dict)
                            or set(record) != {"path"}
                            or not isinstance(record["path"], str)
                            or not Path(record["path"]).is_absolute()
                        ):
                            raise ValueError
                        records[key] = record["path"]
                except ValueError as exc:
                    raise OmmError("ERR_REGISTRY_INVALID", path=path) from exc
            if operation(records):
                data = {
                    "schema_version": 1,
                    "workspaces": {
                        key: {"path": record_path} for key, record_path in records.items()
                    },
                }
                atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except portalocker.exceptions.LockException as exc:
        raise OmmError("ERR_REGISTRY_LOCK", path=path) from exc
    except OSError as exc:
        raise OmmError("ERR_FILE_WRITE", path=path, detail=str(exc)) from exc


def _set_registry_path(
    workspace: Workspace,
    records: dict[str, str],
    *,
    relocating: bool = False,
    replace: bool = False,
) -> bool:
    workspace_id = workspace.workspace_id
    root = resolve_path(workspace.root)
    old = records.get(workspace_id)
    if old is not None and resolve_path(Path(old)) == root:
        return False
    if relocating and old is None:
        raise OmmError("ERR_WORKSPACE_NOT_REGISTERED", path=root, workspace_id=workspace_id)
    if old is not None and (not relocating or (Path(old).exists() and not replace)):
        raise OmmError(
            "ERR_WORKSPACE_ALREADY_REGISTERED", path=root, old_path=old, workspace_id=workspace_id
        )
    if any(resolve_path(Path(value)) == root for value in records.values()):
        raise OmmError("ERR_WORKSPACE_PATH_REGISTERED", path=root)
    records[workspace_id] = str(root)
    return True


def register_workspace(root: Path, *, state_dir: Path | None = None) -> Workspace:
    workspace = load_workspace(root)
    _update_registry(
        _state_directory(state_dir), lambda records: _set_registry_path(workspace, records)
    )
    return workspace


def relocate_workspace(
    root: Path, *, state_dir: Path | None = None, replace: bool = False
) -> Workspace:
    workspace = load_workspace(root)
    _update_registry(
        _state_directory(state_dir),
        lambda records: _set_registry_path(workspace, records, relocating=True, replace=replace),
    )
    return workspace


def init_workspace(
    root: Path, title: str = DEFAULT_TITLE, *, state_dir: Path | None = None
) -> Workspace:
    root = resolve_path(root)
    manifest = root / MANIFEST
    if not isinstance(title, str) or not title.strip():
        raise OmmError("ERR_MANIFEST_INVALID", path=manifest, field="title")
    try:
        title.encode()
    except UnicodeError as exc:
        raise OmmError("ERR_MANIFEST_INVALID", path=manifest, field="title") from exc
    if manifest.exists() or manifest.is_symlink():
        raise OmmError("ERR_WORKSPACE_EXISTS", path=manifest)

    workspace = Workspace(
        root,
        str(uuid4()),
        title,
        "",
        "references.bib",
        Style(),
        {"manuscript": Deliverable("manuscript", ("sections/01-introduction.md",))},
    )

    def initialize(records: dict[str, str]) -> bool:
        if manifest.exists() or manifest.is_symlink():
            raise OmmError("ERR_WORKSPACE_EXISTS", path=manifest)
        _set_registry_path(workspace, records)
        for directory in ("sections", "figures", ".omm"):
            workspace.resolve(directory).mkdir(parents=True, exist_ok=True)
        starter_files = {
            "references.bib": "",
            "sections/01-introduction.md": (
                "# Introduction {#sec-introduction}\n\n"
                ":::{outline} {#out-introduction}\n"
                "- Describe the research question and supporting evidence.\n"
                ":::\n"
            ),
            ANNOTATIONS.as_posix(): '{"schema_version": 1, "items": []}\n',
        }
        for relative, contents in starter_files.items():
            target = workspace.resolve(relative)
            if not target.exists() and not target.is_symlink():
                with target.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(contents)
        if not any((parent / ".git").exists() for parent in (root, *root.parents)):
            git_env = os.environ.copy()
            for key in (
                "GIT_DIR",
                "GIT_WORK_TREE",
                "GIT_COMMON_DIR",
                "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            ):
                git_env.pop(key, None)
            try:
                subprocess.run(
                    ["git", "init", "--quiet", "--template="],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    env=git_env,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                raise OmmError("ERR_GIT_INIT", path=root) from exc
        ignore = workspace.resolve(".gitignore")
        prior = read_text(ignore) if ignore.exists() else ""
        rules = ("/.omm/cache/", "/.omm/build/")
        if tuple(prior.splitlines()[-len(rules):]) != rules:
            prefix = prior if not prior or prior.endswith("\n") else prior + "\n"
            atomic_write_text(ignore, prefix + "\n".join(rules) + "\n")
        toml_title = json.dumps(title, ensure_ascii=False).replace("\x7f", r"\u007f")
        source = (
            f'workspace_id = "{workspace.workspace_id}"\n'
            f"title = {toml_title}\n"
            'authors = ""\nbibliography = "references.bib"\n\n'
            '[deliverables.manuscript]\nkind = "manuscript"\n'
            'sources = ["sections/01-introduction.md"]\n'
        )
        with manifest.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(source)
        return True

    _update_registry(_state_directory(state_dir), initialize)
    return workspace
