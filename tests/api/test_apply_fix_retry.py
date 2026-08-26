"""Applying a fix twice is the commonest thing a person does.

Reported 14 Aug: the second press ALWAYS failed with

    commit failed: {"message":"frontend/src/components/TaskCard.tsx does not
    match 6392bf7c9a03cd3d6cd519b4d4fad0a5781a8b94"}

The mechanism, confirmed in the code: the file's blob sha is read on the PR's
head branch (step 1), the fix branch is created (step 3) or found to exist
already and reused, and the commit (step 4) is sent with the head-derived sha.
The branch name is deterministic, so the second press always lands on the
branch the first press created — where the blob is no longer the one that was
read. GitHub's contents API wants the sha as it exists ON THE TARGET BRANCH and
answers 409 otherwise.

The branch was reused; the sha was not.
"""

from __future__ import annotations

import re
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[2]
          / "src" / "api" / "routers" / "apply_fix.py").read_text()


def test_a_reused_branch_rereads_the_file_on_that_branch():
    assert "branch_reused = True" in SOURCE, "the reuse case is not tracked"
    idx = SOURCE.find("if branch_reused:")
    assert idx > 0, "nothing happens differently when the branch is reused"
    block = SOURCE[idx:idx + 900]
    assert '"ref": branch' in block, (
        "the file is not re-read on the fix branch, so the stale head_ref sha "
        "is still what gets committed"
    )
    assert 'file_sha = current["sha"]' in block


def test_the_reread_happens_before_the_commit():
    """Order is the whole bug. A re-read after the PUT fixes nothing."""
    reread = SOURCE.find("if branch_reused:")
    commit = SOURCE.find("put = http.put(")
    assert 0 < reread < commit


def test_a_second_press_after_success_is_a_no_op_not_a_commit():
    """Re-committing an identical blob is a junk commit; reporting failure is
    a lie. The truth is that it is already there."""
    assert "already_applied" in SOURCE
    idx = SOURCE.find("if already_applied:")
    assert idx > 0
    block = SOURCE[idx:idx + 600]
    assert "ok=True" in block, "an idempotent retry must not report failure"
    assert "already applied" in block


def test_a_missing_file_on_the_branch_sends_no_sha_at_all():
    """Creating a file takes no sha; sending a stale one is a guaranteed 409."""
    assert "file_sha = None" in SOURCE
    assert 'if file_sha:' in SOURCE and 'body["sha"] = file_sha' in SOURCE


def test_the_conflict_status_has_its_own_branch():
    """409 fell through to the generic `>= 300` handler, which dumped
    GitHub's raw JSON into a toast."""
    idx = SOURCE.find("if put.status_code == 409:")
    assert idx > 0, "409 is still handled by the generic branch"
    generic = SOURCE.find("if put.status_code >= 300:")
    assert idx < generic, "the 409 branch must come before the catch-all"
    block = SOURCE[idx:generic]
    # The full body in the LOG is where it belongs — the test is about what
    # reaches the client, so only the detail= is checked.
    detail = block[block.find("detail="):]
    assert "put.text" not in detail, "the raw provider body is back in the reply"
    assert "logger.warning" in block, "the body should still be logged"


def test_no_provider_body_reaches_the_client_from_the_commit_path():
    """The full body belongs in the log; GitHub's own one-line `message` is
    the only part a person can act on."""
    for match in re.finditer(r"detail=f?\"[^\"]*\{put\.text", SOURCE):
        raise AssertionError(
            f"raw provider body in a client-facing detail at offset {match.start()}"
        )
    assert "_gh_message(put)" in SOURCE


def test_the_message_extractor_never_returns_json():
    from types import SimpleNamespace

    from src.api.routers.apply_fix import _gh_message

    ok = SimpleNamespace(json=lambda: {
        "message": "path does not match abc123",
        "documentation_url": "https://docs.github.com/…",
        "errors": [{"resource": "Contents"}],
    })
    out = _gh_message(ok)
    assert out == "path does not match abc123"
    assert "documentation_url" not in out and "{" not in out

    def _boom():
        raise ValueError("not json")

    assert _gh_message(SimpleNamespace(json=_boom)) == "see server log"
    assert _gh_message(SimpleNamespace(json=lambda: ["a", "b"])) == "see server log"
