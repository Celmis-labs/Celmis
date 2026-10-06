"""The sync engine: list, detail, deployments, link, classify.

One run per repository at a time (a soft lease in `productivity_sync_state`),
bounded by a time budget, resumable at every step:

    1 LIST     PRs updated since the watermark, oldest update first, a page at
               a time. The watermark moves after each saved page, so a crash,
               a budget stop or a rate limit resumes after the last page.
    2 DETAIL   Newest PR first (the dashboard fills from the recent end): the
               first commit, the activity feed, the size. A closed PR is
               detailed once and again only when its state changes; an open one
               when it was updated after its last detail.
    3 REVERTS  A revert that named its target by title or ticket is resolved to
               a PR number once the target has been synced.
    4 DEPLOY   Deployments are re-derived from the PR rows (and the provider's
               records for `provider`/`tags`), PRs are linked to them, failures
               marked. Pure arithmetic over rows; no API calls for `merge`.

Opt-in: nothing happens for a repository whose `enabled` setting is false.
Writes no comment text, ever.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.models import (
    ProductivityDeployment,
    ProductivityDeploymentPr,
    ProductivityPrEvent,
    ProductivityPullRequest,
    ProductivitySyncState,
)
from src.productivity import classify, deploys
from src.productivity import settings as settings_mod
from src.productivity.db import get_engine
from src.productivity.providers.base import (
    PRDetail,
    ProductivityProvider,
    ProviderDeployment,
    ProviderError,
    PRRecord,
)
from src.productivity.ratelimit import DEFAULT_MAX_WAIT, RateLimited, fingerprint, gate_for
from src.productivity.settings import ProductivitySettings

logger = logging.getLogger(__name__)

#: Seconds one job may run before it saves its cursor and queues itself again.
DEFAULT_TIME_BUDGET = 480.0
#: How long a run holds the repository's lease beyond its own budget.
_LEASE_SLACK = 180.0
_REVIEW_KINDS = frozenset({"comment", "review", "approval", "changes_requested"})

ProviderFactory = Callable[[ProductivitySettings], ProductivityProvider]


def _utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; Postgres aware ones. Always aware here."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass
class SyncResult:
    #: ok | disabled | busy | rate_limited | budget | cancelled | error
    status: str
    prs_listed: int = 0
    prs_detailed: int = 0
    deployments: int = 0
    #: True when work is left: the caller queues a continuation.
    more_work: bool = False
    resume_at: datetime | None = None
    error: str | None = None


class _Budget:
    def __init__(self, seconds: float, cancel_check: Callable[[], bool] | None) -> None:
        self._deadline = time.monotonic() + max(0.0, seconds)
        self._cancel = cancel_check

    def left(self) -> float:
        return self._deadline - time.monotonic()

    def spent(self) -> bool:
        return self.left() <= 0

    def cancelled(self) -> bool:
        try:
            return bool(self._cancel and self._cancel())
        except Exception:  # noqa: BLE001 — a failing poll must not stop a sync
            return False

    def tune(self, provider: ProductivityProvider) -> None:
        provider.max_wait = max(1.0, min(DEFAULT_MAX_WAIT, self.left()))


# ─── providers ────────────────────────────────────────────────────────

def build_provider(
    workspace_id: str, provider: str, full_name: str, *, user_id: str = "default",
    rate_per_hour: int = 500,
) -> ProductivityProvider:
    """The adapter for a repository, with the workspace's own git credential.

    Raises `ProviderError` (no secrets in the text) when there is none.
    """
    from src.credentials import resolve_git_credential
    from src.http import build_client
    from src.productivity.providers.bitbucket import BitbucketProductivity
    from src.productivity.providers.github import GitHubProductivity
    from src.productivity.providers.gitlab import GitLabProductivity

    creds = resolve_git_credential(provider, user_id=user_id, workspace_id=workspace_id)
    if creds is None or not creds.secret:
        raise ProviderError(f"no {provider} credential saved for workspace {workspace_id}")
    meta = creds.metadata if isinstance(creds.metadata, dict) else {}
    email = str(meta.get("atlassian_email") or "")
    gate = gate_for(fingerprint(provider, creds.secret, email), rate_per_hour)
    if provider == "bitbucket":
        return BitbucketProductivity(full_name, client=build_client(timeout=20.0), gate=gate,
                                     email=email, token=creds.secret)
    if provider == "github":
        return GitHubProductivity(full_name, client=build_client(timeout=30.0), gate=gate,
                                  token=creds.secret)
    if provider == "gitlab":
        from src.sync.gitlab_instance import instance_for_credential

        inst = instance_for_credential(creds)
        return GitLabProductivity(
            full_name, client=build_client(timeout=20.0, **inst.http_kwargs()), gate=gate,
            token=creds.secret, api_base=inst.api_base)
    raise ProviderError(f"unsupported provider {provider!r}")


# ─── rows ─────────────────────────────────────────────────────────────

def _state_row(s: Session, ws: str, provider: str, repo: str) -> ProductivitySyncState:
    row = s.get(ProductivitySyncState, (ws, provider, repo))
    if row is None:
        row = ProductivitySyncState(workspace_id=ws, provider=provider, repo=repo)
        s.add(row)
        s.flush()
    return row


def _pr_row(s: Session, ws: str, provider: str, repo: str, number: int) -> ProductivityPullRequest | None:
    return s.execute(select(ProductivityPullRequest).where(
        ProductivityPullRequest.workspace_id == ws, ProductivityPullRequest.provider == provider,
        ProductivityPullRequest.repo == repo, ProductivityPullRequest.number == number,
    )).scalar_one_or_none()


def _apply_list_fields(
    row: ProductivityPullRequest, rec: PRRecord, cfg: ProductivitySettings, repo_slug: str | None,
) -> bool:
    """Copy the list-level fields onto `row`. True when the PR changed state."""
    changed_state = row.state != rec.state if row.id and row.detail_state != "none" else False
    row.repo_slug = repo_slug or row.repo_slug
    row.title, row.url = rec.title[:500], rec.url or row.url
    row.author_key, row.author_name = rec.author_key or row.author_key, rec.author_name or row.author_name
    row.state, row.is_draft = rec.state, rec.is_draft
    row.source_branch, row.target_branch = rec.source_branch, rec.target_branch
    row.created_at, row.updated_on = rec.created_at or row.created_at, rec.updated_on
    row.merge_commit_sha = rec.merge_commit_sha or row.merge_commit_sha
    row.head_sha = rec.head_sha or row.head_sha
    if rec.merged_at and not (row.merged_at and not row.merged_at_approx and rec.merged_at_approx):
        row.merged_at, row.merged_at_approx = rec.merged_at, rec.merged_at_approx
    if rec.state != "merged" and not rec.merged_at:
        row.merged_at, row.merged_at_approx = None, False
    row.closed_at = rec.closed_at or (row.closed_at if rec.state != "open" else None)
    row.kind = classify.classify_kind(rec.title, rec.source_branch, cfg)
    row.ticket_key = classify.ticket_key(rec.title, rec.source_branch, rec.description)
    if row.kind == "revert":
        number, hint = classify.revert_reference(rec.title, rec.description, rec.number, cfg, repo=row.repo)
        row.reverts_pr_number, row.revert_hint = number, hint
    else:
        row.reverts_pr_number = row.revert_hint = None
    return changed_state


def _upsert_page(
    s: Session, ws: str, provider: str, repo: str, page: list[PRRecord],
    cfg: ProductivitySettings, repo_slug: str | None, now: datetime,
) -> None:
    for rec in page:
        row = _pr_row(s, ws, provider, repo, rec.number)
        if row is None:
            row = ProductivityPullRequest(
                workspace_id=ws, provider=provider, repo=repo, number=rec.number,
                detail_state="none")
            s.add(row)
            s.flush()
        changed_state = _apply_list_fields(row, rec, cfg, repo_slug)
        if rec.detail is not None:
            # The list carried the detail (GitHub): nothing to fetch later.
            apply_detail(s, row, rec.detail, cfg, now)
        elif row.detail_state == "full":
            synced = _utc(row.detail_synced_at)
            stale = changed_state or (
                rec.state == "open" and rec.updated_on is not None
                and (synced is None or rec.updated_on > synced))
            if stale:
                row.detail_state = "partial"


def apply_detail(
    s: Session, row: ProductivityPullRequest, detail: PRDetail, cfg: ProductivitySettings,
    now: datetime,
) -> None:
    """Store a PR's detail and everything derived from it. Replaces its events."""
    row.first_commit_at = detail.first_commit_at or row.first_commit_at
    row.commits_count = detail.commits_count if detail.commits_count is not None else row.commits_count
    row.additions, row.deletions = detail.additions, detail.deletions
    row.files_changed = detail.files_changed
    if row.state == "merged" and detail.merged_at:
        row.merged_at, row.merged_at_approx = detail.merged_at, False
    if row.state == "declined" and detail.closed_at:
        row.closed_at = detail.closed_at

    s.execute(delete(ProductivityPrEvent).where(ProductivityPrEvent.pr_id == row.id))
    seen: set[tuple[str, str]] = set()
    review_times: list[datetime] = []
    approval_times: list[datetime] = []
    human_comments = approvals = 0
    for act in detail.activity:
        if (act.kind, act.external_id) in seen:
            continue
        seen.add((act.kind, act.external_id))
        is_author = bool(row.author_key) and act.actor_key == row.author_key
        is_bot = (
            act.bot_account
            or classify.is_ignored_actor(act.actor_key, act.actor_name, cfg.ignored_authors)
            or (act.kind in ("comment", "review") and classify.is_bot_comment(act.raw, cfg.bot_markers))
        )
        s.add(ProductivityPrEvent(
            workspace_id=row.workspace_id, pr_id=row.id, kind=act.kind,
            external_id=act.external_id, actor_key=act.actor_key, actor_name=act.actor_name[:200],
            at=act.at, is_bot=is_bot, is_author=is_author))
        if is_bot or is_author or act.kind not in _REVIEW_KINDS:
            continue
        review_times.append(act.at)
        if act.kind == "approval":
            approval_times.append(act.at)
            approvals += 1
        elif act.kind == "comment":
            human_comments += 1
    row.first_review_at = min(review_times) if review_times else None
    row.first_approval_at = min(approval_times) if approval_times else None
    row.human_comments, row.approvals = human_comments, approvals
    row.detail_state, row.detail_synced_at = "full", now


