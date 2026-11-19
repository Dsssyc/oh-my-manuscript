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
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path

from omm import ast
from omm.docx_compiler import _visible_text
from omm.errors import OmmError
from omm.git_revs import RevisionSnapshot
from omm.numbering import BlockNumber, NumberedReference, NumberedWorkspace, number_workspace
from omm.omml import (
    _NUMBERED_ENVS,
    _TAG_RE,
    OmmlConversionError,
    build_numbered_equation_paragraph,
    latex_to_omml,
)
from omm.parser import _line_bounds, _markdown
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

_HEADING_COMMANDS = {
    1: "section", 2: "subsection", 3: "subsubsection", 4: "paragraph", 5: "subparagraph",
    6: "subsubparagraph",
}
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
    children: tuple[_Tok, ...] | None = None

    @property
    def latex(self) -> str:
        if self.children is None:
            return self.text
        return f"{self.text}{{{_join(self.children)}}}"


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
    pdf_layout: bool = False
    local_blocks: frozenset[str] = frozenset()
    local_citations: frozenset[str] = frozenset()
    asset_alias: Mapping[str, str] = field(default_factory=dict)
    heading_level: int = 0
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
    return "".join(token.latex for token in tokens)


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
                tokens.append(_wrapped(ctx, node, "\\textbf", quoted_source))
            case ast.Emphasis():
                tokens.append(_wrapped(ctx, node, "\\emph", quoted_source))
            case ast.Subscript():
                tokens.append(_wrapped(ctx, node, "\\textsubscript", quoted_source))
            case ast.Superscript():
                tokens.append(_wrapped(ctx, node, "\\textsuperscript", quoted_source))
            case ast.Link(destination=destination):
                inner = _join(_render_inlines(ctx, node.children, quoted_source=quoted_source))
                tokens.append(
                    _Tok(f"\\href{{{_escape_href(destination)}}}{{{inner}}}", atom=True)
                )
            case ast.Image(destination=destination):
                path = _graphics_path(ctx, destination)
                tokens.append(_Tok(f"\\includegraphics{{{path}}}", atom=True))
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
) -> _Tok:
    children = tuple(_render_inlines(ctx, node.children, quoted_source=quoted_source))
    return _Tok(command, atom=True, children=children)


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
                parts.append(_visible_text(latex_to_omml(latex)) if ctx.pdf_layout else latex)
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
            run.append(f"\\mbox{{{token.latex}}}" if token.atom else token.latex)
    flush()
    return f"{begin} " + "".join(pieces) + f"{end}{{}}"


def _diff_tokens(old: Sequence[_Tok], new: Sequence[_Tok], *, float_mode: bool) -> str:
    parts: list[str] = []
    # Formatting containers can match by command, but their braces always stay together.
    old_keys = [token if token.children is None else token.text for token in old]
    new_keys = [token if token.children is None else token.text for token in new]
    for op, i, j, k, last in SequenceMatcher(None, old_keys, new_keys, autojunk=False).get_opcodes():
        if op == "equal":
            for before, after in zip(old[i:j], new[k:last]):
                if before == after:
                    parts.append(after.latex)
                else:
                    assert before.children is not None and after.children is not None
                    inner = _diff_tokens(before.children, after.children, float_mode=float_mode)
                    parts.append(f"{after.text}{{{inner}}}")
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
    command = _HEADING_COMMANDS[block.level]
    if title is None:
        title = _join(_render_inlines(ctx, block.inlines))
    # Deleted headings use starred forms so they never advance section counters.
    if deleted or ctx.kind == "response_letter":
        return f"\\{command}*{{{title}}}"
    text = _plain(ctx, block.inlines)
    number = ctx.numbered.block_numbers[ctx.name].get(block.block_id)
    if ctx.pdf_layout and number is not None:
        text = f"{number.number} {text}"
    _record(ctx, block, "heading", text)
    start, end = _line_marks(ctx, block.block_id)
    level = block.level
    counters = "".join(
        f"\\setcounter{{{_HEADING_COMMANDS[ancestor]}}}{{1}}\n"
        for ancestor in range(ctx.heading_level + 1, level)
    )
    if level > ctx.heading_level + 1:
        counters += f"\\setcounter{{{command}}}{{0}}\n"
    ctx.heading_level = level
    return f"{counters}{start}\\{command}{{{title}}}{_block_label(ctx, block.block_id)}{end}"


