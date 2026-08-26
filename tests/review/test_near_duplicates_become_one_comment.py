"""One defect repeated down a file is one comment, and only then.

The shape, seen with the LLM veto OFF on the Martian bench: cal.com#10600
posted `defect.unintended_form_submit` three times — one per button — and
cal.com#10967 posted `sec.cwe-862` three times, every copy a false positive.
The exact dedup cannot fold that shape: it keys on the line, and these were
the same defect at different lines. Nor does THIS pass fold those particular
copies — a replay over that run shows them in three files, and the one
same-file pair at 0.43 on the title — so the fixtures below put the shape in
ONE file, which is the case this pass is built for. The cross-file copies are
folded by the second pass beside it, which is pinned in
tests/review/test_one_defect_in_four_files_is_one_comment.py.

So after exact dedup the prefilter buckets by (file, rule) and merges findings
whose TITLES overlap at >= 0.5 token-Jaccard into one finding that keeps the
worst severity, the first body, and lists every line. Kodus guards its LLM
dedup with Jaccard >= 0.3 on the full content; this is the deterministic half
only, and the part that matters most is what it must NOT do — two genuinely
different findings under one rule keep their two comments.
"""
from __future__ import annotations

import random

from src.review.agents.verifier import (
    NEAR_DUPLICATE_JACCARD,
    prefilter,
    title_jaccard,
)
from src.review.models import Finding, FindingSeverity

FORM = "apps/web/components/booking/BookingForm.tsx"


def _f(line, *, rule, title, body="", severity=FindingSeverity.WARNING,
       file=FORM, agent="architect", confidence=0.8, evidence_kind="inferred") -> Finding:
    return Finding(
        file_path=file, line=line, rule_id=rule, title=title, body=body,
        severity=severity, agent=agent, confidence=confidence,
        evidence_kind=evidence_kind,
    )


def _cal_com_10600() -> list[Finding]:
    """The shape the pass is for: one file, one rule, four near-identical
    titles at four lines. (The bench copies were three, in three files.)"""
    return [
        _f(42, rule="defect.unintended_form_submit",
           title="Button without type submits the form", body="first body"),
        _f(88, rule="defect.unintended_form_submit",
           title="Button without explicit type submits the enclosing form", body="second"),
        _f(120, rule="defect.unintended_form_submit",
           title="Button without type submits the form", body="third",
           severity=FindingSeverity.ERROR, agent="quality"),
        _f(171, rule="defect.unintended_form_submit",
           title="Button without type submits form", body="fourth"),
    ]


def test_the_cal_com_shape_becomes_one_finding_listing_every_line():
    result = prefilter(_cal_com_10600())

    assert len(result.kept) == 1
    assert result.dropped_near_duplicate == 3
    merged = result.kept[0]
    assert (merged.file_path, merged.line) == (FORM, 42), "anchored at the first occurrence"
    assert merged.severity is FindingSeverity.ERROR, "the worst severity any copy claimed"
    assert merged.body.startswith("first body"), "the first body, not a concatenation"
    for line in (88, 120, 171):
        assert str(line) in merged.body, f"line {line} vanished from the comment"
    assert merged.agent == "architect,quality"


def test_the_cal_com_10967_shape_three_wordings_one_authorisation_gap():
    result = prefilter([
        _f(30, rule="sec.cwe-862", title="Missing authorization check on booking update"),
        _f(74, rule="sec.cwe-862", title="Missing authorization check on booking delete"),
        _f(110, rule="sec.cwe-862", title="No authorization check on booking reschedule"),
    ])
    assert len(result.kept) == 1
    assert result.dropped_near_duplicate == 2


def test_two_different_findings_under_one_rule_are_not_merged():
    """The guard that makes the merge safe to run on every review: the same
    CWE id on two unrelated defects is two comments."""
    result = prefilter([
        _f(30, rule="sec.cwe-862", title="Attendee can delete any booking by id"),
        _f(74, rule="sec.cwe-862", title="Webhook secret compared with =="),
    ])
    assert len(result.kept) == 2
    assert result.dropped_near_duplicate == 0
    assert {f.line for f in result.kept} == {30, 74}


