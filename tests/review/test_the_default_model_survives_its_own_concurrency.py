"""The shipped default failed under the concurrency the product itself creates.

A stronger model IS better at this task, and the argument for one is measured:
SWR-Bench (arXiv:2509.01494) found identical precision and 67% more recall,
and recall is what F2 pays for. That argument assumes the call comes back.

ONE REVIEW MAKES THREE CONCURRENT CALLS — defect, contract and security run in
parallel — and `CELMIS_SYNC_WORKER_CONCURRENCY` ships at 2, so two overlapping
reviews make six. Measured against Google directly, same prompt, same day:

    gemini-3.1-pro-preview   3 concurrent → 3/3, slowest 51.6s
                             4 concurrent → 3/4, one HTTP 503
    gemini-3.6-flash         3 concurrent → 3/3, slowest 10.0s
                             4 concurrent → 4/4, slowest 11.1s

Fifty-one seconds at the concurrency one review creates, and a refusal one step
past it. Somebody installing this and opening a pull request meets that before
they see any recall at all — and the fallback model that hid it on our own
benchmark install is a field a fresh workspace does not have.

WHAT THIS PINS is not the model name. It is that the three places which have to
agree still agree — the settings class, the bootstrap the gateway reads, and
the compose default — because the last time they disagreed, three agents
defaulted to `gemini-3-pro`, a name that does not exist, and every fresh
install failed TERMINAL on every review while configured workspaces silently
ran their own override and never saw it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")

REVIEW_FIELDS = ("defect_model", "contract_model", "security_model", "verifier_model")


def _compose_default(var: str) -> str:
    """The value compose falls back to when the operator sets nothing."""
    m = re.search(rf"{var}:-([^}}\"]+)", COMPOSE)
    assert m, f"{var} has no default in docker-compose.yml"
    return m.group(1).strip()


# ─── the three layers say the same thing ─────────────────────────────


def test_every_review_agent_shares_one_default():
    """A roster where one agent is on a different model by accident is a
    benchmark that measures two things and reports one."""
    from src.review.settings import ReviewSettings

    s = ReviewSettings()
    models = {getattr(s, f) for f in REVIEW_FIELDS}
    assert len(models) == 1, f"the roster is split across {models}"


def test_the_bootstrap_matches_the_settings_class():
    from src.review.settings import ReviewSettings

    assert _compose_default("GEMINI_REVIEW_MODEL") == ReviewSettings().defect_model


def test_the_gateway_bootstraps_the_review_model_not_the_chat_one():
    """They used to share one variable. Chat makes one call at a time and can
    afford the stronger model; review cannot."""
    assert 'CELMIS_BOOTSTRAP_REVIEW_MODEL: "gemini/${GEMINI_REVIEW_MODEL' in COMPOSE
    assert 'CELMIS_BOOTSTRAP_CHAT_MODEL: "gemini/${GEMINI_GENERATION_MODEL' in COMPOSE


def test_chat_and_review_can_differ():
    """The split is the point. Collapsing them back would re-create the choice
    between a slow review and a weak chat."""
    assert _compose_default("GEMINI_GENERATION_MODEL") != _compose_default("GEMINI_REVIEW_MODEL")


@pytest.mark.parametrize("var", ["GEMINI_GENERATION_MODEL", "GEMINI_REVIEW_MODEL"])
def test_the_operator_can_reach_both(var):
    """A setting only the code knows about is not a setting — the image
    carries no .env, so `environment:` is the only route in."""
    assert f"{var}:" in COMPOSE
    assert var in EXAMPLE, f"{var} is not in .env.example"


# ─── and the name is a name the provider knows ───────────────────────


def test_no_default_names_a_model_that_does_not_exist():
    """`gemini-3-pro` was in litellm's capability table and absent from
    Google's model list. Being in a table and existing are two different
    facts, and the difference cost every fresh install its reviews."""
    from src.review.settings import ReviewSettings

    s = ReviewSettings()
    for f in REVIEW_FIELDS:
        name = getattr(s, f)
        assert name not in ("gemini-3-pro", "gemini-3-pro-preview"), f
        assert name.startswith("gemini-"), f
    for var in ("GEMINI_REVIEW_MODEL", "GEMINI_GENERATION_MODEL"):
        assert "gemini-3-pro" not in _compose_default(var)


def test_the_concurrency_that_forced_this_is_written_down():
    """The measurement lives beside the value it decided. A default whose
    reason is only in a commit message is one the next person re-litigates."""
    settings_src = (ROOT / "src/review/settings.py").read_text(encoding="utf-8")
    assert "concurrent" in settings_src
    assert "503" in settings_src


def test_review_concurrency_still_makes_three_calls():
    """The premise. If this drops to 1, the whole argument above needs
    re-measuring rather than quietly surviving."""
    from src.review.settings import ReviewSettings

    assert ReviewSettings().agent_concurrency >= 3
