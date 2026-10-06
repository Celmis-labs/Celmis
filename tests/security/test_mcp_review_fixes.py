"""Gaps found in review of the access lane, each pinned by what it let through.

  * the server's own loopback callers (agent, claude-engine review, doc
    generation) mint a token the verifier accepts, and it is never wider than
    the person it was minted for;
  * the dependency, issue and alert verbs obey a token's repo list even when
    the person's own access is wider, and obey default-deny when it is not;
  * ``ask_code`` names no repository the caller cannot read;
  * a rule of one team is not overridden by another team's grant;
  * a call refused at the HTTP edge leaves an audit row;
  * a grant can only be narrowed after issue, not widened;
  * smaller leaks: roster emails, job errors, projects, scope at call time.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime

import pytest

from src.automation.actions import ActionError, Actor
from tests.security.mcp_world import GRANTED, RULED, WILD, WS_A


def _verify(token: str):
    from src.mcp_server.auth import JwtTokenVerifier

    return asyncio.run(JwtTokenVerifier().verify_token(token))


def _readable(world, token: str) -> set[str]:
    from src.mcp_server.identity import caller_access

    world.as_(token)
    _caller, access = caller_access([GRANTED, RULED, WILD])
    return {s for s, d in access.items() if d.researchable}


# ─── loopback callers ────────────────────────────────────────────────


def test_the_agents_own_token_passes_the_real_verifier(mcp_world):
    from src.agent.runner import _mint_mcp_token

    token = _mint_mcp_token(mcp_world.users["owner"], WS_A)
    assert _verify(token) is not None, "the in-app agent would lose its MCP tools"


def test_the_agents_own_token_is_never_wider_than_its_person(mcp_world):
    from src.agent.runner import _mint_mcp_token

    for who, expected in (("mg", {GRANTED}), ("mr", {RULED}), ("mn", set()),
                          ("owner", {GRANTED, RULED, WILD})):
        token = _mint_mcp_token(mcp_world.users[who], WS_A)
        assert _readable(mcp_world, token) == expected, who


def test_the_agents_token_is_read_only_and_never_listed_or_widened(mcp_world):
    from src.agent.runner import _mint_mcp_token
    from src.mcp_server import token_store

    token = _mint_mcp_token(mcp_world.users["owner"], WS_A)
    claims = _verify(token)
    assert claims is not None and not any(s.startswith("write:") for s in claims.scopes)
    with mcp_world.session() as s:
        listed = token_store.list_rows(s, user_id=mcp_world.users["owner"],
                                       kinds=token_store.KINDS)
        assert listed == [], "internal rows are not part of any token list"
        [internal] = token_store.list_rows(
            s, user_id=mcp_world.users["owner"], kinds=(token_store.INTERNAL_KIND,))
        with pytest.raises(token_store.TokenError):
            token_store.update_row(s, internal.id, patterns=["*"])


def test_the_agents_token_dies_with_its_row(mcp_world):
    from src.agent.runner import _mint_mcp_token
    from src.mcp_server import token_store

    token = _mint_mcp_token(mcp_world.users["owner"], WS_A)
    assert _verify(token) is not None
    with mcp_world.session() as s:
        [internal] = token_store.list_rows(
            s, user_id=mcp_world.users["owner"], kinds=(token_store.INTERNAL_KIND,))
        token_store.revoke(s, internal.id, by="test")
    assert _verify(token) is None


def test_every_loopback_caller_names_the_workspace_it_acts_in():
    """The three callers mint through ``_mint_mcp_token``; each must hand it the
    workspace, or the row would be bound to ``default``."""
    import inspect

    from src.agent import runner
    from src.generation import claude_docs
    from src.review import claude_engine

    assert "_mint_mcp_token(row.user_id, row.workspace_id)" in inspect.getsource(runner)
    assert "_mint_mcp_token(user_id, workspace_id)" in inspect.getsource(claude_engine)
    assert "self.workspace_id)" in inspect.getsource(claude_docs).split("_mint_mcp_token")[-1]


# ─── the verbs obey a token's repo list ──────────────────────────────


@pytest.fixture
def adb(mcp_world, tmp_path, monkeypatch):
    """Async access to the world's database, and the rows the probes need."""
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from tests.security.mcp_world import _sqlite_booleans

    engine = create_async_engine(f"sqlite+aiosqlite:///{mcp_world.engine.url.database}")
    event.listen(engine.sync_engine, "connect", _sqlite_booleans)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    from src.db import session as session_mod
    from src.mcp_server import http_app

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{mcp_world.engine.url.database}")
    monkeypatch.setattr(http_app, "_SYNC_ENGINE", mcp_world.engine, raising=False)
    monkeypatch.setattr(session_mod, "_engine", engine)
    monkeypatch.setattr(session_mod, "_session_factory", maker)

    from src.db.models import DepAuditRun, DepFinding, IncomingAlert, ReviewIssue

    now = datetime.now(UTC)
    with mcp_world.session() as s:
        s.add(DepAuditRun(
            id="run1", workspace_id=WS_A, status="done",
            summary={"repos_scanned_slugs": [GRANTED, WILD], "repos_total": 2,
                     "audited_commits": {GRANTED: "aaa", WILD: "bbb"},
                     "ai_report": f"{WILD} is on fire"}))
        for slug in (GRANTED, WILD):
            s.add(DepFinding(id=f"f-{slug}", run_id="run1", repo_slug=slug,
                             ecosystem="npm", package=f"pkg-of-{slug}",
                             current_version="1.0.0", severity="high",
                             outdated="minor", recommendation="update_now"))
            s.add(ReviewIssue(
                id=f"i-{slug}", workspace_id=WS_A, repo_slug=slug, fingerprint=slug,
                title=f"issue in {slug}", status="open", first_seen_at=now,
                last_seen_at=now, pr_repo=slug.split("_", 1)[1].replace("-", "/", 1),
                pr_provider="github", pr_number=1))
            s.add(IncomingAlert(id=f"a-{slug}", workspace_id=WS_A, title=f"boom {slug}",
                                repo_hint=slug, created_at=now))
        s.add(IncomingAlert(id="a-none", workspace_id=WS_A, title="no hint", created_at=now))
        s.commit()

    class Db:
        def run(self, fn):
            async def go():
                async with maker() as session:
                    return await fn(session)
            return asyncio.run(go())

    yield Db()
    asyncio.run(engine.dispose())


