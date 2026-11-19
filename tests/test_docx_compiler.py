"""Exercise native Word markup and both projections using assembled snapshots."""

from __future__ import annotations

import re
import struct
import zlib
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from lxml import etree

from omm import ast
from omm.bib import BibEntry
from omm.docx_compiler import (
    accept_docx_revisions,
    compile_docx_workspace,
    reject_docx_revisions,
    verify_docx_package,
)
from omm.errors import OmmError
from omm.git_revs import RevisionBlockEntry, RevisionSnapshot
from omm.parser import parse_source
from omm.workspace import Deliverable, Style, Workspace

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
}
W = "{" + NS["w"] + "}"
REVISION_ATTRS = {"w:id", "w:author", "w:date"}


def _xml(node: etree._Element, expression: str) -> list:
    return node.xpath(expression, namespaces=NS)


def _read(package: Path, part: str = "word/document.xml") -> etree._Element:
    with ZipFile(package) as archive:
        return etree.fromstring(archive.read(part))


def _parts(package: Path) -> dict[str, bytes]:
    with ZipFile(package) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _snapshot(
    manuscript: str,
    *,
    supplementary: str = "",
    response: str = "",
    bibliography: tuple[BibEntry, ...] = (),
    assets: dict[str, bytes] | None = None,
    style: Style | None = None,
    revision: str = "a",
) -> RevisionSnapshot:
    sources = {"manuscript": ("manuscript", manuscript)}
    if supplementary:
        sources["supplementary"] = ("supplementary", supplementary)
    if response:
        sources["response_letter"] = ("response_letter", response)
    docs = {f"{name}.md": parse_source(text) for name, (_, text) in sources.items()}
    publish = {name: Deliverable(kind, (f"{name}.md",)) for name, (kind, _) in sources.items()}
    workspace = Workspace(
        Path("."), "workspace", "River study", "A. Author", "refs.bib", style or Style(), publish
    )
    entries = {
        block.block_id: RevisionBlockEntry(path, (path.removesuffix(".md"),), block, None)
        for path, doc in docs.items()
        for block in ast.walk_blocks(doc.blocks)
        if block.block_id is not None
    }
    return RevisionSnapshot(
        revision * 40,
        workspace,
        {bib.citekey: bib for bib in bibliography},
        docs,
        entries,
        assets or {},
    )


def _assert_revision(
    element: etree._Element,
    author: str = "oh-my-manuscript",
    date: str = "2026-01-01T00:00:00Z",
) -> None:
    assert set(element.attrib) == {W + expanded.split(":")[1] for expanded in REVISION_ATTRS}
    assert int(element.get(W + "id")) > 0
    assert element.get(W + "author") == author
    assert element.get(W + "date") == date


def _text(element: etree._Element) -> str:
    return "".join(_xml(element, ".//w:t/text() | .//m:t/text()"))


def _shape(element: etree._Element) -> tuple:
    return (
        element.tag,
        tuple(sorted(element.attrib.items())),
        element.text,
        tuple(map(_shape, element)),
    )


def _visible(package: Path, document: etree._Element) -> tuple:
    """Decode the package independently of the compiler and ignore generated IDs."""
    parts = _parts(package)
    rels = {
        item.get("Id"): item.get("Target")
        for item in etree.fromstring(parts["word/_rels/document.xml.rels"])
    }
    abstracts = {
        item.get(W + "abstractNumId"): _shape(item)
        for item in _xml(etree.fromstring(parts["word/numbering.xml"]), "w:abstractNum")
    }
    lists = {
        item.get(W + "numId"): (abstracts[item[0].get(W + "val")], tuple(map(_shape, item[1:])))
        for item in _xml(etree.fromstring(parts["word/numbering.xml"]), "w:num")
    }
    assert not _xml(document, "//w:ins | //w:del | //w:delText")
    names = _xml(document, "//w:bookmarkStart/@w:name")
    assert len({name.casefold() for name in names}) == len(names)
    assert set(_xml(document, "//w:hyperlink/@w:anchor")) <= set(names)
    ids = _xml(document, "//w:bookmarkStart/@w:id")
    assert sorted(ids) == sorted(_xml(document, "//w:bookmarkEnd/@w:id"))

    def visit(node: etree._Element) -> tuple:
        if node.tag == W + "drawing":
            image = _xml(node, ".//a:blip/@r:embed")[0]
            width, height = _xml(node, ".//wp:extent/@cx | .//wp:extent/@cy")
            return "image", sha256(parts["word/" + rels[image]]).hexdigest(), width, height
        if node.tag == W + "numId":
            return "list", lists[node.get(W + "val")]
        if node.tag in {"{" + NS["m"] + "}" + name for name in ("oMath", "oMathPara")}:
            return _shape(node)
        attributes = tuple(
            sorted((key, val) for key, val in node.attrib.items() if key != W + "anchor")
        )
        return (
            node.tag,
            attributes,
            node.text,
            tuple(
                visit(child)
                for child in node
                if child.tag not in {W + "bookmarkStart", W + "bookmarkEnd"}
            ),
        )

    return visit(document.find(W + "body"))


