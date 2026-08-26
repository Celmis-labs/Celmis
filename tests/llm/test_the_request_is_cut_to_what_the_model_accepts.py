"""A ceiling above the model's own is a 400, hours later, naming nothing.

The architect agent failed in 43% of runs against a hardcoded 4096-token
output budget — Gemini 3.x counts reasoning tokens against it, so the findings
array was truncated mid-JSON. The fix raised the number, and raising a number
is how you buy the opposite failure: ask a model for more output than it
accepts and the provider answers 400. A configuration mistake then arrives as
an inference failure, in a review, with neither number in the message.

So the request is cut to what the model actually accepts, once, where the model
is known — and the cut is logged with BOTH numbers and carried on the result.

The two halves of the contract that are easy to get backwards:

  * a model LiteLLM has no entry for is NOT clamped. There is nothing to clamp
    to, and a guessed ceiling truncates a call that would have worked;
  * the review agent's corrective retry doubles its budget, so the doubling has
    to be clamped too — it is the one path that can exceed a ceiling the
    configured value was already under.

Measured at the kwargs `litellm.completion` receives, not at a mock of the
layer above: `max_tokens` is what the provider is asked for.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

#: LiteLLM 1.97.0 has an entry for this one and stops it at 4096 output tokens.
SMALL_MODEL = "openai/gpt-4"
#: Same, at 8192 — big enough that a doubling crosses it and a single call does not.
MID_MODEL = "gemini/gemini-2.0-flash"
#: LiteLLM has NO entry for this: `gemini-3-pro-preview` is mapped, the release
#: name is not. A self-hosted `openai/<name>` behaves identically.
UNMAPPED_MODEL = "gemini/gemini-3-pro"

VALID = '[{"reasoning": "line 1 reads x before it is assigned", "file": "a.py", "line": 1, "severity": "critical", "title": "t", "body": "b"}]'


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


class _FakeLiteLLM:
    """Records every kwarg set `litellm.completion` was handed."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.kwargs_seen: list[dict] = []

    def __call__(self, **kwargs):
        self.kwargs_seen.append(kwargs)
        return _Completion(self.replies.pop(0) if self.replies else VALID)

    @property
    def budgets(self) -> list[int | None]:
        return [kw.get("max_tokens") for kw in self.kwargs_seen]


@pytest.fixture(autouse=True)
def _fresh_capability_state():
    """A clamp is reported once per process, so a second test asserting the
    same warning would see nothing at all."""
    from src.llm.capabilities import reset_capability_caches
    reset_capability_caches()
    yield
    reset_capability_caches()


@pytest.fixture
def fake_litellm(monkeypatch):
    import litellm

    def _install(replies: list[str] | None = None) -> _FakeLiteLLM:
        fake = _FakeLiteLLM(replies or [])
        monkeypatch.setattr(litellm, "completion", fake)
        return fake

    return _install


def _client(tmp_path: Path, model: str):
    from src.llm.client import LLMClient
    from src.security.audit import AuditLogger

    return LLMClient(
        resolve_key=lambda provider: "sk-test",
        resolve_model=lambda agent: model,
        surface="review",
        audit=AuditLogger(tmp_path / "audit.jsonl"),
        workspace_id="ws-test",
    )


def _generate(client, **kwargs):
    return client.generate(
        prompt="p", agent="architect", operation="review_architect", **kwargs,
    )


# ─── the clamp itself ────────────────────────────────────────────────


def test_a_budget_above_the_model_ceiling_is_cut_to_it(tmp_path, fake_litellm):
    fake = fake_litellm()

    result = _generate(_client(tmp_path, SMALL_MODEL), max_output_tokens=16384)

    assert fake.budgets == [4096], (
        f"the provider was asked for {fake.budgets[0]} output tokens; gpt-4 "
        "stops at 4096 and answers 400 to anything above it"
    )
    assert result.max_output_tokens_clamped_to == 4096, (
        "the cut has to reach the result — otherwise the only trace of a "
        "misconfiguration is a shorter answer nobody can explain"
    )


def test_the_cut_names_both_numbers_once(tmp_path, fake_litellm, caplog):
    fake_litellm()
    client = _client(tmp_path, SMALL_MODEL)

    with caplog.at_level(logging.WARNING, logger="src.llm.capabilities"):
        _generate(client, max_output_tokens=16384)
        _generate(client, max_output_tokens=16384)

    lines = [r.getMessage() for r in caplog.records
             if "max_output_tokens_clamped" in r.getMessage()]
    assert len(lines) == 1, (
        f"{len(lines)} warnings for one standing misconfiguration — a clamp is "
        "a state, not an event, and one line per agent per review is noise"
    )
    assert "16384" in lines[0] and "4096" in lines[0], (
        f"the warning must name what was configured AND what the model takes: {lines[0]!r}"
    )


