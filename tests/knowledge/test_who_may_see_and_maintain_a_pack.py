"""The visibility rule of a knowledge pack, walked over every combination.

The contract (docs/CODEBASE.md, "Audiences"):

  * the superadmin sees and maintains everything;
  * nobody without a CURRENT role in the workspace sees anything — except a
    global admin, who sees `workspace` and `teams` packs but never `restricted`
    ones (only the superadmin does);
  * `workspace` — every member; `teams` — owners/admins, granted teams and
    named people; `restricted` — granted teams and named people ONLY;
  * a team grant counts only for somebody who still holds a role, and only for
    a team of the pack's own workspace;
  * a VIEWER reads and never writes: a "maintain" naming or team grant gives them
    nothing, so demoting a maintainer to viewer takes the write right with it;
  * an unknown role or audience grants nothing.

The rule exists twice (pure Python for single packs, SQL for lists); the last
tests hold the two equal over the same cases, and `principal_for` is checked
against real rows, including a person who left the workspace but kept their
team rows.
"""

from __future__ import annotations

import itertools
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import tests.api.rbac_world  # noqa: F401 — registers JSONB → JSON for SQLite
from src.knowledge import visibility as v
from src.users.roles import PACK_AUDIENCES, VALID_WORKSPACE_ROLES

MINE, OTHER = "team-mine", "team-other"
ROLES = (None, *sorted(VALID_WORKSPACE_ROLES), "boss")        # "boss" = unknown
AUDIENCES = (*PACK_AUDIENCES, "everyone")                     # "everyone" = unknown
#: How the pack is granted to the principal's team: not at all, read, maintain.
TEAM_GRANT = (None, "read", "maintain")
#: How the principal is named on the pack.
NAMED = (None, "read", "maintain")


def _principal(*, su=False, ga=False, role=None, in_team=True):
    return v.PackPrincipal(
        user_id="u-me", superadmin=su, global_admin=ga, role=role,
        team_ids=frozenset({MINE}) if in_team else frozenset())


def _grants(team_grant):
    # A grant to somebody else's team is always there: it must never count.
    out = [v.TeamGrant(OTHER, "maintain")]
    if team_grant:
        out.append(v.TeamGrant(MINE, team_grant))
    return out


def _expected(*, su, ga, role, in_team, audience, team_grant, named):
    """The rule, spelled out case by case rather than as the code spells it."""
    known_role = role in VALID_WORKSPACE_ROLES
    if su:
        return True, True
    if audience not in PACK_AUDIENCES:
        return False, False
    if not known_role and not ga:
        return False, False
    on_it = known_role and (named is not None or (in_team and team_grant is not None))
    admin_view = ga or role in ("owner", "admin")
    if audience == "workspace":
        sees = True
    elif audience == "teams":
        sees = admin_view or on_it
    else:
        sees = on_it
    if not sees:
        return False, False
    writer = role in ("owner", "admin", "editor", "member")        # a viewer never writes
    maintains = (
        (writer and named == "maintain")
        or (writer and in_team and team_grant == "maintain")
        or (audience in ("workspace", "teams") and admin_view)
    )
    return True, bool(maintains)


CASES = list(itertools.product(
    (False, True), (False, True), ROLES, (False, True), AUDIENCES, TEAM_GRANT, NAMED))


@pytest.mark.parametrize("su", (False, True))
def test_the_pure_rule_matches_the_spelled_out_table_in_every_case(su):
    checked = 0
    for case_su, ga, role, in_team, audience, team_grant, named in CASES:
        if case_su != su:
            continue
        p = _principal(su=su, ga=ga, role=role, in_team=in_team)
        pack = SimpleNamespace(audience=audience)
        member = v.MemberGrant(named) if named else None
        sees, maintains = _expected(
            su=su, ga=ga, role=role, in_team=in_team, audience=audience,
            team_grant=team_grant, named=named)
        label = (su, ga, role, in_team, audience, team_grant, named)
        assert v.can_see(p, pack, _grants(team_grant), member) is sees, label
        assert v.can_maintain(p, pack, _grants(team_grant), member) is maintains, label
        checked += 1
    assert checked > 1000


def test_nobody_maintains_what_they_cannot_see():
    for _su, ga, role, in_team, audience, team_grant, named in CASES:
        p = _principal(ga=ga, role=role, in_team=in_team)
        pack = SimpleNamespace(audience=audience)
        member = v.MemberGrant(named) if named else None
        grants = _grants(team_grant)
        if not v.can_see(p, pack, grants, member):
            assert not v.can_maintain(p, pack, grants, member)


