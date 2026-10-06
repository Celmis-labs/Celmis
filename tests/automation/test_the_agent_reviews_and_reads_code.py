"""The agent starts reviews, reads them, tends issues, indexes and reads code.

Eight verbs (`src/automation/actions_reviews.py`), each in the agent chat and
on the MCP mount, behind the gates of the page that already does the thing:

  review_pr      write   `review` on the repository
  index_repo     write   `review` on each repository
  update_issue   write   member role or above (global admin exempt)
  list_reviews, get_review_run, list_issues, ask_code, search_code   reads,
                 `read` on the repository, research access inside

What these tests pin: they are registered the way their kind needs (a read is
a read; a write names repositories or is a config verb), a refusal is the
page's sentence and writes nothing, a happy path returns what the card and the
MCP client render, and every public function of the module is reachable.
"""

from __future__ import annotations

import asyncio
import inspect
import types

import pytest
from fastapi import HTTPException

from src.automation import actions_reviews as ar
from src.automation import chat
from src.automation.actions import ActionError, Actor

WS = "ws-1"
ACTOR = Actor(user_id="u-1", email="a@b.c", workspace_id=WS, label="test")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def world(monkeypatch):
    """Two registered repositories, an admin asking, `read`/`review` allowed
    except on slugs a test adds to `denied`."""
    import src.api.auto_review as auto_review
    import src.api.deps as deps
    import src.users as users

    state = {"denied": set(), "asked": [], "admin": True, "role": "viewer"}
    cfgs = [
        types.SimpleNamespace(repo_slug="billing-api", full_name="acme/billing-api",
                              provider="github", url="https://github.com/acme/billing-api",
                              enabled=True, branch="main", mode="polling"),
        types.SimpleNamespace(repo_slug="payments", full_name="acme/payments",
                              provider="github", url="https://github.com/acme/payments",
                              enabled=True, branch=None, mode="polling"),
    ]
    monkeypatch.setattr(
        auto_review, "get_auto_review_store",
        lambda: types.SimpleNamespace(
            list_for_workspace=lambda ws: cfgs if ws == WS else []))

    def _user():
        return types.SimpleNamespace(id="u-1", email="a@b.c", is_active=True,
                                     is_admin=state["admin"])

    monkeypatch.setattr(users, "get_user_store", lambda: types.SimpleNamespace(
        get_by_id=lambda _u: _user()))

    async def _repo(slug, _user, min_perm="read", workspace_id=None):
        state["asked"].append((slug, min_perm))
        if slug in state["denied"]:
            raise HTTPException(status_code=403, detail=f"Requires '{min_perm}' on {slug}")

    monkeypatch.setattr(deps, "enforce_repo_permission", _repo)
    monkeypatch.setattr(deps, "workspace_role", lambda _u, _w: state["role"])
    return state


# ─── registration ────────────────────────────────────────────────────


def test_the_verbs_are_in_the_catalogue_as_what_they_are():
    cat = chat.CATALOGUE
    for verb in ar.READ_VERBS:
        assert cat[verb].get("reads") is True, verb
    # review_pr / update_issue are config-shaped (one repo or issue, the card
    # shows the change); index_repo queues over a SET and names its repos.
    assert cat["review_pr"].get("config") and "repo_slug" in cat["review_pr"]["arguments"]
    assert cat["update_issue"].get("config")
    assert "repo_slugs" in cat["index_repo"]["arguments"]
    assert set(ar.WRITE_VERBS) <= set(cat)
    assert not (set(ar.READ_VERBS) & set(chat.CONFIG_VERBS))


def test_explained_reads_and_answer_reads():
    assert {"review_settings", "get_review_run", "list_reviews"} <= set(chat.EXPLAINED_READS)
    assert chat.ANSWER_READS == {"ask_code": "answer"}
    for verb in chat.EXPLAINED_READS:
        assert verb in chat.CATALOGUE


def test_the_explainer_is_generic_with_a_hint_per_verb():
    assert callable(chat.explain_read)
    assert callable(chat.explain_review_settings)   # kept for the old callers
    assert set(chat._EXPLAIN_HINTS) >= {"review_settings", "get_review_run", "list_reviews"}


def test_every_public_function_of_the_module_is_reachable():
    """Nothing here is dead: a verb is in the chat catalogue AND an MCP tool."""
    from src.mcp_server import http_app

    src = inspect.getsource(http_app)
    for verb in ar.READ_VERBS + ar.WRITE_VERBS:
        assert f'name="{verb}"' in src, f"{verb} is not an HTTP MCP tool"
        assert f'"{verb}":' in src, f"{verb} has no scope in _TOOL_SCOPES"
        assert verb in chat.CATALOGUE


