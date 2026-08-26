"""A review that could not read the model's answer must not say "looks fine".

Three separate mechanisms had to agree for this to go wrong, and they did:

  1. `_extract_json_array` scanned with a greedy `(\\[.*\\])`, so one line of
     prose after the array — or a single bracket inside a reasoning trace —
     captured text that was not JSON.
  2. `_parse_findings` turned that into `[]` and left `AgentRunResult.error`
     as None, so the orchestrator counted the agent in `agents_run`.
  3. An agent that ran with no findings is a clean agent, so `compute_verdict`
     returned APPROVE — and where it did degrade to COMMENT for a failed
     critical agent, `mark_complete` upgraded that straight back to APPROVE
     because there were no findings to comment on.

The result was a review product that green-lit a pull request it had never
successfully read, with nothing in the output saying so.
"""

from __future__ import annotations

import json

import pytest

from src.review.agents.base import (
    AgentReplyUnreadable,
    LLMReviewAgent,
    balanced_json_span,
)
from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    PullRequest,
    ReviewBatch,
    ReviewVerdict,
)

FINDING = '[{"reasoning": "line 2 concatenates user input into the query", ' \
          '"file": "src/foo.py", "line": 2, "severity": "critical", ' \
          '"title": "SQL injection", "description": "concatenated query"}]'


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="o/r", number=1,
        title="t", description="d", author="alice",
        base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
        state="open",
        hunks=[Hunk(
            file_path="src/foo.py", old_file_path="src/foo.py",
            old_start=1, old_count=1, new_start=1, new_count=2,
            content="@@ -1 +1,2 @@\n line\n+added\n",
        )],
    )


# ── the extractor ────────────────────────────────────────────────────

@pytest.mark.parametrize("reply", [
    pytest.param(FINDING, id="bare array"),
    pytest.param(f"```json\n{FINDING}\n```", id="fenced"),
    pytest.param(f"<think>compare [a] with [b]</think>\n{FINDING}",
                 id="reasoning trace with brackets"),
    pytest.param(f"<thinking>see [1]</thinking>{FINDING}", id="thinking tag"),
    pytest.param(f"{FINDING}\n\nI flagged the query on line [42].",
                 id="prose with a bracket after the array"),
    pytest.param(f"Here is what I found:\n{FINDING}\nDone.",
                 id="prose on both sides"),
])
def test_the_finding_survives_whatever_the_model_wrapped_it_in(reply):
    extracted = LLMReviewAgent._extract_json_array(reply)
    assert extracted is not None, "no array found at all"
    data = json.loads(extracted)
    assert len(data) == 1
    assert data[0]["title"] == "SQL injection"


def test_a_bracket_inside_a_string_does_not_end_the_array():
    reply = '[{"file": "a.py", "line": 1, "severity": "warning", ' \
            '"title": "unclosed [", "description": "the ] is data"}]'
    assert json.loads(LLMReviewAgent._extract_json_array(reply))[0]["title"] \
        == "unclosed ["


def test_the_scanner_stops_at_the_matching_bracket():
    assert balanced_json_span("noise [1, [2]] tail [9]", "[") == "[1, [2]]"
    assert balanced_json_span("{}", "{") == "{}"
    assert balanced_json_span("no brackets here", "[") is None
    assert balanced_json_span("[1, 2", "[") is None, "unterminated is not a span"


# ── the parse result ─────────────────────────────────────────────────

class _Agent(LLMReviewAgent):
    name = "architect"
    system_instruction = "s"

    def _build_prompt(self, context):  # pragma: no cover - unused
        return "p"


@pytest.mark.parametrize("reply, why", [
    ("I could not review this.", "no array at all"),
    ('[{"file": "a.py",]', "malformed JSON"),
    ('[{"file": "a.py", "line": 1},]', "trailing comma"),
])
def test_an_unreadable_reply_raises_instead_of_reporting_zero_findings(reply, why):
    with pytest.raises(AgentReplyUnreadable):
        _Agent()._parse_findings(reply, None)


def test_a_wrapper_object_is_tolerated():
    """`{"findings": [...]}` is not the documented shape, but the array inside
    it is unambiguous. Leniency here costs nothing; leniency about an absent
    array is what produced the silent approval."""
    assert len(_Agent()._parse_findings(
        '{"findings": ' + FINDING + '}', None)) == 1


def test_an_empty_array_is_a_clean_review_not_a_failure():
    """The prompt says: if nothing is found, return `[]`. That is an answer."""
    assert _Agent()._parse_findings("[]", None) == []


# ── the verdict ──────────────────────────────────────────────────────

def _batch(**kw) -> ReviewBatch:
    b = ReviewBatch(pull_request=_pr(), **kw)
    b.verdict = b.compute_verdict()
    b.mark_complete()
    return b


def test_a_critical_agent_that_failed_blocks_the_approval():
    b = _batch(agents_run=["security"], agents_failed=["architect"])
    assert b.verdict is not ReviewVerdict.APPROVE, (
        "architect never produced a verdict, so nobody checked the "
        "architecture — that is not an approval"
    )
    assert b.verdict is ReviewVerdict.COMMENT


def test_mark_complete_does_not_undo_that():
    """`COMMENT` + no findings normally means APPROVE. Not when the emptiness
    is *because* an agent failed — which is exactly the shape of this bug."""
    b = ReviewBatch(pull_request=_pr(), agents_failed=["security"])
    b.verdict = ReviewVerdict.COMMENT
    b.mark_complete()
    assert b.verdict is ReviewVerdict.COMMENT


def test_a_clean_run_still_approves():
    """The guard must not turn every quiet review into a comment."""
    b = _batch(agents_run=["architect", "security"])
    assert b.verdict is ReviewVerdict.APPROVE


def test_a_non_critical_failure_still_approves_a_clean_diff():
    b = _batch(agents_run=["architect", "security"], agents_failed=["style"])
    assert b.verdict is ReviewVerdict.APPROVE


def test_findings_still_drive_the_verdict():
    f = Finding(
        file_path="src/foo.py", line=2, severity=FindingSeverity.CRITICAL,
        title="x", body="y", agent="security",
    )
    b = _batch(findings=[f], agents_run=["architect", "security"])
    assert b.verdict is ReviewVerdict.REQUEST_CHANGES
