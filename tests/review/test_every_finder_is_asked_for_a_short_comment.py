"""Every prompt that writes a comment asks for the problem and the fix, briefly.

THE DEFECT. The shared output contract asked for a title of "one line" and a
body that was "the full explanation", and nothing else: no limit, no ban on
retelling the code or hedging. Comments came back as paragraphs, and a team
that scores a review by how short and right it is read every one of them twice.

THE RULE. The contract (one constant, spliced into every finder's prompt)
asks for a title that states the problem itself, a body of at most two short
sentences that does not repeat the reason, a concrete fix, and no preamble,
retelling, hedging or advice that fits any code. A doubt belongs in the
confidence field.

WHAT DOES NOT BEND. The evidence rules are untouched: the reasoning sentence
is still written first and refused when missing, a file the pull request does
not change is still discarded, and the order of the fields is the same. The
wording is checked on the languages' common ground: nothing here is English
grammar a Ukrainian review would have to translate, because the language
directive tells the model the examples set the shape and not the language.
"""

from __future__ import annotations

import pytest

from src.review.agents.base import FINDING_OUTPUT_FORMAT
from src.review.agents.business_logic import BusinessLogicAgent
from src.review.agents.contract import ContractAgent
from src.review.agents.defect import DefectAgent
from src.review.agents.performance import PerformanceAgent
from src.review.agents.security import SecurityAgent

FINDERS = [DefectAgent, ContractAgent, SecurityAgent, PerformanceAgent, BusinessLogicAgent]


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize("agent", FINDERS, ids=lambda a: a.name)
def test_every_finder_asks_for_a_short_comment(agent):
    prompt = _flat(agent.system_prompt)
    assert "states the problem itself" in prompt
    assert "at most two short sentences" in prompt
    assert "Be brief" in prompt
    for banned in ("retell the code", "open with a preamble", "hedge", "advice that fits any code"):
        assert banned in prompt, banned


def test_the_doubt_goes_to_the_confidence_not_into_the_wording():
    assert 'a doubt belongs in "confidence"' in _flat(FINDING_OUTPUT_FORMAT)


def test_the_fix_is_asked_for_concretely():
    fmt = _flat(FINDING_OUTPUT_FORMAT)
    assert '"suggestion": "<optional: the concrete fix in a few words, or the snippet>"' in fmt
    assert '"suggested_code"' in fmt, "the exact replacement is still asked for"


def test_the_evidence_rules_did_not_move():
    fmt = _flat(FINDING_OUTPUT_FORMAT)
    assert fmt.index('"reasoning"') < fmt.index('"file"') < fmt.index('"title"') < fmt.index('"confidence"')
    assert "written FIRST" in fmt and "written LAST" in fmt
    assert "the reasoning sentence you cannot finish is the finding you do not have" in fmt
    assert 'A finding with no "reasoning", or on a file this PR does not change, is discarded unread.' in fmt
    assert "If nothing is found, reply exactly []." in fmt


def test_the_body_is_not_asked_to_repeat_the_reason():
    assert "without repeating the reasoning" in _flat(FINDING_OUTPUT_FORMAT)


def test_the_claude_engine_asks_the_same_of_its_findings():
    from src.review.claude_engine import _PROMPT

    prompt = _flat(_PROMPT)
    assert "states the problem itself" in prompt
    assert "at most two short sentences" in prompt
    assert "at most 3 short sentences" in prompt, "the overall assessment is short too"


def test_the_overview_and_the_walkthrough_are_short():
    from src.review.pr_summary import build_prompt

    pr = _small_pr()
    prompt = _flat(build_prompt(pr, language=None))
    assert "2 or 3 short plain sentences" in prompt
    assert "only the substance" in prompt
    assert "at most 12 words" in prompt


def test_the_answer_to_a_question_is_asked_to_lead_with_the_answer():
    from src.review.commands.chat import SYSTEM_PROMPT

    prompt = _flat(SYSTEM_PROMPT)
    assert "the answer first" in prompt
    assert "no preamble" in prompt and "no restating the question" in prompt


def _small_pr():
    from src.review.models import Hunk, PullRequest

    return PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="d", author="a",
        base_ref="main", base_sha="a", head_ref="f", head_sha="b", state="open",
        hunks=[Hunk(file_path="a.py", old_file_path="a.py", old_start=1, old_count=1,
                    new_start=1, new_count=2, content="@@ -1 +1,2 @@\n x\n+y\n")],
    )