def _redline(
    tmp_path: Path,
    old: RevisionSnapshot,
    new: RevisionSnapshot,
    *,
    name: str = "manuscript",
    **kwargs,
) -> tuple[Path, etree._Element]:
    milestone = compile_docx_workspace(old, tmp_path / "old")[name]
    current = compile_docx_workspace(new, tmp_path / "new", milestone=old, **kwargs)[name]
    marked = current.marked_path
    assert marked is not None
    doc = _read(marked)
    for project, clean in (
        (accept_docx_revisions, current.clean_path),
        (reject_docx_revisions, milestone.clean_path),
    ):
        projected = project(doc)
        assert _visible(marked, projected) == _visible(clean, _read(clean))
    return marked, doc


def _png(pixel: int, *, width: int = 2, height: int = 3, dpi_per_meter: int = 4000) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    pixels = (b"\0" + bytes([pixel]) * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"pHYs", struct.pack(">IIB", dpi_per_meter, dpi_per_meter, 1))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def _entry(key: str, title: str) -> BibEntry:
    return BibEntry("article", key, {"author": "Rivers, Ada", "year": "2020", "title": title})


def test_clean_layout_determinism_bookmarks_and_references(tmp_path: Path) -> None:
    prefix = "sec-" + "a" * 57
    first, second = prefix + "-b1", prefix + "-b2"
    raw = f"""# Headwater {{#{first}}}

Figure 1. See [#{first}], [#{second}] and [@rivers]. {{#p-intro}}

:::\u007boutline\u007d {{#out-plan}}
Unpublished notes [@unused].
:::

## Streams {{#{second}}}

1. First **strong**
2. Second $x_i$ {{#li-items}}

- Bullet {{#li-bullet}}

$$
Q = x^2
$$ {{#eq-runoff}}
"""
    snapshot = _snapshot(raw, bibliography=(_entry("rivers", "Flow"), _entry("unused", "Draft")))
    results = compile_docx_workspace(snapshot, tmp_path / "one")
    item = results["manuscript"]
    repeat = compile_docx_workspace(snapshot, tmp_path / "two")["manuscript"]
    assert item.clean_path.read_bytes() == repeat.clean_path.read_bytes()
    assert item.marked_path is None
    verify_docx_package(item.clean_path)
    with ZipFile(item.clean_path) as archive:
        assert archive.namelist() == sorted(archive.namelist())
        assert {info.date_time for info in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}
    document = _read(item.clean_path)
    assert [record.kind for record in item.blocks] == [
        "heading",
        "paragraph",
        "heading",
        "list",
        "list",
        "display_math",
    ]
    assert "First strong\nSecond" in item.blocks[3].plain_text
    assert "Unpublished" not in _text(document) and "Draft" not in _text(document)
    assert "Figure 1." in _text(document)
    assert len(_xml(document, "//w:hyperlink")) == 3
    assert _xml(document, "//w:hyperlink/w:r/w:rPr/w:color/@w:val") == ["0070C0"] * 3
    assert not _xml(document, "//w:r[w:t='Figure']/w:rPr/w:color")
    assert _xml(document, "//w:sectPr/w:lnNumType/@w:restart") == ["continuous"]
    assert _xml(document, "//w:sectPr/w:lnNumType/@w:distance") == ["360"]
    assert len(_xml(document, "//w:pPr/w:numPr")) == 3
    bookmarks = _xml(document, "//w:bookmarkStart/@w:name")
    assert len(bookmarks) == len(set(bookmarks))
    for record in item.blocks:
        for name in (record.bookmark_name, record.start_bookmark, record.end_bookmark):
            assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,39}", name)
            assert name in bookmarks
        start = next(
            node
            for node in _xml(document, "//w:bookmarkStart")
            if node.get(W + "name") == record.start_bookmark
        )
        end = next(
            node
            for node in _xml(document, "//w:bookmarkStart")
            if node.get(W + "name") == record.end_bookmark
        )
        assert start.getnext().tag == W + "bookmarkEnd"
        assert end.getnext().tag == W + "bookmarkEnd"
        assert end.getnext().getnext().tag == W + "bookmarkEnd"


