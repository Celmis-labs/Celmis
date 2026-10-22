"""Putting a share on a token: who may, for whom, for how long, and what is said.

The superadmin issues a developer token for a person of the RECEIVING workspace
and names the shares it carries (`shares`, beside `repos`, never inside it). The
share must be live, be for that workspace, not have blocked that person, and have
room for them (`max_people`). Every refusal about a share is the same sentence,
"No such share.": the answer must not tell the asker whether the id is somebody
else's, ended, full or only mistyped. The token ends no later than the share.

World: `tests/api/sharing_world.py` (B gives A one repository).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.api.rbac_world import A_REPO, A_REPO_FULL, B_REPO, B_REPO_FULL
from tests.api.sharing_world import (
    HOLDER,
    HOLDER_EMAIL,
    WS_A,
    WS_B,
    actions,
    add_fresh_share,
    add_share,
    audit_rows,
    register_b,
)

NO_SUCH_SHARE = "No such share."
OTHER_EMAIL = "viewer-a@acme-corp.io"


async def _issue(w, shares, *, repos=None, who="su", user=HOLDER_EMAIL, **over):
    body = {"user_ref": user, "workspace_id": "ws-a", "label": "laptop",
            "repos": [] if repos is None else repos, "shares": shares}
    body.update(over)
    return await w.client.post("/api/admin/mcp-tokens", json=body, headers=w.h(who, "ws-a"))


async def _patch(w, token_id, body, who="su"):
    return await w.client.patch(f"/api/admin/mcp-tokens/{token_id}", json=body,
                                headers=w.h(who, "ws-a"))


def _token_row(w, token_id):
    from sqlalchemy.orm import Session

    from src.db.models import McpToken

    with Session(w.engine) as s:
        row = s.get(McpToken, token_id)
        s.expunge(row)
        return row


# ─── what a token with a share looks like ────────────────────────────


async def test_a_token_can_carry_only_a_share(sw):
    share_id = add_share(sw)
    r = await _issue(sw, [share_id])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["repos"] == [] and body["matched_repos"] == []
    (shown,) = body["shares"]
    assert shown["share_id"] == share_id and shown["slug"] == B_REPO
    assert shown["owner_name"] == "Bravo B_SECRET" and shown["status"] == "active"
    assert _token_row(sw, body["id"]).shared_repos == [share_id]


async def test_the_shared_repository_is_beside_the_own_ones_and_never_in_them(sw):
    share_id = add_share(sw)
    body = (await _issue(sw, [share_id], repos=[A_REPO])).json()
    assert body["repos"] == [A_REPO] and body["matched_repos"] == [A_REPO]
    assert [s["slug"] for s in body["shares"]] == [B_REPO]
    assert B_REPO not in json.dumps(body["repos"] + body["matched_repos"])


async def test_a_token_that_lists_nothing_is_still_refused(sw):
    r = await _issue(sw, [], repos=[])
    assert r.status_code == 422


@pytest.mark.parametrize("shares,repos", [([], ["a b"]), (["x"], []), ([], [""])])
async def test_the_issue_form_still_validates_what_it_validated(sw, shares, repos):
    r = await _issue(sw, shares, repos=repos)
    assert r.status_code == 422


async def test_the_list_and_the_holders_own_page_show_the_shares_and_what_became_of_them(sw):
    share_id = add_share(sw)
    token_id = (await _issue(sw, [share_id])).json()["id"]
    listed = (await sw.client.get("/api/admin/mcp-tokens", headers=sw.h("su", "ws-a"))).json()
    assert [s["status"] for s in listed[0]["shares"]] == ["active"]
    mine = (await sw.client.get("/api/mcp/tokens/me", headers=sw.h(HOLDER, "ws-a"))).json()
    assert [s["slug"] for s in mine["tokens"][0]["shares"]] == [B_REPO]
    assert mine["tokens"][0]["id"] == token_id
    await sw.client.post(f"/api/sharing/shares/{share_id}/revoke", headers=sw.h("admin_b", "ws-b"))
    listed = (await sw.client.get("/api/admin/mcp-tokens", headers=sw.h("su", "ws-a"))).json()
    assert [s["status"] for s in listed[0]["shares"]] == ["revoked"]


async def test_a_token_that_cannot_use_its_shares_does_not_show_them(sw):
    """Ids put into a row by hand on a token the shares do not count for (a write
    token, another kind) are not presented as if they worked."""
    share_id = add_share(sw)
    token_id = (await _issue(sw, [share_id], repos=[A_REPO])).json()["id"]
    from sqlalchemy.orm import Session

    from src.db.models import McpToken
    from src.mcp_server import token_store

    with Session(sw.engine) as s:
        s.get(McpToken, token_id).allow_write = True
        s.commit()
    token_store.invalidate(token_id)
    listed = (await sw.client.get("/api/admin/mcp-tokens", headers=sw.h("su", "ws-a"))).json()
    assert listed[0]["shares"] == []


# ─── the refusals ────────────────────────────────────────────────────


def _refusals(w):
    """Every way a share can be unusable for this token, as {name: share id}."""
    out = {
        "unknown": "00000000-0000-4000-8000-000000000000",
        "not an id": "not-an-id",
        "a wildcard": "%",
        "revoked": add_fresh_share(w, revoked=True),
        "expired": add_fresh_share(w, days=-1),
        "for another workspace": add_share(w, grantee=WS_B, owner=WS_A, slug=A_REPO,
                                           full_name=A_REPO_FULL),
        "blocks the person": add_fresh_share(w, blocked=[w.uid(HOLDER)]),
        # Registered under one name, shared under another: not the repository that
        # was shared any more.
        "the identity moved": add_fresh_share(w, full_name="someone/else"),
        # Shared, then the owner stopped having it registered.
        "the repository is gone": add_share(w, slug="github_bco-gone", full_name="bco/gone"),
    }
    from src.api.auto_review import get_auto_review_store

    assert get_auto_review_store().get_in_workspace(WS_B, "github_bco-gone") is None
    return out


async def test_every_refusal_about_a_share_is_the_same_sentence(sw):
    bodies = {}
    for name, share_id in _refusals(sw).items():
        r = await _issue(sw, [share_id])
        assert r.status_code == 422, (name, r.status_code, r.text)
        assert r.json()["detail"] == NO_SUCH_SHARE, name
        bodies[name] = r.content
    assert len(set(bodies.values())) == 1, "the refusals differ by more than the id"


async def test_one_unusable_share_among_good_ones_refuses_the_whole_token(sw):
    good = add_share(sw)
    bad = add_share(sw, revoked=True)
    r = await _issue(sw, [good, bad])
    assert r.status_code == 422 and r.json()["detail"] == NO_SUCH_SHARE
    from sqlalchemy.orm import Session

    from src.db.models import McpToken

    with Session(sw.engine) as s:
        assert s.query(McpToken).count() == 0, "a refused issue left a token behind"


async def test_a_share_for_somebody_elses_workspace_is_refused_even_to_the_superadmin(sw):
    mine = add_share(sw)
    theirs = add_share(sw, grantee=WS_B, owner=WS_A, slug=A_REPO, full_name=A_REPO_FULL)
    assert (await _issue(sw, [mine])).status_code == 201
    r = await _issue(sw, [theirs])
    assert r.status_code == 422 and r.json()["detail"] == NO_SUCH_SHARE


@pytest.mark.parametrize("over", [
    {"profile": "full"},
    {"profile": "full", "allow_write": True},
    {"allow_write": True},
])
async def test_only_a_read_only_developer_token_carries_a_share(sw, over):
    share_id = add_share(sw)
    r = await _issue(sw, [share_id], repos=[A_REPO], **over)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] != NO_SUCH_SHARE, "the profile is a fact about the request"


async def test_an_oauth_grant_cannot_carry_a_share(sw):
    share_id = add_share(sw)
    r = await sw.client.post("/api/admin/mcp-grants", json={
        "user_ref": HOLDER_EMAIL, "workspace_id": "ws-a", "repos": [A_REPO],
        "shares": [share_id]}, headers=sw.h("su", "ws-a"))
    assert r.status_code == 422, r.text


async def test_more_shares_than_a_token_can_carry_are_refused(sw):
    ids = [add_fresh_share(sw) for _ in range(11)]
    assert (await _issue(sw, ids)).status_code == 422


async def test_only_the_issuer_may_put_a_share_on_a_token(sw):
    share_id = add_share(sw)
    for who in ("admin_a", "owner_a", "admin_b", "gadmin", "member_a"):
        r = await _issue(sw, [share_id], who=who)
        assert r.status_code == 403, (who, r.status_code)


async def test_a_person_outside_the_receiving_workspace_gets_no_token_for_it(sw):
    share_id = add_share(sw)
    r = await _issue(sw, [share_id], user="member-b@acme-corp.io")
    assert r.status_code == 422
    assert "member" in r.json()["detail"]


# ─── how long ────────────────────────────────────────────────────────


async def test_a_token_ends_with_the_share_it_carries_and_the_issuer_is_told(sw):
    share_id = add_share(sw, days=5)
    r = await _issue(sw, [share_id], expires_in_days=30)
    assert r.status_code == 201, r.text
    body = r.json()
    ends = datetime.fromisoformat(body["expires_at"])
    assert abs(ends - (datetime.now(UTC) + timedelta(days=5))) < timedelta(minutes=2)
    assert any("stops being shared" in w for w in body["warnings"]), body["warnings"]


async def test_the_signed_token_ends_with_the_share_too(sw):
    """The expiry is inside the signed value, not only in the row."""
    import jwt

    from tests.api.sharing_world import SECRET

    share_id = add_share(sw, days=3)
    body = (await _issue(sw, [share_id], expires_in_days=60)).json()
    claims = jwt.decode(body["token"], SECRET, algorithms=["HS256"],
                        options={"verify_aud": False, "verify_signature": True})
    exp = datetime.fromtimestamp(claims["exp"], UTC)
    assert abs(exp - (datetime.now(UTC) + timedelta(days=3))) < timedelta(minutes=2)


async def test_the_earliest_of_several_shares_decides(sw):
    soon = add_share(sw, days=4)
    later = add_share(sw, slug="github_bco-later", full_name="bco/later", days=40)
    register_b(sw, "github_bco-later", "bco/later")
    body = (await _issue(sw, [later, soon], expires_in_days=30)).json()
    assert abs(datetime.fromisoformat(body["expires_at"])
               - (datetime.now(UTC) + timedelta(days=4))) < timedelta(minutes=2)


async def test_a_token_that_ends_before_its_share_is_not_stretched_or_warned_about(sw):
    share_id = add_share(sw, days=60)
    body = (await _issue(sw, [share_id], expires_in_days=10)).json()
    assert abs(datetime.fromisoformat(body["expires_at"])
               - (datetime.now(UTC) + timedelta(days=10))) < timedelta(minutes=2)
    assert not any("stops being shared" in w for w in body["warnings"])


# ─── how many people ─────────────────────────────────────────────────


async def test_a_share_for_one_person_has_no_place_for_a_second(sw):
    share_id = add_share(sw, people=1)
    assert (await _issue(sw, [share_id])).status_code == 201
    r = await _issue(sw, [share_id], user=OTHER_EMAIL)
    assert r.status_code == 422 and r.json()["detail"] == NO_SUCH_SHARE


async def test_the_person_who_already_holds_it_may_have_another_token(sw):
    share_id = add_share(sw, people=1)
    assert (await _issue(sw, [share_id])).status_code == 201
    assert (await _issue(sw, [share_id], label="second laptop")).status_code == 201


async def test_a_token_that_ended_gives_its_place_back(sw):
    share_id = add_share(sw, people=1)
    first = (await _issue(sw, [share_id])).json()["id"]
    await sw.client.post(f"/api/admin/mcp-tokens/{first}/revoke", headers=sw.h("su", "ws-a"))
    assert (await _issue(sw, [share_id], user=OTHER_EMAIL)).status_code == 201


async def test_a_person_the_owner_switched_off_gives_their_place_back(sw):
    """The token is still there but reads nothing; keeping the place would leave
    the owner unable to let anybody else in without widening the share."""
    share_id = add_share(sw, people=1)
    assert (await _issue(sw, [share_id])).status_code == 201
    r = await sw.client.patch(f"/api/sharing/shares/{share_id}",
                              json={"block": [sw.uid("member_a")]}, headers=sw.h("admin_b", "ws-b"))
    assert r.status_code == 200, r.text
    assert (await _issue(sw, [share_id], user=OTHER_EMAIL)).status_code == 201
    # the person who was switched off cannot take the place back
    again = await _issue(sw, [share_id])
    assert again.status_code == 422 and again.json()["detail"] == NO_SUCH_SHARE


async def test_adding_a_share_by_patch_obeys_the_same_count(sw):
    share_id = add_share(sw, people=1)
    assert (await _issue(sw, [share_id])).status_code == 201
    other = (await _issue(sw, [], repos=[A_REPO], user=OTHER_EMAIL)).json()["id"]
    r = await _patch(sw, other, {"shares": [share_id]})
    assert r.status_code == 422 and r.json()["detail"] == NO_SUCH_SHARE
    assert _token_row(sw, other).shared_repos in (None, [])


def _competitor(sw, share_id: str) -> None:
    """Another request's token for the share, written the way that request writes it."""
    from src.mcp_server import token_store

    token_store.mint(
        kind="pat", workspace_id=WS_A, user_id=sw.uid("admin_a"), issued_by=sw.uid("su"),
        label="the other request", patterns=[], profile="dev", shared=[share_id])


