"""Compile revision snapshots to deterministic WordprocessingML packages."""

from __future__ import annotations

import re
import struct
from collections.abc import Collection, Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from fractions import Fraction
from hashlib import sha256
from io import BytesIO
from itertools import count, pairwise
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

from lxml import etree

from omm import ast
from omm.diff import _plain_tokens
from omm.errors import OmmError
from omm.git_revs import RevisionSnapshot
from omm.numbering import NumberedWorkspace, number_workspace
from omm.omml import (
    OmmlConversionError,
    build_numbered_equation_paragraph,
    build_tracked_omml_replacement,
    latex_to_omml,
)
from omm.parser import _line_bounds, _markdown
from omm.workspace import DeliverableKind

_OFFICE = "http://schemas.openxmlformats.org"
_NAMESPACES = {
    "w": f"{_OFFICE}/wordprocessingml/2006/main",
    "m": f"{_OFFICE}/officeDocument/2006/math",
    "r": f"{_OFFICE}/officeDocument/2006/relationships",
    "wp": f"{_OFFICE}/drawingml/2006/wordprocessingDrawing",
    "a": f"{_OFFICE}/drawingml/2006/main",
    "pic": f"{_OFFICE}/drawingml/2006/picture",
    "ct": f"{_OFFICE}/package/2006/content-types",
    "rel": f"{_OFFICE}/package/2006/relationships",
    "cp": f"{_OFFICE}/package/2006/metadata/core-properties",
    "ep": f"{_OFFICE}/officeDocument/2006/extended-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xml": "http://www.w3.org/XML/1998/namespace",
}
_W = f"{{{_NAMESPACES['w']}}}"
_M = f"{{{_NAMESPACES['m']}}}"
_R = f"{{{_NAMESPACES['r']}}}"
_REL_TYPE = _NAMESPACES["r"] + "/"
_XML_SPACE = f"{{{_NAMESPACES['xml']}}}space"
_WIDTH = 9026
_TEXT_STYLES = {
    "Normal": ("Normal", 24),
    "Title": ("Title", 36),
    **{f"Heading{i}": (f"heading {i}", 30 - i * 2) for i in range(1, 7)},
    "DisplayedEquation": ("Displayed Equation", 24),
    "Caption": ("Caption", 22),
    "TableNote": ("Table Note", 20),
    "Bibliography": ("Bibliography", 22),
    "ReviewComment": ("Review Comment", 24),
}
_MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tif": "image/tiff",
}
_PART_TYPES = {
    "docProps/core.xml": "application/vnd.openxmlformats-package.core-properties+xml",
    "docProps/app.xml": "application/vnd.openxmlformats-officedocument.extended-properties+xml",
    "word/document.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    **{
        f"word/{part}.xml": f"application/vnd.openxmlformats-officedocument.wordprocessingml.{mime}+xml"
        for part, mime in (
            ("styles", "styles"),
            ("settings", "settings"),
            ("numbering", "numbering"),
            ("fontTable", "fontTable"),
        )
    },
}
_ROOT_TARGETS = {
    "word/document.xml": _REL_TYPE + "officeDocument",
    "docProps/core.xml": _NAMESPACES["rel"] + "/metadata/core-properties",
    "docProps/app.xml": _REL_TYPE + "extended-properties",
}
_WORD_TARGETS = {
    f"{name}.xml": _REL_TYPE + name
    for name in (
        "styles",
        "settings",
        "numbering",
        "fontTable",
    )
}
_BOOKMARK = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}")
_MEDIA_PATH = re.compile(r"media/image[1-9][0-9]*\.(?:png|jpg|gif|bmp|tif)")
_LINE_NUMBERS = {"countBy": 1, "start": 0, "restart": "continuous", "distance": 360}
_MARKS = (_W + "ins", _W + "del")


@dataclass(frozen=True, slots=True)
class DocxBlockRecord:
    block_id: str
    kind: str
    bookmark_name: str
    start_bookmark: str | None
    end_bookmark: str | None
    plain_text: str


@dataclass(frozen=True, slots=True)
class CompiledDocxDeliverable:
    name: str
    kind: DeliverableKind
    clean_path: Path
    marked_path: Path | None
    blocks: tuple[DocxBlockRecord, ...]


def _q(name: str) -> str:
    prefix, local = name.split(":")
    return f"{{{_NAMESPACES[prefix]}}}{local}"


def _node(
    name: str,
    parent: etree._Element | None = None,
    *,
    attrs: Mapping[str, str | int] | None = None,
    text: str | None = None,
) -> etree._Element:
    attributes = {
        _q(key) if ":" in key else key: str(value) for key, value in (attrs or {}).items()
    }
    if parent is None:
        prefix = name.split(":")[0]
        prefixes = ("w", "m", "r", "wp", "a", "pic") if name == "w:document" else (prefix,)
        node = etree.Element(
            _q(name),
            attributes,
            nsmap={
                key if key not in {"ct", "rel", "ep"} else None: _NAMESPACES[key]
                for key in prefixes
            },
        )
    else:
        node = etree.SubElement(parent, _q(name), attributes)
    node.text = text
    return node


def _w(tag: str, parent: etree._Element, **attrs: str | int) -> etree._Element:
    return _node("w:" + tag, parent, attrs={f"w:{key}": value for key, value in attrs.items()})


_RunProperties = tuple[tuple[str, str], ...]


def _run(value: str, properties: _RunProperties = ()) -> etree._Element:
    run = _node("w:r")
    if properties:
        pr = _w("rPr", run)
        for name, setting in sorted(
            dict(properties).items(),
            key=lambda x: ("rFonts", "b", "i", "color", "vertAlign").index(x[0]),
        ):
            if name == "rFonts":
                _w(name, pr, ascii=setting, hAnsi=setting)
            else:
                _w(name, pr, **({"val": setting} if setting else {}))
    _node("w:t", run, attrs={"xml:space": "preserve"}, text=value)
    return run


def _control_run(name: str) -> etree._Element:
    run = _node("w:r")
    _w(name, run)
    return run


def _paragraph(style: str = "Normal", *, align: str | None = None) -> etree._Element:
    paragraph = _node("w:p")
    pr = _w("pPr", paragraph)
    _w("pStyle", pr, val=style)
    if align is not None:
        _w("jc", pr, val=align)
    return paragraph


def _visible_text(node: etree._Element) -> str:
    text = (_W + "t", _M + "t")
    controls = {_W + "tab": "\t", _W + "br": "\n"}
    values = []
    for item in node.iter(*text, *controls):
        if item.tag in text:
            values.append(item.text or "")
        elif item.getparent().tag == _W + "r":
            values.append(controls[item.tag])
    return "".join(values)


class _Bookmarks:
    """Reserve names across both revisions, including Word's case-insensitive matches."""

    def __init__(self) -> None:
        self.names: dict[tuple[str, str], str] = {}
        self.used: set[str] = set()

    def name(self, purpose: str, key: str) -> str:
        identity = (purpose, key)
        if identity in self.names:
            return self.names[identity]
        prefix = re.sub(r"[^A-Za-z0-9_]", "_", key)
        digest = sha256(f"{purpose}\0{key}".encode()).hexdigest()
        stem = f"{purpose}_{prefix[: 20 - len(purpose)]}_{digest[:18]}"
        candidate = stem
        for suffix in count(1):
            if candidate.casefold() not in self.used:
                self.used.add(candidate.casefold())
                self.names[identity] = candidate
                return candidate
            tail = f"_{suffix:x}"
            candidate = stem[: 40 - len(tail)] + tail
        raise AssertionError("Unreachable")

    @staticmethod
    def point(parent: etree._Element, index: int, name: str) -> None:
        # IDs are allocated in final document order, after revision assembly.
        start = _node("w:bookmarkStart", attrs={"w:id": name, "w:name": name})
        end = _node("w:bookmarkEnd", attrs={"w:id": name})
        parent.insert(index, start)
        parent.insert(index + 1, end)

    @staticmethod
    def span(first: etree._Element, last: etree._Element, name: str) -> None:
        first.insert(1, _node("w:bookmarkStart", attrs={"w:id": name, "w:name": name}))
        _w("bookmarkEnd", last, id=name)


@dataclass(frozen=True, slots=True)
class _Picture:
    data: bytes
    extension: str
    width: Fraction
    height: Fraction


def _tiff(data: bytes, *, dimensions_required: bool = True) -> tuple[int, int, Fraction, Fraction]:
    order = {b"II": "<", b"MM": ">"}[data[:2]]
    magic, offset = struct.unpack_from(order + "HI", data, 2)
    if magic != 42:
        raise ValueError("Invalid TIFF header")
    (size,) = struct.unpack_from(order + "H", data, offset)
    tags: dict[int, int | Fraction] = {}
    for index in range(size):
        start = offset + 2 + index * 12
        tag, kind, items, value = struct.unpack_from(order + "HHII", data, start)
        if items != 1 or tag not in {256, 257, 282, 283, 296}:
            continue
        if kind == 3:
            (tags[tag],) = struct.unpack_from(order + "H", data, start + 8)
        elif kind == 4:
            tags[tag] = value
        elif kind == 5:
            tags[tag] = Fraction(*struct.unpack_from(order + "II", data, value))
    unit = {1: Fraction(0), 2: Fraction(1), 3: Fraction(127, 50)}[tags.get(296, 2)]
    width, height = (tags[256], tags[257]) if dimensions_required else (0, 0)
    return int(width), int(height), unit * tags.get(282, 0), unit * tags.get(283, 0)


