"""Exercise location resolution using real PDF streams and TeX auxiliary records."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    Destination,
    DictionaryObject,
    Fit,
    NameObject,
    TextStringObject,
)

from omm import loc_resolver
from omm.docx_compiler import DocxBlockRecord, compile_docx_workspace
from omm.errors import OmmError
from omm.git_revs import RevisionSnapshot
from omm.loc_resolver import (
    LineSpan,
    format_location_label,
    paginate_docx_headless,
    paginate_latex_headless,
    resolve_blocks_from_pdf,
    resolve_blocks_from_tex_aux,
    resolve_workspace_locations,
)
from omm.numbering import number_workspace
from omm.parser import parse_source
from omm.workspace import Deliverable, Style, Workspace


def _pdf(
    path: Path,
    pages: list[list[tuple[float, float, str] | tuple[float, float, str, float]]],
    *,
    destinations: dict[str, tuple[int, float]] | None = None,
    shared_text_object: bool = False,
) -> Path:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }
    )
    for fragments in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        content = []
        active_size = 10
        for x, y, value, *sizes in fragments:
            size = sizes[0] if sizes else 10
            # A translated graphics state exercises the composition of the two matrices.
            encoded = value.encode("cp1252").hex()
            text = f"1 0 0 1 {x - 3:g} {y - 4:g} Tm <{encoded}> Tj"
            if shared_text_object:
                if active_size != size:
                    content.append(f"/F1 {size:g} Tf")
                    active_size = size
                content.append(text)
            else:
                content.append(f"BT /F1 {size:g} Tf {text} ET")
        if shared_text_object:
            content = ["BT /F1 10 Tf", *content, "ET"]
        content = ["q 1 0 0 1 3 4 cm", *content, "Q"]
        stream.set_data("\n".join(content).encode("ascii"))
        page.replace_contents(stream)
    for name, (page, y) in (destinations or {}).items():
        page_ref = writer.pages[page - 1].indirect_reference
        writer.add_named_destination_object(
            Destination(TextStringObject(name), page_ref, Fit.fit_horizontally(y))
        )
    writer.write(path)
    return path


def _record(block_id: str, text: str, kind: str = "paragraph") -> DocxBlockRecord:
    return DocxBlockRecord(block_id, kind, block_id, f"{block_id}-s", f"{block_id}-e", text)


@pytest.mark.parametrize(
    ("segments", "label"),
    [
        (((2, 105, 105),), "p. 2, line 105"),
        (((2, 105, 109),), "p. 2, lines 105–109"),
        (((2, 105, 109), (3, 110, 113)), "pp. 2–3, lines 105–113"),
        (
            ((2, 105, 109), (3, 1, 1), (4, 1, 7)),
            "p. 2, lines 105–109; p. 3, line 1; p. 4, lines 1–7",
        ),
    ],
)
def test_format_location_label(segments, label):
    span = LineSpan(segments[0][0], segments[0][1], segments[-1][0], segments[-1][2], segments)
    assert format_location_label("Clean.docx", span) == f"Clean.docx, {label}"


@pytest.mark.parametrize("shared_text_object", [False, True])
def test_pdf_matches_printed_numbers_across_pages_and_equations(tmp_path, shared_text_object):
    path = _pdf(
        tmp_path / "Layout.pdf",
        [
            [
                (74, 720, "River study"),
                (74, 700, "Water flows"),
                (142, 700.8, "downstream."),
                (48, 699, "105"),
                (48, 680, "106"),
                (74, 680, "A longer paragraph"),
                (48, 665, "107"),
                (74, 665, "continues across"),
                (48, 38, "1"),
            ],
            [
                (65, 760, "River study"),
                (48, 700, "108"),
                (74, 700, "two pages."),
                (48, 680, "109"),
                (250, 680, "±"),
                (48, 660, "110"),
                (250, 660, "x = 2"),
                (520, 661, "(1)"),
                (74, 641, "Floating text"),
                (48, 620, "111"),
                (74, 620, "The results follow."),
            ],
        ],
        shared_text_object=shared_text_object,
    )
    records = [
        _record("p-first", "Water flows downstream."),
        _record("p-split", "A longer paragraph\ncontinues across two pages."),
        _record("eq-symbol", "±", "display_math"),
        _record("eq-tag", "(1)", "display_math"),
        _record("p-float", "Floating text"),
        _record("p-last", "The results follow."),
    ]
    assert resolve_blocks_from_pdf(path, records, deliverable="manuscript") == {
        "p-first": LineSpan(1, 105, 1, 105, ((1, 105, 105),)),
        "p-split": LineSpan(1, 106, 2, 108, ((1, 106, 107), (2, 108, 108))),
        "eq-symbol": LineSpan(2, 109, 2, 109, ((2, 109, 109),)),
        "eq-tag": LineSpan(2, 110, 2, 110, ((2, 110, 110),)),
        "p-last": LineSpan(2, 111, 2, 111, ((2, 111, 111),)),
    }


@pytest.mark.parametrize("reason", ["unreadable", "missing", "mismatch", "unordered", "no margin"])
def test_pdf_reports_invalid_layout(tmp_path, reason):
    path = tmp_path / "Layout.pdf"
    if reason == "unreadable":
        path.write_bytes(b"malformed document")
    elif reason != "missing":
        rows = [(72, 700, "Original version"), (72, 675, "Next paragraph.")]
        if reason != "no margin":
            rows.extend([(42, 700, "105"), (42, 675, "104" if reason == "unordered" else "106")])
        _pdf(path, [rows])
    with pytest.raises(OmmError) as caught:
        resolve_blocks_from_pdf(path, [_record("p-one", "New version")], deliverable="manuscript")
    assert caught.value.code == "ERR_LAYOUT_PDF_INVALID"
    assert caught.value.diagnostic.params["deliverable"] == "manuscript"
    details = {
        "mismatch": "Could not match the text of block p-one.",
        "unordered": "Page 1 has unordered margin line numbers.",
        "no margin": "No page-margin line numbers were found.",
    }
    if reason in details:
        assert caught.value.diagnostic.params["detail"] == details[reason]


def test_pdf_named_destination_disambiguates_matching_text(tmp_path):
    path = _pdf(
        tmp_path / "Layout.pdf",
        [
            [
                (43, 710, "105"),
                (75, 710, "Repeated text."),
                (43, 695, "106"),
                (75, 695, "Repeated text."),
            ],
        ],
        destinations={"p-repeated-s": (1, 695)},
    )
    assert resolve_blocks_from_pdf(
        path, [_record("p-repeated", "Repeated text.")], deliverable="manuscript"
    ) == {"p-repeated": LineSpan(1, 106, 1, 106, ((1, 106, 106),))}


def test_pdf_validates_word_boundaries_and_document_order(tmp_path):
    path = _pdf(
        tmp_path / "Layout.pdf",
        [
            [
                (42, 714, "105"),
                (75, 714, "A separate word."),
                (42, 696, "106"),
                (75, 696, "Second block."),
            ]
        ],
    )
    first = _record("p-one", "A separate word.")
    second = _record("p-two", "Second block.")
    for records in ([second, first], [_record("p-stale", "A separateword."), second]):
        with pytest.raises(OmmError) as caught:
            resolve_blocks_from_pdf(path, records, deliverable="main")
        assert caught.value.code == "ERR_LAYOUT_PDF_INVALID"


def test_pdf_keeps_body_list_markers_out_of_the_margin_column(tmp_path):
    path = _pdf(
        tmp_path / "Layout.pdf",
        [[(74, 710, "1"), (100, 710, "Numbered heading"), (74, 690, "Body text.")]],
    )
    with pytest.raises(OmmError) as caught:
        resolve_blocks_from_pdf(path, [_record("p-one", "Body text.")], deliverable="main")
    assert caught.value.code == "ERR_LAYOUT_PDF_INVALID"
    assert caught.value.diagnostic.params["detail"] == "No page-margin line numbers were found."

    numbered = _pdf(
        tmp_path / "Numbered.pdf",
        [
            [
                (40, 710, "105"),
                (74, 710, "1."),
                (100, 710, "List entry."),
                (40, 690, "106"),
                (74, 690, "2. Another entry."),
            ]
        ],
    )
    records = [_record("li-one", "List entry.\nAnother entry.", "list")]
    assert resolve_blocks_from_pdf(numbered, records, deliverable="main") == {
        "li-one": LineSpan(1, 105, 1, 106, ((1, 105, 106),)),
    }


@dataclass(frozen=True)
class _LatexRecord:
    block_id: str
    plain_text: str
    start_linelabel: str
    end_linelabel: str
    kind: str = "paragraph"


def test_tex_aux_label_formats_and_cross_page_evidence(tmp_path):
    aux = tmp_path / "clean.aux"
    aux.write_text(
        r"""\relax
