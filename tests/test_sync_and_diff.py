from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from omm import ast
from omm.diff import BlockDiff, diff_block_markdown, diff_revisions, tokenize_inline_diff
from omm.errors import OmmError
from omm.git_revs import (
    LATEST_CHECKPOINT_REF,
    PUBLISHED_REF,
    has_unsynced_changes,
    load_workspace_at_revision,
    lookup_block_at_revision,
    mark_checkpoint,
    resolve_revision,
    sync_workspace,
)
from omm.workspace import ANNOTATIONS, MANIFEST, init_workspace

INTRO = "sections/01-introduction.md"
BIB = "@article{beven1979, title={A Physically Based Model}, year={1979}}\n"


def git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    env = os.environ.copy()
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        env.pop(key, None)
    return subprocess.run(
        ["git", *args], cwd=root, env=env, input=data, check=True, capture_output=True
    ).stdout


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty.gitconfig"))
    root = tmp_path / "课题 repository"
    root.mkdir()
    git(root, "init", "--quiet", "--template=", "--object-format=sha1")
    git(root, "config", "user.name", "Test Author")
    git(root, "config", "user.email", "author@example.com")
    git(root, "config", "core.autocrlf", "false")
    return root


def init(root: Path, repo: Path) -> None:
    init_workspace(root, state_dir=repo.parent / "state")


def test_sync_unborn_repository_and_warnings(repo: Path) -> None:
    init(repo, repo)
    write(
        repo,
        ANNOTATIONS.as_posix(),
        json.dumps(
            {
                "schema_version": 1,
                "items": [
                    {
                        "id": "550e8400-e29b-41d4-a716-446655440000",
                        "block_id": "sec-introduction",
                        "base_rev": "a" * 40,
                        "selector": {"kind": "block"},
                        "comment": "Review the introduction.",
                        "created_at": "2026-11-18T00:00:00Z",
                        "before_block_md": "# Introduction",
                        "resolutions": [],
                    }
                ],
            }
        )
        + "\n",
    )
    with pytest.raises(OmmError, match="^ERR_NO_PUBLISHED_REVISION$"):
        mark_checkpoint(repo)
    assert has_unsynced_changes(repo)
    head = (repo / ".git" / "HEAD").read_bytes()
    first = sync_workspace(repo)
    assert first.changed
    assert git(repo, "rev-list", "--parents", "-1", first.revision).decode().split() == [
        first.revision
    ]
    assert (repo / ".git" / "HEAD").read_bytes() == head
    with pytest.raises(OmmError, match="^ERR_REVISION_NOT_FOUND$"):
        resolve_revision(repo, "HEAD")
    assert not (repo / ".git" / "index").exists()
    assert not has_unsynced_changes(repo)
    snapshot = load_workspace_at_revision(repo, PUBLISHED_REF)
    assert isinstance(snapshot.blocks["out-introduction"].block, ast.OutlineBlock)

    for text in ("First draft", "Second draft"):
        write(repo, INTRO, f"{text} [@beven1979]. {{#p-intro}}\n")
        write(repo, "references.bib", BIB)
        assert has_unsynced_changes(repo)
        next_sync = sync_workspace(repo, expected_old_rev=first.revision)
        assert next_sync.changed
        assert resolve_revision(repo, f"{next_sync.revision}^") == first.revision
        first = next_sync
    assert not has_unsynced_changes(repo)
    repeat = sync_workspace(repo)
    assert repeat.revision == first.revision
    assert not repeat.changed
    assert git(repo, "rev-list", "--count", PUBLISHED_REF).strip() == b"3"


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Bare paragraph.\n", "ERR_BLOCK_ID_MISSING"),
        ("Missing [#p-missing]. {#p-paragraph}\n", "ERR_REFERENCE_MISSING"),
        ("Missing @missing. {#p-paragraph}\n", "ERR_CITATION_MISSING"),
        ("First. {#p-same}\n\nSecond. {#p-same}\n", "ERR_DUPLICATE_BLOCK_ID"),
        (
            ":::{figure} {#fig-missing}\n![Missing](figures/no.png)\n\nCaption.\n:::\n",
            "ERR_FIGURE_ASSET_MISSING",
        ),
    ],
)
def test_invalid_draft_keeps_published_revision(repo: Path, text: str, code: str) -> None:
    init(repo, repo)
    first = sync_workspace(repo)
    write(repo, INTRO, text)
    assert has_unsynced_changes(repo)
    with pytest.raises(OmmError, match=f"^{code}$") as error:
        sync_workspace(repo)
    assert error.value.diagnostic.path == repo / INTRO
    assert resolve_revision(repo, PUBLISHED_REF) == first.revision
    assert (repo / INTRO).read_bytes() == text.encode("utf-8")


