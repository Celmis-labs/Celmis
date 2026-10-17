"""A person's erasure finishes even when removing their names from packs fails.

The contract (src/api/routers/gdpr.py, docs/CODEBASE.md "What deleting things
does"):

  * the account row and the credentials are changed by stores that commit on
    their own, and the e-mail the memories are matched by cannot be found again
    afterwards, so the erasure must not stop half-way;
  * the pack cleanup therefore runs in a savepoint: when it fails, everything
    else is still committed, the answer says ``packs_cleaned: false`` and the
    same call can be repeated;
  * when it works the answer says ``packs_cleaned: true`` and no name is left.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select, text

from src.db.models import KnowledgePackMember, ReviewMemory
from tests.api.codebase_support import call, seeded
from tests.api.rbac_world import world


def _routers():
    from src.api.routers import gdpr

    return (gdpr.router,)


async def _named(w, who: str) -> int:
    async with w.factory() as s:
        return int(await s.scalar(select(func.count()).select_from(KnowledgePackMember).where(
            KnowledgePackMember.user_id == w.uid(who))) or 0)


async def _memory(w, created_by: str) -> str:
    async with w.factory() as s:
        row = ReviewMemory(workspace_id=w.ws["ws-a"], text="A fact the team keeps.",
                           created_by=created_by, status="active")
        s.add(row)
        await s.commit()
        return row.id


async def test_a_pack_cleanup_that_fails_leaves_the_rest_of_the_erasure_committed(
        tmp_path, monkeypatch):
    from src.knowledge import store

    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        ids = await seeded(w)
        added = await call(w, "editor_a", "POST", f"/api/codebase/packs/{ids['pr']}/members",
                           json={"email": w.users["member_a"].email, "permission": "read"})
        assert added.status_code == 200, added.text
        user = w.users["member_a"]
        memory = await _memory(w, user.email)
        assert await _named(w, "member_a") == 1

        real = store._erase_user_statements

        def broken(user_id):
            # one statement that works, then one that cannot: the savepoint takes both back
            return [delete(KnowledgePackMember).where(KnowledgePackMember.user_id == user_id),
                    text("DELETE FROM no_such_pack_table")]

        monkeypatch.setattr(store, "_erase_user_statements", broken)
        r = await w.client.delete(f"/api/gdpr/user/{user.id}", headers=w.h("gadmin"))
        assert r.status_code == 200, r.text
        assert r.json()["erased"] is True and r.json()["packs_cleaned"] is False
        assert r.json()["memories_unlinked"] == 1
        assert "no_such_pack_table" not in r.text

        anonymised = f"deleted-{user.id[:12]}@erased.local"
        async with w.factory() as s:
            assert (await s.get(ReviewMemory, memory)).created_by == anonymised
        assert await _named(w, "member_a") == 1                  # the savepoint undid the half it did

        monkeypatch.setattr(store, "_erase_user_statements", real)
        again = await w.client.delete(f"/api/gdpr/user/{user.id}", headers=w.h("gadmin"))
        assert again.status_code == 200, again.text
        assert again.json()["packs_cleaned"] is True
        assert await _named(w, "member_a") == 0


async def test_a_clean_erasure_says_the_pack_names_are_gone(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        ids = await seeded(w)
        added = await call(w, "editor_a", "POST", f"/api/codebase/packs/{ids['pr']}/members",
                           json={"email": w.users["viewer_a"].email, "permission": "read"})
        assert added.status_code == 200, added.text
        r = await w.client.delete(f"/api/gdpr/user/{w.uid('viewer_a')}", headers=w.h("gadmin"))
        assert r.status_code == 200, r.text
        assert r.json()["packs_cleaned"] is True
        assert await _named(w, "viewer_a") == 0