def test_the_superadmin_sees_a_restricted_pack_nobody_granted_to_them():
    p = _principal(su=True, role=None, in_team=False)
    pack = SimpleNamespace(audience="restricted")
    assert v.can_see(p, pack, [], None) and v.can_maintain(p, pack, [], None)


def test_a_global_admin_does_not_see_a_restricted_pack_unless_named_on_it():
    pack = SimpleNamespace(audience="restricted")
    outsider = _principal(ga=True, role=None, in_team=False)
    assert not v.can_see(outsider, pack, [v.TeamGrant(MINE, "read")], None)
    member = _principal(ga=True, role="member", in_team=True)
    assert not v.can_see(member, pack, [], None)
    assert v.can_see(member, pack, [v.TeamGrant(MINE, "read")], None)
    assert not v.can_maintain(member, pack, [v.TeamGrant(MINE, "read")], None)


def test_a_workspace_owner_does_not_see_a_restricted_pack_unless_named_on_it():
    pack = SimpleNamespace(audience="restricted")
    owner = _principal(role="owner", in_team=False)
    assert not v.can_see(owner, pack, [v.TeamGrant(MINE, "maintain")], None)
    assert v.can_see(owner, pack, [], v.MemberGrant("read"))


def test_a_workspace_owner_sees_and_maintains_a_teams_pack_without_a_grant():
    pack = SimpleNamespace(audience="teams")
    for role in ("owner", "admin"):
        p = _principal(role=role, in_team=False)
        assert v.can_see(p, pack, [], None) and v.can_maintain(p, pack, [], None)
    for role in ("viewer", "member", "editor"):
        p = _principal(role=role, in_team=False)
        assert not v.can_see(p, pack, [], None)


def test_a_team_row_that_outlived_the_membership_grants_nothing():
    """Left the workspace: no role, but the team id is still around."""
    gone = _principal(role=None, in_team=True)
    for audience in ("teams", "restricted"):
        pack = SimpleNamespace(audience=audience)
        assert not v.can_see(gone, pack, [v.TeamGrant(MINE, "maintain")],
                             v.MemberGrant("maintain"))


def test_an_unknown_permission_never_maintains():
    p = _principal(role="member", in_team=True)
    pack = SimpleNamespace(audience="workspace")
    assert not v.can_maintain(p, pack, [v.TeamGrant(MINE, "owner")], None)
    assert not v.can_maintain(p, pack, [], v.MemberGrant("admin"))


def test_the_refusal_says_the_same_thing_whatever_it_is_built_with():
    for args in ((), ("Secret Pack",), ("a", "b")):
        e = v.PackNotAccessible(*args)
        assert str(e) == v.PACK_NOT_FOUND == "pack not found or not accessible"
        assert "Secret" not in str(e) and isinstance(e, LookupError)


# ─── The SQL twin ────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    from src.db.models import Base

    engine = create_engine(f"sqlite:///{tmp_path}/v.db")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _seed(session, ws):
    """One pack per (audience, grant to my team, named) — plus the grants that
    must never count: another team's, and a team of ANOTHER workspace whose id
    I happen to be in."""
    from src.db.models import KnowledgePack, KnowledgePackMember, KnowledgePackTeam, Team

    session.add_all([
        Team(id=MINE, name="mine", workspace_id=ws),
        Team(id=OTHER, name="other", workspace_id=ws),
        Team(id="team-foreign", name="foreign", workspace_id="wsid-elsewhere"),
    ])
    packs = {}
    for audience, team_grant, named in itertools.product(AUDIENCES, TEAM_GRANT, NAMED):
        pid = str(uuid.uuid4())
        session.add(KnowledgePack(
            id=pid, workspace_id=ws, slug=f"p-{len(packs)}", title="t", audience=audience))
        session.add(KnowledgePackTeam(pack_id=pid, team_id=OTHER, permission="maintain"))
        if team_grant:
            session.add(KnowledgePackTeam(pack_id=pid, team_id=MINE, permission=team_grant))
        if named:
            session.add(KnowledgePackMember(pack_id=pid, user_id="u-me", permission=named))
        packs[pid] = (audience, team_grant, named)
    # A pack of another workspace, granted to "my" team id: not this workspace's.
    pid = str(uuid.uuid4())
    session.add(KnowledgePack(id=pid, workspace_id="wsid-elsewhere", slug="x", title="t",
                              audience="workspace"))
    session.commit()
    return packs, pid


