"""How-do-I and where-is questions are answered from a guide, with links.

The floating agent is the first place somebody asks "where do I put my GitLab
token". The answer is the one thing in this chat the model writes in full, so
what it is allowed to say and where it is allowed to send people are pinned:

THE GUIDE IS IN THE PROMPT. An answer about scopes written from a model's
general memory of GitLab is plausible and wrong in the details that matter
(`read_api` is not enough). The planner is handed `src.automation.guide`.

THE GUIDE IS THE ALLOW-LIST FOR LINKS. A link the guide does not name is turned
back into its words before the note is stored, so a made-up path never becomes
a button that lands on a 404 — and nothing outside the app is linkable at all.

IT IS A QUESTION. It answers immediately, with no plan card and no scope.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest


def _stub_client(monkeypatch, reply: dict, seen: dict):
    def _generate(**kwargs):
        seen.update(kwargs)
        return types.SimpleNamespace(text=json.dumps(reply))

    import src.llm.client as llm_client
    monkeypatch.setattr(
        llm_client, "build_llm_client",
        lambda *a, **kw: types.SimpleNamespace(generate=_generate))


def test_help_is_a_read_in_the_catalogue():
    from src.automation.chat import CATALOGUE

    assert CATALOGUE["help"]["reads"] is True
    assert CATALOGUE["help"]["arguments"] == {}


def test_the_planner_is_handed_the_guide(monkeypatch):
    from src.automation.chat import interpret
    from src.automation.guide import GUIDE

    seen: dict = {}
    _stub_client(monkeypatch, {"language": "en", "note": "x", "steps": []}, seen)
    interpret("where do I add a GitLab token?", workspace_id="ws", user_id="u")

    system = seen["system_instruction"]
    assert GUIDE in system, "the guide never reaches the model"
    assert "- help:" in seen["prompt"], "the verb is not offered"


@pytest.mark.parametrize("route", [
    "/connections", "/settings/llm", "/admin/review-policies", "/admin/agents",
    "/automation",
])
def test_the_guide_names_the_pages_people_ask_about(route):
    from src.automation.guide import GUIDE_ROUTES

    assert route in GUIDE_ROUTES


def test_the_guide_says_what_a_gitlab_token_needs():
    """The detail a general-purpose answer gets wrong."""
    from src.automation.guide import GUIDE

    assert "`api`" in GUIDE and "`read_api` is not enough" in GUIDE


def test_an_unknown_link_keeps_its_words_and_loses_its_target():
    from src.automation.guide import keep_known_links

    text = ("Open [connections](/connections), then [a page](/settings/github), "
            "[evil](https://evil.example/x) and [js](javascript:alert)")
    assert keep_known_links(text) == (
        "Open [connections](/connections), then a page, evil and js")


def test_a_page_under_a_named_route_is_kept():
    """`/admin/review-policies/default` is real; the guide names its parent."""
    from src.automation.guide import keep_known_links

    text = "[policy](/admin/review-policies/default)"
    assert keep_known_links(text) == text
    # A sibling that merely shares the prefix is not a child.
    assert keep_known_links("[x](/admin/review-policiesX)") == "x"
    # Protocol-relative is another site.
    assert keep_known_links("[x](//evil.example)") == "x"


def test_the_stored_note_has_only_known_links(monkeypatch):
    from src.automation.chat import interpret

    seen: dict = {}
    _stub_client(monkeypatch, {
        "language": "en",
        "note": "Paste it on [Git connections](/connections) — "
                "not [here](/settings/tokens).",
        "steps": [{"action": "help", "arguments": {}}],
    }, seen)
    plan = interpret("where do tokens go?", workspace_id="ws", user_id="u")

    assert plan.reads_only, "a how-to question waits for a press"
    assert "[Git connections](/connections)" in plan.note
    assert "/settings/tokens" not in plan.note


def test_the_answer_lists_the_pages_it_pointed_at():
    from src.automation.actions import Actor
    from src.automation.chat import Plan, Step, execute

    plan = Plan(
        note="See [LLM keys](/settings/llm) and [LLM keys](/settings/llm), "
             "then [Repositories](/repositories).",
        steps=[Step(action="help")],
    )
    # No session: an answer written from the guide touches no database.
    outcome = asyncio.run(execute(
        plan, Actor(user_id="u", email="a@b.c", workspace_id="ws",
                    label="chat"),
        session=None))
    assert outcome["steps"][0]["result"] == {"links": [
        {"label": "LLM keys", "href": "/settings/llm"},
        {"label": "Repositories", "href": "/repositories"},
    ]}
    assert outcome["queued"] == [] and outcome["run_id"] is None


def test_help_has_no_scope_to_resolve():
    from src.automation.chat import Plan, Step, resolve_scope

    plan = resolve_scope(Plan(steps=[Step(action="help")]), workspace_id="ws")
    assert plan.steps[0].resolved_repos == []
    assert plan.blocked is None
