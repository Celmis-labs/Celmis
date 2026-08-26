"""The posted comment leads with the evidence.

The verifier-off benchmark run posted 77 comments and not one carried a
Why: line — the image predated e01531d, which added the line after the
body. The judge reads the whole comment and matches on the issue it names;
a human decides in the first line whether to read the second. Both now get
the derivation before the conclusion: Why, then the title, then the body,
and the agent's own confidence in the footer where telemetry belongs.
"""

from __future__ import annotations

from src.review.models import Finding, FindingSeverity
from src.review.providers.base import _format_finding_body


def _finding(**over: object) -> Finding:
    base: dict[str, object] = {
        "file_path": "src/foo.py", "line": 2,
        "severity": FindingSeverity.ERROR,
        "title": "x read before assignment",
        "body": "`x` is assigned on line 3 and read on line 2.",
        "reasoning": "line 2 reads x; line 3 is the first assignment",
        "agent": "architect", "rule_id": "arch.x", "confidence": 0.8,
    }
    base.update(over)
    return Finding(**base)  # type: ignore[arg-type]


def test_why_is_the_first_line_and_the_title_the_next():
    lines = _format_finding_body(_finding()).splitlines()
    assert lines[0] == "*Why:* line 2 reads x; line 3 is the first assignment"
    assert lines[1] == ""
    assert lines[2].endswith("x read before assignment**")


def test_the_order_is_why_title_body_suggestion_footer():
    text = _format_finding_body(_finding(suggestion="x = 1"))
    positions = [
        text.index("*Why:*"),
        text.index("x read before assignment**"),
        text.index("is assigned on line 3"),
        text.index("```suggestion"),
        text.index("<sub>"),
    ]
    assert positions == sorted(positions)


def test_confidence_is_in_the_footer_and_nowhere_above_it():
    text = _format_finding_body(_finding(confidence=0.8))
    head, _, footer = text.rpartition("<sub>")
    assert "confidence: 0.80" in footer
    assert "confidence" not in head
    assert "agent: `architect`" in footer and "rule: `arch.x`" in footer


def test_a_finding_with_no_reasoning_still_leads_with_its_title():
    lines = _format_finding_body(_finding(reasoning="")).splitlines()
    assert "Why:" not in "\n".join(lines)
    assert lines[0].endswith("x read before assignment**")


def test_the_marker_stays_last():
    text = _format_finding_body(_finding(), "<!-- celmis:review -->")
    assert text.rstrip().endswith("<!-- celmis:review -->")
    assert text.index("<sub>") < text.index("<!-- celmis:review -->")


def test_a_proven_finding_reports_its_certainty_too():
    text = _format_finding_body(_finding(confidence=1.0, evidence_kind="proven", agent="cve"))
    assert "confidence: 1.00" in text
