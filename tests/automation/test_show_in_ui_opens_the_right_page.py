"""The agent opens the page and the field instead of describing them.

"I need to add custom prompts for repo shop-api" used to be answered with a
paragraph. It now becomes a `show_in_ui` step whose result is a client action:
the route with the right repository selected, and the controls to spotlight.
What can go wrong is specific, so each is pinned:

  * the repository the person named is resolved against the ones THEY may
    read, and a vague name is asked about rather than guessed;
  * the model can only name destinations and focus keys that exist — a made-up
    one is dropped, never turned into a selector;
  * a person whose role cannot change the page gets the link and the reason,
    not a spotlight on a disabled field;
  * the planner is actually handed the routing rules and the destination menu.
"""

from __future__ import annotations

import asyncio
import json
import types
from urllib.parse import parse_qs, urlparse

import pytest

from src.automation import ui_catalogue as cat

REPOS = [
    {"repo": "acme-shop-api", "full_name": "acme/shop-api"},
    {"repo": "acme-shop-web", "full_name": "acme/shop-web"},
    {"repo": "acme-billing", "full_name": "acme/billing"},
    {"repo": "acme-docs", "full_name": "acme/docs"},
    {"repo": "tools-cli", "full_name": "tools/cli"},
]
ADMIN = {"role": "admin"}
EDITOR = {"role": "editor"}
VIEWER = {"role": "viewer"}


def act(**kw):
    kw.setdefault("repos", REPOS)
    kw.setdefault("caller", EDITOR)
    return cat.build_ui_action(**kw)


# ── repository resolution ────────────────────────────────────────────────

@pytest.mark.parametrize("said,slug", [
    ("acme-shop-api", "acme-shop-api"),          # exact slug
    ("acme/shop-api", "acme-shop-api"),          # exact full name
    ("Acme/Shop-API", "acme-shop-api"),          # case
    ("shop-api", "acme-shop-api"),               # the bare name
    ("billing", "acme-billing"),                 # unique substring
    ("acme-biling", "acme-billing"),             # a typo
    ("cli", "tools-cli"),
])
def test_a_named_repository_resolves(said, slug):
    m = cat.match_repo(said, REPOS)
    assert (m.status, m.slug) == ("found", slug)


def test_two_equally_good_repositories_are_asked_about_not_guessed():
    m = cat.match_repo("shop", REPOS)
    assert m.status == "ambiguous"
    assert set(m.candidates) == {"acme-shop-api", "acme-shop-web"}


def test_a_name_that_matches_nothing_is_not_found():
    assert cat.match_repo("zzzz-unrelated", REPOS).status == "none"
    assert cat.match_repo("", REPOS).status == "none"
    assert cat.match_repo("shop", []).status == "none"


def test_a_repository_the_person_may_not_read_does_not_exist_for_them():
    """`list_repos` already drops what the asker may not read, so a repo that
    is absent from the list is exactly as unknown as one never registered."""
    visible = [r for r in REPOS if r["repo"] != "acme-billing"]
    out = act(destination="review_settings.prompts", repo="acme-billing", repos=visible)
    assert out["ok"] is False and out["error"] == "repo_not_found"


def test_ambiguity_comes_back_with_the_candidates():
    out = act(destination="review_settings.prompts", repo="shop")
    assert out["error"] == "repo_ambiguous"
    assert "acme-shop-api" in out["candidates"]


# ── the action ───────────────────────────────────────────────────────────

def test_custom_prompts_for_a_repo_open_the_prompts_section_on_that_repo():
    out = act(destination="review_settings.prompts", repo="shop-api", focus=[
        {"field": "base_instruction", "text": "Rules every agent follows."},
        {"field": "prompt_guidelines:security", "text": "Extra rules for security."},
    ], workspace_id="ws-1")
    assert out["ok"] and out["mode"] == "tour"
    url = urlparse(out["route"])
    assert url.path == "/review-settings"
    q = parse_qs(url.query)
    assert q["repo"] == ["acme-shop-api"] and q["section"] == ["prompts"]
    assert q["agent"] == ["security"]
    assert [s["target"] for s in out["steps"]] == [
        "setting:base_instruction", "prompt-guidelines:security"]
    assert out["steps"][0]["text"] == "Rules every agent follows."
    assert all(s["route"] == out["route"] for s in out["steps"])
    assert out["repo"] == "acme-shop-api" and out["workspace_id"] == "ws-1"


