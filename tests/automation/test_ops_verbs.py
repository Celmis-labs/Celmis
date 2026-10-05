"""Operations verbs: spend, usage, budget, alerts, jobs, audit extras, members.

The agent and MCP gain thirteen verbs whose bodies live in
`src/automation/actions_ops.py` and call the routes the pages call. The tests
are about what makes that safe:

  * the catalogue: reads answer at once, writes are planned (and never flagged
    as reads), `EXPLAINED_READS` names only reads;
  * the card: every write is validated and previewed BEFORE the press, and a
    role that the action will refuse is refused on the card;
  * the gates are the routes' own: members cannot read usage or move the
    budget, a job of another workspace does not exist, and the jobs list never
    shows another tenant's rows (not even to a global admin);
  * the executor hands each verb its arguments, once.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from src.automation import actions_ops as ops
from src.automation.actions import ActionError, Actor
from src.automation.chat import (
    CATALOGUE,
    EXPLAINED_READS,
    OPS_WRITE_VERBS,
    Plan,
    Step,
    execute,
    resolve_scope,
)

WS = "ws-1"
ACTOR = Actor(user_id="u-1", email="a@b.c", workspace_id=WS, label="test")


def run(coro):
    return asyncio.run(coro)


# ─── the catalogue ───────────────────────────────────────────────────


def test_every_ops_verb_is_in_the_catalogue_with_the_right_kind():
    for verb in ops.OPS_READS:
        assert CATALOGUE[verb].get("reads"), f"{verb} must answer at once"
        assert not CATALOGUE[verb].get("ops")
    for verb in ops.OPS_WRITES:
        spec = CATALOGUE[verb]
        assert spec.get("ops") and not spec.get("reads"), (
            f"{verb} is a write: it needs the second press")
    assert set(OPS_WRITE_VERBS) == set(ops.OPS_WRITES)


def test_explained_reads_are_reads():
    assert set(EXPLAINED_READS) <= {n for n, s in CATALOGUE.items() if s.get("reads")}
    for verb in ("get_spend", "get_usage", "list_alerts", "list_jobs", "audit_delta"):
        assert verb in EXPLAINED_READS
    # figures the page already shows plainly do not cost a second model call
    for verb in ("export_sbom", "list_members", "get_budget"):
        assert verb not in EXPLAINED_READS


def test_the_second_model_call_covers_every_explained_read():
    import inspect

    from src.automation import chat
    from src.sync import handlers

    assert "EXPLAINED_READS" in inspect.getsource(handlers.handle_automation_plan)
    assert "verb" in inspect.signature(chat.explain_review_settings).parameters


# ─── the card: validation and roles before the press ─────────────────


def _plan(action, **arguments):
    return Plan(steps=[Step(action=action, arguments=arguments)])


def _resolve(action, caller=None, **arguments):
    plan = resolve_scope(_plan(action, **arguments), workspace_id=WS, caller=caller)
    return plan.steps[0]


ADMIN = {"role": "admin"}
MEMBER = {"role": "member"}


def test_set_budget_card_shows_the_values_it_will_write():
    step = _resolve("set_budget", ADMIN, monthly_usd_cap=200, alert_pct=90,
                    hard_stop=True)
    assert step.blocked is None
    assert step.preview == {"kind": "budget", "monthly_usd_cap": 200.0,
                            "alert_pct": 90, "hard_stop": True}
    assert step.arguments["monthly_usd_cap"] == 200.0


@pytest.mark.parametrize("bad", [-1, "abc", None, 10_000_000])
def test_set_budget_refuses_what_the_route_schema_refuses(bad):
    step = _resolve("set_budget", ADMIN, monthly_usd_cap=bad)
    assert step.blocked, "the card must say why before the press"


def test_set_budget_refuses_a_member_on_the_card():
    step = _resolve("set_budget", MEMBER, monthly_usd_cap=100)
    assert step.blocked and "admin" in step.blocked


@pytest.mark.parametrize("verb", ["retry_job", "cancel_job"])
def test_job_writes_need_an_admin_and_an_id(verb):
    assert _resolve(verb, MEMBER, job_id="j1").blocked
    assert _resolve(verb, ADMIN, job_id="").blocked
    ok = _resolve(verb, ADMIN, job_id="j1")
    assert ok.blocked is None
    assert ok.preview["kind"] == "job" and ok.preview["id"] == "j1"


def test_ack_alert_is_open_to_a_member_as_the_route_is():
    step = _resolve("ack_alert", MEMBER, alert_id="a1")
    assert step.blocked is None
    assert step.preview == {"kind": "alert", "id": "a1", "status": "acked"}
    assert _resolve("ack_alert", MEMBER, alert_id="a1", status="deleted").blocked
    assert _resolve("ack_alert", MEMBER, alert_id=" ").blocked


def test_cancel_dep_audit_is_open_to_a_member_and_takes_no_id():
    step = _resolve("cancel_dep_audit", MEMBER)
    assert step.blocked is None
    assert step.preview == {"kind": "audit_cancel", "run_id": None}


def test_a_global_admin_is_not_refused_on_the_card():
    assert _resolve("set_budget", {"is_admin": True, "role": None},
                    monthly_usd_cap=5).blocked is None


def test_ops_writes_do_not_fall_into_the_repository_fan_out():
    """Left to the default branch they would be blocked with 'Nothing matched —
    no repositories in scope'."""
    step = _resolve("retry_job", ADMIN, job_id="j1")
    assert not (step.blocked or "").startswith("Nothing matched")
    assert step.resolved_repos == []


