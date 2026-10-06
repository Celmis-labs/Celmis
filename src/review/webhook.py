"""Webhook receiver — FastAPI server for GitHub/GitLab/Bitbucket PR events.

Stage 17.4 (May 2026, FastAPI 0.136.0).

Pattern (per research):
    1. Verify HMAC signature on raw bytes (constant-time compare)
    2. Dedup on delivery_id (Redis SETNX TTL=24h, fallback in-memory dict)
    3. Return 202 Accepted within 2s — do not block on the review
    4. Background task triggers ReviewOrchestrator
    5. Comment posted with a marker for idempotent updates on synchronize

Endpoints:
    POST /webhook/github
    POST /webhook/gitlab
    POST /webhook/bitbucket
    GET  /healthz
    GET  /webhook/stats   — diagnostics

Authentication per provider:
    GitHub:    X-Hub-Signature-256 (HMAC-SHA256, secret = settings.webhook_secret)
    GitLab:    X-Gitlab-Token (plaintext compare, secret = settings.gitlab_token)
    Bitbucket: X-Hub-Signature (HMAC-SHA256 — Atlassian aligned 2024+,
               secret = settings.bitbucket_secret)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections import OrderedDict

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from src.review import messages
from src.review.commands.events import (
    BITBUCKET_COMMENT_EVENTS,
    GITHUB_COMMENT_EVENTS,
    GITLAB_NOTE_EVENT,
    extract_bitbucket_comment,
    extract_github_comment,
    extract_gitlab_note,
)
from src.review.settings import ReviewSettings, get_review_settings
from src.review.webhook_secrets import resolve_webhook_secret

logger = logging.getLogger(__name__)


# ─── Idempotency dedup (in-memory fallback) ─────────────────────


class InMemoryDedup:
    """Fallback dedup for single-instance deploys without Redis.

    Bounded LRU dict — TTL 24h, capacity ~10k entries. Not distributed —
    multi-process scenarios require Redis.
    """

    def __init__(self, maxsize: int = 10_000, ttl_seconds: int = 86400) -> None:
        self.maxsize = maxsize
        self.ttl = ttl_seconds
        self._entries: OrderedDict[str, float] = OrderedDict()
        import threading
        self._lock = threading.Lock()

    def is_duplicate(self, delivery_id: str) -> bool:
        """Check + register in one operation."""
        now = time.time()
        with self._lock:
            # Expire old
            cutoff = now - self.ttl
            while self._entries:
                first_key = next(iter(self._entries))
                if self._entries[first_key] < cutoff:
                    self._entries.popitem(last=False)
                else:
                    break

            if delivery_id in self._entries:
                return True

            # Register
            self._entries[delivery_id] = now
            self._entries.move_to_end(delivery_id)

            # Capacity bound — drop oldest
            while len(self._entries) > self.maxsize:
                self._entries.popitem(last=False)

            return False


class RedisDedup:
    """Redis-backed dedup — distributed-safe for multi-instance.

    Uses SET NX EX — atomic check-and-set with a TTL.
    """

    def __init__(self, redis_url: str, ttl_seconds: int = 86400) -> None:
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("redis package not installed") from exc
        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self.ttl = ttl_seconds

    def is_duplicate(self, delivery_id: str) -> bool:
        """SET NX returns False if the key already exists = duplicate."""
        key = f"webhook:dedup:{delivery_id}"
        # SET NX returns True if it is new, False if it already exists
        ok = self._redis.set(key, "1", nx=True, ex=self.ttl)
        return not bool(ok)


def _build_dedup(settings: ReviewSettings):
    if settings.has_redis:
        try:
            return RedisDedup(settings.redis_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("redis_unavailable_falling_back_inmem: %s", exc)
    return InMemoryDedup()


# ─── HMAC verification ──────────────────────────────────────────


def _verify_github_signature(
    body: bytes, signature_header: str | None, secret: str,
) -> bool:
    """X-Hub-Signature-256: 'sha256=<hex>'."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _verify_gitlab_token(token_header: str | None, expected: str) -> bool:
    """GitLab — plaintext token compare."""
    if not token_header:
        return False
    return hmac.compare_digest(token_header, expected)


def _gitlab_instance_matches(workspace_id: str | None, web_url: object) -> bool:
    """Is the payload's ``project.web_url`` on the GitLab this workspace is
    connected to?

    The token check already proved the sender knows the workspace's secret;
    this binds the event to the INSTANCE as well, so a hook on some other
    GitLab carrying the same secret (a copied configuration, a test instance)
    cannot make the workspace review a same-named project from the wrong
    server. A workspace with no GitLab connection has nothing to compare
    against and cannot review anyway — not refused here. A payload without a
    web_url (old GitLab versions, hand-made tests) is not refused either.
    """
    if not isinstance(web_url, str) or not web_url.strip():
        return True
    try:
        from src.credentials import resolve_git_credential
        from src.sync.gitlab_instance import instance_for_credential

        cred = resolve_git_credential("gitlab", workspace_id=workspace_id or "default")
        if cred is None:
            return True
        return instance_for_credential(cred).owns_url(web_url)
    except Exception as exc:  # noqa: BLE001 — unreadable config: refuse, log
        logger.warning("gitlab_webhook_instance_check_failed ws=%s err=%s",
                       workspace_id, type(exc).__name__)
        return False