def test_a_budget_under_the_ceiling_is_left_alone(tmp_path, fake_litellm):
    fake = fake_litellm()

    result = _generate(_client(tmp_path, SMALL_MODEL), max_output_tokens=1024)

    assert fake.budgets == [1024]
    assert result.max_output_tokens_clamped_to is None


def test_a_model_litellm_never_heard_of_is_not_clamped(tmp_path, fake_litellm):
    """The half that is easy to get backwards.

    `gemini-3-pro` is not in the installed table — nor is any self-hosted
    `openai/<name>`, nor next month's release. Substituting a plausible ceiling
    there would truncate a call that would have worked, and would do it while
    reporting a number nobody configured.
    """
    fake = fake_litellm()

    result = _generate(_client(tmp_path, UNMAPPED_MODEL), max_output_tokens=999_999)

    assert fake.budgets == [999_999], (
        "an unknown model was clamped to a ceiling that was invented for it"
    )
    assert result.max_output_tokens_clamped_to is None


def test_the_capability_answer_for_an_unmapped_model_says_unknown():
    """And the source of that decision states it, rather than defaulting."""
    from src.llm.capabilities import model_capabilities

    caps = model_capabilities(UNMAPPED_MODEL).as_dict()

    assert caps["known"] is False
    assert caps["source"] == "unknown"
    assert caps["max_output_tokens"] is None
    assert caps["supports_reasoning"] is None
    assert caps["reasoning_kind"] is None


def test_a_mapped_model_reports_what_litellm_holds():
    from src.llm.capabilities import model_capabilities

    caps = model_capabilities("gemini/gemini-3-flash-preview").as_dict()

    assert caps["known"] is True
    assert caps["source"] == "litellm"
    assert caps["max_output_tokens"] == 65535
    assert caps["supports_reasoning"] is True
    assert caps["reasoning_kind"] == "effort"
    assert "high" in caps["reasoning_values"]
    assert caps["supports_function_calling"] is True


# ─── the corrective retry ────────────────────────────────────────────


def _agent_and_context(llm_client, agent_llm):
    from src.review.agents.base import AgentContext, LLMReviewAgent
    from src.review.models import Hunk, PullRequest

    class _Agent(LLMReviewAgent):
        name = "architect"
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
    return _Agent(), AgentContext(
        pull_request=pr, llm_client=llm_client, agent_llm=agent_llm,
    )


def test_the_corrective_retry_doubles_but_stops_at_the_model_ceiling(
    tmp_path, fake_litellm,
):
    """Both halves in one measurement.

    The commonest unreadable reply is one truncated by the output budget, so
    the second attempt doubles it — deterministically, which is the whole
    reason a verbatim resend would be pointless. 6000 doubles to 12000 and the
    model stops at 8192: without the clamp the correction is a 400, and the
    agent fails on a retry that existed to rescue it.
    """
    from src.review.settings import AgentLLMSettings

    fake = fake_litellm(["not json at all", VALID])
    agent, ctx = _agent_and_context(
        _client(tmp_path, MID_MODEL),
        {"architect": AgentLLMSettings(max_output_tokens=6000)},
    )

    result = agent.review(ctx)

    assert fake.budgets == [6000, 8192], (
        f"attempt budgets were {fake.budgets}; the first is the configured "
        "6000, the second is 12000 cut down to what the model accepts"
    )
    assert len(result.findings) == 1, "the corrective retry stopped working"
    assert result.max_output_tokens_clamped_to == 8192


def test_a_retry_that_stays_under_the_ceiling_doubles_untouched(
    tmp_path, fake_litellm,
):
    """The clamp must not quietly become the ceiling for everybody."""
    from src.review.settings import AgentLLMSettings

    fake = fake_litellm(["not json at all", VALID])
    agent, ctx = _agent_and_context(
        _client(tmp_path, MID_MODEL),
        {"architect": AgentLLMSettings(max_output_tokens=2000)},
    )

    result = agent.review(ctx)

    assert fake.budgets == [2000, 4000]
    assert result.max_output_tokens_clamped_to is None