def test_without_a_repository_the_workspace_defaults_are_opened():
    out = act(destination="review_settings.filters",
              focus=["ignore_globs"], caller=ADMIN)
    assert out["route"] == "/review-settings?section=filters"
    assert out["repo"] == ""


def test_general_section_is_the_default_and_is_left_out_of_the_query():
    out = act(destination="general", repo="acme/docs", focus=["review_cadence"])
    assert out["route"] == "/review-settings?repo=acme-docs"


def test_a_slug_with_a_slash_is_encoded_into_the_query():
    repos = [{"repo": "acme/shop", "full_name": "acme/shop"}]
    out = act(destination="review_settings.filters", repo="acme/shop", repos=repos)
    assert "repo=acme%2Fshop" in out["route"]


def test_a_missing_focus_falls_back_to_the_section_itself():
    out = act(destination="review_settings.summary", repo="docs")
    assert [s["target"] for s in out["steps"]] == ["settings-section:summary"]
    assert out["steps"][0]["text"]


def test_a_missing_popover_text_uses_the_catalogue_description():
    out = act(destination="review_settings.filters", focus=["comment_min_severity"])
    assert "severity" in out["steps"][0]["text"].lower()


def test_made_up_destinations_and_focus_keys_are_dropped():
    assert act(destination="/admin/secrets")["error"] == "unknown_destination"
    assert act(destination=None)["error"] == "unknown_destination"
    out = act(destination="review_settings.filters", focus=[
        "ignore_globs", "nonsense", {"field": 'x"] body'}, 7, None, "prompt_guidelines"])
    assert [s["target"] for s in out["steps"]] == ["setting:ignore_globs"]


def test_a_focus_key_of_another_destination_is_not_accepted():
    out = act(destination="review_settings.filters", focus=["verifier_enabled"])
    assert [s["target"] for s in out["steps"]] == ["settings-section:filters"]


def test_a_per_agent_key_needs_a_real_agent():
    out = act(destination="review_settings.prompts",
              focus=["prompt_guidelines:hacker", "prompt_guidelines:contract"])
    assert [s["target"] for s in out["steps"]] == ["prompt-guidelines:contract"]


def test_a_bare_per_agent_key_points_at_the_first_card():
    out = act(destination="review_settings.prompts", focus=["prompt_guidelines"])
    assert out["steps"][0]["target"] == "prompt-guidelines:defect"


def test_the_replace_prompt_option_is_marked_advanced_in_its_text():
    spec = cat.FIELDS_BY_KEY["prompt_replace"]
    assert spec.advanced and "replace" in spec.what.lower()


def test_a_folded_control_carries_the_opener_that_unfolds_it():
    out = act(destination="review_settings.categories",
              focus=["agent_reasoning:security"], repo="docs")
    step = out["steps"][0]
    assert step["target"] == "review-agent-reasoning:security"
    assert step["opener"] == "review-agent-toggle:security"


def test_the_number_of_steps_is_capped():
    keys = [f"prompt_guidelines:{a}" for a in cat.PROMPT_AGENTS] + ["base_instruction"]
    out = act(destination="review_settings.prompts", focus=keys)
    assert len(out["steps"]) == cat.MAX_FOCUS


def test_popover_text_is_cleaned_and_bounded():
    out = act(destination="review_settings.prompts", focus=[
        {"field": "base_instruction", "text": "**Write** `rules`\n\n" + "x" * 500}])
    text = out["steps"][0]["text"]
    assert "*" not in text and "`" not in text and "\n" not in text
    assert len(text) <= cat.MAX_STEP_TEXT


