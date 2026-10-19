"""`celmis-pack` puts a folder on the server as a pack, checks that it arrived and
fetches it back — with a publish token, never printing it.

Run as a subprocess (`python3 -I`, a bare environment) against the REAL codebase
router served over a socket, so what is tested is what a laptop or a CI job gets:

  * `push` builds the zip with the server's own rules (a copy of the limits and
    the ignore rules, held equal to `src/knowledge/ingest.py` below), prints the
    pack, the version, the files, the skills, the ignored count and the server's
    sha; a dry run stores nothing; the same files again make no new version;
  * `status` shows the pack and its usage, and with a folder says `in sync` (exit
    0) or lists what changed, is new or was removed (exit 3);
  * `verify` also asks the MCP dev endpoint, as the person (their own token), which
    skills they can see, over JSON or an event stream, across pages;
  * `pull` writes the current version into an empty or new folder and nothing
    outside it, whatever the archive says;
  * a refusal by the server is shown as code, message and `path:line label`, never
    as file contents; a refusal checked here happens before any request;
  * the token is not in anything printed, on any path; an http address (other than
    this machine) and a token with odd characters are refused before anything is
    sent; a redirect is not followed with the token in hand.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from tests.api.codebase_support import call
from tests.api.rbac_world import world
from tests.plugin import contract as C

CLI = C.PLUGIN / "bin" / "celmis-pack"
SECRET_SHAPE = "AKIA" + "ABCDEFGHIJKLMNOP"          # an AWS-style key id, assembled here
CONSUMER = "consumer.token-1234"


def load_cli():
    loader = importlib.machinery.SourceFileLoader("celmis_pack_under_test", str(CLI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cli = load_cli()

SKILL = ("---\nname: billing-api\ndescription: How the billing API is called\n---\n"
         "# Billing\n\nCall the endpoint with the key from the BILLING_KEY variable.\n")


# ─── Running it ──────────────────────────────────────────────────────


def run(*args, url=None, token=None, consumer=None, cwd=None, extra=None):
    """The tool in a bare environment. Returns (exit code, stdout, stderr)."""
    env = {"PATH": "/nonexistent", "HOME": "/nonexistent", "LANG": "C.UTF-8"}
    if url is not None:
        env["CELMIS_URL"] = url
    if token is not None:
        env["CELMIS_PACK_TOKEN"] = token
    if consumer is not None:
        env["CELMIS_TOKEN"] = consumer
    env.update(extra or {})
    done = subprocess.run([sys.executable, "-I", str(CLI), *map(str, args)], capture_output=True,
                          text=True, env=env, timeout=60, cwd=cwd)
    return done.returncode, done.stdout, done.stderr


def write(root: Path, files: dict[str, str | bytes]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return root


def pack_folder(tmp_path: Path, name="skills", **more) -> Path:
    files = {"README.md": "# Ledger\n", "billing-api/SKILL.md": SKILL, "billing-api/ref.md": "# Ref\n",
             ".git/config": "[core]\n", "logo.png": b"\x89PNG\r\n"}
    files.update(more)
    return write(tmp_path / name, files)


# ─── A real server, a fake MCP endpoint ──────────────────────────────


class FakeMcp:
    """Just enough of /mcp/dev/ to check the client's side of the protocol."""

    def __init__(self):
        self.token = CONSUMER
        self.sse = False
        self.page = 100
        self.catalogue: list[str] = []
        self.log: list[tuple[str, str | None]] = []       # (method, session header)
        self.closed = False


def mount_mcp(app, mcp: FakeMcp):
    @app.post("/mcp/dev/")
    async def dev(request: Request):
        if request.headers.get("authorization") != f"Bearer {mcp.token}":
            return Response(status_code=401)
        msg = await request.json()
        method = msg.get("method")
        sid = request.headers.get("mcp-session-id")
        mcp.log.append((method, sid))
        if method == "notifications/initialized":
            return Response(status_code=202 if sid == "sess-1" else 400)
        headers = {"Mcp-Session-Id": "sess-1"} if method == "initialize" else {}
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "x"}}
        elif method == "tools/call" and sid == "sess-1" and msg["params"]["name"] == "skills":
            cursor = (msg["params"].get("arguments") or {}).get("cursor")
            off = int(cursor) if cursor else 0
            lines = mcp.catalogue[off:off + mcp.page]
            left = len(mcp.catalogue) - off - len(lines)
            text = "\n".join(lines) + (f"\n… +{left} more · cursor={off + len(lines)}" if left > 0 else "")
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        else:
            return Response(status_code=400)
        body = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        if mcp.sse:
            return Response(f"event: message\ndata: {json.dumps(body)}\n\n",
                            media_type="text/event-stream", headers=headers)
        return JSONResponse(body, headers=headers)

    @app.delete("/mcp/dev/")
    async def close(request: Request):
        mcp.closed = request.headers.get("mcp-session-id") == "sess-1"
        return Response(status_code=204)


