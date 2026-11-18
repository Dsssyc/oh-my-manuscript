"""Exercise Annotation persistence and revision-bound selectors."""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from omm import ast
from omm.annotations import (
    Annotation,
    BlockContext,
    BlockLookup,
    BlockSelector,
    CellSelector,
    RectSelector,
    ResolutionRecord,
    TextSelector,
    create_annotation,
    delete_annotation,
    evaluate_anchor,
    list_annotations,
    load_annotations,
    reopen_annotation,
    resolve_annotation,
)
from omm.errors import OmmError
from omm.parser import parse_source
from omm.workspace import ANNOTATIONS, init_workspace

C1 = "a" * 40
C2 = "b" * 40
C3 = "c" * 40
C4 = "d" * 40
ASSET = "1" * 64
NEW_ASSET = "2" * 64
ID_A = "550e8400-e29b-41d4-a716-446655440000"
ID_B = "550e8400-e29b-41d4-a716-446655440001"
CREATED = "2026-11-18T06:15:00Z"
RESOLVED = "2026-11-18T06:42:10Z"
REOPENED = "2026-11-18T06:45:00Z"
RESOLVED_AGAIN = "2026-11-18T06:52:00Z"

PARAGRAPH = "😀 𝑄 and **$S_y$** plus [@cite], [#fig-one], [#loc:p-1]."
REVISED_PARAGRAPH = "Repeated 𝑄 with a second 😀 𝑄."
TABLE = """\
:::{table} {#tbl-one}
Scores by basin.

| Basin | Metrics | > |
| ^ | NSE | KGE |
| --- | --- | --- |
| Upper | 0.9 | 0.8 |
| --- | --- | --- |
| Lower | 0.7 | 0.6 |
:::
"""
FIGURES = """\
:::{figure} {#fig-one}
![Terrain](figures/terrain.png)

Terrain overview.
:::

:::{figure} {#fig-panels cols=2}
![Terrain](figures/terrain.png){#fig-terrain}
![Drainage](figures/drainage.png){#fig-drainage}

Terrain and drainage.
:::
"""
REVIEW = """\
:::{review-item} {#R1-C3 reviewer="Reviewer 1"}
:::{comment}
Why is $S_y$ constant?
:::

Author reply. {#p-reply}
:::
"""
OUTLINE = """\
:::{outline} {#out-intro}
- Explain $S_y$ here.
:::
"""
TABLE_WITH_NOTE = TABLE.replace("\n:::", "\n\nNote: See [@cite].\n:::")
RECT = {"kind": "rect", "x": 0, "y": 0, "w": 0.5, "h": 0.5}


@pytest.fixture
def lookup() -> BlockLookup:
    sources = {
        C1: f"{PARAGRAPH} {{#p-1}}\n\n{TABLE}\n{FIGURES}",
        C2: f"{REVISED_PARAGRAPH} {{#p-1}}\n\n{TABLE.replace('0.6', '0.5')}\n{FIGURES}",
        C3: "Third revision of the estimate. {#p-1}",
        C4: "Another estimate. {#p-2}",
    }
    revisions = {
        revision: {block.block_id: block for block in ast.walk_blocks(parse_source(source).blocks)}
        for revision, source in sources.items()
    }

    def get_block(revision: str, block_id: str) -> BlockContext | None:
        if revision not in revisions:
            raise OmmError("ERR_REVISION_NOT_FOUND", revision=revision)
        block = revisions[revision].get(block_id)
        if block is None:
            return None
        digest = None
        if isinstance(block, (ast.FigureBlock, ast.Subfigure)):
            digest = ASSET if revision == C1 else NEW_ASSET
        return BlockContext(block, digest)

    return get_block


def write_store(root: Path, data: object) -> Path:
    path = root / ANNOTATIONS
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def stored_data() -> dict:
    return {
        "schema_version": 1,
        "items": [
            {
                "id": ID_A,
                "block_id": "p-1",
                "base_rev": C1,
                "selector": {"kind": "block"},
                "comment": "Check the estimate.",
                "created_at": CREATED,
                "before_block_md": "Prior estimate.",
                "resolutions": [
                    {
                        "resolved_at": RESOLVED,
                        "after_rev": C2,
                        "after_block_md": "Revised estimate.",
                        "reopened_at": REOPENED,
                    },
                    {
                        "resolved_at": RESOLVED_AGAIN,
                        "after_rev": C3,
                        "after_block_md": None,
                        "reopened_at": None,
                    },
                ],
            }
        ],
    }


