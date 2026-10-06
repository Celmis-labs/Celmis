"""Getting a review onto the queue and through it — with a run row for every
outcome, the reviews that never ran included.

Three facts this module exists for:

* A review that a gate refused before the pipeline started — auto-review
  switched off, a draft at the webhook, a duplicate of one already queued —
  used to leave nothing behind but a log line, so the pull-requests page could
  not say "Skipped — draft" and the reader assumed nothing had happened. Each
  of those now writes a run with its reason (`record_gate_skip`).
* The queue path and the inline fallbacks ran the same review through four
  copies of the same code (worker, webhook fallback, poller fallback, the UI
  trigger). The worker and both fallbacks now share `execute_review`, so the
  stages, the run row and the PR record cannot drift between them.
* A manual or bulk review creates its row BEFORE it is enqueued
  (`enqueue_review_run`), so the person who asked sees it as queued at once,
  and a request folded into a review already queued is answered, not dropped.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from dataclasses import dataclass

from src.review.stages import StageRecorder, ms_between, now_iso

logger = logging.getLogger(__name__)

#: Most PRs one bulk request may enqueue.
BULK_LIMIT = 25

_PROVIDER_LABEL = {"github": "GitHub", "gitlab": "GitLab", "bitbucket": "Bitbucket"}


def pr_ref_of(provider: str, repo: str, number: int) -> str:
    return f"{provider}:{repo}#{int(number)}"


def _label(provider: str) -> str:
    return _PROVIDER_LABEL.get(str(provider or "").lower(), str(provider or "provider"))


def received_sentence(source: str, provider: str) -> str:
    """The "Review started" stage: who asked for this review."""
    return {
        "webhook": f"Triggered by a {_label(provider)} webhook delivery.",
        "poller": f"Triggered by the {_label(provider)} poller (a new pull request was seen).",
        "manual": "Triggered manually from Celmis.",
        "bulk": "Triggered by “Review all open PRs” in Celmis.",
        "command": "Triggered by a comment command on the pull request.",
        "cli": "Triggered from the command line.",
        "mcp": "Triggered through the MCP server.",
    }.get(str(source or ""), "Triggered from the review queue.")


def fmt_wait(ms: int | None) -> str:
    if ms is None:
        return "an unknown time"
    s = ms / 1000
    if s < 1:
        return f"{ms} ms"
    if s < 60:
        return f"{s:.0f}s"
    m, sec = divmod(int(s), 60)
    return f"{m}m {sec}s"


def _store():
    from src.api.review_runs import get_review_run_store

    return get_review_run_store()


def new_run(
    provider: str, repo: str, number: int, *, user_id: str, workspace_id: str,
    source: str, status: str = "queued", store=None, pr_ref: str | None = None,
):
    """A run row for a PR, with its "Review started" stage. Returns
    (run, recorder) — the recorder persists every later stage onto the row."""
    from src.api.review_runs import ReviewRun, run_stage_sink

    store = store or _store()
    run = ReviewRun(
        id=str(uuid.uuid4()), user_id=user_id,
        pr_ref=pr_ref or pr_ref_of(provider, repo, number), workspace_id=workspace_id,
        status=status, pr_provider=provider, pr_repo=repo, pr_number=int(number),
    )
    stages = StageRecorder(sink=run_stage_sink(run.id, store))
    stages.add("received", "Review started", "success",
               received_sentence(source, provider), duration_ms=0,
               meta={"trigger": source})
    run.stages = stages.snapshot()
    store.insert(run)
    return run, stages


def _close_as_skipped(store, run_id: str, stages: StageRecorder, reason: str) -> None:
    from src.api.review_runs import finish_stages

    finish_stages(stages, "skipped")
    store.update(run_id, status="skipped", verdict="skipped",
                 summary=f"Review skipped — {reason}"[:500],
                 elapsed_seconds=0.0, finished=True)


def record_gate_skip(
    provider: str, repo: str, number: int, *, user_id: str, workspace_id: str,
    source: str, gate_key: str, gate_name: str, reason: str,
    pr_meta: dict | None = None, store=None,
) -> str | None:
    """A review a gate refused before the pipeline started, written down.

    Run row (status skipped, its stages ending in the gate that closed) and
    the PR record, so the pull-requests page shows it with its reason. Never
    raises: the caller is a webhook handler that has already answered.
    """
    try:
        store = store or _store()
        # One row per run of the same answer, not one per delivery: a repo
        # with auto-review off receives a webhook for every push, and a
        # history of identical skips buries the one review that did run.
        latest = store.latest_for_prs(workspace_id, provider, repo, [int(number)])
        prev = latest.get(int(number))
        if (prev is not None and prev.status == "skipped"
                and reason in (prev.status_reason or "")):
            return prev.id
        run, stages = new_run(provider, repo, number, user_id=user_id,
                              workspace_id=workspace_id, source=source,
                              status="skipped", store=store)
        stages.add(gate_key, gate_name, "skipped", reason, duration_ms=0,
                   ends_run=True)
        _close_as_skipped(store, run.id, stages, reason)
        from src.review.issues import record_unreviewed_run

        meta = {k: v for k, v in (pr_meta or {}).items()
                if k in ("title", "author", "url", "head_ref", "base_ref") and v}
        record_unreviewed_run(workspace_id=workspace_id, provider=provider,
                              repo=repo, number=int(number), run_id=run.id,
                              status="skipped", **meta)
        logger.info("review_gate_skip_recorded run=%s gate=%s pr=%s",
                    run.id, gate_key, run.pr_ref)
        return run.id
    except Exception as exc:  # noqa: BLE001
        logger.warning("review_gate_skip_not_recorded gate=%s repo=%s pr=%s err=%s",
                       gate_key, repo, number, exc)
        return None


@dataclass
class QueuedReview:
    run_id: str
    #: queued | duplicate | inline — inline means the queue was unavailable
    #: and the caller must run `execute_review(payload)` itself.
    status: str
    reason: str
    payload: dict


def enqueue_review_run(
    provider: str, repo: str, number: int, *, user_id: str, workspace_id: str,
    post_comments: bool = True, source: str = "manual", store=None,
    request=None, extra: dict | None = None,
) -> QueuedReview:
    """Create the run row, then put the review on the queue.

    The row exists before the job does, so it is visible as queued at once;
    the worker picks the row up by `run_id` and records the time spent
    waiting. A dedup hit — this PR already has a review queued or running —
    closes the row as skipped with that reason instead of losing the request.

    `request` (`ReviewRequest`) rides in the payload; without one the request
    is what `source` says (see `ReviewRequest.from_payload`). A forced request
    has its own dedup key, so it is not folded into a normal job that is
    already waiting (which would not be forced). `extra` keys ride in the job
    payload as well (a comment command's `command_ack`, which `execute_review`
    answers when the review ends).
    """
    store = store or _store()
    run, stages = new_run(provider, repo, number, user_id=user_id,
                          workspace_id=workspace_id, source=source, store=store)
    stages.begin("queued", "Queued", "Waiting for a review worker.")
    payload = {
        "provider": provider, "repo": repo, "pr_number": int(number),
        "post_comments": bool(post_comments), "user_id": user_id,
        "workspace_id": workspace_id, "run_id": run.id, "source": source,
        "enqueued_at": now_iso(),
    }
    if request is not None:
        payload.update(request.as_payload())
    if extra:
        payload.update(extra)
    forced = bool(request and request.force)
    try:
        from src.sync.queue import KIND_REVIEW, enqueue

        job_id = enqueue(
            kind=KIND_REVIEW, payload=payload,
            dedup_key=f"review:{provider}:{repo}#{int(number)}"
                      + (":force" if forced else ""),
            enqueued_by=f"{source}:{user_id}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("review_enqueue_failed_running_inline run=%s err=%s",
                       run.id, type(exc).__name__)
        stages.end("queued", "success",
                   "The job queue is unavailable; the review runs in the API "
                   "process instead.")
        return QueuedReview(run.id, "inline", "", payload)
    if job_id is None:
        reason = ("a review of this pull request is already queued or running; "
                  "this request was folded into it")
        stages.end("queued", "skipped", reason[:1].upper() + reason[1:] + ".",
                   ends_run=True)
        _close_as_skipped(store, run.id, stages, reason)
        return QueuedReview(run.id, "duplicate", reason, payload)
    return QueuedReview(run.id, "queued", "", payload)


def _answer_command(p: dict, provider, *, result=None, failed: bool = False) -> None:
    """Close the loop with the person who commented `@celmis review`."""
    if not isinstance(p.get("command_ack"), dict):
        return
    try:
        from src.review.commands.handlers import finish_ack

        finish_ack(p, provider, result=result, failed=failed)
    except Exception:  # noqa: BLE001 — an answer never fails the review
        logger.warning("command_ack_failed run=%s", p.get("run_id"))


def execute_review(p: dict, *, attempt: int | None = None) -> None:
    """Run one queued review and record it — the worker's body, and the body
    of every inline fallback.

    Payload: {provider, repo, pr_number, post_comments?, user_id?,
    workspace_id?, run_id?, source?, enqueued_at?, trigger?, force?, scope?,
    resume?} — the last four are the `ReviewRequest`. `run_id` names a row
    created at enqueue time; without it (jobs queued by an older version) a
    row is created here.

    Raises after recording when the review itself fails, so the queue can
    retry; never raises for a failure to WRITE a review that ran — that
    overwrote the row to "failed", counted the PR's review twice and
    re-raised into a queue that may retry, i.e. post the comments again.
    """
    from src.api import review_runs as runs
    from src.review import issues as issues_mod
    from src.review import orchestrator as orch_mod
    from src.review import providers as providers_mod

    # Resolve BOTH halves (git provider + LLM orchestrator) under the SAME
    # tenant — otherwise comments post with one workspace's PAT while another
    # workspace's LLM key is billed.
    user_id = p.get("user_id", "default")
    workspace_id = p.get("workspace_id", "default")
    provider_name = p["provider"]
    repo = p["repo"]
    number = int(p["pr_number"])
    post = bool(p.get("post_comments", True))
    source = str(p.get("source") or "")
    from src.review.scope import ReviewRequest

    request = ReviewRequest.from_payload(p)
    orch = orch_mod.ReviewOrchestrator()

    store = runs.get_review_run_store()
    run_id = str(p.get("run_id") or "") or str(uuid.uuid4())
    existing = None
    if p.get("run_id") and hasattr(store, "get"):
        try:
            existing = store.get(run_id)
        except Exception:  # noqa: BLE001
            existing = None
    stages = StageRecorder(sink=runs.run_stage_sink(run_id, store),
                           stages=getattr(existing, "stages", None) or None)

    if existing is None:
        # A review that leaves no row is a review nobody can look at
        # afterwards: this path posted its comments and recorded nothing,
        # once, so the history was empty on an install with auto review on.
        store.insert(runs.ReviewRun(
            id=run_id, user_id=user_id, pr_ref=pr_ref_of(provider_name, repo, number),
            workspace_id=workspace_id, status="running",
            pr_provider=provider_name, pr_repo=repo, pr_number=number,
        ))
        enq = p.get("enqueued_at")
        stages.add("received", "Review started", "success",
                   received_sentence(source, provider_name),
                   started_at=enq or None, duration_ms=0,
                   meta={"trigger": source or "queue"})
        if enq:
            waited = ms_between(enq)
            stages.add("queued", "Queued", "success",
                       f"Picked up by a review worker after {fmt_wait(waited)}.",
                       started_at=enq, duration_ms=waited)
    else:
        if existing.status not in ("queued", "running"):
            stages.add("retry", "Retry", "success",
                       f"Retried by the queue (attempt {attempt or '?'}) after "
                       f"the previous attempt ended {existing.status}.",
                       duration_ms=0)
        queued = stages.get("queued")
        if queued and queued.get("status") == "running":
            stages.end("queued", "success",
                       "Picked up by a review worker after "
                       f"{fmt_wait(ms_between(queued.get('started_at')))}.")
        store.update(run_id, status="running")

    def _failed(exc: BaseException, reason: str) -> None:
        stages.fail_running(reason)
        runs.finish_stages(stages, "failed", error=reason)
        # "failed", the `ReviewRunStatus` word: this wrote "error" once, which
        # no status bucket, badge or metric knows.
        store.update(run_id, status="failed", finished=True, summary=str(exc)[:500])
        issues_mod.record_failed_review(
            workspace_id=workspace_id, provider=provider_name, repo=repo,
            number=number, run_id=run_id,
        )

    try:
        provider = providers_mod.get_provider_for(
            provider_name, user_id=user_id, workspace_id=workspace_id)
    except Exception as exc:
        reason = (f"No usable {_label(provider_name)} connection for this workspace "
                  f"({type(exc).__name__}) — connect it on the Connections page.")
        stages.add("fetch_pr", "Fetch pull request", "failed", reason, duration_ms=0)
        _failed(exc, reason)
        raise
    try:
        result = orch.review(
            provider_name, repo, number,
            dry_run=not post, post_comments=post, provider=provider,
            user_id=user_id, workspace_id=workspace_id, stages=stages,
            request=request,
        )
        # A review that a comment asked for answers the comment, while the
        # provider is still open. Never raises.
        _answer_command(p, provider, result=result)
    except Exception as exc:
        _failed(exc, orch_mod._safe_failure_reason(exc))
        _answer_command(p, provider, failed=True)
        raise
    finally:
        with contextlib.suppress(Exception):
            provider.close()

    try:
        runs.record_completed_review(
            result, run_id=run_id, store=store,
            # The deterministic drift facts, which only the UI writer stored.
            drift_facts=getattr(orch, "_last_drift_facts", None),
            workspace_id=workspace_id, stages=stages,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("review_run_record_failed run=%s", run_id)
        try:
            batch = getattr(result, "batch", None)
            status = (runs.completion_status(batch, runs.post_failure(result))
                      if batch is not None else "partial")
            runs.finish_stages(stages, status, batch=batch)
            store.update(
                run_id, status=status, finished=True,
                summary=f"Review finished; its record could not be written: {exc}"[:500],
            )
        except Exception:  # noqa: BLE001
            logger.exception("review_run_record_fallback_failed run=%s", run_id)
