"""A Claude Code review says which credential paid for it.

The engine resolves a subscription token first and falls back to the
workspace's Anthropic API key when no Claude connection exists. The fallback
is the right default. Being silent about it was not: the engine is labelled
"Claude Code (subscription)" in the UI, so an operator reasonably believes a
run costs nothing beyond their subscription, while every run was quietly
spending workspace Anthropic credit per token — discovered at the invoice.

Same failure class this project has already hit four times: a setting that
looks saved and does nothing. A Gemini key that never reached Gemini. A
thinking budget the container never received. A master password that was the
text of a comment. A sandbox token the setup script could not see.

Three things have to be true, and each has a test below:
  * the run record names the credential path, on success AND on failure —
    a run that errored halfway still spent whatever it spent;
  * the log says it once per run, at warning, naming the consequence;
  * the spend ledger books real money on the fallback and zero (but real
    tokens) on the subscription — an understated Usage page is worse than
    no Usage page.

And when NEITHER credential exists the engine refuses by name, before the
`claude` subprocess exists to fail obscurely inside.
"""

from __future__ import annotations

import logging
import sys
import types

import pytest

from src.review import claude_engine as ce
from src.review.models import Hunk, PullRequest


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/api", number=11,
        title="t", description="d", author="alice",
        base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
        state="open",
        hunks=[Hunk(
            file_path="src/foo.py", old_file_path="src/foo.py",
            old_start=1, old_count=1, new_start=1, new_count=2,
            content="@@ -1 +1,2 @@\n line\n+added\n",
        )],
        raw_diff="--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1 +1,2 @@\n line\n+added\n",
    )


# ─── a scriptable stand-in for claude_agent_sdk ──────────────────────
# The real SDK spawns the `claude` binary. These carry only the attributes
# the engine reads, so a test can say exactly what came back over the stream.


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _AssistantMessage:
    def __init__(self, text: str) -> None:
        self.content = [_TextBlock(text)]


class _ResultMessage:
    def __init__(
        self, *, num_turns: int = 3, total_cost_usd: float | None = 0.42,
        is_error: bool = False, result: str = "",
        usage: dict | None = None, model: str = "claude-sonnet-4-6",
    ) -> None:
        self.num_turns = num_turns
        self.total_cost_usd = total_cost_usd
        self.is_error = is_error
        self.result = result
        self.usage = {"input_tokens": 1200, "output_tokens": 340} if usage is None else usage
        self.model = model


_CLEAN_ANSWER = '```json\n{"summary": "looks fine", "findings": []}\n```'


@pytest.fixture
def sdk(monkeypatch) -> list:
    """Install the fake SDK; append messages to the returned list to script
    what the stream yields."""
    script: list = []

    class _Options:
        def __init__(self, **kw) -> None:
            self.kw = kw

    class _Client:
        def __init__(self, options=None) -> None:
            self.options = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def query(self, prompt: str) -> None:
            self.prompt = prompt

        async def receive_response(self):
            for message in script:
                yield message

    module = types.ModuleType("claude_agent_sdk")
    module.AssistantMessage = _AssistantMessage
    module.TextBlock = _TextBlock
    module.ResultMessage = _ResultMessage
    module.ClaudeAgentOptions = _Options
    module.ClaudeSDKClient = _Client
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    return script


@pytest.fixture
def spend(monkeypatch) -> list:
    """Everything else a run touches, stubbed. Returns the kwargs of each
    `record_claude_code_spend` call, which is how the run tells the ledger
    which credential it used."""
    import src.agent.runner as runner_mod
    import src.review.agents.base as base_mod

    monkeypatch.setattr(runner_mod, "_mint_mcp_token", lambda user_id: "mcp-token")
    monkeypatch.setattr(base_mod, "_review_language_instruction", lambda ws: "")
    calls: list = []
    monkeypatch.setattr(
        ce, "record_claude_code_spend",
        lambda message, **kw: calls.append(kw),
    )
    return calls


def _subscription(monkeypatch, source: str = "personal") -> None:
    import src.agent.connection as conn_mod
    from src.agent.connection import ClaudeConnection

    monkeypatch.setattr(
        conn_mod, "resolve_connection",
        lambda user_id, ws: ClaudeConnection(
            token="sk-ant-oat-not-a-real-token-value", source=source, saved_by=None,
        ),
    )


def _no_connection(monkeypatch) -> None:
    import src.agent.connection as conn_mod
    monkeypatch.setattr(conn_mod, "resolve_connection", lambda user_id, ws: None)


