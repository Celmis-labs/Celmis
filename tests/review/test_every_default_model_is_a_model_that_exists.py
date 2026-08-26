"""`gemini-3-pro` does not exist, and three agents defaulted to it.

Google's model list has no such entry, and neither does the previous
generation's name:

    GET /v1beta/models  →  gemini-3-pro           absent
                           gemini-3-pro-preview   absent
                           gemini-3.1-pro-preview present

    litellm.completion("gemini/gemini-3-pro") → NotFoundError

`model_not_found` classifies as TERMINAL, so there is no retry, and
`fallback_model` does not help either — it exists for THROTTLED and TRANSIENT.
On a default install the architect, security and verifier agents therefore
failed immediately, every time, and a review ran on quality and tests alone.
Architect produces 60% of all confirmed findings on a 50-PR benchmark.

WHY IT SURVIVED: a workspace-wide `model` in the LLM settings overrides every
agent default, so every workspace anybody ever configured — including the
benchmark's — silently ran something else. Only a fresh, untouched workspace
hit it, which is to say every new customer and nobody testing.
"""

from __future__ import annotations

import pytest

from src.llm.capabilities import _model_info
from src.review.settings import ReviewSettings

MODEL_FIELDS = [
    f for f in ReviewSettings.model_fields
    if f.endswith("_model") and f != "fallback_model"
]


def test_there_are_model_fields_to_check():
    """The list is derived from the settings class, so a renamed field would
    silently empty it and the parametrised tests below would all vanish."""
    assert len(MODEL_FIELDS) >= 4, MODEL_FIELDS


@pytest.mark.parametrize("field", MODEL_FIELDS)
def test_the_default_is_a_name_the_client_can_resolve(field):
    default = ReviewSettings.model_fields[field].default
    assert isinstance(default, str) and default, f"{field} has no default"
    assert _model_info(default) is not None, (
        f"{field} defaults to {default!r}, which is not in the model table. "
        f"An unmapped name reaches the provider verbatim; if the provider "
        f"does not have it either, the agent fails TERMINAL on every run and "
        f"no fallback applies."
    )


def test_the_broken_name_is_gone():
    """Named explicitly, because a rename that reintroduces it would pass the
    table check the day litellm adds the mapping — and the model would still
    not exist at Google."""
    defaults = {ReviewSettings.model_fields[f].default for f in MODEL_FIELDS}
    assert "gemini-3-pro" not in defaults
    assert "gemini-3-pro-preview" not in defaults, (
        "in litellm's table, absent from Google's model list — the exact pair "
        "capabilities.py cites as its mapped/unmapped example"
    )


def test_the_documented_default_matches_the_code():
    """The module docstring lists each env var with its default. It said
    'gemini-3-pro' too — one wrong value in two places, and the docstring is
    the one somebody copies into an env file."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src/review/settings.py"
    doc = src.read_text(encoding="utf-8").split('"""')[1]
    quoted = set(re.findall(r"default '([^']+)'", doc))
    stale = {q for q in quoted if q.startswith("gemini") and _model_info(q) is None}
    assert not stale, f"the docstring advertises models that do not resolve: {stale}"


def test_a_workspace_override_is_still_free_to_be_anything():
    """The check is on the DEFAULTS. A workspace pointing an agent at a
    self-hosted or brand-new model must not be blocked by our table being
    behind — that is what `_model_info` returning None is designed to tolerate
    at runtime."""
    assert _model_info("some-model-nobody-has-heard-of") is None
