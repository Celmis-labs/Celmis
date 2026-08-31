"""Dependency audit API.

    POST /api/deps/audit             — queue a new audit for the active workspace
    GET  /api/deps/latest            — latest run (any status) + its summary
    POST /api/deps/{run_id}/cancel   — stop a queued/running run
    GET  /api/deps/{run_id}/findings — finding rows (filterable)
    GET  /api/deps/{run_id}/export   — the whole run as one .md/.docx document

A run must never be able to strand the page: a workspace can only have one
live run, so if that run stops reporting progress (dead worker, deleted queue
job) the user would be locked out of the feature forever. Hence `cancel` and
`force` — both close the live run and free the queue's dedup slot.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import current_workspace_id, get_current_user
from src.db.models import DepAuditRun, DepFinding
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/deps", tags=["deps"])


class RunOut(BaseModel):
    id: str
    status: str
    summary: dict
    error: str | None
    created_at: str
    updated_at: str


class FindingOut(BaseModel):
    id: str
    repo_slug: str
    ecosystem: str
    package: str
    current_version: str
    latest_version: str | None
    outdated: str
    is_dev: bool
    vulns: list
    severity: str
    recommendation: str
    #: Whether the repository's own code names this package, and where.
    #: `None` on a row written before the scan existed — which is not the same
    #: as "not imported", and the UI must not render it as one.
    #:
    #: NOT reachability: it reports import positions, not whether a vulnerable
    #: function is called. See src/deps/imports.py for what reachability would
    #: require and why this installation cannot answer it.
    named_in_code: dict | None = None


def _run_out(r: DepAuditRun) -> RunOut:
    return RunOut(
        id=r.id, status=r.status, summary=dict(r.summary or {}), error=r.error,
        created_at=r.created_at.isoformat() if r.created_at else "",
        updated_at=r.updated_at.isoformat() if r.updated_at else "",
    )


class StartAuditIn(BaseModel):
    # Optional scoping: explicit repo slugs, and/or an owner filter matching
    # the repo full_name prefix ("owner/…") — e.g. audit every repo of one
    # Bitbucket user without ticking each box.
    repo_slugs: list[str] | None = None
    owner: str | None = None
    # Read every repo at THIS branch for this run only, instead of the branch
    # saved on each registration. Deliberately not persisted: a sticky
    # override is an invisible scope, and "why is this still vulnerable" is
    # already hard enough to answer.
    branch: str | None = Field(default=None, max_length=200)
    # Auto-generate the AI report when the audit finishes.
    report_engine: str = Field(default="none", pattern="^(none|api|claude_code)$")
    # Restart even when a run still *looks* live. The UI offers this only once
    # a run has stopped reporting progress; without it a job whose worker died
    # (but whose queue row survives) blocks every future audit.
    force: bool = False


async def _live_queue_job(
    session: AsyncSession, workspace_id: str,
) -> tuple[str, str] | None:
    """(job_id, status) of the pending/running queue job backing this
    workspace's audit, or None when nothing is queued."""
    from sqlalchemy import text as _text

    row = (await session.execute(_text(
        "SELECT id, status FROM sync_jobs WHERE dedup_key = :dk "
        "AND status IN ('pending','running') ORDER BY created_at DESC LIMIT 1"
    ), {"dk": f"deps_audit:{workspace_id}"})).first()
    return (str(row[0]), str(row[1])) if row is not None else None


