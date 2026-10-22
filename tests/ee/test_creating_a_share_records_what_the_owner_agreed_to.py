# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Creating a share: what is taken from the request, what is checked, what is written.

``POST /api/sharing-grants`` makes one row: ONE repository of the active workspace (the
owner), for ONE other workspace, until a fixed end, with the paths the owner hides, a
ceiling on people, and questions off unless the owner says otherwise. The repository
comes from the owner's own registry and nowhere else, so a share cannot be passed on.
Making a share gives nobody anything by itself: the superadmin still has to name it on
a person's token.

World: `tests/api/sharing_world.py` under a licence that grants ``sharing``
(tests/ee/sharing_support.py). Workspace B owns ``B_REPO``; A is the receiver.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.access import shares as repo_shares
from tests.api.rbac_world import A_REPO, A_REPO_FULL, B_REPO, B_REPO_FULL
from tests.api.sharing_world import (
    HOLDER,
    WS_A,
    WS_B,
    actions,
    add_fresh_share,
    add_share,
    audit_rows,
    register_b,
    share_row,
)
from tests.ee.sharing_support import create

TOKEN_CEILING = 90


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _add_workspace(w, ws_id: str, slug: str, name: str) -> None:
    from src.db.models import Workspace

    with Session(w.engine) as s:
        s.add(Workspace(id=ws_id, name=name, slug=slug, description=""))
        s.commit()


def _rows(w) -> list:
    from sqlalchemy import select

    from src.db.models import RepoShare

    with Session(w.engine) as s:
        rows = s.execute(select(RepoShare).order_by(RepoShare.created_at)).scalars().all()
        s.expunge_all()
        return list(rows)


# ─── what a share is made of ─────────────────────────────────────────


async def test_a_share_made_with_nothing_but_a_repository_and_a_workspace_takes_the_defaults(gw):
    r = await create(gw)

    assert r.status_code == 201, r.text
    body = r.json()
    row = share_row(gw, body["share_id"])
    assert (row.owner_workspace_id, row.grantee_workspace_id) == (WS_B, WS_A)
    assert row.repo_slug == B_REPO
    # The identity of the repository when it was shared, copied from the registry.
    assert (row.repo_provider, row.repo_full_name) == ("github", B_REPO_FULL)
    assert row.allow_ask is False and row.max_people == repo_shares.DEFAULT_PEOPLE == 5
    assert row.deny_globs == [] and row.blocked_user_ids == []
    assert row.revoked_at is None and row.created_by == gw.uid("admin_b")
    ends = _aware(row.expires_at) - _aware(row.created_at)
    assert timedelta(days=30) - timedelta(seconds=5) <= ends <= timedelta(days=30, seconds=5)
    assert body["status"] == "active" and body["slug"] == B_REPO
    assert body["grantee_workspace_id"] == WS_A and body["allow_ask"] is False


async def test_the_answer_has_the_shape_the_owner_reads_a_share_in(gw):
    body = (await create(gw)).json()
    again = (await gw.client.get(f"/api/sharing/shares/{body['share_id']}",
                                 headers=gw.h("admin_b", "ws-b"))).json()

    assert again == body


async def test_everything_the_owner_chose_is_kept(gw):
    r = await create(gw, days=45, deny_globs=["config/**", "**/*.pem"], max_people=2,
                     allow_ask=True)

    row = share_row(gw, r.json()["share_id"])
    assert row.deny_globs == ["config/**", "**/*.pem"]
    assert row.max_people == 2 and row.allow_ask is True
    ends = _aware(row.expires_at) - _aware(row.created_at)
    assert timedelta(days=45) - timedelta(seconds=5) <= ends <= timedelta(days=45, seconds=5)


async def test_a_share_made_gives_nobody_anything_until_a_token_names_it(gw):
    """The share is the owner's consent, not a grant: a token that does not carry it,
    a star included, reads nothing of B, and one that does carry it reads exactly it."""
    from src.access.effective import Principal, effective_access

    share_id = (await create(gw)).json()["share_id"]
    person = gw.uid(HOLDER)

    star = effective_access(Principal(user_id=person, repo_patterns=("*",)), WS_A, [B_REPO])
    named = effective_access(
        Principal(user_id=person, repo_patterns=(), share_ids=(share_id,)), WS_A, [B_REPO])

    assert not star[B_REPO].code_visible
    assert named[B_REPO].code_visible


# ─── the receiving workspace ─────────────────────────────────────────


@pytest.mark.parametrize("reference", ["ws-a", "WS-A", " ws-a ", "wsid-a", "WSID-A"])
async def test_the_receiving_workspace_is_found_by_slug_or_id_in_any_case(gw, reference):
    r = await create(gw, workspace=reference)

    assert r.status_code == 201, r.text
    assert r.json()["grantee_workspace_id"] == WS_A


