"""Review rules and review settings, changed from a sentence.

"Додай до перевірок цього репо правила …", "згенеруй правила для репо X",
"увімкни approve для репо X". Three verbs, and the tests are about the
things that make them safe rather than the happy path:

  * validation — a rule with no instruction, a severity that does not exist,
    a setting outside the whitelist, a setting the installed schema does not
    have (detected at run time, refused by name), a value the route's own
    schema rejects;
  * permission — the gates are the HTTP routes' gates, and a refusal there is
    a refusal here, before the press when the role already says so and at
    the press regardless;
  * confirmation — every one of them is a write: planned, shown with the
    exact change, run only through /execute;
  * the two homes of a rule — the review-rules store (PENDING) when the build
    has it, the repository policy's own rules (through PUT's code) when not.
"""

from __future__ import annotations

import importlib.machinery
import sys
import types

import pytest
from fastapi import HTTPException

WS = "ws-1"


# ─── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def repos(monkeypatch):
    """Two repositories registered in WS, one in another workspace."""
    import src.api.auto_review as auto_review

    configs = {
        WS: [types.SimpleNamespace(repo_slug="billing-api", full_name="acme/billing-api"),
             types.SimpleNamespace(repo_slug="payments", full_name="acme/payments")],
        "ws-other": [types.SimpleNamespace(repo_slug="secret", full_name="evil/secret")],
    }
    store = types.SimpleNamespace(list_for_workspace=lambda ws: configs.get(ws, []))
    monkeypatch.setattr(auto_review, "get_auto_review_store", lambda: store)
    return configs


@pytest.fixture
def person(monkeypatch):
    """The asking user, a workspace role for them, and the team gate."""
    import src.api.deps as deps
    import src.users as users

    state = {"role": "editor", "is_admin": False, "repo_ok": True, "calls": []}
    user = types.SimpleNamespace(id="u-1", email="a@b.c", is_active=True,
                                 is_admin=False)

    def _user(_uid):
        user.is_admin = state["is_admin"]
        return user

    monkeypatch.setattr(users, "get_user_store",
                        lambda: types.SimpleNamespace(get_by_id=_user))
    monkeypatch.setattr(deps, "workspace_role",
                        lambda uid, ws: state["role"])
    monkeypatch.setattr(
        deps, "is_workspace_admin",
        lambda u, ws: u.is_admin or state["role"] in ("owner", "admin"))

    async def _editor(user, workspace_id):
        state["calls"].append(("editor", workspace_id))
        if not user.is_admin and state["role"] not in ("owner", "admin", "editor"):
            raise HTTPException(status_code=403, detail=(
                "Editing prompts requires editor, admin or owner on this workspace"))
        return user

    async def _repo(slug, user, min_perm="read", workspace_id=None):
        state["calls"].append(("repo", slug, min_perm, workspace_id))
        if not state["repo_ok"]:
            raise HTTPException(status_code=403,
                                detail=f"Requires '{min_perm}' on {slug}")

    monkeypatch.setattr(deps, "require_prompt_editor", _editor)
    monkeypatch.setattr(deps, "enforce_repo_permission", _repo)
    return state


@pytest.fixture
def routes(monkeypatch):
    """The two route functions the verbs end in, recording what they got."""
    import src.api.routers.review_defaults as rd
    import src.api.routers.review_policies as rp

    seen: dict = {}

    async def _put_defaults(payload, request, session, user, ws_id):
        seen["defaults"] = (payload.model_dump(exclude_unset=True), ws_id, user.id)
        return types.SimpleNamespace(effective=payload.model_dump(exclude_unset=True))

    async def _upsert(repo_slug, payload, request, session, user, _perm, ws_id):
        seen["policy"] = (repo_slug, payload, ws_id)
        return types.SimpleNamespace()

    monkeypatch.setattr(rd, "put_review_defaults", _put_defaults)
    monkeypatch.setattr(rp, "upsert_policy", _upsert)
    return seen


