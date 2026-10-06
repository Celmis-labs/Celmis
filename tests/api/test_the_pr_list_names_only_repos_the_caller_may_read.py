"""GET /api/pull-requests under default-deny.

A pull request row is what its repository contains: the title, the branch
names, the author, a count of findings. A person who may not read the
repository gets the same answer from the list as from `/{id}/runs` (nothing),
and its name is not in the `repos` facet either. Owners and admins see all.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from tests.api.rbac_world import A_REPO, A_REPO_FULL, world


@pytest.fixture(autouse=True)
def _sync(tmp_path, monkeypatch):
    from src.access import resolver

    eng = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
    monkeypatch.setattr(resolver, "_ENGINE", eng)
    yield
    eng.dispose()


async def _seed(w):
    from src.db.models import ReviewPullRequest

    async with w.factory() as s:
        s.add(ReviewPullRequest(
            id="pr-x", workspace_id=w.ws["ws-a"], provider="github", repo=A_REPO_FULL,
            number=7, repo_slug=A_REPO, title="Rotate the signing key", author="dana",
            reviews_count=1, state="open", head_ref="feat/x", base_ref="main"))
        await s.commit()


@pytest.mark.parametrize("who", ["member_a", "viewer_a"])
async def test_a_person_without_a_grant_gets_an_empty_list(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        await _seed(w)
        runs = await w.client.get("/api/pull-requests/pr-x/runs", headers=w.h(who, "ws-a"))
        assert runs.status_code == 404
        r = await w.client.get("/api/pull-requests", headers=w.h(who, "ws-a"))
        assert r.status_code == 200
        body = r.json()
        assert body["items"] == [] and body["total"] == 0 and body["repos"] == []
        # a filter on the repository name does not reopen it
        r = await w.client.get(f"/api/pull-requests?repo={A_REPO_FULL}&q=signing",
                               headers=w.h(who, "ws-a"))
        assert r.json()["items"] == [] and r.json()["repos"] == []


@pytest.mark.parametrize("who", ["owner_a", "admin_a", "gadmin", "editor_a"])
async def test_owners_admins_and_a_grant_holder_see_the_repository(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        await _seed(w)
        r = await w.client.get("/api/pull-requests", headers=w.h(who, "ws-a"))
        assert r.status_code == 200
        body = r.json()
        assert [i["title"] for i in body["items"]] == ["Rotate the signing key"]
        assert body["total"] == 1 and body["repos"] == [A_REPO_FULL]
