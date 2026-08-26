"""AI report over dependency-audit findings — shared by the API endpoint and
the post-audit auto-report in the job handler.

The audit facts are deterministic (registries + OSV); the chosen engine
(workspace review-profile model, or Claude Code) only ANALYSES them:
what to update first, risk, rollout order.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def build_prompt_sync(run_id: str) -> str | None:
    """Compose the report prompt from stored findings (sync engine)."""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from src.db.models import DepAuditRun, DepFinding
    from src.db.session import get_database_url

    engine = create_engine(get_database_url().replace(
        "postgresql+asyncpg://", "postgresql+psycopg://"), pool_pre_ping=True)
    try:
        with Session(engine) as s:
            run = s.get(DepAuditRun, run_id)
            if run is None:
                return None
            rows = list(s.scalars(
                select(DepFinding).where(DepFinding.run_id == run_id)))
            summary = dict(run.summary or {})
    finally:
        engine.dispose()

    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4}
    rows = sorted(rows, key=lambda r: (sev_rank.get(r.severity, 5), r.outdated == "none"))
    interesting = [r for r in rows if r.severity != "none" or r.outdated != "none"][:120]
    lines = []
    for r in interesting:
        vulns = list(r.vulns or [])
        vuln_bits = "; ".join(
            f"{v.get('cve') or v.get('id')}({v.get('severity')}"
            + (f", fixed in {v['fixed_in']}" if v.get("fixed_in") else ", no fix yet")
            # Provenance matters to the reader: a pip-audit/npm-audit hit came
            # from the resolved tree, an OSV hit from a declared version.
            + f", via {v.get('source') or 'osv'})"
            for v in vulns[:3]
        )
        transitive = any(v.get("transitive") for v in vulns)
        lines.append(
            f"{r.repo_slug} | {r.ecosystem} | {r.package} {r.current_version}"
            f" -> {r.latest_version or '?'} | drift={r.outdated}"
            f" | sev={r.severity}" + (" | TRANSITIVE" if transitive else "")
            + (f" | {vuln_bits}" if vuln_bits else "")
        )

    # Coverage gaps and hygiene are separate sections on purpose: the model
    # must not fold "we never checked Go" into a vulnerability count, and must
    # not present a postinstall script as a CVE.
    gaps = summary.get("not_checked") or []
    gap_lines = [
        f"- {g.get('ecosystem')} in {g.get('repo') or '?'}"
        + (f"/{g.get('subproject')}" if g.get("subproject") else "")
        + f" — {g.get('tool')} {g.get('status')}: {g.get('reason') or 'no reason recorded'}"
        for g in gaps[:25]
    ]
    hygiene = summary.get("hygiene") or {}
    hyg_lines = [
        f"- [{h.get('kind')}/{h.get('severity')}] {h.get('repo') or '?'} "
        f"{h.get('package')}: {h.get('detail')}"
        for h in (hygiene.get("items") or [])[:30]
    ]

    parts = [
        "You are preparing a dependency-audit executive report for an engineering team.\n",
        f"Workspace summary: {summary.get('repos_scanned')} repos scanned, "
        f"{summary.get('packages')} packages, {summary.get('outdated')} outdated, "
        f"{summary.get('vulnerable')} vulnerable ({summary.get('by_severity')}).\n",
        f"Vulnerability sources: {summary.get('sources') or {}}. "
        f"Transitive packages surfaced: {summary.get('transitive') or 0} "
        f"({summary.get('transitive_vulnerable') or 0} vulnerable).\n",
        f"Version drift, counted independently of vulnerabilities: "
        f"{summary.get('drift') or {}}.\n\n",
        "Findings (repo | ecosystem | package current -> latest | drift | severity "
        "| TRANSITIVE | CVEs):\n",
        "\n".join(lines),
    ]
    if gap_lines:
        parts.append(
            "\n\nNOT AUDITED — no native tool and/or no lock file. These are "
            "UNKNOWNS, not clean results, and the report must say so:\n"
            + "\n".join(gap_lines)
        )
    if hyg_lines:
        parts.append(
            "\n\nSupply-chain hygiene (NOT vulnerabilities — do not mix these "
            "into CVE counts):\n" + "\n".join(hyg_lines)
        )
    parts.append(
        "\n\nWrite a concise markdown report: 1) headline risk assessment, "
        "2) update-now list (vulnerable, fix available) with exact target versions "
        "— call out transitive ones, since those are fixed by updating the parent, "
        "3) safe updates worth batching, 4) major upgrades to plan separately with "
        "expected breaking-change risk, 5) supply-chain hygiene items worth a human "
        "decision, 6) an explicit 'what we could not check' section, "
        "7) suggested per-repo rollout order. "
        "Be specific with package names and versions; no generic advice."
    )
    return "".join(parts)


def run_api_report(prompt: str, workspace_id: str, temperature: float = 0.2) -> str:
    from src.llm.profiles import resolve_profile

    p = resolve_profile("review", workspace_id)
    if not p.api_key:
        raise RuntimeError("No LLM key for the review profile — add one on LLM Setup.")
    # LiteLLM for every provider, Google included — see src/llm/completion.py
    # for why the direct google-genai branch is gone.
    import litellm
    resp = litellm.completion(
        model=p.litellm_model, api_key=p.api_key,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature, max_tokens=4096, timeout=120,
    )
    # Spend ledger (surface=deps).
    from src.llm.completion import record_completion_spend
    record_completion_spend(
        p, resp, operation="deps_report", workspace_id=workspace_id,
    )
    return resp.choices[0].message.content or ""


async def run_claude_report(prompt: str, user_id: str, workspace_id: str) -> str:
    import tempfile

    from src.review.claude_engine import _resolve_env

    auth_env = _resolve_env(user_id, workspace_id)
    if auth_env is None:
        raise RuntimeError(
            "Claude Code engine needs a connected Claude account (/claude) "
            "or an Anthropic key on LLM Setup."
        )
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        TextBlock,
    )
    with tempfile.TemporaryDirectory(prefix="deps-report-") as home:
        options = ClaudeAgentOptions(
            cwd=home,
            env={"HOME": home, "CLAUDE_CONFIG_DIR": f"{home}/.claude",
                 "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1",
                 **auth_env},
            allowed_tools=[],
            disallowed_tools=["Bash", "Read", "Write", "Edit", "Grep", "Glob",
                              "WebFetch", "WebSearch"],
            max_turns=1,
        )
        parts: list[str] = []
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
        return "\n".join(parts).strip() or "(empty report)"


def save_report_sync(run_id: str, report: str, engine_name: str) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from src.db.models import DepAuditRun
    from src.db.session import get_database_url

    engine = create_engine(get_database_url().replace(
        "postgresql+asyncpg://", "postgresql+psycopg://"), pool_pre_ping=True)
    try:
        with Session(engine) as s:
            run = s.get(DepAuditRun, run_id)
            if run is None:
                return
            summary = dict(run.summary or {})
            summary["ai_report"] = report
            summary["ai_report_engine"] = engine_name
            run.summary = summary
            s.commit()
    finally:
        engine.dispose()


__all__ = ["build_prompt_sync", "run_api_report", "run_claude_report", "save_report_sync"]
