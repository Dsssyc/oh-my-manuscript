"""Deterministic pure-Python LaTeX target compiler (ADR-001).

Compiles a validated :class:`~omm.git_revs.RevisionSnapshot` into one Clean
``.tex`` document per deliverable and, when a milestone snapshot is supplied,
into a paired ``latexdiff``-compatible Marked ``.tex`` with native revision
macros.  Referenced figure assets are materialized from the revision snapshot
(never from the dirty working tree), so identical inputs always produce
byte-identical output on every platform.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path

from omm import ast
from omm.errors import OmmError
from omm.git_revs import RevisionSnapshot
from omm.numbering import BlockNumber, NumberedReference, NumberedWorkspace, number_workspace
from omm.omml import _NUMBERED_ENVS, _TAG_RE
from omm.parser import _markdown
from omm.workspace import DeliverableKind, Style, Workspace, atomic_write_text

__all__ = [
    "CompiledLatexDeliverable",
    "LatexBlockRecord",
    "compile_latex_workspace",
]

_LINK_COLOR_COMMAND = "OmmLinkColor"
_BIB_LABEL_PREFIX = "omm:bib:"

_TEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "&": r"\&",
    "#": r"\#",
    "_": r"\_",
    "%": r"\%",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
# Inside \href destinations a literal ~ must survive verbatim.
_HREF_SPECIALS = dict(_TEX_SPECIALS, **{"~": r"\string~"})

_HEADING_COMMANDS = {1: "section", 2: "subsection", 3: "subsubsection", 4: "paragraph"}
_ALIGN_LETTERS: dict[str | None, str] = {"left": "l", "center": "c", "right": "r", None: "l"}
_ENV_RES = tuple(
    (env, re.compile(rf"\\begin\s*\{{\s*{re.escape(env)}\s*\}}")) for env in _NUMBERED_ENVS
)
_WORD_RE = re.compile(r"\s+|[^\s]+")
_CJK_NAMES = (
    "CJK ",
    "IDEOGRAPHIC ",
    "HIRAGANA ",
    "KATAKANA ",
    "KATAKANA-",
    "HALFWIDTH KATAKANA ",
    "HANGUL ",
    "HALFWIDTH HANGUL ",
    "BOPOMOFO ",
)
# ListBlock inlines are flattened across items, so item starts are recovered
# from the marker lines of the block's own source span (shared base indent).
_LIST_ITEM = re.compile(r"[ \t]*(?:[-+*]|\d{1,9}[.)])(?:\s+|$)")

_DIF_PREAMBLE = """%DIF latexdiff revision macros (ulem + xcolor)
\\providecommand{\\DIFadd}[1]{{\\protect\\color{blue}\\uwave{#1}}}
\\providecommand{\\DIFdel}[1]{{\\protect\\color{red}\\sout{#1}}}
%DIF SAFE PREAMBLE
\\providecommand{\\DIFaddbegin}{}
\\providecommand{\\DIFaddend}{}
\\providecommand{\\DIFdelbegin}{}
\\providecommand{\\DIFdelend}{}
\\providecommand{\\DIFaddFL}[1]{\\DIFadd{#1}}
\\providecommand{\\DIFdelFL}[1]{\\DIFdel{#1}}
\\providecommand{\\DIFaddbeginFL}{}
\\providecommand{\\DIFaddendFL}{}
\\providecommand{\\DIFdelbeginFL}{}
\\providecommand{\\DIFdelendFL}{}
%DIF END PREAMBLE"""

# CommonMark parser without the manuscript extensions, used to re-read literal
# content (fenced or indented code) of MarkdownBlocks.
_PLAIN_MARKDOWN = _markdown()


@dataclass(frozen=True, slots=True)
class LatexBlockRecord:
    """One independently line-numbered addressable block of a Clean deliverable."""

    block_id: str
    kind: str
    label_name: str
    start_linelabel: str | None
    end_linelabel: str | None
    plain_text: str


@dataclass(frozen=True, slots=True)
class CompiledLatexDeliverable:
    """Compiled Clean (and optional Marked) ``.tex`` outputs of one deliverable."""

    name: str
    kind: DeliverableKind
    clean_path: Path
    marked_path: Path | None
    asset_paths: tuple[Path, ...]
    blocks: tuple[LatexBlockRecord, ...]


@dataclass(frozen=True, slots=True)
class _Tok:
    """One rendered LaTeX token; ``structural`` tokens never enter DIF marks."""

    text: str
    structural: bool = False
    atom: bool = False


@dataclass
class _Ctx:
    """Rendering context for one deliverable of one revision."""

    snapshot: RevisionSnapshot
    numbered: NumberedWorkspace
    name: str
    kind: DeliverableKind
    lineno: bool
    record: bool
    loc_labels: Mapping[tuple[str, str | None], str]
    local_blocks: frozenset[str] = frozenset()
    local_citations: frozenset[str] = frozenset()
    asset_alias: Mapping[str, str] = field(default_factory=dict)
    document: ast.Document | None = None
    records: list[LatexBlockRecord] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Escaping and text tokens
# ---------------------------------------------------------------------------


def _escape(value: str) -> str:
    return "".join(_TEX_SPECIALS.get(char, char) for char in value)


def _escape_href(value: str) -> str:
    return "".join(_HREF_SPECIALS.get(char, char) for char in value)


def _ref_text(label: str) -> str:
    """Escape a logical label and glue label and number with ties (``Fig.~1``)."""
    return _escape(label).replace(" ", "~")


def _is_cjk(char: str) -> bool:
    return unicodedata.name(char, "").startswith(_CJK_NAMES)


def _text_tokens(value: str) -> list[_Tok]:
    """Split escaped plain text into word tokens; CJK text splits per character."""
    tokens: list[_Tok] = []
    for match in _WORD_RE.finditer(value):
        chunk = match[0]
        if chunk.isspace():
            tokens.append(_Tok(" "))
            continue
        start = 0
        for index, char in enumerate(chunk):
            if _is_cjk(char):
                if index > start:
                    tokens.append(_Tok(chunk[start:index]))
                tokens.append(_Tok(char))
                start = index + 1
        if start < len(chunk):
            tokens.append(_Tok(chunk[start:]))
    return tokens


def _join(tokens: Sequence[_Tok]) -> str:
    return "".join(token.text for token in tokens)


# ---------------------------------------------------------------------------
# Math boundary validation
# ---------------------------------------------------------------------------


def _validate_math(snapshot: RevisionSnapshot) -> None:
    """Reject manual numbering once at the compile boundary (ADR-001 2.5)."""
    for document in snapshot.documents.values():
        for block in ast.walk_blocks(document.blocks):
            for node in ast.walk_inlines(block.inlines):
                if isinstance(node, ast.InlineMath):
                    _check_math(node.latex, document.path, block)
            if isinstance(block, ast.DisplayMath):
                _check_math(block.latex, document.path, block)


def _check_math(latex: str, path: Path | None, block: ast.Block) -> None:
    if _TAG_RE.search(latex):
        raise OmmError(
            "ERR_MATH_TAG_FORBIDDEN", path=path, line=block.line, block_id=block.block_id
        )
    for env, pattern in _ENV_RES:
        if pattern.search(latex):
            raise OmmError(
                "ERR_MATH_UNSUPPORTED_ENV", path=path, line=block.line, block_id=block.block_id
            )


# ---------------------------------------------------------------------------
# Inline rendering
# ---------------------------------------------------------------------------


def _block_number(ctx: _Ctx, target: str) -> BlockNumber | None:
    number = ctx.numbered.block_numbers[ctx.name].get(target)
    if number is None:
        for name in ctx.snapshot.workspace.deliverables:
            if (found := ctx.numbered.block_numbers[name].get(target)) is not None:
                return found
    return number


def _citation_parts(numbered: NumberedWorkspace, citation: ast.Citation) -> list[str]:
    """Per-key label texts, byte-identical to ``NumberedWorkspace.citation_label``."""
    parts = []
    for key in citation.keys:
        entry = numbered.references.get(key)
        if entry is None:
            parts.append(f"@{key}")
        elif numbered.citation_style == "numeric":
            parts.append(str(entry.number) if entry.number is not None else f"@{key}")
        elif citation.parenthetical:
            parts.append(f"{entry.short_label}, {entry.year}")
        else:
            parts.append(f"{entry.short_label} ({entry.year})")
    return parts


def _render_inlines(
    ctx: _Ctx, nodes: tuple[ast.Inline, ...], *, quoted_source: str | None = None
) -> list[_Tok]:
    tokens: list[_Tok] = []
    for node in nodes:
        if quoted_source is not None and isinstance(
            node, (ast.BlockRef, ast.LocRef, ast.Citation)
        ):
            # Reviewer comments are quoted verbatim: their references are not
            # validated or counted, so no link anchors exist for them.
            tokens.append(_Tok(_escape(quoted_source[node.span.start : node.span.end])))
            continue
        match node:
            case ast.Text(value=value):
                tokens.extend(_text_tokens(_escape(value)))
            case ast.LineBreak(hard=hard):
                if hard:
                    tokens.append(_Tok(r"\\", structural=True))
                else:
                    tokens.append(_Tok(" "))
            case ast.CodeSpan(value=value):
                tokens.append(_Tok(f"\\texttt{{{_escape(value)}}}", atom=True))
            case ast.InlineMath(latex=latex):
                tokens.append(_Tok(f"${latex}$", atom=True))
            case ast.Strong():
                tokens.extend(_wrapped(ctx, node, "\\textbf", quoted_source))
            case ast.Emphasis():
                tokens.extend(_wrapped(ctx, node, "\\emph", quoted_source))
            case ast.Subscript():
                tokens.extend(_wrapped(ctx, node, "\\textsubscript", quoted_source))
            case ast.Superscript():
                tokens.extend(_wrapped(ctx, node, "\\textsuperscript", quoted_source))
            case ast.Link(destination=destination):
                inner = _join(_render_inlines(ctx, node.children, quoted_source=quoted_source))
                tokens.append(
                    _Tok(f"\\href{{{_escape_href(destination)}}}{{{inner}}}", atom=True)
                )
            case ast.Image(destination=destination):
                tokens.append(_Tok(f"\\includegraphics{{{_escape(destination)}}}", atom=True))
            case ast.BlockRef(block_id=target):
                number = _block_number(ctx, target)
                label = _ref_text(number.label if number else target)
                if target in ctx.local_blocks:
                    tokens.append(_Tok(f"\\hyperref[omm:{target}]{{{label}}}", atom=True))
                else:
                    tokens.append(_Tok(_colored(label), atom=True))
            case ast.Citation():
                tokens.extend(_citation_tokens(ctx, node))
            case ast.LocRef():
                tokens.append(_Tok(_locus(ctx, node), atom=True))
    return tokens


def _colored(text: str) -> str:
    return f"\\textcolor{{{_LINK_COLOR_COMMAND}}}{{{text}}}"


def _wrapped(
    ctx: _Ctx, node: ast.InlineContainer, command: str, quoted_source: str | None = None
) -> list[_Tok]:
    return [
        _Tok(f"{command}{{", structural=True),
        *_render_inlines(ctx, node.children, quoted_source=quoted_source),
        _Tok("}", structural=True),
    ]


def _citation_tokens(ctx: _Ctx, citation: ast.Citation) -> list[_Tok]:
    numbered = ctx.numbered
    texts = [_escape(text) for text in _citation_parts(numbered, citation)]
    if numbered.citation_style == "numeric":
        opener, separator, closer = "[", ", ", "]"
    elif citation.parenthetical:
        opener, separator, closer = "(", "; ", ")"
    else:
        opener = closer = ""
        separator = "; "
    tokens: list[_Tok] = []
    if opener:
        tokens.append(_Tok(opener))
    for index, (key, text) in enumerate(zip(citation.keys, texts)):
        if index:
            tokens.append(_Tok(separator))
        if key in ctx.local_citations:
            tokens.append(_Tok(f"\\hyperref[{_BIB_LABEL_PREFIX}{key}]{{{text}}}", atom=True))
        else:
            # The entry lives in another deliverable's bibliography.
            tokens.append(_Tok(_colored(text), atom=True))
    if closer:
        tokens.append(_Tok(closer))
    return tokens


def _locus(ctx: _Ctx, node: ast.LocRef) -> str:
    key = (node.start_id, node.end_id)
    text = ctx.loc_labels.get(key) or ctx.numbered.locations[key].label(node).lstrip()
    return _colored(_escape(text))


def _plain(ctx: _Ctx, nodes: tuple[ast.Inline, ...]) -> str:
    parts: list[str] = []
    for node in nodes:
        match node:
            case ast.Text(value=value) | ast.CodeSpan(value=value):
                parts.append(value)
            case ast.LineBreak():
                parts.append(" ")
            case ast.InlineMath(latex=latex):
                parts.append(latex)
            case ast.Link() | ast.Strong() | ast.Emphasis() | ast.Subscript() | ast.Superscript():
                parts.append(_plain(ctx, node.children))
            case ast.Image(alt=alt):
                parts.append(alt)
            case ast.BlockRef(block_id=target):
                number = _block_number(ctx, target)
                parts.append(number.label if number else target)
            case ast.Citation():
                parts.append(ctx.numbered.citation_label(node))
            case ast.LocRef():
                label = ctx.numbered.locations[(node.start_id, node.end_id)].label(node)
                parts.append(label.lstrip())
    return " ".join("".join(parts).split())


# ---------------------------------------------------------------------------
# Diff marking of rendered token streams
# ---------------------------------------------------------------------------


def _marked(tokens: Sequence[_Tok], *, deleted: bool, float_mode: bool) -> str:
    if not tokens:
        return ""
    begin = "\\DIFdelbegin" if deleted else "\\DIFaddbegin"
    command = "\\DIFdel" if deleted else "\\DIFadd"
    if float_mode:
        begin += "FL"
        command += "FL"
    end = begin.replace("begin", "end")
    pieces: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            pieces.append(f"{command}{{{''.join(run)}}}")
            run.clear()

    for token in tokens:
        if token.structural:
            flush()
            pieces.append(token.text)
        else:
            run.append(f"\\mbox{{{token.text}}}" if token.atom else token.text)
    flush()
    return f"{begin} " + "".join(pieces) + end


def _diff_tokens(old: Sequence[_Tok], new: Sequence[_Tok], *, float_mode: bool) -> str:
    parts: list[str] = []
    for op, i, j, k, last in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if op == "equal":
            parts.extend(token.text for token in old[i:j])
            continue
        if op in {"delete", "replace"}:
            parts.append(_marked(old[i:j], deleted=True, float_mode=float_mode))
        if op in {"insert", "replace"}:
            parts.append(_marked(new[k:last], deleted=False, float_mode=float_mode))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Block rendering (Clean and Marked live content)
# ---------------------------------------------------------------------------


def _line_marks(ctx: _Ctx, block_id: str | None) -> tuple[str, str]:
    if ctx.lineno and block_id:
        return f"\\linelabel{{omm:ls:{block_id}}}", f"\\linelabel{{omm:le:{block_id}}}"
    return "", ""


def _block_label(ctx: _Ctx, block_id: str | None) -> str:
    if ctx.lineno and block_id:
        return f"\\label{{omm:{block_id}}}"
    return ""


def _record(ctx: _Ctx, block: ast.Block, kind: str, plain: str) -> None:
    block_id = block.block_id
    if ctx.record and block_id:
        ctx.records.append(
            LatexBlockRecord(
                block_id,
                kind,
                f"omm:{block_id}",
                f"omm:ls:{block_id}",
                f"omm:le:{block_id}",
                plain,
            )
        )


def _render_block(ctx: _Ctx, block: ast.Block, *, deleted: bool = False) -> str:
    if isinstance(block, ast.OutlineBlock):
        return ""
    match block:
        case ast.Heading():
            return _render_heading(ctx, block, deleted=deleted)
        case ast.Paragraph():
            return _render_paragraph(ctx, block, deleted=deleted)
        case ast.ListBlock():
            return _render_list(ctx, block, deleted=deleted)
        case ast.DisplayMath():
            return _render_equation(ctx, block, deleted=deleted)
        case ast.FigureBlock():
            return _render_figure(ctx, block, deleted=deleted)
        case ast.TableBlock():
            return _render_table(ctx, block, deleted=deleted)
        case ast.ReviewItem():
            return _render_review_item(ctx, block)
        case ast.MarkdownBlock():
            return _render_markdown(ctx, block, deleted=deleted)
    raise AssertionError(f"unhandled block type {type(block).__name__}")


def _render_heading(
    ctx: _Ctx, block: ast.Heading, *, deleted: bool, title: str | None = None
) -> str:
    command = _HEADING_COMMANDS.get(block.level, "subparagraph")
    if title is None:
        title = _join(_render_inlines(ctx, block.inlines))
    # Deleted headings use starred forms so they never advance section counters.
    if deleted or ctx.kind == "response_letter":
        return f"\\{command}*{{{title}}}"
    _record(ctx, block, "heading", _plain(ctx, block.inlines))
    start, end = _line_marks(ctx, block.block_id)
    return f"{start}\\{command}{{{title}}}{_block_label(ctx, block.block_id)}{end}"


def _render_paragraph(
    ctx: _Ctx, block: ast.Paragraph, *, deleted: bool, content: str | None = None
) -> str:
    if deleted:
        return _diff_tokens(_render_inlines(ctx, block.inlines), (), float_mode=False)
    body = content if content is not None else _join(_render_inlines(ctx, block.inlines))
    _record(ctx, block, "paragraph", _plain(ctx, block.inlines))
    start, end = _line_marks(ctx, block.block_id)
    return f"{start}{_block_label(ctx, block.block_id)}{body}{end}"


def _list_item_starts(document: ast.Document, block: ast.ListBlock) -> list[int]:
    """Item content starts, relative to ``block.source_span.start`` like the
    parser's inline spans."""
    raw = document.source[block.source_span.start : block.source_span.end]
    base = raw[: len(raw) - len(raw.lstrip(" \t"))]
    starts: list[int] = []
    offset = 0
    for line in raw.split("\n"):
        if line.startswith(base) and (match := _LIST_ITEM.match(line, len(base))):
            starts.append(offset + match.end())
        offset += len(line) + 1
    return starts


def _item_nodes(ctx: _Ctx, block: ast.ListBlock) -> list[tuple[ast.Inline, ...]]:
    """Inline node groups per list item.

    The AST flattens item inlines into one stream, so items are re-grouped by
    the marker lines of the block's source span.  Literal list content (fenced
    code) carries no inline nodes and yields no item.
    """
    assert ctx.document is not None
    starts = _list_item_starts(ctx.document, block)
    buckets: list[list[ast.Inline]] = [[] for _ in starts]
    for node in block.inlines:
        index = max(bisect_right(starts, node.span.start) - 1, 0)
        buckets[index].append(node)
    return [tuple(nodes) for nodes in buckets if nodes]


def _item_tokens(ctx: _Ctx, block: ast.ListBlock) -> list[list[_Tok]]:
    return [_render_inlines(ctx, nodes) for nodes in _item_nodes(ctx, block)]


def _render_list(
    ctx: _Ctx, block: ast.ListBlock, *, deleted: bool, items: list[str] | None = None
) -> str:
    environment = "enumerate" if block.ordered else "itemize"
    item_tokens = _item_tokens(ctx, block)
    if deleted:
        body = "\n".join(
            f"\\item {_diff_tokens(tokens, (), float_mode=False)}" for tokens in item_tokens
        )
        rendered = f"\\begin{{{environment}}}\n{body}\n\\end{{{environment}}}"
        return rendered
    if items is None:
        items = [_join(tokens) for tokens in item_tokens]
    body = "\n".join(f"\\item {item}" for item in items)
    rendered = f"\\begin{{{environment}}}\n{body}\n\\end{{{environment}}}"
    _record(ctx, block, "list", " ".join(_plain(ctx, nodes) for nodes in _item_nodes(ctx, block)))
    start, end = _line_marks(ctx, block.block_id)
    return f"{start}{_block_label(ctx, block.block_id)}{rendered}{end}"


def _render_equation(ctx: _Ctx, block: ast.DisplayMath, *, deleted: bool) -> str:
    latex = block.latex.strip()
    # Deleted equations use the star form so they never advance the counter;
    # _render_deleted supplies the surrounding DIFdelbegin/DIFdelend markers.
    if deleted:
        return f"\\[{latex}\\]"
    _record(ctx, block, "display_math", " ".join(latex.split()))
    start, end = _line_marks(ctx, block.block_id)
    if block.unnumbered:
        return f"{start}{_block_label(ctx, block.block_id)}\\[{latex}\\]{end}"
    label = _block_label(ctx, block.block_id)
    body = f"\\begin{{equation}}{label} {latex} \\end{{equation}}"
    return f"{start}{body}{end}"


def _graphics_path(ctx: _Ctx, asset_path: str) -> str:
    return _escape(ctx.asset_alias.get(asset_path, asset_path))


def _render_figure(
    ctx: _Ctx, block: ast.FigureBlock, *, deleted: bool, caption: str | None = None
) -> str:
    if caption is None:
        caption = _join(_render_inlines(ctx, block.caption.inlines))
    caption_command = "\\caption*" if deleted else "\\caption"
    figure_label = "" if deleted else _block_label(ctx, block.block_id)
    lines = ["\\begin{figure}[htbp]", "\\centering"]
    panels = block.subfigures
    if len(panels) == 1:
        lines.append(
            f"\\includegraphics[width=\\linewidth]{{{_graphics_path(ctx, panels[0].asset_path)}}}"
        )
    else:
        columns = block.cols or len(panels)
        width = f"{1 / columns:.3f}"
        rows = [panels[index : index + columns] for index in range(0, len(panels), columns)]
        lines.extend(_figure_rows(ctx, rows, width, deleted=deleted))
    lines.append(f"{caption_command}{{{caption}}}{figure_label}")
    lines.append("\\end{figure}")
    return "\n".join(lines)


def _figure_rows(
    ctx: _Ctx, rows: Sequence[Sequence[ast.Subfigure]], width: str, *, deleted: bool
) -> list[str]:
    lines: list[str] = []
    for index, row in enumerate(rows):
        for position, panel in enumerate(row):
            command = "\\caption*" if deleted else "\\caption"
            label = "" if deleted else _block_label(ctx, panel.block_id)
            lines.append(f"\\begin{{subfigure}}[t]{{{width}\\textwidth}}")
            lines.append("\\centering")
            lines.append(
                f"\\includegraphics[width=\\linewidth]{{{_graphics_path(ctx, panel.asset_path)}}}"
            )
            lines.append(f"{command}{{{_escape(panel.alt)}}}{label}")
            closing = "\\end{subfigure}\\hfill%" if position < len(row) - 1 else "\\end{subfigure}"
            lines.append(closing)
        if index < len(rows) - 1:
            lines.append("\\par\\bigskip")
    return lines


def _cmidrules(header: tuple[tuple[ast.TableCell, ...], ...]) -> dict[int, str]:
    rules: dict[int, list[str]] = defaultdict(list)
    for y, row in enumerate(header):
        for x, cell in enumerate(row):
            if cell.covered or cell.col_span <= 1:
                continue
            bottom = y + cell.row_span - 1
            rules[bottom].append(f"\\cmidrule(lr){{{x + 1}-{x + cell.col_span}}}")
    return {y: " ".join(group) for y, group in rules.items()}


def _table_cell(
    cell: ast.TableCell, tokens: Sequence[_Tok] | None, alignment: str
) -> str:
    if cell.covered or tokens is None:
        return ""
    content = _join(tokens)
    if cell.row_span > 1:
        content = f"\\multirow{{{cell.row_span}}}{{*}}{{{content}}}"
    if cell.col_span > 1:
        content = f"\\multicolumn{{{cell.col_span}}}{{{alignment}}}{{{content}}}"
    return content


def _grid_tokens(
    ctx: _Ctx, grid: tuple[tuple[ast.TableCell, ...], ...]
) -> list[list[list[_Tok] | None]]:
    return [
        [None if cell.covered else _render_inlines(ctx, cell.inlines) for cell in row]
        for row in grid
    ]


def _render_table(
    ctx: _Ctx,
    block: ast.TableBlock,
    *,
    deleted: bool,
    caption: str | None = None,
    header: list[list[list[_Tok] | None]] | None = None,
    data: list[list[list[_Tok] | None]] | None = None,
    notes: list[str] | None = None,
) -> str:
    alignment = block.alignment
    if caption is None:
        caption = _join(_render_inlines(ctx, block.caption.inlines))
    if header is None:
        header = _grid_tokens(ctx, block.header)
    if data is None:
        data = _grid_tokens(ctx, block.rows)
    if notes is None:
        notes = [_join(_render_inlines(ctx, note.inlines)) for note in block.notes]
    caption_command = "\\caption*" if deleted else "\\caption"
    label = "" if deleted else _block_label(ctx, block.block_id)
    spec = "".join(_ALIGN_LETTERS[column] for column in alignment)
    lines = [
        "\\begin{table}[htbp]",
        "\\centering",
        f"{caption_command}{{{caption}}}{label}",
        f"\\begin{{tabular}}{{{spec}}}",
        "\\toprule",
    ]
    rules = _cmidrules(block.header)
    for y, row in enumerate(block.header):
        filled: set[int] = set()
        cells = []
        for x, cell in enumerate(row):
            if cell.covered and x in filled:
                # A \multicolumn already owns this column slot in the row.
                continue
            cells.append(_table_cell(cell, header[y][x], _ALIGN_LETTERS[alignment[x]]))
            filled.update(range(x, x + cell.col_span))
        lines.append(" & ".join(cells) + r" \\")
        if y in rules:
            lines.append(rules[y])
    lines.append("\\midrule")
    for y, row in enumerate(block.rows):
        filled = set()
        cells = []
        for x, cell in enumerate(row):
            if cell.covered and x in filled:
                continue
            cells.append(_table_cell(cell, data[y][x], _ALIGN_LETTERS[alignment[x]]))
            filled.update(range(x, x + cell.col_span))
        lines.append(" & ".join(cells) + r" \\")
        if y in block.midrule_after:
            lines.append("\\midrule")
    lines.extend(("\\bottomrule", "\\end{tabular}"))
    for note in notes:
        lines.append(f"\\par\\noindent{{\\small {note}}}")
    lines.append("\\end{table}")
    return "\n".join(lines)


def _render_review_item(ctx: _Ctx, block: ast.ReviewItem) -> str:
    comment = _join(
        _render_inlines(ctx, block.comment.inlines, quoted_source=block.clean_source)
    )
    parts = [
        f"\\subsection*{{{_escape(block.reviewer)}}}",
        "\\begin{quote}",
        (
            "\\noindent\\colorbox{gray!15}{"
            f"\\parbox{{\\dimexpr\\linewidth-2\\fboxsep\\relax}}{{\\itshape {comment}}}}}"
        ),
        "\\end{quote}",
    ]
    parts.extend(_render_block(ctx, child) for child in block.reply)
    return "\n".join(part for part in parts if part)


def _code_block(block: ast.MarkdownBlock) -> str:
    tokens = _PLAIN_MARKDOWN.parse(block.clean_source, {})
    content = "\n".join(
        token.content.rstrip("\n")
        for token in tokens
        if token.type in {"fence", "code_block"}
    )
    return f"\\begin{{verbatim}}\n{content}\n\\end{{verbatim}}"


def _render_markdown(ctx: _Ctx, block: ast.MarkdownBlock, *, deleted: bool) -> str:
    if block.kind in {"fence", "code_block"}:
        return _code_block(block)
    if block.kind == "hr":
        return "\\noindent\\rule{\\linewidth}{0.4pt}"
    rendered = (_render_block(ctx, child, deleted=deleted) for child in block.children)
    inner = "\n\n".join(part for part in rendered if part)
    if block.kind != "blockquote":
        return inner
    return f"\\begin{{quote}}\n{inner}\n\\end{{quote}}"


# ---------------------------------------------------------------------------
# Marked rendering: block pairing and whole-block revisions
# ---------------------------------------------------------------------------


def _block_sequence(ctx: _Ctx) -> list[tuple[ast.Block, str]]:
    sequence = []
    for source in ctx.snapshot.workspace.deliverables[ctx.name].sources:
        for block in ctx.snapshot.documents[source].blocks:
            if isinstance(block, ast.OutlineBlock):
                continue
            sequence.append((block, source))
    return sequence


def _render_deleted(ctx: _Ctx, block: ast.Block) -> str:
    match block:
        case ast.Heading():
            content = _diff_tokens(_render_inlines(ctx, block.inlines), (), float_mode=True)
            return f"\\DIFdelbegin\n{_render_heading(ctx, block, deleted=True, title=content)}\n\\DIFdelend"
        case ast.Paragraph():
            return _render_paragraph(ctx, block, deleted=True)
        case ast.ListBlock():
            return _render_list(ctx, block, deleted=True)
        case ast.DisplayMath() | ast.FigureBlock() | ast.TableBlock() | ast.MarkdownBlock():
            return f"\\DIFdelbegin\n{_render_block(ctx, block, deleted=True)}\n\\DIFdelend"
        case ast.ReviewItem():
            return f"\\DIFdelbegin\n{_render_block(ctx, block)}\n\\DIFdelend"
    raise AssertionError(f"unhandled block type {type(block).__name__}")


def _render_inserted(ctx: _Ctx, block: ast.Block) -> str:
    return f"\\DIFaddbegin\n{_render_block(ctx, block)}\n\\DIFaddend"


def _render_replacement(
    old: _Ctx, old_block: ast.Block, ctx: _Ctx, block: ast.Block
) -> str:
    return f"{_render_deleted(old, old_block)}\n{_render_inserted(ctx, block)}"


def _figure_structure(ctx: _Ctx, block: ast.FigureBlock) -> tuple:
    return (
        block.cols,
        tuple(panel.block_id for panel in block.subfigures),
        tuple(panel.asset_path for panel in block.subfigures),
        tuple(ctx.snapshot.assets[panel.asset_path] for panel in block.subfigures),
    )


def _grid_shape(grid: tuple[tuple[ast.TableCell, ...], ...]) -> tuple:
    return tuple((cell.row_span, cell.col_span, cell.covered) for row in grid for cell in row)


def _table_structure(block: ast.TableBlock) -> tuple:
    return (
        block.alignment,
        block.midrule_after,
        _grid_shape(block.header),
        _grid_shape(block.rows),
        len(block.notes),
        len(block.alignment),
    )


def _render_marked_pair(old: _Ctx, old_block: ast.Block, ctx: _Ctx, block: ast.Block) -> str:
    if type(old_block) is not type(block):
        return _render_replacement(old, old_block, ctx, block)
    match block:
        case ast.Heading():
            if old_block.level != block.level:
                return _render_replacement(old, old_block, ctx, block)
            content = _diff_tokens(
                _render_inlines(old, old_block.inlines),
                _render_inlines(ctx, block.inlines),
                float_mode=True,
            )
            return _render_heading(ctx, block, deleted=False, title=content)
        case ast.Paragraph():
            content = _diff_tokens(
                _render_inlines(old, old_block.inlines),
                _render_inlines(ctx, block.inlines),
                float_mode=False,
            )
            return _render_paragraph(ctx, block, deleted=False, content=content)
        case ast.ListBlock():
            return _render_marked_list(old, old_block, ctx, block)
        case ast.DisplayMath():
            if old_block.latex == block.latex and old_block.unnumbered == block.unnumbered:
                return _render_equation(ctx, block, deleted=False)
            return _render_replacement(old, old_block, ctx, block)
        case ast.FigureBlock():
            if _figure_structure(old, old_block) != _figure_structure(ctx, block):
                return _render_replacement(old, old_block, ctx, block)
            caption = _diff_tokens(
                _render_inlines(old, old_block.caption.inlines),
                _render_inlines(ctx, block.caption.inlines),
                float_mode=True,
            )
            return _render_figure(ctx, block, deleted=False, caption=caption)
        case ast.TableBlock():
            return _render_marked_table(old, old_block, ctx, block)
        case _:
            # Compare like-for-like without line anchors; live blocks keep them.
            comparison = replace(ctx, lineno=False)
            if _render_block(old, old_block) == _render_block(comparison, block):
                return _render_block(ctx, block)
            return _render_replacement(old, old_block, ctx, block)


def _render_marked_list(
    old: _Ctx, old_block: ast.ListBlock, ctx: _Ctx, block: ast.ListBlock
) -> str:
    if old_block.ordered != block.ordered:
        return _render_replacement(old, old_block, ctx, block)
    old_items = _item_tokens(old, old_block)
    new_items = _item_tokens(ctx, block)
    old_texts = [_join(tokens) for tokens in old_items]
    new_texts = [_join(tokens) for tokens in new_items]
    rendered: list[str] = []
    for op, i, j, k, last in SequenceMatcher(
        None, old_texts, new_texts, autojunk=False
    ).get_opcodes():
        if op == "equal":
            for index in range(j - i):
                content = _diff_tokens(
                    old_items[i + index], new_items[k + index], float_mode=False
                )
                rendered.append(f"\\item {content}")
            continue
        if op in {"delete", "replace"}:
            rendered.extend(
                f"\\item {_diff_tokens(tokens, (), float_mode=False)}"
                for tokens in old_items[i:j]
            )
        if op in {"insert", "replace"}:
            rendered.extend(
                f"\\item {_diff_tokens((), tokens, float_mode=False)}"
                for tokens in new_items[k:last]
            )
    environment = "enumerate" if block.ordered else "itemize"
    content = f"\\begin{{{environment}}}\n" + "\n".join(rendered) + f"\n\\end{{{environment}}}"
    start, end = _line_marks(ctx, block.block_id)
    return f"{start}{_block_label(ctx, block.block_id)}{content}{end}"


def _pair_grid(
    old: list[list[list[_Tok] | None]], new: list[list[list[_Tok] | None]]
) -> list[list[list[_Tok] | None]]:
    grid: list[list[list[_Tok] | None]] = []
    for old_row, new_row in zip(old, new):
        row: list[list[_Tok] | None] = []
        for old_cell, new_cell in zip(old_row, new_row):
            if old_cell is None or new_cell is None:
                row.append(None)
            else:
                row.append([_Tok(_diff_tokens(old_cell, new_cell, float_mode=False))])
        grid.append(row)
    return grid


def _render_marked_table(
    old: _Ctx, old_block: ast.TableBlock, ctx: _Ctx, block: ast.TableBlock
) -> str:
    if _table_structure(old_block) != _table_structure(block):
        return _render_replacement(old, old_block, ctx, block)
    caption = _diff_tokens(
        _render_inlines(old, old_block.caption.inlines),
        _render_inlines(ctx, block.caption.inlines),
        float_mode=True,
    )
    header = _pair_grid(_grid_tokens(old, old_block.header), _grid_tokens(ctx, block.header))
    data = _pair_grid(_grid_tokens(old, old_block.rows), _grid_tokens(ctx, block.rows))
    old_notes = [_render_inlines(old, note.inlines) for note in old_block.notes]
    new_notes = [_render_inlines(ctx, note.inlines) for note in block.notes]
    notes = [_diff_tokens(a, b, float_mode=True) for a, b in zip(old_notes, new_notes)]
    return _render_table(
        ctx, block, deleted=False, caption=caption, header=header, data=data, notes=notes
    )


# ---------------------------------------------------------------------------
# Bibliography assembly
# ---------------------------------------------------------------------------


def _cited_keys(numbered: NumberedWorkspace, name: str, workspace: Workspace) -> tuple[str, ...]:
    kind = workspace.deliverables[name].kind
    merged = workspace.style.bibliography_placement == "merged-in-main"
    if merged and kind == "supplementary":
        return ()
    if merged and kind == "manuscript":
        sources = {
            deliverable
            for deliverable, entry in workspace.deliverables.items()
            if entry.kind != "response_letter"
        }
        return tuple(
            key
            for key, reference in numbered.references.items()
            if any(source in reference.deliverable_counts for source in sources)
        )
    return tuple(
        key
        for key, reference in numbered.references.items()
        if name in reference.deliverable_counts
    )


def _bib_label_text(reference: NumberedReference) -> str:
    return f"{reference.short_label} ({reference.year})"


def _bib_item(numbered: NumberedWorkspace, key: str) -> str:
    reference = numbered.references[key]
    label = ""
    if numbered.citation_style == "author-year":
        label = f"[{{{_escape(_bib_label_text(reference))}}}]"
    text = _escape(reference.formatted_text)
    return f"\\bibitem{label}{{{key}}}\\label{{{_BIB_LABEL_PREFIX}{key}}} {text}"


def _bib_widest(numbered: NumberedWorkspace, keys: Sequence[str]) -> str:
    if numbered.citation_style == "numeric":
        return str(max(len(keys), 1))
    if not keys:
        return "9"
    return max(_bib_label_text(numbered.references[key]) for key in keys)


def _bib_item_marked(
    numbered: NumberedWorkspace, previous: NumberedReference, reference: NumberedReference, key: str
) -> str:
    if numbered.citation_style == "author-year":
        marked = _diff_tokens(
            _text_tokens(_escape(_bib_label_text(previous))),
            _text_tokens(_escape(_bib_label_text(reference))),
            float_mode=True,
        )
        label = f"[{{{marked}}}]"
    else:
        label = ""
    text = _diff_tokens(
        _text_tokens(_escape(previous.formatted_text)),
        _text_tokens(_escape(reference.formatted_text)),
        float_mode=False,
    )
    return f"\\bibitem{label}{{{key}}}\\label{{{_BIB_LABEL_PREFIX}{key}}} {text}"


def _bib_item_deleted(numbered: NumberedWorkspace, key: str) -> str:
    reference = numbered.references[key]
    label = ""
    if numbered.citation_style == "author-year":
        label = f"[{{{_escape(_bib_label_text(reference))}}}]"
    text = _escape(reference.formatted_text)
    # Deleted entries carry no \label: the live entry owns the anchor.
    return f"\\DIFdelbegin \\bibitem{label}{{{key}}} \\DIFdel{{{text}}}\\DIFdelend"


def _bib_item_inserted(numbered: NumberedWorkspace, key: str) -> str:
    reference = numbered.references[key]
    label = ""
    if numbered.citation_style == "author-year":
        label = f"[{{{_escape(_bib_label_text(reference))}}}]"
    text = _escape(reference.formatted_text)
    return (
        f"\\DIFaddbegin \\bibitem{label}{{{key}}}\\label{{{_BIB_LABEL_PREFIX}{key}}} "
        f"\\DIFadd{{{text}}}\\DIFaddend"
    )


def _bibliography(
    ctx: _Ctx,
    keys: Sequence[str],
    *,
    old: _Ctx | None = None,
    old_keys: Sequence[str] = (),
) -> str:
    if not keys and not old_keys:
        return ""
    entries: list[str] = []
    if old is None:
        entries = [_bib_item(ctx.numbered, key) for key in keys]
    else:
        for op, i, j, k, last in SequenceMatcher(
            None, list(old_keys), list(keys), autojunk=False
        ).get_opcodes():
            if op == "equal":
                for index in range(j - i):
                    key = keys[k + index]
                    entries.append(
                        _bib_item_marked(
                            ctx.numbered,
                            old.numbered.references[old_keys[i + index]],
                            ctx.numbered.references[key],
                            key,
                        )
                    )
            if op in {"delete", "replace"}:
                entries.extend(
                    _bib_item_deleted(old.numbered, key) for key in old_keys[i:j]
                )
            if op in {"insert", "replace"}:
                entries.extend(_bib_item_inserted(ctx.numbered, key) for key in keys[k:last])
    counter = ""
    if ctx.numbered.citation_style == "numeric" and keys:
        numbers = [ctx.numbered.references[key].number for key in keys]
        first = min(number for number in numbers if number is not None)
        counter = f"\\setcounter{{enumiv}}{{{first - 1}}}\n"
    widest = _bib_widest(ctx.numbered, keys)
    body = "\n".join(entries)
    return f"{counter}\\begin{{thebibliography}}{{{widest}}}\n{body}\n\\end{{thebibliography}}"


# ---------------------------------------------------------------------------
# Documents and assets
# ---------------------------------------------------------------------------


def _preamble(kind: DeliverableKind, style: Style, *, marked: bool) -> str:
    lines = [
        "\\documentclass[11pt]{article}",
        "\\usepackage[T1]{fontenc}",
        "\\usepackage[utf8]{inputenc}",
        "\\usepackage{amsmath}",
        "\\usepackage{amssymb}",
        "\\usepackage{graphicx}",
        "\\usepackage{booktabs}",
        "\\usepackage{multirow}",
        "\\usepackage{xcolor}",
        "\\usepackage{caption}",
        "\\usepackage{subcaption}",
    ]
    if kind != "response_letter":
        lines.append("\\usepackage[mathlines]{lineno}")
    if marked:
        lines.append("\\usepackage[normalem]{ulem}")
        lines.append(_DIF_PREAMBLE)
    lines.append("\\usepackage{hyperref}")
    color = style.link_color.lstrip("#")
    lines.append(f"\\definecolor{{{_LINK_COLOR_COMMAND}}}{{HTML}}{{{color}}}")
    lines.append(
        "\\hypersetup{colorlinks=true, linkcolor=OmmLinkColor, "
        "citecolor=OmmLinkColor, urlcolor=OmmLinkColor}"
    )
    if kind == "supplementary":
        lines.extend(
            [
                "\\renewcommand{\\thesection}{S\\arabic{section}}",
                "\\renewcommand{\\thefigure}{S\\arabic{figure}}",
                "\\renewcommand{\\thetable}{S\\arabic{table}}",
                "\\renewcommand{\\theequation}{S\\arabic{equation}}",
            ]
        )
    lines.append("\\setcounter{secnumdepth}{5}")
    return "\n".join(lines)


def _author_tokens(workspace: Workspace) -> list[_Tok]:
    authors = workspace.authors
    if isinstance(authors, str):
        return _text_tokens(_escape(authors))
    tokens: list[_Tok] = []
    for index, author in enumerate(authors):
        if index:
            tokens.append(_Tok(" \\and ", structural=True))
        tokens.extend(_text_tokens(_escape(author.name)))
    return tokens


def _frontmatter(ctx: _Ctx, old: _Ctx | None = None) -> str:
    """Render \\title/\\author/\\date; Marked documents diff title and authors."""
    workspace = ctx.snapshot.workspace
    if old is None:
        title = _join(_text_tokens(_escape(workspace.title)))
        authors = _join(_author_tokens(workspace))
    else:
        old_workspace = old.snapshot.workspace
        title = _diff_tokens(
            _text_tokens(_escape(old_workspace.title)),
            _text_tokens(_escape(workspace.title)),
            float_mode=True,
        )
        authors = _diff_tokens(
            _author_tokens(old_workspace), _author_tokens(workspace), float_mode=True
        )
    return f"\\title{{{title}}}\n\\author{{{authors}}}\n\\date{{}}"


def _document(
    kind: DeliverableKind,
    style: Style,
    frontmatter: str,
    body: str,
    bibliography: str,
    *,
    marked: bool,
) -> str:
    lines = [_preamble(kind, style, marked=marked), frontmatter, "", "\\begin{document}"]
    lines.append("\\maketitle")
    if kind != "response_letter":
        lines.append("\\linenumbers")
    lines.append("")
    if body:
        lines.append(body)
    if bibliography:
        lines.append(bibliography)
    lines.append("\\end{document}")
    return "\n".join(lines) + "\n"


def _figure_assets(snapshot: RevisionSnapshot, sources: Sequence[str]) -> set[str]:
    assets = set()
    for source in sources:
        for block in ast.walk_blocks(snapshot.documents[source].blocks):
            if isinstance(block, ast.Subfigure):
                assets.add(block.asset_path)
    return assets


def _asset_plan(
    snapshot: RevisionSnapshot,
    sources: Sequence[str],
    milestone: RevisionSnapshot | None,
    old_sources: Sequence[str] | None,
) -> tuple[dict[str, bytes], dict[str, str]]:
    """Resolve asset writes and old-side path aliases for one deliverable.

    Milestone assets that share a path with different bytes under the target
    snapshot are archived under ``.omm_old_<sha8>_<filename>`` so old and new
    images coexist without overwriting each other.
    """
    writes: dict[str, bytes] = {}
    alias: dict[str, str] = {}
    new_assets = _figure_assets(snapshot, sources)
    if milestone is None or old_sources is None:
        old_assets: set[str] = set()
    else:
        old_assets = _figure_assets(milestone, old_sources)
    for path in sorted(old_assets | new_assets):
        if path in new_assets:
            data = snapshot.assets[path]
        else:
            # The figure is gone in the target revision; keep the milestone bytes.
            data = milestone.assets[path]
        if path not in old_assets:
            writes[path] = data
            continue
        previous = milestone.assets[path]
        if previous == data:
            writes[path] = data
            continue
        digest = hashlib.sha256(previous).hexdigest()[:8]
        parent, _, name = path.rpartition("/")
        archived = f"{parent}/.omm_old_{digest}_{name}" if parent else f".omm_old_{digest}_{name}"
        alias[path] = archived
        writes[path] = data
        writes[archived] = previous
    return writes, alias


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _check_deliverable_name(name: str) -> None:
    if not name or "/" in name or "\\" in name or ".." in name:
        raise OmmError("ERR_MANIFEST_INVALID", field=name)


def _labeled_blocks(snapshot: RevisionSnapshot, sources: Sequence[str]) -> frozenset[str]:
    """Block ids whose ``omm:<id>`` anchors exist in a deliverable's document."""
    ids = set()
    for source in sources:
        for block in ast.walk_blocks(snapshot.documents[source].blocks):
            if block.block_id is not None and not isinstance(block, ast.ReviewItem):
                ids.add(block.block_id)
    return frozenset(ids)


def compile_latex_workspace(
    snapshot: RevisionSnapshot,
    out_dir: Path,
    *,
    milestone: RevisionSnapshot | None = None,
    loc_labels: Mapping[tuple[str, str | None], str] | None = None,
    deliverables: Collection[str] | None = None,
) -> dict[str, CompiledLatexDeliverable]:
    """Compile deliverables of ``snapshot`` into deterministic ``.tex`` files."""
    _validate_math(snapshot)
    if milestone is not None:
        _validate_math(milestone)
    numbered = number_workspace(snapshot)
    old_numbered = number_workspace(milestone) if milestone is not None else None
    labels = loc_labels or {}
    out_dir.mkdir(parents=True, exist_ok=True)
    workspace = snapshot.workspace
    requested = None if deliverables is None else list(dict.fromkeys(deliverables))
    for name in workspace.deliverables:
        _check_deliverable_name(name)
    if requested is not None:
        for name in requested:
            if name not in workspace.deliverables:
                raise KeyError(name)
    names = [name for name in workspace.deliverables if requested is None or name in requested]
    results: dict[str, CompiledLatexDeliverable] = {}
    for name in names:
        deliverable = workspace.deliverables[name]
        kind = deliverable.kind
        lineno = kind != "response_letter"
        old_deliverable = None
        if (
            milestone is not None
            and kind in {"manuscript", "supplementary"}
            and name in milestone.workspace.deliverables
        ):
            old_deliverable = milestone.workspace.deliverables[name]
        writes, alias = _asset_plan(
            snapshot,
            deliverable.sources,
            milestone if old_deliverable is not None else None,
            old_deliverable.sources if old_deliverable is not None else None,
        )
        keys = _cited_keys(numbered, name, workspace)
        ctx = _Ctx(
            snapshot,
            numbered,
            name,
            kind,
            lineno=lineno,
            record=lineno,
            loc_labels=labels,
            local_blocks=_labeled_blocks(snapshot, deliverable.sources),
            local_citations=frozenset(keys),
        )
        parts = []
        for source in deliverable.sources:
            document = snapshot.documents[source]
            ctx.document = document
            for block in document.blocks:
                rendered = _render_block(ctx, block)
                if rendered:
                    parts.append(rendered)
        body = "\n\n".join(parts)
        bibliography = _bibliography(ctx, keys)
        clean_path = out_dir / f"{name}.clean.tex"
        frontmatter = _frontmatter(ctx)
        atomic_write_text(
            clean_path,
            _document(kind, workspace.style, frontmatter, body, bibliography, marked=False),
        )

        marked_path = None
        if old_deliverable is not None and old_numbered is not None and milestone is not None:
            old_keys = _cited_keys(old_numbered, name, milestone.workspace)
            old_ctx = _Ctx(
                milestone,
                old_numbered,
                name,
                old_deliverable.kind,
                lineno=False,
                record=False,
                loc_labels={},
                local_blocks=_labeled_blocks(milestone, old_deliverable.sources),
                local_citations=frozenset(old_keys),
                asset_alias=alias,
            )
            # The Marked pass must not append to the Clean block records.
            marked_ctx = replace(ctx, record=False, records=[])
            marked_body = _marked_body(old_ctx, marked_ctx)
            marked_bibliography = _bibliography(marked_ctx, keys, old=old_ctx, old_keys=old_keys)
            marked_path = out_dir / f"{name}.marked.tex"
            marked_frontmatter = _frontmatter(marked_ctx, old=old_ctx)
            atomic_write_text(
                marked_path,
                _document(
                    kind,
                    workspace.style,
                    marked_frontmatter,
                    marked_body,
                    marked_bibliography,
                    marked=True,
                ),
            )

        asset_paths = tuple(sorted(out_dir / path for path in writes))
        for relative, data in writes.items():
            target = out_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        results[name] = CompiledLatexDeliverable(
            name, kind, clean_path, marked_path, asset_paths, tuple(ctx.records)
        )
    return results


def _pair_key(position: int, block: ast.Block, source: str) -> tuple:
    if block.block_id is not None:
        return (block.block_id, source)
    # Anonymous blocks (code, rules) pair only on identical content at the same
    # position; unrelated anonymous blocks must never match each other.
    return ("anon", position, source, block.clean_source)


def _marked_body(old: _Ctx, ctx: _Ctx) -> str:
    old_sequence = _block_sequence(old)
    new_sequence = _block_sequence(ctx)
    old_keys = [
        _pair_key(position, block, source)
        for position, (block, source) in enumerate(old_sequence)
    ]
    new_keys = [
        _pair_key(position, block, source)
        for position, (block, source) in enumerate(new_sequence)
    ]
    parts: list[str] = []
    for op, i, j, k, last in SequenceMatcher(
        None, old_keys, new_keys, autojunk=False
    ).get_opcodes():
        if op == "equal":
            for index in range(j - i):
                old_block, old_source = old_sequence[i + index]
                new_block, new_source = new_sequence[k + index]
                old.document = old.snapshot.documents[old_source]
                ctx.document = ctx.snapshot.documents[new_source]
                parts.append(_render_marked_pair(old, old_block, ctx, new_block))
            continue
        if op in {"delete", "replace"}:
            for block, source in old_sequence[i:j]:
                old.document = old.snapshot.documents[source]
                parts.append(_render_deleted(old, block))
        if op in {"insert", "replace"}:
            for block, source in new_sequence[k:last]:
                ctx.document = ctx.snapshot.documents[source]
                parts.append(_render_inserted(ctx, block))
    return "\n\n".join(parts)
