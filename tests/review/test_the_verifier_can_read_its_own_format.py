"""The false-positive filter could not express its strongest verdict.

The prompt asks the verifier for `{"keep": [...], "reasons": {...}}`. The
parser looked for it with `\\{[^{}]*"keep"[^{}]*\\}` — a pattern that cannot
cross the brace of the nested `reasons` object, so it never matched the
documented format at all. What made it look like it worked was a fallback
that scanned for a bare array of digits, and that fallback cannot see `[]`.

So the one answer this agent exists to give — "none of these findings are
real" — parsed as unreadable, and unreadable fails open: every finding the
verifier had just rejected was posted to the pull request.
"""

from __future__ import annotations

import pytest

from src.review.agents.verifier import VerifierAgent

parse = VerifierAgent._parse_keep_indices


@pytest.mark.parametrize("reply, expected, id_", [
    ('{"keep": [0, 2], "reasons": {"1": "duplicate"}}', [0, 2], "documented shape"),
    ('{"keep": [], "reasons": {"0": "no", "1": "no"}}', [], "REJECT EVERYTHING"),
    ('{"keep": [1]}', [1], "no reasons key"),
    ('```json\n{"keep": [1], "reasons": {"0": "noise"}}\n```', [1], "fenced"),
    ('<think>weigh {a} against {b}</think>{"keep": [], "reasons": {"0": "x"}}',
     [], "reasoning trace with braces"),
    ('Verdict below.\n{"keep": [2], "reasons": {"0": "a", "1": "b"}}\nDone.',
     [2], "prose on both sides"),
    ('keep these: [0, 1]', [0, 1], "bare array fallback"),
])
def test_the_verifier_is_understood(reply, expected, id_):
    assert parse(reply, total=3) == expected, id_


def test_rejecting_everything_is_not_the_same_as_being_unreadable():
    """The distinction the docstring in verifier.py already claimed to make."""
    assert parse('{"keep": [], "reasons": {"0": "hallucinated"}}', total=2) == []
    assert parse("I am not sure what to do here.", total=2) is None


def test_a_genuinely_unreadable_reply_still_fails_open():
    """Deliberate: an unreadable verifier must not silently delete real
    findings. Only the *misreading* was the bug, not this policy."""
    assert parse("", total=3) is None
    assert parse("no json at all", total=3) is None
    assert parse('{"kept": [1]}', total=3) is None, "wrong key is not 'keep'"


def test_a_brace_inside_a_reason_string_does_not_truncate_the_object():
    reply = '{"keep": [0], "reasons": {"1": "the literal { in the code"}}'
    assert parse(reply, total=2) == [0]
