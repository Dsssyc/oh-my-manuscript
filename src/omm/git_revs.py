"""Publish validated Manuscript Workspace revisions and read them from Git."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from omm import ast
from omm.bib import BibEntry, parse_bib
from omm.checker import _Checker
from omm.errors import OmmError
from omm.parser import parse_source
from omm.workspace import (
    MANIFEST,
    Workspace,
    load_workspace,
    normalize_newlines,
    read_text,
    resolve_path,
    workspace_path,
)

PUBLISHED_REF = "refs/omm/published"
LATEST_CHECKPOINT_REF = "refs/omm/checkpoints/latest"
_SHA = re.compile(r"[0-9a-f]{40}")
_GIT_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


@dataclass(frozen=True, slots=True)
class SyncResult:
    revision: str
    changed: bool


@dataclass(frozen=True, slots=True)
class RevisionBlockEntry:
    source: str
    deliverables: tuple[str, ...]
    block: ast.Block
    asset_sha256: str | None


@dataclass(frozen=True, slots=True)
class RevisionSnapshot:
    revision: str
    workspace: Workspace
    bibliography: dict[str, BibEntry]
    documents: dict[str, ast.Document]
    blocks: dict[str, RevisionBlockEntry]


class _Repository:
    def __init__(self, root: Path) -> None:
        self.root = resolve_path(root)
        # A historical workspace directory can have been deleted from the working tree.
        self.top = next(
            (path for path in (self.root, *self.root.parents) if path.is_dir()), self.root
        )
        self.env = {key: value for key, value in os.environ.items() if key.upper() not in _GIT_ENV}
        top = self.git("rev-parse", "--show-toplevel", error="ERR_GIT_WORKSPACE")
        directory = os.fsdecode(top).removesuffix("\n")
        self.top = resolve_path(Path(directory.removesuffix("\r") if os.name == "nt" else directory))
        try:
            self.prefix = PurePosixPath(self.root.relative_to(self.top))
        except ValueError as exc:
            raise OmmError("ERR_GIT_WORKSPACE", path=self.root) from exc

    def run(
        self, *args: str, data: bytes | None = None, index: Path | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        env = self.env if index is None else {**self.env, "GIT_INDEX_FILE": str(index)}
        try:
            return subprocess.run(
                ["git", "-c", "core.splitIndex=false", *args],
                cwd=self.top,
                env=env,
                input=data,
                check=False,
                capture_output=True,
            )
        except OSError as exc:
            raise OmmError("ERR_GIT_COMMAND", path=self.root, detail=str(exc)) from exc

    def git(
        self,
        *args: str,
        data: bytes | None = None,
        index: Path | None = None,
        error: str = "ERR_GIT_COMMAND",
    ) -> bytes:
        result = self.run(*args, data=data, index=index)
        if result.returncode:
            raise OmmError(
                error,
                path=self.root,
                detail=result.stderr.decode("utf-8", errors="replace").strip(),
            )
        return result.stdout

    def path(self, relative: str) -> str:
        return (self.prefix / relative).as_posix()

    def resolve(self, rev_or_ref: str, *, optional: bool = False) -> str | None:
        if not rev_or_ref or "\0" in rev_or_ref or rev_or_ref.startswith("-"):
            raise OmmError("ERR_REVISION_NOT_FOUND", path=self.root, revision=rev_or_ref)
        result = self.run(
            "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev_or_ref}^{{commit}}"
        )
        revision = result.stdout.decode("ascii", errors="replace").strip()
        if result.returncode == 0 and _SHA.fullmatch(revision):
            return revision
        if optional and result.returncode == 1 and not result.stderr:
            # A missing reference is normal here. An existing non-commit is not.
            raw = self.run("rev-parse", "--verify", "--quiet", "--end-of-options", rev_or_ref)
            if raw.returncode == 1 and not raw.stderr:
                return None
        raise OmmError("ERR_REVISION_NOT_FOUND", path=self.root, revision=rev_or_ref)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        if ancestor == descendant:
            return True
        result = self.run("merge-base", "--is-ancestor", ancestor, descendant)
        if result.returncode not in (0, 1):
            raise OmmError("ERR_GIT_COMMAND", path=self.root, detail="git merge-base failed")
        return result.returncode == 0

    def publish(self, revision: str, old: str | None) -> SyncResult:
        if revision == old:
            if self.resolve(PUBLISHED_REF, optional=True) != old:
                raise OmmError("ERR_SYNC_CONFLICT", path=self.root)
            return SyncResult(revision, False)
        self.git(
            "update-ref",
            "--no-deref",
            PUBLISHED_REF,
            revision,
            old or "0" * 40,
            error="ERR_SYNC_CONFLICT",
        )
        return SyncResult(revision, True)


class _RevisionTree:
    def __init__(self, repo: _Repository, revision: str) -> None:
        self.repo = repo
        self.revision = revision
        args = ["ls-tree", "-r", "-z", "--full-tree", revision]
        if repo.prefix.parts:
            args.extend(["--", repo.prefix.as_posix() + "/"])
        self.entries: dict[bytes, tuple[bytes, bytes]] = {}
        for record in repo.git("--literal-pathspecs", *args).split(b"\0"):
            if record:
                metadata, path = record.split(b"\t", 1)
                mode, _, oid = metadata.split()
                self.entries[path] = mode, oid

    def entry(self, relative: str) -> tuple[bytes, bytes] | None:
        return self.entries.get(self.repo.path(relative).encode("utf-8"))

    def read(self, relative: str) -> bytes:
        entry = self.entry(relative)
        if entry is None or entry[0] not in (b"100644", b"100755"):
            raise OmmError("ERR_FILE_READ", path=self.repo.root / relative, revision=self.revision)
        return self.repo.git("cat-file", "blob", entry[1].decode("ascii"))

    def text(self, relative: str) -> str:
        return _decode_text(self.read(relative), self.repo.root / relative)

    def matches(self, blobs: dict[str, bytes], removed: set[str]) -> bool:
        return all(
            (entry := self.entry(path)) is not None
            and entry[0] in (b"100644", b"100755")
            and entry[1] == oid
            for path, oid in blobs.items()
        ) and all(self.entry(path) is None for path in removed)


def _decode_text(data: bytes, path: Path) -> str:
    try:
        return normalize_newlines(data.decode("utf-8"))
    except UnicodeError as exc:
        raise OmmError("ERR_UTF8_INVALID", path=path) from exc


def _parse_manifest(root: Path, text: str) -> Workspace:
    # The existing manifest reader takes a directory. Give it only the captured manifest.
    with TemporaryDirectory() as directory:
        temp_root = Path(directory)
        (temp_root / MANIFEST).write_bytes(text.encode("utf-8"))
        try:
            return replace(load_workspace(temp_root), root=root)
        except OmmError as exc:
            raise OmmError(exc.code, path=root / MANIFEST, **exc.diagnostic.params) from exc


def _assets(block: ast.Block) -> tuple[str, ...]:
    if isinstance(block, ast.Subfigure):
        return (block.asset_path,)
    if isinstance(block, ast.FigureBlock):
        return tuple(panel.asset_path for panel in block.subfigures)
    return ()


class _SyncChecker(_Checker):
    """Validate the captured bytes that will be written to Git."""

    def __init__(self, workspace: Workspace, manifest: str) -> None:
        super().__init__(workspace)
        self.files = {MANIFEST: manifest.encode("utf-8")}

    def read_text(self, relative: str) -> str:
        if relative in self.files:
            return self.files[relative].decode("utf-8")
        content = read_text(self.workspace.resolve(relative))
        self.files[relative] = content.encode("utf-8")
        return content

    def load(self) -> None:
        bib = self.workspace.bibliography
        self.citekeys = set(parse_bib(self.read_text(bib), self.workspace.root / bib))
        for source in self.workspace.sources:
            self.documents[source] = parse_source(
                self.read_text(source),
                self.workspace.root / source,
                allow_unstamped_figures=True,
                diagnostics=self.diagnostics,
            )

    def check_asset(self, source: str, block: ast.Block, asset: str) -> None:
        if asset in self.checked_assets:
            return
        self.checked_assets.add(asset)
        try:
            data = self.workspace.resolve(asset).read_bytes()
        except FileNotFoundError:
            self.report(source, block, "ERR_FIGURE_ASSET_MISSING", asset=asset)
            return
        except OSError:
            self.report(source, block, "ERR_FIGURE_ASSET_UNREADABLE", asset=asset)
            return
        except OmmError as exc:
            self.report(source, block, exc.code, **exc.diagnostic.params)
            return
        if asset in self.files and self.files[asset] != data:
            self.report(source, block, "ERR_MANAGED_PATH_CONFLICT", asset=asset)
            return
        self.files[asset] = data


def _validated_files(root: Path) -> dict[str, bytes]:
    manifest = read_text(workspace_path(root, MANIFEST, root / MANIFEST))
    checker = _SyncChecker(_parse_manifest(root, manifest), manifest)
    for diag in checker.run().diagnostics:
        if diag.severity == "error":
            raise OmmError(
                diag.code, path=diag.path, line=diag.line, block_id=diag.block_id, **diag.params
            )
    return checker.files


def _managed_paths(tree: _RevisionTree, *, strict: bool) -> tuple[set[str], set[str]]:
    """Find paths managed by a tree's own manifest and Structured Source."""
    try:
        workspace = _parse_manifest(tree.repo.root, tree.text(MANIFEST))
    except OmmError as exc:
        if strict or exc.code not in {"ERR_FILE_READ", "ERR_UTF8_INVALID", "ERR_MANIFEST_INVALID"}:
            raise
        # A branch can contain a draft or predate Manuscript Workspace initialization.
        return set(), set()

    text_paths = {MANIFEST, workspace.bibliography, *workspace.sources}
    asset_paths: set[str] = set()
    for source in workspace.sources:
        try:
            document = parse_source(
                tree.text(source),
                tree.repo.root / source,
                allow_unstamped_figures=not strict,
                diagnostics=None if strict else [],
            )
        except OmmError as exc:
            if strict or exc.code not in {
                "ERR_FILE_READ",
                "ERR_UTF8_INVALID",
                "ERR_SOURCE_INVALID",
            }:
                raise
            continue
        for block in ast.walk_blocks(document.blocks):
            if isinstance(block, ast.Subfigure):
                asset_paths.add(block.asset_path)
    return text_paths, asset_paths


