"""`find`: ranked, typo-tolerant, and not drowned by one repository or kind."""

from __future__ import annotations

import re

from src.mcp_server.dev_contract import HIT_RE
from src.mcp_server.dev_profile import tools_find
from tests.mcp_server.dev.conftest import BILLING, SHOP


def _hits(out: str) -> list[str]:
    return [ln for ln in out.split("\n")[1:] if re.match(HIT_RE, ln)]


def test_an_exact_name_comes_before_a_prefix_a_substring_and_a_test(as_caller, world):
    hits = _hits(tools_find.run("create_order"))
    assert hits[0].startswith(f"{SHOP} app/services/orders.py:19-20 function")
    test_row = next(i for i, h in enumerate(hits) if "tests/test_orders.py" in h)
    vendor_row = next(i for i, h in enumerate(hits) if "vendor/lib.py" in h)
    assert test_row > 0 and vendor_row > 0


def test_a_tests_or_vendor_copy_ranks_below_the_same_name_in_real_code(as_caller, world):
    hits = _hits(tools_find.run("create_order"))
    exact = [h for h in hits if " create_order(" in h or " create_order" in h.split(" def ")[0]]
    real = next(i for i, h in enumerate(hits) if "app/services/orders.py:19-20" in h)
    vendor = next(i for i, h in enumerate(hits) if "vendor/lib.py" in h)
    assert real < vendor
    assert exact


def test_a_misspelt_name_still_finds_the_class_it_meant(as_caller, world):
    out = tools_find.run("OrderSrv")
    assert any("class OrderService" in h for h in _hits(out))


def test_kind_filters_before_the_limit_so_a_function_is_not_crowded_out(as_caller, world):
    # 25 variables are named user<N>; the one function must still be found.
    out = tools_find.run("user", kind="function", limit=3)
    hits = _hits(out)
    assert hits and "user_summary" in hits[0]
    assert "variable" not in out


def test_the_graph_store_applies_kind_inside_the_query_before_its_limit(shared_graphs):
    rows = shared_graphs(SHOP).find_symbols("user", kind="function", limit=2)
    assert [r["name"] for r in rows] == ["user_summary"]


def test_one_repository_cannot_fill_a_whole_page(as_caller, world):
    hits = _hits(tools_find.run("order", limit=6))
    assert any(h.startswith(BILLING) for h in hits)
    assert any(h.startswith(SHOP) for h in hits)


def test_a_repo_argument_limits_the_search_to_that_repository(as_caller, world):
    hits = _hits(tools_find.run("create", repo=BILLING))
    assert hits and all(h.startswith(BILLING) for h in hits)


def test_a_query_with_no_match_says_so_and_suggests_a_next_step(as_caller, world):
    out = tools_find.run("zzzqqqxxx")
    assert 'no symbol matching "zzzqqqxxx"' in out
    assert "mode=fuzzy" in out


def test_an_empty_query_is_an_error_line_not_an_exception(as_caller, world):
    assert "query is empty" in tools_find.run("  ")


def test_an_unknown_mode_is_refused_with_the_allowed_ones(as_caller, world):
    assert "mode must be one of" in tools_find.run("x", mode="regex")


def test_a_hit_line_shows_how_many_callers_a_symbol_has(as_caller, world):
    hits = _hits(tools_find.run("create_order", repo=SHOP))
    assert "← 2 callers" in hits[0]


def test_detailed_format_adds_the_symbol_signature_and_concise_stays_one_line(as_caller, world):
    concise = _hits(tools_find.run("create_order", repo=SHOP))
    detailed = tools_find.run("create_order", repo=SHOP, response_format="detailed")
    assert len(detailed) > sum(len(h) for h in concise) // 2
    assert all("\n" not in h for h in concise)
