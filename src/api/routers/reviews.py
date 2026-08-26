"""Review routes — manual trigger + history.

Runs are persisted to ReviewRunStore (sqlite) so queued/in-progress/failed
runs are visible in the UI, not just successful ones.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from src.api.deps import (
    current_workspace_id,
    enforce_repo_permission,
    get_current_user,
    slug_from_pr_ref,
)
from src.api.review_runs import (
    ReviewRun,
    adjustments_payload,
    get_review_run_store,
    hidden_payload,
)
from src.api.schemas import ReviewRunOut, ReviewTriggerRequest
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reviews", tags=["reviews"])


def _run_to_out(run: ReviewRun, *, with_adjustments: bool = True) -> ReviewRunOut:
    """The wire row for a run.

    `with_adjustments=False` is the history list's shape: the count travels,
    the list does not — fifty rows times every agent's adjustments is weight
    the list view has no use for, it badges and links to the detail view.
    """
    return ReviewRunOut(
        id=run.id,
        pr_ref=run.pr_ref,
        # `verdict` has always doubled as the display state, which is why
        # 'partial' is NOT folded into it: the UI reads this field and a run
        # that posted comments with one stage missing still has a real
        # verdict to show. The gap travels in `status` + `agents_failed`.
        verdict=run.status if run.status in ("queued", "running", "failed") else run.verdict,
        status=run.status,
        agents_run=run.agents_run,
        agents_failed=run.agents_failed,
        agents_skipped=run.agents_skipped,
        findings_count=run.findings_count,
        critical=run.critical,
        error=run.error_count,
        warning=run.warning,
        info=run.info,
        cross_repo_callers=run.cross_repo_callers,
        drift_hits=getattr(run, "drift_hits", 0),
        posted=run.posted,
        started_at=run.started_at,
        elapsed_seconds=run.elapsed_seconds,
        # `or run.summary`: 'failed' used to be reachable only from the
        # except-branch that writes `error_message`, so swapping the two was
        # safe. A run in which every agent errored now finishes normally and
        # is FAILED with no error_message — the explanation is the banner in
        # `summary`, and the bare swap blanked it.
        summary=(run.error_message or run.summary) if run.status == "failed" else run.summary,
        cost_usd=run.cost_usd,
        cost_source=run.cost_source,
        tokens_input=run.tokens_input,
        tokens_output=run.tokens_output,
        cleanup=run.cleanup,
        # Plain dicts in, typed models out — every field of the Out model
        # defaults, so a row another version shaped cannot fail the request.
        parameter_adjustments=(
            run.parameter_adjustments if with_adjustments else None
        ),
        adjustments_count=run.adjustments_count,
        hidden=run.hidden,
    )


@router.post("/trigger", response_model=ReviewRunOut)
async def trigger_review(
    req: ReviewTriggerRequest,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> ReviewRunOut:
    # RBAC (Stage 14): resolve slug from pr_ref, then enforce team
    # permission. Falls open if the repo has no team grants configured
    # — matches the path-param dep behaviour so single-user workspaces
    # keep working without any RBAC setup.
    slug = slug_from_pr_ref(req.pr_ref)
    if slug:
        # The workspace lets the check reach a grant stored under the other
        # spelling — see repo_grant_candidates.
        await enforce_repo_permission(
            slug, user, min_perm="review", workspace_id=workspace_id)

    run_id = str(uuid.uuid4())
    run = ReviewRun(id=run_id, user_id=user.id, pr_ref=req.pr_ref,
                    workspace_id=workspace_id)
    get_review_run_store().insert(run)

    background.add_task(
        _run_review_task,
        pr_ref=req.pr_ref, post_comments=req.post_comments,
        run_id=run_id, user_id=user.id, workspace_id=workspace_id,
    )
    return _run_to_out(run)


def _can_see(run, user: User, workspace_id: str) -> bool:
    """Workspace members see every run of the workspace; the user_id
    fallback covers legacy rows persisted before workspace tenancy."""
    return run.workspace_id == workspace_id or run.user_id == user.id


@router.get("/history", response_model=list[ReviewRunOut])
def history(
    limit: int = 50,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> list[ReviewRunOut]:
    runs = get_review_run_store().list_for_workspace(
        workspace_id, user_id=user.id, limit=limit)
    # The count only — see `_run_to_out`.
    return [_run_to_out(r, with_adjustments=False) for r in runs]


@router.get("/{run_id}", response_model=ReviewRunOut)
def get_run(
    run_id: str,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> ReviewRunOut:
    run = get_review_run_store().get(run_id)
    if run is None or not _can_see(run, user, workspace_id):
        raise HTTPException(status_code=404, detail="Run not found")
    return _run_to_out(run)


@router.get("/{run_id}/diff")
def get_diff(
    run_id: str,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """Return the persisted unified diff for a run (Stage 21). Empty when
    the run predates diff persistence — the UI shows a hint instead."""
    import sqlite3
    store = get_review_run_store()
    run = store.get(run_id)
    if run is None or not _can_see(run, user, workspace_id):
        raise HTTPException(status_code=404, detail="Run not found")
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT raw_diff FROM review_runs WHERE id = ?", (run_id,),
        ).fetchone()
    diff = (row["raw_diff"] if row else None) or ""
    return {
        "run_id": run_id,
        "diff": diff,
        "bytes": len(diff),
        "available": bool(diff),
    }


@router.get("/{run_id}/findings")
def get_findings(
    run_id: str,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """Return the persisted findings snapshot + PR coordinates so the UI
    can render an actionable list (one Apply-fix button per finding)
    without hitting the audit-log JSONL or re-fetching from the provider.

    Older runs (predating the findings_json ALTER) return an empty list
    with a note flag so the UI can degrade gracefully.
    """
    import json
    import sqlite3
    store = get_review_run_store()
    run = store.get(run_id)
    if run is None or not _can_see(run, user, workspace_id):
        raise HTTPException(status_code=404, detail="Run not found")
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT findings_json, drift_json, pr_head_sha, pr_head_ref, "
            "       pr_provider, pr_repo, pr_number FROM review_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    findings: list = []
    if row and row["findings_json"]:
        try:
            findings = json.loads(row["findings_json"])
        except (json.JSONDecodeError, TypeError):
            findings = []
    # The deterministic half, travelling BESIDE the model's findings rather
    # than inside them. It is not paginated and not filtered: a drift report is
    # a handful of rows, and it is the part that needs no defending.
    drift = None
    # sqlite3.Row: `in row` tests values, not column names.
    if row is not None and "drift_json" in row.keys() and row["drift_json"]:  # noqa: SIM118
        try:
            drift = json.loads(row["drift_json"])
        except (json.JSONDecodeError, TypeError):
            drift = None
    total = len(findings)
    # Stage 21 — pagination. A 500-finding monorepo PR would otherwise
    # push a multi-MB payload; UI loads pages of 50.
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    page = findings[offset:offset + limit]
    return {
        "run_id": run_id,
        "pr": {
            "provider": row["pr_provider"] if row else None,
            "repo": row["pr_repo"] if row else None,
            "number": row["pr_number"] if row else None,
            "head_sha": row["pr_head_sha"] if row else None,
            "head_ref": row["pr_head_ref"] if row else None,
        },
        "findings": page,
        # Separate key, not merged into `findings`. They are different KINDS of
        # claim — one is a grep result with a file and a line, the other is a
        # model's judgement — and a single list with a badge invites the reader
        # to trust them equally. The market number behind that: 20% false
        # positives is where developers stop reading a tool at all, so the
        # proven part is kept out of reach of the inferred part's reputation.
        "drift": drift,
        "count": len(page),
        "total": total,
        "limit": limit,
        "offset": offset,
        "legacy": total == 0 and (run.findings_count or 0) > 0,
    }


def _run_review_task(
    *, pr_ref: str, post_comments: bool, run_id: str, user_id: str,
    workspace_id: str = "default",
) -> None:
    """Background task — runs orchestrator + updates run row at every step."""
    store = get_review_run_store()
    store.update(run_id, status="running")
    t0 = datetime.now(UTC)
    try:
        from src.cli import _parse_pr_ref
        from src.review.orchestrator import ReviewOrchestrator
        from src.review.providers import get_provider_for

        provider, repo, pr_number = _parse_pr_ref(pr_ref)
        pr_provider = get_provider_for(provider, user_id=user_id, workspace_id=workspace_id)
        try:
            orch = ReviewOrchestrator()
            result = orch.review(
                provider, repo, pr_number,
                dry_run=not post_comments,
                post_comments=post_comments,
                provider=pr_provider,
                user_id=user_id,     # Stage 11 — routes BYOK keys + policy overrides
                workspace_id=workspace_id,  # multi-tenant — ws:{id} keys/config
            )
        finally:
            pr_provider.close()

        batch = result.batch
        import json as _json
        findings_payload = [
            {
                "id": f.rule_id or f"{f.agent}.{f.file_path}:{f.line}",
                "agent": f.agent,
                "file_path": f.file_path,
                "line": f.line,
                "severity": f.severity.value if hasattr(f.severity, "value") else str(f.severity),
                "title": f.title,
                "body": f.body,
                "suggestion": f.suggestion,
                "rule_id": f.rule_id,
                "confidence": f.confidence,
                # The sentence the evidence gate exists to force, and the one
                # the bench judge most often matches on: 43 of 51 extracted
                # candidates were lexically closer to the comment's first
                # `*Why:*` line than to the whole rest of the body. It was
                # dropped here while `evidence_kind` — HOW the finding was
                # arrived at — was kept, so the history page, every report and
                # any reconstruction of a review that failed to post showed a
                # finding with no reason attached. Measured on a full run of
                # the benchmark: 0 of 63 stored findings carried it.
                "reasoning": getattr(f, "reasoning", "") or "",
                # How it was arrived at, which is a different question from
                # how sure we are. A float cannot carry both.
                "evidence_kind": getattr(f, "evidence_kind", "inferred"),
            }
            for f in batch.findings
        ]
        pr = batch.pull_request
        # The providers count what their cleanup actually did ({deleted,
        # failed, kept_threaded, complete}) — and the count used to die right
        # here, in a provider_response nothing persisted, so the UI could not
        # tell a finished cleanup from one that left the previous run's
        # comments on the PR. Only a dict qualifies: a dry-run's {} and an
        # {"error": ...} post carry no report, and NULL must keep meaning
        # "never cleaned", not become an empty report that reads as clean.
        # getattr, not attribute access — record_completed_review reads the
        # same field the same way, and a result object without it (test
        # doubles have shipped without one) must degrade to "no report",
        # not fail the whole run into 'failed'.
        cleanup = getattr(result, "provider_response", None)
        cleanup = cleanup.get("cleanup") if isinstance(cleanup, dict) else None
        store.update(
            run_id,
            # See record_completed_review: "complete" is a claim, and a run
            # that lost its security agent is not entitled to make it.
            status=batch.run_status.value,
            agents_run=list(batch.agents_run),
            agents_failed=list(batch.agents_failed),
            agents_skipped=list(getattr(batch, "agents_skipped", []) or []),
            verdict=batch.verdict.value,
            findings_count=len(batch.findings),
            critical=batch.critical_count,
            error_count=batch.error_count,
            warning=batch.warning_count,
            info=batch.info_count,
            cross_repo_callers=batch.cross_repo_callers,
            posted=bool(result.posted),
            elapsed_seconds=batch.elapsed_seconds,
            summary=(batch.summary or "")[:500],
            finished=True,
            # Stage 11 — cost + token accounting
            cost_usd=batch.cost_usd,
            cost_source=batch.cost_source,
            tokens_input=batch.tokens_in,
            tokens_output=batch.tokens_out,
            # Stage 17 — findings + PR coordinates snapshot
            findings_json=_json.dumps(findings_payload),
            # The deterministic half, kept as data. Without this the drift
            # detector's output exists only inside the architect's prompt, so
            # the user reads a model's summary of a fact instead of the fact.
            drift_json=(_json.dumps(orch._last_drift_facts)
                        if getattr(orch, "_last_drift_facts", None)
                        else None),
            pr_head_sha=pr.head_sha or None,
            pr_head_ref=pr.head_ref or None,
            pr_provider=pr.provider or None,
            pr_repo=pr.repo or None,
            pr_number=pr.number or None,
            # Stage 21 — diff snapshot for the side-by-side UI. Capped so a
            # multi-MB monorepo diff can't bloat the sqlite row.
            raw_diff=(pr.raw_diff or "")[:800_000] or None,
            cleanup_json=(_json.dumps(cleanup)
                          if isinstance(cleanup, dict) else None),
            # Same helper as record_completed_review — the two writers have
            # drifted before, and this list must not be the third field to.
            parameter_adjustments=adjustments_payload(batch),
            hidden=hidden_payload(batch),
        )
        logger.info(
            "review_run_complete id=%s user=%s pr_ref=%s verdict=%s findings=%d",
            run_id, user_id, pr_ref, batch.verdict.value, len(batch.findings),
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = (datetime.now(UTC) - t0).total_seconds()
        msg = str(exc)[:500] or exc.__class__.__name__
        store.update(
            run_id, status="failed",
            error_message=msg, elapsed_seconds=elapsed,
            finished=True,
        )
        logger.exception("review_run_failed id=%s err=%s", run_id, exc)
