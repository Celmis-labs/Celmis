"""Who may see which repository — principals x repository states x surfaces.

The rule under test (docs/mcp-access.md): a repository nobody granted anything
on is visible to its workspace's owner, admins and the superadmin only; a
member sees what a team of theirs was given, by a team grant or by a research
rule; and a repository the caller may not see does not exist for them. Not a
403 that confirms it, not a count that includes it, not a slug in a list, and
the single-slug answers for "denied" and "never heard of it" are the same.

The world is `tests/api/rbac_world.py` (real routers, real resolver, SQLite),
with three repositories registered in workspace A:

  GRANTED  a team grant (team_a: review). Its members read it.
  RULED    a research rule at `code` for team-r, whose only member is member_a.
  WILD     no rule, no grant. The default-deny case.

Every surface gets the same expectation table, so a surface that forgets the
filter fails here by name rather than in a review three weeks later.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from tests.api.rbac_world import A_REPO, world

GRANTED = A_REPO
RULED = "github_aco-ruled"
WILD = "github_aco-wild"
ALL = {GRANTED, RULED, WILD}


def _uid(kind: str, i: int) -> str:
    """Projects and chats have UUID keys; these are stable ones."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"access-matrix/{kind}/{i}"))


NOTHING = "github_aco-nothing"          # registered nowhere

#: principal -> the repositories they may see in workspace A
EXPECTED: dict[str, set[str]] = {
    "su": ALL,
    "owner_a": ALL,
    "admin_a": ALL,
    "admin2_a": ALL,
    "editor_a": {GRANTED},
    "member_a": {RULED},
    "viewer_a": set(),
    "both": set(),
}
PRINCIPALS = list(EXPECTED)


def _routers():
    from src.api.routers import access, chats, deps, projects

    return (access.router, projects.router, chats.router, deps.router)


async def _seed(w) -> dict[str, str]:
    from src.api.auto_review import RepoConfig, get_auto_review_store
    from src.db.models import (
        Chat,
        DepAuditRun,
        DepFinding,
        Project,
        ProjectRepo,
        RepoAccessRule,
        ReviewIssue,
        Team,
        TeamMember,
    )

    a = w.ws["ws-a"]
    now = datetime.now(UTC)
    store = get_auto_review_store()
    for slug in (RULED, WILD):
        full = slug.split("_", 1)[1].replace("-", "/", 1)
        store.upsert(RepoConfig(
            user_id=w.uid("admin_a"), repo_slug=slug, provider="github",
            full_name=full, url=f"https://github.com/{full}", workspace_id=a))

    ids = {"mixed": _uid("project", 99), "run": "run-matrix"}
    async with w.factory() as s:
        s.add(Team(id="team-r", name="rule-team", description="", workspace_id=a))
        s.add(TeamMember(team_id="team-r", user_id=w.uid("member_a"), role="member"))
        s.add(RepoAccessRule(id="rule-r", workspace_id=a, team_id="team-r",
                             repo_slug=RULED, visibility="code"))
        for i, slug in enumerate(sorted(ALL)):
            s.add(ReviewIssue(
                id=f"issue-{i}", workspace_id=a, repo_slug=slug, fingerprint=f"fp{i}",
                file_path="x.py", line=1, agent="defect", rule_id="defect.x",
                category="bug", severity="error", title=f"issue in {slug}", body="",
                suggestion=None, status="open", resolution_source=None,
                pr_provider="github", pr_repo=slug.split("_", 1)[1].replace("-", "/", 1), pr_number=i,
                pr_url=None, first_run_id="r1", last_run_id="r1", occurrences=1,
                first_seen_at=now, last_seen_at=now, closed_at=None))
            s.add(Project(id=_uid("project", i), workspace_id=a, name=f"project of {slug}"))
            s.add(ProjectRepo(project_id=_uid("project", i), repo_slug=slug))
            s.add(Chat(id=_uid("chat", i), workspace_id=a, repo_slug=slug,
                       name=f"chat about {slug}"))
            s.add(DepFinding(id=f"f-{i}", run_id=ids["run"], repo_slug=slug,
                             ecosystem="PyPI", package=f"pkg-{i}", current_version="1.0",
                             latest_version="2.0", outdated="major", is_dev=False,
                             vulns=[], severity="none", recommendation="upgrade"))
        s.add(Project(id=ids["mixed"], workspace_id=a, name="everything"))
        for slug in sorted(ALL):
            s.add(ProjectRepo(project_id=ids["mixed"], repo_slug=slug))
        s.add(DepAuditRun(id=ids["run"], workspace_id=a, status="done",
                          summary={"repos_scanned_slugs": sorted(ALL)}))
        await s.commit()
    return ids


