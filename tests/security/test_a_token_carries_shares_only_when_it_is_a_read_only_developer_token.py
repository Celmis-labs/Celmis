"""Where a share id may be written, and where it is read.

`mcp_tokens.shared_repos` is a column beside the repository list. The issuing code
refuses it on any token a share cannot work on (not a `pat`, not the `dev`
profile, or able to write), so a caller that thinks it granted a share finds out it
did not; the verifier ignores the column on such a token even when somebody wrote
ids into it in the database; and a token may have an empty list of its own only
because it has a share.
"""

from __future__ import annotations

import types
import uuid

import pytest

from src.mcp_server import token_store
from tests.security.mcp_world import WILD, WS_A
from tests.security.share_world import make_share

IDS = [str(uuid.uuid4()) for _ in range(12)]


def _row(world, **over):
    kw = dict(kind="pat", workspace_id=WS_A, user_id=world.users["mn"],
              issued_by=world.users["su"], label="t", patterns=[WILD], profile="dev")
    kw.update(over)
    with world.session() as s:
        row = token_store.create_row(s, **kw)
        return types.SimpleNamespace(id=row.id, shared_repos=row.shared_repos,
                                     repo_patterns=row.repo_patterns)


# ─── the shape of the ids ────────────────────────────────────────────


def test_no_shares_is_the_empty_list_whatever_the_caller_passed():
    for none in (None, [], ()):
        assert token_store.normalise_shares(none, kind="cli", profile="full", allow_write=True) == []


def test_ids_are_kept_once_each_in_order():
    got = token_store.normalise_shares([IDS[1], IDS[0], IDS[1]], kind="pat", profile="dev",
                                       allow_write=False)
    assert got == [IDS[1], IDS[0]]


@pytest.mark.parametrize("bad", [
    "x", "", "share:" + IDS[0], IDS[0].upper(), IDS[0] + " ", None, 5, ["nested"], IDS[0][:-1],
])
def test_something_that_is_not_an_id_is_refused_not_dropped(bad):
    with pytest.raises(token_store.TokenError, match="not a share id"):
        token_store.normalise_shares([IDS[0], bad], kind="pat", profile="dev", allow_write=False)


@pytest.mark.parametrize("kind,profile,write", [
    ("cli", "dev", False), ("self", "dev", False), ("oauth_grant", "dev", False),
    ("internal", "dev", False), ("pat", "full", False), ("pat", "dev", True),
])
def test_a_token_a_share_cannot_work_on_is_refused_one(kind, profile, write):
    with pytest.raises(token_store.TokenError, match="read-only developer token"):
        token_store.normalise_shares([IDS[0]], kind=kind, profile=profile, allow_write=write)


def test_at_most_ten_shares_to_a_token():
    ok = token_store.normalise_shares(IDS[:10], kind="pat", profile="dev", allow_write=False)
    assert len(ok) == 10
    with pytest.raises(token_store.TokenError):
        token_store.normalise_shares(IDS[:11], kind="pat", profile="dev", allow_write=False)


# ─── the row ─────────────────────────────────────────────────────────


def test_a_token_without_shares_stores_none_so_a_plain_row_is_byte_for_byte_the_old_one(mcp_world):
    row = _row(mcp_world)
    assert row.shared_repos is None and row.repo_patterns == [WILD]
    assert token_store.lookup(row.id).shares == ()


def test_a_token_with_a_share_stores_the_id_beside_the_list_and_not_in_it(mcp_world):
    s1 = make_share(mcp_world)
    row = _row(mcp_world, shared=[s1, s1])
    assert row.shared_repos == [s1] and row.repo_patterns == [WILD]
    view = token_store.lookup(row.id)
    assert view.shares == (s1,) and view.shares_apply


def test_a_token_that_lists_only_a_share_has_an_empty_list_of_its_own(mcp_world):
    s1 = make_share(mcp_world)
    row = _row(mcp_world, patterns=[], shared=[s1])
    assert row.repo_patterns == [] and row.shared_repos == [s1]
    view = token_store.lookup(row.id)
    assert view.patterns == () and view.authoritative


