"""Whatever a pack says passes the dev profile's redaction hook, and a commit sha survives it.

The contract (src/mcp_server/dev_profile/emit.py `_redact`, docs/MCP_DEV.md "Secrets"):

  * pack text is workspace-written and can hold a pasted secret: a password in a
    call, a DSN, a token. No value of one leaves `skills` or `brief`: not in a
    skill body, a file, a search snippet, a brief entry, a title or a
    description, and not as the stump of one a cut left behind (the text is
    masked first and cut second);
  * a 40-hex git sha is not a secret and is kept: an agent needs it to say which
    commit a note is about;
  * if the redactor itself fails, the answer is the one withheld line, never the
    text unchecked.

The upload scans (`ingest._refuse_secrets` and the file sniff) normally keeps such text out of a pack.
These tests switch it off, as a version made before a rule existed, or through
another path, would have got past it: this is the second line of defence.
Secret values are assembled at run time; none is a literal in this file.
"""

from __future__ import annotations

import re

import pytest

from src.mcp_server.dev_profile import emit
from src.mcp_server.dev_profile import tools_skills as ts
from tests.mcp_server.dev.pack_world import (
    PackWorld,  # noqa: F401  (the fixture comes from conftest)
)
from tests.mcp_server.dev.test_skills_and_brief_answers_stay_inside_their_budgets_and_page_by_cursor import (
    _pack,
)

SHA = "3f2a91c8d7e6b5a4938271605f4e3d2c1b0a9f8e"
PW = "Zq8x" + "Lm2Pv9"
DSN_PW = "hunter" + "2canary" + "Pw"
TOKEN = "tok" + "_live_" + "canaryAbCdEf0123456789"
SECRETS = (PW, DSN_PW, TOKEN)

BODY = (f"Invoicing client setup, verified at commit {SHA}:\n\n"
        f"    client = connect(host='db', user='app', password='{PW}')\n"
        f"    DSN = postgresql://app:{DSN_PW}@db.internal/shop\n"
        f"    api_key = '{TOKEN}'\n")
SKILL = ("---\nname: setup\ndescription: Invoicing setup (password = '" + PW + "').\n---\n# Setup\n\n"
         + BODY)
NOTE = "---\ndescription: Invoicing note\n---\n# Note\n\n" + BODY


@pytest.fixture(autouse=True)
def _a_secret_got_into_the_pack(monkeypatch):
    monkeypatch.setattr("src.knowledge.ingest._refuse_secrets", lambda loaded: None)
    monkeypatch.setattr("src.security.secret_files.classify", lambda *a, **k: "allow")


