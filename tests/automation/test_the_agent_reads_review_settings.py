"""The agent can say what the review settings are, and explain them.

"Перевір поточні налаштування PR review та поясни як це працює" had no verb:
the planner returned no steps, the page said it did not understand, and the
note was generic text about a configuration nobody had read. `review_settings`
is that verb, and these tests are about what makes it trustworthy:

  * it is a READ — answered at once, never a plan card — and it is told apart
    from `help` (where is it) and `update_review_setting` (change it);
  * the values and their SOURCES are the resolver's: a repository's own
    override, the workspace default and the built-in value are three different
    answers and must not blur;
  * the access rules are the settings routes' — workspace scoping, and the
    team's `read` grant on a repository;
  * the explanation is a second model call written from the snapshot, and its
    failure never fails the answer.
"""

from __future__ import annotations

import asyncio
import types

import pytest
from fastapi import HTTPException

WS = "ws-1"


# ─── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def world(monkeypatch):
    """One workspace with two repositories, an admin asking, no stored rows
    until a test adds them."""
    import src.api.auto_review as auto_review
    import src.api.deps as deps
    import src.api.routers.agents as agents
    import src.api.routers.review_policies as rp
    import src.automation.actions as actions
    import src.users as users

    state = {"defaults": None, "denied": set(), "guidelines": {}, "replaced": set(),
             "asked": []}
    cfgs = [
        types.SimpleNamespace(repo_slug="billing-api", full_name="acme/billing-api",
                              enabled=True, branch="main", mode="polling"),
        types.SimpleNamespace(repo_slug="payments", full_name="acme/payments",
                              enabled=False, branch=None, mode="polling"),
    ]
    store = types.SimpleNamespace(
        list_for_workspace=lambda ws: cfgs if ws == WS else [])
    monkeypatch.setattr(auto_review, "get_auto_review_store", lambda: store)

    user = types.SimpleNamespace(id="u-1", email="a@b.c", is_active=True, is_admin=True)
    monkeypatch.setattr(users, "get_user_store",
                        lambda: types.SimpleNamespace(get_by_id=lambda _u: user))

    async def _repo(slug, _user, min_perm="read", workspace_id=None):
        state["asked"].append((slug, min_perm, workspace_id))
        if slug in state["denied"]:
            raise HTTPException(status_code=403, detail=f"Requires '{min_perm}' on {slug}")

    monkeypatch.setattr(deps, "enforce_repo_permission", _repo)

    async def _defaults(_session, _ws):
        return state["defaults"]

    monkeypatch.setattr(rp, "_load_workspace_defaults", _defaults)
    monkeypatch.setattr(rp, "_workspace_review_language_layer",
                        lambda _ws: ("en", "install"))
    monkeypatch.setattr(agents, "get_workspace_guidelines",
                        lambda agent, _ws="default": state["guidelines"].get(agent, ""))
    monkeypatch.setattr(agents, "_load_override",
                        lambda agent, _ws="default": "x" if agent in state["replaced"] else None)

    async def _no_rules(_session, _ws, _slug):
        return 3

    monkeypatch.setattr(actions, "_active_rules_count", _no_rules)
    return state


def _row(**own):
    """A `RepoReviewPolicy`-shaped row: every inheritable field "inherit"."""
    from src.review.review_defaults import INHERITABLE_FIELDS

    base = dict.fromkeys(INHERITABLE_FIELDS)
    base.update(workspace_id=WS, enabled=True, review_language=None,
                agent_prompt_guidelines={}, agent_guidelines_extend=[],
                agent_prompt_overrides={}, agent_llm_overrides=None)
    base.update(own)
    return types.SimpleNamespace(**base)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows=()):
        self.rows = {r.repo_slug: r for r in rows}

    async def get(self, _model, key):
        return self.rows.get(key)

    async def scalars(self, _stmt):
        return _Result(list(self.rows.values()))


def _actor():
    from src.automation.actions import Actor

    return Actor(user_id="u-1", email="a@b.c", workspace_id=WS, label="chat")


def _read(session, repo=None):
    from src.automation.actions import read_review_settings

    return asyncio.run(read_review_settings(_actor(), session, repo_slug=repo))


# ─── the catalogue ───────────────────────────────────────────────────


def test_the_verb_is_a_read_and_is_not_a_write():
    from src.automation.chat import CATALOGUE, CONFIG_VERBS

    spec = CATALOGUE["review_settings"]
    assert spec["reads"] is True
    assert "review_settings" not in CONFIG_VERBS
    assert "repo_slug" in spec["arguments"]


def test_it_is_told_apart_from_its_neighbours():
    """The planner chooses on these sentences alone: the read must name the
    thing it does not do, and the neighbours must not claim it."""
    from src.automation.chat import CATALOGUE

    summary = CATALOGUE["review_settings"]["summary"].lower()
    assert "update_review_setting" in summary and "help" in summary
    assert "перевір" in summary
    assert CATALOGUE["update_review_setting"].get("config")
    assert not CATALOGUE["update_review_setting"].get("reads")