def test_a_token_with_nothing_to_read_is_refused(mcp_world):
    with pytest.raises(token_store.TokenError, match="at least one repository"):
        _row(mcp_world, patterns=[])
    with pytest.raises(token_store.TokenError, match="at least one repository"):
        _row(mcp_world, patterns=[], shared=[])


@pytest.mark.parametrize("over", [
    {"kind": "cli"}, {"kind": "oauth_grant"}, {"kind": "self"}, {"profile": "full"},
    {"allow_write": True, "profile": "full"},
])
def test_an_issue_of_the_wrong_kind_with_a_share_fails_and_leaves_no_row(mcp_world, over):
    from sqlalchemy import func, select

    from src.db.models import McpToken

    s1 = make_share(mcp_world)
    with pytest.raises(token_store.TokenError):
        _row(mcp_world, shared=[s1], **over)
    with mcp_world.session() as s:
        assert s.scalar(select(func.count()).select_from(McpToken)) == 0


# ─── the verifier ────────────────────────────────────────────────────


@pytest.mark.parametrize("over", [
    {"kind": "cli"}, {"kind": "oauth_grant"}, {"kind": "self"}, {"profile": "full"},
])
def test_a_column_written_behind_the_issuers_back_is_not_a_share_on_such_a_token(mcp_world, over):
    s1 = make_share(mcp_world)
    row = _row(mcp_world, **over)
    from tests.security.share_world import tamper_token

    tamper_token(mcp_world, row.id, shared_repos=[s1])
    view = token_store.lookup(row.id)
    assert view.shares == (s1,), "it is stored as written"
    assert not view.shares_apply, "and it does not count"


def test_a_token_that_can_write_does_not_count_its_shares(mcp_world):
    from tests.security.share_world import tamper_token

    row = _row(mcp_world, shared=[make_share(mcp_world)])
    tamper_token(mcp_world, row.id, allow_write=True)
    assert not token_store.lookup(row.id).shares_apply


def test_a_corrupt_column_is_no_shares_rather_than_an_error(mcp_world):
    from tests.security.share_world import tamper_token

    row = _row(mcp_world)
    for junk in ("a string", {"a": 1}, 7, [1, None, "x"], [["nested"]]):
        tamper_token(mcp_world, row.id, shared_repos=junk)
        assert token_store.lookup(row.id).shares == ()


def test_the_shares_of_a_token_come_from_its_row_and_a_change_is_seen_at_once(mcp_world):
    s1 = make_share(mcp_world)
    row = _row(mcp_world, shared=[s1])
    assert token_store.lookup(row.id).shares == (s1,)
    with mcp_world.session() as s:
        token_store.update_row(s, row.id, shares=[])
    assert token_store.lookup(row.id).shares == ()


# ─── changing them ───────────────────────────────────────────────────


def test_changing_the_shares_of_a_token_replaces_them(mcp_world):
    s1 = make_share(mcp_world)
    row = _row(mcp_world, shared=[s1])
    other = IDS[0]
    with mcp_world.session() as s:
        token_store.update_row(s, row.id, shares=[other])
    assert mcp_world.row(row.id).shared_repos == [other]


def test_a_token_that_cannot_hold_a_share_cannot_be_given_one_later(mcp_world):
    row = _row(mcp_world, kind="cli")
    with mcp_world.session() as s, pytest.raises(token_store.TokenError):
        token_store.update_row(s, row.id, shares=[IDS[0]])
    assert mcp_world.row(row.id).shared_repos is None


def test_the_other_changes_to_a_token_leave_its_shares_alone(mcp_world):
    s1 = make_share(mcp_world)
    row = _row(mcp_world, shared=[s1])
    with mcp_world.session() as s:
        token_store.update_row(s, row.id, label="renamed", expires_in_days=3)
    assert mcp_world.row(row.id).shared_repos == [s1]
