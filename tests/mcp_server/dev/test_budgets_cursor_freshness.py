"""Every answer starts with its revision, stays inside a budget, and pages
with a cursor that cannot be replayed against the wrong question."""

from __future__ import annotations

import re

from src.mcp_server.dev_contract import IDX_ENTRY_RE, IDX_LINE_RE, MORE_RE
from src.mcp_server.dev_profile import cursor, emit, tools_find, tools_grep, tools_read, tools_repos
from src.mcp_server.dev_profile.freshness import RepoFresh, human_age
from tests.mcp_server.dev.conftest import BILLING, SHOP


def _entries(first_line: str) -> list[dict]:
    m = re.match(IDX_LINE_RE, first_line)
    assert m, first_line
    return [e.groupdict() for e in re.finditer(IDX_ENTRY_RE, m["entries"])]


# ─── freshness header ────────────────────────────────────────────────

def test_a_successful_answer_opens_with_the_branch_sha_age_and_fresh_state(as_caller, world, freshness):
    first = tools_find.run("create_order", repo=SHOP).split("\n")[0]
    (entry,) = _entries(first)
    assert entry["slug"] == SHOP and entry["branch"] == "develop"
    assert entry["sha"] == world[1][SHOP][:8] and entry["state"] == "fresh"


def test_an_index_behind_the_remote_head_is_marked_stale(as_caller, world, freshness):
    freshness[SHOP]["remote"] = "f" * 40
    first = tools_find.run("create_order", repo=SHOP).split("\n")[0]
    assert _entries(first)[0]["state"] == "STALE"


def test_an_index_last_checked_against_the_remote_long_ago_is_marked_unknown(as_caller, world, freshness):
    from datetime import UTC, datetime, timedelta

    freshness[SHOP]["checked"] = datetime.now(UTC) - timedelta(days=3)
    first = tools_find.run("create_order", repo=SHOP).split("\n")[0]
    assert _entries(first)[0]["state"] == "unknown"


def test_a_repo_with_no_recorded_revision_falls_back_to_head_and_says_unknown(as_caller, world, freshness):
    freshness[SHOP]["missing"] = True
    first = tools_find.run("create_order", repo=SHOP).split("\n")[0]
    assert _entries(first)[0]["state"] == "unknown"


def test_an_empty_result_still_carries_the_idx_line(as_caller, world):
    out = tools_find.run("zzzqqqxxx", repo=SHOP)
    assert re.match(IDX_LINE_RE, out.split("\n")[0])


def test_an_error_answer_still_carries_the_idx_line(as_caller, world):
    out = tools_find.run("x", mode="nope")
    assert out.split("\n")[0] == "idx: none"


def test_an_answer_over_two_repos_lists_both_in_the_idx_line(as_caller, world):
    first = tools_find.run("create_order", limit=20).split("\n")[0]
    assert {e["slug"] for e in _entries(first)} == {SHOP, BILLING}


def test_human_age_is_compact():
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    assert human_age(now - timedelta(minutes=5), now) == "5m"
    assert human_age(now - timedelta(hours=3), now) == "3h"
    assert human_age(now - timedelta(days=2), now) == "2d"
    assert human_age(None, now) == "?"


# ─── budgets ─────────────────────────────────────────────────────────

def _fresh() -> list[RepoFresh]:
    return [RepoFresh(SHOP, "develop", "a" * 40, "1h", "fresh")]


def test_a_long_page_is_cut_to_the_tool_budget_and_says_how_much_is_left():
    page = [f"{SHOP} app/x.py:{i}-{i + 3} function def f{i}(a, b, c)  padding padding" for i in range(300)]
    out = emit.render_list("find", _fresh(), page, total=300)
    assert len(out) <= int(emit.BUDGETS["find"][0] * emit.CHARS_PER_TOKEN) + 120
    m = re.search(MORE_RE, out.split("\n")[-1])
    assert m and int(m["n"]) == 300 - (len(out.split("\n")) - 2)


def test_the_first_item_is_always_shown_even_when_it_alone_exceeds_the_budget():
    long_line = "word " * 1800
    out = emit.render_list("repos", _fresh(), [long_line], total=1)
    assert long_line.strip() in out


def test_a_raised_limit_gets_the_hard_cap_not_the_default():
    assert emit.budget_chars("find", raised=True) > emit.budget_chars("find")
    assert emit.budget_chars("find", raised=True) == int(4000 * emit.CHARS_PER_TOKEN)


def test_detailed_format_doubles_the_default_budget_but_never_passes_the_hard_cap():
    assert emit.budget_chars("find", detailed=True) == 2 * emit.budget_chars("find")
    assert emit.budget_chars("repos", detailed=True, budget_tokens=10**6) == int(1500 * 3.5)


def test_a_symbol_body_is_elided_in_the_middle_and_names_the_range_to_read(as_caller, world):
    out = tools_read.read_symbol(SHOP, "long_report")
    assert "lines elided" in out
    assert re.search(r"Read app/services/orders\.py:\d+-\d+", out)


# ─── cursors ─────────────────────────────────────────────────────────

def _cursor_of(out: str) -> str:
    m = re.search(MORE_RE, out.split("\n")[-1])
    assert m, out
    return m["cursor"]


def test_the_cursor_on_a_truncated_page_fetches_the_next_distinct_page(as_caller, world):
    p1 = tools_find.run("order", limit=3)
    p2 = tools_find.run("order", limit=3, cursor_=_cursor_of(p1))
    rows1 = p1.split("\n")[1:-1]
    rows2 = p2.split("\n")[1:]
    assert rows2 and not set(rows1) & set(rows2)


def test_a_cursor_used_with_a_different_query_returns_page_one_with_a_note(as_caller, world):
    c = _cursor_of(tools_find.run("order", limit=3))
    out = tools_find.run("create", limit=3, cursor_=c)
    assert cursor.MISMATCH_NOTE in out


def test_a_cursor_from_before_a_reindex_returns_page_one_with_a_stale_note(as_caller, world, freshness):
    c = _cursor_of(tools_find.run("order", limit=3))
    freshness[SHOP]["sha"] = "e" * 40
    out = tools_find.run("order", limit=3, cursor_=c)
    assert cursor.STALE_NOTE in out


def test_a_garbage_cursor_is_page_one_with_a_note_not_an_error(as_caller, world):
    out = tools_find.run("order", limit=3, cursor_="!!!not-a-cursor")
    assert cursor.MISMATCH_NOTE in out


def test_cursor_encode_and_decode_round_trip():
    h = cursor.fingerprint(q="a", repo="")
    c = cursor.encode(h, 7, {"a": "1", "b": "2"})
    assert cursor.resolve(c, h, {"b": "2", "a": "1"}) == (7, "")


def test_the_repos_tool_lists_every_readable_repo_with_its_access_level(as_caller, world):
    from tests.mcp_server.dev.conftest import BILLING as B

    as_caller({SHOP: "code", B: "metadata"})
    out = tools_repos.run()
    assert f"{SHOP} code" in out and f"{B} metadata" in out


def test_grep_output_is_cut_to_its_budget(as_caller, world):
    out = tools_grep.run("out.append", repo=SHOP, limit=60)
    assert len(out) <= int(emit.BUDGETS["grep"][1] * emit.CHARS_PER_TOKEN) + 200
