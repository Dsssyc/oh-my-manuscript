"""Derive logical numbers, citation labels, and review targets from a revision."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass

from omm import ast
from omm.bib import BibEntry
from omm.git_revs import RevisionSnapshot


@dataclass(frozen=True, slots=True)
class BlockNumber:
    number: str
    label: str
    sublabel: str | None = None


@dataclass(frozen=True, slots=True)
class CitingBlock:
    block_id: str | None
    deliverable: str
    source: str


@dataclass(frozen=True, slots=True)
class NumberedReference:
    citekey: str
    entry_type: str
    number: int | None
    short_label: str
    formatted_text: str
    fields: dict[str, str]
    cited: bool
    deliverable_counts: dict[str, int]
    citing_blocks: tuple[CitingBlock, ...]
    year: str


@dataclass(frozen=True, slots=True)
class ReferenceLocation:
    deliverable: str
    section: str | None
    block_ids: tuple[str, ...]

    def label(self, reference: ast.LocRef) -> str:
        section = f" §{self.section}" if self.section else ""
        target = reference.start_id
        if reference.end_id is not None:
            target += f"..{reference.end_id}"
        return f"{self.deliverable}{section} [{target}]"


@dataclass(frozen=True, slots=True)
class NumberedWorkspace:
    block_numbers: dict[str, dict[str, BlockNumber]]
    references: dict[str, NumberedReference]
    review_target_index: dict[str, list[str]]
    locations: dict[tuple[str, str | None], ReferenceLocation]
    citation_style: str

    def citation_label(self, citation: ast.Citation) -> str:
        labels = []
        for key in citation.keys:
            entry = self.references.get(key)
            if entry is None:
                labels.append(f"@{key}")
            elif self.citation_style == "numeric":
                labels.append(str(entry.number) if entry.number is not None else f"@{key}")
            elif citation.parenthetical:
                labels.append(f"{entry.short_label}, {entry.year}")
            else:
                labels.append(f"{entry.short_label} ({entry.year})")
        if self.citation_style == "numeric":
            return f"[{', '.join(labels)}]"
        if citation.parenthetical:
            return f"({'; '.join(labels)})"
        return "; ".join(labels)


def _letters(index: int) -> str:
    """Return a, ..., z, aa, ... for a zero-based index."""
    result = ""
    while index >= 0:
        index, remainder = divmod(index, 26)
        result = chr(ord("a") + remainder) + result
        index -= 1
    return result


def _plain(value: str) -> str:
    return " ".join(value.replace("{", "").replace("}", "").split())


def _split_names(value: str, separator: re.Pattern[str]) -> Iterator[str]:
    """Split BibTeX names at a separator outside brace groups."""
    depth = start = pos = 0
    while pos < len(value):
        char = value[pos]
        if char == "\\":
            pos += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif not depth and (match := separator.match(value, pos)):
            yield value[start:pos]
            pos = start = match.end()
            continue
        pos += 1
    yield value[start:]


_AND = re.compile(r"\s+and\s+", re.IGNORECASE)
_COMMA = re.compile(r",")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class _Author:
    last: str
    given: str = ""
    suffix: str = ""

    @property
    def formatted(self) -> str:
        initials = " ".join(
            "-".join(f"{part[0]}." for part in name.split("-") if part)
            for name in self.given.split()
        )
        return ", ".join(part for part in (self.last, self.suffix, initials) if part)


def _authors(value: str) -> tuple[_Author, ...]:
    authors = []
    for name in _split_names(value, _AND):
        if not name.strip():
            continue
        parts = list(_split_names(name.strip(), _COMMA))
        if len(parts) > 1:
            suffix = _plain(parts[1]) if len(parts) == 3 else ""
            authors.append(_Author(_plain(parts[0]), _plain(parts[-1]), suffix))
        else:
            words = list(_split_names(parts[0], _SPACE))
            # A braced organizational name is one surname. Lowercase particles
            # directly preceding a surname are retained as part of it.
            last = len(words) - 1
            while last > 0 and words[last - 1][:1].islower():
                last -= 1
            authors.append(_Author(_plain(" ".join(words[last:])), _plain(" ".join(words[:last]))))
    return tuple(author for author in authors if author.last)


def _short_label(entry: BibEntry, authors: tuple[_Author, ...]) -> str:
    if len(authors) > 2:
        return f"{authors[0].last} et al."
    if authors:
        return " & ".join(author.last for author in authors)
    return _plain(entry.fields.get("organization", "")) or entry.citekey


def _format_entry(entry: BibEntry, authors: tuple[_Author, ...], year: str, short: str) -> str:
    fields = {key: _plain(value) for key, value in entry.fields.items()}
    names = "; ".join(author.formatted for author in authors) or short
    volume = fields.get("volume", "")
    if issue := fields.get("number"):
        volume += f"({issue})"
    publication = ", ".join(
        part
        for part in (
            fields.get("journal") or fields.get("booktitle"),
            volume,
            fields.get("pages", "").replace("--", "–"),
        )
        if part
    )
    parts = [f"{names} ({year})", fields.get("title", ""), publication]
    text = " ".join(
        part if part.endswith((".", "?", "!")) else f"{part}." for part in parts if part
    )
    if doi := fields.get("doi"):
        if not doi.startswith(("https://doi.org/", "http://doi.org/")):
            doi = "https://doi.org/" + doi.removeprefix("doi:").strip()
        text += f" {doi}"
    return text


def _references(
    snapshot: RevisionSnapshot,
    counts: dict[str, Counter[str]],
    citing: dict[str, dict[CitingBlock, None]],
) -> dict[str, NumberedReference]:
    entries = snapshot.bibliography
    authors = {key: _authors(entry.fields.get("author", "")) for key, entry in entries.items()}
    short = {key: _short_label(entry, authors[key]) for key, entry in entries.items()}
    years = {key: _plain(entry.fields.get("year", "")) or "n.d." for key, entry in entries.items()}
    ordered = sorted(
        entries,
        key=lambda key: (
            tuple((author.last.casefold(), author.given.casefold()) for author in authors[key])
            or ((short[key].casefold(), ""),),
            years[key],
            _plain(entries[key].fields.get("title", "")).casefold(),
            key,
        ),
    )
    numbers: dict[str, int] = {}
    if snapshot.workspace.style.citation_style == "numeric":
        numbers = {key: index for index, key in enumerate(counts, 1)}
        ordered = [*numbers, *(key for key in ordered if key not in numbers)]
    else:
        groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for key in ordered:
            if key in counts:
                groups[short[key].casefold(), years[key]].append(key)
        for group in groups.values():
            if len(group) > 1:
                for index, key in enumerate(group):
                    years[key] += _letters(index)
    return {
        key: NumberedReference(
            key,
            entries[key].entry_type,
            numbers.get(key),
            short[key],
            _format_entry(entries[key], authors[key], years[key], short[key]),
            dict(entries[key].fields),
            key in counts,
            dict(counts.get(key, {})),
            tuple(citing.get(key, {})),
            years[key],
        )
        for key in ordered
    }


def _number_block(
    block: ast.Block, prefix: str, headings: list[int], counters: Counter[str]
) -> BlockNumber | None:
    if isinstance(block, ast.ReviewItem):
        counters["review"] += 1
        number = block.block_id or str(counters["review"])
        return BlockNumber(number, number)
    if isinstance(block, ast.Heading):
        index = block.level - 1
        for ancestor in range(index):
            if headings[ancestor] == 0:
                headings[ancestor] = 1
        headings[index] += 1
        headings[index + 1 :] = [0] * (len(headings) - index - 1)
        number = prefix + ".".join(str(level) for level in headings[: index + 1])
        return BlockNumber(number, f"Section {number}")
    if isinstance(block, ast.FigureBlock):
        kind = "Fig."
    elif isinstance(block, ast.TableBlock):
        kind = "Table"
    elif isinstance(block, ast.DisplayMath) and not block.unnumbered:
        kind = "Eq."
    else:
        return None
    counters[kind] += 1
    number = f"{prefix}{counters[kind]}"
    if kind == "Eq.":
        number = f"({number})"
    return BlockNumber(number, f"{kind} {number}")


def number_workspace(snapshot: RevisionSnapshot) -> NumberedWorkspace:
    """Compute labels and reference indexes in Deliverable order."""
    numbers: dict[str, dict[str, BlockNumber]] = {}
    sections: dict[str, dict[str, str | None]] = {}
    counts: dict[str, Counter[str]] = {}
    citing: dict[str, dict[CitingBlock, None]] = defaultdict(dict)
    reviews: dict[str, ast.ReviewItem] = {}
    for name, deliverable in snapshot.workspace.deliverables.items():
        numbers[name] = labels = {}
        sections[name] = positions = {}
        prefix = "S" if deliverable.kind == "supplementary" else ""
        headings = [0] * 6
        counters: Counter[str] = Counter()
        section = None
        for source in deliverable.sources:
            for block in ast.walk_blocks(snapshot.documents[source].blocks):
                block_id = block.block_id
                if deliverable.kind != "response_letter" or isinstance(block, ast.ReviewItem):
                    number = _number_block(block, prefix, headings, counters)
                    if number is not None and block_id is not None:
                        labels[block_id] = number
                        if isinstance(block, ast.Heading):
                            section = number.number
                        if isinstance(block, ast.FigureBlock):
                            for index, panel in enumerate(block.subfigures):
                                if panel.block_id is not None:
                                    suffix = (
                                        f"({_letters(index)})"
                                        if len(block.subfigures) > 1
                                        else None
                                    )
                                    labels[panel.block_id] = BlockNumber(
                                        number.number, number.label + (suffix or ""), suffix
                                    )
                if block_id is not None and deliverable.kind != "response_letter":
                    positions[block_id] = section
                if isinstance(block, ast.OutlineBlock):
                    continue
                if isinstance(block, ast.ReviewItem):
                    if block_id is not None:
                        reviews[block_id] = block
                    continue
                for node in ast.walk_inlines(block.inlines):
                    if isinstance(node, ast.Citation):
                        for key in node.keys:
                            if key in snapshot.bibliography:
                                counts.setdefault(key, Counter())[name] += 1
                                citing[key][CitingBlock(block_id, name, source)] = None

    locations = {}
    review_targets: dict[str, list[str]] = defaultdict(list)
    # The reply fields cover author references and exclude the reviewer's comment.
    for review_id, review in reviews.items():
        targets = dict.fromkeys(review.referenced_block_ids)
        for reference in review.location_refs:
            key = (reference.start_id, reference.end_id)
            if key not in locations:
                locations[key] = resolve_location(sections, *key)
            location = locations[key]
            targets.update(dict.fromkeys(location.block_ids))
        for target in targets:
            review_targets[target].append(review_id)

    # Resolve location references outside Review Items as well (e.g. in an Outline).
    for document in snapshot.documents.values():
        for block in ast.walk_blocks(document.blocks):
            inlines = block.comment.inlines if isinstance(block, ast.ReviewItem) else block.inlines
            for node in ast.walk_inlines(inlines):
                if isinstance(node, ast.LocRef):
                    key = (node.start_id, node.end_id)
                    if key not in locations:
                        locations[key] = resolve_location(sections, *key)
    return NumberedWorkspace(
        numbers,
        _references(snapshot, counts, citing),
        dict(review_targets),
        locations,
        snapshot.workspace.style.citation_style,
    )


def resolve_location(
    sections: dict[str, dict[str, str | None]], start: str, end: str | None
) -> ReferenceLocation:
    last = end or start
    for name, positions in sections.items():
        if start in positions and last in positions:
            keys = tuple(positions)
            first_index, last_index = keys.index(start), keys.index(last)
            if first_index <= last_index:
                return ReferenceLocation(name, positions[start], keys[first_index : last_index + 1])
    return ReferenceLocation("", None, tuple(dict.fromkeys((start, last))))
