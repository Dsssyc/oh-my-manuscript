"""Serve the local workbench over loopback HTTP and server-sent events."""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
from contextlib import suppress
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Full, Queue
from socketserver import TCPServer
from threading import Event, Lock, Thread
from urllib.parse import parse_qs, unquote, urlsplit

from omm import ast
from omm.annotations import (
    Annotation,
    BlockContext,
    create_annotation,
    delete_annotation,
    evaluate_anchor,
    load_annotations,
    reopen_annotation,
    resolve_annotation,
)
from omm.diff import diff_block_markdown, diff_revisions
from omm.errors import OmmError
from omm.git_revs import (
    LATEST_CHECKPOINT_REF,
    PUBLISHED_REF,
    RevisionSnapshot,
    SyncResult,
    _parse_manifest,
    _Repository,
    _RevisionTree,
    has_unsynced_changes,
    load_workspace_at_revision,
    mark_checkpoint,
    sync_workspace,
)
from omm.i18n import detect_locale, t
from omm.numbering import number_workspace
from omm.parser import parse_source
from omm.workspace import (
    MANIFEST,
    Workspace,
    _state_directory,
    _uuid4,
    atomic_write_text,
    load_workspace,
    normalize_newlines,
    normalize_relative_path,
    resolve_path,
)

CONTRACT_VERSION = "0.1.0"
DEFAULT_PORT = 49638
LOOPBACK = "127.0.0.1"
_REVISION = re.compile(r"[0-9a-f]{40}")
_MAX_BODY_SIZE = 1 << 20
_EVENTS = {
    "revision_published",
    "checkpoint_updated",
    "annotations_changed",
    "unsynced_status",
    "workspaces_changed",
}
_WEB_TYPES = {
    ".js": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
_ASSET_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
}
_BLOCK_KINDS: dict[type[ast.Block], str] = {
    ast.Heading: "heading",
    ast.Paragraph: "paragraph",
    ast.ListBlock: "list",
    ast.DisplayMath: "display_math",
    ast.OutlineBlock: "outline",
    ast.FigureBlock: "figure",
    ast.Subfigure: "subfigure",
    ast.TableBlock: "table",
    ast.ReviewItem: "review_item",
    ast.MarkdownBlock: "markdown",
}
_INLINE_KINDS: dict[type[ast.Inline], str] = {
    ast.Text: "text",
    ast.LineBreak: "line_break",
    ast.CodeSpan: "code_span",
    ast.Strong: "strong",
    ast.Emphasis: "emphasis",
    ast.Subscript: "subscript",
    ast.Superscript: "superscript",
    ast.Link: "link",
    ast.Image: "image",
    ast.InlineMath: "inline_math",
    ast.BlockRef: "block_ref",
    ast.LocRef: "loc_ref",
    ast.Citation: "citation",
}
_ERROR_STATUS = {
    "ERR_BAD_REQUEST": 400,
    "ERR_FORBIDDEN_HOST": 403,
    "ERR_FORBIDDEN_ORIGIN": 403,
    "ERR_FORBIDDEN_FETCH_SITE": 403,
    "ERR_FORBIDDEN_CSRF": 403,
    "ERR_FORBIDDEN_TOKEN": 403,
    "ERR_NOT_FOUND": 404,
    "ERR_WORKSPACE_NOT_REGISTERED": 404,
    "ERR_ANNOTATION_NOT_FOUND": 404,
    "ERR_ANNOTATION_TARGET_MISSING": 404,
    "ERR_REVISION_NOT_FOUND": 404,
    "ERR_METHOD_NOT_ALLOWED": 405,
    "ERR_OUTLINE_CONFLICT": 409,
    "ERR_SYNC_CONFLICT": 409,
    "ERR_NO_PUBLISHED_REVISION": 409,
    "ERR_ANNOTATION_ID_CONFLICT": 409,
    "ERR_ANNOTATION_STATE_CONFLICT": 409,
    "ERR_PAYLOAD_TOO_LARGE": 413,
    "ERR_SERVER": 500,
}


def _outline_span(block: ast.OutlineBlock) -> tuple[int, int]:
    start = block.clean_source.index("\n") + 1
    return start, max(start, block.clean_source.rindex("\n"))