def test_load_decodes_resolution_history(tmp_path: Path, stored_data: dict) -> None:
    stored_data["items"][0]["created_at"] = "2026-11-17T22:15:00-08:00"
    write_store(tmp_path, stored_data)
    (annotation,) = load_annotations(tmp_path)
    assert annotation.status == "resolved"
    assert annotation.selector == BlockSelector()
    assert annotation.created_at == CREATED
    first, second = annotation.resolutions
    assert first.reopened_at == REOPENED
    assert first.after_block_md == "Revised estimate."
    assert second.after_rev == C3
    assert second.after_block_md is None


@pytest.mark.parametrize(
    ("field_path", "bad_value"),
    [
        (("schema_version",), True),
        (("schema_version",), 1.0),
        (("schema_version",), 2),
        (("extra",), 0),
        (("items",), {}),
        (("items", 0, "status"), "resolved"),
        (("items", 0, "state"), "resolved"),
        (("items", 0, "id"), ID_A.upper()),
        (("items", 0, "id"), ID_A.replace("41d4", "11d4")),
        (("items", 0, "block_id"), "unknown id"),
        (("items", 0, "base_rev"), C1.upper()),
        (("items", 0, "comment"), " \t"),
        (("items", 0, "created_at"), CREATED.removesuffix("Z")),
        (("items", 0, "created_at"), "2026-11-18T06:15:00+00:60"),
        (("items", 0, "before_block_md"), None),
        (("items", 0, "selector"), {"kind": "block", "start": 0}),
        (("items", 0, "selector"), {"kind": "text", "start": 0, "end": True, "quote": "P"}),
        (("items", 0, "selector"), {"kind": "cell", "row": 0, "col": False}),
        (("items", 0, "selector"), {"kind": "rect", "x": 0, "y": 0, "w": 1, "h": 1}),
        (("items", 0, "selector"), RECT | {"asset_sha256": ASSET, "x": float("nan")}),
        (("items", 0, "selector"), RECT | {"asset_sha256": ASSET, "h": float("inf")}),
        (("items", 0, "resolutions"), None),
        (("items", 0, "resolutions", 0, "reopened_at"), None),
        (("items", 0, "resolutions", 0, "resolved_at"), "2026-02-30T09:00:00Z"),
        (("items", 0, "resolutions", 1, "after_rev"), "123"),
        (("items", 0, "resolutions", 1, "after_block_md"), False),
        (("items", 0, "resolutions", 1, "extra"), "unknown"),
    ],
)
def test_load_rejects_invalid_schema(
    tmp_path: Path, stored_data: dict, field_path: tuple[str | int, ...], bad_value: object
) -> None:
    record = stored_data
    for part in field_path[:-1]:
        record = record[part]
    record[field_path[-1]] = bad_value
    write_store(tmp_path, stored_data)
    with pytest.raises(OmmError, match="^ERR_ANNOTATIONS_INVALID$"):
        load_annotations(tmp_path)


def test_load_rejects_missing_fields_and_duplicate_ids(tmp_path: Path, stored_data: dict) -> None:
    item = stored_data["items"][0]
    del item["resolutions"][1]["after_block_md"]
    write_store(tmp_path, stored_data)
    with pytest.raises(OmmError, match="^ERR_ANNOTATIONS_INVALID$"):
        load_annotations(tmp_path)

    item["resolutions"][1]["after_block_md"] = None
    stored_data["items"].append(item)
    write_store(tmp_path, stored_data)
    with pytest.raises(OmmError, match="^ERR_ANNOTATIONS_INVALID$"):
        load_annotations(tmp_path)


@pytest.mark.parametrize(
    "source", ['{"schema_version":1,', '{"schema_version":1,"items":[],"items":[]}']
)
def test_load_rejects_invalid_json(tmp_path: Path, source: str) -> None:
    path = tmp_path / ANNOTATIONS
    path.parent.mkdir()
    path.write_text(source, encoding="utf-8")
    with pytest.raises(OmmError, match="^ERR_ANNOTATIONS_INVALID$"):
        load_annotations(tmp_path)


