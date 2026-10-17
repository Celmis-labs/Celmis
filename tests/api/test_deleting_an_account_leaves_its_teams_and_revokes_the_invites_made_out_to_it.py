"""Deleting an account ends what it held in every workspace, by the rules of a removal.

The contract (docs/ROLES_AND_ACCESS.md, docs/CODEBASE.md "What deleting things does"):

  * `DELETE /api/auth/me` takes the person out of the teams of every workspace
    they belonged to (a team row left behind would give its repository and pack
    grants to whoever is later added under the same id) and revokes the live
    invites made out to their address, writing one `invite.revoked_on_removal`
    row per workspace with the invite ids and `via: account_deleted`;
  * the person's names, publish tokens and invite links on packs go the way they
    do for a removal (`tests/api/test_a_member_who_leaves_a_workspace_...`), and
    a restricted pack only they maintained is reported as orphaned;
  * nobody else's team rows or invites are touched, and an invite made out to
    another address, or an open link, stays live.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, func, select

from src.db.models import KnowledgePackToken, TeamMember, WorkspaceInvite
from tests.api.codebase_support import call
from tests.api.rbac_world import world


def _invite(invite_id: str, ws_id: str, email: str | None) -> WorkspaceInvite:
    return WorkspaceInvite(
        id=invite_id, workspace_id=ws_id, token_hash=f"hash-{invite_id}", email=email, role="member",
        max_uses=1, used_count=0, expires_at=datetime.now(UTC) + timedelta(days=3), revoked=False,
        created_by="owner-a@acme-corp.io")


async def _count(w, model, **where) -> int:
    async with w.factory() as s:
        q = select(func.count()).select_from(model)
        for key, value in where.items():
            q = q.where(getattr(model, key) == value)
        return int(await s.scalar(q) or 0)


async def _revoked(w, invite_id: str) -> bool:
    async with w.factory() as s:
        return bool((await s.get(WorkspaceInvite, invite_id)).revoked)


async def test_deleting_an_account_leaves_its_teams_and_revokes_the_invites_made_out_to_it(tmp_path, monkeypatch):
    from src.api.routers import auth

    async with world(tmp_path, monkeypatch, extra_routers=(auth.router,)) as w:
        import src.llm.budget as budget

        sync_engine = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
        monkeypatch.setattr(budget, "_ENGINE", sync_engine)
        # the route imported `record_action` by name, which the world's spy does not reach
        monkeypatch.setattr(auth, "record_action", lambda **kw: w.audit.append(kw))
        me, other = w.uid("admin_a"), w.uid("owner_a")
        assert await _count(w, TeamMember, user_id=me) >= 1                  # in a team of workspace A
        async with w.factory() as s:
            s.add(_invite("inv-mine", w.ws["ws-a"], w.users["admin_a"].email.upper()))   # any letter case
            s.add(_invite("inv-other", w.ws["ws-a"], "someone-else@acme-corp.io"))
            s.add(_invite("inv-open", w.ws["ws-a"], None))
            await s.commit()
        pid = (await call(w, "editor_a", "POST", "/api/codebase/packs",
                          json={"slug": "vault", "title": "Vault", "audience": "restricted"})).json()["id"]
        named = await call(w, "editor_a", "POST", f"/api/codebase/packs/{pid}/members",
                           json={"email": w.users["admin_a"].email, "permission": "maintain"})
        assert named.status_code == 200, named.text
        token = (await call(w, "admin_a", "POST", f"/api/codebase/packs/{pid}/tokens",
                            json={"label": "ci"})).json()["token"]
        teams_of_other = await _count(w, TeamMember, user_id=other)
        before = len(w.audit)

        r = await w.client.delete("/api/auth/me", headers=w.h("admin_a"))
        assert r.status_code == 204, r.text

        assert await _count(w, TeamMember, user_id=me) == 0
        assert await _count(w, TeamMember, user_id=other) == teams_of_other        # nobody else left a team
        assert await _revoked(w, "inv-mine") is True
        assert await _revoked(w, "inv-other") is False and await _revoked(w, "inv-open") is False
        rows = [a for a in w.audit[before:] if a["action"] == "invite.revoked_on_removal"]
        assert [(a["workspace_id"], a["detail"]) for a in rows] == [
            (w.ws["ws-a"], {"invite_ids": ["inv-mine"], "via": "account_deleted"})]
        assert await _count(w, KnowledgePackToken, created_by_id=me) == 0           # the pack side, as before
        assert (await w.client.get("/api/codebase/publish",
                                   headers={"Authorization": f"Bearer {token}"})).status_code == 401
        sync_engine.dispose()
