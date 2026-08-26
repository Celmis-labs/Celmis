"""The Claude connection endpoints tell the operator the truth.

    PUT  /api/claude/connection       — refuses what Anthropic refuses, and
                                        says so in Anthropic's words
    POST /api/claude/test-connection  — re-checks a saved token without a
                                        re-paste, and writes the answer back
    GET  /api/claude/connection       — presence and validity, separately

The route functions are called directly (no TestClient) so the dependency
values are explicit: these tests are about what the handlers decide, and the
auth wiring has its own suite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException

from src.agent import connection as conn
from src.api.routers import claude_code as routes

TOKEN = "sk-ant-oat01-fake-token-for-tests-0000000000"
WS = "ws-1"
_ADMIN = SimpleNamespace(id="u-admin", email="admin@test", is_admin=True)
_MEMBER = SimpleNamespace(id="u-dev", email="dev@test", is_admin=False)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
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
    queue = list(responses)

    def responder():
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item

    client = _FakeClient(responder)
    return patch.object(conn, "_probe_client", lambda: client), client


def _accepted():
    return _FakeResponse(200, {"id": "msg_1", "content": []})


def _refused():
    return _FakeResponse(401, {"type": "error", "error": {
        "type": "authentication_error", "message": "OAuth token has been revoked"}})


def _save(user=_MEMBER, scope="personal"):
    return routes.save_connection(
        routes.ConnectionIn(token=TOKEN, scope=scope), user=user, workspace_id=WS)


def _test_connection(user=_MEMBER, scope="personal"):
    return routes.test_connection(
        routes.ConnectionTestIn(scope=scope), user=user, workspace_id=WS)


def _age_last_check(store, slot, *, seconds):
    meta = store.rows[(conn.PROVIDER, slot, "default")].metadata
    meta["verify_checked_at"] = (
        datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


# ─── PUT /api/claude/connection ──────────────────────────────────────


def test_saving_a_token_anthropic_refuses_is_a_400_in_anthropics_words(store):
    """Not a generic "could not save". The operator needs to know whether to
    re-mint the token or to go and pay a bill, and only the provider knows."""
    patcher, _ = _transport(_refused())
    with patcher, pytest.raises(HTTPException) as exc:
        _save()

    assert exc.value.status_code == 400
    assert "revoked" in exc.value.detail
    assert store.rows == {}


def test_saving_a_token_anthropic_accepts_returns_it_verified(store):
    patcher, _ = _transport(_accepted())
    with patcher:
        out = _save()

    assert out.personal is True
    assert out.personal_state == conn.STATE_VERIFIED
    assert out.personal_verified_at
    assert out.personal_reason is None


def test_a_save_that_could_not_be_checked_comes_back_labelled_unverified(store):
    """The response is not an error — the token is saved — but the page must
    not draw it the way it draws a checked one."""
    patcher, _ = _transport(httpx.ReadTimeout("timed out"))
    with patcher:
        out = _save()

    assert out.personal is True
    assert out.personal_state == conn.STATE_UNREACHABLE
    assert "could not reach Anthropic" in out.personal_reason


def test_a_bad_paste_is_still_refused_without_a_round_trip(store):
    patcher, client = _transport(_accepted())
    with patcher, pytest.raises(HTTPException) as exc:
        routes.save_connection(
            routes.ConnectionIn(token="not-a-claude-token-at-all"),
            user=_MEMBER, workspace_id=WS)

    assert exc.value.status_code == 400
    assert "setup-token" in exc.value.detail
    assert client.calls == []


# ─── POST /api/claude/test-connection ────────────────────────────────


def test_the_test_endpoint_rechecks_and_updates_the_stored_record(store):
    """The whole point of the button: the token was fine on Tuesday and the
    subscription was cancelled on Wednesday. Pressing it must move the row,
    not just report."""
    patcher, _ = _transport(_accepted())
    with patcher:
        _save()
    _age_last_check(store, _MEMBER.id, seconds=conn.VERIFY_CACHE_SECONDS + 60)

    patcher, _ = _transport(_refused())
    with patcher:
        out = _test_connection()

    assert out.ok is False
    assert "revoked" in out.detail
    assert out.cached is False
    after = routes.connection_status(user=_MEMBER, workspace_id=WS)
    assert after.personal_state == conn.STATE_FAILED
    assert "revoked" in after.personal_reason


def test_the_test_endpoint_is_rate_limited_by_the_stored_answer(store):
    """A page that polls it, or a person who clicks it six times, must not be
    six requests against somebody's subscription."""
    patcher, client = _transport(_accepted())
    with patcher:
        _save()
        outs = [_test_connection() for _ in range(4)]

    assert len(client.calls) == 1
    assert all(o.ok and o.cached for o in outs)


def test_testing_an_empty_slot_answers_instead_of_erroring(store):
    patcher, client = _transport(_accepted())
    with patcher:
        out = _test_connection()

    assert out.ok is False
    assert "No token saved" in out.detail
    assert client.calls == []


def test_a_member_may_not_test_the_shared_workspace_token(store):
    """Same gate as saving and disconnecting it: the shared slot is the
    admin's, and a probe spends the admin's quota."""
    gate = patch.object(routes, "_require_workspace_admin",
                        side_effect=HTTPException(status_code=403, detail="nope"))
    with gate, pytest.raises(HTTPException) as exc:
        _test_connection(user=_MEMBER, scope="workspace")
    assert exc.value.status_code == 403


def test_an_admin_tests_the_shared_slot_and_gets_its_own_answer(store):
    patcher, _ = _transport(_accepted())
    with patcher:
        _save(user=_ADMIN, scope="workspace")
    _age_last_check(store, conn._ws_slot(WS), seconds=conn.VERIFY_CACHE_SECONDS + 60)

    patcher, _ = _transport(_refused())
    with patcher:
        out = _test_connection(user=_ADMIN, scope="workspace")

    assert (out.ok, out.scope) == (False, "workspace")
    after = routes.connection_status(user=_ADMIN, workspace_id=WS)
    assert after.workspace_state == conn.STATE_FAILED
    assert after.personal_state == conn.STATE_ABSENT


# ─── GET /api/claude/connection ──────────────────────────────────────


def test_the_status_route_never_carries_the_token(store):
    patcher, _ = _transport(_accepted())
    with patcher:
        _save()
    assert TOKEN not in routes.connection_status(
        user=_MEMBER, workspace_id=WS).model_dump_json()