def test_create_selectors_round_trip(tmp_path: Path, lookup: BlockLookup) -> None:
    cases = [
        ("p-1", BlockSelector(), BlockSelector()),
        (
            "p-1",
            {"kind": "text", "start": 0, "end": 3, "quote": "😀 𝑄"},
            TextSelector(0, 3, "😀 𝑄"),
        ),
        (
            "fig-one",
            {"kind": "rect", "x": 0.9, "y": 0.8, "w": 0.1, "h": 0.2},
            RectSelector(0.9, 0.8, 0.1, 0.2, ASSET),
        ),
        ("fig-terrain", RectSelector(0, 0, 1, 1, ASSET), RectSelector(0, 0, 1, 1, ASSET)),
        ("tbl-one", CellSelector(1, 2), CellSelector(1, 2)),
        ("tbl-one", CellSelector(3, 2), CellSelector(3, 2)),
    ]
    annotations = []
    for block_id, selector, expected in cases:
        annotation = create_annotation(
            tmp_path,
            block_id=block_id,
            base_rev=C1,
            selector=selector,
            comment="  补充完整说明。  ",
            lookup=lookup,
            now_utc="2026-11-18T14:15:00+08:00",
        )
        annotations.append(annotation)
        current = lookup(C1, block_id)
        assert current is not None
        assert annotation.selector == expected
        assert annotation.before_block_md == current.block.clean_source
        assert annotation.comment == "补充完整说明。"
        assert annotation.created_at == CREATED
        assert annotation.status == "open"
        assert evaluate_anchor(annotation, current) == "exact"

    assert load_annotations(tmp_path) == tuple(sorted(annotations, key=lambda item: item.id))
    contents = (tmp_path / ANNOTATIONS).read_bytes()
    assert b"\r" not in contents and contents.endswith(b"\n")
    assert "补充完整说明。".encode() in contents
    assert all(
        "status" not in item and "state" not in item for item in json.loads(contents)["items"]
    )


@pytest.mark.parametrize("atom", ["$S_y$", "[@cite]", "[#fig-one]", "[#loc:p-1]"])
def test_text_selection_must_contain_entire_inline_atom(
    tmp_path: Path, lookup: BlockLookup, atom: str
) -> None:
    start = PARAGRAPH.index(atom)
    end = start + len(atom)
    for first, last in ((start + 1, end), (start, end - 1)):
        with pytest.raises(OmmError, match="^ERR_ANNOTATION_ATOMIC_SPLIT$"):
            create_annotation(
                tmp_path,
                block_id="p-1",
                base_rev=C1,
                lookup=lookup,
                comment="Check source.",
                selector=TextSelector(first, last, PARAGRAPH[first:last]),
            )
    for first, last in ((start, end), (start - 1, start), (end, end + 1)):
        selector = TextSelector(first, last, PARAGRAPH[first:last])
        assert (
            create_annotation(
                tmp_path,
                block_id="p-1",
                base_rev=C1,
                selector=selector,
                comment="Check source.",
                lookup=lookup,
            ).selector
            == selector
        )


@pytest.mark.parametrize(
    "selector",
    [
        TextSelector(-1, 1, "😀"),
        TextSelector(0, 0, ""),
        TextSelector(2, 1, "𝑄"),
        TextSelector(0, len(PARAGRAPH) + 1, PARAGRAPH),
        TextSelector(0, 2, "😀 𝑄"),
        TextSelector(0, 1, ""),
    ],
)
def test_text_selection_verifies_bounds_and_quote(
    tmp_path: Path, lookup: BlockLookup, selector: TextSelector
) -> None:
    with pytest.raises(OmmError, match="^ERR_ANNOTATION_QUOTE_MISMATCH$"):
        create_annotation(
            tmp_path,
            block_id="p-1",
            base_rev=C1,
            selector=selector,
            comment="Check source.",
            lookup=lookup,
        )


def _annotate_quote(root: Path, block: ast.Block, quote: str) -> Annotation:
    assert block.block_id is not None
    start = block.clean_source.index(quote)
    return create_annotation(
        root,
        block_id=block.block_id,
        base_rev=C1,
        selector=TextSelector(start, start + len(quote), quote),
        comment="Check source.",
        lookup=lambda *_: BlockContext(block),
    )


