"""What the OWNER of a share sees of the people who read it, and what it can change.

The owner learns who reads its repository, when, with which tool and how the call
ended: the names and e-mail addresses of the receiving workspace's people, because
they are the ones reading the owner's code. It learns nothing else of the call:
not which of its own paths were read, not the token, the arguments' digest or the
sizes. The shape is a fixed projection, so a field added to the call log does not
reach the owner by accident; this test pins the exact key set.

The owner can narrow the share and end it, never widen it, and every change is
written to the OWNER's audit trail, in the owner's workspace and nowhere else.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from tests.api.sharing_world import (
    HOLDER,
    HOLDER_EMAIL,
    WS_A,
    WS_B,
    actions,
    add_call,
    add_share,
    audit_rows,
    share_row,
)

ACTIVITY_KEYS = {"ts", "tool", "status", "user_id", "name", "email"}
SUMMARY_KEYS = {
    "share_id", "slug", "full_name", "grantee_workspace_id", "grantee_name", "status",
    "created_at", "expires_at", "allow_ask", "max_people", "deny_count", "holders",
    "last_used_at", "revoked_at", "revoke_reason",
}
DETAIL_KEYS = SUMMARY_KEYS | {"deny_globs", "blocked", "people", "created_by"}


async def _activity(w, share_id, who="admin_b", **params):
    r = await w.client.get(f"/api/sharing/shares/{share_id}/activity", params=params,
                           headers=w.h(who, "ws-b"))
    assert r.status_code == 200, r.text
    return r.json()


# ─── the usage view ──────────────────────────────────────────────────


async def test_a_read_shows_when_with_which_tool_how_it_ended_and_who(sw):
    share_id = add_share(sw)
    add_call(sw, [share_id], tool="read_symbol", status="ok")
    add_call(sw, [share_id], tool="grep", status="denied", ago=timedelta(hours=2))
    rows = await _activity(sw, share_id)
    assert [(r["tool"], r["status"]) for r in rows] == [("read_symbol", "ok"), ("grep", "denied")]
    assert all(r["email"] == HOLDER_EMAIL and r["name"] == HOLDER
               and r["user_id"] == sw.uid(HOLDER) for r in rows)


async def test_the_projection_is_exactly_these_keys_and_nothing_of_the_call_row_beyond_them(sw):
    share_id = add_share(sw)
    add_call(sw, [share_id], repos=["github_aco-app"], token_id="tok-secret-id")
    body = await _activity(sw, share_id)
    assert len(body) == 1 and set(body[0]) == ACTIVITY_KEYS
    text = json.dumps(body)
    for leaked in ("tok-secret-id", "abc123", "1234", "github_aco-app", "repos", "token_id",
                   "args_hash", "result_bytes", "duration"):
        assert leaked not in text, leaked


async def test_only_the_calls_that_named_this_share_are_its_usage(sw):
    mine, other = add_share(sw), add_share(sw, slug="github_bco-other", full_name="bco/other")
    add_call(sw, [mine], tool="find")
    add_call(sw, [other], tool="outline")
    add_call(sw, [mine, other], tool="refs")
    add_call(sw, None, tool="repos")
    add_call(sw, [], tool="map")
    tools = sorted(r["tool"] for r in await _activity(sw, mine))
    assert tools == ["find", "refs"]


async def test_a_row_of_another_workspace_that_names_the_id_is_not_its_usage(sw):
    """The id is looked for in the receiving workspace's rows only. A row written
    into the owner's own log (or any other) with the id in it is not a read."""
    share_id = add_share(sw)
    add_call(sw, [share_id], tool="genuine")
    add_call(sw, [share_id], tool="planted", workspace=WS_B)
    assert [r["tool"] for r in await _activity(sw, share_id)] == ["genuine"]


async def test_the_window_and_the_limit_bound_the_answer(sw):
    share_id = add_share(sw)
    add_call(sw, [share_id], tool="recent", ago=timedelta(days=1))
    add_call(sw, [share_id], tool="old", ago=timedelta(days=20))
    assert [r["tool"] for r in await _activity(sw, share_id, days=7)] == ["recent"]
    assert sorted(r["tool"] for r in await _activity(sw, share_id, days=30)) == ["old", "recent"]
    assert len(await _activity(sw, share_id, days=30, limit=1)) == 1


async def test_the_window_asks_for_something_sensible(sw):
    share_id = add_share(sw)
    for params in ({"days": 0}, {"days": 100000}, {"limit": 0}, {"limit": 100000}):
        r = await sw.client.get(f"/api/sharing/shares/{share_id}/activity", params=params,
                                headers=sw.h("admin_b", "ws-b"))
        assert r.status_code == 422, params


