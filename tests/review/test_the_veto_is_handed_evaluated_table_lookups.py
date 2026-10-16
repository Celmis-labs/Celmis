"""Table lookups near a finding are evaluated by code and shown to the veto.

A finding can say that one branch of a `switch` reads `$rates[3][1]`, "the index
already used by the other branch". The table is in the window, the answer is
arithmetic, and a small verifier model may keep the finding anyway. The
gatherer evaluates `name[<literal>]...` against the nearest earlier array/map
literal and puts the computed value in the evidence.
"""

from __future__ import annotations

from src.review.agents.literal_lookups import resolve_lookups
from src.review.agents.verifier_evidence import FindingEvidence, render_evidence

PHP = """\
<?php
$rates = [1 => [10,20], 2 => [20,30], 3 => [30,40], 4 => [40,50]];
foreach ($parcels as $kind => $weight) {
    switch ($kind) {
        case 'standard':
            $fees[] = (int)$rates[2][0];
            break;
        case 'express':
            $fees[] = (int)$rates[3][1];
            break;
        case 'freight':
            $fees[] = (int)$rates[3][0];
            break;
    }
}
""".splitlines()


def test_a_php_table_lookup_is_evaluated_next_to_the_claim():
    got = resolve_lookups(PHP, 9)
    text = "\n".join(got)
    assert "$rates[3][1] = 40" in text
    assert "$rates[3][0] = 30" in text, "the lookup the claim says is reused"
    assert "$rates[2][0] = 20" in text
    assert "defined at line 2" in text


def test_python_dict_and_js_object_literals():
    py = ["SIZES = {'small': 2, 'large': 4}", "x = SIZES['large']"]
    assert "SIZES['large'] = 4" in resolve_lookups(py, 2)[0]
    js = ["const sizes = { small: 2, large: 4 };", "let x = sizes['small'];"]
    # bare (unquoted) JS keys are not evaluated: nothing is better than a guess
    assert resolve_lookups(js, 2) == []


def test_a_table_that_is_reassigned_before_the_use_is_not_evaluated():
    lines = ["$t = [1, 2, 3];", "$t = compute();", "$x = $t[1];"]
    assert resolve_lookups(lines, 3) == []
    lines = ["$t = [1, 2, 3];", "$t[1] = 99;", "$x = $t[1];"]
    assert resolve_lookups(lines, 3) == []


def test_a_non_literal_table_or_missing_key_yields_nothing():
    assert resolve_lookups(["$t = [foo(), 2];", "$x = $t[1];"], 2) == []
    assert resolve_lookups(["$t = [1, 2];", "$x = $t[7];"], 2) == []
    assert resolve_lookups(["$x = $t[1];"], 1) == []
    assert resolve_lookups([], 1) == []


def test_member_access_is_not_mistaken_for_a_local_table():
    lines = ["$t = [1, 2];", "$x = $obj->t[1];"]
    assert resolve_lookups(lines, 2) == []


def test_evidence_renders_the_computed_lines():
    ev = FindingEvidence(window="1: x", lookups=["$m[1] = 4  (line 2; $m defined at line 1)"])
    out = render_evidence(ev)
    assert "$m[1] = 4" in out and "evaluated" in out
    assert "Lookups" not in render_evidence(FindingEvidence(window="1: x")), (
        "an empty block would teach the model to ignore the heading"
    )