@pytest.fixture(
    params=[
        ("$$E = mc^2$$ {#eq-1}", "eq-1", "E = "),
        (FIGURES, "fig-terrain", "Terrain"),
        ("```\nliteral content\n```", None, "literal content"),
    ],
    ids=["display-math", "subfigure", "markdown-block"],
)
def non_text_target(request: pytest.FixtureRequest) -> tuple[ast.Block, str]:
    source, block_id, quote = request.param
    block = next(
        block
        for block in ast.walk_blocks(parse_source(source).blocks)
        if block.block_id == block_id
    )
    return replace(block, block_id="p-1"), quote


def test_text_selection_rejects_non_text_blocks(
    tmp_path: Path, non_text_target: tuple[ast.Block, str]
) -> None:
    block, quote = non_text_target
    with pytest.raises(OmmError, match="^ERR_ANNOTATION_SELECTOR_MISMATCH$") as error:
        _annotate_quote(tmp_path, block, quote)
    assert error.value.diagnostic.block_id == block.block_id
    assert load_annotations(tmp_path) == ()


def test_text_anchor_invalidated_when_target_becomes_non_text(
    tmp_path: Path, non_text_target: tuple[ast.Block, str]
) -> None:
    (before,) = parse_source("Before revision. {#p-1}").blocks
    annotation = _annotate_quote(tmp_path, before, "Before")
    block, _ = non_text_target
    assert evaluate_anchor(annotation, BlockContext(block)) == "invalidated"


@pytest.mark.parametrize(
    ("source", "accepted", "outside", "split_atomic"),
    [
        (
            REVIEW,
            ("Why is", "$S_y$"),
            (":::{comment}", "Author reply.", "constant?\n:::\n\nAuthor"),
            "S_y",
        ),
        (FIGURES, ("Terrain overview.",), (":::{figure}", "![Terrain]"), None),
        (
            TABLE_WITH_NOTE,
            ("Scores by basin.", "Metrics", "0.6", "Note: See [@cite]."),
            ("NSE | KGE", ">", "^", ":::{table}", "0.6 |\n\nNote"),
            "@cite",
        ),
        (OUTLINE, ("Explain $S_y$",), (":::{outline}", "here.\n:::"), "S_y"),
    ],
    ids=["review-item", "figure", "table", "outline"],
)
def test_text_selection_stays_within_one_container_region(
    tmp_path: Path,
    source: str,
    accepted: tuple[str, ...],
    outside: tuple[str, ...],
    split_atomic: str | None,
) -> None:
    block = parse_source(source).blocks[0]
    annotations = [_annotate_quote(tmp_path, block, quote) for quote in accepted]
    assert {item.id: item for item in load_annotations(tmp_path)} == {
        item.id: item for item in annotations
    }
    assert all(evaluate_anchor(item, BlockContext(block)) == "exact" for item in annotations)

    if split_atomic is not None:
        with pytest.raises(OmmError, match="^ERR_ANNOTATION_ATOMIC_SPLIT$"):
            _annotate_quote(tmp_path, block, split_atomic)
    for quote in outside:
        with pytest.raises(OmmError, match="^ERR_ANNOTATION_SELECTOR_MISMATCH$"):
            _annotate_quote(tmp_path, block, quote)


def test_review_reply_text_can_be_annotated_on_its_own_block(tmp_path: Path) -> None:
    (review,) = parse_source(REVIEW).blocks
    assert isinstance(review, ast.ReviewItem)
    (reply,) = review.reply
    assert _annotate_quote(tmp_path, reply, "Author reply.").block_id == "p-reply"


