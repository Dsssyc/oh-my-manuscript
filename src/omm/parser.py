"""Parse Structured Source using CommonMark and manuscript-specific syntax."""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path
from typing import Literal
from urllib.parse import unquote

from markdown_it import MarkdownIt
from markdown_it.parser_inline import ParserInline
from markdown_it.rules_block import StateBlock
from markdown_it.rules_block.lheading import lheading
from markdown_it.rules_inline import StateInline
from markdown_it.token import Token

from omm import ast
from omm.errors import Diagnostic, OmmError
from omm.workspace import normalize_newlines, normalize_relative_path, read_text

BLOCK_ID = r"[A-Za-z][A-Za-z0-9_-]{1,63}"
_ID = re.compile(BLOCK_ID)
_REF = re.compile(rf"\[#({BLOCK_ID})\]")
_LOC = re.compile(rf"\[#loc:({BLOCK_ID})(?:\.\.({BLOCK_ID}))?\]")
_KEY = r"[\w][\w:./+?-]*"
_CITE = re.compile(rf"@({_KEY})")
_CITATION_GROUP = re.compile(rf"\[\s*@{_KEY}(?:\s*;\s*@{_KEY})*\s*\]")
_DIRECTIVE = re.compile(r":::\{([a-zA-Z][\w-]*)\}(.*)")
_SUFFIX = re.compile(r"[ \t]*(\{#[^{}\n]*\})[ \t]*$")
_SETEXT = re.compile(r"^[ \t]*(?:>[ \t]*)*((?:=+|-+)[ \t]*\{#[^{}\n]*\}[ \t]*)$", re.MULTILINE)
_ATTRIBUTE = re.compile(r'([a-z][a-z_-]*)(?:=("(?:[^"\\]|\\["\\])*"|[^\s"={}]+))?')
_TEX_COMMAND = re.compile(r"\\(?:[a-zA-Z]+|[^\n])")
_ALIGN = re.compile(r":?-{3,}:?")
_RULE = re.compile(r"-{3,}")
_EMAIL = re.compile(r"[A-Za-z0-9][\w.!#$%&'*+/=?^`{|}~-]*@[\w-]+(?:\.[\w-]+)+")
_LOC_TRAILER = re.compile(r"[\s\[({<,;:!?\u2013\u2014]")
_LIST_TYPES = {"bullet_list_open", "ordered_list_open"}
_LITERAL_BLOCKS = {"fence", "code_block", "omm_display", "hr"}
_LIST_PREFIX = re.compile(r"([ \t]*(?:>[ \t]*)*)(?:[-+*]|\d{1,9}[.)])")


def _escaped(source: str, pos: int) -> bool:
    start = pos
    while pos and source[pos - 1] == "\\":
        pos -= 1
    return (start - pos) % 2 == 1


def _math_rule(state: StateInline, silent: bool) -> bool:
    start = state.pos
    src = state.src
    limit = state.posMax
    if (
        src[start] != "$"
        or start + 1 >= limit
        or src[start + 1].isspace()
        or src[start + 1] == "$"
        or (start and src[start - 1] == "$")
    ):
        return False
    end = start + 1
    while (end := src.find("$", end + 1, limit)) != -1:
        if _escaped(src, end):
            continue
        if src[end - 1].isspace() or (end + 1 < limit and src[end + 1] == "$"):
            return False
        if not silent:
            state.push("inline_math", "", 0).content = src[start + 1 : end]
        state.pos = end + 1
        return True
    return False


def _reference_rule(state: StateInline, silent: bool) -> bool:
    start = state.pos
    source = state.src
    for pattern, kind in ((_LOC, "loc_ref"), (_REF, "block_ref")):
        if match := pattern.match(source, start, state.posMax):
            if not silent:
                state.push(kind, "", 0).meta["ids"] = match.groups()
            state.pos = match.end()
            return True
    if source.startswith("[#", start):
        end = source.find("]", start + 2, state.posMax)
        if end != -1:
            if not silent:
                state.push("invalid_ref", "", 0).content = source[start : end + 1]
            state.pos = end + 1
            return True
    if match := _CITATION_GROUP.match(source, start, state.posMax):
        if not silent:
            token = state.push("citation", "", 0)
            token.meta.update(keys=tuple(_CITE.findall(match[0])), parenthetical=True)
        state.pos = match.end()
        return True
    if source[start] != "@" or (match := _CITE.match(source, start, state.posMax)) is None:
        return False
    if start:
        previous = source[start - 1]
        if not (previous.isspace() or unicodedata.category(previous).startswith("P")):
            return False
        if previous in ".!#%&'*+/=?^`{|}~-":
            before = start - 1
            while before and not _LOC_TRAILER.fullmatch(source[before - 1]):
                before -= 1
            if _EMAIL.match(source, before, state.posMax):
                return False
    key = match[1].rstrip(".:?!")
    if not key:
        return False
    if not silent:
        token = state.push("citation", "", 0)
        token.meta.update(keys=(key,), parenthetical=False)
    state.pos = start + 1 + len(key)
    return True


def _script_rule(state: StateInline, silent: bool) -> bool:
    start = state.pos
    src = state.src
    marker = src[start]
    if marker not in "~^" or (start and src[start - 1] == marker):
        return False
    end = src.find(marker, start + 1, state.posMax)
    if end <= start + 1:
        return False
    value = src[start + 1 : end]
    if (
        any(char.isspace() for char in value)
        or _escaped(src, end)
        or src.startswith(marker * 2, end)
    ):
        return False
    if not silent:
        token = state.push("subscript" if marker == "~" else "superscript", "", 0)
        token.content = value
    state.pos = end + 1
    return True


