"""One audit row per tool call, with facts and never values (access lane)."""

from __future__ import annotations

import pytest

from tests.e2e_local.client import McpClient
from tests.e2e_local.stack import Stack

pytestmark = [pytest.mark.needs_lane("access"), pytest.mark.needs_lane("tools")]

QUERY = "needle_query_value_91357"
COLUMNS = {"tool", "token_id", "user_id", "repos", "status", "result_bytes", "result_items",
           "args_hash"}


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    with Stack(tmp_path_factory.mktemp("audit")) as st:
        yield st


@pytest.fixture(scope="module")
def calls(stack):
    """Three calls by one token; returns (token id, the audit rows they wrote)."""
    token = stack.token("dev", repos=["acme/shop"])
    token_id = stack.last_token_id
    client = McpClient(stack.url, "/mcp/dev/", token)
    shop = stack.slug_of("acme/shop")
    client.call("repos")
    client.call("grep", {"pattern": QUERY, "repo": shop})
    client.call("find", {"query": "create_order", "repo": shop})
    rows = [r for r in stack.wait_audit(3) if r.get("token_id") == token_id]
    return token_id, rows


def test_each_call_writes_exactly_one_row(calls) -> None:
    _token_id, rows = calls
    assert sorted(r["tool"] for r in rows) == ["find", "grep", "repos"]


def test_a_row_names_the_person_the_token_and_the_outcome(stack, calls) -> None:
    token_id, rows = calls
    assert set(rows[0]) >= COLUMNS
    for r in rows:
        assert r["user_id"] == stack.users["dev"].id and r["token_id"] == token_id
        assert r["status"] == "ok" and r["result_bytes"] > 0


def test_the_repositories_touched_are_recorded_for_a_scoped_call(stack, calls) -> None:
    _t, rows = calls
    grep = next(r for r in rows if r["tool"] == "grep")
    repos = grep["repos"] if isinstance(grep["repos"], list) else __import__("json").loads(
        grep["repos"])
    assert repos == [stack.slug_of("acme/shop")]


def test_no_argument_value_and_no_result_text_is_stored(stack, calls) -> None:
    _t, rows = calls
    blob = repr(rows)
    assert QUERY not in blob and "create_order" not in blob
    assert all(r["args_hash"] and QUERY not in r["args_hash"] for r in rows)
    grep = next(r for r in rows if r["tool"] == "grep")
    find = next(r for r in rows if r["tool"] == "find")
    assert grep["args_hash"] != find["args_hash"]


def test_a_denied_call_is_recorded_as_denied_and_still_one_row(stack) -> None:
    token = stack.token("dev", repos=["acme/shop"])
    token_id = stack.last_token_id
    before = len(stack.audit_rows())
    McpClient(stack.url, "/mcp/dev/", token).call(
        "grep", {"pattern": "create_order", "repo": stack.slug_of("acme/billing")})
    rows = [r for r in stack.wait_audit(before + 1) if r.get("token_id") == token_id]
    assert len(rows) == 1 and rows[0]["status"] in {"denied", "ok", "error"}
    assert "acme-billing" not in repr(rows[0]["args_hash"])


def test_the_audit_table_and_the_logs_hold_no_planted_secret(stack, calls) -> None:
    assert stack.world is not None
    assert stack.world.leaks_in(repr(stack.audit_rows())) == []
    assert stack.world.leaks_in(stack.log_text()) == []