def test_sync_preserves_index_head_and_unmanaged_files(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repo / "paper"
    init(root, repo)
    write(repo, "analysis/results.txt", "Committed result\n")
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "Initial branch commit")
    head = resolve_revision(root, "HEAD")
    git(repo, "config", "core.autocrlf", "true")

    write(repo, "analysis/results.txt", "Staged result\n")
    write(root, INTRO, "Staged draft. {#p-intro}\n")
    git(repo, "add", "--", "analysis/results.txt", f"paper/{INTRO}")
    draft = "Unstaged 估计 $S_y$. {#p-intro}\r\n".encode()
    (root / INTRO).write_bytes(draft)
    write(root, "untracked.txt", "A local note\n")
    index = repo / ".git" / "index"
    index_before = index.read_bytes()

    with monkeypatch.context() as hooks:
        for key in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        ):
            hooks.setenv(key, str(repo.parent / f"not-the-repo-{key}"))
        result = sync_workspace(root)
        assert not has_unsynced_changes(root)
    assert result.changed
    assert resolve_revision(root, "HEAD") == head
    assert resolve_revision(root, f"{result.revision}^") == head
    assert index.read_bytes() == index_before
    assert (root / INTRO).read_bytes() == draft
    assert (root / "untracked.txt").read_bytes() == b"A local note\n"
    assert (repo / "analysis/results.txt").read_bytes() == b"Staged result\n"
    assert git(repo, "show", f"{result.revision}:paper/{INTRO}") == draft.replace(b"\r\n", b"\n")
    assert git(repo, "show", f"{result.revision}:analysis/results.txt") == b"Committed result\n"


def test_subdirectory_revision_refs_and_head_alignment(repo: Path) -> None:
    root = repo / "paper with spaces"
    init(root, repo)
    write(root, INTRO, "Introduction [@beven1979]. {#p-intro}\n")
    write(root, "references.bib", BIB)
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "Establish manuscript branch")
    first = sync_workspace(root)
    assert first.revision == resolve_revision(root, "HEAD")
    assert first.changed

    write(root, INTRO, "Complete submission [@beven1979]. {#p-intro}\n")
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "Submit v1")
    head = resolve_revision(root, "HEAD")
    git(repo, "tag", "-a", "v1-submitted", "-m", "Submission milestone")
    synced = sync_workspace(root, expected_old_rev=first.revision)
    assert synced.revision == head
    assert synced.changed
    assert not sync_workspace(root).changed

    assert mark_checkpoint(root, revision=first.revision) == first.revision
    assert resolve_revision(root, LATEST_CHECKPOINT_REF) == first.revision
    assert mark_checkpoint(root) == head
    shutil.rmtree(root)
    for revision in (PUBLISHED_REF, LATEST_CHECKPOINT_REF, "refs/tags/v1-submitted", head[:12]):
        snapshot = load_workspace_at_revision(root, revision)
        assert snapshot.revision == head
        assert snapshot.workspace.root == root
        assert snapshot.bibliography["beven1979"].fields["year"] == "1979"
        entry = snapshot.blocks["p-intro"]
        assert entry.source == INTRO
        assert entry.deliverables == ("manuscript",)
        assert entry.block.clean_source == "Complete submission [@beven1979]."
    assert has_unsynced_changes(root)