class _Session:
    """`session.get` for a policy row; nothing else is touched."""

    def __init__(self, row=None):
        self.row = row

    async def get(self, _model, _key):
        return self.row


@pytest.fixture
def no_optional_modules(monkeypatch):
    import src.automation.actions as actions

    real = actions._module_available
    monkeypatch.setattr(
        actions, "_module_available",
        lambda name: False if name in (actions._RULES_STORE,
                                       actions._RULES_GENERATE) else real(name))


def _install(monkeypatch, name: str, **attrs):
    mod = types.ModuleType(name)
    mod.__spec__ = importlib.machinery.ModuleSpec(name, None)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def _actor():
    from src.automation.actions import Actor

    return Actor(user_id="u-1", email="a@b.c", workspace_id=WS, label="chat")


RULES = [{"title": "No raw SQL", "instructions": "Handlers never build SQL by hand",
          "path_glob": "src/api/**", "severity": "high"},
         {"instructions": "Every endpoint checks the tenant"}]


# ─── the catalogue ───────────────────────────────────────────────────


@pytest.mark.parametrize("verb", ["propose_review_rules", "generate_review_rules",
                                  "update_review_setting"])
def test_every_review_change_is_a_write_that_waits_for_the_press(verb):
    from src.automation.chat import CATALOGUE, CONFIG_VERBS, Plan, Step

    assert verb in CONFIG_VERBS
    assert not CATALOGUE[verb].get("reads"), f"{verb} would run unconfirmed"
    assert Plan(steps=[Step(action=verb)]).reads_only is False
    mixed = Plan(steps=[Step(action="list_repos"), Step(action=verb)])
    assert mixed.reads_only is False