def test_atomic_math_replacements_and_paragraph_marks(tmp_path: Path) -> None:
    old_text = r"""# Scope {#sec-scope}

Old lead. {#p-old}

Threshold $\begin{cases}x & \text{if } x>0 \\ 0 & \text{otherwise}\end{cases}$ applies. {#p-inline}

$$
\begin{aligned} x &= 1 \\ y &= 2 \end{aligned}
$$ {#eq-array}

$$
x^2
$$ {#eq-plain unnumbered}

Link to [#p-moved]. {#p-link}

Moved material. {#p-moved}

# Test {#sec-test}
"""
    new_text = (
        old_text.replace("Old lead. {#p-old}", "$$u=1$$ {#eq-added}")
        .replace("x>0", "x>1")
        .replace("y &= 2", "y &= 3")
        .replace("x^2", "x^3")
        .replace(
            "Moved material. {#p-moved}\n\n# Test {#sec-test}",
            "# Test {#sec-test}\n\nRevised material. {#p-moved}\n\nNew ending. {#p-new}",
        )
    )
    marked, doc = _redline(
        tmp_path,
        _snapshot(old_text),
        _snapshot(new_text, revision="b"),
        revision_author="Editor",
        revision_date_utc="2025-06-13T00:00:00Z",
    )
    for revision in _xml(doc, "//w:ins | //w:del"):
        _assert_revision(revision, "Editor", "2025-06-13T00:00:00Z")
    for opcode in ("del", "ins"):
        (formula,) = _xml(doc, f"//w:p/w:{opcode}/m:oMath[.//m:d]")
        assert _xml(formula, ".//m:r[m:rPr/m:nor]/m:t/text()") == ["if\u00a0", "otherwise"]
        assert not _xml(formula, ".//m:rPr[m:nor]/m:scr | .//m:rPr[m:nor]/m:sty")
    (paragraph,) = _xml(doc, "//w:p[w:del/m:oMath//m:eqArr]")
    assert _xml(paragraph, "w:pPr/w:tabs/w:tab/@w:pos") == ["9026"]
    assert _xml(paragraph, "w:del/w:r/w:delText/text()") == ["(1)"]
    assert _xml(paragraph, "w:ins/w:r/w:t/text()") == ["(2)"]
    assert _xml(paragraph, "w:del[m:oMath]/following-sibling::*[1][self::w:ins[m:oMath]]")
    assert _xml(doc, "//w:del/m:oMathPara/m:oMath")
    assert _xml(doc, "//w:ins/m:oMathPara/m:oMath")
    assert not _xml(doc, "//m:oMath//m:oMath | //w:del//w:t")
    (deleted,) = _xml(doc, "//w:p[w:del/w:r/w:delText='Old']")
    assert _xml(deleted, "w:pPr/w:rPr/*[1][self::w:del]")
    (moved,) = _xml(doc, "//w:p[w:del/w:r/w:delText='Moved']")
    assert _xml(moved, "w:pPr/w:rPr/*[1][self::w:del]")
    (inserted,) = _xml(doc, "//w:p[w:ins/w:r/w:t='Revised']")
    assert _xml(inserted, "w:pPr/w:rPr/*[1][self::w:ins]")
    (heading,) = _xml(doc, "//w:p[w:r/w:t='Test']")
    assert not _xml(heading, ".//w:del | .//w:ins")
    settings = _read(marked, "word/settings.xml")
    assert [etree.QName(child).localname for child in settings][:4] == [
        "proofState",
        "trackRevisions",
        "defaultTabStop",
        "compat",
    ]


@pytest.mark.parametrize("opcode", ["ins", "del"])
def test_subheading_edits_leave_existing_paragraphs_unmarked(tmp_path: Path, opcode: str) -> None:
    source = """# Methods {#sec-methods}

First unchanged paragraph. {#p-first}

Second unchanged paragraph. {#p-second}

Third unchanged paragraph. {#p-third}
"""
    with_subheading = source.replace(
        "Second unchanged", "## Subheading {#sec-sub}\n\nSecond unchanged"
    )
    before, after = (source, with_subheading) if opcode == "ins" else (with_subheading, source)
    _, doc = _redline(tmp_path, _snapshot(before), _snapshot(after, revision="b"))
    (heading,) = _xml(doc, "//w:p[.//w:ins or .//w:del]")
    assert _xml(heading, "w:pPr/w:pStyle/@w:val") == ["Heading2"]
    assert _xml(heading, f"w:pPr/w:rPr/w:{opcode}")
    text_tag = "t" if opcode == "ins" else "delText"
    assert "Subheading" in _xml(heading, f"w:{opcode}/w:r/w:{text_tag}/text()")


TABLE = r""":::{table} {#tbl-metrics}
Model metrics.

| Basin | Group | > | East |
| ^ | A | B | C |
|:---|:---:|:---:|---:|
| Joined | > | $x$ | 0.2 |
| ^ | ^ | 0.3 | 0.4 |
|---|---|---|---|
| Last | 0.5 | 0.6 | 0.7 |

Note on units.
:::
"""


