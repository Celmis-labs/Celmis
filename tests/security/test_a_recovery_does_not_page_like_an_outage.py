"""Grafana sends the same labels when an alert resolves as when it fires.

That is the design — the labels identify the RULE, not the event — and only
`status` says which of the two happened. `_parse_alerts` read the labels and
ignored `status`, so a recovery arrived carrying the outage's own severity and
the outage's own summary.

Reproduced end to end on production before this was written: a settlements
gateway came back up at 14:45 and the workspace was paged `critical` with the
title "5xx rate 100% over 1m" — byte for byte the page it had sent at 14:30
when the service actually broke.

Two harms, and the second is the one worth a test. A false page is noise. A
false page INDISTINGUISHABLE from the real one teaches people that a critical
card might mean nothing, which is how the next real outage gets scrolled past.
"""

from __future__ import annotations

import pytest

from src.api.routers.alerts import _parse_alerts

FIRING = {
    "status": "firing",
    "alerts": [{
        "status": "firing",
        "labels": {"alertname": "gateway_5xx_rate", "severity": "critical",
                   "repo": "celmis-demo-gateway"},
        "annotations": {"summary": "5xx rate 100% over 1m"},
    }],
}
RESOLVED = {
    "status": "resolved",
    "alerts": [{
        "status": "resolved",
        "labels": {"alertname": "gateway_5xx_rate", "severity": "critical",
                   "repo": "celmis-demo-gateway"},
        "annotations": {"summary": "5xx rate 100% over 1m"},
    }],
}


def test_the_outage_still_pages_at_its_own_severity() -> None:
    """The half that must not change. A fix that quiets real alerts is worse."""
    alert = _parse_alerts(FIRING)[0]
    assert alert["severity"] == "critical"
    assert alert["title"] == "5xx rate 100% over 1m"
    assert alert["repo_hint"] == "celmis-demo-gateway"


def test_a_recovery_is_not_critical() -> None:
    alert = _parse_alerts(RESOLVED)[0]
    assert alert["severity"] == "info", (
        "a resolved alert still carries the firing severity, so coming back up "
        "pages the workspace exactly as hard as going down did"
    )


def test_a_recovery_says_so_in_the_words_a_person_reads() -> None:
    """Severity routes; the title is what somebody actually sees on the card."""
    fired = _parse_alerts(FIRING)[0]["title"]
    healed = _parse_alerts(RESOLVED)[0]["title"]
    assert healed != fired, (
        f"the recovery and the outage read identically ({fired!r}); severity "
        f"alone is not on the card"
    )
    assert healed.lower().startswith("resolved"), healed


def test_the_two_are_told_apart_by_status_not_by_wording() -> None:
    """The labels are identical on purpose — that is what makes this a trap."""
    assert FIRING["alerts"][0]["labels"] == RESOLVED["alerts"][0]["labels"]
    assert (FIRING["alerts"][0]["annotations"]
            == RESOLVED["alerts"][0]["annotations"])
    assert _parse_alerts(FIRING)[0]["resolved"] is False
    assert _parse_alerts(RESOLVED)[0]["resolved"] is True


def test_one_member_resolving_inside_a_firing_batch_is_still_a_recovery() -> None:
    """Grafana sets the OUTER status to firing if any member is firing.

    Reading only the envelope would mark the resolved member as an incident.
    """
    mixed = {"status": "firing", "alerts": [
        FIRING["alerts"][0], RESOLVED["alerts"][0],
    ]}
    fired, healed = _parse_alerts(mixed)
    assert fired["severity"] == "critical"
    assert healed["severity"] == "info", (
        "the envelope said firing, so the resolved member was read as one"
    )


@pytest.mark.parametrize("payload", [
    {"status": "resolved", "title": "checkout is back", "severity": "critical"},
    {"status": "resolved", "message": "checkout is back", "severity": "error"},
])
def test_the_generic_shape_is_covered_too(payload: dict) -> None:
    """Not everything that posts here is Grafana; the rule is about status."""
    alert = _parse_alerts(payload)[0]
    assert alert["severity"] == "info"
    assert alert["title"].lower().startswith("resolved")
