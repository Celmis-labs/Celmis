"""Two promises this package makes, each defeated one layer down.

Both defects have the same shape, which is why they are pinned together: a
guarantee is stated and tested at the layer that states it, and the layer
underneath quietly does the opposite. The test at the top layer passes, because
it is watching the top layer.

  1. `ProviderFailure.reason` — "a sentence safe to show an end user", and
     "both halves are already free of the provider's body". Neither half was
     free of it for `generation_failed`, the ONE code with no row in `_REASON`
     and therefore the only code that reached the `self.hint or self.code`
     fallback. Its hint is `f"{type(exc).__name__}: {message}"` — the
     provider's response body, clipped to MAX_HINT and nothing else. So the
     property whose docstring promised a safe sentence returned the payload the
     module exists to keep off a screen, for the failure most likely to carry
     one. `test_provider_errors.py` never caught it: it asserts on `hint`,
     which is allowed to be the clipped message, and never read `reason`.

  2. `LLMReviewAgent` — "a 429 resent inside its own window is worse than a
     failure", pinned by tests/review/test_a_hopeless_call_is_not_retried.py
     with `client.generate.call_count == 1`. True of `LLMClient.generate`, and
     false of the provider: neither `_gen` closure passed `num_retries`, so
     both inherited `LLMClient.generate`'s default of 3 and handed it to
     `litellm.completion`, which retries BELOW the classification that decides
     whether a retry is worth anything. The rate-limited call the agent
     refuses to resend had already been sent four times. The existing test
     counts calls to a mock of the layer above the retry, so nothing it could
     assert would have seen it.

What makes each of these testable is refusing to stop at the promising layer:
read `reason` rather than `hint`, and count what the PROVIDER saw rather than
what the client was asked for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.llm.errors import (
    MAX_HINT,
    UNRECOGNISED,
    ProviderFailure,
    classify,
    classify_vector_store,
)

# ─── 1. the reason a user is shown ──────────────────────────────────

#: A quota refusal as it actually arrived — nested JSON, quota metric names,
#: internal model ids. Long enough that `_trim` has to bite.
VERTEX_429 = (
    'litellm.RateLimitError: vertex_ai_betaException - {"error": {"code": 429, '
    '"message": "You exceeded your current quota, please check your plan and '
    "billing details. Quota exceeded for metric: "
    "generativelanguage.googleapis.com/generate_content_free_tier_input_token_count, "
    'limit: 0, model: gemini-3.1-pro"}, "status": "RESOURCE_EXHAUSTED"}'
) * 4

#: Fragments that only exist in a provider's body. If one of these turns up in
#: a user-facing string, the body got there.
BODY_MARKERS = (
    "RESOURCE_EXHAUSTED", "generativelanguage", "billing details",
    "QuotaFailure", "{", "}",
)


def err(message: str, **attrs: object) -> Exception:
    exc = Exception(message)
    for key, value in attrs.items():
        setattr(exc, key, value)
    return exc


def test_an_unclassified_failure_does_not_put_the_body_in_the_reason():
    """The defect, in one call.

    No status code and no recognised type, so `classify` falls all the way
    through to `generation_failed` — which is exactly the failure whose body
    nobody has read yet and which is therefore likeliest to be a wall of JSON.
    """
    failure = classify(err(VERTEX_429))

    assert failure.code == "generation_failed"
    assert failure.disposition == UNRECOGNISED
    for marker in BODY_MARKERS:
        assert marker not in failure.reason, (
            f"the provider's body reached `reason` via {marker!r}: "
            f"{failure.reason!r}"
        )


def test_the_reason_still_names_the_failure_it_could_not_classify():
    """Not fixed by returning "" — a user with no code to quote cannot report
    anything, and the slug is ours, stable, and what the UI translates."""
    reason = classify(err(VERTEX_429)).reason
    assert "generation_failed" in reason
    assert reason.strip() and reason != "generation_failed", (
        "a bare slug is a token, not the sentence this property promises"
    )


def test_the_clipped_message_is_still_available_on_the_hint():
    """The fix must not over-correct. `hint` is deliberately allowed to carry a
    clipped message — the Q&A error event sends it as `detail`, and
    test_provider_errors.py pins that it stays bounded. Only `reason` promised
    to be free of it, so only `reason` changed."""
    failure = classify(err(VERTEX_429))
    assert failure.hint, "the diagnostic detail was thrown away, not relocated"
    assert len(failure.hint) <= MAX_HINT
    assert "Exception" in failure.hint


@pytest.mark.parametrize(
    "exc",
    [
        err(VERTEX_429),                                   # unclassified
        err(VERTEX_429, status_code=429),                  # quota_exhausted
        err(VERTEX_429, status_code=401),                  # invalid_key
        err(VERTEX_429, status_code=503),                  # provider_unavailable
        err(VERTEX_429, code=404),                         # model_not_found
        err(VERTEX_429, llm_provider="vertex_ai", model="gemini-3.1-pro",
            status_code=429),
    ],
)
def test_no_classification_lets_the_body_through(exc):
    """Swept across the codes rather than asserted on one, because the hole was
    in the FALLBACK — the branch nobody writes a case for."""
    reason = classify(exc).reason
    for marker in BODY_MARKERS:
        assert marker not in reason, f"{marker!r} in {reason!r}"


def test_the_vault_classifier_answers_to_the_same_promise():
    """It lives in this module for the reason its docstring gives — "no
    provider body ever reaches a user" is ONE rule — so its codes are held to
    it too. They also have to read like the vault: a collection that was never
    created means nobody has generated documentation, not that a provider
    call failed.
    """
    raw = ('Unexpected Response: 404 (Not Found) Raw response content: '
           'b\'{"status":{"error":"Not found: Collection code_analysis_vault '
           'doesn\'t exist!"}}\'')
    missing = classify_vector_store(err(raw, status_code=404))
    assert missing.code == "vault_not_generated"
    assert "code_analysis_vault" not in missing.reason
    assert "Raw response" not in missing.reason
    assert "provider" not in missing.reason.lower(), (
        "the vault is an accelerator, not a provider — calling its absence a "
        "provider failure is advice to go fix the wrong thing"
    )

    down = classify_vector_store(ConnectionError("connection refused to qdrant:6333"))
    assert down.code == "vault_unavailable"
    assert "qdrant:6333" not in down.reason, "the reason leaks internal topology"


def test_a_code_nobody_has_written_a_sentence_for_still_says_nothing_unsafe():
    """Fail-closed, for the code that does not exist yet.

    A `ProviderFailure` assembled by hand, or by a branch added later, must not
    be able to reintroduce the hole by simply not appearing in `_REASON` — that
    is precisely how the hole existed in the first place.
    """
    invented = ProviderFailure("a_code_from_next_year", hint=VERTEX_429[:MAX_HINT])
    for marker in BODY_MARKERS:
        assert marker not in invented.reason, f"{marker!r} in {invented.reason!r}"
    assert "a_code_from_next_year" in invented.reason


# ─── 2. the retry the provider actually gets ────────────────────────


class _FakeLiteLLM:
    """A stand-in for `litellm.completion` that honours `num_retries`.

    Recording the kwarg would pin the value and prove nothing about what it
    does; the retry loop this defect is about lives INSIDE litellm, so the
    stub implements its contract — one call, then up to `num_retries` more
    after a failure — and the assertion becomes how many times the provider
    was asked. That is the number the promise is about.
    """

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.attempts = 0
        self.kwargs_seen: list[dict] = []

    def __call__(self, **kwargs):
        self.kwargs_seen.append(kwargs)
        budget = int(kwargs.get("num_retries", 0) or 0)
        last: BaseException | None = None
        for _ in range(budget + 1):
            self.attempts += 1
            outcome = (self.outcomes.pop(0) if self.outcomes
                       else _completion("[]"))
            if isinstance(outcome, BaseException):
                last = outcome
                continue
            return outcome
        assert last is not None
        raise last


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Message(content)
        self.finish_reason = "stop"


class _Usage:
    prompt_tokens = 100
    completion_tokens = 40
    prompt_tokens_details = None
    total_cost = 0.01


class _Completion:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]
        self.usage = _Usage()


def _completion(content: str) -> _Completion:
    return _Completion(content)


VALID = '[{"reasoning": "line 1 reads x before it is assigned", "file": "a.py", "line": 1, "severity": "critical", "title": "t", "body": "b"}]'


def _rate_limited() -> BaseException:
    import litellm.exceptions as le
    return le.RateLimitError(
        message="Too many requests", llm_provider="openai", model="gpt-4",
    )


def _client(tmp_path: Path):
    from src.llm.client import LLMClient
    from src.security.audit import AuditLogger

    return LLMClient(
        resolve_key=lambda provider: "sk-test",
        resolve_model=lambda agent: "openai/gpt-4",
        surface="review",
        audit=AuditLogger(tmp_path / "audit.jsonl"),
        workspace_id="ws-test",
    )


def _agent_and_context(llm_client):
    from src.review.agents.base import AgentContext, LLMReviewAgent
    from src.review.models import Hunk, PullRequest

    class _Agent(LLMReviewAgent):
        name = "security"
        system_prompt = "find problems"

        def _build_prompt(self, context):
            return "p"

    pr = PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="d",
        author="a", base_ref="main", base_sha="a", head_ref="f", head_sha="b",
        state="open",
        hunks=[Hunk(file_path="a.py", old_file_path="a.py", old_start=1,
                    old_count=1, new_start=1, new_count=1, content="@@")],
    )
    return _Agent(), AgentContext(pull_request=pr, llm_client=llm_client)


@pytest.fixture
def fake_litellm(monkeypatch):
    """Installed on the `litellm` module itself — `LLMClient.generate` imports
    it inside the call, so patching the attribute is what the real code reads.
    """
    import litellm

    def _install(outcomes: list) -> _FakeLiteLLM:
        fake = _FakeLiteLLM(outcomes)
        monkeypatch.setattr(litellm, "completion", fake)
        return fake

    return _install


def test_a_rate_limited_review_reaches_the_provider_exactly_once(
    tmp_path, fake_litellm,
):
    """The guarantee, measured where it is actually kept or broken.

    tests/review/test_a_hopeless_call_is_not_retried.py asserts
    `client.generate.call_count == 1` against a mocked client and passes either
    way. With the inherited `num_retries=3` the provider saw FOUR requests, all
    inside the window that had already refused one — which is the behaviour
    that file's docstring calls "worse than a failure".
    """
    fake = fake_litellm([_rate_limited()] * 8)
    agent, ctx = _agent_and_context(_client(tmp_path))

    result = agent.review(ctx)

    assert fake.attempts == 1, (
        f"the provider was asked {fake.attempts} times for a call the agent "
        "reports as not retried"
    )
    assert result.error is not None and "rate-limiting" in result.error


def test_the_review_path_states_its_retry_budget_instead_of_inheriting_one(
    tmp_path, fake_litellm,
):
    """The value litellm receives, pinned.

    `LLMClient.generate`'s default of 3 is right for callers with no retry
    policy of their own. This surface has one — two attempts total, decided by
    `classify` — so it has to say so, or it is silently running the default's
    policy multiplied by its own.
    """
    fake = fake_litellm([_completion(VALID)])
    agent, ctx = _agent_and_context(_client(tmp_path))

    agent.review(ctx)

    assert fake.kwargs_seen, "no call reached litellm at all"
    assert fake.kwargs_seen[0]["num_retries"] == 0, (
        "the review path inherited LLMClient.generate's default again"
    )


def test_the_agents_own_transport_retry_is_untouched(tmp_path, fake_litellm):
    """Zero at the provider is not zero overall. The one retry that survives is
    the one `_generate_and_parse` makes deliberately, for the one failure class
    a plain resend fixes — so a 5xx still costs two calls and the review still
    succeeds."""
    import litellm.exceptions as le

    fake = fake_litellm([
        le.ServiceUnavailableError(
            message="503", llm_provider="openai", model="gpt-4"),
        _completion(VALID),
    ])
    agent, ctx = _agent_and_context(_client(tmp_path))

    result = agent.review(ctx)

    assert fake.attempts == 2, "the deliberate resend was lost with the default"
    assert result.error is None
    assert len(result.findings) == 1


def test_the_client_the_agent_builds_for_itself_carries_the_same_budget(
    tmp_path, fake_litellm, monkeypatch,
):
    """There are two `_gen` closures. The second one runs when the orchestrator
    injects no client, which is the path that used to reach Google directly —
    fixing only the injected one would leave the hole open on exactly the
    branch that has historically been the one nobody looked at.
    """
    import src.llm.client as client_mod

    built = _client(tmp_path)
    monkeypatch.setattr(client_mod, "build_llm_client", lambda *a, **kw: built)

    fake = fake_litellm([_rate_limited()] * 8)
    agent, ctx = _agent_and_context(None)

    agent.review(ctx)

    assert fake.kwargs_seen[0]["num_retries"] == 0
    assert fake.attempts == 1
