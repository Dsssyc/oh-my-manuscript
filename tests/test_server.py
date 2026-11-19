from __future__ import annotations

import hashlib
import json
import os
import subprocess
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from http.client import HTTPConnection, HTTPResponse
from pathlib import Path
from threading import Thread
from time import monotonic
from urllib.parse import urlencode

import pytest

from omm import ast
from omm.errors import OmmError
from omm.git_revs import PUBLISHED_REF, load_workspace_at_revision, resolve_revision, sync_workspace
from omm.i18n import MESSAGES
from omm.numbering import number_workspace
from omm.server import WorkbenchServer, _fs_path
from omm.workspace import MANIFEST, atomic_write_text, init_workspace, relocate_workspace

INTRO = "sections/01-introduction.md"
SUPPLEMENT = "sections/supplement.md"
REPLY = "sections/reply.md"
PANEL = "figures/panel.png"
SECOND_PANEL = "figures/second.png"
C1_TEXT = "Water flows slowly."
C2_TEXT = "Water flows towards the outlet."
BIB = """\
@article{zeta, author = {Kirchner, James}, title = {Zeta}, year = {2020}}
@article{alpha, author = {James {Kirchner}}, title = {{Alpha}}, year = {2020},
  journal = {Water Research}, volume = {12}, number = {3}, pages = {9--15}, doi = {10.1/example}}
@article{beven, author = {Beven, Keith J. and Michael J. Kirkby},
  title = {Hydrology}, year = {1979}}
@inproceedings{unused, author = {{Water and Soil Consortium} and Doe, Jane and Li, Tao},
  year = {2021}, title = {Observations}, booktitle = {Water Conference}}
"""


def write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty.gitconfig"))
    monkeypatch.setenv("OMM_DEV_SOURCE", "1")
    state = tmp_path / "state"
    return init_workspace(tmp_path / "workspace", "Catchment study", state_dir=state)


@pytest.fixture
def web_dist(tmp_path: Path) -> Path:
    root = tmp_path / "web"
    (root / "assets").mkdir(parents=True)
    write(root / "index.html", "<html><head><title>Workbench</title></head><body></body></html>")
    for extension in ("js", "css", "svg", "woff2", "woff", "ttf"):
        (root / "assets" / f"test.{extension}").write_bytes(b"test")
    return root


@contextmanager
def running_server(state: Path, **kwargs):
    server = WorkbenchServer(state_dir=state, port=0, **kwargs)
    thread = Thread(target=server.serve_forever, name="test-omm-http", daemon=True)
    thread.start()
    try:
        yield server, thread
    finally:
        if thread.is_alive():
            server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive()
        assert server.socket.fileno() == -1


@pytest.fixture
def server(workspace, web_dist):
    with running_server(
        workspace.root.parent / "state", web_dist_dir=web_dist, runtime_id="test-runtime"
    ) as (instance, _):
        yield instance


def browser_headers(server: WorkbenchServer) -> dict[str, str]:
    return {
        "Origin": f"http://{server.authority}",
        "Sec-Fetch-Site": "same-origin",
        "X-OMM-CSRF": server.csrf_token,
    }


def bearer(server: WorkbenchServer) -> dict[str, str]:
    return {"Authorization": f"Bearer {server.daemon_token}"}


def request(server: WorkbenchServer, method: str, path: str, *, body=None, headers=None):
    connection = HTTPConnection(*server.server_address, timeout=5)
    try:
        connection.request(
            method, path, body=None if body is None else json.dumps(body), headers=headers or {}
        )
        with connection.getresponse() as response:
            payload = response.read()
            assert not any(
                name.lower().startswith("access-control-allow-")
                for name, _ in response.getheaders()
            )
            if response.getheader("Content-Type", "").startswith("application/json"):
                payload = json.loads(payload)
            return response.status, response.headers, payload
    finally:
        connection.close()


def assert_error(response, status: int, code: str) -> None:
    assert response[0] == status, response
    assert response[2]["error"]["code"] == code
    assert response[2]["error"]["message"]


