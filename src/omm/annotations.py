"""Store Annotations and their frozen Resolution Snapshots."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import RFC_4122, UUID, uuid4

import portalocker

from omm import ast
from omm.errors import OmmError
from omm.parser import BLOCK_ID
from omm.workspace import ANNOTATIONS, atomic_write_text, resolve_path, workspace_path

_LOCK = Path(".omm", "cache", "annotations.lock")
_REVISION = re.compile(r"[0-9a-f]{40}")
_ASSET_HASH = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?"
    r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
_TEXT_BLOCKS = (
    ast.Heading,
    ast.Paragraph,
    ast.ListBlock,
    ast.ReviewItem,
    ast.FigureBlock,
    ast.TableBlock,
    ast.OutlineBlock,
)

AnnotationStatus = Literal["open", "resolved"]
AnchorEvaluation = Literal[
    "exact", "block_deleted", "block_modified", "asset_changed", "invalidated"
]


@dataclass(frozen=True, slots=True)
class BlockSelector:
    kind: Literal["block"] = "block"


@dataclass(frozen=True, slots=True)
class TextSelector:
    start: int
    end: int
    quote: str
    kind: Literal["text"] = "text"


@dataclass(frozen=True, slots=True)
class RectSelector:
    x: float
    y: float
    w: float
    h: float
    asset_sha256: str
    kind: Literal["rect"] = "rect"


@dataclass(frozen=True, slots=True)
class CellSelector:
    row: int
    col: int
    kind: Literal["cell"] = "cell"


Selector = BlockSelector | TextSelector | RectSelector | CellSelector


@dataclass(frozen=True, slots=True)
class ResolutionRecord:
    resolved_at: str
    after_rev: str
    after_block_md: str | None
    reopened_at: str | None


@dataclass(frozen=True, slots=True)
class Annotation:
    id: str
    block_id: str
    base_rev: str
    selector: Selector
    comment: str
    created_at: str
    before_block_md: str
    resolutions: tuple[ResolutionRecord, ...]

    @property
    def status(self) -> AnnotationStatus:
        if not self.resolutions or self.resolutions[-1].reopened_at is not None:
            return "open"
        return "resolved"


@dataclass(frozen=True, slots=True)
class BlockContext:
    block: ast.Block
    asset_sha256: str | None = None


# An unreachable revision raises ERR_REVISION_NOT_FOUND. None means the block is absent.
BlockLookup = Callable[[str, str], BlockContext | None]


def _object(value: object, field: str, *keys: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or value.keys() != set(keys):
        raise ValueError(field)
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(field)
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError(field) from exc
    return value


def _matched(value: object, pattern: str | re.Pattern[str], field: str) -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(field)
    return value


def _annotation_id(value: object) -> str:
    value = _string(value, "id")
    try:
        identifier = UUID(value)
    except ValueError as exc:
        raise ValueError("id") from exc
    if identifier.version != 4 or identifier.variant != RFC_4122 or str(identifier) != value:
        raise ValueError("id")
    return value


def _comment(value: object) -> str:
    result = _string(value, "comment").strip()
    if not result:
        raise ValueError("comment")
    return result


def _utc(value: object, field: str) -> str:
    timestamp = _matched(value, _TIMESTAMP, field)
    try:
        return datetime.fromisoformat(timestamp).astimezone(UTC).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError) as exc:
        raise ValueError(field) from exc


def _integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(field)
    return value


def _coordinate(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(field)
    if (isinstance(value, float) and not math.isfinite(value)) or not 0.0 <= value <= 1.0:
        raise ValueError(field)
    return float(value) or 0.0


def _decode_selector(value: object) -> Selector:
    if not isinstance(value, Mapping):
        raise TypeError("selector")
    match value.get("kind"):
        case "block":
            _object(value, "selector", "kind")
            return BlockSelector()
        case "text":
            _object(value, "selector", "kind", "start", "end", "quote")
            return TextSelector(
                _integer(value["start"], "selector.start"),
                _integer(value["end"], "selector.end"),
                _string(value["quote"], "selector.quote"),
            )
        case "rect":
            _object(value, "selector", "kind", "x", "y", "w", "h", "asset_sha256")
            x = _coordinate(value["x"], "selector.x")
            y = _coordinate(value["y"], "selector.y")
            w = _coordinate(value["w"], "selector.w")
            h = _coordinate(value["h"], "selector.h")
            # Check the endpoints directly so a decimal edge such as 0.9 + 0.1 fits.
            if x >= 1.0 or y >= 1.0 or w <= 0.0 or h <= 0.0 or x + w > 1.0 or y + h > 1.0:
                raise ValueError("selector")
            return RectSelector(
                x, y, w, h, _matched(value["asset_sha256"], _ASSET_HASH, "selector.asset_sha256")
            )
        case "cell":
            _object(value, "selector", "kind", "row", "col")
            row = _integer(value["row"], "selector.row")
            col = _integer(value["col"], "selector.col")
            if row < 0 or col < 0:
                raise ValueError("selector")
            return CellSelector(row, col)
        case _:
            raise ValueError("selector.kind")


def _quote_matches(selector: TextSelector, source: str) -> bool:
    return (
        0 <= selector.start < selector.end <= len(source)
        and source[selector.start : selector.end] == selector.quote
    )


def _decode_resolution(value: object) -> ResolutionRecord:
    data = _object(
        value, "resolutions", "resolved_at", "after_rev", "after_block_md", "reopened_at"
    )
    snapshot = data["after_block_md"]
    reopened_at = data["reopened_at"]
    return ResolutionRecord(
        resolved_at=_utc(data["resolved_at"], "resolved_at"),
        after_rev=_matched(data["after_rev"], _REVISION, "after_rev"),
        after_block_md=None if snapshot is None else _string(snapshot, "after_block_md"),
        reopened_at=None if reopened_at is None else _utc(reopened_at, "reopened_at"),
    )


def _decode_annotation(value: object) -> Annotation:
    data = _object(
        value,
        "items",
        "id",
        "block_id",
        "base_rev",
        "selector",
        "comment",
        "created_at",
        "before_block_md",
        "resolutions",
    )
    history = data["resolutions"]
    if not isinstance(history, list):
        raise TypeError("resolutions")
    resolutions = tuple(_decode_resolution(item) for item in history)
    if any(record.reopened_at is None for record in resolutions[:-1]):
        raise ValueError("resolutions.reopened_at")
    selector = _decode_selector(data["selector"])
    before = _string(data["before_block_md"], "before_block_md")
    if isinstance(selector, TextSelector) and not _quote_matches(selector, before):
        raise ValueError("selector.quote")
    return Annotation(
        id=_annotation_id(data["id"]),
        block_id=_matched(data["block_id"], BLOCK_ID, "block_id"),
        base_rev=_matched(data["base_rev"], _REVISION, "base_rev"),
        selector=selector,
        comment=_comment(data["comment"]),
        created_at=_utc(data["created_at"], "created_at"),
        before_block_md=before,
        resolutions=resolutions,
    )


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    data: dict[str, object] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(key)
        data[key] = value
    return data


def _read_annotations(path: Path) -> dict[str, Annotation]:
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except UnicodeError as exc:
        raise OmmError("ERR_UTF8_INVALID", path=path) from exc
    except OSError as exc:
        raise OmmError("ERR_FILE_READ", path=path, detail=str(exc)) from exc
    try:
        data = _object(
            json.loads(source, object_pairs_hook=_unique_keys), "schema", "schema_version", "items"
        )
        if _integer(data["schema_version"], "schema_version") != 1:
            raise ValueError("schema_version")
        items = data["items"]
        if not isinstance(items, list):
            raise TypeError("items")
        records: dict[str, Annotation] = {}
        for item in items:
            annotation = _decode_annotation(item)
            if annotation.id in records:
                raise ValueError("id")
            records[annotation.id] = annotation
        return records
    except (TypeError, ValueError, RecursionError) as exc:
        raise OmmError("ERR_ANNOTATIONS_INVALID", path=path, detail=str(exc)) from exc


@contextmanager
def _locked_path(root: Path) -> Iterator[Path]:
    root = resolve_path(root)
    path = workspace_path(root, ANNOTATIONS.as_posix())
    lock = workspace_path(root, _LOCK.as_posix())
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        guard = portalocker.Lock(lock, mode="ab", timeout=5, check_interval=0.02)
        guard.acquire()
    except portalocker.exceptions.LockException as exc:
        raise OmmError("ERR_ANNOTATIONS_LOCK", path=lock) from exc
    except OSError as exc:
        raise OmmError("ERR_FILE_WRITE", path=lock, detail=str(exc)) from exc
    try:
        yield path
    finally:
        guard.release()


def load_annotations(root: Path) -> tuple[Annotation, ...]:
    """Read and validate the store. A missing file represents an empty store."""
    with _locked_path(root) as path:
        return tuple(_read_annotations(path).values())


def list_annotations(
    root: Path,
    *,
    status: AnnotationStatus | None = None,
    block_id: str | None = None,
) -> tuple[Annotation, ...]:
    return tuple(
        annotation
        for annotation in load_annotations(root)
        if (status is None or annotation.status == status)
        and (block_id is None or annotation.block_id == block_id)
    )


def _write_annotations(path: Path, records: dict[str, Annotation]) -> None:
    items = sorted(records.values(), key=lambda item: (item.created_at, item.id))
    data = {"schema_version": 1, "items": [asdict(item) for item in items]}
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _is_image(block: ast.Block) -> bool:
    return isinstance(block, ast.Subfigure) or (
        isinstance(block, ast.FigureBlock) and len(block.subfigures) == 1
    )


def _table_cell(block: ast.Block, selector: CellSelector) -> ast.TableCell | None:
    if isinstance(block, ast.TableBlock):
        grid = (*block.header, *block.rows)
        if 0 <= selector.row < len(grid) and 0 <= selector.col < len(grid[selector.row]):
            return grid[selector.row][selector.col]
    return None


def _text_regions(block: ast.Block) -> Iterator[ast.Content | ast.TableCell]:
    if isinstance(block, ast.ReviewItem):
        yield block.comment
    elif isinstance(block, ast.FigureBlock):
        yield block.caption
    elif isinstance(block, ast.TableBlock):
        yield block.caption
        for row in (*block.header, *block.rows):
            yield from (cell for cell in row if not cell.covered)
        yield from block.notes
    elif isinstance(block, ast.OutlineBlock):
        start = block.clean_source.find("\n") + 1
        end = block.clean_source.rfind("\n")
        if 0 < start < end:
            yield ast.Content(ast.Span(start, end), block.inlines)
    else:
        yield ast.Content(ast.Span(0, len(block.clean_source)), block.inlines)


def _text_selection_error(block: ast.Block, selector: TextSelector) -> str | None:
    if not isinstance(block, _TEXT_BLOCKS):
        return "ERR_ANNOTATION_SELECTOR_MISMATCH"
    if not _quote_matches(selector, block.clean_source):
        return "ERR_ANNOTATION_QUOTE_MISMATCH"
    for region in _text_regions(block):
        if selector.start < region.span.start or selector.end > region.span.end:
            continue
        for node in ast.walk_inlines(region.inlines):
            if isinstance(node, (ast.InlineMath, ast.Citation, ast.BlockRef, ast.LocRef)):
                start, end = node.span.start, node.span.end
                if (
                    selector.start < end
                    and start < selector.end
                    and (selector.start > start or selector.end < end)
                ):
                    return "ERR_ANNOTATION_ATOMIC_SPLIT"
        return None
    return "ERR_ANNOTATION_SELECTOR_MISMATCH"


def _validate_target(selector: Selector, context: BlockContext, block_id: str) -> None:
    block = context.block
    if isinstance(selector, TextSelector):
        if code := _text_selection_error(block, selector):
            raise OmmError(code, block_id=block_id)
    elif isinstance(selector, RectSelector):
        if not _is_image(block) or selector.asset_sha256 != context.asset_sha256:
            raise OmmError("ERR_ANNOTATION_SELECTOR_MISMATCH", block_id=block_id)
    elif isinstance(selector, CellSelector):
        cell = _table_cell(block, selector)
        if cell is None:
            raise OmmError("ERR_ANNOTATION_SELECTOR_MISMATCH", block_id=block_id)
        if cell.covered:
            raise OmmError("ERR_ANNOTATION_CELL_COVERED", block_id=block_id)


def _selector_data(selector: Selector | Mapping[str, object]) -> dict[str, object]:
    if isinstance(selector, Mapping):
        return dict(selector)
    if isinstance(selector, (BlockSelector, TextSelector, RectSelector, CellSelector)):
        return {field.name: getattr(selector, field.name) for field in fields(selector)}
    raise ValueError("selector")


def _now(now_utc: str | None, path: Path) -> str:
    if now_utc is None:
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")
    try:
        return _utc(now_utc, "now_utc")
    except ValueError as exc:
        raise OmmError("ERR_ANNOTATIONS_INVALID", path=path, field=str(exc)) from exc


def create_annotation(
    root: Path,
    *,
    block_id: str,
    base_rev: str,
    selector: Selector | Mapping[str, object],
    comment: str,
    lookup: BlockLookup,
    annotation_id: str | None = None,
    now_utc: str | None = None,
) -> Annotation:
    """Create an Annotation against its base revision or return an identical prior request."""
    with _locked_path(root) as path:
        records = _read_annotations(path)
        try:
            identifier = str(uuid4()) if annotation_id is None else _annotation_id(annotation_id)
            block_id = _matched(block_id, BLOCK_ID, "block_id")
            base_rev = _matched(base_rev, _REVISION, "base_rev")
            comment = _comment(comment)
        except (TypeError, ValueError) as exc:
            raise OmmError("ERR_ANNOTATIONS_INVALID", path=path, field=str(exc)) from exc
        created_at = _now(now_utc, path)

        existing = records.get(identifier)
        try:
            selector_data = _selector_data(selector)
        except ValueError as exc:
            raise OmmError("ERR_ANNOTATION_SELECTOR_MISMATCH", block_id=block_id) from exc
        if existing is not None:
            if isinstance(existing.selector, RectSelector) and selector_data.get("kind") == "rect":
                # An omitted hash in a retry refers to the originally frozen asset.
                selector_data.setdefault("asset_sha256", existing.selector.asset_sha256)
            try:
                requested_selector = _decode_selector(selector_data)
            except (TypeError, ValueError) as exc:
                raise OmmError("ERR_ANNOTATION_ID_CONFLICT", path=path, id=identifier) from exc
            if (block_id, base_rev, requested_selector, comment) == (
                existing.block_id,
                existing.base_rev,
                existing.selector,
                existing.comment,
            ):
                return existing
            raise OmmError("ERR_ANNOTATION_ID_CONFLICT", path=path, id=identifier)

        context = lookup(base_rev, block_id)
        if context is None:
            raise OmmError("ERR_ANNOTATION_TARGET_MISSING", block_id=block_id, revision=base_rev)
        if selector_data.get("kind") == "rect":
            selector_data.setdefault("asset_sha256", context.asset_sha256)
        try:
            bound_selector = _decode_selector(selector_data)
        except (TypeError, ValueError) as exc:
            raise OmmError(
                "ERR_ANNOTATION_SELECTOR_MISMATCH", block_id=block_id, field=str(exc)
            ) from exc
        _validate_target(bound_selector, context, block_id)
        annotation = Annotation(
            identifier,
            block_id,
            base_rev,
            bound_selector,
            comment,
            created_at,
            _snapshot(context, path, "before_block_md"),
            (),
        )
        records[identifier] = annotation
        _write_annotations(path, records)
        return annotation


def _snapshot(context: BlockContext, path: Path, field: str) -> str:
    try:
        return _string(context.block.clean_source, field)
    except (TypeError, ValueError) as exc:
        raise OmmError("ERR_ANNOTATIONS_INVALID", path=path, field=field) from exc


def _find_annotation(records: dict[str, Annotation], annotation_id: str, path: Path) -> Annotation:
    try:
        identifier = _annotation_id(annotation_id)
    except (TypeError, ValueError) as exc:
        raise OmmError("ERR_ANNOTATIONS_INVALID", path=path, field="id") from exc
    try:
        return records[identifier]
    except KeyError as exc:
        raise OmmError("ERR_ANNOTATION_NOT_FOUND", path=path, id=identifier) from exc


def resolve_annotation(
    root: Path,
    annotation_id: str,
    *,
    after_rev: str,
    lookup: BlockLookup,
    now_utc: str | None = None,
) -> Annotation:
    with _locked_path(root) as path:
        records = _read_annotations(path)
        annotation = _find_annotation(records, annotation_id, path)
        if annotation.status != "open":
            raise OmmError("ERR_ANNOTATION_STATE_CONFLICT", path=path, id=annotation_id)
        try:
            after_rev = _matched(after_rev, _REVISION, "after_rev")
        except ValueError as exc:
            raise OmmError("ERR_ANNOTATIONS_INVALID", path=path, field="after_rev") from exc
        resolved_at = _now(now_utc, path)
        context = lookup(after_rev, annotation.block_id)
        snapshot = None if context is None else _snapshot(context, path, "after_block_md")
        resolution = ResolutionRecord(resolved_at, after_rev, snapshot, None)
        updated = replace(annotation, resolutions=(*annotation.resolutions, resolution))
        records[annotation_id] = updated
        _write_annotations(path, records)
        return updated


def reopen_annotation(root: Path, annotation_id: str, *, now_utc: str | None = None) -> Annotation:
    with _locked_path(root) as path:
        records = _read_annotations(path)
        annotation = _find_annotation(records, annotation_id, path)
        if annotation.status != "resolved":
            raise OmmError("ERR_ANNOTATION_STATE_CONFLICT", path=path, id=annotation_id)
        resolution = replace(annotation.resolutions[-1], reopened_at=_now(now_utc, path))
        updated = replace(annotation, resolutions=(*annotation.resolutions[:-1], resolution))
        records[annotation_id] = updated
        _write_annotations(path, records)
        return updated


def delete_annotation(root: Path, annotation_id: str) -> None:
    with _locked_path(root) as path:
        records = _read_annotations(path)
        annotation = _find_annotation(records, annotation_id, path)
        del records[annotation.id]
        _write_annotations(path, records)


def evaluate_anchor(annotation: Annotation, current: BlockContext | None) -> AnchorEvaluation:
    """Evaluate the stored selector against the block in the current revision."""
    if current is None:
        return "block_deleted"
    block = current.block
    selector = annotation.selector
    if isinstance(selector, BlockSelector):
        return "exact"
    if isinstance(selector, RectSelector):
        if not _is_image(block):
            return "invalidated"
        return "exact" if current.asset_sha256 == selector.asset_sha256 else "asset_changed"
    if isinstance(selector, TextSelector):
        if not isinstance(block, _TEXT_BLOCKS):
            return "invalidated"
        if block.clean_source != annotation.before_block_md:
            return "block_modified"
        return "exact" if _text_selection_error(block, selector) is None else "invalidated"
    if isinstance(selector, CellSelector):
        cell = _table_cell(block, selector)
        if cell is None or cell.covered:
            return "invalidated"
    return "exact" if block.clean_source == annotation.before_block_md else "block_modified"
