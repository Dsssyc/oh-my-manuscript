"""Read-only validation and asset fingerprints across a Manuscript Workspace."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from omm import ast
from omm.bib import load_bib
from omm.errors import Diagnostic, OmmError
from omm.parser import parse_file
from omm.workspace import ANNOTATIONS, Workspace, load_workspace, read_text


@dataclass(frozen=True, slots=True)
class CheckResult:
    diagnostics: tuple[Diagnostic, ...]
    asset_sha256: dict[str, str]

    @property
    def ok(self) -> bool:
        return not any(
            diag.severity == "error"
            or diag.code in {"ERR_OUTLINE_UNEXPANDED", "ERR_ANNOTATION_OPEN"}
            for diag in self.diagnostics
        )


def _check_annotations(workspace: Workspace) -> list[Diagnostic]:
    path = workspace.root / ANNOTATIONS
    try:
        data = json.loads(read_text(workspace.resolve(ANNOTATIONS.as_posix())))
        if (
            not isinstance(data, dict)
            or set(data) != {"schema_version", "items"}
            or type(data["schema_version"]) is not int
            or data["schema_version"] != 1
            or not isinstance(data["items"], list)
        ):
            raise ValueError
        diagnostics = []
        for item in data["items"]:
            if not isinstance(item, dict) or "state" in item or "status" in item:
                raise TypeError
            block_id = item.get("block_id")
            annotation_id = item.get("id")
            resolutions = item.get("resolutions")
            if (
                not isinstance(block_id, str)
                or not isinstance(annotation_id, str)
                or not isinstance(resolutions, list)
            ):
                raise TypeError
            for record in resolutions:
                if (
                    not isinstance(record, dict)
                    or "reopened_at" not in record
                    or (
                        record["reopened_at"] is not None
                        and not isinstance(record["reopened_at"], str)
                    )
                ):
                    raise ValueError
            if not resolutions or resolutions[-1]["reopened_at"] is not None:
                diagnostics.append(
                    Diagnostic(
                        "ERR_ANNOTATION_OPEN",
                        path,
                        block_id=block_id,
                        severity="warning",
                        params={"id": annotation_id},
                    )
                )
        return diagnostics
    except (TypeError, ValueError):
        return [Diagnostic("ERR_ANNOTATIONS_INVALID", path)]
    except OmmError as exc:
        return [exc.diagnostic]


class _Checker:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.diagnostics: list[Diagnostic] = []
        self.documents: dict[str, ast.Document] = {}
        self.blocks: dict[str, tuple[str, ast.Block]] = {}
        self.duplicate_ids: set[str] = set()
        self.asset_sha256: dict[str, str] = {}
        self.checked_assets: set[str] = set()
        self.citekeys: set[str] | None = None
        self.source_order = {
            name: {source: position for position, source in enumerate(deliverable.sources)}
            for name, deliverable in workspace.deliverables.items()
        }

    def report(
        self,
        source: str,
        block: ast.Block,
        code: str,
        *,
        node: ast.Inline | None = None,
        warning: bool = False,
        **params: object,
    ) -> None:
        line = block.line
        if node is not None:
            line += block.clean_source.count("\n", 0, node.span.start)
        self.diagnostics.append(
            Diagnostic(
                code,
                self.documents[source].path,
                line,
                block.block_id,
                "warning" if warning else "error",
                params,
            )
        )

    def load(self) -> None:
        try:
            self.citekeys = set(load_bib(self.workspace.resolve(self.workspace.bibliography)))
        except OmmError as exc:
            self.diagnostics.append(exc.diagnostic)
        for source in self.workspace.sources:
            try:
                self.documents[source] = parse_file(
                    self.workspace.resolve(source),
                    allow_unstamped_figures=True,
                    diagnostics=self.diagnostics,
                )
            except OmmError as exc:
                self.diagnostics.append(exc.diagnostic)

    def index(self) -> None:
        for source, document in self.documents.items():
            for block in ast.walk_blocks(document.blocks):
                block_id = block.block_id
                if block_id is None and block.stamp_offset is not None:
                    self.report(source, block, "ERR_BLOCK_ID_MISSING")
                elif block_id is not None:
                    if block_id in self.blocks:
                        self.duplicate_ids.add(block_id)
                        self.report(source, block, "ERR_DUPLICATE_BLOCK_ID")
                    else:
                        self.blocks[block_id] = source, block
                if isinstance(block, ast.OutlineBlock):
                    self.report(source, block, "ERR_OUTLINE_UNEXPANDED", warning=True)

    def check_asset(self, source: str, block: ast.Block, asset: str) -> None:
        if asset in self.checked_assets:
            return
        self.checked_assets.add(asset)
        try:
            path = self.workspace.resolve(asset)
            with path.open("rb") as stream:
                self.asset_sha256[asset] = hashlib.file_digest(stream, "sha256").hexdigest()
        except FileNotFoundError:
            self.report(source, block, "ERR_FIGURE_ASSET_MISSING", asset=asset)
        except OSError:
            self.report(source, block, "ERR_FIGURE_ASSET_UNREADABLE", asset=asset)
        except OmmError as exc:
            self.report(source, block, exc.code, **exc.diagnostic.params)

    def target(
        self, source: str, block: ast.Block, node: ast.Inline, target_id: str
    ) -> tuple[str, ast.Block] | None:
        if target_id in self.duplicate_ids:
            return None
        target = self.blocks.get(target_id)
        if target is None:
            if len(self.documents) == len(self.workspace.sources):
                self.report(source, block, "ERR_REFERENCE_MISSING", node=node, target=target_id)
            return None
        if isinstance(target[1], ast.OutlineBlock) and not isinstance(block, ast.OutlineBlock):
            self.report(source, block, "ERR_REFERENCE_OUTLINE", node=node, target=target_id)
            return None
        return target

    def check_location(self, source: str, block: ast.Block, node: ast.LocRef) -> None:
        if not isinstance(block, ast.OutlineBlock) and any(
            source in order and self.workspace.deliverables[name].kind != "response_letter"
            for name, order in self.source_order.items()
        ):
            self.report(source, block, "ERR_LOCATION_CONTEXT", node=node)

        endpoints = []
        for target_id in dict.fromkeys([node.start_id, node.end_id]):
            if target_id is None:
                continue
            target = self.target(source, block, node, target_id)
            if target is not None:
                if isinstance(
                    target[1],
                    (
                        ast.FigureBlock,
                        ast.Subfigure,
                        ast.TableBlock,
                        ast.OutlineBlock,
                        ast.ReviewItem,
                    ),
                ) or not any(
                    target[0] in order
                    and self.workspace.deliverables[name].kind != "response_letter"
                    for name, order in self.source_order.items()
                ):
                    self.report(
                        source, block, "ERR_LOCATION_UNAVAILABLE", node=node, target=target_id
                    )
                else:
                    endpoints.append(target)
        if node.end_id is not None and endpoints:
            if node.start_id == node.end_id:
                endpoints *= 2
            if len(endpoints) == 2:
                self.check_location_range(source, block, node, endpoints[0], endpoints[1])

    def check_location_range(
        self,
        source: str,
        block: ast.Block,
        node: ast.LocRef,
        start: tuple[str, ast.Block],
        end: tuple[str, ast.Block],
    ) -> None:
        first, last = start[0], end[0]
        matching = [
            order
            for name, order in self.source_order.items()
            if first in order
            and last in order
            and self.workspace.deliverables[name].kind != "response_letter"
        ]
        if not matching:
            self.report(
                source,
                block,
                "ERR_LOCATION_RANGE_DELIVERABLE",
                node=node,
                start=node.start_id,
                end=node.end_id,
            )
        elif len(matching) > 1:
            self.report(
                source,
                block,
                "ERR_LOCATION_RANGE_AMBIGUOUS",
                node=node,
                start=node.start_id,
                end=node.end_id,
            )
        else:
            order = matching[0]
            if (order[first], start[1].source_span.start) > (order[last], end[1].source_span.start):
                self.report(
                    source,
                    block,
                    "ERR_LOCATION_RANGE_ORDER",
                    node=node,
                    start=node.start_id,
                    end=node.end_id,
                )

    def validate(self) -> None:
        for source, document in self.documents.items():
            for block in ast.walk_blocks(document.blocks):
                if isinstance(block, ast.Subfigure):
                    self.check_asset(source, block, block.asset_path)
                for node in ast.walk_inlines(block.inlines):
                    if isinstance(node, ast.BlockRef):
                        self.target(source, block, node, node.block_id)
                    elif isinstance(node, ast.LocRef):
                        self.check_location(source, block, node)
                    elif isinstance(node, ast.Citation) and self.citekeys is not None:
                        for key in dict.fromkeys(node.keys):
                            if key not in self.citekeys:
                                self.report(
                                    source, block, "ERR_CITATION_MISSING", node=node, key=key
                                )

    def run(self) -> CheckResult:
        self.load()
        self.index()
        self.validate()
        self.diagnostics.extend(_check_annotations(self.workspace))
        return CheckResult(tuple(self.diagnostics), self.asset_sha256)


def check_workspace(root: Path) -> CheckResult:
    try:
        workspace = load_workspace(root)
    except OmmError as exc:
        return CheckResult((exc.diagnostic,), {})
    return _Checker(workspace).run()