class _SpannedState(StateInline):
    pending_start: int | None = None

    def push(self, ttype: str, tag: str, nesting: Literal[-1, 0, 1]) -> Token:
        token = super().push(ttype, tag, nesting)
        self.pending_start = None
        return token

    def pushPending(self) -> Token:
        token = super().pushPending()
        if self.pending_start is not None:
            token.meta["span"] = ast.Span(
                self.pending_start, self.pending_start + len(token.content)
            )
        self.pending_start = None
        return token


class _SpannedInlineParser(ParserInline):
    """Record the source consumed by CommonMark before delimiter balancing."""

    def __init__(self) -> None:
        super().__init__()
        self.ruler.disable(("strikethrough", "linkify"))
        self.ruler2.disable(("fragments_join", "strikethrough"))
        self.ruler.before("link", "omm_reference", _reference_rule)
        self.ruler.before("emphasis", "omm_math", _math_rule)
        self.ruler.before("emphasis", "omm_script", _script_rule)

    def tokenize(self, state: _SpannedState) -> None:
        rules = self.ruler.getRules("")
        end = state.posMax
        while state.pos < end:
            start = state.pos
            index = len(state.tokens)
            if state.pending_start is None:
                state.pending_start = start
            if not (
                state.level < state.md.options["maxNesting"]
                and any(rule(state, False) for rule in rules)
            ):
                state.pending += state.src[start]
                state.pos += 1
                continue

            new_tokens = state.tokens[index:]
            span_start = start
            if state.src[start] == "\n":
                while span_start and state.src[span_start - 1] == " ":
                    span_start -= 1
            span = ast.Span(span_start, state.pos)
            if all(
                token.type == "text" and token.content == state.src[start]
                for token in new_tokens
                if "span" not in token.meta
            ):
                # Emphasis initially emits one literal token per delimiter.
                for offset, token in enumerate(
                    token for token in new_tokens if "span" not in token.meta
                ):
                    token.meta["span"] = ast.Span(start + offset, start + offset + 1)
            else:
                for token in new_tokens:
                    if token.type == "text" and "span" not in token.meta:
                        # The sole text child of an autolink excludes its angle brackets.
                        token.meta["span"] = ast.Span(start + 1, state.pos - 1)
                    token.meta.setdefault("span", span)
            if new_tokens:
                state.pending_start = None

        if state.pending:
            state.pushPending()

    def parse(self, src: str, md: MarkdownIt, env: dict, tokens: list[Token]) -> list[Token]:
        state = _SpannedState(src, md, env, tokens)
        self.tokenize(state)
        for rule in self.ruler2.getRules(""):
            rule(state)
        return tokens


def _math_close(source: str, start: int, limit: int) -> int:
    while (pos := source.find("$$", start, limit)) != -1:
        if not _escaped(source, pos):
            return pos
        start = pos + 2
    return -1


def _display_rule(state: StateBlock, start: int, end: int, silent: bool) -> bool:
    pos = state.bMarks[start] + state.tShift[start]
    if state.is_code_block(start) or not state.src.startswith("$$", pos):
        return False
    if silent:
        return True
    for line in range(start, end):
        if line > start and state.sCount[line] < state.blkIndent and not state.isEmpty(line):
            end = line
            break
        begin = pos + 2 if line == start else state.bMarks[line] + state.tShift[line]
        close = _math_close(state.src, begin, state.eMarks[line])
        if close != -1:
            token = state.push("omm_display", "", 0)
            token.content = state.src[pos + 2 : close]
            token.meta["close"] = close
            token.map = [start, line + 1]
            state.line = line + 1
            return True
    token = state.push("omm_display", "", 0)
    token.map = [start, end]
    state.line = end
    return True


def _directive_rule(state: StateBlock, start: int, end: int, silent: bool) -> bool:
    pos = state.bMarks[start] + state.tShift[start]
    nested = state.level and (not silent or state.sCount[start] >= state.blkIndent)
    if nested or state.is_code_block(start) or start in state.env.get("protected_lines", ()):
        return False
    text = state.src[pos : state.eMarks[start]].rstrip(" \t")
    if text != ":::" and _DIRECTIVE.fullmatch(text) is None:
        return False
    if not silent:
        token = state.push("omm_directive", "", 0)
        token.content = text
        token.map = [start, start + 1]
        state.line = start + 1
    return True


