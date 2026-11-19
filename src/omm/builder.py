"""Compile submission deliverables from immutable Git revisions."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from omm.checker import _Checker
from omm.docx_compiler import compile_docx_workspace
from omm.errors import OmmError
from omm.git_revs import (
    PUBLISHED_REF,
    RevisionSnapshot,
    _Repository,
    has_unsynced_changes,
    load_workspace_at_revision,
)
from omm.latex_compiler import CompiledLatexDeliverable, compile_latex_workspace
from omm.loc_resolver import resolve_blocks_from_pdf, resolve_workspace_locations
from omm.numbering import number_workspace
from omm.workspace import _replace_file, fs_path, resolve_path

Target = Literal["docx", "latex"]
_COMPILERS = {"docx": compile_docx_workspace, "latex": compile_latex_workspace}


def _submission_milestone(repo: _Repository, revision: str) -> str | None:
    refs = repo.git(
        "for-each-ref", "--format=%(refname)", "refs/tags/v*-submitted"
    ).splitlines()
    candidates: set[str] = set()
    for ref in refs:
        try:
            commit = repo.resolve(os.fsdecode(ref))
        except OmmError as exc:
            if exc.code != "ERR_REVISION_NOT_FOUND":
                raise
            continue
        if commit is not None and commit != revision and repo.is_ancestor(commit, revision):
            candidates.add(commit)
    if not candidates:
        return None
    tip = min(candidates)
    for commit in sorted(candidates):
        if repo.is_ancestor(tip, commit):
            tip = commit
    if any(not repo.is_ancestor(commit, tip) for commit in candidates):
        raise OmmError("ERR_MILESTONE_AMBIGUOUS", path=repo.root)
    return tip


def _load_snapshot(root: Path, revision: str) -> RevisionSnapshot:
    snapshot = load_workspace_at_revision(root, revision)
    checker = _Checker(snapshot.workspace)
    checker.documents = snapshot.documents
    checker.citekeys = set(snapshot.bibliography)
    # The snapshot loader already read the referenced assets from Git.
    checker.checked_assets = set(snapshot.assets)
    checker.index()
    checker.validate()
    for diag in checker.diagnostics:
        if diag.severity == "error":
            raise OmmError(
                diag.code, path=diag.path, line=diag.line, block_id=diag.block_id, **diag.params
            )
    return snapshot


def _workspace_output_parts(
    directory: Path, root: Path, *, reject_ancestors: bool = False,
) -> tuple[str, ...] | None:
    """Find workspace-relative parts, with an empty tuple for protected directories."""
    probe = directory
    while not fs_path(probe).exists():
        if probe == probe.parent:
            return None
        probe = probe.parent
    tail = directory.relative_to(probe).parts
    if reject_ancestors and not tail and any(
        fs_path(probe).samefile(fs_path(parent)) for parent in root.parents
    ):
        return ()
    for ancestor in (probe, *probe.parents):
        if fs_path(ancestor).samefile(fs_path(root)):
            return (*probe.relative_to(ancestor).parts, *tail)
    return None


def build_workspace(
    root: Path,
    *,
    target: Target | Literal["all"] = "all",
    out_dir: Path | None = None,
    revision: str | None = None,
    milestone: str | None = None,
    layout_pdfs: Mapping[str, Mapping[str, Path]] | None = None,
) -> dict[str, object]:
    """Build the requested formats and return a JSON-ready artifact inventory."""
    repo = _Repository(root)
    if revision is None:
        revision = repo.resolve(PUBLISHED_REF, optional=True)
        if revision is None:
            raise OmmError("ERR_NO_PUBLISHED_REVISION", path=repo.root)
        if has_unsynced_changes(repo.root) or repo.resolve(PUBLISHED_REF, optional=True) != revision:
            raise OmmError("ERR_BUILD_UNSYNCED_CHANGES", path=repo.root)
    else:
        revision = repo.resolve(revision)
    milestone = (
        _submission_milestone(repo, revision) if milestone is None else repo.resolve(milestone)
    )
    snapshot = _load_snapshot(repo.root, revision)
    previous = _load_snapshot(repo.root, milestone) if milestone is not None else None
    numbered = number_workspace(snapshot)
    targets: tuple[Target, ...] = ("docx", "latex") if target == "all" else (target,)
    layouts = layout_pdfs or {}
    requested_dir = out_dir or snapshot.workspace.resolve(".omm/build")
    final_dir = resolve_path(requested_dir)
    workspace_root = snapshot.workspace.root
    metadata_dir = workspace_root / ".omm"
    parts = _workspace_output_parts(final_dir, workspace_root, reject_ancestors=True)
    # Windows resolution can replace the spelling of .omm with its on-disk case.
    requested_parts = (
        _workspace_output_parts(Path(os.path.abspath(requested_dir)), workspace_root)
        if os.name == "nt" and parts else None
    )
    if (
        final_dir == resolve_path(metadata_dir)
        or (parts is not None and (len(parts) < 2 or parts[0] != ".omm"))
        or (
            requested_parts
            and requested_parts[0].casefold() == ".omm"
            and requested_parts[0] != ".omm"
        )
    ):
        raise OmmError("ERR_WORKSPACE_PATH_INVALID", path=final_dir, value=str(final_dir))
    primary = []
    replies = []
    for name, deliverable in snapshot.workspace.deliverables.items():
        (replies if deliverable.kind == "response_letter" else primary).append(name)
    for fmt, pdfs in layouts.items():
        if fmt not in targets:
            raise OmmError("ERR_BAD_REQUEST")
        for name in pdfs:
            if name not in primary:
                raise OmmError(
                    "ERR_LAYOUT_PDF_INVALID", deliverable=name,
                    detail="Expected the name of a line-numbered deliverable.",
                )

    deliverables = {}
    locations = {}
    try:
        final_dir.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".omm-build-", dir=final_dir) as directory:
            staging = Path(directory)
            files = set()
            stale_marked = []
            for fmt in targets:
                pdfs = layouts.get(fmt, {})
                compiler = _COMPILERS[fmt]
                if fmt == "latex":
                    compiled = compile_latex_workspace(
                        snapshot, staging, milestone=previous, deliverables=primary,
                        pdf_deliverables=pdfs.keys(),
                        shared_asset_deliverables=replies,
                    )
                else:
                    compiled = compile_docx_workspace(
                        snapshot, staging, milestone=previous, deliverables=primary
                    )
                labels = {}
                if replies or pdfs:
                    labels = resolve_workspace_locations(
                        snapshot, numbered, target=fmt, compiled_deliverables=compiled,
                        layout_pdfs=pdfs,
                    )
                    # Explicit PDFs also need validation when no location refers to them.
                    used_layouts = {numbered.locations[key].deliverable for key in labels}
                    for name in pdfs.keys() - used_layouts:
                        resolve_blocks_from_pdf(pdfs[name], compiled[name].blocks, deliverable=name)
                if replies:
                    compiled.update(
                        compiler(snapshot, staging, loc_labels=labels, deliverables=replies)
                    )
                locations[fmt] = {
                    start if end is None else f"{start}..{end}": label
                    for (start, end), label in labels.items()
                }
                deliverables[fmt] = {}
                for name, artifact in compiled.items():
                    clean = artifact.clean_path.relative_to(staging)
                    marked = (
                        artifact.marked_path.relative_to(staging) if artifact.marked_path else None
                    )
                    assets = (
                        [path.relative_to(staging) for path in artifact.asset_paths]
                        if isinstance(artifact, CompiledLatexDeliverable) else []
                    )
                    files.update((clean, *assets))
                    if marked is not None:
                        files.add(marked)
                    else:
                        stale_marked.append(clean.with_name(f"{name}.marked{clean.suffix}"))
                    deliverables[fmt][name] = {
                        "clean": str(final_dir / clean),
                        "marked": str(final_dir / marked) if marked is not None else None,
                        "assets": [str(final_dir / asset) for asset in assets],
                    }
            for relative in files | set(stale_marked):
                parent = resolve_path((final_dir / relative).parent)
                if not parent.is_relative_to(final_dir):
                    raise OmmError(
                        "ERR_WORKSPACE_PATH_INVALID", path=parent, value=str(parent)
                    )
            for relative in sorted(files):
                destination = final_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                _replace_file(staging / relative, destination)
            for relative in stale_marked:
                (final_dir / relative).unlink(missing_ok=True)
    except OSError as exc:
        raise OmmError("ERR_FILE_WRITE", path=final_dir, detail=str(exc)) from exc

    return {
        "revision": revision,
        "milestone": milestone,
        "out_dir": str(final_dir),
        "targets": list(targets),
        "deliverables": deliverables,
        "resolved_locations": locations,
    }