def test_stdio_serves_the_new_reads_and_keeps_its_own_review_pr():
    from src.mcp_server import server

    src = inspect.getsource(server)
    for verb in ("list_reviews", "get_review_run", "index_repo", "list_issues",
                 "update_issue", "ask_code", "search_code"):
        assert f'name="{verb}"' in src, verb
    assert src.count('name="review_pr"') == 1   # the older synchronous one


def test_http_scopes_are_the_documented_ones():
    from src.mcp_server import http_app

    scopes = http_app._TOOL_SCOPES
    for verb in ("review_pr", "index_repo", "update_issue"):
        assert scopes[verb] == "write:repos"
    for verb in ("list_reviews", "get_review_run", "list_issues"):
        assert scopes[verb] == "read:reviews"
    for verb in ("ask_code", "search_code"):
        assert scopes[verb] == "read:graph"


# ─── planning ────────────────────────────────────────────────────────


def test_plan_review_pr_names_exactly_what_will_run(world):
    args, preview, slugs = ar.plan_step(
        ACTOR, "review_pr", {"repo_slug": "acme/billing-api", "number": "#42"})
    assert args["repo_slug"] == "billing-api" and args["number"] == 42
    assert preview["kind"] == "review_pr" and preview["post_comments"] is True
    assert slugs == ["billing-api"]

    args, preview, _ = ar.plan_step(
        ACTOR, "review_pr",
        {"repo_slug": "payments", "all_open": True, "post_comments": False})
    assert preview["all_open"] is True and preview["number"] is None
    assert args["post_comments"] is False


def test_plan_refuses_what_cannot_run(world):
    with pytest.raises(ActionError):
        ar.plan_step(ACTOR, "review_pr", {"repo_slug": "nope", "number": 1})
    with pytest.raises(ActionError):
        ar.plan_step(ACTOR, "review_pr", {"repo_slug": "payments", "number": "x"})
    with pytest.raises(ActionError):
        ar.plan_step(ACTOR, "update_issue", {"issue_ids": ["a"], "status": "bogus"})
    with pytest.raises(ActionError):
        ar.plan_step(ACTOR, "update_issue", {"status": "open"})


def test_plan_update_issue_understands_everyday_words(world):
    args, preview, slugs = ar.plan_step(
        ACTOR, "update_issue", {"issue_id": "i-1", "status": "close"})
    assert args["status"] == "resolved" and preview["kind"] == "issue"
    assert preview["ids"] == ["i-1"] and slugs == []


# ─── review_pr ───────────────────────────────────────────────────────


def test_review_pr_is_refused_without_the_review_grant(world, monkeypatch):
    world["denied"].add("payments")
    called = []
    monkeypatch.setattr(ar, "queue_pr_reviews", lambda *a, **k: called.append(1))
    with pytest.raises(ActionError, match="Requires 'review' on payments"):
        run(ar.review_pr(ACTOR, None, repo_slug="payments", number=7))
    assert called == []
    assert ("payments", "review") in world["asked"]


def test_review_pr_queues_one_and_reports_the_run(world, monkeypatch):
    seen = {}

    def fake_queue(cfg, targets, *, user_id, post_comments, source):
        seen.update(targets=targets, post=post_comments, source=source)
        return [{"number": n, "run_id": f"run-{n}", "status": "queued"}
                for n in targets], []

    monkeypatch.setattr(ar, "queue_pr_reviews", fake_queue)
    monkeypatch.setattr(ar, "_spawn_inline", lambda payloads: None)
    out = run(ar.review_pr(ACTOR, None, repo_slug="acme/billing-api", number=42,
                           post_comments=False))
    assert seen == {"targets": [42], "post": False, "source": "manual"}
    assert out["queued"][0]["run_id"] == "run-42" and out["run_id"] == "run-42"
    assert out["pr_urls"]["42"] == "https://github.com/acme/billing-api/pull/42"
    assert "count" not in out   # execute() would double count it with `queued`


# ─── index_repo ──────────────────────────────────────────────────────


def test_index_repo_skips_a_repository_without_the_grant(world, monkeypatch):
    import src.sync.queue as queue

    world["denied"].add("payments")
    jobs = []
    monkeypatch.setattr(queue, "enqueue", lambda **kw: jobs.append(kw) or f"job-{len(jobs)}")
    out = run(ar.index_repo(ACTOR, None, repo_slugs=["billing-api", "payments"]))
    assert [q["repo"] for q in out["queued"]] == ["billing-api"]
    assert out["skipped"][0]["repo"] == "payments"
    assert "Requires 'review'" in out["skipped"][0]["reason"]
    assert len(jobs) == 1 and jobs[0]["payload"]["workspace_id"] == WS


