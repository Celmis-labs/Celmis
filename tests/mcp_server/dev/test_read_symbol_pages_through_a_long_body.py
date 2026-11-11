"""`read_symbol` pages through a long body with a cursor, whatever cut it:
the line limit or the token budget. Every line of the symbol is reachable,
in order, and no page is cut short by the budget."""

from __future__ import annotations

import re

from src.mcp_server.dev_profile import cursor, emit, tools_read
from tests.mcp_server.dev.conftest import SHOP
from tests.mcp_server.dev.test_read_grep_refs import ORDERS, _git_show

CURSOR = re.compile(r"cursor=(\S+)")


def _between(out: str) -> list[str]:
    parts = (out + "\n").split("\n---\n")
    return parts[1].split("\n") if len(parts) > 1 else []


def _span(out: str, world) -> list[str]:
    m = re.search(rf"{SHOP} {ORDERS}:(\d+)-(\d+) ", out)
    assert m, out
    return _git_show(world, SHOP, ORDERS)[int(m[1]) - 1:int(m[2])]


def _read_all(max_lines: int) -> tuple[str, list[str], int]:
    """(page 1, the body read through every cursor, pages read)."""
    first = tools_read.read_symbol(SHOP, "long_report", max_lines=max_lines)
    lines = _between(first)
    marker = next(ln for ln in lines if ln.startswith("… ") and "elided" in ln)
    got = lines[:lines.index(marker)]
    cur, pages = CURSOR.search(marker)[1], 1
    while cur:
        out = tools_read.read_symbol(SHOP, "long_report", max_lines=max_lines, cursor_=cur)
        assert "truncated to the token budget" not in out
        assert re.search(r"^lines \d+-\d+ of \d+-\d+$", out, re.M), out
        body = _between(out)
        more = body[-1] if body and body[-1].startswith("… +") else ""
        got += body[:-1] if more else body
        cur = CURSOR.search(more)[1] if more else ""
        pages += 1
        assert pages < 200
    return first, got, pages


def test_the_elided_middle_is_read_page_by_page_to_the_last_line(as_caller, world):
    first, got, pages = _read_all(max_lines=20)
    assert got == _span(first, world)
    assert pages > 2
    assert re.search(r"Read app/services/orders\.py:\d+-\d+", first)


def test_a_body_over_the_token_budget_pages_instead_of_being_truncated(as_caller, world,
                                                                     monkeypatch):
    monkeypatch.setitem(emit.BUDGETS, "read_symbol", (300, 420))
    first, got, pages = _read_all(max_lines=300)
    assert "lines elided" in first
    assert got == _span(first, world)
    assert pages > 3


def test_a_cursor_of_another_symbol_answers_page_1_and_says_so(as_caller, world):
    other = cursor.encode(cursor.fingerprint(tool="read_symbol", repo=SHOP, name="nope",
                                             path=""), 5, {})
    out = tools_read.read_symbol(SHOP, "long_report", max_lines=20, cursor_=other)
    assert cursor.MISMATCH_NOTE in out
    assert "sig: " in out and "lines elided" in out


def test_a_cursor_past_the_body_answers_page_1(as_caller, world):
    first = tools_read.read_symbol(SHOP, "long_report", max_lines=20)
    h = CURSOR.search(first)[1].split(".")
    far = ".".join([h[0], "100000", h[2]])
    out = tools_read.read_symbol(SHOP, "long_report", max_lines=20, cursor_=far)
    assert cursor.MISMATCH_NOTE in out and "sig: " in out