async def test_the_last_place_goes_to_the_request_that_wrote_first(sw, monkeypatch):
    """Two requests for the last place: the count must come after the lock, so that
    the one that waited for it sees the token the other wrote while it waited. (The
    count used to be made before anything was held, and both requests got a token.)"""
    from src.access import shares as repo_shares

    share_id = add_share(sw, people=2)
    assert (await _issue(sw, [share_id])).status_code == 201          # one place left
    real_lock = repo_shares.lock_places
    taken: list[list[str]] = []

    def lock_after_the_other_request_won(session, share_ids):
        taken.append(list(share_ids))
        if len(taken) == 1:
            _competitor(sw, share_id)          # it was written while this one waited
        return real_lock(session, share_ids)

    monkeypatch.setattr(repo_shares, "lock_places", lock_after_the_other_request_won)
    r = await _issue(sw, [share_id], user=OTHER_EMAIL)
    assert r.status_code == 422 and r.json()["detail"] == NO_SUCH_SHARE
    assert taken == [[share_id]]


async def test_the_places_are_counted_and_the_token_written_in_one_session_holding_the_lock(
        sw, monkeypatch):
    from src.access import shares as repo_shares
    from src.mcp_server import token_store

    share_id = add_share(sw, people=2)
    seen: list[tuple[str, int]] = []
    real_lock, real_create = repo_shares.lock_places, token_store.create_row

    def lock(session, share_ids):
        seen.append(("lock", id(session)))
        return real_lock(session, share_ids)

    def create(session, **kw):
        seen.append(("write", id(session)))
        return real_create(session, **kw)

    monkeypatch.setattr(repo_shares, "lock_places", lock)
    monkeypatch.setattr(token_store, "create_row", create)
    assert (await _issue(sw, [share_id])).status_code == 201
    assert [what for what, _ in seen] == ["lock", "write"]
    assert seen[0][1] == seen[1][1], "the lock is let go only when the token is committed"


