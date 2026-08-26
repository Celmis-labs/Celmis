"""A saved Claude token has been shown to work, and the row says so.

Before this, a save was gated by `token_looks_valid` — a prefix and a length —
and `status()` reported presence only. An expired, revoked or half-pasted
token stored as "connected" and the operator found out at the first review.

These tests pin the three things that fix costs:

    1. The probe is the one the CLI itself would make with this token
       (Authorization: Bearer + anthropic-beta: oauth-2025-04-20, one word in,
       one token out). The transport is faked; everything above it is real.
    2. A refusal from Anthropic is a refusal to store. An inconclusive answer
       is NOT — the paste survives, marked unverified, because the operator's
       wifi is not evidence about their credential.
    3. status() answers presence and validity separately, and never calls out.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from src.agent import connection as conn

# Obviously fake, and shaped like the real thing so the cheap gate passes.
TOKEN = "sk-ant-oat01-fake-token-for-tests-0000000000"
USER = "user-1"
WS = "ws-1"


# ─── Transport double ────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    """Stands in exactly at the boundary `verify_token` reaches the network at.

    The URL, the headers, the request body and the status → verdict mapping
    above it are the shipping code, which is the only way this suite can say
    anything about the probe at all — there is no Claude token in CI.
    """

    def __init__(self, responder):
        self._responder = responder
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self._responder()


class _FakeStore:
    def __init__(self):
        self.rows: dict[tuple[str, str, str], SimpleNamespace] = {}

    def save(self, *, provider, secret, metadata=None, user_id="", account_label="default"):
        self.rows[(provider, user_id, account_label)] = SimpleNamespace(
            secret=secret, metadata=dict(metadata or {}),
        )

    def load(self, *, provider, user_id="", account_label="default"):
        return self.rows.get((provider, user_id, account_label))

    def delete(self, *, provider, user_id="", account_label="default"):
        return self.rows.pop((provider, user_id, account_label), None) is not None


@pytest.fixture
def store():
    fake = _FakeStore()
    with patch("src.credentials.get_credential_store", return_value=fake):
        yield fake


def _transport(*responses):
    """Patch the probe's client. Each element is a response or an exception."""
    queue = list(responses)

    def responder():
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item

    client = _FakeClient(responder)
    return patch.object(conn, "_probe_client", lambda: client), client


def _accepted():
    return _FakeResponse(200, {"id": "msg_1", "content": [], "stop_reason": "max_tokens"})


def _refused():
    return _FakeResponse(401, {"type": "error", "error": {
        "type": "authentication_error", "message": "OAuth token has expired"}})


def _row(store, slot=USER):
    return store.rows[(conn.PROVIDER, slot, "default")]


