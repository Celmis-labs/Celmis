"""GDPR and the team's memories: the export lists what a person wrote, and the
erasure keeps the knowledge but cuts the link to the person.

A repository removed from the workspace takes its memories with it.
"""

from __future__ import annotations

import json

from tests.api.rbac_world import A_REPO, world


def _routers() -> tuple:
    from src.api.routers import gdpr, memories

    return (gdpr.router, memories.router)


async def _seed(w, **kw):
    from src.db.models import ReviewMemory

    async with w.factory() as s:
        row = ReviewMemory(workspace_id=w.ws["ws-a"], status="active", **kw)
        s.add(row)
        await s.commit()
        return row.id


async def test_the_export_lists_the_memories_a_person_wrote(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        email = w.users["editor_a"].email
        mine = await _seed(w, text="Written by the editor.", created_by=email)
        await _seed(w, text="Written by somebody else.", created_by="other@acme-corp.io")
        r = await w.client.get(f"/api/gdpr/export/{w.uid('editor_a')}", headers=w.h("gadmin"))
        assert r.status_code == 200, r.text
        exported = r.json()["review_memories"]
        assert [m["id"] for m in exported] == [mine]
        assert "somebody else" not in json.dumps(r.json())


async def test_an_erased_person_keeps_no_name_on_a_memory_but_the_memory_stays(tmp_path, monkeypatch):
    from src.db.models import ReviewMemory

    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        user = w.users["editor_a"]
        wrote = await _seed(w, text="Written by the editor.", created_by=user.email)
        edited = await _seed(w, text="Edited by the editor.", created_by="x@y.z",
                             updated_by=user.email)
        r = await w.client.delete(f"/api/gdpr/user/{user.id}", headers=w.h("gadmin"))
        assert r.status_code == 200, r.text
        assert r.json()["memories_unlinked"] == 2
        anonymised = f"deleted-{user.id[:12]}@erased.local"
        async with w.factory() as s:
            a, b = await s.get(ReviewMemory, wrote), await s.get(ReviewMemory, edited)
            assert (a.created_by, a.text) == (anonymised, "Written by the editor.")
            assert (b.updated_by, b.created_by) == (anonymised, "x@y.z")


async def test_removing_a_repository_removes_its_memories_and_only_its(tmp_path, monkeypatch):
    from sqlalchemy import select

    from src.db.models import ReviewMemory
    from src.repos.purge import PurgeReport, _purge_postgres

    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        await _seed(w, text="Repo fact.", repo_slug=A_REPO)
        await _seed(w, text="Workspace fact.")
        async with w.factory() as s:
            await _purge_postgres(A_REPO, s, PurgeReport(slug=A_REPO))
        async with w.factory() as s:
            left = [m.text for m in (await s.scalars(select(ReviewMemory))).all()]
        assert left == ["Workspace fact."]
