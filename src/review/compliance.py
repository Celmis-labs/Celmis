"""Compliance agent — runs after main review, one LLM call per matching
rule, hard-blocks APPROVE if any blocking rule fails.

This is a first-class policy object (see ComplianceCheck model), NOT part
of the free-form `prompt_template`. Reasons:

  * Failure is machine-readable — the verdict layer needs to know which
    checks tripped, not parse markdown prose.
  * Auditability — every check has a stable ID + created_by + timestamp;
    compliance teams can grep audit logs by ID.
  * Scoping — checks can be workspace-wide OR repo-specific without
    duplicating rules across every policy row.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from dataclasses import dataclass

from src.review.agents.base import (
    AgentContext,
    AgentRunResult,
    _llm_timeout,
    agent_llm_settings,
)
from src.review.models import Finding, FindingSeverity, HunkSide
from src.review.settings import AgentLLMSettings

logger = logging.getLogger(__name__)


@dataclass
class ComplianceCheckSpec:
    id: str
    name: str
    scope: str          # 'workspace' | 'repo:<slug>'
    glob_pattern: str
    rule: str
    severity: str
    blocking: bool


_SYSTEM = """You are a compliance auditor. You are given ONE rule and \
a PR diff. Answer with strict JSON: {"passes": bool, "reason": "..."}.\n\
- passes=true if the diff satisfies the rule OR the rule does not apply.\n\
- passes=false only if the diff clearly violates the rule.\n\
- reason: one sentence, cite the specific file:line if applicable.\n\
Do not add prose outside the JSON.
"""


def load_active_checks(repo_slug: str) -> list[ComplianceCheckSpec]:
    """Read enabled compliance checks whose scope covers this repo."""
    try:
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session

        from src.db.models import ComplianceCheck
        from src.db.session import get_database_url

        sync_url = get_database_url().replace(
            "postgresql+asyncpg://", "postgresql+psycopg://"
        )
        engine = create_engine(sync_url, pool_pre_ping=True)
        try:
            with Session(engine) as s:
                rows = s.execute(
                    select(ComplianceCheck).where(ComplianceCheck.enabled.is_(True))
                ).scalars().all()
                specs: list[ComplianceCheckSpec] = []
                for r in rows:
                    if r.scope != "workspace" and r.scope != f"repo:{repo_slug}":
                        continue
                    specs.append(ComplianceCheckSpec(
                        id=r.id, name=r.name, scope=r.scope,
                        glob_pattern=r.glob_pattern, rule=r.rule,
                        severity=r.severity, blocking=bool(r.blocking),
                    ))
                return specs
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001
        logger.warning("compliance_load_failed repo=%s err=%s", repo_slug, exc)
        return []


def run_compliance(context: AgentContext) -> AgentRunResult:
    """Evaluate every active check that matches the changed files. One LLM
    call per check. Returns findings tagged agent="compliance"; the
    verdict layer treats blocking failures as a hard REJECT.
    """
    import time
    t0 = time.time()
    pr = context.pull_request
    specs = load_active_checks(pr.repo)
    if not specs:
        return AgentRunResult(agent="compliance", elapsed_seconds=0.0)

    findings: list[Finding] = []
    total_in = total_out = 0
    # Resolved once, not once per rule: compliance is one LLM call per matching
    # rule, and the fallback arm of `agent_llm_settings` reads the workspace
    # config out of the credentials store. Anything this loop multiplies, it
    # multiplies by the rule count — the same arithmetic that made an inherited
    # retry budget expensive here.
    llm = agent_llm_settings(context, "compliance")
    for spec in specs:
        matching = [
            f for f in pr.changed_files
            if fnmatch.fnmatch(f, spec.glob_pattern)
        ] if spec.glob_pattern and spec.glob_pattern != "**" else pr.changed_files
        if not matching and spec.glob_pattern not in ("", "**"):
            continue
        verdict = _evaluate(context, spec, matching_files=matching, llm=llm)
        if verdict is None:
            continue
        total_in += verdict["tokens_in"]
        total_out += verdict["tokens_out"]
        if verdict["passes"]:
            continue
        sev = (
            FindingSeverity.CRITICAL if spec.blocking
            else FindingSeverity.WARNING
        )
        first_file = matching[0] if matching else (pr.changed_files[0] if pr.changed_files else "")
        findings.append(Finding(
            file_path=first_file,
            line=1,
            side=HunkSide.RIGHT,
            severity=sev,
            title=f"Compliance: {spec.name}",
            body=(
                f"**Rule** ({spec.scope}, {spec.severity}"
                f"{', BLOCKING' if spec.blocking else ''}):\n"
                f"{spec.rule}\n\n"
                f"**Reason:** {verdict['reason']}"
            ),
            agent="compliance",
            rule_id=f"compliance.{spec.id}",
            confidence=0.9,
        ))
    return AgentRunResult(
        agent="compliance",
        findings=findings,
        tokens_in=total_in,
        tokens_out=total_out,
        elapsed_seconds=time.time() - t0,
    )


def _evaluate(
    context: AgentContext,
    spec: ComplianceCheckSpec,
    *,
    matching_files: list[str],
    llm: AgentLLMSettings | None = None,
) -> dict | None:
    """Run one LLM call for this rule; parse strict-JSON verdict.
    Returns None on total failure (network/parse); the caller treats
    that as "unknown" (does not block).
    """
    if context.llm_client is None:
        return None
    pr = context.pull_request
    prompt = (
        f"Rule to enforce: {spec.rule}\n\n"
        f"PR title: {pr.title}\n"
        f"Files changed matching rule pattern `{spec.glob_pattern}`:\n"
        + "\n".join(f"  - {f}" for f in matching_files[:20])
        + "\n\nDiff (unified):\n"
        + (pr.raw_diff or "(diff unavailable)")[:8000]
    )
    # 256 was written here as a literal, and it is a deliberate number: the
    # whole reply is {"passes": bool, "reason": "one sentence"}. It is also the
    # tightest budget in the review, and a reasoning model spends output tokens
    # thinking before it writes the first brace — 256 is precisely the shape of
    # the ceiling that broke the architect agent, one order of magnitude
    # smaller. So it is the FLOOR now (ReviewSettings.compliance_max_output_
    # tokens) and a per-agent setting can lift it without editing this file.
    llm = llm or agent_llm_settings(context, "compliance")
    try:
        response = context.llm_client.generate(
            prompt=prompt,
            agent="compliance",
            system_instruction=_SYSTEM,
            operation="compliance_check",
            repo=pr.repo,
            temperature=0.0,
            max_output_tokens=llm.max_output_tokens,
            reasoning=llm.reasoning,
            # Stated, for the same reason as the retry budget below: the
            # inherited 120s was a deadline no operator could reach. One call
            # per matching rule, so an inherited-and-wrong bound here is paid
            # once per rule.
            timeout=_llm_timeout(),
            # Stated zero, not the inherited default of 3 — see
            # LLMReviewAgent._LLM_NUM_RETRIES in agents/base.py for why the
            # retry decision must not run a layer below the classification.
            # Compliance is one call per matching rule, so an inherited retry
            # budget here multiplied by the rule count.
            num_retries=0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("compliance_llm_failed rule=%s err=%s", spec.id, exc)
        return None
    text = (response.text or "").strip()
    # Best-effort JSON extraction — models sometimes wrap in ```json.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return {
        "passes": bool(payload.get("passes")),
        "reason": str(payload.get("reason", "")),
        "tokens_in": response.input_tokens,
        "tokens_out": response.output_tokens,
    }


__all__ = ["run_compliance", "load_active_checks", "ComplianceCheckSpec"]
