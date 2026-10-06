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

import logging
import os
import threading
from typing import Any

import httpx

from src.api.auto_review import RepoConfig, get_auto_review_store
from src.credentials import get_credential_store, resolve_git_credential
from src.http import build_client
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
    # The credential row per key: a GitLab row also names its instance.
    cred_rows: dict[tuple[str, str], object] = {}
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
        cred_rows.setdefault(key, creds)

    for (provider, ws), (secret, cfg_list) in by_token.items():
        try:
            if provider == "github":
                _poll_github(secret, cfg_list)
            elif provider == "gitlab":
                from src.sync.gitlab_instance import instance_for_credential

                # The instance of THIS workspace's own GitLab row.
                instance = instance_for_credential(cred_rows.get((provider, ws)))
                for cfg in cfg_list:
                    _poll_gitlab_project(secret, cfg, instance)
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


def _poll_gitlab_project(token: str, cfg: RepoConfig, gitlab=None) -> None:
    """`gitlab` — the workspace's GitLabInstance (None → gitlab.com)."""
    import urllib.parse as _u

    from src.sync.gitlab_instance import DEFAULT_INSTANCE, UnsafeGitLabURL

    gitlab = gitlab or DEFAULT_INSTANCE

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
        with build_client(timeout=15.0, **gitlab.http_kwargs()) as client:
            resp = client.get(
                f"{gitlab.api_base}/projects/{project_id}/merge_requests",
                headers=headers, params=params,
            )
    except (httpx.HTTPError, UnsafeGitLabURL) as exc:
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
            # Seen before — unless the title gate held it: an edited title
            # (the "WIP" removed) brings it back into the listing.
            if _title_hold_released("gitlab", cfg, iid, mr):
                _trigger_review("gitlab", cfg.full_name, iid,
                                user_id=cfg.user_id, workspace_id=cfg.workspace_id)
            continue
        if _skip_untargeted("gitlab", cfg, iid, mr) or _skip_gated("gitlab", cfg, iid, mr):
            new_max_iid = max(new_max_iid, iid)
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


def _skip_untargeted(provider: str, cfg: RepoConfig, number: int, mr: dict) -> bool:
    """True when the MR's target branch is outside the repo's target patterns:
    the skip is recorded (as the webhook does) instead of queuing a job that
    would only end at the orchestrator's gate. The listing names the base
    branch; GitHub notifications do not, so that path leaves it to the gate."""
    base = str(mr.get("target_branch") or "")
    if not base:
        return False
    try:
        from src.review.branch_patterns import branch_targeted, skip_sentence
        from src.review.dispatch import record_gate_skip
        from src.review.review_defaults import target_branches_for_repo

        patterns = target_branches_for_repo(provider, cfg.full_name)
        if not patterns or branch_targeted(base, patterns):
            return False
        record_gate_skip(
            provider, cfg.full_name, number, user_id=cfg.user_id,
            workspace_id=cfg.workspace_id, source="poller",
            gate_key="gate_target_branch", gate_name="Validate target branch",
            reason=skip_sentence(base, patterns),
            pr_meta={"title": mr.get("title"),
                     "author": (mr.get("author") or {}).get("username"),
                     "url": mr.get("web_url"), "head_ref": mr.get("source_branch"),
                     "base_ref": base},
        )
        return True
    except Exception as exc:  # noqa: BLE001 — the gate in the orchestrator decides
        logger.warning("poller_target_check_failed repo=%s err=%s",
                       cfg.full_name, type(exc).__name__)
        return False


#: Merge requests the title gate held, per (provider, repo). The poller only
#: lists numbers above the last one it saw, so without this a "WIP" MR would
#: never be looked at again after its title is fixed. In memory: a restart
#: forgets it, and the next push (webhook) or a Review click still reviews.
_TITLE_HELD: dict[tuple[str, str], set[int]] = {}


def _title_hold_released(provider: str, cfg: RepoConfig, number: int, mr: dict) -> bool:
    """True once a held merge request's title no longer carries an ignored
    keyword; the hold is then dropped. Never raises."""
    held = _TITLE_HELD.get((provider, cfg.full_name))
    if not held or number not in held:
        return False
    try:
        from src.review.review_defaults import gate_settings_for_repo
        from src.review.scope import title_keyword_match

        gates = gate_settings_for_repo(provider, cfg.full_name)
        if title_keyword_match(mr.get("title"), gates["ignored_title_keywords"]):
            return False
    except Exception as exc:  # noqa: BLE001 — keep the hold, look again next poll
        logger.warning("poller_title_hold_check_failed repo=%s err=%s",
                       cfg.full_name, type(exc).__name__)
        return False
    held.discard(number)
    return True