class _SnapshotJSON:
    def __init__(self, snapshot: RevisionSnapshot) -> None:
        self.snapshot = snapshot
        self.numbered = number_workspace(snapshot)

    def inline(self, node: ast.Inline, deliverables: tuple[str, ...]) -> dict:
        data = {"kind": _INLINE_KINDS[type(node)], "span": [node.span.start, node.span.end]}
        match node:
            case ast.BlockRef(block_id=target):
                entry = self.snapshot.blocks.get(target)
                names = entry.deliverables if entry else ()
                name = next((name for name in deliverables if name in names), None)
                name = name or (names[0] if names else None)
                number = self.numbered.block_numbers.get(name, {}).get(target)
                data.update(
                    atomic=True,
                    target_id=target,
                    target_kind=_BLOCK_KINDS[type(entry.block)] if entry else None,
                    target_deliverable=name,
                    label=number.label if number else target,
                )
            case ast.LocRef(start_id=start, end_id=end):
                location = self.numbered.locations[start, end]
                data.update(
                    atomic=True,
                    start_id=start,
                    end_id=end,
                    target_deliverable=location.deliverable or None,
                    label=location.label(node).lstrip(),
                )
            case ast.Citation(keys=keys, parenthetical=parenthetical):
                data.update(
                    atomic=True,
                    keys=keys,
                    parenthetical=parenthetical,
                    label=self.numbered.citation_label(node),
                )
            case ast.InlineMath(latex=latex):
                data.update(atomic=True, latex=latex)
            case ast.Link(destination=destination, title=title, children=children):
                data.update(
                    destination=destination,
                    title=title,
                    children=self.inlines(children, deliverables),
                )
            case ast.InlineContainer(children=children):
                data["children"] = self.inlines(children, deliverables)
            case ast.Image(alt=alt, destination=destination, title=title):
                data.update(alt=alt, destination=destination, title=title)
            case ast.LineBreak(hard=hard):
                data["hard"] = hard
            case ast.Text(value=value) | ast.CodeSpan(value=value):
                data["value"] = value
        return data

    def inlines(self, nodes: tuple[ast.Inline, ...], deliverables: tuple[str, ...]) -> list[dict]:
        return [self.inline(node, deliverables) for node in nodes]

    def content(self, content: ast.Content | ast.TableCell, deliverables: tuple[str, ...]) -> dict:
        data = {
            "span": [content.span.start, content.span.end],
            "inlines": self.inlines(content.inlines, deliverables),
        }
        if isinstance(content, ast.TableCell):
            data.update(
                row_span=content.row_span, col_span=content.col_span, covered=content.covered
            )
        return data

    def block(
        self,
        block: ast.Block,
        source: str,
        deliverables: tuple[str, ...],
        parent: dict | None = None,
    ) -> dict:
        number = self.numbered.block_numbers[deliverables[0]].get(block.block_id)
        entry = self.snapshot.blocks.get(block.block_id)
        data = {
            "block_id": block.block_id,
            "kind": _BLOCK_KINDS[type(block)],
            "source": source,
            "deliverables": deliverables,
            "line": block.line,
            "clean_source": block.clean_source,
            "number": number.number if number else None,
            "label": number.label if number else None,
            "asset_sha256": entry.asset_sha256 if entry else None,
        }
        match block:
            case ast.Heading(level=level):
                data.update(level=level, inlines=self.inlines(block.inlines, deliverables))
            case ast.ListBlock(ordered=ordered):
                data.update(ordered=ordered, inlines=self.inlines(block.inlines, deliverables))
            case ast.Paragraph():
                data["inlines"] = self.inlines(block.inlines, deliverables)
            case ast.DisplayMath(latex=latex, unnumbered=unnumbered):
                data.update(latex=latex, unnumbered=unnumbered)
            case ast.OutlineBlock():
                start, end = _outline_span(block)
                data.update(
                    body_span=[start, end],
                    body_markdown=block.clean_source[start:end],
                    inlines=self.inlines(block.inlines, deliverables),
                )
            case ast.FigureBlock(cols=cols, caption=caption, subfigures=panels):
                data.update(
                    cols=cols,
                    caption=self.content(caption, deliverables),
                    subfigures=[self.block(panel, source, deliverables, data) for panel in panels],
                )
            case ast.Subfigure(alt=alt, asset_path=asset_path):
                data.update(
                    alt=alt, asset_path=asset_path, sublabel=number.sublabel if number else None
                )
                if parent is not None and block.block_id is None:
                    for key in ("number", "label", "asset_sha256"):
                        data[key] = parent[key]
            case ast.TableBlock():
                data.update(
                    caption=self.content(block.caption, deliverables),
                    alignment=block.alignment,
                    header=[
                        [self.content(cell, deliverables) for cell in row] for row in block.header
                    ],
                    rows=[[self.content(cell, deliverables) for cell in row] for row in block.rows],
                    midrule_after=block.midrule_after,
                    notes=[self.content(note, deliverables) for note in block.notes],
                )
            case ast.ReviewItem():
                data.update(
                    reviewer=block.reviewer,
                    comment=self.content(block.comment, deliverables),
                    reply=[self.block(child, source, deliverables) for child in block.reply],
                    referenced_block_ids=block.referenced_block_ids,
                    location_refs=[self.inline(node, deliverables) for node in block.location_refs],
                )
            case ast.MarkdownBlock(kind=kind, children=children):
                data.update(
                    markdown_kind=kind,
                    children=[self.block(child, source, deliverables) for child in children],
                )
        return data

    def blocks(self) -> list[dict]:
        workspace = self.snapshot.workspace
        blocks = []
        for source in workspace.sources:
            names = tuple(name for name, d in workspace.deliverables.items() if source in d.sources)
            blocks.extend(
                self.block(block, source, names) for block in self.snapshot.documents[source].blocks
            )
        return blocks

    def references(self) -> dict:
        entries = []
        for entry in self.numbered.references.values():
            data = asdict(entry)
            del data["year"]
            entries.append(data)
        return {"entries": entries}


def _context(snapshot: RevisionSnapshot, block_id: str) -> BlockContext | None:
    entry = snapshot.blocks.get(block_id)
    return BlockContext(entry.block, entry.asset_sha256) if entry else None


