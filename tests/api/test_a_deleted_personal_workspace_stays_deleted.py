"""An admin deletes someone's personal workspace: signing in must not bring it back.

Login provisioning (`provision_personal_workspace`) creates the personal workspace
whenever it is missing, so a deletion used to last until the person's next login.
The delete now leaves a marker. While it exists, sign-in leaves the workspace
deleted as long as the person belongs to another workspace; the no-membership
fallback (`ensure_personal_workspace`, used by `deps.current_workspace_id`) must
still create one, because that person may not land in the shared tenant, and it
clears the marker.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from tests.api.rbac_world import _sqlite_booleans, world


@pytest.fixture
async def w(tmp_path, monkeypatch):
    from src.access import resolver
    from src.config import get_settings
    from src.mcp_server import token_store

    monkeypatch.setenv("MCP_JWT_SECRET", "test-secret-long-enough-for-hs256-aaaaaaaaaaaa")
    async with world(tmp_path, monkeypatch) as world_:
        engine = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
        event.listen(engine, "connect", _sqlite_booleans)
        monkeypatch.setattr(resolver, "_ENGINE", engine)
        token_store.invalidate()
        world_.engine = engine
        try:
            yield world_
        finally:
            engine.dispose()
            token_store.invalidate()
            get_settings.cache_clear()


def _make(engine, user_id, **kw):
    from src.api.workspace_provision import ensure_personal_workspace

    with Session(engine) as s:
        return ensure_personal_workspace(s, user_id, f"{user_id}@example.com", "Pat", **kw)


def _exists(engine, ws_id):
    from src.db.models import Workspace

    with Session(engine) as s:
        return s.get(Workspace, ws_id) is not None


def _marked(engine, user_id):
    from src.db.models import DeletedPersonalWorkspace

    with Session(engine) as s:
        return s.get(DeletedPersonalWorkspace, user_id) is not None


async def _delete(w, ws_id):
    r = await w.client.delete(f"/api/workspaces/{ws_id}", headers=w.h("su"))
    assert r.status_code == 204, r.text


async def test_login_does_not_bring_back_a_deleted_personal_workspace(w):
    uid = w.uid("member_a")                     # also a member of workspace A
    ws_id = _make(w.engine, uid)
    assert ws_id and _exists(w.engine, ws_id)
    await _delete(w, ws_id)
    assert _marked(w.engine, uid) and not _exists(w.engine, ws_id)

    assert _make(w.engine, uid, honour_deletion=True) is None      # the login path
    with Session(w.engine) as s:
        from src.db.models import Workspace
        assert s.scalar(select(Workspace).where(Workspace.slug == f"u-{uid}")) is None
    assert _marked(w.engine, uid)


async def test_a_person_with_no_other_workspace_still_gets_one(w):
    uid = "u-nobody"                                  # belongs to no workspace
    first = _make(w.engine, uid)
    await _delete(w, first)
    assert _marked(w.engine, uid)

    again = _make(w.engine, uid, honour_deletion=True)             # login, no membership
    assert again and _exists(w.engine, again)
    assert not _marked(w.engine, uid)


async def test_the_fallback_provisions_and_clears_the_marker(w):
    uid = w.uid("member_a")
    await _delete(w, _make(w.engine, uid))
    again = _make(w.engine, uid)                                   # deps fallback path
    assert again and _exists(w.engine, again)
    assert not _marked(w.engine, uid)


async def test_deleting_a_team_workspace_leaves_no_marker(w):
    from src.db.models import DeletedPersonalWorkspace

    await _delete(w, w.ws["ws-b"])
    with Session(w.engine) as s:
        assert s.scalars(select(DeletedPersonalWorkspace)).all() == []
