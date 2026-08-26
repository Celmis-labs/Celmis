"""A knob is only settable if the value can reach the process that reads it.

`ReviewSettings` declares `env_file=_PROJECT_ROOT / ".env"`, which is true on a
developer's laptop and false everywhere the product actually runs: the image
does not carry a `.env`, and nothing mounts one. So inside the container the
ONLY route in is a name listed in docker-compose's `environment:` block, and a
setting the deployment does not forward silently takes the code default.

Nothing said which had happened. The reviews would run, the numbers would be
whatever they were, and finding out needed shell access to the host.

It is not hypothetical. The compose file forwards the review budget under its
PRE-RENAME spelling with a hardcoded default of 300 — the value a measurement
over 517 real reviews retired, because it cuts 14.3% of them short — while the
code says 900. Both numbers are defaults for one setting, which is the same
defect as every other one this week, one layer further out.

So the deadlines are reported. `/healthz` already carried two model names; it
now carries the numbers as this process resolved them, which is how anyone
learns that a value never arrived.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.review.settings import ReviewSettings

ROOT = Path(__file__).resolve().parents[2]

#: The settings whose whole purpose is to be turned by an operator. A number
#: enforced in code and unreachable from outside is worse than a hardcoded
#: one: it reads as an answer.
OPERATOR_SETTINGS = (
    "timeout_seconds",
    "llm_timeout_seconds",
    "llm_timeout_retry_factor",
    "max_diff_size_bytes",
    "cve_lookup_timeout_seconds",
    "verifier_enabled",
)


# ─── each one exists and is a decision ───────────────────────────────


@pytest.mark.parametrize("name", OPERATOR_SETTINGS)
def test_the_setting_exists(name):
    assert name in ReviewSettings.model_fields


@pytest.mark.parametrize("name", OPERATOR_SETTINGS)
def test_the_env_var_is_the_obvious_spelling(name, monkeypatch):
    """`env_prefix` plus the field name, with nothing doubled — the defect
    `review_timeout_seconds` carried for as long as it existed."""
    assert not name.startswith("review_"), (
        f"REVIEW_{name.upper()} would read REVIEW_REVIEW_… — a spelling "
        f"nobody guesses and pydantic silently ignores the other one"
    )


# ─── and the process says what it resolved ───────────────────────────


@pytest.fixture
def health():
    from fastapi.testclient import TestClient

    from src.review.webhook import build_webhook_app

    with TestClient(build_webhook_app()) as client:
        return client.get("/healthz").json()


@pytest.mark.parametrize("name", OPERATOR_SETTINGS)
def test_the_resolved_value_is_reported(health, name):
    assert name in health["review_settings"], (
        "a value the deployment dropped is indistinguishable from one it set"
    )


def test_the_reported_values_are_this_process_s(health):
    s = ReviewSettings()
    reported = health["review_settings"]
    assert reported["llm_timeout_seconds"] == s.llm_timeout_seconds
    assert reported["verifier_enabled"] == s.verifier_enabled


def test_no_secret_rides_along(health):
    """The endpoint is public. Everything here is an integer an operator
    chose; a key, a token or a URL must never join them."""
    blob = json.dumps(health).lower()
    for word in ("key", "token", "secret", "password", "://"):
        assert word not in blob, f"{word!r} in a public health payload"


# ─── the numbers still fit inside one another ────────────────────────


def test_one_agent_cannot_outlive_the_review_budget():
    """The worst case for a single agent is the deadline plus its widened
    retry. Past the review budget, the stage gate becomes unreachable and the
    budget is a dead setting again."""
    s = ReviewSettings()
    worst = s.llm_timeout_seconds * (1 + s.llm_timeout_retry_factor)
    assert worst <= s.timeout_seconds, (
        f"one agent may take {worst}s against a {s.timeout_seconds}s budget"
    )


def test_the_cve_sweep_fits_inside_its_own_timebox():
    import src.review.agents.cve as cve

    s = ReviewSettings()
    assert s.cve_lookup_timeout_seconds > cve._DEADLINE_MARGIN


def test_the_retry_widening_can_be_switched_off(monkeypatch):
    """1.0 is "same deadline, ask once more" — the old behaviour, for an
    install that would rather fail fast than hold a worker thread."""
    from src.review.settings import get_review_settings

    monkeypatch.setenv("REVIEW_LLM_TIMEOUT_RETRY_FACTOR", "1.0")
    get_review_settings.cache_clear()
    try:
        assert get_review_settings().llm_timeout_retry_factor == 1.0
    finally:
        get_review_settings.cache_clear()