@pytest.mark.parametrize("same_grid", [True, False])
def test_native_table_revisions_preserve_grid_and_borders(tmp_path: Path, same_grid: bool) -> None:
    updated = (
        TABLE.replace("Model metrics.", "Updated model metrics.")
        .replace("$x$", "$y$")
        .replace("Note on units.", "Note on units and scale.")
    )
    if not same_grid:
        updated = updated.replace("| Joined | > |", "| North | South |").replace(
            "| ^ | ^ | 0.3", "| West | East | 0.3"
        )
    adjacent = TABLE.replace("tbl-metrics", "tbl-other")
    _, doc = _redline(
        tmp_path,
        _snapshot(TABLE + "\n" + adjacent),
        _snapshot(updated + "\n" + adjacent, revision="b"),
    )
    tables = _xml(doc, "//w:body/w:tbl")
    old = tables[0]
    assert _xml(old, "w:tblPr/w:tblBorders/w:top/@w:sz | w:tblPr/w:tblBorders/w:bottom/@w:sz") == [
        "12",
        "12",
    ]
    assert _xml(old, "w:tr[1]/w:tc[2]/w:tcPr/w:tcBorders/w:bottom/@w:sz") == ["4"]
    assert set(_xml(old, "w:tr[2]/w:tc/w:tcPr/w:tcBorders/w:bottom/@w:sz")) == {"6"}
    assert set(_xml(old, "w:tr[4]/w:tc/w:tcPr/w:tcBorders/w:bottom/@w:sz")) == {"4"}
    assert _xml(old, "w:tr[4]/w:tc[1]/w:tcPr/w:gridSpan/@w:val") == ["2"]
    assert _xml(old, "w:tr[4]/w:tc[1]/w:tcPr/w:vMerge[not(@w:val)]")
    assert not _xml(doc, "//w:tbl/following-sibling::*[1][self::w:tbl]")
    if same_grid:
        assert len(tables) == 2
        assert _xml(old, ".//w:p/w:del/m:oMath") and _xml(old, ".//w:p/w:ins/m:oMath")
        assert not _xml(old, "w:tr/w:trPr/w:ins | w:tr/w:trPr/w:del")
    else:
        assert len(tables) == 3
        for table, mark in zip(tables[:2], ("del", "ins"), strict=True):
            assert len(_xml(table, f"w:tr/w:trPr/w:{mark}")) == len(_xml(table, "w:tr"))
            assert len(_xml(table, f".//w:tc/w:p/w:pPr/w:rPr/w:{mark}")) == len(
                _xml(table, ".//w:tc")
            )
            caption = table.getprevious()
            assert _xml(caption, f"w:pPr/w:rPr/w:{mark}")
            note = table.getnext()
            assert _xml(note, f"w:pPr/w:rPr/w:{mark}")


def test_table_group_rule_spans_shifted_header_groups(tmp_path):
    source = """:::{table} {#tbl-groups}
Groups.

| A | Group | > | Repeated | > |
| Shifted | > | > | Inner | > |
| C | D | E | F | G |
|---|---|---|---|---|
| 1 | 2 | 3 | 4 | 5 |
:::
"""
    clean = compile_docx_workspace(_snapshot(source), tmp_path)["manuscript"].clean_path
    doc = _read(clean)
    assert _xml(doc, "//w:tr[1]/w:tc[2]/w:tcPr/w:tcBorders/w:bottom/@w:sz") == ["4"]
    assert not _xml(doc, "//w:tr[1]/w:tc[3]/w:tcPr/w:tcBorders/w:bottom")
    assert _xml(doc, "//w:tr[2]/w:tc[2]/w:tcPr/w:tcBorders/w:bottom/@w:sz") == ["4"]


FIGURE = """:::{figure} {#fig-catchment cols=2}
![Terrain](figures/map.png){#fig-map}
![Channel](figures/channel.png){#fig-channel}

Catchment and [#fig-channel].
:::
"""


def test_changed_bitmap_same_path_and_global_drawing_ids(tmp_path: Path) -> None:
    old_map, new_map, channel = _png(5), _png(7), _png(10)
    old = _snapshot(FIGURE, assets={"figures/map.png": old_map, "figures/channel.png": channel})
    new = _snapshot(
        FIGURE, assets={"figures/map.png": new_map, "figures/channel.png": channel}, revision="b"
    )
    package, doc = _redline(tmp_path, old, new)
    assert _xml(doc, "//wp:docPr/@id") == ["1", "2", "3"]
    assert _xml(doc, "//w:del/w:r/w:drawing/wp:inline")
    assert _xml(doc, "//w:ins/w:r/w:drawing/wp:inline")
    assert _xml(doc, "//wp:extent/@cx") == ["18000"] * 3
    assert _xml(doc, "//wp:extent/@cy") == ["27000"] * 3
    parts = _parts(package)
    assert [parts[f"word/media/image{i}.png"] for i in (1, 2, 3)] == [old_map, new_map, channel]
    assert "(a)" in _text(doc) and "(b)" in _text(doc)