# ─── phases ───────────────────────────────────────────────────────────

def _list_phase(
    engine, ws: str, provider_name: str, repo: str, repo_slug: str | None,
    cfg: ProductivitySettings, provider: ProductivityProvider, now: datetime,
    budget: _Budget, result: SyncResult,
) -> bool:
    """True when the list was read to its end."""
    with Session(engine) as s:
        state = _state_row(s, ws, provider_name, repo)
        wanted_from = now - timedelta(days=cfg.backfill_days)
        have_from = _utc(state.backfill_from)
        if have_from is None or wanted_from < have_from - timedelta(days=1):
            # First run, or a wider window than before: read from the new start.
            # Rows already stored are updated in place, never duplicated.
            state.updated_watermark, state.backfill_from = wanted_from, wanted_from
            state.backfill_done = False
        since = _utc(state.updated_watermark) or wanted_from
        s.commit()
    for page in provider.list_pull_requests(since):
        if budget.cancelled():
            result.status = "cancelled"
            return False
        if budget.spent():
            return False
        with Session(engine) as s:
            _upsert_page(s, ws, provider_name, repo, page, cfg, repo_slug, now)
            newest = max((r.updated_on for r in page if r.updated_on), default=None)
            state = _state_row(s, ws, provider_name, repo)
            mark = _utc(state.updated_watermark)
            if newest and (mark is None or newest > mark):
                state.updated_watermark = newest
            s.commit()
        result.prs_listed += len(page)
        budget.tune(provider)
    with Session(engine) as s:
        _state_row(s, ws, provider_name, repo).backfill_done = True
        s.commit()
    return True


