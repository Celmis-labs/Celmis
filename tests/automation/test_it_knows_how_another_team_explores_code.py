"""Two questions the agent was asked and could not answer from its knowledge.

"Як дати сусідній команді переглядати код?" — the answer spans five pages
(workspace members, teams, code access, projects, MCP) and two independent
controls that people routinely confuse. And "add review rules to this repo",
which the agent can now DO, and has to be able to explain.

Retrieval is pinned for the phrasings people use, in Ukrainian, Russian and
English; the facts the answer rests on are pinned to the code that makes
them true, so the knowledge cannot quietly outlive the product.
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("question", [
    "як дати сусідній команді переглядати код",
    "Як дати іншій команді доступ лише на перегляд коду нашого репо?",
    "Как дать соседней команде просматривать код?",
    "Как дать другой команде исследовать наш код?",
    "How do I let another team explore our code?",
    "how can a neighbouring team browse our repositories read-only",
])
def test_the_neighbour_team_question_reads_its_section(question):
    from src.automation.knowledge import select_sections

    ids = [s.id for s in select_sections(question)]
    assert ids and ids[0] == "neighbour-team-code", (question, ids)
    assert "teams-code-access" in ids, "the reference section should come too"


@pytest.mark.parametrize("question", [
    "add review rules to this repo",
    "add review rules for billing-api: no raw SQL in handlers",
    "додай до перевірок цього репо правила: не використовувати print",
    "згенеруй правила для репо billing",
    "увімкни approve для репо billing-api",
    "добавь правила проверки для этого репозитория",
])
def test_the_review_rules_question_reads_the_agent_section(question):
    from src.automation.knowledge import select_sections

    ids = [s.id for s in select_sections(question)]
    assert "agent-review-config" in ids[:2], (question, ids)


def test_the_answer_names_both_controls_and_the_trap_between_them():
    from src.automation.knowledge import BY_ID

    body = BY_ID["neighbour-team-code"].body
    for page in ("(/admin/workspaces)", "(/admin/teams)", "(/admin/access)",
                 "(/projects)", "(/settings/mcp)"):
        assert page in body, page
    # The first rule on a repository hides it from every team without one.
    assert "Keep your own access" in body
    # Code search and docs are not filtered by research rules.
    assert "neither is filtered by research rules" in body


def test_the_visibility_labels_are_the_ones_on_screen():
    import json
    from pathlib import Path

    from src.automation.knowledge import BY_ID

    en = json.loads((Path(__file__).resolve().parents[2] / "web" / "lib" / "i18n"
                     / "messages" / "en.json").read_text(encoding="utf-8"))
    body = BY_ID["neighbour-team-code"].body
    for key in ("admin.access.visMetadata", "admin.access.visCode",
                "admin.access.denyLabel", "admin.access.presetCredentials"):
        assert f'"{en[key]}"' in body, key


def test_the_mcp_scopes_are_the_ones_a_token_is_issued_with():
    from src.api.routers.mcp_access import _TOKEN_SCOPES
    from src.automation.knowledge import BY_ID

    body = BY_ID["neighbour-team-code"].body
    for scope in _TOKEN_SCOPES:
        assert f"`{scope}`" in body, scope


def test_the_deployment_switch_is_named_as_the_code_reads_it():
    from src.automation.knowledge import BY_ID
    from src.deployment import ENV_VAR

    body = BY_ID["neighbour-team-code"].body
    assert f"`{ENV_VAR}`" in body
    assert "single_tenant" in body and "multi_tenant" in body


def test_the_team_grant_levels_are_the_ones_the_api_accepts():
    from pathlib import Path

    import src.api.routers.teams as teams
    from src.automation.knowledge import BY_ID

    body = BY_ID["neighbour-team-code"].body
    src = Path(teams.__file__).read_text(encoding="utf-8")
    for perm in ("read", "review", "admin"):
        assert f"`{perm}`" in body
        assert f'"{perm}"' in src


def test_the_settings_the_agent_can_change_are_the_whitelist():
    from src.automation.actions import REVIEW_SETTING_KEYS
    from src.automation.knowledge import BY_ID

    body = BY_ID["agent-review-config"].body
    for key in REVIEW_SETTING_KEYS:
        assert f"`{key}`" in body, key


def test_the_agent_says_it_remembers_and_when_it_forgets():
    from src.automation.knowledge import BY_ID

    body = BY_ID["celmis-agent"].body
    assert "remembers the conversation" in body
    assert "New chat" in body and "signing out" in body
