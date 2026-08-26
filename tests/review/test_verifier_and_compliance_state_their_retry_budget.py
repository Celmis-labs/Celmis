"""The review's bracket surfaces — verifier and compliance — hand litellm zero.

tests/llm/test_a_guarantee_survives_the_layer_below_it.py already caught the
agents themselves: neither `_gen` closure passed `num_retries`, so litellm's
default of 3 retried one layer BELOW the classification that decides whether a
retry is worth anything. The same inheritance survived at the two review call
sites that are not LLMReviewAgent subclasses — `VerifierAgent._llm_verify` and
`compliance._evaluate`.

The verifier was the worse of the two. A rate-limited verify was resent three
times into the window that had just refused it, and only then "failed" into
pass-through: every review an unfiltered agent dump, with one WARNING line to
show for four provider calls. Compliance is one call per matching rule, so an
inherited budget there multiplied by the rule count.

Same method as the file that caught the agents: a stand-in for
`litellm.completion` that HONOURS `num_retries`, so the assertions are the
kwarg litellm actually received and how many times the provider was asked —
not what a mock of the layer above was told.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.review import compliance
from src.review.agents.base import AgentContext
from src.review.agents.verifier import VerifierAgent
from src.review.compliance import ComplianceCheckSpec, run_compliance
from src.review.models import Finding, FindingSeverity, Hunk, PullRequest


class _FakeLiteLLM:
    """A stand-in for `litellm.completion` that honours `num_retries`.

    Same contract as the original in
    tests/llm/test_a_guarantee_survives_the_layer_below_it.py: recording the
    kwarg alone would prove nothing about what it does, so the stub implements
    litellm's loop — one call, then up to `num_retries` more after a failure —
    and `attempts` is how many times the provider was asked.
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
                       else _completion('{"keep": [], "reasons": {}}'))
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


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="d",
        author="a", base_ref="main", base_sha="a", head_ref="f", head_sha="b",
        state="open",
        hunks=[Hunk(file_path="a.py", old_file_path="a.py", old_start=1,
                    old_count=1, new_start=1, new_count=1, content="@@")],
    )


def _findings() -> list[Finding]:
    """Three, because the verifier skips its LLM pass below that count."""
    return [
        Finding(file_path="a.py", line=i + 1, severity=FindingSeverity.WARNING,
                title=f"t{i}", body=f"b{i}", agent="security",
                rule_id=f"rule.{i}", confidence=0.9)
        for i in range(3)
    ]


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


# ─── the verifier ────────────────────────────────────────────────────


def test_the_verifier_hands_litellm_an_explicit_zero(tmp_path, fake_litellm):
    fake = fake_litellm([_completion('{"keep": [0, 1, 2], "reasons": {}}')])
    ctx = AgentContext(pull_request=_pr(), llm_client=_client(tmp_path))

    result = VerifierAgent(model="openai/gpt-4").verify(_findings(), ctx)

    assert fake.kwargs_seen, "the LLM pass never ran — the test proved nothing"
    assert fake.kwargs_seen[0]["num_retries"] == 0, (
        "the verifier inherited LLMClient.generate's default again"
    )
    assert len(result.kept) == 3


def test_a_rate_limited_verify_asks_the_provider_once_then_passes_through(
    tmp_path, fake_litellm,
):
    """The shape of the original defect, measured at the provider.

    With the inherited default this was FOUR requests inside the window that
    refused the first — and then the pass-through anyway. The pass-through is
    the part that must survive: an unavailable verifier must not delete
    findings, it just has to stop paying quadruple for its own failure.
    """
    fake = fake_litellm([_rate_limited()] * 8)
    ctx = AgentContext(pull_request=_pr(), llm_client=_client(tmp_path))

    result = VerifierAgent(model="openai/gpt-4").verify(_findings(), ctx)

    assert fake.attempts == 1, (
        f"the provider was asked {fake.attempts} times inside the window "
        "that had already refused the first call"
    )
    assert len(result.kept) == 3, "fail-open was the one behaviour to keep"
    assert result.dropped_llm_filter == 0