def test_aliases_in_the_words_people_use_resolve():
    out = act(destination="review_settings.filters", focus=["severity", "ignore_files"])
    assert [s["target"] for s in out["steps"]] == [
        "setting:comment_min_severity", "setting:ignore_globs"]


def test_a_bare_section_name_is_a_destination():
    assert cat.resolve_destination("prompts").id == "review_settings.prompts"
    assert cat.resolve_destination("review-settings.prompts").section == "prompts"


def test_the_advanced_section_needs_a_repository():
    out = act(destination="review_settings.advanced")
    assert out["error"] == "repo_required"
    ok = act(destination="review_settings.advanced", repo="docs", focus=["mcp_sources"])
    assert ok["ok"] and ok["steps"][0]["target"] == "advanced-mcp"


def test_repository_only_controls_are_not_offered_at_workspace_level():
    out = act(destination="review_settings.prompts", focus=["repo_instructions"])
    assert [s["target"] for s in out["steps"]] == ["settings-section:prompts"]
    out = act(destination="review_settings.prompts", repo="docs", focus=["repo_instructions"])
    assert out["steps"][0]["target"] == "prompt-repo-instructions"


def test_a_repo_given_for_a_page_that_is_not_per_repository_is_ignored():
    out = act(destination="llm", repo="shop-api", focus=["llm_provider_keys"], caller=ADMIN)
    assert out["ok"] and out["route"] == "/settings/llm" and out["repo"] == ""


@pytest.mark.parametrize("dest,focus,route,target", [
    ("connections", "connection_provider:github", "/connections", "connection-provider:github"),
    ("memories", "memories_add", "/memories", "memories-add"),
    ("alerts", "alerts_webhook", "/alerts", "alerts-webhook"),
    ("notifications", "notifications_channel", "/admin/notifications", "notifications-channel"),
    ("llm", "llm_base_reasoning", "/settings/llm", "llm-base-reasoning"),
    ("review_rules", "review_rules_list", "/admin/review-rules", "page-review-rules"),
])
def test_the_other_pages_open_with_their_anchor(dest, focus, route, target):
    out = act(destination=dest, focus=[focus], caller=ADMIN)
    assert out["route"] == route
    assert out["steps"][0]["target"] == target


# ── roles ────────────────────────────────────────────────────────────────

def test_an_editor_may_be_shown_the_repo_prompts():
    assert act(destination="review_settings.prompts", repo="docs")["mode"] == "tour"


def test_a_viewer_gets_the_link_and_the_roles_that_can():
    out = act(destination="review_settings.prompts", repo="docs", caller=VIEWER)
    assert out["ok"] and out["mode"] == "link"
    assert "editor" in out["needs"] and out["role"] == "viewer"
    assert out["route"].startswith("/review-settings?")


def test_workspace_defaults_need_an_admin_but_the_prompts_need_only_an_editor():
    assert act(destination="review_settings.filters", caller=EDITOR)["mode"] == "link"
    assert act(destination="review_settings.filters", caller=ADMIN)["mode"] == "tour"
    assert act(destination="review_settings.prompts", caller=EDITOR)["mode"] == "tour"


def test_a_global_admin_is_never_downgraded():
    caller = {"role": "viewer", "is_admin": True}
    assert act(destination="llm", caller=caller)["mode"] == "tour"


def test_an_unknown_caller_is_not_refused_in_advance():
    assert act(destination="llm", caller=None)["mode"] == "tour"


def test_the_llm_page_is_for_admins():
    assert act(destination="llm", caller=EDITOR)["mode"] == "link"


# ── the verb in the planner ──────────────────────────────────────────────

def test_show_in_ui_is_a_read_in_the_catalogue():
    from src.automation.chat import CATALOGUE

    spec = CATALOGUE["show_in_ui"]
    assert spec["reads"] is True
    assert set(spec["arguments"]) == {"destination", "repo", "focus"}


