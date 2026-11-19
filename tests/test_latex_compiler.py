"""Unit tests for the deterministic LaTeX target compiler."""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from omm import ast
from omm.errors import OmmError
from omm.git_revs import RevisionSnapshot, load_workspace_at_revision, sync_workspace
from omm.latex_compiler import (
    CompiledLatexDeliverable,
    LatexBlockRecord,
    compile_latex_workspace,
)
from omm.workspace import MANIFEST

INTRO = "sections/01-introduction.md"
METHODS = "sections/02-methods.md"
APPENDIX = "sections/03-appendix.md"
REBUTTAL = "reviews/rebuttal.md"

BIB = (
    "@article{beven2020, author={Beven, K.}, year={2020}, title={T one}}\n"
    "@article{kirchner2009, author={Kirchner, J. W.}, year={2009}, title={Catchments}}\n"
)


def git(root: Path, *args: str) -> bytes:
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
        ["git", *args], cwd=root, env=env, check=True, capture_output=True
    ).stdout


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_asset(root: Path, relative: str, data: bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty.gitconfig"))
    root = tmp_path / "manuscript repository"
    root.mkdir()
    git(root, "init", "--quiet", "--template=")
    git(root, "config", "user.name", "Test Author")
    git(root, "config", "user.email", "author@example.com")
    git(root, "config", "core.autocrlf", "false")
    return root


def manifest(
    deliverables: dict[str, tuple[str, list[str]]],
    style: str = "",
    title: str = "Test Manuscript",
    authors: str = '""',
    workspace_id: str | None = None,
) -> str:
    blocks = []
    for name, (kind, sources) in deliverables.items():
        joined = ", ".join(f'"{source}"' for source in sources)
        blocks.append(f'[deliverables.{name}]\nkind = "{kind}"\nsources = [{joined}]\n')
    return (
        f'workspace_id = "{workspace_id or str(uuid.uuid4())}"\n'
        f'title = "{title}"\n'
        f"authors = {authors}\n"
        'bibliography = "references.bib"\n\n'
        + (f"{style}\n" if style else "")
        + "\n".join(blocks)
    )


def stage(root: Path, text: str, files: dict[str, str], assets: dict[str, bytes]) -> None:
    write(root, MANIFEST, text)
    for relative, source in files.items():
        write(root, relative, source)
    for relative, data in assets.items():
        write_asset(root, relative, data)


def publish(root: Path) -> str:
    return sync_workspace(root).revision


def snapshot_at(root: Path, revision: str) -> RevisionSnapshot:
    return load_workspace_at_revision(root, revision)


def compile_out(
    repo: Path,
    tmp_path: Path,
    revision: str,
    **kwargs: object,
) -> dict[str, CompiledLatexDeliverable]:
    counter = len(list(tmp_path.glob("out-*")))
    out_dir = tmp_path / f"out-{revision[:8]}-{counter}"
    snapshot = snapshot_at(repo, revision)
    return compile_latex_workspace(snapshot, out_dir, **kwargs)  # type: ignore[arg-type]


def compile_marked(
    repo: Path, tmp_path: Path, first: str, second: str
) -> dict[str, CompiledLatexDeliverable]:
    out_dir = tmp_path / f"out-marked-{second[:8]}-{len(list(tmp_path.glob('out-marked-*')))}"
    snapshot = snapshot_at(repo, second)
    milestone = snapshot_at(repo, first)
    return compile_latex_workspace(snapshot, out_dir, milestone=milestone)


def read(tmp_path: Path, revision: str, name: str) -> str:
    directories = sorted(
        tmp_path.glob(f"out-{revision[:8]}-*"), key=lambda path: path.stat().st_mtime
    )
    for directory in reversed(directories):
        path = directory / name
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise AssertionError(f"missing output {name} for revision {revision}")


def brace_balance(text: str) -> int:
    cleaned = text.replace(r"\{", "").replace(r"\}", "")
    return cleaned.count("{") - cleaned.count("}")


def dif_regions(text: str, *, deleted: bool) -> list[str]:
    if deleted:
        pattern = re.compile(r"\\DIFdelbegin(?:FL)?\s(?s:(.*?))\\DIFdelend(?:FL)?")
    else:
        pattern = re.compile(r"\\DIFaddbegin(?:FL)?\s(?s:(.*?))\\DIFaddend(?:FL)?")
    return pattern.findall(text)


# ---------------------------------------------------------------------------
# Clean compilation
# ---------------------------------------------------------------------------


def test_clean_skeleton_labels_and_records(repo: Path, tmp_path: Path) -> None:
    intro = (
        "# Intro {#sec-intro}\n\n"
        ":::{outline} {#out-todo}\n- pending\n:::\n\n"
        "Body text. {#p-body}\n\n"
        "- one\n- two {#li-points}\n\n"
        "$$u = f(x)$$ {#eq-main}\n\n"
        ":::{figure} {#fig-site}\n![Site](figures/site.png)\n\nSite map.\n:::\n\n"
        ":::{table} {#tbl-metrics}\nMetrics.\n\n"
        "| A | B |\n|:---|---:|\n| 1 | 2 |\n:::\n"
    )
    stage(
        repo,
        manifest({"manuscript": ("manuscript", [INTRO])}),
        {"references.bib": "", INTRO: intro},
        {"figures/site.png": b"site-bytes"},
    )
    revision = publish(repo)
    results = compile_out(repo, tmp_path, revision)
    assert sorted(results) == ["manuscript"]
    deliverable = results["manuscript"]
    assert isinstance(deliverable, CompiledLatexDeliverable)
    assert deliverable.kind == "manuscript"
    assert deliverable.marked_path is None
    assert deliverable.clean_path.exists()

    document = read(tmp_path, revision, "manuscript.clean.tex")
    assert document.startswith("\\documentclass[11pt]{article}\n")
    assert "\\usepackage{amsmath}" in document
    assert "\\usepackage{amssymb}" in document
    assert "\\usepackage[mathlines]{lineno}" in document
    assert "\\usepackage{booktabs}" in document
    assert "\\usepackage{multirow}" in document
    assert "\\definecolor{OmmLinkColor}{HTML}{0070C0}" in document
    assert (
        "\\hypersetup{colorlinks=true, linkcolor=OmmLinkColor, "
        "citecolor=OmmLinkColor, urlcolor=OmmLinkColor}" in document
    )
    assert "\\setcounter{secnumdepth}{6}" in document
    assert "\\linenumbers" in document
    assert document.rstrip().endswith("\\end{document}")

    # Outline blocks are stripped completely.
    assert "pending" not in document
    assert "omm:out-todo" not in document

    assert "\\linelabel{omm:ls:sec-intro}\\section{Intro}\\label{omm:sec-intro}" in document
    assert "\\label{omm:sec-intro}\\linelabel{omm:le:sec-intro}" in document
    assert "\\linelabel{omm:ls:p-body}\\label{omm:p-body}Body text." in document
    assert "\\item\none\n\\par\n\\item\ntwo" in document
    assert "\\begin{itemize}" in document
    assert "\\linelabel{omm:ls:eq-main}\\begin{equation}\\label{omm:eq-main}" in document
    assert "\n\\end{equation}\\linelabel{omm:le:eq-main}" in document
    assert "\\begin{figure}[htbp]" in document
    assert "\\caption{Site map.}\\label{omm:fig-site}" in document
    assert "\\caption{Metrics.}\\label{omm:tbl-metrics}" in document
    assert "figures/site.png" in document

    records = deliverable.blocks
    kinds = [(record.block_id, record.kind) for record in records]
    assert kinds == [
        ("sec-intro", "heading"),
        ("p-body", "paragraph"),
        ("li-points", "list"),
        ("eq-main", "display_math"),
    ]
    for record in records:
        assert isinstance(record, LatexBlockRecord)
        assert record.label_name == f"omm:{record.block_id}"
        assert record.start_linelabel == f"omm:ls:{record.block_id}"
        assert record.end_linelabel == f"omm:le:{record.block_id}"
    assert records[0].plain_text == "Intro"
    assert records[1].plain_text == "Body text."
    assert records[2].plain_text == "one two"
    assert records[3].plain_text == "u = f(x)"

    written_assets = {path.name: path.read_bytes() for path in deliverable.asset_paths}
    assert written_assets == {"site.png": b"site-bytes"}
    assert deliverable.asset_paths[0] == deliverable.clean_path.parent / "figures/site.png"


def test_table_booktabs_structure(repo: Path, tmp_path: Path) -> None:
    intro = (
        ":::{table} {#tbl-metrics}\nModel performance.\n\n"
        "| Basin | Calibration | > | Validation | > |\n"
        "| ^ | NSE | KGE | NSE | KGE |\n"
        "|:---|:---:|:---:|:---:|:---:|\n"
        "| Upper | **0.87** | 0.84 | 0.83 | 0.81 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| Lower | 0.79 | 0.82 | 0.76 | 0.78 |\n\n"
        "Note: bold values are best ($p < 0.01$).\n:::\n"
    )
    stage(
        repo,
        manifest({"manuscript": ("manuscript", [INTRO])}),
        {"references.bib": "", INTRO: intro},
        {},
    )
    revision = publish(repo)
    compile_out(repo, tmp_path, revision)
    document = read(tmp_path, revision, "manuscript.clean.tex")
    body = document[document.index("\\begin{tabular}") : document.index("\\end{tabular}")]
    assert "\\toprule" in body
    assert "\\bottomrule" in body
    assert "\\multirow{2}{*}{Basin}" in body
    assert "\\multicolumn{2}{c}{Calibration}" in body
    assert "\\multicolumn{2}{c}{Validation}" in body
    assert "\\cmidrule(lr){2-3} \\cmidrule(lr){4-5}" in body
    assert "\\textbf{0.87}" in body
    rows = [line for line in body.splitlines() if line.endswith(r" \\")]
    assert rows == [
        (
            "\\multirow{2}{*}{Basin} & \\multicolumn{2}{c}{Calibration} "
            "& \\multicolumn{2}{c}{Validation} \\\\"
        ),
        " & NSE & KGE & NSE & KGE \\\\",
        "Upper & \\textbf{0.87} & 0.84 & 0.83 & 0.81 \\\\",
        "Lower & 0.79 & 0.82 & 0.76 & 0.78 \\\\",
    ]
    assert document.index("\\caption{Model performance.}") < document.index("\\begin{tabular}")
    assert "\\par\\noindent{\\small Note: bold values are best ($p < 0.01$).}" in document
    assert document.count("\\midrule") == 2  # header separation plus midrule_after
    assert brace_balance(document) == 0


def test_inline_markup_escaping_and_atoms(repo: Path, tmp_path: Path) -> None:
    body = (
        "Bold **strong** and *em*; sub H~2~O; sup cm^3^; code `x_y` here; "
        "link [text](https://example.com/a_b) too; math $a_b^c$ plus "
        "soft\ncontinuation and hard\\\nbreak and specials "
        "\\ { } $ & # _ % ~ ^ end. {#p-x}\n"
    )
    stage(
        repo,
        manifest({"manuscript": ("manuscript", [INTRO])}),
        {"references.bib": "", INTRO: body},
        {},
    )
    revision = publish(repo)
    compile_out(repo, tmp_path, revision)
    document = read(tmp_path, revision, "manuscript.clean.tex")
    rendered = document[
        document.index("\\linelabel{omm:ls:p-x}") : document.index("\\linelabel{omm:le:p-x}")
    ]
    assert "\\textbf{strong}" in rendered
    assert "\\emph{em}" in rendered
    assert "H\\textsubscript{2}O" in rendered
    assert "cm\\textsuperscript{3}" in rendered
    assert "\\texttt{x\\_y}" in rendered
    assert "\\href{https://example.com/a\\_b}{text}" in rendered
    assert "$a_b^c$" in rendered
    assert "soft continuation" in rendered
    assert r"hard\\break" in rendered
    assert (
        r"\textbackslash{} \{ \} \$ \& \# \_ \% \textasciitilde{} "
        r"\textasciicircum{} end" in rendered
    )
    assert brace_balance(document) == 0


def test_math_tag_and_numbered_environments_forbidden(repo: Path, tmp_path: Path) -> None:
    stage(
        repo,
        manifest({"manuscript": ("manuscript", [INTRO])}),
        {"references.bib": "", INTRO: "$$u = f(x)$$ {#eq-main}\n\nInline $v$ math. {#p-x}\n"},
        {},
    )
    revision = publish(repo)
    snapshot = snapshot_at(repo, revision)
    document = snapshot.documents[INTRO]
    equation = next(b for b in document.blocks if isinstance(b, ast.DisplayMath))
    tampered = replace(
        snapshot,
        documents={
            INTRO: replace(document, blocks=(replace(equation, latex=r"u \tag{1}"),))
        },
    )
    with pytest.raises(OmmError, match="^ERR_MATH_TAG_FORBIDDEN$") as error:
        compile_latex_workspace(tampered, tmp_path / "out-tag")
    assert error.value.diagnostic.block_id == "eq-main"

    paragraph = next(b for b in document.blocks if isinstance(b, ast.Paragraph))
    span = paragraph.inlines[0].span
    tampered = replace(
        snapshot,
        documents={
            INTRO: replace(
                document,
                blocks=(replace(paragraph, inlines=(ast.InlineMath(span, r"x\tag{2}"),)),),
            )
        },
    )
    with pytest.raises(OmmError, match="^ERR_MATH_TAG_FORBIDDEN$") as error:
        compile_latex_workspace(tampered, tmp_path / "out-inline-tag")
    assert error.value.diagnostic.block_id == "p-x"

    tampered = replace(
        snapshot,
        documents={
            INTRO: replace(
                document,
                blocks=(replace(equation, latex=r"\begin{align}u\end{align}"),),
            )
        },
    )
    with pytest.raises(OmmError, match="^ERR_MATH_UNSUPPORTED_ENV$"):
        compile_latex_workspace(tampered, tmp_path / "out-align")


def test_block_refs_citations_and_disambiguation(repo: Path, tmp_path: Path) -> None:
    bib = (
        "@article{a2020, author={Beven, K.}, year={2020}, title={One}}\n"
        "@article{b2020, author={Beven, K.}, year={2020}, title={Two}}\n"
    )
    intro = (
        "Citing [@a2020; @b2020] and narrative @a2020 here. "
        "See [#fig-f], plain Figure 1 stays literal. {#p-x}\n\n"
        ":::{figure} {#fig-f}\n![F](figures/f.png)\n\nPanel.\n:::\n"
    )
    stage(
        repo,
        manifest({"manuscript": ("manuscript", [INTRO])}),
        {"references.bib": bib, INTRO: intro},
        {"figures/f.png": b"f"},
    )
    revision = publish(repo)
    compile_out(repo, tmp_path, revision)
    document = read(tmp_path, revision, "manuscript.clean.tex")
    assert (
        "(\\hyperref[omm:bib:a2020]{Beven, 2020a}; "
        "\\hyperref[omm:bib:b2020]{Beven, 2020b})" in document
    )
    assert "\\hyperref[omm:bib:a2020]{Beven (2020a)} here" in document
    assert "\\hyperref[omm:fig-f]{Fig.~1}" in document
    assert "plain Figure 1 stays literal" in document
    assert document.count("\\hyperref") == document.count("[omm:")
    assert "plain \\hyperref" not in document
    assert "\\begin{thebibliography}" in document
    assert brace_balance(document) == 0


def test_multi_subfigure_layout_and_panel_references(repo: Path, tmp_path: Path) -> None:
    intro = (
        "Panels in [#fig-panels], first one is [#fig-p1]. {#p-x}\n\n"
        ":::{figure} {#fig-panels cols=2}\n"
        "![Alpha](figures/a.png){#fig-p1}\n"
        "![Beta](figures/b.png){#fig-p2}\n"
        "![Gamma](figures/c.png){#fig-p3}\n\n"
        "Overview: [#fig-p1] and [#fig-p3].\n:::\n"
    )
    stage(
        repo,
        manifest({"manuscript": ("manuscript", [INTRO])}),
        {"references.bib": "", INTRO: intro},
        {"figures/a.png": b"a", "figures/b.png": b"b", "figures/c.png": b"c"},
    )
    revision = publish(repo)
    results = compile_out(repo, tmp_path, revision)
    document = read(tmp_path, revision, "manuscript.clean.tex")
    assert "\\begin{subfigure}[t]{0.500\\textwidth}" in document
    assert document.count("\\begin{subfigure}") == 3
    assert "\\caption{Alpha}\\label{omm:fig-p1}" in document
    assert "\\caption{Beta}\\label{omm:fig-p2}" in document
    assert "\\caption{Gamma}\\label{omm:fig-p3}" in document
    assert "\\par\\bigskip" in document  # row break after the first row of two
    assert "\\hyperref[omm:fig-p1]{Fig.~1(a)}" in document
    assert "\\hyperref[omm:fig-p3]{Fig.~1(c)}" in document
    names = sorted(path.name for path in results["manuscript"].asset_paths)
    assert names == ["a.png", "b.png", "c.png"]
    assert brace_balance(document) == 0


def test_bibliography_placement_per_deliverable(repo: Path, tmp_path: Path) -> None:
    stage(
        repo,
        manifest(
            {
                "manuscript": ("manuscript", [INTRO]),
                "supplementary": ("supplementary", [APPENDIX]),
            }
        ),
        {
            "references.bib": BIB,
            INTRO: "Main cites [@beven2020]. {#p-x}\n",
            APPENDIX: "# Appendix {#sec-app}\n\nSupplement cites [@kirchner2009]. {#p-app}\n",
        },
        {},
    )
    revision = publish(repo)
    compile_out(repo, tmp_path, revision)
    main = read(tmp_path, revision, "manuscript.clean.tex")
    supplement = read(tmp_path, revision, "supplementary.clean.tex")
    assert "kirchner2009" not in main
    assert "beven2020" not in supplement
    assert "\\begin{thebibliography}" in supplement
    assert "\\bibitem" in supplement


def test_markdown_block_blockquote(repo: Path, tmp_path: Path) -> None:
    intro = "Before. {#p-before}\n\n> Quoted line one.\n> quoted **bold** end. {#p-quote}\n\nAfter. {#p-after}\n"
    stage(
        repo,
        manifest({"manuscript": ("manuscript", [INTRO])}),
        {"references.bib": "", INTRO: intro},
        {},
    )
    revision = publish(repo)
    results = compile_out(repo, tmp_path, revision)
    document = read(tmp_path, revision, "manuscript.clean.tex")
    assert "\\begin{quote}" in document
    assert "\\end{quote}" in document
    assert "\\label{omm:p-quote}Quoted line one" in document
    assert "\\textbf{bold}" in document
    kinds = [record.block_id for record in results["manuscript"].blocks]
    assert kinds == ["p-before", "p-quote", "p-after"]
    assert brace_balance(document) == 0

    # An unchanged paired blockquote renders once in the Marked deliverable.
    first = revision
    write(repo, INTRO, intro + "Appended. {#p-new}\n")
    second = publish(repo)
    results = compile_marked(repo, tmp_path, first, second)
    marked = results["manuscript"].marked_path.read_text(encoding="utf-8")
    assert marked.count("\\begin{quote}") == 1
    deleted = " ".join(dif_regions(marked, deleted=True))
    inserted = " ".join(dif_regions(marked, deleted=False))
    assert "quoted" not in deleted
    assert "Appended." in inserted
    assert brace_balance(marked) == 0


def test_numeric_style_bibliography(repo: Path, tmp_path: Path) -> None:
    style = '[style]\ncitation_style = "numeric"\nlink_color = "#000000"\n'
    intro = "First [@kirchner2009] then [@beven2020]. {#p-x}\n"
    stage(
        repo,
        manifest({"manuscript": ("manuscript", [INTRO])}, style=style),
        {"references.bib": BIB, INTRO: intro},
        {},
    )
    revision = publish(repo)
    compile_out(repo, tmp_path, revision)
    document = read(tmp_path, revision, "manuscript.clean.tex")
    assert "\\definecolor{OmmLinkColor}{HTML}{000000}" in document
    assert "\\hyperref[omm:bib:kirchner2009]{1}" in document
    assert "\\hyperref[omm:bib:beven2020]{2}" in document
    assert "\\begin{thebibliography}{2}" in document
    assert (
        "\\bibitem[1]{kirchner2009}\\label{omm:bib:kirchner2009} "
        "Kirchner, J. W. (2009). Catchments." in document
    )
    assert "[{Kirchner" not in document
    assert brace_balance(document) == 0


def test_bibliography_placement_merged_in_main(repo: Path, tmp_path: Path) -> None:
    style = '[style]\nbibliography_placement = "merged-in-main"\n'
    stage(
        repo,
        manifest(
            {
                "manuscript": ("manuscript", [INTRO]),
                "supplementary": ("supplementary", [APPENDIX]),
                "response": ("response_letter", [REBUTTAL]),
            },
            style=style,
        ),
        {
            "references.bib": BIB + "@article{reply2022, author={A. Smith}, year={2022}, title={Reply}}",
            INTRO: "Main cites [@beven2020]. {#p-x}\n",
            APPENDIX: "# Appendix {#sec-app}\n\nSupplement cites [@kirchner2009]. {#p-app}\n",
            REBUTTAL: "Reply cites [@reply2022]. {#p-reply}\n",
        },
        {},
    )
    revision = publish(repo)
    compile_out(repo, tmp_path, revision)
    main = read(tmp_path, revision, "manuscript.clean.tex")
    supplement = read(tmp_path, revision, "supplementary.clean.tex")
    assert "kirchner2009" in main
    assert "beven2020" in main
    assert "omm:bib:reply2022" in main
    response = read(tmp_path, revision, "response.clean.tex")
    assert "\\begin{thebibliography}" not in response
    assert "\\textcolor{OmmLinkColor}{Smith, 2022}" in response
    assert "\\begin{thebibliography}" not in supplement
    assert "\\bibitem" not in supplement
    # The merged entry has no anchor in the supplement: colored text, no link.
    assert "\\textcolor{OmmLinkColor}{Kirchner, 2009}" in supplement
    assert "omm:bib:kirchner2009" not in supplement


def test_display_math_numbered_and_unnumbered(repo: Path, tmp_path: Path) -> None:
    intro = "$$u = f(x)$$ {#eq-a}\n\n$$w = 1$$ {#eq-b unnumbered}\n"
    stage(
        repo,
        manifest({"manuscript": ("manuscript", [INTRO])}),
        {"references.bib": "", INTRO: intro},
        {},
    )
    revision = publish(repo)
    compile_out(repo, tmp_path, revision)
    document = read(tmp_path, revision, "manuscript.clean.tex")
    assert "\\begin{equation}\\label{omm:eq-a} u = f(x)\n\\end{equation}" in document
    assert "\\[w = 1\n\\]" in document
    assert document.count("\\begin{equation}") == 1


def test_supplementary_prefix_and_response_letter(repo: Path, tmp_path: Path) -> None:
    stage(
        repo,
        manifest(
            {
                "manuscript": ("manuscript", [INTRO]),
                "supplementary": ("supplementary", [APPENDIX]),
                "response_letter": ("response_letter", [REBUTTAL]),
            }
        ),
        {
            "references.bib": BIB,
            INTRO: "# Main {#sec-main}\n\nTarget text. {#p-first}\n",
            APPENDIX: "# Appendix {#sec-app}\n\nAppendix. {#p-app}\n",
            REBUTTAL: (
                ':::{review-item} {#R1-C1 reviewer="Reviewer 1"}\n'
                ":::{comment}\nWhy is $S_y$ constant?\n:::\n\n"
                "We revised [#sec-main] ([#loc:p-first..p-first]). {#p-r1}\n"
                ":::\n"
            ),
        },
        {},
    )
    revision = publish(repo)
    physical = {("p-first", "p-first"): "p. 7, lines 12-40"}
    results = compile_out(repo, tmp_path, revision, loc_labels=physical)
    assert sorted(results) == ["manuscript", "response_letter", "supplementary"]
    assert results["response_letter"].blocks == ()

    letter = read(tmp_path, revision, "response_letter.clean.tex")
    assert "\\usepackage{lineno}" not in letter
    assert "\\linenumbers" not in letter
    assert "\\subsection*{Reviewer 1}" in letter
    assert "\\begin{quote}" in letter
    assert "\\colorbox{gray!15}" in letter
    assert "\\itshape Why is $S_y$ constant?" in letter
    # The section target lives in the manuscript deliverable: colored text,
    # no dangling \hyperref.
    assert "\\textcolor{OmmLinkColor}{Section~1}" in letter
    assert "omm:sec-main" not in letter
    assert "\\textcolor{OmmLinkColor}{p. 7, lines 12-40}" in letter

    # Without supplied layout labels the logical location label is used.
    compile_out(repo, tmp_path, revision)
    fallback = read(tmp_path, revision, "response_letter.clean.tex")
    assert "\\textcolor{OmmLinkColor}{manuscript §1 [p-first..p-first]}" in fallback

    supplement = read(tmp_path, revision, "supplementary.clean.tex")
    assert "\\renewcommand{\\thesection}{S\\arabic{section}}" in supplement
    assert "\\renewcommand{\\thefigure}{S\\arabic{figure}}" in supplement
    assert "\\renewcommand{\\theequation}{S\\arabic{equation}}" in supplement
    assert "\\renewcommand{\\thetable}{S\\arabic{table}}" in supplement


def test_deliverable_subset_and_no_marked_without_milestone(
    repo: Path, tmp_path: Path
) -> None:
    stage(
        repo,
        manifest(
            {
                "manuscript": ("manuscript", [INTRO]),
                "supplementary": ("supplementary", [APPENDIX]),
            }
        ),
        {"references.bib": "", INTRO: "Main. {#p-x}\n", APPENDIX: "Appendix. {#p-app}\n"},
        {},
    )
    revision = publish(repo)
    results = compile_out(repo, tmp_path, revision, deliverables={"manuscript"})
    assert sorted(results) == ["manuscript"]
    out_dir = next(tmp_path.glob("out-*"))
    assert sorted(path.name for path in out_dir.iterdir()) == ["manuscript.clean.tex"]
    assert results["manuscript"].marked_path is None


def test_deterministic_output(repo: Path, tmp_path: Path) -> None:
    intro = (
        "Text [@beven2020] and [#fig-f]. {#p-x}\n\n"
        ":::{figure} {#fig-f}\n![F](figures/f.png)\n\nF.\n:::\n"
    )
    stage(
        repo,
        manifest({"manuscript": ("manuscript", [INTRO])}),
        {"references.bib": BIB, INTRO: intro},
        {"figures/f.png": b"f"},
    )
    revision = publish(repo)
    compile_out(repo, tmp_path, revision)
    compile_out(repo, tmp_path, revision)
    directories = sorted(tmp_path.glob("out-*"))
    assert len(directories) == 2
    first = (directories[0] / "manuscript.clean.tex").read_bytes()
    second = (directories[1] / "manuscript.clean.tex").read_bytes()
    assert first == second


# ---------------------------------------------------------------------------
# Marked compilation
# ---------------------------------------------------------------------------


def two_revisions(
    repo: Path, files_v1: dict[str, str], files_v2: dict[str, str]
) -> tuple[RevisionSnapshot, RevisionSnapshot]:
    stage(repo, manifest({"manuscript": ("manuscript", [INTRO])}), {"references.bib": "", **files_v1}, {})
    first = publish(repo)
    for relative, source in files_v2.items():
        write(repo, relative, source)
    second = publish(repo)
    return snapshot_at(repo, first), snapshot_at(repo, second)


def test_marked_word_diff_and_macro_balance(repo: Path, tmp_path: Path) -> None:
    milestone, snapshot = two_revisions(
        repo,
        {INTRO: "The quick brown fox jumps. {#p-x}\n"},
        {INTRO: "The quick red fox leaps. {#p-x}\n"},
    )
    results = compile_marked(repo, tmp_path, milestone.revision, snapshot.revision)
    deliverable = results["manuscript"]
    assert deliverable.marked_path is not None
    document = deliverable.marked_path.read_text(encoding="utf-8")
    assert "\\usepackage[normalem]{ulem}" in document
    assert "\\providecommand{\\DIFadd}[1]" in document
    assert "\\providecommand{\\DIFdel}[1]" in document
    deleted = dif_regions(document, deleted=True)
    inserted = dif_regions(document, deleted=False)
    assert any("brown" in region for region in deleted)
    assert any("red" in region for region in inserted)
    assert "quick" in document
    assert all("quick" not in region for region in deleted)
    assert document.count("\\DIFdelbegin") == document.count("\\DIFdelend")
    assert document.count("\\DIFaddbegin") == document.count("\\DIFaddend")
    assert brace_balance(document) == 0


def test_marked_word_boundaries_and_balanced_format_changes(repo, tmp_path):
    old, new = two_revisions(
        repo,
        {INTRO: "two {#p-word}\n\n**bold** and **same command**. {#p-format}\n"},
        {INTRO: "one two {#p-word}\n\n*bold* and **same style**. {#p-format}\n"},
    )
    artifact = compile_marked(repo, tmp_path, old.revision, new.revision)["manuscript"]
    marked = artifact.marked_path.read_text(encoding="utf-8")
    assert r"\DIFaddend{}two" in marked
    assert r"\textbf{\DIFdel{bold}}" in marked
    assert r"\emph{\DIFadd{bold}}" in marked
    assert r"\textbf{same \DIFdelbegin \DIFdel{command}" in marked
    assert brace_balance(marked) == 0


def test_marked_long_format_changes_allow_line_breaks(repo, tmp_path):
    bold = " ".join(f"word{index}" for index in range(60))
    italic = " ".join(f"term{index}" for index in range(60))
    old, new = two_revisions(
        repo, {INTRO: f"*{italic}* {{#p-format}}\n"}, {INTRO: f"**{bold}** {{#p-format}}\n"},
    )
    artifact = compile_marked(repo, tmp_path, old.revision, new.revision)["manuscript"]
    marked = artifact.marked_path.read_text(encoding="utf-8")
    assert r"\textbf{\DIFadd{" + bold + "}}" in marked
    assert r"\emph{\DIFdel{" + italic + "}}" in marked
    assert r"\mbox{" not in marked
    assert brace_balance(marked) == 0


def test_marked_deleted_reference_has_no_dangling_link(repo, tmp_path):
    old, new = two_revisions(
        repo,
        {INTRO: "# Methods {#sec-old}\n\nSee [#sec-old] for details. {#p-ref}\n"},
        {INTRO: "See the new protocol. {#p-ref}\n"},
    )
    artifact = compile_marked(repo, tmp_path, old.revision, new.revision)["manuscript"]
    marked = artifact.marked_path.read_text(encoding="utf-8")
    assert r"\hyperref[omm:sec-old]" not in marked
    assert any("Section~1" in region for region in dif_regions(marked, deleted=True))


def test_marked_ordered_list_diffs_code_inside_one_item(repo, tmp_path):
    def source(code):
        return (
            f"1. Step\n\n   ~~~python\n   {code}()\n   ~~~\n\n"
            "2. Finish. {#li-steps}\n"
        )

    old, new = two_revisions(repo, {INTRO: source("alpha")}, {INTRO: source("beta")})
    artifact = compile_marked(repo, tmp_path, old.revision, new.revision)["manuscript"]
    marked = artifact.marked_path.read_text(encoding="utf-8")
    assert marked.count("\\item\n") == 2
    assert "\\item\nStep\n\\par\n\\DIFdelbegin\n\\begin{verbatim}\nalpha()\n" in marked
    assert "\\end{verbatim}\n\\DIFdelend\n\\DIFaddbegin\n\\begin{verbatim}\nbeta()\n" in marked
    assert "\\end{verbatim}\n\\DIFaddend\n\\item\nFinish.\n" in marked


@pytest.mark.parametrize("deleted", [True, False])
def test_marked_ordered_item_marks_code_and_math(repo, tmp_path, deleted):
    with_step = (
        "1. Start.\n"
        "2. Extra step.\n\n   ~~~python\n   alpha()\n   ~~~\n\n"
        "   $$\n   x = 1\n   $$\n\n"
        "3. Finish. {#li-steps}\n"
    )
    without_step = "1. Start.\n2. Finish. {#li-steps}\n"
    before, after = (with_step, without_step) if deleted else (without_step, with_step)
    old, new = two_revisions(repo, {INTRO: before}, {INTRO: after})
    artifact = compile_marked(repo, tmp_path, old.revision, new.revision)["manuscript"]
    marked = artifact.marked_path.read_text(encoding="utf-8")
    assert (r"\item\addtocounter{enumi}{-1}" in marked) == deleted
    dif = "\\DIFdel" if deleted else "\\DIFadd"
    assert f"{dif}begin\n\\begin{{verbatim}}\nalpha()\n\\end{{verbatim}}\n{dif}end\n" in marked
    assert re.search(re.escape(dif + "begin\n\\[") + r"\s*x = 1\s*" + re.escape(r"\]"), marked)
    assert f"\\]\n{dif}end\n\\item\nFinish.\n" in marked


def test_marked_inserted_content_and_changed_panel_caption(repo, tmp_path):
    figure = (
        ":::{figure} {#fig-main}\n![Old panel](a.png){#fig-a}\n"
        "![Same panel](b.png){#fig-b}\n\nMain caption.\n:::\n"
    )
    old_body = (
        "# Stable {#sec-stable}\n\nSee [#sec-stable]. {#p-ref}\n\n"
        "- Stable item. {#li-stable}\n\n" + figure
    )
    new_body = old_body.replace("Old panel", "New panel").replace(
        "- Stable item. {#li-stable}", "- Stable item.\n- Added to existing list. {#li-stable}"
    ) + (
        "\n## New heading {#sec-new}\n\nNew paragraph. {#p-new}\n\n"
        "- New list item. {#li-new}\n\n"
        ":::{figure} {#fig-inserted}\n![Single](a.png){#fig-single}\n\nNew figure.\n:::\n\n"
        ":::{table} {#tbl-new}\nNew table.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n:::\n"
    )
    stage(
        repo, manifest({"manuscript": ("manuscript", [INTRO])}),
        {"references.bib": "", INTRO: old_body}, {"a.png": b"a", "b.png": b"b"},
    )
    first = publish(repo)
    write(repo, INTRO, new_body)
    second = publish(repo)
    artifact = compile_marked(repo, tmp_path, first, second)["manuscript"]
    marked = artifact.marked_path.read_text(encoding="utf-8")
    assert r"\DIFaddFL{New heading}" in marked
    assert r"\DIFadd{New paragraph.}" in marked
    assert r"\DIFadd{New list item.}" in marked
    assert r"\DIFadd{Added to existing list.}" in marked
    assert "Stable item." not in "".join(dif_regions(marked, deleted=False))
    assert r"\DIFaddFL{New figure.}" in marked
    assert r"\DIFaddFL{New table.}" in marked
    assert r"\DIFdelFL{Old}" in marked and r"\DIFaddFL{New}" in marked
    assert r"\caption{Same panel}\label{omm:fig-b}" in marked
    assert r"See \hyperref[omm:sec-stable]{Section~1}." in marked
    assert r"\label{omm:fig-inserted}\label{omm:fig-single}" in marked
    assert brace_balance(marked) == 0


def test_shared_new_asset_wins_over_deleted_figure(repo, tmp_path):
    def figure(name):
        return f":::{{figure}} {{#fig-{name}}}\n![Map](figures/map.png)\n\nMap.\n:::\n"

    stage(
        repo, manifest({"main": ("manuscript", [INTRO]), "appendix": ("supplementary", [APPENDIX])}),
        {"references.bib": "", INTRO: figure("main"), APPENDIX: figure("appendix")},
        {"figures/map.png": b"milestone"},
    )
    old = snapshot_at(repo, publish(repo))
    write(repo, INTRO, figure("main"))
    write(repo, APPENDIX, "Removed map. {#p-no-map}\n")
    write_asset(repo, "figures/map.png", b"current")
    new = snapshot_at(repo, publish(repo))
    out_dir = tmp_path / "build"
    artifacts = compile_latex_workspace(new, out_dir, milestone=old)
    path = out_dir / "figures/map.png"
    assert path in artifacts["main"].asset_paths
    assert path.read_bytes() == b"current"
    assert r"\includegraphics[width=\linewidth]{figures/map.png}" in (
        artifacts["main"].clean_path.read_text(encoding="utf-8")
    )
    [archived] = artifacts["appendix"].clean_path.parent.glob("figures/.omm_old_*_map.png")
    assert archived.read_bytes() == b"milestone"
    assert archived in artifacts["appendix"].asset_paths
    marked = artifacts["appendix"].marked_path.read_text(encoding="utf-8")
    assert f"figures/{archived.name}" in marked


def test_inline_images_render_alt_text_without_exporting_assets(repo, tmp_path):
    stage(
        repo, manifest({"main": ("manuscript", [INTRO]), "appendix": ("supplementary", [APPENDIX])}),
        {
            "references.bib": "",
            INTRO: "Inline ![Map & legend](figures/map.png) and ![Key](missing.png). {#p-image}\n",
            APPENDIX: ":::{figure} {#fig-map}\n![Map](figures/map.png)\n\nMap.\n:::\n",
        },
        {"figures/map.png": b"managed figure"},
    )
    artifact = compile_out(repo, tmp_path, publish(repo))["main"]
    clean = artifact.clean_path.read_text(encoding="utf-8")
    assert r"Inline Map \& legend and Key." in clean
    assert r"\includegraphics" not in clean
    assert not artifact.asset_paths


def test_clean_deep_headings_quoted_lists_and_literal_closers(repo, tmp_path):
    source = (
        "###### Deep first {#sec-deep}\n\nSee [#sec-deep]. {#p-ref}\n\n"
        "> 3. ## Item title\n>    - Nested **word**\n>    - Next\n"
        "> 4. Text before code\n>    ~~~text\n>    Sample code\n>    ~~~\n"
        "> {#li-quote}\n\n"
        "$$\nx = 2 % numbered\n$$ {#eq-comment}\n\n"
        "$$\ny = 3 % unnumbered\n$$ {#eq-comment-free unnumbered}\n\n"
        "~~~text\n  \\end{verbatim}\nreset\n~~~\n"
    )
    stage(
        repo, manifest({"manuscript": ("manuscript", [INTRO])}),
        {"references.bib": "", INTRO: source}, {},
    )
    artifact = compile_out(repo, tmp_path, publish(repo))["manuscript"]
    rendered = artifact.clean_path.read_text(encoding="utf-8")
    assert rendered.count(r"\begin{quote}") == 1
    assert r"\setcounter{section}{1}" in rendered
    assert r"\setcounter{subsection}{1}" in rendered
    assert r"\setcounter{subparagraph}{1}" in rendered
    assert r"\subsubparagraph{Deep first}" in rendered
    assert r"\hyperref[omm:sec-deep]{Section~1.1.1.1.1.1}" in rendered
    assert r"\setcounter{enumi}{2}" in rendered
    assert r"\textbf{Item title}" in rendered
    assert r"Nested \textbf{word}" in rendered
    assert "\\begin{verbatim}\nSample code\n\\end{verbatim}" in rendered
    assert "% numbered\n\\end{equation}" in rendered
    assert "% unnumbered\n\\]" in rendered
    assert r"\texttt{\strut \ \ \textbackslash{}end\{verbatim\}}" in rendered
    assert r"\texttt{\strut reset}" in rendered


def test_sparse_numeric_bibliography_uses_global_labels(repo, tmp_path):
    stage(
        repo,
        manifest(
            {"main": ("manuscript", [INTRO]), "appendix": ("supplementary", [APPENDIX])},
            style='[style]\ncitation_style = "numeric"\n',
        ),
        {
            "references.bib": BIB + "@article{k3, author={A. Smith}, year={2022}, title={Extra}}",
            INTRO: "Earlier [@beven2020; @kirchner2009]. {#p-main}\n",
            APPENDIX: "Cites [@beven2020; @k3]. {#p-appendix}\n",
        },
        {},
    )
    artifact = compile_out(repo, tmp_path, publish(repo))["appendix"]
    rendered = artifact.clean_path.read_text(encoding="utf-8")
    assert r"\begin{thebibliography}{3}" in rendered
    assert r"\bibitem[1]{beven2020}" in rendered
    assert r"\bibitem[3]{k3}" in rendered


def test_heading_counter_resets_when_a_level_is_skipped_after_a_new_section(repo, tmp_path):
    source = (
        "# One {#sec-one}\n\n## First {#sec-first}\n\n### Previous {#sec-previous}\n\n"
        "# Two {#sec-two}\n\n### Skipped subsection {#sec-skipped}\n"
    )
    stage(
        repo, manifest({"manuscript": ("manuscript", [INTRO])}),
        {"references.bib": "", INTRO: source}, {},
    )
    artifact = compile_out(
        repo, tmp_path, publish(repo), pdf_deliverables=["manuscript"]
    )["manuscript"]
    assert artifact.blocks[-1].plain_text == "2.1.1 Skipped subsection"
    rendered = artifact.clean_path.read_text(encoding="utf-8")
    assert (
        "\\setcounter{subsection}{1}\n\\setcounter{subsubsection}{0}\n"
        "\\linelabel{omm:ls:sec-skipped}\\subsubsection{Skipped subsection}"
    ) in rendered


def test_author_year_bibliography_width_escapes_institution_name(repo, tmp_path):
    stage(
        repo, manifest({"manuscript": ("manuscript", [INTRO])}),
        {
            "references.bib": "@report{agency, author={{Water & Land}}, year={2020}, title={Study}}",
            INTRO: "See [@agency]. {#p-agency}\n",
        },
        {},
    )
    artifact = compile_out(repo, tmp_path, publish(repo))["manuscript"]
    assert r"\begin{thebibliography}{Water \& Land (2020)}" in artifact.clean_path.read_text()


def test_marked_renumbering_and_year_disambiguation(repo: Path, tmp_path: Path) -> None:
    stage(
        repo,
        manifest({"manuscript": ("manuscript", [INTRO])}),
        {
            "references.bib": "@article{a2020, author={Beven, K.}, year={2020}, title={One}}\n",
            INTRO: (
                "See [#fig-a] in [@a2020]. {#p-x}\n\n"
                ":::{figure} {#fig-a}\n![A](figures/old.png)\n\nAlpha.\n:::\n"
            ),
        },
        {"figures/old.png": b"old", "figures/new.png": b"new"},
    )
    first = publish(repo)
    write(
        repo,
        "references.bib",
        "@article{a2020, author={Beven, K.}, year={2020}, title={One}}\n"
        "@article{b2020, author={Beven, K.}, year={2020}, title={Two}}\n",
    )
    write(
        repo,
        INTRO,
        (
            "See [#fig-a] and [#fig-b] in [@a2020; @b2020]. {#p-x}\n\n"
            ":::{figure} {#fig-b}\n![B](figures/new.png)\n\nBeta.\n:::\n\n"
            ":::{figure} {#fig-a}\n![A](figures/old.png)\n\nAlpha.\n:::\n"
        ),
    )
    second = publish(repo)
    results = compile_marked(repo, tmp_path, first, second)
    document = results["manuscript"].marked_path.read_text(encoding="utf-8")
    deleted = " ".join(dif_regions(document, deleted=True))
    inserted = " ".join(dif_regions(document, deleted=False))
    # Fig. 1 became Fig. 2 and 2020 became 2020a inside the kept paragraph.
    assert "Fig.~1" in deleted
    assert "Fig.~2" in inserted
    assert "2020" in deleted
    assert "2020a" in inserted
    assert document.count("\\DIFdelbegin") == document.count("\\DIFdelend")
    assert document.count("\\DIFaddbegin") == document.count("\\DIFaddend")
    assert brace_balance(document) == 0


def test_marked_cross_chapter_move_has_no_duplicate_labels(
    repo: Path, tmp_path: Path
) -> None:
    stage(
        repo,
        manifest(
            {
                "manuscript": ("manuscript", [INTRO]),
                "supplementary": ("supplementary", [APPENDIX]),
            }
        ),
        {
            "references.bib": "",
            INTRO: "Stay. {#p-stay}\n\nMover. {#p-move}\n",
            APPENDIX: "# Appendix {#sec-app}\n",
        },
        {},
    )
    first = publish(repo)
    write(repo, INTRO, "Stay. {#p-stay}\n")
    write(repo, APPENDIX, "# Appendix {#sec-app}\n\nMover. {#p-move}\n")
    second = publish(repo)
    results = compile_marked(repo, tmp_path, first, second)
    main = results["manuscript"].marked_path.read_text(encoding="utf-8")
    supplement = results["supplementary"].marked_path.read_text(encoding="utf-8")
    assert any("Mover." in region for region in dif_regions(main, deleted=True))
    assert "omm:p-move" not in main
    assert any("Mover." in region for region in dif_regions(supplement, deleted=False))
    assert supplement.count("\\label{omm:p-move}") == 1
    assert supplement.count("\\linelabel{omm:ls:p-move}") == 1


def test_marked_deleted_equation_figure_and_counters(repo: Path, tmp_path: Path) -> None:
    stage(
        repo,
        manifest({"manuscript": ("manuscript", [INTRO])}),
        {
            "references.bib": "",
            INTRO: (
                "$$u = f(x)$$ {#eq-a}\n\n"
                ":::{figure} {#fig-f}\n![F](figures/f.png)\n\nChart.\n:::\n\n"
                "Kept. {#p-keep}\n"
            ),
        },
        {"figures/f.png": b"f"},
    )
    first = publish(repo)
    write(repo, INTRO, "Kept. {#p-keep}\n")
    second = publish(repo)
    results = compile_marked(repo, tmp_path, first, second)
    document = results["manuscript"].marked_path.read_text(encoding="utf-8")
    deleted = " ".join(dif_regions(document, deleted=True))
    # Deleted numbered equations are struck in star form and never advance the
    # equation counter or emit a duplicate label.
    assert "\\[u = f(x)\n\\]" in deleted
    assert "\\caption*{Chart.}" in document
    assert "omm:eq-a" not in document
    assert "omm:fig-f" not in document
    assert document.count("\\begin{equation}") == 0
    # One deleted equation plus one deleted figure: exactly one marker pair
    # each in the body, never nested.
    body = document[document.index("\\maketitle") :]
    assert body.count("\\DIFdelbegin") == 2
    assert body.count("\\DIFdelend") == 2
    assert all(
        "\\DIFdelbegin" not in region for region in dif_regions(document, deleted=True)
    )
    assert brace_balance(document) == 0


def test_marked_homogeneous_table_marks_cells_only(repo: Path, tmp_path: Path) -> None:
    table_v1 = (
        ":::{table} {#tbl-metrics}\nOld caption.\n\n"
        "| A | B |\n|:---|---:|\n| 0.87 | 0.84 |\n\n"
        "Old note.\n:::\n"
    )
    table_v2 = (
        ":::{table} {#tbl-metrics}\nNew caption.\n\n"
        "| A | B |\n|:---|---:|\n| 0.88 | 0.84 |\n\n"
        "New note.\n:::\n"
    )
    milestone, snapshot = two_revisions(repo, {INTRO: table_v1}, {INTRO: table_v2})
    results = compile_marked(repo, tmp_path, milestone.revision, snapshot.revision)
    document = results["manuscript"].marked_path.read_text(encoding="utf-8")
    assert document.count("\\begin{tabular}") == 1
    assert "\\DIFdelFL{Old}" in document
    assert "\\DIFaddFL{New}" in document
    assert "\\DIFdel{0.87}" in document
    assert "\\DIFadd{0.88}" in document
    # Table structure tokens are never wrapped in revision marks.
    for region in dif_regions(document, deleted=True) + dif_regions(document, deleted=False):
        assert "&" not in region
        assert "\\toprule" not in region
        assert "\\midrule" not in region
        assert "\\cmidrule" not in region
        assert "\\\\" not in region
    assert brace_balance(document) == 0


def test_marked_heterogeneous_table_whole_replacement(repo: Path, tmp_path: Path) -> None:
    table_v1 = ":::{table} {#tbl-metrics}\nCaption.\n\n| A | B |\n|:---|---:|\n| 1 | 2 |\n:::\n"
    table_v2 = (
        ":::{table} {#tbl-metrics}\nCaption.\n\n"
        "| A | B | C |\n|:---|---:|---:|\n| 1 | 2 | 3 |\n:::\n"
    )
    milestone, snapshot = two_revisions(repo, {INTRO: table_v1}, {INTRO: table_v2})
    results = compile_marked(repo, tmp_path, milestone.revision, snapshot.revision)
    document = results["manuscript"].marked_path.read_text(encoding="utf-8")
    deleted = dif_regions(document, deleted=True)
    assert any("1 & 2" in region and "1 & 2 & 3" not in region for region in deleted)
    assert "1 & 2 & 3" in document
    assert document.count("\\begin{tabular}") == 2
    assert document.count("\\caption*{Caption.}") == 1
    assert "\\DIFaddFL{Caption.}" in document
    assert document.count("\\label{omm:tbl-metrics}") == 1
    assert brace_balance(document) == 0


def test_marked_bibliography_lcs_entries(repo: Path, tmp_path: Path) -> None:
    bib_v1 = (
        "@article{k1, author={A. Alpha}, year={2001}, title={One}}\n"
        "@article{k2, author={B. Beta}, year={2002}, title={Two}}\n"
        "@article{k3, author={C. Gamma}, year={2003}, title={Three}}\n"
    )
    bib_v2 = bib_v1 + "@article{k4, author={D. Delta}, year={2004}, title={Four}}\n"
    style = '[style]\ncitation_style = "numeric"\n'
    stage(
        repo,
        manifest({"manuscript": ("manuscript", [INTRO])}, style=style),
        {"references.bib": bib_v1, INTRO: "One [@k1; @k2; @k3]. {#p-x}\n"},
        {},
    )
    first = publish(repo)
    write(repo, "references.bib", bib_v2)
    write(repo, INTRO, "One [@k3; @k1; @k4]. {#p-x}\n")
    second = publish(repo)
    results = compile_marked(repo, tmp_path, first, second)
    document = results["manuscript"].marked_path.read_text(encoding="utf-8")
    assert document.count("\\DIFdelbegin \\bibitem[") == 2
    assert document.count("\\DIFaddbegin \\bibitem[") == 2
    assert "\\DIFaddbegin \\bibitem[3]{k4}" in document
    assert document.count("\\label{omm:bib:k4}") == 1
    assert "\\bibitem[2]{omm-del-k2}" in document
    assert "\\bibitem[3]{omm-del-k3}" in document
    assert "\\bibitem[1]{k3}" in document
    assert r"\hyperref[omm:bib:k2]" not in document
    assert r"\textcolor{OmmLinkColor}{2}" in document
    keys = re.findall(r"\\bibitem\[\d+\]\{([^}]+)\}", document)
    assert len(keys) == len(set(keys)) == 5
    deleted_entries = " ".join(dif_regions(document, deleted=True))
    for key in ("k1", "k2", "k3"):
        assert f"\\label{{omm:bib:{key}}}" not in deleted_entries
    assert brace_balance(document) == 0


def test_marked_kept_bibliography_entry_year_suffix(repo: Path, tmp_path: Path) -> None:
    bib_v1 = "@article{a2020, author={Beven, K.}, year={2020}, title={One}}\n"
    bib_v2 = (
        "@article{a2020, author={Beven, K.}, year={2020}, title={One}}\n"
        "@article{b2020, author={Beven, K.}, year={2020}, title={Two}}\n"
    )
    stage(
        repo,
        manifest({"manuscript": ("manuscript", [INTRO])}),
        {"references.bib": bib_v1, INTRO: "Cited [@a2020]. {#p-x}\n"},
        {},
    )
    first = publish(repo)
    write(repo, "references.bib", bib_v2)
    # Both same-author-year entries must be cited for 2020a/b disambiguation.
    write(repo, INTRO, "Cited [@a2020]. Also [@b2020]. {#p-x}\n")
    second = publish(repo)
    results = compile_marked(repo, tmp_path, first, second)
    document = results["manuscript"].marked_path.read_text(encoding="utf-8")
    inserted = " ".join(dif_regions(document, deleted=False))
    assert "2020a" in inserted
    assert "\\DIFdelFL{(2020)}" in document
    assert "\\DIFaddFL{(2020a)}" in document
    assert "\\label{omm:bib:a2020}" in document
    assert brace_balance(document) == 0


def test_marked_figure_asset_replacement_archives_old_image(
    repo: Path, tmp_path: Path
) -> None:
    intro = ":::{figure} {#fig-f}\n![F](figures/panel.png)\n\nPanel.\n:::\n"
    stage(
        repo,
        manifest({"manuscript": ("manuscript", [INTRO])}),
        {"references.bib": "", INTRO: intro},
        {"figures/panel.png": b"first-bytes"},
    )
    first = publish(repo)
    write_asset(repo, "figures/panel.png", b"second-bytes")
    second = publish(repo)
    results = compile_marked(repo, tmp_path, first, second)
    deliverable = results["manuscript"]
    document = deliverable.marked_path.read_text(encoding="utf-8")
    archived = [path for path in deliverable.asset_paths if ".omm_old_" in path.name]
    assert len(archived) == 1
    assert archived[0].read_bytes() == b"first-bytes"
    assert archived[0].parent == deliverable.clean_path.parent / "figures"
    digest = archived[0].name.split("_")[2]
    assert f"figures/.omm_old_{digest}_panel.png" in document
    assert "figures/panel.png" in document
    assert document.count("\\caption*{Panel.}") == 1
    assert "\\DIFaddFL{Panel.}" in document
    assert document.count("\\label{omm:fig-f}") == 1
    # The clean document never references the archived old image.
    clean = deliverable.clean_path.read_text(encoding="utf-8")
    assert ".omm_old_" not in clean
    assert "figures/panel.png" in clean


def test_marked_inline_math_atomic_replacement(repo: Path, tmp_path: Path) -> None:
    milestone, snapshot = two_revisions(
        repo,
        {INTRO: "Value $a_1$ stays. {#p-x}\n"},
        {INTRO: "Value $a_2$ stays. {#p-x}\n"},
    )
    results = compile_marked(repo, tmp_path, milestone.revision, snapshot.revision)
    document = results["manuscript"].marked_path.read_text(encoding="utf-8")
    assert "\\DIFdel{\\mbox{$a_1$}}" in document
    assert "\\DIFadd{\\mbox{$a_2$}}" in document
    assert brace_balance(document) == 0


def test_marked_display_math_atomic_replacement(repo: Path, tmp_path: Path) -> None:
    milestone, snapshot = two_revisions(
        repo,
        {INTRO: "$$u = f(x)$$ {#eq-a}\n"},
        {INTRO: "$$u = g(x)$$ {#eq-a}\n"},
    )
    results = compile_marked(repo, tmp_path, milestone.revision, snapshot.revision)
    document = results["manuscript"].marked_path.read_text(encoding="utf-8")
    assert "\\[u = f(x)\n\\]" in document
    assert "\\begin{equation}\\label{omm:eq-a} u = g(x)\n\\end{equation}" in document
    assert document.count("\\begin{equation}") == 1
    assert document.count("\\label{omm:eq-a}") == 1


def test_marked_compile_is_byte_identical_when_repeated(
    repo: Path, tmp_path: Path
) -> None:
    intro_v1 = (
        "Text [@beven2020] and [#fig-f]. {#p-x}\n\n"
        ":::{figure} {#fig-f}\n![F](figures/f.png)\n\nF.\n:::\n"
    )
    stage(
        repo,
        manifest({"manuscript": ("manuscript", [INTRO])}),
        {"references.bib": BIB, INTRO: intro_v1},
        {"figures/f.png": b"f"},
    )
    first = publish(repo)
    write(
        repo,
        INTRO,
        "Revised [@beven2020] and [#fig-f] text. {#p-x}\n\n"
        ":::{figure} {#fig-f}\n![F](figures/f.png)\n\nF.\n:::\n",
    )
    second = publish(repo)
    compile_marked(repo, tmp_path, first, second)
    compile_marked(repo, tmp_path, first, second)
    directories = sorted(tmp_path.glob("out-marked-*"))
    assert len(directories) == 2
    for name in ("manuscript.clean.tex", "manuscript.marked.tex"):
        assert (directories[0] / name).read_bytes() == (directories[1] / name).read_bytes()


def test_milestone_does_not_duplicate_block_records(repo: Path, tmp_path: Path) -> None:
    milestone, snapshot = two_revisions(
        repo,
        {INTRO: "# Heading {#sec-h}\n\nFirst. {#p-a}\n"},
        {INTRO: "# Heading {#sec-h}\n\nSecond. {#p-a}\n"},
    )
    clean = compile_out(repo, tmp_path, snapshot.revision)
    marked = compile_marked(repo, tmp_path, milestone.revision, snapshot.revision)
    expected = (
        LatexBlockRecord(
            "sec-h", "heading", "omm:sec-h", "omm:ls:sec-h", "omm:le:sec-h", "Heading"
        ),
        LatexBlockRecord(
            "p-a", "paragraph", "omm:p-a", "omm:ls:p-a", "omm:le:p-a", "Second."
        ),
    )
    # The Marked pass re-renders every kept block; it must not append a second
    # copy of the Clean block records.
    assert clean["manuscript"].blocks == expected
    assert marked["manuscript"].blocks == expected


def test_code_blocks_render_verbatim_and_marked_replacement(
    repo: Path, tmp_path: Path
) -> None:
    intro_v1 = "```python\nalpha_one()\n```\n\n```python\nbeta_two()\n```\n"
    intro_v2 = "```python\nalpha_one()\n```\n\n```python\nbeta_changed()\n```\n"
    milestone, snapshot = two_revisions(repo, {INTRO: intro_v1}, {INTRO: intro_v2})
    compile_out(repo, tmp_path, snapshot.revision)
    document = read(tmp_path, snapshot.revision, "manuscript.clean.tex")
    assert document.count("\\begin{verbatim}") == 2
    assert "\\begin{verbatim}\nalpha_one()\n\\end{verbatim}" in document
    assert "alpha_one()" in document

    results = compile_marked(repo, tmp_path, milestone.revision, snapshot.revision)
    marked = results["manuscript"].marked_path.read_text(encoding="utf-8")
    # The untouched anonymous code block pairs and renders once; only the
    # changed one is deleted whole and re-inserted whole.
    assert marked.count("\\begin{verbatim}") == 3
    assert marked.count("alpha_one()") == 1
    assert any("beta_two()" in region for region in dif_regions(marked, deleted=True))
    assert any(
        "beta_changed()" in region for region in dif_regions(marked, deleted=False)
    )
    # Verbatim material must never be wrapped in a DIF macro argument.
    assert "\\DIFdel{\\begin{verbatim}" not in marked
    assert "\\DIFadd{\\begin{verbatim}" not in marked


def test_document_title_authors_and_maketitle(repo: Path, tmp_path: Path) -> None:
    workspace_id = str(uuid.uuid4())
    authors = '[{ name = "Kate Beven" }, { name = "J. W. Kirchner" }]'
    stage(
        repo,
        manifest(
            {"manuscript": ("manuscript", [INTRO])},
            title="River Modeling",
            authors=authors,
            workspace_id=workspace_id,
        ),
        {"references.bib": "", INTRO: "Body. {#p-x}\n"},
        {},
    )
    revision = publish(repo)
    compile_out(repo, tmp_path, revision)
    document = read(tmp_path, revision, "manuscript.clean.tex")
    assert "\\title{River Modeling}" in document
    assert "\\author{Kate Beven \\and J. W. Kirchner}" in document
    assert "\\date{}" in document
    assert "\\maketitle" in document

    write(
        repo,
        MANIFEST,
        manifest(
            {"manuscript": ("manuscript", [INTRO])},
            title="River Modeling Handbook",
            authors=authors,
            workspace_id=workspace_id,
        ),
    )
    second = publish(repo)
    results = compile_marked(repo, tmp_path, revision, second)
    marked = results["manuscript"].marked_path.read_text(encoding="utf-8")
    assert "\\DIFaddFL{ Handbook}" in marked


def test_reviewer_comment_renders_raw_reference_slices(
    repo: Path, tmp_path: Path
) -> None:
    stage(
        repo,
        manifest(
            {
                "manuscript": ("manuscript", [INTRO]),
                "response_letter": ("response_letter", [REBUTTAL]),
            }
        ),
        {
            "references.bib": BIB,
            INTRO: "Target. {#p-target}\n",
            REBUTTAL: (
                ':::{review-item} {#R1-C1 reviewer="Reviewer 1"}\n'
                ":::{comment}\nWhy this? See [#p-gone] and [@ghost2019].\n:::\n\n"
                "We fixed it in [#p-target]. {#p-r1}\n"
                ":::\n"
            ),
        },
        {},
    )
    revision = publish(repo)
    compile_out(repo, tmp_path, revision)
    letter = read(tmp_path, revision, "response_letter.clean.tex")
    # Stale references inside the reviewer's comment are quoted verbatim.
    assert "[\\#p-gone]" in letter
    assert "[@ghost2019]" in letter
    assert "omm:p-gone" not in letter
    assert "omm:bib:ghost2019" not in letter
    # The author's reply targets another deliverable: colored text, no anchor.
    assert "omm:p-target" not in letter
    assert "\\textcolor{OmmLinkColor}{p-target}" in letter


def test_deliverables_subset_validation(repo: Path, tmp_path: Path) -> None:
    stage(
        repo,
        manifest(
            {
                "manuscript": ("manuscript", [INTRO]),
                "supplementary": ("supplementary", [APPENDIX]),
            }
        ),
        {"references.bib": "", INTRO: "Main. {#p-x}\n", APPENDIX: "Appendix. {#p-app}\n"},
        {},
    )
    revision = publish(repo)
    snapshot = snapshot_at(repo, revision)
    results = compile_latex_workspace(
        snapshot, tmp_path / "out-subset", deliverables=["manuscript", "manuscript"]
    )
    assert sorted(results) == ["manuscript"]
    with pytest.raises(KeyError):
        compile_latex_workspace(
            snapshot, tmp_path / "out-unknown", deliverables=["ghost"]
        )
    write(
        repo,
        MANIFEST,
        (
            f'workspace_id = "{uuid.uuid4()}"\ntitle = "T"\nauthors = ""\n'
            'bibliography = "references.bib"\n\n'
            f'[deliverables.manuscript]\nkind = "manuscript"\nsources = ["{INTRO}"]\n\n'
            f'[deliverables."sub/dir"]\nkind = "supplementary"\nsources = ["{APPENDIX}"]\n'
        ),
    )
    with pytest.raises(OmmError, match="^ERR_MANIFEST_INVALID$") as error:
        publish(repo)
    assert error.value.diagnostic.params["field"] == "deliverables.sub/dir"