def _setext_rule(state: StateBlock, start: int, end: int, silent: bool) -> bool:
    # Match a marked underline without shortening the physical line seen
    # by CommonMark containers and lazy paragraph continuations.
    if not hasattr(state, "_omm_setext_offsets"):
        state._omm_setext_offsets = [match.start(1) for match in _SETEXT.finditer(state.src)]
        # A match can begin after a list or quote prefix. Resolve its line
        # using the original offsets before CommonMark temporarily shifts them.
        bounds = _line_bounds(state.src) if state._omm_setext_offsets else []
        state._omm_setext_lines = [
            bisect_right(bounds, offset) - 1 for offset in state._omm_setext_offsets
        ]
    candidate = bisect_right(state._omm_setext_lines, start)
    if candidate >= len(state._omm_setext_lines) or state._omm_setext_lines[candidate] >= end:
        return lheading(state, start, end, silent)
    line = state._omm_setext_lines[candidate]
    begin = state._omm_setext_offsets[candidate]
    if (
        begin != state.bMarks[line] + state.tShift[line]
        or state.sCount[line] < state.blkIndent
        or state.is_code_block(line)
        or state.is_code_block(start)
    ):
        return lheading(state, start, end, silent)
    if lheading(state, start, line, silent):
        return True

    parent = state.parentType
    state.parentType = "paragraph"
    terminators = state.md.block.ruler.getRules("paragraph")
    try:
        for current in range(start + 1, line):
            if state.isEmpty(current):
                break
            if state.is_code_block(current) or state.sCount[current] < 0:
                continue
            if any(rule(state, current, end, True) for rule in terminators):
                break
        else:
            marker = state.src[begin]
            tag = "h1" if marker == "=" else "h2"
            state.line = line + 1
            opening = state.push("heading_open", tag, 1)
            opening.markup = marker
            opening.map = [start, line + 1]
            inline = state.push("inline", "", 0)
            inline.content = state.getLines(start, line, state.blkIndent, False).strip(" \t\n")
            inline.map = [start, line]
            state.push("heading_close", tag, -1).markup = marker
            return True
    finally:
        state.parentType = parent
    return lheading(state, start, end, silent)


def _markdown(*, directives: bool = False) -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False})
    md.inline = _SpannedInlineParser()
    md.core.ruler.disable("inline")
    md.block.ruler.at("lheading", _setext_rule)
    md.block.ruler.before("fence", "omm_display", _display_rule, {"alt": ["paragraph"]})
    if directives:
        md.block.ruler.before(
            "fence", "omm_directive", _directive_rule, {"alt": ["paragraph", "list"]}
        )
    return md


_MARKDOWN = _markdown()
_STRUCTURED = _markdown(directives=True)


def _inline_tokens(value: str, env: dict) -> list[Token]:
    return _MARKDOWN.inline.parse(value, _MARKDOWN, env, [])


def _token_lines(token: Token) -> tuple[int, int]:
    assert token.map is not None
    return token.map[0], token.map[1]


def _literal_list_end(tokens: Sequence[Token]) -> int | None:
    if tokens[0].type in _LIST_TYPES:
        terminal = next(token for token in reversed(tokens) if token.map is not None)
        if terminal.type in _LITERAL_BLOCKS:
            return _token_lines(terminal)[1]
    return None


def _line_bounds(source: str) -> list[int]:
    return [0, *(match.end() for match in re.finditer("\n", source)), len(source) + 1]


def _map_inline(token: Token, source: str, bounds: list[int]) -> tuple[int, ...]:
    """Map each CommonMark inline character back through container prefixes."""
    first, _ = _token_lines(token)
    offsets: list[int] = []
    for line, part in enumerate(token.content.split("\n"), first):
        begin, end = bounds[line : line + 2]
        physical = source[begin:end].removesuffix("\n")
        column = physical.rfind(part) if part else 0
        if column < 0:
            # CommonMark may expand the indented part of a tab in a container.
            content = part.lstrip(" ")
            column = physical.rfind(content)
            if column < 0:
                raise OmmError("ERR_SOURCE_MAPPING")
            offsets.extend([begin + max(0, column - 1)] * (len(part) - len(content)))
            part = content
        offsets.extend(range(begin + column, begin + column + len(part)))
        offsets.append(begin + len(physical))
    return tuple(offsets[:-1])


def _protected_directive_lines(source: str, bounds: list[int]) -> set[int]:
    if ":::" not in source:
        return set()
    protected = set()
    env: dict = {}
    for token in _MARKDOWN.parse(source, env):
        if token.type != "inline" or ":::" not in token.content:
            continue
        offsets = _map_inline(token, source, bounds)
        for item in _inline_tokens(token.content, env):
            if item.type in {"code_inline", "inline_math"}:
                span = item.meta["span"]
                start = bisect_right(bounds, offsets[span.start])
                end = bisect_right(bounds, offsets[span.end - 1])
                protected.update(range(start, end - 1))
    return protected


@dataclass(frozen=True, slots=True)
class _BlockSource:
    source: str
    span: ast.Span
    line: int
    block_id: str | None
    stamp_offset: int | None
    stamp_separator: str = " "
    stamp_in_attributes: bool = False

    def fields(self) -> dict:
        return {
            "block_id": self.block_id,
            "line": self.line,
            "clean_source": self.source,
            "source_span": self.span,
            "stamp_offset": self.stamp_offset,
            "stamp_separator": self.stamp_separator,
            "stamp_in_attributes": self.stamp_in_attributes,
        }


@dataclass(frozen=True, slots=True)
class _CellSource:
    text: str
    span: ast.Span
    line: int


