"""A filter that cannot be satisfied says so instead of answering "none".

THE DEFECT. The audit time filter is a lexicographic string compare, correct
for UTC ISO and silently wrong for anything else:

    GET /api/audit?from_ts=not-a-date
    → 200 {"records": [], "count": 0, …}

`"2026-08-23T07:00:00Z" < "not-a-date"` is true, so every record was skipped.
On a compliance page a typo in the date box is therefore indistinguishable
from "nothing happened" — which is the one answer an audit log must never
give by accident.

All three endpoints that accept the filter validate it: the list, the stats
the page renders beside it, and the CSV export. An export whose filter behaves
differently from the screen is its own defect.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException

from src.api.routers.audit import _validate_ts


@pytest.mark.parametrize("value", [
    "not-a-date", "2026-13-45", "yesterday", "23/08/2026", "0", "??",
])
def test_an_unparseable_timestamp_is_refused(value: str):
    with pytest.raises(HTTPException) as exc:
        _validate_ts(value, "from_ts")

    assert exc.value.status_code == 422
    assert "from_ts" in str(exc.value.detail)


def test_the_error_names_the_field_and_shows_the_value():
    """So the page can point at the box the user typed in."""
    with pytest.raises(HTTPException) as exc:
        _validate_ts("nope", "to_ts")

    detail = str(exc.value.detail)
    assert "to_ts" in detail
    assert "nope" in detail


@pytest.mark.parametrize("value", [
    "2026-08-23",
    "2026-08-23T07:00:00",
    "2026-08-23T07:00:00Z",
    "2026-08-23T07:00:00+00:00",
    "2026-08-23T07:00:00.123456Z",
])
def test_every_shape_the_ui_sends_is_accepted(value: str):
    assert _validate_ts(value, "from_ts") == value


def test_no_filter_is_not_a_bad_filter():
    assert _validate_ts(None, "from_ts") is None
    assert _validate_ts("", "from_ts") is None


def test_all_three_endpoints_validate():
    """The list, the stats beside it, and the export. A filter that only holds
    on screen is not a filter."""
    from src.api.routers import audit

    src = inspect.getsource(audit)
    # Two calls per endpoint (from_ts and to_ts), three endpoints.
    assert src.count('_validate_ts(from_ts, "from_ts")') == 3
    assert src.count('_validate_ts(to_ts, "to_ts")') == 3