async def test_putting_a_share_on_a_token_counts_the_places_under_the_same_lock(sw, monkeypatch):
    from src.access import shares as repo_shares

    share_id = add_share(sw, people=1)
    assert (await _issue(sw, [share_id])).status_code == 201
    other = (await _issue(sw, [], repos=[A_REPO], user=OTHER_EMAIL)).json()["id"]
    real_lock = repo_shares.lock_places
    calls: list[list[str]] = []

    def lock(session, share_ids):
        calls.append(list(share_ids))
        _competitor(sw, share_id) if len(calls) == 1 else None
        return real_lock(session, share_ids)

    monkeypatch.setattr(repo_shares, "lock_places", lock)
    r = await _patch(sw, other, {"shares": [share_id]})
    assert r.status_code == 422 and r.json()["detail"] == NO_SUCH_SHARE
    assert calls and calls[0] == [share_id]


def test_the_lock_is_a_row_lock_on_the_shares_taken_in_a_fixed_order():
    """What a database with row locks is told: the shares' own rows, FOR UPDATE, by id.
    (SQLite, which the test worlds run on, has no row locks and leaves the clause out,
    so the statement is looked at as the production database would receive it.)"""
    from sqlalchemy.dialects import postgresql

    from src.access import shares as repo_shares

    first, second = "b" * 8 + "-bbbb-4bbb-8bbb-" + "b" * 12, "a" * 8 + "-aaaa-4aaa-8aaa-" + "a" * 12

    class Recorder:
        statements: list = []

        def execute(self, statement):
            self.statements.append(statement)

            class Rows:
                def scalars(self):
                    return self

                def all(self):
                    return []

            return Rows()

    recorder = Recorder()
    repo_shares.lock_places(recorder, [first, "not an id", second, first])
    (statement,) = recorder.statements
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "FROM repo_shares" in sql and sql.rstrip().endswith("FOR UPDATE")
    assert "ORDER BY repo_shares.id" in sql
    assert statement.compile(dialect=postgresql.dialect()).params  # the ids, bound not pasted
    repo_shares.lock_places(recorder, ["not an id"])
    assert len(recorder.statements) == 1, "nothing to lock, nothing asked of the database"