def _actor(world, who: str, patterns=None) -> Actor:
    return Actor(user_id=world.users[who], email=f"{who}@acme.io", workspace_id=WS_A,
                 label="mcp", token_filter=tuple(patterns) if patterns is not None else None)


def test_dependency_findings_follow_the_tokens_repo_list(mcp_world, adb):
    from src.automation.actions import list_dep_findings

    # an owner can read everything, but the token lists only one repository
    rows = adb.run(lambda s: list_dep_findings(
        _actor(mcp_world, "owner", [GRANTED]), s, "run1"))
    assert {r["repo"] for r in rows} == {GRANTED}


def test_the_dependency_summary_is_rebuilt_from_what_the_caller_may_see(mcp_world, adb):
    from src.automation.actions import get_dep_audit

    out = adb.run(lambda s: get_dep_audit(_actor(mcp_world, "owner", [GRANTED]), s))
    text = json.dumps(out)
    assert WILD not in text and "on fire" not in text
    assert out["summary"]["repos_scanned_slugs"] == [GRANTED]


def test_a_member_without_access_sees_no_dependency_row(mcp_world, adb):
    """A self-service token has no list: the person's own access (default-deny)."""
    from src.automation.actions import get_dep_audit, list_dep_findings

    nobody = _actor(mcp_world, "mn")
    assert adb.run(lambda s: list_dep_findings(nobody, s, "run1")) == []
    out = adb.run(lambda s: get_dep_audit(nobody, s))
    assert WILD not in json.dumps(out) and GRANTED not in json.dumps(out)


