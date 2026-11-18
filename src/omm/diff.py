"""Block pairing and inline source differences for manuscript review."""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

from omm.git_revs import RevisionBlockEntry, RevisionSnapshot, load_workspace_at_revision
from omm.parser import _inline_tokens

DiffOp = Literal["equal", "delete", "insert"]
BlockStatus = Literal["unchanged", "modified", "moved", "modified_and_moved", "added", "deleted"]
_ATOMIC_INLINE = {"inline_math", "citation", "block_ref", "loc_ref"}
_PLAIN_TOKENS = re.compile(r"\s+|\w+|[^\w\s]")
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


@dataclass(frozen=True, slots=True)
class DiffChunk:
    op: DiffOp
    text: str


@dataclass(frozen=True, slots=True)
class BlockDiff:
    before_md: str | None
    after_md: str | None
    chunks: tuple[DiffChunk, ...]


@dataclass(frozen=True, slots=True)
class RevisionBlockDiff:
    block_id: str
    status: BlockStatus
    before: RevisionBlockEntry | None
    after: RevisionBlockEntry | None
    asset_changed: bool
    markdown: BlockDiff


@dataclass(frozen=True, slots=True)
class WorkspaceDiff:
    base_revision: str
    target_revision: str
    blocks: dict[str, RevisionBlockDiff]


def _plain_tokens(text: str) -> list[str]:
    result = []
    for match in _PLAIN_TOKENS.finditer(text):
        word = match[0]
        start = 0
        for index, char in enumerate(word):
            if unicodedata.name(char, "").startswith(_CJK_NAMES):
                if index > start:
                    result.append(word[start:index])
                result.append(char)
                start = index + 1
        if start < len(word):
            result.append(word[start:])
    return result


def tokenize_inline_diff(text: str) -> list[str]:
    tokens = []
    end = 0
    # Use the same source spans as the Structured Source parser, including its
    # escape, email address, and code span rules.
    for token in _inline_tokens(text, {}):
        if token.type in _ATOMIC_INLINE:
            span = token.meta["span"]
            tokens.extend(_plain_tokens(text[end : span.start]))
            tokens.append(text[span.start : span.end])
            end = span.end
    tokens.extend(_plain_tokens(text[end:]))
    return tokens


def diff_block_markdown(before_md: str | None, after_md: str | None) -> BlockDiff:
    before = tokenize_inline_diff(before_md or "")
    after = tokenize_inline_diff(after_md or "")
    chunks = []
    for op, i, j, k, last in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if op == "equal":
            chunks.append(DiffChunk("equal", "".join(before[i:j])))
        if op in {"delete", "replace"}:
            chunks.append(DiffChunk("delete", "".join(before[i:j])))
        if op in {"insert", "replace"}:
            chunks.append(DiffChunk("insert", "".join(after[k:last])))
    return BlockDiff(before_md, after_md, tuple(chunks))


def _in_order(base: RevisionSnapshot, target: RevisionSnapshot) -> set[str]:
    # With unique IDs, LCS is the longest increasing subsequence of the base
    # positions in target order. Exclude moves between sources or deliverables.
    base_positions = {block_id: i for i, block_id in enumerate(base.blocks)}
    tails: list[int] = []
    tail_ids: list[str] = []
    predecessors: dict[str, str | None] = {}
    for block_id, entry in target.blocks.items():
        before = base.blocks.get(block_id)
        if (
            before is None
            or before.source != entry.source
            or before.deliverables != entry.deliverables
        ):
            continue
        position = base_positions[block_id]
        length = bisect_left(tails, position)
        predecessors[block_id] = tail_ids[length - 1] if length else None
        if length == len(tails):
            tails.append(position)
            tail_ids.append(block_id)
        else:
            tails[length] = position
            tail_ids[length] = block_id

    retained = set()
    block_id = tail_ids[-1] if tail_ids else None
    while block_id is not None:
        retained.add(block_id)
        block_id = predecessors[block_id]
    return retained


def _block_source(snapshot: RevisionSnapshot, entry: RevisionBlockEntry) -> str:
    span = entry.block.source_span
    return snapshot.documents[entry.source].source[span.start : span.end]


def diff_revisions(root: Path, base_rev: str, target_rev: str) -> WorkspaceDiff:
    base = load_workspace_at_revision(root, base_rev)
    target = load_workspace_at_revision(root, target_rev)
    in_order = _in_order(base, target)
    blocks: dict[str, RevisionBlockDiff] = {}
    for block_id in dict.fromkeys((*base.blocks, *target.blocks)):
        before = base.blocks.get(block_id)
        after = target.blocks.get(block_id)
        asset_changed = False
        status: BlockStatus
        if before is None:
            status = "added"
        elif after is None:
            status = "deleted"
        else:
            asset_changed = before.asset_sha256 != after.asset_sha256
            # The full block span includes attributes such as figure columns.
            modified = asset_changed or _block_source(base, before) != _block_source(target, after)
            moved = block_id not in in_order
            if modified and moved:
                status = "modified_and_moved"
            elif modified:
                status = "modified"
            elif moved:
                status = "moved"
            else:
                status = "unchanged"
        markdown = diff_block_markdown(
            before.block.clean_source if before is not None else None,
            after.block.clean_source if after is not None else None,
        )
        blocks[block_id] = RevisionBlockDiff(
            block_id, status, before, after, asset_changed, markdown
        )
    return WorkspaceDiff(base.revision, target.revision, blocks)
