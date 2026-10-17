"""A small world for the pack store tests — not a test module.

Two workspaces on one SQLite file, read through the async engine (the store the
router uses) and the sync engine (the one the MCP helpers use), a user store
with a cast, and real `workspace_members` / `teams` / `team_members` rows.

  ws-a (wsid-a)   owner, admin, editor, editor2, member, viewer, and team ``ta``
                  (editor2 and member are in it)
  ws-b (wsid-b)   admin_b, and team ``tb`` (admin_b is in it)
  no workspace    outsider
  global          su (the superadmin), ga (a global admin)
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.api.rbac_world import MASTER_EMAIL, _sqlite_booleans

A, B = "wsid-a", "wsid-b"
TEAM_A, TEAM_B = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"

#: name -> (email, global admin, {workspace id: role})
CAST: dict[str, tuple[str, bool, dict[str, str]]] = {
    "su": (MASTER_EMAIL, True, {}),
    "ga": ("ga@acme-corp.io", True, {}),
    "owner": ("owner@acme-corp.io", False, {A: "owner"}),
    "admin": ("admin@acme-corp.io", False, {A: "admin"}),
    "editor": ("editor@acme-corp.io", False, {A: "editor"}),
    "editor2": ("editor2@acme-corp.io", False, {A: "editor"}),
    "member": ("member@acme-corp.io", False, {A: "member"}),
    "viewer": ("viewer@acme-corp.io", False, {A: "viewer"}),
    "admin_b": ("admin-b@acme-corp.io", False, {B: "admin"}),
    "outsider": ("outsider@acme-corp.io", False, {}),
}
TEAM_MEMBERS = {TEAM_A: ("editor2", "member"), TEAM_B: ("admin_b",)}


@dataclass
class Rig:
    factory: Any
    sync_engine: Any
    users: dict[str, Any]

    def uid(self, who: str) -> str:
        return self.users[who].id

    def principal(self, who: str, ws: str = A):
        from src.knowledge.visibility import principal_for_sync

        return principal_for_sync(self.uid(who), ws)

    @contextlib.asynccontextmanager
    async def session(self):
        async with self.factory() as s:
            yield s

    async def count(self, model, **where) -> int:
        from sqlalchemy import func, select

        async with self.factory() as s:
            q = select(func.count()).select_from(model)
            for key, value in where.items():
                q = q.where(getattr(model, key) == value)
            return int(await s.scalar(q) or 0)


@contextlib.asynccontextmanager
async def rig(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", MASTER_EMAIL)
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "data"))
    db_file = tmp_path / "packs.db"

    import src.users.store as users_mod
    from src.access import resolver
    from src.db.models import Base, Team, TeamMember, Workspace, WorkspaceMember
    from src.users import User

    sync_engine = create_engine(f"sqlite:///{db_file}")
    with sync_engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(sync_engine)
    monkeypatch.setattr(resolver, "_ENGINE", sync_engine)

    store = users_mod.UserStore(tmp_path / "users.db")
    monkeypatch.setattr(users_mod, "_default_store", store)
    users: dict[str, Any] = {}
    for name, (email, is_admin, _ws) in CAST.items():
        user = User(id="master-admin" if name == "su" else f"u-{name}", email=email,
                    name=name, is_admin=is_admin)
        store.create(user)
        users[name] = store.get_by_id(user.id)

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    event.listen(engine.sync_engine, "connect", _sqlite_booleans)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False,
                                 autoflush=False)
    async with factory() as s:
        s.add(Workspace(id=A, name="Alpha", slug="ws-a", description=""))
        s.add(Workspace(id=B, name="Bravo", slug="ws-b", description=""))
        for name, (_e, _adm, memberships) in CAST.items():
            for ws, role in memberships.items():
                s.add(WorkspaceMember(workspace_id=ws, user_id=users[name].id, role=role))
        s.add(Team(id=TEAM_A, name="ta", description="", workspace_id=A))
        s.add(Team(id=TEAM_B, name="tb", description="", workspace_id=B))
        for team, people in TEAM_MEMBERS.items():
            for who in people:
                s.add(TeamMember(team_id=team, user_id=users[who].id, role="member"))
        await s.commit()
    try:
        yield Rig(factory, sync_engine, users)
    finally:
        await engine.dispose()
        sync_engine.dispose()


def upload(*pairs: tuple[str, str]):
    """An ``IngestResult`` from (path, text) pairs, through the real ingest."""
    from src.knowledge.ingest import ingest_files

    return ingest_files([("README.md", b"# pack\n"), *((p, t.encode()) for p, t in pairs)])
