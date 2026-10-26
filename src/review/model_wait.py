"""The deferred list of reviews: who is waiting for the model, and when to let go.

A review whose model did not answer is parked in `sync_jobs` as 'deferred'
(`src.sync.queue.mark_deferred`). This is the processor the worker runs every
`CELMIS_REVIEW_DEFER_CHECK_SECONDS` (60) and once at startup:

  1. EXPIRE   jobs deferred for longer than `CELMIS_REVIEW_DEFER_MAX_HOURS`
              (24) go dead; their run row fails with "model unavailable for
              N h"; a placeholder on the pull request is rewritten to say the
              review was not done and how to run it again. One ERROR line per
              job — the service could not do its job.
  2. CLAIM    the jobs whose next check is due. Each row is claimed by its own
              conditional UPDATE, so two processors (two workers, two
              replicas) never hold the same row.
  3. PROBE    once per group of (workspace, set of models), through the same
              cached preflight a review uses.
  4. RELEASE  an available group goes back to 'pending', oldest first, to
              review the PR's CURRENT head as every job does; an unavailable
              one is rescheduled 1, 2, 5, 10, then 15 minutes out.

Logging: the deferral itself warns once per job (in the queue); a release
logs INFO with counts. Never raises.
"""

from __future__ import annotations

import contextlib
import logging
import os
from datetime import timedelta
from typing import Any

from src.sync import queue as jq

logger = logging.getLogger(__name__)


def check_seconds() -> float:
    try:
        return max(5.0, float(os.environ.get("CELMIS_REVIEW_DEFER_CHECK_SECONDS", "60")))
    except ValueError:
        return 60.0


def max_age() -> timedelta:
    try:
        hours = max(0.001, float(os.environ.get("CELMIS_REVIEW_DEFER_MAX_HOURS", "24")))
    except ValueError:
        hours = 24.0
    return timedelta(hours=hours)


def process_deferred() -> dict[str, int]:
    """One pass over the deferred reviews. Returns counts (for logs and tests)."""
    counts = {"expired": 0, "claimed": 0, "released": 0, "waiting": 0}
    try:
        for job in jq.expire_deferred(jq.KIND_REVIEW, max_age=max_age()):
            counts["expired"] += 1
            _close_expired(job)
    except Exception as exc:  # noqa: BLE001
        logger.warning("review_defer_expire_failed err_type=%s", type(exc).__name__)
    try:
        due = jq.claim_due_deferred(jq.KIND_REVIEW)
    except Exception as exc:  # noqa: BLE001
        logger.warning("review_defer_claim_failed err_type=%s", type(exc).__name__)
        return counts
    counts["claimed"] = len(due)
    if not due:
        return counts

    from src.review.model_preflight import ModelCheck, check_models, review_models

    groups: dict[tuple[str, frozenset[str]], list[dict[str, Any]]] = {}
    answers: dict[tuple[str, frozenset[str]], ModelCheck] = {}
    resolved: dict[tuple[str, str, str], Any] = {}
    failed_jobs: list[dict[str, Any]] = []
    for job in due:  # oldest first, as claimed
        p = job.get("payload") or {}
        ws = str(job.get("workspace_id") or p.get("workspace_id") or "default")
        rkey = (ws, str(p.get("provider")), str(p.get("repo")))
        try:
            if rkey not in resolved:
                resolved[rkey] = review_models(
                    rkey[1], rkey[2], workspace_id=ws,
                    user_id=str(p.get("user_id") or "default"))
            models = resolved[rkey]
        except Exception as exc:  # noqa: BLE001 — keep waiting; do not release blind
            logger.warning("review_defer_models_unresolved workspace=%s err_type=%s",
                           ws, type(exc).__name__)
            failed_jobs.append(job)
            continue
        gkey = (ws, frozenset(models.models))
        groups.setdefault(gkey, []).append(job)
        if gkey not in answers:
            answers[gkey] = check_models(ws, models)
    if failed_jobs:
        jq.reschedule_deferred(failed_jobs, floor=check_seconds())
        counts["waiting"] += len(failed_jobs)
    for gkey, jobs in groups.items():
        answer = answers[gkey]
        if answer.available:
            counts["released"] += jq.release_deferred([j["id"] for j in jobs])
        else:
            jq.reschedule_deferred(jobs, reason=answer.reason, code=answer.code,
                                   floor=check_seconds())
            counts["waiting"] += len(jobs)
    if counts["released"]:
        logger.info("review_defer_released released=%d still_waiting=%d groups=%d",
                    counts["released"], counts["waiting"], len(groups))
    return counts


