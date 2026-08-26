"""The review fallback model: stored, reported, and refused when pointless.

Why the field exists at all is a measured afternoon, not a theory: in one
benchmark run gemini-3.7-flash refused 40% of agent calls while
gemini-3.6-flash, same vendor, refused none. A workspace that can name a
second model keeps its reviews flowing through exactly that kind of outage —
at the cost of comparability between runs, which is why empty (no fallback)
is the default and the operator has to opt in.

Three properties are pinned here:

**Round-trip with patch semantics.** PUT carries it, GET reports it, "" clears
it — and a PUT that does not mention the field leaves it alone, because the
settings page saves one card at a time and a language save must not wipe the
fallback (the same partial-PUT bug `model_fields_set` exists to prevent).

**Identical to the primary is refused.** A fallback exists to answer when that
exact model cannot; retrying it buys a second failure at full price. Refused
in both spellings ("gemini/x" resolves to the same wire string as "x"), and
from both directions — saving a primary equal to the stored fallback is the
same pointless pair.

**Unknown is handled the way the primary handles it: accepted.** A self-hosted
model is addressed by a name litellm has never heard of, and the PRIMARY
saves fine that way (`known: false` is an answer — see
test_per_agent_model_settings). A stricter gate on the fallback would refuse
installations the primary path serves.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.deps import current_workspace_id, get_current_user, require_workspace_admin
from src.api.routers import llm as llm_router

KNOWN_PROVIDER = "google"
PRIMARY_MODEL = "gemini-3-flash-preview"
# A different model under the same vendor — the shape the measured outage
# made a case for.
FALLBACK_MODEL = "gemini-3-pro-preview"
# The shape of a self-hosted name: litellm has never heard of it and never will.
UNKNOWN_MODEL = "celmis-vllm-not-in-any-catalogue"

# `id` as well as `email`: the config handler audits who changed the LLM
# routing, and an actor with no id is not an actor. A stub that omits a
# field the real `User` always has turns a correct call into a test
# failure — the stub was wrong, not the caller.
_USER = SimpleNamespace(id="u-lead", email="lead@test", is_admin=True)


class _FakeStore:
    """In-memory credentials store — the only persistence config touches."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], SimpleNamespace] = {}

    def save(self, *, provider, secret, metadata=None, user_id="", account_label="default"):
        self.rows[(provider, user_id, account_label)] = SimpleNamespace(
            secret=secret, metadata=metadata or {},
        )

    def load(self, *, provider, user_id="", account_label="default"):
        return self.rows.get((provider, user_id, account_label))


