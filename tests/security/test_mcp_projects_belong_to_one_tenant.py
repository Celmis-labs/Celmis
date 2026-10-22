"""The MCP project tools answered for every workspace at once.

`_list_projects_impl` ran `select(Project)` with no filter, and
`_get_project_impl` did `s.get(Project, project_id)` with no ownership check —
while the REST twin has always had `.where(Project.workspace_id == ws_id)`
(src/api/routers/projects.py:80).

So a token issued to one workspace listed every workspace's projects: names,
descriptions, repository counts — and could then open any of them by id and
read the repository slugs. Those slugs are the addresses every other tool
takes.

The pair is worth noticing on its own: the same question answered by two
transports, one filtered and one not. Whenever a capability grows a second
front door, the tenancy check is the part that gets left behind.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.db.models import Base, Project, ProjectRepo
from src.mcp_server.http_app import _get_project_impl, _list_projects_impl

WS_A, WS_B = "ws-alpha", "ws-beta"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(
        engine, tables=[Project.__table__, ProjectRepo.__table__])
    ids = {}
    with Session(engine) as s:
        for ws, name, slug in ((WS_A, "alpha-platform", "github_acme-api"),
                               (WS_B, "beta-secrets", "github_other-secret")):
            p = Project(workspace_id=ws, name=name, description=f"{ws} only")
            s.add(p)
            s.flush()
            s.add(ProjectRepo(project_id=p.id, repo_slug=slug))
            ids[ws] = str(p.id)
        s.commit()
    monkeypatch.setattr("src.mcp_server.http_app._sync_engine", lambda: engine)
    return ids


def test_listing_shows_only_the_callers_own_projects(db):
    out = _list_projects_impl(WS_A)

    assert [p["name"] for p in out["projects"]] == ["alpha-platform"]
    assert out["count"] == 1


def test_the_other_tenants_project_is_not_listed(db):
    names = {p["name"] for p in _list_projects_impl(WS_B)["projects"]}

    assert "alpha-platform" not in names


def test_opening_a_project_by_id_across_tenants_is_not_found(db):
    out = _get_project_impl(db[WS_B], WS_A)

    assert "error" in out
    assert "not found" in out["error"], (
        "a tenant learned that another tenant's project id exists"
    )


def test_the_owner_still_opens_their_own(db):
    out = _get_project_impl(db[WS_A], WS_A)

    assert out.get("name") == "alpha-platform"


def test_a_missing_id_and_a_foreign_id_read_the_same(db):
    """404 rather than 403: the two answers must be indistinguishable."""
    foreign = _get_project_impl(db[WS_B], WS_A)
    missing = _get_project_impl("00000000-0000-0000-0000-000000000000", WS_A)

    assert set(foreign) == set(missing) == {"error"}


# ─── a project does not carry a shared repository to people who do not hold the share ───
#
# The routes refuse to put a repository of another workspace into a project (the REST
# twin resolves slugs in the workspace's own registry), so a project holding one is a
# row somebody wrote past them. What it must still not do is name the repository to
# anyone the share was not given to: the project tools filter the repositories of a
# project by what the caller can read, and a project whose repositories the caller
# can read none of is not shown at all.


def _project_with(world, name: str, *slugs: str) -> str:
    with world.session() as s:
        p = Project(workspace_id="wsid-a", name=name, description="receiver only")
        s.add(p)
        s.flush()
        s.add_all(ProjectRepo(project_id=p.id, repo_slug=slug) for slug in slugs)
        s.commit()
        return str(p.id)


@pytest.fixture(params=["single_tenant", "multi_tenant"])
def shared_world(request, mcp_world, monkeypatch):
    from tests.security.mcp_world import set_env

    set_env(monkeypatch, CELMIS_DEPLOYMENT_MODE=request.param)
    monkeypatch.setattr("src.mcp_server.http_app._sync_engine", lambda: mcp_world.engine)
    return mcp_world


def test_a_project_that_names_a_shared_repository_shows_it_to_the_holder_and_to_nobody_else(
        shared_world):
    from tests.security.mcp_world import SHARED, WILD
    from tests.security.share_world import holder_token, make_share

    world = shared_world
    pid = _project_with(world, "mixed", WILD, SHARED)

    world.as_(holder_token(world, [make_share(world)], own=[WILD], who="owner")[0])
    held = _get_project_impl(pid, "wsid-a")
    assert {r["repo_slug"] for r in held["repos"]} == {WILD, SHARED}

    world.as_(world.issue("owner", [WILD], kind="pat", profile="dev")[0])
    other = _get_project_impl(pid, "wsid-a")
    assert [r["repo_slug"] for r in other["repos"]] == [WILD]
    assert SHARED not in repr(other)


def test_a_project_of_nothing_but_a_shared_repository_is_not_there_for_anybody_without_the_share(
        shared_world):
    from tests.security.mcp_world import SHARED, WILD
    from tests.security.share_world import holder_token, make_share

    world = shared_world
    pid = _project_with(world, "only-shared", SHARED)

    world.as_(world.issue("owner", [WILD], kind="pat", profile="dev")[0])
    assert _get_project_impl(pid, "wsid-a") == {"error": f"project {pid!r} not found"}
    names = [p["name"] for p in _list_projects_impl("wsid-a")["projects"]]
    assert "only-shared" not in names

    world.as_(holder_token(world, [make_share(world)], own=[WILD], who="owner")[0])
    assert _get_project_impl(pid, "wsid-a")["name"] == "only-shared"


def test_a_project_of_the_owner_is_not_shown_to_the_receiver_for_the_share(shared_world):
    """The share is one repository. The owner's projects, and what else is in them,
    stay the owner's."""
    from tests.security.mcp_world import SHARED, WILD
    from tests.security.share_world import holder_token, make_share

    world = shared_world
    with world.session() as s:
        p = Project(workspace_id="wsid-b", name="owners-platform", description="owner only")
        s.add(p)
        s.flush()
        s.add(ProjectRepo(project_id=p.id, repo_slug=SHARED))
        s.commit()
        owners = str(p.id)

    world.as_(holder_token(world, [make_share(world)], own=[WILD], who="owner")[0])
    assert _get_project_impl(owners, "wsid-a") == {"error": f"project {owners!r} not found"}
    assert "owners-platform" not in repr(_list_projects_impl("wsid-a"))
