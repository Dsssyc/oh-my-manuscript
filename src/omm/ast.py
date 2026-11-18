"""Typed Structured Source nodes with Unicode code-point source intervals."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal


@dataclass(frozen=True, slots=True)
class Span:
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Inline:
    span: Span


@dataclass(frozen=True, slots=True)
class Text(Inline):
    value: str


@dataclass(frozen=True, slots=True)
class LineBreak(Inline):
    hard: bool


@dataclass(frozen=True, slots=True)
class InlineContainer(Inline):
    children: tuple[Inline, ...]


class Strong(InlineContainer):
    __slots__ = ()


class Emphasis(InlineContainer):
    __slots__ = ()


class Subscript(InlineContainer):
    __slots__ = ()


class Superscript(InlineContainer):
    __slots__ = ()


@dataclass(frozen=True, slots=True)
class Link(InlineContainer):
    destination: str
    title: str | None


@dataclass(frozen=True, slots=True)
class Image(Inline):
    alt: str
    destination: str
    title: str | None


@dataclass(frozen=True, slots=True)
class CodeSpan(Inline):
    value: str


@dataclass(frozen=True, slots=True)
class InlineMath(Inline):
    latex: str


@dataclass(frozen=True, slots=True)
class BlockRef(Inline):
    block_id: str


@dataclass(frozen=True, slots=True)
class LocRef(Inline):
    start_id: str
    end_id: str | None


@dataclass(frozen=True, slots=True)
class Citation(Inline):
    keys: tuple[str, ...]
    parenthetical: bool


def walk_inlines(nodes: tuple[Inline, ...]) -> Iterator[Inline]:
    for node in nodes:
        yield node
        if isinstance(node, InlineContainer):
            yield from walk_inlines(node.children)


@dataclass(frozen=True, slots=True, kw_only=True)
class Block:
    block_id: str | None
    line: int
    clean_source: str
    source_span: Span
    stamp_offset: int | None
    inlines: tuple[Inline, ...] = ()
    stamp_separator: str = " "
    stamp_in_attributes: bool = False
    id_prefix: ClassVar[str | None] = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Heading(Block):
    level: int
    id_prefix: ClassVar[str] = "sec"


class Paragraph(Block):
    __slots__ = ()
    id_prefix: ClassVar[str] = "p"


@dataclass(frozen=True, slots=True, kw_only=True)
class ListBlock(Block):
    ordered: bool
    id_prefix: ClassVar[str] = "li"


@dataclass(frozen=True, slots=True, kw_only=True)
class DisplayMath(Block):
    latex: str
    unnumbered: bool
    id_prefix: ClassVar[str] = "eq"


@dataclass(frozen=True, slots=True, kw_only=True)
class MarkdownBlock(Block):
    kind: str
    children: tuple[Block, ...] = ()


class OutlineBlock(Block):
    __slots__ = ()
    id_prefix: ClassVar[str] = "out"


@dataclass(frozen=True, slots=True)
class Content:
    span: Span
    inlines: tuple[Inline, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Subfigure(Block):
    alt: str
    asset_path: str
    id_prefix: ClassVar[str] = "fig"


@dataclass(frozen=True, slots=True, kw_only=True)
class FigureBlock(Block):
    subfigures: tuple[Subfigure, ...]
    caption: Content
    cols: int | None
    id_prefix: ClassVar[str] = "fig"


@dataclass(frozen=True, slots=True)
class TableCell:
    span: Span
    inlines: tuple[Inline, ...]
    row_span: int
    col_span: int
    covered: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class TableBlock(Block):
    caption: Content
    alignment: tuple[Literal["left", "center", "right"] | None, ...]
    header: tuple[tuple[TableCell, ...], ...]
    rows: tuple[tuple[TableCell, ...], ...]
    midrule_after: tuple[int, ...]
    notes: tuple[Content, ...]
    id_prefix: ClassVar[str] = "tbl"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewItem(Block):
    reviewer: str
    comment: Content
    reply: tuple[Block, ...]
    referenced_block_ids: tuple[str, ...]
    location_refs: tuple[LocRef, ...]
    id_prefix: ClassVar[str] = "review"


@dataclass(frozen=True, slots=True)
class Document:
    path: Path | None
    source: str
    blocks: tuple[Block, ...]


def walk_blocks(blocks: tuple[Block, ...]) -> Iterator[Block]:
    for block in blocks:
        yield block
        if isinstance(block, MarkdownBlock):
            yield from walk_blocks(block.children)
        elif isinstance(block, ReviewItem):
            yield from walk_blocks(block.reply)
        elif isinstance(block, FigureBlock):
            yield from walk_blocks(block.subfigures)