def _candidate_tree(
    repo: _Repository, base: str | None, blobs: dict[str, bytes], removed: set[str]
) -> str:
    with TemporaryDirectory() as directory:
        index = Path(directory) / "index"
        if base is not None:
            repo.git("read-tree", base, index=index)
        records = [
            b"0 " + b"0" * 40 + b"\t" + repo.path(path).encode("utf-8") + b"\0"
            for path in sorted(removed)
        ]
        records.extend(
            b"100644 " + oid + b"\t" + repo.path(path).encode("utf-8") + b"\0"
            for path, oid in blobs.items()
        )
        repo.git("update-index", "-z", "--index-info", data=b"".join(records), index=index)
        return repo.git("write-tree", index=index).decode("ascii").strip()


def sync_workspace(root: Path, *, expected_old_rev: str | None = None) -> SyncResult:
    repo = _Repository(root)
    old = repo.resolve(PUBLISHED_REF, optional=True)
    if expected_old_rev is not None and expected_old_rev != old:
        raise OmmError("ERR_SYNC_CONFLICT", path=repo.root)
    head = repo.resolve("HEAD", optional=True)
    files = _validated_files(repo.root)
    blobs = {
        path: repo.git("hash-object", "-w", "--stdin", data=data).strip()
        for path, data in files.items()
    }

    removed: set[str] = set()
    old_tree = _RevisionTree(repo, old) if old else None
    if old_tree is not None:
        text_paths, asset_paths = _managed_paths(old_tree, strict=False)
        removed = (text_paths | asset_paths) - blobs.keys()

    head_tree = old_tree
    if head != old:
        head_tree = _RevisionTree(repo, head) if head else None
    if (
        head_tree is not None
        and head_tree.matches(blobs, removed)
        and (old is None or repo.is_ancestor(old, head_tree.revision))
    ):
        return repo.publish(head_tree.revision, old)
    if old_tree is not None and old_tree.matches(blobs, removed):
        return repo.publish(old_tree.revision, old)

    if head_tree is not None and head != old:
        text_paths, asset_paths = _managed_paths(head_tree, strict=False)
        removed.update((text_paths | asset_paths) - blobs.keys())
    tree = _candidate_tree(repo, head or old, blobs, removed)
    parents = ["-p", old] if old else []
    if head and (old is None or not repo.is_ancestor(head, old)):
        parents.extend(["-p", head])
    revision = (
        repo.git(
            "-c",
            "user.name=oh-my-manuscript",
            "-c",
            "user.email=omm@localhost",
            "commit-tree",
            "--no-gpg-sign",
            tree,
            *parents,
            data=b"Sync Manuscript Workspace\n",
        )
        .decode("ascii")
        .strip()
    )
    return repo.publish(revision, old)