def test_single_figure_without_panel_id_compiles_and_tracks_replacement(tmp_path: Path) -> None:
    source = """See [#fig-terrain]. {#p-intro}

:::{figure} {#fig-terrain}
![Terrain](figures/map.png)

Catchment terrain.
:::
"""
    asset = "figures/map.png"
    old_map, new_map = _png(2), _png(7)
    package, marked = _redline(
        tmp_path,
        _snapshot(source, assets={asset: old_map}),
        _snapshot(source, assets={asset: new_map}, revision="b"),
    )
    clean_path = tmp_path / "new" / "manuscript.clean.docx"
    clean = _read(clean_path)
    assert _xml(clean, "//w:p[w:r/w:drawing]/w:bookmarkStart") == []
    assert _xml(clean, "//w:hyperlink/@w:anchor") == _xml(
        clean, "//w:p[w:pPr/w:pStyle/@w:val='Caption']/w:bookmarkStart/@w:name"
    )
    assert _parts(clean_path)["word/media/image1.png"] == new_map
    assert _xml(marked, "//wp:docPr/@id") == ["1", "2"]
    assert _xml(marked, "//w:del/w:r/w:drawing") and _xml(marked, "//w:ins/w:r/w:drawing")
    media = _parts(package)
    assert [media[f"word/media/image{index}.png"] for index in (1, 2)] == [old_map, new_map]


def test_bibliography_disambiguation_and_word_revisions(tmp_path: Path) -> None:
    old = _snapshot("For [@a]. {#p-cite}\n", bibliography=(_entry("a", "First river"),))
    new = _snapshot(
        "For [@a; @b]. {#p-cite}\n",
        bibliography=(
            _entry("a", "First river upstream"),
            _entry("b", "Second river"),
        ),
        revision="b",
    )
    _, doc = _redline(tmp_path, old, new)
    bibliography = "//w:p[w:pPr/w:pStyle/@w:val='Bibliography']"
    retained, added = _xml(doc, bibliography)
    assert _xml(retained, "w:del/w:r/w:delText[text()='2020']")
    assert _xml(retained, "w:ins/w:r/w:t[text()='2020a']")
    assert _xml(added, "w:pPr/w:rPr/w:ins")
    assert _xml(doc, "//w:hyperlink/w:ins/w:r/w:rPr/w:color/@w:val") == ["0070C0", "0070C0"]
    assert "(Rivers, 2020a; Rivers, 2020b)" in _text(accept_docx_revisions(doc))
    assert "(Rivers, 2020)" in _text(reject_docx_revisions(doc))


def test_numeric_bibliography_lcs_and_heading_visibility(tmp_path: Path) -> None:
    style = Style(citation_style="numeric")
    bib = tuple(_entry(key, key.upper()) for key in "abc")
    old = _snapshot("Text without citations. {#p-cite}\n", style=style, bibliography=bib)
    mid = _snapshot("Text [@a; @b; @c]. {#p-cite}\n", style=style, bibliography=bib)
    new = _snapshot("Text [@c; @a; @b]. {#p-cite}\n", style=style, bibliography=bib)
    _, created = _redline(tmp_path / "create", old, mid)
    assert _xml(created, "//w:p[w:ins/w:r/w:t='References']/w:pPr/w:rPr/w:ins")
    _, removed = _redline(tmp_path / "remove", mid, old)
    assert _xml(removed, "//w:p[w:del/w:r/w:delText='References']/w:pPr/w:rPr/w:del")
    _, doc = _redline(tmp_path / "move", mid, new)
    assert len(_xml(doc, "//w:p[w:pPr/w:pStyle/@w:val='Bibliography']/w:pPr/w:rPr/w:del")) == 1
    assert len(_xml(doc, "//w:p[w:pPr/w:pStyle/@w:val='Bibliography']/w:pPr/w:rPr/w:ins")) == 1


REPLY = r""":::{review-item} {#R1-C3 reviewer="Reviewer 1"}
:::{comment}
Consider [@uncited] and $q$.
:::

We changed [#sec-method] at [#loc:p-main] and cite @cited. {#p-reply}
:::
"""


