"""The LLM veto that "did not answer" is asked once more, and reads damaged JSON.

A review of a small PR can end with "The LLM veto did not answer; 2
findings were kept unfiltered". The log says `verifier_unparseable`: the call
succeeds quickly, spends few output tokens (far under the ceiling, so
not a truncation and not a timeout), and the reply could not be read.

Two causes live in the reader. The prompt asks for a `checks` object
FIRST, and its sentences quote code; an unescaped quote or a raw newline there
makes the whole wrapper invalid JSON. The scan then moved on to the inner
`checks` object, which parses by itself, set "saw an object" and refused the
bare-array fallback — so a reply with a perfectly clear `"keep": [1]` was
reported unreadable. The reader now reads a closed `"keep"` array on its own,
tolerates raw control characters, and the pass asks once more before it gives
up. How many times it asked is recorded in the stage meta as `attempts`.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.review.agents.base import AgentContext
from src.review.agents.verifier import VerifierAgent
from src.review.models import Finding, FindingSeverity, Hunk, PullRequest


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="d",
        author="a", base_ref="main", base_sha="a", head_ref="f", head_sha="b",
        state="open",
        hunks=[Hunk(file_path="a.py", old_file_path="a.py", old_start=1,
                    old_count=1, new_start=1, new_count=1, content="@@")],
    )


def _findings(n: int = 3) -> list[Finding]:
    return [
        Finding(file_path="a.py", line=i + 1, severity=FindingSeverity.WARNING,
                title=f"t{i}", body=f"b{i}", agent="security",
                rule_id=f"rule.{i}", confidence=0.9)
        for i in range(n)
    ]


def _reply(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, input_tokens=10, output_tokens=5)


def _run(monkeypatch, replies: list):
    """Verify three findings with a veto that answers from `replies`."""
    asked: list[int] = []

    def fake(self, prompt, context, llm):
        asked.append(1)
        out = replies.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(VerifierAgent, "_call_llm", fake)
    ctx = AgentContext(pull_request=_pr())
    return VerifierAgent().llm_pass(_findings(), ctx), asked


BROKEN = ('{"checks": {"0": "premise: "$a" is set | verdict: refuted", '
          '"1": "ok", "2": "ok"},\n "keep": [1, 2],\n'
          ' "reasons": {"0": "line 12 sets it"}}')


def test_a_wrapper_that_is_not_json_still_yields_the_verdict():
    assert VerifierAgent._parse_keep_indices(BROKEN, total=3) == [1, 2]
    assert VerifierAgent._parse_reasons(BROKEN) == {0: "line 12 sets it"}


def test_raw_newline_inside_a_string_is_tolerated():
    text = '{"checks": {"0": "a\nb"}, "keep": [0], "reasons": {}}'
    assert VerifierAgent._parse_keep_indices(text, total=1) == [0]


def test_a_reply_cut_off_inside_the_array_stays_unreadable():
    """Salvage must not invent a verdict from half an array."""
    for text in ('{"checks": {"0": "x"}, "keep": [0, 1', '{"keep": [0,'):
        assert VerifierAgent._parse_keep_indices(text, total=3) is None


def test_an_unreadable_reply_is_asked_once_more(monkeypatch):
    result, asked = _run(monkeypatch, [_reply("I think they are fine."),
                                       _reply('{"keep": [0, 1, 2]}')])
    assert len(asked) == 2
    assert result.error is None
    assert result.veto_attempts == 2
    assert len(result.kept) == 3


def test_two_unreadable_replies_fail_open_with_the_attempts_recorded(monkeypatch):
    result, asked = _run(monkeypatch, [_reply("nope"), _reply("still nope"),
                                       _reply('{"keep": []}')])
    assert len(asked) == 2, "one extra ask, never a loop"
    assert result.error == "the verifier's reply could not be read"
    assert result.veto_attempts == 2
    assert len(result.kept) == 3
    assert result.tokens_out == 10, "both calls are paid for and counted"


def test_a_timeout_is_retried_but_a_throttle_is_not(monkeypatch):
    import litellm.exceptions as le

    timeout = le.Timeout(message="slow", model="m", llm_provider="openai")
    result, asked = _run(monkeypatch, [timeout, _reply('{"keep": [0]}')])
    assert len(asked) == 2 and result.error is None

    throttled = le.RateLimitError(message="Too many requests",
                                  llm_provider="openai", model="m")
    result, asked = _run(monkeypatch, [throttled, _reply('{"keep": [0]}')])
    assert len(asked) == 1, "resending into a window that just refused"
    assert result.error and len(result.kept) == 3


def test_the_stage_meta_carries_the_attempts():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "src" / "review"
           / "orchestrator.py").read_text(encoding="utf-8")
    assert '"attempts": v_result.veto_attempts' in src