def _jpeg(data: bytes) -> tuple[int, int, Fraction, Fraction]:
    frame_tags = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    width = height = 0
    dpi = Fraction(0), Fraction(0)
    offset = 2
    while offset < len(data):
        if data[offset] != 0xFF:
            raise ValueError("Invalid JPEG marker")
        while data[offset] == 0xFF:
            offset += 1
        marker = data[offset]
        offset += 1
        if marker in {0xDA, 0xD9}:
            break
        (length,) = struct.unpack_from(">H", data, offset)
        segment = data[offset + 2 : offset + length]
        if length < 2 or len(segment) != length - 2:
            raise ValueError("Truncated JPEG segment")
        offset += length
        if marker in frame_tags:
            height, width = struct.unpack_from(">HH", segment, 1)
        elif marker == 0xE0 and segment.startswith(b"JFIF\0"):
            units, x, y = struct.unpack_from(">BHH", segment, 7)
            scale = {0: Fraction(0), 1: Fraction(1), 2: Fraction(127, 50)}[units]
            if not any(dpi):
                dpi = x * scale, y * scale
        elif marker == 0xE1 and segment.startswith(b"Exif\0\0"):
            # Exif IFD0 may omit the dimensions carried by the JPEG frame.
            _, _, x, y = _tiff(segment[6:], dimensions_required=False)
            if x and y:
                dpi = x, y
    return width, height, *dpi


def _bitmap(data: bytes, asset: str) -> _Picture:
    x_dpi = y_dpi = Fraction(0)
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            extension = "png"
            if data[12:16] != b"IHDR":
                raise ValueError("Missing PNG header")
            width, height = struct.unpack_from(">II", data, 16)
            offset = 8
            while offset + 12 <= len(data):
                size, tag = struct.unpack_from(">I4s", data, offset)
                if tag == b"pHYs":
                    x, y, unit = struct.unpack_from(">IIB", data, offset + 8)
                    if unit == 1:
                        x_dpi, y_dpi = Fraction(x * 127, 5000), Fraction(y * 127, 5000)
                    break
                if tag in {b"IDAT", b"IEND"}:
                    break
                offset += size + 12
        elif data.startswith(b"\xff\xd8"):
            extension = "jpg"
            width, height, x_dpi, y_dpi = _jpeg(data)
        elif data.startswith((b"GIF87a", b"GIF89a")):
            extension = "gif"
            width, height = struct.unpack_from("<HH", data, 6)
        elif data.startswith(b"BM"):
            extension = "bmp"
            (header,) = struct.unpack_from("<I", data, 14)
            if header == 12:
                width, height = struct.unpack_from("<HH", data, 18)
            elif header >= 40:
                width, height = struct.unpack_from("<ii", data, 18)
                height = abs(height)
                x, y = struct.unpack_from("<ii", data, 38)
                x_dpi, y_dpi = Fraction(x * 127, 5000), Fraction(y * 127, 5000)
            else:
                raise ValueError("Unsupported BMP information header")
        elif data.startswith((b"II", b"MM")):
            extension = "tif"
            width, height, x_dpi, y_dpi = _tiff(data)
        else:
            raise ValueError("Unknown bitmap signature")
        if min(width, height) <= 0 or min(x_dpi, y_dpi) < 0:
            raise ValueError("Invalid bitmap dimensions")
    except (ValueError, KeyError, IndexError, struct.error, ZeroDivisionError) as exc:
        raise OmmError("ERR_FIGURE_FORMAT_UNSUPPORTED", asset=asset) from exc
    return _Picture(
        data,
        extension,
        width * 914400 / (x_dpi or Fraction(96)),
        height * 914400 / (y_dpi or Fraction(96)),
    )


def _drawing(picture: _Picture, key: str, alt: str, width_twips: int) -> etree._Element:
    scale = min(1, width_twips * 635 / picture.width, 6 * 914400 / picture.height)
    size = {"cx": max(1, round(picture.width * scale)), "cy": max(1, round(picture.height * scale))}
    run = _node("w:r")
    inline = _node("wp:inline", _w("drawing", run))
    _node("wp:extent", inline, attrs=size)
    _node("wp:docPr", inline, attrs={"id": 1, "name": alt or "Figure", "descr": alt})
    graphic = _node("a:graphic", inline)
    data = _node("a:graphicData", graphic, attrs={"uri": _NAMESPACES["pic"]})
    pic = _node("pic:pic", data)
    nv = _node("pic:nvPicPr", pic)
    _node("pic:cNvPr", nv, attrs={"id": 0, "name": alt or "Figure"})
    _node("pic:cNvPicPr", nv)
    fill = _node("pic:blipFill", pic)
    _node("a:blip", fill, attrs={"r:embed": key})
    _node("a:fillRect", _node("a:stretch", fill))
    shape = _node("pic:spPr", pic)
    transform = _node("a:xfrm", shape)
    _node("a:off", transform, attrs={"x": 0, "y": 0})
    _node("a:ext", transform, attrs=size)
    geometry = _node("a:prstGeom", shape, attrs={"prst": "rect"})
    _node("a:avLst", geometry)
    return run


def _border(name: str, parent: etree._Element, weight: int) -> None:
    _w(name, parent, val="single", sz=weight, color="000000")


def _table(
    block: ast.TableBlock,
) -> tuple[etree._Element, list[tuple[etree._Element, ast.TableCell]]]:
    columns = len(block.alignment)
    width, remainder = divmod(_WIDTH, columns)
    edges = [index * width + min(index, remainder) for index in range(columns + 1)]
    table = _node("w:tbl")
    pr = _w("tblPr", table)
    _w("tblW", pr, w=_WIDTH, type="dxa")
    borders = _w("tblBorders", pr)
    _border("top", borders, 12)
    _w("left", borders, val="nil")
    _border("bottom", borders, 12)
    for side in ("right", "insideH", "insideV"):
        _w(side, borders, val="nil")
    _w("tblLayout", pr, type="fixed")
    grid = _w("tblGrid", table)
    for index in range(columns):
        _w("gridCol", grid, w=edges[index + 1] - edges[index])

    paragraphs = []
    header_rows = len(block.header)
    active: dict[int, tuple[int, int]] = {}
    for row_index, row in enumerate((*block.header, *block.rows)):
        tr = _w("tr", table)
        if row_index < header_rows:
            _w("tblHeader", _w("trPr", tr))
        col = 0
        while col < columns:
            logical = row[col]
            if logical.covered:
                end_row, span = active[col]
                if row_index == end_row:
                    del active[col]
            else:
                end_row, span = row_index + logical.row_span - 1, logical.col_span
                if logical.row_span > 1:
                    active[col] = end_row, span
            cell = _w("tc", tr)
            pr = _w("tcPr", cell)
            _w("tcW", pr, w=edges[col + span] - edges[col], type="dxa")
            if span > 1:
                _w("gridSpan", pr, val=span)
            if logical.covered:
                _w("vMerge", pr)
            elif logical.row_span > 1:
                _w("vMerge", pr, val="restart")
            weight = 0
            if row_index == header_rows - 1:
                weight = 6
            elif row_index in range(header_rows - 1) and end_row == row_index and span > 1:
                lower = block.header[row_index + 1][col]
                if lower.covered or lower.col_span != span:
                    weight = 4
            elif row_index - header_rows in block.midrule_after:
                weight = 4
            if weight:
                _border("bottom", _w("tcBorders", pr), weight)
            paragraph = _paragraph(align=block.alignment[col])
            cell.append(paragraph)
            paragraphs.append((paragraph, logical))
            col += span
    return table, paragraphs


@dataclass(slots=True)
class _Group:
    key: tuple[str, ...]
    nodes: list[etree._Element]
    shape: object = None
    is_heading: bool = False


@dataclass(slots=True)
class _Document:
    groups: list[_Group] = field(default_factory=list)
    records: list[DocxBlockRecord] = field(default_factory=list)
    formulas: dict[etree._Element, tuple[str, str | None]] = field(default_factory=dict)
    pictures: dict[str, _Picture] = field(default_factory=dict)
    lists: dict[str, tuple[bool, int]] = field(default_factory=dict)
    links: dict[str, str] = field(default_factory=dict)


def _format_for(node: ast.Inline) -> _RunProperties:
    if isinstance(node, ast.Strong):
        return (("b", ""),)
    if isinstance(node, ast.Emphasis):
        return (("i", ""),)
    if isinstance(node, ast.Subscript):
        return (("vertAlign", "subscript"),)
    if isinstance(node, ast.Superscript):
        return (("vertAlign", "superscript"),)
    return ()