async def test_a_call_from_before_the_share_existed_cannot_be_its_usage(sw):
    share_id = add_share(sw)
    from sqlalchemy.orm import Session

    from src.db.models import RepoShare

    with Session(sw.engine) as s:
        s.get(RepoShare, share_id).created_at = datetime.now(UTC) - timedelta(hours=1)
        s.commit()
    add_call(sw, [share_id], tool="before", ago=timedelta(hours=3))
    add_call(sw, [share_id], tool="after", ago=timedelta(minutes=10))
    assert [r["tool"] for r in await _activity(sw, share_id, days=30)] == ["after"]


async def test_the_superadmin_reads_it_too(sw):
    share_id = add_share(sw)
    add_call(sw, [share_id])
    assert len(await _activity(sw, share_id, who="su")) == 1


async def test_a_person_the_user_store_does_not_know_still_appears_by_id(sw):
    share_id = add_share(sw)
    from sqlalchemy.orm import Session

    from src.db.models import McpCallLog

    add_call(sw, [share_id])
    with Session(sw.engine) as s:
        for row in s.query(McpCallLog).all():
            row.user_id = "u-gone"
        s.commit()
    (row,) = await _activity(sw, share_id)
    assert row["user_id"] == "u-gone" and row["name"] == "" and row["email"] == ""


# ─── the share and its holders ───────────────────────────────────────


def _hold(w, share_id, who=HOLDER):
    from src.access.resolver import _sync_engine  # noqa: F401 — the patched engine
    from src.mcp_server import token_store
    from tests.api.rbac_world import A_REPO

    _token, view = token_store.mint(
        kind="pat", workspace_id=WS_A, user_id=w.uid(who), issued_by="master-admin",
        label="t", patterns=[A_REPO], profile="dev", expires_in_days=30, shared=[share_id])
    return view.id


async def test_the_detail_is_exactly_these_keys_and_the_people_are_named_without_their_tokens(sw):
    share_id = add_share(sw, deny=["secrets/**"])
    token_id = _hold(sw, share_id)
    add_call(sw, [share_id])
    r = await sw.client.get(f"/api/sharing/shares/{share_id}", headers=sw.h("admin_b", "ws-b"))
    body = r.json()
    assert set(body) == DETAIL_KEYS
    assert body["slug"] == "github_bco-b_secret" and body["status"] == "active"
    assert body["deny_globs"] == ["secrets/**"] and body["deny_count"] == 1
    assert body["grantee_workspace_id"] == WS_A and body["grantee_name"] == "Alpha"
    assert body["holders"] == 1 and body["last_used_at"]
    (person,) = body["people"]
    assert set(person) == {"user_id", "name", "email", "tokens", "last_used_at"}
    assert (person["email"], person["tokens"]) == (HOLDER_EMAIL, 1) and person["last_used_at"]
    assert token_id not in r.text


async def test_the_readers_are_listed_by_the_name_the_owner_reads_and_not_by_id(sw):
    """Their ids run the other way round (`u-admin_a` before `u-member_a`), and the
    owner never sees an id: the list must not look shuffled."""
    import src.users.store as users_mod

    store = users_mod._default_store
    for who, name in (("admin_a", "Zoe"), ("member_a", "bob"), ("viewer_a", "Alice")):
        person = store.get_by_id(sw.uid(who))
        person.name = name
        store.update(person)
    share_id = add_share(sw)
    for who in ("admin_a", "member_a", "viewer_a"):
        _hold(sw, share_id, who)
    body = (await sw.client.get(f"/api/sharing/shares/{share_id}",
                                headers=sw.h("admin_b", "ws-b"))).json()
    assert [p["name"] for p in body["people"]] == ["Alice", "bob", "Zoe"]


async def test_the_list_gives_a_summary_per_share_with_its_last_use(sw):
    share_id = add_share(sw)
    _hold(sw, share_id)
    add_call(sw, [share_id], ago=timedelta(hours=3))
    (row,) = (await sw.client.get("/api/sharing/shares", headers=sw.h("admin_b", "ws-b"))).json()
    assert set(row) == SUMMARY_KEYS
    assert row["holders"] == 1 and row["last_used_at"] and row["share_id"] == share_id


async def test_people_who_were_blocked_are_listed_by_name(sw):
    share_id = add_share(sw, blocked=[sw.uid("member_a")])
    body = (await sw.client.get(f"/api/sharing/shares/{share_id}",
                                headers=sw.h("admin_b", "ws-b"))).json()
    assert [(p["user_id"], p["email"]) for p in body["blocked"]] == [(sw.uid("member_a"), HOLDER_EMAIL)]


# ─── narrowing ───────────────────────────────────────────────────────


async def _patch(w, share_id, body, who="admin_b"):
    return await w.client.patch(f"/api/sharing/shares/{share_id}", json=body,
                                headers=w.h(who, "ws-b"))