def test_request_security(server, workspace, monkeypatch):
    path = f"/api/workspaces/{workspace.workspace_id}/sync"
    for host in ("evil.com", f"localhost:{server.port}", "127.0.0.1"):
        assert_error(
            request(server, "GET", "/api/health", headers={"Host": host}),
            403,
            "ERR_FORBIDDEN_HOST",
        )
    assert_error(
        request(server, "OPTIONS", path, headers={"Host": "evil.com"}),
        403,
        "ERR_FORBIDDEN_HOST",
    )
    for changed, code in [
        ({"Origin": "https://example.org"}, "ERR_FORBIDDEN_ORIGIN"),
        ({"Sec-Fetch-Site": "cross-site"}, "ERR_FORBIDDEN_FETCH_SITE"),
        ({"X-OMM-CSRF": "wrong"}, "ERR_FORBIDDEN_CSRF"),
    ]:
        headers = browser_headers(server) | changed
        for method in ("POST", "PUT", "DELETE"):
            assert_error(request(server, method, path, body={}, headers=headers), 403, code)
    headers = browser_headers(server)
    del headers["X-OMM-CSRF"]
    assert_error(request(server, "POST", path, body={}, headers=headers), 403, "ERR_FORBIDDEN_CSRF")
    assert_error(request(server, "POST", path, body={}), 403, "ERR_FORBIDDEN_ORIGIN")
    for route in ("/api/shutdown", "/api/cli/notify"):
        assert_error(
            request(server, "POST", route, body={}, headers=browser_headers(server)),
            403,
            "ERR_FORBIDDEN_TOKEN",
        )
    for site in ("cross-site", "same-site"):
        assert_error(
            request(server, "GET", "/api/bootstrap", headers={"Sec-Fetch-Site": site}),
            403,
            "ERR_FORBIDDEN_FETCH_SITE",
        )
    monkeypatch.setenv("OMM_LANG", "zh-CN")
    response = request(server, "GET", "/api/health", headers={"Host": "evil.com"})
    assert response[2]["error"]["message"] == MESSAGES["zh-CN"]["ERR_FORBIDDEN_HOST"]
    assert request(server, "POST", path, body={}, headers=bearer(server))[0] == 200


def test_port_exclusive(server, workspace):
    assert server.server_address == ("127.0.0.1", server.port)
    with pytest.raises(OmmError, match="^ERR_PORT_IN_USE$") as error:
        WorkbenchServer(state_dir=workspace.root.parent / "state", port=server.port)
    assert error.value.diagnostic.params == {"port": server.port}
    assert request(server, "GET", "/api/health")[2] == {
        "status": "ok",
        "pid": os.getpid(),
        "port": server.port,
        "runtime_id": "test-runtime",
        "contract_version": "0.1.0",
    }


def test_bootstrap_static_assets_and_path_containment(server, web_dist):
    for site in ("none", "same-origin", None):
        headers = {"Sec-Fetch-Site": site} if site else {}
        assert request(server, "GET", "/api/bootstrap", headers=headers)[2] == {
            "csrf_token": server.csrf_token,
            "contract_version": "0.1.0",
        }
    for path in ("/", "/index.html"):
        status, headers, data = request(server, "GET", path)
        assert status == 200
        assert headers["Cache-Control"] == "no-store"
        bootstrap, tail = data.split(b"window.__OMM_BOOTSTRAP__ = ")[1].split(b";</script>")
        assert tail.startswith(b"</head>")
        assert json.loads(bootstrap) == {"csrfToken": server.csrf_token, "contractVersion": "0.1.0"}
        assert server.daemon_token.encode() not in data
    for ext, mime in {
        "js": "text/javascript",
        "css": "text/css",
        "svg": "image/svg+xml",
        "woff2": "font/woff2",
        "woff": "font/woff",
        "ttf": "font/ttf",
    }.items():
        status, headers, data = request(server, "GET", f"/assets/test.{ext}")
        assert (status, headers["Content-Type"], data) == (200, mime, b"test")
    long_asset = "/".join(("assets", "nested-" + "a" * 120, "nested-" + "b" * 120, "font.woff2"))
    font = web_dist / long_asset
    _fs_path(font.parent).mkdir(parents=True)
    _fs_path(font).write_bytes(b"long-path font")
    try:
        status, headers, data = request(server, "GET", f"/{long_asset}")
        assert (status, headers["Content-Type"], data) == (200, "font/woff2", b"long-path font")
    finally:
        # External cleanup tools cannot remove >260-char paths without \\?\ prefixes.
        _fs_path(font).unlink(missing_ok=True)
        _fs_path(font.parent).rmdir()
        _fs_path(font.parent.parent).rmdir()
    for path in ("/assets/%2e%2e/index.html", "/assets/../index.html", "/assets/%5C.."):
        assert_error(request(server, "GET", path), 404, "ERR_NOT_FOUND")
    secret = web_dist.parent / "outside.js"
    write(secret, "private")
    link = web_dist / "assets" / "outside.js"
    try:
        link.symlink_to(secret)
    except OSError:
        return  # Windows may require permission to create a symlink.
    assert_error(request(server, "GET", "/assets/outside.js"), 404, "ERR_NOT_FOUND")