def test_a_member_sees_the_dependency_rows_of_their_own_repository_only(mcp_world, adb):
    from src.automation.actions import list_dep_findings

    rows = adb.run(lambda s: list_dep_findings(_actor(mcp_world, "mg"), s, "run1"))
    assert {r["repo"] for r in rows} == {GRANTED}


def test_the_dependency_delta_follows_the_token_too(mcp_world, adb):
    from src.automation.actions_ops import audit_delta

    out = adb.run(lambda s: audit_delta(_actor(mcp_world, "owner", [GRANTED]), s, run_id="run1"))
    assert WILD not in json.dumps(out)


def test_an_sbom_link_for_a_listed_token_must_name_one_listed_repo(mcp_world, adb):
    from src.automation.actions_ops import export_sbom

    actor = _actor(mcp_world, "owner", [GRANTED])
    with pytest.raises(ActionError):
        adb.run(lambda s: export_sbom(actor, s, run_id="run1"))
    with pytest.raises(ActionError):
        adb.run(lambda s: export_sbom(actor, s, run_id="run1", repo=WILD))
    out = adb.run(lambda s: export_sbom(actor, s, run_id="run1", repo=GRANTED))
    assert GRANTED in out["path"]


def test_issues_follow_the_tokens_list_when_the_person_can_read_more(mcp_world, adb):
    from src.automation.actions_reviews import list_issues

    out = adb.run(lambda s: list_issues(_actor(mcp_world, "owner", [GRANTED]), s))
    assert {i["repo"] for i in out["issues"]} == {GRANTED}
    assert out["total"] == 1


def test_an_issue_outside_the_token_cannot_be_changed_by_id(mcp_world, adb):
    from src.automation.actions_reviews import update_issue

    actor = _actor(mcp_world, "owner", [GRANTED])
    with pytest.raises(ActionError, match="not found"):
        adb.run(lambda s: update_issue(actor, s, issue_id=f"i-{WILD}", status="dismissed"))


def test_alerts_follow_the_tokens_list(mcp_world, adb):
    from src.automation.actions_ops import list_alerts

    out = adb.run(lambda s: list_alerts(_actor(mcp_world, "owner", [GRANTED]), s))
    assert {a["repo"] for a in out["alerts"]} == {GRANTED}, \
        "a repo-limited token gets neither other repos' alerts nor workspace-level ones"


def test_alerts_for_a_member_follow_default_deny(mcp_world, adb):
    from src.automation.actions_ops import list_alerts

    out = adb.run(lambda s: list_alerts(_actor(mcp_world, "mn"), s))
    assert {a["repo"] for a in out["alerts"]} == {""}, "only the workspace-level alert"
    out = adb.run(lambda s: list_alerts(_actor(mcp_world, "mg"), s))
    assert {a["repo"] for a in out["alerts"]} == {GRANTED, ""}


def test_runs_of_an_unlisted_repository_are_not_listed(mcp_world, adb, monkeypatch):
    """A run whose repository is not among the token's is dropped, not kept
    because it could not be matched to a registered repo."""
    import types

    from src.api.routers import reviews
    from src.automation import actions_reviews as ar

    def _out(run_id: str, repo: str):
        return types.SimpleNamespace(
            id=run_id, pr_provider="github", pr_repo=repo, pr_number=1, pr_title="t",
            pr_url="", status="done", verdict="ok", started_at="", elapsed_seconds=1,
            summary="", status_reason=None, agents_run=[], agents_failed=[],
            findings_count=0, finding_counts={}, cost_usd=0)

    runs = [_out("r-g", "aco/app"), _out("r-w", "aco/wild")]

    async def _history(limit, user, ws):
        return runs

    monkeypatch.setattr(reviews, "history", _history)
    monkeypatch.setattr(ar, "_run_row", lambda out, cfg: {"id": out.id})
    out = asyncio.run(ar.list_reviews(_actor(mcp_world, "owner", [GRANTED])))
    assert [r["id"] for r in out["runs"]] == ["r-g"]