# ─── changing the list ───────────────────────────────────────────────


async def test_patch_replaces_the_list_of_shares(sw):
    first = add_share(sw)
    second = add_share(sw, slug="github_bco-second", full_name="bco/second")
    register_b(sw, "github_bco-second", "bco/second")
    token_id = (await _issue(sw, [first], repos=[A_REPO])).json()["id"]
    r = await _patch(sw, token_id, {"shares": [first, second]})
    assert r.status_code == 200, r.text
    assert sorted(s["share_id"] for s in r.json()["shares"]) == sorted([first, second])
    r = await _patch(sw, token_id, {"shares": [second]})
    assert [s["share_id"] for s in r.json()["shares"]] == [second]
    assert _token_row(sw, token_id).shared_repos == [second]
    assert r.json()["repos"] == [A_REPO], "the own list is not touched by a change to the shares"


async def test_patch_leaves_the_shares_alone_when_it_does_not_mention_them(sw):
    share_id = add_share(sw)
    token_id = (await _issue(sw, [share_id], repos=[A_REPO])).json()["id"]
    r = await _patch(sw, token_id, {"label": "renamed"})
    assert r.status_code == 200 and [s["share_id"] for s in r.json()["shares"]] == [share_id]


async def test_an_empty_list_takes_every_share_off(sw):
    share_id = add_share(sw)
    token_id = (await _issue(sw, [share_id], repos=[A_REPO])).json()["id"]
    r = await _patch(sw, token_id, {"shares": []})
    assert r.status_code == 200 and r.json()["shares"] == []
    assert not _token_row(sw, token_id).shared_repos