def test_sync_preserves_both_histories_and_removes_retired_files(repo: Path) -> None:
    init(repo, repo)
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "Start branch")
    write(repo, INTRO, "Published draft. {#p-intro}\n")
    first = sync_workspace(repo)

    append = "sections/02-results.md"
    manifest = (repo / MANIFEST).read_text(encoding="utf-8")
    write(repo, MANIFEST, manifest.replace(INTRO, append))
    write(repo, append, "New results. {#p-results}\n")
    write(repo, "analysis.txt", "Branch material\n")
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "Complete phase")
    head = resolve_revision(repo, "HEAD")
    second = sync_workspace(repo, expected_old_rev=first.revision)
    assert second.changed
    assert git(repo, "rev-list", "--parents", "-1", second.revision).decode().split() == [
        second.revision,
        first.revision,
        head,
    ]
    assert git(repo, "ls-tree", "-r", second.revision, "--", INTRO) == b""
    assert git(repo, "show", f"{second.revision}:analysis.txt") == b"Branch material\n"
    assert (repo / INTRO).read_bytes() == b"Published draft. {#p-intro}\n"
    assert not sync_workspace(repo).changed


def test_stale_sync_and_invalid_revisions_fail_without_fallback(repo: Path) -> None:
    init(repo, repo)
    first = sync_workspace(repo)
    write(repo, INTRO, "Updated. {#p-intro}\n")
    second = sync_workspace(repo, expected_old_rev=first.revision)
    with pytest.raises(OmmError, match="^ERR_SYNC_CONFLICT$"):
        sync_workspace(repo, expected_old_rev=first.revision)
    assert resolve_revision(repo, PUBLISHED_REF) == second.revision
    blob = git(repo, "rev-parse", f"{second.revision}:{INTRO}").decode().strip()
    for revision in ("refs/tags/missing", "0" * 40, "--all", blob):
        with pytest.raises(OmmError, match="^ERR_REVISION_NOT_FOUND$"):
            load_workspace_at_revision(repo, revision)
        with pytest.raises(OmmError, match="^ERR_REVISION_NOT_FOUND$"):
            mark_checkpoint(repo, revision=revision)


def test_revision_diff_pairs_moved_deleted_and_replaced_figure_blocks(repo: Path) -> None:
    init(repo, repo)
    appendix = "sections/02-appendix.md"
    manifest = (repo / MANIFEST).read_text(encoding="utf-8")
    write(
        repo,
        MANIFEST,
        manifest
        + (f'\n[deliverables.appendix]\nkind = "supplementary"\nsources = ["{appendix}"]\n'),
    )
    figure = (
        ":::{figure} {#fig-site}\n"
        "![Terrain](figures/terrain.png){#fig-terrain}\n"
        "![Stream](figures/stream.png){#fig-stream}\n\n"
        "Site map.\n:::\n"
    )
    write(
        repo,
        INTRO,
        (
            "First. {#p-first}\n\nSecond. {#p-second}\n\n"
            "Move this. {#p-move}\n\nRemove this. {#p-delete}\n\n"
            "Keep this. {#p-stay}\n\n" + figure
        ),
    )
    write(repo, appendix, "# Appendix {#sec-appendix}\n")
    old_image = b"\x00\r\nold terrain\xff"
    new_image = b"\x00\r\nnew terrain\xff"
    terrain = repo / "figures" / "terrain.png"
    terrain.write_bytes(old_image)
    (repo / "figures" / "stream.png").write_bytes(b"unchanged stream")
    first = sync_workspace(repo).revision
    mark_checkpoint(repo)

    write(
        repo,
        INTRO,
        (
            "Second. {#p-second}\n\nInserted. {#p-added}\n\nFirst. {#p-first}\n\n"
            "Keep this. {#p-stay}\n\n" + figure
        ),
    )
    write(repo, appendix, "# Appendix {#sec-appendix}\n\nMoved and rewritten. {#p-move}\n")
    terrain.write_bytes(new_image)
    assert has_unsynced_changes(repo)
    second = sync_workspace(repo).revision
    result = diff_revisions(repo, LATEST_CHECKPOINT_REF, PUBLISHED_REF)
    assert (result.base_revision, result.target_revision) == (first, second)
    blocks = result.blocks
    assert blocks["p-move"].status == "modified_and_moved"
    assert blocks["p-move"].before.source == INTRO
    assert blocks["p-move"].after.source == appendix
    assert blocks["p-move"].after.deliverables == ("appendix",)
    assert {blocks["p-first"].status, blocks["p-second"].status} == {"moved", "unchanged"}
    assert blocks["p-stay"].status == "unchanged"
    assert blocks["p-delete"].status == "deleted"
    assert blocks["p-added"].status == "added"
    for block_id in ("fig-site", "fig-terrain"):
        change = blocks[block_id]
        assert change.status == "modified"
        assert change.asset_changed
        assert change.markdown.before_md == change.markdown.after_md
    assert blocks["fig-stream"].status == "unchanged"
    assert not blocks["fig-stream"].asset_changed
    assert blocks["fig-terrain"].before.asset_sha256 == hashlib.sha256(old_image).hexdigest()
    assert blocks["fig-terrain"].after.asset_sha256 == hashlib.sha256(new_image).hexdigest()

    (repo / INTRO).unlink()
    terrain.unlink()
    assert lookup_block_at_revision(repo, second, "p-delete") is None
    historical = lookup_block_at_revision(repo, first, "p-delete")
    assert historical is not None
    assert historical.block.clean_source == "Remove this."