# ─── the executor ────────────────────────────────────────────────────


def test_the_executor_hands_each_verb_its_arguments(monkeypatch):
    seen: dict[str, dict] = {}

    def fake(name):
        async def _fn(*a, **kw):
            seen[name] = kw
            return {"ok": True}
        return _fn

    for name in (*ops.OPS_READS, *ops.OPS_WRITES):
        monkeypatch.setattr(ops, name, fake(name))

    steps = [
        Step("get_spend", {"days": 7, "surface": "review", "model": "m",
                           "repo_slug": "r"}),
        Step("get_usage", {"days": 14}),
        Step("get_budget", {}),
        Step("set_budget", {"monthly_usd_cap": 50.0, "alert_pct": 70,
                            "hard_stop": True}),
        Step("list_alerts", {"status": "new", "limit": 5}),
        Step("ack_alert", {"alert_id": "a1", "status": "fixed"}),
        Step("list_jobs", {"status": "dead", "kind": "index", "limit": 3}),
        Step("retry_job", {"job_id": "j1"}),
        Step("cancel_job", {"job_id": "j2"}),
        Step("cancel_dep_audit", {"run_id": "r1"}),
        Step("audit_delta", {"run_id": "r2"}),
        Step("export_sbom", {"run_id": "r3", "repo_slug": "x"}),
        Step("list_members", {}),
    ]
    out = run(execute(Plan(steps=steps), ACTOR, object()))
    assert [r["action"] for r in out["steps"]] == [s.action for s in steps]
    assert seen["get_spend"] == {"days": 7, "surface": "review", "model": "m",
                                 "repo": "r"}
    assert seen["set_budget"] == {"monthly_usd_cap": 50.0, "alert_pct": 70,
                                  "hard_stop": True}
    assert seen["ack_alert"] == {"alert_id": "a1", "status": "fixed"}
    assert seen["retry_job"] == {"job_id": "j1"}
    assert seen["export_sbom"] == {"run_id": "r3", "repo": "x"}
    assert seen["list_jobs"] == {"status": "dead", "kind": "index", "limit": 3}


# ─── the gates, as the routes have them ──────────────────────────────


@pytest.fixture
def who(monkeypatch):
    """A signed-in person with a chosen workspace role."""
    import src.api.deps as deps
    import src.automation.actions as actions

    state = {"role": "member", "is_admin": False}
    user = types.SimpleNamespace(id="u-1", email="a@b.c", is_active=True)

    def _user_for(_actor):
        user.is_admin = state["is_admin"]
        return user

    monkeypatch.setattr(actions, "_user_for", _user_for)
    monkeypatch.setattr(ops, "_user_for", _user_for)
    monkeypatch.setattr(deps, "workspace_role", lambda uid, ws: state["role"])
    monkeypatch.setattr(
        deps, "is_workspace_admin",
        lambda u, ws: bool(getattr(u, "is_admin", False)) or state["role"] in ("owner", "admin"))
    return state


