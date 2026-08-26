"""Background poller — auto-discover new PRs/MRs without webhooks.

Strategy per provider (per research May 2026):
    GitHub:    GET /notifications with a conditional GET (If-Modified-Since).
               304 doesn't count against rate limit. Cheap.
    GitLab:    Per-project GET /merge_requests?state=opened&updated_after=
               with ETag. New MRs discovered since last poll are reviewed.
    Bitbucket: Manual mode only — UI lists PRs + user clicks Review.
               (Bitbucket no aggregated notifications API; per-repo polling
                is feasible but kept manual per requirements.)

Loop:
    Every POLL_INTERVAL_SECONDS (default 60s):
        for cfg in auto_review_config WHERE enabled=1 AND mode='polling':
            new_prs = provider_specific_poll(cfg)
            for pr in new_prs:
                ReviewOrchestrator.review(pr, post_comments=True)

Concurrency:
    Single background thread (daemon=True) — avoids accidentally double-running
    reviews. Multi-instance scenarios should disable poller and use webhooks.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from typing import Any

import httpx

from src.api.auto_review import RepoConfig, get_auto_review_store
from src.credentials import get_credential_store, resolve_git_credential
from src.http import build_client
from src.review.orchestrator import ReviewOrchestrator
from src.review.providers import get_provider_for
from src.review.providers.base import PullRequestProviderError

logger = logging.getLogger(__name__)


POLL_INTERVAL_SECONDS = int(os.environ.get("CELMIS_POLL_INTERVAL", "60"))


_thread: threading.Thread | None = None
_stop_event = threading.Event()


def start_background_poller() -> None:
    """Idempotent — starts the poller thread once."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop_event.clear()
    t = threading.Thread(target=_poller_loop, daemon=True, name="celmis-poller")
    t.start()
    _thread = t
    logger.info("poller_started interval=%ds", POLL_INTERVAL_SECONDS)


def stop_background_poller() -> None:
    _stop_event.set()


def _poller_loop() -> None:
    while not _stop_event.wait(POLL_INTERVAL_SECONDS):
        try:
            _poll_cycle()
        except Exception as exc:  # noqa: BLE001
            logger.exception("poller_cycle_failed err=%s", exc)


def _poll_cycle() -> None:
    """One pass over all enabled polling configs."""
    store = get_auto_review_store()
    configs = store.list_enabled(mode="polling")
    if not configs:
        return
    cred_store = get_credential_store()

    # Group by the credential that will actually be used. Each workspace has
    # its own git token now, so the key is (provider, workspace_id): repos in
    # the same workspace share one token (GitHub's account-wide /notifications
    # is fetched once per workspace per cycle), and two workspaces never share
    # a token. Polling *state* stays per-config (cfg.user_id, cfg.repo_slug).
    by_token: dict[tuple[str, str], tuple[str, list[RepoConfig]]] = {}
    for cfg in configs:
        try:
            creds = resolve_git_credential(
                cfg.provider, user_id=cfg.user_id,
                workspace_id=cfg.workspace_id, store=cred_store,
            )
        except Exception as exc:  # noqa: BLE001 — corrupted row, not fatal
            logger.warning(
                "poller_credential_unreadable repo=%s provider=%s err=%s",
                cfg.full_name, cfg.provider, exc,
            )
            continue
        if creds is None:
            # WARNING, not debug: this is the offboarding failure mode. Auto
            # review is silently off for this repo until someone reconnects.
            logger.warning(
                "poller_skip_no_creds repo=%s provider=%s owner=%s — auto review "
                "is NOT running for this repo; reconnect %s on the Connections "
                "page (admin) to restore it",
                cfg.full_name, cfg.provider, cfg.user_id, cfg.provider,
            )
            continue
        key = (cfg.provider, cfg.workspace_id)
        by_token.setdefault(key, (creds.secret, []))[1].append(cfg)

    for (provider, ws), (secret, cfg_list) in by_token.items():
        try:
            if provider == "github":
                _poll_github(secret, cfg_list)
            elif provider == "gitlab":
                for cfg in cfg_list:
                    _poll_gitlab_project(secret, cfg)
            # Bitbucket polling intentionally not supported — manual mode only
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "poller_provider_failed provider=%s workspace=%s err=%s",
                provider, ws, exc,
            )


# ─── GitHub: single /notifications call covers all watched repos ──────


