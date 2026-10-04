"""The agent's product knowledge covers the workspace review defaults."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("question", [
    "how do I turn off the security agent for all repositories?",
    "where are the workspace default review settings?",
    "як задати типові налаштування рев'ю для всіх репозиторіїв?",
    "Как выключить агента для всех репозиториев сразу?",
])
def test_a_workspace_wide_question_reads_the_defaults_section(question):
    from src.automation.knowledge import select_sections

    ids = [s.id for s in select_sections(question)]
    assert "review-defaults" in ids[:3], (question, ids)


def test_the_section_states_the_precedence_the_code_applies():
    from src.automation.knowledge import BY_ID
    from src.review.review_defaults import INHERITABLE_FIELDS

    text = BY_ID["review-defaults"].render()
    assert "repository's own policy → the\nworkspace review defaults → the install default" in text
    assert "/admin/review-defaults" in text
    # The fields it names are the ones a workspace can default.
    assert {"disabled_agents", "summary_enabled", "ignore_globs",
            "target_branches"} <= set(INHERITABLE_FIELDS)
