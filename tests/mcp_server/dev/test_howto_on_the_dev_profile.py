"""`howto` lives in its own package; the dev profile plugs it in and gives it
the profile's freshness, so its first line reads like every other dev answer."""

from __future__ import annotations

import re

from src.mcp_server.dev_contract import IDX_ENTRY_RE
from tests.mcp_server.dev.conftest import SHOP


def test_howto_reports_the_indexed_sha_branch_and_state_the_other_dev_tools_report(
    monkeypatch, world, freshness,
):
    from src.mcp_server import dev_profile
    from src.mcp_server.dev_profile.freshness import read_freshness

    monkeypatch.setenv("MCP_JWT_SECRET", "s" * 48)
    dev_profile.build_dev_mcp()
    root, shas = world
    from src.mcp_server.howto import engine

    info = engine._idx_provider(SHOP, root / SHOP)
    expected = read_freshness([SHOP])[SHOP]

    assert info.sha == shas[SHOP] == expected.sha
    assert info.branch == "develop"
    assert info.state == expected.state == "fresh"
    assert re.fullmatch(IDX_ENTRY_RE, info.line().removeprefix("idx: ")) is not None


def test_howto_falls_back_to_the_clone_when_nothing_was_recorded(
    monkeypatch, world, freshness,
):
    from src.mcp_server import dev_profile
    from src.mcp_server.howto import engine

    monkeypatch.setenv("MCP_JWT_SECRET", "s" * 48)
    dev_profile.build_dev_mcp()
    root, _shas = world
    freshness[SHOP]["missing"] = True

    info = engine._idx_provider(SHOP, root / SHOP)

    assert info.slug == SHOP
    assert info.state in ("unknown", "fresh", "STALE")