@pytest.mark.parametrize("placement", ["per-deliverable", "merged-in-main"])
def test_subset_reply_locations_and_bibliography(tmp_path: Path, placement: str) -> None:
    style = Style(link_color="#a1b2c3", bibliography_placement=placement)
    snapshot = _snapshot(
        "# Method {#sec-method}\n\nStudy area. {#p-main}\n",
        supplementary="See [#sec-method] and [@cited]. {#p-supp}\n",
        response=REPLY,
        style=style,
        bibliography=(_entry("cited", "Based on rivers"), _entry("uncited", "Quote only")),
    )
    labels = {("p-main", None): "Main manuscript, page 2, lines 17–19"}
    compiled = compile_docx_workspace(
        snapshot,
        tmp_path / "subset",
        loc_labels=labels,
        deliverables=["response_letter"],
        milestone=snapshot,
    )
    assert list(compiled) == ["response_letter"]
    reply = compiled["response_letter"]
    assert reply.marked_path is None
    assert list(reply.clean_path.parent.iterdir()) == [reply.clean_path]
    doc = _read(reply.clean_path)
    assert not _xml(doc, "//w:lnNumType")
    assert _xml(doc, "//w:p[w:pPr/w:pStyle/@w:val='ReviewComment']//m:oMath")
    assert "[@uncited]" in _text(doc)
    assert _xml(doc, "//w:p/w:r[w:rPr/w:color/@w:val='A1B2C3']/w:t/text()")[:2] == [
        "Section 1",
        labels["p-main", None],
    ]
    bibliography = _xml(doc, "//w:p[w:pPr/w:pStyle/@w:val='Bibliography']")
    assert len(bibliography) == (1 if placement == "per-deliverable" else 0)
    assert len(_xml(doc, "//w:hyperlink[@w:anchor]")) == len(bibliography)
    assert b'TargetMode="External"' not in _parts(reply.clean_path)["word/_rels/document.xml.rels"]
    all_items = compile_docx_workspace(snapshot, tmp_path / "all")
    assert list(all_items) == ["manuscript", "supplementary", "response_letter"]
    assert "manuscript §1 [p-main]" in _text(_read(all_items["response_letter"].clean_path))
    main = _read(all_items["manuscript"].clean_path)
    assert len(_xml(main, "//w:p[w:pPr/w:pStyle/@w:val='Bibliography']")) == int(
        placement == "merged-in-main"
    )
    supp = _read(all_items["supplementary"].clean_path)
    assert len(_xml(supp, "//w:hyperlink")) == int(placement == "per-deliverable")
    assert _xml(supp, "//w:lnNumType")


def test_unsupported_asset_and_math_conversion_identify_source(tmp_path: Path) -> None:
    snapshot = _snapshot(
        FIGURE, assets={"figures/map.png": b"<svg/>", "figures/channel.png": _png(1)}
    )
    with pytest.raises(OmmError, match="ERR_FIGURE_FORMAT_UNSUPPORTED") as failure:
        compile_docx_workspace(snapshot, tmp_path)
    assert failure.value.diagnostic.params["asset"] == "figures/map.png"

    source = _snapshot("Formula $\\unavailableCommand{x}$. {#p-math}\n")
    with pytest.raises(OmmError, match="ERR_MATH_") as failure:
        compile_docx_workspace(source, tmp_path)
    assert failure.value.diagnostic.block_id == "p-math"
    assert failure.value.diagnostic.params["detail"]
    assert not list(tmp_path.iterdir())


def _zip(parts: dict[str, bytes]) -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return stream.getvalue()