def _annotation_json(annotation: Annotation, current: RevisionSnapshot) -> dict:
    data = asdict(annotation)
    data["status"] = annotation.status
    data["anchor"] = evaluate_anchor(annotation, _context(current, annotation.block_id))
    for record in data["resolutions"]:
        diff = diff_block_markdown(annotation.before_block_md, record["after_block_md"])
        record["diff_chunks"] = [asdict(chunk) for chunk in diff.chunks]
    return data


def _diff_json(root: Path, base: str, target: str) -> dict:
    diff = diff_revisions(root, base, target)
    blocks = {}
    for block_id, block in diff.blocks.items():
        before, after = block.before, block.after
        blocks[block_id] = {
            "block_id": block_id,
            "status": block.status,
            "before_source": before.source if before else None,
            "after_source": after.source if after else None,
            "before_deliverables": before.deliverables if before else [],
            "after_deliverables": after.deliverables if after else [],
            "asset_changed": block.asset_changed,
            **asdict(block.markdown),
        }
    return {
        "base_revision": diff.base_revision,
        "target_revision": diff.target_revision,
        "blocks": blocks,
    }


def _required_string(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise OmmError("ERR_BAD_REQUEST")
    return value


def _optional_string(body: dict, key: str) -> str | None:
    return None if body.get(key) is None else _required_string(body, key)


def _require_revision(revision: str) -> str:
    if not _REVISION.fullmatch(revision):
        raise OmmError("ERR_BAD_REQUEST")
    return revision


def _registry(state_dir: Path) -> dict[str, Path]:
    path = state_dir / "workspaces.json"
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except UnicodeError as exc:
        raise OmmError("ERR_REGISTRY_INVALID", path=path) from exc
    except OSError as exc:
        raise OmmError("ERR_FILE_READ", path=path) from exc
    try:
        data = json.loads(source)
        if (
            not isinstance(data, dict)
            or set(data) != {"schema_version", "workspaces"}
            or type(data["schema_version"]) is not int
            or data["schema_version"] != 1
            or not isinstance(data["workspaces"], dict)
        ):
            raise ValueError
        result = {}
        for key, record in data["workspaces"].items():
            if (
                _uuid4(key) != key
                or not isinstance(record, dict)
                or set(record) != {"path"}
                or not isinstance(record["path"], str)
                or not Path(record["path"]).is_absolute()
                or "\0" in record["path"]
            ):
                raise ValueError
            result[key] = Path(record["path"])
        return result
    except (ValueError, RecursionError) as exc:
        raise OmmError("ERR_REGISTRY_INVALID", path=path) from exc


def _milestones(repo: _Repository) -> list[dict[str, str]]:
    refs = repo.git("for-each-ref", "--format=%(refname)", "refs/tags").splitlines()
    result = []
    for raw in refs:
        ref = os.fsdecode(raw)
        try:
            revision = repo.resolve(ref)
        except OmmError as exc:
            if exc.code != "ERR_REVISION_NOT_FOUND":
                raise
            continue
        result.append({"name": ref.removeprefix("refs/tags/"), "revision": revision})
    return result


def _verify_workspace(expected: str, actual: str) -> None:
    if expected != actual:
        raise OmmError("ERR_WORKSPACE_NOT_REGISTERED", workspace_id=expected)


def _save_outline(
    repo: _Repository, workspace_id: str, block_id: str, expected: str, body: str
) -> SyncResult:
    root = repo.root
    if repo.resolve(PUBLISHED_REF, optional=True) != expected or has_unsynced_changes(root):
        raise OmmError("ERR_OUTLINE_CONFLICT")
    snapshot = load_workspace_at_revision(root, expected)
    _verify_workspace(workspace_id, snapshot.workspace.workspace_id)
    entry = snapshot.blocks.get(block_id)
    if entry is None or not isinstance(entry.block, ast.OutlineBlock):
        raise OmmError("ERR_NOT_FOUND")
    block = entry.block
    document = snapshot.documents[entry.source]
    path = snapshot.workspace.resolve(entry.source)
    try:
        original = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise OmmError("ERR_OUTLINE_CONFLICT") from exc
    if normalize_newlines(original) != document.source:
        raise OmmError("ERR_OUTLINE_CONFLICT")

    # AST offsets are LF-normalized. Find the corresponding raw line boundaries
    # so both fences and the content outside them keep their original bytes.
    lines = [0, *(match.end() for match in re.finditer(r"\r\n?|\n", original))]
    close_line = document.source.count("\n", 0, block.source_span.end)
    start, end = lines[block.line], lines[close_line]
    newline = re.search(r"(?:\r\n|\r|\n)$", original[:start])
    assert newline is not None
    body = normalize_newlines(body)
    if body and not body.endswith("\n"):
        body += "\n"
    updated = original[:start] + body.replace("\n", newline[0]) + original[end:]
    parsed = parse_source(updated, path)
    matches = [
        candidate for candidate in ast.walk_blocks(parsed.blocks) if candidate.block_id == block_id
    ]
    span = block.source_span
    new_end = span.end + len(parsed.source) - len(document.source)
    if (
        len(matches) != 1
        or not isinstance(matches[0], ast.OutlineBlock)
        or matches[0].source_span != ast.Span(span.start, new_end)
    ):
        raise OmmError("ERR_DIRECTIVE_INVALID", kind="outline")

    if (
        has_unsynced_changes(root)
        or repo.resolve(PUBLISHED_REF, optional=True) != expected
        or path.read_bytes() != original.encode("utf-8")
    ):
        raise OmmError("ERR_OUTLINE_CONFLICT")
    if updated == original:
        return SyncResult(expected, False)
    atomic_write_text(path, updated)
    try:
        return sync_workspace(root, expected_old_rev=expected)
    except Exception as exc:
        if path.read_bytes() == updated.encode("utf-8"):
            atomic_write_text(path, original)
        if isinstance(exc, OmmError) and exc.code == "ERR_SYNC_CONFLICT":
            raise OmmError("ERR_OUTLINE_CONFLICT") from exc
        raise


class WorkbenchServer(ThreadingHTTPServer):
    """Own a loopback listener, workspace write locks, and the event stream."""

    daemon_threads = True
    allow_reuse_address = os.name != "nt"
    allow_reuse_port = False

    def __init__(
        self,
        state_dir: Path | None = None,
        port: int = DEFAULT_PORT,
        *,
        runtime_id: str = "development",
        daemon_token: str | None = None,
        web_dist_dir: Path | None = None,
        poll_interval: float = 1.5,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.state_dir = _state_directory(state_dir)
        self.runtime_id = runtime_id
        self.daemon_token = daemon_token or secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        self.web_dist_dir = resolve_path(web_dist_dir or Path(__file__).with_name("_web_dist"))
        self.poll_interval = poll_interval
        self._guard = Lock()
        self._stop = Event()
        self._poller: Thread | None = None
        self._subscribers: set[Queue[bytes | None]] = set()
        self._registered: dict[str, Path] = {}
        self._observed: dict[str, tuple[Path, bool, str | None]] = {}
        self._announced: dict[str, str] = {}
        self._workspace_locks: dict[Path, Lock] = {}
        super().__init__((LOOPBACK, port), _WorkbenchHandler, bind_and_activate=False)
        try:
            self.server_bind()
            self.server_activate()
        except OSError as exc:
            self.server_close()
            if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) in {10013, 10048}:
                raise OmmError("ERR_PORT_IN_USE", port=port) from exc
            raise

    def server_bind(self) -> None:
        if os.name == "nt":
            # Winsock defines SO_EXCLUSIVEADDRUSE as ~SO_REUSEADDR. Some Python
            # builds do not expose the constant even though Winsock supports it.
            option = getattr(socket, "SO_EXCLUSIVEADDRUSE", ~socket.SO_REUSEADDR)
            self.socket.setsockopt(socket.SOL_SOCKET, option, 1)
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address
        self.port = self.server_port

    @property
    def authority(self) -> str:
        return f"{LOOPBACK}:{self.port}"

    def _workspace_lock(self, root: Path) -> Lock:
        root = resolve_path(root)
        with self._guard:
            return self._workspace_locks.setdefault(root, Lock())

    @staticmethod
    def _enqueue(subscriber: Queue[bytes | None], frame: bytes | None) -> None:
        try:
            subscriber.put_nowait(frame)
        except Full:
            # Disconnect stalled readers. EventSource reconnects and reloads state.
            while True:
                try:
                    subscriber.get_nowait()
                except Empty:
                    break
            subscriber.put_nowait(None)

    def _subscribe(self) -> Queue[bytes | None]:
        subscriber: Queue[bytes | None] = Queue(maxsize=64)
        with self._guard:
            if self._stop.is_set():
                subscriber.put_nowait(None)
            else:
                self._subscribers.add(subscriber)
        return subscriber

    def _unsubscribe(self, subscriber: Queue[bytes | None]) -> None:
        with self._guard:
            self._subscribers.discard(subscriber)

    def broadcast(self, event: dict) -> None:
        frame = ("data: " + json.dumps(event, separators=(",", ":")) + "\n\n").encode()
        with self._guard:
            if self._stop.is_set():
                return
            if event["type"] == "revision_published" and isinstance(event.get("revision"), str):
                self._announced[event["workspace_id"]] = event["revision"]
            for subscriber in self._subscribers:
                self._enqueue(subscriber, frame)

    def _poll(self, *, initial: bool = False) -> None:
        try:
            registered = _registry(self.state_dir)
        except OmmError:
            return
        if not initial and registered != self._registered:
            self.broadcast({"type": "workspaces_changed"})
        self._registered = registered
        for workspace_id in self._observed.keys() - registered.keys():
            del self._observed[workspace_id]
        for workspace_id, root in registered.items():
            if self._stop.is_set():
                return
            try:
                with self._workspace_lock(root):
                    self._poll_workspace(workspace_id, root, initial=initial)
            except (OSError, OmmError, RuntimeError):
                # A registration can be inaccessible while its directory is moved.
                continue

    def _poll_workspace(self, workspace_id: str, root: Path, *, initial: bool) -> None:
        revision, dirty = None, True
        if root.is_dir():
            repo = _Repository(root)
            revision = repo.resolve(PUBLISHED_REF, optional=True)
            dirty = revision is None or has_unsynced_changes(root)
            if revision != repo.resolve(PUBLISHED_REF, optional=True):
                return
            workspace = _workspace_at(repo, revision) if revision else load_workspace(root)
            _verify_workspace(workspace_id, workspace.workspace_id)

        previous = self._observed.get(workspace_id)
        self._observed[workspace_id] = (root, dirty, revision)
        if initial or (root, dirty, revision) == previous:
            return
        with self._guard:
            announced = self._announced.get(workspace_id)
        if (
            revision is not None
            and revision != announced
            and (previous is None or previous[2] != revision)
        ):
            self.broadcast(
                {"type": "revision_published", "workspace_id": workspace_id, "revision": revision}
            )
        self.broadcast(
            {
                "type": "unsynced_status",
                "workspace_id": workspace_id,
                "has_unsynced_changes": dirty,
                "published_rev": revision,
            }
        )

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            self._poll()

    def _stop_background(self) -> None:
        with self._guard:
            self._stop.set()
            for subscriber in self._subscribers:
                self._enqueue(subscriber, None)

    def serve_forever(self, poll_interval: float = 0.1) -> None:
        self._poll(initial=True)
        self._poller = Thread(target=self._poll_loop, name="omm-workspace-poll", daemon=True)
        self._poller.start()
        try:
            super().serve_forever(poll_interval=poll_interval)
        finally:
            self._stop_background()
            self._poller.join()

    def shutdown(self) -> None:
        self._stop_background()
        super().shutdown()

    def server_close(self) -> None:
        self._stop_background()
        super().server_close()

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        connection, peer = super().get_request()
        connection.settimeout(10)
        return connection, peer


def _workspace_at(repo: _Repository, revision: str) -> Workspace:
    return _parse_manifest(repo.root, _RevisionTree(repo, revision).text(MANIFEST))


class _WorkbenchHandler(BaseHTTPRequestHandler):
    server: WorkbenchServer
    _body_consumed = False

    def log_message(self, format: str, *args: object) -> None:
        # Paths and query strings can contain private manuscript details.
        pass

    def _header(self, name: str) -> str | None:
        values = self.headers.get_all(name, ())
        return values[0] if len(values) == 1 else None

    @staticmethod
    def _matches(value: str | None, expected: str) -> bool:
        return value is not None and hmac.compare_digest(
            value.encode("utf-8"), expected.encode("utf-8")
        )

    def parse_request(self) -> bool:
        self._body_consumed = False
        if not super().parse_request():
            return False
        try:
            if self._header("Host") != self.server.authority or self.request_version == "HTTP/0.9":
                # Emit a status and headers even for legacy requests without a Host.
                if self.request_version == "HTTP/0.9":
                    self.request_version = "HTTP/1.0"
                raise OmmError("ERR_FORBIDDEN_HOST")
            if self.command in {"POST", "PUT", "DELETE"}:
                self._authorize()
        except OmmError as exc:
            self._error(exc)
            return False
        return True

    def _authorize(self) -> None:
        bearer = f"Bearer {self.server.daemon_token}"
        if self._matches(self._header("Authorization"), bearer):
            return
        if self.path.partition("?")[0] in {"/api/shutdown", "/api/cli/notify"}:
            raise OmmError("ERR_FORBIDDEN_TOKEN")
        if self._header("Origin") != f"http://{self.server.authority}":
            raise OmmError("ERR_FORBIDDEN_ORIGIN")
        self._fetch_site({"same-origin"})
        if not self._matches(self._header("X-OMM-CSRF"), self.server.csrf_token):
            raise OmmError("ERR_FORBIDDEN_CSRF")

    def _fetch_site(self, allowed: set[str]) -> None:
        if "Sec-Fetch-Site" in self.headers and self._header("Sec-Fetch-Site") not in allowed:
            raise OmmError("ERR_FORBIDDEN_FETCH_SITE")

    def _headers(self, status: int, mime: str, **headers: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()

    def _send(self, data: bytes, mime: str, *, status: int = 200, **headers: str) -> None:
        self._headers(status, mime, **{"Content-Length": str(len(data)), **headers})
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, value: dict, status: int = 200) -> None:
        self._send(
            json.dumps(value, separators=(",", ":")).encode(),
            "application/json; charset=utf-8",
            status=status,
            **{"Cache-Control": "no-store"},
        )

    def _error(self, exc: OmmError) -> None:
        params = {**exc.diagnostic.params}
        # Underlying OS/Git messages can contain local paths or Git internals.
        params.pop("detail", None)
        message = t(exc.code, detect_locale(), **params)
        with suppress(ConnectionError, TimeoutError):
            self._json(
                {"error": {"code": exc.code, "message": message}}, _ERROR_STATUS.get(exc.code, 422)
            )
        self._drain_body()

    def _body(self) -> dict:
        raw_length = self._header("Content-Length")
        if "Transfer-Encoding" in self.headers or (
            "Content-Length" in self.headers and (raw_length is None or not raw_length.isdecimal())
        ):
            raise OmmError("ERR_BAD_REQUEST")
        if raw_length is None or raw_length == "0":
            return {}
        try:
            length = int(raw_length)
            if length > _MAX_BODY_SIZE:
                raise OmmError("ERR_PAYLOAD_TOO_LARGE")
            raw = self.rfile.read(length)
            self._body_consumed = True
            if len(raw) != length:
                raise ValueError
            body = json.loads(raw.decode("utf-8"), parse_constant=_invalid_json_constant)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise OmmError("ERR_BAD_REQUEST") from exc
        if not isinstance(body, dict):
            raise OmmError("ERR_BAD_REQUEST")
        return body

    def _drain_body(self) -> None:
        """Read a small unread request body before the connection closes.

        Winsock aborts a socket whose receive buffer still holds unread bytes,
        replacing an in-flight response with an RST instead of a clean FIN.
        """
        if self._body_consumed or "Transfer-Encoding" in self.headers:
            return
        raw_length = self._header("Content-Length")
        if (
            raw_length is None
            or not raw_length.isdecimal()
            or not 0 < int(raw_length) <= _MAX_BODY_SIZE
        ):
            return
        timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(0.5)
            self.rfile.read(int(raw_length))
        except (OSError, TimeoutError):
            pass
        finally:
            with suppress(OSError):
                self.connection.settimeout(timeout)

    def _handle(self) -> None:
        try:
            try:
                url = urlsplit(self.path)
                if url.netloc or url.scheme or url.fragment:
                    raise ValueError
                query = parse_qs(
                    url.query, keep_blank_values=True, max_num_fields=32, errors="strict"
                )
            except ValueError as exc:
                raise OmmError("ERR_BAD_REQUEST") from exc
            if any(len(values) != 1 for values in query.values()):
                raise OmmError("ERR_BAD_REQUEST")
            params = {name: values[0] for name, values in query.items()}
            if self.command in {"GET", "HEAD"}:
                self._read(url.path, params)
            else:
                self._write(url.path)
        except OmmError as exc:
            self._error(exc)
        except (ConnectionError, TimeoutError):
            pass
        except OSError:
            self._error(OmmError("ERR_SERVER"))

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _handle

    def _read(self, path: str, query: dict[str, str]) -> None:
        match path:
            case "/api/health":
                self._json(
                    {
                        "status": "ok",
                        "pid": os.getpid(),
                        "port": self.server.port,
                        "runtime_id": self.server.runtime_id,
                        "contract_version": CONTRACT_VERSION,
                    }
                )
            case "/api/bootstrap":
                self._fetch_site({"same-origin", "none"})
                self._json(
                    {"csrf_token": self.server.csrf_token, "contract_version": CONTRACT_VERSION}
                )
            case "/api/workspaces":
                items = [
                    self._workspace_summary(wid, root)
                    for wid, root in _registry(self.server.state_dir).items()
                ]
                self._json({"workspaces": items})
            case "/api/events":
                self._events()
            case _:
                if path.startswith("/api/workspaces/"):
                    workspace_id, repo, route = self._workspace_route(path)
                    self._read_workspace(workspace_id, repo, route, query)
                else:
                    self._static(path)

    def _workspace_summary(self, workspace_id: str, root: Path) -> dict:
        summary = {
            "workspace_id": workspace_id,
            "path": str(root),
            "exists": root.is_dir(),
            "title": "",
            "published_rev": None,
            "checkpoint_rev": None,
            "has_unsynced_changes": True,
            "milestones": [],
        }
        if summary["exists"]:
            try:
                repo = _Repository(root)
                published = repo.resolve(PUBLISHED_REF, optional=True)
                workspace = _workspace_at(repo, published) if published else load_workspace(root)
                _verify_workspace(workspace_id, workspace.workspace_id)
                summary.update(title=workspace.title, **self._revision_state(repo, published))
            except OmmError:
                # Preserve the registration so moved or damaged entries can be located.
                pass
        return summary

    @staticmethod
    def _revision_state(repo: _Repository, published: str | None) -> dict:
        return {
            "published_rev": published,
            "checkpoint_rev": repo.resolve(LATEST_CHECKPOINT_REF, optional=True)
            if published
            else None,
            "has_unsynced_changes": published is None or has_unsynced_changes(repo.root),
            "milestones": _milestones(repo),
        }

    def _workspace_route(self, path: str) -> tuple[str, _Repository, list[str]]:
        workspace_id, *route = path.removeprefix("/api/workspaces/").split("/")
        root = _registry(self.server.state_dir).get(workspace_id)
        if root is None or not root.is_dir():
            raise OmmError("ERR_WORKSPACE_NOT_REGISTERED", workspace_id=workspace_id)
        return workspace_id, _Repository(root), route

    def _read_workspace(
        self, workspace_id: str, repo: _Repository, route: list[str], query: dict[str, str]
    ) -> None:
        match route:
            case ["snapshot"]:
                self._json(self._snapshot(workspace_id, repo, query))
            case ["assets"]:
                self._asset(workspace_id, repo, query)
            case ["blocks", block_id]:
                revision = _require_revision(_required_string(query, "rev"))
                snapshot = self._load(workspace_id, repo, revision)
                entry = snapshot.blocks.get(block_id)
                self._json(
                    {
                        "revision": snapshot.revision,
                        "block_id": block_id,
                        "found": entry is not None,
                        "clean_source": entry.block.clean_source if entry else None,
                        "kind": _BLOCK_KINDS[type(entry.block)] if entry else None,
                        "source": entry.source if entry else None,
                    }
                )
            case _:
                raise OmmError("ERR_NOT_FOUND")

    @staticmethod
    def _load(workspace_id: str, repo: _Repository, revision: str) -> RevisionSnapshot:
        snapshot = load_workspace_at_revision(repo.root, revision)
        if _REVISION.fullmatch(revision) and revision != snapshot.revision:
            raise OmmError("ERR_REVISION_NOT_FOUND", revision=revision)
        _verify_workspace(workspace_id, snapshot.workspace.workspace_id)
        return snapshot

    def _snapshot(self, workspace_id: str, repo: _Repository, query: dict[str, str]) -> dict:
        published = repo.resolve(PUBLISHED_REF, optional=True)
        requested = query.get("rev")
        if published is None and requested is None:
            workspace = load_workspace(repo.root)
            _verify_workspace(workspace_id, workspace.workspace_id)
            return {
                "workspace_id": workspace_id,
                "title": workspace.title,
                "published": False,
                "revision": None,
                **self._revision_state(repo, published),
            }
        snapshot = self._load(workspace_id, repo, requested if requested is not None else published)
        workspace = snapshot.workspace
        if base := query.get("diff_base"):
            base = repo.resolve(base)
            _verify_workspace(workspace_id, _workspace_at(repo, base).workspace_id)
        serialized = _SnapshotJSON(snapshot)
        return {
            "workspace_id": workspace_id,
            "title": workspace.title,
            "authors": workspace.authors
            if isinstance(workspace.authors, str)
            else [asdict(author) for author in workspace.authors],
            "published": True,
            "revision": snapshot.revision,
            **self._revision_state(repo, published),
            "style": asdict(workspace.style),
            "deliverables": [
                {"name": name, **asdict(deliverable)}
                for name, deliverable in workspace.deliverables.items()
            ],
            "blocks": serialized.blocks(),
            "references": serialized.references(),
            "annotations": [
                _annotation_json(annotation, snapshot) for annotation in load_annotations(repo.root)
            ],
            "diff": _diff_json(repo.root, base, snapshot.revision) if base else None,
            "review_target_index": serialized.numbered.review_target_index,
        }

    def _asset(self, workspace_id: str, repo: _Repository, query: dict[str, str]) -> None:
        revision = _require_revision(_required_string(query, "rev"))
        path = _safe_path(_required_string(query, "path"))
        mime = _ASSET_TYPES.get(Path(path).suffix.lower())
        if mime is None:
            raise OmmError("ERR_NOT_FOUND")
        if revision != repo.resolve(revision):
            raise OmmError("ERR_REVISION_NOT_FOUND", revision=revision)
        tree = _RevisionTree(repo, revision)
        workspace = _parse_manifest(repo.root, tree.text(MANIFEST))
        _verify_workspace(workspace_id, workspace.workspace_id)
        if not any(
            isinstance(block, ast.Subfigure) and block.asset_path == path
            for source in workspace.sources
            for block in ast.walk_blocks(parse_source(tree.text(source), repo.root / source).blocks)
        ):
            raise OmmError("ERR_NOT_FOUND")
        data = tree.read(path)
        etag = f'"{hashlib.sha256(data).hexdigest()}"'
        headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
        if mime in {"image/svg+xml", "application/pdf"}:
            headers["Content-Security-Policy"] = (
                "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:"
            )
        if etag == self._header("If-None-Match"):
            self._headers(304, mime, **headers)
        else:
            self._send(data, mime, **headers)

    def _static(self, path: str) -> None:
        root = self.server.web_dist_dir
        if path in {"/", "/index.html"}:
            relative = "index.html"
            mime = "text/html; charset=utf-8"
        elif path.startswith("/assets/"):
            try:
                relative = _safe_path(unquote(path[1:], errors="strict"))
            except UnicodeError as exc:
                raise OmmError("ERR_NOT_FOUND") from exc
            mime = _WEB_TYPES.get(Path(relative).suffix.lower(), "application/octet-stream")
            root = root / "assets"
            relative = relative.removeprefix("assets/")
        else:
            raise OmmError("ERR_NOT_FOUND")
        try:
            file = resolve_path(root / relative)
            # Containment compares the un-prefixed canonical path; only the
            # file access needs a \\\\?\\ prefix to pass the MAX_PATH limit.
            if not file.is_relative_to(root) or not _fs_path(file).is_file():
                raise OmmError("ERR_NOT_FOUND")
            data = _fs_path(file).read_bytes()
        except (FileNotFoundError, RuntimeError):
            raise OmmError("ERR_NOT_FOUND") from None
        if path in {"/", "/index.html"}:
            config = json.dumps(
                {"csrfToken": self.server.csrf_token, "contractVersion": CONTRACT_VERSION}
            )
            bootstrap = f"<script>window.__OMM_BOOTSTRAP__ = {config};</script>".encode()
            head = re.search(rb"</head\s*>", data, flags=re.IGNORECASE)
            if head is None:
                raise OmmError("ERR_SERVER")
            offset = head.start()
            self._send(
                data[:offset] + bootstrap + data[offset:], mime, **{"Cache-Control": "no-store"}
            )
        else:
            self._send(data, mime)

    def _events(self) -> None:
        self._fetch_site({"same-origin", "none"})
        if self.command == "HEAD":
            self._headers(200, "text/event-stream", **{"Cache-Control": "no-cache"})
            return
        subscriber = self.server._subscribe()
        try:
            self._headers(200, "text/event-stream", **{"Cache-Control": "no-cache"})
            self.wfile.write(b": connected\n\n")
            while not self.server._stop.is_set():
                try:
                    frame = subscriber.get(timeout=1.0)
                except Empty:
                    frame = b": heartbeat\n\n"
                if frame is None:
                    break
                self.wfile.write(frame)
                self.wfile.flush()
        finally:
            self.server._unsubscribe(subscriber)

    def _write(self, path: str) -> None:
        if path in {"/api/cli/notify", "/api/shutdown"}:
            if self.command != "POST":
                raise OmmError("ERR_METHOD_NOT_ALLOWED")
            if path == "/api/shutdown":
                self._json({"status": "stopping"})
                Thread(target=self.server.shutdown, name="omm-shutdown", daemon=True).start()
            else:
                event = self._body()
                event_type = _required_string(event, "type")
                if event_type not in _EVENTS:
                    raise OmmError("ERR_BAD_REQUEST")
                if event_type != "workspaces_changed" or "workspace_id" in event:
                    _required_string(event, "workspace_id")
                self.server.broadcast(event)
                self._json({"status": "ok"})
            return
        if not path.startswith("/api/workspaces/"):
            raise OmmError("ERR_NOT_FOUND")
        workspace_id, repo, route = self._workspace_route(path)
        body = self._body()
        with self.server._workspace_lock(repo.root):
            self._write_workspace(workspace_id, repo, route, body)

    def _write_workspace(
        self, workspace_id: str, repo: _Repository, route: list[str], body: dict
    ) -> None:
        root = repo.root
        if self.command == "PUT" and len(route) == 2 and route[0] == "outlines":
            expected = _require_revision(_required_string(body, "expected_rev"))
            result = _save_outline(
                repo, workspace_id, route[1], expected, _required_string(body, "body_markdown")
            )
            self._published(workspace_id, result)
            return

        published = repo.resolve(PUBLISHED_REF, optional=True)
        workspace = _workspace_at(repo, published) if published else load_workspace(root)
        _verify_workspace(workspace_id, workspace.workspace_id)
        match self.command, route:
            case "POST", ["sync"]:
                _verify_workspace(workspace_id, load_workspace(root).workspace_id)
                expected = _optional_string(body, "expected_rev")
                result = sync_workspace(root, expected_old_rev=expected)
                self._published(workspace_id, result)
            case "POST", ["checkpoint"]:
                checkpoint = mark_checkpoint(root, revision=_optional_string(body, "revision"))
                self.server.broadcast(
                    {
                        "type": "checkpoint_updated",
                        "workspace_id": workspace_id,
                        "checkpoint": checkpoint,
                    }
                )
                self._json({"checkpoint": checkpoint})
            case (("POST" | "DELETE"), ["annotations", *annotation_route]):
                self._write_annotation(workspace_id, repo, published, annotation_route, body)
            case _:
                raise OmmError("ERR_NOT_FOUND")

    def _published(self, workspace_id: str, result: SyncResult) -> None:
        self.server.broadcast(
            {
                "type": "revision_published",
                "workspace_id": workspace_id,
                "revision": result.revision,
            }
        )
        self._json(asdict(result))

    def _write_annotation(
        self,
        workspace_id: str,
        repo: _Repository,
        published: str | None,
        route: list[str],
        body: dict,
    ) -> None:
        def lookup(revision: str, block_id: str) -> BlockContext | None:
            return _context(self._load(workspace_id, repo, revision), block_id)

        root = repo.root
        match self.command, route:
            case "POST", []:
                annotation = create_annotation(
                    root,
                    block_id=_required_string(body, "block_id"),
                    base_rev=_require_revision(_required_string(body, "base_rev")),
                    selector=body.get("selector"),
                    comment=_required_string(body, "comment"),
                    lookup=lookup,
                    annotation_id=_optional_string(body, "id"),
                )
            case "POST", [annotation_id, "resolve"]:
                if published is None:
                    raise OmmError("ERR_NO_PUBLISHED_REVISION")
                after = _optional_string(body, "after_rev")
                revision = repo.resolve(after) if after is not None else published
                if not repo.is_ancestor(revision, published):
                    raise OmmError("ERR_REVISION_NOT_PUBLISHED", revision=revision)
                annotation = resolve_annotation(
                    root, annotation_id, after_rev=revision, lookup=lookup
                )
            case "POST", [annotation_id, "reopen"]:
                annotation = reopen_annotation(root, annotation_id)
            case "DELETE", [annotation_id]:
                delete_annotation(root, annotation_id)
                annotation = None
            case _:
                raise OmmError("ERR_NOT_FOUND")
        self.server.broadcast({"type": "annotations_changed", "workspace_id": workspace_id})
        if annotation is None:
            self._json({"id": annotation_id})
        else:
            revision = published or annotation.base_rev
            self._json(_annotation_json(annotation, self._load(workspace_id, repo, revision)))


def _invalid_json_constant(value: str) -> None:
    raise ValueError(value)


def _fs_path(path: Path) -> Path:
    """Extend long Windows paths past MAX_PATH when no long-path policy is enabled."""
    if os.name != "nt":
        return path
    text = str(path)
    if text.startswith("\\\\?\\") or len(text) < 240:
        return path
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC" + text[1:])
    return Path("\\\\?\\" + text)


def _safe_path(path: str) -> str:
    if "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise OmmError("ERR_NOT_FOUND")
    return normalize_relative_path(path)
