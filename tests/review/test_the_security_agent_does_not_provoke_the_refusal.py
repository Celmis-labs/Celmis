"""The security prompt carries its authorisation from the first word.

Measured on a benchmark fork set: the model refused this agent's request in
one call out of five — "Sorry, I cannot fulfill your request to analyze or
identify vulnerabilities in specific code snippets" — while quality, tests
and architect were never refused. The retry ladder now re-asks once with an
authorising frame, but at 20% per call the cheaper fix is not to provoke the
refusal at all. This pins that the frame is in the FIRST ask for security,
and uses the same words as the re-frame so the two cannot drift apart.
"""

from __future__ import annotations

from src.review.agents.base import LLMReviewAgent
from src.review.agents.security import SecurityAgent


def _flat(text: str) -> str:
    """Line wraps are layout, not meaning — compare the words."""
    return " ".join(text.split())


def test_the_first_ask_already_says_the_review_is_authorised():
    prompt = _flat(SecurityAgent.system_prompt)
    assert "authorised code review" in prompt
    assert "their own change" in prompt
    assert "before it merges" in prompt


def test_the_frame_and_the_reframe_make_the_same_claim():
    """One task description, two places. If the re-frame ever says something
    the base prompt does not, the model is told two different stories."""
    reframe = _flat(LLMReviewAgent._REFUSAL_REFRAME)
    for phrase in ("authorised code review", "owner of this", "legitimate task"):
        assert phrase in reframe
        assert phrase in _flat(SecurityAgent.system_prompt), (
            f"{phrase!r} is in the re-frame but not in the first ask — the two "
            "have drifted"
        )