def _stub(monkeypatch, reply, seen):
    def generate(**kw):
        seen.update(kw)
        return types.SimpleNamespace(text=json.dumps(reply))

    import src.llm.client as llm_client
    monkeypatch.setattr(llm_client, "build_llm_client",
                        lambda *a, **k: types.SimpleNamespace(generate=generate))


def test_the_planner_is_handed_the_routing_rules_and_the_destination_menu(monkeypatch):
    from src.automation.chat import interpret

    seen: dict = {}
    _stub(monkeypatch, {"language": "en", "note": "x", "steps": []}, seen)
    interpret("I need to add custom prompts for repo shop-api",
              workspace_id="ws", user_id="u")
    system = seen["system_instruction"]
    assert "SHOW them instead of describing" in system
    for dest in cat.DESTINATIONS:
        assert dest in system
    assert "prompt_guidelines:<agent>" in system
    assert "- show_in_ui:" in seen["prompt"]


def test_the_routing_prompt_prefers_the_tool_for_setup_questions():
    text = cat.routing_prompt()
    for must in ("configure, change, add or find", "base_instruction",
                 "ignore_globs", "comment_min_severity", "verifier_enabled",
                 "ask which", "ALSO asks what"):
        assert must in text, must
    assert "custom prompts for repo shop-api" in text
    assert "acme" in text  # examples use neutral names


def test_the_plan_parses_a_show_in_ui_step_and_is_reads_only():
    from src.automation.chat import _parse

    plan = _parse(json.dumps({"language": "uk", "note": "ok", "steps": [
        {"action": "show_in_ui", "arguments": {
            "destination": "review_settings.prompts", "repo": "shop-api",
            "focus": [{"field": "base_instruction", "text": "t"}]}}]}))
    assert plan.reads_only and plan.steps[0].action == "show_in_ui"


# ── execute() ───────────────────────────────────────────────────────────

def _execute(monkeypatch, steps, caller=EDITOR, repos=REPOS):
    import src.automation.actions as actions
    from src.automation.chat import Plan, Step, execute

    async def fake_repos(actor):
        return {"repos": repos}

    async def fake_member(actor):
        return None

    monkeypatch.setattr(actions, "list_repos", fake_repos)
    monkeypatch.setattr(actions, "_require_workspace_member", fake_member)
    plan = Plan(note="n", steps=[Step(action="show_in_ui", arguments=a) for a in steps])
    actor = actions.Actor(user_id="u", email="u@example.com", workspace_id="ws-1")
    return asyncio.run(execute(plan, actor, None, caller=caller))


def test_execute_resolves_the_repository_against_the_readable_ones(monkeypatch):
    out = _execute(monkeypatch, [{
        "destination": "review_settings.prompts", "repo": "shop-api",
        "focus": ["base_instruction"]}])
    res = out["steps"][0]["result"]
    assert res["ok"] and res["repo"] == "acme-shop-api"
    assert res["workspace_id"] == "ws-1"


def test_execute_reports_an_ambiguous_repository_instead_of_opening_one(monkeypatch):
    out = _execute(monkeypatch, [{"destination": "review_settings.prompts", "repo": "shop"}])
    assert out["steps"][0]["result"]["error"] == "repo_ambiguous"


def test_execute_passes_the_role_through(monkeypatch):
    out = _execute(monkeypatch, [{"destination": "llm"}], caller=VIEWER)
    assert out["steps"][0]["result"]["mode"] == "link"


def test_the_worker_asks_for_words_only_when_the_page_cannot_just_open():
    from src.sync.handlers import _ui_needs_words

    read = lambda **r: {"action": "show_in_ui", "result": r}  # noqa: E731
    assert _ui_needs_words(read(ok=False, error="repo_ambiguous"))
    assert _ui_needs_words(read(ok=True, mode="link"))
    assert not _ui_needs_words(read(ok=True, mode="tour"))
    assert not _ui_needs_words({"action": "help", "result": {}})