def test_legacy_text_range_loads_without_projecting_outside_review_comment(
    tmp_path: Path, stored_data: dict
) -> None:
    (review,) = parse_source(REVIEW).blocks
    quote = "Author reply."
    start = review.clean_source.index(quote)
    stored_data["items"][0].update(
        block_id=review.block_id,
        before_block_md=review.clean_source,
        selector={"kind": "text", "start": start, "end": start + len(quote), "quote": quote},
    )
    path = write_store(tmp_path, stored_data)
    before = path.read_bytes()
    (annotation,) = load_annotations(tmp_path)
    assert annotation.selector == TextSelector(start, start + len(quote), quote)
    assert evaluate_anchor(annotation, BlockContext(review)) == "invalidated"
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("block_id", "selector"),
    [
        ("p-1", {"kind": "block", "extra": 0}),
        ("p-1", {"kind": "unknown"}),
        ("p-1", {"kind": "text", "start": 0, "end": 1}),
        ("p-1", {"kind": "text", "start": False, "end": 1, "quote": "😀"}),
        ("p-1", {"kind": "text", "start": 0, "end": 1.0, "quote": "😀"}),
        ("p-1", CellSelector(0, 0)),
        ("tbl-one", CellSelector(0, True)),
        ("tbl-one", CellSelector(-1, 0)),
        ("tbl-one", CellSelector(0, -1)),
        ("tbl-one", CellSelector(4, 0)),
        ("tbl-one", CellSelector(0, 3)),
        ("p-1", RECT),
        ("fig-panels", RECT),
        ("fig-one", RECT | {"x": True}),
        ("fig-one", RECT | {"x": float("nan")}),
        ("fig-one", RECT | {"w": float("inf")}),
        ("fig-one", RECT | {"y": -0.1}),
        ("fig-one", RECT | {"x": 1.0}),
        ("fig-one", RECT | {"w": 0.0}),
        ("fig-one", RECT | {"h": -0.1}),
        ("fig-one", RECT | {"x": 0.6}),
        ("fig-one", RECT | {"y": 0.6}),
        ("fig-one", RECT | {"asset_sha256": NEW_ASSET}),
        ("fig-one", RECT | {"asset_sha256": "A" * 64}),
        ("fig-one", RECT | {"asset_sha256": None}),
    ],
)
def test_create_rejects_invalid_selector(
    tmp_path: Path, lookup: BlockLookup, block_id: str, selector: dict | CellSelector
) -> None:
    with pytest.raises(OmmError, match="^ERR_ANNOTATION_SELECTOR_MISMATCH$"):
        create_annotation(
            tmp_path,
            block_id=block_id,
            base_rev=C1,
            selector=selector,
            comment="Check source.",
            lookup=lookup,
        )
    assert load_annotations(tmp_path) == ()


@pytest.mark.parametrize("digest", [None, "f" * 63, "F" * 64])
def test_rect_requires_valid_context_asset_hash(
    tmp_path: Path, lookup: BlockLookup, digest: str | None
) -> None:
    figure = lookup(C1, "fig-one")
    assert figure is not None
    with pytest.raises(OmmError, match="^ERR_ANNOTATION_SELECTOR_MISMATCH$"):
        create_annotation(
            tmp_path,
            block_id="fig-one",
            base_rev=C1,
            selector=RECT,
            comment="Check source.",
            lookup=lambda *_: BlockContext(figure.block, digest),
        )


@pytest.mark.parametrize("selector", [CellSelector(0, 2), CellSelector(1, 0)])
def test_cell_selector_rejects_covered_cells(
    tmp_path: Path, lookup: BlockLookup, selector: CellSelector
) -> None:
    with pytest.raises(OmmError, match="^ERR_ANNOTATION_CELL_COVERED$"):
        create_annotation(
            tmp_path,
            block_id="tbl-one",
            base_rev=C1,
            selector=selector,
            comment="Check source.",
            lookup=lookup,
        )


@pytest.mark.parametrize("changed_source", [f"{REVISED_PARAGRAPH} {{#p-1}}", "New. {#p-2}"])
def test_submit_old_page_after_publish(
    tmp_path: Path, lookup: BlockLookup, changed_source: str
) -> None:
    (changed,) = parse_source(changed_source).blocks

    def published_lookup(revision: str, block_id: str) -> BlockContext | None:
        if revision == C2:
            return BlockContext(changed) if changed.block_id == block_id else None
        return lookup(revision, block_id)

    current = published_lookup(C2, "p-1")
    selector = TextSelector(2, 3, "𝑄")
    older = create_annotation(
        tmp_path,
        block_id="p-1",
        base_rev=C1,
        selector=selector,
        comment="Define discharge.",
        lookup=published_lookup,
    )
    assert older.before_block_md == PARAGRAPH
    assert older.base_rev == C1
    assert load_annotations(tmp_path) == (older,)
    assert evaluate_anchor(older, current) == (
        "block_deleted" if current is None else "block_modified"
    )


