"""Acceptance criteria out of a task (or a pull-request description).

Three places a team writes them, tried in this order:

1. a configured custom field (`task_acceptance_field`, `customfield_NNNNN`),
   whose value may be ADF, a string, a list of strings or of option objects;
2. the description: the bullets (or sentences) under a heading that names
   criteria ("Acceptance criteria", "Критерії приймання", "Definition of
   done", …), plus every checkbox anywhere;
3. nothing: the description itself is then the specification.

Each criterion gets a stable id AC1..ACn (at most `MAX_CRITERIA`, each at
most `MAX_CRITERION_CHARS`) so a finding and the requirements check can cite
it as `[PROJ-6066 AC2]`.

The markdown parsing used to live in agents/business_logic.py, where it read
the pull request's own description; it is the same parser, moved here so the
task's description is read by exactly the same rules. business_logic.py
re-imports every name it used to define.
"""

from __future__ import annotations

import re
from typing import Any

from src.review.task_context.adf import adf_to_text, clean_text
from src.review.task_context.models import CriteriaSource, Criterion

#: At most this many acceptance criteria are listed separately.
MAX_CRITERIA = 30
#: One criterion longer than this is a paragraph; its head carries the point.
MAX_CRITERION_CHARS = 500

HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
#: A markdown heading needs the space after its hashes — "#88 is fixed" is a
#: sentence that starts with a pull-request reference, not a heading.
_HEADING_LINE = re.compile(r"^\s{0,3}#{1,6}(?:\s+(.*))?$")


class _Heading:
    """`HEADING.match(line)`. The line is stripped on the right first: a lazy
    group followed by `\\s*$` is quadratic on a long run of spaces, and Jira
    text is written by anyone who can edit a task."""

    @staticmethod
    def match(line: str) -> re.Match[str] | None:
        return _HEADING_LINE.match(line.rstrip())


HEADING = _Heading()
CHECKBOX = re.compile(r"^\s*[-*+]\s+\[[ xX]\]\s+(.*\S)\s*$")
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
#: Headings under which the bullets are the acceptance criteria.
CRITERIA_HEADING = re.compile(
    r"acceptance|criteria|definition of done|\bdod\b|requirements?|"
    r"expected (?:behaviou?r|result)|критері|вимог|очікуван|критери|требовани|"
    r"умови приймання|умови прийому",
    re.IGNORECASE,
)


def acceptance_criteria(text: str) -> list[str]:
    """Checkbox items anywhere, plus the bullets under a criteria heading."""
    out: list[str] = []
    under_criteria = False
    for line in (raw.rstrip() for raw in text.splitlines()):   # rstrip: see _Heading
        heading = HEADING.match(line)
        if heading:
            under_criteria = bool(CRITERIA_HEADING.search(heading.group(1) or ""))
            continue
        box = CHECKBOX.match(line)
        if box:
            out.append(box.group(1))
            continue
        if under_criteria and line.strip():
            # A bullet under the heading is a criterion; so is a line of
            # prose there — some teams write them as sentences.
            bullet = BULLET.match(line)
            out.append(bullet.group(1) if bullet else line.strip())
    seen: list[str] = []
    for item in out:
        if item not in seen:
            seen.append(item)
    return seen[:MAX_CRITERIA]


def field_value_text(value: Any) -> str:
    """The text of a custom-field value of any shape Jira returns: ADF, a
    string, a list of either, an option object (`{"value": …}`)."""
    if value is None:
        return ""
    if isinstance(value, dict):
        if value.get("type") == "doc":
            return adf_to_text(value)
        for key in ("value", "name", "text"):
            if isinstance(value.get(key), str):
                return clean_text(value[key]).strip()
        return ""
    if isinstance(value, list):
        return "\n".join(t for t in (field_value_text(v) for v in value) if t)
    return clean_text(str(value)).strip()


def _from_field(text: str) -> list[str]:
    """One criterion per non-empty line of a field's text, list markers and
    checkboxes removed."""
    out: list[str] = []
    for line in (raw.rstrip() for raw in text.splitlines()):
        if not line.strip() or HEADING.match(line):
            continue
        box = CHECKBOX.match(line)
        bullet = BULLET.match(line)
        item = (box or bullet).group(1) if (box or bullet) else line.strip()
        if item and item not in out:
            out.append(item)
    return out[:MAX_CRITERIA]


def number(items: list[str]) -> list[Criterion]:
    """`items` as AC1..ACn, each cut to `MAX_CRITERION_CHARS`."""
    out = []
    for n, item in enumerate(items[:MAX_CRITERIA], 1):
        text = " ".join(item.split())
        if len(text) > MAX_CRITERION_CHARS:
            text = text[: MAX_CRITERION_CHARS - 1].rstrip() + "…"
        out.append(Criterion(id=f"AC{n}", text=text))
    return out


def extract_criteria(
    description: str, field_value: Any = None,
) -> tuple[list[Criterion], CriteriaSource]:
    """(criteria, where they came from) for a task."""
    from_field = _from_field(field_value_text(field_value))
    if from_field:
        return number(from_field), "field"
    from_description = acceptance_criteria(HTML_COMMENT.sub("", description or ""))
    if from_description:
        return number(from_description), "description"
    return [], "none"