# ─── Expiry ──────────────────────────────────────────────────────────


def _close_expired(job: dict[str, Any]) -> None:
    """The review never got its model. Close its run row, tell the pull request
    (only over a placeholder, never over a finished summary) and log ERROR once.
    Every step is best-effort: the job is dead either way."""
    p = job.get("payload") or {}
    reason = str(job.get("reason") or "model unavailable")
    logger.error(
        "review_deferred_expired job=%s workspace=%s repo=%s pr=%s since=%s — %s; "
        "the review was NOT done",
        job.get("id"), job.get("workspace_id") or "-", p.get("repo"),
        p.get("pr_number"), job.get("deferred_since"), reason)
    _fail_run(p, reason)
    if p.get("post_comments", True):
        hours = reason.rsplit(" for ", 1)[-1].replace(" h", "")
        # The one place a note may be CREATED: a review deferred at preflight
        # never wrote a placeholder, and the author must still learn it did not
        # happen. An existing comment of ours (a finished summary) is left alone.
        _rewrite_placeholder(p, "abandoned", hours, create_if_none=True)


def close_cancelled(job: dict[str, Any]) -> None:
    """An administrator cancelled a waiting review: close its run and rewrite
    the placeholder (if one was published) so it stops promising a re-run."""
    p = job.get("payload") or {}
    if isinstance(p, str):
        import json

        try:
            p = json.loads(p)
        except ValueError:
            p = {}
    reason = "cancelled by an administrator"
    _fail_run(p, reason)
    if p.get("post_comments", True):
        _rewrite_placeholder(p, "cancelled", reason)


def _fail_run(p: dict, reason: str) -> None:
    run_id = str(p.get("run_id") or "")
    if not run_id:
        return
    try:
        from src.api import review_runs as runs
        from src.review.stages import StageRecorder

        store = runs.get_review_run_store()
        existing = store.get(run_id) if hasattr(store, "get") else None
        stages = StageRecorder(sink=runs.run_stage_sink(run_id, store),
                               stages=getattr(existing, "stages", None) or None)
        waiting = stages.get("model_wait")
        if waiting and waiting.get("status") == "running":
            stages.end("model_wait", "failed", reason[:1].upper() + reason[1:] + ".")
        runs.finish_stages(stages, "failed", error=reason)
        store.update(run_id, status="failed", finished=True,
                     summary=f"Review not done: {reason}"[:500])
        from src.review import issues as issues_mod

        issues_mod.record_failed_review(
            workspace_id=str(p.get("workspace_id") or "default"),
            provider=p["provider"], repo=p["repo"], number=int(p["pr_number"]),
            run_id=run_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("review_deferred_run_not_closed run=%s err_type=%s",
                       run_id, type(exc).__name__)


def _rewrite_placeholder(p: dict, outcome: str, reason: str, *,
                         create_if_none: bool = False) -> None:
    provider = None
    try:
        from src.review import providers as providers_mod
        from src.review.dispatch import _answer_command
        from src.review.orchestrator import ReviewOrchestrator
        from src.review.providers.base import _format_status_comment

        ws = str(p.get("workspace_id") or "default")
        provider = providers_mod.get_provider_for(
            p["provider"], user_id=p.get("user_id", "default"), workspace_id=ws)
        pr = provider.fetch_pull_request(p["repo"], int(p["pr_number"]))
        language = ReviewOrchestrator()._language_for(pr, ws)
        body = _format_status_comment(pr, outcome=outcome, reason=reason,
                                      language=language)
        # Only a placeholder (the "postponed" note, or one a killed run left):
        # a finished summary of an earlier commit is a record, not ours to touch.
        written = provider.upsert_status_comment(pr, body, create=False,
                                                 only_if_in_progress=True)
        if written is None and create_if_none and not provider._our_summary_comments(
                pr, provider._comment_marker()):
            # No comment of ours at all: say once that the review did not happen.
            # Carries the review marker, so a later run reuses this comment.
            provider.upsert_status_comment(pr, body, create=True)
        _answer_command(p, provider, failed=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("review_deferred_placeholder_not_rewritten pr=%s err_type=%s",
                       p.get("pr_number"), type(exc).__name__)
    finally:
        if provider is not None:
            with contextlib.suppress(Exception):
                provider.close()
