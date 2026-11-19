"""Exercise the ADR-001 acceptance contract through Git, HTTP, and omm CLI."""

from __future__ import annotations

import json
import os
import re
import subprocess
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from zipfile import ZipFile

import pytest
from lxml import etree
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from omm.cli import main
from omm.docx_compiler import accept_docx_revisions, reject_docx_revisions, verify_docx_package
from omm.server import WorkbenchServer
from omm.workspace import resolve_path

INTRO = "sections/01-introduction.md"
METHODS = "sections/02-methods.md"
SUPPLEMENT = "sections/supplement.md"
REPLY = "reviews/reply.md"
BEFORE = r"Specific yield $S_y$ is constant [@rivers-a]."
AFTER = r"Specific yield $S_y$ changes with depth [@rivers-a]."
OLD_EQ = r"q = \begin{cases} x, & \text{if } x>1 \\ 0, & \text{otherwise} \end{cases}"
NEW_EQ = OLD_EQ.replace("x>1", "x>0")
BIB_A = "@article{rivers-a, author={Rivers, Ada}, year={2020}, title={Alpha catchment}}\n"
BIB_B = "@article{rivers-b, author={Rivers, Ada}, year={2020}, title={Beta catchment}}\n"
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
}
W = "{" + NS["w"] + "}"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.org", *args],
        cwd=root, capture_output=True, text=True, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    ).stdout.strip()