def test_a_planned_review_change_is_stored_as_planned_not_run():
    """The worker answers reads and parks everything else as "planned" —
    the card with Confirm. Nothing in that branch executes."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "src" / "sync"
           / "handlers.py").read_text(encoding="utf-8")
    body = src[src.index("async def handle_automation_plan("):
               src.index("_PARTIAL_WRITE_INTERVAL")]
    assert "if plan.reads_only and not plan.blocked:" in body
    assert 'status="planned"' in body


# ─── validation ──────────────────────────────────────────────────────


@pytest.mark.parametrize(("rules", "says"), [
    ([], "no rules"),
    ("not a list", "no rules"),
    ([{"title": "x"}], "no instructions"),
    ([{"instructions": "x", "severity": "catastrophic"}], "severity"),
    ([{"instructions": "x", "agents": ["nobody"]}], "unknown agent"),
    ([{"instructions": "x"}] * 11, "at most 10"),
    ([{"instructions": "x" * 5000}], "Rule 1"),
])
def test_a_bad_rule_is_refused_by_name(rules, says):
    from src.automation.actions import ActionError, normalise_review_rules

    with pytest.raises(ActionError, match=says):
        normalise_review_rules(rules)


def test_rules_are_normalised_to_what_will_be_stored():
    from src.automation.actions import normalise_review_rules

    out = normalise_review_rules(RULES)
    assert out[0] == {"title": "No raw SQL",
                      "instructions": "Handlers never build SQL by hand",
                      "path_glob": "src/api/**", "severity": "error",
                      "agents": None}
    assert out[1]["path_glob"] is None and out[1]["severity"] is None


@pytest.mark.parametrize(("scope", "key", "value", "says"), [
    ("repo", "prompt_template", "be nice", "cannot be changed from here"),
    ("workspace", "agent_llm_overrides", {}, "cannot be changed from here"),
    ("repo", "max_inline_comments", 500, "max_inline_comments"),
    ("repo", "comment_min_severity", "apocalyptic", "comment_min_severity"),
    ("workspace", "review_language", "klingon", "review_language"),
    ("workspace", "disabled_agents", ["nobody"], "unknown agent"),
    ("galaxy", "summary_enabled", True, "scope"),
])
def test_a_bad_setting_is_refused(scope, key, value, says):
    from src.automation.actions import ActionError, review_setting_value

    with pytest.raises(ActionError, match=says):
        review_setting_value(scope, key, value)


def test_a_whitelisted_key_the_schema_does_not_have_is_refused_by_name(monkeypatch):
    """Some whitelisted settings are added to the schema separately. One that
    is not there yet must be refused — never written where nothing reads it."""
    import src.automation.actions as actions

    monkeypatch.setattr(actions, "REVIEW_SETTING_KEYS",
                        (*actions.REVIEW_SETTING_KEYS, "not_in_any_schema"))
    with pytest.raises(actions.ActionError, match="not a setting"):
        actions.review_setting_value("repo", "not_in_any_schema", True)


@pytest.mark.parametrize("key", ["approve_when_clean", "run_on_drafts",
                                 "request_changes_on_critical",
                                 "committable_suggestions"])
def test_the_new_switches_follow_the_installed_schema(key):
    """Accepted exactly when the schema has them — whichever build this is."""
    from src.api.schemas import ReviewPolicyIn
    from src.automation.actions import ActionError, review_setting_value

    if key in ReviewPolicyIn.model_fields:
        assert review_setting_value("repo", key, True) is True
    else:
        with pytest.raises(ActionError, match="not a setting"):
            review_setting_value("repo", key, True)


def test_values_are_read_by_the_routes_own_schema():
    from src.automation.actions import review_setting_value

    assert review_setting_value("repo", "summary_enabled", "false") is False
    assert review_setting_value("workspace", "max_inline_comments", "12") == 12
    assert review_setting_value("repo", "comment_min_severity", "Error") == "error"
    assert review_setting_value("workspace", "review_language", "UK") == "uk"
    assert review_setting_value("repo", "summary_enabled", None) is None


# ─── the plan card: refused early, exact change shown ────────────────


def _resolved(step, caller=None):
    from src.automation.chat import Plan, resolve_scope

    return resolve_scope(Plan(steps=[step]), workspace_id=WS, caller=caller).steps[0]


def test_a_setting_step_shows_the_exact_change(repos):
    from src.automation.chat import Step

    step = _resolved(Step(action="update_review_setting", arguments={
        "scope": "repo", "repo_slug": "acme/billing-api",
        "key": "comment_min_severity", "value": "Warning"}),
        caller={"role": "editor"})
    assert step.blocked is None
    assert step.arguments == {"scope": "repo", "repo_slug": "billing-api",
                              "key": "comment_min_severity", "value": "warning"}
    assert step.preview == {"kind": "setting", "scope": "repo",
                            "repo": "billing-api",
                            "key": "comment_min_severity", "value": "warning"}
    assert step.resolved_repos == ["billing-api"]


def test_a_rules_step_shows_the_rules_and_where_they_land(repos, no_optional_modules):
    from src.automation.chat import Step

    step = _resolved(Step(action="propose_review_rules", arguments={
        "repo_slug": "payments", "rules": RULES}), caller={"role": "editor"})
    assert step.blocked is None
    assert step.preview["kind"] == "rules"
    assert step.preview["status"] == "active", (
        "without the rules store the rules are live at once — the card must say so")
    assert [r["instructions"] for r in step.preview["rules"]] == [
        "Handlers never build SQL by hand", "Every endpoint checks the tenant"]


def test_another_workspaces_repository_is_refused_on_the_card(repos):
    from src.automation.chat import Step

    step = _resolved(Step(action="update_review_setting", arguments={
        "scope": "repo", "repo_slug": "secret", "key": "summary_enabled",
        "value": False}), caller={"role": "owner"})
    assert step.blocked and "Not registered in this workspace" in step.blocked


@pytest.mark.parametrize(("role", "scope", "blocked"), [
    ("viewer", "repo", True),
    ("member", "repo", True),
    ("editor", "repo", False),
    ("editor", "workspace", True),
    ("admin", "workspace", False),
])
def test_a_role_that_cannot_make_the_change_is_refused_before_the_press(
        repos, role, scope, blocked):
    from src.automation.chat import Step

    step = _resolved(Step(action="update_review_setting", arguments={
        "scope": scope, "repo_slug": "billing-api" if scope == "repo" else None,
        "key": "summary_enabled", "value": False}), caller={"role": role})
    assert bool(step.blocked) is blocked, step.blocked
    if blocked:
        assert f"yours: {role}" in step.blocked


def test_a_global_admin_is_not_refused_by_role(repos):
    from src.automation.chat import Step

    step = _resolved(Step(action="update_review_setting", arguments={
        "scope": "workspace", "key": "summary_enabled", "value": False}),
        caller={"role": None, "is_admin": True})
    assert step.blocked is None


def test_a_blocked_step_blocks_the_whole_plan(repos):
    from src.automation.chat import Plan, Step, resolve_scope

    plan = resolve_scope(Plan(steps=[
        Step(action="update_review_setting", arguments={
            "scope": "repo", "repo_slug": "billing-api", "key": "nope",
            "value": 1}),
    ]), workspace_id=WS)
    assert plan.blocked and "cannot be changed from here" in plan.blocked


def test_generation_without_a_generator_is_explained_not_offered(
        repos, no_optional_modules):
    from src.automation.actions import RULES_GENERATION_MISSING
    from src.automation.chat import Step

    step = _resolved(Step(action="generate_review_rules",
                          arguments={"repo_slug": "billing-api"}))
    assert step.blocked == RULES_GENERATION_MISSING


def test_workspace_wide_rules_need_the_store(repos, no_optional_modules):
    from src.automation.chat import Step

    step = _resolved(Step(action="propose_review_rules", arguments={
        "repo_slug": None, "rules": RULES}), caller={"role": "owner"})
    assert step.blocked and "Name a repository" in step.blocked


# ─── execution: the rules store, and the fallback ────────────────────


@pytest.mark.asyncio
async def test_rules_go_to_the_store_as_pending_when_it_exists(
        monkeypatch, repos, person, routes):
    from src.automation.actions import propose_review_rules

    got: dict = {}

    def _propose(ws, repo_slug, rules, *, origin, created_by):
        got.update(ws=ws, repo=repo_slug, rules=rules, origin=origin, by=created_by)
        return {"ids": ["r1", "r2"], "status": "pending"}

    _install(monkeypatch, "src.review.rules_store", propose_rules=_propose)
    person["role"] = "member"
    out = await propose_review_rules(_actor(), _Session(),
                                     repo_slug="acme/billing-api", rules=RULES)
    assert got["ws"] == WS and got["repo"] == "billing-api"
    assert got["origin"] == "agent" and got["by"] == "a@b.c"
    assert got["rules"][0]["severity"] == "error"
    assert out["status"] == "pending" and out["count"] == 2
    hrefs = [link["href"] for link in out["links"]]
    assert "/admin/review-rules?status=pending" in hrefs
    assert "/review-settings?repo=billing-api&section=advanced" in hrefs
    assert ("repo", "billing-api", "review", WS) in person["calls"]
    assert "policy" not in routes, "the store path also wrote the policy"


@pytest.mark.asyncio
async def test_an_async_store_is_awaited(monkeypatch, repos, person, routes):
    from src.automation.actions import propose_review_rules

    async def _propose(ws, repo_slug, rules, **_kw):
        return [{"id": "r1"}]

    _install(monkeypatch, "src.review.rules_store", propose_rules=_propose)
    out = await propose_review_rules(_actor(), _Session(), repo_slug=None,
                                     rules=RULES[:1])
    assert out["proposal"] == [{"id": "r1"}]
    assert [link["href"] for link in out["links"]] == [
        "/admin/review-rules?status=pending"]


@pytest.mark.asyncio
async def test_a_viewer_cannot_propose(monkeypatch, repos, person, routes):
    from src.automation.actions import ActionError, propose_review_rules

    _install(monkeypatch, "src.review.rules_store",
             propose_rules=lambda *a, **k: pytest.fail("stored for a viewer"))
    person["role"] = "viewer"
    with pytest.raises(ActionError, match="yours: viewer"):
        await propose_review_rules(_actor(), _Session(), repo_slug="payments",
                                   rules=RULES)


@pytest.mark.asyncio
async def test_without_the_store_rules_are_appended_through_the_policy_save(
        repos, person, routes, no_optional_modules):
    from src.automation.actions import propose_review_rules

    row = types.SimpleNamespace(
        workspace_id=WS, enabled=True, prompt_template="be strict",
        folder_rules=[{"pattern": "docs/**", "prompt": "Spell-check"}],
        department=None, target_branches=["main"], agent_llm_overrides={"x": {}},
        disabled_agents=None, architect_model=None)
    out = await propose_review_rules(_actor(), _Session(row),
                                     repo_slug="billing-api", rules=RULES)
    slug, payload, ws = routes["policy"]
    assert (slug, ws) == ("billing-api", WS)
    rules = [r.model_dump(exclude_none=True) for r in payload.folder_rules]
    assert rules[0] == {"pattern": "docs/**", "prompt": "Spell-check", "agents": []}
    assert rules[1]["pattern"] == "src/api/**"
    assert rules[1]["severity_hint"] == "error" and rules[1]["title"] == "No raw SQL"
    assert rules[2]["pattern"] == "**"
    # The rest of the stored policy goes back as it was…
    assert payload.prompt_template == "be strict"
    assert payload.target_branches == ["main"]
    # …and what PUT keeps when absent is left absent.
    assert "agent_llm_overrides" not in payload.model_fields_set
    assert "disabled_agents" not in payload.model_fields_set
    assert out["status"] == "active" and out["count"] == 2
    assert [link["href"] for link in out["links"]] == [
        "/review-settings?repo=billing-api&section=advanced"]
    assert ("editor", WS) in person["calls"]
    assert ("repo", "billing-api", "review", WS) in person["calls"]


@pytest.mark.asyncio
async def test_the_fallback_has_the_policy_pages_gates(
        repos, person, routes, no_optional_modules):
    from src.automation.actions import ActionError, propose_review_rules

    person["role"] = "member"
    with pytest.raises(ActionError, match="requires editor, admin or owner"):
        await propose_review_rules(_actor(), _Session(), repo_slug="payments",
                                   rules=RULES)
    person["role"] = "editor"
    person["repo_ok"] = False
    with pytest.raises(ActionError, match="Requires 'review' on payments"):
        await propose_review_rules(_actor(), _Session(), repo_slug="payments",
                                   rules=RULES)
    assert "policy" not in routes


@pytest.mark.asyncio
async def test_another_workspaces_policy_row_is_not_overwritten(
        repos, person, routes, no_optional_modules):
    from src.automation.actions import ActionError, propose_review_rules

    foreign = types.SimpleNamespace(workspace_id="ws-other", folder_rules=[])
    with pytest.raises(ActionError, match="not found in this workspace"):
        await propose_review_rules(_actor(), _Session(foreign),
                                   repo_slug="payments", rules=RULES)


# ─── execution: settings ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_workspace_setting_goes_through_the_defaults_route(
        repos, person, routes):
    from src.automation.actions import update_review_setting

    person["role"] = "admin"
    out = await update_review_setting(_actor(), _Session(), scope="workspace",
                                      key="max_inline_comments", value="15")
    assert routes["defaults"] == ({"max_inline_comments": 15}, WS, "u-1")
    assert out["value"] == 15 and out["links"][0]["href"] == "/review-settings?section=filters"


@pytest.mark.asyncio
async def test_a_workspace_setting_needs_owner_or_admin(repos, person, routes):
    from src.automation.actions import ActionError, update_review_setting

    person["role"] = "editor"
    with pytest.raises(ActionError, match="owner/admin"):
        await update_review_setting(_actor(), _Session(), scope="workspace",
                                    key="summary_enabled", value=False)
    assert "defaults" not in routes


@pytest.mark.asyncio
async def test_a_repository_setting_goes_through_the_policy_route(
        repos, person, routes):
    from src.automation.actions import update_review_setting

    out = await update_review_setting(_actor(), _Session(), scope="repo",
                                      repo_slug="payments",
                                      key="summary_enabled", value="false")
    slug, payload, _ws = routes["policy"]
    assert slug == "payments"
    assert payload.summary_enabled is False
    assert "summary_enabled" in payload.model_fields_set
    assert out["repo"] == "payments" and out["count"] == 1


@pytest.mark.asyncio
async def test_a_repository_setting_has_the_policy_pages_gates(
        repos, person, routes):
    from src.automation.actions import ActionError, update_review_setting

    person["role"] = "viewer"
    with pytest.raises(ActionError, match="requires editor"):
        await update_review_setting(_actor(), _Session(), scope="repo",
                                    repo_slug="payments",
                                    key="summary_enabled", value=False)
    person["role"] = "editor"
    person["repo_ok"] = False
    with pytest.raises(ActionError, match="Requires 'review'"):
        await update_review_setting(_actor(), _Session(), scope="repo",
                                    repo_slug="payments",
                                    key="summary_enabled", value=False)
    assert "policy" not in routes


@pytest.mark.asyncio
async def test_an_unknown_person_changes_nothing(monkeypatch, repos, routes):
    import src.users as users
    from src.automation.actions import ActionError, update_review_setting

    monkeypatch.setattr(users, "get_user_store",
                        lambda: types.SimpleNamespace(get_by_id=lambda _u: None))
    with pytest.raises(ActionError, match="who is asking"):
        await update_review_setting(_actor(), _Session(), scope="workspace",
                                    key="summary_enabled", value=False)


# ─── execution: generation ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_generation_runs_the_generator_when_there_is_one(
        monkeypatch, repos, person):
    from src.automation.actions import generate_review_rules

    calls: list = []

    async def _generate(ws, repo_slug, actor):
        calls.append((ws, repo_slug, actor.email))
        return {"job_id": "j-1"}

    _install(monkeypatch, "src.review.rules_generate", generate_rules=_generate)
    person["role"] = "member"
    out = await generate_review_rules(_actor(), _Session(), repo_slug="payments")
    assert calls == [(WS, "payments", "a@b.c")]
    assert out["queued"] == [{"repo": "payments", "job_id": "j-1"}]
    assert out["status"] == "pending"


@pytest.mark.asyncio
async def test_generation_without_a_generator_refuses_at_the_press_too(
        repos, person, no_optional_modules):
    from src.automation.actions import ActionError, generate_review_rules

    with pytest.raises(ActionError, match="cannot draft review rules"):
        await generate_review_rules(_actor(), _Session(), repo_slug="payments")


# ─── the whole path: plan, confirm, execute ──────────────────────────


@pytest.mark.asyncio
async def test_execute_dispatches_each_verb_and_counts_the_change(
        repos, person, routes):
    from src.automation.chat import Plan, Step, execute, resolve_scope

    person["role"] = "admin"
    plan = resolve_scope(Plan(steps=[Step(action="update_review_setting", arguments={
        "scope": "workspace", "key": "comment_min_severity", "value": "error"})]),
        workspace_id=WS, caller={"role": "admin"})
    out = await execute(plan, _actor(), _Session())
    assert out["changed"] == 1
    assert out["steps"][0]["action"] == "update_review_setting"
    assert routes["defaults"][0] == {"comment_min_severity": "error"}


@pytest.mark.asyncio
async def test_execute_refuses_a_plan_the_card_refused(repos, person, routes):
    from src.automation.actions import ActionError
    from src.automation.chat import Plan, Step, execute, resolve_scope

    plan = resolve_scope(Plan(steps=[Step(action="update_review_setting", arguments={
        "scope": "workspace", "key": "summary_enabled", "value": False})]),
        workspace_id=WS, caller={"role": "viewer"})
    with pytest.raises(ActionError):
        await execute(plan, _actor(), _Session())
    assert "defaults" not in routes
