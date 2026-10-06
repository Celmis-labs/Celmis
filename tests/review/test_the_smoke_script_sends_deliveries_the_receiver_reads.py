"""`scripts/pr_commands_smoke.py` builds a delivery per provider; each must be
one the extractors read as a command, and each must be signed the way its
receiver verifies it."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from scripts import pr_commands_smoke as smoke
from src.review.commands import events
from src.review.webhook import _verify_github_signature


def _read(provider: str, payload: dict):
    if provider == "github":
        return events.extract_github_comment(payload, "issue_comment")
    if provider == "bitbucket":
        return events.extract_bitbucket_comment(payload, "pullrequest:comment_created")
    return events.extract_gitlab_note(payload)


@pytest.mark.parametrize("provider", sorted(smoke.BUILDERS))
def test_each_synthetic_delivery_reads_as_the_comment_that_was_asked_for(provider):
    payload, _ = smoke.BUILDERS[provider]("acme/shop", 7, "@celmis review --force", "smoke")
    ev = _read(provider, payload)
    assert ev is not None
    assert (ev.repo, ev.pr_number, ev.body) == ("acme/shop", 7, "@celmis review --force")


def test_a_github_delivery_is_signed_so_the_receiver_accepts_it():
    body = b'{"a": 1}'
    header = smoke._sign("github", body, "s3cret")["X-Hub-Signature-256"]
    assert _verify_github_signature(body, header, "s3cret")
    assert not _verify_github_signature(body, header, "other")


def test_a_bitbucket_delivery_uses_the_hmac_header_and_gitlab_the_token():
    body = b"{}"
    expected = "sha256=" + hmac.new(b"k", body, hashlib.sha256).hexdigest()
    assert smoke._sign("bitbucket", body, "k") == {"X-Hub-Signature": expected}
    assert smoke._sign("gitlab", body, "k") == {"X-Gitlab-Token": "k"}