class _Parser:
    def __init__(
        self,
        source: str,
        path: Path | None,
        *,
        allow_unstamped_figures: bool,
        diagnostics: list[Diagnostic] | None,
    ) -> None:
        self.source = source
        self.path = path
        self.bounds = _line_bounds(source)
        self.allow_unstamped_figures = allow_unstamped_figures
        self.diagnostics = diagnostics
        self.env = {"protected_lines": _protected_directive_lines(source, self.bounds)}

    def error(
        self, code: str, line: int, block_id: str | None = None, **params: object
    ) -> OmmError:
        return OmmError(code, path=self.path, line=line, block_id=block_id, **params)

    def block_id(self, value: str, line: int) -> str:
        if _ID.fullmatch(value) is None:
            raise self.error("ERR_BLOCK_ID_INVALID", line, value)
        return value

    def check_math(self, latex: str, line: int, block_id: str | None) -> None:
        if any(command[0] == r"\tag" for command in _TEX_COMMAND.finditer(latex)):
            raise self.error("ERR_MATH_TAG_FORBIDDEN", line, block_id)

    def inlines(
        self,
        value: str,
        offsets: Sequence[int],
        owner: _BlockSource,
        *,
        env: dict | None = None,
    ) -> tuple[ast.Inline, ...]:
        root: list[ast.Inline] = []
        frames: list[tuple[Token, list[ast.Inline]]] = []

        def mapped(span: ast.Span) -> ast.Span:
            return ast.Span(offsets[span.start], offsets[span.end - 1] + 1)

        def at(span: ast.Span) -> int:
            return owner.line + owner.source.count("\n", 0, offsets[span.start])

        for token in _inline_tokens(value, self.env if env is None else env):
            raw = token.meta["span"]
            target = frames[-1][1] if frames else root
            if token.nesting == 1:
                frames.append((token, []))
                continue
            if token.nesting == -1:
                opening, children = frames.pop()
                target = frames[-1][1] if frames else root
                start = opening.meta["span"].start
                end = raw.end
                if token.type == "strong_close":
                    span = mapped(ast.Span(start - 1, end + 1))
                    node = ast.Strong(span, tuple(children))
                elif token.type == "em_close":
                    node = ast.Emphasis(mapped(ast.Span(start, end)), tuple(children))
                else:
                    node = ast.Link(
                        mapped(ast.Span(start, end)),
                        tuple(children),
                        str(opening.attrGet("href")),
                        opening.attrGet("title"),
                    )
                target.append(node)
                continue

            if token.type == "text" and not token.content:
                continue
            span = mapped(raw)
            match token.type:
                case "inline_math":
                    self.check_math(token.content, at(raw), owner.block_id)
                    node = ast.InlineMath(span, token.content)
                case "block_ref":
                    node = ast.BlockRef(span, token.meta["ids"][0])
                case "loc_ref":
                    node = ast.LocRef(span, *token.meta["ids"])
                case "invalid_ref":
                    raise self.error(
                        "ERR_REFERENCE_INVALID", at(raw), owner.block_id, value=token.content
                    )
                case "citation":
                    node = ast.Citation(span, token.meta["keys"], token.meta["parenthetical"])
                case "code_inline":
                    node = ast.CodeSpan(span, token.content)
                case "softbreak" | "hardbreak":
                    node = ast.LineBreak(span, token.type == "hardbreak")
                case "image":
                    node = ast.Image(
                        span, token.content, str(token.attrGet("src")), token.attrGet("title")
                    )
                case "subscript" | "superscript":
                    child = ast.Text(mapped(ast.Span(raw.start + 1, raw.end - 1)), token.content)
                    script = ast.Subscript if token.type == "subscript" else ast.Superscript
                    node = script(span, (child,))
                case _:
                    node = ast.Text(span, token.content)
            if isinstance(node, ast.Text) and target and isinstance(target[-1], ast.Text):
                last = target[-1]
                if last.span.end == node.span.start:
                    target[-1] = ast.Text(
                        ast.Span(last.span.start, node.span.end), last.value + node.value
                    )
                    continue
            target.append(node)
        return tuple(root)

    def tail(
        self,
        raw: str,
        start: int,
        line: int,
        *,
        required: bool,
        literal_tail: bool = False,
    ) -> _BlockSource:
        match = _SUFFIX.search(raw)
        block_id = None
        stamp_offset = None
        clean = raw
        if required:
            if match and not literal_tail and not _escaped(raw, match.start(1)):
                block_id = self.block_id(match[1][2:-1], line + raw.count("\n"))
                clean = raw[: match.start()]
                if not raw[raw.rfind("\n") + 1 : match.start()].strip() and clean.endswith("\n"):
                    clean = clean.removesuffix("\n")
            else:
                stamp_offset = start + len(raw.rstrip(" \t"))
        return _BlockSource(clean, ast.Span(start, start + len(raw)), line, block_id, stamp_offset)

    def suffix_attributes(
        self,
        value: str,
        line: int,
        block_id: str | None = None,
    ) -> tuple[str | None, dict[str, str | None]]:
        if not value:
            return None, {}
        if not value[0].isspace() or not value.strip().startswith("{"):
            raise self.error("ERR_BLOCK_ATTRIBUTES_INVALID", line, block_id)
        value = value.strip()
        if not value.endswith("}"):
            raise self.error("ERR_BLOCK_ATTRIBUTES_INVALID", line, block_id)
        remaining = value[1:-1]
        if remaining.startswith("#"):
            parts = remaining[1:].split(maxsplit=1)
            block_id = self.block_id(parts[0] if parts else "", line)
            remaining = parts[1] if len(parts) == 2 else ""
        elif not remaining:
            raise self.error("ERR_BLOCK_ATTRIBUTES_INVALID", line, block_id)
        attrs: dict[str, str | None] = {}
        while remaining:
            attr = _ATTRIBUTE.match(remaining)
            if attr is None or attr[1] in attrs:
                raise self.error("ERR_BLOCK_ATTRIBUTES_INVALID", line, block_id)
            name, val = attr.groups()
            attrs[name] = val
            remaining = remaining[attr.end() :]
            if remaining and not remaining[0].isspace():
                raise self.error("ERR_BLOCK_ATTRIBUTES_INVALID", line, block_id)
            remaining = remaining.lstrip()
        return block_id, attrs

    def markdown_block(
        self,
        tokens: Sequence[Token],
        *,
        list_marker: Token | None = None,
    ) -> ast.Block:
        token = tokens[0]
        first, after = _token_lines(token)
        start = self.bounds[first]
        end = min(len(self.source), self.bounds[after] - 1)
        raw = self.source[start:end].rstrip("\n")
        if token.type in _LIST_TYPES:
            while raw.rsplit("\n", 1)[-1].strip(" \t") == "":
                raw = raw.rsplit("\n", 1)[0]
        line = first + 1
        if token.type == "omm_display":
            if "close" not in token.meta:
                raise self.error("ERR_MATH_UNCLOSED", line)
            close = token.meta["close"] - start + 2
            block_id, attrs = self.suffix_attributes(raw[close:].rstrip(" \t"), after)
            if attrs.keys() - {"unnumbered"} or attrs.get("unnumbered") is not None:
                raise self.error("ERR_BLOCK_ATTRIBUTES_INVALID", line, block_id)
            self.check_math(token.content, line, block_id)
            stamp = None
            if block_id is None:
                stamp = start + (raw.index("{", close) + 1 if attrs else len(raw.rstrip(" \t")))
            clean = raw[:close] if block_id is not None or attrs else raw
            return ast.DisplayMath(
                block_id=block_id,
                line=line,
                clean_source=clean,
                source_span=ast.Span(start, start + len(raw)),
                stamp_offset=stamp,
                latex=token.content,
                unnumbered="unnumbered" in attrs,
                stamp_in_attributes=block_id is None and bool(attrs),
            )

        literal_tail = _literal_list_end(tokens) is not None
        addressable = token.type in _LIST_TYPES or token.type in {"paragraph_open", "heading_open"}
        owner = self.tail(raw, start, line, required=addressable, literal_tail=literal_tail)
        if list_marker is not None:
            marker_line, marker_after = _token_lines(list_marker)
            block_id = self.block_id(list_marker.content[2:-1], marker_line + 1)
            span = ast.Span(start, min(len(self.source), self.bounds[marker_after] - 1))
            owner = replace(owner, block_id=block_id, span=span, stamp_offset=None)
        elif literal_tail:
            marker = _LIST_PREFIX.match(raw)
            if marker is not None:
                owner = replace(owner, stamp_separator="\n" + marker[1])
        if token.type == "blockquote_open":
            return ast.MarkdownBlock(
                **owner.fields(), kind="blockquote", children=tuple(self.blocks(tokens[1:-1]))
            )
        nodes: list[ast.Inline] = []
        for inline in tokens:
            if inline.type == "omm_display":
                formula_line = _token_lines(inline)[0] + 1
                if "close" not in inline.meta:
                    raise self.error("ERR_MATH_UNCLOSED", formula_line, owner.block_id)
                self.check_math(inline.content, formula_line, owner.block_id)
            if inline.type != "inline":
                continue
            offsets = _map_inline(inline, self.source, self.bounds)
            count = bisect_right(offsets, start + len(owner.source) - 1)
            value = inline.content[:count].rstrip(" \t")
            if token.type == "heading_open" and token.markup.startswith("#"):
                value = re.sub(r"[ \t]+#+$", "", value)
            nodes.extend(self.inlines(value, tuple(pos - start for pos in offsets), owner))
        if token.type == "paragraph_open":
            return ast.Paragraph(**owner.fields(), inlines=tuple(nodes))
        if token.type == "heading_open":
            return ast.Heading(**owner.fields(), inlines=tuple(nodes), level=int(token.tag[1:]))
        if token.type in _LIST_TYPES:
            return ast.ListBlock(
                **owner.fields(), inlines=tuple(nodes), ordered=token.type == "ordered_list_open"
            )
        return ast.MarkdownBlock(**owner.fields(), kind=token.type)

    def body_inlines(self, owner: _BlockSource, start: int, end: int) -> tuple[ast.Inline, ...]:
        source = owner.source[start:end]
        bounds = _line_bounds(source)
        env = dict(self.env)
        result: list[ast.Inline] = []
        for token in _MARKDOWN.parse(source, env):
            if token.type == "inline":
                offsets = tuple(start + pos for pos in _map_inline(token, source, bounds))
                result.extend(self.inlines(token.content, offsets, owner, env=env))
            elif token.type == "omm_display":
                line = owner.line + owner.source.count("\n", 0, start) + _token_lines(token)[0]
                if "close" not in token.meta:
                    raise self.error("ERR_MATH_UNCLOSED", line, owner.block_id)
                self.check_math(token.content, line, owner.block_id)
        return tuple(result)

    def paragraphs(
        self,
        owner: _BlockSource,
        start: int,
        end: int,
        code: str,
    ) -> tuple[ast.Content, ...]:
        source = owner.source[start:end]
        bounds = _line_bounds(source)
        env = dict(self.env)
        tokens = _MARKDOWN.parse(source, env)
        result = []
        for token in tokens:
            if token.type not in {"paragraph_open", "inline", "paragraph_close"}:
                line = owner.line + owner.source.count("\n", 0, start)
                raise self.error(code, line, owner.block_id)
            if token.type == "inline":
                offsets = tuple(start + pos for pos in _map_inline(token, source, bounds))
                nodes = self.inlines(token.content, offsets, owner, env=env)
                result.append(ast.Content(ast.Span(offsets[0], offsets[-1] + 1), nodes))
        return tuple(result)

    def figure(
        self,
        owner: _BlockSource,
        start: int,
        end: int,
        cols: int | None,
    ) -> ast.FigureBlock:
        images: list[ast.Subfigure] = []
        cursor = start
        while cursor < end:
            stop = owner.source.find("\n", cursor, end)
            if stop == -1:
                stop = end
            raw = owner.source[cursor:stop]
            if not raw.strip() and not images:
                cursor = stop + 1
                continue
            if not raw.lstrip(" \t").startswith("!["):
                break
            absolute = owner.span.start + (cursor - start) + self._body_offset(owner)
            line = owner.line + owner.source.count("\n", 0, cursor)
            sub = self.tail(raw, absolute, line, required=True)
            text = sub.source.strip(" \t")
            relative = len(sub.source) - len(sub.source.lstrip(" \t"))
            nodes = self.inlines(text, range(relative, relative + len(text)), sub)
            if len(nodes) != 1 or not isinstance(nodes[0], ast.Image):
                raise self.error("ERR_FIGURE_INVALID", line, owner.block_id)
            image = nodes[0]
            try:
                asset_path = normalize_relative_path(unquote(image.destination))
            except OmmError as exc:
                raise self.error(
                    exc.code, line, sub.block_id or owner.block_id, **exc.diagnostic.params
                ) from exc
            images.append(
                ast.Subfigure(
                    **sub.fields(),
                    alt=image.alt,
                    asset_path=asset_path,
                )
            )
            cursor = stop + 1
        captions = self.paragraphs(owner, min(cursor, end), end, "ERR_FIGURE_INVALID")
        if not images or len(captions) != 1:
            raise self.error("ERR_FIGURE_INVALID", owner.line, owner.block_id)
        if len(images) == 1 and images[0].block_id is None:
            images[0] = replace(images[0], stamp_offset=None)
        elif not self.allow_unstamped_figures:
            for image in images:
                if image.block_id is None:
                    raise self.error("ERR_BLOCK_ID_MISSING", image.line)
        return ast.FigureBlock(
            **owner.fields(),
            inlines=captions[0].inlines,
            subfigures=tuple(images),
            caption=captions[0],
            cols=cols,
        )

    def _body_offset(self, owner: _BlockSource) -> int:
        return self.bounds[owner.line] - owner.span.start

    def split_cells(self, owner: _BlockSource, line: str, start: int) -> tuple[_CellSource, ...]:
        # CommonMark code spans and OMM math consume protected pipes atomically.
        protected = [
            token.meta["span"]
            for token in _inline_tokens(line, {})
            if token.type in {"code_inline", "inline_math"}
        ]
        masks = iter(protected)
        current = next(masks, None)
        separators: list[int] = []
        for pipe in re.finditer(r"\|", line):
            position = pipe.start()
            while current is not None and position >= current.end:
                current = next(masks, None)
            if (current is None or position < current.start) and not _escaped(line, position):
                separators.append(position)
        number = owner.line + owner.source.count("\n", 0, start)
        if (
            len(separators) < 2
            or line[: separators[0]].strip()
            or line[separators[-1] + 1 :].strip()
        ):
            raise self.error("ERR_TABLE_INVALID", number, owner.block_id)
        cells = []
        for left, right in pairwise(separators):
            raw = line[left + 1 : right]
            begin = start + left + 1 + len(raw) - len(raw.lstrip())
            text = raw.strip()
            cells.append(_CellSource(text, ast.Span(begin, begin + len(text)), number))
        return tuple(cells)

    def table_grid(
        self,
        owner: _BlockSource,
        rows: Sequence[tuple[_CellSource, ...]],
        midrules: Sequence[int] = (),
    ) -> tuple[tuple[ast.TableCell, ...], ...]:
        sizes: dict[tuple[int, int], tuple[int, int]] = {}
        previous: list[tuple[int, int]] = []
        breaks = set(midrules)
        for y, row in enumerate(rows):
            current: list[tuple[int, int]] = []
            x = 0
            while x < len(row):
                cell = row[x]
                if cell.text == "^":
                    if not previous or previous[x][1] != x:
                        raise self.error("ERR_TABLE_SPAN_INVALID", cell.line, owner.block_id)
                    primary = previous[x]
                    height, width = sizes[primary]
                    if any(c.text != "^" for c in row[x : x + width]):
                        raise self.error("ERR_TABLE_SPAN_INVALID", cell.line, owner.block_id)
                    sizes[primary] = (height + 1, width)
                elif cell.text == ">":
                    raise self.error("ERR_TABLE_SPAN_INVALID", cell.line, owner.block_id)
                else:
                    primary = (y, x)
                    width = 1
                    while x + width < len(row) and row[x + width].text == ">":
                        width += 1
                    sizes[primary] = (1, width)
                current.extend([primary] * width)
                x += width
            previous = [] if y in breaks else current

        def build(cell: _CellSource, y: int, x: int) -> ast.TableCell:
            height, width = sizes.get((y, x), (0, 0))
            nodes = (
                self.inlines(cell.text, range(cell.span.start, cell.span.end), owner)
                if height
                else ()
            )
            return ast.TableCell(cell.span, nodes, height, width, not height)

        return tuple(
            tuple(build(cell, y, x) for x, cell in enumerate(row)) for y, row in enumerate(rows)
        )

    def table(self, owner: _BlockSource, start: int, end: int) -> ast.TableBlock:
        rows = []
        table_start = table_end = None
        cursor = start
        for match in re.finditer(r"[^\n]*\n|[^\n]+$", owner.source[start:end]):
            line = match[0]
            if line.lstrip(" \t").startswith("|"):
                if table_end is not None and cursor != table_end:
                    raise self.error("ERR_TABLE_INVALID", owner.line, owner.block_id)
                if table_start is None:
                    table_start = cursor
                rows.append(self.split_cells(owner, line.removesuffix("\n"), cursor))
                table_end = cursor + len(line)
            cursor += len(line)
        if table_start is None or len(rows) < 3:
            raise self.error("ERR_TABLE_INVALID", owner.line, owner.block_id)
        width = len(rows[0])
        for row in rows:
            if len(row) != width:
                raise self.error(
                    "ERR_TABLE_COLUMNS_INVALID",
                    row[0].line,
                    owner.block_id,
                    expected=width,
                    actual=len(row),
                )
        captions = self.paragraphs(owner, start, table_start, "ERR_TABLE_INVALID")
        notes = self.paragraphs(owner, table_end, end, "ERR_TABLE_INVALID")
        if len(captions) != 1:
            raise self.error("ERR_TABLE_INVALID", owner.line, owner.block_id)
        separator = next(
            (
                index
                for index, row in enumerate(rows)
                if all(_ALIGN.fullmatch(cell.text) for cell in row)
            ),
            None,
        )
        if not separator or separator == len(rows) - 1:
            raise self.error("ERR_TABLE_INVALID", owner.line, owner.block_id)
        alignment: list[Literal["left", "center", "right"] | None] = []
        for cell in rows[separator]:
            left = cell.text.startswith(":")
            right = cell.text.endswith(":")
            alignment.append(
                "center" if left and right else "left" if left else "right" if right else None
            )
        body = []
        midrules = []
        for index, row in enumerate(rows[separator + 1 :], separator + 1):
            if all(_RULE.fullmatch(cell.text) for cell in row):
                if (
                    not body
                    or index == len(rows) - 1
                    or (midrules and midrules[-1] == len(body) - 1)
                ):
                    raise self.error("ERR_TABLE_INVALID", row[0].line, owner.block_id)
                midrules.append(len(body) - 1)
            else:
                body.append(row)
        header = self.table_grid(owner, rows[:separator])
        data = self.table_grid(owner, body, midrules)
        nodes = list(captions[0].inlines)
        nodes.extend(node for row in (*header, *data) for cell in row for node in cell.inlines)
        nodes.extend(node for note in notes for node in note.inlines)
        return ast.TableBlock(
            **owner.fields(),
            inlines=tuple(nodes),
            caption=captions[0],
            notes=notes,
            alignment=tuple(alignment),
            header=header,
            rows=data,
            midrule_after=tuple(midrules),
        )

    def closing(self, tokens: Sequence[Token], index: int) -> int:
        depth = 0
        for pos in range(index, len(tokens)):
            token = tokens[pos]
            if token.type != "omm_directive":
                continue
            depth += -1 if token.content == ":::" else 1
            if depth == 0:
                return pos
        token = tokens[index]
        raise self.error("ERR_DIRECTIVE_UNCLOSED", _token_lines(token)[0] + 1)

    def directive(
        self,
        opening: Token,
        body: Sequence[Token],
        closing: Token,
    ) -> ast.Block:
        match = _DIRECTIVE.fullmatch(opening.content)
        assert match is not None
        kind, tail = match.groups()
        first, after_head = _token_lines(opening)
        close_line, after = _token_lines(closing)
        line = first + 1
        block_id, attrs = self.suffix_attributes(tail, line)
        start = self.bounds[first]
        head_end = self.bounds[after_head] - 1
        end = min(len(self.source), self.bounds[after] - 1)
        clean_head_end = head_end
        if block_id is not None or attrs:
            # The directive header is metadata, but the fence itself is source.
            clean_head_end = start + len(self.source[start:head_end].rstrip(" \t")) - len(tail)
        clean = self.source[start:clean_head_end] + self.source[head_end:end]
        body_start = clean_head_end - start + 1
        body_end = self.bounds[close_line] - (head_end - clean_head_end) - start
        stamp = None
        if block_id is None:
            stamp = (
                self.source.index("{", clean_head_end, head_end) + 1
                if attrs
                else start + len(self.source[start:head_end].rstrip(" \t"))
            )
        owner = _BlockSource(
            clean,
            ast.Span(start, end),
            line,
            block_id,
            stamp,
            stamp_in_attributes=block_id is None and bool(attrs),
        )

        if kind in {"outline", "figure", "table"} and any(
            token.type == "omm_directive" for token in body
        ):
            raise self.error("ERR_DIRECTIVE_INVALID", line, block_id, kind=kind)

        match kind:
            case "outline":
                if attrs:
                    raise self.error("ERR_BLOCK_ATTRIBUTES_INVALID", line, block_id)
                return ast.OutlineBlock(
                    **owner.fields(), inlines=self.body_inlines(owner, body_start, body_end)
                )
            case "figure":
                cols = attrs.get("cols")
                if attrs and (
                    attrs.keys() != {"cols"}
                    or cols is None
                    or re.fullmatch(r"[1-9][0-9]{0,8}", cols) is None
                ):
                    raise self.error("ERR_BLOCK_ATTRIBUTES_INVALID", line, block_id)
                return self.figure(owner, body_start, body_end, int(cols) if cols else None)
            case "table":
                if attrs:
                    raise self.error("ERR_BLOCK_ATTRIBUTES_INVALID", line, block_id)
                return self.table(owner, body_start, body_end)
            case "review-item":
                value = attrs.get("reviewer")
                if attrs.keys() != {"reviewer"} or not value or not value.startswith('"'):
                    raise self.error("ERR_BLOCK_ATTRIBUTES_INVALID", line, block_id)
                reviewer = re.sub(r'\\(["\\])', r"\1", value[1:-1])
                if not reviewer.strip():
                    raise self.error("ERR_BLOCK_ATTRIBUTES_INVALID", line, block_id)
                if not body or body[0].type != "omm_directive" or body[0].content != ":::{comment}":
                    raise self.error("ERR_REVIEW_COMMENT_REQUIRED", line, block_id)
                comment_close = self.closing(body, 0)
                if any(token.type == "omm_directive" for token in body[1:comment_close]):
                    raise self.error("ERR_DIRECTIVE_INVALID", line, block_id, kind="comment")
                shift = head_end - clean_head_end + start
                comment_start = self.bounds[_token_lines(body[0])[1]] - shift
                comment_end = self.bounds[_token_lines(body[comment_close])[0]] - shift
                comment = ast.Content(
                    ast.Span(comment_start, comment_end),
                    self.body_inlines(owner, comment_start, comment_end),
                )
                reply = tuple(self.blocks(body[comment_close + 1 :]))
                refs = [
                    node
                    for block in ast.walk_blocks(reply)
                    for node in ast.walk_inlines(block.inlines)
                ]
                return ast.ReviewItem(
                    **owner.fields(),
                    reviewer=reviewer,
                    comment=comment,
                    reply=reply,
                    referenced_block_ids=tuple(
                        dict.fromkeys(
                            node.block_id for node in refs if isinstance(node, ast.BlockRef)
                        )
                    ),
                    location_refs=tuple(node for node in refs if isinstance(node, ast.LocRef)),
                )
            case _:
                raise self.error("ERR_DIRECTIVE_INVALID", line, block_id, kind=kind)

    def blocks(self, tokens: Sequence[Token]) -> Iterator[ast.Block]:
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.type == "omm_directive":
                closing = index
                try:
                    if token.content == ":::":
                        raise self.error(
                            "ERR_DIRECTIVE_UNEXPECTED_CLOSE", _token_lines(token)[0] + 1
                        )
                    closing = self.closing(tokens, index)
                    yield self.directive(token, tokens[index + 1 : closing], tokens[closing])
                except OmmError as exc:
                    if self.diagnostics is None:
                        raise
                    self.diagnostics.append(exc.diagnostic)
                    if exc.code == "ERR_DIRECTIVE_UNCLOSED":
                        return
                index = closing + 1
                continue
            end = index + 1
            if token.nesting == 1:
                while tokens[end].nesting != -1 or tokens[end].level != token.level:
                    end += 1
                end += 1
            group = tokens[index:end]
            list_end = _literal_list_end(group)
            marker = None
            if list_end is not None and end + 2 < len(tokens):
                following = tokens[end : end + 3]
                if (
                    following[0].type == "paragraph_open"
                    and following[1].type == "inline"
                    and _token_lines(following[1]) == (list_end, list_end + 1)
                    and _SUFFIX.fullmatch(following[1].content) is not None
                ):
                    marker = following[1]
                    end += 3
            try:
                yield self.markdown_block(group, list_marker=marker)
            except OmmError as exc:
                if self.diagnostics is None:
                    raise
                self.diagnostics.append(exc.diagnostic)
            index = end


def parse_source(
    source: str,
    path: Path | None = None,
    *,
    allow_unstamped_figures: bool = False,
    diagnostics: list[Diagnostic] | None = None,
) -> ast.Document:
    source = normalize_newlines(source)
    if "\0" in source:
        raise OmmError(
            "ERR_SOURCE_INVALID", path=path, line=source.count("\n", 0, source.index("\0")) + 1
        )
    parser = _Parser(
        source, path, allow_unstamped_figures=allow_unstamped_figures, diagnostics=diagnostics
    )
    tokens = _STRUCTURED.parse(source, parser.env)
    return ast.Document(path, source, tuple(parser.blocks(tokens)))


def parse_file(
    path: Path,
    *,
    allow_unstamped_figures: bool = False,
    diagnostics: list[Diagnostic] | None = None,
) -> ast.Document:
    return parse_source(
        read_text(path),
        path,
        allow_unstamped_figures=allow_unstamped_figures,
        diagnostics=diagnostics,
    )
