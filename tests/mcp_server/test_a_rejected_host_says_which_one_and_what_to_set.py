"""`421 Invalid Host header` names neither the host nor the fix.

MCP over HTTP is the transport the documentation leads with, and on a
README-following install every call to it answers:

    421 Invalid Host header

That is the SDK's DNS-rebinding guard doing its job: its allowed list is
localhost plus whatever `PUBLIC_BASE_URL` / `MCP_ALLOWED_HOSTS` name, and on a
box reached at some other address neither of those was ever set to it. The
guard is right to refuse. The message is what fails the operator — it names
the host it rejected nowhere, the setting that would admit it nowhere, and
reads like a bug in the client.

It also hid behind the auth check: without a token the same request answers
401, so a first attempt looks like a credentials problem and the real cause
only appears once the token is right.

The guard stays. What changes is that its refusal now says which host arrived
and which line to add — the difference between a thirty-second fix and an
afternoon of reading someone else's transport code.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.mcp_server.http_app import _explain_invalid_host, _ExplainInvalidHost

ROOT = Path(__file__).resolve().parents[2]


def _scope(host: str = "celmis.example.com", forwarded: str | None = None):
    headers = [(b"host", host.encode())]
    if forwarded:
        headers.append((b"x-forwarded-host", forwarded.encode()))
    return {"type": "http", "method": "POST", "path": "/", "headers": headers}


async def _run(inner_status: int, inner_body: bytes, scope) -> tuple[int, bytes]:
    sent: list[dict] = []

    async def app(scope_, receive_, send_):
        await send_({"type": "http.response.start", "status": inner_status,
                     "headers": [(b"content-type", b"text/plain")]})
        await send_({"type": "http.response.body", "body": inner_body})

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await _ExplainInvalidHost(app)(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


@pytest.mark.asyncio
async def test_the_refusal_names_the_host_that_was_refused():
    status, body = await _run(421, b"Invalid Host header", _scope("celmis.example.com"))
    assert status == 421
    assert b"celmis.example.com" in body, (
        "the operator is told a host was invalid without being told which one"
    )


@pytest.mark.asyncio
async def test_the_refusal_names_the_setting_that_would_admit_it():
    _, body = await _run(421, b"Invalid Host header", _scope("celmis.example.com"))
    text = body.decode()
    assert "MCP_ALLOWED_HOSTS" in text or "PUBLIC_BASE_URL" in text, (
        "nothing in the message points at the knob"
    )
    assert "celmis.example.com" in text.split("MCP_ALLOWED_HOSTS")[-1] or \
           "celmis.example.com" in text, "the example is not fillable as printed"


@pytest.mark.asyncio
async def test_it_reads_the_proxy_header_like_everything_else():
    """Behind Caddy the operator's host is the forwarded one.

    Printing the internal hop back at them would be a fix instruction that
    admits the wrong name.
    """
    _, body = await _run(421, b"Invalid Host header",
                         _scope(host="api:8000", forwarded="celmis.example.com"))
    assert b"celmis.example.com" in body
    assert b"api:8000" not in body


@pytest.mark.asyncio
async def test_a_working_response_is_not_touched():
    """Only the refusal is rewritten; everything else passes through byte for byte."""
    status, body = await _run(200, b'{"jsonrpc":"2.0","id":1,"result":{}}', _scope())
    assert status == 200
    assert body == b'{"jsonrpc":"2.0","id":1,"result":{}}'


@pytest.mark.asyncio
async def test_another_error_is_not_touched():
    status, body = await _run(401, b"Authentication required", _scope())
    assert status == 401
    assert body == b"Authentication required"


@pytest.mark.asyncio
async def test_a_websocket_scope_passes_straight_through():
    """The wrapper must not assume every scope is HTTP."""
    seen = []

    async def app(scope_, receive_, send_):
        seen.append(scope_["type"])

    await _ExplainInvalidHost(app)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


def test_the_message_is_the_whole_instruction():
    text = _explain_invalid_host("celmis.example.com")
    assert "421" in text or "Host" in text
    assert "celmis.example.com" in text
    assert "MCP_ALLOWED_HOSTS" in text


# ─── and where the operator will look first ──────────────────────────

def test_the_example_file_ties_the_setting_to_mcp():
    """`PUBLIC_BASE_URL` reads as optional until you learn it gates MCP."""
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.startswith("# \u2500\u2500") and "MCP" in ln)
    ends = [i for i in range(start + 1, len(lines)) if lines[i].startswith("# \u2500\u2500")]
    section = "\n".join(lines[start:ends[0] if ends else len(lines)])
    assert "MCP_ALLOWED_HOSTS" in section
    assert "421" in section, (
        "the section names the setting without naming what happens when it is "
        "empty — and what happens is that the endpoint is entirely unreachable"
    )


def test_the_skill_troubleshooting_table_lists_it():
    """A 421 row belongs beside the 401 and 307 rows already there."""
    skill = ROOT / ".claude" / "skills" / "celmis-mcp" / "SKILL.md"
    if not skill.exists():
        pytest.skip("skill doc not shipped here")
    text = skill.read_text(encoding="utf-8")
    assert "421" in text, "the failure table skips the one that stops a deployment"


def test_the_wrapper_is_actually_mounted(monkeypatch):
    """The behaviour tests above pass with the wrapper disconnected.

    Removing `_ExplainInvalidHost` from the `app.mount(...)` call leaves every
    other test in this file green, because they exercise the class directly.
    A fix that is correct and not installed is the same as no fix, and it is
    the easy one to leave behind in a rebase.
    """
    import contextlib

    from fastapi import FastAPI

    from src.mcp_server import http_app as mod

    class _Route:
        path = "/"

    class _Inner:
        routes = [_Route()]

        async def __call__(self, scope, receive, send):  # pragma: no cover
            raise AssertionError("not called")

    class _Sessions:
        @contextlib.asynccontextmanager
        async def run(self):  # pragma: no cover - lifespan is not exercised here
            yield

    class _Settings:
        streamable_http_path = "/mcp"

    class _Mcp:
        session_manager = _Sessions()
        settings = _Settings()

        def streamable_http_app(self):
            return _Inner()

    monkeypatch.setattr(mod, "_build_mcp", lambda: _Mcp())

    app = FastAPI()
    assert mod.mount_mcp(app) is True

    mounted = [r for r in app.routes if getattr(r, "path", None) == "/mcp"]
    assert mounted, "nothing was mounted at /mcp"
    assert isinstance(mounted[0].app, mod._ExplainInvalidHost), (
        "the MCP sub-app is mounted raw — the 421 keeps its original wording"
    )