def test_the_client_the_verifier_builds_for_itself_carries_the_same_budget(
    tmp_path, fake_litellm, monkeypatch,
):
    """The verifier has the same two branches as the agents, and the fallback
    branch is the one that historically nobody looked at — fixing only the
    injected client would leave the fallback quietly retrying."""
    import src.llm.client as client_mod

    built = _client(tmp_path)
    monkeypatch.setattr(client_mod, "build_llm_client", lambda *a, **kw: built)
    fake = fake_litellm([_rate_limited()] * 8)
    ctx = AgentContext(pull_request=_pr(), llm_client=None)

    VerifierAgent(model="openai/gpt-4").verify(_findings(), ctx)

    assert fake.kwargs_seen[0]["num_retries"] == 0
    assert fake.attempts == 1


# ─── compliance ──────────────────────────────────────────────────────


def _one_rule(monkeypatch, *, blocking: bool = True) -> None:
    spec = ComplianceCheckSpec(
        id="no-secrets", name="No secrets", scope="workspace",
        glob_pattern="**", rule="No hardcoded secrets", severity="error",
        blocking=blocking,
    )
    monkeypatch.setattr(compliance, "load_active_checks", lambda repo: [spec])


def test_a_compliance_check_hands_litellm_an_explicit_zero(
    tmp_path, fake_litellm, monkeypatch,
):
    _one_rule(monkeypatch)
    fake = fake_litellm([_completion('{"passes": true, "reason": "ok"}')])
    ctx = AgentContext(pull_request=_pr(), llm_client=_client(tmp_path))

    result = run_compliance(ctx)

    assert fake.kwargs_seen[0]["num_retries"] == 0, (
        "compliance inherited LLMClient.generate's default again"
    )
    assert result.findings == []


def test_a_rate_limited_rule_costs_one_call_and_does_not_block(
    tmp_path, fake_litellm, monkeypatch,
):
    """Compliance is one call per matching rule — the inherited budget
    multiplied by the rule count. The 'unknown does not block' contract of
    `_evaluate` must survive the failure arriving faster."""
    _one_rule(monkeypatch, blocking=True)
    fake = fake_litellm([_rate_limited()] * 8)
    ctx = AgentContext(pull_request=_pr(), llm_client=_client(tmp_path))

    result = run_compliance(ctx)

    assert fake.attempts == 1
    assert result.findings == [], (
        "a rule that could not be evaluated is unknown, not violated"
    )


# ─── and their deadline, for the same reason ─────────────────────────


def test_every_review_llm_call_states_its_deadline():
    """The retry budget below is stated at these call sites because a decision
    made a layer down is a decision nobody can see. The DEADLINE was in
    exactly that state and worse: `generate`'s `timeout: float = 120` was
    inherited by every review call, named by no setting, and reachable by no
    operator — sixteen agent failures in eight hours on the benchmark install,
    every one of them this.

    Keyed on the call sites rather than on a number, so raising the default
    cannot retire the test and a sixth call site cannot join without one."""
    import ast
    import inspect

    import src.review.agents.base as base_mod
    import src.review.agents.verifier as verifier_mod
    import src.review.compliance as compliance_mod

    missing: list[str] = []
    for mod in (base_mod, verifier_mod, compliance_mod):
        tree = ast.parse(inspect.getsource(mod))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) != "generate":
                continue
            kwargs = {k.arg for k in node.keywords if k.arg}
            # `operation=` marks a real model call rather than an unrelated
            # `.generate(` on some other object.
            if "operation" not in kwargs:
                continue
            if "timeout" not in kwargs:
                missing.append(f"{mod.__name__}:{node.lineno}")

    assert not missing, f"review LLM calls with no deadline of their own: {missing}"