\@LN{105}{0}
\newlabel{one:start}{{105}{1}}
\@LN{106}{0}
\newlabel{one:end}{{106}{1}{First {section}}{section.1}{}}
\newlabel{split:start}{{107}{1}{First {section}}{section.1}{}}
\@LN{107}{0}
\@LN{108}{0}
\@LN{109}{1}
\@LN{110}{1}
\newlabel{split:end}{{110}{2}{}{section.1}{}}
\newlabel{missing:start}{{111}{2}}
\newlabel{unknown:start}{{112}{2}}
\newlabel{unknown:end}{{118}{3}}
\newlabel{reversed:start}{{111}{2}}
\newlabel{reversed:end}{{110}{2}}
\@LN{1}{3}
\@LN{2}{3}
\newlabel{restart:start}{{110}{2}}
\newlabel{restart:end}{{2}{4}}
""",
        encoding="utf-8",
    )
    blocks = [
        _LatexRecord(name, "", f"{name}:start", f"{name}:end")
        for name in ("one", "split", "missing", "unknown", "reversed", "restart")
    ]
    assert resolve_blocks_from_tex_aux(aux, blocks, deliverable="main") == {
        "one": LineSpan(1, 105, 1, 106, ((1, 105, 106),)),
        "split": LineSpan(1, 107, 2, 110, ((1, 107, 108), (2, 109, 110))),
        "restart": LineSpan(2, 110, 4, 2, ((2, 110, 110), (4, 1, 2))),
    }


def _snapshot(tmp_path, manuscript, reply="", *, supplementary=""):
    contents = {
        "main": ("manuscript", manuscript),
        "reply": ("response_letter", reply),
        "appendix": ("supplementary", supplementary),
    }
    deliverables = {
        name: Deliverable(kind, (f"{name}.md",))
        for name, (kind, source) in contents.items()
        if source
    }
    workspace = Workspace(tmp_path, "study", "River study", "", "refs.bib", Style(), deliverables)
    documents = {
        f"{name}.md": parse_source(source) for name, (_, source) in contents.items() if source
    }
    return RevisionSnapshot("a" * 40, workspace, {}, documents, {})


def _no_engine(*args, **kwargs):
    pytest.fail("No layout command was expected.")


@pytest.mark.parametrize("shared_text_object", [False, True])
def test_pdf_locates_prose_with_raised_and_lowered_inline_math(tmp_path, shared_text_object):
    snapshot = _snapshot(
        tmp_path,
        "Unnumbered introduction. {#p-intro}\n\n"
        "During recession, $S_y$ is constant ($R^2$ = 0.8). {#p-science}\n\n"
        "Floating text. {#p-float}\n",
    )
    clean = compile_docx_workspace(snapshot, tmp_path / "build", deliverables=["main"])["main"]
    pdf = _pdf(
        tmp_path / "layout.pdf",
        [
            [
                (74, 713, "Unnumbered introduction.", 11),
                (42, 705, "105", 8),
                (74, 705, "During recession, S", 11),
                (169, 701, "y", 7),
                (176, 705, "is constant (R", 11),
                (244, 709, "2", 7),
                (250, 705, "= 0.8).", 11),
                (74, 697, "Floating text.", 11),
            ]
        ],
        shared_text_object=shared_text_object,
    )
    assert resolve_blocks_from_pdf(pdf, clean.blocks, deliverable="main") == {
        "p-science": LineSpan(1, 105, 1, 105, ((1, 105, 105),))
    }


@pytest.mark.parametrize(
    ("upper_branch", "tag", "line_number"),
    [
        ("x, if x > 0", "(1)", "106"),
        ("x, if x > 1", "(1)", "106"),
        ("x, if x > 0", "(2)", "106"),
        ("x, if x > 0", "(1)", None),
    ],
)
def test_pdf_validates_numbered_cases_on_multiple_baselines(
    tmp_path, upper_branch, tag, line_number
):
    snapshot = _snapshot(
        tmp_path,
        "Before the cases. {#p-before}\n\n"
        "$$\n"
        r"q = \begin{cases} x, & \text{if } x>0 \\ 0, & \text{otherwise} \end{cases}"
        "\n$$ {#eq-1}\n\n"
        "After the cases. {#p-after}\n",
        "See [#loc:eq-1].\n",
    )
    compiled = compile_docx_workspace(snapshot, tmp_path / "build", deliverables=["main"])
    record = compiled["main"].blocks[1]
    assert record.plain_text == "q=x,if\u00a0x>00,otherwise\t(1)"
    rows = [
        (42, 688, "105"),
        (74, 688, "Before the cases."),
        (238, 668, upper_branch),
        (190, 660, "q ="),
        (218, 660, "{", 22),
        (526, 660, tag),
        (238, 652, "0, otherwise"),
        (42, 633, "107"),
        (74, 633, "After the cases."),
    ]
    if line_number is not None:
        rows.append((42, 660, line_number))
    pdf = _pdf(tmp_path / "layout.pdf", [rows])
    args = {
        "target": "docx",
        "compiled_deliverables": compiled,
        "layout_pdfs": {"main": pdf},
    }
    if upper_branch != "x, if x > 0" or tag != "(1)" or line_number is None:
        with pytest.raises(OmmError) as caught:
            resolve_workspace_locations(snapshot, number_workspace(snapshot), **args)
        if line_number is None:
            assert caught.value.code == "ERR_LOCATION_UNAVAILABLE"
            assert caught.value.diagnostic.params == {"target": "eq-1"}
        else:
            assert caught.value.code == "ERR_LAYOUT_PDF_INVALID"
            assert (
                caught.value.diagnostic.params["detail"]
                == "Could not match the text of block eq-1."
            )
    else:
        assert resolve_workspace_locations(snapshot, number_workspace(snapshot), **args) == {
            ("eq-1", None): "main.clean.docx, p. 1, line 106"
        }


@pytest.mark.parametrize(
    ("source", "math_fragments"),
    [
        (
            r"q = \frac{a+b}{cd}",
            [(192, 660, "q ="), (217, 664, "a + b", 7), (219, 656, "cd", 7)],
        ),
        (
            r"\begin{aligned} x &= 1 \\ y &= 2 \end{aligned}",
            [(220, 668, "x = 1"), (220, 652, "y = 2")],
        ),
    ],
)
def test_pdf_locates_stacked_equations(tmp_path, source, math_fragments):
    snapshot = _snapshot(tmp_path, f"$$\n{source}\n$$ {{#eq-1}}\n")
    clean = compile_docx_workspace(snapshot, tmp_path / "build", deliverables=["main"])["main"]
    pdf = _pdf(
        tmp_path / "layout.pdf",
        [[(42, 660, "106"), (526, 660, "(1)"), *math_fragments]],
    )
    assert resolve_blocks_from_pdf(pdf, clean.blocks, deliverable="main") == {
        "eq-1": LineSpan(1, 106, 1, 106, ((1, 106, 106),))
    }


@pytest.mark.parametrize("target", ["docx", "latex"])
@pytest.mark.parametrize("layout_name", [None, "main", "wrong"])
def test_no_compiled_location_refs_do_not_need_external_layout(
    tmp_path, monkeypatch, target, layout_name
):
    snapshot = _snapshot(
        tmp_path,
        "The study. {#p-study}\n\n:::{outline} {#out-future}\n[#loc:p-study]\n:::\n",
        ':::{review-item} {#R1-C1 reviewer="Reviewer"}\n'
        ":::{comment}\nThis quote contains [#loc:p-study].\n:::\n"
        "Thank you.\n:::\n",
    )
    with monkeypatch.context() as patch:
        patch.setattr(loc_resolver.shutil, "which", _no_engine)
        patch.setattr(loc_resolver, "resolve_blocks_from_pdf", _no_engine)
        args = {
            "target": target,
            "compiled_deliverables": {},
            "layout_pdfs": {layout_name: tmp_path / "unused.pdf"} if layout_name else None,
        }
        numbered = number_workspace(snapshot)
        if layout_name == "wrong":
            with pytest.raises(OmmError) as caught:
                resolve_workspace_locations(snapshot, numbered, **args)
            assert caught.value.code == "ERR_LAYOUT_PDF_INVALID"
            assert caught.value.diagnostic.params["deliverable"] == "wrong"
        else:
            assert resolve_workspace_locations(snapshot, numbered, **args) == {}


@pytest.mark.parametrize("restart", [False, True])
def test_workspace_merges_visual_spans_and_reuses_supplied_pdf(tmp_path, monkeypatch, restart):
    snapshot = _snapshot(
        tmp_path,
        "Upstream reach. {#p-start}\n\nAt the confluence. {#p-middle}\n\n"
        "The downstream reach\ncontinues. {#p-end}\n",
        "**[#loc:p-start..p-end]**, [#loc:p-end] and [#loc:p-start..p-end].\n",
    )
    compiled = compile_docx_workspace(snapshot, tmp_path / "build", deliverables=["main"])
    reset = 1 if restart else 108
    pdf = _pdf(
        tmp_path / "export.pdf",
        [
            [
                (47, 690, "105"),
                (72, 690, "Upstream reach."),
                (47, 660, "106"),
                (72, 660, "At the confluence."),
                (47, 640, "107"),
                (72, 640, "The downstream reach"),
            ],
            [(47, 704, str(reset)), (72, 704, "continues.")],
        ],
    )
    reads = []

    def read(path, blocks, *, deliverable):
        reads.append(path)
        return resolve_blocks_from_pdf(path, blocks, deliverable=deliverable)

    monkeypatch.setattr(loc_resolver, "resolve_blocks_from_pdf", read)
    monkeypatch.setattr(loc_resolver.shutil, "which", _no_engine)
    assert resolve_workspace_locations(
        snapshot,
        number_workspace(snapshot),
        target="docx",
        compiled_deliverables=compiled,
        layout_pdfs={"main": pdf},
    ) == {
        ("p-start", "p-end"): "main.clean.docx, "
        + ("p. 1, lines 105–107; p. 2, line 1" if restart else "pp. 1–2, lines 105–108"),
        ("p-end", None): "main.clean.docx, "
        + ("p. 1, line 107; p. 2, line 1" if restart else "pp. 1–2, lines 107–108"),
    }
    assert reads == [pdf]


@pytest.mark.parametrize("target", ["p-float", "p-partial"])
def test_workspace_reports_unavailable_without_using_neighboring_line(tmp_path, target):
    snapshot = _snapshot(
        tmp_path,
        "Numbered line. {#p-numbered}\n\nA floating line. {#p-float}\n\n"
        "Starts numbered and ends without a margin label. {#p-partial}\n",
        f"See [#loc:{target}].\n",
    )
    compiled = compile_docx_workspace(snapshot, tmp_path / "build", deliverables=["main"])
    pdf = _pdf(
        tmp_path / "export.pdf",
        [
            [
                (48, 705, "105"),
                (76, 705, "Numbered line."),
                (76, 697, "A floating line."),
                (48, 679, "106"),
                (76, 679, "Starts numbered"),
                (76, 671, "and ends without a margin label."),
            ]
        ],
    )
    with pytest.raises(OmmError) as caught:
        resolve_workspace_locations(
            snapshot,
            number_workspace(snapshot),
            target="docx",
            compiled_deliverables=compiled,
            layout_pdfs={"main": pdf},
        )
    assert caught.value.code == "ERR_LOCATION_UNAVAILABLE"
    assert caught.value.diagnostic.params == {"target": target}


def test_workspace_uses_supplied_pdf_for_the_referenced_supplement_only(tmp_path, monkeypatch):
    snapshot = _snapshot(
        tmp_path,
        "In the main manuscript. {#p-main}\n",
        "See [#loc:p-appendix].\n",
        supplementary="Supporting data. {#p-appendix}\n",
    )
    pdf = _pdf(tmp_path / "layout.pdf", [[(46, 690, "125"), (74, 690, "Supporting data.")]])
    compiled = {
        "main": SimpleNamespace(clean_path=tmp_path / "main.tex", blocks=()),
        "appendix": SimpleNamespace(
            clean_path=tmp_path / "appendix.clean.tex",
            blocks=(_LatexRecord("p-appendix", "Supporting data.", "start", "end"),),
        ),
    }
    monkeypatch.setattr(loc_resolver.shutil, "which", _no_engine)
    assert resolve_workspace_locations(
        snapshot,
        number_workspace(snapshot),
        target="latex",
        compiled_deliverables=compiled,
        layout_pdfs={"appendix": pdf, "main": tmp_path / "unused.pdf"},
    ) == {("p-appendix", None): "appendix.clean.tex, p. 1, line 125"}


@pytest.mark.parametrize("target, suffix", [("docx", ".docx"), ("latex", ".tex")])
def test_workspace_requires_layout_if_engine_is_missing(tmp_path, monkeypatch, target, suffix):
    snapshot = _snapshot(tmp_path, "Text. {#p-first}\n", "See [#loc:p-first].\n")
    clean_path = tmp_path / ("main.clean" + suffix)
    compiled = {"main": SimpleNamespace(clean_path=clean_path, blocks=())}
    monkeypatch.setattr(loc_resolver.shutil, "which", lambda _name: None)
    with pytest.raises(OmmError) as caught:
        resolve_workspace_locations(
            snapshot, number_workspace(snapshot), target=target, compiled_deliverables=compiled
        )
    assert caught.value.code == "ERR_LOCATION_LAYOUT_REQUIRED"
    assert caught.value.diagnostic.params == {"deliverable": "main", "clean_file": clean_path.name}


@pytest.fixture
def fake_engine(tmp_path, monkeypatch):
    # Use the same Python launcher on Windows and POSIX rather than relying on shebangs.
    launch = loc_resolver.subprocess.run
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return launch([sys.executable, *argv], **kwargs)

    monkeypatch.setattr(loc_resolver.subprocess, "run", run)

    def create(name, source):
        script = tmp_path / name
        script.write_text(source, encoding="utf-8")
        monkeypatch.setattr(loc_resolver.shutil, "which", lambda _name: str(script))
        return script, calls

    return create


def test_soffice_headless_uses_independent_profile_and_bookmark_export(tmp_path, fake_engine):
    script, calls = fake_engine(
        "fake_soffice.py",
        r"""