def _workspace_api_key(monkeypatch) -> None:
    import src.llm.keys as keys_mod
    monkeypatch.setattr(
        keys_mod, "resolve_api_key",
        lambda provider, user_id="default", *, workspace_id="default": "not-a-real-key-value",
    )


def _no_api_key(monkeypatch) -> None:
    import src.llm.keys as keys_mod
    from src.llm.keys import LLMCredentialError

    def _raise(provider, user_id="default", *, workspace_id="default"):
        raise LLMCredentialError(f"no key for {provider}")

    monkeypatch.setattr(keys_mod, "resolve_api_key", _raise)


# ─── 1. the run record names the credential path ─────────────────────


def test_a_subscription_run_is_recorded_as_a_subscription_run(sdk, spend, monkeypatch):
    _subscription(monkeypatch)
    sdk.extend([_AssistantMessage(_CLEAN_ANSWER), _ResultMessage()])

    result = ce.run_claude_review(_pr(), user_id="u1", workspace_id="ws1")

    assert result.error is None
    assert result.cost_source == ce.RUN_COST_SOURCE_SUBSCRIPTION


def test_a_fallback_run_is_recorded_as_an_api_key_run(sdk, spend, monkeypatch):
    """The whole point. Without this the run row said "claude_code" for both,
    and nothing anywhere distinguished a free run from a billed one."""
    _no_connection(monkeypatch)
    _workspace_api_key(monkeypatch)
    sdk.extend([_AssistantMessage(_CLEAN_ANSWER), _ResultMessage()])

    result = ce.run_claude_review(_pr(), user_id="u1", workspace_id="ws1")

    assert result.error is None
    assert result.cost_source == ce.RUN_COST_SOURCE_API_KEY
    assert result.cost_source != ce.RUN_COST_SOURCE_SUBSCRIPTION


def test_a_workspace_shared_subscription_still_counts_as_subscription(sdk, spend, monkeypatch):
    """Personal and workspace-shared are two slots but one billing story —
    neither spends Anthropic credit."""
    _subscription(monkeypatch, source="workspace")
    sdk.extend([_AssistantMessage(_CLEAN_ANSWER), _ResultMessage()])

    result = ce.run_claude_review(_pr(), user_id="u1", workspace_id="ws1")

    assert result.cost_source == ce.RUN_COST_SOURCE_SUBSCRIPTION


@pytest.mark.parametrize(
    ("answer", "final"),
    [
        # Model never produced a JSON block — turns were still paid for.
        ("I had a look but I am not sure.", _ResultMessage()),
        # SDK itself reported the run as failed, mid-session.
        (_CLEAN_ANSWER, _ResultMessage(is_error=True, result="rate limited")),
    ],
)
def test_a_failed_fallback_run_still_names_the_api_key(sdk, spend, monkeypatch, answer, final):
    """A review that produced nothing usable still spent money on the way
    there. Naming the credential only in the success arm would hide exactly
    the runs an operator most wants to see."""
    _no_connection(monkeypatch)
    _workspace_api_key(monkeypatch)
    sdk.extend([_AssistantMessage(answer), final])

    result = ce.run_claude_review(_pr(), user_id="u1", workspace_id="ws1")

    assert result.error
    assert result.cost_source == ce.RUN_COST_SOURCE_API_KEY


def test_the_run_tells_the_ledger_the_same_credential_it_reports(sdk, spend, monkeypatch):
    """One resolution, one answer: the run record and the ledger row must not
    be able to disagree about who paid."""
    _no_connection(monkeypatch)
    _workspace_api_key(monkeypatch)
    sdk.extend([_AssistantMessage(_CLEAN_ANSWER), _ResultMessage()])

    result = ce.run_claude_review(_pr(), user_id="u1", workspace_id="ws1")

    assert len(spend) == 1
    assert spend[0]["api_key_auth"] is True
    assert result.cost_source == ce.RUN_COST_SOURCE_API_KEY


def test_a_subscription_run_does_not_tell_the_ledger_it_was_billed(sdk, spend, monkeypatch):
    _subscription(monkeypatch)
    sdk.extend([_AssistantMessage(_CLEAN_ANSWER), _ResultMessage()])

    ce.run_claude_review(_pr(), user_id="u1", workspace_id="ws1")

    assert len(spend) == 1
    assert spend[0]["api_key_auth"] is False


# ─── 2. one log line per run, naming the consequence ─────────────────