def test_index_repo_skips_what_is_already_queued(world, monkeypatch):
    import src.sync.queue as queue

    monkeypatch.setattr(queue, "enqueue", lambda **kw: None)
    out = run(ar.index_repo(ACTOR, None, repo_slugs=["billing-api"]))
    assert out["queued"] == []
    assert "already queued" in out["skipped"][0]["reason"]


def test_index_repo_refuses_an_unknown_repository(world):
    with pytest.raises(ActionError, match="Not registered"):
        run(ar.index_repo(ACTOR, None, repo_slugs=["ghost"]))


# ─── reads of review runs ────────────────────────────────────────────


def _out(**kw):
    base = dict(id="r-1", pr_ref="acme/billing-api#42", pr_provider="github",
                pr_repo="acme/billing-api", pr_number=42, status="complete",
                verdict="approve", findings_count=3, critical=0, error=1,
                warning=2, info=0, posted=True, started_at="2026-10-05T10:00:00",
                elapsed_seconds=12, summary="fine", status_reason=None,
                agents_run=["security"], agents_failed=[])
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_list_reviews_narrows_and_hides_unreadable_repositories(world, monkeypatch):
    from src.api.routers import reviews

    runs = [_out(id="r-1"), _out(id="r-2", pr_repo="acme/payments", pr_number=9),
            _out(id="r-3", status="failed")]
    async def _history(limit, user, ws):
        return runs

    monkeypatch.setattr(reviews, "history", _history)

    out = run(ar.list_reviews(ACTOR, None, limit=10))
    assert [r["run_id"] for r in out["runs"]] == ["r-1", "r-2", "r-3"]
    assert out["runs"][0]["findings"]["total"] == 3

    only = run(ar.list_reviews(ACTOR, None, repo_slug="payments"))
    assert [r["run_id"] for r in only["runs"]] == ["r-2"] and only["scope"] == "repo"

    failed = run(ar.list_reviews(ACTOR, None, status="failed"))
    assert [r["run_id"] for r in failed["runs"]] == ["r-3"]

    world["denied"].add("payments")
    out = run(ar.list_reviews(ACTOR, None))
    assert "r-2" not in [r["run_id"] for r in out["runs"]]
    with pytest.raises(ActionError, match="Requires 'read' on payments"):
        run(ar.list_reviews(ACTOR, None, repo_slug="payments"))


def test_list_reviews_is_bounded(world, monkeypatch):
    from src.api.routers import reviews

    async def _history(limit, user, ws):
        return [_out(id=f"r-{i}") for i in range(60)]

    monkeypatch.setattr(reviews, "history", _history)
    assert len(run(ar.list_reviews(ACTOR, None, limit=999))["runs"]) == ar.MAX_LIST


def test_get_review_run_without_an_id_or_a_pr_asks_for_one(world):
    with pytest.raises(ActionError):
        run(ar.get_review_run(ACTOR, None))


# ─── issues ──────────────────────────────────────────────────────────


def _issue(**kw):
    import datetime as dt

    base = dict(id="i-1", status="open", severity="error", category="bug",
                title="Null deref", repo_slug="billing-api", file_path="a.py",
                line=3, pr_number=42, pr_title="t", pr_url=None, occurrences=2,
                last_seen_at=dt.datetime(2026, 10, 5))
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_list_issues_is_the_pages_query(world, monkeypatch):
    from src.api.routers import issues

    seen = {}

    async def fake(**kw):
        seen.update(kw)
        return types.SimpleNamespace(items=[_issue()], total=1,
                                     status_counts={"open": 1})

    monkeypatch.setattr(issues, "list_issues", fake)
    out = run(ar.list_issues(ACTOR, object(), status="open,fixed,bogus",
                             repo_slug="acme/billing-api", limit=500))
    assert seen["status"] == "open,fixed" and seen["repo"] == "billing-api"
    assert seen["limit"] == ar.MAX_LIST and seen["sort"] == "severity"
    assert out["issues"][0]["title"] == "Null deref" and out["total"] == 1


def test_list_issues_needs_read_on_the_named_repository(world):
    world["denied"].add("payments")
    with pytest.raises(ActionError, match="Requires 'read' on payments"):
        run(ar.list_issues(ACTOR, object(), repo_slug="payments"))


def test_update_issue_needs_the_member_role(world, monkeypatch):
    world["admin"] = False
    world["role"] = "viewer"
    touched = []

    class Session:
        async def get(self, *_a):
            touched.append(1)

    with pytest.raises(ActionError, match="member or above"):
        run(ar.update_issue(ACTOR, Session(), issue_id="i-1", status="dismissed"))
    assert touched == []


