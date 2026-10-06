"""Repository groups under default-deny.

A group is a list of repository identifiers and a grep target for drift.
  * LISTING it names its repositories, so a person sees only the members whose
    code they may read, and no group at all when they may read none;
  * CHANGING it changes what drift reads and quotes for every member, so it
    takes an editor or above who holds `review` on every repository in it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from tests.api.rbac_world import A_REPO_FULL, world


@pytest.fixture(autouse=True)
def _sync_resolver_on_the_world_database(tmp_path, monkeypatch):
    from src.access import resolver

    engine = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
    monkeypatch.setattr(resolver, "_ENGINE", engine)
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def _groups_in_a_scratch_directory(tmp_path, monkeypatch):
    from src.groups.manager import GroupManager

    mgr = GroupManager()
    mgr.groups_dir = tmp_path / "groups"
    mgr.groups_dir.mkdir(parents=True, exist_ok=True)
    for target in ("src.groups.get_group_manager", "src.groups.manager.get_group_manager"):
        monkeypatch.setattr(target, lambda: mgr, raising=False)
    monkeypatch.setattr("src.groups.manager._default_manager", mgr, raising=False)
    yield
    import src.groups.manager as gm

    gm._default_manager = None


def _router():
    from src.api.routers import groups

    return groups.router


STORED = f"github:{A_REPO_FULL}"


async def _admin_makes_the_group(w) -> None:
    r = await w.client.post("/api/repos/groups", json={"name": "plat", "repos": [STORED]},
                            headers=w.h("admin_a", "ws-a"))
    assert r.status_code == 201, r.text


async def _make_editor_without_the_grant(w, who: str) -> None:
    from src.db.models import WorkspaceMember

    async with w.factory() as s:
        member = await s.get(WorkspaceMember, (w.ws["ws-a"], w.uid(who)))
        member.role = "editor"
        await s.commit()


@pytest.mark.parametrize("who", ["member_a", "viewer_a"])
async def test_a_group_of_closed_repositories_is_not_listed(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch, extra_routers=(_router(),)) as w:
        await _admin_makes_the_group(w)
        r = await w.client.get("/api/repos/groups", headers=w.h(who, "ws-a"))
        assert r.status_code == 200
        assert r.json() == [], "a closed repository's identifier was served in a group"
        assert A_REPO_FULL not in r.text


@pytest.mark.parametrize("who", ["admin_a", "owner_a", "editor_a"])
async def test_a_group_is_listed_to_whoever_may_read_its_repositories(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch, extra_routers=(_router(),)) as w:
        await _admin_makes_the_group(w)
        r = await w.client.get("/api/repos/groups", headers=w.h(who, "ws-a"))
        assert [(g["name"], g["repos"]) for g in r.json()] == [("plat", [STORED])]


@pytest.mark.parametrize("who", ["member_a", "viewer_a"])
async def test_a_member_or_viewer_cannot_change_a_group(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch, extra_routers=(_router(),)) as w:
        await _admin_makes_the_group(w)
        h = w.h(who, "ws-a")
        assert (await w.client.delete("/api/repos/groups/plat", headers=h)).status_code == 403
        assert (await w.client.request(
            "DELETE", "/api/repos/groups/plat/repos", json={"repos": [STORED]},
            headers=h)).status_code == 403
        assert (await w.client.post(
            "/api/repos/groups/plat/repos", json={"repos": [STORED]},
            headers=h)).status_code == 403
        assert (await w.client.post(
            "/api/repos/groups", json={"name": "mine", "repos": []}, headers=h)).status_code == 403
        listed = await w.client.get("/api/repos/groups", headers=w.h("admin_a", "ws-a"))
        assert [g["repos"] for g in listed.json()] == [[STORED]]


async def test_an_editor_who_cannot_read_the_members_finds_no_such_group(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=(_router(),)) as w:
        await _admin_makes_the_group(w)
        await _make_editor_without_the_grant(w, "member_a")
        h = w.h("member_a", "ws-a")
        assert (await w.client.delete("/api/repos/groups/plat", headers=h)).status_code == 404
        assert (await w.client.request(
            "DELETE", "/api/repos/groups/plat/repos", json={"repos": [STORED]},
            headers=h)).status_code == 404


async def test_an_editor_with_review_on_every_member_changes_the_group(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=(_router(),)) as w:
        await _admin_makes_the_group(w)
        h = w.h("editor_a", "ws-a")
        # the bare owner/name the UI shows removes the stored provider:owner/name
        r = await w.client.request("DELETE", "/api/repos/groups/plat/repos",
                                   json={"repos": [A_REPO_FULL]}, headers=h)
        assert r.status_code == 200 and r.json()["repos"] == [], r.text
        assert (await w.client.delete("/api/repos/groups/plat", headers=h)).status_code == 204