@pytest.mark.parametrize(
    ("mutation", "detail"),
    [
        ("dangling-bookmark", "hyperlink"),
        ("duplicate-bookmark", "bookmark"),
        ("missing-end", "bookmark"),
        ("wrong-content-type", "content type"),
        ("missing-core", "OPC parts"),
        ("missing-line-numbers", "Incorrect document line numbering"),
        ("broken-rel", "relationship"),
        ("duplicate-drawing", "Drawing IDs"),
        ("invalid-track-order", "trackRevisions"),
        ("deleted-plain-text", "text inside revision"),
        ("bad-grid", "grid"),
        ("adjacent-tables", "Adjacent tables"),
        ("nested-math", "OMML equation"),
        ("math-run-order", "out-of-order children in rPr"),
        ("math-run-conflicting-style", "normal text cannot specify a script or style"),
        ("doctype", "Invalid word/document.xml"),
    ],
)
def test_verifier_rejects_corrupt_opc_and_word_markup(
    tmp_path: Path,
    mutation: str,
    detail: str,
) -> None:
    source = (
        "# Section {#sec-head}\n\n"
        "Old [#sec-head] and $x + \\text{measured}$. {#p-edit}\n\n" + FIGURE + "\n" + TABLE
    )
    data = {"figures/map.png": _png(2), "figures/channel.png": _png(4)}
    old = _snapshot(source, assets=data)
    new = _snapshot(source.replace("Old [", "New ["), assets=data, revision="b")
    output = compile_docx_workspace(new, tmp_path, milestone=old)["manuscript"]
    parts = _parts(output.marked_path)
    path = "word/document.xml"
    doc = etree.fromstring(parts[path])
    if mutation == "dangling-bookmark":
        _xml(doc, "//w:hyperlink")[0].set(W + "anchor", "Missing")
    elif mutation == "duplicate-bookmark":
        first, second, *_ = _xml(doc, "//w:bookmarkStart")
        second.set(W + "name", first.get(W + "name"))
    elif mutation == "missing-end":
        node = _xml(doc, "//w:bookmarkEnd")[0]
        node.getparent().remove(node)
    elif mutation == "wrong-content-type":
        path = "[Content_Types].xml"
        doc = etree.fromstring(parts[path])
        doc[2].set("ContentType", "application/json")
    elif mutation == "missing-core":
        del parts["docProps/core.xml"]
    elif mutation == "missing-line-numbers":
        (line_numbers,) = _xml(doc, "//w:sectPr/w:lnNumType")
        line_numbers.getparent().remove(line_numbers)
    elif mutation == "broken-rel":
        path = "word/_rels/document.xml.rels"
        doc = etree.fromstring(parts[path])
        doc[-1].set("Target", "media/image999.png")
    elif mutation == "duplicate-drawing":
        first, second = _xml(doc, "//wp:docPr")
        second.set("id", first.get("id"))
    elif mutation == "invalid-track-order":
        path = "word/settings.xml"
        doc = etree.fromstring(parts[path])
        doc.append(_xml(doc, "w:trackRevisions")[0])
    elif mutation == "deleted-plain-text":
        _xml(doc, "//w:delText")[0].tag = W + "t"
    elif mutation == "bad-grid":
        _xml(doc, "//w:gridSpan")[0].set(W + "val", "99")
    elif mutation == "adjacent-tables":
        table = _xml(doc, "//w:tbl")[0]
        table.addnext(etree.fromstring(etree.tostring(table)))
    elif mutation == "nested-math":
        math = _xml(doc, "//m:oMath")[0]
        math.append(etree.fromstring(etree.tostring(math)))
    elif mutation in {"math-run-order", "math-run-conflicting-style"}:
        (properties,) = _xml(doc, "//m:rPr[m:nor]")
        if mutation == "math-run-order":
            properties.insert(0, etree.Element(f"{{{NS['m']}}}aln"))
        else:
            etree.SubElement(properties, f"{{{NS['m']}}}sty").set(f"{{{NS['m']}}}val", "p")
    parts[path] = etree.tostring(doc)
    if mutation == "doctype":
        parts[path] = b'<!DOCTYPE document [<!ENTITY hazard "x">]>' + parts[path]
    with pytest.raises(OmmError, match="ERR_DOCX_PACKAGE_INVALID") as failure:
        verify_docx_package(_zip(parts))
    assert detail in failure.value.diagnostic.params["detail"]


def _jpeg_fixture() -> bytes:
    def segment(marker: int, data: bytes) -> bytes:
        return struct.pack(">BBH", 0xFF, marker, len(data) + 2) + data

    jfif = b"JFIF\0" + struct.pack(">BBBHHBB", 1, 1, 1, 144, 144, 0, 0)
    frame = struct.pack(">BHHBBBB", 8, 8, 8, 1, 1, 0x11, 0)
    huffman = bytes([0, 1]) + bytes(16) + bytes([0x10, 1]) + bytes(16)
    return (
        b"\xff\xd8"
        + segment(0xE0, jfif)
        + segment(0xDB, b"\0" + b"\1" * 64)
        + segment(0xC0, frame)
        + segment(0xC4, huffman)
        + segment(0xDA, bytes([1, 1, 0, 0, 63, 0]))
        + b"\x3f\xff\xd9"
    )


def _tiff_fixture() -> bytes:
    fields = {
        256: (4, 2),
        257: (4, 3),
        258: (3, 8),
        259: (3, 1),
        262: (3, 1),
        273: (4, 0),
        277: (3, 1),
        278: (4, 3),
        279: (4, 6),
        282: (5, 0),
        283: (5, 0),
        296: (3, 2),
    }
    density = 8 + 2 + 12 * len(fields) + 4
    fields[273], fields[282], fields[283] = (4, density + 16), (5, density), (5, density + 8)
    return (
        b"II"
        + struct.pack("<HIH", 42, 8, len(fields))
        + b"".join(struct.pack("<HHII", tag, kind, 1, val) for tag, (kind, val) in fields.items())
        + struct.pack("<IIIII", 0, 72, 1, 72, 1)
        + bytes(6)
    )