def _record_of(row: ProductivityPullRequest) -> PRRecord:
    return PRRecord(
        number=row.number, title=row.title, state=row.state, created_at=_utc(row.created_at),
        updated_on=_utc(row.updated_on), author_key=row.author_key or "",
        author_name=row.author_name or "", url=row.url or "", target_branch=row.target_branch,
        source_branch=row.source_branch, merged_at=_utc(row.merged_at), closed_at=_utc(row.closed_at))


#: Distinct PRs whose detail failed in one run before it gives up: a revoked
#: token fails every PR the same way, and each attempt costs API calls.
_MAX_DETAIL_FAILURES = 10


def _detail_phase(
    engine, ws: str, provider_name: str, repo: str, cfg: ProductivitySettings,
    provider: ProductivityProvider, now: datetime, budget: _Budget, result: SyncResult,
) -> bool:
    """True when no PR is left without detail."""
    failed: dict[int, str] = {}
    while True:
        with Session(engine) as s:
            numbers = list(s.execute(
                select(ProductivityPullRequest.number).where(
                    ProductivityPullRequest.workspace_id == ws,
                    ProductivityPullRequest.provider == provider_name,
                    ProductivityPullRequest.repo == repo,
                    ProductivityPullRequest.detail_state != "full",
                    ProductivityPullRequest.number.notin_(list(failed) or [-1]),
                ).order_by(ProductivityPullRequest.created_at.desc(),
                           ProductivityPullRequest.number.desc()).limit(25)).scalars())
        if not numbers:
            if failed:
                result.error = f"detail failed for {len(failed)} PR(s): {next(iter(failed.values()))}"
            return True
        for number in numbers:
            if budget.cancelled():
                result.status = "cancelled"
                return False
            if budget.spent():
                return False
            budget.tune(provider)
            try:
                with Session(engine) as s:
                    row = _pr_row(s, ws, provider_name, repo, number)
                    if row is None:
                        continue
                    apply_detail(s, row, provider.pr_detail(_record_of(row)), cfg, now)
                    s.commit()
            except ProviderError as exc:
                failed[number] = str(exc)[:200]
                if len(failed) >= _MAX_DETAIL_FAILURES:
                    raise
                continue
            result.prs_detailed += 1