def _figure(fid: str, *, multi: bool = False) -> str:
    images = (
        f"![Terrain]({PANEL}){{#fig-first-a}}\n![Stream]({SECOND_PANEL}){{#fig-first-b}}"
        if multi
        else f"![Terrain]({PANEL})"
    )
    return f":::{{figure}} {{#{fid}}}\n{images}\n\nCatchment [@beven].\n:::\n"


def _table(tid: str) -> str:
    return f"""\
:::{{table}} {{#{tid}}}
Performance [@zeta].

| Header | Value |
| :--- | ---: |
| Catchment | $x$ and [@beven] |

Note [@alpha].
:::
"""


def test_numbering_and_snapshot_contract(workspace, server):
    root = workspace.root
    with (root / MANIFEST).open("a", encoding="utf-8") as manifest:
        manifest.write(
            f'\n[deliverables.supplementary]\nkind = "supplementary"\nsources = ["{SUPPLEMENT}"]\n'
            f'\n[deliverables.reply]\nkind = "response_letter"\nsources = ["{REPLY}"]\n'
        )
    write(root / "references.bib", BIB)
    (root / PANEL).write_bytes(b"first image")
    (root / SECOND_PANEL).write_bytes(b"second image")
    write(
        root / INTRO,
        "# First {#sec-first}\n\n"
        "First [@zeta; @beven]. {#p-intro}\n\n"
        ":::{outline} {#out-draft}\nPlan [@unused] and @zeta.\n:::\n\n"
        + "# Second {#sec-second}\n\n"
        + "## Methods {#sec-methods}\n\n"
        + "We use **bold [@beven]** and @alpha. {#p-start}\n\n"
        + "A point between endpoints. {#p-between}\n\n"
        + "We show [#fig-first-a] with $x$. {#p-end}\n\n"
        + _figure("fig-first", multi=True)
        + "\n"
        + _table("tbl-first")
        + "\n$$x$$ {#eq-first}\n\n$$y$$ {#eq-unnum unnumbered}\n\n$$z$$ {#eq-next}\n",
    )
    write(
        root / SUPPLEMENT,
        "# Supplement {#sec-sup}\n\n## Method {#sec-sup-method}\n\n"
        "A detail. {#p-sup}\n\n"
        + _figure("fig-sup")
        + "\n"
        + _table("tbl-sup")
        + "\n$$a$$ {#eq-sup}\n",
    )
    write(
        root / REPLY,
        ':::{review-item} {#R1-C3 reviewer="Reviewer 1"}\n'
        ":::{comment}\nWe point to [#sec-first] and [#loc:p-intro]. See @unused.\n:::\n\n"
        "See [#loc:p-start..p-end] and [#loc:p-sup] and [#eq-first]. @alpha reports this. "
        "{#p-reply}\n:::\n",
    )
    published = sync_workspace(root).revision
    snapshot = load_workspace_at_revision(root, published)
    numbered = number_workspace(snapshot)
    assert numbered.review_target_index == {
        "eq-first": ["R1-C3"],
        "p-start": ["R1-C3"],
        "p-between": ["R1-C3"],
        "p-end": ["R1-C3"],
        "p-sup": ["R1-C3"],
    }
    assert numbered.references["alpha"].year == "2020a"
    assert numbered.references["zeta"].year == "2020b"
    assert numbered.references["unused"].cited is False
    assert numbered.references["unused"].short_label == "Water and Soil Consortium et al."
    assert "Water Conference" in numbered.references["unused"].formatted_text
    assert "Kirchner, J. (2020a). Alpha. Water Research, 12(3), 9–15." in (
        numbered.references["alpha"].formatted_text
    )
    numeric_snapshot = replace(
        snapshot,
        workspace=replace(
            snapshot.workspace, style=replace(snapshot.workspace.style, citation_style="numeric")
        ),
    )
    numeric = number_workspace(numeric_snapshot)
    assert list(numeric.references) == ["zeta", "beven", "alpha", "unused"]
    assert [item.number for item in numeric.references.values()] == [1, 2, 3, None]
    assert numeric.citation_label(ast.Citation(ast.Span(0, 0), ("zeta", "beven"), True)) == "[1, 2]"
    assert numeric.citation_label(ast.Citation(ast.Span(0, 0), ("beven",), False)) == "[2]"

    path = f"/api/workspaces/{workspace.workspace_id}/snapshot"
    status, _, data = request(server, "GET", path)
    assert status == 200
    blocks = {block["block_id"]: block for block in data["blocks"]}
    for block_id, expected in {
        "sec-first": ("1", "Section 1"),
        "sec-methods": ("2.1", "Section 2.1"),
        "sec-sup": ("S1", "Section S1"),
        "sec-sup-method": ("S1.1", "Section S1.1"),
        "fig-first": ("1", "Fig. 1"),
        "fig-sup": ("S1", "Fig. S1"),
        "tbl-first": ("1", "Table 1"),
        "tbl-sup": ("S1", "Table S1"),
        "eq-first": ("(1)", "Eq. (1)"),
        "eq-unnum": (None, None),
        "eq-next": ("(2)", "Eq. (2)"),
        "eq-sup": ("(S1)", "Eq. (S1)"),
        "R1-C3": ("R1-C3", "R1-C3"),
    }.items():
        assert (blocks[block_id]["number"], blocks[block_id]["label"]) == expected
    assert [(panel["sublabel"], panel["label"]) for panel in blocks["fig-first"]["subfigures"]] == [
        ("(a)", "Fig. 1(a)"),
        ("(b)", "Fig. 1(b)"),
    ]
    assert blocks["fig-sup"]["subfigures"][0]["sublabel"] is None
    assert (
        blocks["fig-sup"]["subfigures"][0]["asset_sha256"]
        == hashlib.sha256(b"first image").hexdigest()
    )
    assert blocks["tbl-first"]["rows"][0][1]["col_span"] == 1
    assert blocks["R1-C3"]["reply"][0]["kind"] == "paragraph"
    assert [location["label"] for location in blocks["R1-C3"]["location_refs"]] == [
        "manuscript §2.1 [p-start..p-end]",
        "supplementary §S1.1 [p-sup]",
    ]
    inlines = blocks["p-intro"]["inlines"]
    assert inlines[1] == {
        "kind": "citation",
        "span": [6, 21],
        "atomic": True,
        "keys": ["zeta", "beven"],
        "parenthetical": True,
        "label": "(Kirchner, 2020b; Beven & Kirkby, 1979)",
    }
    assert blocks["p-start"]["inlines"][3]["label"] == "Kirchner (2020a)"
    references = {item["citekey"]: item for item in data["references"]["entries"]}
    assert references["zeta"]["deliverable_counts"] == {"manuscript": 2, "supplementary": 1}
    assert references["unused"]["cited"] is False
    assert references["unused"]["citing_blocks"] == []
    assert references["alpha"]["citing_blocks"][-1] == {
        "block_id": "p-reply",
        "source": REPLY,
        "deliverable": "reply",
    }
    assert data["review_target_index"] == numbered.review_target_index


