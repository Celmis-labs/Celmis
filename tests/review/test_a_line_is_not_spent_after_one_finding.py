"""The instruction to look at a commented line twice reaches every agent.

WHAT THIS PINS. Across five benchmark runs and 302 findings, no agent ever
wrote two findings on the same line — zero, not "rarely". Different agents
commented the same line 62 times, so the constraint is not global; each agent
treats a line it has written about as spent.

Two audited misses sit on a line we did comment, carrying a second unrelated
defect: `topic_embed.rb:13` and `next-auth-options.ts:144`. A third candidate
was dropped once its golden was checked — `embed.html.erb:11` is the
postMessage targetOrigin claim a headless-Chrome test disproved, so a second
comment there would elaborate on an error we are already rewarded for.

`SECOND_DEFECT_PROMPT` is the instruction against that, and it lives in the
shared module because four local copies drift. Two things can silently break
it, and each has a test here:

  * a new agent that composes its own `_SYSTEM` and never imports it — the
    block would be live for three agents and absent for the fourth, which is
    exactly the state `FINDING_OUTPUT_FORMAT` was in before it was shared;
  * an edit that keeps the "read it again" half and loses the restraint half.
    Unclamped, "look for a second defect" is an instruction to produce a
    second comment, and the cheapest second comment is the first reworded. At
    this benchmark's operating point one true positive is worth eleven false
    ones, but a comment the author closes unread costs the findings next to it
    their reader — which is the whole reason the avoid-list exists.

The wording is scoped to the LINE on purpose. Reread-the-hunk would address
seven of eight audited positions instead of two, and was rejected on
measured background: two findings within twenty lines from one agent happened
5 times in those 302, roughly once a run, so a hunk-scoped instruction needs
four extra pairs before it clears noise while a line-scoped one needs one.
"""

from __future__ import annotations

import pytest

from src.review.agents import contract, defect, security
from src.review.agents.base import AVOID_LIST_PROMPT, SECOND_DEFECT_PROMPT

AGENTS = pytest.mark.parametrize(
    "module",
    [defect, contract, security],
    ids=["defect", "contract", "security"],
)


@AGENTS
def test_every_agent_is_told_to_read_the_line_twice(module):
    assert SECOND_DEFECT_PROMPT in module._SYSTEM


@AGENTS
def test_it_stands_after_the_avoid_list_and_before_the_severities(module):
    """Order is meaning here: the avoid-list says what not to write, this says
    when a second comment is nevertheless warranted, and the severities grade
    whatever survives. Read before the avoid-list it would look like a licence
    to double up on the categories that list forbids."""
    system = module._SYSTEM

    assert system.index(AVOID_LIST_PROMPT) < system.index(SECOND_DEFECT_PROMPT)
    assert system.index(SECOND_DEFECT_PROMPT) < system.index(module._SEVERITY)


def test_the_restraint_half_cannot_be_edited_away():
    """The failure mode with a cost: an instruction to look again, with
    nothing telling the model when to stay silent."""
    body = SECOND_DEFECT_PROMPT.lower()

    assert "if it does not" in body
    assert "worse than one comment" in body


def test_it_asks_for_a_different_defect_not_a_second_wording():
    """"A SECOND defect of a different kind" is the load-bearing phrase — the
    thing being excluded is a restatement, not a second comment as such."""
    assert "second defect of a different kind" in SECOND_DEFECT_PROMPT.lower()
    assert "two different fixes" in SECOND_DEFECT_PROMPT.lower()


def test_the_scope_is_one_line():
    """Guards the decision, not the prose: widening to the hunk is a change
    that must be argued from a measurement, not made by editing a word. This
    reads the constant's VALUE, so a comment discussing hunks cannot trip it."""
    assert "that same line" in SECOND_DEFECT_PROMPT
    assert "hunk" not in SECOND_DEFECT_PROMPT.lower()


def test_it_names_no_rule_id():
    """Every rule id in a prompt so far has been a surface feature the model
    renamed out from under it."""
    assert "quality." not in SECOND_DEFECT_PROMPT
    assert "security." not in SECOND_DEFECT_PROMPT