@pytest.mark.parametrize(
    ("extension", "data", "extent"),
    [
        ("jpg", _jpeg_fixture(), ("50800", "50800")),
        (
            "gif",
            bytes.fromhex("47494638396101000100800000000000ffffff2c00000000010001000002024401003b"),
            ("9525", "9525"),
        ),
        (
            "bmp",
            b"BM"
            + struct.pack("<IHHI", 58, 0, 0, 54)
            + struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 4000, 4000, 0, 0)
            + bytes(4),
            ("9000", "9000"),
        ),
        ("tif", _tiff_fixture(), ("25400", "38100")),
    ],
)
def test_native_bitmap_dimensions_without_a_decoder_dependency(
    tmp_path: Path,
    extension: str,
    data: bytes,
    extent: tuple[str, str],
) -> None:
    figure = """:::{figure} {#fig-example}
![Single panel](figures/panel.asset){#fig-panel}

Single panel.
:::
"""
    snapshot = _snapshot(figure, assets={"figures/panel.asset": data})
    path = compile_docx_workspace(snapshot, tmp_path)["manuscript"].clean_path
    (drawing,) = _xml(_read(path), "//wp:extent")
    assert (drawing.get("cx"), drawing.get("cy")) == extent
    assert _parts(path)[f"word/media/image1.{extension}"] == data


def test_empty_math_is_valid_omml_and_projects_back_to_clean(tmp_path: Path) -> None:
    old = _snapshot("$$ $$ {#eq-empty}\n")
    new = _snapshot("$$ x $$ {#eq-empty}\n", revision="b")
    _, marked = _redline(tmp_path, old, new)
    assert _xml(marked, "//w:del/m:oMath[not(*)]")
    assert _xml(marked, "//w:ins/m:oMath[m:r/m:t='x']")


def test_projector_merges_surviving_runs_and_does_not_mutate_input() -> None:
    ns = NS["w"]
    for op, project, inverse in (
        ("del", accept_docx_revisions, reject_docx_revisions),
        ("ins", reject_docx_revisions, accept_docx_revisions),
    ):
        raw = f"""<w:document xmlns:w="{ns}"><w:body>
        <w:p><w:pPr><w:rPr><w:{op} w:id="1" w:author="Editor"/></w:rPr></w:pPr>
        <w:r><w:t>First </w:t></w:r></w:p>
        <w:p><w:pPr><w:pStyle w:val="Normal"/></w:pPr><w:r><w:t>last.</w:t></w:r></w:p>
        <w:sectPr/></w:body></w:document>"""
        tree = etree.fromstring(raw.encode())
        original = etree.tostring(tree)
        merged = project(tree)
        (paragraph,) = _xml(merged, "//w:body/w:p")
        assert _text(paragraph) == "First last."
        assert _xml(paragraph, "w:pPr/w:pStyle/@w:val") == ["Normal"]
        assert len(_xml(inverse(tree), "//w:body/w:p")) == 2
        assert not _xml(merged, "//w:ins | //w:del")
        assert etree.tostring(tree) == original


def test_supplementary_revisions_and_marked_bytes_are_repeatable(tmp_path: Path) -> None:
    before = _snapshot("Main. {#p-main}\n", supplementary="Old. {#p-sup}\n")
    after = _snapshot("Main. {#p-main}\n", supplementary="New. {#p-sup}\n", revision="b")
    path, marked = _redline(
        tmp_path,
        before,
        after,
        name="supplementary",
        deliverables=["supplementary"],
    )
    again = compile_docx_workspace(
        after,
        tmp_path / "repeat",
        milestone=before,
        deliverables=["supplementary"],
    )
    assert list(again) == ["supplementary"]
    assert path.read_bytes() == again["supplementary"].marked_path.read_bytes()
    assert _xml(marked, "//w:lnNumType/@w:countBy") == ["1"]


def test_nested_lists_restart_and_preserve_code(tmp_path: Path) -> None:
    source = """3. ## Ordered
   - Nested bullet
   - Nested second
4. Ordered second {#li-first}

> - Quoted
>   ```txt
>   Retained code
>   ```
> {#li-quoted}
"""
    path = compile_docx_workspace(_snapshot(source), tmp_path)["manuscript"].clean_path
    doc = _read(path)
    assert _xml(doc, "//w:pPr/w:numPr/w:ilvl/@w:val") == ["0", "1", "1", "0", "0"]
    assert _xml(doc, "//w:p[w:pPr/w:numPr and w:pPr/w:pStyle/@w:val='Heading2']//w:t/text()") == [
        "Ordered",
    ]
    assert "Retained code" in _text(doc)
    numbering = _read(path, "word/numbering.xml")
    assert set(_xml(numbering, "//w:startOverride/@w:val")) == {"3"}
