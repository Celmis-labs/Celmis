"""A claim the agent cannot derive is not a finding.

Measured on the Martian Code Review Bench, 14 PRs, one judge: the LLM
verifier took false positives from 31 to 6 and true positives from 24 to 10
— its DROP rules ("a risk, not a consequence", "line not in the diff",
"severity mismatch") delete exactly the shapes the bench rewards, because the
judge reads the comment text and never sees severity. The tool ranked first
keeps by default and gates on facts decided in code.

So the gate here is two facts no model weighs: the `reasoning` sentence is
there, and the file is one the PR changes. Both are settled at parse time,
each drop is counted so a run can say "2 findings, 8 unbacked" instead of
looking clean, and a line the PR did not touch is deliberately NOT a gate —
a change that breaks an untouched caller is a finding on the caller's line.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from src.review.agents.base import (
    AgentContext,
    AgentRunResult,
    LLMReviewAgent,
    ParsedFindings,
)
from src.review.models import FindingSeverity, Hunk, PullRequest


class _Agent(LLMReviewAgent):
    name = "architect"
    severity_default = FindingSeverity.WARNING
    system_prompt = "s"
    user_prompt_template = "{diff}"
    model = "test-model"


def _hunk(path: str, old_path: str | None = None) -> Hunk:
    return Hunk(
        file_path=path, old_file_path=old_path or path,
        old_start=1, old_count=2, new_start=1, new_count=4,
        content="@@ -1,2 +1,4 @@\n line\n+added1\n+added2\n",
    )


def _pr(*hunks: Hunk, skipped: list[str] | None = None) -> PullRequest:
    return PullRequest(
        provider="github", repo="o/r", number=1,
        title="t", description="d", author="alice",
        base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
        state="open",
        hunks=list(hunks) or [_hunk("src/foo.py")],
        skipped_files=skipped or [],
    )


def _ctx(pr: PullRequest | None = None) -> AgentContext:
    return AgentContext(pull_request=pr or _pr())


def _item(**over: object) -> str:
    import json

    base: dict[str, object] = {
        "reasoning": "x is read on line 2 before line 3 assigns it",
        "file": "src/foo.py", "line": 2, "severity": "error",
        "title": "x read before assignment", "body": "b", "rule_id": "arch.x",
        "confidence": 0.8,
    }
    base.update(over)
    for key in [k for k, v in base.items() if v is None]:
        del base[key]
    return json.dumps([base])


# ─── the reasoning gate ──────────────────────────────────────────────


def test_a_reply_with_no_reasoning_yields_no_finding_and_counts_it():
    out = _Agent()._parse_findings(_item(reasoning=None), _ctx())
    assert out == []
    assert out.dropped_no_evidence == 1


def test_blank_reasoning_is_no_reasoning():
    out = _Agent()._parse_findings(_item(reasoning="   \n"), _ctx())
    assert out == []
    assert out.dropped_no_evidence == 1


def test_a_backed_claim_is_a_finding_and_counts_nothing():
    out = _Agent()._parse_findings(_item(), _ctx())
    assert len(out) == 1
    assert out[0].reasoning == "x is read on line 2 before line 3 assigns it"
    assert out.dropped_no_evidence == 0


def test_a_malformed_item_is_not_an_unbacked_claim():
    """No file, no line — the model did not make a claim, it made a mess.
    The counter is for claims the model made and could not support."""
    out = _Agent()._parse_findings('[{"line": 5, "title": "t"}]', _ctx())
    assert out == []
    assert out.dropped_no_evidence == 0


# ─── the file gate ───────────────────────────────────────────────────


def test_a_file_outside_the_pr_is_dropped_and_counted():
    out = _Agent()._parse_findings(_item(file="src/other.py"), _ctx())
    assert out == []
    assert out.dropped_no_evidence == 1


def test_an_untouched_line_in_a_changed_file_is_kept():
    """The hunk covers lines 1-4. Line 400 is not in the diff, and the
    finding stays: the change on line 2 is what reaches it."""
    out = _Agent()._parse_findings(_item(line=400), _ctx())
    assert len(out) == 1
    assert out[0].line == 400
    assert out.dropped_no_evidence == 0


@pytest.mark.parametrize("spelling", ["./src/foo.py", "repo-name/src/foo.py", "foo.py", "/src/foo.py"])
def test_the_models_spelling_of_a_changed_path_is_resolved_to_the_prs(spelling):
    out = _Agent()._parse_findings(_item(file=spelling), _ctx())
    assert len(out) == 1, spelling
    assert out[0].file_path == "src/foo.py"


def test_an_ambiguous_basename_is_not_guessed():
    pr = _pr(_hunk("src/foo.py"), _hunk("tests/foo.py"))
    out = _Agent()._parse_findings(_item(file="foo.py"), _ctx(pr))
    assert out == []
    assert out.dropped_no_evidence == 1


def test_a_renames_old_path_and_a_skipped_file_both_count_as_changed():
    pr = _pr(_hunk("src/new.py", old_path="src/old.py"), skipped=["assets/big.bin"])
    on_old = _Agent()._parse_findings(_item(file="src/old.py"), _ctx(pr))
    on_skipped = _Agent()._parse_findings(_item(file="assets/big.bin"), _ctx(pr))
    assert len(on_old) == 1 and len(on_skipped) == 1


def test_with_no_pr_the_file_gate_stands_down_but_the_reasoning_gate_does_not():
    """A directly built call with nothing to gate against must not drop
    everything it cannot check — that would be a review lost over a filter.
    The reasoning sentence needs no PR to be checked, so it still is."""
    kept = _Agent()._parse_findings(_item(file="anything/at/all.py"), None)
    assert len(kept) == 1
    dropped = _Agent()._parse_findings(_item(reasoning=None), None)
    assert dropped == [] and dropped.dropped_no_evidence == 1


# ─── what the drop leaves behind ─────────────────────────────────────


def test_the_drop_is_logged_at_debug_with_the_title(caplog):
    with caplog.at_level(logging.DEBUG, logger="src.review.agents.base"):
        _Agent()._parse_findings(_item(reasoning=None, title="x read before assignment"), _ctx())
    lines = [r for r in caplog.records if r.levelno == logging.DEBUG and "no_evidence" in r.getMessage()]
    assert len(lines) == 1
    assert "x read before assignment" in lines[0].getMessage()


def test_every_claim_unbacked_is_said_out_loud(caplog):
    with caplog.at_level(logging.WARNING, logger="src.review.agents.base"):
        _Agent()._parse_findings(_item(reasoning=None), _ctx())
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("dropped=1" in w for w in warnings), warnings


def test_one_backed_claim_among_unbacked_ones_is_not_a_warning(caplog):
    import json

    items = json.loads(_item()) + json.loads(_item(reasoning=None))
    with caplog.at_level(logging.WARNING, logger="src.review.agents.base"):
        out = _Agent()._parse_findings(json.dumps(items), _ctx())
    assert len(out) == 1 and out.dropped_no_evidence == 1
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


# ─── the count reaches the run record ────────────────────────────────


def test_the_count_rides_on_the_run_result():
    import json

    items = (
        json.loads(_item())
        + json.loads(_item(reasoning=None, title="unbacked"))
        + json.loads(_item(file="src/elsewhere.py", title="outside"))
    )
    response = MagicMock()
    response.text = json.dumps(items)
    response.input_tokens = 10
    response.output_tokens = 5
    with patch("src.llm.client.build_llm_client") as build:
        client = MagicMock()
        client.generate.return_value = response
        build.return_value = client
        result = _Agent().review(_ctx())

    assert result.error is None
    assert [f.title for f in result.findings] == ["x read before assignment"]
    assert result.dropped_no_evidence == 2


def test_the_result_lifts_the_count_off_the_list_and_an_explicit_one_wins():
    lifted = AgentRunResult(agent="a", findings=ParsedFindings([], dropped_no_evidence=3))
    assert lifted.dropped_no_evidence == 3
    plain = AgentRunResult(agent="a", findings=[])
    assert plain.dropped_no_evidence == 0
    explicit = AgentRunResult(
        agent="a", findings=ParsedFindings([], dropped_no_evidence=3), dropped_no_evidence=7,
    )
    assert explicit.dropped_no_evidence == 7