def test_the_fallback_warns_once_per_run_not_once_per_message(sdk, spend, monkeypatch, caplog):
    """Resolution happens once, before the stream opens. A warning emitted
    from the message loop instead would repeat for every chunk and be tuned
    out — which is the same as not warning at all."""
    _no_connection(monkeypatch)
    _workspace_api_key(monkeypatch)
    sdk.extend([
        _AssistantMessage("Let me look at the diff."),
        _AssistantMessage("Checking the callers."),
        _AssistantMessage("One more thing."),
        _AssistantMessage(_CLEAN_ANSWER),
        _ResultMessage(),
    ])

    with caplog.at_level(logging.DEBUG, logger="src.review.claude_engine"):
        ce.run_claude_review(_pr(), user_id="u1", workspace_id="ws1")

    hits = [r for r in caplog.records if "claude_code_api_key_fallback" in r.getMessage()]
    assert len(hits) == 1, f"expected exactly one fallback line, got {len(hits)}"
    assert hits[0].levelno >= logging.WARNING, (
        "an INFO line about someone else's money is a line nobody reads"
    )


def test_the_fallback_line_names_the_consequence_not_just_the_fact(sdk, spend, monkeypatch, caplog):
    """"no claude connection" tells an operator nothing they can act on.
    "this review is billed to the Anthropic key" does."""
    _no_connection(monkeypatch)
    _workspace_api_key(monkeypatch)
    sdk.extend([_AssistantMessage(_CLEAN_ANSWER), _ResultMessage()])

    with caplog.at_level(logging.DEBUG, logger="src.review.claude_engine"):
        ce.run_claude_review(_pr(), user_id="u1", workspace_id="ws1")

    line = next(
        r.getMessage() for r in caplog.records
        if "claude_code_api_key_fallback" in r.getMessage()
    ).lower()
    assert "billed" in line
    assert "anthropic" in line
    assert "subscription" in line
    assert "ws1" in line


def test_a_connected_subscription_does_not_cry_wolf(sdk, spend, monkeypatch, caplog):
    """A warning on every run, including the ones that are genuinely free, is
    how a real warning stops being read."""
    _subscription(monkeypatch)
    sdk.extend([_AssistantMessage(_CLEAN_ANSWER), _ResultMessage()])

    with caplog.at_level(logging.DEBUG, logger="src.review.claude_engine"):
        ce.run_claude_review(_pr(), user_id="u1", workspace_id="ws1")

    assert not [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "fallback" in r.getMessage()
    ]


def test_the_fallback_line_carries_no_key_material(sdk, spend, monkeypatch, caplog):
    _no_connection(monkeypatch)
    _workspace_api_key(monkeypatch)
    sdk.extend([_AssistantMessage(_CLEAN_ANSWER), _ResultMessage()])

    with caplog.at_level(logging.DEBUG, logger="src.review.claude_engine"):
        ce.run_claude_review(_pr(), user_id="u1", workspace_id="ws1")

    for record in caplog.records:
        assert "not-a-real-key-value" not in record.getMessage()


# ─── 3. the ledger is honest on both paths ───────────────────────────


@pytest.fixture
def ledger(monkeypatch) -> list:
    import src.llm.budget as budget_mod
    rows: list = []
    monkeypatch.setattr(budget_mod, "record_spend", lambda **kw: rows.append(kw))
    return rows


def test_the_api_key_path_books_the_money_that_actually_left(ledger):
    """The Usage page is the only place a workspace sees this spend. A zero
    here understates a real bill, which is worse than showing nothing."""
    ce.record_claude_code_spend(
        _ResultMessage(total_cost_usd=0.37), surface="review",
        workspace_id="ws1", user_id="u1", repo="acme/api", api_key_auth=True,
    )

    assert len(ledger) == 1
    assert ledger[0]["cost_usd"] == pytest.approx(0.37)
    assert ledger[0]["cost_source"] == ce.LEDGER_COST_SOURCE_API_KEY
    assert ledger[0]["tokens_in"] == 1200
    assert ledger[0]["tokens_out"] == 340


def test_the_subscription_path_books_real_tokens_and_no_money(ledger):
    """The SDK reports an API-equivalent price on a subscription run too.
    Booking it would invent spend that never happened — the tokens are real,
    the dollars are not."""
    ce.record_claude_code_spend(
        _ResultMessage(total_cost_usd=0.37), surface="review",
        workspace_id="ws1", user_id="u1", repo="acme/api", api_key_auth=False,
    )

    assert len(ledger) == 1
    assert ledger[0]["cost_usd"] == 0.0
    assert ledger[0]["cost_source"] == ce.LEDGER_COST_SOURCE_SUBSCRIPTION
    assert ledger[0]["tokens_in"] == 1200
    assert ledger[0]["tokens_out"] == 340


