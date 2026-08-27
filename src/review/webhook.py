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

    Triggers on opened / synchronize / ready_for_review / reopened.
    """
    action = payload.get("action")
    if action not in ("opened", "synchronize", "ready_for_review", "reopened"):
        return None
    pr = payload.get("pull_request") or {}
    repo = (payload.get("repository") or {}).get("full_name")
    if not pr or not repo:
        return None
    return {
        "provider": "github",
        "repo": repo,
        "number": int(pr.get("number") or 0),
        "head_sha": str((pr.get("head") or {}).get("sha") or ""),
        "action": action,
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
    }


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
             if c.full_name == full_name and c.provider == provider),
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


async def _dispatch_review(
    provider_name: str,
    repo: str,
    pr_number: int,
    *,
    post_comments: bool = True,
    head_sha: str = "",
    expected_workspace_id: str | None = None,
) -> None:
    """Enqueue the review as a durable job. If the sync queue fails,
    fall back to inline dispatch (legacy behaviour) so a broken DB
    connection doesn't drop the webhook."""
    # Derive the tenant from the repo — the webhook is unauthenticated, so this
    # is the ONLY tenant binding. workspace_for_repo returns None when the repo
    # is unknown OR bound to more than one workspace; in both cases we FAIL
    # CLOSED (skip) rather than guess and run under the wrong tenant's keys.
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

    # A delivery is not permission. Auto-review off means off, whoever POSTs.
    #
    # The dispatcher used to consult only the repo→workspace binding, so a
    # webhook left installed after somebody switched auto-review off kept
    # spending that workspace's model budget on every pull request. The
    # binding row survives the toggle — that is what the toggle toggles — so
    # "the repo is known" was never the same question as "the owner wants
    # this".
    if not cfg.enabled:
        logger.info(
            "webhook_auto_review_disabled provider=%s repo=%s pr=%d ws=%s — "
            "skipping (the repo is bound but auto-review is switched off)",
            provider_name, repo, pr_number, workspace_id,
        )
        return

    # The URL's workspace and the repo's workspace must be the SAME workspace.
    #
    # Per-workspace secrets stop tenant A signing for tenant B's URL, and this
    # closes the other half. A's only remaining move is to POST a payload
    # naming B's repository to A's OWN url, signed with A's OWN valid secret.
    # The signature checks out, and without this line the delivery flows on
    # with workspace_id=B — so the review runs on B's provider token, spends
    # B's LLM budget and comments as B, on a request A composed.
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

    try:
        from src.sync.queue import KIND_REVIEW, enqueue
        # Dedup key matches the poller's exactly (no head_sha), so a PR spotted
        # by both webhook AND poller produces exactly one queued review.
        dedup = f"review:{provider_name}:{repo}#{pr_number}"
        enqueue(
            kind=KIND_REVIEW,
            payload={
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
            },
            dedup_key=dedup,
            enqueued_by=f"webhook:{provider_name}",
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "enqueue_failed_falling_back_inline provider=%s pr=%d err=%s",
            provider_name, pr_number, exc,
        )

    # Inline fallback (previous behaviour).
    from src.review.orchestrator import ReviewOrchestrator
    from src.review.providers import get_provider_for
    from src.review.providers.base import PullRequestProviderError

    logger.info(
        "review_dispatch_start provider=%s repo=%s pr=%d workspace=%s",
        provider_name, repo, pr_number, workspace_id,
    )
    try:
        orchestrator = ReviewOrchestrator()
        # user_id here for the same reason as in the queue payload above: both
        # `get_provider_for` and `orchestrator.review` default it to "default",
        # and that default is what broke the queued path. The poller's inline
        # fallback has always passed it; this one silently did not.
        provider = get_provider_for(
            provider_name, user_id=cfg.user_id, workspace_id=workspace_id,
        )
        try:
            # Run in a thread pool (orchestrator sync — uses ThreadPool internally)
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: orchestrator.review(
                    provider_name, repo, pr_number,
                    dry_run=not post_comments,
                    post_comments=True,
                    provider=provider,
                    user_id=cfg.user_id,
                    workspace_id=workspace_id,
                ),
            )
        finally:
            provider.close()

        logger.info(
            "review_dispatch_done provider=%s repo=%s pr=%d "
            "findings=%d verdict=%s posted=%s",
            provider_name, repo, pr_number,
            len(result.batch.findings), result.batch.verdict.value,
            result.posted,
        )
    except PullRequestProviderError as exc:
        logger.error(
            "review_dispatch_provider_error provider=%s repo=%s pr=%d err=%s",
            provider_name, repo, pr_number, exc,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "review_dispatch_unhandled provider=%s repo=%s pr=%d err=%s",
            provider_name, repo, pr_number, exc,
        )


# ─── FastAPI app factory ───────────────────────────────────────


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
    }

    @app.get("/healthz")
    async def healthz() -> dict:
        return {
            "status": "ok",
            "review_settings": {
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
            },
        }

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
            stats_counter["dispatched"] += 1
            return JSONResponse({"status": "accepted", **info}, status_code=202)

        if x_github_event != "pull_request":
            return JSONResponse({"status": "ignored", "event": x_github_event})

        # 4. Parse + dispatch
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            # The decoder's offset is noise to a webhook sender; 400 is the answer.
            raise HTTPException(400, "Invalid JSON") from None

        pr_info = _extract_github_pr(payload)
        if pr_info is None:
            return JSONResponse({"status": "ignored", "reason": "non-trigger action"})

        # Skip drafts if action=opened (drafts trigger ready_for_review later)
        is_draft = (payload.get("pull_request") or {}).get("draft", False)
        if is_draft and pr_info["action"] == "opened":
            return JSONResponse({"status": "skipped", "reason": "draft PR"})

        asyncio.create_task(_dispatch_review(
            "github", pr_info["repo"], pr_info["number"],
            head_sha=pr_info.get("head_sha", ""),
            expected_workspace_id=workspace_id,
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
        if x_gitlab_event != "Merge Request Hook":
            return JSONResponse({"status": "ignored", "event": x_gitlab_event})

        # 3. Parse
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            # The decoder's offset is noise to a webhook sender; 400 is the answer.
            raise HTTPException(400, "Invalid JSON") from None

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

        mr_info = _extract_gitlab_mr(payload)
        if mr_info is None:
            return JSONResponse({"status": "ignored", "reason": "non-trigger"})

        is_draft = bool(attrs.get("work_in_progress") or attrs.get("draft"))
        if is_draft:
            return JSONResponse({"status": "skipped", "reason": "draft MR"})

        delivery_key = f"gl:{proj}:{iid}:{sha}"
        if dedup.is_duplicate(delivery_key):
            stats_counter["deduped"] += 1
            return JSONResponse({"status": "duplicate"})

        asyncio.create_task(_dispatch_review(
            "gitlab", mr_info["repo"], mr_info["number"],
            head_sha=mr_info.get("head_sha", ""),
            expected_workspace_id=workspace_id,
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

        pr_info = _extract_bitbucket_pr(payload, x_event_key or "")
        if pr_info is None:
            return JSONResponse({"status": "ignored", "event": x_event_key})

        asyncio.create_task(_dispatch_review(
            "bitbucket", pr_info["repo"], pr_info["number"],
            head_sha=pr_info.get("head_sha", ""),
            expected_workspace_id=workspace_id,
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
