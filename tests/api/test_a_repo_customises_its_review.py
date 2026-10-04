"""Per-repository review customization: what the policy API now accepts.

A repo policy can carry, beyond prompts and models:

  * review output — `summary_enabled`, `summary_instructions`,
    `started_comment_enabled`, `review_language`, `max_inline_comments`;
  * structured custom rules — a `folder_rules` entry may add `title`,
    `severity_hint` and `agents`, and a rule addressed to some agents must
    reach ONLY their prompts;
  * a per-repo system prompt for every overridable agent, the verifier
    included, each previewable.

And /admin/agents can ask which repositories override an agent's prompt
(GET /api/review-policies/overrides-summary) — scoped to the caller's
workspace and to the repositories the caller may read.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from src.api import deps as deps_module
from src.api.deps import current_workspace_id, get_current_user
from src.api.routers import llm as llm_router
from src.api.routers import review_policies as policies_router
from src.db.models import RepoReviewPolicy
from src.db.session import get_async_session
from tests.api.rbac_world import A_REPO, B_REPO, B_SECRET, world


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


REPO = "acme/api"
WS = "ws-1"
_USER = SimpleNamespace(id="u-1", email="lead@test", is_admin=True)


@asynccontextmanager
async def policy_api(*, rows: list[dict] | None = None, workspace: dict | None = None):
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(RepoReviewPolicy.__table__.create)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        for row in rows or []:
            session.add(RepoReviewPolicy(**{
                "repo_slug": REPO, "workspace_id": WS, "enabled": True,
                "prompt_template": "", "target_branches": [], "folder_rules": [],
                "agent_prompt_overrides": {}, "mcp_sources": [],
                "disabled_agents": [], **row,
            }))
        await session.commit()

    app = FastAPI()
    app.include_router(policies_router.router)

    async def _session():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[get_current_user] = lambda: _USER
    app.dependency_overrides[current_workspace_id] = lambda: WS

    async def _permitted(_slug, _user, _workspace_id=None):
        return "admin", True

    ws_blob = workspace if workspace is not None else {}
    original = (deps_module._effective_repo_permission, llm_router._load_workspace_config,
                policies_router._require_repo_in_workspace)
    deps_module._effective_repo_permission = _permitted
    llm_router._load_workspace_config = lambda workspace_id="default": ws_blob
    policies_router._require_repo_in_workspace = lambda _slug, _ws: None
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://policies") as c:
            yield c
    finally:
        (deps_module._effective_repo_permission, llm_router._load_workspace_config,
         policies_router._require_repo_in_workspace) = original
        await engine.dispose()


def _body(**overrides) -> dict:
    return {"enabled": True, "prompt_template": "", "target_branches": [],
            "folder_rules": [], **overrides}


async def _put(client, **overrides):
    return await client.put(f"/api/review-policies/{REPO}", json=_body(**overrides))


async def _get(client) -> dict:
    r = await client.get(f"/api/review-policies/{REPO}")
    assert r.status_code == 200, r.text
    return r.json()


async def _preview(client, agent: str) -> dict:
    r = await client.get(f"/api/review-policies/{REPO}/prompt-preview",
                         params={"agent": agent})
    assert r.status_code == 200, r.text
    return r.json()


def _finders() -> tuple[str, ...]:
    return policies_router._llm_agent_names()


# ══════════════════════════════════════════════════════════════════════
#  Review output fields
# ══════════════════════════════════════════════════════════════════════


async def test_an_unconfigured_repo_reports_the_defaults():
    async with policy_api(workspace={"review_language": "de"}) as client:
        p = await _get(client)
    # Nothing said here: every switch inherits, and resolves to on.
    assert p["summary_enabled"] is None and p["summary_enabled_effective"] is True
    assert p["started_comment_enabled"] is None
    assert p["started_comment_enabled_effective"] is True
    assert p["summary_instructions"] is None
    assert p["review_language"] is None
    assert p["sources"]["review_language"] == "workspace"
    assert p["review_language_effective"] == "de"
    assert p["max_inline_comments"] is None
    assert p["max_inline_comments_effective"] == policies_router._max_inline_default()
    assert p["overridable_agents"] == [*_finders(), "verifier"]
    assert p["rule_target_agents"] == list(_finders())
    assert "uk" in p["review_languages"] and "en" in p["review_languages"]


async def test_the_output_fields_round_trip_and_absent_keeps_them():
    async with policy_api() as client:
        r = await _put(client, summary_enabled=False, started_comment_enabled=False,
                       summary_instructions="  Lead with the risk.  ",
                       review_language="UK", max_inline_comments=7)
        assert r.status_code == 200, r.text
        p = await _get(client)
        assert p["summary_enabled"] is False
        assert p["started_comment_enabled"] is False
        assert p["summary_instructions"] == "Lead with the risk."
        assert p["review_language"] == "uk"
        assert p["review_language_effective"] == "uk"
        assert p["max_inline_comments"] == 7
        assert p["max_inline_comments_effective"] == 7

        # A client that does not send them cannot reset them.
        assert (await _put(client)).status_code == 200
        p = await _get(client)
        assert p["summary_enabled"] is False
        assert p["review_language"] == "uk"
        assert p["max_inline_comments"] == 7

        # Explicit null goes back to inheriting / the default.
        r = await _put(client, summary_enabled=None, review_language="",
                       max_inline_comments=None, summary_instructions=None)
        assert r.status_code == 200, r.text
        p = await _get(client)
        assert p["summary_enabled"] is None
        assert p["summary_enabled_effective"] is True
        assert p["sources"]["summary_enabled"] == "install"
        assert p["review_language"] is None
        assert p["max_inline_comments"] is None
        assert p["summary_instructions"] is None


@pytest.mark.parametrize("bad", [0, -1, 101, 1000])
async def test_the_inline_cap_is_bounded(bad):
    async with policy_api() as client:
        r = await _put(client, max_inline_comments=bad)
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("good", [1, 100])
async def test_the_inline_cap_bounds_are_inclusive(good):
    async with policy_api() as client:
        assert (await _put(client, max_inline_comments=good)).status_code == 200
        assert (await _get(client))["max_inline_comments"] == good


async def test_an_unknown_language_is_refused():
    async with policy_api() as client:
        r = await _put(client, review_language="klingon")
    assert r.status_code == 422
    assert "review_language" in r.text


async def test_summary_instructions_are_bounded():
    async with policy_api() as client:
        r = await _put(client, summary_instructions="x" * 4001)
    assert r.status_code == 422


# ══════════════════════════════════════════════════════════════════════
#  Structured rules
# ══════════════════════════════════════════════════════════════════════


async def test_an_old_rule_row_still_reads_and_saves_unchanged():
    legacy = [{"pattern": "src/**", "prompt": "No prints."}]
    async with policy_api(rows=[{"folder_rules": legacy}]) as client:
        p = await _get(client)
        assert p["folder_rules"] == [{"pattern": "src/**", "prompt": "No prints.",
                                      "title": None, "severity_hint": None,
                                      "agents": []}]
        assert (await _put(client, folder_rules=legacy)).status_code == 200
    # Stored exactly as before — no empty keys sprinkled into the row.
    async with policy_api(rows=[{"folder_rules": legacy}]) as client:
        r = await _put(client, folder_rules=[{**legacy[0], "title": "", "agents": []}])
        assert r.status_code == 200, r.text
        assert r.json()["folder_rules"][0]["agents"] == []


async def test_a_rule_can_name_its_agents_and_severity():
    rule = {"pattern": "api/**", "prompt": "Every handler checks auth.",
            "title": "Auth on handlers", "severity_hint": "error",
            "agents": ["security"]}
    async with policy_api() as client:
        r = await _put(client, folder_rules=[rule])
        assert r.status_code == 200, r.text
        assert (await _get(client))["folder_rules"] == [rule]


@pytest.mark.parametrize("patch", [
    {"severity_hint": "fatal"},
    {"agents": ["nobody"]},
    # The verifier reads no rules: a rule addressed to it reaches nothing.
    {"agents": ["verifier"]},
    {"title": "t" * 201},
    {"unknown_key": 1},
])
async def test_a_malformed_rule_is_refused(patch):
    rule = {"pattern": "api/**", "prompt": "x", **patch}
    async with policy_api() as client:
        r = await _put(client, folder_rules=[rule])
    assert r.status_code == 422, r.text


async def test_naming_every_agent_is_stored_as_naming_none():
    rule = {"pattern": "api/**", "prompt": "x", "agents": list(_finders())}
    async with policy_api() as client:
        assert (await _put(client, folder_rules=[rule])).status_code == 200
        assert (await _get(client))["folder_rules"][0]["agents"] == []


async def test_a_targeted_rule_reaches_only_its_agents_prompts():
    finders = _finders()
    target, other = "security", next(a for a in finders if a != "security")
    rules = [
        {"pattern": "api/**", "prompt": "TARGETED-RULE-TEXT", "title": "Auth",
         "severity_hint": "critical", "agents": [target]},
        {"pattern": "**", "prompt": "SHARED-RULE-TEXT"},
    ]
    async with policy_api() as client:
        assert (await _put(client, folder_rules=rules,
                           prompt_template="TEMPLATE-TEXT")).status_code == 200
        targeted = await _preview(client, target)
        untargeted = await _preview(client, other)

    assert "TARGETED-RULE-TEXT" in targeted["system_prompt"]
    assert "**Rule — Auth**" in targeted["system_prompt"]
    assert "severity `critical`" in targeted["system_prompt"]
    assert "TARGETED-RULE-TEXT" not in untargeted["system_prompt"]
    for p in (targeted, untargeted):
        assert "SHARED-RULE-TEXT" in p["system_prompt"]
        assert "TEMPLATE-TEXT" in p["system_prompt"]


# ══════════════════════════════════════════════════════════════════════
#  Per-agent prompts — every overridable agent, the verifier included
# ══════════════════════════════════════════════════════════════════════


async def test_every_overridable_agent_keeps_its_override_and_previews_it():
    agents = [*_finders(), "verifier"]
    overrides = {a: f"REPO-PROMPT-FOR-{a.upper()}" for a in agents}
    async with policy_api() as client:
        assert (await _put(client, agent_prompt_overrides=overrides)).status_code == 200
        assert (await _get(client))["agent_prompt_overrides"] == overrides
        for agent in agents:
            p = await _preview(client, agent)
            assert p["system_prompt"].startswith(overrides[agent]), agent
            assert p["prompt_source"] == "repo"


async def test_the_verifier_preview_inherits_the_builtin_without_an_override():
    from src.review.agents.verifier import _VERIFIER_SYSTEM

    async with policy_api() as client:
        p = await _preview(client, "verifier")
    assert p["system_prompt"] == _VERIFIER_SYSTEM
    assert p["prompt_source"] == "builtin"


def test_the_verifier_call_uses_the_repo_override():
    from src.review.agents.base import AgentContext
    from src.review.agents.verifier import verifier_system_prompt
    from src.review.models import PullRequest

    pr = PullRequest(provider="x", repo="a/b", number=1, title="", description="",
                     author="", base_ref="main", base_sha="", head_ref="f",
                     head_sha="", state="open", url="")
    ctx = AgentContext(pull_request=pr, repo_agent_prompts={"verifier": "MINE"},
                       workspace_id="ws")
    assert verifier_system_prompt(ctx) == "MINE"


# ══════════════════════════════════════════════════════════════════════
#  overrides-summary — workspace-scoped, permission-filtered
# ══════════════════════════════════════════════════════════════════════


async def _seed_overrides(w) -> None:
    from src.db.models import RepoReviewPolicy as Row

    async with w.factory() as s:
        s.add(Row(repo_slug=A_REPO, workspace_id=w.ws["ws-a"], prompt_template="",
                  target_branches=[], folder_rules=[],
                  agent_prompt_overrides={"security": "A security prompt",
                                          "verifier": "A verifier prompt"}))
        await s.commit()
    # B's own policy already overrides security and the verifier (rbac_world).


async def test_the_summary_names_the_workspaces_overriding_repos(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await _seed_overrides(w)
        r = await w.client.get("/api/review-policies/overrides-summary",
                               headers=w.h("editor_a", "ws-a"))
        assert r.status_code == 200, r.text
        got = r.json()["prompt_overrides"]
        assert [x["repo_slug"] for x in got["security"]] == [A_REPO]
        assert [x["repo_slug"] for x in got["verifier"]] == [A_REPO]
        assert got["defect"] == []
        assert B_REPO not in r.text and B_SECRET not in r.text


@pytest.mark.parametrize("actor", ["editor_a", "member_a", "viewer_a", "loner", "both"])
async def test_the_summary_never_names_another_tenants_repo(tmp_path, monkeypatch, actor):
    async with world(tmp_path, monkeypatch) as w:
        await _seed_overrides(w)
        # Asking for B by header: non-members of B are pinned to their own
        # workspace, so B's rows are never read.
        headers = w.h(actor, "ws-b") if actor != "both" else w.h(actor, "ws-a")
        r = await w.client.get("/api/review-policies/overrides-summary", headers=headers)
        assert r.status_code in (200, 403, 404), r.text
        assert B_REPO not in r.text and B_SECRET not in r.text


async def test_the_summary_hides_repos_the_caller_cannot_read(tmp_path, monkeypatch):
    """member_a is in workspace A but not in the team granted A's repo."""
    async with world(tmp_path, monkeypatch) as w:
        await _seed_overrides(w)
        r = await w.client.get("/api/review-policies/overrides-summary",
                               headers=w.h("member_a", "ws-a"))
        assert r.status_code == 200, r.text
        assert all(v == [] for v in r.json()["prompt_overrides"].values())


