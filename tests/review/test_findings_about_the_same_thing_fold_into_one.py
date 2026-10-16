"""One defect reported at several lines folds into one comment; distinct ones do not.

A discount check written as `if ($coupon)` can be reported three times in one
file, each in its own words, and an unclosed `<figure>` twice. The title-token
comparison scores every such pair near 0.0 because sentences about one variable
share no words. The pass added here compares what the findings are ABOUT (the
variable, the function, the tag), and is written to err toward keeping both
comments, which the negative tests pin down.
"""

from __future__ import annotations

from src.review.agents.root_cause_keys import root_cause_keys, share_a_root_cause
from src.review.agents.verifier import prefilter
from src.review.models import Finding, FindingSeverity


def _f(line, title, body, *, rule="sec.cwe-862", path="a.php", agent="security",
       kind="inferred", sev=FindingSeverity.ERROR):
    return Finding(file_path=path, line=line, severity=sev, title=title, body=body,
                   agent=agent, rule_id=rule, confidence=0.9, evidence_kind=kind)


def _kept(*findings):
    return prefilter(list(findings), confidence_threshold=0.0)


GUARD = [
    _f(110, "Price check is skipped for orders without a coupon",
       "The total is validated only when `$coupon` is set."),
    _f(240, "Missing validation when the cart is loaded",
       "The condition `if ($coupon)` lets a cart without a coupon skip the total check."),
    _f(380, "Validation bypass when the invoice is built",
       "Using `&& $coupon` means the check does not run for plain carts."),
]


def test_one_guard_reported_three_times_is_one_comment_with_every_line():
    res = _kept(*GUARD)
    assert len(res.kept) == 1
    assert res.dropped_near_duplicate == 2
    body = res.kept[0].body
    assert res.kept[0].line == 110
    assert "240" in body and "380" in body, "every location stays in the body"


def test_an_open_figure_and_a_stranded_caption_are_one_markup_defect():
    a = _f(15, "Missing closing tag", "The `<figure>` is opened and never closed.",
           rule="defect.logic-error", agent="defect", path="t.php")
    b = _f(27, "Caption is no longer grouped with its image",
           "The caption element ended up outside the `<figure>`.",
           rule="defect.logic-error", agent="defect", path="t.php")
    res = _kept(a, b)
    assert len(res.kept) == 1 and "27" in res.kept[0].body


# ─── what must NOT merge ─────────────────────────────────────────────


def test_two_functions_sharing_only_a_builtin_stay_apart():
    """Two real, separately counted defects in one file: both mention
    `Array.isArray`, in different functions."""
    a = _f(512, "Array method called before the type check",
           "In `renderPosts` the call `posts.map` runs before `Array.isArray(posts)`.",
           rule="defect.logic-error", agent="defect", path="c.js")
    b = _f(480, "Wrong type check for an object",
           "In `editPost` the check `Array.isArray(posts)` is applied to an object.",
           rule="defect.logic-error", agent="defect", path="c.js")
    assert len(_kept(a, b).kept) == 2


def test_a_generic_name_does_not_tie_findings_together():
    a = _f(10, "No validation", "The value `$data` reaches the query unchecked.")
    b = _f(90, "Other problem", "The array `$data` is empty and `$result` is never filled.")
    assert len(_kept(a, b).kept) == 2
    assert root_cause_keys("x", "`$data` and $result and $this") == frozenset()


def test_one_shared_key_among_many_is_not_enough():
    a = _f(10, "t1", "`$coupon` is used by `saveCart` and `loadCart` with `$token`.")
    b = _f(90, "t2", "`$coupon` is only logged by `auditTrail`, never checked, see `$sink`.")
    assert not share_a_root_cause(root_cause_keys(a.title, a.body), root_cause_keys(b.title, b.body))
    assert len(_kept(a, b).kept) == 2


def test_other_file_or_other_rule_is_never_folded_here():
    other_file = _f(240, GUARD[1].title, GUARD[1].body, path="b.php")
    assert len(_kept(GUARD[0], other_file).kept) == 2
    other_rule = _f(240, GUARD[1].title, GUARD[1].body, rule="sec.cwe-89")
    assert len(_kept(GUARD[0], other_rule).kept) == 2


def test_a_proven_finding_is_never_folded():
    proven = _f(240, GUARD[1].title, GUARD[1].body, kind="proven")
    assert len(_kept(GUARD[0], proven).kept) == 2


def test_findings_that_name_nothing_are_left_alone():
    a = _f(1, "One problem", "Something is wrong.")
    b = _f(2, "Another problem", "Also wrong.")
    assert len(_kept(a, b).kept) == 2


def test_the_merge_does_not_chain_through_a_third_finding():
    """C overlaps B but not A; folding is by the first member only."""
    a = _f(1, "ta", "`$alpha` and `$beta` are unchecked.")
    b = _f(2, "tb", "`$beta` and `$gamma` are unchecked.")
    c = _f(3, "tc", "`$gamma` and `$delta` are unchecked.")
    assert len(_kept(a, b, c).kept) >= 2