def _skip_gated(provider: str, cfg: RepoConfig, number: int, mr: dict) -> bool:
    """True when the title gate or a manual review cadence stops this MR, the
    skip recorded as the webhook records it. The listing names the title;
    GitHub notifications do not, so that path leaves it to the orchestrator.

    A manual cadence with `status_feedback` on is NOT skipped here: the
    orchestrator's gate then posts the one note that says how to ask for a
    review, and records the skip itself."""
    try:
        from src.review import cadence, messages
        from src.review.dispatch import record_gate_skip
        from src.review.review_defaults import gate_settings_for_repo
        from src.review.scope import title_keyword_match

        gates = gate_settings_for_repo(provider, cfg.full_name)
        meta = {"title": mr.get("title"),
                "author": (mr.get("author") or {}).get("username"),
                "url": mr.get("web_url"), "head_ref": mr.get("source_branch"),
                "base_ref": mr.get("target_branch")}
        matched = title_keyword_match(mr.get("title"), gates["ignored_title_keywords"])
        if matched:
            _TITLE_HELD.setdefault((provider, cfg.full_name), set()).add(number)
            sentence = messages.t("gate.title", "en", keyword=matched)
            record_gate_skip(
                provider, cfg.full_name, number, user_id=cfg.user_id,
                workspace_id=cfg.workspace_id, source="poller",
                gate_key="gate_title", gate_name="Check title keywords",
                reason=sentence[:1].upper() + sentence[1:] + ".", pr_meta=meta)
            return True
        if gates["review_cadence"] == "manual" and not gates["status_feedback"]:
            decision = cadence.decide("manual")
            sentence = cadence.gate_reason(
                decision, "en", handle=cadence.bot_handle(), pushes=0, minutes=0,
                reason=None)
            record_gate_skip(
                provider, cfg.full_name, number, user_id=cfg.user_id,
                workspace_id=cfg.workspace_id, source="poller",
                gate_key="gate_cadence", gate_name="Check review cadence",
                reason=sentence[:1].upper() + sentence[1:] + ".", pr_meta=meta)
            return True
        return False
    except Exception as exc:  # noqa: BLE001 — the gates in the orchestrator decide
        logger.warning("poller_gate_check_failed repo=%s err=%s",
                       cfg.full_name, type(exc).__name__)
        return False


def _trigger_review(provider: str, repo: str, pr_number: int, *, user_id: str,
                    workspace_id: str = "default") -> None:
    """Enqueue the review as a durable job (Stage 21).

    Previously ran the orchestrator inline in the poller thread — a crash
    mid-review lost the run and blocked the poll loop for minutes. Now the
    sync worker owns execution (with retries + dead-letter); the poller
    only detects. Dedup key = provider:repo#pr so a PR spotted by both
    poller AND webhook produces exactly one queued review.

    Falls back to inline execution when the queue is unreachable, so a
    broken Postgres doesn't silently stop all reviews — through the worker's
    own body (`execute_review`), so the fallback records the run and its
    stages exactly as the queue would.
    """
    logger.info(
        "poller_review_enqueue user=%s provider=%s repo=%s pr=%d",
        user_id, provider, repo, pr_number,
    )
    from src.review.stages import now_iso

    payload = {
        "provider": provider, "repo": repo, "pr_number": pr_number,
        "post_comments": True, "user_id": user_id,
        "workspace_id": workspace_id,
        # The run's "Review started" / "Queued" stages are written from these.
        "source": "poller", "enqueued_at": now_iso(),
    }
    try:
        from src.sync.queue import KIND_REVIEW, enqueue
        enqueue(
            kind=KIND_REVIEW,
            payload=payload,
            dedup_key=f"review:{provider}:{repo}#{pr_number}",
            enqueued_by=f"poller:{provider}",
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "poller_enqueue_failed_falling_back_inline provider=%s pr=%d err=%s",
            provider, pr_number, exc,
        )

    # Inline fallback — legacy path, now the worker's body.
    from src.review.dispatch import execute_review

    try:
        execute_review(payload)
        logger.info("poller_review_done provider=%s repo=%s pr=%d",
                    provider, repo, pr_number)
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