def _release_phase(
    engine, ws: str, provider_name: str, repo: str, cfg: ProductivitySettings,
    provider: ProductivityProvider, budget: _Budget, result: SyncResult | None = None,
) -> None:
    """Read the commit list of PRs merged into production (the release PRs).

    Only worth the calls when integration branches are configured: the list is
    what ties a PR merged into the integration branch to the release that shipped it.
    """
    if not cfg.integration_branches:
        return
    with Session(engine) as s:
        numbers = [r.number for r in s.execute(select(ProductivityPullRequest).where(
            ProductivityPullRequest.workspace_id == ws,
            ProductivityPullRequest.provider == provider_name,
            ProductivityPullRequest.repo == repo,
            ProductivityPullRequest.state == "merged",
            ProductivityPullRequest.commit_shas.is_(None)).order_by(
                ProductivityPullRequest.merged_at.desc())).scalars()
            if deploys.branch_matches(r.target_branch, cfg.production_branches)]
    failed: dict[int, str] = {}
    for number in numbers:
        if budget.spent() or budget.cancelled():
            break
        budget.tune(provider)
        try:
            shas = provider.pr_commit_shas(number)
        except ProviderError as exc:
            # One unreadable PR (404, 5xx) must not stop the deployments being
            # rebuilt; it is tried again on the next run.
            failed[number] = str(exc)[:200]
            if len(failed) >= _MAX_DETAIL_FAILURES:
                raise
            continue
        with Session(engine) as s:
            row = _pr_row(s, ws, provider_name, repo, number)
            if row is not None:
                row.commit_shas = shas
                s.commit()
    if failed and result is not None and not result.error:
        result.error = f"commit list failed for {len(failed)} PR(s): {next(iter(failed.values()))}"


def _remember_release_shas(
    provider: ProductivityProvider, row: ProductivityPullRequest, cfg: ProductivitySettings,
) -> None:
    """A PR merged into production names the commits it shipped (the release PR)."""
    if (row.state == "merged" and cfg.integration_branches and row.commit_shas is None
            and deploys.branch_matches(row.target_branch, cfg.production_branches)):
        # An unreadable list is left for the next sync run (`_release_phase`); the refresh goes on.
        with contextlib.suppress(ProviderError):
            row.commit_shas = provider.pr_commit_shas(row.number)


def _resolve_reverts(engine, ws: str, provider_name: str, repo: str) -> None:
    with Session(engine) as s:
        rows = list(s.execute(select(ProductivityPullRequest).where(
            ProductivityPullRequest.workspace_id == ws,
            ProductivityPullRequest.provider == provider_name,
            ProductivityPullRequest.repo == repo)).scalars())
        candidates = [{"number": r.number, "title": r.title, "ticket_key": r.ticket_key}
                      for r in rows if r.kind != "revert"]
        for r in rows:
            if r.kind == "revert" and r.reverts_pr_number is None and r.revert_hint:
                r.reverts_pr_number = classify.resolve_hint(r.revert_hint, r.number, candidates)
        s.commit()


def _deploy_phase(
    engine, ws: str, provider_name: str, repo: str, cfg: ProductivitySettings,
    provider: ProductivityProvider, now: datetime, result: SyncResult, *,
    stored_only: bool = False,
) -> None:
    provided = []
    if cfg.deploy_source != "merge":
        if stored_only:
            # A merge webhook must not cost one API call per deployment (GitHub
            # reads a status for each): the stored ones stand until the next tick.
            provided = _stored_provided(engine, workspace_id=ws, provider_name=provider_name, repo=repo)
        else:
            # A failure here (a token without the deployments scope) must not wipe
            # the deployments already stored: nothing is rebuilt, the error is kept.
            provided = provider.deployments(now - timedelta(days=cfg.backfill_days), cfg)
    rebuild_deployments(engine, ws, provider_name, repo, cfg, provided, result=result)


