"""Fixes to the existing `/mcp` tools that the dev profile depends on."""

from __future__ import annotations

import pytest

from src.mcp_server import http_app
from tests.mcp_server.dev.conftest import BILLING, SHOP, _decision

LEAK_FIELDS = ("blocked_repos", "access_notice", "hidden_symbol_count")


@pytest.fixture
def legacy_caller(monkeypatch, world, freshness, shared_graphs):
    """`legacy_caller({slug: level})` — what the full server's access check says."""
    from src.mcp_server import identity

    def apply(levels: dict[str, str], deny: dict[str, tuple[str, ...]] | None = None):
        deny = deny or {}
        dec = {s: _decision(s, lvl, deny.get(s, ())) for s, lvl in levels.items() if lvl != "none"}

        def fake(repos):
            from src.access import RepoAccessDecision

            return None, {r: dec.get(r) or RepoAccessDecision.denied(r) for r in repos}

        monkeypatch.setattr(identity, "caller_access", fake)

    apply({SHOP: "code", BILLING: "code"})
    return apply


def _all_repos(monkeypatch):
    from src.mcp_server.dev_profile import access

    monkeypatch.setattr(access, "indexed_slugs", lambda: [BILLING, SHOP])


def test_search_symbols_needs_no_project_and_then_searches_every_readable_repo(legacy_caller, monkeypatch):
    _all_repos(monkeypatch)
    out = http_app._search_symbols_impl(None, "create_order", None, 20)
    assert {m["repo_slug"] for m in out["matches"]} == {SHOP, BILLING}


def test_search_symbols_applies_kind_before_the_limit(legacy_caller, monkeypatch):
    _all_repos(monkeypatch)
    out = http_app._search_symbols_impl(None, "user", "function", 3)
    assert [m["name"] for m in out["matches"]] == ["user_summary"]


def test_search_symbols_finds_a_misspelt_name_in_auto_mode(legacy_caller, monkeypatch):
    _all_repos(monkeypatch)
    out = http_app._search_symbols_impl(None, "OrderSrv", None, 10)
    assert "OrderService" in [m["name"] for m in out["matches"]]


def test_search_symbols_reports_the_end_line_and_drops_a_missing_signature(legacy_caller, monkeypatch):
    _all_repos(monkeypatch)
    match = http_app._search_symbols_impl(None, "OrderService", None, 5)["matches"][0]
    assert match["end_line"] >= match["line"] and "signature" not in match


def test_search_symbols_interleaves_repos_so_one_cannot_fill_the_page(legacy_caller, monkeypatch):
    _all_repos(monkeypatch)
    out = http_app._search_symbols_impl(None, "create_order", None, 2)
    assert {m["repo_slug"] for m in out["matches"]} == {SHOP, BILLING}


def test_search_symbols_exact_mode_does_not_fall_back_to_substring(legacy_caller, monkeypatch):
    _all_repos(monkeypatch)
    out = http_app._search_symbols_impl(None, "create_order", None, 20, mode="exact")
    assert {m["name"] for m in out["matches"]} == {"create_order"}


def test_a_repo_without_access_contributes_nothing_and_is_never_named(legacy_caller, monkeypatch):
    _all_repos(monkeypatch)
    legacy_caller({SHOP: "code", BILLING: "none"})
    out = http_app._search_symbols_impl(None, "create_order", None, 20)
    assert BILLING not in repr(out)
    assert not any(k in out for k in LEAK_FIELDS)


def test_a_denied_path_hides_its_symbols_without_counting_them(legacy_caller, monkeypatch):
    _all_repos(monkeypatch)
    legacy_caller({SHOP: "code"}, deny={SHOP: ("vendor/**",)})
    out = http_app._search_symbols_impl(None, "create_order", None, 20)
    assert all(m["file"] != "vendor/lib.py" for m in out["matches"])
    assert not any(k in out for k in LEAK_FIELDS)


def test_search_symbols_over_a_project_with_no_repos_says_so(legacy_caller, monkeypatch):
    monkeypatch.setattr(http_app, "_project_repo_slugs", lambda pid: [])
    out = http_app._search_symbols_impl("p-1", "x", None, 5)
    assert out["matches"] == [] and "no repos" in out["error"]


def test_the_public_search_symbols_wrapper_strips_boundary_fields():
    out = http_app._without_boundary_fields(
        {"matches": [], "blocked_repos": ["x"], "access_notice": "n", "hidden_symbol_count": 3, "count": 0})
    assert out == {"matches": [], "count": 0}


def test_find_consumers_omits_the_boundary_fields_for_a_repo_it_may_not_read(legacy_caller, monkeypatch):
    monkeypatch.setattr(http_app, "_project_repo_slugs", lambda pid: [SHOP, BILLING])
    monkeypatch.setattr(http_app, "_legacy_callers", lambda sym, slug: [])
    legacy_caller({SHOP: "code", BILLING: "none"})
    out = http_app._find_consumers_impl("p-1", "create_order")
    assert not any(k in out for k in LEAK_FIELDS) and BILLING not in repr(out)


def test_get_api_surface_says_unsupported_when_the_repo_has_no_readable_revision(legacy_caller, monkeypatch):
    from src.mcp_server.dev_profile import freshness as fr
    from src.mcp_server.dev_profile import git_io

    monkeypatch.setattr(fr, "read_freshness", lambda slugs: {})
    monkeypatch.setattr(git_io, "head_sha", lambda slug: None)
    out = http_app._get_api_surface_impl(SHOP, None)
    assert out["supported"] is False and out["endpoints"] == []


def test_the_public_api_surface_tool_never_names_a_repo_the_caller_may_not_read(legacy_caller):
    legacy_caller({SHOP: "code", BILLING: "none"})
    raw = http_app._get_api_surface_impl(BILLING, None)
    public = http_app._without_boundary_fields(raw)
    from src.access.effective import NOT_ACCESSIBLE

    assert public == {"error": NOT_ACCESSIBLE}, "the same bytes as a repository that does not exist"


def test_every_description_on_the_full_server_is_at_most_three_hundred_characters(monkeypatch):
    import asyncio

    monkeypatch.setenv("MCP_JWT_SECRET", "s" * 48)
    mcp = http_app._build_mcp()
    for t in asyncio.run(mcp.list_tools()):
        assert len(t.description or "") <= 300, (t.name, len(t.description or ""))
