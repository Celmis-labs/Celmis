"""When the model's reply cannot be read, the log says what the model said.

It did not. `_parse_findings` logged `agent_no_json agent=security` and
raised; what the reply actually contained was stored nowhere — not in the
log, not in the run record. So when ~11% of security runs on a benchmark
ended that way, the only path to a diagnosis was intercepting the call inside
the container, which is what it took: the reply was a refusal, prose with no
JSON, and every layer above had called it "unreadable" and moved on.

So an unreadable reply now leaves evidence: its first ~300 characters on the
WARNING line, the whole of it at DEBUG. Redacted first — the reply is model
output, the model had the diff, and the diff can carry the secret somebody
committed; a log is as much an egress as a prompt. Redacted WHOLE, before the
cut: a key cut at the preview boundary is half a key, which no key pattern
matches and the log then keeps. And never the prompt: it is the customer's
code, and the reply is evidence enough.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import src.security.redactor as redactor_mod
from src.review.agents.base import AgentContext, LLMReviewAgent
from src.review.models import Hunk, PullRequest

LOGGER = "src.review.agents.base"
PROMPT_SENTINEL = "PROMPT-SENTINEL-d41d8cd98f00b204-the-diff-goes-here"

VALID = '[{"reasoning": "line 1 reads x before it is assigned", "file": "a.py", "line": 1, "severity": "critical", "title": "t", "body": "b"}]'

#: Prose with no array in it, long enough that the preview has to cut, with a
#: marker well past the cut so "preview" and "full" can be told apart.
LONG_PROSE = (
    "The diff adds a helper and renames two callers. " * 10
    + "MARKER-DEEP-IN-THE-REPLY "
    + "More prose follows, none of it an array. " * 20
)
assert LONG_PROSE.index("MARKER-DEEP") > 300


def _response(text: str) -> MagicMock:
    r = MagicMock()
    r.text = text
    r.input_tokens = 100
    r.output_tokens = 40
    r.cost_usd = 0.01
    r.cost_source = "litellm_estimate"
    r.model = "m"
    return r


def _ctx(replies: list[str]) -> tuple[AgentContext, MagicMock]:
    client = MagicMock()
    client.generate.side_effect = [_response(t) for t in replies]
    pr = PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="d",
        author="a", base_ref="main", base_sha="a", head_ref="f", head_sha="b",
        state="open",
        hunks=[Hunk(file_path="a.py", old_file_path="a.py", old_start=1,
                    old_count=1, new_start=1, new_count=1, content="@@")],
    )
    return AgentContext(pull_request=pr, llm_client=client), client


class _Agent(LLMReviewAgent):
    name = "security"
    system_prompt = "find problems"

    def _build_prompt(self, context):
        return PROMPT_SENTINEL


def _messages(caplog, level: int | None = None) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.name == LOGGER and (level is None or r.levelno == level)
    ]


# ─── The preview: ~300 chars at WARNING, the whole thing at DEBUG ────


def test_the_warning_line_carries_the_start_of_the_reply_and_debug_carries_it_all(caplog):
    ctx, _ = _ctx([LONG_PROSE, VALID])
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        _Agent().review(ctx)

    warnings = [m for m in _messages(caplog, logging.WARNING) if "agent_no_json" in m]
    assert len(warnings) == 1, warnings
    line = warnings[0]
    assert "The diff adds a helper and renames two callers." in line, (
        "the WARNING line used to say `agent_no_json agent=security` and "
        "nothing else — the evidence is the point"
    )
    assert "MARKER-DEEP-IN-THE-REPLY" not in line, "~300 characters, not the essay"
    assert "…" in line, "a cut preview says it was cut"
    assert len(line) < 400

    full = [m for m in _messages(caplog, logging.DEBUG) if "agent_reply_unreadable_full" in m]
    assert len(full) == 1, full
    assert "MARKER-DEEP-IN-THE-REPLY" in full[0]


def test_the_prompt_is_never_in_the_log(caplog):
    ctx, _ = _ctx([LONG_PROSE, LONG_PROSE])
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        _Agent().review(ctx)

    assert _messages(caplog), "nothing was logged at all — the test is not looking"
    for message in _messages(caplog):
        assert PROMPT_SENTINEL not in message, message


def test_a_parse_failure_leaves_evidence_too(caplog):
    """The other branch of `_parse_findings`: an array was found, and it was
    not JSON. Same operator, same question — what did the model say?"""
    ctx, _ = _ctx(['[{"file": "a.py",]', VALID])
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        _Agent().review(ctx)

    lines = [m for m in _messages(caplog, logging.WARNING) if "agent_json_parse_failed" in m]
    assert len(lines) == 1, lines
    assert '[{"file": "a.py",]' in lines[0]


# ─── Redacted: whole, first, and fail-closed ─────────────────────────


def test_a_secret_shaped_string_in_the_reply_does_not_reach_the_log(caplog):
    key = "sk-live-" + "A" * 40
    aws = "AKIAIOSFODNN7EXAMPLE"
    reply = (
        "Here is what I saw in the change:\n"
        f"OPENAI_KEY = '{key}'\n"
        f"the bucket client is built with {aws}\n"
        'password = "hunter2hunter2hunter2hunter2"\n'
        "and that is all, no findings array from me today."
    )
    ctx, _ = _ctx([reply, reply])
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = _Agent().review(ctx)

    messages = _messages(caplog)
    assert any("[REDACTED:" in m for m in messages), "the reply was logged — redacted"
    for message in messages:
        for leak in (key, "sk-live", aws, "hunter2"):
            assert leak not in message, (leak, message)
    assert result.error is not None
    for leak in (key, aws, "hunter2"):
        assert leak not in result.error


def test_the_reply_is_redacted_whole_before_it_is_cut(caplog):
    """A key that straddles the preview boundary. Cut first and only the
    first few characters of it are in the preview — which no key pattern
    matches, so they stay. Redact first and the whole key is gone before
    there is a boundary to straddle."""
    filler = "The diff adds a helper. " * 12          # 288 chars: the cut lands inside the key
    key = "sk-live-" + "A" * 40
    reply = f"{filler}{key} is hard-coded on line 9, and that is my only note."
    assert 280 < reply.index(key) < 300 < reply.index(key) + len(key)

    ctx, _ = _ctx([reply, reply])
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        _Agent().review(ctx)

    messages = _messages(caplog)
    assert any("[REDACTED:" in m for m in messages)
    for message in messages:
        assert "sk-live" not in message, message
        assert "sk-" not in message, message


def test_a_redactor_that_raises_withholds_the_reply_rather_than_logging_it_raw(
    caplog, monkeypatch,
):
    """Fail-closed, the same rule every other caller of the redactor keeps:
    no text beats unredacted text. The agent itself still finishes — a
    crashed redactor is not a reason to lose the review."""
    def _boom(*_a, **_k):
        raise RuntimeError("redactor crashed")

    monkeypatch.setattr(redactor_mod, "redact", _boom)
    secret_prose = "Sorry, no array: the key is sk-live-" + "B" * 40 + " and I quit."
    ctx, _ = _ctx([secret_prose, VALID])
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        result = _Agent().review(ctx)

    assert result.error is None and len(result.findings) == 1, "the agent survived"
    messages = _messages(caplog)
    assert any("withheld" in m for m in messages)
    for message in messages:
        assert "sk-live" not in message, message
        assert "and I quit" not in message, message


def test_a_refusal_leaves_its_own_line_with_the_same_evidence(caplog):
    refusal = (
        "Sorry, I cannot fulfill your request to analyze or identify "
        "vulnerabilities in specific code snippets."
    )
    ctx, _ = _ctx([refusal, VALID])
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        _Agent().review(ctx)

    lines = [m for m in _messages(caplog, logging.WARNING) if "agent_model_refused" in m]
    assert lines, "a refusal is named as one, not filed under agent_no_json"
    assert any("cannot fulfill your request" in m for m in lines)
    assert not [m for m in _messages(caplog) if "agent_no_json" in m]