async def test_a_signed_out_caller_gets_nothing(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.get("/api/review-policies/overrides-summary")
        assert r.status_code in (401, 422)


# ══════════════════════════════════════════════════════════════════════
#  Who may set the new fields
# ══════════════════════════════════════════════════════════════════════


NEW_FIELDS = {
    "summary_enabled": False, "summary_instructions": "Short.",
    "started_comment_enabled": False, "review_language": "uk",
    "max_inline_comments": 5,
    "folder_rules": [{"pattern": "src/**", "prompt": "No prints.",
                      "title": "No prints", "severity_hint": "warning",
                      "agents": ["defect"]}],
    "agent_prompt_overrides": {"verifier": "Keep only proven findings."},
    "suppressed_rules": ["quality.todo"],
}


@pytest.mark.parametrize("actor, status", [
    ("editor_a", 200), ("admin_a", 200), ("member_a", 403), ("viewer_a", 403),
])
async def test_an_editor_sets_them_and_a_member_cannot(tmp_path, monkeypatch, actor, status):
    from src.db.models import RepoReviewPolicy as Row
    from src.db.models import TeamMember

    async with world(tmp_path, monkeypatch) as w:
        async with w.factory() as s:
            for who in ("member_a", "viewer_a"):
                s.add(TeamMember(team_id=w.ids["team_a"], user_id=w.uid(who),
                                 role="member"))
            await s.commit()
        r = await w.client.put(f"/api/review-policies/{A_REPO}", json=NEW_FIELDS,
                               headers=w.h(actor, "ws-a"))
        assert r.status_code == status, r.text
        row = await w.scalar(Row, A_REPO)
        if status == 200:
            assert row.summary_enabled is False
            assert row.started_comment_enabled is False
            assert row.summary_instructions == "Short."
            assert row.review_language == "uk"
            assert row.max_inline_comments == 5
            assert row.folder_rules == NEW_FIELDS["folder_rules"]
            assert row.agent_prompt_overrides == NEW_FIELDS["agent_prompt_overrides"]
            assert row.suppressed_rules == ["quality.todo"]
        else:
            assert row is None