@contextlib.asynccontextmanager
async def served(tmp_path, monkeypatch):
    """(world, base url, FakeMcp) with the app listening on 127.0.0.1."""
    import uvicorn

    async with world(tmp_path, monkeypatch) as w:
        mcp = FakeMcp()
        mount_mcp(w.app, mcp)
        server = uvicorn.Server(uvicorn.Config(w.app, host="127.0.0.1", port=0, log_level="warning",
                                               lifespan="off"))
        task = asyncio.create_task(server.serve())
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started, "the test server did not start"
        port = server.servers[0].sockets[0].getsockname()[1]
        try:
            yield w, f"http://127.0.0.1:{port}", mcp
        finally:
            server.should_exit = True
            await task


async def new_pack(w, slug="ledger", who="editor_a", **body) -> tuple[str, str]:
    """(pack id, publish token)"""
    r = await call(w, who, "POST", "/api/codebase/packs", json={
        "slug": slug, "title": f"Pack {slug}", "audience": "teams", **body})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    t = await call(w, who, "POST", f"/api/codebase/packs/{pid}/tokens", json={"label": "ci"})
    assert t.status_code == 201, t.text
    return pid, t.json()["token"]


async def versions(w, pid) -> list[int]:
    r = await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}/versions")
    return sorted(v["number"] for v in r.json()["versions"])


async def tool(*args, **kw):
    return await asyncio.to_thread(lambda: run(*args, **kw))


def assert_no_token(token: str, *outputs: str):
    body = token[len("cpk_"):]
    for text in outputs:
        assert token not in text and body not in text and hashlib.sha256(token.encode()).hexdigest() not in text


# ─── push ────────────────────────────────────────────────────────────