def test_published_snapshot_assets_and_cross_revision_annotations(workspace, server):
    root = workspace.root
    base = f"/api/workspaces/{workspace.workspace_id}"
    headers = browser_headers(server)
    response = request(server, "GET", f"{base}/snapshot")
    assert response[2] == {
        "workspace_id": workspace.workspace_id,
        "title": workspace.title,
        "published": False,
        "revision": None,
        "published_rev": None,
        "checkpoint_rev": None,
        "has_unsynced_changes": True,
        "milestones": [],
    }
    source = root / INTRO
    write(root / "references.bib", BIB)
    write(
        source, f"{C1_TEXT} {{#p-water}}\n\nDelete this. {{#p-delete}}\n\n" + _figure("fig-water")
    )
    (root / PANEL).write_bytes(b"C1 image")
    status, _, result = request(server, "POST", f"{base}/sync", headers=headers, body={})
    assert status == 200 and result["changed"]
    c1 = result["revision"]
    assert request(server, "POST", f"{base}/checkpoint", headers=headers, body={})[2] == {
        "checkpoint": c1
    }
    subprocess.run(["git", "tag", "v1-submitted", c1], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "tag",
            "--annotate",
            "--message",
            "Submission",
            "v1-annotated",
            c1,
        ],
        cwd=root,
        check=True,
    )
    blob = subprocess.run(
        ["git", "rev-parse", f"{c1}:{PANEL}"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "tag", "image-blob", blob], cwd=root, check=True)
    write(source, f"{C2_TEXT} {{#p-water}}\n\n" + _figure("fig-water"))
    (root / PANEL).write_bytes(b"C2 image")
    _, _, dirty = request(server, "GET", f"{base}/snapshot")
    assert dirty["revision"] == c1
    assert dirty["checkpoint_rev"] == c1
    assert dirty["has_unsynced_changes"] is True
    assert dirty["blocks"][0]["clean_source"] == C1_TEXT
    assert dirty["blocks"][-1]["asset_sha256"] == hashlib.sha256(b"C1 image").hexdigest()
    milestones = [
        {"name": "v1-annotated", "revision": c1},
        {"name": "v1-submitted", "revision": c1},
    ]
    assert request(server, "GET", "/api/workspaces")[2] == {
        "workspaces": [
            {
                "workspace_id": workspace.workspace_id,
                "path": str(root),
                "exists": True,
                "title": workspace.title,
                "published_rev": c1,
                "checkpoint_rev": c1,
                "has_unsynced_changes": True,
                "milestones": milestones,
            }
        ]
    }
    asset = f"{base}/assets?" + urlencode({"rev": c1, "path": PANEL})
    status, response_headers, data = request(server, "GET", asset)
    assert (status, response_headers["Content-Type"], data) == (200, "image/png", b"C1 image")
    assert (
        request(server, "GET", asset, headers={"If-None-Match": response_headers["ETag"]})[0] == 304
    )
    assert_error(
        request(server, "GET", f"{base}/assets?" + urlencode({"rev": "HEAD", "path": PANEL})),
        400,
        "ERR_BAD_REQUEST",
    )
    assert_error(
        request(server, "GET", f"{base}/assets?" + urlencode({"rev": c1, "path": "../" + PANEL})),
        404,
        "ERR_NOT_FOUND",
    )
    status, _, result = request(
        server, "POST", f"{base}/sync", headers=headers, body={"expected_rev": c1}
    )
    assert status == 200 and result["changed"]
    c2 = result["revision"]
    assert c2 != c1
    assert request(server, "GET", asset)[2] == b"C1 image"
    assert (
        request(server, "GET", f"{base}/assets?" + urlencode({"rev": c2, "path": PANEL}))[2]
        == b"C2 image"
    )
    historic_block = f"{base}/blocks/p-delete"
    assert request(server, "GET", f"{historic_block}?rev={c1}")[2] == {
        "revision": c1,
        "block_id": "p-delete",
        "found": True,
        "clean_source": "Delete this.",
        "kind": "paragraph",
        "source": INTRO,
    }
    assert request(server, "GET", f"{historic_block}?rev={c2}")[2]["found"] is False
    status, _, annotation = request(
        server,
        "POST",
        f"{base}/annotations",
        headers=headers,
        body={
            "block_id": "p-water",
            "base_rev": c1,
            "selector": {"kind": "text", "start": 0, "end": 5, "quote": "Water"},
            "comment": "Describe the direction.",
        },
    )
    assert status == 200
    assert annotation["before_block_md"] == C1_TEXT
    path = f"{base}/annotations/{annotation['id']}"
    assert (
        request(server, "POST", f"{path}/resolve", body={}, headers=headers)[2]["status"]
        == "resolved"
    )
    status, _, snapshot = request(server, "GET", f"{base}/snapshot?diff_base={c1}")
    assert status == 200
    assert snapshot["revision"] == c2 and not snapshot["has_unsynced_changes"]
    assert snapshot["blocks"][0]["clean_source"] == C2_TEXT
    [annotation] = snapshot["annotations"]
    assert annotation["base_rev"] == c1 and annotation["anchor"] == "block_modified"
    [resolution] = annotation["resolutions"]
    assert resolution["after_rev"] == c2
    assert resolution["after_block_md"] == C2_TEXT
    assert resolution["diff_chunks"] == snapshot["diff"]["blocks"]["p-water"]["chunks"]
    assert snapshot["diff"]["blocks"]["fig-water"]["asset_changed"]
    assert snapshot["diff"]["blocks"]["p-delete"]["status"] == "deleted"
    assert request(server, "POST", f"{path}/reopen", headers=headers)[2]["status"] == "open"
    assert request(server, "DELETE", path, headers=headers)[2] == {"id": annotation["id"]}
    assert request(server, "GET", f"{base}/snapshot")[2]["annotations"] == []