def test_update_issue_sets_status_and_reports_the_rest(world, monkeypatch):
    rows = {"i-1": _issue(workspace_id=WS, pr_provider="github",
                          pr_repo="acme/billing-api", resolution_source=None,
                          closed_at=None, fixed_in_sha=None)}

    class Result:
        def scalar_one_or_none(self):
            return None

    class Session:
        async def get(self, _model, ident):
            return rows.get(ident)

        async def commit(self):
            pass

        async def refresh(self, _row):
            pass

        async def execute(self, _stmt):
            return Result()

    out = run(ar.update_issue(ACTOR, Session(), ids=["i-1", "missing"],
                              status="dismissed"))
    assert out["count"] == 1 and out["status"] == "dismissed"
    assert rows["i-1"].status == "dismissed" and rows["i-1"].resolution_source == "manual"
    assert out["skipped"][0]["repo"] == "missing"

    run(ar.update_issue(ACTOR, Session(), issue_id="i-1", status="reopen"))
    assert rows["i-1"].status == "open" and rows["i-1"].closed_at is None


# ─── code questions ──────────────────────────────────────────────────


def test_ask_code_renders_the_answer_and_its_sources(world, monkeypatch):
    import src.llm.budget as budget
    from src.api.routers import qa

    seen = {}

    async def fake_generate(**kw):
        seen.update(kw)
        return "x" * 7000, {"files_read": ["a.py", "b.py"]}

    monkeypatch.setattr(qa, "_generate_full", fake_generate)
    monkeypatch.setattr(budget, "enforce", lambda ws: None)
    out = run(ar.ask_code(ACTOR, None, question="how is auth done?",
                          repo_slugs=["billing-api"]))
    assert seen["target_repos"] == ["billing-api"] and seen["workspace_id"] == WS
    assert out["truncated"] is True and len(out["answer"]) <= ar.MAX_ANSWER_CHARS + 1
    assert out["files"] == ["a.py", "b.py"]


def test_ask_code_honours_budget_and_read_access(world, monkeypatch):
    import src.llm.budget as budget
    from src.api.routers import qa

    async def never(**kw):
        raise AssertionError("the model must not be called")

    monkeypatch.setattr(qa, "_generate_full", never)

    def over(ws):
        raise budget.BudgetExceeded(WS, 10.0, 5.0)

    monkeypatch.setattr(budget, "enforce", over)
    with pytest.raises(ActionError, match="budget"):
        run(ar.ask_code(ACTOR, None, question="q", repo_slugs=["billing-api"]))

    monkeypatch.setattr(budget, "enforce", lambda ws: None)
    world["denied"].update({"billing-api", "payments"})
    with pytest.raises(ActionError, match="No repository"):
        run(ar.ask_code(ACTOR, None, question="q"))
    with pytest.raises(ActionError):
        run(ar.ask_code(ACTOR, None, question="  "))


def test_search_code_validates_before_it_looks(world):
    with pytest.raises(ActionError, match="kind must be"):
        run(ar.search_code(ACTOR, None, kind="grep", query="x"))
    with pytest.raises(ActionError, match="at least two"):
        run(ar.search_code(ACTOR, None, kind="search", query="x"))
    with pytest.raises(ActionError, match="Name the repository"):
        run(ar.search_code(ACTOR, None, kind="usages", query="Foo"))
    world["denied"].add("payments")
    with pytest.raises(ActionError, match="Requires 'read' on payments"):
        run(ar.search_code(ACTOR, None, kind="search", query="Foo", repo_slug="payments"))


def test_search_code_search_is_the_search_pages_function(world, monkeypatch):
    from src.api.routers import search

    seen = {}

    def fake(q, repo, limit, user, ws):
        seen.update(q=q, repo=repo, limit=limit, ws=ws)
        return {"symbols": [{"name": "Foo", "kind": "class", "file": "a.py",
                             "line": 1, "repo_slug": "billing-api"}],
                "notes": [{"repo_slug": "billing-api", "path": "n.md",
                           "title": "Foo", "score": 0.9}]}

    monkeypatch.setattr(search, "search", fake)
    out = run(ar.search_code(ACTOR, None, query="Foo", repo_slug="billing-api", limit=3))
    assert seen == {"q": "Foo", "repo": "billing-api", "limit": 3, "ws": WS}
    assert out["count"] == 1 and out["symbols"][0]["name"] == "Foo"
    assert out["notes"][0]["path"] == "n.md"