def _verify_bitbucket_signature(
    body: bytes, signature_header: str | None, secret: str,
) -> bool:
    """Bitbucket — same HMAC-SHA256 pattern as GitHub (Atlassian aligned 2024+)."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# ─── Event extraction ──────────────────────────────────────────


def _extract_github_pr(payload: dict) -> dict | None:
    """Get (action, repo, pr_number) from a GitHub pull_request payload.

    Triggers on opened / synchronize / ready_for_review / reopened — and on
    `edited` when the TITLE changed, so a pull request skipped for a title
    keyword ("WIP") is reviewed once the keyword is removed. The same-commit
    check keeps every other title edit quiet.
    """
    action = payload.get("action")
    if action == "edited":
        if "title" not in (payload.get("changes") or {}):
            return None
    elif action not in ("opened", "synchronize", "ready_for_review", "reopened"):
        return None
    pr = payload.get("pull_request") or {}
    repo = (payload.get("repository") or {}).get("full_name")
    if not pr or not repo:
        return None
    if action == "edited" and pr.get("state") not in (None, "open"):
        return None  # a title fixed on a closed pull request needs no review
    return {
        "provider": "github",
        "repo": repo,
        "number": int(pr.get("number") or 0),
        "head_sha": str((pr.get("head") or {}).get("sha") or ""),
        "action": action,
        "title": pr.get("title"),
    }


def _extract_github_push(payload: dict) -> dict | None:
    """(repo, ref, after) from a GitHub push, or None when it is not ours.

    Branch pushes only. A tag push carries `refs/tags/…` and does not move the
    code the index is built from; a branch DELETION arrives with an `after` of
    forty zeroes and nothing to index. Both are silently not-our-event rather
    than errors — a webhook that 400s on a tag push looks broken in the
    provider's delivery list, and an operator reading red rows stops trusting
    the green ones.

    Which branch is not decided here: the sweep resolves the repo's tracked
    branch when it checks. Passing every branch push through and letting the
    check compare against the tracked ref keeps one definition of "the branch
    we index" instead of two that can disagree.
    """
    ref = str(payload.get("ref") or "")
    if not ref.startswith("refs/heads/"):
        return None
    after = str(payload.get("after") or "")
    if not after or set(after) == {"0"}:
        return None
    repo = (payload.get("repository") or {}).get("full_name")
    if not repo:
        return None
    return {"repo": repo, "ref": ref, "after": after}


def _extract_bitbucket_push(payload: dict) -> dict | None:
    """(repo, ref, after) from a Bitbucket `repo:push`, or None when it is not ours.

    The same rule as :func:`_extract_github_push`, in Bitbucket's shape: one
    delivery carries `push.changes[]`, each with the `new` state of a ref. Only
    a BRANCH that still exists counts. A tag has `new.type == "tag"`; a deleted
    branch arrives with `new: null` and nothing to index. The first branch
    change wins — which branch the index tracks is decided later, by the same
    freshness check the daily sweep uses.
    """
    repo = (payload.get("repository") or {}).get("full_name")
    if not repo:
        return None
    for change in ((payload.get("push") or {}).get("changes") or []):
        new = (change or {}).get("new")
        if not isinstance(new, dict) or new.get("type") != "branch":
            continue
        after = str((new.get("target") or {}).get("hash") or "")
        name = str(new.get("name") or "")
        if after and name:
            return {"repo": repo, "ref": f"refs/heads/{name}", "after": after}
    return None


def _extract_gitlab_mr(payload: dict) -> dict | None:
    """Get (action, project, mr_iid) from a GitLab merge_request hook."""
    if payload.get("object_kind") != "merge_request":
        return None
    attrs = payload.get("object_attributes") or {}
    mr_action = attrs.get("action")
    if mr_action not in ("open", "reopen", "update"):
        return None
    project = (payload.get("project") or {}).get("path_with_namespace")
    if not project:
        return None
    return {
        "provider": "gitlab",
        "repo": project,
        "number": int(attrs.get("iid") or 0),
        "head_sha": str((attrs.get("last_commit") or {}).get("id") or ""),
        "action": mr_action,
        "title": attrs.get("title"),
    }


def _extract_bitbucket_pr(payload: dict, event_key: str) -> dict | None:
    """Bitbucket — events 'pullrequest:created' / 'pullrequest:updated'."""
    if event_key not in ("pullrequest:created", "pullrequest:updated"):
        return None
    pr = payload.get("pullrequest") or {}
    repo = (payload.get("repository") or {}).get("full_name")
    if not pr or not repo:
        return None
    head_sha = (
        ((pr.get("source") or {}).get("commit") or {}).get("hash") or ""
    )
    return {
        "provider": "bitbucket",
        "repo": repo,
        "number": int(pr.get("id") or 0),
        "head_sha": str(head_sha),
        "action": event_key,
        "title": pr.get("title"),
    }


# ─── PR lifecycle (merged / closed / reopened) ─────────────────
#
# Not review triggers: these feed `review_pull_requests.state`, which is how
# the analytics page can say "found by Celmis, merged anyway" and how a PR
# closed unmerged resolves its open issues. Each returns None for anything
# that is not a lifecycle change.


def _extract_github_pr_state(payload: dict) -> dict | None:
    action = payload.get("action")
    if action not in ("closed", "reopened"):
        return None
    pr = payload.get("pull_request") or {}
    repo = (payload.get("repository") or {}).get("full_name")
    if not pr or not repo:
        return None
    state = (
        "open" if action == "reopened"
        else "merged" if pr.get("merged")
        else "closed"
    )
    return {
        "provider": "github", "repo": repo, "number": int(pr.get("number") or 0),
        "state": state, "title": pr.get("title"),
        "author": (pr.get("user") or {}).get("login"), "url": pr.get("html_url"),
        "base_ref": (pr.get("base") or {}).get("ref"),
    }


def _extract_gitlab_mr_state(payload: dict) -> dict | None:
    if payload.get("object_kind") != "merge_request":
        return None
    attrs = payload.get("object_attributes") or {}
    state = {"merge": "merged", "close": "closed", "reopen": "open"}.get(
        str(attrs.get("action") or ""))
    project = (payload.get("project") or {}).get("path_with_namespace")
    if state is None or not project:
        return None
    return {
        "provider": "gitlab", "repo": project, "number": int(attrs.get("iid") or 0),
        "state": state, "title": attrs.get("title"),
        "author": (payload.get("user") or {}).get("username"), "url": attrs.get("url"),
        "base_ref": attrs.get("target_branch"),
    }


def _extract_bitbucket_pr_state(payload: dict, event_key: str) -> dict | None:
    state = {"pullrequest:fulfilled": "merged",
             "pullrequest:rejected": "closed"}.get(event_key)
    pr = payload.get("pullrequest") or {}
    repo = (payload.get("repository") or {}).get("full_name")
    if state is None or not pr or not repo:
        return None
    return {
        "provider": "bitbucket", "repo": repo, "number": int(pr.get("id") or 0),
        "state": state, "title": pr.get("title"),
        "author": (pr.get("author") or {}).get("nickname")
                  or (pr.get("author") or {}).get("display_name"),
        "url": ((pr.get("links") or {}).get("html") or {}).get("href"),
        "base_ref": (((pr.get("destination") or {}).get("branch") or {}).get("name")),
    }


#: Bitbucket events that only say "this PR changed": the productivity sync marks
#: the PR stale for its next tick (no API call). The merge/decline events go
#: through `_dispatch_pr_state` instead.
_BITBUCKET_TOUCH_EVENTS = {
    "pullrequest:created": "created", "pullrequest:updated": "updated",
    "pullrequest:approved": "approved", "pullrequest:unapproved": "unapproved",
}


def _extract_bitbucket_pr_touch(payload: dict, event_key: str) -> dict | None:
    event = _BITBUCKET_TOUCH_EVENTS.get(event_key)
    pr = payload.get("pullrequest") or {}
    repo = (payload.get("repository") or {}).get("full_name")
    if event is None or not pr or not repo or not pr.get("id"):
        return None
    return {"provider": "bitbucket", "repo": repo, "number": int(pr["id"]), "event": event}


async def _dispatch_pr_touch(info: dict, *, expected_workspace_id: str | None) -> None:
    """Tell the productivity sync a PR changed. Same tenant binding as `_dispatch_pr_state`. Never raises."""
    try:
        from src.api.auto_review import get_auto_review_store
        cfg = get_auto_review_store().config_for_repo(info["provider"], info["repo"])
        if cfg is None or (
                expected_workspace_id is not None and cfg.workspace_id != expected_workspace_id):
            return
        from src.productivity.sync import on_lifecycle_event
        await asyncio.to_thread(
            on_lifecycle_event, cfg.workspace_id, info["provider"], cfg.full_name,
            info["number"], info["event"], user_id=cfg.user_id, repo_slug=cfg.repo_slug)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pr_touch_dispatch_failed repo=%s err=%s", info.get("repo"), exc)


async def _dispatch_pr_state(
    info: dict, *, expected_workspace_id: str | None,
) -> None:
    """Record a PR's lifecycle change under the repo's ONE workspace.

    The same tenant binding `_dispatch_review` uses, and the same fail-closed
    rule: a repo bound to no workspace, or to several, records nothing, and a
    delivery signed for one workspace cannot write another's PR. Never raises.
    """
    try:
        from src.api.auto_review import get_auto_review_store
        cfg = get_auto_review_store().config_for_repo(info["provider"], info["repo"])
        if cfg is None:
            logger.info("pr_state_no_workspace_binding provider=%s repo=%s",
                        info["provider"], info["repo"])
            return
        if expected_workspace_id is not None and cfg.workspace_id != expected_workspace_id:
            logger.warning(
                "pr_state_workspace_mismatch url_ws=%s bound_ws=%s repo=%s",
                expected_workspace_id, cfg.workspace_id, info["repo"])
            return
        from src.review.issues import record_pr_state
        await asyncio.to_thread(
            record_pr_state, workspace_id=cfg.workspace_id,
            provider=info["provider"], repo=info["repo"], number=info["number"],
            state=info["state"], title=info.get("title"),
            author=info.get("author"), url=info.get("url"),
            base_ref=info.get("base_ref") or None,
        )
        if info["state"] == "merged":
            await _schedule_backlog_recheck(
                cfg.workspace_id, info["provider"], info["repo"],
                info.get("base_ref"), merged_pr=info["number"])
        # Productivity history: a merge or close queues a single-PR refresh
        # (when the repository opted in). Never raises.
        from src.productivity.sync import on_lifecycle_event
        await asyncio.to_thread(
            on_lifecycle_event, cfg.workspace_id, info["provider"], cfg.full_name,
            info["number"], {"merged": "merged", "closed": "closed"}.get(info["state"], "reopened"),
            user_id=cfg.user_id, repo_slug=cfg.repo_slug)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pr_state_dispatch_failed repo=%s err=%s",
                       info.get("repo"), exc)


async def _schedule_backlog_recheck(
    workspace_id: str, provider: str, repo: str, base_ref: str | None, *,
    merged_pr: int | None = None, reason: str = "merge",
) -> None:
    """A branch moved: look at its issues backlog soon (debounced; a burst of
    merges is one recheck). The branch is the webhook's word, else the one the
    ledger stored for the PR. Never raises."""
    try:
        from src.review import issues as ledger
        from src.review.issue_resolver import has_backlog, schedule_recheck

        branch = base_ref
        if not branch and merged_pr:
            branch = await asyncio.to_thread(
                ledger.pr_base_ref, workspace_id=workspace_id, provider=provider,
                repo=repo, number=merged_pr)
        if not branch:
            return
        if not await asyncio.to_thread(has_backlog, workspace_id, provider, repo, branch):
            return
        await schedule_recheck(workspace_id, provider, repo, branch,
                               reason=reason, merged_pr=merged_pr)
    except Exception as exc:  # noqa: BLE001
        logger.warning("issue_recheck_trigger_failed repo=%s err=%s", repo, exc)


async def _dispatch_issue_recheck(
    provider: str, repo: str, branch: str, *, expected_workspace_id: str | None,
) -> None:
    """A push to `branch`: recheck its backlog if it has one. Same tenant
    binding as the other dispatchers: an unbound or mismatched repo does
    nothing. Never raises."""
    try:
        from src.api.auto_review import get_auto_review_store

        cfg = get_auto_review_store().config_for_repo(provider, repo)
        if cfg is None:
            return
        if expected_workspace_id is not None and cfg.workspace_id != expected_workspace_id:
            logger.warning("issue_recheck_workspace_mismatch url_ws=%s bound_ws=%s repo=%s",
                           expected_workspace_id, cfg.workspace_id, repo)
            return
        await _schedule_backlog_recheck(
            cfg.workspace_id, provider, repo, branch, reason="push")
    except Exception as exc:  # noqa: BLE001
        logger.warning("issue_recheck_dispatch_failed repo=%s err=%s", repo, exc)


# ─── Background review dispatch ────────────────────────────────


#: One freshness check per repository at a time, and a bound on all of them.
#:
#: A push webhook fires per push, and a busy morning or a force-push storm
#: arrives as a burst. Without this, each one became an unreferenced
#: `asyncio.create_task` running its own `git ls-remote`: N concurrent git
#: processes and N requests to one provider, which is how a webhook earns a
#: rate limit for the whole workspace. The per-repo lock also collapses the
#: five pushes of one branch being force-pushed into one check.
_REFRESH_GATE = asyncio.Semaphore(4)
_REFRESH_INFLIGHT: set[str] = set()


async def _dispatch_refresh(
    provider: str, full_name: str, *, expected_workspace_id: str | None,
) -> None:
    """A push landed — ask the remote and re-index if it moved.

    Goes through the same `check_repo` the daily sweep uses rather than
    enqueueing an index straight from the payload. The push tells us something
    changed; it does not tell us the branch this instance tracks, and two
    routes deciding that separately is how they come to disagree.

    Never raises: this runs in a fire-and-forget task, so an exception here
    reaches nobody and would only surface as an unhandled-task warning.
    """
    key = f"{provider}:{full_name}"
    if key in _REFRESH_INFLIGHT:
        # A check for this repository is already running and will read the
        # branch as it is now — including this push. A second one would ask
        # the same question and get the same answer.
        logger.info("push_refresh_already_running repo=%s", full_name)
        return
    _REFRESH_INFLIGHT.add(key)
    try:
        from src.api.auto_review import get_auto_review_store
        from src.repos.freshness import check_repo

        store = get_auto_review_store()
        workspace_id = expected_workspace_id or store.workspace_for_repo(provider, full_name)
        if not workspace_id:
            logger.info("push_refresh_no_workspace repo=%s", full_name)
            return
        cfg = next(
            (c for c in store.list_for_workspace(workspace_id)
             if c.full_name.lower() == full_name.lower() and c.provider == provider),
            None,
        )
        if cfg is None:
            logger.info("push_refresh_unregistered repo=%s ws=%s", full_name, workspace_id)
            return
        async with _REFRESH_GATE:
            result = await asyncio.to_thread(
                check_repo, cfg.repo_slug, workspace_id=workspace_id,
                user_id=getattr(cfg, "user_id", "default") or "default",
            )
        logger.info("push_refresh repo=%s state=%s queued=%s",
                    cfg.repo_slug, result.state, bool(result.reindex_job_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("push_refresh_failed repo=%s err=%s", full_name, exc)
    finally:
        _REFRESH_INFLIGHT.discard(key)


async def _reviews_drafts(provider_name: str, repo: str) -> bool:
    """`run_on_drafts` for the repository a delivery names. The lookup is
    blocking (store + database), so it runs off the event loop; it never
    raises and answers False when it cannot tell — the old behaviour."""
    from src.review.review_defaults import run_on_drafts_for_repo

    return await asyncio.to_thread(run_on_drafts_for_repo, provider_name, repo)


#: Deliveries that fire for ANY change to the pull request, not only a push:
#: Bitbucket's `pullrequest:updated` and GitLab's `update` also arrive when the
#: title or description is edited. Those are the ones the same-commit check
#: applies to. GitHub's `edited` (a title change only) is one too; opened /
#: reopened / ready_for_review mean somebody wants a review now.
_ANY_CHANGE_EVENTS = frozenset({
    ("github", "edited"),
    ("bitbucket", "pullrequest:updated"),
    ("gitlab", "update"),
})


def _same_commit_already_reviewed(
    provider_name: str, repo: str, pr_number: int, head_sha: str, workspace_id: str,
) -> bool:
    """True when this commit of the PR already has a finished review that was
    POSTED: `pr_state.last_reviewed_sha` is the head delivered.

    What stops a description edit from starting another review — and, with
    `summary_target=description`, what stops Celmis's own description write
    from doing it forever: every PUT fires `pullrequest:updated`, which would
    queue a review, which writes the description again.

    Only a complete run whose comments went up moves `last_reviewed_sha`. A
    dry run, a run whose posting failed, a partial run, a skipped or failed
    one, a different head or a PR never seen does not, so the delivery is
    reviewed: nothing was ever shown on the pull request for that commit.
    Fails OPEN: if the database cannot answer, the review runs — a duplicate
    is cheaper than a lost one.
    """
    if not head_sha:
        return False
    try:
        from src.review import pr_state

        state = pr_state.load(workspace_id, provider_name, repo, pr_number)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "same_commit_check_failed provider=%s repo=%s pr=%d err=%s",
            provider_name, repo, pr_number, exc,
        )
        return False
    return bool(state and _same_sha(state.last_reviewed_sha, head_sha))


def _same_sha(stored: str | None, delivered: str) -> bool:
    """Two commit ids of one commit (see `pr_state.same_sha`)."""
    from src.review.pr_state import same_sha

    return same_sha(stored, delivered)


def _bitbucket_pr_meta(payload: dict) -> dict:
    """What a Bitbucket pull request delivery says about the PR, for the gates
    and for the run rows they write."""
    pr = payload.get("pullrequest") or {}
    return {
        "title": pr.get("title"),
        "author": (pr.get("author") or {}).get("nickname")
                  or (pr.get("author") or {}).get("display_name"),
        "url": ((pr.get("links") or {}).get("html") or {}).get("href"),
        "head_ref": (((pr.get("source") or {}).get("branch")) or {}).get("name"),
        "base_ref": (((pr.get("destination") or {}).get("branch")) or {}).get("name"),
    }


def _cadence_reason(decision, registered, gates) -> str:
    """The sentence the run row of a cadence skip carries — the orchestrator
    gate's own text, so the two write one reason and `record_gate_skip` folds
    repeats."""
    from src.review import cadence as cadence_mod

    sentence = cadence_mod.gate_reason(
        decision, "en", handle=cadence_mod.bot_handle(),
        pushes=int(gates["auto_pause_pushes"]),
        minutes=int(gates["auto_pause_window_minutes"]),
        reason=registered.paused_reason)
    return sentence[:1].upper() + sentence[1:] + "."


async def _dispatch_review(
    provider_name: str,
    repo: str,
    pr_number: int,
    *,
    post_comments: bool = True,
    head_sha: str = "",
    expected_workspace_id: str | None = None,
    skip_reason: str | None = None,
    pr_meta: dict | None = None,
    event: str = "",
) -> None:
    """Enqueue the review as a durable job. If the sync queue fails,
    fall back to inline dispatch (legacy behaviour) so a broken DB
    connection doesn't drop the webhook.

    `skip_reason` ("draft") is a delivery the handler already decided not to
    review. It still comes through here, after the tenant checks, so the
    skip is recorded as a run in the right workspace — a draft that left no
    trace read, on the pull-requests page, exactly like a webhook that never
    arrived. A draft whose base branch the target patterns exclude is
    recorded as that skip instead: marking it ready would not get it reviewed.
    """
    # Derive the tenant from the repo — the webhook is unauthenticated, so this
    # is the ONLY tenant binding. workspace_for_repo returns None when the repo
    # is unknown OR bound to more than one workspace; in both cases we FAIL
    # CLOSED (skip) rather than guess and run under the wrong tenant's keys.
    # Nothing is recorded for these: there is no workspace to record it in.
    from src.api.auto_review import get_auto_review_store
    cfg = get_auto_review_store().config_for_repo(provider_name, repo)
    if cfg is None:
        logger.warning(
            "webhook_no_workspace_binding provider=%s repo=%s pr=%d — skipping "
            "review (repo is not bound to exactly one workspace; fail closed)",
            provider_name, repo, pr_number,
        )
        return
    workspace_id = cfg.workspace_id

    # The URL's workspace and the repo's workspace must be the SAME workspace.
    #
    # Per-workspace secrets stop tenant A signing for tenant B's URL, and this
    # closes the other half. A's only remaining move is to POST a payload
    # naming B's repository to A's OWN url, signed with A's OWN valid secret.
    # The signature checks out, and without this line the delivery flows on
    # with workspace_id=B — so the review runs on B's provider token, spends
    # B's LLM budget and comments as B, on a request A composed.
    #
    # Checked BEFORE anything is recorded: a skipped run written into B's
    # history on A's say-so would be the same hole, smaller.
    #
    # `None` is the legacy un-suffixed route: it has no workspace in the URL to
    # compare, so there is nothing to assert and the binding stands alone, as
    # it always did.
    if expected_workspace_id is not None and workspace_id != expected_workspace_id:
        logger.warning(
            "webhook_workspace_mismatch url_ws=%s bound_ws=%s provider=%s "
            "repo=%s pr=%d — skipping (a delivery may only trigger reviews for "
            "repos bound to the workspace whose secret signed it)",
            expected_workspace_id, workspace_id, provider_name, repo, pr_number,
        )
        return

    from src.review.dispatch import execute_review, record_gate_skip

    # A delivery is not permission. Auto-review off means off, whoever POSTs.
    #
    # The dispatcher used to consult only the repo→workspace binding, so a
    # webhook left installed after somebody switched auto-review off kept
    # spending that workspace's model budget on every pull request. The
    # binding row survives the toggle — that is what the toggle toggles — so
    # "the repo is known" was never the same question as "the owner wants
    # this". The skip is recorded, so the PR shows why it was not reviewed.
    if not cfg.enabled:
        logger.info(
            "webhook_auto_review_disabled provider=%s repo=%s pr=%d ws=%s — "
            "skipping (the repo is bound but auto-review is switched off)",
            provider_name, repo, pr_number, workspace_id,
        )
        await asyncio.to_thread(
            record_gate_skip, provider_name, repo, pr_number,
            user_id=cfg.user_id, workspace_id=workspace_id, source="webhook",
            gate_key="gate_enabled", gate_name="Check review is enabled",
            reason=("Auto-review disabled: automatic review is switched off for "
                    "this repository."),
            pr_meta=pr_meta,
        )
        return

    # A pull request into a branch the target patterns leave out is never
    # reviewed — a draft included (marking it ready would not get it
    # reviewed). Recorded in the orchestrator gate's own words (one matcher,
    # src/review/branch_patterns.py) instead of queuing a job that would only
    # end at that gate. Only when the delivery named the base branch; without
    # it the orchestrator's gate stays the authority.
    base_ref = str((pr_meta or {}).get("base_ref") or "")
    if base_ref:
        from src.review.branch_patterns import branch_targeted, skip_sentence
        from src.review.review_defaults import target_branches_for_repo

        patterns = await asyncio.to_thread(
            target_branches_for_repo, provider_name, repo)
        if patterns and not branch_targeted(base_ref, patterns):
            logger.info(
                "webhook_skipped reason=branch_not_targeted provider=%s "
                "repo=%s pr=%d base=%s ws=%s",
                provider_name, repo, pr_number, base_ref, workspace_id)
            await asyncio.to_thread(
                record_gate_skip, provider_name, repo, pr_number,
                user_id=cfg.user_id, workspace_id=workspace_id, source="webhook",
                gate_key="gate_target_branch", gate_name="Validate target branch",
                reason=skip_sentence(base_ref, patterns),
                pr_meta=pr_meta,
            )
            return

    if skip_reason == "draft":
        logger.info("webhook_draft_skipped provider=%s repo=%s pr=%d ws=%s",
                    provider_name, repo, pr_number, workspace_id)
        await asyncio.to_thread(
            record_gate_skip, provider_name, repo, pr_number,
            user_id=cfg.user_id, workspace_id=workspace_id, source="webhook",
            gate_key="gate_draft", gate_name="Check draft status",
            reason=("Draft: the pull request is a draft; it is reviewed once it "
                    "is marked ready."),
            pr_meta=pr_meta,
        )
        return

    # The title and cadence gates, answered here from the same rows the
    # orchestrator's gates read, so a PR they would skip never costs a job.
    # One settings read for both; unreadable is the built-in (review).
    from src.review import cadence as cadence_mod
    from src.review.review_defaults import gate_settings_for_repo
    from src.review.scope import title_keyword_match

    gates = await asyncio.to_thread(gate_settings_for_repo, provider_name, repo)
    matched = title_keyword_match(
        (pr_meta or {}).get("title"), gates["ignored_title_keywords"])
    if matched:
        logger.info(
            "webhook_skipped reason=title_keyword provider=%s repo=%s pr=%d "
            "keyword=%s ws=%s", provider_name, repo, pr_number, matched,
            workspace_id)
        sentence = messages.t("gate.title", "en", keyword=matched)
        await asyncio.to_thread(
            record_gate_skip, provider_name, repo, pr_number,
            user_id=cfg.user_id, workspace_id=workspace_id, source="webhook",
            gate_key="gate_title", gate_name="Check title keywords",
            reason=sentence[:1].upper() + sentence[1:] + ".",
            pr_meta=pr_meta,
        )
        return

    # A delivery that only says "something about this PR changed" for a commit
    # that is already reviewed is a description or title edit: nothing to read.
    # Quiet on purpose — no run row, since nothing was skipped by a gate.
    if (provider_name, event) in _ANY_CHANGE_EVENTS and await asyncio.to_thread(
        _same_commit_already_reviewed,
        provider_name, repo, pr_number, head_sha, workspace_id,
    ):
        logger.info(
            "webhook_skipped reason=same_commit provider=%s repo=%s pr=%d "
            "head=%s ws=%s", provider_name, repo, pr_number, head_sha[:12],
            workspace_id,
        )
        return

    # Count the push, then ask the cadence. Fails OPEN: a PR whose state
    # cannot be written is reviewed, as it was before cadence existed.
    cadence_name = str(gates["review_cadence"])
    try:
        from src.review import pr_state

        registered = await asyncio.to_thread(
            pr_state.register_push, workspace_id, provider_name, repo, pr_number,
            head_sha, cadence_name=cadence_name,
            limit=int(gates["auto_pause_pushes"]),
            window_minutes=int(gates["auto_pause_window_minutes"]),
            meta=pr_meta,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("push_register_failed provider=%s repo=%s pr=%d err=%s",
                       provider_name, repo, pr_number, exc)
        registered = None
    if registered is not None:
        decision = cadence_mod.decide(
            cadence_name, registered.paused, registered.paused_reason)
        if decision.action == "skip":
            logger.info(
                "webhook_skipped reason=%s provider=%s repo=%s pr=%d ws=%s",
                decision.code, provider_name, repo, pr_number, workspace_id)
            # The orchestrator's gate posts the one note that says how to
            # resume; every later delivery is only written down.
            if not (registered.notice_pending and gates["status_feedback"]):
                await asyncio.to_thread(
                    record_gate_skip, provider_name, repo, pr_number,
                    user_id=cfg.user_id, workspace_id=workspace_id,
                    source="webhook", gate_key="gate_cadence",
                    gate_name="Check review cadence",
                    reason=_cadence_reason(decision, registered, gates),
                    pr_meta=pr_meta,
                )
                return

    from src.review.stages import now_iso

    payload = {
        "provider": provider_name,
        "repo": repo,
        "pr_number": pr_number,
        "post_comments": post_comments,
        "workspace_id": workspace_id,
        # The config row's owner, never the literal "default".
        #
        # This one string was the whole of why automatic review did not
        # work. `resolve_auth(user_id, workspace_id)` looks for a
        # personal Claude credential keyed by user id and falls back to
        # `ws:{workspace_id}`; "default" matches neither, so a
        # workspace whose Claude account is connected personally — the
        # default in the UI — failed every webhook review in 0.06s and
        # posted "this pull request has NOT been reviewed" to the PR.
        # The poller never had the bug: it passes `cfg.user_id`.
        "user_id": cfg.user_id,
        # Who asked and when — the run's "Review started" and "Queued"
        # stages are written from these when the worker picks the job up.
        "source": "webhook",
        "enqueued_at": now_iso(),
    }
    try:
        from src.sync.queue import KIND_REVIEW, enqueue
        # Dedup key matches the poller's exactly (no head_sha), so a PR spotted
        # by both webhook AND poller produces exactly one queued review.
        dedup = f"review:{provider_name}:{repo}#{pr_number}"
        enqueue(
            kind=KIND_REVIEW,
            payload=payload,
            dedup_key=dedup,
            enqueued_by=f"webhook:{provider_name}",
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "enqueue_failed_falling_back_inline provider=%s pr=%d err=%s",
            provider_name, pr_number, exc,
        )

    # Inline fallback — the worker's own body, so the run is recorded with its
    # stages exactly as a queued one would be.
    logger.info(
        "review_dispatch_start provider=%s repo=%s pr=%d workspace=%s",
        provider_name, repo, pr_number, workspace_id,
    )
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: execute_review(payload))
        logger.info("review_dispatch_done provider=%s repo=%s pr=%d",
                    provider_name, repo, pr_number)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "review_dispatch_unhandled provider=%s repo=%s pr=%d err=%s",
            provider_name, repo, pr_number, exc,
        )

# ─── Comment commands (`@celmis ...`) ──────────────────────────
#
# A comment webhook is the cheapest delivery there is to abuse: anybody who can
# comment on a pull request produces one. So the receiver stays small. The
# delivery is verified like every other (signature or token, then dedup), the
# extractor turns it into a `CommentEvent`, and a text filter drops everything
# that does not name the bot before any task is created. What is left is
# bound to ONE workspace through the repo (fail closed, and the URL's
# workspace must agree, exactly as for a review) and handed to
# `commands.handlers.accept`, which claims the comment in the ledger and
# applies the rate limits. The answer is produced by a queue job.
#
# `cfg.enabled` (auto-review) is deliberately NOT consulted: a person asking
# for a review in a comment is not the automatic review that switch governs.
# The repository's `commands_enabled` setting is the switch for commands.


def _resolve_tenant(
    provider_name: str, repo: str, pr_number: int, expected_workspace_id: str | None,
):
    """The auto-review config of the ONE workspace this repo is bound to, or
    None — with the reason logged — when the repo is bound to none or several
    workspaces, or to a workspace other than the one whose secret signed the
    delivery. The tenant binding every dispatcher shares."""
    from src.api.auto_review import get_auto_review_store

    cfg = get_auto_review_store().config_for_repo(provider_name, repo)
    if cfg is None:
        logger.warning(
            "webhook_no_workspace_binding provider=%s repo=%s pr=%d — skipping "
            "(repo is not bound to exactly one workspace; fail closed)",
            provider_name, repo, pr_number,
        )
        return None
    if expected_workspace_id is not None and cfg.workspace_id != expected_workspace_id:
        logger.warning(
            "webhook_workspace_mismatch url_ws=%s bound_ws=%s provider=%s repo=%s "
            "pr=%d — skipping (a delivery may only act on repos bound to the "
            "workspace whose secret signed it)",
            expected_workspace_id, cfg.workspace_id, provider_name, repo, pr_number,
        )
        return None
    return cfg


def _command_candidate(ev, settings: ReviewSettings):
    """The parsed command when this comment is one the bot should look at;
    None for everything else. Pure text work, safe to run per delivery."""
    from src.review import markers
    from src.review.commands.parser import might_address_bot, parse_comment

    if ev is None or ev.actor_is_bot:
        return None
    if not might_address_bot(ev.body, settings.bot_handle):
        return None
    # Our own words (a reply, a summary) never start a command.
    if markers.is_bot_text(ev.body):
        return None
    return parse_comment(ev.body, settings.bot_handle)


def _may_be_feedback(ev, command) -> bool:
    """A reply that could be feedback on one of our findings: a human comment
    in a thread, written for nobody in particular (no command) or as a question
    to the bot. A named command (`review`, `remember`, ...) is never feedback.
    Pure text work, like `_command_candidate`."""
    from src.review.commands.parser import CHAT
    from src.review.learning.replies import is_reply_candidate

    return (command is None or command.name == CHAT) and is_reply_candidate(ev)


def _is_our_text(ev) -> bool:
    from src.review import markers

    return markers.is_bot_text(ev.body)


async def _dispatch_command(
    ev, command, *, expected_workspace_id: str | None,
) -> None:
    """Read the comment as feedback on a finding, then (unless it was) accept
    it as a command and queue its answer. Never raises.

    Feedback goes first: `@celmis dismiss` or a thumbs-down in a finding's
    thread parses as a question for the chat, which would swallow it. A reply
    the learning loop took is not also a chat question; one it read as a
    question goes to the chat even without the handle (a reply in the bot's own
    thread is addressed to it)."""
    try:
        cfg = _resolve_tenant(ev.provider, ev.repo, ev.pr_number, expected_workspace_id)
        if cfg is None:
            return
        if _may_be_feedback(ev, command):
            from src.review.commands.parser import CHAT, MAX_ARGS_CHARS, ParsedCommand
            from src.review.learning.receiver import learn_from_comment
            from src.review.learning.replies import written_text

            learned = await asyncio.to_thread(
                learn_from_comment, ev, workspace_id=cfg.workspace_id, user_id=cfg.user_id)
            if learned.handled:
                return
            if command is None:
                if learned.handoff != "chat":
                    return
                command = ParsedCommand(CHAT, args=written_text(ev.body)[:MAX_ARGS_CHARS])
        from src.review.commands.handlers import accept, execute

        payload = await asyncio.to_thread(
            accept, ev, command, workspace_id=cfg.workspace_id, user_id=cfg.user_id)
        if payload is None:
            return
        try:
            from src.sync.queue import KIND_PR_COMMAND, enqueue

            enqueue(
                kind=KIND_PR_COMMAND, payload=payload,
                dedup_key=f"cmd:{ev.provider}:{ev.repo}#{ev.pr_number}:{ev.comment_id}",
                enqueued_by=f"webhook:{ev.provider}", max_attempts=2,
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "command_enqueue_failed_falling_back_inline provider=%s pr=%d err=%s",
                ev.provider, ev.pr_number, type(exc).__name__)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: execute(payload))
    except Exception as exc:  # noqa: BLE001
        logger.warning("command_dispatch_failed provider=%s repo=%s err=%s",
                       getattr(ev, "provider", "?"), getattr(ev, "repo", "?"),
                       type(exc).__name__)


def _comment_response(
    ev, workspace_id: str | None, settings: ReviewSettings, stats: dict,
) -> JSONResponse:
    """The receiver's answer to a comment delivery, and the dispatch behind it."""
    if ev is None:
        return JSONResponse({"status": "ignored", "reason": "not a pull request comment"})
    command = _command_candidate(ev, settings)
    if command is None:
        # Not a command, but possibly the answer to one of our findings: that is
        # read in the background and never changes this response.
        if _may_be_feedback(ev, None) and not _is_our_text(ev):
            asyncio.create_task(_dispatch_command(
                ev, None, expected_workspace_id=workspace_id))
        return JSONResponse({"status": "ignored", "reason": "no command"})
    asyncio.create_task(_dispatch_command(
        ev, command, expected_workspace_id=workspace_id))
    stats["commands"] = stats.get("commands", 0) + 1
    return JSONResponse({"status": "accepted", "command": command.name}, status_code=202)