async def test_push_sends_the_folder_and_prints_what_the_server_stored(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as (w, url, _mcp):
        pid, token = await new_pack(w)
        folder = pack_folder(tmp_path)
        code, out, err = await tool("push", folder, "--note", "first", url=url, token=token)
        assert code == 0, (out, err)
        lines = dict(line.split(": ", 1) for line in out.splitlines() if ": " in line and not line.startswith(" "))
        assert lines["pack"] == "ledger (Pack ledger)" and lines["version"] == "1"
        assert lines["files"] == "3" and lines["skills"] == "billing-api"
        assert lines["ignored"] == "2"                        # .git/ and logo.png
        assert ".git/" in out and "logo.png" in out
        detail = (await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}/versions")).json()
        assert [v["sha"] for v in detail["versions"]] == [lines["sha"]]
        assert detail["versions"][0]["note"] == "first"
        # the sha the tool computes locally is the server's
        assert cli.read_folder(str(folder)).version_sha() == lines["sha"]
        assert err == ""
        assert_no_token(token, out, err)


async def test_the_same_files_again_make_no_version_and_a_change_makes_one(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as (w, url, _mcp):
        pid, token = await new_pack(w)
        folder = pack_folder(tmp_path)
        assert (await tool("push", folder, url=url, token=token))[0] == 0
        code, out, _ = await tool("push", folder, url=url, token=token)
        assert code == 0 and "unchanged: version 1" in out and await versions(w, pid) == [1]
        (folder / "billing-api" / "ref.md").write_text("# Ref\n\nMore.\n")
        code, out, _ = await tool("push", folder, url=url, token=token)
        assert code == 0 and "version: 2" in out and await versions(w, pid) == [1, 2]


async def test_a_dry_run_asks_the_server_and_stores_nothing(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as (w, url, _mcp):
        pid, token = await new_pack(w)
        code, out, err = await tool("push", pack_folder(tmp_path), "--dry-run", url=url, token=token)
        assert code == 0, (out, err)
        assert "dry run: nothing was stored" in out and "billing-api/SKILL.md" in out
        assert await versions(w, pid) == []
        status = await tool("status", url=url, token=token)
        assert "version: none yet" in status[1]


async def test_a_secret_is_refused_by_the_server_and_shown_as_a_place_never_as_text(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as (w, url, _mcp):
        pid, token = await new_pack(w)
        folder = pack_folder(tmp_path, **{"billing-api/notes.md": f"key = {SECRET_SHAPE}\n"})
        code, out, err = await tool("push", folder, url=url, token=token)
        assert code == 4 and out == ""
        assert "secrets_found" in err and "billing-api/notes.md:1" in err
        assert SECRET_SHAPE not in out + err
        assert_no_token(token, out, err)
        assert await versions(w, pid) == []


async def test_a_line_the_server_took_on_the_allow_marker_is_printed_as_place_and_rule_only(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as (w, url, _mcp):
        pid, token = await new_pack(w)
        example = f"Never do this:\n\nkey = '{SECRET_SHAPE}'  # celmis:allow-secret\n"
        folder = pack_folder(tmp_path, **{"billing-api/example.md": example})
        code, out, err = await tool("push", folder, "--dry-run", url=url, token=token)
        assert code == 0, (out, err)
        assert "allowed by celmis:allow-secret (1):" in out and "billing-api/example.md:3 " in out
        code, out, err = await tool("push", folder, url=url, token=token)
        assert code == 0, (out, err)
        assert "allowed by celmis:allow-secret (1):" in out and "billing-api/example.md:3 " in out
        assert SECRET_SHAPE not in out + err
        assert_no_token(token, out, err)


@pytest.mark.parametrize("make,code_word", [
    (lambda r: write(r, {f"f{i}.md": "x\n" for i in range(401)}), "too_many_files"),
    (lambda r: write(r, {"big.md": "x" * (512 * 1024 + 1)}), "file_too_large"),
    (lambda r: write(r, {f"b{i}.md": "x" * (500 * 1024) for i in range(17)}), "total_too_large"),
    (lambda r: write(r, {"bin.md": b"\xff\xfe\x00\x01"}), "binary_file"),
    (lambda r: write(r, {"nul.md": b"text\x00more"}), "binary_file"),
    (lambda r: write(r, {"only.png": b"\x89PNG", ".hidden/x.md": "x"}), "no_files"),
    (lambda r: write(r, {"a/b/c/d/e/f/g/h/i/j/k.md": "x\n", "README.md": "x\n"}), "too_deep"),
    (lambda r: write(r, {"back\\slash.md": "x\n"}), "unsafe_path"),
])
def test_what_the_server_would_refuse_is_refused_here_before_any_request(tmp_path, make, code_word):
    folder = make(tmp_path / "f")
    # a port nothing listens on: a request would be exit 1, so exit 4 proves none was made
    code, out, err = run("push", folder, url=f"http://127.0.0.1:{free_port()}", token="cpk_" + "a" * 43)
    assert (code, out) == (4, ""), err
    assert f"({code_word})" in err


def test_a_folder_too_large_once_compressed_is_refused_here(tmp_path):
    import base64
    files = {f"r{i}.md": base64.b64encode(os.urandom(370 * 1024)).decode() for i in range(14)}
    code, _out, err = run("push", write(tmp_path / "f", files),
                          url=f"http://127.0.0.1:{free_port()}", token="cpk_" + "a" * 43)
    assert code == 4 and "(archive_too_large)" in err


def test_links_are_refused_here(tmp_path):
    folder = write(tmp_path / "f", {"README.md": "x\n", "real/note.md": "x\n"})
    (folder / "alias.md").symlink_to(folder / "README.md")
    url, token = f"http://127.0.0.1:{free_port()}", "cpk_" + "a" * 43
    assert run("push", folder, url=url, token=token)[0] == 4
    (folder / "alias.md").unlink()
    (folder / "dir-alias").symlink_to(folder / "real")
    code, _out, err = run("push", folder, url=url, token=token)
    assert code == 4 and "(symlink)" in err
    link = tmp_path / "link-to-folder"
    link.symlink_to(folder)
    assert run("push", link, url=url, token=token)[0] == 2           # DIR itself is not a link


def test_a_hidden_secret_file_is_not_sent_even_though_it_is_ignored(tmp_path):
    root = pack_folder(tmp_path)
    (root / ".env").write_text(f"KEY={SECRET_SHAPE}\n")
    folder = cli.read_folder(str(root))
    assert ".env" in folder.ignored and ".env" not in folder.raw
    assert SECRET_SHAPE.encode() not in folder.zip()


# ─── status ──────────────────────────────────────────────────────────


async def test_status_shows_the_pack_its_version_and_its_use(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as (w, url, _mcp):
        pid, token = await new_pack(w)
        folder = pack_folder(tmp_path)
        await tool("push", folder, url=url, token=token)
        code, out, err = await tool("status", url=url, token=token)
        assert code == 0 and err == ""
        assert "pack: ledger (Pack ledger), audience: teams" in out
        assert "version: 1, 3 files" in out and "usage, last 30 days: 0 calls" in out
        assert "billing-api: 0 opens" in out
        assert_no_token(token, out, err)


async def test_status_with_a_folder_says_in_sync_or_lists_the_difference(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as (w, url, _mcp):
        pid, token = await new_pack(w)
        folder = pack_folder(tmp_path)
        code, out, _ = await tool("status", folder, url=url, token=token)
        assert code == 3 and "nothing is published" in out and "new (3):" in out
        await tool("push", folder, url=url, token=token)
        code, out, _ = await tool("status", folder, url=url, token=token)
        assert code == 0 and "in sync: 3 files match version 1" in out
        (folder / "billing-api" / "ref.md").write_text("# Changed\n")
        (folder / "added.md").write_text("# Added\n")
        (folder / "README.md").unlink()
        code, out, _ = await tool("status", folder, url=url, token=token)
        assert code == 3 and "not in sync with version 1" in out
        assert "changed (1):\n  billing-api/ref.md" in out
        assert "new (1):\n  added.md" in out and "removed (1):\n  README.md" in out


async def test_a_byte_order_mark_and_composed_names_do_not_make_a_false_difference(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as (w, url, _mcp):
        pid, token = await new_pack(w)
        folder = pack_folder(tmp_path, **{"café.md": b"\xef\xbb\xbf# Cafe\n"})     # e + combining acute
        assert (await tool("push", folder, url=url, token=token))[0] == 0
        code, out, _ = await tool("status", folder, url=url, token=token)
        assert code == 0 and "in sync" in out, out


# ─── verify ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("sse", [False, True])
async def test_verify_passes_when_in_sync_and_every_skill_is_visible_to_you(tmp_path, monkeypatch, sse):
    async with served(tmp_path, monkeypatch) as (w, url, mcp):
        pid, token = await new_pack(w)
        folder = pack_folder(tmp_path, **{"second/SKILL.md": SKILL.replace("billing-api", "second")})
        await tool("push", folder, url=url, token=token)
        mcp.sse, mcp.page = sse, 1
        mcp.catalogue = ["ledger/billing-api — How the billing API is called", "ledger/second — x",
                         "ledger: 2 docs", "other/billing-api — someone else's"]
        code, out, err = await tool("verify", folder, url=url, token=token, consumer=CONSUMER)
        assert code == 0, (out, err)
        assert "in sync" in out and "visible to you: 2/2" in out
        assert [m for m, _ in mcp.log] == ["initialize", "notifications/initialized"] + ["tools/call"] * 4
        assert {sid for m, sid in mcp.log if m != "initialize"} == {"sess-1"}
        assert mcp.closed
        assert_no_token(token, out, err)
        assert CONSUMER not in out + err


async def test_verify_accepts_the_four_hundred_character_token_a_consumer_really_holds(tmp_path, monkeypatch):
    long_token = "ey" + "Ab0._-" * 66 + "xyz"                  # 399 characters, the shape of an MCP token
    async with served(tmp_path, monkeypatch) as (w, url, mcp):
        pid, token = await new_pack(w)
        folder = pack_folder(tmp_path)
        await tool("push", folder, url=url, token=token)
        mcp.token = long_token
        mcp.catalogue = ["ledger/billing-api — x"]
        code, out, err = await tool("verify", folder, url=url, token=token, consumer=long_token)
        assert code == 0 and "visible to you: 1/1" in out, (out, err)
        assert long_token not in out + err


async def test_verify_lists_the_skills_you_cannot_see_and_exits_3(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as (w, url, mcp):
        pid, token = await new_pack(w)
        folder = pack_folder(tmp_path, **{"second/SKILL.md": SKILL.replace("billing-api", "second")})
        await tool("push", folder, url=url, token=token)
        mcp.catalogue = ["ledger/billing-api — x", "other/second — a skill of another pack"]
        code, out, _ = await tool("verify", folder, url=url, token=token, consumer=CONSUMER)
        assert code == 3
        assert "visible to you: 1/2" in out and "not visible to you (1):\n  second" in out


async def test_verify_is_not_fine_when_the_folder_is_not_what_is_published(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as (w, url, mcp):
        pid, token = await new_pack(w)
        folder = pack_folder(tmp_path)
        await tool("push", folder, url=url, token=token)
        mcp.catalogue = ["ledger/billing-api — x", "ledger/fresh — a skill only on disk"]
        write(folder, {"fresh/SKILL.md": SKILL.replace("billing-api", "fresh")})
        code, out, _ = await tool("verify", folder, url=url, token=token, consumer=CONSUMER)
        assert code == 3 and "not in sync" in out
        assert "visible to you: 2/2" in out              # the stale catalogue is the fake's, the exit is the sync's


async def test_verify_says_so_when_your_own_token_is_not_accepted_or_missing(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as (w, url, mcp):
        pid, token = await new_pack(w)
        folder = pack_folder(tmp_path)
        await tool("push", folder, url=url, token=token)
        code, _out, err = await tool("verify", folder, url=url, token=token, consumer="wrong.token-1")
        assert code == 1 and "did not accept your Celmis token" in err and "wrong.token-1" not in err
        code, _out, err = await tool("verify", folder, url=url, token=token)
        assert code == 2 and "no token of yours" in err


# ─── pull ────────────────────────────────────────────────────────────


async def test_pull_writes_the_current_version_into_a_new_folder(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as (w, url, _mcp):
        pid, token = await new_pack(w)
        folder = pack_folder(tmp_path)
        await tool("push", folder, url=url, token=token)
        target = tmp_path / "fetched" / "again"
        code, out, err = await tool("pull", target, url=url, token=token)
        assert code == 0, (out, err)
        assert "pulled: version 1, 3 files" in out
        got = {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}
        assert got == {"README.md": b"# Ledger\n", "billing-api/SKILL.md": SKILL.encode(),
                       "billing-api/ref.md": b"# Ref\n"}
        assert (await tool("status", target, url=url, token=token))[0] == 0      # and it is in sync
        empty = tmp_path / "empty"
        empty.mkdir()
        assert (await tool("pull", empty, url=url, token=token))[0] == 0 and (empty / "README.md").is_file()
        assert [p.name for p in (tmp_path / "fetched").iterdir()] == ["again"]   # no staging left behind


async def test_pull_refuses_a_folder_that_is_not_empty_and_changes_nothing(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as (w, url, _mcp):
        pid, token = await new_pack(w)
        await tool("push", pack_folder(tmp_path), url=url, token=token)
        target = write(tmp_path / "mine", {"keep.md": "mine\n"})
        code, out, err = await tool("pull", target, url=url, token=token)
        assert code == 2 and out == "" and "not empty" in err
        assert [p.name for p in target.iterdir()] == ["keep.md"]
        file = write(tmp_path, {"afile": "x"}) / "afile"
        assert (await tool("pull", file, url=url, token=token))[0] == 2


async def test_pull_before_anything_is_published_says_so(tmp_path, monkeypatch):
    async with served(tmp_path, monkeypatch) as (w, url, _mcp):
        pid, token = await new_pack(w)
        target = tmp_path / "out"
        code, _out, err = await tool("pull", target, url=url, token=token)
        assert code == 1 and "nothing is published" in err and not target.exists()


# ─── stub servers: bad archives, redirects ───────────────────────────


class Stub:
    """A scripted server: `answer(method, path, headers, body) -> (status, headers, body)`."""

    def __init__(self, answer):
        self.requests: list[tuple[str, str, dict, bytes]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                stub.requests.append((self.command, self.path, dict(self.headers), body))
                status, headers, payload = answer(self.command, self.path, dict(self.headers), body)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_DELETE = _serve

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@contextlib.contextmanager
def stub(answer):
    s = Stub(answer)
    try:
        yield s
    finally:
        s.close()


def status_json(files: dict[str, str]) -> bytes:
    body = {"pack": {"id": "p", "slug": "ledger", "title": "Pack ledger", "audience": "teams"},
            "current_version": {"number": 1, "created_at": None, "file_count": len(files), "sha": "0" * 64},
            "files": [{"path": p, "sha": s, "size_bytes": 1, "kind": "file", "skill": None}
                      for p, s in files.items()],
            "usage": {"calls_30d": 0, "last_used_at": None, "skills": []}}
    return json.dumps(body).encode()


def zip_bytes(members) -> bytes:
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for item in members:
            if isinstance(item, zipfile.ZipInfo):
                zf.writestr(item, "linked")
            else:
                zf.writestr(*item)
    return buf.getvalue()


def archive_stub(archive: bytes, files: dict[str, str] | None = None):
    def answer(method, path, _headers, _body):
        if path.endswith("/publish/archive"):
            return 200, {"Content-Type": "application/zip"}, archive
        return 200, {"Content-Type": "application/json"}, status_json(files or {})
    return stub(answer)


def symlink_member(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = 0o120777 << 16
    return info


@pytest.mark.parametrize("name,members", [
    ("parent", [("../evil.md", "x")]),
    ("deep parent", [("a/../../evil.md", "x")]),
    ("absolute", [("/tmp/evil-abs.md", "x")]),
    ("backslash", [("..\\evil.md", "x")]),
    ("drive", [("C:evil.md", "x")]),
    ("dot segment", [("a/./b.md", "x")]),
    ("control", [("a\x01b.md", "x")]),
    ("link", [symlink_member("link.md")]),
    ("twice", [("a.md", "x"), ("A.md", "y")]),
])
def test_pull_will_not_write_what_an_archive_names_outside_its_folder(tmp_path, name, members):
    target = tmp_path / "area" / "out"
    (tmp_path / "area").mkdir()
    before = {p.name for p in tmp_path.iterdir()}
    token = "cpk_" + "a" * 43
    with archive_stub(zip_bytes(members)) as s:
        code, out, err = run("pull", target, url=s.url, token=token)
    assert code == 4 and out == "", (name, err)
    assert not target.exists()
    assert {p.name for p in (tmp_path / "area").iterdir()} == set()           # no staging folder left
    assert {p.name for p in tmp_path.iterdir()} == before
    assert not Path("/tmp/evil-abs.md").exists()


def test_pull_will_not_expand_an_archive_past_what_a_pack_may_hold(tmp_path):
    big = zip_bytes([(f"f{i}.md", "x" * (500 * 1024)) for i in range(17)])        # compresses small
    with archive_stub(big) as s:
        code, _out, err = run("pull", tmp_path / "out", url=s.url, token="cpk_" + "a" * 43)
    assert code == 4 and "more than a pack may hold" in err and not (tmp_path / "out").exists()
    many = zip_bytes([(f"f{i}.md", "x") for i in range(401)])
    with archive_stub(many) as s:
        code, _out, err = run("pull", tmp_path / "out2", url=s.url, token="cpk_" + "a" * 43)
    assert code == 4 and not (tmp_path / "out2").exists()


def test_pull_notices_when_the_files_are_not_the_ones_the_server_listed(tmp_path):
    archive = zip_bytes([("a.md", "# A\n")])
    with archive_stub(archive, files={"a.md": "f" * 64}) as s:
        code, _out, err = run("pull", tmp_path / "out", url=s.url, token="cpk_" + "a" * 43)
    assert code == 3 and "do not match" in err


def test_a_redirect_is_not_followed_with_the_token_in_hand(tmp_path):
    with stub(lambda *a: (200, {}, b"{}")) as elsewhere:
        target = elsewhere.url + "/api/codebase/publish"
        with stub(lambda *a: (302, {"Location": target}, b"")) as origin:
            token = "cpk_" + "a" * 43
            code, out, err = run("status", url=origin.url, token=token)
    assert code == 1 and "redirected" in err
    assert elsewhere.requests == []                      # the token never went anywhere else
    assert_no_token(token, out, err)


def test_a_server_that_echoes_the_token_back_still_cannot_make_the_tool_print_it(tmp_path):
    token = "cpk_" + "Secret-Echo_" + "b" * 30

    def answer(_m, _p, headers, _b):
        sent = headers.get("Authorization", "")
        return 500, {"Content-Type": "application/json"}, json.dumps({"detail": f"boom {sent}"}).encode()

    with stub(answer) as s:
        code, out, err = run("status", url=s.url, token=token)
    assert code == 1 and "HTTP 500" in err
    assert token not in out + err and "Secret-Echo_" not in out + err


@pytest.mark.parametrize("status,words,code", [
    (401, "did not accept the pack token", 1), (404, "not found", 1), (413, "larger than the server accepts", 4),
    (429, "too many requests", 1), (503, "HTTP 503", 1)])
def test_the_ways_a_request_fails_are_said_in_words_and_exit_codes(tmp_path, status, words, code):
    with stub(lambda *a: (status, {"Content-Type": "application/json"}, b'{"detail":"x"}')) as s:
        got, out, err = run("status", url=s.url, token="cpk_" + "a" * 43)
    assert (got, out) == (code, "") and words in err


@pytest.mark.parametrize("header,words", [
    ("37", "try again in 37 seconds"), ("1", "try again in 1 second"), ("0", "try again in a minute"),
    ("soon", "try again in a minute"), ("99999", "try again in a minute"), (None, "try again in a minute")])
def test_a_refusal_for_too_many_requests_says_how_many_seconds_the_server_asked_for(header, words):
    answered = {"Content-Type": "application/json", **({"Retry-After": header} if header else {})}
    with stub(lambda *a: (429, answered, b'{"detail":"rate limit exceeded"}')) as s:
        code, out, err = run("status", url=s.url, token="cpk_" + "a" * 43)
    assert (code, out) == (1, "") and words in err


def test_a_server_that_does_not_answer_or_is_not_there_is_exit_1(tmp_path):
    token = "cpk_" + "a" * 43
    code, out, err = run("status", url=f"http://127.0.0.1:{free_port()}", token=token)
    assert code == 1 and "could not reach the server" in err and out == ""
    assert_no_token(token, out, err)


def test_something_that_is_not_a_celmis_server_is_said_plainly(tmp_path):
    with stub(lambda *a: (200, {"Content-Type": "text/html"}, b"<html>hi</html>")) as s:
        code, _out, err = run("status", url=s.url, token="cpk_" + "a" * 43)
    assert code == 1 and "not a Celmis pack status" in err


# ─── settings ────────────────────────────────────────────────────────

GOOD = "cpk_" + "a" * 43


@pytest.mark.parametrize("url", [
    "http://celmis.example.com", "http://localhost.evil.example.com", "http://127.0.0.1.evil.example.com",
    "ftp://celmis.example.com", "celmis.example.com", "https://user:pw@celmis.example.com",
    "https://celmis.example.com?x=1", "https://", "",
])
def test_an_address_that_is_not_https_or_this_machine_is_refused_before_anything_is_sent(url):
    code, out, err = run("status", url=url, token=GOOD)
    assert (code, out) == (2, "") and "CELMIS_URL" in err
    assert_no_token(GOOD, err)


@pytest.mark.parametrize("url", ["http://localhost:1", "http://127.0.0.1:1", "http://[::1]:1"])
def test_this_machine_may_use_http(url):
    code, _out, err = run("status", url=url, token=GOOD)
    assert code == 1 and "could not reach the server" in err           # it tried; nothing listens


@pytest.mark.parametrize("token", [
    "cpk_ has a space", "cpk_new\nline", "cpk_quo\"te", "cpk_semi;colon", "cpk_é",
    "cpk_$(touch x)", "cpk_a b",
])
def test_a_token_with_characters_a_token_never_has_is_refused_and_not_echoed(token):
    code, out, err = run("status", url="https://celmis.example.com", token=token)
    assert (code, out) == (2, "") and "characters a token never has" in err
    assert token.strip()[4:] not in err


def test_a_token_of_four_hundred_characters_is_a_token_and_is_sent():
    token = "cpk_" + "a" * 396                                 # the length of a consumer's MCP token
    with stub(lambda *a: (200, {"Content-Type": "application/json"}, status_json({}))) as s:
        code, out, err = run("status", url=s.url, token=token)
    assert code == 0 and s.requests and token not in out + err


def test_a_token_longer_than_any_token_is_called_too_long_and_not_echoed():
    token = "cpk_" + "a" * cli.MAX_TOKEN_CHARS
    code, out, err = run("status", url="https://celmis.example.com", token=token)
    assert (code, out) == (2, "") and "too long" in err and "never has" not in err
    assert token[4:] not in err


def test_a_value_that_is_not_a_pack_token_is_refused_without_a_request(tmp_path):
    code, _out, err = run("status", url="https://celmis.example.com", token="eyJhbGciOiJIUzI1NiJ9.e30.abc")
    assert code == 2 and "not a pack token" in err and "eyJhbGci" not in err


def test_no_token_anywhere_is_exit_2():
    code, out, err = run("status", url="https://celmis.example.com")
    assert (code, out) == (2, "") and "no pack token" in err


def test_the_arguments_are_checked(tmp_path):
    assert run("push", url="https://c.example.com", token=GOOD)[0] == 2                 # DIR missing
    assert run("nonsense", url="https://c.example.com", token=GOOD)[0] == 2
    assert run("push", tmp_path / "missing", url="https://c.example.com", token=GOOD)[0] == 2
    assert run("push", pack_folder(tmp_path), "--note", "x" * 501, url="https://c.example.com",
               token=GOOD)[0] == 2
    assert run()[0] == 2


# ─── the file itself ─────────────────────────────────────────────────


def test_the_tool_is_an_executable_standard_library_script_that_documents_its_exit_codes():
    assert CLI.stat().st_mode & 0o111, "celmis-pack must be executable"
    text = CLI.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env python3\n")
    tree = ast.parse(text)
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
    code, out, _ = run("--help")
    assert code == 0
    for word in ("push", "status", "verify", "pull", "CELMIS_URL", "CELMIS_PACK_TOKEN", "exit codes"):
        assert word in out, word
    for number in "01234":
        assert f"\n  {number}  " in out, f"exit code {number} is not documented"


def test_it_names_no_company_and_no_credential():
    assert C.forbidden_hits([CLI]) == []
    text = CLI.read_text(encoding="utf-8")
    assert "cpk_" in text and not any(
        line.strip().startswith("print") and "token" in line.lower() and "_scrub" not in line
        for line in text.splitlines()), "a print of a token"


def test_a_planted_module_in_the_working_folder_is_not_imported(tmp_path):
    planted = tmp_path / "planted"
    write(planted, {"json.py": "open('PWNED', 'w').write('x')\n", "argparse.py": "open('PWNED', 'w').write('x')\n"})
    code, out, _ = run("--help", cwd=planted)
    assert code == 0 and "celmis-pack" in out and not (planted / "PWNED").exists()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ─── kept equal to the server ────────────────────────────────────────


def test_the_limits_and_lists_are_the_servers():
    from src.knowledge import ingest, store

    for name in ("MAX_ARCHIVE_BYTES", "MAX_FILES", "MAX_ENTRIES", "MAX_FILE_BYTES", "MAX_TOTAL_BYTES",
                 "MAX_PATH_CHARS", "MAX_DEPTH", "TEXT_EXTENSIONS", "SKILL_FILE"):
        assert getattr(cli, name) == getattr(ingest, name), name
    assert cli.MAX_NOTE == store.MAX_NOTE
    assert cli.TOKEN_PREFIX == store.TOKEN_PREFIX
    assert cli.MAX_TOKEN_CHARS >= store.MAX_PUBLISH_TOKEN_CHARS      # the bound here is wider: MCP tokens pass it too


PATHS = ["a.md", "./a.md", "a/b.md", "a//b.md", "a/./b.md", "a/../b.md", "../a.md", "/a.md", "C:a.md", "a\\b.md",
         "a\x00b.md", "a\x1fb.md", "é.md", "é.md", "a/", "", ".", "..", "a/b/", ".hidden/a.md",
         "a/.b.md", "__MACOSX/a.md", "x/__MACOSX/y.md", "a.PNG", "A.MD", "x.env.example", "x.ENV.EXAMPLE",
         "notes.markdown", "Makefile", "a/b/c.tsx", "script.sh", "data.csv", "image.svg"]


def test_the_path_rules_agree_with_the_servers_on_every_case():
    from src.knowledge import ingest

    for path in PATHS:
        try:
            theirs = ingest._clean_path(path)
        except ingest.IngestError:
            theirs = None
        try:
            ours = cli._clean_path(path)
        except cli.Stop:
            ours = None
        assert ours == theirs, repr(path)
        if theirs is not None:
            assert cli._is_hidden(theirs) == ingest._is_hidden(theirs), path
            assert cli._extension_allowed(theirs) == ingest._extension_allowed(theirs), path


def test_the_top_folder_and_the_slug_rules_agree_with_the_servers():
    from src.knowledge import ingest

    for paths in (["a/x.md", "a/y.md"], ["a/x.md", "b/y.md"], ["x.md", "a/y.md"], ["SKILL.md", "a/y.md"],
                  ["a/SKILL.md", "a/y.md"], ["a/b/x.md", "a/b/y.md"], []):
        assert cli._common_top_dir(paths) == ingest._common_top_dir(paths), paths
    for text in ("Billing API", "  --x--  ", "Été", "a" * 100, "!!!", "snake_case.v2", ""):
        assert cli.slugify(text) == ingest.slugify(text), text


def test_a_folder_reads_to_the_same_files_shas_and_ignored_list_as_the_server_reads_it(tmp_path):
    from src.knowledge import ingest

    root = pack_folder(tmp_path, **{"billing-api/notes/n.md": "# n\n", "docs/BOM.md": b"\xef\xbb\xbf# bom\n",
                                    "café/a.md": "# a\n", ".github/ci.yml": "x: 1\n",
                                    "__MACOSX/junk.md": "x\n"})
    theirs = ingest.ingest_directory(root)
    ours = cli.read_folder(str(root))
    assert ours.shas() == {f.path: f.sha for f in theirs.files}
    assert ours.version_sha() == ingest.version_sha(theirs.files)
    assert sorted(p.rstrip("/") for p in ours.ignored) == sorted(p.rstrip("/") for p in theirs.ignored) or (
        # the server lists a hidden folder by its path; the tool lists the same path with a slash
        {p.rstrip("/") for p in ours.ignored} == set(theirs.ignored))
    assert sorted(ours.skills()) == sorted(theirs.skills)
    # and the zip the tool builds is read by the server to the same result
    again = ingest.ingest_zip(ours.zip())
    assert {f.path: f.sha for f in again.files} == ours.shas() and again.skills == theirs.skills


def test_a_folder_that_is_one_wrapper_directory_is_stripped_the_way_the_server_strips_it(tmp_path):
    from src.knowledge import ingest

    root = write(tmp_path / "wrapped", {"inner/a.md": "# a\n", "inner/b/c.md": "# c\n"})
    ours = cli.read_folder(str(root))
    assert ours.top_dir == "inner" and sorted(ours.shas()) == ["a.md", "b/c.md"]
    assert {f.path for f in ingest.ingest_zip(ours.zip()).files} == {"a.md", "b/c.md"}


def test_a_not_found_page_from_the_web_app_says_celmis_url_misses_the_api_path(tmp_path):
    # CELMIS_URL without /backend on the bundled deployment reaches the web app,
    # whose 404 is a page, not the API's JSON: the advice is the address, not the pack.
    page = (404, {"Content-Type": "text/html"}, b"<!DOCTYPE html><html><body>404</body></html>")
    with stub(lambda *a: page) as s:
        code, out, err = run("status", url=s.url, token="cpk_" + "a" * 43)
    assert (code, out) == (1, "")
    assert "not from the Celmis API" in err and "/backend" in err
    assert "was the pack deleted" not in err


def test_an_answer_that_is_not_a_pack_status_names_the_address_to_use(tmp_path):
    with stub(lambda *a: (200, {"Content-Type": "application/json"}, b'{"ok": true}')) as s:
        code, out, err = run("status", url=s.url, token="cpk_" + "a" * 43)
    assert code == 1 and "not a Celmis pack status" in err and "/backend" in err