class _Render:
    def __init__(
        self,
        snapshot: RevisionSnapshot,
        numbered: NumberedWorkspace,
        name: str,
        bookmarks: _Bookmarks,
        loc_labels: Mapping[tuple[str, str | None], str],
    ) -> None:
        self.snapshot = snapshot
        self.numbered = numbered
        self.name = name
        self.deliverable = snapshot.workspace.deliverables[name]
        self.bookmarks = bookmarks
        self.loc_labels = loc_labels
        self.model = _Document()
        self.color = snapshot.workspace.style.link_color[1:].upper()
        self.local = {
            block_id
            for block_id, entry in snapshot.blocks.items()
            if name in entry.deliverables and not isinstance(entry.block, ast.OutlineBlock)
        }
        self.labels = {
            block_id: number
            for group in numbered.block_numbers.values()
            for block_id, number in group.items()
        } | numbered.block_numbers[name]
        if snapshot.workspace.style.bibliography_placement == "per-deliverable":
            self.references = {
                key: entry
                for key, entry in numbered.references.items()
                if name in entry.deliverable_counts
            }
        elif self.deliverable.kind == "manuscript":
            self.references = {
                key: entry for key, entry in numbered.references.items() if entry.cited
            }
        else:
            self.references = {}

    def _reference(
        self,
        label: str,
        anchor: str | None,
        properties: _RunProperties,
    ) -> etree._Element:
        run = _run(label, (*properties, ("color", self.color)))
        if anchor is None:
            return run
        link = _node("w:hyperlink", attrs={"w:anchor": anchor})
        link.append(run)
        return link

    def _citation(
        self,
        node: ast.Citation,
        properties: _RunProperties,
    ) -> Iterator[etree._Element]:
        numeric = self.numbered.citation_style == "numeric"
        opening, ending = ("[", "]") if numeric else ("(", ")") if node.parenthetical else ("", "")
        separator = ", " if numeric else "; "
        for index, key in enumerate(node.keys):
            single = ast.Citation(node.span, (key,), node.parenthetical)
            label = self.numbered.citation_label(single)
            if opening:
                label = label[1:-1]
            if index == 0:
                label = opening + label
            if index == len(node.keys) - 1:
                label += ending
            else:
                label += separator
            anchor = self.bookmarks.name("Ref", key) if key in self.references else None
            yield self._reference(label, anchor, properties)

    def _inlines(
        self,
        nodes: IterableInlines,
        owner: ast.Block,
        properties: _RunProperties = (),
        *,
        quoted: bool = False,
    ) -> Iterator[etree._Element]:
        for node in nodes:
            if isinstance(node, ast.Text):
                yield from (_run(token, properties) for token in _plain_tokens(node.value))
            elif isinstance(node, ast.LineBreak):
                yield _control_run("br") if node.hard else _run(" ", properties)
            elif isinstance(node, ast.CodeSpan):
                yield _run(node.value, (*properties, ("rFonts", "Courier New")))
            elif isinstance(node, ast.Image):
                yield _run(node.alt, properties)
            elif isinstance(node, ast.InlineMath):
                equation = latex_to_omml(node.latex, block_id=owner.block_id)
                self.model.formulas[equation] = (node.latex, owner.block_id)
                yield equation
            elif quoted and isinstance(node, (ast.BlockRef, ast.LocRef, ast.Citation)):
                yield _run(owner.clean_source[node.span.start : node.span.end], properties)
            elif isinstance(node, ast.BlockRef):
                number = self.labels.get(node.block_id)
                anchor = (
                    self.bookmarks.name("Block", node.block_id)
                    if node.block_id in self.local
                    else None
                )
                yield self._reference(number.label if number else node.block_id, anchor, properties)
            elif isinstance(node, ast.LocRef):
                key = (node.start_id, node.end_id)
                label = (
                    self.loc_labels[key]
                    if key in self.loc_labels
                    else self.numbered.locations[key].label(node)
                )
                yield self._reference(label, None, properties)
            elif isinstance(node, ast.Citation):
                yield from self._citation(node, properties)
            elif isinstance(node, ast.Link):
                target = sha256(node.destination.encode()).hexdigest()
                self.model.links[target] = node.destination
                link = _node("w:hyperlink", attrs={"r:id": target})
                link.extend(self._inlines(node.children, owner, properties, quoted=quoted))
                yield link
            elif isinstance(node, ast.InlineContainer):
                yield from self._inlines(
                    node.children,
                    owner,
                    (*properties, *_format_for(node)),
                    quoted=quoted,
                )
            else:
                raise TypeError(type(node))

    def _text(
        self,
        value: str,
        style: str = "Normal",
    ) -> etree._Element:
        paragraph = _paragraph(style)
        paragraph.extend(_run(token) for token in _plain_tokens(value))
        return paragraph

    def _content(self, content: ast.Content, style: str, block: ast.Block) -> etree._Element:
        paragraph = _paragraph(style)
        paragraph.extend(self._inlines(content.inlines, block))
        return paragraph

    def _caption(self, block: ast.FigureBlock | ast.TableBlock) -> etree._Element:
        paragraph = self._content(block.caption, "Caption", block)
        number = self.labels.get(block.block_id)
        if number is not None:
            paragraph.insert(1, _run(f"{number.label}. "))
        return paragraph

    def _figure(self, block: ast.FigureBlock) -> list[etree._Element]:
        panels = block.subfigures
        cols = min(block.cols or len(panels), len(panels))
        width = _WIDTH // cols
        paragraphs = []
        for start in range(0, len(panels), cols):
            row = panels[start : start + cols]
            paragraph = _paragraph(align="center" if cols == 1 else None)
            if cols > 1:
                tabs = _w("tabs", paragraph[0])
                for column in range(len(row)):
                    _w("tab", tabs, val="center", pos=width * column + width // 2)
            for panel in row:
                if cols > 1:
                    paragraph.append(_control_run("tab"))
                data = self.snapshot.assets[panel.asset_path]
                key = sha256(data).hexdigest()
                if key not in self.model.pictures:
                    self.model.pictures[key] = _bitmap(data, panel.asset_path)
                label = None
                if panel.block_id is not None:
                    self.bookmarks.point(
                        paragraph, len(paragraph), self.bookmarks.name("Block", panel.block_id)
                    )
                    number = self.labels.get(panel.block_id)
                    label = number.sublabel if number is not None else None
                label_width = min(340, width // 4) if label else 0
                paragraph.append(
                    _drawing(self.model.pictures[key], key, panel.alt, width - label_width)
                )
                if label:
                    paragraph.append(_run(f" {label}"))
            paragraphs.append(paragraph)
        return [*paragraphs, self._caption(block)]

    def _list(self, block: ast.ListBlock) -> list[etree._Element]:
        tokens = _markdown(directives=True).parse(block.clean_source)
        bounds = _line_bounds(block.clean_source)
        list_ids = []
        items = []
        paragraphs = []

        def first_paragraph(style: str = "Normal") -> etree._Element:
            paragraph = _paragraph(style)
            if not items[-1]:
                items[-1] = True
                num = _w("numPr", paragraph[0])
                _w("ilvl", num, val=min(len(list_ids) - 1, 8))
                _w("numId", num, val=list_ids[-1])
            paragraphs.append(paragraph)
            return paragraph

        for index, token in enumerate(tokens):
            if token.type in {"bullet_list_open", "ordered_list_open"}:
                if items and not items[-1]:
                    first_paragraph()
                ordered = token.type == "ordered_list_open"
                start = token.attrGet("start")
                start = 1 if start is None else int(start)
                key = f"{block.block_id}:{index}:{int(ordered)}:{start}"
                self.model.lists[key] = ordered, start
                list_ids.append(key)
            elif token.type in {"bullet_list_close", "ordered_list_close"}:
                list_ids.pop()
            elif token.type == "list_item_open":
                items.append(False)
            elif token.type == "list_item_close":
                if not items[-1]:
                    first_paragraph()
                items.pop()
            elif items and token.type in {
                "paragraph_open", "heading_open", "omm_display", "fence", "code_block"
            }:
                style = f"Heading{token.tag[1:]}" if token.type == "heading_open" else "Normal"
                paragraph = first_paragraph(style)
                first, last = token.map
                if token.type == "omm_display":
                    paragraph.append(
                        latex_to_omml(token.content, display=True, block_id=block.block_id)
                    )
                elif token.type in {"fence", "code_block"}:
                    for line in token.content.rstrip("\n").split("\n"):
                        if len(paragraph) > 1:
                            paragraph.append(_control_run("br"))
                        paragraph.append(_run(line, (("rFonts", "Courier New"),)))
                else:
                    paragraph.extend(
                        self._inlines(
                            (
                                node
                                for node in block.inlines
                                if bounds[first] <= node.span.start < bounds[last]
                            ),
                            block,
                        )
                    )
        return paragraphs

    def _block(
        self,
        block: ast.Block,
        source: str,
        occurrence: str,
        *,
        review_id: str | None = None,
    ) -> None:
        if isinstance(block, ast.OutlineBlock):
            return
        block_id = block.block_id
        key = (
            (source, block_id)
            if block_id is not None and review_id is None
            else (source, review_id or "", block_id or occurrence)
        )
        shape: object = type(block)
        kind = None
        if isinstance(block, ast.DisplayMath):
            label = self.labels.get(block_id)
            number = label.number if label is not None else None
            equation = build_numbered_equation_paragraph(block.latex, number, block_id=block_id)
            if number is not None:
                self.model.formulas[equation[1]] = (block.latex, block_id)
            nodes, kind = [equation], "display_math"
            shape = type(block), number is not None, block.latex if number is None else None
        elif isinstance(block, ast.ListBlock):
            nodes, kind = self._list(block), "list"
            shape = (
                type(block),
                tuple(_signature(node[0]) for node in nodes),
                tuple(
                    self.model.lists[num.get(_W + "val")]
                    for node in nodes
                    for num in node.findall(f"{_W}pPr/{_W}numPr/{_W}numId")
                ),
            )
        elif isinstance(block, ast.Heading):
            paragraph = _paragraph(f"Heading{block.level}")
            if block_id in self.labels:
                paragraph.append(_run(f"{self.labels[block_id].number} "))
            paragraph.extend(self._inlines(block.inlines, block))
            nodes, kind = [paragraph], "heading"
            shape = type(block), block.level
        elif isinstance(block, ast.Paragraph):
            paragraph = _paragraph()
            paragraph.extend(self._inlines(block.inlines, block))
            nodes, kind = [paragraph], "paragraph"
        elif isinstance(block, ast.TableBlock):
            table, paragraphs = _table(block)
            for paragraph, cell in paragraphs:
                paragraph.extend(self._inlines(cell.inlines, block))
            nodes = [self._caption(block), table]
            nodes.extend(self._content(note, "TableNote", block) for note in block.notes)
            shape = (
                type(block),
                len(block.header),
                block.alignment,
                block.midrule_after,
                tuple(
                    tuple((cell.row_span, cell.col_span, cell.covered) for cell in row)
                    for row in (*block.header, *block.rows)
                ),
            )
        elif isinstance(block, ast.FigureBlock):
            nodes = self._figure(block)
            shape = type(block), block.cols, tuple(panel.block_id for panel in block.subfigures)
        elif isinstance(block, ast.ReviewItem):
            heading = self._text(f"{block.reviewer} ({block_id})", "Heading2")
            comment = _paragraph("ReviewComment")
            comment.extend(self._inlines(block.comment.inlines, block, quoted=True))
            nodes = [heading, comment]
        elif isinstance(block, ast.MarkdownBlock):
            if block.children:
                for index, child in enumerate(block.children):
                    self._block(child, source, f"{occurrence}.{index}", review_id=review_id)
                return
            if block.kind in {"fence", "code_block"}:
                code = next(
                    token.content
                    for token in _markdown().parse(block.clean_source)
                    if token.type == block.kind
                ).rstrip("\n")
                nodes = [_paragraph()]
                for index, line in enumerate(code.split("\n")):
                    if index:
                        nodes[0].append(_control_run("br"))
                    nodes[0].append(_run(line, (("rFonts", "Courier New"),)))
            else:
                nodes = [self._text(block.clean_source)]
        else:
            raise TypeError(type(block))

        if block_id is not None:
            name = self.bookmarks.name("Block", block_id)
            if kind is not None:
                start = self.bookmarks.name("Start", block_id)
                end = self.bookmarks.name("End", block_id)
                self.bookmarks.span(nodes[0], nodes[-1], name)
                self.bookmarks.point(nodes[0], 2, start)
                self.bookmarks.point(nodes[-1], len(nodes[-1]) - 1, end)
                self.model.records.append(
                    DocxBlockRecord(
                        block_id,
                        kind,
                        name,
                        start,
                        end,
                        "\n".join(_visible_text(node) for node in nodes),
                    )
                )
            else:
                # Table captions precede the table and figure captions follow the panels.
                marker = nodes[-1] if isinstance(block, ast.FigureBlock) else nodes[0]
                self.bookmarks.point(marker, 1, name)
        self.model.groups.append(
            _Group(key, nodes, shape, is_heading=isinstance(block, ast.Heading))
        )
        if isinstance(block, ast.ReviewItem):
            for index, child in enumerate(block.reply):
                self._block(child, source, str(index), review_id=block_id)

    def build(self) -> _Document:
        title = self._text(self.snapshot.workspace.title, "Title")
        self.model.groups.append(_Group(("title",), [title]))
        authors = self.snapshot.workspace.authors
        if authors:
            names = (
                authors
                if isinstance(authors, str)
                else ", ".join(author.name for author in authors)
            )
            self.model.groups.append(_Group(("authors",), [self._text(names)]))
        for source in self.deliverable.sources:
            for index, block in enumerate(self.snapshot.documents[source].blocks):
                self._block(block, source, str(index))
        if self.references:
            self.model.groups.append(
                _Group(("references",), [self._text("References", "Heading1")])
            )
        for key, reference in self.references.items():
            prefix = f"[{reference.number}] " if reference.number is not None else ""
            paragraph = self._text(prefix + reference.formatted_text, "Bibliography")
            self.bookmarks.point(paragraph, 1, self.bookmarks.name("Ref", key))
            self.model.groups.append(_Group(("citation", key), [paragraph]))
        return self.model


IterableInlines = Sequence[ast.Inline] | Iterator[ast.Inline]


def _signature(node: etree._Element) -> tuple:
    return node.tag, tuple(sorted(node.attrib.items())), node.text, tuple(map(_signature, node))


def _ordered_pairs(
    before: list[_Group],
    after: list[_Group],
) -> Iterator[tuple[_Group | None, _Group | None]]:
    # With unique keys, align an increasing subsequence of old positions.
    # A Fenwick tree keeps prefix maxima in O(log n). For equal total weight,
    # prefer the earliest old endpoint, also preserving the unweighted LCS tie-break.
    positions = {group.key: index for index, group in enumerate(before)}
    empty = (0, 0, -1)
    prefix_best = [empty] * (len(before) + 1)
    best = empty
    predecessors: dict[int, tuple[int, int]] = {}
    for new_index, group in enumerate(after):
        old_index = positions.get(group.key)
        if old_index is None:
            continue
        previous = empty
        position = old_index
        while position:
            previous = max(previous, prefix_best[position])
            position -= position & -position
        # Prefer retaining a heading when a single paragraph crosses it.
        weight = 2 if group.is_heading and before[old_index].is_heading else 1
        match = (previous[0] + weight, -old_index, new_index)
        predecessors[new_index] = old_index, previous[2]
        best = max(best, match)
        position = old_index + 1
        while position < len(prefix_best):
            prefix_best[position] = max(prefix_best[position], match)
            position += position & -position

    retained = []
    new_index = best[2]
    while new_index != -1:
        old_index, previous_index = predecessors[new_index]
        retained.append((old_index, new_index))
        new_index = previous_index
    old_start = new_start = 0
    for old_end, new_end in (*reversed(retained), (len(before), len(after))):
        yield from ((group, None) for group in before[old_start:old_end])
        yield from ((None, group) for group in after[new_start:new_end])
        if old_end < len(before):
            yield before[old_end], after[new_end]
        old_start, new_start = old_end + 1, new_end + 1


def _whole_pair(before: _Group | None, after: _Group | None) -> bool:
    return before is None or after is None or before.shape != after.shape


def _alias_old_bookmarks(
    pairs: list[tuple[_Group | None, _Group | None]],
    bookmarks: _Bookmarks,
) -> None:
    new_names = {
        node.get(_W + "name")
        for _, group in pairs
        if group is not None
        for root in group.nodes
        for node in root.iter(_W + "bookmarkStart")
    }
    aliases = {
        name: bookmarks.name("Old", name)
        for old, new in pairs
        if old is not None and _whole_pair(old, new)
        for root in old.nodes
        for node in root.iter(_W + "bookmarkStart")
        if (name := node.get(_W + "name")) in new_names
    }
    if not aliases:
        return
    for old, _ in pairs:
        if old is None:
            continue
        for root in old.nodes:
            for node in root.iter(_W + "bookmarkStart", _W + "bookmarkEnd", _W + "hyperlink"):
                attr = {
                    _W + "bookmarkStart": "name",
                    _W + "bookmarkEnd": "id",
                    _W + "hyperlink": "anchor",
                }[node.tag]
                if (key := node.get(_W + attr)) in aliases:
                    node.set(_W + attr, aliases[key])
                    if node.tag == _W + "bookmarkStart":
                        node.set(_W + "id", aliases[key])


class _Redline:
    def __init__(
        self,
        before: _Document,
        after: _Document,
        author: str,
        date: str,
    ) -> None:
        self.before = before
        self.after = after
        self.author = author
        self.date = date
        self.next_id = 1

    def _marker(self, op: str) -> etree._Element:
        marker = _node(
            "w:" + op,
            attrs={
                "w:id": self.next_id,
                "w:author": self.author,
                "w:date": self.date,
            },
        )
        self.next_id += 1
        return marker

    def _run_change(self, op: str, nodes: Sequence[etree._Element]) -> etree._Element:
        marker = self._marker(op)
        marker.extend(deepcopy(node) for node in nodes)
        if op == "del":
            for item in marker.iter(_W + "t"):
                item.tag = _W + "delText"
                item.set(_XML_SPACE, "preserve")
        return marker

    def _wrap(self, op: str, nodes: Sequence[etree._Element]) -> Iterator[etree._Element]:
        # CT_Hyperlink can contain CT_RunTrackChange. CT_RunTrackChange
        # cannot contain a hyperlink, even in a wholly revised paragraph.
        start = 0
        for index, node in enumerate(nodes):
            if node.tag != _W + "hyperlink":
                continue
            if start < index:
                yield self._run_change(op, nodes[start:index])
            hyperlink = etree.Element(node.tag, attrib=dict(node.attrib))
            hyperlink.extend(self._wrap(op, node[:]))
            yield hyperlink
            start = index + 1
        if start < len(nodes):
            yield self._run_change(op, nodes[start:])

    def whole(self, source: etree._Element, op: str) -> etree._Element:
        node = deepcopy(source)
        for row in node.iter(_W + "tr"):
            pr = row.find(_W + "trPr")
            if pr is None:
                pr = _node("w:trPr")
                row.insert(0, pr)
            # CT_TrPr places row revisions after the ordinary row properties.
            pr.append(self._marker(op))
        for paragraph in node.iter(_W + "p"):
            pr = paragraph[0]
            mark_properties = _w("rPr", pr)
            mark_properties.append(self._marker(op))
            content = paragraph[1:]
            del paragraph[1:]
            paragraph.extend(self._wrap(op, content))
        return node

    def _replace_run_span(
        self,
        old: list[etree._Element],
        new: list[etree._Element],
    ) -> Iterator[etree._Element]:
        previous = current = 0
        before_math = (index for index, node in enumerate(old) if node in self.before.formulas)
        after_math = (index for index, node in enumerate(new) if node in self.after.formulas)
        for i, j in zip(before_math, after_math):
            if previous < i:
                yield from self._wrap("del", old[previous:i])
            if current < j:
                yield from self._wrap("ins", new[current:j])
            before, block_id = self.before.formulas[old[i]]
            after, _ = self.after.formulas[new[j]]
            deleted, inserted, self.next_id = build_tracked_omml_replacement(
                before,
                after,
                rev_id_start=self.next_id,
                author=self.author,
                date_utc=self.date,
                block_id=block_id,
            )
            yield deleted
            yield inserted
            previous, current = i + 1, j + 1
        if previous < len(old):
            yield from self._wrap("del", old[previous:])
        if current < len(new):
            yield from self._wrap("ins", new[current:])

    def _paragraph(self, old: etree._Element, new: etree._Element) -> etree._Element:
        result = _node("w:p")
        result.append(deepcopy(new[0]))
        before, after = old[1:], new[1:]
        matcher = SequenceMatcher(
            None,
            list(map(_signature, before)),
            list(map(_signature, after)),
            autojunk=False,
        )
        for op, i, j, k, end in matcher.get_opcodes():
            if op == "equal":
                result.extend(deepcopy(node) for node in after[k:end])
            else:
                result.extend(self._replace_run_span(before[i:j], after[k:end]))
        return result

    def _table(self, old: etree._Element, new: etree._Element) -> etree._Element:
        result = deepcopy(new)
        before = old.iter(_W + "p")
        for old_p, new_p, p in zip(
            before, new.iter(_W + "p"), list(result.iter(_W + "p")), strict=True
        ):
            p.getparent().replace(p, self._paragraph(old_p, new_p))
        return result

    def group(
        self,
        old: _Group | None,
        new: _Group | None,
    ) -> Iterator[etree._Element]:
        if _whole_pair(old, new):
            if old is not None:
                yield from (self.whole(node, "del") for node in old.nodes)
            if new is not None:
                yield from (self.whole(node, "ins") for node in new.nodes)
            return
        for before, after in zip(old.nodes, new.nodes):
            if after.tag == _W + "tbl":
                yield self._table(before, after)
            else:
                yield self._paragraph(before, after)
        yield from (self.whole(node, "del") for node in old.nodes[len(new.nodes) :])
        yield from (self.whole(node, "ins") for node in new.nodes[len(old.nodes) :])


def _project(document: etree._Element, *, accept: bool) -> etree._Element:
    result = deepcopy(document)
    keep, drop = _MARKS if accept else _MARKS[::-1]
    for table in reversed(list(result.iter(_W + "tbl"))):
        for row in table.findall(_W + "tr"):
            pr = row.find(_W + "trPr")
            if pr is None:
                continue
            if pr.find(drop) is not None:
                table.remove(row)
                continue
            mark = pr.find(keep)
            if mark is not None:
                pr.remove(mark)
            if not len(pr):
                row.remove(pr)
        if table.find(_W + "tr") is None:
            table.getparent().remove(table)

    revised_links = set()
    for mark in reversed(list(result.iter(*_MARKS))):
        parent = mark.getparent()
        if parent.tag == _W + "rPr" and parent.getparent().tag == _W + "pPr":
            continue
        revised_links.update(mark.iterancestors(_W + "hyperlink"))
        if mark.tag == keep:
            index = parent.index(mark)
            for child in list(mark):
                parent.insert(index, child)
                index += 1
        parent.remove(mark)

    for link in reversed(list(result.iter(_W + "hyperlink"))):
        if link in revised_links and not len(link):
            link.getparent().remove(link)

    if not accept:
        for deleted_text in result.iter(_W + "delText"):
            deleted_text.tag = _W + "t"

    for paragraph in reversed(list(result.iter(_W + "p"))):
        pr = paragraph.find(_W + "pPr")
        if pr is None:
            continue
        props = pr.find(_W + "rPr")
        if props is None:
            continue
        removed = props.find(drop)
        retained = props.find(keep)
        if retained is not None:
            props.remove(retained)
        if removed is None:
            if not len(props):
                pr.remove(props)
            continue
        props.remove(removed)
        if not len(props):
            pr.remove(props)
        contents = list(paragraph)[1:]
        if contents:
            following = paragraph.getnext()
            if following is None or following.tag != _W + "p":
                continue
            start = int(following.find(_W + "pPr") is not None)
            for index, item in enumerate(contents, start):
                following.insert(index, item)
        paragraph.getparent().remove(paragraph)
    return result


def accept_docx_revisions(document_xml: etree._Element) -> etree._Element:
    """Return a copy with inserted content retained and deleted content removed."""
    return _project(document_xml, accept=True)


def reject_docx_revisions(document_xml: etree._Element) -> etree._Element:
    """Return a copy with deleted content restored and inserted content removed."""
    return _project(document_xml, accept=False)


def _styles() -> etree._Element:
    styles = _node("w:styles")
    defaults = _w("docDefaults", styles)
    run = _w("rPr", _w("rPrDefault", defaults))
    _w("rFonts", run, ascii="Times New Roman", hAnsi="Times New Roman", cs="Times New Roman")
    _w("sz", run, val=24)
    paragraph = _w("pPr", _w("pPrDefault", defaults))
    _w("spacing", paragraph, after=120, line=360, lineRule="auto")
    for style_id, (name, size) in _TEXT_STYLES.items():
        attrs = {"default": 1} if style_id == "Normal" else {}
        style = _w("style", styles, type="paragraph", styleId=style_id, **attrs)
        _w("name", style, val=name)
        if style_id != "Normal":
            _w("basedOn", style, val="Normal")
            _w("next", style, val="Normal")
        pr = _w("pPr", style)
        heading = style_id.startswith("Heading")
        if heading or style_id == "Title":
            _w("keepNext", pr)
        if style_id == "Bibliography":
            _w("ind", pr, left=360, hanging=360)
        if style_id == "Title":
            _w("jc", pr, val="center")
        if heading:
            _w("outlineLvl", pr, val=int(style_id[-1]) - 1)
        run = _w("rPr", style)
        if heading or style_id == "Title":
            _w("b", run)
        if style_id == "ReviewComment":
            _w("i", run)
        _w("sz", run, val=size)
    return styles


def _settings(marked: bool) -> etree._Element:
    settings = _node("w:settings")
    _w("proofState", settings, spelling="clean", grammar="clean")
    if marked:
        _w("trackRevisions", settings)
    _w("defaultTabStop", settings, val=720)
    _w("compat", settings)
    math = _node("m:mathPr", settings)
    _node("m:mathFont", math, attrs={"m:val": "Cambria Math"})
    return settings


def _numbering(lists: Collection[tuple[bool, int]]) -> etree._Element:
    root = _node("w:numbering")
    for abstract_id, ordered in enumerate((False, True)):
        abstract = _w("abstractNum", root, abstractNumId=abstract_id)
        _w("multiLevelType", abstract, val="multilevel")
        for level in range(9):
            lvl = _w("lvl", abstract, ilvl=level)
            _w("start", lvl, val=1)
            _w("numFmt", lvl, val="decimal" if ordered else "bullet")
            _w("lvlText", lvl, val=f"%{level + 1}." if ordered else "•")
            _w("lvlJc", lvl, val="left")
            pr = _w("pPr", lvl)
            _w("ind", pr, left=720 + 360 * level, hanging=360)
    for num_id, (ordered, start) in enumerate(lists, 1):
        num = _w("num", root, numId=num_id)
        _w("abstractNumId", num, val=int(ordered))
        if start != 1:
            for level in range(9):
                override = _w("lvlOverride", num, ilvl=level)
                _w("startOverride", override, val=start)
    return root


def _font_table() -> etree._Element:
    root = _node("w:fonts")
    for font in ("Times New Roman", "Cambria Math", "Courier New"):
        _w("font", root, name=font)
    return root


def _section(body: etree._Element, line_numbers: bool) -> None:
    section = _w("sectPr", body)
    _w("pgSz", section, w=11906, h=16838)
    _w(
        "pgMar",
        section,
        top=1440,
        right=1440,
        bottom=1440,
        left=1440,
        header=720,
        footer=720,
        gutter=0,
    )
    if line_numbers:
        _w("lnNumType", section, **_LINE_NUMBERS)
    _w("cols", section, space=720)
    _w("docGrid", section, linePitch=360)


def _relationship(
    relationships: etree._Element,
    relationship_type: str,
    target: str,
    *,
    external: bool = False,
) -> str:
    rid = f"rId{len(relationships) + 1}"
    mode = {"TargetMode": "External"} if external else {}
    _node(
        "rel:Relationship",
        relationships,
        attrs={
            "Id": rid,
            "Type": relationship_type,
            "Target": target,
            **mode,
        },
    )
    return rid


def _package(
    nodes: Iterator[etree._Element],
    snapshot: RevisionSnapshot,
    kind: DeliverableKind,
    models: Sequence[_Document],
    *,
    marked: bool,
) -> bytes:
    document = _node("w:document")
    body = _w("body", document)
    body.extend(deepcopy(node) for node in nodes)
    _section(body, kind != "response_letter")
    core = _node("cp:coreProperties")
    _node("dc:title", core, text=snapshot.workspace.title)
    authors = snapshot.workspace.authors
    _node(
        "dc:creator",
        core,
        text=authors if isinstance(authors, str) else ", ".join(author.name for author in authors),
    )
    _node("cp:category", core, text=kind)
    app = _node("ep:Properties")
    _node("ep:Application", app, text="oh-my-manuscript")
    content_types = _node("ct:Types")
    _node(
        "ct:Default",
        content_types,
        attrs={
            "Extension": "rels",
            "ContentType": "application/vnd.openxmlformats-package.relationships+xml",
        },
    )
    _node("ct:Default", content_types, attrs={"Extension": "xml", "ContentType": "application/xml"})
    for part, content_type in _PART_TYPES.items():
        _node(
            "ct:Override",
            content_types,
            attrs={"PartName": "/" + part, "ContentType": content_type},
        )
    package_relationships = _node("rel:Relationships")
    for target, relation in _ROOT_TARGETS.items():
        _relationship(package_relationships, relation, target)
    word_relationships = _node("rel:Relationships")
    for target, relation in _WORD_TARGETS.items():
        _relationship(word_relationships, relation, target)

    pictures = {key: value for model in models for key, value in model.pictures.items()}
    links = {key: value for model in models for key, value in model.links.items()}
    lists = {key: value for model in models for key, value in model.lists.items()}
    list_ids: dict[str, int] = {}
    media = {}
    extensions = set()
    for index, inline in enumerate(document.iter(_q("wp:inline")), 1):
        inline.find(_q("wp:docPr")).set("id", str(index))
        blip = inline.find(".//" + _q("a:blip"))
        picture = pictures[blip.get(_R + "embed")]
        extensions.add(picture.extension)
        target = f"media/image{index}.{picture.extension}"
        media[f"word/{target}"] = picture.data
        rid = _relationship(word_relationships, _REL_TYPE + "image", target)
        blip.set(_R + "embed", rid)
    for extension in sorted(extensions):
        _node(
            "ct:Default",
            content_types,
            attrs={
                "Extension": extension,
                "ContentType": _MEDIA_TYPES[extension],
            },
        )
    for link in document.iter(_W + "hyperlink"):
        if key := link.get(_R + "id"):
            rid = _relationship(
                word_relationships, _REL_TYPE + "hyperlink", links[key], external=True
            )
            link.set(_R + "id", rid)
    for num in document.iter(_W + "numId"):
        key = num.get(_W + "val")
        index = list_ids.setdefault(key, len(list_ids) + 1)
        num.set(_W + "val", str(index))
    ids = {}
    for index, start in enumerate(document.iter(_W + "bookmarkStart"), 1):
        ids[start.get(_W + "id")] = str(index)
        start.set(_W + "id", str(index))
    for end in document.iter(_W + "bookmarkEnd"):
        end.set(_W + "id", ids[end.get(_W + "id")])
    xml = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": package_relationships,
        "docProps/core.xml": core,
        "docProps/app.xml": app,
        "word/document.xml": document,
        "word/_rels/document.xml.rels": word_relationships,
        "word/styles.xml": _styles(),
        "word/settings.xml": _settings(marked),
        "word/numbering.xml": _numbering([lists[key] for key in list_ids]),
        "word/fontTable.xml": _font_table(),
    }
    parts = {
        name: etree.tostring(node, encoding="UTF-8", xml_declaration=True)
        for name, node in xml.items()
    } | media
    archive = BytesIO()
    with ZipFile(archive, "w", compression=ZIP_DEFLATED, compresslevel=9) as output:
        for name in sorted(parts):
            entry = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            output.writestr(entry, parts[name], compress_type=ZIP_DEFLATED, compresslevel=9)
    return archive.getvalue()


def compile_docx_workspace(
    snapshot: RevisionSnapshot,
    out_dir: Path,
    *,
    milestone: RevisionSnapshot | None = None,
    loc_labels: Mapping[tuple[str, str | None], str] | None = None,
    deliverables: Collection[str] | None = None,
    revision_author: str = "oh-my-manuscript",
    revision_date_utc: str = "2026-01-01T00:00:00Z",
) -> dict[str, CompiledDocxDeliverable]:
    """Build and validate the requested Word deliverables from immutable source snapshots."""
    selected = snapshot.workspace.deliverables
    if deliverables is not None:
        for name in sorted(set(deliverables) - selected.keys()):
            raise KeyError(name)
        selected = {name: entry for name, entry in selected.items() if name in deliverables}
    for name in selected:
        if PurePosixPath(name).name != name or name in {".", ".."} or "\\" in name:
            raise OmmError("ERR_MANIFEST_INVALID", field=f"deliverables.{name}")

    numbered = number_workspace(snapshot)
    previous = number_workspace(milestone) if milestone is not None else None
    labels = loc_labels if loc_labels is not None else {}
    bookmarks = _Bookmarks()
    result = {}
    try:
        for name, deliverable in selected.items():
            kind = deliverable.kind
            target = _Render(snapshot, numbered, name, bookmarks, labels).build()
            clean = _package(
                (node for group in target.groups for node in group.nodes),
                snapshot,
                kind,
                [target],
                marked=False,
            )
            verify_docx_package(clean)
            clean_path = out_dir / f"{name}.clean.docx"
            marked_path = None
            if (
                milestone is not None
                and name in milestone.workspace.deliverables
                and kind in {"manuscript", "supplementary"}
            ):
                before = _Render(milestone, previous, name, bookmarks, labels).build()
                pairs = list(_ordered_pairs(before.groups, target.groups))
                _alias_old_bookmarks(pairs, bookmarks)
                redline = _Redline(before, target, revision_author, revision_date_utc)
                marked = _package(
                    (node for pair in pairs for node in redline.group(*pair)),
                    snapshot,
                    kind,
                    [before, target],
                    marked=True,
                )
                verify_docx_package(marked)
                marked_path = out_dir / f"{name}.marked.docx"
            out_dir.mkdir(parents=True, exist_ok=True)
            clean_path.write_bytes(clean)
            if marked_path is not None:
                marked_path.write_bytes(marked)
            result[name] = CompiledDocxDeliverable(
                name,
                kind,
                clean_path,
                marked_path,
                tuple(target.records),
            )
    except OmmlConversionError as exc:
        raise OmmError(exc.code, block_id=exc.block_id, detail=exc.message) from exc
    return result


def _require(condition: object, detail: str) -> None:
    if not condition:
        raise OmmError("ERR_DOCX_PACKAGE_INVALID", detail=detail)


def _uint(value: str | None, context: str, *, minimum: int = 0, maximum: int = 2147483646) -> int:
    _require(value is not None and re.fullmatch(r"[0-9]{1,10}", value), f"Invalid {context}")
    number = int(value)
    _require(minimum <= number <= maximum, f"Out-of-range {context}")
    return number


def _children(node: etree._Element, *tags: str) -> bool:
    return [child.tag for child in node] == [_q(tag) for tag in tags]


def _order(node: etree._Element, prefix: str, names: str) -> None:
    allowed = {f"{{{_NAMESPACES[prefix]}}}{tag}": i for i, tag in enumerate(names.split())}
    indexes = [allowed.get(child.tag, -1) for child in node]
    _require(
        all(index >= 0 for index in indexes) and indexes == sorted(set(indexes)),
        f"Invalid or out-of-order children in {etree.QName(node).localname}",
    )


def _content_types(types: etree._Element, parts: set[str]) -> None:
    defaults: dict[str, str] = {}
    overrides: dict[str, str] = {}
    for entry in types:
        if entry.tag == _q("ct:Default"):
            name, destination = entry.get("Extension"), defaults
        else:
            _require(entry.tag == _q("ct:Override"), "Unknown content type declaration")
            name, destination = entry.get("PartName"), overrides
        mime = entry.get("ContentType")
        _require(
            name and name not in destination and mime and not len(entry),
            "Invalid content type entry",
        )
        destination[name] = mime
    expected = {"/" + part: mime for part, mime in _PART_TYPES.items()}
    _require(overrides == expected, "Missing or incorrect OPC content type overrides")
    media_extensions = {
        PurePosixPath(part).suffix[1:] for part in parts if part.startswith("word/media/")
    }
    _require(
        defaults
        == {
            "rels": "application/vnd.openxmlformats-package.relationships+xml",
            "xml": "application/xml",
            **{extension: _MEDIA_TYPES[extension] for extension in media_extensions},
        },
        "Missing or incorrect OPC content type defaults",
    )


def _relationships(
    root: etree._Element,
    expected: dict[str, str],
    parts: set[str],
    *,
    word: bool,
) -> dict[str, tuple[str, str]]:
    relationships = {}
    fixed = {}
    media = set()
    for rel in root:
        _require(
            rel.tag == _q("rel:Relationship") and not len(rel), "Invalid OPC relationship element"
        )
        rid, kind, target = (rel.get(key) for key in ("Id", "Type", "Target"))
        mode = rel.get("TargetMode")
        _require(
            rid
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", rid)
            and rid not in relationships
            and kind
            and target,
            "Missing or duplicate OPC relationship fields",
        )
        if word and kind == _REL_TYPE + "hyperlink":
            _require(
                mode == "External"
                and not re.search(r"[\x00-\x20]", target)
                and urlsplit(unquote(target)).scheme.lower()
                not in {"javascript", "data", "vbscript"},
                "Invalid external hyperlink relationship",
            )
        elif word and kind == _REL_TYPE + "image":
            _require(
                mode is None
                and _MEDIA_PATH.fullmatch(target)
                and f"word/{target}" in parts
                and target not in media,
                f"Invalid image relationship {rid}",
            )
            media.add(target)
        else:
            _require(
                mode is None and expected.get(target) == kind and target not in fixed,
                f"Unexpected internal relationship {rid} to {target}",
            )
            fixed[target] = kind
        relationships[rid] = kind, target
    _require(fixed == expected, "Missing required OPC relationships")
    if word:
        _require(
            {f"word/{name}" for name in media}
            == {name for name in parts if name.startswith("word/media/")},
            "Image parts and relationships differ",
        )
    return relationships


def _verify_table(table: etree._Element) -> None:
    _require(
        len(table) >= 3
        and _children(table[0], "w:tblW", "w:tblBorders", "w:tblLayout")
        and [child.tag for child in table[:2]] == [_W + "tblPr", _W + "tblGrid"],
        "Invalid table properties or missing table rows",
    )
    borders = table[0][1]
    _require(
        _children(borders, "w:top", "w:left", "w:bottom", "w:right", "w:insideH", "w:insideV")
        and all(
            borders[index].get(_W + "sz") == "12" and borders[index].get(_W + "val") == "single"
            for index in (0, 2)
        )
        and all(borders[index].get(_W + "val") == "nil" for index in (1, 3, 4, 5)),
        "Invalid three-line table borders",
    )
    grid = table[1]
    _require(
        len(grid) and all(column.tag == _W + "gridCol" for column in grid), "Invalid table grid"
    )
    widths = [_uint(col.get(_W + "w"), "column width", minimum=1) for col in grid]
    previous = {}
    for row in table[2:]:
        _require(row.tag == _W + "tr", "Invalid table row")
        start = 0
        if len(row) and row[0].tag == _W + "trPr":
            _order(row[0], "w", "tblHeader ins del")
            _require(
                not all(row[0].find(mark) is not None for mark in _MARKS),
                "Conflicting row revisions",
            )
            start = 1
        column = 0
        active = {}
        for cell in row[start:]:
            _require(
                cell.tag == _W + "tc"
                and len(cell) >= 2
                and cell[0].tag == _W + "tcPr"
                and all(p.tag == _W + "p" for p in cell[1:]),
                "Invalid table cell",
            )
            props = cell[0]
            _order(props, "w", "tcW gridSpan vMerge tcBorders")
            border = props.find(_W + "tcBorders")
            if border is not None:
                _require(
                    _children(border, "w:bottom")
                    and border[0].get(_W + "sz") in {"4", "6"}
                    and border[0].get(_W + "val") == "single",
                    "Invalid table cell border",
                )
            width = props.find(_W + "tcW")
            span = props.find(_W + "gridSpan")
            span = 1 if span is None else _uint(span.get(_W + "val"), "grid span", minimum=2)
            _require(
                column + span <= len(grid)
                and width is not None
                and width.get(_W + "type") == "dxa"
                and width.get(_W + "w") == str(sum(widths[column : column + span])),
                "Table cell does not match the grid",
            )
            merge = props.find(_W + "vMerge")
            if merge is not None:
                value = merge.get(_W + "val")
                _require(
                    value == "restart"
                    or (value in {None, "continue"} and previous.get(column) == span),
                    "Invalid vertical merge continuation",
                )
                active[column] = span
            column += span
        _require(column == len(grid), "Table row spans do not match the grid")
        previous = active


_DATE_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z")
_RUN_CHILDREN = {
    _W + tag for tag in ("r", "hyperlink", "bookmarkStart", "bookmarkEnd", "ins", "del")
}
_RUN_CHILDREN.update({_M + "oMath", _M + "oMathPara"})
_REVISION_CHILDREN = _RUN_CHILDREN - {*_MARKS, _W + "hyperlink"}


def _verify_picture(inline: etree._Element) -> etree._Element:
    _require(
        _children(inline, "wp:extent", "wp:docPr", "a:graphic")
        and _children(inline[2], "a:graphicData")
        and _children(inline[2][0], "pic:pic")
        and inline[2][0].get("uri") == _NAMESPACES["pic"],
        "Invalid inline drawing children",
    )
    picture = inline[2][0][0]
    _require(
        _children(picture, "pic:nvPicPr", "pic:blipFill", "pic:spPr")
        and _children(picture[0], "pic:cNvPr", "pic:cNvPicPr")
        and _children(picture[1], "a:blip", "a:stretch")
        and _children(picture[1][1], "a:fillRect")
        and _children(picture[2], "a:xfrm", "a:prstGeom")
        and _children(picture[2][0], "a:off", "a:ext")
        and _children(picture[2][1], "a:avLst"),
        "Invalid DrawingML picture",
    )
    for key, context in (("cx", "drawing width"), ("cy", "drawing height")):
        extent = inline[0].get(key)
        _uint(extent, context, minimum=1)
        _require(extent == picture[2][0][1].get(key), "Inconsistent drawing extents")
    _require(inline[1].get("name") and picture[0][0].get("name"), "Missing drawing name")
    return picture[1][0]


def _verify_document(
    document: etree._Element,
    marked: bool,
    line_numbered: bool,
    styles: set[str],
    lists: set[str],
    relationships: dict[str, tuple[str, str]],
) -> None:
    _require(_children(document, "w:body"), "Expected exactly one document body")
    body = document[0]
    _require(
        len(body)
        and body[-1].tag == _W + "sectPr"
        and all(node.tag in {_W + "p", _W + "tbl"} for node in body[:-1]),
        "Invalid document body",
    )
    section = body[-1]
    _order(section, "w", "pgSz pgMar lnNumType cols docGrid")
    _require(
        all(section.find(_W + tag) is not None for tag in ("pgSz", "pgMar", "cols", "docGrid")),
        "Missing section layout settings",
    )
    line_numbers = section.find(_W + "lnNumType")
    _require((line_numbers is not None) == line_numbered, "Incorrect document line numbering")
    if line_numbers is not None:
        _require(
            line_numbers.attrib == {_W + k: str(v) for k, v in _LINE_NUMBERS.items()},
            "Invalid continuous line numbering",
        )
    for first, second in pairwise(body):
        _require(
            first.tag != _W + "tbl" or second.tag != _W + "tbl", "Adjacent tables have no paragraph"
        )
    for table in body.iter(_W + "tbl"):
        _verify_table(table)

    revision_ids = set()
    starts = {}
    names = set()
    ends = set()
    anchors = set()
    used = set()
    last_drawing = 0
    for node in body.iter():
        tag = node.tag
        if tag == _W + "p":
            _require(
                not len(node) or node[0].tag == _W + "pPr" or node[0].tag in _RUN_CHILDREN,
                "Invalid paragraph content",
            )
            content = node[1:] if len(node) and node[0].tag == _W + "pPr" else node[:]
            _require(
                all(child.tag in _RUN_CHILDREN for child in content), "Invalid paragraph children"
            )
        elif tag == _W + "pPr":
            _order(
                node,
                "w",
                "pStyle keepNext numPr pBdr tabs spacing ind jc outlineLvl rPr sectPr pPrChange",
            )
        elif tag == _W + "pStyle":
            _require(node.get(_W + "val") in styles, "Unknown paragraph style")
        elif tag == _W + "numPr":
            _require(
                _children(node, "w:ilvl", "w:numId") and node[1].get(_W + "val") in lists,
                "Invalid numbering reference",
            )
            _uint(node[0].get(_W + "val"), "numbering level", maximum=8)
        elif tag in _MARKS:
            rid = _uint(node.get(_W + "id"), "revision ID")
            date = node.get(_W + "date")
            _require(
                marked
                and rid not in revision_ids
                and node.get(_W + "author")
                and date
                and _DATE_UTC.fullmatch(date),
                "Invalid or duplicate revision metadata",
            )
            datetime.fromisoformat(date)
            revision_ids.add(rid)
            parent = node.getparent()
            if parent.tag == _W + "rPr":
                _require(
                    parent.getparent().tag == _W + "pPr"
                    and parent[0] is node
                    and not len(node)
                    and sum(child.tag in _MARKS for child in parent) == 1,
                    "Invalid paragraph-mark revision",
                )
            elif parent.tag == _W + "trPr":
                _require(not len(node), "Row revision must be empty")
            else:
                _require(
                    parent.tag in {_W + "p", _W + "hyperlink"}
                    and all(child.tag in _REVISION_CHILDREN for child in node),
                    "Invalid run revision content",
                )
        elif tag in {_W + "t", _W + "delText"}:
            deleted = next(node.iterancestors(_W + "del"), None) is not None
            _require(
                not len(node)
                and node.getparent().tag == _W + "r"
                and (tag == _W + "delText") == deleted,
                "Invalid text inside revision",
            )
            if deleted:
                _require(
                    node.get(_XML_SPACE) == "preserve", "Deletion text must preserve whitespace"
                )
        elif tag == _W + "r":
            parent = node.getparent()
            if parent.tag in {_W + "p", _W + "hyperlink", *_MARKS}:
                children = node[1:] if len(node) and node[0].tag == _W + "rPr" else node[:]
                _require(
                    children
                    and all(
                        child.tag
                        in {_W + name for name in ("t", "delText", "tab", "br", "drawing")}
                        for child in children
                    ),
                    "Invalid run children",
                )
        elif tag == _W + "rPr" and node.getparent().tag == _W + "r":
            _order(node, "w", "rFonts b i color vertAlign")
        elif tag == _W + "bookmarkStart":
            name = node.get(_W + "name")
            bid = _uint(node.get(_W + "id"), "bookmark start ID")
            _require(
                name
                and _BOOKMARK.fullmatch(name)
                and name.casefold() not in names
                and bid not in starts
                and not len(node),
                "Invalid or duplicate bookmark start",
            )
            starts[bid] = name
            names.add(name.casefold())
        elif tag == _W + "bookmarkEnd":
            bid = _uint(node.get(_W + "id"), "bookmark end ID")
            _require(bid in starts and bid not in ends and not len(node), "Unpaired bookmark end")
            ends.add(bid)
        elif tag == _W + "hyperlink":
            anchor = node.get(_W + "anchor")
            rid = node.get(_R + "id")
            _require(
                (anchor is None) != (rid is None)
                and all(child.tag in _RUN_CHILDREN for child in node),
                "Invalid hyperlink content or target",
            )
            if anchor is not None:
                anchors.add(anchor)
            else:
                _require(
                    relationships.get(rid, (None,))[0] == _REL_TYPE + "hyperlink",
                    "Missing hyperlink relationship",
                )
                used.add(rid)
        elif tag == _W + "color":
            color = node.get(_W + "val")
            _require(color and re.fullmatch(r"[0-9A-Fa-f]{6}", color), "Invalid run color")
        elif tag == _M + "oMathPara":
            _require(_children(node, "m:oMath"), "Invalid display equation structure")
        elif tag == _M + "oMath":
            _require(
                next(node.iterancestors(_M + "oMath"), None) is None
                and all(etree.QName(child).namespace == _NAMESPACES["m"] for child in node),
                "Invalid OMML equation structure",
            )
        elif tag == _M + "rPr":
            _order(node, "m", "lit nor scr sty brk aln")
            if node.find(_M + "nor") is not None:
                _require(
                    all(node.find(_M + name) is None for name in ("scr", "sty")),
                    "Math run normal text cannot specify a script or style",
                )
        elif tag == _W + "drawing":
            _require(_children(node, "wp:inline"), "Invalid inline drawing")
        elif tag == _q("wp:inline"):
            blip = _verify_picture(node)
            drawing = _uint(node[1].get("id"), "drawing ID", minimum=1)
            _require(drawing > last_drawing, "Drawing IDs must be strictly increasing")
            last_drawing = drawing
            rid = blip.get(_R + "embed")
            _require(
                relationships.get(rid, (None,))[0] == _REL_TYPE + "image" and rid not in used,
                "Missing or reused image relationship",
            )
            used.add(rid)

    _require(starts.keys() == ends, "Unpaired bookmark start")
    _require(anchors <= set(starts.values()), "Internal hyperlink has no bookmark")
    _require(
        used
        == {
            rid
            for rid, (kind, _) in relationships.items()
            if kind in {_REL_TYPE + "image", _REL_TYPE + "hyperlink"}
        },
        "Unreferenced document relationship",
    )


def _verify_parts(archive: ZipFile) -> None:
    parts = set(archive.namelist())
    fixed = {
        "[Content_Types].xml": "ct:Types",
        "_rels/.rels": "rel:Relationships",
        "docProps/core.xml": "cp:coreProperties",
        "docProps/app.xml": "ep:Properties",
        "word/document.xml": "w:document",
        "word/_rels/document.xml.rels": "rel:Relationships",
        "word/styles.xml": "w:styles",
        "word/settings.xml": "w:settings",
        "word/numbering.xml": "w:numbering",
        "word/fontTable.xml": "w:fonts",
    }
    _require(
        len(archive.infolist()) == len({name.casefold() for name in parts}),
        "Duplicate OPC part names",
    )
    _require(
        set(fixed) <= parts
        and all(
            name.startswith("word/") and _MEDIA_PATH.fullmatch(name[5:])
            for name in parts - fixed.keys()
        ),
        "Missing or unexpected OPC parts",
    )
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, remove_comments=True
    )
    xml = {}
    for path, tag in fixed.items():
        root = etree.fromstring(archive.read(path), parser=parser)
        _require(not root.getroottree().docinfo.doctype and root.tag == _q(tag), f"Invalid {path}")
        xml[path] = root
    for path in sorted(parts - fixed.keys()):
        try:
            picture = _bitmap(archive.read(path), path)
        except OmmError as exc:
            raise OmmError("ERR_DOCX_PACKAGE_INVALID", detail=f"Invalid image part {path}") from exc
        _require(path.endswith("." + picture.extension), f"Incorrect image extension in {path}")
    _content_types(xml["[Content_Types].xml"], parts)
    _relationships(xml["_rels/.rels"], _ROOT_TARGETS, parts, word=False)
    rels = _relationships(xml["word/_rels/document.xml.rels"], _WORD_TARGETS, parts, word=True)
    core = xml["docProps/core.xml"]
    _require(_children(core, "dc:title", "dc:creator", "cp:category"), "Invalid core properties")
    kind = core[2].text
    _require(kind in {"manuscript", "supplementary", "response_letter"}, "Invalid deliverable kind")
    _require(_children(xml["docProps/app.xml"], "ep:Application"), "Invalid application properties")
    settings = xml["word/settings.xml"]
    track = settings.find(_W + "trackRevisions")
    marked = track is not None
    if marked:
        _require(not track.attrib and not len(track), "Invalid trackRevisions flag")
    expected = ["w:proofState"]
    if marked:
        expected.append("w:trackRevisions")
    expected.extend(("w:defaultTabStop", "w:compat", "m:mathPr"))
    _require(_children(settings, *expected), "Invalid settings or trackRevisions order")
    _require(
        _children(settings[-1], "m:mathFont") and settings[-1][0].get(_M + "val") == "Cambria Math",
        "Missing Cambria Math settings",
    )
    numbering = xml["word/numbering.xml"]
    _require(
        len(numbering) >= 2
        and all(node.tag == _W + "abstractNum" for node in numbering[:2])
        and all(node.tag == _W + "num" for node in numbering[2:]),
        "Invalid list numbering",
    )
    list_ids = {node.get(_W + "numId") for node in numbering[2:]}
    _require(None not in list_ids and len(list_ids) == len(numbering) - 2, "Duplicate list IDs")
    styles = xml["word/styles.xml"]
    style_ids = {node.get(_W + "styleId") for node in styles.findall(_W + "style")}
    _require(
        len(style_ids) == len(styles) - 1 and set(_TEXT_STYLES) <= style_ids,
        "Missing or duplicate styles",
    )
    fonts = xml["word/fontTable.xml"]
    _require(
        {"Times New Roman", "Cambria Math"}
        <= {node.get(_W + "name") for node in fonts.findall(_W + "font")},
        "Missing document fonts",
    )
    _verify_document(
        xml["word/document.xml"],
        marked,
        kind != "response_letter",
        style_ids,
        list_ids,
        rels,
    )


def verify_docx_package(docx: Path | bytes) -> None:
    """Validate OPC integrity and the WordprocessingML profile emitted by this compiler."""
    source = BytesIO(docx) if isinstance(docx, bytes) else docx
    try:
        with ZipFile(source) as archive:
            _verify_parts(archive)
    except (
        OSError,
        BadZipFile,
        etree.XMLSyntaxError,
        ValueError,
        NotImplementedError,
        RuntimeError,
    ) as exc:
        raise OmmError("ERR_DOCX_PACKAGE_INVALID", detail=str(exc)) from exc
