"""The deterministic half of a review must not need the model's permission.

Cross-repo drift is the one result in this product that needed no
interpretation: a constant changed here and left behind in a sibling
repository is a fact, found by comparison, not a judgement.

It was invisible exactly when it mattered most. The reviews page gated the
whole findings block on `findings_count > 0` — the MODEL's findings — while
drift lives in `drift_json` and is counted separately. So a run where the
agents found nothing and drift found three things rendered as an empty row.

The part that makes this worth a test rather than a one-line fix: the code
that handles the case was already there, written deliberately, with a comment
saying "Drift stands on its own... rendering 'no findings' over it would hide
the one result that needed no interpretation". It was unreachable. Nothing
failed; it simply never ran.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PAGE = (ROOT / "web" / "app" / "(app)" / "reviews" / "page.tsx").read_text(
    encoding="utf-8")


def _strip_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


# ─── the count ───────────────────────────────────────────────────────


def _row(drift: object) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE r (drift_json TEXT)")
    conn.execute("INSERT INTO r VALUES (?)",
                 (drift if isinstance(drift, str) or drift is None
                  else json.dumps(drift),))
    return conn.execute("SELECT * FROM r").fetchone()


@pytest.mark.parametrize("stored,expected", [
    ({"hits": [{"a": 1}, {"b": 2}, {"c": 3}]}, 3),
    ({"hits": []}, 0),
    (None, 0),
    ("", 0),
    ("not json at all", 0),
    ({"no_hits_key": True}, 0),
    ({"hits": "three"}, 0),
    ([1, 2, 3], 0),
])
def test_counting_hits_never_fails_a_list_request(stored, expected):
    """The column is TEXT added by a later migration, so it can be absent,
    NULL, empty, or shaped by an older version. None of that is worth failing
    a page over — the count feeds one badge and one boolean."""
    from src.api.review_runs import _drift_hits

    assert _drift_hits(_row(stored)) == expected


def test_a_row_without_the_column_at_all_counts_zero():
    """Rows written before the migration have no such column."""
    from src.api.review_runs import _drift_hits

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE r (id TEXT)")
    conn.execute("INSERT INTO r VALUES ('x')")
    assert _drift_hits(conn.execute("SELECT * FROM r").fetchone()) == 0


# ─── it reaches the browser ──────────────────────────────────────────


def test_the_run_carries_it():
    import dataclasses

    from src.api.review_runs import ReviewRun

    assert "drift_hits" in [f.name for f in dataclasses.fields(ReviewRun)]


def test_the_api_sends_it():
    from src.api.schemas import ReviewRunOut

    assert "drift_hits" in ReviewRunOut.model_fields
    src = (ROOT / "src" / "api" / "routers" / "reviews.py").read_text(
        encoding="utf-8")
    assert "drift_hits=" in src, "_run_to_out drops it on the way out"


# ─── the gate ────────────────────────────────────────────────────────


def test_drift_alone_opens_the_findings_block():
    """The whole point. A run with zero model findings and a drift report has
    something to show, and the gate must let it through."""
    body = _strip_comments(PAGE)
    gate = next((line for line in body.splitlines()
                 if "findings_count ?? 0) > 0" in line), None)
    assert gate is not None, "the findings gate moved — check this still holds"
    assert "drift_hits" in gate, (
        "the block still opens only for the model's findings, so a run whose "
        "only result is deterministic renders as empty"
    )


def test_the_unreachable_branch_is_still_there():
    """It handles exactly this case and was written before the gate hid it.
    If a later tidy-up deletes it as dead code, the fix above stops meaning
    anything."""
    body = _strip_comments(PAGE)
    assert "if (!p.findings.length) {" in body
    idx = body.index("if (!p.findings.length) {")
    assert "DriftPanel" in body[idx:idx + 500], (
        "the no-model-findings branch no longer renders drift"
    )