def _render_paragraph(
    ctx: _Ctx, block: ast.Paragraph, *, deleted: bool, content: str | None = None
) -> str:
    if deleted:
        return _diff_tokens(_render_inlines(ctx, block.inlines), (), float_mode=False)
    body = content if content is not None else _join(_render_inlines(ctx, block.inlines))
    _record(ctx, block, "paragraph", _plain(ctx, block.inlines))
    start, end = _line_marks(ctx, block.block_id)
    return f"{start}{_block_label(ctx, block.block_id)}{body}{end}"


type _ListPart = str | list[_Tok]


def _list_parts(
    ctx: _Ctx, block: ast.ListBlock
) -> tuple[list[_ListPart], list[str], list[int]]:
    parts: list[_ListPart] = []
    plain = []
    item_starts = []
    bounds = _line_bounds(block.clean_source)
    counters: list[str] = []
    environments: list[str] = []
    for token in _PLAIN_MARKDOWN.parse(block.clean_source):
        if token.type in {"bullet_list_open", "ordered_list_open"}:
            ordered = token.type == "ordered_list_open"
            environment = "enumerate" if ordered else "itemize"
            environments.append(environment)
            parts.append(f"\\begin{{{environment}}}")
            if ordered:
                counter = ("enumi", "enumii", "enumiii", "enumiv")[min(len(counters), 3)]
                counters.append(counter)
                start = int(token.attrGet("start") or 1)
                if start != 1:
                    parts.append(f"\\setcounter{{{counter}}}{{{start - 1}}}")
        elif token.type in {"bullet_list_close", "ordered_list_close"}:
            parts.append(f"\\end{{{environments.pop()}}}")
            if token.type == "ordered_list_close":
                counters.pop()
        elif token.type == "list_item_open":
            if len(environments) == 1:
                item_starts.append(len(parts))
            parts.append(r"\item")
        elif token.type == "blockquote_open" and environments:
            parts.append(r"\begin{quote}")
        elif token.type == "blockquote_close" and environments:
            parts.append(r"\end{quote}")
        elif token.type in {"paragraph_open", "heading_open"}:
            first, last = token.map
            nodes = tuple(
                node for node in block.inlines if bounds[first] <= node.span.start < bounds[last]
            )
            inline = _render_inlines(ctx, nodes)
            if token.type == "heading_open":
                inline = [_Tok(r"\textbf", atom=True, children=tuple(inline))]
            parts.extend((inline, r"\par"))
            plain.append(_plain(ctx, nodes))
        elif token.type in {"fence", "code_block"}:
            parts.append(_literal_code(token.content.rstrip("\n")))
            plain.append(token.content)
        elif token.type == "omm_display":
            parts.append(f"\\[{token.content}\n\\]")
            plain.append(token.content)
        elif token.type == "hr":
            parts.append(r"\noindent\rule{\linewidth}{0.4pt}")
    return parts, plain, item_starts


def _render_list_parts(
    parts: Sequence[_ListPart], *, deleted: bool = False, inserted: bool = False,
) -> str:
    rendered_parts = []
    for part in parts:
        if isinstance(part, str):
            rendered_parts.append(part)
        elif deleted or inserted:
            rendered_parts.append(_marked(part, deleted=deleted, float_mode=False))
        else:
            rendered_parts.append(_join(part))
    return "\n".join(rendered_parts)


def _render_list(
    ctx: _Ctx, block: ast.ListBlock, *, deleted: bool, inserted: bool = False
) -> str:
    parts, plain, _ = _list_parts(ctx, block)
    rendered = _render_list_parts(parts, deleted=deleted, inserted=inserted)
    if deleted:
        return rendered
    _record(ctx, block, "list", " ".join(" ".join(plain).split()))
    start, end = _line_marks(ctx, block.block_id)
    return f"{start}{_block_label(ctx, block.block_id)}{rendered}{end}"


def _render_equation(ctx: _Ctx, block: ast.DisplayMath, *, deleted: bool) -> str:
    latex = block.latex.strip()
    # Deleted equations use the star form so they never advance the counter;
    # _render_deleted supplies the surrounding DIFdelbegin/DIFdelend markers.
    if deleted:
        return f"\\[{latex}\n\\]"
    if ctx.pdf_layout:
        number = ctx.numbered.block_numbers[ctx.name].get(block.block_id)
        equation = build_numbered_equation_paragraph(
            latex, number.number if number else None, block_id=block.block_id
        )
        text = _visible_text(equation)
    else:
        text = " ".join(latex.split())
    _record(ctx, block, "display_math", text)
    start, end = _line_marks(ctx, block.block_id)
    if block.unnumbered:
        return f"{start}{_block_label(ctx, block.block_id)}\\[{latex}\n\\]{end}"
    label = _block_label(ctx, block.block_id)
    body = f"\\begin{{equation}}{label} {latex}\n\\end{{equation}}"
    return f"{start}{body}{end}"


