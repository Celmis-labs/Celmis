"""`sha[:7]` on whatever arrived produced a 404 as the AGPL's offer of source.

The deploy script stamped the IMAGE DIGEST, which is immutable and therefore
looked like the safer identity to record. `"sha256:150805b1…"[:7]` is the
literal string `"sha256:"`, so `/api/capabilities` answered `0.1.0+sha256:`
and the §13 footer built a link ending `/tree/sha256:` — the source offer the
licence requires, pointing at nothing.

Two mistakes, and the second is the one worth keeping:

  * a digest names an IMAGE and the footer needs a COMMIT. The commit was
    available all along, in `org.opencontainers.image.revision` on the image
    itself; the script reads that now.
  * truncating is a CLAIM ABOUT SHAPE, and nothing checked the shape. Seven
    wrong characters are harder to notice than an obviously foreign string,
    and this value is read by a link somebody clicks.

Found by running the script, not by reading it. I named the two places I
expected to be wrong and both were clean; the break was in the step I called
the simplest.
"""

from __future__ import annotations

import pytest

from src.ops.build import build_info

COMMIT = "adccb2941ff1e0b5c7a3d9e8f6b4a2c1d0e9f8a7"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("CELMIS_GIT_SHA", raising=False)
    monkeypatch.delenv("CELMIS_DEPLOYED_AT", raising=False)
    build_info.cache_clear()
    yield
    build_info.cache_clear()


def test_a_commit_is_shortened(monkeypatch):
    monkeypatch.setenv("CELMIS_GIT_SHA", COMMIT)
    build_info.cache_clear()

    info = build_info()

    assert info["git_sha_short"] == COMMIT[:7]
    assert info["source"] == "env"


def test_a_digest_is_not_sliced_into_the_word_sha256(monkeypatch):
    """The exact failure: seven characters of a digest are its algorithm."""
    monkeypatch.setenv("CELMIS_GIT_SHA", "sha256:150805b1c0d0aa77bb")
    build_info.cache_clear()

    info = build_info()

    assert info["git_sha_short"] != "sha256:"
    assert "150805b1" in info["git_sha_short"], "the value was hidden instead"


def test_something_unrecognised_says_so(monkeypatch):
    """An operator who set this to something odd has to see what they set —
    and a caller building a URL has to be able to tell it is not a commit."""
    monkeypatch.setenv("CELMIS_GIT_SHA", "sha256:150805b1c0d0aa77bb")
    build_info.cache_clear()

    assert build_info()["source"] == "unrecognised"


@pytest.mark.parametrize("value", [
    "sha256:150805b1c0d0",      # an image digest
    "v0.1.0",                   # a tag
    "not-a-sha",
    "ADCCB2941FF1E0B5",         # upper case is not what git writes
])
def test_only_a_commit_shaped_value_is_shortened(monkeypatch, value):
    monkeypatch.setenv("CELMIS_GIT_SHA", value)
    build_info.cache_clear()

    info = build_info()

    assert info["git_sha_short"] == value, "a non-commit was truncated anyway"
    assert info["source"] == "unrecognised"


@pytest.mark.parametrize("value", ["adccb29", COMMIT])
def test_both_lengths_git_writes_are_accepted(monkeypatch, value):
    """`rev-parse --short` gives seven, `rev-parse HEAD` gives forty, and both
    reach this variable depending on who set it."""
    monkeypatch.setenv("CELMIS_GIT_SHA", value)
    build_info.cache_clear()

    assert build_info()["source"] == "env"


def test_an_empty_stamp_falls_back_to_the_checkout():
    """A developer running locally has the commit right there."""
    build_info.cache_clear()

    info = build_info()

    assert info["source"] in {"git", "unknown"}
    if info["source"] == "git":
        assert len(info["git_sha_short"]) == 7