def test_usage_is_refused_to_a_member_and_allowed_to_an_editor(who, monkeypatch):
    import src.api.routers.usage as usage

    seen = {}

    def fake(days, user, workspace_id):
        seen["ws"] = workspace_id
        return usage.UsageSummary(
            days=days, total_runs=2, completed_runs=1, failed_runs=1, tokens_input=1,
            tokens_output=1, cost_usd=0.5, cost_source_mix={}, daily=[])

    monkeypatch.setattr(usage, "usage_summary", fake)
    with pytest.raises(ActionError, match="editor"):
        run(ops.get_usage(ACTOR))
    who["role"] = "editor"
    out = run(ops.get_usage(ACTOR, days=7))
    assert out["total_runs"] == 2 and seen["ws"] == WS


def test_set_budget_needs_a_workspace_admin_and_calls_the_route(who, monkeypatch):
    import src.api.routers.spend as spend

    calls = {}

    async def fake_put(payload, session, admin, ws):
        calls.update(cap=payload.monthly_usd_cap, ws=ws)
        return spend.BudgetOut(workspace_id=ws, cap_usd=payload.monthly_usd_cap,
                               spent_usd=0, used_pct=0, hard_stop=payload.hard_stop,
                               alert_pct=payload.alert_pct, enabled=True,
                               over_cap=False, over_alert=False)

    monkeypatch.setattr(spend, "put_budget", fake_put)
    with pytest.raises(ActionError, match="owner/admin"):
        run(ops.set_budget(ACTOR, object(), monthly_usd_cap=100))
    assert not calls
    who["role"] = "admin"
    out = run(ops.set_budget(ACTOR, object(), monthly_usd_cap=100, alert_pct=75))
    assert calls == {"cap": 100.0, "ws": WS}
    assert out["cap_usd"] == 100.0 and out["alert_pct"] == 75


def test_set_budget_refuses_an_invalid_value_with_the_schemas_words(who):
    who["role"] = "admin"
    with pytest.raises(ActionError, match="monthly_usd_cap"):
        run(ops.set_budget(ACTOR, object(), monthly_usd_cap=-5))


def test_spend_and_budget_are_for_owner_and_admin_only(who):
    for role in ("viewer", "member", "editor"):
        who["role"] = role
        with pytest.raises(ActionError):
            run(ops.get_spend(ACTOR, object()))
        with pytest.raises(ActionError):
            run(ops.get_budget(ACTOR))


def test_spend_is_read_for_this_workspace_and_trimmed(who, monkeypatch):
    import src.api.routers.spend as spend

    who["role"] = "admin"

    rows = [spend.GroupRow(key=f"m{i}", calls=1, tokens_in=1, tokens_out=1,
                           cached_tokens_in=0, cost_usd=1.0) for i in range(20)]
    asked = {}

    async def fake_summary(**kw):
        asked["summary"] = kw
        return spend.SpendSummary(
            days=kw["days"], calls=20, tokens_in=20, tokens_out=20, cached_tokens_in=0,
            cost_usd=20.0, cache_hit_pct=0, estimated_share_pct=0, by_surface=rows,
            by_agent=[], by_model=rows, by_provider=[])

    async def fake_daily(**kw):
        asked["daily"] = kw
        return [spend.DailyPoint(date="2026-10-01", cost_usd=1, tokens_in=1,
                                 tokens_out=1, calls=1)]

    monkeypatch.setattr(spend, "summary", fake_summary)
    monkeypatch.setattr(spend, "daily", fake_daily)
    out = run(ops.get_spend(ACTOR, object(), days=9999, surface="review"))
    assert asked["summary"]["ws"] == WS and asked["daily"]["ws"] == WS
    assert asked["summary"]["days"] == 365, "the window is clamped like the route's"
    assert asked["summary"]["surface"] == "review"
    assert len(out["by_model"]) <= 8, "a chat bubble is not a table of 50 models"
    assert out["filters"] == {"surface": "review"}
    assert out["daily"][0]["date"] == "2026-10-01"
    assert "by_user" not in out, "people's spend is the page's, not a chat answer"