# ─── ask_code names nobody ───────────────────────────────────────────


def test_ask_code_returns_no_blocked_repos_and_asks_for_a_name_free_notice(
        mcp_world, adb, monkeypatch):
    from src.api.routers import qa
    from src.automation import actions_reviews as ar
    from src.llm import budget

    seen = {}

    async def fake(**kw):
        seen.update(kw)
        return "an answer", {"files_read": ["a.py"], "blocked_repos": [WILD]}

    monkeypatch.setattr(qa, "_generate_full", fake)
    monkeypatch.setattr(budget, "enforce", lambda ws: None)
    out = asyncio.run(ar.ask_code(_actor(mcp_world, "owner", [GRANTED]), None, question="how?"))
    assert "blocked_repos" not in out and WILD not in json.dumps(out)
    assert seen["name_free_notice"] is True
    assert seen["token_filter"] == (GRANTED,)
    assert seen["target_repos"] == [GRANTED]


def test_the_boundary_notice_can_be_written_without_a_repository_name():
    from src.qa.multi_repo_retriever import MultiRepoRetriever

    for no_accessible in (False, True):
        text = MultiRepoRetriever._build_access_notice(
            denied_repos=[WILD], related_repos=["github_aco-other"], hidden_files=[],
            access={}, no_accessible=no_accessible, anonymous=True)
        assert WILD not in text and "aco-other" not in text and text
    named = MultiRepoRetriever._build_access_notice(
        denied_repos=[WILD], related_repos=[], hidden_files=[], access={},
        no_accessible=True, anonymous=False)
    assert WILD in named, "the page keeps naming them to the people who can ask for access"


# ─── a rule narrows, a grant is only the fallback ────────────────────


def _resolve(world, who: str, slug: str):
    from src.access.resolver import resolve_access

    return resolve_access(user_id=world.users[who], is_admin=False, workspace_id=WS_A,
                          repos=[slug])[slug]


def test_a_grant_does_not_override_another_teams_research_rule(mcp_world):
    from src.db.models import RepoAccessRule, RepoTeamAccess

    with mcp_world.session() as s:
        # team-r has a rule on WILD; team-g holds a read grant on it
        s.add_all([
            RepoAccessRule(id="rule-wild-r", workspace_id=WS_A, team_id="team-r",
                           repo_slug=WILD, visibility="code"),
            RepoTeamAccess(repo_slug=WILD, team_id="team-g", permission="read"),
        ])
        s.commit()
    assert _resolve(mcp_world, "mr", WILD).researchable
    assert not _resolve(mcp_world, "mg", WILD).researchable, \
        "team-g holds a grant, but the repo has a rule for another team"


def test_a_grant_still_opens_a_repo_nobody_wrote_a_rule_for(mcp_world):
    assert _resolve(mcp_world, "mg", GRANTED).researchable


def test_a_grant_with_an_unknown_permission_grants_nothing(mcp_world):
    from src.db.models import RepoTeamAccess

    with mcp_world.session() as s:
        s.add(RepoTeamAccess(repo_slug=WILD, team_id="team-g", permission="godmode"))
        s.commit()
    assert not _resolve(mcp_world, "mg", WILD).researchable


# ─── refusals at the edge are audited ────────────────────────────────


def _rows(world):
    from sqlalchemy import select

    from src.db.models import McpCallLog
    from src.mcp_server import audit

    assert audit.flush(10.0)
    with world.session() as s:
        return list(s.execute(select(McpCallLog).order_by(McpCallLog.ts)).scalars())


def test_a_revoked_token_leaves_a_denied_row_with_its_id(mcp_world):
    from src.mcp_server import audit, token_store

    audit._DENIED_SEEN.clear()
    token, view = mcp_world.issue("mn", [WILD])
    with mcp_world.session() as s:
        token_store.revoke(s, view.id, by=mcp_world.users["su"])
    assert _verify(token) is None
    [row] = _rows(mcp_world)
    assert row.status == "denied" and row.token_id == view.id
    assert row.user_id == mcp_world.users["mn"] and row.workspace_id == WS_A
    assert row.tool == "(refused:revoked)" and row.args_hash == ""
    assert token not in repr(row.__dict__)