def test_outline_save_conflict_and_rollback(workspace, server):
    root = workspace.root
    source = root / INTRO
    original = source.read_bytes().replace(b"\n", b"\r\n")
    source.write_bytes(original)
    c1 = sync_workspace(root).revision
    base = f"/api/workspaces/{workspace.workspace_id}"
    path = f"{base}/outlines/out-introduction"
    headers = browser_headers(server)

    def save(revision: str, body: str):
        return request(
            server,
            "PUT",
            path,
            headers=headers,
            body={"expected_rev": revision, "body_markdown": body},
        )

    body = "- Develop the central argument.\n\n```text\n:::\n```"
    status, _, result = save(c1, body)
    assert status == 200 and result["changed"]
    c2 = result["revision"]
    assert c2 != c1 and resolve_revision(root, f"{c2}^") == c1
    synced = source.read_bytes()
    assert synced == original.replace(
        b"- Describe the research question and supporting evidence.",
        body.replace("\n", "\r\n").encode(),
    )
    blocks = request(server, "GET", f"{base}/snapshot")[2]["blocks"]
    outline = blocks[1]
    start, end = outline["body_span"]
    assert outline["body_markdown"] == outline["clean_source"][start:end] == body
    assert save(c2, body)[2] == {"revision": c2, "changed": False}
    assert_error(save(c1, "Obsolete edit"), 409, "ERR_OUTLINE_CONFLICT")
    assert source.read_bytes() == synced
    assert_error(
        save(c2, "::: \n\nNew paragraph {#p-other}"), 422, "ERR_DIRECTIVE_UNEXPECTED_CLOSE"
    )
    assert source.read_bytes() == synced
    assert_error(save(c2, "A missing citation [@missing]."), 422, "ERR_CITATION_MISSING")
    assert source.read_bytes() == synced
    assert resolve_revision(root, PUBLISHED_REF) == c2
    dirty = synced + b"\r\nUnsynced text. {#p-dirty}\r\n"
    source.write_bytes(dirty)
    assert_error(save(c2, "Would overwrite local edits"), 409, "ERR_OUTLINE_CONFLICT")
    assert source.read_bytes() == dirty