def _poll_github(token: str, configs: list[RepoConfig]) -> None:
    """Poll GitHub Notifications API and trigger reviews for new PRs.

    Conditional GET — /notifications is account-wide for the token, so the
    configs sharing that token also share one etag.
    """
    cfg_by_full_name = {cfg.full_name.lower(): cfg for cfg in configs}

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    # Use the most recent etag from any config sharing this token
    etag = next((c.last_poll_etag for c in configs if c.last_poll_etag), None)
    if etag:
        headers["If-None-Match"] = etag

    try:
        with build_client(timeout=15.0) as client:
            resp = client.get(
                "https://api.github.com/notifications",
                headers=headers,
                params={"all": "false", "participating": "false"},
            )
    except httpx.HTTPError as exc:
        logger.warning("github_notif_failed err=%s", exc)
        return

    if resp.status_code == 304:
        logger.debug("github_notif_304 repos=%d", len(configs))
        return
    if resp.status_code >= 400:
        logger.warning(
            "github_notif_status=%d body=%s", resp.status_code, resp.text[:200],
        )
        return

    new_etag = resp.headers.get("ETag")
    items: list[dict[str, Any]] = resp.json() or []

    for item in items:
        subject = item.get("subject") or {}
        if subject.get("type") != "PullRequest":
            continue
        repo = item.get("repository") or {}
        full_name = str(repo.get("full_name") or "").lower()
        cfg = cfg_by_full_name.get(full_name)
        if cfg is None:
            continue  # notification for repo not registered in our store

        subject_url = str(subject.get("url") or "")
        # Subject URL: https://api.github.com/repos/{o}/{r}/pulls/{n}
        try:
            pr_number = int(subject_url.rstrip("/").rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            continue
        if cfg.last_seen_pr_id and pr_number <= cfg.last_seen_pr_id:
            continue
        _trigger_review("github", cfg.full_name, pr_number,
                        user_id=cfg.user_id, workspace_id=cfg.workspace_id)
        get_auto_review_store().update_polling_state(
            cfg.user_id, cfg.repo_slug, last_seen_pr_id=pr_number,
        )

    # Update etag across every config sharing this token (shared notifications)
    if new_etag:
        store = get_auto_review_store()
        for cfg in configs:
            store.update_polling_state(cfg.user_id, cfg.repo_slug, etag=new_etag)


# ─── GitLab: per-project MR polling ───────────────────────────────────


def _poll_gitlab_project(token: str, cfg: RepoConfig) -> None:
    import urllib.parse as _u

    project_id = _u.quote(cfg.full_name, safe="")
    headers = {"PRIVATE-TOKEN": token}
    if cfg.last_poll_etag:
        headers["If-None-Match"] = cfg.last_poll_etag

    params: dict[str, str] = {
        "state": "opened",
        "order_by": "updated_at",
        "per_page": "20",
    }
    if cfg.last_polled_at:
        params["updated_after"] = cfg.last_polled_at

    try:
        with build_client(timeout=15.0) as client:
            resp = client.get(
                f"https://gitlab.com/api/v4/projects/{project_id}/merge_requests",
                headers=headers, params=params,
            )
    except httpx.HTTPError as exc:
        logger.warning("gitlab_poll_failed repo=%s err=%s", cfg.full_name, exc)
        return

    if resp.status_code == 304:
        get_auto_review_store().update_polling_state(cfg.user_id, cfg.repo_slug)
        return
    if resp.status_code >= 400:
        logger.warning(
            "gitlab_poll_status=%d repo=%s body=%s",
            resp.status_code, cfg.full_name, resp.text[:200],
        )
        return

    items = resp.json() or []
    new_max_iid = cfg.last_seen_pr_id or 0
    for mr in items:
        iid = int(mr.get("iid", 0))
        if iid <= (cfg.last_seen_pr_id or 0):
            continue
        _trigger_review("gitlab", cfg.full_name, iid,
                        user_id=cfg.user_id, workspace_id=cfg.workspace_id)
        new_max_iid = max(new_max_iid, iid)

    new_etag = resp.headers.get("ETag")
    get_auto_review_store().update_polling_state(
        cfg.user_id, cfg.repo_slug,
        etag=new_etag,
        last_seen_pr_id=new_max_iid if new_max_iid > (cfg.last_seen_pr_id or 0) else None,
    )


def _trigger_review(provider: str, repo: str, pr_number: int, *, user_id: str,
                    workspace_id: str = "default") -> None:
    """Enqueue the review as a durable job (Stage 21).

    Previously ran the orchestrator inline in the poller thread — a crash
    mid-review lost the run and blocked the poll loop for minutes. Now the
    sync worker owns execution (with retries + dead-letter); the poller
    only detects. Dedup key = provider:repo#pr so a PR spotted by both
    poller AND webhook produces exactly one queued review.

    Falls back to inline execution when the queue is unreachable, so a
    broken Postgres doesn't silently stop all reviews.
    """
    logger.info(
        "poller_review_enqueue user=%s provider=%s repo=%s pr=%d",
        user_id, provider, repo, pr_number,
    )
    try:
        from src.sync.queue import KIND_REVIEW, enqueue
        enqueue(
            kind=KIND_REVIEW,
            payload={
                "provider": provider, "repo": repo, "pr_number": pr_number,
                "post_comments": True, "user_id": user_id,
                "workspace_id": workspace_id,
            },
            dedup_key=f"review:{provider}:{repo}#{pr_number}",
            enqueued_by=f"poller:{provider}",
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "poller_enqueue_failed_falling_back_inline provider=%s pr=%d err=%s",
            provider, pr_number, exc,
        )

    # Inline fallback — legacy path.
    pr_provider = None
    try:
        pr_provider = get_provider_for(provider, user_id=user_id, workspace_id=workspace_id)
        orch = ReviewOrchestrator()
        result = orch.review(
            provider, repo, pr_number,
            dry_run=False,
            post_comments=True,
            provider=pr_provider,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        logger.info(
            "poller_review_done provider=%s repo=%s pr=%d verdict=%s findings=%d",
            provider, repo, pr_number,
            result.batch.verdict.value, len(result.batch.findings),
        )
    except PullRequestProviderError as exc:
        logger.warning(
            "poller_review_provider_failed provider=%s repo=%s pr=%d err=%s",
            provider, repo, pr_number, exc,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "poller_review_failed provider=%s repo=%s pr=%d err=%s",
            provider, repo, pr_number, exc,
        )
    finally:
        if pr_provider is not None:
            with contextlib.suppress(Exception):
                pr_provider.close()