def has_unsynced_changes(root: Path) -> bool:
    repo = _Repository(root)
    revision = repo.resolve(PUBLISHED_REF, optional=True)
    if revision is None:
        return True
    tree = _RevisionTree(repo, revision)
    if not _matches_disk(tree, MANIFEST, text=True):
        return True
    text_paths, asset_paths = _managed_paths(tree, strict=True)
    return any(not _matches_disk(tree, path, text=True) for path in text_paths - {MANIFEST}) or any(
        not _matches_disk(tree, path, text=False) for path in asset_paths
    )


def _matches_disk(tree: _RevisionTree, relative: str, *, text: bool) -> bool:
    historical = tree.read(relative)
    root = tree.repo.root
    path = root / relative
    try:
        disk = workspace_path(root, relative, root / MANIFEST).read_bytes()
    except OmmError as exc:
        if exc.code != "ERR_WORKSPACE_PATH_INVALID":
            raise
        return False
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise OmmError("ERR_FILE_READ", path=path, detail=str(exc)) from exc
    if text:
        try:
            disk = normalize_newlines(disk.decode("utf-8")).encode("utf-8")
        except UnicodeError:
            return False
    return disk == historical


def resolve_revision(root: Path, rev_or_ref: str) -> str:
    revision = _Repository(root).resolve(rev_or_ref)
    assert revision is not None
    return revision


