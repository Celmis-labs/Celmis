"""Leaving a workspace ends the person's MCP tokens for it, for good.

The contract (docs/MCP_ACCESS.md "Who can use a token", docs/CODEBASE.md
"Lifecycle"):

  * removing a member - by a workspace admin, by the superadmin from the users
    page, several workspaces at once, or by deleting their account - revokes
    every MCP token the person holds for the workspace they left (and for that
    one only), records why (`revoked_by = membership_removed:<who>`) and writes
    one `mcp.token_revoked_on_removal` audit row per workspace;
  * adding the person back, in any role, brings NOTHING back: the membership
    check alone used to refuse the token while the person was out, so the same
    token worked again the day they were re-added, until it expired;
  * a change of role revokes nothing (a token never reads the role);
  * deleting a workspace revokes the tokens bound to it, for everyone, project
    tokens (`cmcp_`, which have no person) included;
  * deleting an account deletes the person's personal workspace, and with it the
    tokens that OTHER members hold for that workspace, and its project tokens;
  * a knowledge-pack publish token (`cpk_`) of the same person goes the same way;
  * a token that carries a SHARE (a repository another workspace lets this one read)
    goes the same way, and the share goes with the token and no further: the owner's
    consent is the owner's, it does not end because one person left, and the place
    the person held on it is free again.

The world is `tests/api/rbac_world.py`: real routers, real resolver, SQLite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from tests.api.codebase_support import call, note_zip, upload_zip
from tests.api.rbac_world import _sqlite_booleans, world
from tests.api.sharing_world import add_share

AUDIT = "mcp.token_revoked_on_removal"


@pytest.fixture
async def tokens(tmp_path, monkeypatch):
    from src.access import resolver
    from src.config import get_settings
    from src.mcp_server import token_store

    monkeypatch.setenv("MCP_JWT_SECRET", "test-secret-long-enough-for-hs256-aaaaaaaaaaaa")
    from src.api.routers import auth

    async with world(tmp_path, monkeypatch, extra_routers=(auth.router,)) as w:
        import src.llm.budget as budget

        engine = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
        event.listen(engine, "connect", _sqlite_booleans)
        monkeypatch.setattr(resolver, "_ENGINE", engine)
        monkeypatch.setattr(budget, "_ENGINE", engine)
        # the account route imported `record_action` by name, which the world's spy does not reach
        monkeypatch.setattr(auth, "record_action", lambda **kw: w.audit.append(kw))
        token_store.invalidate()
        w.engine = engine
        try:
            yield w
        finally:
            engine.dispose()
            token_store.invalidate()
            get_settings.cache_clear()


def _issue(w, who: str, ws: str, kind: str = "pat", **over) -> str:
    """A live token row for `who` in workspace slug `ws`; returns its id."""
    from src.mcp_server import token_store

    with Session(w.engine) as s:
        row = token_store.create_row(
            s, kind=kind, workspace_id=w.ws[ws], user_id=w.uid(who), issued_by="su",
            label=f"{kind}-{who}-{ws}", patterns=["*"], profile="dev", **over)
        return row.id


def _row(w, token_id: str):
    from src.db.models import McpToken

    with Session(w.engine) as s:
        row = s.get(McpToken, token_id)
        s.expunge(row)
        return row


def _live(w, token_id: str) -> bool:
    """Is the token usable now, as the verifier decides it (through the cache)?"""
    from src.mcp_server import token_store

    view = token_store.lookup(token_id)
    return view is not None and view.problem() is None


def _audit(w) -> list[dict]:
    return [a for a in w.audit if a.get("action") == AUDIT]


def _project_token(w, ws: str) -> str:
    """A live project token (`cmcp_`) of a project in workspace slug `ws`; returns its id."""
    from src.db.models import Project
    from src.mcp_server import project_tokens

    with Session(w.engine) as s:
        project = Project(workspace_id=w.ws[ws], name=f"Platform {ws}")
        s.add(project)
        s.flush()
        _raw, view = project_tokens.mint(
            s, workspace_id=w.ws[ws], project_id=project.id, label="agent",
            ttl_seconds=None, created_by="su")
        s.commit()
        return view.id


def _project_live(token_id: str) -> bool:
    """Would the verifier accept this project token now (it reads the row on every call)?"""
    from src.mcp_server import project_tokens

    view = project_tokens.lookup_id(token_id)
    return view is not None and view.problem() == ""


def _project_revoked_at(w, token_id: str):
    from src.db.models import McpProjectToken

    with Session(w.engine) as s:
        return s.get(McpProjectToken, token_id).revoked_at


def _personal_workspace(w, owner: str, *others: str) -> str:
    """The personal workspace the account deletion looks for (`u-<id>`), with `owner` and
    `others` as members. Registered in `w.ws` under "personal"; returns the id."""
    from src.api.workspace_provision import personal_slug
    from src.db.models import Workspace, WorkspaceMember

    ws_id = "wsid-personal"
    with Session(w.engine) as s:
        s.add(Workspace(id=ws_id, name="Personal", slug=personal_slug(w.uid(owner)), description=""))
        s.flush()
        s.add(WorkspaceMember(workspace_id=ws_id, user_id=w.uid(owner), role="owner"))
        for other in others:
            s.add(WorkspaceMember(workspace_id=ws_id, user_id=w.uid(other), role="member"))
        s.commit()
    w.ws["personal"] = ws_id
    return ws_id


async def _remove(w, who: str, member: str, ws: str = "ws-a") -> None:
    r = await call(w, who, "DELETE", f"/api/workspaces/{w.ws[ws]}/members/{w.uid(member)}", None)
    assert r.status_code == 204, r.text


async def _add(w, who: str, member: str, role: str, ws: str = "ws-a") -> None:
    r = await call(w, who, "PUT", f"/api/workspaces/{w.ws[ws]}/members/{w.uid(member)}",
                   None, json={"role": role})
    assert r.status_code == 200, r.text


# ─── an admin removes a member ───────────────────────────────────────


async def test_removing_a_member_revokes_their_tokens_for_that_workspace_only(tokens):
    w = tokens
    mine = [_issue(w, "both", "ws-a", "pat"), _issue(w, "both", "ws-a", "self"),
            _issue(w, "both", "ws-a", "oauth_grant")]
    elsewhere = _issue(w, "both", "ws-b", "pat")
    bystander = _issue(w, "viewer_a", "ws-a", "pat")
    assert all(_live(w, t) for t in [*mine, elsewhere, bystander])     # and now cached

    await _remove(w, "su", "both", "ws-a")

    for token_id in mine:
        row = _row(w, token_id)
        assert row.revoked_at is not None
        assert row.revoked_by == f"membership_removed:{w.uid('su')}"
        assert not _live(w, token_id), "the cached row was forgotten, not left to expire"
    assert _live(w, elsewhere), "the same person's token for another workspace is theirs still"
    assert _live(w, bystander), "nobody else's token is touched"
    [entry] = _audit(w)
    assert entry["workspace_id"] == w.ws["ws-a"] and entry["target"] == w.uid("both")
    assert sorted(entry["detail"]["token_ids"]) == sorted(mine)
    assert entry["detail"]["via"] == "workspace_members" and entry["actor_id"] == w.uid("su")


@pytest.mark.parametrize("role", ["viewer", "member", "admin"])
async def test_adding_the_person_back_does_not_bring_a_token_back(tokens, role):
    from src.mcp_server import identity

    w = tokens
    token_id = _issue(w, "member_a", "ws-a", "pat")
    assert _live(w, token_id)

    await _remove(w, "su", "member_a")
    assert identity.token_workspace_problem(w.uid("member_a"), False, w.ws["ws-a"]) is not None

    await _add(w, "su", "member_a", role)
    # The membership check is satisfied again - the ROW is what keeps the token dead.
    assert identity.token_workspace_problem(w.uid("member_a"), False, w.ws["ws-a"]) is None
    assert not _live(w, token_id)
    assert _row(w, token_id).revoked_at is not None


async def test_a_change_of_role_revokes_nothing(tokens):
    w = tokens
    token_id = _issue(w, "member_a", "ws-a", "pat")

    await _add(w, "su", "member_a", "viewer")
    await _add(w, "su", "member_a", "admin")

    assert _live(w, token_id) and _row(w, token_id).revoked_at is None
    assert _audit(w) == []


async def test_an_expired_token_is_left_as_it_is_and_not_audited(tokens):
    w = tokens
    token_id = _issue(w, "member_a", "ws-a", "pat")
    from src.db.models import McpToken

    with Session(w.engine) as s:
        s.get(McpToken, token_id).expires_at = datetime.now(UTC) - timedelta(days=1)
        s.commit()

    await _remove(w, "su", "member_a")

    assert _row(w, token_id).revoked_at is None
    assert _audit(w) == []


# ─── every other way to remove a membership ──────────────────────────


async def test_the_superadmin_removing_a_membership_from_the_users_page_revokes_too(tokens):
    w = tokens
    token_id = _issue(w, "member_a", "ws-a", "cli")

    r = await w.client.delete(
        f"/api/admin/users/{w.uid('member_a')}/memberships/{w.ws['ws-a']}",
        headers=w.h("su"))
    assert r.status_code == 200, r.text

    assert not _live(w, token_id)
    assert _row(w, token_id).revoked_by == f"membership_removed:{w.uid('su')}"
    assert [a["detail"]["via"] for a in _audit(w)] == ["admin_users"]


async def test_a_removal_from_several_workspaces_at_once_revokes_in_each(tokens):
    from src.api.memberships import change_memberships

    w = tokens
    a, b = _issue(w, "both", "ws-a"), _issue(w, "both", "ws-b")
    keep = _issue(w, "member_b", "ws-b")

    async with w.factory() as s:
        await change_memberships(
            s, actor=w.users["su"], user_id=w.uid("both"),
            changes=[(w.ws["ws-a"], None), (w.ws["ws-b"], None)], via="access_request")

    assert not _live(w, a) and not _live(w, b) and _live(w, keep)
    assert sorted(x["workspace_id"] for x in _audit(w)) == sorted([w.ws["ws-a"], w.ws["ws-b"]])


async def test_removing_somebody_who_is_not_a_member_revokes_nothing(tokens):
    w = tokens
    token_id = _issue(w, "viewer_a", "ws-a")

    await _remove(w, "su", "loner")          # not in the workspace: a no-op

    assert _live(w, token_id) and _audit(w) == []


async def test_deleting_an_account_revokes_every_token_they_hold(tokens):
    w = tokens
    a, b = _issue(w, "admin_a", "ws-a", "pat"), _issue(w, "admin_a", "ws-a", "self")
    other = _issue(w, "owner_a", "ws-a", "pat")
    before = len(w.audit)
    assert all(_live(w, t) for t in (a, b, other))       # resolved once: the rows are now cached

    r = await w.client.delete("/api/auth/me", headers=w.h("admin_a"))
    assert r.status_code == 204, r.text

    # The very next resolve on this worker, not 30 s later when the cache entry expires.
    assert not _live(w, a) and not _live(w, b) and _live(w, other)
    assert _row(w, a).revoked_by == f"account_deleted:{w.uid('admin_a')}"
    rows = [x for x in w.audit[before:] if x.get("action") == AUDIT]
    assert [(x["workspace_id"], x["detail"]["via"]) for x in rows] == [
        (w.ws["ws-a"], "account_deleted")]
    assert sorted(rows[0]["detail"]["token_ids"]) == sorted([a, b])


async def test_deleting_a_workspace_revokes_the_tokens_bound_to_it_for_everyone(tokens):
    w = tokens
    inside = [_issue(w, "member_b", "ws-b"), _issue(w, "admin_b", "ws-b", "self")]
    other_workspace = _issue(w, "member_a", "ws-a")
    assert all(_live(w, t) for t in (*inside, other_workspace))      # resolved once: now cached

    r = await w.client.delete(f"/api/workspaces/{w.ws['ws-b']}", headers=w.h("su"))
    assert r.status_code == 204, r.text

    for token_id in inside:
        assert not _live(w, token_id), "the cached row was forgotten, not left to expire"
        assert _row(w, token_id).revoked_by == f"workspace_deleted:{w.uid('su')}"
    assert _live(w, other_workspace)
    [entry] = _audit(w)
    assert entry["workspace_id"] == w.ws["ws-b"] and entry["detail"]["via"] == "workspace_deleted"
    assert sorted(entry["detail"]["token_ids"]) == sorted(inside)


async def test_deleting_a_workspace_revokes_its_project_tokens_too(tokens):
    """A `cmcp_` token has no person, so no membership change ends it; the workspace going must."""
    w = tokens
    inside = _project_token(w, "ws-b")
    elsewhere = _project_token(w, "ws-a")
    person = _issue(w, "member_b", "ws-b")
    assert _project_live(inside) and _project_live(elsewhere)
    assert _live(w, person)                                          # resolved once: now cached

    r = await w.client.delete(f"/api/workspaces/{w.ws['ws-b']}", headers=w.h("su"))
    assert r.status_code == 204, r.text

    assert not _project_live(inside) and _project_revoked_at(w, inside) is not None
    assert _project_live(elsewhere), "another workspace's project token is not touched"
    assert not _live(w, person)
    [entry] = _audit(w)
    assert entry["workspace_id"] == w.ws["ws-b"] and entry["detail"]["via"] == "workspace_deleted"
    assert entry["detail"]["token_ids"] == [person]
    assert entry["detail"]["project_token_ids"] == [inside]


async def test_a_workspace_with_only_a_project_token_is_still_audited(tokens):
    w = tokens
    only = _project_token(w, "ws-b")

    r = await w.client.delete(f"/api/workspaces/{w.ws['ws-b']}", headers=w.h("su"))
    assert r.status_code == 204, r.text

    assert not _project_live(only)
    [entry] = _audit(w)
    assert entry["detail"]["project_token_ids"] == [only] and entry["detail"]["token_ids"] == []


async def test_an_expired_project_token_is_left_as_it_is_when_the_workspace_goes(tokens):
    from src.db.models import McpProjectToken

    w = tokens
    old = _project_token(w, "ws-b")
    with Session(w.engine) as s:
        s.get(McpProjectToken, old).expires_at = datetime.now(UTC) - timedelta(days=1)
        s.commit()

    r = await w.client.delete(f"/api/workspaces/{w.ws['ws-b']}", headers=w.h("su"))
    assert r.status_code == 204, r.text

    assert _project_revoked_at(w, old) is None and _audit(w) == []


async def test_deleting_an_account_revokes_what_others_hold_in_the_personal_workspace_it_deletes(tokens):
    """The person's own tokens went already; the other members' tokens for the same workspace,
    and its project tokens, stayed live rows of a workspace that no longer exists."""
    w = tokens
    personal = _personal_workspace(w, "admin_a", "viewer_a")
    mine = _issue(w, "admin_a", "personal", "pat")
    theirs = [_issue(w, "viewer_a", "personal", "pat"), _issue(w, "viewer_a", "personal", "self")]
    project = _project_token(w, "personal")
    their_other_workspace = _issue(w, "viewer_a", "ws-a", "pat")
    bystander = _issue(w, "owner_a", "ws-a", "pat")
    before = len(w.audit)
    assert all(_live(w, t) for t in (mine, *theirs, their_other_workspace, bystander))   # now cached

    r = await w.client.delete("/api/auth/me", headers=w.h("admin_a"))
    assert r.status_code == 204, r.text

    assert not _live(w, mine)
    assert _row(w, mine).revoked_by == f"account_deleted:{w.uid('admin_a')}"
    for token_id in theirs:
        assert not _live(w, token_id), "another member's token for the deleted workspace"
        assert _row(w, token_id).revoked_by == f"workspace_deleted:{w.uid('admin_a')}"
    assert not _project_live(project) and _project_revoked_at(w, project) is not None
    assert _live(w, their_other_workspace), "their token for a workspace that still exists is theirs"
    assert _live(w, bystander)
    rows = [x for x in w.audit[before:] if x.get("action") == AUDIT and x["workspace_id"] == personal]
    [entry] = rows
    assert entry["detail"]["via"] == "account_deleted"
    assert sorted(entry["detail"]["token_ids"]) == sorted([mine, *theirs])
    assert entry["detail"]["project_token_ids"] == [project]


# ─── the pack publish token is treated the same ──────────────────────


async def test_a_pack_publish_token_and_an_mcp_token_leave_together(tokens):
    w = tokens
    r = await call(w, "editor_a", "POST", "/api/codebase/packs", json={
        "slug": "open-notes", "title": "Notes", "description": "d",
        "audience": "workspace", "teams": []})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert (await upload_zip(w, "editor_a", pid, note_zip("open-notes"))).status_code == 201
    r = await call(w, "admin2_a", "POST", f"/api/codebase/packs/{pid}/tokens",
                   json={"label": "ci", "expires_in_days": 30})
    assert r.status_code == 201, r.text
    publish = {"Authorization": f"Bearer {r.json()['token']}"}
    mcp = _issue(w, "admin2_a", "ws-a")
    assert (await w.client.get("/api/codebase/publish", headers=publish)).status_code == 200

    await _remove(w, "su", "admin2_a")
    await _add(w, "su", "admin2_a", "admin")

    assert (await w.client.get("/api/codebase/publish", headers=publish)).status_code == 401
    assert not _live(w, mcp)


# ─── the store function ──────────────────────────────────────────────


def test_revoke_held_needs_a_person_or_workspaces_and_never_everything(tokens):
    from src.mcp_server import token_store

    with Session(tokens.engine) as s, pytest.raises(ValueError):
        token_store.revoke_held(s, by="x")
    with Session(tokens.engine) as s:
        assert token_store.revoke_held(s, by="x", workspace_ids=[]) == {}


def test_revoke_held_stages_the_change_for_the_callers_transaction(tokens):
    from src.mcp_server import token_store

    w = tokens
    token_id = _issue(w, "member_a", "ws-a")
    with Session(w.engine) as s:
        out = token_store.revoke_held(
            s, by="membership_removed:x", user_id=w.uid("member_a"),
            workspace_ids=[w.ws["ws-a"]])
        assert out == {w.ws["ws-a"]: [token_id]}
        s.rollback()                              # the caller's transaction failed
    assert _row(w, token_id).revoked_at is None


# ─── a token that carries a share ────────────────────────────────────


def _holders(w, share_id: str) -> dict[str, list[str]]:
    from src.access import shares

    with Session(w.engine) as s:
        return shares.holders(s, share_id)


async def test_a_person_who_leaves_takes_a_share_off_their_token_and_nothing_off_the_share(tokens):
    from src.access import shares

    w = tokens
    share_id = add_share(w, people=1)
    token_id = _issue(w, "member_a", "ws-a", "pat", shared=[share_id])
    assert _live(w, token_id) and list(_holders(w, share_id)) == [w.uid("member_a")]
    with Session(w.engine) as s:
        assert not shares.has_room(s, share_id, w.uid("viewer_a")), "the one place is taken"

    await _remove(w, "su", "member_a")

    assert not _live(w, token_id)
    assert _row(w, token_id).revoked_by == f"membership_removed:{w.uid('su')}"
    assert _holders(w, share_id) == {}, "a revoked token holds no place"
    with Session(w.engine) as s:
        assert shares.has_room(s, share_id, w.uid("viewer_a")), "and the place is free again"
    from tests.api.sharing_world import share_row

    assert share_row(w, share_id).revoked_at is None, "the owner's consent is untouched"


@pytest.mark.parametrize("role", ["viewer", "member", "admin"])
async def test_adding_the_person_back_does_not_bring_the_share_back(tokens, role):
    from src.access import shares

    w = tokens
    share_id = add_share(w)
    token_id = _issue(w, "member_a", "ws-a", "pat", shared=[share_id])
    await _remove(w, "su", "member_a")
    await _add(w, "su", "member_a", role)

    assert not _live(w, token_id)
    assert _holders(w, share_id) == {}
    with Session(w.engine) as s:
        # The share could be put on a NEW token for them (that is the issuer's call);
        # what must not happen is that the old token reads it again.
        assert shares.check_attachable(
            s, [share_id], workspace_id=w.ws["ws-a"], user_id=w.uid("member_a")) == [share_id]


async def test_a_token_with_a_share_is_left_alone_when_somebody_else_leaves(tokens):
    w = tokens
    share_id = add_share(w)
    mine = _issue(w, "member_a", "ws-a", "pat", shared=[share_id])
    theirs = _issue(w, "viewer_a", "ws-a", "pat", shared=[share_id])

    await _remove(w, "su", "viewer_a")

    assert _live(w, mine) and not _live(w, theirs)
    assert list(_holders(w, share_id)) == [w.uid("member_a")]


async def test_deleting_the_receiving_workspace_revokes_the_token_and_ends_the_share(tokens):
    from tests.api.sharing_world import share_row

    w = tokens
    share_id = add_share(w)
    token_id = _issue(w, "member_a", "ws-a", "pat", shared=[share_id])
    assert _live(w, token_id)

    r = await w.client.delete(f"/api/workspaces/{w.ws['ws-a']}", headers=w.h("su"))
    assert r.status_code == 204, r.text

    assert not _live(w, token_id)
    row = share_row(w, share_id)
    assert row.revoked_at is not None and row.revoke_reason == "workspace_deleted"
