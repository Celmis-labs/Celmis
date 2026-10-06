"""PUT /api/access/rules stores the registry slug, or refuses.

Rules are matched on the registry slug exactly. A rule saved under another
spelling matches nothing, and a deny rule that matches nothing leaves the
repository open to a token scoped to it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from tests.api.rbac_world import A_REPO, A_REPO_FULL, world


def _router():
    from src.api.routers import access

    return access.router


def _body(world_, repo: str) -> dict:
    return {"team_id": world_.ids["team_a"], "repo_slug": repo, "visibility": "code",
            "deny_globs": ["secrets/**"]}


@pytest.mark.parametrize("spelling", [A_REPO, A_REPO_FULL, f"github:{A_REPO_FULL}"])
async def test_every_spelling_of_a_registered_repository_lands_on_its_slug(
        tmp_path, monkeypatch, spelling):
    from src.db.models import RepoAccessRule

    async with world(tmp_path, monkeypatch, extra_routers=(_router(),)) as w:
        r = await w.client.put("/api/access/rules", json=_body(w, spelling),
                               headers=w.h("admin_a", "ws-a"))
        assert r.status_code == 200, r.text
        assert r.json()["repo_slug"] == A_REPO
        async with w.factory() as s:
            rows = (await s.scalars(select(RepoAccessRule))).all()
        assert [x.repo_slug for x in rows] == [A_REPO]


async def test_an_unknown_repository_is_refused_not_stored(tmp_path, monkeypatch):
    from src.db.models import RepoAccessRule

    async with world(tmp_path, monkeypatch, extra_routers=(_router(),)) as w:
        r = await w.client.put("/api/access/rules", json=_body(w, "nobody/nothing"),
                               headers=w.h("admin_a", "ws-a"))
        assert r.status_code == 404
        async with w.factory() as s:
            assert (await s.scalars(select(RepoAccessRule))).all() == []