def test_a_plan_with_only_this_verb_is_an_answer_not_a_card():
    from src.automation.chat import Plan, Step

    plan = Plan(steps=[Step(action="review_settings", arguments={})])
    assert plan.reads_only


# ─── the snapshot ────────────────────────────────────────────────────


def test_the_workspace_scope_reports_builtin_and_workspace_sources(world):
    world["defaults"] = {"run_on_drafts": True, "comment_min_severity": "warning"}
    out = _read(_Session())

    assert out["scope"] == "workspace" and out["repo"] is None
    assert out["fields"]["run_on_drafts"] == {"value": True, "source": "workspace"}
    assert out["fields"]["comment_min_severity"]["source"] == "workspace"
    # Nobody said anything about this one: the built-in value, labelled so.
    assert out["fields"]["approve_when_clean"] == {"value": False, "source": "install"}
    assert "defect" in out["agents_on"]
    # business_logic is opt-in: off until a layer enables it.
    assert "business_logic" in out["agents_off"]
    assert out["links"][0]["href"] == "/review-settings"


def test_a_repository_override_wins_over_the_workspace_default(world):
    world["defaults"] = {"run_on_drafts": True, "approve_when_clean": True}
    row = _row(repo_slug="billing-api", run_on_drafts=False,
               enabled_agents=["business_logic"], disabled_agents=["cve"])
    out = _read(_Session([row]), "billing-api")

    assert out["scope"] == "repo" and out["repo"] == "billing-api"
    assert out["fields"]["run_on_drafts"] == {"value": False, "source": "repo"}
    assert out["fields"]["approve_when_clean"] == {"value": True, "source": "workspace"}
    assert out["fields"]["request_changes_on_critical"]["source"] == "install"
    assert "business_logic" in out["agents_on"]
    assert "cve" in out["agents_off"]
    assert out["auto_review"] == {"enabled": True, "branch": "main", "mode": "polling"}
    assert out["rules_enabled"] == 3
    assert out["links"][0]["href"] == "/review-settings?repo=billing-api"
    # Read-checked on the repository, in this workspace.
    assert ("billing-api", "read", WS) in world["asked"]


def test_either_spelling_of_a_repository_is_accepted(world):
    assert _read(_Session(), "acme/payments")["repo"] == "payments"


def test_guidelines_are_cut_and_a_replaced_prompt_is_flagged(world):
    world["guidelines"] = {"security": "x" * 1000}
    world["replaced"] = {"defect"}
    out = _read(_Session())

    g = out["guidelines"]["security"]
    assert g["from"] == "workspace" and g["chars"] == 1000
    assert len(g["text"]) <= 301
    assert out["prompt_replaced_agents"] == ["defect"]


def test_the_repository_guidelines_replace_the_workspaces(world):
    world["guidelines"] = {"security": "workspace text"}
    row = _row(repo_slug="payments", agent_prompt_guidelines={"security": "repo text"})
    out = _read(_Session([row]), "payments")

    assert out["guidelines"]["security"]["from"] == "repository"
    assert out["guidelines"]["security"]["text"] == "repo text"


def test_the_workspace_overview_names_repositories_that_differ(world):
    row = _row(repo_slug="payments", approve_when_clean=True, enabled=False)
    out = _read(_Session([row]))

    assert out["repos"] == [{
        "repo": "payments", "overridden": ["approve_when_clean"],
        "review_enabled": False, "auto_review": False,
    }]
    assert out["auto_review"] == {"repos_total": 2, "auto_review_on": 1}


def test_a_repository_the_caller_may_not_read_is_neither_named_nor_readable(world):
    from src.automation.actions import ActionError

    world["denied"] = {"payments"}
    row = _row(repo_slug="payments", approve_when_clean=True)
    session = _Session([row])

    with pytest.raises(ActionError, match="Requires 'read'"):
        _read(session, "payments")
    overview = _read(session)
    assert overview["repos"] == []
    assert overview["auto_review"]["repos_total"] == 1


def test_another_workspaces_repository_is_not_registered_here(world):
    from src.automation.actions import ActionError

    with pytest.raises(ActionError, match="Not registered"):
        _read(_Session(), "evil/secret")


def test_another_tenants_policy_row_is_not_read(world):
    row = _row(repo_slug="payments", run_on_drafts=True)
    row.workspace_id = "ws-other"
    out = _read(_Session([row]), "payments")
    assert out["fields"]["run_on_drafts"]["source"] == "install"


def test_execute_returns_the_snapshot_as_the_step_result(world):
    from src.automation.chat import Plan, Step, execute

    plan = Plan(steps=[Step(action="review_settings",
                            arguments={"repo_slug": "billing-api"})])
    out = asyncio.run(execute(plan, _actor(), _Session()))

    result = out["steps"][0]
    assert result["action"] == "review_settings"
    assert result["result"]["repo"] == "billing-api"
    assert "fields" in result["result"]