def _clean(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, secret[:4]
    # not even the stump of one
    for secret in SECRETS:
        for size in (6, 7, 8):
            assert not any(secret[i:i + size] in text for i in range(len(secret) - size + 1)), secret[:4]


async def _world(pack_world):
    await _pack(pack_world, "leaky", [("setup/SKILL.md", SKILL), ("setup/ref.md", NOTE),
                                      ("notes/note.md", NOTE)])
    pack_world.as_caller("owner")


async def test_a_skill_body_and_its_files_are_masked_and_keep_the_sha(pack_world):
    await _world(pack_world)
    for args in (("", "leaky/setup"), ("", "leaky/setup", "ref.md"), ("", "leaky", "notes/note.md")):
        text, _rec = pack_world.call(ts.run, *args)
        _clean(text)
        assert SHA in text, args
        assert "Invoicing client setup" in text


async def test_a_search_snippet_and_a_title_are_masked(pack_world):
    await _world(pack_world)
    for args in (("invoicing setup", "", "", True), ("invoicing setup",), ()):
        text, _rec = pack_world.call(ts.run, *args)
        _clean(text)
        assert "leaky/" in text
    detailed, _ = pack_world.call(ts.run, "invoicing setup", "", "", True)
    assert SHA in detailed


async def test_a_brief_entry_is_masked_and_keeps_the_sha(pack_world):
    await _world(pack_world)
    text, _rec = pack_world.call(ts.run_brief, "invoicing client setup", tool="brief")
    _clean(text)
    assert "[leaky@v1]" in text and SHA in text


@pytest.mark.parametrize("pad", [0, 20, 60, 100, 140, 170, 200, 230, 260, 300, 340])
async def test_no_cut_of_a_snippet_leaves_the_stump_of_a_secret(pack_world, pad):
    """The secret is `pad` characters before the word the task asks about, so the
    snippet window starts or ends at every place in and around it."""
    note = (f"# Padded\n\nclient = connect(password='{PW}', dsn='postgresql://app:{DSN_PW}@db/x')\n"
            + "filler words go here " * (pad // 20) + "\nzebramarker invoicing is described here.\n"
            + f"after it, api_key = '{TOKEN}' and commit {SHA}\n")
    await _pack(pack_world, "padded", [("notes/padded.md", note)])
    pack_world.as_caller("owner")
    text, _rec = pack_world.call(ts.run_brief, "zebramarker invoicing", tool="brief")
    assert "[padded@v1]" in text
    _clean(text)
    detailed, _ = pack_world.call(ts.run, "zebramarker", "", "", True)
    _clean(detailed)


async def test_a_failing_redactor_withholds_every_answer(pack_world, monkeypatch):
    await _world(pack_world)

    def broken(_text):
        raise RuntimeError("redactor down")

    monkeypatch.setattr(emit, "_redact", broken)
    for call in (
        lambda: pack_world.call(ts.run, "", "leaky/setup"),
        lambda: pack_world.call(ts.run, "", "leaky/setup", "ref.md"),
        lambda: pack_world.call(ts.run, "", "leaky"),
        lambda: pack_world.call(ts.run),
        lambda: pack_world.call(ts.run, "invoicing", "", "", True),
        lambda: pack_world.call(ts.run_brief, "invoicing client setup", tool="brief"),
    ):
        text, _rec = call()
        assert text == f"idx: none\n{emit.WITHHELD}", text[:120]


async def test_the_answer_through_the_real_envelope_is_masked_too(pack_world, monkeypatch):
    import mcp.types as types

    from src.mcp_server import audit
    from src.mcp_server.dev_profile import build_dev_mcp

    monkeypatch.setenv("MCP_JWT_SECRET", "s" * 48)
    monkeypatch.setattr(audit, "_enqueue", lambda row: None)
    await _world(pack_world)
    handler = build_dev_mcp()._mcp_server.request_handlers[types.CallToolRequest]
    for tool, args in (("skills", {"name": "leaky/setup"}), ("brief", {"task": "invoicing client setup"})):
        req = types.CallToolRequest(
            method="tools/call", params=types.CallToolRequestParams(name=tool, arguments=args))
        result = (await handler(req)).root
        text = "\n".join(c.text for c in result.content)
        _clean(text)
        assert SHA in text and not re.search(r"redaction failed", text)


def _pem(lines: int = 24) -> tuple[str, list[str]]:
    """A private-key block (assembled here) and its body lines."""
    import base64
    import hashlib

    body = [base64.b64encode(hashlib.sha256(f"line-{i}".encode()).digest() * 2).decode()[:64]
            for i in range(lines)]
    head, tail = "-----BEGIN " + "PRIVATE KEY-----", "-----END " + "PRIVATE KEY-----"
    return "\n".join([head, *body, tail]), body


@pytest.mark.parametrize("where", ["before", "inside", "after"])
async def test_a_key_block_a_snippet_would_cut_through_is_masked_whole(pack_world, where):
    """A multi-line secret is only recognisable whole, so it is masked before the
    snippet window is cut out of the text, not after."""
    block, body = _pem()
    lines = block.split("\n")
    mark = "zebramarker invoicing is described here."
    if where == "before":
        text = "\n".join([mark, *lines])
    elif where == "after":
        text = "\n".join([*lines, mark])
    else:
        text = "\n".join([*lines[:13], mark, *lines[13:]])
    await _pack(pack_world, "keys", [("notes/keys.md", "# Keys\n\n" + text + "\n")])
    pack_world.as_caller("owner")
    outputs = [pack_world.call(ts.run_brief, "zebramarker invoicing", tool="brief")[0],
               pack_world.call(ts.run, "zebramarker", "", "", True)[0],
               pack_world.call(ts.run, "", "keys", "notes/keys.md")[0]]
    for out in outputs:
        assert "[keys@v1]" in out or "keys/" in out
        for line in body:
            assert line[:16] not in out and line[-16:] not in out