def _graphics_path(ctx: _Ctx, asset_path: str) -> str:
    # graphicx reads underscores as filename characters, including in archive aliases.
    return _escape(ctx.asset_alias.get(asset_path, asset_path)).replace(r"\_", "_")


def _render_figure(
    ctx: _Ctx, block: ast.FigureBlock, *, deleted: bool, caption: str | None = None,
    panel_captions: Sequence[str] | None = None,
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
        if not deleted:
            figure_label += _block_label(ctx, panels[0].block_id)
    else:
        columns = block.cols or len(panels)
        width = f"{1 / columns:.3f}"
        if panel_captions is None:
            panel_captions = [_escape(panel.alt) for panel in panels]
        entries = list(zip(panels, panel_captions))
        rows = [entries[index : index + columns] for index in range(0, len(entries), columns)]
        lines.extend(_figure_rows(ctx, rows, width, deleted=deleted))
    lines.append(f"{caption_command}{{{caption}}}{figure_label}")
    lines.append("\\end{figure}")
    return "\n".join(lines)


def _figure_rows(
    ctx: _Ctx, rows: Sequence[Sequence[tuple[ast.Subfigure, str]]], width: str, *, deleted: bool
) -> list[str]:
    lines: list[str] = []
    for index, row in enumerate(rows):
        for position, (panel, caption) in enumerate(row):
            command = "\\caption*" if deleted else "\\caption"
            label = "" if deleted else _block_label(ctx, panel.block_id)
            lines.append(f"\\begin{{subfigure}}[t]{{{width}\\textwidth}}")
            lines.append("\\centering")
            lines.append(
                f"\\includegraphics[width=\\linewidth]{{{_graphics_path(ctx, panel.asset_path)}}}"
            )
            lines.append(f"{command}{{{caption}}}{label}")
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
    return _literal_code(content)


def _literal_code(content: str) -> str:
    if r"\end{verbatim}" not in content:
        return f"\\begin{{verbatim}}\n{content}\n\\end{{verbatim}}"
    lines = [_escape(line).replace(" ", r"\ ") for line in content.expandtabs().split("\n")]
    return "\n".join(f"\\noindent\\texttt{{\\strut {line}}}\\par" for line in lines)


def _render_markdown(
    ctx: _Ctx, block: ast.MarkdownBlock, *, deleted: bool, inserted: bool = False
) -> str:
    if block.kind in {"fence", "code_block"}:
        return _code_block(block)
    if block.kind == "hr":
        return "\\noindent\\rule{\\linewidth}{0.4pt}"
    rendered = (
        _render_inserted(ctx, child) if inserted else _render_block(ctx, child, deleted=deleted)
        for child in block.children
    )
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
    match block:
        case ast.Heading():
            title = _marked(_render_inlines(ctx, block.inlines), deleted=False, float_mode=True)
            return _render_heading(ctx, block, deleted=False, title=title)
        case ast.Paragraph():
            content = _marked(_render_inlines(ctx, block.inlines), deleted=False, float_mode=False)
            return _render_paragraph(ctx, block, deleted=False, content=content)
        case ast.ListBlock():
            return _render_list(ctx, block, deleted=False, inserted=True)
        case ast.FigureBlock() | ast.TableBlock():
            caption = _marked(
                _render_inlines(ctx, block.caption.inlines), deleted=False, float_mode=True
            )
            if isinstance(block, ast.TableBlock):
                return _render_table(ctx, block, deleted=False, caption=caption)
            panel_captions = [
                _marked(_text_tokens(_escape(panel.alt)), deleted=False, float_mode=True)
                for panel in block.subfigures
            ]
            return _render_figure(
                ctx, block, deleted=False, caption=caption, panel_captions=panel_captions
            )
        case ast.MarkdownBlock(children=children) if children:
            return _render_markdown(ctx, block, deleted=False, inserted=True)
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
            panel_captions = [
                _diff_tokens(
                    _text_tokens(_escape(before.alt)), _text_tokens(_escape(after.alt)),
                    float_mode=True,
                )
                for before, after in zip(old_block.subfigures, block.subfigures)
            ]
            return _render_figure(
                ctx, block, deleted=False, caption=caption, panel_captions=panel_captions
            )
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
    old_parts, _, old_starts = _list_parts(old, old_block)
    new_parts, _, new_starts = _list_parts(ctx, block)
    if not old_starts or not new_starts or old_parts[:old_starts[0]] != new_parts[:new_starts[0]]:
        return _render_replacement(old, old_block, ctx, block)
    # The final part closes the outer environment. Nested lists and code stay with their item.
    old_items = [old_parts[a:b] for a, b in zip(old_starts, [*old_starts[1:], len(old_parts) - 1])]
    new_items = [new_parts[a:b] for a, b in zip(new_starts, [*new_starts[1:], len(new_parts) - 1])]
    rendered = [_render_list_parts(new_parts[:new_starts[0]])]
    old_keys = [_render_list_parts(item) for item in old_items]
    new_keys = [_render_list_parts(item) for item in new_items]
    for op, i, j, k, last in SequenceMatcher(None, old_keys, new_keys, autojunk=False).get_opcodes():
        if op == "equal":
            rendered.extend(new_keys[k:last])
        if op in {"delete", "replace"}:
            rendered.extend(_render_list_parts(item, deleted=True) for item in old_items[i:j])
        if op in {"insert", "replace"}:
            rendered.extend(_render_list_parts(item, inserted=True) for item in new_items[k:last])
    closing = new_parts[-1]
    assert isinstance(closing, str)
    rendered.append(closing)
    content = "\n".join(rendered)
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
    if workspace.style.bibliography_placement == "merged-in-main":
        if kind != "manuscript":
            return ()
        return tuple(key for key, reference in numbered.references.items() if reference.cited)
    return tuple(
        key
        for key, reference in numbered.references.items()
        if name in reference.deliverable_counts
    )


def _bib_label_text(reference: NumberedReference) -> str:
    return f"{reference.short_label} ({reference.year})"


def _bib_label(numbered: NumberedWorkspace, reference: NumberedReference) -> str:
    if numbered.citation_style == "numeric":
        return f"[{reference.number}]"
    return f"[{{{_escape(_bib_label_text(reference))}}}]"


def _bib_item(numbered: NumberedWorkspace, key: str) -> str:
    reference = numbered.references[key]
    label = _bib_label(numbered, reference)
    text = _escape(reference.formatted_text)
    return f"\\bibitem{label}{{{key}}}\\label{{{_BIB_LABEL_PREFIX}{key}}} {text}"


def _bib_widest(numbered: NumberedWorkspace, keys: Sequence[str]) -> str:
    if numbered.citation_style == "numeric":
        return str(max((numbered.references[key].number or 1 for key in keys), default=1))
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
        label = _bib_label(numbered, reference)
    text = _diff_tokens(
        _text_tokens(_escape(previous.formatted_text)),
        _text_tokens(_escape(reference.formatted_text)),
        float_mode=False,
    )
    return f"\\bibitem{label}{{{key}}}\\label{{{_BIB_LABEL_PREFIX}{key}}} {text}"


def _bib_item_deleted(numbered: NumberedWorkspace, key: str, alias: str) -> str:
    reference = numbered.references[key]
    label = _bib_label(numbered, reference)
    text = _escape(reference.formatted_text)
    # Deleted entries carry no \label: the live entry owns the anchor.
    return f"\\DIFdelbegin \\bibitem{label}{{{alias}}} \\DIFdel{{{text}}}\\DIFdelend"


def _bib_item_inserted(numbered: NumberedWorkspace, key: str) -> str:
    reference = numbered.references[key]
    label = _bib_label(numbered, reference)
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
        occupied = set(keys)
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
                for key in old_keys[i:j]:
                    alias = f"omm-del-{key}"
                    while alias in occupied:
                        alias = f"omm-del-{alias}"
                    occupied.add(alias)
                    entries.append(_bib_item_deleted(old.numbered, key, alias))
            if op in {"insert", "replace"}:
                entries.extend(_bib_item_inserted(ctx.numbered, key) for key in keys[k:last])
    widest = _bib_widest(ctx.numbered, keys)
    if old_keys and old is not None:
        old_widest = _bib_widest(old.numbered, old_keys)
        widest = max((widest, old_widest), key=len)
    body = "\n".join(entries)
    return f"\\begin{{thebibliography}}{{{_escape(widest)}}}\n{body}\n\\end{{thebibliography}}"


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
    lines.extend((
        "\\makeatletter",
        "\\newcounter{subsubparagraph}[subparagraph]",
        "\\renewcommand{\\thesubsubparagraph}{\\thesubparagraph.\\arabic{subsubparagraph}}",
        "\\newcommand{\\toclevel@subsubparagraph}{6}",
        (
            "\\newcommand{\\subsubparagraph}{\\@startsection{subsubparagraph}{6}{\\parindent}"
            "{3.25ex plus 1ex minus .2ex}{-1em}{\\normalfont\\normalsize\\bfseries}}"
        ),
        "\\makeatother",
        "\\setcounter{secnumdepth}{6}",
    ))
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


def _referenced_assets(snapshot: RevisionSnapshot, sources: Sequence[str]) -> set[str]:
    assets = set()
    for source in sources:
        for block in ast.walk_blocks(snapshot.documents[source].blocks):
            if isinstance(block, ast.Subfigure):
                assets.add(block.asset_path)
            inlines = block.comment.inlines if isinstance(block, ast.ReviewItem) else block.inlines
            assets.update(
                node.destination for node in ast.walk_inlines(inlines)
                if isinstance(node, ast.Image) and node.destination in snapshot.assets
            )
    return assets


def _asset_plan(
    snapshot: RevisionSnapshot,
    new_assets: set[str],
    global_new_assets: set[str],
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
    if milestone is None or old_sources is None:
        old_assets: set[str] = set()
    else:
        old_assets = _referenced_assets(milestone, old_sources)
    for path in sorted(old_assets | new_assets):
        if path in global_new_assets:
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
        archived = (Path(parent) / f".omm_old_{digest}_{name}").as_posix()
        while archived in snapshot.assets or archived in milestone.assets:
            archived = (Path(parent) / f".omm_old_{Path(archived).name}").as_posix()
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
            if block.block_id is not None and not isinstance(block, (ast.OutlineBlock, ast.ReviewItem)):
                ids.add(block.block_id)
    return frozenset(ids)


def compile_latex_workspace(
    snapshot: RevisionSnapshot,
    out_dir: Path,
    *,
    milestone: RevisionSnapshot | None = None,
    loc_labels: Mapping[tuple[str, str | None], str] | None = None,
    deliverables: Collection[str] | None = None,
    pdf_deliverables: Collection[str] = (),
    shared_asset_deliverables: Collection[str] = (),
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
    referenced_assets = {
        name: _referenced_assets(snapshot, workspace.deliverables[name].sources)
        for name in dict.fromkeys((*names, *shared_asset_deliverables))
    }
    global_new_assets = set().union(*referenced_assets.values())
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
            referenced_assets[name],
            global_new_assets,
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
            pdf_layout=lineno and name in pdf_deliverables,
            local_blocks=_labeled_blocks(snapshot, deliverable.sources) if lineno else frozenset(),
            local_citations=frozenset(keys),
        )
        parts = []
        for source in deliverable.sources:
            document = snapshot.documents[source]
            for block in document.blocks:
                try:
                    rendered = _render_block(ctx, block)
                except OmmlConversionError as exc:
                    raise OmmError(
                        exc.code, path=document.path, line=block.line,
                        block_id=block.block_id, detail=exc.message,
                    ) from exc
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
                local_blocks=(
                    _labeled_blocks(milestone, old_deliverable.sources)
                    if old_deliverable.kind != "response_letter" else frozenset()
                ),
                local_citations=frozenset(old_keys).intersection(keys),
                asset_alias=alias,
            )
            # The Marked pass must not append to the Clean block records.
            marked_ctx = replace(ctx, record=False, pdf_layout=False, heading_level=0, records=[])
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
                old_block, _ = old_sequence[i + index]
                new_block, _ = new_sequence[k + index]
                parts.append(_render_marked_pair(old, old_block, ctx, new_block))
            continue
        if op in {"delete", "replace"}:
            for block, _ in old_sequence[i:j]:
                parts.append(_render_deleted(old, block))
        if op in {"insert", "replace"}:
            for block, _ in new_sequence[k:last]:
                parts.append(_render_inserted(ctx, block))
    return "\n\n".join(parts)
