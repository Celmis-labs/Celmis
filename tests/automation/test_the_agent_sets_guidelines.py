"""The chat assistant sets an agent's team guidelines — and never replaces a prompt.

"Додай до security агента для репо X: перевіряй …" is a guideline: text
ADDED to the agent's prompt. The verb is `update_review_setting` with the key
`agent_prompt_guidelines`, whose value is {agent: text}:

  * the agents and the 2000-character cap are the API's own, refused by name
    on the plan card;
  * a change is merged per agent into what the scope holds ("" removes one),
    so adding security's guidelines does not wipe defect's;
  * the editor role may do it at both scopes, as on the page — the workspace
    review defaults' owner/admin gate does not apply to prompts;
  * replacing a prompt is not a key the chat has.
"""

from __future__ import annotations

import types

import pytest

from src.review.prompt_guidelines import GUIDELINES_MAX_CHARS
from tests.automation import test_the_agent_changes_review_config as _base

WS = _base.WS
_actor = _base._actor
_Session = _base._Session
# The fixtures of the review-config tests, shared rather than copied.
repos = _base.repos
person = _base.person
routes = _base.routes


def _resolved(step, caller=None):
    from src.automation.chat import Plan, resolve_scope

    return resolve_scope(Plan(steps=[step]), workspace_id=WS, caller=caller).steps[0]


def test_the_key_is_whitelisted_and_replacement_is_not():
    from src.automation.actions import REVIEW_SETTING_KEYS, review_setting_keys

    assert "agent_prompt_guidelines" in REVIEW_SETTING_KEYS
    assert "agent_prompt_overrides" not in REVIEW_SETTING_KEYS
    for scope in ("workspace", "repo"):
        assert "agent_prompt_guidelines" in review_setting_keys(scope)


@pytest.mark.parametrize(("value", "says"), [
    ({"nobody": "x"}, "unknown agent 'nobody'"),
    ({"defect": "x" * (GUIDELINES_MAX_CHARS + 1)}, "at most 2000"),
    ("not json", "object of agent"),
    ({}, "object of agent"),
])
def test_a_bad_value_is_refused_by_name(value, says):
    from src.automation.actions import ActionError, review_setting_value

    with pytest.raises(ActionError, match=says):
        review_setting_value("repo", "agent_prompt_guidelines", value)


def test_a_value_is_normalised():
    from src.automation.actions import review_setting_value

    assert review_setting_value(
        "workspace", "agent_prompt_guidelines",
        '{"security": "  - flag raw SQL ", "defect": null}',
    ) == {"security": "- flag raw SQL", "defect": ""}


@pytest.mark.parametrize(("role", "scope", "blocked"), [
    ("editor", "workspace", False),
    ("editor", "repo", False),
    ("member", "workspace", True),
    ("viewer", "repo", True),
])
def test_the_editor_role_may_set_guidelines_at_both_scopes(repos, role, scope, blocked):
    from src.automation.chat import Step

    step = _resolved(Step(action="update_review_setting", arguments={
        "scope": scope, "repo_slug": "payments" if scope == "repo" else None,
        "key": "agent_prompt_guidelines", "value": {"security": "- g"}}),
        caller={"role": role})
    assert bool(step.blocked) is blocked, step.blocked


@pytest.mark.asyncio
async def test_a_repository_change_is_merged_per_agent(repos, person, routes):
    from src.automation.actions import update_review_setting

    row = types.SimpleNamespace(
        workspace_id=WS, agent_prompt_guidelines={"defect": "- keep me", "contract": "- drop me"},
        agent_guidelines_extend=[], folder_rules=[])
    out = await update_review_setting(
        _actor(), _Session(row), scope="repo", repo_slug="payments",
        key="agent_prompt_guidelines", value={"security": "- new", "contract": ""})
    slug, payload, _ws = routes["policy"]
    assert slug == "payments"
    assert payload.agent_prompt_guidelines == {"defect": "- keep me", "security": "- new"}
    assert out["links"][0]["href"] == "/review-settings?repo=payments&section=prompts"


@pytest.mark.asyncio
async def test_a_workspace_change_writes_the_workspace_guidelines(
        monkeypatch, repos, person, routes):
    import src.api.routers.agents as agents_router
    from src.automation.actions import update_review_setting

    written: dict = {}
    monkeypatch.setattr(agents_router, "_save_guidelines",
                        lambda agent, text, updated_by, workspace_id: written.update(
                            {agent: (text, workspace_id)}))
    monkeypatch.setattr(agents_router, "_delete_guidelines",
                        lambda agent, workspace_id: written.update({agent: None}))
    person["role"] = "editor"
    out = await update_review_setting(
        _actor(), _Session(), scope="workspace",
        key="agent_prompt_guidelines", value={"defect": "- g", "verifier": ""})
    assert written == {"defect": ("- g", WS), "verifier": None}
    assert "defaults" not in routes      # not the review-defaults route
    assert out["scope"] == "workspace" and out["count"] == 2


@pytest.mark.asyncio
async def test_a_viewer_sets_nothing(repos, person, routes):
    from src.automation.actions import ActionError, update_review_setting

    person["role"] = "viewer"
    with pytest.raises(ActionError, match="requires editor"):
        await update_review_setting(_actor(), _Session(), scope="workspace",
                                    key="agent_prompt_guidelines", value={"defect": "- g"})
