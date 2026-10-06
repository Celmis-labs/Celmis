"""The output guard: fail-closed, always on, and kind to the references clients need."""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

from mcp import types
from mcp.server.fastmcp import FastMCP

from src.mcp_server import output_guard
from src.mcp_server.output_guard import WITHHELD, install_output_guard, redact_result

SHA = "3f2a91c8d7e6b5a4938271605f4e3d2c1b0a9f8e"


def _server(payload) -> FastMCP:
    mcp = FastMCP("t")

    @mcp.tool(name="echo")
    def echo() -> dict[str, Any]:
        return payload

    @mcp.tool(name="text", structured_output=False)
    def text() -> str:
        return payload if isinstance(payload, str) else json.dumps(payload)

    install_output_guard(mcp)
    return mcp


def _call(mcp, name: str = "echo") -> types.CallToolResult:
    handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call", params=types.CallToolRequestParams(name=name, arguments={})
    )
    return asyncio.run(handler(req)).root


def test_the_guard_redacts_both_the_text_block_and_the_structured_content():
    c = secrets.token_urlsafe(9)
    result = _call(_server({"dsn": f"postgresql://u:{c}@h/db", "password": c}))
    assert c not in json.dumps(result.model_dump(mode="json"))
    assert result.structuredContent is not None
    assert "[REDACTED" in result.content[0].text


def test_a_plain_text_result_is_redacted_line_by_line():
    c = secrets.token_urlsafe(9)
    result = _call(_server(f"idx: a/b main@{SHA[:7]} 1h fresh\nPGPASSWORD={c} psql"), "text")
    assert c not in result.content[0].text
    assert result.content[0].text.splitlines()[0].startswith("idx: a/b main@")


def test_the_indexed_sha_and_cursor_and_ids_survive():
    payload = {"indexed_sha": SHA, "sha": SHA[:12], "cursor": "p2", "id": "tok_ab12cd34", "note": "ok"}
    result = _call(_server(payload))
    assert result.structuredContent == payload
    assert json.loads(result.content[0].text) == payload


def test_a_clean_result_is_returned_byte_for_byte():
    payload = {"count": 2, "items": ["a", "b"]}
    mcp = _server(payload)
    assert _call(mcp).content[0].text == json.dumps(payload, indent=2)


def test_a_redaction_failure_withholds_the_whole_result(monkeypatch):
    c = secrets.token_urlsafe(9)
    mcp = _server({"password": c, "other": "data"})

    def boom(*_a, **_k):
        raise RuntimeError("redactor down")

    monkeypatch.setattr(output_guard, "redact_for_mcp", boom)
    monkeypatch.setattr(output_guard, "redact_structure", boom)
    result = _call(mcp)
    assert result.isError is True
    assert result.content[0].text == WITHHELD
    assert c not in json.dumps(result.model_dump(mode="json"))
    assert result.structuredContent is None


def test_the_setting_that_disables_llm_redaction_does_not_touch_mcp_output(monkeypatch):
    from src.config import get_settings

    monkeypatch.setenv("REDACTION_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert get_settings().redaction_enabled is False
        c = secrets.token_urlsafe(9)
        result = _call(_server({"dsn": f"postgresql://u:{c}@h/db"}))
        assert c not in json.dumps(result.model_dump(mode="json"))
    finally:
        get_settings.cache_clear()


def test_installing_the_guard_twice_does_not_wrap_twice():
    mcp = _server({"a": 1})
    assert install_output_guard(mcp) is False


def test_an_error_result_is_redacted_too():
    c = secrets.token_urlsafe(9)
    mcp = FastMCP("t")

    @mcp.tool(name="boom")
    def boom() -> dict:
        raise ValueError(f"cannot connect: postgresql://u:{c}@h/db")

    install_output_guard(mcp)
    result = _call(mcp, "boom")
    assert result.isError is True
    assert c not in result.content[0].text


def test_redact_result_leaves_other_result_types_alone():
    sentinel = object()
    assert redact_result(sentinel, "x") is sentinel


def test_the_guard_counts_what_it_removed_without_keeping_values(caplog):
    import logging

    c = secrets.token_urlsafe(9)
    with caplog.at_level(logging.INFO, logger="src.mcp_server.output_guard"):
        _call(_server({"dsn": f"postgresql://u:{c}@h/db"}))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "mcp_output_redacted" in text and c not in text