def test_anchor_projection_for_block_image_and_cell(tmp_path: Path, lookup: BlockLookup) -> None:
    def create(block_id: str, selector: BlockSelector | CellSelector | dict) -> Annotation:
        return create_annotation(
            tmp_path,
            block_id=block_id,
            base_rev=C1,
            selector=selector,
            comment="Check source.",
            lookup=lookup,
        )

    whole_block = create("p-1", BlockSelector())
    rect = create("fig-one", RECT)
    cell = create("tbl-one", CellSelector(3, 2))
    assert evaluate_anchor(whole_block, lookup(C2, "p-1")) == "exact"
    assert evaluate_anchor(rect, lookup(C2, "fig-one")) == "asset_changed"
    assert evaluate_anchor(rect, lookup(C2, "p-1")) == "invalidated"
    assert evaluate_anchor(rect, lookup(C2, "fig-panels")) == "invalidated"
    assert evaluate_anchor(cell, lookup(C2, "tbl-one")) == "block_modified"
    assert evaluate_anchor(cell, lookup(C2, "p-1")) == "invalidated"
    for modified_table in (
        TABLE.replace("| Lower | 0.7 | 0.6 |", "| Lower | 0.7 | > |"),
        TABLE.replace("| --- | --- | --- |\n| Lower | 0.7 | 0.6 |\n", ""),
    ):
        (block,) = parse_source(modified_table).blocks
        assert evaluate_anchor(cell, BlockContext(block)) == "invalidated"


def test_lifecycle_preserves_independent_resolution_snapshots(
    tmp_path: Path, lookup: BlockLookup
) -> None:
    b = create_annotation(
        tmp_path,
        block_id="p-1",
        base_rev=C1,
        selector=BlockSelector(),
        comment="Second issue.",
        lookup=lookup,
        annotation_id=ID_B,
        now_utc=CREATED,
    )
    a = create_annotation(
        tmp_path,
        block_id="p-1",
        base_rev=C1,
        selector=BlockSelector(),
        comment="First issue.",
        lookup=lookup,
        annotation_id=ID_A,
        now_utc=CREATED,
    )
    assert list_annotations(tmp_path, status="open", block_id="p-1") == (a, b)
    assert list_annotations(tmp_path, block_id="p-2") == ()
    with pytest.raises(OmmError, match="^ERR_ANNOTATION_STATE_CONFLICT$"):
        reopen_annotation(tmp_path, a.id)

    resolved_a = resolve_annotation(tmp_path, a.id, after_rev=C2, lookup=lookup, now_utc=RESOLVED)
    assert resolved_a.status == "resolved"
    assert resolved_a.resolutions == (ResolutionRecord(RESOLVED, C2, REVISED_PARAGRAPH, None),)
    assert list_annotations(tmp_path, status="open") == (b,)
    with pytest.raises(OmmError, match="^ERR_ANNOTATION_STATE_CONFLICT$"):
        resolve_annotation(tmp_path, a.id, after_rev=C3, lookup=lookup)

    reopened_a = reopen_annotation(tmp_path, a.id, now_utc="2026-11-18T08:45:00+02:00")
    assert reopened_a.status == "open"
    assert reopened_a.resolutions == (ResolutionRecord(RESOLVED, C2, REVISED_PARAGRAPH, REOPENED),)
    resolved_b = resolve_annotation(
        tmp_path,
        b.id,
        after_rev=C3,
        lookup=lookup,
        now_utc=RESOLVED_AGAIN,
    )
    assert resolved_b.resolutions == (
        ResolutionRecord(RESOLVED_AGAIN, C3, "Third revision of the estimate.", None),
    )
    assert evaluate_anchor(reopened_a, lookup(C4, "p-1")) == "block_deleted"
    assert list_annotations(tmp_path, status="open") == (reopened_a,)

    deleted_at = "2026-11-18T07:00:00Z"
    final_a = resolve_annotation(tmp_path, a.id, after_rev=C4, lookup=lookup, now_utc=deleted_at)
    assert final_a.resolutions == (
        *reopened_a.resolutions,
        ResolutionRecord(deleted_at, C4, None, None),
    )
    assert final_a.before_block_md == resolved_b.before_block_md == PARAGRAPH
    assert list_annotations(tmp_path, status="resolved") == (final_a, resolved_b)
    delete_annotation(tmp_path, a.id)
    assert load_annotations(tmp_path) == (resolved_b,)
    with pytest.raises(OmmError, match="^ERR_ANNOTATION_NOT_FOUND$"):
        delete_annotation(tmp_path, a.id)