async def test_the_owner_hides_more_paths_and_the_audit_says_what_changed(sw):
    share_id = add_share(sw, deny=["docs/**"])
    r = await _patch(sw, share_id, {"add_deny": ["internal/**", "docs/**"]})
    assert r.status_code == 200 and r.json()["deny_globs"] == ["docs/**", "internal/**"]
    assert share_row(sw, share_id).deny_globs == ["docs/**", "internal/**"]
    (row,) = audit_rows(sw, "sharing.narrowed")
    assert row["workspace_id"] == WS_B and row["target"] == share_id
    assert row["detail"]["changes"]["deny_globs"] == {
        "before": ["docs/**"], "after": ["docs/**", "internal/**"]}
    assert row["actor_id"] == sw.uid("admin_b")


async def test_an_earlier_end_fewer_people_no_questions_and_a_blocked_person_all_narrow(sw):
    share_id = add_share(sw, ask=True, people=5, days=30)
    soon = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    r = await _patch(sw, share_id, {
        "expires_at": soon, "allow_ask": False, "max_people": 2, "block": [sw.uid("member_a")]})
    assert r.status_code == 200, r.text
    row = share_row(sw, share_id)
    assert row.allow_ask is False and row.max_people == 2
    assert row.blocked_user_ids == [sw.uid("member_a")]
    assert row.expires_at.replace(tzinfo=UTC) < datetime.now(UTC) + timedelta(days=3)
    (audit,) = audit_rows(sw, "sharing.narrowed")
    assert set(audit["detail"]["changes"]) == {"expires_at", "allow_ask", "max_people",
                                               "blocked_user_ids"}
    assert audit["detail"]["changes"]["allow_ask"] == {"before": True, "after": False}


async def test_widening_is_refused_with_the_licence_sentence_and_changes_nothing(sw):
    share_id = add_share(sw, ask=False, people=3, days=10)
    later = (datetime.now(UTC) + timedelta(days=60)).isoformat()
    for body in ({"expires_at": later}, {"allow_ask": True}, {"max_people": 9},
                 # one widening beside a narrowing: the whole request is refused
                 {"add_deny": ["x/**"], "max_people": 9}):
        r = await _patch(sw, share_id, body)
        assert r.status_code == 409, (body, r.text)
        assert r.json()["detail"] == (
            "widening a share needs a Celmis Enterprise licence (sharing); "
            "with one, use POST /api/sharing-grants/{id}/extend")
    row = share_row(sw, share_id)
    assert (row.deny_globs, row.allow_ask, row.max_people) == ([], False, 3)
    assert audit_rows(sw, "sharing.narrowed") == []


async def test_a_field_that_is_not_a_narrowing_is_refused_as_unknown(sw):
    """A client that sends `deny_globs` (a replacement) or `unblock` must be told,
    not have the field dropped and think it was applied."""
    share_id = add_share(sw, deny=["a/**"], blocked=[sw.uid("member_a")])
    for body in ({"deny_globs": []}, {"unblock": [sw.uid("member_a")]}, {"repo_slug": "x"},
                 {"revoked_at": None}, {"grantee_workspace_id": WS_B}):
        r = await _patch(sw, share_id, body)
        assert r.status_code == 422, (body, r.text)
    row = share_row(sw, share_id)
    assert row.deny_globs == ["a/**"] and row.blocked_user_ids == [sw.uid("member_a")]


async def test_a_pattern_that_cannot_be_kept_is_refused_and_leaves_the_share_alone(sw):
    """Any text is a pattern (one that matches nothing hides nothing); a NUL, a
    pattern longer than a path and a list longer than the limit are not."""
    share_id = add_share(sw, deny=["a/**"])
    for body in ({"add_deny": ["ok/**", "a\x00b"]}, {"add_deny": ["ok/**", "x" * 500]},
                 {"add_deny": [5]}, {"add_deny": "a/**"},
                 {"add_deny": [f"p{i}/**" for i in range(101)]}):
        r = await _patch(sw, share_id, body)
        assert r.status_code == 422, (str(body)[:60], r.text[:200])
    assert share_row(sw, share_id).deny_globs == ["a/**"]
    assert (await _patch(sw, share_id, {"add_deny": ["   ", ""]})).status_code == 200
    assert share_row(sw, share_id).deny_globs == ["a/**"]


async def test_fewer_people_than_hold_it_is_refused(sw):
    share_id = add_share(sw, people=5)
    _hold(sw, share_id, "member_a")
    _hold(sw, share_id, "viewer_a")
    r = await _patch(sw, share_id, {"max_people": 1})
    assert r.status_code == 422
    assert (await _patch(sw, share_id, {"max_people": 2})).status_code == 200