def test_revision_diff_detects_deliverable_move_without_source_reordering(repo: Path) -> None:
    init(repo, repo)
    methods = "sections/02-methods.md"
    appendix = "sections/03-appendix.md"
    manifest = (repo / MANIFEST).read_text(encoding="utf-8")
    supplementary = '\n[deliverables.supplementary]\nkind = "supplementary"\n'
    write(
        repo,
        MANIFEST,
        manifest.replace(f'sources = ["{INTRO}"]', f'sources = ["{INTRO}", "{methods}"]')
        + supplementary
        + f'sources = ["{appendix}"]\n',
    )
    write(repo, methods, "# Methods {#sec-methods}\n\nOriginal procedure. {#p-procedure}\n")
    write(repo, appendix, "# Appendix {#sec-appendix}\n")
    first = sync_workspace(repo).revision

    write(repo, MANIFEST, manifest + supplementary + f'sources = ["{methods}", "{appendix}"]\n')
    write(repo, methods, "# Methods {#sec-methods}\n\nRevised procedure. {#p-procedure}\n")
    second = sync_workspace(repo).revision

    for revision in (first, second):
        assert load_workspace_at_revision(repo, revision).workspace.sources == (
            INTRO,
            methods,
            appendix,
        )
    blocks = diff_revisions(repo, first, second).blocks
    assert {block_id: change.status for block_id, change in blocks.items()} == {
        "sec-introduction": "unchanged",
        "out-introduction": "unchanged",
        "sec-methods": "moved",
        "p-procedure": "modified_and_moved",
        "sec-appendix": "unchanged",
    }


def assert_roundtrip(diff: BlockDiff) -> None:
    assert "".join(chunk.text for chunk in diff.chunks if chunk.op in {"equal", "delete"}) == (
        diff.before_md or ""
    )
    assert "".join(chunk.text for chunk in diff.chunks if chunk.op in {"equal", "insert"}) == (
        diff.after_md or ""
    )


def test_inline_diff_preserves_atoms_and_roundtrips() -> None:
    before = "中文かな한글 mixes\t$S_y$ [@beven1979] @smith1980 [#fig-site] [#loc:p-one..p-two].\n"
    after = "中英かな한글 adds\t$S_z$ [@beven1980] @smith1981 [#fig-map] [#loc:p-one..p-new].\n"
    assert tokenize_inline_diff(before) == [
        "中",
        "文",
        "か",
        "な",
        "한",
        "글",
        " ",
        "mixes",
        "\t",
        "$S_y$",
        " ",
        "[@beven1979]",
        " ",
        "@smith1980",
        " ",
        "[#fig-site]",
        " ",
        "[#loc:p-one..p-two]",
        ".",
        "\n",
    ]
    diff = diff_block_markdown(before, after)
    assert_roundtrip(diff)
    for op, text in (("delete", before), ("insert", after)):
        changed = [chunk.text for chunk in diff.chunks if chunk.op == op]
        for token in tokenize_inline_diff(text):
            if token.startswith(("$", "[@", "@", "[#")):
                assert any(token in chunk for chunk in changed)
    for old, new, op in ((None, after, "insert"), (before, None, "delete")):
        diff = diff_block_markdown(old, new)
        assert_roundtrip(diff)
        assert [chunk.op for chunk in diff.chunks] == [op]