def test_a_legacy_token_and_an_expired_one_are_audited_with_their_reason(mcp_world):
    from src.mcp_server import audit, token_store

    audit._DENIED_SEEN.clear()
    assert _verify(mcp_world.legacy_token("owner")) is None
    token, view = mcp_world.issue("mn", [WILD])
    with mcp_world.session() as s:
        row = mcp_world.row(view.id)
        row.expires_at = datetime(2020, 1, 1, tzinfo=UTC)
        s.merge(row)
        s.commit()
    token_store.invalidate()
    assert _verify(token) is None
    tools = sorted(r.tool for r in _rows(mcp_world))
    assert tools == ["(refused:expired)", "(refused:legacy)"]


def test_a_hammering_revoked_token_is_logged_once_a_minute(mcp_world):
    from src.mcp_server import audit, token_store

    audit._DENIED_SEEN.clear()
    token, view = mcp_world.issue("mn", [WILD])
    with mcp_world.session() as s:
        token_store.revoke(s, view.id, by=mcp_world.users["su"])
    for _ in range(5):
        assert _verify(token) is None
    assert len(_rows(mcp_world)) == 1


def test_the_audit_counts_the_rows_a_full_queue_drops(monkeypatch, caplog):
    import logging
    import queue

    from src.mcp_server import audit

    tiny: queue.Queue = queue.Queue(maxsize=1)
    monkeypatch.setattr(audit, "_QUEUE", tiny)
    monkeypatch.setattr(audit, "_ensure_worker", lambda: None)
    before = audit.dropped_rows()
    with caplog.at_level(logging.WARNING):
        for i in range(3):
            audit._enqueue({"i": i})
    assert audit.dropped_rows() == before + 2
    assert any("mcp_audit_queue_full" in r.message for r in caplog.records)


def test_the_argument_digest_is_keyed_to_the_installation(mcp_world, monkeypatch):
    from src.mcp_server import call_envelope

    args = {"repo_slug": GRANTED}
    plain = hashlib.sha256(
        json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16]
    monkeypatch.setattr(call_envelope, "_SALT", None)
    first = call_envelope._args_hash(args)
    assert first != plain, "a dictionary of slugs could undo an unkeyed digest"
    assert first == call_envelope._args_hash(args) and len(first) == 16
    monkeypatch.setattr(call_envelope, "_SALT", b"another installation")
    assert call_envelope._args_hash(args) != first


# ─── a grant can be narrowed, not widened ────────────────────────────


def test_a_self_service_token_has_no_repo_list_to_patch(mcp_world):
    from src.mcp_server import token_store

    _token, view = mcp_world.self_token("mn")
    with mcp_world.session() as s, pytest.raises(token_store.TokenError, match="self-service"):
        token_store.update_row(s, view.id, patterns=[WILD])


def test_an_issued_token_cannot_be_extended_or_given_write(mcp_world):
    from src.mcp_server import token_store

    _token, view = mcp_world.issue("mn", [WILD], days=5)
    with mcp_world.session() as s:
        with pytest.raises(token_store.TokenError, match="extended"):
            token_store.update_row(s, view.id, expires_in_days=60)
        with pytest.raises(token_store.TokenError, match="write"):
            token_store.update_row(s, view.id, allow_write=True)
        row = token_store.update_row(s, view.id, expires_in_days=1)
        assert (row.expires_at - datetime.now(UTC).replace(tzinfo=None)
                if row.expires_at.tzinfo is None else row.expires_at - datetime.now(UTC)
                ).days <= 1


