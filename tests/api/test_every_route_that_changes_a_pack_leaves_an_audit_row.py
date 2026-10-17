"""Every Codebase route that changes something audits it; what a leave ends is audited too.

The contract (docs/CODEBASE.md, "REST"):

  * each `POST`, `PUT`, `PATCH` and `DELETE` route of the Codebase router writes
    a `codebase.<verb>` row, except the two that only read (an invite preview and
    the brief) - a new mutating route that forgets to audit fails here, and an
    exception that no longer exists fails too;
  * the verbs the routes write are the verbs the docs list;
  * naming a person who is already named is `member_changed`, a new name is
    `member_added`, so the trail says which one it was;
  * removing someone from the workspace, or erasing their account, writes a
    `member_removed`, `token_revoked` and `invite_revoked` row for each name,
    publish token and invite link it ended, with the pack as target and ids and
    the reason as detail (an erased account's id is not repeated).
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from src.api.routers import codebase
from tests.api.codebase_support import call
from tests.api.rbac_world import world
from tests.api.test_a_pack_token_publishes_one_pack_and_nothing_else import (
    bearer,
    make_pack,
    make_token,
)

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
#: Routes that use a mutating verb and change nothing.
READS = {("POST", "/invites/preview"), ("POST", "/brief")}
DOCS = Path(__file__).resolve().parents[2] / "docs" / "CODEBASE.md"


def _mutating_routes() -> list[tuple[str, str, object]]:
    out = []
    for route in codebase.router.routes:
        for method in sorted(set(getattr(route, "methods", ())) & MUTATING):
            out.append((method, route.path.removeprefix(codebase.router.prefix), route.endpoint))
    return out


def test_every_mutating_route_writes_an_audit_row_unless_it_only_reads():
    routes = _mutating_routes()
    assert len(routes) >= 14
    silent = [(m, p) for m, p, fn in routes if "_audit(" not in inspect.getsource(fn) and (m, p) not in READS]
    assert silent == [], f"routes that change something and leave no audit row: {silent}"
    present = {(m, p) for m, p, _fn in routes}
    assert present >= READS, f"exceptions that no longer exist: {sorted(READS - present)}"
    for m, p, fn in routes:
        if (m, p) in READS:
            assert "_audit(" not in inspect.getsource(fn), (m, p)


def test_every_verb_the_router_writes_is_listed_in_the_docs():
    verbs = [re.findall(r'"([a-z_]+)"', first)
             for first in re.findall(r"_audit\(([^,]+),", inspect.getsource(codebase))]
    written = {verb for group in verbs for verb in group}
    section = DOCS.read_text().split("Every change is audited as", 1)[1].split("**Who reads these rows.**", 1)[0]
    listed = set(re.findall(r"`([a-z_]+)`", section))
    assert written, "no audit verbs found"
    assert written <= listed, f"verbs missing from the docs: {sorted(written - listed)}"


async def test_naming_a_named_person_again_is_a_member_changed_row_not_a_second_member_added(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await make_pack(w, slug="vault", audience="restricted")
        body = {"email": w.users["member_a"].email, "permission": "read"}
        assert (await call(w, "editor_a", "POST", f"/api/codebase/packs/{pid}/members", json=body)).status_code == 200
        assert (await call(w, "editor_a", "POST", f"/api/codebase/packs/{pid}/members",
                           json={**body, "permission": "maintain"})).status_code == 200
        rows = [(a["action"], a["detail"]["permission"]) for a in w.audit
                if a["action"] in ("codebase.member_added", "codebase.member_changed")]
        assert rows == [("codebase.member_added", "read"), ("codebase.member_changed", "maintain")]


async def test_a_person_removed_from_the_workspace_leaves_a_row_for_each_thing_that_ended(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await make_pack(w, slug="vault", audience="restricted")
        named = await call(w, "editor_a", "POST", f"/api/codebase/packs/{pid}/members",
                           json={"email": w.users["member_a"].email, "permission": "maintain"})
        assert named.status_code == 200
        token, info = await make_token(w, pid, who="member_a")
        invite = await call(w, "member_a", "POST", f"/api/codebase/packs/{pid}/invites", json={})
        assert invite.status_code == 201, invite.text
        before = len(w.audit)

        gone = await call(w, "su", "DELETE", f"/api/workspaces/{w.ws['ws-a']}/members/{w.uid('member_a')}", None)
        assert gone.status_code == 204, gone.text

        rows = {a["action"]: a for a in w.audit[before:] if a["action"].startswith("codebase.")
                and a["action"] != "codebase.pack_orphaned"}
        assert set(rows) == {"codebase.member_removed", "codebase.token_revoked", "codebase.invite_revoked"}
        for row in rows.values():
            assert row["target"] == pid and row["workspace_id"] == w.ws["ws-a"]
            assert row["detail"]["reason"] == "member_left"
            assert row["actor_id"] == w.uid("su")
        assert rows["codebase.member_removed"]["detail"]["user_id"] == w.uid("member_a")
        assert rows["codebase.token_revoked"]["detail"]["token_id"] == info["id"]
        assert rows["codebase.invite_revoked"]["detail"]["invite_id"] == invite.json()["invite"]["id"]
        assert token not in repr(w.audit)
        r = await w.client.get("/api/codebase/publish", headers=bearer(token))
        assert r.status_code == 401


async def test_a_removal_that_ends_nothing_on_any_pack_leaves_no_codebase_row(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await make_pack(w, slug="plain", audience="workspace")
        before = len(w.audit)
        gone = await call(w, "su", "DELETE", f"/api/workspaces/{w.ws['ws-a']}/members/{w.uid('viewer_a')}", None)
        assert gone.status_code == 204, gone.text
        assert [a["action"] for a in w.audit[before:] if a["action"].startswith("codebase.")] == []