def _event(response: HTTPResponse, event_type: str) -> dict:
    deadline = monotonic() + 5
    while monotonic() < deadline:
        line = response.readline()
        assert line, f"SSE closed before {event_type}"
        if line.startswith(b"data: "):
            event = json.loads(line[6:])
            if event["type"] == event_type:
                return event
    pytest.fail(f"SSE did not send {event_type}")


def test_sse_poll_notify_and_shutdown(workspace):
    root = workspace.root
    state = root.parent / "state"
    source = root / INTRO
    c1 = sync_workspace(root).revision
    with (
        running_server(state, poll_interval=0.05) as (server, thread),
        ExitStack() as clients,
    ):
        streams = []
        for _ in range(2):
            connection = HTTPConnection(*server.server_address, timeout=5)
            clients.callback(connection.close)
            connection.request("GET", "/api/events")
            response = clients.enter_context(connection.getresponse())
            assert response.status == 200
            assert response.getheader("Content-Type") == "text/event-stream"
            assert response.getheader("Cache-Control") == "no-cache"
            assert not any(
                name.lower().startswith("access-control-allow-")
                for name, _ in response.getheaders()
            )
            assert response.readline() == b": connected\n"
            assert response.readline() == b"\n"
            streams.append(response)
        write(source, f"{C2_TEXT} {{#p-water}}\n")
        for response in streams:
            assert _event(response, "unsynced_status") == {
                "type": "unsynced_status",
                "workspace_id": workspace.workspace_id,
                "published_rev": c1,
                "has_unsynced_changes": True,
            }
        c2 = sync_workspace(root).revision
        for response in streams:
            assert _event(response, "revision_published") == {
                "type": "revision_published",
                "workspace_id": workspace.workspace_id,
                "revision": c2,
            }
            assert _event(response, "unsynced_status") == {
                "type": "unsynced_status",
                "workspace_id": workspace.workspace_id,
                "published_rev": c2,
                "has_unsynced_changes": False,
            }
        notify = {"type": "annotations_changed", "workspace_id": workspace.workspace_id}
        assert (
            request(server, "POST", "/api/cli/notify", body=notify, headers=bearer(server))[0]
            == 200
        )
        for response in streams:
            assert _event(response, "annotations_changed") == notify
        changed = {"type": "workspaces_changed"}
        notify_path = "/api/cli/notify"
        headers = bearer(server)
        for event in (changed, {**changed, "workspace_id": workspace.workspace_id}):
            assert request(server, "POST", notify_path, body=event, headers=headers)[0] == 200
            for response in streams:
                assert _event(response, "workspaces_changed") == event
        for event in (
            {**changed, "workspace_id": None},
            {**changed, "workspace_id": 1},
            *(
                {"type": kind}
                for kind in (
                    "annotations_changed",
                    "checkpoint_updated",
                    "revision_published",
                    "unsynced_status",
                )
            ),
        ):
            assert_error(
                request(server, "POST", notify_path, body=event, headers=headers),
                400,
                "ERR_BAD_REQUEST",
            )

        second = init_workspace(root.parent / "second", state_dir=state)
        for response in streams:
            assert _event(response, "workspaces_changed") == changed
            assert _event(response, "unsynced_status")["workspace_id"] == second.workspace_id
        moved = root.parent / "relocated"
        second.root.rename(moved)
        relocate_workspace(moved, state_dir=state)
        for response in streams:
            assert _event(response, "workspaces_changed") == changed
            assert _event(response, "unsynced_status")["workspace_id"] == second.workspace_id
        registry = state / "workspaces.json"
        data = json.loads(registry.read_text(encoding="utf-8"))
        del data["workspaces"][second.workspace_id]
        atomic_write_text(registry, json.dumps(data))
        for response in streams:
            assert _event(response, "workspaces_changed") == changed

        assert request(server, "POST", "/api/shutdown", headers=bearer(server))[2] == {
            "status": "stopping"
        }
        thread.join(timeout=5)
        assert not thread.is_alive()
        for response in streams:
            assert not response.read().strip()
        assert not server._subscribers
        assert server._poller is not None and not server._poller.is_alive()