# ─── the answer ──────────────────────────────────────────────────────


@pytest.fixture
def handler(monkeypatch):
    """`handle_automation_plan` with the model, the queue and the row stubbed
    to what the test needs."""
    import src.automation.chat as chat
    import src.sync.handlers as handlers
    import src.sync.queue as queue

    seen: dict = {"finished": []}
    plan = chat.Plan(
        steps=[chat.Step(action="review_settings", arguments={})],
        note="plan note", language="uk")

    monkeypatch.setattr(chat, "interpret", lambda *a, **k: plan)
    monkeypatch.setattr(chat, "resolve_scope", lambda p, **k: p)
    monkeypatch.setattr(queue, "is_cancel_requested", lambda _id: False)
    monkeypatch.setattr(handlers, "_partial_note_writer", lambda _rid: (lambda _t: None))
    monkeypatch.setattr(handlers, "_cancel_poller", lambda _jid: (lambda: False))

    async def _reads(_plan, _payload):
        return {"steps": [{"action": "review_settings",
                           "result": {"scope": "workspace", "fields": {}}}]}

    async def _finish(run_id, **kw):
        seen["finished"].append((run_id, kw))

    monkeypatch.setattr(handlers, "_run_automation_reads", _reads)
    monkeypatch.setattr(handlers, "_finish_automation_plan", _finish)
    return seen


def _job():
    return {"id": "j-1", "payload": {
        "run_id": "r-1", "workspace_id": WS, "user_id": "u-1",
        "message": "перевір налаштування pr review", "history": None}}


def test_the_answer_is_the_explanation_written_from_the_snapshot(handler, monkeypatch):
    import src.automation.chat as chat
    import src.sync.handlers as handlers

    calls: dict = {}

    def _explain(message, snapshot, **kw):
        calls.update(message=message, snapshot=snapshot, **kw)
        return "## Налаштування\nПояснення"

    monkeypatch.setattr(chat, "explain_review_settings", _explain)
    asyncio.run(handlers.handle_automation_plan(_job()))

    (run_id, kw), = handler["finished"]
    assert run_id == "r-1"
    assert kw["status"] == "answered"
    assert kw["note"] == "## Налаштування\nПояснення"
    # The model is handed what the read returned, and the language it was asked in.
    assert calls["snapshot"] == {"scope": "workspace", "fields": {}}
    assert calls["language"] == "uk" and calls["workspace_id"] == WS
    assert calls["on_note"] is not None


def test_a_failed_explanation_keeps_the_plan_note_and_still_answers(handler, monkeypatch):
    import src.automation.chat as chat
    import src.sync.handlers as handlers

    def _boom(*a, **k):
        raise RuntimeError("upstream 503")

    monkeypatch.setattr(chat, "explain_review_settings", _boom)
    asyncio.run(handlers.handle_automation_plan(_job()))

    (_, kw), = handler["finished"]
    assert kw["status"] == "answered"
    assert kw["note"] == "plan note"
    assert kw["result"]["steps"][0]["action"] == "review_settings"


def test_other_reads_do_not_pay_for_a_second_call(handler, monkeypatch):
    import src.automation.chat as chat
    import src.sync.handlers as handlers

    async def _reads(_plan, _payload):
        return {"steps": [{"action": "list_repos", "result": {}}]}

    monkeypatch.setattr(handlers, "_run_automation_reads", _reads)

    def _never(*a, **k):
        raise AssertionError("a second model call for a read that has no data to explain")

    monkeypatch.setattr(chat, "explain_review_settings", _never)
    asyncio.run(handlers.handle_automation_plan(_job()))
    assert handler["finished"][0][1]["note"] == "plan note"


def test_the_explainer_is_its_own_operation_on_the_agents_bill(monkeypatch):
    """Same client pattern as the reading, a separate operation name, and the
    reply is cleaned by the same link allow-list."""
    import src.llm.client as llm_client
    from src.automation.chat import explain_review_settings

    captured: dict = {}

    class _Client:
        def generate(self, **kw):
            captured.update(kw)
            kw["on_delta"]("partial")
            return types.SimpleNamespace(
                text="See [x](/review-settings) and [bad](/nope/at/all)")

    def _build(user_id, workspace_id, **kw):
        captured["build"] = (user_id, workspace_id, kw)
        return _Client()

    monkeypatch.setattr(llm_client, "build_llm_client", _build)
    notes: list[str] = []
    text = explain_review_settings(
        "what are the settings", {"scope": "workspace"},
        workspace_id=WS, user_id="u-1", language="en", on_note=notes.append)

    assert captured["operation"] == "automation_explain_settings"
    assert captured["temperature"] == 0.0 and captured["timeout"] == 20
    assert captured["build"][2]["spend_surface"] == "automation"
    assert "workspace" in captured["prompt"]
    assert notes == ["partial"]
    assert "[x](/review-settings)" in text and "/nope/at/all" not in text