@pytest.mark.parametrize("reference", ["ws-nobody", "wsid-nobody", "ws", "%", "ws-a'--", "x" * 300])
async def test_an_unknown_workspace_is_not_found_and_nothing_is_made(gw, reference):
    r = await create(gw, workspace=reference)

    assert r.status_code in (404, 422), r.text
    if r.status_code == 404:
        assert r.json()["detail"] == "No such workspace."
    assert _rows(gw) == []


async def test_a_name_that_is_one_workspaces_slug_and_anothers_id_picks_neither(gw):
    """Guessing would hand the owner's code to a workspace they did not mean."""
    _add_workspace(gw, "wsid-lookalike", WS_A, "Lookalike")

    r = await create(gw, workspace=WS_A)

    assert r.status_code == 404 and r.json()["detail"] == "No such workspace."
    assert _rows(gw) == []
    assert (await create(gw, workspace="ws-a")).status_code == 201, "the unambiguous name still works"


async def test_a_repository_cannot_be_shared_with_its_own_workspace(gw):
    r = await create(gw, workspace="ws-b")

    assert r.status_code == 422
    assert r.json()["detail"] == "A repository cannot be shared with its own workspace."
    assert _rows(gw) == []


async def test_the_built_in_default_workspace_cannot_receive_a_share(gw):
    _add_workspace(gw, "default", "default", "Default")

    r = await create(gw, workspace="default")

    assert r.status_code == 422
    assert "default workspace" in r.json()["detail"]
    assert _rows(gw) == []


async def test_the_built_in_default_workspace_cannot_give_a_share(gw):
    """A global administrator can work in any workspace, the built-in one included."""
    _add_workspace(gw, "default", "default", "Default")

    r = await create(gw, who="gadmin", ws="default", repo_slug=B_REPO)

    assert r.status_code == 422
    assert "default workspace" in r.json()["detail"]
    assert _rows(gw) == []


# ─── the repository ──────────────────────────────────────────────────


@pytest.mark.parametrize("slug", ["github_nobody-nothing", A_REPO, "", " ", "../etc", "x" * 400])
async def test_only_a_repository_the_owner_has_registered_can_be_shared(gw, slug):
    """A repository of the receiving workspace, or one that is not anybody's, is not in
    the owner's registry: 404 (or 422 for text no registry holds), and no row."""
    r = await create(gw, repo_slug=slug)

    assert r.status_code in (404, 422), r.text
    if r.status_code == 404:
        assert r.json()["detail"] == "No such repository."
    assert _rows(gw) == []


async def test_a_repository_shared_with_the_owner_cannot_be_shared_onward(gw):
    """Not transitive: what B received from A is not in B's registry, so B cannot give it
    to a third workspace, however the share into B reads."""
    _add_workspace(gw, "wsid-c", "ws-c", "Charlie")
    received = add_share(gw, slug=A_REPO, full_name=A_REPO_FULL, owner=WS_A, grantee=WS_B)

    r = await create(gw, repo_slug=A_REPO, workspace="ws-c")

    assert r.status_code == 404 and r.json()["detail"] == "No such repository."
    assert [row.id for row in _rows(gw)] == [received]


async def test_a_slug_two_workspaces_registered_is_nobodys_to_share(gw):
    """The clone and the graph on disk are keyed by the slug alone: with two owners the
    share could not be told from the other's repository."""
    from src.api.auto_review import RepoConfig, get_auto_review_store

    # A second row for the same slug, registered by ANOTHER person in the other
    # workspace (rows are keyed by person and slug, so the same person would move the
    # repository instead of duplicating it).
    store = get_auto_review_store()
    store.upsert(RepoConfig(
        user_id=gw.uid("member_a"), repo_slug=B_REPO, provider="github",
        full_name="other/elsewhere", url="https://github.com/other/elsewhere",
        workspace_id=WS_A))
    assert store.get_in_workspace(WS_B, B_REPO) is not None, "B must still have it"
    assert store.workspace_for_slug(B_REPO) is None, "the slug must now be ambiguous"

    r = await create(gw)

    assert r.status_code == 404 and r.json()["detail"] == "No such repository."
    assert _rows(gw) == []


async def test_a_person_who_cannot_read_the_repository_cannot_share_it(gw):
    """The route is for an administrator, who can read all of the workspace; the service
    checks it anyway, so that a share is never how one hands over what one could not
    open. `member_a` is no member of B at all."""
    from src.ee.sharing import service

    with Session(gw.engine) as s:
        with pytest.raises(service.NotFound):
            service.create_share(s, owner_workspace_id=WS_B, creator=gw.users["member_a"],
                                 repo_slug=B_REPO, workspace="ws-a")
        s.rollback()
    assert _rows(gw) == []