import json
import sys
from pathlib import Path

args = sys.argv[1:]
assert args[0] == f"-env:UserInstallation={(Path.cwd() / 'profile').as_uri()}"
assert "--headless" in args
fmt, options = args[args.index("--convert-to") + 1].split(":", 2)[1:]
assert fmt == "writer_pdf_Export"
assert json.loads(options)["ExportBookmarksToPDFDestination"]["value"] == "true"
clean = Path(args[-1])
assert clean.read_bytes() == b"mock DOCX"
output = Path(args[args.index("--outdir") + 1])
assert output == Path.cwd()
(output / clean.with_suffix(".pdf").name).write_bytes(clean.with_suffix(".layout.pdf").read_bytes())
""",
    )
    clean = tmp_path / "Main Clean.docx"
    clean.write_bytes(b"mock DOCX")
    layout = _pdf(clean.with_suffix(".layout.pdf"), [[(45, 690, "105"), (74, 690, "The study.")]])
    out_pdf = tmp_path / "export" / "Renamed layout.pdf"
    for _ in range(2):
        assert (
            paginate_docx_headless(clean, out_pdf, deliverable="main", soffice_bin=script)
            == out_pdf
        )
        assert out_pdf.read_bytes() == layout.read_bytes()
    first, second = (kwargs for _, kwargs in calls)
    assert first["cwd"] != second["cwd"]
    assert not first["cwd"].exists() and not second["cwd"].exists()
    expected_flags = (
        getattr(loc_resolver.subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    )
    assert first["creationflags"] == second["creationflags"] == expected_flags
    snapshot = _snapshot(tmp_path, "The study. {#p-study}\n", "See [#loc:p-study].\n")
    compiled = SimpleNamespace(clean_path=clean, blocks=(_record("p-study", "The study."),))
    assert resolve_workspace_locations(
        snapshot,
        number_workspace(snapshot),
        target="docx",
        compiled_deliverables={"main": compiled},
    ) == {("p-study", None): "Main Clean.docx, p. 1, line 105"}
    assert len(calls) == 3


def test_pdflatex_headless_runs_twice_and_resolves_workspace_location(tmp_path, fake_engine):
    script, calls = fake_engine(
        "fake_pdflatex.py",
        r"""