def mark_checkpoint(root: Path, *, revision: str | None = None) -> str:
    repo = _Repository(root)
    target = repo.resolve(revision) if revision is not None else None
    published = repo.resolve(PUBLISHED_REF, optional=True)
    if published is None:
        raise OmmError("ERR_NO_PUBLISHED_REVISION", path=repo.root)
    target = target or published
    if not repo.is_ancestor(target, published):
        raise OmmError("ERR_REVISION_NOT_PUBLISHED", path=repo.root, revision=target)
    repo.git("update-ref", "--no-deref", LATEST_CHECKPOINT_REF, target)
    return target


def load_workspace_at_revision(root: Path, rev_or_ref: str) -> RevisionSnapshot:
    repo = _Repository(root)
    revision = repo.resolve(rev_or_ref)
    assert revision is not None
    tree = _RevisionTree(repo, revision)
    workspace = _parse_manifest(repo.root, tree.text(MANIFEST))
    bibliography = parse_bib(tree.text(workspace.bibliography), repo.root / workspace.bibliography)
    documents: dict[str, ast.Document] = {}
    blocks: dict[str, RevisionBlockEntry] = {}
    fingerprints: dict[str, str] = {}
    for source in workspace.sources:
        path = repo.root / source
        document = documents[source] = parse_source(tree.text(source), path)
        deliverables = tuple(
            name
            for name, deliverable in workspace.deliverables.items()
            if source in deliverable.sources
        )
        for block in ast.walk_blocks(document.blocks):
            hashes = []
            for asset in _assets(block):
                if asset not in fingerprints:
                    fingerprints[asset] = hashlib.sha256(tree.read(asset)).hexdigest()
                hashes.append(fingerprints[asset])
            block_id = block.block_id
            if block_id is None:
                if block.stamp_offset is not None:
                    raise OmmError("ERR_BLOCK_ID_MISSING", path=path, line=block.line)
                continue
            if block_id in blocks:
                raise OmmError(
                    "ERR_DUPLICATE_BLOCK_ID", path=path, line=block.line, block_id=block_id
                )
            asset_sha256 = None
            if len(hashes) == 1:
                asset_sha256 = hashes[0]
            elif hashes:
                # Ordered fixed-width digests distinguish both replacement and panel reordering.
                asset_sha256 = hashlib.sha256(bytes.fromhex("".join(hashes))).hexdigest()
            blocks[block_id] = RevisionBlockEntry(source, deliverables, block, asset_sha256)
    return RevisionSnapshot(revision, workspace, bibliography, documents, blocks)


def lookup_block_at_revision(
    root: Path, rev_or_ref: str, block_id: str
) -> RevisionBlockEntry | None:
    return load_workspace_at_revision(root, rev_or_ref).blocks.get(block_id)