def test_a_charge_with_no_token_breakdown_is_still_booked(ledger):
    """The SDK has moved the shape of `usage` before. On the API-key path the
    charge is real whether or not the tokens came broken out, and dropping the
    row loses money from the Usage page silently."""
    ce.record_claude_code_spend(
        _ResultMessage(total_cost_usd=0.21, usage={}), surface="review",
        workspace_id="ws1", user_id="u1", api_key_auth=True,
    )

    assert len(ledger) == 1
    assert ledger[0]["cost_usd"] == pytest.approx(0.21)
    assert ledger[0]["cost_source"] == ce.LEDGER_COST_SOURCE_API_KEY


def test_a_subscription_run_with_no_usage_books_nothing(ledger):
    """Nothing to book: no tokens counted and no money charged. An all-zero
    row is noise in the ledger."""
    ce.record_claude_code_spend(
        _ResultMessage(total_cost_usd=0.21, usage={}), surface="review",
        workspace_id="ws1", user_id="u1", api_key_auth=False,
    )

    assert ledger == []


def test_a_free_api_key_result_with_no_usage_books_nothing(ledger):
    """No tokens and no charge is not a spend event on either path."""
    ce.record_claude_code_spend(
        _ResultMessage(total_cost_usd=0.0, usage={}), surface="review",
        workspace_id="ws1", user_id="u1", api_key_auth=True,
    )

    assert ledger == []


# ─── 4. neither credential: refuse, by name ──────────────────────────


def test_with_neither_credential_the_engine_refuses_before_the_subprocess(spend, monkeypatch):
    """`claude_agent_sdk` is made unimportable on purpose: if the refusal ever
    slips past the auth check, this test fails with an ImportError instead of
    passing quietly — which is the point, since the real failure mode was an
    auth error thrown deep inside the `claude` subprocess."""
    _no_connection(monkeypatch)
    _no_api_key(monkeypatch)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

    result = ce.run_claude_review(_pr(), user_id="u1", workspace_id="ws1")

    assert result.findings == []
    assert result.error


def test_the_refusal_names_both_ways_to_fix_it(spend, monkeypatch):
    _no_connection(monkeypatch)
    _no_api_key(monkeypatch)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

    error = (ce.run_claude_review(_pr(), user_id="u1", workspace_id="ws1").error or "").lower()

    assert "/claude" in error, "the subscription remedy is missing"
    assert "anthropic" in error, "the API-key remedy is missing"


def test_the_refusal_claims_no_credential_it_did_not_use(spend, monkeypatch):
    """A run that never chose a credential must not be filed under either —
    "unknown" is the honest answer and the one ReviewBatch already starts
    from."""
    _no_connection(monkeypatch)
    _no_api_key(monkeypatch)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

    result = ce.run_claude_review(_pr(), user_id="u1", workspace_id="ws1")

    assert result.cost_source not in (
        ce.RUN_COST_SOURCE_SUBSCRIPTION, ce.RUN_COST_SOURCE_API_KEY,
    )


def test_the_refusal_is_logged_where_an_operator_will_see_it(spend, monkeypatch, caplog):
    _no_connection(monkeypatch)
    _no_api_key(monkeypatch)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

    with caplog.at_level(logging.DEBUG, logger="src.review.claude_engine"):
        ce.run_claude_review(_pr(), user_id="u1", workspace_id="ws1")

    hits = [r for r in caplog.records if "claude_code_no_credential" in r.getMessage()]
    assert len(hits) == 1
    assert hits[0].levelno >= logging.ERROR
    message = hits[0].getMessage().lower()
    assert "/claude" in message
    assert "anthropic" in message


def test_the_report_path_resolves_the_same_way(monkeypatch, caplog):
    """`_resolve_env` is what src/deps/report.py and the deps router call. It
    still hands back the plain env mapping they splat into ClaudeAgentOptions,
    and it warns about the same fallback they are also paying for."""
    _no_connection(monkeypatch)
    _workspace_api_key(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger="src.review.claude_engine"):
        env = ce._resolve_env("u1", "ws1")

    assert list(env) == ["ANTHROPIC_API_KEY"]
    assert [r for r in caplog.records if "claude_code_api_key_fallback" in r.getMessage()]


def test_a_connected_report_gets_the_oauth_token(monkeypatch):
    _subscription(monkeypatch)

    env = ce._resolve_env("u1", "ws1")

    assert list(env) == ["CLAUDE_CODE_OAUTH_TOKEN"]