def test_a_write_token_can_be_switched_back_to_read_only(mcp_world):
    from src.mcp_server import token_store

    token, view = mcp_world.issue("mn", [WILD], write=True)
    with mcp_world.session() as s:
        row = token_store.update_row(s, view.id, allow_write=False)
        assert not row.allow_write and not any(x.startswith("write:") for x in row.scopes)
    mcp_world.as_(token)
    from src.mcp_server.identity import resolve_caller

    assert resolve_caller().allow_write is False


def test_a_pattern_that_spans_two_providers_is_warned_about(mcp_world):
    from src.api.auto_review import RepoConfig, get_auto_review_store
    from src.api.routers.mcp_tokens import _ambiguity_warnings
    from src.db.models import Workspace  # noqa: F401 — keeps the world loaded

    get_auto_review_store().upsert(RepoConfig(
        user_id=mcp_world.users["su"], repo_slug="gitlab_aco-app", provider="gitlab",
        full_name="aco/app", url="https://gitlab.example.com/aco/app", workspace_id=WS_A))
    warnings = _ambiguity_warnings(["aco/app", GRANTED, "github_aco-*"], WS_A)
    assert len(warnings) == 1 and "aco/app" in warnings[0] and "exact slugs" in warnings[0]
    assert _ambiguity_warnings([GRANTED], WS_A) == []


# ─── smaller exposures ───────────────────────────────────────────────


def test_a_member_does_not_get_the_rosters_addresses(mcp_world, adb):
    from src.automation.actions_ops import list_members

    member = adb.run(lambda s: list_members(_actor(mcp_world, "mn"), s))
    assert member["members"] and all(m["email"] == "" for m in member["members"])
    narrowed = adb.run(lambda s: list_members(_actor(mcp_world, "owner", [GRANTED]), s))
    assert all(m["email"] == "" for m in narrowed["members"])
    owner = adb.run(lambda s: list_members(_actor(mcp_world, "owner"), s))
    assert any("@" in m["email"] for m in owner["members"])


def test_a_project_of_repositories_nobody_here_can_read_is_not_shown(mcp_world, adb):
    import uuid

    from src.db.models import Project, ProjectRepo
    from src.mcp_server import http_app

    wild_id, ok_id = str(uuid.uuid4()), str(uuid.uuid4())
    with mcp_world.session() as s:
        s.add_all([
            Project(id=wild_id, workspace_id=WS_A, name="Secret plans", description="d"),
            Project(id=ok_id, workspace_id=WS_A, name="Shop", description="d"),
        ])
        s.flush()
        s.add_all([
            ProjectRepo(project_id=wild_id, repo_slug=WILD, role="primary"),
            ProjectRepo(project_id=ok_id, repo_slug=GRANTED, role="primary"),
        ])
        s.commit()
    token, _ = mcp_world.issue("mn", [GRANTED])
    mcp_world.as_(token)
    listed = http_app._list_projects_impl(WS_A)
    assert [p["name"] for p in listed["projects"]] == ["Shop"]
    assert "not found" in http_app._get_project_impl(str(wild_id), WS_A)["error"]


def test_a_dev_token_cannot_call_the_full_profiles_read_tools_by_name(mcp_world, adb):
    import mcp.types as types

    from src.mcp_server import http_app

    token, _ = mcp_world.issue("owner", [GRANTED], profile="dev")
    mcp_world.as_(token)
    mcp = http_app._build_mcp()
    handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
        name="list_projects", arguments={}))
    result = asyncio.run(handler(req)).root
    assert result.isError and "Required scope" in result.content[0].text
    rows = _rows(mcp_world)
    assert rows and rows[-1].tool == "list_projects" and rows[-1].status == "denied"


def test_a_full_token_still_reaches_the_read_tools(mcp_world, adb):
    import mcp.types as types

    from src.mcp_server import http_app

    token, _ = mcp_world.issue("owner", [GRANTED], profile="full")
    mcp_world.as_(token)
    mcp = http_app._build_mcp()
    handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(
        name="list_projects", arguments={}))
    assert not asyncio.run(handler(req)).root.isError