import os
import sys
from pathlib import Path

args = sys.argv[1:]
assert {"-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape"} <= set(args)
entry = Path(args[-1])
assert entry.resolve().parent == Path.cwd()
assert "\\input{source/document.tex}" in entry.read_text(encoding="ascii")
assert (Path.cwd() / "source/document.tex").read_bytes() == b"mock TeX"
assert Path(os.environ["TEXINPUTS"].split(os.pathsep)[0]).is_dir()
assert os.environ["MIKTEX_MPM_AUTOINSTALL"] == "0"
aux = entry.with_suffix(".aux")
if not aux.exists():
    aux.write_text(r"\newlabel{start}{{1}{1}}", encoding="ascii")
else:
    aux.write_text("\n".join([
        r"\newlabel{start}{{105}{2}}",
        r"\@LN{105}{1}",
        r"\@LN{106}{1}",
        r"\@LN{107}{2}",
        r"\newlabel{end}{{107}{3}{}{anchor}{}}",
    ]), encoding="ascii")
""",
    )
    snapshot = _snapshot(tmp_path, "Text. {#p-first}\n", "See [#loc:p-first].\n")
    clean = tmp_path / "Main Clean.tex"
    clean.write_bytes(b"mock TeX")
    block = _LatexRecord("p-first", "Text.", "start", "end")
    work_dir = tmp_path / "work"
    aux = paginate_latex_headless(clean, work_dir, deliverable="main", tex_bin=script)
    assert aux == work_dir / "Main Clean.aux"
    assert resolve_blocks_from_tex_aux(aux, [block], deliverable="main") == {
        "p-first": LineSpan(2, 105, 3, 107, ((2, 105, 106), (3, 107, 107))),
    }
    assert resolve_workspace_locations(
        snapshot,
        number_workspace(snapshot),
        target="latex",
        compiled_deliverables={"main": SimpleNamespace(clean_path=clean, blocks=(block,))},
    ) == {("p-first", None): "Main Clean.tex, pp. 2–3, lines 105–107"}
    assert len(calls) == 4
    first, second, third, fourth = [kwargs["cwd"] for _, kwargs in calls]
    assert first == second and third == fourth and first != third
    assert not first.exists() and not third.exists()


@pytest.mark.skipif(os.name == "nt", reason="Requires symlink support without elevation.")
def test_tex_keeps_the_format_selecting_executable_name(tmp_path, fake_engine, monkeypatch):
    executable, calls = fake_engine(
        "pdftex",
        "import sys\nfrom pathlib import Path\n"
        "assert Path(sys.argv[0]).name == 'pdflatex'\n"
        "Path(sys.argv[-1]).with_suffix('.aux').touch()\n",
    )
    alias = tmp_path / "pdflatex"
    alias.symlink_to(executable)
    original = tmp_path / "article.tex"
    original.touch()
    monkeypatch.setattr(loc_resolver.shutil, "which", lambda _name: str(alias))
    assert paginate_latex_headless(original, tmp_path, deliverable="main").is_file()
    assert len(calls) == 2


def test_windows_creation_flag_is_used_for_all_headless_commands(monkeypatch, tmp_path):
    launches = []
    monkeypatch.setattr(loc_resolver, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(loc_resolver.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(loc_resolver.subprocess, "run", lambda *a, **kw: launches.append(kw))
    loc_resolver._run_headless(["headless"], tmp_path)
    assert launches[0]["creationflags"] == 0x08000000
