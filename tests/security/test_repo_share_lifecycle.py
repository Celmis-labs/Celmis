"""A share ends when what it shares goes: the repository, or either workspace.

The check on every call (the repository is still registered under the same name,
both workspaces exist) would end a share on its own. It is the SECOND lock. The
first is explicit: removing a repository, purging it or deleting a workspace ENDS
the shares, with a row that says so, in the same step. Without that, registering
the same repository again, or a workspace coming back under the same id, would
bring a consent back that nobody gave twice.

World: `tests/api/sharing_world.py` (B gives A one repository).
"""

from __future__ import annotations

import pytest

from tests.api.rbac_world import A_REPO, A_REPO_FULL, B_REPO, B_REPO_FULL
from tests.api.sharing_world import (
    HOLDER,
    WS_A,
    WS_B,
    add_fresh_share,
    add_share,
    audit_rows,
    register_b,
    share_row,
)


@pytest.fixture(autouse=True)
def _no_vector_store(monkeypatch):
    """A purge also empties the vector store; there is none in a test."""
    monkeypatch.setattr("src.repos.purge._purge_qdrant", lambda *a, **k: None)
    monkeypatch.setattr("src.review.learning.similarity.delete_repo_vectors",
                        lambda *a, **k: None)


async def _remove(w, who, slug, *, purge=False, ws="ws-b"):
    return await w.client.delete(f"/api/repos/{slug}", params={"purge": "true"} if purge else None,
                                 headers=w.h(who, ws))


def _ended(w, share_id):
    row = share_row(w, share_id)
    return row.revoked_at is not None, row.revoke_reason, row.revoked_by


def _can_still_be_put_on_a_token(w, share_id) -> bool:
    from sqlalchemy.orm import Session

    from src.access import shares

    with Session(w.engine) as s:
        try:
            shares.check_attachable(s, [share_id], workspace_id=WS_A, user_id=w.uid(HOLDER))
        except shares.ShareError:
            return False
        return True


# ─── the repository goes ─────────────────────────────────────────────


@pytest.mark.parametrize("purge", [False, True], ids=["unregister", "purge"])
async def test_removing_a_repository_ends_every_share_of_it(sw, purge):
    to_a = add_share(sw)
    to_c = _share_to_third_workspace(sw)
    r = await _remove(sw, "admin_b", B_REPO, purge=purge)
    assert r.status_code in (200, 204), r.text
    for share_id in (to_a, to_c):
        assert _ended(sw, share_id) == (True, "repo_removed", sw.uid("admin_b"))
    rows = audit_rows(sw, "sharing.auto_revoked")
    assert sorted(x["target"] for x in rows) == sorted([to_a, to_c])
    assert {x["workspace_id"] for x in rows} == {WS_B}, "the owner's trail, nobody else's"
    assert all(x["detail"] == {"reason": "repo_removed"} for x in rows)
    if purge:
        assert r.json()["shares_revoked"] == 2


def _share_to_third_workspace(w) -> str:
    from sqlalchemy.orm import Session

    from src.db.models import Workspace

    with Session(w.engine) as s:
        s.add(Workspace(id="wsid-c", name="Charlie", slug="ws-c", description=""))
        s.commit()
    return add_share(w, grantee="wsid-c")


@pytest.mark.parametrize("purge", [False, True], ids=["unregister", "purge"])
async def test_removing_one_repository_leaves_the_shares_of_the_others(sw, purge):
    gone, kept = add_share(sw), add_fresh_share(sw)
    received = add_share(sw, owner=WS_A, grantee=WS_B, slug=A_REPO, full_name=A_REPO_FULL)
    await _remove(sw, "admin_b", B_REPO, purge=purge)
    assert _ended(sw, gone)[0] is True
    assert _ended(sw, kept)[0] is False, "another repository of the same owner"
    assert _ended(sw, received)[0] is False, "B removing its repository is not A removing its own"


async def test_a_share_that_had_already_ended_keeps_its_own_reason_and_writes_no_row(sw):
    share_id = add_share(sw, revoked=True)
    await _remove(sw, "admin_b", B_REPO)
    assert _ended(sw, share_id)[1] == "owner"
    assert audit_rows(sw, "sharing.auto_revoked") == []


async def test_a_repository_nobody_shared_is_removed_as_before(sw):
    r = await _remove(sw, "admin_b", B_REPO)
    assert r.status_code == 204
    assert audit_rows(sw, "sharing.auto_revoked") == []


@pytest.mark.parametrize("purge", [False, True], ids=["unregister", "purge"])
async def test_registering_the_same_repository_again_brings_nothing_back(sw, purge):
    share_id = add_share(sw)
    assert _can_still_be_put_on_a_token(sw, share_id)
    await _remove(sw, "admin_b", B_REPO, purge=purge)
    register_b(sw, B_REPO, B_REPO_FULL)               # the very same name, the very same slug
    assert not _can_still_be_put_on_a_token(sw, share_id)
    from tests.api.test_sharing_token_issuance import _issue

    r = await _issue(sw, [share_id], repos=[A_REPO])
    assert r.status_code == 422 and r.json()["detail"] == "No such share."
    picker = await sw.client.get("/api/admin/mcp-shares", params={"workspace": "ws-a"},
                                 headers=sw.h("su", "ws-a"))
    assert picker.json() == []