def _stored_provided(engine, *, workspace_id: str, provider_name: str, repo: str) -> list[ProviderDeployment]:
    with Session(engine) as s:
        return [ProviderDeployment(
            external_id=d.external_id, deployed_at=_utc(d.deployed_at), sha=d.sha,
            environment=d.environment, branch=d.branch, source=d.source)
            for d in s.execute(select(ProductivityDeployment).where(
                ProductivityDeployment.workspace_id == workspace_id,
                ProductivityDeployment.provider == provider_name,
                ProductivityDeployment.repo == repo,
                ProductivityDeployment.source.in_(("provider", "tag")))).scalars()]


def rebuild_deployments(
    engine, ws: str, provider_name: str, repo: str, cfg: ProductivitySettings,
    provided=(), *, result: SyncResult | None = None,
) -> int:
    """Re-derive the repository's deployments and every PR's link to one."""
    with Session(engine) as s:
        rows = {r.number: r for r in s.execute(select(ProductivityPullRequest).where(
            ProductivityPullRequest.workspace_id == ws,
            ProductivityPullRequest.provider == provider_name,
            ProductivityPullRequest.repo == repo)).scalars()}
        facts = [deploys.PRFact(
            number=r.number, state=r.state, target_branch=r.target_branch,
            merged_at=_utc(r.merged_at), merge_commit_sha=r.merge_commit_sha, head_sha=r.head_sha,
            kind=r.kind, reverts_pr_number=r.reverts_pr_number,
            commit_shas=frozenset(r.commit_shas or ())) for r in rows.values()]
        plans, links = deploys.plan_deployments(facts, cfg, provided)

        # Upsert by (source, external_id): a deployment keeps its id from one
        # rebuild to the next, and only the ones that vanished are deleted.
        old = {(d.source, d.external_id): d for d in s.execute(select(ProductivityDeployment).where(
            ProductivityDeployment.workspace_id == ws,
            ProductivityDeployment.provider == provider_name,
            ProductivityDeployment.repo == repo)).scalars()}
        planned = {(p.source, p.external_id) for p in plans}
        gone = [d.id for key, d in old.items() if key not in planned]
        if old:
            s.execute(delete(ProductivityDeploymentPr).where(
                ProductivityDeploymentPr.deployment_id.in_([d.id for d in old.values()])))
        if gone:
            s.execute(delete(ProductivityDeployment).where(ProductivityDeployment.id.in_(gone)))
        ids: dict[str, str] = {}
        for plan in plans:
            dep = old.get((plan.source, plan.external_id))
            if dep is None:
                dep = ProductivityDeployment(
                    workspace_id=ws, provider=provider_name, repo=repo, source=plan.source,
                    external_id=plan.external_id)
                s.add(dep)
            dep.environment, dep.branch, dep.sha = plan.environment, plan.branch, plan.sha
            dep.deployed_at, dep.status, dep.is_failure = plan.deployed_at, "success", plan.is_failure
            dep.failed_by_pr_number, dep.recovered_at = plan.failed_by_pr_number, plan.recovered_at
            s.flush()
            ids[plan.external_id] = dep.id
            for number in sorted(plan.pr_numbers):
                s.add(ProductivityDeploymentPr(deployment_id=dep.id, pr_number=number))
        for number, row in rows.items():
            link = links.get(number)
            row.prod_deployed_at = link.deployed_at if link else None
            row.prod_deployment_id = ids.get(link.external_id) if link else None
            row.deploy_link = link.link if link else None
        s.commit()
    if result is not None:
        result.deployments = len(plans)
    return len(plans)


def _counts(engine, ws: str, provider_name: str, repo: str) -> tuple[int, int]:
    with Session(engine) as s:
        base = (ProductivityPullRequest.workspace_id == ws,
                ProductivityPullRequest.provider == provider_name,
                ProductivityPullRequest.repo == repo)
        total = s.execute(select(func.count()).select_from(ProductivityPullRequest).where(*base)).scalar_one()
        full = s.execute(select(func.count()).select_from(ProductivityPullRequest).where(
            *base, ProductivityPullRequest.detail_state == "full")).scalar_one()
    return int(total), int(full)