# ─── how long ────────────────────────────────────────────────────────


@pytest.mark.parametrize("days", [0, -1, TOKEN_CEILING + 1, 10_000, "soon", 1.5, None, [30]])
async def test_an_end_that_is_not_between_a_day_and_the_ceiling_is_refused(gw, days):
    r = await create(gw, days=days)

    assert r.status_code == 422, (days, r.text)
    assert _rows(gw) == []


@pytest.mark.parametrize("days", [1, TOKEN_CEILING])
async def test_the_shortest_and_the_longest_end_are_accepted(gw, days):
    assert (await create(gw, days=days)).status_code == 201


async def test_the_ceiling_is_the_one_the_operator_set_for_tokens(gw, monkeypatch):
    from src.config import get_settings

    monkeypatch.setenv("CELMIS_MCP_TOKEN_MAX_DAYS", "10")
    get_settings.cache_clear()

    assert (await create(gw, days=11)).status_code == 422
    assert (await create(gw, days=10)).status_code == 201


# ─── how many people ─────────────────────────────────────────────────


@pytest.mark.parametrize("people", [0, -3, 26, 1000, "many", 2.5, None])
async def test_a_ceiling_on_people_outside_one_to_twenty_five_is_refused(gw, people):
    r = await create(gw, max_people=people)

    assert r.status_code == 422, (people, r.text)
    assert _rows(gw) == []


@pytest.mark.parametrize("people", [1, 25])
async def test_one_and_twenty_five_people_are_accepted(gw, people):
    r = await create(gw, max_people=people)

    assert r.status_code == 201 and r.json()["max_people"] == people


# ─── the paths the owner hides ───────────────────────────────────────


async def test_hidden_paths_are_written_the_way_the_matcher_reads_them(gw):
    r = await create(gw, deny_globs=["/secrets/**", "./config/**", " docs/** ", "docs/**", ""])

    assert r.status_code == 201, r.text
    assert share_row(gw, r.json()["share_id"]).deny_globs == [
        "secrets/**", "config/**", "docs/**"]


@pytest.mark.parametrize("globs", [
    ["a" * 201],
    ["bad\x00glob"],
    [f"dir{n}/**" for n in range(101)],
    [7],
])
async def test_hidden_paths_that_cannot_be_kept_are_refused(gw, globs):
    r = await create(gw, deny_globs=globs)

    assert r.status_code == 422, r.text
    assert _rows(gw) == []


async def test_a_list_of_hidden_paths_must_be_a_list(gw):
    assert (await create(gw, deny_globs="config/**")).status_code == 422


# ─── questions ───────────────────────────────────────────────────────


async def test_questions_are_off_unless_the_owner_turns_them_on(gw):
    off = await create(gw)
    assert off.json()["allow_ask"] is False

    on = await create(gw, workspace="wsid-a", repo_slug=_other_repo(gw), allow_ask=True)
    assert on.json()["allow_ask"] is True


@pytest.mark.parametrize("value", ["yes", 1, "true", None])
async def test_questions_are_a_yes_or_a_no_and_nothing_else(gw, value):
    r = await create(gw, allow_ask=value)

    assert r.status_code == 422, (value, r.text)


def _other_repo(w) -> str:
    slug = "github_bco-second"
    register_b(w, slug, "bco/second")
    return slug


# ─── what the request may contain ────────────────────────────────────


@pytest.mark.parametrize("extra", [
    {"expires_at": "2099-01-01T00:00:00Z"}, {"owner": "wsid-a"}, {"grantee_workspace_id": "x"},
    {"blocked_user_ids": ["u"]}, {"revoked_at": "2020-01-01T00:00:00Z"}, {"id": "abc"},
])
async def test_a_field_the_request_does_not_define_is_refused_not_ignored(gw, extra):
    """A client that sends an end date must hear that it was not used, not believe the
    share runs until then."""
    r = await create(gw, **extra)

    assert r.status_code == 422 and _rows(gw) == []


async def test_a_request_without_a_repository_or_a_workspace_is_refused(gw):
    for body in ({}, {"repo_slug": B_REPO}, {"workspace": "ws-a"}):
        r = await gw.client.post("/api/sharing-grants", json=body,
                                 headers=gw.h("admin_b", "ws-b"))
        assert r.status_code == 422, (body, r.text)


# ─── one running share per repository and pair ───────────────────────


async def test_a_repository_already_shared_with_that_workspace_is_a_conflict_that_points_at_extending(gw):
    first = (await create(gw)).json()["share_id"]

    r = await create(gw, deny_globs=["config/**"], days=5)

    assert r.status_code == 409
    assert r.json()["detail"] == "This repository is already shared with that workspace. Extend it instead."
    assert [row.id for row in _rows(gw)] == [first]
    assert share_row(gw, first).deny_globs == [], "the refused request changed the running share"


