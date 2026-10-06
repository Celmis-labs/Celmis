"""The dev profile's tool list is small, static and identical for everyone."""

from __future__ import annotations

import asyncio
import json

import pytest

from src.mcp_server.dev_contract import DEV_TOOLS


@pytest.fixture
def dev_mcp(monkeypatch):
    monkeypatch.setenv("MCP_JWT_SECRET", "s" * 48)
    from src.mcp_server.dev_profile import build_dev_mcp

    return build_dev_mcp()


def _listing(mcp) -> list:
    return asyncio.run(mcp.list_tools())


def test_the_whole_tool_listing_stays_under_six_thousand_characters(dev_mcp):
    tools = _listing(dev_mcp)
    wire = json.dumps([t.model_dump(mode="json", exclude_none=True) for t in tools])
    assert len(wire) <= 6000, len(wire)


def test_every_description_is_at_most_two_hundred_characters(dev_mcp):
    for t in _listing(dev_mcp):
        assert len(t.description) <= 200, (t.name, len(t.description))


def test_no_tool_declares_an_output_schema_or_a_title_in_its_input_schema(dev_mcp):
    for t in _listing(dev_mcp):
        assert t.outputSchema is None, t.name
        assert "title" not in json.dumps(t.inputSchema), t.name


def test_every_tool_is_marked_read_only(dev_mcp):
    for t in _listing(dev_mcp):
        assert t.annotations is not None and t.annotations.readOnlyHint is True, t.name


def test_the_nine_profile_tools_are_listed_howto_included(dev_mcp):
    names = {t.name for t in _listing(dev_mcp)}
    assert names == set(DEV_TOOLS)


def test_the_list_does_not_depend_on_who_is_asking(dev_mcp, as_caller, world):
    from tests.mcp_server.dev.conftest import BILLING, SHOP

    as_caller({SHOP: "code", BILLING: "code"})
    first = [t.name for t in _listing(dev_mcp)]
    as_caller({SHOP: "metadata"})
    assert [t.name for t in _listing(dev_mcp)] == first


def test_an_extra_tool_can_be_registered_once_and_only_under_a_known_name():
    from src.mcp_server.dev_profile import register_dev_tool

    with pytest.raises(ValueError):
        register_dev_tool("not_a_dev_tool", lambda: "x", "d")
    with pytest.raises(ValueError):
        register_dev_tool("howto", lambda: "x", "d" * 201)


def test_a_registered_howto_tool_appears_in_a_new_server(monkeypatch):
    monkeypatch.setenv("MCP_JWT_SECRET", "s" * 48)
    from src.mcp_server import dev_profile

    monkeypatch.setattr(dev_profile, "_EXTRA", {})

    def howto(topic: str) -> str:
        return "idx: none"

    dev_profile.register_dev_tool("howto", howto, "How a pattern is done in your repos.")
    names = {t.name for t in _listing(dev_profile.build_dev_mcp())}
    assert "howto" in names