async def test_a_token_cannot_be_left_with_nothing_to_read(sw):
    share_id = add_share(sw)
    token_id = (await _issue(sw, [share_id])).json()["id"]
    assert (await _patch(sw, token_id, {"shares": []})).status_code == 422
    assert _token_row(sw, token_id).shared_repos == [share_id]


async def test_somebody_elses_share_cannot_be_added_and_the_token_is_unchanged(sw):
    mine = add_share(sw)
    theirs = add_share(sw, grantee=WS_B, owner=WS_A, slug=A_REPO, full_name=A_REPO_FULL)
    token_id = (await _issue(sw, [mine], repos=[A_REPO])).json()["id"]
    r = await _patch(sw, token_id, {"shares": [mine, theirs]})
    assert r.status_code == 422 and r.json()["detail"] == NO_SUCH_SHARE
    assert _token_row(sw, token_id).shared_repos == [mine]


async def test_removing_a_share_that_has_ended_is_always_allowed(sw):
    """Only what is ADDED is judged: a share that ended since stays on the list
    until somebody takes it off, and taking it off must work."""
    gone = add_share(sw)
    fresh = add_share(sw, slug="github_bco-fresh", full_name="bco/fresh")
    register_b(sw, "github_bco-fresh", "bco/fresh")
    token_id = (await _issue(sw, [gone], repos=[A_REPO])).json()["id"]
    await sw.client.post(f"/api/sharing/shares/{gone}/revoke", headers=sw.h("admin_b", "ws-b"))
    keep_and_add = await _patch(sw, token_id, {"shares": [gone, fresh]})
    assert keep_and_add.status_code == 200, keep_and_add.text
    removed = await _patch(sw, token_id, {"shares": [fresh]})
    assert removed.status_code == 200
    assert [s["share_id"] for s in removed.json()["shares"]] == [fresh]


async def test_a_token_that_cannot_carry_shares_cannot_be_given_one(sw):
    share_id = add_share(sw)
    body = {"user_ref": HOLDER_EMAIL, "workspace_id": "ws-a", "repos": [A_REPO],
            "profile": "full", "allow_write": True}
    full = (await sw.client.post("/api/admin/mcp-tokens", json=body,
                                 headers=sw.h("su", "ws-a"))).json()
    r = await _patch(sw, full["id"], {"shares": [share_id]})
    assert r.status_code == 422 and r.json()["detail"] != NO_SUCH_SHARE