def test_alerts_filter_validation_and_scope(who, monkeypatch):
    import src.api.routers.alerts as alerts

    seen = {}

    async def fake_list(limit, session, _user, workspace_id):
        seen["ws"] = workspace_id
        mk = lambda i, st: alerts.AlertOut(  # noqa: E731
            id=f"a{i}", source="grafana", title=f"t{i}", body="b" * 1000,
            severity="critical", status=st, repo_hint=None, session_id=None,
            created_at="2026-10-01")
        return [mk(1, "new"), mk(2, "acked"), mk(3, "new")]

    monkeypatch.setattr(alerts, "list_alerts", fake_list)
    out = run(ops.list_alerts(ACTOR, object(), status="new"))
    assert [a["id"] for a in out["alerts"]] == ["a1", "a3"]
    assert out["by_status"] == {"new": 2, "acked": 1}
    assert len(out["alerts"][0]["body"]) <= 300, "bodies are clipped"
    assert seen["ws"] == WS
    with pytest.raises(ActionError, match="status"):
        run(ops.list_alerts(ACTOR, object(), status="bogus"))


def test_ack_alert_calls_the_patch_route_with_the_workspace(who, monkeypatch):
    import src.api.routers.alerts as alerts

    seen = {}

    async def fake_patch(alert_id, payload, session, _user, workspace_id):
        seen.update(id=alert_id, status=payload.status, ws=workspace_id)
        return alerts.AlertOut(id=alert_id, source="s", title="t", body="", severity="x",
                               status=payload.status, repo_hint=None, session_id=None,
                               created_at="")

    monkeypatch.setattr(alerts, "patch_alert", fake_patch)
    out = run(ops.ack_alert(ACTOR, object(), alert_id="a1"))
    assert seen == {"id": "a1", "status": "acked", "ws": WS}
    assert out["status"] == "acked"
    with pytest.raises(ActionError):
        run(ops.ack_alert(ACTOR, object(), alert_id="a1", status="deleted"))


def test_jobs_list_is_this_workspaces_even_for_a_global_admin(who, monkeypatch):
    from src.sync import queue as jq

    asked = {}
    monkeypatch.setattr(jq, "list_jobs", lambda **kw: asked.update(list=kw) or [
        {"id": "j1", "kind": "index", "status": "dead", "last_error": "x" * 900,
         "workspace_id": WS, "payload": {"secret": "no"}}])
    monkeypatch.setattr(jq, "stats", lambda **kw: asked.update(stats=kw) or {"dead": 1})
    who["is_admin"] = True
    out = run(ops.list_jobs(ACTOR, status="dead", limit=10))
    assert asked["list"]["workspace_id"] == WS and asked["stats"]["workspace_id"] == WS
    assert out["stats"] == {"dead": 1}
    job = out["jobs"][0]
    assert "payload" not in job, "job payloads are the queue's, not a chat answer"
    assert len(job["last_error"]) <= 300


@pytest.mark.parametrize("verb,queue_fn", [("retry_job", "retry_dead"),
                                           ("cancel_job", "request_cancel")])