@pytest.fixture
def store(monkeypatch):
    """Hermetic config storage: no DB, no gateway, no leaked provider env."""
    for var in ("LITELLM_PROXY_URL", "LITELLM_MASTER_KEY", "LITELLM_PROXY_API_BASE",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_COMPATIBLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    fake = _FakeStore()
    with patch("src.credentials.get_credential_store", return_value=fake):
        yield fake


def _client(*, admin: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(llm_router.router)
    app.dependency_overrides[get_current_user] = lambda: _USER
    app.dependency_overrides[current_workspace_id] = lambda: "default"

    def _admin_gate():
        if not admin:
            raise HTTPException(status_code=403, detail="workspace admin required")
        return _USER

    app.dependency_overrides[require_workspace_admin] = _admin_gate
    return TestClient(app)


def _set_review_primary(client: TestClient, model: str = PRIMARY_MODEL) -> None:
    resp = client.put("/api/llm/config", json={
        "profiles": {"review": {"provider": KNOWN_PROVIDER, "model": model}},
    })
    assert resp.status_code == 200, resp.text


# ══════════════════════════════════════════════════════════════════════
#  Round-trip
# ══════════════════════════════════════════════════════════════════════


def test_fallback_round_trips_and_defaults_to_none(store):
    client = _client()
    _set_review_primary(client)

    # Nothing configured → no fallback: empty is the default.
    assert client.get("/api/llm/config").json()["review_fallback_model"] is None

    body = client.put("/api/llm/config", json={
        "review_fallback_model": FALLBACK_MODEL,
    })
    assert body.status_code == 200, body.text
    assert body.json()["review_fallback_model"] == FALLBACK_MODEL
    assert client.get("/api/llm/config").json()["review_fallback_model"] == FALLBACK_MODEL


def test_a_put_that_does_not_mention_the_fallback_keeps_it(store):
    """The settings page saves one card at a time; a language save must not
    wipe the fallback — the partial-PUT reset bug, one field later."""
    client = _client()
    _set_review_primary(client)
    client.put("/api/llm/config", json={"review_fallback_model": FALLBACK_MODEL})

    resp = client.put("/api/llm/config", json={"review_language": "de"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["review_fallback_model"] == FALLBACK_MODEL


def test_empty_string_clears_the_fallback(store):
    """"" is how the form says "no fallback any more" — it must not be read
    as falsy-therefore-keep-the-old-value."""
    client = _client()
    _set_review_primary(client)
    client.put("/api/llm/config", json={"review_fallback_model": FALLBACK_MODEL})

    resp = client.put("/api/llm/config", json={"review_fallback_model": ""})

    assert resp.status_code == 200, resp.text
    assert resp.json()["review_fallback_model"] is None
    assert client.get("/api/llm/config").json()["review_fallback_model"] is None


# ══════════════════════════════════════════════════════════════════════
#  Identical to the primary is refused
# ══════════════════════════════════════════════════════════════════════


def test_a_fallback_equal_to_the_primary_is_refused(store):
    client = _client()
    _set_review_primary(client)

    resp = client.put("/api/llm/config", json={
        "review_fallback_model": PRIMARY_MODEL,
    })

    assert resp.status_code == 422, resp.text
    assert PRIMARY_MODEL in resp.json()["detail"]
    # The refusal saved nothing.
    assert client.get("/api/llm/config").json()["review_fallback_model"] is None


def test_the_same_model_in_litellm_spelling_is_still_the_same_model(store):
    """"gemini/gemini-3-flash-preview" and "gemini-3-flash-preview" resolve
    to one wire string; a fallback that resolves to the primary retries the
    model that just failed."""
    client = _client()
    _set_review_primary(client)

    resp = client.put("/api/llm/config", json={
        "review_fallback_model": f"gemini/{PRIMARY_MODEL}",
    })

    assert resp.status_code == 422, resp.text


def test_moving_the_primary_onto_the_stored_fallback_is_refused_too(store):
    """The pair is judged as it will be AFTER the save, whichever half the
    request carried — otherwise a later primary change quietly recreates the
    pointless pair the direct save is refused for."""
    client = _client()
    _set_review_primary(client)
    ok = client.put("/api/llm/config", json={"review_fallback_model": FALLBACK_MODEL})
    assert ok.status_code == 200, ok.text

    resp = client.put("/api/llm/config", json={
        "profiles": {"review": {"provider": KNOWN_PROVIDER, "model": FALLBACK_MODEL}},
    })

    assert resp.status_code == 422, resp.text
    # And the workspace still runs on the config from before the refusal.
    cfg = client.get("/api/llm/config").json()
    assert cfg["profiles"]["review"]["model"] == PRIMARY_MODEL
    assert cfg["review_fallback_model"] == FALLBACK_MODEL


# ══════════════════════════════════════════════════════════════════════
#  Unknown is an answer, for the fallback exactly as for the primary
# ══════════════════════════════════════════════════════════════════════


def test_an_unknown_fallback_is_accepted_the_way_an_unknown_primary_is(store):
    """A self-hosted model string is the ordinary case on this surface. The
    primary saves while litellm reports `known: false`; the fallback must
    not be held to a stricter gate the primary never passes through."""
    client = _client()

    # The primary itself saves unknown — the behaviour the fallback mirrors.
    primary = client.put("/api/llm/config", json={
        "profiles": {"review": {"provider": KNOWN_PROVIDER, "model": UNKNOWN_MODEL}},
    })
    assert primary.status_code == 200, primary.text

    _set_review_primary(client)  # back to a real primary
    resp = client.put("/api/llm/config", json={
        "review_fallback_model": UNKNOWN_MODEL,
    })

    assert resp.status_code == 200, resp.text
    assert resp.json()["review_fallback_model"] == UNKNOWN_MODEL