def _write(root: Path, name: str, content: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


@pytest.fixture
def workspace(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    monkeypatch.setenv("OMM_STATE_DIR", str(state))
    monkeypatch.setenv("OMM_DEV_SOURCE", "1")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty.gitconfig"))
    monkeypatch.setenv("GIT_DEFAULT_HASH", "sha1")
    root = tmp_path / "Paper"

    def run(*args: str, expected: int = 0, cwd: Path = root):
        status = main(["--lang", "en", "--cwd", str(cwd), *args])
        result = capsys.readouterr()
        assert status == expected, result
        return result

    run("init", "--title", "Catchment study")
    return root, run


def _publish(run) -> str:
    return json.loads(run("sync", "--json").out)["revision"]


@contextmanager
def _http(state: Path, workspace_id: str):
    with WorkbenchServer(state_dir=state, port=0) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(path: str, body: dict | None = None) -> dict:
            connection = HTTPConnection(*server.server_address, timeout=5)
            try:
                connection.request(
                    "GET" if body is None else "POST",
                    f"/api/workspaces/{workspace_id}/{path}",
                    body=None if body is None else json.dumps(body),
                    headers={
                        "Origin": f"http://{server.authority}",
                        "X-OMM-CSRF": server.csrf_token,
                        "Sec-Fetch-Site": "same-origin",
                    },
                )
                with connection.getresponse() as response:
                    result = json.load(response)
                    assert response.status == 200, result
                    return result
            finally:
                connection.close()

        try:
            yield request
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


def _numbered_pdf(path: Path, *, offset: int = 0) -> None:
    """Lay out the revised Clean manuscript as two pages of positioned PDF text."""
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    pages = [
        [
            (74, 728, 14, "Catchment study"),
            (42, 705, 8, "1"), (74, 705, 11, "1 Introduction"),
            (42, 685, 8, "2"), (74, 685, 11, "The boundary follows Eq. (1)."),
            (238, 668, 11, "x, if x > 0"),
            (42, 660, 8, "3"), (190, 660, 11, "q ="), (218, 660, 22, "{"),
            (526, 660, 11, "(1)"),
            (238, 652, 11, "0, otherwise"),
        ],
        [
            (42, 705, 8, "4"), (74, 705, 11, "2 Methods"),
            (42, 685, 8, "5"), (74, 685, 11, "Specific yield S"),
            (146, 681, 7, "y"), (153, 685, 11, "changes with depth (Rivers, 2020a)."),
            (42, 665, 8, "6"), (74, 665, 11, "References"),
            (42, 645, 8, "7"), (74, 645, 11, "Rivers, A. (2020a). Alpha catchment."),
        ],
    ]
    writer = PdfWriter()
    for rows in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
        content = DecodedStreamObject()
        content.set_data("\n".join(
            f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm "
            f"<{(str(int(value) + offset) if x == 42 else value).encode('ascii').hex()}> Tj ET"
            for x, y, size, value in rows
        ).encode("ascii"))
        page.replace_contents(content)
    writer.write(path)


def _document(path: str) -> etree._Element:
    with ZipFile(path) as package:
        return etree.fromstring(package.read("word/document.xml"))


def _normalized_document(doc: etree._Element) -> bytes:
    # Marked reserves bookmark names and IDs across both versions. Compare
    # their topology and link targets in document order after renumbering.
    doc = etree.fromstring(etree.tostring(doc))
    ids = {}
    names = {}
    for i, start in enumerate(doc.iter(W + "bookmarkStart"), 1):
        normalized = str(i)
        ids[start.get(W + "id")] = normalized
        names[start.get(W + "name")] = normalized
        start.set(W + "id", normalized)
        start.set(W + "name", normalized)
    for end in doc.iter(W + "bookmarkEnd"):
        end.set(W + "id", ids[end.get(W + "id")])
    for link in doc.iter(W + "hyperlink"):
        if (anchor := link.get(W + "anchor")) is not None:
            link.set(W + "anchor", names[anchor])
    return etree.tostring(doc, method="c14n")


def _bibliography(doc: etree._Element) -> list[str]:
    return [
        "".join(para.xpath(".//w:t/text()", namespaces=NS))
        for para in doc.xpath("//w:p[w:pPr/w:pStyle/@w:val='Bibliography']", namespaces=NS)
    ]


def test_adr_001_revision_cycle(workspace, tmp_path):
    root, run = workspace
    manifest = (root / "manuscript.toml").read_text(encoding="utf-8")
    workspace_id = re.search(r'^workspace_id = "([^"]+)"', manifest)[1]
    _write(root, "manuscript.toml", manifest.replace(
        f'sources = ["{INTRO}"]', f'sources = ["{INTRO}", "{METHODS}"]',
    ) + f"""
[deliverables.supplementary]
kind = "supplementary"
sources = ["{SUPPLEMENT}"]

[deliverables.response_letter]
kind = "response_letter"
sources = ["{REPLY}"]

[style]
bibliography_placement = "per-deliverable"
""")
    intro = "# Introduction {#sec-intro}\n\nThe boundary follows [#eq-flux]. {#p-boundary}\n\n"
    methods = "# Methods {#sec-methods}\n"
    _write(root, INTRO, intro + f"{BEFORE} {{#p-sy}}\n\n$$\n{OLD_EQ}\n$$ {{#eq-flux}}\n")
    _write(root, METHODS, methods)
    _write(root, SUPPLEMENT, "Sensitivity data. {#p-data}\n")
    _write(root, REPLY, "# Response to reviewers {#sec-reply}\n")
    _write(root, "references.bib", BIB_A)

    # Publish the submission and annotate the exact C1 source through HTTP.
    c1 = _publish(run)
    _git(root, "tag", "v1-submitted", c1)
    run("checkpoint", "--revision", c1)
    with _http(tmp_path / "state", workspace_id) as request:
        snapshot = request("snapshot")
        assert snapshot["revision"] == c1 and not snapshot["has_unsynced_changes"]
        block = next(block for block in snapshot["blocks"] if block["block_id"] == "p-sy")
        assert (block["source"], block["clean_source"]) == (INTRO, BEFORE)
        start = BEFORE.index("$S_y$")
        selector = {"kind": "text", "start": start, "end": start + 5, "quote": "$S_y$"}
        payload = {"block_id": "p-sy", "base_rev": c1, "selector": selector}
        first = request("annotations", {**payload, "comment": "Explain the yield assumption."})
        assert first["before_block_md"] == BEFORE and first["selector"] == selector

        # Revise on disk. The same block moves to another chapter.
        _write(root, INTRO, intro + f"$$\n{NEW_EQ}\n$$ {{#eq-flux}}\n")
        _write(root, METHODS, methods + f"\n{AFTER} {{#p-sy}}\n")
        _write(root, SUPPLEMENT, "Sensitivity data [@rivers-b]. {#p-data}\n")
        _write(root, "references.bib", BIB_A + BIB_B)
        _write(root, REPLY, """\
# Response to reviewers {#sec-reply}

:::{review-item} {#R1-C1 reviewer="Reviewer 1"}
:::{comment}
Please justify the yield assumption and flux condition.
:::

We revised the assumption in [#loc:p-sy] and the condition in [#eq-flux].
See [#sec-reply] for the response. {#p-reply}
:::
""")
        dirty = request("snapshot")
        assert dirty["revision"] == c1 and dirty["has_unsynced_changes"] is True
        assert dirty["blocks"] == snapshot["blocks"]
        second = request("annotations", {**payload, "comment": "Describe the revised range."})
        assert second["id"] != first["id"] and second["before_block_md"] == BEFORE
        assert "ERR_BUILD_UNSYNCED_CHANGES" in run("build", expected=1).err
        submitted = json.loads(run(
            "build", "--revision", "v1-submitted", "--target", "docx",
            "--out-dir", str(tmp_path / "submitted"), "--json",
        ).out)
        assert submitted["revision"] == c1 and submitted["milestone"] is None

        # Publish C2 and freeze both resolutions against the two source snapshots.
        c2 = _publish(run)
        assert c2 != c1
        for annotation in (first, second):
            resolution = request(f"annotations/{annotation['id']}/resolve", {})
            assert resolution["status"] == "resolved"
        current = request(f"snapshot?diff_base={snapshot['checkpoint_rev']}")
        assert current["revision"] == c2 and not current["has_unsynced_changes"]
        assert current["checkpoint_rev"] == c1
        assert current["diff"]["base_revision"] == c1
        assert current["diff"]["target_revision"] == c2
        moved = current["diff"]["blocks"]["p-sy"]
        assert (moved["status"], moved["before_source"], moved["after_source"]) == (
            "modified_and_moved", INTRO, METHODS,
        )
        assert len(current["annotations"]) == 2
        for annotation in current["annotations"]:
            [resolution] = annotation["resolutions"]
            assert (annotation["base_rev"], annotation["before_block_md"]) == (c1, BEFORE)
            assert (resolution["after_rev"], resolution["after_block_md"]) == (c2, AFTER)
            assert resolution["diff_chunks"] == moved["chunks"]
        assert not run("check").err

    # Compile both formats using the printed line numbers from the matching PDF layout.
    word_layout = tmp_path / "word-numbered.pdf"
    tex_layout = tmp_path / "tex-numbered.pdf"
    _numbered_pdf(word_layout)
    _numbered_pdf(tex_layout, offset=100)
    built = json.loads(run(
        "build", "--layout-pdf", f"docx:manuscript={word_layout}",
        "--layout-pdf", f"latex:manuscript={tex_layout}", "--json",
    ).out)
    assert (built["revision"], built["milestone"]) == (c2, c1)
    assert built["out_dir"] == str(resolve_path(root / ".omm" / "build"))
    assert built["resolved_locations"] == {
        "docx": {"p-sy": "manuscript.clean.docx, p. 2, line 5"},
        "latex": {"p-sy": "manuscript.clean.tex, p. 2, line 105"},
    }
    word = built["deliverables"]["docx"]
    latex = built["deliverables"]["latex"]
    response_doc = _document(word["response_letter"]["clean"])
    colored_runs = response_doc.xpath(
        "//w:r[w:rPr/w:color/@w:val='0070C0']/w:t/text()", namespaces=NS,
    )
    assert built["resolved_locations"]["docx"]["p-sy"] in colored_runs
    assert "Eq. (1)" in colored_runs
    response_tex = Path(latex["response_letter"]["clean"]).read_text(encoding="utf-8")
    assert built["resolved_locations"]["latex"]["p-sy"] in response_tex
    assert r"\hyperref[omm:sec-reply]" not in response_tex
    assert r"\textcolor{OmmLinkColor}{Eq.~(1)}" in response_tex

    clean = _document(word["manuscript"]["clean"])
    appendix = _document(word["supplementary"]["clean"])
    assert _bibliography(clean) == ["Rivers, A. (2020a). Alpha catchment."]
    assert _bibliography(appendix) == ["Rivers, A. (2020b). Beta catchment."]
    for name, letter, citekey, title in (
        ("manuscript", "a", "rivers-a", "Alpha catchment"),
        ("supplementary", "b", "rivers-b", "Beta catchment"),
    ):
        contents = Path(latex[name]["clean"]).read_text(encoding="utf-8")
        bibliography = contents.split(r"\begin{thebibliography}", 1)[1]
        assert bibliography.count(r"\bibitem") == 1
        assert f"{{{citekey}}}" in bibliography
        assert f"Rivers, A. (2020{letter}). {title}." in bibliography

    marked_path = Path(word["manuscript"]["marked"])
    verify_docx_package(marked_path)
    marked = _document(str(marked_path))
    assert len(marked.xpath("//m:oMath[m:d//m:m]", namespaces=NS)) == 2
    accepted = accept_docx_revisions(marked)
    rejected = reject_docx_revisions(marked)
    old_clean = _document(submitted["deliverables"]["docx"]["manuscript"]["clean"])
    assert _normalized_document(accepted) == _normalized_document(clean)
    assert _normalized_document(rejected) == _normalized_document(old_clean)
    assert accepted.xpath("//w:hyperlink/w:r/w:rPr/w:color/@w:val", namespaces=NS) == [
        "0070C0", "0070C0",
    ]

    marked_tex = Path(latex["manuscript"]["marked"]).read_text(encoding="utf-8")
    body = marked_tex.split(r"\begin{document}", 1)[1]
    regions = re.findall(r"\\DIF(add|del)(begin|end)(FL)?\b", body)
    stack = []
    for action, boundary, floating in regions:
        if boundary == "begin":
            stack.append((action, floating))
        else:
            assert stack.pop() == (action, floating)
    assert regions and not stack
    assert r"\DIFdelFL{(2020)}" in body and r"\DIFaddFL{(2020a)}" in body
    assert f"\\DIFdelbegin\n\\[{OLD_EQ}\n\\]\n\\DIFdelend" in body
    assert re.search(
        r"\\DIFaddbegin\n[^\n]+" + re.escape(NEW_EQ) + r"\n\\end\{equation\}.*\n\\DIFaddend",
        body,
    )


def test_build_rejects_workspace_source_directories(workspace, monkeypatch):
    root, run = workspace
    monkeypatch.chdir(root)
    asset = root / "figures" / "map.png"
    asset.parent.mkdir(exist_ok=True)
    asset.write_bytes(b"published image")
    _write(root, INTRO, ":::{figure} {#fig-map}\n![Map](figures/map.png)\n\nA map.\n:::\n")
    revision = _publish(run)
    asset.write_bytes(b"working image")
    for out_dir in (".", str(root.parent), "figures", ".omm"):
        error = run("build", "--target", "latex", "--revision", revision,
                    "--out-dir", out_dir, expected=1)
        assert "ERR_WORKSPACE_PATH_INVALID" in error.err
    assert asset.read_bytes() == b"working image"
    run("build", "--target", "latex", "--revision", revision, "--out-dir", ".omm/submission")
    assert (root / ".omm/submission/figures/map.png").read_bytes() == b"published image"


def test_build_rejects_case_aliases_of_workspace_output_paths(workspace):
    root, run = workspace
    alias = root.with_name(root.name.upper())
    if not alias.exists() or not alias.samefile(root):
        pytest.skip("Requires a case-insensitive filesystem.")
    _write(root, INTRO, "Introduction. {#p-intro}\n")
    revision = _publish(run)
    (root / ".omm" / "Existing").mkdir()
    for out_dir in (alias, alias / "new" / "child", alias / "SECTIONS",
                    alias / "SECTIONS" / "new", root / ".OMM" / "Existing",
                    root / ".OMM" / "new"):
        error = run("build", "--target", "latex", "--revision", revision,
                    "--out-dir", str(out_dir), expected=1)
        assert "ERR_WORKSPACE_PATH_INVALID" in error.err
    run("build", "--target", "latex", "--revision", revision,
        "--out-dir", str(alias / ".omm" / "new"))


def test_build_selects_latest_ancestral_submission(workspace):
    root, run = workspace
    _write(root, INTRO, "First submission. {#p-intro}\n")
    c1 = _publish(run)
    _git(root, "tag", "v1-submitted", c1)
    _write(root, INTRO, "Second submission. {#p-intro}\n")
    c2 = _publish(run)
    _git(root, "tag", "-am", "Second submission", "v2-submitted", c2)
    _write(root, INTRO, "Revised second submission. {#p-intro}\n")
    c3 = _publish(run)
    _git(root, "tag", "v3-submitted", c3)
    result = json.loads(run("build", "--target", "latex", "--json").out)
    assert result["milestone"] == c2
    assert Path(result["deliverables"]["latex"]["manuscript"]["marked"]).is_file()


def test_build_rejects_incomparable_submission_tags(workspace):
    root, run = workspace
    original = _publish(run)
    tree = _git(root, "rev-parse", f"{original}^{{tree}}")
    left = _git(root, "commit-tree", tree, "-p", original, "-m", "Left submission")
    right = _git(root, "commit-tree", tree, "-p", original, "-m", "Right submission")
    merged = _git(root, "commit-tree", tree, "-p", left, "-p", right, "-m", "Merge")
    _git(root, "tag", "v1-submitted", left)
    _git(root, "tag", "v2-submitted", right)
    args = ("build", "--revision", merged, "--target", "latex", "--json")
    assert "ERR_MILESTONE_AMBIGUOUS" in run(*args, expected=1).err
    result = json.loads(run(*args, "--milestone", "v1-submitted").out)
    assert result["milestone"] == left


def test_build_from_git_worktree_preserves_registry(workspace, tmp_path):
    root, run = workspace
    revision = _publish(run)
    registry = tmp_path / "state" / "workspaces.json"
    original = registry.read_bytes()
    worktree = tmp_path / "worktree"
    _git(root, "worktree", "add", "--detach", str(worktree), revision)
    result = json.loads(run("build", "--target", "docx", "--json", cwd=worktree).out)
    assert result["revision"] == revision
    assert result["out_dir"] == str(resolve_path(worktree / ".omm" / "build"))
    assert Path(result["deliverables"]["docx"]["manuscript"]["clean"]).is_file()
    assert registry.read_bytes() == original