# ─── one run ──────────────────────────────────────────────────────────

def run_repo_sync(
    workspace_id: str, provider: str, repo: str, *,
    repo_slug: str | None = None, user_id: str = "default",
    provider_factory: ProviderFactory | None = None, engine=None,
    time_budget: float = DEFAULT_TIME_BUDGET, full: bool = False, force: bool = False,
    cancel_check: Callable[[], bool] | None = None, now: datetime | None = None,
) -> SyncResult:
    """Sync one repository until its work is done or its budget is spent. Never raises."""
    engine = get_engine(engine)
    now = now or datetime.now(UTC)
    result = SyncResult("ok")
    try:
        cfg = settings_mod.load(workspace_id, provider, repo, engine=engine)
        if not cfg.enabled and not force:
            return SyncResult("disabled")
        lease_until = now + timedelta(seconds=time_budget + _LEASE_SLACK)
        with Session(engine) as s:
            state = _state_row(s, workspace_id, provider, repo)
            until = _utc(state.rate_limited_until)
            if until and until > now:
                return SyncResult("rate_limited", more_work=True, resume_at=until)
            claimed = s.execute(update(ProductivitySyncState).where(
                ProductivitySyncState.workspace_id == workspace_id,
                ProductivitySyncState.provider == provider,
                ProductivitySyncState.repo == repo,
                (ProductivitySyncState.running_until.is_(None))
                | (ProductivitySyncState.running_until < now),
            ).values(running_until=lease_until),
                execution_options={"synchronize_session": False}).rowcount
            if not claimed:
                s.rollback()
                return SyncResult("busy")
            s.refresh(state)
            if full:
                state.updated_watermark = state.backfill_from = None
                state.backfill_done = False
                s.execute(update(ProductivityPullRequest).where(
                    ProductivityPullRequest.workspace_id == workspace_id,
                    ProductivityPullRequest.provider == provider,
                    ProductivityPullRequest.repo == repo,
                    ProductivityPullRequest.detail_state == "full",
                ).values(detail_state="partial"))
            state.rate_limited_until = None
            state.last_run_at = now
            s.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("productivity_sync_start_failed repo=%s err=%s", repo, exc)
        return SyncResult("error", error=str(exc)[:300])

    provider_obj: ProductivityProvider | None = None
    budget = _Budget(time_budget, cancel_check)
    error: str | None = None
    try:
        provider_obj = (provider_factory(cfg) if provider_factory else build_provider(
            workspace_id, provider, repo, user_id=user_id, rate_per_hour=cfg.rate_per_hour))
        budget.tune(provider_obj)
        listed = _list_phase(engine, workspace_id, provider, repo, repo_slug, cfg,
                             provider_obj, now, budget, result)
        detailed = listed and _detail_phase(
            engine, workspace_id, provider, repo, cfg, provider_obj, now, budget, result)
        if result.status != "cancelled":
            _release_phase(engine, workspace_id, provider, repo, cfg, provider_obj, budget, result)
            _resolve_reverts(engine, workspace_id, provider, repo)
            _deploy_phase(engine, workspace_id, provider, repo, cfg, provider_obj, now, result)
            if not (listed and detailed):
                result.status, result.more_work = "budget", True
    except RateLimited as limited:
        result.status, result.more_work, result.resume_at = "rate_limited", True, limited.until
        error = limited.reason
        with Session(engine) as s:
            _state_row(s, workspace_id, provider, repo).rate_limited_until = limited.until
            s.commit()
    except (ProviderError, httpx.HTTPError) as exc:
        result.status, error = "error", f"{type(exc).__name__}: {exc}"[:300]
        result.error = error
    except Exception as exc:  # noqa: BLE001
        logger.exception("productivity_sync_failed repo=%s", repo)
        result.status, error = "error", f"{type(exc).__name__}: {exc}"[:300]
        result.error = error
    finally:
        if provider_obj is not None:
            with contextlib.suppress(Exception):
                provider_obj.client.close()
        _finish(engine, workspace_id, provider, repo, result, error)
    return result


def _finish(engine, ws: str, provider: str, repo: str, result: SyncResult, error: str | None) -> None:
    try:
        total, full = _counts(engine, ws, provider, repo)
        with Session(engine) as s:
            state = _state_row(s, ws, provider, repo)
            state.running_until = None
            state.prs_total, state.prs_detailed, state.prs_pending = total, full, total - full
            state.last_error = error if result.status in ("error", "rate_limited") else result.error
            if result.status == "ok":
                state.last_ok_at = datetime.now(UTC)
            state.updated_at = datetime.now(UTC)
            s.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("productivity_sync_finish_failed repo=%s err=%s", repo, exc)