def test_the_same_title_under_two_rules_stays_apart():
    """The rule_id is the bucket in both passes, so one wording under two
    rules is two findings wherever they sit.

    This test used to assert a third thing — that the same title under one
    rule in two FILES also stayed apart — and that half is deliberately gone.
    It was the behaviour that let `defect.unintended_form_submit` be posted
    once per file on the measured run; the cross-file pass added beside this
    one now folds that shape into a single comment naming every path and line
    (tests/review/test_one_defect_in_four_files_is_one_comment.py, and
    CROSS_FILE_JACCARD for why it takes a stricter title match to do it)."""
    result = prefilter([
        _f(1, rule="defect.a", title="Unchecked result of parse"),
        _f(2, rule="defect.b", title="Unchecked result of parse"),
    ])
    assert len(result.kept) == 2
    assert result.dropped_near_duplicate == 0

    across_files = prefilter([
        _f(1, rule="defect.a", title="Unchecked result of parse"),
        _f(3, rule="defect.a", title="Unchecked result of parse", file="other.ts"),
    ])
    assert len(across_files.kept) == 1
    assert across_files.dropped_near_duplicate == 1


def test_the_threshold_is_the_documented_one():
    assert NEAR_DUPLICATE_JACCARD == 0.5
    assert title_jaccard("a b c d", "a b x y") == 1 / 3
    assert title_jaccard("a b c d", "a b c x") == 0.6
    assert title_jaccard("", "") == 0.0, "two missing titles are no evidence of sameness"


def test_untitled_findings_are_never_folded_into_each_other():
    result = prefilter([
        _f(1, rule="r", title=""),
        _f(2, rule="r", title=""),
    ])
    assert len(result.kept) == 2


def test_exact_dedup_happens_first_and_is_counted_apart():
    """Two agents on one line fold by line (consensus bonus, both names), and
    THEN the survivor folds with its copy on another line."""
    result = prefilter([
        _f(10, rule="r", title="Null deref", agent="architect"),
        _f(10, rule="r", title="Null deref", agent="security"),
        _f(50, rule="r", title="Null deref", agent="architect"),
    ])
    assert len(result.kept) == 1
    assert result.dropped_dedup == 1
    assert result.dropped_near_duplicate == 1
    assert "50" in result.kept[0].body


def test_a_proven_finding_stays_proven_through_both_merges():
    """The merged Finding used to be built with the default evidence_kind,
    so four copies of one proven CVE came out INFERRED — which is exactly the
    kind the LLM pass is allowed to veto. A database agreeing with itself is
    not a judgement."""
    cve = dict(rule="sec.cve-GHSA-1", title="lodash 4.17.15 — prototype pollution",
               file="package-lock.json", agent="cve", confidence=1.0,
               evidence_kind="proven")
    same_line = prefilter([_f(4102, **cve), _f(4102, **{**cve, "agent": "structural"})])
    assert same_line.kept[0].is_proven

    across_lines = prefilter([_f(4102, **cve), _f(9000, **cve)])
    assert across_lines.kept[0].is_proven


def test_the_result_does_not_depend_on_the_order_the_agents_finished_in():
    """The orchestrator collects results `as_completed`; the same review can
    hand the prefilter the same findings in a different order every run, and
    "the first body" must mean the same body each time."""
    findings = _cal_com_10600() + [
        _f(30, rule="sec.cwe-862", title="Attendee can delete any booking by id"),
        _f(74, rule="sec.cwe-862", title="Webhook secret compared with =="),
        _f(5, rule="q.x", title="Unused import", severity=FindingSeverity.INFO),
    ]
    expected = None
    for seed in range(6):
        shuffled = list(findings)
        random.Random(seed).shuffle(shuffled)
        got = [
            (f.file_path, f.line, f.rule_id, f.title, f.body, f.severity, f.agent)
            for f in prefilter(shuffled).kept
        ]
        if expected is None:
            expected = got
        assert got == expected, f"order of arrival changed the review (seed {seed})"


def test_the_kept_list_comes_out_in_severity_order():
    result = prefilter([
        _f(1, rule="a", title="one", severity=FindingSeverity.INFO),
        _f(2, rule="b", title="two", severity=FindingSeverity.CRITICAL),
        _f(3, rule="c", title="three", severity=FindingSeverity.WARNING),
        _f(4, rule="d", title="four", severity=FindingSeverity.ERROR),
    ])
    assert [f.severity for f in result.kept] == [
        FindingSeverity.CRITICAL, FindingSeverity.ERROR,
        FindingSeverity.WARNING, FindingSeverity.INFO,
    ]