async def test_the_same_repository_can_go_to_two_workspaces_and_two_repositories_to_one(gw):
    _add_workspace(gw, "wsid-c", "ws-c", "Charlie")

    assert (await create(gw)).status_code == 201
    assert (await create(gw, workspace="ws-c")).status_code == 201
    assert (await create(gw, repo_slug=_other_repo(gw))).status_code == 201


async def test_an_ended_share_does_not_stand_in_the_way_of_a_new_one(gw):
    old = add_share(gw, revoked=True)

    r = await create(gw)

    assert r.status_code == 201 and r.json()["share_id"] != old
    assert share_row(gw, old).revoke_reason == "owner", "history is left as it was"


async def test_a_share_that_lapsed_is_ended_as_expired_and_replaced(gw):
    old = add_share(gw, days=-2)

    r = await create(gw)

    assert r.status_code == 201
    new = r.json()["share_id"]
    gone = share_row(gw, old)
    assert new != old and gone.revoked_at is not None and gone.revoke_reason == "expired"
    assert [row.id for row in _rows(gw) if row.revoked_at is None] == [new]
    (created,) = audit_rows(gw, "sharing.created")
    assert created["detail"]["replaced"] == [old]


async def test_two_requests_for_one_pair_make_one_share(gw):
    """The database holds the rule (a partial unique index), not only the check before
    the insert: the second of two racing requests is the same conflict."""
    from src.db.models import RepoShare

    first = (await create(gw)).json()["share_id"]
    row = share_row(gw, first)
    with Session(gw.engine) as s:
        s.add(RepoShare(
            owner_workspace_id=row.owner_workspace_id, grantee_workspace_id=row.grantee_workspace_id,
            repo_slug=row.repo_slug, repo_provider=row.repo_provider,
            repo_full_name=row.repo_full_name, created_by="x",
            expires_at=datetime.now(UTC) + timedelta(days=1)))
        with pytest.raises(IntegrityError):
            s.commit()


# ─── limits ──────────────────────────────────────────────────────────


async def test_a_workspace_cannot_give_more_running_shares_than_the_limit(gw, monkeypatch):
    from src.ee.sharing import service

    monkeypatch.setattr(service, "MAX_GIVEN", 2)
    _add_workspace(gw, "wsid-c", "ws-c", "Charlie")
    assert (await create(gw)).status_code == 201
    assert (await create(gw, workspace="ws-c")).status_code == 201

    r = await create(gw, repo_slug=_other_repo(gw))

    assert r.status_code == 409 and r.json()["detail"] == "Too many active shares."
    assert len(_rows(gw)) == 2


async def test_a_workspace_cannot_receive_more_running_shares_than_the_limit(gw, monkeypatch):
    from src.ee.sharing import service

    monkeypatch.setattr(service, "MAX_RECEIVED", 1)
    assert (await create(gw)).status_code == 201

    r = await create(gw, repo_slug=_other_repo(gw))

    assert r.status_code == 409 and r.json()["detail"] == "Too many active shares."


async def test_ended_and_lapsed_shares_do_not_count_against_the_limit(gw, monkeypatch):
    from src.ee.sharing import service

    monkeypatch.setattr(service, "MAX_RECEIVED", 1)
    add_fresh_share(gw, revoked=True)
    add_fresh_share(gw, days=-1)

    assert (await create(gw)).status_code == 201


# ─── the audit ───────────────────────────────────────────────────────


async def test_making_a_share_is_written_to_the_owners_trail_and_to_nobody_elses(gw):
    r = await create(gw, days=7, deny_globs=["config/**"], max_people=3, allow_ask=True)

    share_id = r.json()["share_id"]
    assert actions(gw, "sharing.") == [("sharing.created", WS_B, share_id)]
    (row,) = audit_rows(gw, "sharing.created")
    assert row["actor_id"] == gw.uid("admin_b") and row["actor"] == "admin-b@acme-corp.io"
    assert row["detail"]["repo"] == B_REPO and row["detail"]["grantee_workspace_id"] == WS_A
    assert (row["detail"]["max_people"], row["detail"]["allow_ask"]) == (3, True)
    assert row["detail"]["deny_globs"] == ["config/**"]
    assert datetime.fromisoformat(row["detail"]["expires_at"]).tzinfo is not None
    assert not [a for a in gw.audit if a.get("workspace_id") == WS_A and
                str(a["action"]).startswith("sharing.")], "the receiver's trail has no row"


async def test_a_refused_request_leaves_no_trace_in_the_trail(gw):
    await create(gw, days=0)
    await create(gw, workspace="ws-nobody")
    await create(gw, repo_slug=A_REPO)

    assert actions(gw, "sharing.") == []
