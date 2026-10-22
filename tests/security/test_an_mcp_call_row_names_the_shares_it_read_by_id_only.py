"""The MCP call log says which shares a call read through, by id, and never which repository.

The log belongs to the receiving workspace, and a shared repository is the
owner's. So (src/mcp_server/callctx.py, audit.py):

  * a call that read a shared repository notes the share's id, not the repository:
    `callctx.note_repos` never gets its slug, `note_shares` gets the id;
  * only ids are kept, so a slug, a title or a path passed by mistake logs nothing;
  * the row for the call carries the ids in `mcp_call_log.shares` (empty becomes
    NULL) and `repos` holds the receiver's own repositories only;
  * the `mcp.call` row of the action audit, which workspace owners read, is the
    same whether or not a share was read: it has neither `shares` nor `packs`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.db.models import McpCallLog
from src.mcp_server import audit, callctx
from tests.security.mcp_world import SHARED, WILD
from tests.security.share_world import holder_token, make_share

S1, S2 = str(uuid.uuid4()), str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _no_call_in_flight():
    callctx.reset()
    yield
    callctx.reset()


def _row(rec, **extra):
    base = {"ts": datetime.now(UTC), "tool": rec.tool, "profile": rec.profile,
            "repos": sorted(rec.repos), "packs": sorted(rec.packs),
            "shares": sorted(rec.shares), "status": rec.status,
            "result_bytes": 1, "result_items": 1, "duration_ms": 1, "args_hash": "h",
            "workspace_id": "wsid-a", "user_id": "u", "token_id": None, "kind": "pat",
            "client_id": ""}
    return {**base, **extra}


def test_only_ids_are_kept_and_repeats_collapse():
    rec = callctx.begin("find")
    callctx.note_shares(S1, [S2, S1], (S1,), {S2})
    callctx.note_shares(SHARED, "a title", "src/app.py", "", None, 7, S1.upper(), S1.replace("-", ""))
    assert rec.shares == {S1, S2}


def test_noting_a_share_with_no_call_in_flight_is_nothing_and_no_error():
    callctx.note_shares(S1)
    assert callctx.current() is None


def test_a_call_that_noted_nothing_has_no_shares():
    rec = callctx.begin("find")
    assert rec.shares == set()


def test_a_holders_call_notes_the_share_it_read_and_not_the_repository(mcp_world):
    from src.mcp_server.dev_profile import access

    s1 = make_share(mcp_world)
    bearer, _view = holder_token(mcp_world, [s1], own=[WILD])
    mcp_world.as_(bearer)
    rec = callctx.begin("find")
    scope = access.mcp_scope()
    scope.get(SHARED)
    assert rec.shares == {s1}
    assert rec.repos == {WILD}
    assert SHARED not in repr(rec.repos)


def test_deciding_what_a_holder_may_read_is_not_reading_the_share(mcp_world):
    """The owner reads the usage of a share from this log. Every dev tool decides on
    the whole workspace first, so a share noted at that point would put every call of
    a person who holds one into the owner's view as a read."""
    from src.mcp_server.dev_profile import access
    from src.mcp_server.identity import caller_access

    s1 = make_share(mcp_world)
    bearer, _view = holder_token(mcp_world, [s1], own=[WILD])
    mcp_world.as_(bearer)
    rec = callctx.begin("find")
    _caller, decided = caller_access([WILD, SHARED])
    assert decided[SHARED].via_share == s1, "it is decided, and readable"
    assert rec.shares == set() and rec.repos == {WILD}
    scope = access.mcp_scope()                      # the whole workspace, as every tool asks
    scope.get(WILD)
    assert rec.shares == set(), "reading an own repository is not reading the share"
    scope.get(SHARED)
    assert rec.shares == {s1}


def test_touching_a_shared_repository_through_a_scope_notes_the_share_not_the_slug(mcp_world):
    from src.mcp_server.dev_profile import access

    s1 = make_share(mcp_world)
    bearer, _view = holder_token(mcp_world, [s1], own=[WILD])
    mcp_world.as_(bearer)
    rec = callctx.begin("read_symbol")
    scope = access.mcp_scope()
    scope.get(SHARED)
    scope.get(WILD)
    assert rec.shares == {s1} and rec.repos == {WILD}


def test_the_same_goes_for_the_scope_every_other_tool_holds(mcp_world):
    from src.access.effective import mcp_scope

    s1 = make_share(mcp_world)
    bearer, _view = holder_token(mcp_world, [s1], own=[WILD])
    mcp_world.as_(bearer)
    rec = callctx.begin("grep")
    scope = mcp_scope()
    scope.get(SHARED)
    assert rec.shares == {s1} and SHARED not in rec.repos