#: GitHub tells us a person resolved or reopened a review thread; the learning
#: loop reads it as a weak signal on the finding the thread carries.
GITHUB_THREAD_EVENT = "pull_request_review_thread"


async def _dispatch_thread_event(ev, *, expected_workspace_id: str | None) -> None:
    """Hand a resolved / reopened thread to the learning loop. Never raises."""
    try:
        cfg = _resolve_tenant(ev.provider, ev.repo, ev.pr_number, expected_workspace_id)
        if cfg is None:
            return
        from src.review.learning.receiver import learn_from_thread

        await asyncio.to_thread(
            learn_from_thread, ev, workspace_id=cfg.workspace_id, user_id=cfg.user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("thread_event_dispatch_failed provider=%s err=%s",
                       getattr(ev, "provider", "?"), type(exc).__name__)


def _thread_response(payload: object, workspace_id: str | None) -> JSONResponse:
    from src.review.learning.resolve import extract_github_thread_event

    ev = extract_github_thread_event(payload) if isinstance(payload, dict) else None
    if ev is None or ev.actor_is_bot:
        return JSONResponse({"status": "ignored", "reason": "not a thread we read"})
    asyncio.create_task(_dispatch_thread_event(ev, expected_workspace_id=workspace_id))
    return JSONResponse({"status": "accepted", "thread": "resolved" if ev.resolved else "reopened"},
                        status_code=202)


# ─── FastAPI app factory ───────────────────────────────────────


def resolved_review_settings() -> dict:
    """The deadlines and budgets AS THIS PROCESS RESOLVED THEM.

    Every one is enforced and every one is settable — but `env_file` points at
    a `.env` that does not exist inside the container, so the only route in is
    a name listed in docker-compose's `environment:` block. A setting the
    deployment does not forward silently takes the code default, and there was
    no way to tell from outside which had happened. That is not hypothetical:
    the compose file forwarded one budget under its pre-rename spelling with a
    hardcoded 300, so an installation could run the number a measurement
    retired while the code said 900.

    BEHIND AN ADMIN NOW. This used to be the body of the webhook sub-app's
    `/healthz`, and those routes are copied into the main app — so it answered
    on the public `/backend/healthz`, ahead of the plain one, to anybody. The
    old comment ended "the endpoint is already public", which was true and was
    the problem: model names, deadlines, cache size and which backends are
    configured are a map of the installation. Values, never secrets — and a
    map is worth having anyway.
    """
    from src.review.settings import get_review_settings

    settings = get_review_settings()
    return {
                "has_s3": settings.has_s3,
                "has_redis": settings.has_redis,
                "hot_cache_size": settings.hot_cache_size,
                "defect_model": settings.defect_model,
                "contract_model": settings.contract_model,
                # THE DEADLINES AS THIS PROCESS RESOLVED THEM.
                #
                # Not decoration. Every one of these is now enforced, and
                # every one is settable — but `env_file` points at a `.env`
                # that does not exist inside the container, so the only route
                # in is a name listed in docker-compose's `environment:`
                # block. A setting the deployment does not forward silently
                # takes the code default, and there was no way to tell from
                # outside which had happened.
                #
                # That is not hypothetical: the compose file forwards this
                # budget under its pre-rename spelling with a hardcoded
                # default of 300, so an installation can be running the
                # number a measurement retired while the code says 900. This
                # block is how anyone finds out without shell access.
                #
                # Values, never secrets: these are integers an operator chose,
                # and the endpoint is already public.
                "timeout_seconds": settings.timeout_seconds,
                "llm_timeout_seconds": settings.llm_timeout_seconds,
                "llm_timeout_retry_factor": settings.llm_timeout_retry_factor,
                "max_diff_size_bytes": settings.max_diff_size_bytes,
                "cve_lookup_timeout_seconds": settings.cve_lookup_timeout_seconds,
                "verifier_enabled": settings.verifier_enabled,
    }


def build_webhook_app(
    settings: ReviewSettings | None = None,
    *,
    dedup_backend=None,
) -> FastAPI:
    """Create FastAPI webhook receiver app."""
    settings = settings or get_review_settings()
    dedup = dedup_backend or _build_dedup(settings)

    app = FastAPI(
        title="code-analyzer review webhook",
        description="PR review webhook receiver — GitHub/GitLab/Bitbucket",
        version=__import__("src").__version__,
    )

    stats_counter = {
        "received": 0,
        "verified": 0,
        "deduped": 0,
        "dispatched": 0,
        "rejected": 0,
        "commands": 0,
    }

    @app.get("/healthz")
    async def healthz() -> dict:
        # NOT copied into the main app any more — see `_generated` in
        # src/api/main.py. This answered on the public /backend/healthz,
        # shadowing the plain one, and handed every model name, deadline
        # and budget to anybody who asked. The reason the block exists is
        # real and is kept: the same payload is served from
        # /api/ops/review-settings, behind an admin.
        return {"status": "ok", "review_settings": resolved_review_settings()}

    @app.get("/webhook/stats")
    async def webhook_stats() -> dict:
        return dict(stats_counter)

    # The tenant comes from the PATH, not the body.
    #
    # Verification has to happen before anything is parsed — that is the whole
    # point of a signature — so the workspace cannot be read out of the
    # payload. A query parameter would be worse than a path segment: FastAPI
    # would expose it on the un-suffixed route too, turning
    # `?workspace_id=other` into a tenant selector on the legacy URL.
    #
    # `None` means the legacy un-suffixed route, which resolves to the
    # instance-wide secret exactly as before.
    @app.post("/webhook/github/{workspace_id}")
    async def webhook_github(
        request: Request,
        workspace_id: str | None = None,
        x_hub_signature_256: str = Header(None),
        x_github_delivery: str = Header(None),
        x_github_event: str = Header(None),
    ) -> JSONResponse:
        stats_counter["received"] += 1
        body = await request.body()

        # 1. Verify signature
        secret = resolve_webhook_secret("github", workspace_id, settings)
        if not secret:
            logger.error("github_webhook_no_secret_configured workspace=%s",
                         workspace_id)
            stats_counter["rejected"] += 1
            raise HTTPException(500, "Webhook secret not configured")

        if not _verify_github_signature(body, x_hub_signature_256, secret):
            stats_counter["rejected"] += 1
            raise HTTPException(401, "Invalid signature")
        stats_counter["verified"] += 1

        # 2. Dedup
        if x_github_delivery and dedup.is_duplicate(f"gh:{x_github_delivery}"):
            stats_counter["deduped"] += 1
            return JSONResponse({"status": "duplicate"}, status_code=200)

        # 3. Filter event types
        #
        # `push` is not a review trigger — it is an INDEX trigger. A merge to
        # the tracked branch changes the code every later question is answered
        # from, and until this existed nothing noticed: the index held
        # whatever was there when somebody last pressed a button, and answered
        # from it without saying so.
        if x_github_event == "push":
            try:
                payload = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                raise HTTPException(400, "Invalid JSON") from None
            info = _extract_github_push(payload)
            if info is None:
                return JSONResponse({"status": "ignored", "reason": "not the tracked branch"})
            asyncio.create_task(_dispatch_refresh(
                "github", info["repo"], expected_workspace_id=workspace_id))
            asyncio.create_task(_dispatch_issue_recheck(
                "github", info["repo"], info["ref"][len("refs/heads/"):],
                expected_workspace_id=workspace_id))
            stats_counter["dispatched"] += 1
            return JSONResponse({"status": "accepted", **info}, status_code=202)

        if x_github_event in GITHUB_COMMENT_EVENTS:
            try:
                payload = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                raise HTTPException(400, "Invalid JSON") from None
            return _comment_response(
                extract_github_comment(
                    payload, x_github_event, delivery=x_github_delivery or ""),
                workspace_id, settings, stats_counter)

        if x_github_event == GITHUB_THREAD_EVENT:
            try:
                payload = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                raise HTTPException(400, "Invalid JSON") from None
            return _thread_response(payload, workspace_id)

        if x_github_event != "pull_request":
            return JSONResponse({"status": "ignored", "event": x_github_event})

        # 4. Parse + dispatch
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            # The decoder's offset is noise to a webhook sender; 400 is the answer.
            raise HTTPException(400, "Invalid JSON") from None

        state_info = _extract_github_pr_state(payload)
        if state_info is not None:
            asyncio.create_task(_dispatch_pr_state(
                state_info, expected_workspace_id=workspace_id))

        pr_info = _extract_github_pr(payload)
        if pr_info is None:
            if state_info is not None:
                return JSONResponse({"status": "recorded", "state": state_info["state"]})
            return JSONResponse({"status": "ignored", "reason": "non-trigger action"})

        # Skip drafts if action=opened (drafts trigger ready_for_review later)
        # — unless the repository reviews drafts (`run_on_drafts`, repo >
        # workspace > built-in False). Asked only of a draft, so every other
        # delivery costs what it did; an unbound repo answers False.
        gh_pr = payload.get("pull_request") or {}
        is_draft = gh_pr.get("draft", False)
        if (is_draft and pr_info["action"] == "opened"
                and not await _reviews_drafts("github", pr_info["repo"])):
            # Recorded as a skipped run (after the tenant checks), so the
            # pull-requests page says "Skipped — draft" instead of nothing.
            asyncio.create_task(_dispatch_review(
                "github", pr_info["repo"], pr_info["number"],
                expected_workspace_id=workspace_id, skip_reason="draft",
                pr_meta={
                    "title": gh_pr.get("title"),
                    "author": (gh_pr.get("user") or {}).get("login"),
                    "url": gh_pr.get("html_url"),
                    "head_ref": (gh_pr.get("head") or {}).get("ref"),
                    "base_ref": (gh_pr.get("base") or {}).get("ref"),
                },
            ))
            return JSONResponse({"status": "skipped", "reason": "draft PR"})

        asyncio.create_task(_dispatch_review(
            "github", pr_info["repo"], pr_info["number"],
            head_sha=pr_info.get("head_sha", ""),
            expected_workspace_id=workspace_id,
            event=pr_info["action"],
            pr_meta={
                "title": gh_pr.get("title"),
                "author": (gh_pr.get("user") or {}).get("login"),
                "url": gh_pr.get("html_url"),
                "head_ref": (gh_pr.get("head") or {}).get("ref"),
                "base_ref": (gh_pr.get("base") or {}).get("ref"),
            },
        ))
        stats_counter["dispatched"] += 1
        return JSONResponse(
            {"status": "accepted", **pr_info}, status_code=202,
        )

    # The tenant comes from the PATH, not the body.
    #
    # Verification has to happen before anything is parsed — that is the whole
    # point of a signature — so the workspace cannot be read out of the
    # payload. A query parameter would be worse than a path segment: FastAPI
    # would expose it on the un-suffixed route too, turning
    # `?workspace_id=other` into a tenant selector on the legacy URL.
    #
    # `None` means the legacy un-suffixed route, which resolves to the
    # instance-wide secret exactly as before.
    # The un-suffixed URL, kept working.
    #
    # A deployment that registered `/webhook/github` before per-workspace
    # secrets existed keeps delivering to it, and `workspace_id=None` resolves
    # to the instance-wide secret exactly as it always did. Written as its own
    # function rather than a second decorator on the one above: with a single
    # function FastAPI sees `workspace_id` as a parameter absent from the
    # un-suffixed path and exposes it as a QUERY parameter, so
    # `?workspace_id=someone-else` becomes a tenant selector on the legacy URL.
    @app.post("/webhook/github")
    async def webhook_github_legacy(
        request: Request,
        x_hub_signature_256: str = Header(None),
        x_github_delivery: str = Header(None),
        x_github_event: str = Header(None),
    ) -> JSONResponse:
        return await webhook_github(
            request,
            workspace_id=None,
            x_hub_signature_256=x_hub_signature_256,
            x_github_delivery=x_github_delivery,
            x_github_event=x_github_event,
        )

    @app.post("/webhook/gitlab/{workspace_id}")
    async def webhook_gitlab(
        request: Request,
        workspace_id: str | None = None,
        x_gitlab_token: str = Header(None),
        x_gitlab_event: str = Header(None),
    ) -> JSONResponse:
        stats_counter["received"] += 1
        body = await request.body()

        # 1. Token verification
        secret = resolve_webhook_secret("gitlab", workspace_id, settings)
        if not secret:
            logger.error("gitlab_webhook_no_token_configured")
            stats_counter["rejected"] += 1
            raise HTTPException(500, "GitLab webhook token not configured")

        if not _verify_gitlab_token(x_gitlab_token, secret):
            stats_counter["rejected"] += 1
            raise HTTPException(401, "Invalid token")
        stats_counter["verified"] += 1

        # 2. Filter event
        if x_gitlab_event not in ("Merge Request Hook", GITLAB_NOTE_EVENT):
            return JSONResponse({"status": "ignored", "event": x_gitlab_event})

        # 3. Parse
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            # The decoder's offset is noise to a webhook sender; 400 is the answer.
            raise HTTPException(400, "Invalid JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "Invalid JSON")

        # 3b. The event must come from the GitLab instance this workspace is
        # connected to (gitlab.com or its self-hosted URL).
        project = payload.get("project")
        web_url = project.get("web_url") if isinstance(project, dict) else None
        if not await asyncio.to_thread(_gitlab_instance_matches, workspace_id, web_url):
            stats_counter["rejected"] += 1
            logger.warning("gitlab_webhook_instance_mismatch ws=%s", workspace_id)
            raise HTTPException(
                403, "This event comes from a GitLab instance this workspace is "
                     "not connected to")

        # 3c. A comment is a command, not a merge request event.
        if x_gitlab_event == GITLAB_NOTE_EVENT:
            return _comment_response(
                extract_gitlab_note(payload), workspace_id, settings, stats_counter)

        # 4. Filter FIRST, then dedup.
        #
        # GitLab has no delivery id, so the key is synthesised from
        # (project, iid, head sha) — which means it identifies a COMMIT, not an
        # event. Registering it before the filters let the first event at a
        # given head burn the key for 24 hours: a draft MR marked ready, a
        # closed MR reopened, an approval on an unchanged head — each of those
        # arrives first, is correctly ignored, and takes the commit's only
        # chance at a review with it.
        #
        # GitHub does not have this problem because X-GitHub-Delivery is unique
        # per event; here the key can only be claimed by an event we are
        # actually going to act on.
        attrs = payload.get("object_attributes") or {}
        proj = (payload.get("project") or {}).get("path_with_namespace") or ""
        iid = attrs.get("iid")
        sha = (attrs.get("last_commit") or {}).get("id", "")

        state_info = _extract_gitlab_mr_state(payload)
        if state_info is not None:
            asyncio.create_task(_dispatch_pr_state(
                state_info, expected_workspace_id=workspace_id))

        mr_info = _extract_gitlab_mr(payload)
        if mr_info is None:
            if state_info is not None:
                return JSONResponse({"status": "recorded", "state": state_info["state"]})
            return JSONResponse({"status": "ignored", "reason": "non-trigger"})

        # A draft MR is skipped BEFORE the dedup key is claimed (see above:
        # "mark as ready" arrives with the same sha) — unless this repository
        # reviews drafts, in which case the draft's review is the review of
        # that sha and the later "ready" event is rightly a duplicate.
        is_draft = bool(attrs.get("work_in_progress") or attrs.get("draft"))
        skip_draft = is_draft and not await _reviews_drafts("gitlab", mr_info["repo"])
        if skip_draft and mr_info["action"] in ("open", "reopen"):
            # Recorded as a skipped run — see the GitHub handler. Once per
            # opening, not on every push to a draft (GitLab fires "update"
            # for each), which would be a row per commit saying the same.
            asyncio.create_task(_dispatch_review(
                "gitlab", mr_info["repo"], mr_info["number"],
                expected_workspace_id=workspace_id, skip_reason="draft",
                pr_meta={
                    "title": attrs.get("title"),
                    "author": (payload.get("user") or {}).get("username"),
                    "url": attrs.get("url"),
                    "head_ref": attrs.get("source_branch"),
                    "base_ref": attrs.get("target_branch"),
                },
            ))
            return JSONResponse({"status": "skipped", "reason": "draft MR"})
        if skip_draft:
            return JSONResponse({"status": "skipped", "reason": "draft MR"})

        delivery_key = f"gl:{proj}:{iid}:{sha}"
        if dedup.is_duplicate(delivery_key):
            stats_counter["deduped"] += 1
            return JSONResponse({"status": "duplicate"})

        asyncio.create_task(_dispatch_review(
            "gitlab", mr_info["repo"], mr_info["number"],
            head_sha=mr_info.get("head_sha", ""),
            event=mr_info["action"],
            expected_workspace_id=workspace_id,
            pr_meta={
                "title": attrs.get("title"),
                "author": (payload.get("user") or {}).get("username"),
                "url": attrs.get("url"),
                "head_ref": attrs.get("source_branch"),
                "base_ref": attrs.get("target_branch"),
            },
        ))
        stats_counter["dispatched"] += 1
        return JSONResponse(
            {"status": "accepted", **mr_info}, status_code=202,
        )

    # The tenant comes from the PATH, not the body.
    #
    # Verification has to happen before anything is parsed — that is the whole
    # point of a signature — so the workspace cannot be read out of the
    # payload. A query parameter would be worse than a path segment: FastAPI
    # would expose it on the un-suffixed route too, turning
    # `?workspace_id=other` into a tenant selector on the legacy URL.
    #
    # `None` means the legacy un-suffixed route, which resolves to the
    # instance-wide secret exactly as before.
    # The un-suffixed URL, kept working — see the GitHub delegate above.
    @app.post("/webhook/gitlab")
    async def webhook_gitlab_legacy(
        request: Request,
        x_gitlab_token: str = Header(None),
        x_gitlab_event: str = Header(None),
    ) -> JSONResponse:
        return await webhook_gitlab(
            request,
            workspace_id=None,
            x_gitlab_token=x_gitlab_token,
            x_gitlab_event=x_gitlab_event,
        )

    @app.post("/webhook/bitbucket/{workspace_id}")
    async def webhook_bitbucket(
        request: Request,
        workspace_id: str | None = None,
        x_hub_signature: str = Header(None),
        x_event_key: str = Header(None),
        x_request_uuid: str = Header(None),
    ) -> JSONResponse:
        stats_counter["received"] += 1
        body = await request.body()

        # 1. Verify HMAC (if a secret is configured) — Bitbucket Cloud allows
        # a webhook without HMAC but we require it for security
        secret = resolve_webhook_secret("bitbucket", workspace_id, settings)
        if not secret:
            # Was `if secret:` — verification skipped entirely when unconfigured,
            # and the request still counted as verified. The comment above has
            # always said we require it; the code did the opposite, and both
            # sibling handlers (GitHub, GitLab) refuse without a secret. An
            # unauthenticated webhook is not a working integration: every
            # accepted event starts an LLM review, so anyone who learns the URL
            # can spend this workspace's budget and feed it arbitrary PR data.
            logger.error("bitbucket_webhook_no_secret_configured")
            stats_counter["rejected"] += 1
            raise HTTPException(500, "Bitbucket webhook secret not configured")

        if not _verify_bitbucket_signature(body, x_hub_signature, secret):
            stats_counter["rejected"] += 1
            raise HTTPException(401, "Invalid signature")
        stats_counter["verified"] += 1

        # 2. Dedup
        if x_request_uuid and dedup.is_duplicate(f"bb:{x_request_uuid}"):
            stats_counter["deduped"] += 1
            return JSONResponse({"status": "duplicate"})

        # 3. Parse
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            # The decoder's offset is noise to a webhook sender; 400 is the answer.
            raise HTTPException(400, "Invalid JSON") from None

        if (x_event_key or "") in BITBUCKET_COMMENT_EVENTS:
            return _comment_response(
                extract_bitbucket_comment(
                    payload, x_event_key or "", delivery=x_request_uuid or ""),
                workspace_id, settings, stats_counter)

        # `repo:push` is an INDEX trigger, not a review one — the same split the
        # GitHub handler makes. It goes to the refresh path and never reaches
        # the review dispatcher below.
        if (x_event_key or "") == "repo:push":
            push = _extract_bitbucket_push(payload)
            if push is None:
                return JSONResponse(
                    {"status": "ignored", "reason": "not a branch update"})
            asyncio.create_task(_dispatch_refresh(
                "bitbucket", push["repo"], expected_workspace_id=workspace_id))
            stats_counter["dispatched"] += 1
            return JSONResponse({"status": "accepted", **push}, status_code=202)

        state_info = _extract_bitbucket_pr_state(payload, x_event_key or "")
        if state_info is not None:
            asyncio.create_task(_dispatch_pr_state(
                state_info, expected_workspace_id=workspace_id))
            return JSONResponse({"status": "recorded", "state": state_info["state"]})

        touch = _extract_bitbucket_pr_touch(payload, x_event_key or "")
        if touch is not None:
            asyncio.create_task(_dispatch_pr_touch(
                touch, expected_workspace_id=workspace_id))

        pr_info = _extract_bitbucket_pr(payload, x_event_key or "")
        if pr_info is None:
            if touch is not None:
                return JSONResponse({"status": "recorded", "event": x_event_key})
            return JSONResponse({"status": "ignored", "event": x_event_key})

        asyncio.create_task(_dispatch_review(
            "bitbucket", pr_info["repo"], pr_info["number"],
            head_sha=pr_info.get("head_sha", ""),
            event=pr_info["action"],
            expected_workspace_id=workspace_id,
            pr_meta=_bitbucket_pr_meta(payload),
        ))
        stats_counter["dispatched"] += 1
        return JSONResponse(
            {"status": "accepted", **pr_info}, status_code=202,
        )

    # The un-suffixed URL, kept working — see the GitHub delegate above.
    @app.post("/webhook/bitbucket")
    async def webhook_bitbucket_legacy(
        request: Request,
        x_hub_signature: str = Header(None),
        x_request_uuid: str = Header(None),
        x_event_key: str = Header(None),
    ) -> JSONResponse:
        return await webhook_bitbucket(
            request,
            workspace_id=None,
            x_hub_signature=x_hub_signature,
            x_event_key=x_event_key,
            x_request_uuid=x_request_uuid,
        )

    return app