def test_create_retries_return_original_record_without_lookup(
    tmp_path: Path, lookup: BlockLookup
) -> None:
    request = {"block_id": "fig-one", "base_rev": C1, "selector": RECT, "annotation_id": ID_A}
    create_annotation(
        tmp_path, **request, comment="  Verify terrain. ", lookup=lookup, now_utc=CREATED
    )
    resolved = resolve_annotation(tmp_path, ID_A, after_rev=C2, lookup=lookup, now_utc=RESOLVED)
    path = tmp_path / ANNOTATIONS
    before = path.read_bytes()

    def expired_lookup(revision: str, block_id: str) -> BlockContext | None:
        raise OmmError("ERR_REVISION_NOT_FOUND", revision=revision)

    assert (
        create_annotation(
            tmp_path,
            **request,
            comment="Verify terrain.",
            lookup=expired_lookup,
            now_utc=REOPENED,
        )
        == resolved
    )
    with pytest.raises(OmmError, match="^ERR_ANNOTATION_ID_CONFLICT$"):
        create_annotation(
            tmp_path, **request, comment="A different request.", lookup=expired_lookup
        )
    conflicting = request | {"selector": RECT | {"asset_sha256": NEW_ASSET}}
    with pytest.raises(OmmError, match="^ERR_ANNOTATION_ID_CONFLICT$"):
        create_annotation(tmp_path, **conflicting, comment="Verify terrain.", lookup=expired_lookup)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "override",
    [
        {"base_rev": "A" * 40},
        {"annotation_id": ID_A.upper()},
        {"comment": False},
        {"comment": " \t"},
        {"now_utc": "2026-11-18T06:15:00"},
    ],
)
def test_create_rejects_invalid_record_fields(
    tmp_path: Path, lookup: BlockLookup, override: dict
) -> None:
    request = {"block_id": "p-1", "base_rev": C1, "selector": BlockSelector(), "comment": "Check."}
    with pytest.raises(OmmError, match="^ERR_ANNOTATIONS_INVALID$"):
        create_annotation(tmp_path, **(request | override), lookup=lookup)
    assert not (tmp_path / ANNOTATIONS).exists()


def test_missing_revision_and_target_have_distinct_errors(
    tmp_path: Path, lookup: BlockLookup
) -> None:
    missing_rev = "f" * 40
    for block_id, revision, code in (
        ("p-1", missing_rev, "ERR_REVISION_NOT_FOUND"),
        ("p-2", C1, "ERR_ANNOTATION_TARGET_MISSING"),
    ):
        with pytest.raises(OmmError, match=f"^{code}$"):
            create_annotation(
                tmp_path,
                block_id=block_id,
                base_rev=revision,
                selector=BlockSelector(),
                comment="Check source.",
                lookup=lookup,
            )
    annotation = create_annotation(
        tmp_path,
        block_id="p-1",
        base_rev=C1,
        selector=BlockSelector(),
        comment="Check source.",
        lookup=lookup,
    )
    with pytest.raises(OmmError, match="^ERR_REVISION_NOT_FOUND$"):
        resolve_annotation(tmp_path, annotation.id, after_rev=missing_rev, lookup=lookup)
    assert load_annotations(tmp_path) == (annotation,)


def test_mutation_rejects_invalid_existing_store(tmp_path: Path, stored_data: dict) -> None:
    stored_data["items"][0]["status"] = "open"
    path = write_store(tmp_path, stored_data)
    before = path.read_bytes()
    with pytest.raises(OmmError, match="^ERR_ANNOTATIONS_INVALID$"):
        delete_annotation(tmp_path, ID_A)
    assert path.read_bytes() == before


def test_concurrent_writes_use_ignored_workspace_lock(tmp_path: Path, lookup: BlockLookup) -> None:
    root = tmp_path / "论文"
    init_workspace(root, state_dir=tmp_path / "state")
    barrier = Barrier(8)

    def create(index: int) -> Annotation:
        barrier.wait(timeout=10)
        return create_annotation(
            root,
            block_id="p-1",
            base_rev=C1,
            selector=BlockSelector(),
            comment=f"Check estimate {index}.",
            lookup=lookup,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        created = list(executor.map(create, range(32)))
    assert len({item.id for item in created}) == 32
    assert load_annotations(root) == tuple(
        sorted(created, key=lambda item: (item.created_at, item.id))
    )

    lock = Path(".omm/cache/annotations.lock")
    assert {path.relative_to(root) for path in (root / ".omm").rglob("*.lock")} == {lock}
    status = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all", "--", ".omm"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout.splitlines() == ["?? .omm/annotations.json"]