def test_job_writes_need_admin_and_a_row_of_this_workspace(who, monkeypatch, verb, queue_fn):
    from src.sync import queue as jq

    row = {"id": "j1", "kind": "index", "workspace_id": WS}
    monkeypatch.setattr(jq, "get_job", lambda jid: row if jid == "j1" else None)
    done = []
    monkeypatch.setattr(jq, queue_fn, lambda jid: done.append(jid) or True)
    fn = getattr(ops, verb)

    with pytest.raises(ActionError, match="owner/admin"):
        run(fn(ACTOR, job_id="j1"))
    who["role"] = "admin"
    assert run(fn(ACTOR, job_id="j1"))["id"] == "j1"
    assert done == ["j1"]

    # another tenant's row, an untenanted maintenance row and an unknown id are
    # all the same answer — a different one would confirm the id exists
    for other in ({"id": "j2", "workspace_id": "ws-2"}, {"id": "j2", "workspace_id": None}):
        monkeypatch.setattr(jq, "get_job", lambda jid, other=other: other)
        with pytest.raises(ActionError, match="job not found"):
            run(fn(ACTOR, job_id="j2"))
    monkeypatch.setattr(jq, "get_job", lambda jid: None)
    with pytest.raises(ActionError, match="job not found"):
        run(fn(ACTOR, job_id="nope"))
    # even a global admin acts only inside the workspace they are asking in
    who["is_admin"] = True
    monkeypatch.setattr(jq, "get_job", lambda jid: {"id": "j2", "workspace_id": "ws-2"})
    with pytest.raises(ActionError, match="job not found"):
        run(fn(ACTOR, job_id="j2"))
    assert done == ["j1"]


def test_a_job_in_the_wrong_state_is_a_refusal_not_a_success(who, monkeypatch):
    from src.sync import queue as jq

    who["role"] = "owner"
    monkeypatch.setattr(jq, "get_job", lambda jid: {"id": jid, "workspace_id": WS})
    monkeypatch.setattr(jq, "retry_dead", lambda jid: False)
    monkeypatch.setattr(jq, "request_cancel", lambda jid: False)
    with pytest.raises(ActionError, match="retryable"):
        run(ops.retry_job(ACTOR, job_id="j1"))
    with pytest.raises(ActionError, match="not running"):
        run(ops.cancel_job(ACTOR, job_id="j1"))


def test_export_sbom_returns_a_link_not_the_file(who, monkeypatch):
    run_row = types.SimpleNamespace(id="run-9", status="done")

    async def fake_run(actor, session, run_id, *, statuses):
        return run_row

    monkeypatch.setattr(ops, "_run_or_latest", fake_run)
    out = run(ops.export_sbom(ACTOR, object()))
    assert out["path"] == "/api/deps/run-9/sbom"
    assert out["url"].endswith("/api/deps/run-9/sbom")
    assert "content" not in out and "sbom" not in {k for k in out if k != "path"}
    one = run(ops.export_sbom(ACTOR, object(), repo="acme/api"))
    assert one["path"] == "/api/deps/run-9/sbom?repo=acme/api"
    run_row.status = "running"
    with pytest.raises(ActionError, match="not finished"):
        run(ops.export_sbom(ACTOR, object()))


def test_a_run_of_another_workspace_is_not_found():
    class Session:
        async def get(self, _model, _id):
            return types.SimpleNamespace(id="r1", workspace_id="ws-other", status="done")

    with pytest.raises(ActionError, match="Run not found"):
        run(ops._run_or_latest(ACTOR, Session(), "r1", statuses=None))


def test_members_are_listed_with_their_teams(who, monkeypatch):
    import src.api.routers.workspaces as workspaces

    async def fake_members(ws_id, session, user):
        assert ws_id == WS
        return [workspaces.MemberOut(user_id="u1", role="admin", email="a@x", name="Ann"),
                workspaces.MemberOut(user_id="u2", role="viewer", email="b@x", name="")]

    class Result:
        def all(self):
            return [("u1", "backend"), ("u1", "sre")]

    class Session:
        async def execute(self, _stmt):
            return Result()

    monkeypatch.setattr(workspaces, "list_members", fake_members)
    out = run(ops.list_members(ACTOR, Session()))
    assert out["count"] == 2
    ann = next(m for m in out["members"] if m["user_id"] == "u1")
    assert ann["teams"] == ["backend", "sre"] and ann["role"] == "admin"
    assert next(m for m in out["members"] if m["user_id"] == "u2")["teams"] == []
    # nothing here invites, removes or changes a role
    assert not any(hasattr(ops, n) for n in ("invite_member", "set_member_role",
                                             "remove_member"))