@pytest.mark.parametrize("su", (False, True))
def test_the_sql_filter_returns_exactly_what_the_pure_rule_allows(db, su):
    from src.db.models import KnowledgePack

    ws = "wsid-a"
    with Session(db) as s:
        packs, elsewhere = _seed(s, ws)
        compared = 0
        for case_su, ga, role, in_team, *_ in CASES:
            if case_su != su:
                continue
            p = _principal(su=su, ga=ga, role=role, in_team=in_team)
            sql_visible = set(s.scalars(
                v.visible_packs_query(p, ws).with_only_columns(KnowledgePack.id)).all())
            sql_maintain = set(s.scalars(
                v.visible_packs_query(p, ws).where(v.maintain_clause(p))
                .with_only_columns(KnowledgePack.id)).all())
            for pid, (audience, team_grant, named) in packs.items():
                pack = SimpleNamespace(audience=audience)
                grants = _grants(team_grant)
                member = v.MemberGrant(named) if named else None
                label = (ga, role, in_team, audience, team_grant, named)
                assert (pid in sql_visible) is v.can_see(p, pack, grants, member), label
                assert (pid in sql_maintain) is v.can_maintain(p, pack, grants, member), label
                compared += 1
            assert elsewhere not in sql_visible          # another workspace's pack
        assert compared > 10_000


def test_a_grant_to_a_team_that_was_deleted_grants_nothing(db):
    from src.db.models import KnowledgePack, KnowledgePackTeam, Team

    ws = "wsid-a"
    with Session(db) as s:
        s.add(Team(id=MINE, name="mine", workspace_id=ws))
        s.add(KnowledgePack(id=(pid := str(uuid.uuid4())), workspace_id=ws, slug="p",
                            title="t", audience="restricted"))
        s.add(KnowledgePackTeam(pack_id=pid, team_id=MINE, permission="read"))
        s.commit()
        p = _principal(role="member", in_team=True)
        q = v.visible_packs_query(p, ws).with_only_columns(KnowledgePack.id)
        assert s.scalars(q).all() == [pid]
        s.delete(s.get(Team, MINE))
        s.commit()
        assert s.scalars(q).all() == []


def test_a_grant_to_a_team_of_another_workspace_grants_nothing(db):
    from src.db.models import KnowledgePack, KnowledgePackTeam, Team

    with Session(db) as s:
        s.add(Team(id="t-elsewhere", name="e", workspace_id="wsid-b"))
        s.add(KnowledgePack(id=(pid := str(uuid.uuid4())), workspace_id="wsid-a", slug="p",
                            title="t", audience="restricted"))
        s.add(KnowledgePackTeam(pack_id=pid, team_id="t-elsewhere", permission="maintain"))
        s.commit()
        p = v.PackPrincipal("u-me", False, False, "member", frozenset({"t-elsewhere"}))
        q = v.visible_packs_query(p, "wsid-a").with_only_columns(KnowledgePack.id)
        assert s.scalars(q).all() == []


# ─── The principal, from real rows ───────────────────────────────────


@pytest.fixture
def engine_for_sync(tmp_path, monkeypatch):
    """The pooled sync engine `principal_for_sync` reads, over a tmp SQLite."""
    from src.access import resolver
    from src.db.models import Base
    from src.users import User
    from src.users import store as users_mod

    engine = create_engine(f"sqlite:///{tmp_path}/p.db")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(resolver, "_ENGINE", engine)
    store = users_mod.UserStore(tmp_path / "users.db")
    monkeypatch.setattr(users_mod, "_default_store", store)
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", "root@acme-corp.io")
    store.create(User(id="master-admin", email="root@acme-corp.io", name="su", is_admin=True))
    store.create(User(id="u-ga", email="ga@acme-corp.io", name="ga", is_admin=True))
    store.create(User(id="u-member", email="m@acme-corp.io", name="m"))
    store.create(User(id="u-left", email="l@acme-corp.io", name="l"))
    store.create(User(id="u-boss", email="b@acme-corp.io", name="b"))
    yield engine
    engine.dispose()