async def test_patch_does_not_stretch_a_token_past_its_share_but_says_so(sw):
    share_id = add_share(sw, days=3)
    token_id = (await _issue(sw, [], repos=[A_REPO], expires_in_days=30)).json()["id"]
    r = await _patch(sw, token_id, {"shares": [share_id]})
    assert r.status_code == 200
    assert any("no longer reads it" in w for w in r.json()["warnings"]), r.json()["warnings"]


# ─── warnings ────────────────────────────────────────────────────────


async def test_a_star_is_said_to_read_the_whole_workspace_and_never_a_shared_repository(sw):
    share_id = add_share(sw)
    star = (await _issue(sw, [], repos=["*"])).json()
    assert "this token reads every repository of the workspace." in star["warnings"]
    assert not any("star never includes" in w for w in star["warnings"])
    both = (await _issue(sw, [share_id], repos=["*"])).json()
    assert any("a star never includes shared repositories" in w for w in both["warnings"])
    assert B_REPO not in both["matched_repos"]


async def test_a_glob_with_a_share_gets_the_star_sentence(sw):
    share_id = add_share(sw)
    body = (await _issue(sw, [share_id], repos=["github_*"])).json()
    assert any("star never includes" in w for w in body["warnings"])
    assert not any("every repository" in w for w in body["warnings"])


# ─── the trails ──────────────────────────────────────────────────────


async def test_the_receivers_trail_holds_a_number_and_the_owners_holds_the_share(sw):
    share_id = add_share(sw)
    body = (await _issue(sw, [share_id], repos=[A_REPO])).json()
    (issued,) = audit_rows(sw, "mcp.token_issued")
    assert issued["workspace_id"] == WS_A and issued["detail"]["shared_repos"] == 1
    written = json.dumps(issued, default=str)
    assert share_id not in written and B_REPO not in written and "bco" not in written
    (attached,) = audit_rows(sw, "sharing.token_attached")
    assert attached["workspace_id"] == WS_B and attached["target"] == share_id
    assert attached["detail"] == {"grantee_workspace_id": WS_A, "holder": sw.uid(HOLDER)}
    assert body["id"] not in json.dumps(attached, default=str), "the owner learns no token id"


async def test_a_token_without_shares_writes_the_trail_it_always_did(sw):
    await _issue(sw, [], repos=[A_REPO])
    (issued,) = audit_rows(sw, "mcp.token_issued")
    assert "shared_repos" not in issued["detail"]
    assert actions(sw, "sharing.") == []


async def test_changing_the_list_and_revoking_the_token_reach_the_owners_trail(sw):
    first = add_share(sw)
    second = add_share(sw, slug="github_bco-second", full_name="bco/second")
    register_b(sw, "github_bco-second", "bco/second")
    token_id = (await _issue(sw, [first], repos=[A_REPO])).json()["id"]
    await _patch(sw, token_id, {"shares": [second]})
    await sw.client.post(f"/api/admin/mcp-tokens/{token_id}/revoke", headers=sw.h("su", "ws-a"))
    assert [(a, t) for a, ws, t in actions(sw, "sharing.") if ws == WS_B] == [
        ("sharing.token_attached", first),
        ("sharing.token_attached", second),
        ("sharing.token_detached", first),
        ("sharing.token_detached", second),
    ]
    assert {ws for _, ws, _ in actions(sw, "sharing.")} == {WS_B}
    (updated,) = audit_rows(sw, "mcp.token_updated")
    assert updated["detail"]["shared_repos"] == 1 and first not in json.dumps(updated, default=str)


async def test_a_holder_revoking_their_own_token_reaches_the_owners_trail_too(sw):
    share_id = add_share(sw)
    token_id = (await _issue(sw, [share_id], repos=[A_REPO])).json()["id"]
    r = await sw.client.post(f"/api/mcp/tokens/{token_id}/revoke", headers=sw.h(HOLDER, "ws-a"))
    assert r.status_code == 200
    assert ("sharing.token_detached", WS_B, share_id) in actions(sw, "sharing.")


# ─── the picker ──────────────────────────────────────────────────────


KEYS = {"share_id", "slug", "full_name", "owner_name", "expires_at", "allow_ask", "deny_count",
        "holders", "max_people", "status"}


