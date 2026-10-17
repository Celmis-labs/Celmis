"""A person named on a pack whose account no longer exists is listed, not a 500.

The contract (src/api/routers/codebase.py `MemberOut`, `_email`):

  * a member row whose user id the user store does not know comes back with
    `"email": null` — the list of members is built from every row and a missing
    account must not stop the page from loading;
  * the other members keep their address.
"""

from __future__ import annotations

from src.db.models import KnowledgePackMember
from tests.api.codebase_support import call
from tests.api.rbac_world import world

GHOST = "u-no-such-account"


async def test_a_member_row_for_an_unknown_account_has_a_null_email(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await call(w, "editor_a", "POST", "/api/codebase/packs", json={
            "slug": "mine", "title": "Mine", "audience": "restricted"})
        pid = r.json()["id"]
        async with w.factory() as s:
            s.add(KnowledgePackMember(pack_id=pid, user_id=GHOST, permission="read"))
            await s.commit()

        r = await call(w, "editor_a", "GET", f"/api/codebase/packs/{pid}")
        assert r.status_code == 200, r.text
        members = {m["user_id"]: m for m in r.json()["members"]}
        assert members[GHOST]["email"] is None and members[GHOST]["permission"] == "read"
        assert members[w.uid("editor_a")]["email"] == "editor-a@acme-corp.io"