def _mentions(body: str) -> set[str]:
    return {slug for slug in ALL if slug in body}


def _check_listing(who: str, body: str, *, surface: str, lists_all: bool = True) -> None:
    """The serialized response names exactly the repositories ``who`` may see."""
    allowed = EXPECTED[who]
    seen = _mentions(body)
    leaked = seen - allowed
    assert not leaked, f"{surface} told {who} about {sorted(leaked)}"
    if lists_all:
        assert seen == allowed, f"{surface} hid {sorted(allowed - seen)} from {who}"


@pytest.fixture
async def matrix(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, event

    from src.access import resolver
    from tests.api.rbac_world import _sqlite_booleans

    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        # The research resolver reads through a blocking engine, as in production.
        sync = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
        event.listen(sync, "connect", _sqlite_booleans)
        monkeypatch.setattr(resolver, "_ENGINE", sync)
        w.matrix = await _seed(w)
        try:
            yield w
        finally:
            sync.dispose()


# ─── repository listings ─────────────────────────────────────────────


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_the_repos_page_lists_what_the_caller_may_see(matrix, who):
    r = await matrix.client.get("/api/repos", headers=matrix.h(who, "ws-a"))
    assert r.status_code == 200, r.text
    assert {x["slug"] for x in r.json()} == EXPECTED[who]


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_my_access_lists_only_repos_the_caller_may_research(matrix, who):
    r = await matrix.client.get("/api/access/my", headers=matrix.h(who, "ws-a"))
    assert r.status_code == 200, r.text
    # the list covers repositories that carry a research rule: RULED only
    _check_listing(who, r.text, surface="/api/access/my", lists_all=False)
    assert {x["repo_slug"] for x in r.json()} == (EXPECTED[who] & {RULED})


@pytest.mark.parametrize("who", PRINCIPALS)
@pytest.mark.parametrize("slug", sorted(ALL))
async def test_my_access_for_one_repo_says_researchable_only_when_it_is(matrix, who, slug):
    r = await matrix.client.get("/api/access/my", params={"repo_slug": slug},
                                headers=matrix.h(who, "ws-a"))
    assert r.status_code == 200, r.text
    [item] = r.json()
    assert item["researchable"] is (slug in EXPECTED[who])


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_a_denied_repo_answers_like_one_that_does_not_exist(matrix, who):
    h = matrix.h(who, "ws-a")
    ghost = (await matrix.client.get("/api/access/my", params={"repo_slug": NOTHING},
                                     headers=h)).json()
    for slug in ALL - EXPECTED[who]:
        denied = (await matrix.client.get("/api/access/my", params={"repo_slug": slug},
                                          headers=h)).json()
        assert [{**d, "repo_slug": "X"} for d in denied] == [
            {**g, "repo_slug": "X"} for g in ghost], (who, slug)


# ─── content surfaces ────────────────────────────────────────────────


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_the_issue_list_holds_no_issue_of_an_unseen_repo(matrix, who):
    r = await matrix.client.get("/api/issues", headers=matrix.h(who, "ws-a"))
    assert r.status_code == 200, r.text
    _check_listing(who, r.text, surface="/api/issues")
    body = r.json()
    assert body["total"] == len(EXPECTED[who]), "the total counts hidden issues"
    assert sum(body["status_counts"].values()) == len(EXPECTED[who])
    assert set(body["repos"]) == EXPECTED[who], "the repo filter names an unseen repo"


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_the_issue_filter_list_does_not_name_an_unseen_repo(matrix, who):
    r = await matrix.client.get("/api/issues", params={"repo": WILD},
                                headers=matrix.h(who, "ws-a"))
    assert r.status_code == 200, r.text
    assert (len(r.json()["items"]) == 1) is (WILD in EXPECTED[who])


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_the_project_list_shows_each_project_only_with_its_visible_repos(matrix, who):
    r = await matrix.client.get("/api/projects", headers=matrix.h(who, "ws-a"))
    assert r.status_code == 200, r.text
    _check_listing(who, r.text, surface="/api/projects")
    names = {p["name"] for p in r.json()}
    for slug in ALL:
        assert (f"project of {slug}" in names) is (slug in EXPECTED[who]), (who, slug)
    assert ("everything" in names) is bool(EXPECTED[who])


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_a_project_of_unseen_repos_answers_like_a_missing_project(matrix, who):
    h = matrix.h(who, "ws-a")
    missing = await matrix.client.get(f"/api/projects/{_uid('project', 500)}", headers=h)
    for i, slug in enumerate(sorted(ALL)):
        r = await matrix.client.get(f"/api/projects/{_uid('project', i)}", headers=h)
        if slug in EXPECTED[who]:
            assert r.status_code == 200, (who, slug)
        else:
            assert (r.status_code, r.json()) == (missing.status_code, missing.json()), (
                who, slug)


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_the_chat_list_holds_no_chat_about_an_unseen_repo(matrix, who):
    r = await matrix.client.get("/api/chats", headers=matrix.h(who, "ws-a"))
    assert r.status_code == 200, r.text
    _check_listing(who, r.text, surface="/api/chats")


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_a_chat_about_an_unseen_repo_answers_like_a_missing_chat(matrix, who):
    h = matrix.h(who, "ws-a")
    missing = await matrix.client.get(f"/api/chats/{_uid('chat', 500)}", headers=h)
    for i, slug in enumerate(sorted(ALL)):
        r = await matrix.client.get(f"/api/chats/{_uid('chat', i)}", headers=h)
        if slug in EXPECTED[who]:
            assert r.status_code == 200, (who, slug)
        else:
            assert (r.status_code, r.json()) == (missing.status_code, missing.json()), (
                who, slug)


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_a_chat_cannot_be_started_on_an_unseen_repo(matrix, who):
    h = matrix.h(who, "ws-a")
    for slug in ALL - EXPECTED[who]:
        r = await matrix.client.post("/api/chats", json={"repo_slug": slug}, headers=h)
        assert r.status_code == 404, (who, slug, r.text)
        assert slug not in r.text


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_the_dependency_findings_hold_no_row_of_an_unseen_repo(matrix, who):
    h = matrix.h(who, "ws-a")
    run_id = matrix.matrix["run"]
    latest = await matrix.client.get("/api/deps/latest", headers=h)
    findings = await matrix.client.get(f"/api/deps/{run_id}/findings", headers=h)
    assert latest.status_code == 200 and findings.status_code == 200, (
        latest.text, findings.text)
    _check_listing(who, latest.text, surface="/api/deps/latest", lists_all=False)
    _check_listing(who, findings.text, surface="/api/deps/findings")
    summary = latest.json()["summary"]
    if who not in ("su", "owner_a", "admin_a", "admin2_a"):
        assert summary.get("restricted") is True
        assert summary["packages"] == len(EXPECTED[who])


@pytest.mark.parametrize("who", ["editor_a", "member_a", "viewer_a", "both"])
async def test_the_ai_report_and_evidence_are_for_admins_only(matrix, who):
    h = matrix.h(who, "ws-a")
    run_id = matrix.matrix["run"]
    for path in (f"/api/deps/{run_id}/evidence",):
        r = await matrix.client.get(path, headers=h)
        assert r.status_code == 403, (path, r.status_code)
    r = await matrix.client.post(f"/api/deps/{run_id}/report", json={"engine": "api"},
                                 headers=h)
    assert r.status_code == 403, r.status_code


# ─── the permission dependency: denied == nonexistent ────────────────


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_a_repo_route_refuses_an_unseen_repo_like_an_unregistered_one(matrix, who):
    from fastapi import HTTPException

    from src.api.deps import enforce_repo_permission

    user = matrix.users[who]
    a = matrix.ws["ws-a"]

    async def verdict(slug: str):
        try:
            await enforce_repo_permission(slug, user, "read", a)
        except HTTPException as exc:
            return exc.status_code, exc.detail
        return 200, None

    ghost = await verdict(NOTHING)
    for slug in ALL:
        got = await verdict(slug)
        if slug in EXPECTED[who]:
            assert got == (200, None), (who, slug, got)
        else:
            assert got == ghost, (who, slug, got)


async def test_the_matrix_is_not_vacuous(matrix):
    """A world where nobody is denied anything proves nothing."""
    assert EXPECTED["viewer_a"] == set() and EXPECTED["member_a"] == {RULED}
    r = await matrix.client.get("/api/repos", headers=matrix.h("viewer_a", "ws-a"))
    assert json.loads(r.text) == []
    r = await matrix.client.get("/api/repos", headers=matrix.h("admin_a", "ws-a"))
    assert len(r.json()) == 3