async def test_a_token_that_carried_the_share_reads_nothing_through_it_after_the_repository_is_re_added(sw):
    from sqlalchemy.orm import Session

    from src.access import shares
    from src.mcp_server import token_store

    share_id = add_share(sw)
    _token, view = token_store.mint(
        kind="pat", workspace_id=WS_A, user_id=sw.uid(HOLDER), issued_by="master-admin",
        label="t", patterns=[A_REPO], profile="dev", expires_in_days=30, shared=[share_id])
    await _remove(sw, "admin_b", B_REPO)
    register_b(sw, B_REPO, B_REPO_FULL)
    with Session(sw.engine) as s:
        held = shares.live_grants(s, share_ids=list(token_store.lookup(view.id).shares),
                                  workspace_id=WS_A, user_id=sw.uid(HOLDER))
    assert held == {}


async def test_a_purge_with_no_workspace_ends_the_shares_of_every_owner(sw):
    """The command line purges a slug for everybody: the clone is shared by all."""
    from src.db import async_session
    from src.repos.purge import purge_repo

    to_a, to_c = add_share(sw), _share_to_third_workspace(sw)
    async with async_session() as session:
        report = await purge_repo(B_REPO, session=session)
    assert report.errors == [] or all("shares" not in e for e in report.errors)
    assert report.shares_revoked == 2
    assert _ended(sw, to_a)[:2] == _ended(sw, to_c)[:2] == (True, "repo_removed")


async def test_a_purge_for_one_workspace_ends_only_that_workspaces_shares(sw):
    from src.db import async_session
    from src.repos.purge import purge_repo

    mine = add_share(sw)
    elsewhere = add_share(sw, owner=WS_A, grantee=WS_B, slug=B_REPO, full_name=B_REPO_FULL)
    async with async_session() as session:
        report = await purge_repo(B_REPO, session=session, workspace_id=WS_B)
    assert report.shares_revoked == 1
    assert _ended(sw, mine)[0] is True and _ended(sw, elsewhere)[0] is False


async def test_a_purge_that_cannot_end_the_shares_says_so_and_still_runs(sw, monkeypatch):
    """The purge never raises: a step that fails is in the report, the rest run."""
    from src.db import async_session
    from src.repos import purge as purge_mod

    async def broken(*_a, **_k):
        raise RuntimeError("the database said no")

    monkeypatch.setattr(purge_mod, "_end_shares", broken)
    async with async_session() as session:
        report = await purge_mod.purge_repo(B_REPO, session=session, workspace_id=WS_B)
    assert any(e.startswith("shares:") for e in report.errors)


# ─── a workspace goes ────────────────────────────────────────────────


async def test_deleting_the_receiving_workspace_ends_what_it_received_and_tells_the_owner(sw):
    received = add_share(sw)
    r = await sw.client.delete(f"/api/workspaces/{WS_A}", headers=sw.h("owner_a"))
    assert r.status_code == 204, r.text
    assert _ended(sw, received) == (True, "workspace_deleted", sw.uid("owner_a"))
    (row,) = audit_rows(sw, "sharing.auto_revoked")
    assert row["workspace_id"] == WS_B and row["target"] == received
    assert row["detail"] == {"reason": "workspace_deleted"}


async def test_deleting_the_owning_workspace_ends_what_it_gives_and_what_it_receives(sw):
    gives = add_share(sw)
    gets = add_share(sw, owner=WS_A, grantee=WS_B, slug=A_REPO, full_name=A_REPO_FULL)
    other = add_fresh_share(sw)                       # also B's, also given
    r = await sw.client.delete(f"/api/workspaces/{WS_B}", headers=sw.h("su"))
    assert r.status_code == 204, r.text
    for share_id in (gives, gets, other):
        assert _ended(sw, share_id)[:2] == (True, "workspace_deleted")
    rows = audit_rows(sw, "sharing.auto_revoked")
    owners = {x["target"]: x["workspace_id"] for x in rows}
    assert owners == {gives: WS_B, other: WS_B, gets: WS_A}, "each in its OWNER's trail"


async def test_a_workspace_that_shares_nothing_is_deleted_as_before(sw):
    r = await sw.client.delete(f"/api/workspaces/{WS_A}", headers=sw.h("owner_a"))
    assert r.status_code == 204
    assert audit_rows(sw, "sharing.auto_revoked") == []


async def test_deleting_an_account_ends_the_shares_of_the_personal_workspace_it_deletes(sw):
    from sqlalchemy.orm import Session

    from src.api.workspace_provision import personal_slug
    from src.db.models import Workspace, WorkspaceMember

    with Session(sw.engine) as s:
        s.add(Workspace(id="wsid-personal", name="Personal", slug=personal_slug(sw.uid("admin_a")),
                        description=""))
        s.flush()
        s.add(WorkspaceMember(workspace_id="wsid-personal", user_id=sw.uid("admin_a"),
                              role="owner"))
        s.commit()
    gives = add_share(sw, owner="wsid-personal", grantee=WS_A, slug="github_pers-repo",
                      full_name="pers/repo")
    r = await sw.client.delete("/api/auth/me", headers=sw.h("admin_a"))
    assert r.status_code == 204, r.text
    assert _ended(sw, gives) == (True, "workspace_deleted", sw.uid("admin_a"))
    (row,) = audit_rows(sw, "sharing.auto_revoked")
    assert row["workspace_id"] == "wsid-personal" and row["target"] == gives


async def test_a_share_that_was_ended_stays_as_history(sw):
    """Nothing deletes the row: who gave what to whom, until when, and why it ended."""
    share_id = add_share(sw)
    await sw.client.delete(f"/api/workspaces/{WS_A}", headers=sw.h("owner_a"))
    row = share_row(sw, share_id)
    assert (row.owner_workspace_id, row.grantee_workspace_id, row.repo_slug) == (
        WS_B, WS_A, B_REPO)