# ─── one PR ───────────────────────────────────────────────────────────

#: How long a single-PR refresh holds the repository: a handful of calls, not a sync slice.
_REFRESH_LEASE = 120.0


def _claim_lease(engine, ws: str, provider: str, repo: str, now: datetime, seconds: float) -> bool:
    """Take the repository's lease (`running_until`) when nobody holds it. The same claim `run_repo_sync` makes."""
    with Session(engine) as s:
        _state_row(s, ws, provider, repo)
        claimed = s.execute(update(ProductivitySyncState).where(
            ProductivitySyncState.workspace_id == ws, ProductivitySyncState.provider == provider,
            ProductivitySyncState.repo == repo,
            (ProductivitySyncState.running_until.is_(None)) | (ProductivitySyncState.running_until < now),
        ).values(running_until=now + timedelta(seconds=seconds)),
            execution_options={"synchronize_session": False}).rowcount
        s.commit()
        return bool(claimed)


def _release_lease(engine, ws: str, provider: str, repo: str) -> None:
    with contextlib.suppress(Exception), Session(engine) as s:
        _state_row(s, ws, provider, repo).running_until = None
        s.commit()


def refresh_pull_request(
    workspace_id: str, provider: str, repo: str, number: int, *,
    repo_slug: str | None = None, user_id: str = "default",
    provider_factory: ProviderFactory | None = None, engine=None, now: datetime | None = None,
) -> SyncResult:
    """Re-read one PR (after its merge or close) and re-derive the deployments.

    Holds the repository's lease while it rewrites the PR's events and the
    deployment links, so it cannot collide with a sync slice doing the same.
    A sync in progress means `busy`: the job is queued again a little later.
    """
    engine = get_engine(engine)
    now = now or datetime.now(UTC)
    cfg = settings_mod.load(workspace_id, provider, repo, engine=engine)
    if not cfg.enabled:
        return SyncResult("disabled")
    if not _claim_lease(engine, workspace_id, provider, repo, now, _REFRESH_LEASE):
        return SyncResult("busy", more_work=True)
    provider_obj: ProductivityProvider | None = None
    result = SyncResult("ok")
    try:
        provider_obj = (provider_factory(cfg) if provider_factory else build_provider(
            workspace_id, provider, repo, user_id=user_id, rate_per_hour=cfg.rate_per_hour))
        provider_obj.max_wait = DEFAULT_MAX_WAIT
        rec = provider_obj.get_pull_request(number)
        if rec is None:
            return SyncResult("ok")
        with Session(engine) as s:
            _upsert_page(s, workspace_id, provider, repo, [rec], cfg, repo_slug, now)
            row = _pr_row(s, workspace_id, provider, repo, number)
            if row is not None and rec.detail is None:
                apply_detail(s, row, provider_obj.pr_detail(rec), cfg, now)
            if row is not None:
                _remember_release_shas(provider_obj, row, cfg)
            s.commit()
        result.prs_detailed = 1
        _resolve_reverts(engine, workspace_id, provider, repo)
        _deploy_phase(engine, workspace_id, provider, repo, cfg, provider_obj, now, result,
                      stored_only=True)
    except RateLimited as limited:
        result.status, result.more_work, result.resume_at = "rate_limited", True, limited.until
    except (ProviderError, httpx.HTTPError, IntegrityError) as exc:
        result.status, result.error = "error", f"{type(exc).__name__}: {exc}"[:300]
    finally:
        if provider_obj is not None:
            with contextlib.suppress(Exception):
                provider_obj.client.close()
        _release_lease(engine, workspace_id, provider, repo)
    return result


# ─── webhooks ─────────────────────────────────────────────────────────

#: Lifecycle events that change what a PR's row says for good: refresh now.
_REFRESH_EVENTS = frozenset({"merged", "closed", "fulfilled", "rejected"})


