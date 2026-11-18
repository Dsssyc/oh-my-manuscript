"""Explicitly assign persistent IDs to unlabelled blocks."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from omm.ast import walk_blocks
from omm.errors import OmmError
from omm.parser import parse_file, parse_source
from omm.workspace import atomic_write_text, load_workspace


def stamp_workspace(root: Path) -> int:
    workspace = load_workspace(root)
    documents = [
        parse_file(workspace.resolve(source), allow_unstamped_figures=True)
        for source in workspace.sources
    ]
    used: set[str] = set()
    for document in documents:
        for block in walk_blocks(document.blocks):
            if block.block_id is None:
                continue
            if block.block_id in used:
                raise OmmError(
                    "ERR_DUPLICATE_BLOCK_ID",
                    path=document.path,
                    line=block.line,
                    block_id=block.block_id,
                )
            used.add(block.block_id)

    changes = 0
    updates: list[tuple[Path, str]] = []
    for document in documents:
        edits: list[tuple[int, str]] = []
        expected_ids: list[str] = []
        for block in walk_blocks(document.blocks):
            if block.stamp_offset is None:
                if block.block_id is not None:
                    expected_ids.append(block.block_id)
                continue
            block_id = f"{block.id_prefix}-{uuid4().hex[:6]}"
            while block_id in used:
                block_id = f"{block.id_prefix}-{uuid4().hex[:6]}"
            used.add(block_id)
            expected_ids.append(block_id)
            marker = (
                f"#{block_id} "
                if block.stamp_in_attributes
                else f"{block.stamp_separator}{{#{block_id}}}"
            )
            edits.append((block.stamp_offset, marker))
        if edits:
            parts = []
            cursor = 0
            for offset, marker in sorted(edits):
                parts.extend((document.source[cursor:offset], marker))
                cursor = offset
            parts.append(document.source[cursor:])
            assert document.path is not None
            updated = "".join(parts)
            stamped = parse_source(updated, document.path)
            stamped_ids = []
            for block in walk_blocks(stamped.blocks):
                if block.stamp_offset is not None:
                    raise OmmError("ERR_STAMP_INVALID", path=document.path, line=block.line)
                if block.block_id is not None:
                    stamped_ids.append(block.block_id)
            if stamped_ids != expected_ids:
                raise OmmError("ERR_STAMP_INVALID", path=document.path)
            updates.append((document.path, updated))
            changes += len(edits)
    for path, updated in updates:
        atomic_write_text(path, updated)
    return changes