async def test_a_person_switched_off_stops_counting_and_the_ceiling_can_follow(sw):
    """Switching somebody off is the way to make room for a lower ceiling: their
    token reads nothing any more, so it neither counts as a holder nor blocks the
    PATCH that lowers `max_people` to the people who are left."""
    share_id = add_share(sw, people=5)
    _hold(sw, share_id, "member_a")
    _hold(sw, share_id, "viewer_a")
    assert (await _patch(sw, share_id, {"max_people": 1})).status_code == 422

    r = await _patch(sw, share_id, {"block": [sw.uid("member_a")]})
    assert r.status_code == 200 and r.json()["holders"] == 1
    assert [p["user_id"] for p in r.json()["people"]] == [sw.uid("viewer_a")]
    assert [p["user_id"] for p in r.json()["blocked"]] == [sw.uid("member_a")]

    r = await _patch(sw, share_id, {"max_people": 1})
    assert r.status_code == 200, r.text
    assert share_row(sw, share_id).max_people == 1


async def test_switching_off_and_lowering_the_ceiling_can_be_one_request(sw):
    share_id = add_share(sw, people=5)
    _hold(sw, share_id, "member_a")
    _hold(sw, share_id, "viewer_a")
    r = await _patch(sw, share_id, {"max_people": 1, "block": [sw.uid("member_a")]})
    assert r.status_code == 200, r.text
    row = share_row(sw, share_id)
    assert row.max_people == 1 and row.blocked_user_ids == [sw.uid("member_a")]
    (audit,) = audit_rows(sw, "sharing.narrowed")
    assert set(audit["detail"]["changes"]) == {"max_people", "blocked_user_ids"}


async def test_switching_off_the_wrong_person_does_not_make_room(sw):
    share_id = add_share(sw, people=5)
    _hold(sw, share_id, "member_a")
    _hold(sw, share_id, "viewer_a")
    r = await _patch(sw, share_id, {"max_people": 1, "block": [sw.uid("admin_a")]})
    assert r.status_code == 422
    row = share_row(sw, share_id)
    assert row.max_people == 5 and row.blocked_user_ids == []


async def test_an_ended_share_cannot_be_narrowed(sw):
    share_id = add_share(sw, revoked=True)
    assert (await _patch(sw, share_id, {"add_deny": ["x/**"]})).status_code == 422
    assert share_row(sw, share_id).deny_globs == []


async def test_nothing_changed_writes_nothing(sw):
    share_id = add_share(sw, deny=["a/**"])
    assert (await _patch(sw, share_id, {"add_deny": ["a/**"]})).status_code == 200
    assert (await _patch(sw, share_id, {})).status_code == 200
    assert audit_rows(sw, "sharing.narrowed") == []


# ─── ending ──────────────────────────────────────────────────────────


async def _revoke(w, share_id, who="admin_b"):
    return await w.client.post(f"/api/sharing/shares/{share_id}/revoke", headers=w.h(who, "ws-b"))


async def test_the_owner_ends_a_share_and_the_trail_says_who_and_why(sw):
    share_id = add_share(sw)
    _hold(sw, share_id)
    r = await _revoke(sw, share_id)
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert r.json()["revoke_reason"] == "owner" and r.json()["revoked_at"]
    row = share_row(sw, share_id)
    assert row.revoked_at is not None and row.revoked_by == sw.uid("admin_b")
    (audit,) = audit_rows(sw, "sharing.revoked")
    assert audit["workspace_id"] == WS_B and audit["target"] == share_id
    assert audit["detail"] == {"reason": "owner", "holders": 1}


async def test_the_superadmin_who_is_not_the_owners_administrator_ends_it_as_the_platform(sw):
    share_id = add_share(sw)
    assert (await _revoke(sw, share_id, who="su")).json()["revoke_reason"] == "superadmin"


async def test_ending_a_share_twice_is_fine_and_writes_one_row(sw):
    share_id = add_share(sw)
    assert (await _revoke(sw, share_id)).status_code == 200
    assert (await _revoke(sw, share_id)).status_code == 200
    assert len(audit_rows(sw, "sharing.revoked")) == 1


async def test_ending_a_share_leaves_the_holders_tokens_alone(sw):
    """Their tokens are theirs; the share id on them just stops counting."""
    share_id = add_share(sw)
    token_id = _hold(sw, share_id)
    await _revoke(sw, share_id)
    from sqlalchemy.orm import Session

    from src.db.models import McpToken

    with Session(sw.engine) as s:
        token = s.get(McpToken, token_id)
        assert token.revoked_at is None and token.shared_repos == [share_id]


async def test_every_owner_side_row_is_in_the_owners_workspace_and_none_in_the_receivers(sw):
    share_id = add_share(sw)
    await _patch(sw, share_id, {"add_deny": ["x/**"]})
    await _revoke(sw, share_id)
    written = actions(sw, "sharing.")
    assert [a for a, _, _ in written] == ["sharing.narrowed", "sharing.revoked"]
    assert {ws for _, ws, _ in written} == {WS_B}