def on_lifecycle_event(
    workspace_id: str, provider: str, repo: str, number: int, event: str, *,
    user_id: str = "default", repo_slug: str | None = None, engine=None,
    enqueue: Callable[..., Any] | None = None,
) -> str:
    """A webhook said something happened to a PR. Never raises.

    merged/closed queue an immediate single-PR refresh (deduplicated per PR, so
    a retried delivery is one job). Anything else — created, updated, approved,
    unapproved, reopened — only marks a known PR stale: the next tick re-reads
    it. A force-push storm therefore costs database writes, not API calls.
    Returns disabled | queued | stale | unknown.
    """
    try:
        engine = get_engine(engine)
        cfg = settings_mod.load(workspace_id, provider, repo, engine=engine)
        if not cfg.enabled:
            return "disabled"
        if event in _REFRESH_EVENTS:
            if enqueue is None:
                from src.sync import queue as jq

                enqueue = jq.enqueue
            from src.sync.queue import KIND_PRODUCTIVITY_PR

            enqueue(
                kind=KIND_PRODUCTIVITY_PR,
                payload={"workspace_id": workspace_id, "provider": provider, "repo": repo,
                         "number": int(number), "user_id": user_id, "repo_slug": repo_slug},
                dedup_key=f"prodpr:{workspace_id}:{repo}:{int(number)}",
                delay_seconds=10, workspace_id=workspace_id)
            return "queued"
        with Session(engine) as s:
            row = _pr_row(s, workspace_id, provider, repo, int(number))
            if row is None:
                return "unknown"
            if row.detail_state == "full":
                row.detail_state = "partial"
            s.commit()
        return "stale"
    except Exception as exc:  # noqa: BLE001
        logger.warning("productivity_event_failed repo=%s pr=%s err=%s", repo, number, exc)
        return "unknown"


# ─── jobs ─────────────────────────────────────────────────────────────

def enqueue_sync(
    workspace_id: str, provider: str, repo: str, *, user_id: str = "default",
    repo_slug: str | None = None, full: bool = False, delay_seconds: float = 0,
    continuation: bool | str = False, enqueue: Callable[..., Any] | None = None,
) -> str | None:
    """Queue one sync job for a repository. None when one is already queued.

    A continuation (the job queuing its own next slice) uses its own dedup key,
    which names the job that queues it (`continuation` is that job's id): the
    queue dedups against `running` rows too, so a key shared by every slice would
    be refused by the second slice, which is itself running under it.
    """
    if enqueue is None:
        from src.sync import queue as jq

        enqueue = jq.enqueue
    from src.sync.queue import KIND_PRODUCTIVITY_SYNC

    key = f"prodsync:{workspace_id}:{provider}:{repo}"
    if continuation:
        key = f"prodsync-next:{workspace_id}:{provider}:{repo}"
        if isinstance(continuation, str):
            key = f"{key}:{continuation}"
    return enqueue(
        kind=KIND_PRODUCTIVITY_SYNC,
        payload={"workspace_id": workspace_id, "provider": provider, "repo": repo,
                 "user_id": user_id, "repo_slug": repo_slug, "full": bool(full)},
        dedup_key=key, delay_seconds=delay_seconds, workspace_id=workspace_id)


def sync_status(workspace_id: str, *, engine=None) -> list[dict[str, Any]]:
    """Per-repository progress, for the sync panel."""
    engine = get_engine(engine)
    with Session(engine) as s:
        rows = s.execute(select(ProductivitySyncState).where(
            ProductivitySyncState.workspace_id == workspace_id
        ).order_by(ProductivitySyncState.repo)).scalars().all()
        return [{
            "provider": r.provider, "repo": r.repo,
            "watermark": _iso(r.updated_watermark), "backfill_from": _iso(r.backfill_from),
            "backfill_done": bool(r.backfill_done), "last_run_at": _iso(r.last_run_at),
            "last_ok_at": _iso(r.last_ok_at), "last_error": r.last_error,
            "rate_limited_until": _iso(r.rate_limited_until),
            "running": bool(r.running_until and _utc(r.running_until) > datetime.now(UTC)),
            "prs_total": r.prs_total, "prs_detailed": r.prs_detailed, "prs_pending": r.prs_pending,
        } for r in rows]


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value else None


def estimate_backfill(
    provider: ProductivityProvider, days: int, *, rate_per_hour: int = 500,
    now: datetime | None = None,
) -> dict[str, Any]:
    """What a backfill would cost, before anybody switches it on."""
    since = (now or datetime.now(UTC)) - timedelta(days=days)
    prs = provider.count_pull_requests(since)
    if prs is None:
        return {"prs": None, "requests": None, "hours": None}
    requests = prs * provider.requests_per_pr + max(1, prs // max(1, provider.page_size))
    return {"prs": prs, "requests": requests, "hours": round(requests / max(1, rate_per_hour), 1)}