def _rows(engine):
    from src.db.models import Team, TeamMember, WorkspaceMember

    with Session(engine) as s:
        s.add_all([
            Team(id="t-a", name="ta", workspace_id="wsid-a"),
            Team(id="t-b", name="tb", workspace_id="wsid-b"),
            WorkspaceMember(workspace_id="wsid-a", user_id="u-member", role="member"),
            WorkspaceMember(workspace_id="wsid-b", user_id="u-member", role="member"),
            WorkspaceMember(workspace_id="wsid-a", user_id="u-boss", role="boss"),
            TeamMember(team_id="t-a", user_id="u-member"),
            TeamMember(team_id="t-b", user_id="u-member"),
            # u-left was in the team, then left the workspace; the row stayed.
            TeamMember(team_id="t-a", user_id="u-left"),
            TeamMember(team_id="t-a", user_id="u-boss"),
        ])
        s.commit()


def test_the_principal_counts_only_the_teams_of_this_workspace(engine_for_sync):
    _rows(engine_for_sync)
    p = v.principal_for_sync("u-member", "wsid-a")
    assert p.role == "member" and p.team_ids == frozenset({"t-a"})
    assert v.principal_for_sync("u-member", "wsid-b").team_ids == frozenset({"t-b"})


def test_a_person_who_left_the_workspace_keeps_no_team_and_no_role(engine_for_sync):
    _rows(engine_for_sync)
    p = v.principal_for_sync("u-left", "wsid-a")
    assert p.role is None and p.team_ids == frozenset()


def test_an_unknown_role_string_is_no_role(engine_for_sync):
    _rows(engine_for_sync)
    p = v.principal_for_sync("u-boss", "wsid-a")
    assert p.role is None and p.team_ids == frozenset()


def test_the_superadmin_and_a_global_admin_are_told_apart(engine_for_sync):
    _rows(engine_for_sync)
    su = v.principal_for_sync("master-admin", "wsid-a")
    ga = v.principal_for_sync("u-ga", "wsid-a")
    assert su.superadmin and not su.global_admin
    assert ga.global_admin and not ga.superadmin and ga.role is None


def test_an_unknown_user_or_workspace_builds_a_principal_that_sees_nothing(engine_for_sync):
    _rows(engine_for_sync)
    assert v.principal_for_sync("nobody", "wsid-a") == v.NOBODY
    assert v.principal_for_sync("", "wsid-a") == v.NOBODY
    assert v.principal_for_sync("u-member", "") == v.NOBODY
    stranger = v.principal_for_sync("u-member", "wsid-nowhere")
    assert stranger.role is None and not stranger.superadmin


def test_a_database_that_cannot_be_read_builds_a_principal_that_sees_nothing(
        engine_for_sync, monkeypatch):
    _rows(engine_for_sync)
    from src.access import resolver

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(resolver, "_sync_engine", boom)
    assert v.principal_for_sync("master-admin", "wsid-a") == v.NOBODY


async def test_the_async_principal_reads_the_same_rows(engine_for_sync, tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.users.store import get_user_store

    _rows(engine_for_sync)
    aengine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/p.db")
    factory = async_sessionmaker(aengine, expire_on_commit=False)
    store = get_user_store()
    async with factory() as s:
        member = await v.principal_for(s, store.get_by_id("u-member"), "wsid-a")
        left = await v.principal_for(s, store.get_by_id("u-left"), "wsid-a")
        su = await v.principal_for(s, store.get_by_id("master-admin"), "wsid-a")
        none = await v.principal_for(s, None, "wsid-a")
    await aengine.dispose()
    assert member.role == "member" and member.team_ids == frozenset({"t-a"})
    assert left.role is None and left.team_ids == frozenset()
    assert su.superadmin
    assert none == v.NOBODY


async def test_the_async_principal_of_a_broken_session_sees_nothing():
    class Broken:
        async def get(self, *_a, **_k):
            raise RuntimeError("db down")

    user = SimpleNamespace(id="u-x", is_admin=True, is_active=True)
    assert await v.principal_for(Broken(), user, "wsid-a") == v.NOBODY


async def test_an_inactive_account_sees_nothing_even_as_admin():
    class Quiet:
        async def get(self, *_a, **_k):
            return None

    user = SimpleNamespace(id="u-x", is_admin=True, is_active=False)
    assert await v.principal_for(Quiet(), user, "wsid-a") == v.NOBODY
