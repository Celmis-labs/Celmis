"""The header line reads as a sentence for every kind of age the index can report."""

from __future__ import annotations

import pytest

from src.mcp_server.howto.engine import IdxInfo, Result, render


def _result(age: str) -> Result:
    return Result(slug="github_acme-shop", topic="db", idx=IdxInfo(
        slug="github_acme-shop", branch="develop", sha="8c39a1a0", age=age, state="fresh"),
        candidates=[], shown=[], names={}, stores=[], warnings=[], scanned=3, more=0,
        next_cursor="", extras=[])


@pytest.mark.parametrize(("age", "phrase"), [
    ("now", "(indexed just now)"),
    ("5m", "(indexed 5m ago)"),
    ("3d", "(indexed 3d ago)"),
    ("?", "(indexed at an unknown time)"),
    ("unknown", "(indexed at an unknown time)"),
])
def test_the_header_names_the_index_age_in_words(age: str, phrase: str) -> None:
    header = render(_result(age), "concise").splitlines()[1]
    assert phrase in header
    assert "now ago" not in header and "unknown ago" not in header