async def test_the_picker_lists_the_live_shares_of_the_workspace(sw):
    live = add_share(sw, deny=["secrets/**", "docs/**"], ask=True, people=3)
    add_fresh_share(sw, revoked=True)
    add_fresh_share(sw, days=-1)
    add_share(sw, grantee=WS_B, owner=WS_A, slug=A_REPO, full_name=A_REPO_FULL)
    await _issue(sw, [live])
    r = await sw.client.get("/api/admin/mcp-shares", params={"workspace": "ws-a"},
                            headers=sw.h("su", "ws-a"))
    assert r.status_code == 200, r.text
    (row,) = r.json()
    assert set(row) == KEYS
    assert (row["share_id"], row["slug"], row["full_name"]) == (live, B_REPO, B_REPO_FULL)
    assert (row["allow_ask"], row["deny_count"], row["holders"], row["max_people"]) == (True, 2, 1, 3)
    assert row["status"] == "active" and row["owner_name"] == "Bravo B_SECRET"
    assert "secrets" not in r.text, "the owner's hidden paths are a count, not a list"


async def test_the_picker_takes_the_workspace_by_id_or_slug_and_knows_when_there_is_none(sw):
    add_share(sw)
    by_id = await sw.client.get("/api/admin/mcp-shares", params={"workspace": WS_A},
                                headers=sw.h("su", "ws-a"))
    assert len(by_id.json()) == 1
    nowhere = await sw.client.get("/api/admin/mcp-shares", params={"workspace": "ws-nowhere"},
                                  headers=sw.h("su", "ws-a"))
    assert nowhere.status_code == 404
    other = await sw.client.get("/api/admin/mcp-shares", params={"workspace": "ws-b"},
                                headers=sw.h("su", "ws-a"))
    assert other.json() == [], "B receives nothing"


@pytest.mark.parametrize("who", ["admin_a", "owner_a", "admin_b", "gadmin", "member_a", "loner"])
async def test_only_the_issuer_sees_the_picker(sw, who):
    add_share(sw)
    r = await sw.client.get("/api/admin/mcp-shares", params={"workspace": "ws-a"},
                            headers=sw.h(who, "ws-a"))
    assert r.status_code == 403, (who, r.status_code)


async def test_the_own_repository_picker_never_offers_a_shared_one(sw):
    add_share(sw)
    r = await sw.client.get("/api/admin/mcp-repos", params={"workspace": "ws-a"},
                            headers=sw.h("su", "ws-a"))
    assert [x["slug"] for x in r.json()] == [A_REPO]


# ─── the call log ────────────────────────────────────────────────────


async def test_the_call_log_names_a_share_for_the_issuer_and_the_csv_has_a_column(sw):
    from tests.api.sharing_world import add_call

    share_id = add_share(sw)
    add_call(sw, [share_id], repos=[A_REPO])
    add_call(sw, None, tool="repos")
    r = await sw.client.get("/api/admin/mcp-calls", headers=sw.h("su", "ws-a"))
    by_tool = {c["tool"]: c for c in r.json()}
    assert by_tool["read_symbol"]["shares"] == [f"{B_REPO}@Bravo B_SECRET"]
    assert by_tool["read_symbol"]["repos"] == [A_REPO], "the shared slug is not in repos"
    assert by_tool["repos"]["shares"] == []
    assert share_id not in r.text
    csv_ = await sw.client.get("/api/admin/mcp-calls", params={"format": "csv"},
                               headers=sw.h("su", "ws-a"))
    header, *lines = csv_.text.strip().splitlines()
    assert header.split(",")[-1] == "shares"
    assert any(f"{B_REPO}@Bravo B_SECRET" in line for line in lines)


async def test_a_call_log_row_of_a_share_that_has_no_row_shows_its_id(sw):
    from tests.api.sharing_world import add_call

    add_call(sw, ["11111111-1111-4111-8111-111111111111"])
    r = await sw.client.get("/api/admin/mcp-calls", headers=sw.h("su", "ws-a"))
    assert r.json()[0]["shares"] == ["11111111-1111-4111-8111-111111111111"]


async def test_only_the_issuer_reads_the_call_log_with_its_shares(sw):
    from tests.api.sharing_world import add_call

    add_call(sw, [add_share(sw)])
    for who in ("admin_a", "owner_a", "admin_b"):
        r = await sw.client.get("/api/admin/mcp-calls", headers=sw.h(who, "ws-a"))
        assert r.status_code == 403, who
