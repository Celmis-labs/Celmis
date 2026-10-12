"""The Test button: one tiny real call, and an honest account of what happened.

Covers POST /api/llm/test-agent and the base reasoning level it sits next to
(`review_reasoning`): whether the value was honoured, withheld by our own
rules, or refused by the provider (recorded like a real run records it), that
an agent left on "inherit" really sends the base level, and the resolution
order repo -> agent -> base -> none. No network: litellm.completion is a stub.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import current_workspace_id, get_current_user, require_prompt_editor
from src.api.routers import llm as llm_router

KNOWN_MODEL = "gemini/gemini-3-flash-preview"   # takes an effort word
NO_REASONING = "openai/gpt-4o"                 # takes no reasoning parameter
_USER = SimpleNamespace(id="u-lead", email="lead@test", is_admin=True)

REFUSAL_BODY = (
    'litellm.BadRequestError: VertexAIException BadRequestError - {"error": '
    '{"code": 400, "message": "Thinking level MINIMAL is not supported for '
    'this model. Please retry with other thinking level.", "status": '
    '"INVALID_ARGUMENT"}}'
)


class _Completion:
    def __init__(self, reasoning_tokens: int | None) -> None:
        details = (SimpleNamespace(reasoning_tokens=reasoning_tokens)
                   if reasoning_tokens is not None else None)
        self.choices = [SimpleNamespace(
            message=SimpleNamespace(content="ok"), finish_reason="stop")]
        self.usage = SimpleNamespace(
            prompt_tokens=8, completion_tokens=40, prompt_tokens_details=None,
            completion_tokens_details=details, total_cost=0.0001)


class _Provider:
    """Records every call; optionally refuses the effort or times out."""

    def __init__(self, *, reasoning_tokens=None, refuse_effort=False, timeout=False):
        self.reasoning_tokens = reasoning_tokens
        self.refuse_effort = refuse_effort
        self.timeout = timeout
        self.kwargs_seen: list[dict] = []

    def __call__(self, **kwargs):
        import litellm
        self.kwargs_seen.append(kwargs)
        if self.timeout:
            raise litellm.exceptions.Timeout(
                message="timed out", model=kwargs.get("model", ""), llm_provider="gemini")
        if self.refuse_effort and "reasoning_effort" in kwargs:
            raise litellm.exceptions.BadRequestError(
                message=REFUSAL_BODY, model=kwargs.get("model", ""), llm_provider="gemini")
        return _Completion(self.reasoning_tokens)


class _FakeStore:
    def __init__(self) -> None:
        self.rows: dict = {}

    def save(self, *, provider, secret, metadata=None, user_id="", account_label="default"):
        self.rows[(provider, user_id, account_label)] = SimpleNamespace(
            secret=secret, metadata=metadata or {})

    def load(self, *, provider, user_id="", account_label="default"):
        return self.rows.get((provider, user_id, account_label))


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for var in ("LITELLM_PROXY_URL", "LITELLM_MASTER_KEY", "LITELLM_PROXY_API_BASE"):
        monkeypatch.delenv(var, raising=False)
    from src.llm.capabilities import reset_capability_caches
    reset_capability_caches()
    monkeypatch.setattr("src.llm.keys.resolve_api_key", lambda *a, **k: "sk-test")
    with patch("src.credentials.get_credential_store", return_value=_FakeStore()):
        yield
    reset_capability_caches()


@pytest.fixture
def provider(monkeypatch):
    import litellm

    def _install(**kw) -> _Provider:
        p = _Provider(**kw)
        monkeypatch.setattr(litellm, "completion", p)
        return p
    return _install


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(llm_router.router)
    app.dependency_overrides[get_current_user] = lambda: _USER
    app.dependency_overrides[current_workspace_id] = lambda: "default"
    app.dependency_overrides[require_prompt_editor] = lambda: _USER
    app.dependency_overrides[llm_router.require_workspace_admin] = lambda: _USER
    return TestClient(app)


def _test(client, **body):
    body.setdefault("model", KNOWN_MODEL)
    resp = client.post("/api/llm/test-agent", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ─── the Test call ───────────────────────────────────────────────────


def test_an_accepted_level_is_honoured_and_the_thinking_tokens_are_shown(provider):
    p = provider(reasoning_tokens=37)
    out = _test(_client(), agent="defect", reasoning="high", temperature=0.2)

    assert out["status"] == "honoured"
    assert out["reasoning_sent"] == "high"
    assert out["reasoning_tokens"] == 37
    assert out["temperature_status"] == "honoured"
    assert p.kwargs_seen[0]["reasoning_effort"] == "high"
    assert p.kwargs_seen[0]["max_tokens"] <= 1024      # tiny on purpose


def test_a_level_the_model_cannot_take_is_withheld_by_us_and_says_so(provider):
    p = provider()
    out = _test(_client(), model=NO_REASONING, reasoning="high")

    assert out["status"] == "not_sent"
    assert out["reasoning_sent"] is None
    assert "reasoning_effort" not in p.kwargs_seen[0]


def test_a_level_the_provider_refuses_is_reported_in_its_words_and_recorded(provider):
    from src.llm.capabilities import provider_refusals

    p = provider(refuse_effort=True)
    out = _test(_client(), reasoning="minimal")

    assert out["status"] == "refused"
    assert "not supported" in out["message"]
    assert len(p.kwargs_seen) == 2                      # refused, then retried without
    assert "reasoning_effort" not in p.kwargs_seen[1]
    # The same record a real run leaves: the settings page now strikes the word.
    assert [r.value for r in provider_refusals(KNOWN_MODEL)] == ["minimal"]


def test_no_reasoning_asked_is_a_plain_connectivity_check(provider):
    provider()
    assert _test(_client())["status"] == "ok"


def test_a_timeout_is_reported_as_one(provider):
    provider(timeout=True)
    assert _test(_client(), reasoning="high")["status"] == "timeout"


def test_an_unknown_agent_is_refused(provider):
    provider()
    resp = _client().post("/api/llm/test-agent", json={"agent": "nope", "model": KNOWN_MODEL})
    assert resp.status_code == 422


def test_the_call_is_its_own_rate_limit_class():
    from src.api.middleware import _body_caps, _classify, _limits

    assert _classify("/api/llm/test-agent") == "llm_test"
    assert _limits()["llm_test"] < _limits()["default"]
    assert "llm_test" in _body_caps()


# ─── the base level ──────────────────────────────────────────────────


def _use_known_model(client):
    r = client.put("/api/llm/config", json={
        "profiles": {"review": {"provider": "google", "model": "gemini-3-flash-preview"}}})
    assert r.status_code == 200, r.text


def test_the_base_reasoning_round_trips_and_agents_on_inherit_report_it():
    client = _client()
    _use_known_model(client)
    assert client.put("/api/llm/config", json={"review_reasoning": "medium"}).status_code == 200

    cfg = client.get("/api/llm/config").json()
    assert cfg["review_reasoning"] == "medium"
    assert cfg["agents"]["defect"]["reasoning"] is None          # no override...
    assert cfg["agents"]["defect"]["effective_reasoning"] == "medium"   # ...inherits base

    # An unrelated save leaves it alone; "" clears it.
    client.put("/api/llm/config", json={"review_language": "en"})
    assert client.get("/api/llm/config").json()["review_reasoning"] == "medium"
    client.put("/api/llm/config", json={"review_reasoning": ""})
    assert client.get("/api/llm/config").json()["review_reasoning"] is None


def test_a_base_level_the_review_model_cannot_take_is_refused():
    client = _client()
    r = client.put("/api/llm/config", json={
        "profiles": {"review": {"provider": "openai", "model": "gpt-4o"}},
        "review_reasoning": "high"})
    assert r.status_code == 422


def test_resolution_order_is_repo_then_agent_then_base_then_none():
    from src.review.settings import resolve_agent_llm

    ws = {"review_reasoning": "low", "agents": {"defect": {"reasoning": "medium"}}}
    repo = {"agents": {"defect": {"reasoning": "high"}}}

    assert resolve_agent_llm("defect", policy=repo, workspace_cfg=ws).reasoning == "high"
    assert resolve_agent_llm("defect", workspace_cfg=ws).reasoning == "medium"
    assert resolve_agent_llm("security", workspace_cfg=ws).reasoning == "low"
    assert resolve_agent_llm("security", workspace_cfg={}).reasoning is None


def test_an_agent_on_inherit_sends_the_base_level_on_the_wire(provider, tmp_path):
    """The outgoing litellm call, not just the resolver's answer."""
    from src.llm.client import LLMClient
    from src.review.settings import resolve_agent_llm
    from src.security.audit import AuditLogger

    p = provider()
    resolved = resolve_agent_llm(
        "security", workspace_cfg={"review_reasoning": "medium", "model": KNOWN_MODEL})
    client = LLMClient(
        resolve_key=lambda _p: "sk-test", resolve_model=lambda _a: KNOWN_MODEL,
        surface="review", audit=AuditLogger(tmp_path / "audit.jsonl"), workspace_id="ws")
    client.generate(prompt="p", agent="security", operation="review_security",
                    max_output_tokens=256, reasoning=resolved.reasoning)

    assert p.kwargs_seen[0]["reasoning_effort"] == "medium"


def test_the_orchestrator_builds_every_inheriting_agent_with_the_base_level(monkeypatch):
    from src.review.orchestrator import ReviewOrchestrator

    monkeypatch.setattr(
        "src.api.routers.llm._load_workspace_config",
        lambda ws="default": {"review_reasoning": "medium",
                              "agents": {"verifier": {"reasoning": "low"}}})
    orch = ReviewOrchestrator.__new__(ReviewOrchestrator)
    _client_obj, by_agent = orch._build_llm_client("u", "default", None)

    assert by_agent["defect"].reasoning == "medium"
    assert by_agent["security"].reasoning == "medium"
    assert by_agent["verifier"].reasoning == "low"      # its own override wins
