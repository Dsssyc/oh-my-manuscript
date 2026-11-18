"""Phase 1A contract tests for Workspace validation and Structured Source."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from omm import ast
from omm.bib import parse_bib
from omm.checker import check_workspace
from omm.errors import OmmError
from omm.parser import parse_source
from omm.stamp import stamp_workspace
from omm.workspace import Author, Style, init_workspace, load_workspace, register_workspace

WORKSPACE_ID = "e7b96785-b014-41d2-96d2-c486e29081ce"
MANIFEST = f'''
workspace_id = "{WORKSPACE_ID}"
title = "水文"
authors = [{{name = "张三", affiliation = "Institute"}}]
bibliography = 'library\u005creferences.bib'

[deliverables.manuscript]
kind = "manuscript"
sources = ['章节\u005c./一 章.md', "sections/../第二章.md"]
'''


def write(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8", newline="\n")


def test_manifest_loads_strict_typed_fields_and_posix_paths(tmp_path: Path) -> None:
    root = tmp_path / "论文 写作"
    root.mkdir()
    (root / "manuscript.toml").write_bytes(MANIFEST.replace("\n", "\r\n").encode("utf-8"))
    workspace = load_workspace(root)
    assert workspace.workspace_id == WORKSPACE_ID
    assert workspace.title == "水文"
    assert workspace.style == Style()
    assert workspace.authors == (Author("张三", affiliation="Institute"),)
    assert workspace.bibliography == "library/references.bib"
    assert workspace.sources == ("章节/一 章.md", "第二章.md")


@pytest.mark.parametrize(
    ("original", "replacement", "code"),
    [
        (WORKSPACE_ID, "a8f5d064-c66c-11ed-afa1-0242ac120002", "ERR_MANIFEST_INVALID"),
        ('title = "水文"', 'title = "  "', "ERR_MANIFEST_INVALID"),
        ('name = "张三"', "name = 2", "ERR_MANIFEST_INVALID"),
        ('affiliation = "Institute"', 'extra = "Institute"', "ERR_MANIFEST_INVALID"),
        (
            "[deliverables.manuscript]",
            "extra = 'invalid'\n[deliverables.manuscript]",
            "ERR_MANIFEST_INVALID",
        ),
        (
            "[deliverables.manuscript]",
            '[style]\nlink_color = "#fff"\n[deliverables.manuscript]',
            "ERR_MANIFEST_INVALID",
        ),
        (
            "[deliverables.manuscript]",
            "[style]\njournal = true\n[deliverables.manuscript]",
            "ERR_MANIFEST_INVALID",
        ),
        ('kind = "manuscript"', 'kind = "appendix"', "ERR_MANIFEST_INVALID"),
        ('kind = "manuscript"', 'kind = "manuscript"\nextra = 1', "ERR_MANIFEST_INVALID"),
        ("sections/../第二章.md", "../第二章.md", "ERR_WORKSPACE_PATH_INVALID"),
        ("sections/../第二章.md", "/第二章.md", "ERR_WORKSPACE_PATH_INVALID"),
        ("sections/../第二章.md", "C:/第二章.md", "ERR_WORKSPACE_PATH_INVALID"),
        ("sections/../第二章.md", "//server/第二章.md", "ERR_WORKSPACE_PATH_INVALID"),
    ],
)
def test_manifest_rejects_invalid_input(
    tmp_path: Path,
    original: str,
    replacement: str,
    code: str,
) -> None:
    manifest = tmp_path / "manuscript.toml"
    write(manifest, MANIFEST.replace(original, replacement))
    with pytest.raises(OmmError) as error:
        load_workspace(tmp_path)
    assert error.value.diagnostic.path == manifest
    assert error.value.code == code


def test_init_refuses_overwrite_and_register_refuses_a_copied_id(tmp_path: Path) -> None:
    state = tmp_path / "state"
    first = tmp_path / "first"
    second = tmp_path / "second"
    created = init_workspace(first, 'Title with "quotes" and 中文', state_dir=state)
    assert UUID(created.workspace_id).version == 4
    assert load_workspace(first) == created
    registry = state / "workspaces.json"
    before = registry.read_bytes()

    with pytest.raises(OmmError, match="ERR_WORKSPACE_EXISTS"):
        init_workspace(first, "New title", state_dir=state)

    second.mkdir()
    (second / "manuscript.toml").write_bytes((first / "manuscript.toml").read_bytes())
    with pytest.raises(OmmError, match="ERR_WORKSPACE_ALREADY_REGISTERED"):
        register_workspace(second, state_dir=state)
    assert registry.read_bytes() == before


def test_inline_spans_use_unicode_code_points_and_normalized_lines() -> None:
    source = (
        "# 流域 {#sec-main}\r\n\r\n"
        "😀 𝑄 + **bold *emphasis*** and H~2~O, m^2^, `@code`, $𝑄_{i} + @math$;\r"
        "[@beven; @kirchner] (see @smith), [#sec-main], [#loc:pa..pb] and "
        "[two *words*](https://example.test). user@domain.edu   {#pa}\r\n"
    )
    document = parse_source(source)
    heading, paragraph = document.blocks
    assert document.source.count("\r") == 0
    assert isinstance(heading, ast.Heading)
    assert paragraph.line == 3
    assert paragraph.clean_source.endswith(" user@domain.edu")
    fragments = [
        (type(node), paragraph.clean_source[node.span.start : node.span.end])
        for node in ast.walk_inlines(paragraph.inlines)
        if not isinstance(node, (ast.Text, ast.LineBreak))
    ]
    assert fragments == [
        (ast.Strong, "**bold *emphasis***"),
        (ast.Emphasis, "*emphasis*"),
        (ast.Subscript, "~2~"),
        (ast.Superscript, "^2^"),
        (ast.CodeSpan, "`@code`"),
        (ast.InlineMath, "$𝑄_{i} + @math$"),
        (ast.Citation, "[@beven; @kirchner]"),
        (ast.Citation, "@smith"),
        (ast.BlockRef, "[#sec-main]"),
        (ast.LocRef, "[#loc:pa..pb]"),
        (ast.Link, "[two *words*](https://example.test)"),
        (ast.Emphasis, "*words*"),
    ]
    assert paragraph.inlines[0].span == ast.Span(0, len("😀 𝑄 + "))


def test_fences_in_code_spans_and_formula_do_not_close_directive() -> None:
    source = """:::{outline} {#out-plan}
Inline `one
:::
two` and $x
:::
+y$.

```md
:::
:::{figure} {#f1}
```

$$
x
:::
y
$$
:::
"""
    (outline,) = parse_source(source).blocks
    assert isinstance(outline, ast.OutlineBlock)
    assert outline.clean_source == source.replace(" {#out-plan}", "").rstrip("\n")
    assert [node.value for node in outline.inlines if isinstance(node, ast.CodeSpan)] == [
        "one ::: two"
    ]
    assert [node.latex for node in outline.inlines if isinstance(node, ast.InlineMath)] == [
        "x\n:::\n+y"
    ]


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("### Heading {#a}", "ERR_BLOCK_ID_INVALID"),
        (":::{outline} {#oo state=draft}\n:::", "ERR_BLOCK_ATTRIBUTES_INVALID"),
        (":::{outline} {#oo}\n", "ERR_DIRECTIVE_UNCLOSED"),
        (r"$𝑄 \tag{2}$ {#pp}", "ERR_MATH_TAG_FORBIDDEN"),
        (r"$$𝑄 \tag{2}$$ {#eq unnumbered}", "ERR_MATH_TAG_FORBIDDEN"),
        (':::{review-item} {#r1 reviewer="A"}\nText {#pp}\n:::', "ERR_REVIEW_COMMENT_REQUIRED"),
    ],
)
def test_parser_errors_are_structured(source: str, code: str) -> None:
    path = Path("section.md")
    with pytest.raises(OmmError) as error:
        parse_source(source, path)
    assert error.value.code == code
    assert error.value.diagnostic.path == path
    assert error.value.diagnostic.line == 1


def test_display_math_has_one_block_id_for_multiline_formula() -> None:
    (formula,) = parse_source("$$\r\n𝑄_i = 1\r\n$$ {#eq-flow unnumbered}\r\n").blocks
    assert isinstance(formula, ast.DisplayMath)
    assert formula.block_id == "eq-flow"
    assert formula.unnumbered
    assert formula.clean_source == "$$\n𝑄_i = 1\n$$"


def test_table_protects_pipes_and_builds_rectangles_and_logical_rows() -> None:
    source = r""":::{table} {#tbl-result}
Calibration results [#eq-flow].

| Station | Metric | > |
| ^ | Model A | Model B |
|:---|:---:|---:|
| first | $|x|$ | `a|b` |
| ^ | a\|b | \> |
|---|----|---|
| second | \^ | **0.2** |

Note with [@beven].

Second note.
:::
"""
    (table,) = parse_source(source).blocks
    assert isinstance(table, ast.TableBlock)
    assert table.alignment == ("left", "center", "right")
    assert table.header[0][0].row_span == 2
    assert table.header[0][1].col_span == 2
    assert table.header[0][2].covered and table.rows[1][0].covered
    assert table.midrule_after == (1,)
    assert len(table.rows) == 3
    assert len(table.notes) == 2
    first = table.rows[0]
    assert isinstance(first[1].inlines[0], ast.InlineMath)
    assert isinstance(first[2].inlines[0], ast.CodeSpan)
    for cell, raw in zip(first, ("first", "$|x|$", "`a|b`"), strict=True):
        assert table.clean_source[cell.span.start : cell.span.end] == raw
    assert table.rows[1][1].inlines[0].value == "a|b"
    assert table.rows[1][2].inlines[0].value == ">"
    assert table.rows[2][1].inlines[0].value == "^"


@pytest.mark.parametrize(
    "rows",
    [
        "| A | > |\n| ^ | > |",
        "| A | > |\n| B | ^ |",
        "| A | B |\n| ^ | > |",
        "| > | A |",
        "| ^ | A |",
        "| A | B |\n| --- | --- |\n| ^ | C |",
    ],
)
def test_table_rejects_noncanonical_or_cross_midrule_spans(rows: str) -> None:
    source = f":::{'{table}'} {{#tbl}}\nCaption.\n\n| H | J |\n|---|---|\n{rows}\n:::"
    with pytest.raises(OmmError, match="ERR_TABLE_SPAN_INVALID"):
        parse_source(source)


def test_table_requires_equal_columns_and_one_caption() -> None:
    table = ":::{{table}} {{#tbl}}\n{caption}\n| A | B |\n|---|---|\n{row}\n:::"
    with pytest.raises(OmmError, match="ERR_TABLE_COLUMNS_INVALID"):
        parse_source(table.format(caption="Caption.", row="| A |"))
    with pytest.raises(OmmError, match="ERR_TABLE_INVALID"):
        parse_source(table.format(caption="One.\n\nTwo.", row="| A | B |"))


def test_figure_ids_and_caption_spans_with_paths_containing_spaces() -> None:
    source = """:::{figure} {#figs cols=2}
![高程](<figures/高程 图.png>){#fig-a}
![河网](figures/river.png){#fig-b}

Figure [#fig-a].
:::
"""
    (figure,) = parse_source(source).blocks
    assert isinstance(figure, ast.FigureBlock)
    assert figure.cols == 2
    assert [(sub.block_id, sub.asset_path) for sub in figure.subfigures] == [
        ("fig-a", "figures/高程 图.png"),
        ("fig-b", "figures/river.png"),
    ]
    reference = figure.caption.inlines[1]
    assert figure.clean_source[reference.span.start : reference.span.end] == "[#fig-a]"
    with pytest.raises(OmmError, match="ERR_BLOCK_ID_MISSING"):
        parse_source(source.replace("{#fig-a}", ""))


def test_review_bindings_are_derived_only_from_the_author_reply() -> None:
    (review,) = parse_source(""":::{review-item} {#R1-C3 reviewer="Reviewer 1"}
:::{comment}
See [#ghost] and [#loc:ghost].
:::

Reply links **[#sec-main]** at [#loc:pa..pb], again [#sec-main]. {#p-reply}
:::
""").blocks
    assert isinstance(review, ast.ReviewItem)
    assert review.reviewer == "Reviewer 1"
    assert review.referenced_block_ids == ("sec-main",)
    assert [(loc.start_id, loc.end_id) for loc in review.location_refs] == [("pa", "pb")]
    assert review.reply[0].block_id == "p-reply"
    assert review.clean_source[review.comment.span.start : review.comment.span.end] == (
        "See [#ghost] and [#loc:ghost].\n"
    )


def test_bibtex_nested_fields_and_duplicate_key() -> None:
    source = (
        '@string{org = "Hydrology"}\r\n'
        r'@article{key1, title={State of {the {Art}}}, note="A \"Quote\" with {brace \"pair}",'
        '\r journal=org # " Letters", year=2026}\n'
    )
    entry = parse_bib(source)["key1"]
    assert entry.entry_type == "article"
    assert entry.citekey == "key1"
    assert entry.fields == {
        "title": "State of {the {Art}}",
        "note": r"A \"Quote\" with {brace \"pair}",
        "journal": "Hydrology Letters",
        "year": "2026",
    }
    with pytest.raises(OmmError, match="ERR_BIB_DUPLICATE_KEY") as error:
        parse_bib(source + "@book(key1, title={Duplicate})")
    assert error.value.diagnostic.line == 4
    assert error.value.diagnostic.params["key"] == "key1"


def test_check_aggregates_workspace_errors_and_hashes_figure_assets(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    init_workspace(root, state_dir=tmp_path / "state")
    manifest = root / "manuscript.toml"
    write(
        manifest,
        manifest.read_text(encoding="utf-8")
        + """
[deliverables.supplementary]
kind = "supplementary"
sources = ["supplement.md"]

[deliverables.response_letter]
kind = "response_letter"
sources = ["reply.md"]
""",
    )
    write(
        root / "sections" / "01-introduction.md",
        """# Hello {#sec-introduction}

First. {#p-first}

Second with [#out-pending], [#absent], and @unknown. {#p-second}

Bare paragraph.

:::{outline} {#out-pending}
- Draft
:::

:::{figure} {#fig-main cols=2}
![Existing](<figures/已存在 图.png>){#fig-existing}
![Missing](figures/missing.png){#fig-missing}

Caption. {#still-caption-text}
:::
""",
    )
    write(root / "supplement.md", "# Supplement {#sec-introduction}\n\nText. {#p-sup}\n")
    write(
        root / "reply.md",
        """:::{review-item} {#R1 reviewer="Reviewer 1"}
:::{comment}
Please see [#ignored] and @not-in-bib.
:::
The revisions are at [#loc:p-first..p-sup], [#loc:p-second..p-first], [#loc:fig-main],
and [#loc:p-first..p-second]. {#p-reply}
:::
""",
    )
    open_annotation = {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "block_id": "p-first",
        "base_rev": "a" * 40,
        "selector": {"kind": "block"},
        "comment": "Clarify the first result.",
        "created_at": "2026-11-18T00:00:00Z",
        "before_block_md": "First.",
        "resolutions": [],
    }
    resolution = {
        "resolved_at": "2026-11-18T00:01:00Z",
        "after_rev": "b" * 40,
        "after_block_md": "Revised.",
        "reopened_at": None,
    }
    write(
        root / ".omm" / "annotations.json",
        json.dumps(
            {
                "schema_version": 1,
                "items": [
                    open_annotation,
                    {
                        **open_annotation,
                        "id": "550e8400-e29b-41d4-a716-446655440001",
                        "resolutions": [resolution],
                    },
                    {
                        **open_annotation,
                        "id": "550e8400-e29b-41d4-a716-446655440002",
                        "block_id": "p-second",
                        "before_block_md": "Second.",
                        "resolutions": [{**resolution, "reopened_at": "2026-11-18T00:02:00Z"}],
                    },
                ],
            }
        ),
    )
    picture = b"figure content"
    (root / "figures" / "已存在 图.png").write_bytes(picture)
    result = check_workspace(root)
    assert not result.ok
    assert result.asset_sha256 == {"figures/已存在 图.png": hashlib.sha256(picture).hexdigest()}
    assert sorted(issue.code for issue in result.diagnostics) == sorted(
        [
            "ERR_BLOCK_ID_MISSING",
            "ERR_DUPLICATE_BLOCK_ID",
            "ERR_REFERENCE_OUTLINE",
            "ERR_REFERENCE_MISSING",
            "ERR_CITATION_MISSING",
            "ERR_FIGURE_ASSET_MISSING",
            "ERR_LOCATION_RANGE_DELIVERABLE",
            "ERR_LOCATION_RANGE_ORDER",
            "ERR_LOCATION_UNAVAILABLE",
            "ERR_OUTLINE_UNEXPANDED",
            "ERR_ANNOTATION_OPEN",
            "ERR_ANNOTATION_OPEN",
        ]
    )
    assert [issue.line for issue in result.diagnostics if issue.code == "ERR_BLOCK_ID_MISSING"] == [
        7
    ]
    assert {issue.code for issue in result.diagnostics if issue.severity == "warning"} == {
        "ERR_OUTLINE_UNEXPANDED",
        "ERR_ANNOTATION_OPEN",
    }
    assert [
        (issue.block_id, issue.params["id"])
        for issue in result.diagnostics
        if issue.code == "ERR_ANNOTATION_OPEN"
    ] == [
        ("p-first", open_annotation["id"]),
        ("p-second", "550e8400-e29b-41d4-a716-446655440002"),
    ]


def test_stamp_preflights_duplicate_ids_across_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    init_workspace(root, state_dir=tmp_path / "state")
    path = root / "sections" / "01-introduction.md"
    write(path, "# Heading {#sec-same}\n\nUnstamped.\n\nOther {#sec-same}\n")
    before = path.read_bytes()
    with pytest.raises(OmmError, match="ERR_DUPLICATE_BLOCK_ID"):
        stamp_workspace(root)
    assert path.read_bytes() == before


def test_setext_heading_uses_the_last_line_for_its_block_id() -> None:
    (heading,) = parse_source("流域 𝑄\r\n=== {#sec-flow}\r\n").blocks
    assert isinstance(heading, ast.Heading)
    assert heading.block_id == "sec-flow"
    assert heading.level == 1
    assert heading.clean_source == "流域 𝑄\n==="
    assert heading.inlines[0].span == ast.Span(0, len("流域 𝑄"))


def test_stamp_preserves_code_fences_and_existing_directive_attributes(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    init_workspace(root, state_dir=tmp_path / "state")
    section = root / "sections" / "01-introduction.md"
    source = """Intro
=====

- Example:

  ```txt
  A literal marker {#literal}.
  ```

$$x_i$$ {unnumbered}

:::{review-item} {reviewer="Reviewer 1"}
:::{comment}
It needs discussion.
:::

Clarification.
:::
"""
    write(section, source)
    assert stamp_workspace(root) == 5
    stamped = section.read_text(encoding="utf-8")
    heading, example, equation, review = parse_source(stamped).blocks
    assert isinstance(heading, ast.Heading) and heading.block_id.startswith("sec-")
    assert isinstance(example, ast.ListBlock) and example.block_id.startswith("li-")
    assert example.clean_source == "- Example:\n\n  ```txt\n  A literal marker {#literal}.\n  ```"
    assert isinstance(equation, ast.DisplayMath) and equation.unnumbered
    assert isinstance(review, ast.ReviewItem) and review.reviewer == "Reviewer 1"
    assert isinstance(review.reply[0], ast.Paragraph) and review.reply[0].block_id.startswith("p-")


def test_check_reports_independent_syntax_errors_in_one_source(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    init_workspace(root, state_dir=tmp_path / "state")
    section = root / "sections" / "01-introduction.md"
    write(section, "# Invalid ID {#x}\n\n" + r"$$\tag{2} x$$ {#eq}" + "\n\nAn unlabelled block.")
    result = check_workspace(root)
    assert [(item.code, item.line) for item in result.diagnostics] == [
        ("ERR_BLOCK_ID_INVALID", 1),
        ("ERR_MATH_TAG_FORBIDDEN", 3),
        ("ERR_BLOCK_ID_MISSING", 5),
    ]


def test_stamp_does_not_edit_literal_content_inside_a_list(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    init_workspace(root, state_dir=tmp_path / "state")
    section = root / "sections" / "01-introduction.md"
    source = "> - Read this:\n>   ```txt\n>   Code with a literal marker {#example}"
    write(section, source)
    assert stamp_workspace(root) == 1
    (quote,) = parse_source(section.read_text(encoding="utf-8")).blocks
    assert isinstance(quote, ast.MarkdownBlock)
    (example,) = quote.children
    assert isinstance(example, ast.ListBlock)
    assert example.block_id.startswith("li-")
    assert example.clean_source == source
