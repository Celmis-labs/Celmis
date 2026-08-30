"""Claude Code review engine — the workspace's alternative review "brain".

When a workspace sets review_engine="claude_code", the PR is reviewed by a
single headless Claude Code run (the user's subscription token, or the
workspace's Anthropic API key as fallback) instead of the 5-agent LiteLLM
pipeline. Everything around the brain — policy gates, posting, persistence,
verdict handling — stays the platform's.

The model gets the diff + repo context in the prompt and the internal Celmis
MCP tools for exploration, and must answer with a strict JSON block that maps
1:1 onto Finding rows.

Which credential paid is never silent. The engine is labelled "Claude Code
(subscription)" in the UI, so an operator reasonably reads a run as costing
nothing beyond their subscription — while a workspace with no Claude
connection quietly spends the workspace's Anthropic credit, per token, and
finds out at the invoice. Every run therefore says which of the two paid, in
three places: the run record (`ClaudeReviewResult.cost_source`), one warning
line in the log, and the spend ledger.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from src.review.models import Finding, FindingSeverity, HunkSide, PullRequest

logger = logging.getLogger(__name__)

_MAX_DIFF_CHARS = 120_000
_MAX_TURNS = 15

_SEVERITIES = {s.value for s in FindingSeverity}

# ─── Which credential paid, in the two vocabularies that need to know ──
#
# Run record — `review_runs.cost_source`, the field that QUALIFIES that row's
# `cost_usd` everywhere else in this codebase ('litellm_estimate' vs
# 'openrouter_actual'). The old value here was a flat "claude_code", which
# answered "which engine" and not "whose money"; these two extend it rather
# than adding a second flag beside it.
RUN_COST_SOURCE_SUBSCRIPTION = "claude_code_subscription"
RUN_COST_SOURCE_API_KEY = "claude_code_api_key"

# Spend ledger — `llm_spend.cost_source`. These two strings are already
# understood downstream (src/api/routers/spend.py splits "subscription" out of
# the billed total), so they must stay exactly as they are.
LEDGER_COST_SOURCE_SUBSCRIPTION = "subscription"
LEDGER_COST_SOURCE_API_KEY = "sdk_actual"

#: Refusal text when NEITHER credential exists. One constant, so the engine,
#: the log line and the run record all name the same two remedies — the
#: alternative was an obscure auth failure inside the `claude` subprocess,
#: several layers below anyone who could act on it.
NO_AUTH_MESSAGE = (
    "Claude Code engine selected but no credential is configured for this "
    "workspace. Fix it either way: connect a Claude subscription on /claude, "
    "or add an Anthropic API key on LLM Setup (provider 'anthropic')."
)

_PROMPT = """You are performing a professional pull-request review.

Repository: {repo}
PR #{number}: {title}
Base: {base_ref}

{context_block}

## Diff
```diff
{diff}
```

Explore the codebase with the mcp__celmis__* tools when you need context
(symbol search, cross-repo consumers, architecture). Focus on real problems:
bugs, security issues, breaking changes for callers, missing error handling.
Skip style nitpicks unless they hide a bug.

