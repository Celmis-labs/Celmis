"""A timeout, an exhausted quota, a rejected key and a refusal read alike.

`classify` produced a curated sentence for every failure code it knows.
`AgentRunResult.error` carried it. The orchestrator logged it — and then
appended the agent's NAME to `agents_failed` and dropped the sentence. So the
pull-request comment and the run row rendered byte-identical text for four
problems with four different owners:

    ⚠ PARTIAL REVIEW — security did not run. … Check server logs for LLM errors.

"Check server logs" is an instruction a SaaS user cannot follow. The user whose
billing lapsed and the user whose model is merely slow got the same sentence,
and neither could tell which one they were.

WHY A CODE AND NOT THE PROSE. `_agent_error_text` keeps `str(exc)` verbatim for
an UNRECOGNISED failure, deliberately: a failure we cannot name should look
unnamed to the operator reading the record. But the banner goes into a public
pull-request comment, and a provider's own message is the one thing the errors
module exists to keep out of a user's face. So the agent now carries the CODE,
the banner asks `curated_reason` for a sentence written for publication, and a
code with no row yields nothing — the banner stays generic rather than dressing
an unknown failure as a familiar one.
"""

from __future__ import annotations

from src.llm.errors import curated_reason
from src.review.agents.base import AgentRunResult
from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    PullRequest,
    ReviewBatch,
)


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/api", number=7,
        title="t", description="d", author="alice",
        base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
        state="open",
        hunks=[Hunk(
            file_path="src/foo.py", old_file_path="src/foo.py",
            old_start=1, old_count=1, new_start=1, new_count=2,
            content="@@ -1 +1,2 @@\n line\n+added\n",
        )],
    )


def _batch(*, failed=(), reasons=None, run=("defect",), findings=1) -> ReviewBatch:
    b = ReviewBatch(pull_request=_pr())
    b.agents_failed = list(failed)
    b.agent_errors = dict(reasons or {})
    b.agents_run = list(run)
    b.findings = [Finding(
        file_path="src/foo.py", line=i + 1, severity=FindingSeverity.WARNING,
        title=f"c{i}", body="b", agent="defect",
    ) for i in range(findings)]
    return b


# ─── the gate between a record and a public comment ──────────────────


def test_a_known_failure_has_a_sentence_written_for_publication():
    assert curated_reason("quota_exhausted") == "provider quota exhausted"


def test_an_unknown_failure_yields_nothing_rather_than_an_invented_sentence():
    """A failure we cannot name must not be dressed as one we can, or the next
    reader stops looking."""
    assert curated_reason("something_new_from_a_provider") is None
    assert curated_reason(None) is None


def test_the_parse_failure_has_a_row_in_both_tables():
    """The module's own rule: a code without a disposition row falls through
    to UNRECOGNISED by accident rather than by decision, and one without a
    reason row gets the generic sentence that calls everything a provider."""
    from src.llm.errors import _DISPOSITION, _REASON

    assert "unreadable_reply" in _DISPOSITION
    assert "unreadable_reply" in _REASON


# ─── the agent carries it out ────────────────────────────────────────


def test_the_result_can_say_why():
    assert "error_code" in AgentRunResult.__dataclass_fields__
    assert AgentRunResult(agent="defect").error_code is None


def test_every_failing_exit_names_a_code():
    """Six returns in `_generate_and_parse` and `review` end in an error. One
    of them without a code is one failure class that silently goes back to
    "check server logs" — the state this whole change is undoing."""
    import ast
    import inspect

    import src.review.agents.base as base

    tree = ast.parse(inspect.getsource(base))
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "AgentRunResult":
            continue
        kwargs = {k.arg for k in node.keywords if k.arg}
        if "error" in kwargs and "error_code" not in kwargs:
            missing.append(node.lineno)
    assert not missing, f"AgentRunResult(error=…) with no code at lines {missing}"


# ─── and the reader is told ──────────────────────────────────────────


def test_the_banner_says_why_not_where_to_look():
    banner = _batch(failed=["security"],
                    reasons={"security": "provider quota exhausted"}).partial_banner

    assert "provider quota exhausted" in banner
    assert "Check server logs" not in banner, (
        "an instruction a SaaS user cannot follow"
    )


def test_two_agents_failing_differently_say_two_things():
    banner = _batch(
        failed=["security", "contract"],
        reasons={"security": "provider quota exhausted",
                 "contract": "the request passed this installation's own timeout"},
    ).partial_banner

    assert "provider quota exhausted" in banner
    assert "own timeout" in banner


def test_one_cause_stopping_three_agents_is_said_once():
    """Three agents behind one exhausted quota is one fact, not three."""
    reason = "provider quota exhausted"
    banner = _batch(
        failed=["security", "contract", "defect"],
        reasons=dict.fromkeys(("security", "contract", "defect"), reason),
    ).partial_banner

    assert banner.count(reason) == 1
    assert "security, contract, defect" in banner


def test_a_failure_with_no_publishable_reason_leaves_the_banner_generic():
    """Silence is the right ending for "we have no sentence for this yet"."""
    banner = _batch(failed=["security"], reasons={}).partial_banner

    assert "⚠ PARTIAL REVIEW" in banner and "security" in banner
    assert "Check server logs" not in banner


def test_the_unreviewed_arm_carries_it_too():
    """The arm for a review where nothing answered at all — the one where the
    reader most needs to know whose problem it is."""
    banner = _batch(failed=["defect"], reasons={"defect": "provider quota exhausted"},
                    run=(), findings=0).partial_banner

    assert "REVIEW FAILED" in banner
    assert "provider quota exhausted" in banner


def test_it_reaches_the_posted_comment():
    from src.review.providers.base import _format_summary

    posted = _format_summary(
        _batch(failed=["security"], reasons={"security": "provider quota exhausted"}),
        "<!-- celmis -->",
    )
    assert "provider quota exhausted" in posted
