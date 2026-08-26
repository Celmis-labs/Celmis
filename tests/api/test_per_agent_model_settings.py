"""Per-agent model parameters, and telling the truth about what a model takes.

The architect agent failed in 43% of runs against a 4096-token output ceiling:
Gemini 3.x counts reasoning tokens against the same budget, so thinking ate it
and the findings array came back truncated mid-JSON. Raising the ceiling fixed
that run. These tests pin the surface that keeps the NEXT number from being a
guess as well:

    GET /api/llm/model-capabilities   what the INSTALLED litellm knows
    GET /api/llm/config               the per-agent overrides, and what is in force
    PUT /api/llm/config               saving them, admin-only, validated

Two properties are worth more than the rest.

**"Unknown" is an answer.** A self-hosted server is addressed as
``openai/<whatever the operator called it>`` and litellm has no entry for it.
That has to come back as ``known: false`` with a 200 — not a 400, not an
invented ceiling — because a settings page still has to render for the
installation that runs its own models.

**A control that silently does nothing is the bug.** ``gemini_thinking_budget``
sat in the UI for months wired only into the native client, reaching no LiteLLM
call at all. So a reasoning value the chosen model cannot be sent is refused at
save time, naming what the model does take, rather than stored and dropped.

The model strings below are asserted against the litellm that is installed, on
purpose — a fixture of hand-written model facts would be the stale vendor table
this whole feature exists to avoid. If a litellm upgrade changes them, these
tests are the thing that says so.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.deps import current_workspace_id, get_current_user, require_workspace_admin
from src.api.routers import llm as llm_router

# Known to the installed litellm, reasons, and takes an effort WORD.
KNOWN_PROVIDER = "google"
KNOWN_MODEL = "gemini-3-flash-preview"
KNOWN_LITELLM = "gemini/gemini-3-flash-preview"
# The shape of a self-hosted selection: provider openai_compatible, model
# string openai/<free text>. litellm has never heard of it and never will.
SELF_HOSTED_LITELLM = "openai/celmis-vllm-not-in-any-catalogue"

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


@pytest.fixture(autouse=True)
def _fresh_capability_cache():
    """Capability answers are memoised for the life of the process.

    That is the shipped behaviour (see the note on GET /model-capabilities),
    which means one test's lookup would otherwise satisfy the next test's
    counter. Cleared on both sides so neither direction can borrow.
    """
    from src.llm.capabilities import reset_capability_caches
    reset_capability_caches()
    yield
    reset_capability_caches()


def _client(*, admin: bool = True) -> TestClient:
    """The real router, with the auth gates stood in for.

    `require_workspace_admin` is overridden rather than exercised: whether a
    given person administers a given workspace is decided against the database
    and is tested where that logic lives. What is tested HERE is that the
    write route goes through that gate at all and the read routes do not.
    """
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


def _use_known_model(client: TestClient) -> None:
    """Point the workspace's review surface at a model litellm knows.

    Every per-agent override inherits its model from here, so without this the
    agents would resolve to whatever REVIEW_* the developer's .env happens to
    carry and the assertions would be about their machine.
    """
    resp = client.put("/api/llm/config", json={
        "profiles": {"review": {"provider": KNOWN_PROVIDER, "model": KNOWN_MODEL}},
    })
    assert resp.status_code == 200, resp.text


# ══════════════════════════════════════════════════════════════════════
#  What a model supports
# ══════════════════════════════════════════════════════════════════════


def test_capabilities_report_what_a_known_model_actually_takes(store):
    """The facts come from litellm, and they are the facts that were missing.

    The output ceiling is the number the 43% failure rate was about: it has to
    come back as the model's real one, which is many times the 4096 that
    truncated the findings array.
    """
    body = _client().get(
        "/api/llm/model-capabilities", params={"model": KNOWN_LITELLM},
    ).json()

    assert body["known"] is True
    assert body["source"] == "litellm"
    assert body["model"] == KNOWN_LITELLM
    assert isinstance(body["max_output_tokens"], int)
    assert body["max_output_tokens"] > 4096
    assert body["supports_reasoning"] is True
    assert body["reasoning_kind"] == "effort"
    assert "high" in body["reasoning_values"]
    assert body["supports_function_calling"] is True


def test_a_model_litellm_never_heard_of_says_unknown_instead_of_guessing(store):
    """A self-hosted model is the ordinary case here, not an edge one.

    200 with `known: false` and nulls — a 400 would leave the settings page
    with nothing to render for an installation running its own server, and a
    guessed ceiling would truncate a call that would have worked.
    """
    resp = _client().get(
        "/api/llm/model-capabilities", params={"model": SELF_HOSTED_LITELLM},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["known"] is False
    assert body["source"] == "unknown"
    assert body["max_output_tokens"] is None
    assert body["supports_reasoning"] is None
    assert body["reasoning_kind"] is None
    assert body["reasoning_values"] is None
    assert body["supports_function_calling"] is None


def test_the_second_render_of_the_settings_page_does_not_re_enter_litellm(store):
    """Cached on the model string. A settings page asks about every agent's
    model on every render, and get_model_info walks a 3000-entry map."""
    import litellm
    real = litellm.get_model_info
    calls: list[str] = []

    def counted(model, *args, **kwargs):
        calls.append(model)
        return real(model, *args, **kwargs)

    client = _client()
    with patch.object(litellm, "get_model_info", counted):
        first = client.get("/api/llm/model-capabilities", params={"model": KNOWN_LITELLM})
        second = client.get("/api/llm/model-capabilities", params={"model": KNOWN_LITELLM})

    assert first.json() == second.json()
    assert calls == [KNOWN_LITELLM], f"litellm was re-entered: {calls}"


# ══════════════════════════════════════════════════════════════════════
#  Per-agent overrides
# ══════════════════════════════════════════════════════════════════════


def test_per_agent_overrides_survive_a_round_trip(store):
    """What was saved comes back, and what was not saved reports what it
    inherits — the question a settings page exists to answer."""
    client = _client()
    _use_known_model(client)

    saved = client.put("/api/llm/config", json={
        "agents": {"contract": {"max_output_tokens": 32768, "reasoning": "high"}},
    })
    assert saved.status_code == 200, saved.text

    agents = client.get("/api/llm/config").json()["agents"]

    architect = agents["contract"]
    assert architect["max_output_tokens"] == 32768
    assert architect["reasoning"] == "high"
    assert architect["effective_max_output_tokens"] == 32768
    assert architect["effective_model"] == KNOWN_LITELLM

    # An agent nobody touched still answers: null overrides, and the model it
    # inherits, as a LiteLLM string the UI can ask about capabilities with.
    verifier = agents["verifier"]
    assert verifier["max_output_tokens"] is None
    assert verifier["reasoning"] is None
    assert verifier["effective_model"] == KNOWN_LITELLM
    assert isinstance(verifier["effective_max_output_tokens"], int)

    # Every configurable agent is present, so the UI never has to guess
    # whether an absent key means "inherit" or "no such agent".
    from src.review.settings import REVIEW_AGENTS
    assert set(agents) == set(REVIEW_AGENTS)


def test_saving_agents_does_not_reset_the_rest_of_the_config(store):
    """PATCH semantics, the rule this handler already lives by: a partial save
    used to reset the provider, the model and the temperature to Pydantic
    defaults, which is how the docs-language selector wiped the LLM setup."""
    client = _client()
    _use_known_model(client)
    client.put("/api/llm/config", json={"temperature": 0.7})

    client.put("/api/llm/config", json={"agents": {"security": {"max_output_tokens": 8192}}})

    after = client.get("/api/llm/config").json()
    assert after["temperature"] == 0.7
    assert after["profiles"]["review"]["model"] == KNOWN_MODEL


def test_an_override_is_cleared_by_leaving_it_out_of_the_next_save(store):
    """The `agents` block is sent whole and replaces the stored map.

    That is the only shape that can express a removal: absent already means
    "inherit" at every layer of this chain, so the form clears an override by
    omitting it. A per-key merge would read the same request as "leave it
    alone" and keep a value the operator watched disappear from the form.
    """
    client = _client()
    _use_known_model(client)
    client.put("/api/llm/config", json={
        "agents": {
            "defect": {"max_output_tokens": 9000, "reasoning": "low"},
            "security": {"max_output_tokens": 4096},
        },
    })

    # The form is re-saved with quality's reasoning box emptied and the tests
    # row untouched: both facts travel in the same whole-map payload.
    client.put("/api/llm/config", json={
        "agents": {
            "defect": {"max_output_tokens": 9000},
            "security": {"max_output_tokens": 4096},
        },
    })
    agents = client.get("/api/llm/config").json()["agents"]
    assert agents["defect"]["reasoning"] is None
    assert agents["defect"]["max_output_tokens"] == 9000
    assert agents["security"]["max_output_tokens"] == 4096

    # An agent left out entirely has no overrides left, and says what it
    # inherits instead of going silent.
    client.put("/api/llm/config", json={"agents": {"security": {"max_output_tokens": 4096}}})
    agents = client.get("/api/llm/config").json()["agents"]
    assert agents["defect"]["max_output_tokens"] is None
    assert agents["defect"]["reasoning"] is None
    assert agents["defect"]["effective_max_output_tokens"] > 0
    assert agents["security"]["max_output_tokens"] == 4096


def test_a_save_that_does_not_mention_agents_leaves_them_alone(store):
    """Whole-map replacement applies to the `agents` key, not to the request:
    the settings page has several cards, and the one that saves a language
    must not wipe what the agents card saved."""
    client = _client()
    _use_known_model(client)
    client.put("/api/llm/config", json={
        "agents": {"contract": {"max_output_tokens": 32768}},
    })

    client.put("/api/llm/config", json={"docs_language": "de"})

    after = client.get("/api/llm/config").json()
    assert after["docs_language"] == "de"
    assert after["agents"]["contract"]["max_output_tokens"] == 32768


# ══════════════════════════════════════════════════════════════════════
#  Refusals — every one of them in front of the person who typed it
# ══════════════════════════════════════════════════════════════════════


def test_an_agent_nobody_ships_is_refused_and_named(store):
    client = _client()
    resp = client.put("/api/llm/config", json={
        "agents": {"archtiect": {"max_output_tokens": 8192}},
    })

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "archtiect" in detail
    assert "contract" in detail, "the refusal must name the agents that do exist"


def test_a_misspelled_field_is_refused_rather_than_stored_silently(store):
    """`max_tokens` for `max_output_tokens` saved quietly is the whole failure
    mode: a blob that looks configured and changes nothing."""
    client = _client()
    resp = client.put("/api/llm/config", json={
        "agents": {"contract": {"max_tokens": 8192}},
    })

    assert resp.status_code == 422
    assert "max_output_tokens" in resp.json()["detail"]


def test_an_effort_the_model_cannot_do_is_refused_by_name(store):
    """litellm does not error on an effort a vendor lacks — it DOWNGRADES it
    silently (max→xhigh→high) or drops it. The operator picks "xhigh", is
    billed for "high", and nothing anywhere says so. So it is refused here,
    listing what this model does take."""
    client = _client()
    _use_known_model(client)

    resp = client.put("/api/llm/config", json={
        "agents": {"contract": {"reasoning": "xhigh"}},
    })

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "xhigh" in detail
    assert "high" in detail and "low" in detail, f"did not name the alternatives: {detail}"
    assert KNOWN_LITELLM in detail

    from src.llm.capabilities import model_capabilities
    supported = model_capabilities(KNOWN_LITELLM).reasoning_values or ()
    assert "xhigh" not in supported, "the model gained xhigh — this test is now vacuous"
    assert all(word in detail for word in supported)


def test_a_ceiling_above_the_models_own_is_refused_with_both_numbers(store):
    """A request over the model's ceiling is a provider 400 hours later, with
    the number that caused it nowhere in the message."""
    client = _client()
    _use_known_model(client)
    ceiling = _client().get(
        "/api/llm/model-capabilities", params={"model": KNOWN_LITELLM},
    ).json()["max_output_tokens"]

    resp = client.put("/api/llm/config", json={
        "agents": {"contract": {"max_output_tokens": ceiling + 1}},
    })

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert str(ceiling) in detail
    assert str(ceiling + 1) in detail


def test_reasoning_is_refused_for_a_model_litellm_cannot_send_it_to(store):
    """Fail closed. For a self-hosted model litellm has no entry for, the
    reasoning parameter is dropped before the request goes out — storing one
    would be another setting that reaches nothing."""
    client = _client()
    client.put("/api/llm/config", json={
        "profiles": {"review": {
            "provider": "openai_compatible",
            "model": SELF_HOSTED_LITELLM,
            "base_url": "http://vllm.internal:8000/v1",
        }},
    })

    resp = client.put("/api/llm/config", json={
        "agents": {"security": {"reasoning": "high"}},
    })

    assert resp.status_code == 422
    assert "no entry" in resp.json()["detail"]

    # …while the output ceiling, which every model takes, still saves.
    ok = client.put("/api/llm/config", json={
        "agents": {"security": {"max_output_tokens": 12000}},
    })
    assert ok.status_code == 200, ok.text
    assert ok.json()["agents"]["security"]["max_output_tokens"] == 12000


def test_only_a_workspace_admin_may_change_agent_parameters(store):
    """Model and parameter changes stay admin-only — the policy already in
    force for keys and profiles. Reading is not gated: a developer has to be
    able to see what the reviewer will run with."""
    admin = _client()
    _use_known_model(admin)
    admin.put("/api/llm/config", json={
        "agents": {"contract": {"max_output_tokens": 32768}},
    })

    developer = _client(admin=False)
    refused = developer.put("/api/llm/config", json={
        "agents": {"contract": {"max_output_tokens": 64}},
    })

    assert refused.status_code == 403
    assert developer.get("/api/llm/config").status_code == 200
    assert developer.get(
        "/api/llm/model-capabilities", params={"model": KNOWN_LITELLM},
    ).status_code == 200
    # The refusal has to have refused, not just answered 403 on the way out.
    assert admin.get("/api/llm/config").json()["agents"]["contract"][
        "max_output_tokens"] == 32768


# ══════════════════════════════════════════════════════════════════════
#  The model in play, and no other
# ══════════════════════════════════════════════════════════════════════
#
# Three refusals had one root: something asked about a model that was not the
# model in play. The screen took the vendor from the review PROFILE, so a bare
# id from another vendor got the wrong prefix welded on; and the validator
# resolved an agent's model out of the config as it stood BEFORE the save, so a
# request that changed the model was judged against the model it replaced. The
# runtime was right the whole time — which is what made both refusals so hard
# to believe from the outside.

# Known to the installed litellm, from a DIFFERENT vendor than KNOWN_PROVIDER,
# and reasons with a WIDER effort vocabulary than KNOWN_MODEL — the width is
# what makes the acceptance below mean something.
CROSS_VENDOR_MODEL = "claude-sonnet-4-5"
# Known to litellm, another vendor again, and takes no reasoning parameter at
# all — the model whose name the "reasoning" refusal used to be about.
NO_REASONING_MODEL = "gpt-4o"


def test_an_agent_can_be_moved_to_a_model_from_another_vendor(store):
    """The dropdown offers bare ids and the policy columns have always stored
    them, so a cross-vendor pick is point-and-click reachable. Taking the vendor
    from the review profile turned "claude-sonnet-4-5" into "gemini/claude-…",
    which litellm has never heard of — so the save was refused, naming a model
    that does not exist, for a choice the review path handles correctly.
    """
    client = _client()
    _use_known_model(client)

    saved = client.put("/api/llm/config", json={
        "agents": {"contract": {"model": CROSS_VENDOR_MODEL, "reasoning": "xhigh"}},
    })

    assert saved.status_code == 200, saved.text
    architect = client.get("/api/llm/config").json()["agents"]["contract"]
    assert architect["model"] == CROSS_VENDOR_MODEL
    assert architect["reasoning"] == "xhigh"

    # And the effort was accepted on the strength of THAT model's vocabulary:
    # the review profile's own model refuses the very same word.
    from src.llm.capabilities import model_capabilities
    assert "xhigh" in (
        model_capabilities(architect["effective_model"]).reasoning_values or ())
    assert "xhigh" not in (
        model_capabilities(KNOWN_LITELLM).reasoning_values or ()), \
        "the profile's model gained xhigh — this test no longer proves anything"


def test_a_ceiling_is_checked_against_the_model_the_agent_is_moving_to(store):
    """The same root, seen from the other side: the wrong prefix made the model
    unknown, and an unknown model has no ceiling to refuse against — so a
    request four times over gpt-4o's limit saved cleanly and became a provider
    400 in a queued review, with the number that caused it nowhere in it."""
    from src.llm.capabilities import model_capabilities
    ceiling = model_capabilities(NO_REASONING_MODEL).max_output_tokens
    assert ceiling, f"{NO_REASONING_MODEL} left the installed litellm's map"

    client = _client()
    _use_known_model(client)
    resp = client.put("/api/llm/config", json={
        "agents": {"contract": {
            "model": NO_REASONING_MODEL, "max_output_tokens": ceiling + 1,
        }},
    })

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert NO_REASONING_MODEL in detail
    assert str(ceiling) in detail
    # The number the profile's model would have allowed, had the wrong model
    # been consulted. Naming it would mean the refusal is about the wrong one.
    assert str(model_capabilities(KNOWN_LITELLM).max_output_tokens) not in detail


def test_clearing_a_model_override_is_judged_by_what_the_agent_will_inherit(store):
    """The `agents` block replaces the stored map, so an omitted `model` is the
    form saying "go back to inheriting". The validator was resolving that agent
    out of the config as it stood BEFORE the merge, where the old model was
    still sitting — so emptying the model box and choosing an effort came back
    "a reasoning level cannot be sent to gpt-4o", about a model the workspace
    was in the act of getting rid of.
    """
    client = _client()
    _use_known_model(client)
    client.put("/api/llm/config", json={
        "agents": {"contract": {"model": NO_REASONING_MODEL}},
    })

    resp = client.put("/api/llm/config", json={
        "agents": {"contract": {"reasoning": "high"}},
    })

    assert resp.status_code == 200, resp.text
    architect = client.get("/api/llm/config").json()["agents"]["contract"]
    assert architect["model"] is None, "the override was meant to be cleared"
    assert architect["reasoning"] == "high"
    assert architect["effective_model"] == KNOWN_LITELLM


def test_the_refusal_names_the_model_that_will_actually_run(store):
    """The other direction of the same clearing, and the one that must still
    fail: the inherited model does NOT take the effort that the model being
    dropped did. Fail-closed is right here — but the message has to be about
    the model the workspace is about to use, or the operator goes looking for a
    setting that is no longer anywhere on the page.
    """
    client = _client()
    _use_known_model(client)
    client.put("/api/llm/config", json={
        "agents": {"contract": {"model": CROSS_VENDOR_MODEL, "reasoning": "xhigh"}},
    })

    resp = client.put("/api/llm/config", json={
        "agents": {"contract": {"reasoning": "xhigh"}},
    })

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert KNOWN_LITELLM in detail, f"did not name the inherited model: {detail}"
    assert "claude" not in detail, \
        f"named the model being replaced rather than the one taking over: {detail}"

    # And nothing was written: the stored map still describes the last save.
    architect = client.get("/api/llm/config").json()["agents"]["contract"]
    assert architect["model"] == CROSS_VENDOR_MODEL