def test_a_call_that_read_only_its_own_repositories_has_no_shares_even_with_a_share_on_the_token(
        mcp_world):
    from src.mcp_server.identity import caller_access

    s1 = make_share(mcp_world)
    bearer, _view = holder_token(mcp_world, [s1], own=[WILD])
    mcp_world.as_(bearer)
    rec = callctx.begin("find")
    caller_access([WILD])
    assert rec.shares == set() and rec.repos == {WILD}


def test_a_share_that_no_longer_holds_is_not_noted(mcp_world):
    from src.mcp_server.identity import caller_access
    from tests.security.share_world import end_share

    s1 = make_share(mcp_world)
    bearer, _view = holder_token(mcp_world, [s1], own=[WILD])
    end_share(mcp_world, s1)
    mcp_world.as_(bearer)
    rec = callctx.begin("find")
    caller_access([WILD, SHARED])
    assert rec.shares == set()
    from src.mcp_server.dev_profile import access

    scope = access.mcp_scope()
    with pytest.raises(access.RepoNotAccessible):
        scope.get(SHARED)
    assert rec.shares == set()


def test_record_call_queues_the_ids_and_never_a_slug(monkeypatch):
    queued: list[dict] = []
    monkeypatch.setattr(audit, "_enqueue", queued.append)
    rec = callctx.begin("find")
    callctx.note_shares(S1, SHARED)
    callctx.note_repos(WILD)
    rec.args_hash = "digest"
    audit.record_call(rec)
    assert queued[0]["shares"] == [S1] and queued[0]["repos"] == [WILD]
    assert SHARED not in repr(queued)


def test_a_record_without_the_field_still_writes(monkeypatch):
    """An older queued row, or a stand-in record, has no `shares`."""
    queued: list[dict] = []
    monkeypatch.setattr(audit, "_enqueue", queued.append)

    class Old:
        tool, profile, repos, status = "find", "dev", {"r"}, "ok"
        result_bytes = result_items = duration_ms = 0
        args_hash = ""

    audit.record_call(Old())
    assert queued and queued[0]["shares"] == []


def test_a_refused_call_has_no_shares(monkeypatch):
    queued: list[dict] = []
    monkeypatch.setattr(audit, "_enqueue", queued.append)
    audit._DENIED_SEEN.clear()
    audit.record_denied({"sub": "user:u-x", "grant": str(uuid.uuid4())}, "This MCP token was revoked.")
    assert queued and queued[0]["shares"] == []


def test_the_row_carries_the_ids_and_the_action_audit_row_never_does(mcp_world, monkeypatch):
    mirrored: list[dict] = []
    import src.security.audit as security_audit

    monkeypatch.setattr(security_audit, "record_action", lambda **kw: mirrored.append(kw))
    with_shares = callctx.begin("find")
    callctx.note_shares(S2, S1)
    callctx.note_repos(WILD)
    audit.write_row(_row(with_shares))
    without = callctx.begin("grep")
    audit.write_row(_row(without))
    callctx.reset()

    with mcp_world.session() as s:
        rows = {x.tool: x for x in s.scalars(select(McpCallLog)).all()}
    assert rows["find"].shares == sorted([S1, S2]) and rows["find"].repos == [WILD]
    assert rows["grep"].shares is None, "empty is NULL, as for packs"

    by_tool = {m["target"]: m["detail"] for m in mirrored}
    assert set(by_tool) == {"find", "grep"}
    assert all("shares" not in d and "packs" not in d for d in by_tool.values())
    assert S1 not in repr(mirrored) and S2 not in repr(mirrored)


def test_a_call_through_a_share_leaves_a_row_that_names_the_id_and_not_the_repository(
        mcp_world, monkeypatch):
    mirrored: list[dict] = []
    import src.security.audit as security_audit

    monkeypatch.setattr(security_audit, "record_action", lambda **kw: mirrored.append(kw))
    from src.mcp_server.dev_profile import access

    s1 = make_share(mcp_world)
    bearer, _view = holder_token(mcp_world, [s1], own=[WILD])
    mcp_world.as_(bearer)
    rec = callctx.begin("find")
    scope = access.mcp_scope()
    scope.get(WILD)
    scope.get(SHARED)
    audit.write_row(_row(rec))
    callctx.reset()
    with mcp_world.session() as s:
        [row] = s.scalars(select(McpCallLog)).all()
    assert row.shares == [s1] and row.repos == [WILD]
    assert SHARED not in repr((row.repos, row.shares, row.args_hash)) and SHARED not in repr(mirrored)
    assert s1 not in repr(mirrored)