def _age_last_check(store, slot, *, seconds):
    """Push the row's last-check timestamp back past the cache window."""
    meta = _row(store, slot).metadata
    meta["verify_checked_at"] = (
        datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


# ─── 1. The probe is the CLI's probe ─────────────────────────────────


def test_the_probe_carries_the_headers_the_cli_uses_for_this_token(store):
    """A setup-token is an OAuth access token: Bearer plus the oauth beta
    header. Sent as an x-api-key it would be rejected, and a save that
    "verified" against the wrong auth scheme would be worse than none."""
    patcher, client = _transport(_accepted())
    with patcher:
        conn.save_token(token=TOKEN, user_id=USER, workspace_id=WS,
                        scope="personal", saved_by="dev@test")

    call = client.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert call["headers"]["anthropic-beta"] == "oauth-2025-04-20"
    assert call["headers"]["anthropic-version"] == "2023-06-01"


def test_the_probe_is_one_word_in_and_one_token_out(store):
    """The only surface a setup-token can reach is inference, so the probe is
    a turn — and it is the smallest turn there is, because the Test button is
    spending somebody's subscription."""
    patcher, client = _transport(_accepted())
    with patcher:
        conn.verify_token(TOKEN)

    body = client.calls[0]["json"]
    assert body["max_tokens"] == 1
    assert body["model"] == conn.PROBE_MODEL
    assert len(body["messages"]) == 1
    assert body["system"].startswith("You are Claude Code")


def test_an_obviously_wrong_paste_never_reaches_the_network(store):
    """The cheap gate stays first: telling somebody they pasted their email
    address does not need a round trip to Anthropic."""
    patcher, client = _transport(_accepted())
    with patcher, pytest.raises(conn.TokenRejected):
        conn.save_token(token="hunter2-not-a-token", user_id=USER, workspace_id=WS,
                        scope="personal", saved_by="dev@test")

    assert client.calls == []
    assert store.rows == {}


# ─── 2. Refused is not stored; unreachable is ────────────────────────


def test_a_token_anthropic_refuses_is_not_stored(store):
    patcher, _ = _transport(_refused())
    with patcher, pytest.raises(conn.TokenRejected) as exc:
        conn.save_token(token=TOKEN, user_id=USER, workspace_id=WS,
                        scope="personal", saved_by="dev@test")

    assert "expired" in exc.value.reason
    assert store.rows == {}


def test_a_token_anthropic_accepts_is_stored_with_the_moment_it_passed(store):
    patcher, _ = _transport(_accepted())
    with patcher:
        result = conn.save_token(token=TOKEN, user_id=USER, workspace_id=WS,
                                 scope="personal", saved_by="dev@test")

    assert result.ok is True
    meta = _row(store).metadata
    assert meta["saved_by"] == "dev@test"
    stamped = datetime.fromisoformat(meta["verified_at"])
    assert (datetime.now(UTC) - stamped).total_seconds() < 60
    assert conn.status(USER, WS)["personal_state"] == conn.STATE_VERIFIED


def test_a_network_blip_during_save_keeps_the_paste_and_calls_it_unverified(store):
    """The decision: an answer we never got is not evidence about the token.

    Dropping it would cost the operator a trip back to their own machine to
    re-run `claude setup-token`; storing it as "connected" would be the bug
    this whole change exists to kill. So it is stored and it is labelled, and
    the label is what the UI renders.
    """
    patcher, _ = _transport(httpx.ConnectError("[Errno 8] nodename nor servname provided"))
    with patcher:
        result = conn.save_token(token=TOKEN, user_id=USER, workspace_id=WS,
                                 scope="personal", saved_by="dev@test")

    assert (result.ok, result.conclusive) == (False, False)
    assert _row(store).secret == TOKEN
    assert "verified_at" not in _row(store).metadata

    out = conn.status(USER, WS)
    assert out["personal"] is True                       # present
    assert out["personal_state"] == conn.STATE_UNREACHABLE   # and not valid
    assert "could not reach Anthropic" in out["personal_reason"]


@pytest.mark.parametrize("status_code", [429, 500, 404])
def test_an_answer_that_is_not_a_verdict_neither_verifies_nor_refuses(store, status_code):
    """A rate limit, a bad gateway, a probe model Anthropic has since retired —
    none of those says anything about this credential. Only 200 may write
    "verified", and only 401/403 may throw the paste away."""
    patcher, _ = _transport(_FakeResponse(status_code, {"type": "error", "error": {
        "type": "api_error", "message": "upstream sad"}}))
    with patcher:
        result = conn.save_token(token=TOKEN, user_id=USER, workspace_id=WS,
                                 scope="personal", saved_by="dev@test")

    assert result.conclusive is False
    assert _row(store).secret == TOKEN
    assert conn.status(USER, WS)["personal_state"] == conn.STATE_UNREACHABLE


# ─── 3. status() answers both questions, and asks nobody ─────────────


def test_status_tells_never_checked_from_verified_from_stale_from_failed(store):
    """Four rows, four different truths. A UI that can only say "connected"
    puts the same green tick on all of them."""
    now = datetime.now(UTC)
    old = (now - conn.STALE_AFTER - timedelta(hours=1)).isoformat()
    fresh = now.isoformat()

    def put(slot, metadata):
        store.save(provider=conn.PROVIDER, secret=TOKEN, metadata=metadata,
                   user_id=slot, account_label="default")

    put("never", {"saved_by": "a@test"})
    assert conn.status("never", WS)["personal_state"] == conn.STATE_NEVER_CHECKED

    put("good", {"verified_at": fresh, "verify_checked_at": fresh,
                 "verify_reason": "", "verify_conclusive": True})
    assert conn.status("good", WS)["personal_state"] == conn.STATE_VERIFIED

    put("old", {"verified_at": old, "verify_checked_at": old,
                "verify_reason": "", "verify_conclusive": True})
    stale = conn.status("old", WS)
    assert stale["personal_state"] == conn.STATE_STALE
    assert stale["personal_verified_at"] == old

    put("dead", {"verified_at": old, "verify_checked_at": fresh,
                 "verify_reason": "authentication_error: revoked",
                 "verify_conclusive": True})
    failed = conn.status("dead", WS)
    assert failed["personal_state"] == conn.STATE_FAILED
    assert failed["personal_reason"] == "authentication_error: revoked"
    # Still reports when it last worked — "good until yesterday" is the clue
    # that tells an operator this was a revocation, not a bad paste.
    assert failed["personal_verified_at"] == old


def test_status_is_a_read_and_never_a_provider_call(store):
    """It renders on every page load. One probe per page view would be a bill
    and a rate limit; worse, it would make the status page the thing that
    breaks when Anthropic has a bad afternoon."""
    patcher, client = _transport(_accepted())
    with patcher:
        conn.save_token(token=TOKEN, user_id=USER, workspace_id=WS,
                        scope="personal", saved_by="dev@test")
        for _ in range(5):
            conn.status(USER, WS)

    assert len(client.calls) == 1        # the save, and nothing since


def test_status_reports_the_workspace_slot_separately(store):
    patcher, _ = _transport(_accepted())
    with patcher:
        conn.save_token(token=TOKEN, user_id=USER, workspace_id=WS,
                        scope="workspace", saved_by="admin@test")

    out = conn.status(USER, WS)
    assert (out["personal"], out["personal_state"]) == (False, conn.STATE_ABSENT)
    assert out["workspace"] is True
    assert out["workspace_state"] == conn.STATE_VERIFIED
    assert out["workspace_saved_by"] == "admin@test"


# ─── 4. Re-check, and the rate limit on it ───────────────────────────


def test_a_recheck_writes_the_new_answer_onto_the_row(store):
    """A token that was good and has since been revoked: the row stays (the
    operator's paste is not ours to discard) and its state flips."""
    patcher, _ = _transport(_accepted())
    with patcher:
        conn.save_token(token=TOKEN, user_id=USER, workspace_id=WS,
                        scope="personal", saved_by="dev@test")
    _age_last_check(store, USER, seconds=conn.VERIFY_CACHE_SECONDS + 60)

    patcher, _ = _transport(_refused())
    with patcher:
        result = conn.recheck_token(user_id=USER, workspace_id=WS, scope="personal")

    assert (result.ok, result.conclusive) == (False, True)
    assert _row(store).secret == TOKEN
    assert conn.status(USER, WS)["personal_state"] == conn.STATE_FAILED
    assert "expired" in conn.status(USER, WS)["personal_reason"]


def test_a_recheck_inside_the_cache_window_does_not_ask_again(store):
    """The Test button must survive being leaned on. Nothing an extra request
    could reveal changes inside five minutes."""
    patcher, client = _transport(_accepted())
    with patcher:
        conn.save_token(token=TOKEN, user_id=USER, workspace_id=WS,
                        scope="personal", saved_by="dev@test")
        first = conn.recheck_token(user_id=USER, workspace_id=WS, scope="personal")
        second = conn.recheck_token(user_id=USER, workspace_id=WS, scope="personal")

    assert len(client.calls) == 1              # only the save probed
    assert (first.cached, second.cached) == (True, True)
    assert first.ok is True


def test_a_recheck_asks_again_once_the_window_has_passed(store):
    patcher, client = _transport(_accepted())
    with patcher:
        conn.save_token(token=TOKEN, user_id=USER, workspace_id=WS,
                        scope="personal", saved_by="dev@test")
        _age_last_check(store, USER, seconds=conn.VERIFY_CACHE_SECONDS + 1)
        result = conn.recheck_token(user_id=USER, workspace_id=WS, scope="personal")

    assert len(client.calls) == 2
    assert (result.ok, result.cached) == (True, False)


def test_a_recheck_of_an_empty_slot_says_so_instead_of_probing(store):
    patcher, client = _transport(_accepted())
    with patcher:
        assert conn.recheck_token(
            user_id=USER, workspace_id=WS, scope="personal") is None
    assert client.calls == []


# ─── 5. The token never leaves this module ───────────────────────────


def test_nothing_on_the_way_out_carries_the_token(store, caplog):
    """Rows, reasons, statuses and log lines. The provider does not echo the
    token back today; a reason string that reached a log file would be a
    credential in plaintext, so it is scrubbed rather than trusted."""
    echoed = _FakeResponse(401, {"type": "error", "error": {
        "type": "authentication_error",
        "message": f"token {TOKEN} is not valid"}})

    caplog.set_level(logging.DEBUG)
    patcher, _ = _transport(echoed)
    with patcher, pytest.raises(conn.TokenRejected) as exc:
        conn.save_token(token=TOKEN, user_id=USER, workspace_id=WS,
                        scope="personal", saved_by="dev@test")

    assert TOKEN not in exc.value.reason
    assert "sk-ant-…" in exc.value.reason
    for record in caplog.records:
        assert TOKEN not in record.getMessage()


def test_a_status_payload_never_carries_the_token(store):
    patcher, _ = _transport(_accepted())
    with patcher:
        conn.save_token(token=TOKEN, user_id=USER, workspace_id=WS,
                        scope="personal", saved_by="dev@test")
    assert TOKEN not in repr(conn.status(USER, WS))
