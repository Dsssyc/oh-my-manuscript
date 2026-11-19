"""Resolve response-letter references against the paginated Clean Deliverable."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import groupby, pairwise
from math import hypot
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal, Protocol

from pypdf import PageObject, PdfReader
from pypdf.generic import ContentStream, NameObject, NullObject

from omm import ast
from omm.errors import OmmError
from omm.git_revs import RevisionSnapshot
from omm.numbering import NumberedWorkspace


class BlockRecordLike(Protocol):
    """Compiler record with optional bookmark or linelabel endpoints."""

    @property
    def block_id(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def plain_text(self) -> str: ...


@dataclass(frozen=True, slots=True)
class LineSpan:
    """One-based physical pages with the printed line endpoints on each page."""

    start_page: int
    start_line: int
    end_page: int
    end_line: int
    page_segments: tuple[tuple[int, int, int], ...]


def _span(segments: tuple[tuple[int, int, int], ...]) -> LineSpan:
    first_page, first_line, _ = segments[0]
    last_page, _, last_line = segments[-1]
    return LineSpan(first_page, first_line, last_page, last_line, segments)


def _page_label(page: int, first: int, last: int) -> str:
    lines = f"line {first}" if first == last else f"lines {first}–{last}"
    return f"p. {page}, {lines}"


def format_location_label(clean_filename: str, span: LineSpan) -> str:
    if span.start_page == span.end_page:
        label = _page_label(span.start_page, span.start_line, span.end_line)
    elif all(before[2] < after[1] for before, after in pairwise(span.page_segments)):
        label = f"pp. {span.start_page}–{span.end_page}, lines {span.start_line}–{span.end_line}"
    else:
        label = "; ".join(_page_label(*segment) for segment in span.page_segments)
    return f"{clean_filename}, {label}"


_BASELINE_TOLERANCE = 2.5  # PDF points
_SCRIPT_SIZE_RATIO = 0.8
_SCRIPT_SHIFT_RATIO = 0.5
_MARGIN_GAP = 8
_NUMBER_COLUMN_WIDTH = 24  # Accommodates right-aligned labels with differing digit counts.
_POSITIVE_INTEGER = re.compile(r"[1-9][0-9]*")
_EQUATION_NUMBER = re.compile(r"\([A-Za-z]*[0-9]+\)")
_MATH_DELIMITERS = frozenset("()[]{}|‖⟨⟩")
_LIST_MARKER = re.compile(r"^(?:[•◦▪●‣∙\uf0b7–-]|[0-9]+[.)])(?:\s+|$)")


@dataclass(frozen=True, slots=True)
class _Fragment:
    x: float
    y: float
    text: str
    size: float


@dataclass(frozen=True, slots=True)
class _VisualLine:
    page: int
    y: float
    fragments: tuple[str, ...]
    number: int | None
    page_furniture: bool
    equation_number: str | None
    size: float
    offset_glyphs: bool


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).replace("\u00ad", "").split())


def _separate_positioned_text(page: PageObject) -> None:
    # One PDF text object can position a margin number and the body separately.
    # pypdf can combine them into a single callback at the first origin. Repeat
    # the active font selection before a move to flush the buffered fragment.
    contents = page.get("/Contents")
    if contents is None or isinstance(contents.get_object(), NullObject):
        return
    stream = ContentStream(contents.get_object(), page.pdf, "bytes")
    operations = []
    font = None
    saved_fonts = []
    buffered = False
    for operands, operator in stream.operations:
        if operator == b"q":
            saved_fonts.append(font)
        elif operator == b"Q" and saved_fonts:
            font = saved_fonts.pop()
        elif operator == b"Tf":
            font = operands
        if operator in (b"Tm", b"Td", b"TD", b"T*") and buffered and font:
            operations.append((font, b"Tf"))
            buffered = False
        if operator in (b"BT", b"ET", b"Tf", b"cm", b"Do"):
            buffered = False
        elif operator in (b"Tj", b"TJ", b"'", b'"'):
            buffered = True
        operations.append((operands, operator))
    stream.operations = operations
    page[NameObject("/Contents")] = stream


def _pdf_rows(page: PageObject) -> list[list[_Fragment]]:
    fragments: list[_Fragment] = []
    left, bottom, right, top = map(float, page.cropbox)

    def visit(text, cm, tm, _font, size):
        if not text.strip():
            return
        # Transform the text origin into page user space.
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        if left <= x < right and bottom <= y < top:
            vertical_scale = hypot(tm[2] * cm[0] + tm[3] * cm[2], tm[2] * cm[1] + tm[3] * cm[3])
            fragments.append(_Fragment(x, y, text.strip(), size * vertical_scale))

    _separate_positioned_text(page)
    page.extract_text(orientations=(0,), visitor_text=visit)
    rows: list[list[_Fragment]] = []
    for fragment in sorted(fragments, key=lambda part: -part.y):
        if not rows or rows[-1][0].y - fragment.y > _BASELINE_TOLERANCE:
            rows.append([])
        rows[-1].append(fragment)
    return [sorted(row, key=lambda part: part.x) for row in rows]


def _margin_column(rows: list[list[_Fragment]]) -> tuple[float, float] | None:
    # Use body starts from both numbered and unnumbered rows. Numbered list
    # items within the body must not establish a line-number column.
    starts: list[float] = []
    candidates: list[float] = []
    for first, *rest in rows:
        if not _POSITIVE_INTEGER.fullmatch(first.text):
            starts.append(first.x)
        elif rest:
            starts.append(rest[0].x)
            if rest[0].x - first.x >= _MARGIN_GAP:
                candidates.append(first.x)
    if not candidates:
        return None

    # Select the most common left edge, discounting indented and centered text.
    body_edge = max(sorted(set(starts)), key=lambda x: sum(x <= s <= x + 6 for s in starts))
    candidates = sorted(x for x in candidates if x <= body_edge - _MARGIN_GAP)
    if not candidates:
        return None
    left = max(
        candidates, key=lambda x: sum(x <= c <= x + _NUMBER_COLUMN_WIDTH for c in candidates)
    )
    return left - 2, min(left + _NUMBER_COLUMN_WIDTH, body_edge - _MARGIN_GAP)


def _in_margin(fragment: _Fragment, column: tuple[float, float] | None) -> bool:
    return column is not None and column[0] <= fragment.x <= column[1]


def _body_baseline(body: list[_Fragment]) -> _Fragment:
    # A stretched delimiter can extend far above and below the text it encloses.
    text = (fragment for fragment in body if fragment.text not in _MATH_DELIMITERS)
    return max(text, key=lambda part: part.size, default=body[0])


def _attach_inline_scripts(
    rows: list[list[_Fragment]], column: tuple[float, float] | None
) -> list[list[_Fragment]]:
    bodies = [row[1:] if _in_margin(row[0], column) else row for row in rows]
    baselines = {index: _body_baseline(body) for index, body in enumerate(bodies) if body}
    attached = set()
    # A row with a margin mark has its own baseline. For a smaller offset row,
    # use the nearest larger body baseline whose text starts to its left.
    for index, script in sorted(baselines.items(), key=lambda item: -item[1].size):
        if _in_margin(rows[index][0], column):
            continue
        left = rows[index][0].x
        hosts = [
            (abs(script.y - body.y), not _in_margin(rows[host][0], column), host)
            for host, body in baselines.items()
            if host != index
            and host not in attached
            and 0 < script.size <= body.size * _SCRIPT_SIZE_RATIO
            and abs(script.y - body.y) <= body.size * _SCRIPT_SHIFT_RATIO
            and left > bodies[host][0].x
        ]
        if hosts:
            _, _, host = min(hosts)
            rows[host].extend(rows[index])
            attached.add(index)
    return [sorted(row, key=lambda part: part.x) for i, row in enumerate(rows) if i not in attached]


def _pdf_lines(page: PageObject, page_number: int) -> list[_VisualLine]:
    rows = _pdf_rows(page)
    column = _margin_column(rows)
    rows = _attach_inline_scripts(rows, column)
    bottom, top = float(page.cropbox.bottom), float(page.cropbox.top)
    midpoint = (float(page.cropbox.left) + float(page.cropbox.right)) / 2
    outer_band = (top - bottom) * 0.1
    lines = []
    previous = 0
    for first, *rest in rows:
        number = None
        if _in_margin(first, column):
            if _POSITIVE_INTEGER.fullmatch(first.text):
                if not rest:
                    # An isolated integer can be a page number or a blank line.
                    continue
                number = int(first.text)
                if number <= previous:
                    raise ValueError(f"Page {page_number} has unordered margin line numbers.")
                previous = number
            elif first.text.isdecimal():
                raise ValueError(f"Page {page_number} has an invalid margin line number.")
        body = [first, *rest] if number is None else rest
        if not body:
            continue
        baseline = _body_baseline(body)
        y = baseline.y
        furniture = number is None and min(y - bottom, top - y) < outer_band
        tag = body[-1].text
        equation_number = (
            tag if body[-1].x >= midpoint and _EQUATION_NUMBER.fullmatch(tag) else None
        )
        lines.append(
            _VisualLine(
                page_number,
                y,
                tuple(_normalize(part.text) for part in body),
                number,
                furniture,
                equation_number,
                baseline.size,
                any(abs(part.y - y) > _BASELINE_TOLERANCE for part in body),
            )
        )
    return lines


def _pdf_anchors(reader: PdfReader) -> dict[str, tuple[int, float | None]]:
    anchors = {}
    for name, destination in reader.named_destinations.items():
        page = reader.get_destination_page_number(destination)
        if page >= 0:
            top = destination.top
            anchors[name] = page + 1, float(top) if isinstance(top, (int, float)) else None
    return anchors


def _consume_line(line: _VisualLine, expected: str, kind: str) -> str | None:
    parts = line.fragments
    if kind == "list":
        parts = (_LIST_MARKER.sub("", parts[0]), *parts[1:])
    remaining = expected
    for part in parts:
        if not remaining.startswith(part):
            return None
        # PDF fragments may split a word or omit spaces at their boundaries.
        remaining = remaining[len(part) :].lstrip()
    return remaining if remaining != expected else None


def _match_lines(
    lines: list[_VisualLine],
    start: int,
    expected: str,
    kind: str,
) -> tuple[int, ...]:
    selected = []
    for index in range(start, len(lines)):
        line = lines[index]
        remaining = _consume_line(line, expected, kind)
        if remaining is None:
            if selected and line.page_furniture:
                continue
            break
        selected.append(index)
        expected = remaining
        if not expected:
            return tuple(selected)
    return ()


def _math_chars(text: str) -> str:
    return _normalize(text).replace(" ", "").replace("\u200b", "")


@dataclass(frozen=True, slots=True)
class _EquationText:
    body: str
    number: str | None
    characters: Counter[str]

    @classmethod
    def from_record(cls, text: str) -> _EquationText:
        body, _, tail = text.rpartition("\t")
        tag = _math_chars(tail)
        if _EQUATION_NUMBER.fullmatch(tag):
            body = _math_chars(body)
            return cls(body, tag, Counter(body))
        body = _math_chars(text)
        return cls(body, None, Counter(body))


def _math_order_matches(actual: str, expected: str) -> bool:
    position = 0
    for character in actual:
        if position < len(expected) and character == expected[position]:
            position += 1
        elif character not in _MATH_DELIMITERS:
            return False
    return position == len(expected)


def _match_equation(
    lines: list[_VisualLine], start: int, expected: _EquationText
) -> tuple[int, ...]:
    if not expected.body:
        line = lines[start]
        if expected.number == line.equation_number or expected.number == _math_chars(
            "".join(line.fragments)
        ):
            return (start,)
        return ()

    observed: Counter[str] = Counter()
    found_number = expected.number is None
    for index in range(start, len(lines)):
        line = lines[index]
        if index > start:
            previous = lines[index - 1]
            if line.page != previous.page or previous.y - line.y > 2 * max(
                previous.size, line.size
            ):
                break
        parts = line.fragments
        if expected.number is not None and line.equation_number is not None:
            if found_number or line.equation_number != expected.number:
                break
            found_number = True
            parts = parts[:-1]
        text = _math_chars("".join(parts))
        observed.update(text)
        # OMML stores some delimiters in attributes that are absent from the
        # block text. Every other glyph, and any recorded delimiter, must match.
        if (observed - expected.characters).keys() - _MATH_DELIMITERS:
            break
        if found_number and expected.characters <= observed:
            # A flat equation can also be checked in left-to-right order.
            if (
                index == start
                and not line.offset_glyphs
                and not _math_order_matches(text, expected.body)
            ):
                break
            return tuple(range(start, index + 1))
    return ()


def _align_block(
    lines: list[_VisualLine],
    cursor: int,
    block: BlockRecordLike,
    anchors: dict[str, tuple[int, float | None]],
) -> tuple[int, ...]:
    expected = _normalize(block.plain_text)
    equation = _EquationText.from_record(block.plain_text) if block.kind == "display_math" else None
    start = anchors.get(getattr(block, "start_bookmark", None))
    end = anchors.get(getattr(block, "end_bookmark", None))
    if start is None:
        start = anchors.get(getattr(block, "start_linelabel", None))
    if end is None:
        end = anchors.get(getattr(block, "end_linelabel", None))
    best = ()
    best_distance = (float("inf"), float("inf"))
    for index in range(cursor, len(lines)):
        matched = (
            _match_equation(lines, index, equation)
            if equation is not None
            else _match_lines(lines, index, expected, block.kind)
        )
        if not matched:
            continue
        endpoints = ((lines[index], start), (lines[matched[-1]], end))
        distance = (
            sum(abs(line.page - anchor[0]) for line, anchor in endpoints if anchor),
            sum(
                abs(line.y - anchor[1])
                for line, anchor in endpoints
                if anchor and line.page == anchor[0] and anchor[1] is not None
            ),
        )
        if distance == (0, 0):
            return matched
        if distance < best_distance:
            best, best_distance = matched, distance
    return best


def resolve_blocks_from_pdf(
    pdf_path: Path, blocks: Sequence[BlockRecordLike], *, deliverable: str
) -> dict[str, LineSpan]:
    """Match compiler records to visual lines and keep independently numbered spans."""
    try:
        with pdf_path.open("rb") as stream:
            reader = PdfReader(stream)
            lines = [
                line
                for number, page in enumerate(reader.pages, 1)
                for line in _pdf_lines(page, number)
            ]
            anchors = _pdf_anchors(reader)
        if not any(line.number is not None for line in lines):
            raise ValueError("No page-margin line numbers were found.")
    except Exception as exc:
        # pypdf also raises built-in exceptions for some malformed PDF objects.
        raise OmmError("ERR_LAYOUT_PDF_INVALID", deliverable=deliverable, detail=str(exc)) from exc

    spans = {}
    cursor = 0
    for block in blocks:
        if not _normalize(block.plain_text):
            continue
        indices = _align_block(lines, cursor, block, anchors)
        if not indices:
            raise OmmError(
                "ERR_LAYOUT_PDF_INVALID",
                deliverable=deliverable,
                detail=f"Could not match the text of block {block.block_id}.",
            )
        cursor = indices[-1] + 1
        segments = []
        for page, group in groupby((lines[i] for i in indices), key=lambda line: line.page):
            numbers = [line.number for line in group]
            if block.kind == "display_math":
                # Stacked glyph rows can belong to one numbered equation.
                numbers = [number for number in numbers if number is not None]
            if not numbers or any(number is None for number in numbers):
                break
            segments.append((page, numbers[0], numbers[-1]))
        else:
            spans[block.block_id] = _span(tuple(segments))
    return spans


_AUX_LABEL = re.compile(
    r"^\s*\\newlabel\{([^{}\n]+)\}\{\{([1-9][0-9]*)\}\{([1-9][0-9]*)\}[^\n]*\}\s*$",
    re.MULTILINE,
)
_AUX_PAGE_LINE = re.compile(r"^\\@LN\{([1-9][0-9]*)\}\{([0-9]+)\}", re.MULTILINE)


def _aux_span(
    start: tuple[int, int], end: tuple[int, int], page_lines: Mapping[int, set[int]]
) -> LineSpan | None:
    first, first_line = start
    last, last_line = end
    if start > end:
        return None
    if first == last:
        return _span(((first, first_line, last_line),))

    # Endpoint labels alone do not give the last or first printed line on
    # either side of a page break. Use lineno's per-line records for those.
    if first_line not in page_lines.get(first, ()) or last_line not in page_lines.get(last, ()):
        return None
    segments = []
    for page, numbers in sorted(page_lines.items()):
        if first <= page <= last:
            low = first_line if page == first else min(numbers)
            high = last_line if page == last else max(numbers)
            segments.append((page, low, high))
    return _span(tuple(segments))


def resolve_blocks_from_tex_aux(
    aux_path: Path, blocks: Sequence[BlockRecordLike], *, deliverable: str
) -> dict[str, LineSpan]:
    """Read lineno labels and omit blocks without a provable physical span."""
    try:
        contents = aux_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise OmmError(
            "ERR_LOCATION_LAYOUT_REQUIRED", deliverable=deliverable, clean_file=aux_path.name
        ) from exc

    labels = defaultdict(set)
    for label, line, page in _AUX_LABEL.findall(contents):
        labels[label].add((int(page), int(line)))
    page_lines: dict[int, set[int]] = defaultdict(set)
    for line, page in _AUX_PAGE_LINE.findall(contents):
        # lineno's true-page counter is zero-based.
        page_lines[int(page) + 1].add(int(line))

    spans = {}
    for block in blocks:
        starts = labels.get(getattr(block, "start_linelabel", None), ())
        ends = labels.get(getattr(block, "end_linelabel", None), ())
        if len(starts) == len(ends) == 1 and (
            span := _aux_span(next(iter(starts)), next(iter(ends)), page_lines)
        ):
            spans[block.block_id] = span
    return spans


def _find_engine(
    override: str | Path | None,
    names: tuple[str, ...],
    deliverable: str,
    clean_path: Path,
) -> str:
    candidates = (os.fspath(override),) if override is not None else names
    for candidate in candidates:
        if executable := shutil.which(candidate):
            # TeX launchers select a format by the executable name, often a symlink.
            return os.path.abspath(executable)
    raise OmmError(
        "ERR_LOCATION_LAYOUT_REQUIRED", deliverable=deliverable, clean_file=clean_path.name
    )


def _run_headless(command: list[str], work_dir: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        command,
        cwd=work_dir,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def paginate_docx_headless(
    clean_docx: Path,
    out_pdf: Path,
    *,
    deliverable: str,
    soffice_bin: str | Path | None = None,
) -> Path:
    """Export with a private LibreOffice profile and named bookmark destinations."""
    names = (
        ("soffice.com", "soffice", "libreoffice")
        if os.name == "nt"
        else ("soffice", "libreoffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice")
    )
    executable = _find_engine(soffice_bin, names, deliverable, clean_docx)
    options = (
        'pdf:writer_pdf_Export:{"ExportBookmarks":{"type":"boolean","value":"true"},'
        '"ExportBookmarksToPDFDestination":{"type":"boolean","value":"true"}}'
    )
    try:
        with TemporaryDirectory(prefix="omm-docx-layout-") as directory:
            work_dir = Path(directory).resolve()
            _run_headless(
                [
                    executable,
                    f"-env:UserInstallation={(work_dir / 'profile').as_uri()}",
                    "--headless",
                    "--convert-to",
                    options,
                    "--outdir",
                    str(work_dir),
                    str(clean_docx.resolve()),
                ],
                work_dir,
            )
            generated = work_dir / f"{clean_docx.stem}.pdf"
            out_pdf.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(generated, out_pdf)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise OmmError("ERR_LAYOUT_PDF_INVALID", deliverable=deliverable, detail=str(exc)) from exc
    return out_pdf


# In running mode lineno normally writes only the explicitly requested labels.
# Log the same counter on each numbered line so a cross-page block has real
# page boundaries in the aux file. In pagewise mode lineno already logs lines.
_TEX_LAYOUT_INPUT = r"""\makeatletter
\AddToHook{package/lineno/after}{%
  \let\omm@makeLineNumberRunning\makeLineNumberRunning
  \def\makeLineNumberRunning{\logtheLineNumber\omm@makeLineNumberRunning}%
}
\makeatother
\input{source/document.tex}
"""


def paginate_latex_headless(
    clean_tex: Path,
    work_dir: Path,
    *,
    deliverable: str,
    tex_bin: str | Path | None = None,
) -> Path:
    """Run TeX twice in a fresh directory and publish its auxiliary file."""
    executable = _find_engine(tex_bin, ("xelatex", "pdflatex", "lualatex"), deliverable, clean_tex)
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="omm-tex-layout-", dir=work_dir) as directory:
            run_dir = Path(directory).resolve()
            source_dir = run_dir / "source"
            source_dir.mkdir()
            tex_cache = run_dir / "texmf"
            tex_cache.mkdir()
            shutil.copyfile(clean_tex, source_dir / "document.tex")
            entry = run_dir / clean_tex.name
            entry.write_text(_TEX_LAYOUT_INPUT, encoding="ascii")
            tex_inputs = os.pathsep.join(
                (str(clean_tex.resolve().parent), os.environ.get("TEXINPUTS", ""))
            )
            env = {
                **os.environ,
                "TEXINPUTS": tex_inputs,
                "TEXMFVAR": str(tex_cache),
                "TEXMFCACHE": str(tex_cache),
                # MiKTeX otherwise offers to install missing packages in a dialog.
                "MIKTEX_MPM_AUTOINSTALL": "0",
                "MIKTEX_MIKTEX_MPM_AUTOINSTALL": "0",
            }
            command = [
                executable,
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-no-shell-escape",
                f"./{entry.name}",
            ]
            for _ in range(2):
                _run_headless(command, run_dir, env)
            aux_path = (work_dir / clean_tex.name).with_suffix(".aux")
            shutil.copyfile(entry.with_suffix(".aux"), aux_path)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise OmmError(
            "ERR_LOCATION_LAYOUT_REQUIRED",
            deliverable=deliverable,
            clean_file=clean_tex.name,
            detail=str(exc),
        ) from exc
    return aux_path


def _merge_spans(spans: Sequence[LineSpan]) -> LineSpan:
    segments = []
    pages = groupby(
        (segment for span in spans for segment in span.page_segments),
        key=lambda segment: segment[0],
    )
    for page, page_segments in pages:
        values = list(page_segments)
        segments.append((page, values[0][1], values[-1][2]))
    return _span(tuple(segments))


def resolve_workspace_locations(
    snapshot: RevisionSnapshot,
    numbered: NumberedWorkspace,
    *,
    target: Literal["docx", "latex"],
    compiled_deliverables: Mapping[str, Any],
    layout_pdfs: Mapping[str, Path] | None = None,
) -> dict[tuple[str, str | None], str]:
    """Resolve each actual location reference, paginating each target only once."""
    pdfs = layout_pdfs or {}
    for name in pdfs:
        if name not in snapshot.workspace.deliverables:
            raise OmmError(
                "ERR_LAYOUT_PDF_INVALID", deliverable=name, detail="Unknown deliverable."
            )
    keys = dict.fromkeys(
        (node.start_id, node.end_id)
        for document in snapshot.documents.values()
        for block in ast.walk_blocks(document.blocks)
        if not isinstance(block, ast.OutlineBlock)
        for node in ast.walk_inlines(block.inlines)
        if isinstance(node, ast.LocRef)
    )

    labels = {}
    layouts: dict[str, dict[str, LineSpan]] = {}
    for key in keys:
        location = numbered.locations.get(key)
        if location is None or location.deliverable not in compiled_deliverables:
            raise OmmError("ERR_LOCATION_UNAVAILABLE", target=key[0])
        name = location.deliverable
        compiled = compiled_deliverables[name]
        if name not in layouts:
            if name in pdfs:
                spans = resolve_blocks_from_pdf(pdfs[name], compiled.blocks, deliverable=name)
            else:
                with TemporaryDirectory(prefix="omm-location-") as directory:
                    work_dir = Path(directory)
                    if target == "docx":
                        pdf = paginate_docx_headless(
                            compiled.clean_path, work_dir / "layout.pdf", deliverable=name
                        )
                        spans = resolve_blocks_from_pdf(pdf, compiled.blocks, deliverable=name)
                    else:
                        aux = paginate_latex_headless(
                            compiled.clean_path, work_dir, deliverable=name
                        )
                        spans = resolve_blocks_from_tex_aux(aux, compiled.blocks, deliverable=name)
            layouts[name] = spans
        recorded = {block.block_id for block in compiled.blocks}
        selected = []
        for block_id in location.block_ids:
            # Numbering includes figures and tables inside a range. Compiler
            # records enumerate the blocks that can carry independent line numbers.
            if block_id not in recorded and block_id not in key:
                continue
            span = layouts[name].get(block_id)
            if span is None:
                raise OmmError("ERR_LOCATION_UNAVAILABLE", target=block_id)
            selected.append(span)
        if not selected:
            raise OmmError("ERR_LOCATION_UNAVAILABLE", target=key[0])
        labels[key] = format_location_label(compiled.clean_path.name, _merge_spans(selected))
    return labels