When done, output ONLY a JSON object in a ```json code block:

```json
{{
  "summary": "2-5 sentence overall assessment (markdown ok)",
  "findings": [
    {{
      "file": "path/in/new/tree.py",
      "line": 42,
      "severity": "info|warning|error|critical",
      "title": "one-line issue",
      "body": "explanation with reasoning (markdown)",
      "suggestion": "optional replacement code or null"
    }}
  ]
}}
```

Lines must be 1-indexed positions in the NEW file that appear in the diff.
An empty findings array is a valid answer for a clean PR."""


@dataclass
class ClaudeReviewResult:
    findings: list[Finding]
    summary: str
    turns: int = 0
    cost_usd: float | None = None
    #: Which credential paid, in the run-record vocabulary above. Defaults to
    #: "unknown" — the same "we did not measure it" value ReviewBatch starts
    #: from — because a run that refused before choosing a credential has no
    #: honest answer, and guessing "subscription" there is exactly the lie
    #: this field exists to stop.
    #:
    #: Read it as a qualifier on `cost_usd`: on the subscription path the SDK
    #: still reports an API-equivalent price, so the dollar figure is what the
    #: run WOULD have cost — real tokens, no marginal charge. On the API-key
    #: path the same figure is money that left the account.
    #:
    #: The caller must copy this onto the run record it renders —
    #: `batch.cost_source` in src/review/orchestrator.py, which is what
    #: reviews.py persists and the API returns. Assigning a flat engine name
    #: there instead throws the answer away and the fallback goes silent
    #: again.
    cost_source: str = "unknown"
    error: str | None = None


@dataclass(frozen=True)
class ClaudeAuth:
    """The credential a headless Claude Code run will use, and what it costs.

    `source` is "personal" or "workspace" for a connected subscription (which
    of the two slots in src/agent/connection.py answered), and "api_key" for
    the Anthropic-key fallback.
    """

    env: dict[str, str]
    source: str

    @property
    def api_key_auth(self) -> bool:
        return self.source == "api_key"

    @property
    def run_cost_source(self) -> str:
        """For the run record — see ClaudeReviewResult.cost_source."""
        return RUN_COST_SOURCE_API_KEY if self.api_key_auth else RUN_COST_SOURCE_SUBSCRIPTION

    @property
    def ledger_cost_source(self) -> str:
        """For llm_spend.cost_source."""
        return (
            LEDGER_COST_SOURCE_API_KEY if self.api_key_auth
            else LEDGER_COST_SOURCE_SUBSCRIPTION
        )


def run_claude_review(
    pr: PullRequest, *, user_id: str, workspace_id: str,
    custom_rules: str = "", graph_summary: str = "",
    cross_repo_drift: str = "",
) -> ClaudeReviewResult:
    """Blocking wrapper — the orchestrator is sync; the SDK is async.

    `cross_repo_drift` is passed for the same reason `graph_summary` is: it is
    context this engine cannot produce for itself. It is a deterministic grep
    across the siblings in a repository's group, it costs no model call, and
    without it this engine reviews a change to a shared constant with no way
    to know the constant is shared.
    """
    try:
        return asyncio.run(_run(pr, user_id=user_id, workspace_id=workspace_id,
                                custom_rules=custom_rules, graph_summary=graph_summary,
                                cross_repo_drift=cross_repo_drift))
    except RuntimeError as exc:
        if "asyncio.run() cannot be called" in str(exc):
            # Called from a thread with a running loop — run in a fresh thread.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(
                    asyncio.run,
                    _run(pr, user_id=user_id, workspace_id=workspace_id,
                         custom_rules=custom_rules, graph_summary=graph_summary,
                         cross_repo_drift=cross_repo_drift),
                ).result()
        raise


def record_claude_code_spend(
    message, *, surface: str, workspace_id: str, user_id: str,
    repo: str | None = None, api_key_auth: bool = False,
) -> None:
    """Ledger row for any headless Claude Code run (review, deps report, …)
    from its SDK ``ResultMessage``. Never raises.

    Two auth modes, two truths about money: on a connected subscription the
    tokens are real but nothing is charged per token (cost 0, source
    ``subscription``); on the Anthropic-API-key fallback the SDK's
    ``total_cost_usd`` is an actual charge.
    """
    try:
        from src.agent.runner import _sdk_usage
        from src.llm.budget import record_spend

        tokens_in, tokens_out, cached = _sdk_usage(message)
        try:
            total_cost = float(getattr(message, "total_cost_usd", None) or 0.0)
        except (TypeError, ValueError):
            total_cost = 0.0
        # No usage breakdown. On the subscription path there is genuinely
        # nothing to book — the row would be all zeros. On the API-key path
        # the charge is real whether or not the SDK broke the tokens out, and
        # dropping it makes the Usage page understate money that actually left
        # the account, which is worse than showing nothing at all.
        if not (tokens_in or tokens_out) and not (api_key_auth and total_cost > 0):
            logger.info(
                "claude_code_spend_no_usage surface=%s api_key_auth=%s",
                surface, api_key_auth,
            )
            return
        record_spend(
            workspace_id=workspace_id or "default",
            surface=surface,
            agent="claude_code",
            model=str(getattr(message, "model", "") or "claude-code"),
            provider="anthropic",
            cost_usd=total_cost if api_key_auth else 0.0,
            cost_source=(
                LEDGER_COST_SOURCE_API_KEY if api_key_auth
                else LEDGER_COST_SOURCE_SUBSCRIPTION
            ),
            tokens_in=tokens_in, tokens_out=tokens_out, cached_tokens_in=cached,
            user_id=user_id, repo_slug=repo,
        )
    except Exception as exc:  # noqa: BLE001 — ledger must never break a run
        logger.warning("claude_code_spend_record_failed surface=%s err=%s", surface, exc)


def resolve_auth(
    user_id: str, workspace_id: str, *, surface: str = "review",
) -> ClaudeAuth | None:
    """Auth for the CLI: subscription token first, workspace Anthropic API key
    as fallback. None → neither is configured.

    The fallback is a sensible default and stays. What was wrong is that it
    was silent: the engine is labelled "Claude Code (subscription)" in the UI,
    so an operator reasonably believes a run costs nothing beyond their
    subscription while it is in fact spending the workspace's Anthropic
    credit, per token. Same failure class as a Gemini key that never reached
    Gemini — it looks configured and it is quietly doing something else.

    So: one warning per run, naming the consequence rather than the fact.
    Called exactly once per run (never per SDK message), which is what keeps
    the line from repeating.
    """
    from src.agent.connection import resolve_connection

    conn = resolve_connection(user_id, workspace_id)
    if conn is not None:
        # `conn.env` rather than a hand-written variable: a workspace slot may
        # now hold an API key, and the CLI does not treat the two variables as
        # interchangeable.
        return ClaudeAuth(env=dict(conn.env), source=conn.source)

    try:
        from src.llm.keys import resolve_api_key
        key = resolve_api_key("anthropic", workspace_id=workspace_id)
    except Exception as exc:  # noqa: BLE001
        # Only the exception TYPE is logged: this branch catches anything the
        # key resolver can raise, and not every one of those guarantees a
        # secret-free message the way LLMCredentialError does.
        logger.error(
            "claude_code_no_credential ws=%s surface=%s cause=%s — %s",
            workspace_id, surface, type(exc).__name__, NO_AUTH_MESSAGE,
        )
        return None

    logger.warning(
        "claude_code_api_key_fallback ws=%s surface=%s — no Claude connection "
        "for this workspace, so this %s is billed to the workspace's Anthropic "
        "API key, per token, not to anyone's subscription. Connect a Claude "
        "account on /claude to run it on the subscription instead.",
        workspace_id, surface, surface,
    )
    return ClaudeAuth(env={"ANTHROPIC_API_KEY": key}, source="api_key")


def _resolve_env(user_id: str, workspace_id: str) -> dict | None:
    """Back-compat shim returning the env mapping alone.

    src/api/routers/deps.py and src/deps/report.py build their own
    ClaudeAgentOptions from this and test ``"ANTHROPIC_API_KEY" in auth_env``
    to bill themselves. New code wants `resolve_auth`, which additionally says
    what the run costs and who to tell.
    """
    auth = resolve_auth(user_id, workspace_id, surface="report")
    return dict(auth.env) if auth is not None else None


async def _run(
    pr: PullRequest, *, user_id: str, workspace_id: str,
    custom_rules: str, graph_summary: str, cross_repo_drift: str = "",
) -> ClaudeReviewResult:
    import os
    import tempfile

    auth = resolve_auth(user_id, workspace_id, surface="review")
    if auth is None:
        # Refused here, before the SDK is even imported, so the operator gets
        # both remedies instead of an auth error thrown by the `claude`
        # subprocess several layers below anyone who can act on it.
        return ClaudeReviewResult(findings=[], summary="", error=NO_AUTH_MESSAGE)
    auth_env = auth.env

    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
    )

    from src.agent.runner import _mint_mcp_token  # scoped, never-empty scopes

    context_parts = []
    if graph_summary:
        context_parts.append(f"## Impact graph\n{graph_summary[:4000]}")
    # BEFORE the repo rules and after the graph, because it is a finding
    # rather than a policy: the drift block names sibling repositories that
    # still carry the old value of something this diff changed.
    if cross_repo_drift:
        context_parts.append(cross_repo_drift[:6000])
    if custom_rules:
        context_parts.append(f"## Repo rules\n{custom_rules[:4000]}")
    from src.review.agents.base import _review_language_instruction
    lang_extra = _review_language_instruction(workspace_id)
    prompt = _PROMPT.format(
        repo=pr.repo, number=pr.number, title=pr.title,
        base_ref=pr.base_ref or "?",
        context_block="\n\n".join(context_parts),
        diff=(pr.raw_diff or "")[:_MAX_DIFF_CHARS],
    ) + lang_extra

    api_port = os.environ.get("PORT", "8000")
    with tempfile.TemporaryDirectory(prefix="claude-review-") as home:
        options = ClaudeAgentOptions(
            cwd=home,
            env={
                "HOME": home,
                "CLAUDE_CONFIG_DIR": f"{home}/.claude",
                "DISABLE_TELEMETRY": "1",
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                **auth_env,
            },
            allowed_tools=["mcp__celmis__*", "mcp__celmis"],
            # Agent/Task default to a BACKGROUND subagent whose result arrives
            # in a later turn. This loop stops at the first ResultMessage and
            # then tears down the client, so that turn is never read and the
            # review would end after the "let me look into it" line. See the
            # longer note in src/agent/runner.py next to _DISALLOWED_TOOLS.
            disallowed_tools=["Bash", "Read", "Write", "Edit", "Grep", "Glob",
                              "WebFetch", "WebSearch", "Agent", "Task"],
            permission_mode="acceptEdits",
            max_turns=_MAX_TURNS,
            mcp_servers={
                "celmis": {
                    "type": "http",
                    # Trailing slash on purpose: without it Starlette answers 307,
                    # and a redirected POST is not something the MCP
                    # streamable-HTTP client is guaranteed to follow.
                    "url": f"http://localhost:{api_port}/mcp/",
                    "headers": {"Authorization": f"Bearer {_mint_mcp_token(user_id)}"},
                },
            },
        )
        text_parts: list[str] = []
        turns = 0
        cost = None
        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                text_parts.append(block.text)
                    elif isinstance(message, ResultMessage):
                        turns = message.num_turns
                        cost = message.total_cost_usd
                        from src.llm.budget import SURFACE_REVIEW
                        await asyncio.to_thread(
                            record_claude_code_spend, message,
                            surface=SURFACE_REVIEW, workspace_id=workspace_id,
                            user_id=user_id, repo=pr.repo,
                            api_key_auth=auth.api_key_auth,
                        )
                        if message.is_error:
                            return ClaudeReviewResult(
                                findings=[], summary="", turns=turns, cost_usd=cost,
                                cost_source=auth.run_cost_source,
                                error=f"Claude Code run errored: {(message.result or '')[:300]}",
                            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("claude_review_failed pr=%d", pr.number)
            return ClaudeReviewResult(
                findings=[], summary="", cost_source=auth.run_cost_source,
                error=str(exc)[:500],
            )

    parsed = _parse_json_block("\n".join(text_parts))
    if parsed is None:
        return ClaudeReviewResult(
            findings=[], summary="", turns=turns, cost_usd=cost,
            cost_source=auth.run_cost_source,
            error="Claude Code returned no parseable JSON review block.",
        )

    findings = []
    for f in parsed.get("findings") or []:
        try:
            sev = str(f.get("severity", "warning")).lower()
            findings.append(Finding(
                file_path=str(f["file"]),
                line=max(1, int(f.get("line", 1))),
                side=HunkSide.RIGHT,
                severity=FindingSeverity(sev if sev in _SEVERITIES else "warning"),
                title=str(f.get("title", ""))[:200],
                body=str(f.get("body", ""))[:4000],
                suggestion=(str(f["suggestion"]) if f.get("suggestion") else None),
                agent="claude_code",
                rule_id="claude_code.finding",
                confidence=0.8,
            ))
        except Exception:  # noqa: BLE001
            continue
    return ClaudeReviewResult(
        findings=findings,
        summary=str(parsed.get("summary", ""))[:6000],
        turns=turns, cost_usd=cost, cost_source=auth.run_cost_source,
    )


def _parse_json_block(text: str) -> dict | None:
    """Last ```json block wins; fall back to the widest braces span."""
    blocks = re.findall(r"```json\s*(.*?)```", text, re.DOTALL)
    candidates = list(reversed(blocks))
    if not candidates:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            candidates = [text[start:end + 1]]
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


__all__ = [
    "LEDGER_COST_SOURCE_API_KEY",
    "LEDGER_COST_SOURCE_SUBSCRIPTION",
    "NO_AUTH_MESSAGE",
    "RUN_COST_SOURCE_API_KEY",
    "RUN_COST_SOURCE_SUBSCRIPTION",
    "ClaudeAuth",
    "ClaudeReviewResult",
    "record_claude_code_spend",
    "resolve_auth",
    "run_claude_review",
]