@router.post("/audit", response_model=RunOut, status_code=202)
async def start_audit(
    payload: StartAuditIn | None = None,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> RunOut:
    # One implementation, in src/automation/actions.py. This route had its own
    # copy and the two had already diverged: the 50-repository cap and the
    # "is this repo even in your workspace" check existed only in the action,
    # so an audit an agent was refused through MCP went through untouched from
    # the web. Every surface now inherits the same invariants — including the
    # audit-log entry, which is what the evidence pack is assembled from, and
    # which is the next thing that would have grown on one side only.
    from src.automation.actions import ActionError, Actor, start_dep_audit

    actor = Actor(user_id=user.id, email=user.email,
                  workspace_id=workspace_id, label="web")
    try:
        queued = await start_dep_audit(
            actor, session,
            repo_slugs=(payload.repo_slugs if payload else None),
            owner=(payload.owner if payload else None),
            branch=(payload.branch if payload else None),
            report_engine=(payload.report_engine if payload else "none"),
            force=bool(payload.force) if payload else False,
        )
    except ActionError as exc:
        # The action refuses with a sentence a person can act on; a 409 keeps
        # that sentence rather than turning it into a stack trace.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    run = await session.get(DepAuditRun, queued["run_id"])
    # This log sat AFTER the return and never fired — and referenced a
    # `job_id` that does not exist in this scope, which is why the linter
    # found it. Moved above the return, and reading the id from the queue
    # result, which is where it actually is.
    logger.info("deps_audit_queued run=%s job=%s ws=%s by=%s",
                run.id, queued.get("job_id"), workspace_id, user.email)
    return _run_out(run)


@router.get("/latest", response_model=RunOut | None)
async def latest_run(
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> RunOut | None:
    run = (await session.scalars(
        select(DepAuditRun)
        .where(DepAuditRun.workspace_id == workspace_id)
        .order_by(DepAuditRun.created_at.desc())
        .limit(1)
    )).first()
    return _run_out(run) if run else None


@router.post("/{run_id}/cancel", response_model=RunOut)
async def cancel_run(
    run_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> RunOut:
    """Stop a queued/running audit.

    The run row is flipped to `error` immediately — that is what the UI reads,
    and the auditor re-checks it at every phase checkpoint, so a live worker
    stops on its own even if the queue-level cancel flag is lost. Cancelling
    an already-finished run is a no-op (idempotent for double clicks).
    """
    run = await session.get(DepAuditRun, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status not in ("queued", "running"):
        return _run_out(run)

    job = await _live_queue_job(session, workspace_id)
    if job is not None:
        from src.sync.queue import mark_cancelled, request_cancel
        job_id, job_status = job
        if job_status == "running":
            # Cooperative — the handler notices at its next checkpoint.
            request_cancel(job_id)
        else:
            # Never started: drop it outright so it can't wake up later.
            mark_cancelled(job_id, "cancelled by user")

    run.status = "error"
    run.error = "cancelled by user"
    await session.commit()
    await session.refresh(run)
    logger.info("deps_audit_cancelled run=%s ws=%s by=%s", run_id, workspace_id, user.email)
    return _run_out(run)


class ReportIn(BaseModel):
    engine: str = Field(default="api", pattern="^(api|claude_code)$")
    # Model params (api engine; claude_code manages its own settings)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)


class ReportOut(BaseModel):
    report: str
    engine: str


@router.post("/{run_id}/report", response_model=ReportOut)
async def generate_report(
    run_id: str,
    payload: ReportIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> ReportOut:
    """Executive AI report over the (deterministic) audit findings: what to
    update first, risk assessment, suggested rollout order. Facts come from
    the audit; the model only analyses them."""
    run = await session.get(DepAuditRun, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "done":
        raise HTTPException(status_code=400, detail="Audit not finished yet")

    rows = (await session.scalars(
        select(DepFinding).where(DepFinding.run_id == run_id)
    )).all()
    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4}
    rows = sorted(rows, key=lambda r: (sev_rank.get(r.severity, 5), r.outdated == "none"))
    interesting = [r for r in rows if r.severity != "none" or r.outdated != "none"][:120]

    lines = []
    for r in interesting:
        vuln_bits = "; ".join(
            f"{v.get('cve') or v.get('id')}({v.get('severity')}"
            + (f", fixed in {v['fixed_in']}" if v.get("fixed_in") else ", no fix yet") + ")"
            for v in (r.vulns or [])[:3]
        )
        lines.append(
            f"{r.repo_slug} | {r.ecosystem} | {r.package} {r.current_version}"
            f" -> {r.latest_version or '?'} | drift={r.outdated}"
            f" | sev={r.severity}" + (f" | {vuln_bits}" if vuln_bits else "")
        )
    summary = dict(run.summary or {})
    prompt = (
        "You are preparing a dependency-audit executive report for an engineering team.\n"
        f"Workspace summary: {summary.get('repos_scanned')} repos scanned, "
        f"{summary.get('packages')} packages, {summary.get('outdated')} outdated, "
        f"{summary.get('vulnerable')} vulnerable ({summary.get('by_severity')}).\n\n"
        "Findings (repo | ecosystem | package current -> latest | drift | severity | CVEs):\n"
        + "\n".join(lines)
        + "\n\nWrite a concise markdown report: 1) headline risk assessment, "
        "2) update-now list (vulnerable, fix available) with exact target versions, "
        "3) safe updates worth batching, 4) major upgrades to plan separately with "
        "expected breaking-change risk, 5) suggested per-repo rollout order. "
        "Be specific with package names and versions; no generic advice."
    )

    if payload.engine == "claude_code":
        report = await _claude_report(prompt, user.id, workspace_id)
    else:
        report = await asyncio.to_thread(
            _api_report, prompt, workspace_id, payload.temperature,
        )

    summary["ai_report"] = report
    summary["ai_report_engine"] = payload.engine
    run.summary = summary
    await session.commit()
    return ReportOut(report=report, engine=payload.engine)


def _api_report(prompt: str, workspace_id: str, temperature: float) -> str:
    """Report via the workspace's review-profile provider (BYOK models)."""
    from src.llm.profiles import resolve_profile

    p = resolve_profile("review", workspace_id)
    if not p.api_key:
        raise HTTPException(
            status_code=400,
            detail="No LLM key for the review profile — add one on LLM Setup.",
        )
    # LiteLLM for every provider, Google included — see src/llm/completion.py
    # for why the direct google-genai branch is gone.
    import litellm
    resp = litellm.completion(
        model=p.litellm_model, api_key=p.api_key,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature, max_tokens=4096, timeout=120,
    )
    # Spend ledger (surface=deps) — the Google branch bills itself inside
    # GeminiClient, this one has to be recorded explicitly.
    from src.llm.completion import record_completion_spend
    record_completion_spend(
        p, resp, operation="deps_report", workspace_id=workspace_id,
    )
    return resp.choices[0].message.content or ""


async def _claude_report(prompt: str, user_id: str, workspace_id: str) -> str:
    """Report via Claude Code (subscription token / Anthropic key fallback)."""
    import tempfile

    from src.review.claude_engine import _resolve_env

    auth_env = _resolve_env(user_id, workspace_id)
    if auth_env is None:
        raise HTTPException(
            status_code=400,
            detail="Claude Code engine needs a connected Claude account (/claude) "
                   "or an Anthropic key on LLM Setup.",
        )
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
    )
    with tempfile.TemporaryDirectory(prefix="deps-report-") as home:
        options = ClaudeAgentOptions(
            cwd=home,
            env={"HOME": home, "CLAUDE_CONFIG_DIR": f"{home}/.claude",
                 "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1",
                 **auth_env},
            allowed_tools=[], disallowed_tools=["Bash", "Read", "Write", "Edit",
                                                "Grep", "Glob", "WebFetch", "WebSearch"],
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
                elif isinstance(message, ResultMessage):
                    # Spend ledger — Claude Code runs are otherwise invisible
                    # to the Usage view. Sync DB write → off the event loop.
                    from src.llm.budget import SURFACE_DEPS
                    from src.review.claude_engine import record_claude_code_spend
                    await asyncio.to_thread(
                        record_claude_code_spend, message,
                        surface=SURFACE_DEPS, workspace_id=workspace_id,
                        user_id=user_id,
                        api_key_auth="ANTHROPIC_API_KEY" in auth_env,
                    )
        return "\n".join(parts).strip() or "(empty report)"


@router.get("/{run_id}/findings", response_model=list[FindingOut])
async def findings(
    run_id: str,
    only: str | None = Query(default=None, pattern="^(vulnerable|outdated)$"),
    repo: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> list[FindingOut]:
    run = await session.get(DepAuditRun, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Run not found")
    q = select(DepFinding).where(DepFinding.run_id == run_id)
    if only == "vulnerable":
        q = q.where(DepFinding.severity != "none")
    elif only == "outdated":
        q = q.where(DepFinding.outdated != "none")
    if repo:
        q = q.where(DepFinding.repo_slug == repo)
    rows = (await session.scalars(q.limit(limit))).all()
    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4}
    rows = sorted(rows, key=lambda r: (sev_rank.get(r.severity, 5), r.repo_slug, r.package))
    return [FindingOut(
        id=r.id, repo_slug=r.repo_slug, ecosystem=r.ecosystem, package=r.package,
        current_version=r.current_version, latest_version=r.latest_version,
        outdated=r.outdated, is_dev=r.is_dev, vulns=list(r.vulns or []),
        severity=r.severity, recommendation=r.recommendation,
        named_in_code=r.named_in_code,
    ) for r in rows]


@router.get("/{run_id}/export")
async def export_run(
    run_id: str,
    format: str = Query(default="md", pattern="^(md|docx)$"),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> Response:
    """The whole run as one document: overview, coverage, AI report, findings.

    PDF is not generated here — the dependencies page prints to PDF from the
    browser, the same choice the documentation export made, and for the same
    reason: server-side PDF means weasyprint and its cairo/pango libraries.
    """
    from datetime import datetime

    from src.deps.document import PRODUCT, build_markdown

    run = await session.get(DepAuditRun, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Run not found")
    # An unfinished run has partial counts and no report — exporting it would
    # produce a document that reads as complete and is not.
    if run.status != "done":
        raise HTTPException(
            status_code=400,
            detail="Audit is not finished — export once the run completes.",
        )

    rows = (await session.scalars(
        select(DepFinding).where(DepFinding.run_id == run_id)
    )).all()
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    markdown = build_markdown(run, list(rows), generated_at=stamp)

    logger.info("deps_exported run=%s format=%s findings=%d by=%s",
                run_id, format, len(rows), user.email)
    name = f"{PRODUCT.lower()}-dependency-audit-{stamp}"
    if format == "docx":
        from src.docs.export import to_docx_document
        return Response(
            content=to_docx_document(markdown),
            media_type=("application/vnd.openxmlformats-officedocument"
                        ".wordprocessingml.document"),
            headers={"Content-Disposition": f'attachment; filename="{name}.docx"'},
        )
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.md"'},
    )


__all__ = ["router"]

@dataclass(frozen=True)
class _DepRow:
    """The shape `build_sbom` reads. A DepFinding carries more than the SBOM
    needs, and passing the ORM row directly would make the document depend on
    the database schema."""

    ecosystem: str
    package: str
    version: str
    is_dev: bool = False
    manifest: str = ""



def _audited_commits(run, repo_slugs) -> dict[str, str]:
    """slug → the commit the RUN read, falling back to the indexed sha.

    `_commits_for` below answers a different question — what the knowledge
    graph was last built from — and using it here defeated the whole point of
    stamping a commit. A run that audited a branch produced a document
    describing that branch under main's sha and main's serialNumber, so the
    post-fix SBOM (0 vulnerabilities) and the pre-fix one (38) were one
    document to any consumer that de-duplicates by serial. That is exactly the
    collision the commit was added to prevent.

    Runs recorded before `audited_commits` existed have none, and fall back to
    the old behaviour rather than losing the field entirely.
    """
    audited = (getattr(run, "summary", None) or {}).get("audited_commits") or {}
    out = {slug: sha for slug, sha in audited.items() if sha}
    missing = [s for s in repo_slugs if s not in out]
    if missing:
        out.update({k: v for k, v in _commits_for(missing).items() if v})
    return out


def _commits_for(repo_slugs) -> dict[str, str]:
    """slug → the commit the graph was last indexed at, or "".

    Why the SBOM needs it: `build_sbom` derives its `serialNumber` from
    (repo_slug, commit) precisely so that two exports of two different code
    states are two different documents. Nothing ever passed a commit, so every
    export for a repo carried a byte-identical serial and
    `metadata.component.version` read "unknown" — proven by taking two audits
    of the same repo and diffing: same urn:uuid, both times.

    A consumer that de-duplicates by serialNumber — standard CycloneDX
    practice, and the reason the field exists — would treat the SBOM published
    after a vulnerability was fixed as the one published before it. The
    function's own docstring says it exists to prevent exactly that.

    Failure is not fatal: an SBOM with an empty commit is what shipped for
    months, so a lookup that raises must degrade to that rather than refuse
    the export.
    """
    try:
        from src.repos.index_state import read_index_states
        states = read_index_states(list(repo_slugs))
    except Exception as exc:  # noqa: BLE001
        logger.warning("sbom_commit_lookup_failed err=%s", exc)
        return {}
    return {
        slug: (getattr(st, "last_indexed_sha", "") or "")
        for slug, st in (states or {}).items()
    }


@router.get("/{run_id}/evidence")
async def export_evidence(
    run_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> Response:
    """The audit run as an evidence pack: SBOMs, findings, timeline, hashes.

    A different artefact from `/export`, for a different reader. That one is a
    document somebody reads. This is what gets attached to a filing: one
    CycloneDX SBOM per repository, every finding, the history of runs, and a
    sha256 of each file.

    THE HASHES PROVE INTERNAL CONSISTENCY. They do not, by themselves, prove
    the pack is the one that left here: the manifest records no hash for
    itself, so somebody who edits a file and rewrites its entry passes the
    check. `X-Celmis-Manifest-SHA256` on this response is the value that
    closes it — publish it where the pack is not, and the recipient can check
    against something the sender did not control.

    The 24-hour reporting clock in the Cyber Resilience Act starts on the day
    something is exploited, and the question asked then is about the past —
    what did you know and when. A dashboard cannot answer it. This can.
    """
    from datetime import datetime

    from src.deps.evidence import build_evidence_pack, manifest_sha256
    from src.deps.sbom import build_sbom

    run = await session.get(DepAuditRun, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "done":
        # Same rule as the document export, and it matters more here: a pack
        # is filed. One built from a half-finished run would read as a complete
        # inventory of a repository that was only partly scanned.
        raise HTTPException(
            status_code=400,
            detail="Audit is not finished — export once the run completes.",
        )

    rows = list((await session.scalars(
        select(DepFinding).where(DepFinding.run_id == run_id)
    )).all())

    # Findings are already per (repo, package, version); the SBOM wants the
    # same rows grouped by repository, and the vulnerability list flattened out
    # of each finding's `vulns`.
    by_repo: dict[str, list] = {}
    # The commit each repo was indexed at, resolved once. An evidence pack
    # without it can be shown not to have been ALTERED — the sha256 manifest
    # does that — and cannot be used to check whether any finding was TRUE:
    # there is nothing to point at in the repository. With repo + commit +
    # ecosystem + package + version an auditor checks out that state and reads
    # the manifest themselves, which is the whole difference between a hash
    # and evidence.
    evidence_commits = _audited_commits(run, {r.repo_slug for r in rows})

    flat_vulns: list[dict] = []
    for row in rows:
        by_repo.setdefault(row.repo_slug, []).append(_DepRow(
            ecosystem=row.ecosystem, package=row.package,
            version=row.current_version, is_dev=bool(row.is_dev),
            manifest=getattr(row, "manifest", "") or "",
        ))
        for v in (row.vulns or []):
            flat_vulns.append({
                "id": v.get("id"),
                "package": row.package,
                "version": row.current_version,
                "ecosystem": row.ecosystem,
                "severity": v.get("severity") or row.severity,
                "summary": v.get("summary"),
                "fixed_version": v.get("fixed_in"),
                "aliases": v.get("aliases") or [],
                "repo": row.repo_slug,
                # Where to look, and who said so. Still missing the manifest
                # PATH — `DepFinding` has no column for it, so the `manifest`
                # field the SBOM emits is always empty and adding it needs a
                # migration. The commit closes most of that gap: a reader can
                # check the pinned version at this exact state without it.
                "commit": evidence_commits.get(row.repo_slug, ""),
                "source": v.get("source") or "osv",
                "transitive": bool(v.get("transitive")),
                "advisory_url": v.get("url") or (
                    f"https://osv.dev/vulnerability/{v.get('id')}"
                    if v.get("id") else None
                ),
                "detail_unavailable": bool(v.get("detail_unavailable")),
            })

    commits = _audited_commits(run, by_repo)
    sboms = {
        slug: build_sbom(repo_slug=slug, deps=deps,
                         commit=commits.get(slug, ""),
                         vulnerabilities=[v for v in flat_vulns
                                          if v.get("repo") == slug])
        for slug, deps in by_repo.items()
    }

    # The timeline is what answers "when did you know" — every previous run in
    # this workspace, not just this one.
    # `created_at`, from TimestampMixin — DepAuditRun has never had a
    # `started_at`, and asking for one raised AttributeError before the
    # response was ever built. So the evidence pack answered 500 to every
    # request it has ever received: the endpoint's own tests exercise
    # `build_evidence_pack` as a pure function and never call the route, so
    # nothing executed the lines that assemble its arguments.
    history = list((await session.scalars(
        select(DepAuditRun)
        .where(DepAuditRun.workspace_id == workspace_id)
        .order_by(DepAuditRun.created_at.desc())
        .limit(50)
    )).all())
    # Read from `summary`, which is where the auditor writes these. It used
    # to be `getattr(h, "findings_count", None)` and `DepAuditRun` has no such
    # attribute, so every row in every evidence pack carried `findings: null`
    # — a timeline that answers "when did a run happen" and never "what did it
    # find", which is the what-did-you-know-and-when question the file exists
    # for.
    timeline = [{
        "run": h.id,
        "at": h.created_at.isoformat() if h.created_at else None,
        "status": h.status,
        "vulnerable": (h.summary or {}).get("vulnerable"),
        "packages": (h.summary or {}).get("packages"),
        "repos_scanned": (h.summary or {}).get("repos_scanned"),
    } for h in reversed(history)]

    blob = build_evidence_pack(
        run={"id": run.id,
             "started_at": run.created_at.isoformat() if run.created_at else ""},
        findings=flat_vulns,
        sboms=sboms,
        timeline=timeline,
    )
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")

    # THE SECOND CHANNEL STARTS HERE. `MANIFEST.json` records a hash for every
    # other file and none for itself, so recomputing them proves the archive is
    # internally consistent and nothing more: anyone who edits a file and
    # rewrites its entry in the manifest passes that check. What they cannot do
    # is make the manifest hash to a value the recipient got from somewhere
    # else — so the export hands that value over separately, in a header and in
    # the log, and the operator can publish it wherever the pack is not.
    digest = manifest_sha256(blob)
    logger.info("deps_evidence_exported run=%s repos=%d findings=%d "
                "manifest_sha256=%s by=%s",
                run_id, len(sboms), len(flat_vulns), digest, user.email)
    return Response(
        content=blob,
        media_type="application/zip",
        headers={
            "Content-Disposition":
                f'attachment; filename="celmis-evidence-{stamp}.zip"',
            "X-Celmis-Manifest-SHA256": digest,
        },
    )


@router.get("/{run_id}/sbom")
async def export_sbom(
    run_id: str,
    repo: str = Query(default="", description="one repo slug; omit for all"),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> Response:
    """The CycloneDX bill of materials on its own.

    It existed only inside the evidence pack, which meant the one artefact
    procurement actually asks for — "send us your SBOM" — could not be
    obtained by any button, request or command. A zip containing it is not an
    answer to that question; the file is.

    One repository with `?repo=`, or every repository in the run as a zip.
    """
    from datetime import datetime

    from src.deps.sbom import build_sbom, to_json

    run = await session.get(DepAuditRun, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "done":
        raise HTTPException(
            status_code=400,
            detail="Audit is not finished — export once the run completes.",
        )

    rows = list((await session.scalars(
        select(DepFinding).where(DepFinding.run_id == run_id)
    )).all())
    if repo:
        rows = [r for r in rows if r.repo_slug == repo]
        if not rows:
            raise HTTPException(
                status_code=404,
                detail=f"{repo} is not in this audit run.")

    by_repo: dict[str, list] = {}
    vulns_by_repo: dict[str, list] = {}
    for row in rows:
        by_repo.setdefault(row.repo_slug, []).append(_DepRow(
            ecosystem=row.ecosystem, package=row.package,
            version=row.current_version, is_dev=bool(row.is_dev),
            manifest=getattr(row, "manifest", "") or "",
        ))
        for v in (row.vulns or []):
            vulns_by_repo.setdefault(row.repo_slug, []).append({
                "id": v.get("id"), "package": row.package,
                "version": row.current_version, "ecosystem": row.ecosystem,
                "severity": v.get("severity") or row.severity,
                "summary": v.get("summary"),
                "fixed_version": v.get("fixed_in"),
                "aliases": v.get("aliases") or [],
            })

    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    logger.info("deps_sbom_exported run=%s repo=%s repos=%d by=%s",
                run_id, repo or "*", len(by_repo), user.email)

    commits = _audited_commits(run, by_repo)
    if len(by_repo) == 1:
        slug, deps = next(iter(by_repo.items()))
        doc = build_sbom(repo_slug=slug, deps=deps,
                         commit=commits.get(slug, ""),
                         vulnerabilities=vulns_by_repo.get(slug, []))
        safe = slug.replace("/", "_")
        return Response(
            content=to_json(doc),
            # The registered CycloneDX media type, not application/json: it is
            # what a consuming tool content-negotiates on.
            media_type="application/vnd.cyclonedx+json",
            headers={"Content-Disposition":
                     f'attachment; filename="{safe}-{stamp}.cdx.json"'},
        )

    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for slug, deps in sorted(by_repo.items()):
            doc = build_sbom(repo_slug=slug, deps=deps,
                             commit=commits.get(slug, ""),
                             vulnerabilities=vulns_by_repo.get(slug, []))
            info = zipfile.ZipInfo(f"{slug.replace('/', '_')}.cdx.json",
                                   date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, to_json(doc))
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="celmis-sbom-{stamp}.zip"'},
    )


@router.get("/{run_id}/delta")
async def run_delta(
    run_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """What changed since the previous audit of this workspace.

    Every run was a complete picture of now and answered nothing about
    movement — the question somebody actually asks on a Monday is what
    appeared since Friday. Both runs' findings were already stored; nothing
    compared them.

    Computed rather than recorded: a stored delta is a third artefact that can
    disagree with the two it came from.
    """
    from src.deps.delta import compute_delta

    run = await session.get(DepAuditRun, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Run not found")

    previous = (await session.scalars(
        select(DepAuditRun)
        .where(DepAuditRun.workspace_id == workspace_id)
        .where(DepAuditRun.status == "done")
        .where(DepAuditRun.created_at < run.created_at)
        .order_by(DepAuditRun.created_at.desc())
        .limit(1)
    )).first()

    current_rows = list((await session.scalars(
        select(DepFinding).where(DepFinding.run_id == run_id)
    )).all())
    previous_rows: list = []
    if previous is not None:
        previous_rows = list((await session.scalars(
            select(DepFinding).where(DepFinding.run_id == previous.id)
        )).all())

    # Runs recorded before `repos_scanned_slugs` existed have no scope to
    # offer, and `compute_delta` deliberately refuses to guess one — it
    # returns the old undifferentiated answer rather than a confident wrong
    # split.
    scanned = (run.summary or {}).get("repos_scanned_slugs")
    delta = compute_delta(
        current_rows, previous_rows,
        previous_run_id=previous.id if previous is not None else None,
        current_repos=set(scanned) if scanned else None,
    )
    return {**delta.as_dict(), "headline": delta.headline(),
            "run_id": run_id}
