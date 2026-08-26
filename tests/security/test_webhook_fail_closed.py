"""A review webhook with no secret configured must refuse, not accept.

Three handlers, one rule, and one of them broke it: Bitbucket ran

    if secret:
        if not _verify_bitbucket_signature(...): raise 401
    stats_counter["verified"] += 1

so an unset REVIEW_BITBUCKET_SECRET skipped verification altogether — and
still counted the request as verified. The comment directly above it said the
signature was required. GitHub and GitLab both raise 500 when their secret is
missing, which is the intended shape.

Why it matters more than a missing check usually does: every accepted event
starts an LLM review. An unauthenticated webhook URL is a way to spend a
workspace's budget and to feed the reviewer arbitrary pull-request content.

These tests read the handler source rather than driving FastAPI, so they need
no app, no settings and no HTTP client — and they pin the exact shape that
regressed, which a request-level test would not.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

WEBHOOK = Path(__file__).resolve().parents[2] / "src" / "review" / "webhook.py"

#: handler function name -> the settings attribute holding its secret
HANDLERS = {
    "github": "webhook_secret",
    "gitlab": "gitlab_token",
    "bitbucket": "bitbucket_secret",
}


def _handler_sources() -> dict[str, str]:
    """Source of each webhook handler, keyed by the provider in its name."""
    tree = ast.parse(WEBHOOK.read_text())
    lines = WEBHOOK.read_text().splitlines()
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for provider in HANDLERS:
            if provider in node.name.lower() and node.end_lineno:
                body = "\n".join(lines[node.lineno - 1:node.end_lineno])
                # Keep the longest match — helpers share the provider name.
                if len(body) > len(found.get(provider, "")):
                    found[provider] = body
    return found


@pytest.mark.parametrize("provider", sorted(HANDLERS))
def test_a_missing_secret_refuses_the_request(provider: str):
    """`if not secret: raise` — the guard, present in all three."""
    source = _handler_sources().get(provider, "")
    assert source, f"no handler found for {provider}"
    assert re.search(r"if not secret:", source), (
        f"{provider} does not refuse when its secret is unconfigured"
    )


@pytest.mark.parametrize("provider", sorted(HANDLERS))
def test_verification_is_not_conditional_on_having_a_secret(provider: str):
    """The exact regression: `if secret:` wrapping the signature check.

    It reads as prudence and behaves as a bypass — with no secret set, the
    check never runs and the request proceeds.
    """
    source = _handler_sources().get(provider, "")
    assert source
    assert not re.search(r"^\s+if secret:\s*$", source, re.M), (
        f"{provider} verifies only when a secret happens to be set"
    )


@pytest.mark.parametrize("provider", sorted(HANDLERS))
def test_a_request_is_counted_verified_only_after_the_check(provider: str):
    """`stats_counter["verified"]` after the raise, never before it.

    Bitbucket incremented it unconditionally, so the ops counters reported
    verified traffic that had been verified against nothing.
    """
    source = _handler_sources().get(provider, "")
    assert source
    verified_at = source.find('stats_counter["verified"]')
    invalid_at = max(source.find("Invalid signature"), source.find("Invalid token"))
    assert verified_at > 0 and invalid_at > 0, provider
    assert invalid_at < verified_at, (
        f"{provider} counts a request as verified before rejecting a bad one"
    )
