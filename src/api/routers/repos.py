"""Repository routes — list/add/remove + auto-review toggle + browse provider repos.

Browse-from-provider is provider-aware:
    GitHub:    GET /user/repos                  (name search: scan + filter)
    GitLab:    GET /projects?membership=true    (name search: `search=`)
    Bitbucket: GET /repositories/{workspace}    (name search: `q=name ~ "…"`)

The name search always spans every page, never just the one on screen —
otherwise it would answer "not found" for a repo that merely sits further down
the list.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auto_review import RepoConfig, get_auto_review_store
from src.api.deps import (
    client_ip,
    current_workspace_id,
    get_current_user,
    is_workspace_admin,
    require_repo_permission,
    require_workspace_admin,
)
from src.api.schemas import (
    AutoReviewToggle,
    BulkReviewIn,
    BulkReviewOut,
    OpenPullListOut,
    OpenPullOut,
    QueuedReviewOut,
    RepoAddRequest,
    RepoBranchesOut,
    RepoBranchUpdate,
    RepoBrowseItem,
    RepoDeveloperItem,
    RepoDeveloperScan,
    RepoOut,
    RepoOwnerItem,
    RepoWebhookOut,
)
from src.config import get_settings, is_valid_repo_slug
from src.credentials import resolve_git_credential
from src.db.session import get_async_session
from src.http import build_client
from src.security.audit import record_action
from src.sync.git_providers import GitProvider, parse_repo_url
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/repos", tags=["repos"])


# ─── List + add + remove ─────────────────────────────────────────────


def _graph_exists(slug: str) -> bool:
    """Is there a graph for ``slug``? False — never a raise — for a stored
    slug that is not one safe path segment (a row registered before slugs
    were validated): one such row must not take down a whole listing."""
    if not is_valid_repo_slug(slug):
        logger.warning("repo_slug_invalid_in_registry slug=%r", slug)
        return False
    return get_settings().repo_graph_path(slug).exists()


@router.get("", response_model=list[RepoOut])
def list_repos(
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> list[RepoOut]:
    """Repos registered in the ACTIVE workspace — every member sees the
    same list regardless of who registered each repo."""
    from src.repos.index_state import read_index_states

    store = get_auto_review_store()
    cfg_by_slug = {c.repo_slug: c for c in store.list_for_workspace(workspace_id)}

    # One query for the whole page, not one per row. `indexed` below is a
    # file-exists check: it cannot say WHEN the graph was built, at which
    # revision, or that the newest attempt died. cal.diy is the case that
    # matters — its clone failed six times on a dangling symlink (commit
    # ab2a864) and 10 of the 50 benchmark PRs went out with no graph, while
    # this list rendered it exactly like a repository nobody had asked to
    # index. `read_index_states` never raises; {} is "nothing recorded", which
    # renders the same as "no database here", because the repositories page
    # has to load without one.
    states = read_index_states(cfg_by_slug)
    hooks = _webhook_states(workspace_id)

    out: list[RepoOut] = []
    # Primary source: auto_review_config (user-registered repos)
    for slug, cfg in cfg_by_slug.items():
        st = states.get(slug)
        out.append(RepoOut(
            slug=slug,
            provider=cfg.provider,
            full_name=cfg.full_name,
            url=cfg.url,
            indexed=_graph_exists(slug),
            # Not counted here: opening every repo's graph to list N
            # repositories is the wrong trade. None says "not counted"; the
            # literal 0 it used to send said "counted, and empty".
            symbol_count=None,
            auto_review_enabled=cfg.enabled,
            auto_review_mode=cfg.mode,
            branch=cfg.branch,
            last_indexed_sha=st.last_indexed_sha if st else None,
            last_indexed_at=st.last_indexed_at if st else None,
            last_full_rebuild_at=st.last_full_rebuild_at if st else None,
            last_index_error=st.last_error if st else None,
            last_index_error_at=st.last_error_at if st else None,
            last_checked_at=st.last_checked_at if st else None,
            last_remote_sha=st.last_remote_sha if st else None,
            last_check_error=st.last_check_error if st else None,
            up_to_date=st.up_to_date if st else None,
            webhook=_webhook_out(hooks.get(slug)),
        ))
    return out


def _qualify_with_connected_provider(value: str, workspace_id: str,
                                     user_id: str) -> str:
    """`owner/name` means whichever provider this workspace is connected to.

    THE FORM INVITED A SPELLING THAT MEANT SOMETHING ELSE. Its own hint reads
    "Accepts: full URL, owner/name, or provider:owner/name", and a bare
    `owner/name` with no scheme parses as BITBUCKET — that default predates
    GitHub support and is relied on by the tests that own it. So a user who
    connected GitHub and typed exactly what the hint suggested got a repository
    registered under the wrong provider, whose index job then failed five times
    with "No bitbucket credentials" and died. The page showed `indexed: false`
    and no reason.

    Found by installing this product from its own README and following the
    interface instead of prior knowledge.

    The workspace already knows the answer: if exactly one Git provider is
    connected, an unqualified name belongs to it. Two connected providers make
    it genuinely ambiguous and the historical default stands — guessing between
    two right answers is worse than the documented one.

    A value that already names its provider — a URL, or `github:owner/name` —
    is returned untouched.
    """
    v = (value or "").strip()
    if not v or "://" in v or ":" in v.split("/", 1)[0]:
        return v
    try:
        from src.credentials import get_credential_store
        from src.credentials.git_keys import resolve_git_credential

        store = get_credential_store()
        connected = [
            p for p in ("github", "gitlab", "bitbucket")
            if resolve_git_credential(p, user_id=user_id,
                                      workspace_id=workspace_id, store=store)
        ]
    except Exception:  # noqa: BLE001 — an unreadable store decides nothing
        return v
    if len(connected) == 1:
        logger.info("repo_provider_inferred provider=%s from=%r",
                    connected[0], v)
        return f"{connected[0]}:{v}"
    return v


def _workspace_gitlab_base(workspace_id: str, user_id: str) -> str | None:
    """The workspace's self-hosted GitLab root, or None (gitlab.com / none /
    unreadable). Read from the workspace's OWN connection only."""
    try:
        from src.sync.gitlab_instance import instance_for_workspace

        inst = instance_for_workspace(workspace_id, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 — parse as gitlab.com
        logger.warning("gitlab_instance_unreadable ws=%s err=%s", workspace_id,
                       type(exc).__name__)
        return None
    return None if inst.is_default else inst.base_url


def _gitlab_of(creds: Any):
    """The GitLabInstance a stored GitLab credential belongs to; 400 when the
    stored URL is no longer usable (never silently gitlab.com — that would
    send a self-hosted token to gitlab.com)."""
    from src.sync.gitlab_instance import UnsafeGitLabURL, instance_for_credential

    try:
        return instance_for_credential(creds)
    except UnsafeGitLabURL as exc:
        raise HTTPException(
            status_code=400,
            detail=f"The saved GitLab URL is not usable: {exc}. Re-save the "
                   "GitLab connection.",
        ) from None


@router.post("", response_model=RepoOut, status_code=status.HTTP_201_CREATED)
def add_repo(
    request: Request,
    req: RepoAddRequest, user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> RepoOut:
    # QUALIFY ONCE, AND STORE WHAT WAS QUALIFIED. The first version of this
    # qualified the string for PARSING and then saved `req.url` unchanged — so
    # the slug said github and every later re-parse of the stored url said
    # bitbucket, and the clone went to an address that does not exist. Half a
    # fix is the same defect wearing the fix's name: one place corrected, the
    # stored value left alone, two derivations disagreeing.
    qualified = _qualify_with_connected_provider(req.url, workspace_id, user.id)
    gitlab_base = _workspace_gitlab_base(workspace_id, user.id)
    parsed = parse_repo_url(qualified, gitlab_base_url=gitlab_base)
    requested_branch = (req.branch or "").strip() or None
    if parsed.base_url:
        # A URL on the workspace's self-hosted GitLab is STORED in the
        # provider-prefixed form. Every later re-parse of the stored value —
        # clone, freshness, agent, groups — then says gitlab and the same
        # slug without needing to know the host, and the host itself always
        # comes from the workspace's connection, never from this string.
        qualified = f"gitlab:{parsed.owner}/{parsed.name}"
        requested_branch = requested_branch or parsed.branch_hint
    elif parsed.provider == GitProvider.GENERIC:
        raise HTTPException(
            status_code=422,
            detail=("This address is not on a connected git provider. For a "
                    "self-hosted GitLab, set its URL on the Connections page "
                    "first, then add the repository again."),
        )
    # Before anything is stored: the slug names the clone, graph and vault
    # directories, and a stored slug that is not one safe path segment would
    # make every later per-repo path lookup raise — the workspace's whole
    # repository list included.
    if not is_valid_repo_slug(parsed.slug):
        raise HTTPException(
            status_code=422,
            detail=(f"Unsupported repository name {parsed.slug!r}: only "
                    "letters, digits, '.', '_' and '-' (and no '..') are "
                    "supported."),
        )
    full_name = f"{parsed.owner}/{parsed.name}"
    store = get_auto_review_store()
    # Enforce a 1:1 repo->workspace binding so the unauthenticated webhook can
    # deterministically route this repo's events to exactly one tenant (and no
    # one can register another workspace's repo to intercept its reviews).
    bound = store.existing_workspace_binding(parsed.provider.value, full_name)
    if bound is not None and bound != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This repository is already registered in another workspace.",
        )
    # And again on the SLUG, which is the key everything downstream actually
    # uses — the clone, the graph and the vault are all slug-keyed. Two
    # different repositories can produce one slug, because ParsedRepo.slug
    # flattens the owner path with '-': acme/group/billing and
    # acme-group/billing collide. The check above cannot see that, because the
    # full names differ; without this one, two tenants end up sharing a vault
    # directory and each can read and rewrite the other's documentation.
    slug_bound = store.existing_slug_binding(parsed.slug)
    if slug_bound is not None and slug_bound != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=("Another repository in a different workspace already uses "
                    f"the local name {parsed.slug!r}. Rename or remove it "
                    "there first — the clone, graph and documentation are all "
                    "stored under that name."),
        )
    cfg = RepoConfig(
        user_id=user.id,
        repo_slug=parsed.slug,
        provider=parsed.provider.value,
        full_name=full_name,
        url=qualified,
        workspace_id=workspace_id,
        branch=requested_branch,
        enabled=req.auto_review,
        # Bitbucket cannot poll — force manual mode if user toggled auto for BB
        mode=_default_mode(parsed.provider.value, req.auto_review),
    )
    store.upsert(cfg)

    # Registering now queues the clone+graph index. It did not until this
    # line, and the gap was invisible: a script registered 50 forks here, no
    # one pressed "Index all", and all 161 Martian-bench review runs went out
    # with the literal string "(no graph context)" in place of the blast
    # radius. The enqueue is deliberately AFTER the upsert and cannot raise
    # (see queue_index_if_needed) — a queue problem must never cost the caller
    # the registration they just made.
    from src.repos.indexing import (
        INDEX_NOT_REQUESTED,
        INDEX_QUEUED,
        queue_index_if_needed,
    )

    index_status = (
        queue_index_if_needed(
            parsed.slug,
            workspace_id=workspace_id,
            user_id=user.id,
            enqueued_by=user.email,
        )
        if req.index
        else INDEX_NOT_REQUESTED
    )
    record_action(
        action="repo.registered", actor=user.email, actor_id=user.id,
        workspace_id=workspace_id, target=parsed.slug, ip=client_ip(request),
        detail={"provider": parsed.provider, "full_name": parsed.full_path,
                "index_queued": bool(req.index)},
    )
    logger.info("repo_registered ws=%s repo=%s index=%s by=%s",
                workspace_id, parsed.slug, index_status, user.email)
    webhook = _auto_install_on_register(cfg, user, workspace_id)
    return RepoOut(
        slug=parsed.slug,
        provider=parsed.provider.value,
        full_name=cfg.full_name,
        url=cfg.url,
        indexed=_graph_exists(parsed.slug),
        auto_review_enabled=cfg.enabled,
        auto_review_mode=cfg.mode,
        branch=cfg.branch,
        index_queued=index_status == INDEX_QUEUED,
        index_status=index_status,
        webhook=_webhook_out(webhook),
    )


@router.delete("/{slug}")
async def remove_repo(
    slug: str,
    request: Request,
    purge: bool = Query(
        False,
        description="Full purge: also delete clone, graph, vault notes, "
                    "Qdrant points, RepoGroup membership, ProjectRepo links, "
                    "review_runs. Default false — only this user's "
                    "auto_review_config row is removed.",
    ),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
    _perm: User = Depends(require_repo_permission("admin")),
):
    """Remove repo. Default — lightweight (unregister from the workspace).
    With ?purge=true — cascade delete across all stores. Returns a
    PurgeReport JSON if purge=true, else 204."""
    if not purge:
        store = get_auto_review_store()
        if not store.delete_in_workspace(workspace_id, slug):
            store.delete(user.id, slug)
        _forget_webhook_state(workspace_id, slug)
        # Registering a repository was audited; removing one was not. Half a
        # lifecycle in the file reads as a repository that is still connected
        # long after somebody disconnected it.
        record_action(
            action="repo.unregistered", actor=user.email, actor_id=user.id,
            workspace_id=workspace_id, target=slug, ip=client_ip(request),
            detail={"purge": False},
        )
        from fastapi import Response
        return Response(status_code=204)

    from src.db import async_session
    from src.repos.purge import purge_repo as do_purge

    async with async_session() as session:
        # The caller's workspace, not the slug alone: a slug is
        # `{provider}_{owner}-{name}` and is not unique across tenants, so a
        # purge scoped only by slug can take another workspace's vectors with
        # it.
        report = await do_purge(slug, session=session, workspace_id=workspace_id)
    _forget_webhook_state(workspace_id, slug)
    # A separate action from the unregister above, because it is a separate
    # event: this one destroys the clone, the graph, the vault notes, the
    # Qdrant points and the review history. "Repository removed" covering both
    # would make the irreversible one invisible among the reversible ones. The
    # report's own counts go in, so the row says how much went with it.
    record_action(
        action="repo.purged", actor=user.email, actor_id=user.id,
        workspace_id=workspace_id, target=slug, ip=client_ip(request),
        detail={"purge": True, **{
            k: v for k, v in report.as_dict().items() if isinstance(v, (int, bool))
        }},
    )
    return report.as_dict()


@router.post("/{slug}/index", response_model=RepoOut)
def trigger_index(
    slug: str,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> RepoOut:
    """Clone + tree-sitter index a repo. Synchronous (~5-60s for small repos).

    Graphs only — vault generation is a separate, explicitly-chosen step.
    """
    from src.repos.indexing import IndexError_, index_repo_sync

    # The ACTIVE workspace's registration only. A fallback to "a row this
    # user registered" used to follow, and that row may sit in another
    # workspace: somebody removed from workspace B kept indexing, listing PRs
    # and reading branches of B's repositories — with B's stored token, since
    # the row carries B's workspace id. Every by-slug route here asks the
    # same single question.
    store = get_auto_review_store()
    cfg = store.get_in_workspace(workspace_id, slug)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Repo not registered")
    try:
        result = index_repo_sync(slug, user_id=user.id, workspace_id=workspace_id)
    except IndexError_ as exc:
        message = str(exc)
        # "not registered" and a missing token are the caller's problem; a
        # failed clone or an empty graph is ours.
        status_code = 400 if message.startswith("No ") else (
            404 if message == "Repo not registered" else 500)
        raise HTTPException(status_code=status_code, detail=message) from exc

    return RepoOut(
        slug=cfg.repo_slug,
        provider=cfg.provider,
        full_name=cfg.full_name,
        url=cfg.url,
        indexed=True,
        symbol_count=result.symbols,
        auto_review_enabled=cfg.enabled,
        auto_review_mode=cfg.mode,
        branch=cfg.branch,
    )


class IndexAllOut(BaseModel):
    queued: int
    skipped: int
    #: WHICH repositories were skipped, because the count alone cannot be
    #: acted on. `{"queued": 0, "skipped": 4}` is a successful answer to a
    #: request that did nothing, and it reads the same whether every repo is
    #: already indexing (fine) or every one of them failed and was retried into
    #: the ground (not fine). Naming them lets the page say which.
    skipped_repos: list[str] = []
    #: Slugs that already had a graph and were left alone unless `force`.
    already_indexed: list[str] = []


class FreshnessOut(BaseModel):
    """What one look at the remote learned."""

    repo_slug: str
    #: up_to_date | behind | never_indexed | unreachable.
    #:
    #: Four, not two. "Could not reach the remote" and "nothing changed" are
    #: opposite answers that a two-state field renders identically, and the
    #: second one carries a fresh timestamp while being wrong.
    state: str
    remote_sha: str | None = None
    indexed_sha: str | None = None
    #: Set when the check queued an incremental re-index.
    reindex_queued: bool = False
    #: Why the check failed, redacted.
    detail: str | None = None


@router.post("/{slug}/check-freshness", response_model=FreshnessOut)
def check_freshness(
    slug: str,
    reindex: bool = Query(default=True),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> FreshnessOut:
    """Ask the remote now, and re-index if the branch moved.

    The manual half of the same mechanism the daily sweep and the push webhook
    use — one `check_repo`, so a button, a schedule and a webhook cannot come
    to three different conclusions about what "current" means.

    Synchronous because it is one `git ls-remote`: a network round trip, no
    clone and no parse. The re-index it may queue is the slow part, and that
    goes to the queue as it always did.

    `reindex=false` looks without touching anything — for a caller that wants
    the answer and not the consequence.
    """
    from src.repos.freshness import check_repo

    # The tenant check every by-slug route here does, and for the reason this
    # codebase has closed the same hole in channels, chats and projects: a
    # slug is not a permission. Without it, knowing another workspace's slug
    # would be enough to make this instance reach out with THEIR credential
    # and queue work in THEIR queue.
    store = get_auto_review_store()
    if (store.get_in_workspace(workspace_id, slug)) is None:
        raise HTTPException(status_code=404, detail="Repo not registered")

    result = check_repo(slug, workspace_id=workspace_id, user_id=user.id,
                        reindex=reindex)
    return FreshnessOut(
        repo_slug=result.repo_slug,
        state=result.state,
        remote_sha=result.remote_sha,
        indexed_sha=result.indexed_sha,
        reindex_queued=bool(result.reindex_job_id),
        detail=result.detail,
    )


@router.post("/index-all", response_model=IndexAllOut, status_code=202)
def index_all(
    force: bool = Query(default=False),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> IndexAllOut:
    """Queue a graph index for every repository in the workspace.

    Queued rather than synchronous: nine repos at 5-60s each is well past any
    request's patience, and the queue already gives retries, backoff and a
    cancel button. Vaults are NOT generated — indexing builds the graph, and
    chat and search answer from it without any vault existing.

    Repos that already have a graph are skipped unless `force`, so the button
    is safe to press twice.
    """
    from src.config import get_settings
    from src.repos.indexing import index_dedup_key
    from src.sync.queue import KIND_INDEX_REPO_FULL, enqueue

    settings = get_settings()
    configs = get_auto_review_store().list_for_workspace(workspace_id)
    queued, skipped, already = 0, 0, []
    skipped_repos: list[str] = []
    for cfg in configs:
        if not is_valid_repo_slug(cfg.repo_slug):
            skipped += 1
            skipped_repos.append(cfg.repo_slug)
            continue
        if not force and settings.repo_graph_path(cfg.repo_slug).exists():
            already.append(cfg.repo_slug)
            continue
        job_id = enqueue(
            kind=KIND_INDEX_REPO_FULL,
            payload={
                "repo_slug": cfg.repo_slug,
                "workspace_id": workspace_id,
                "user_id": user.id,
            },
            # One live index per repo per workspace, and the SAME key
            # registration uses — pressing the button while a registration's
            # own index is queued must not clone the repo a second time into
            # the same directory.
            dedup_key=index_dedup_key(cfg.repo_slug, workspace_id),
            enqueued_by=user.email,
        )
        if job_id is None:
            # An index for this repo is already pending or running — dedup by
            # the same key registration uses.
            skipped += 1
            skipped_repos.append(cfg.repo_slug)
        else:
            queued += 1
    logger.info("index_all ws=%s queued=%d skipped=%d already=%d by=%s",
                workspace_id, queued, skipped, len(already), user.email)
    return IndexAllOut(queued=queued, skipped=skipped,
                       skipped_repos=skipped_repos, already_indexed=already)


class GenerateVaultIn(BaseModel):
    """Optional per-run overrides for a vault build."""

    #: Documentation language for THIS run only, deliberately not persisted.
    #: "Generate this one in English for the customer" must not require
    #: changing a workspace setting and remembering to change it back — and a
    #: sticky override is an invisible setting, which is how you end up with a
    #: vault whose language nobody can account for. None → workspace setting.
    language: str | None = Field(default=None, max_length=16)
    #: Which engine writes the documents for THIS run. The real
    #: workflow is "the api engine swept the whole repository, now
    #: let the agent redo the five modules that matter", so this
    #: has to beat the workspace setting rather than replace it.
    engine: str | None = Field(default=None, max_length=32)
    #: Rebuild every document instead of resuming.
    #:
    #: `handle_generate_vault` has always read `payload["force"]` and nothing
    #: could ever set it, so a resume was the only build the product could ask
    #: for. That is fine until a build half-succeeds — notes on disk, no
    #: vectors — because a resume then regenerates nothing, re-embeds nothing,
    #: and reports success, and the user has no supported way out of the loop
    #: the chat banner keeps pointing them into.
    force: bool = False


def _self_hosted_generation_ready(workspace_id: str) -> bool:
    """Can vault generation run on a self-hosted (openai_compatible) profile?

    Generation runs on the *chat* profile (src/generation/engines.py), and the
    hosted-provider `has_key` gate above cannot see it: a keyless local server
    resolves to the "local-no-key" sentinel, so `has_key("openai_compatible")`
    would answer True for every workspace, local or not. Ask the profile
    instead — and only count it when the base URL is actually set, because
    without one every call refuses at dispatch time anyway (fail-closed in
    src/llm/completion.py) and the person should get a 400 now, not a job that
    dies ten minutes in.
    """
    try:
        from src.llm.profiles import resolve_profile

        p = resolve_profile("chat", workspace_id)
        return p.provider == "openai_compatible" and bool(p.api_base)
    except Exception:  # noqa: BLE001 — no profile blob → not a local install
        return False


@router.post("/{slug}/generate-vault", status_code=202)
def trigger_generate_vault(
    slug: str,
    payload: GenerateVaultIn | None = None,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    """Queue full vault generation (LLM notes + Qdrant embeddings) for a repo.

    This is the step that makes a repo usable in Projects/Q&A — the graph
    index alone only powers review/impact. Long-running (one LLM call per
    module), so it runs as a durable job; resume-mode makes re-runs cheap.
    Requires a resolvable LLM key (Connections/LLM Setup, or env).
    """
    store = get_auto_review_store()
    cfg = store.get_in_workspace(workspace_id, slug)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Repo not registered")

    from src.llm.keys import has_key
    if not (
        any(
            has_key(prov, workspace_id=ws)
            # "litellm" = a workspace LiteLLM proxy; has_key answers only for
            # a complete (base URL + key) pair.
            for prov in ("google", "openai", "anthropic", "litellm")
            for ws in dict.fromkeys((workspace_id, "default"))
        )
        or _self_hosted_generation_ready(workspace_id)
    ):
        raise HTTPException(
            status_code=400,
            detail="No LLM key available for generation — add one on LLM "
                   "Setup, or point the chat profile at a self-hosted "
                   "OpenAI-compatible server (its base URL is required).",
        )

    # Validated here rather than in the worker: a typo should be a 400 the
    # person sees now, not a job that runs for ten minutes and quietly falls
    # back to the workspace default.
    from src.generation.doc_language import resolve_doc_language
    from src.llm.prompts.language import DOC_LANGUAGES

    requested = (payload.language if payload else None) or None
    if requested is not None and requested not in DOC_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported documentation language {requested!r}. "
                   f"Expected one of: {', '.join(sorted(DOC_LANGUAGES))}.",
        )
    language = resolve_doc_language(requested, workspace_id)

    from src.generation.doc_language import resolve_doc_engine
    from src.generation.engines import ENGINES

    requested_engine = (payload.engine if payload else None) or None
    if requested_engine is not None and requested_engine not in ENGINES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported documentation engine {requested_engine!r}. "
                   f"Expected one of: {', '.join(ENGINES)}.",
        )
    doc_engine = resolve_doc_engine(requested_engine, workspace_id)

    from src.sync.queue import KIND_GENERATE_VAULT, enqueue
    job_id = enqueue(
        kind=KIND_GENERATE_VAULT,
        payload={"repo_url": cfg.url, "repo_slug": cfg.repo_slug,
                 "workspace_id": workspace_id, "language": language,
                 "engine": doc_engine, "user_id": user.id,
                 "force": bool(payload.force if payload else False)},
        # With the workspace, like index_full two handlers up: without it a
        # pending build in one tenant silently swallowed another's request.
        # A forced rebuild is a DIFFERENT request from a resume, so it must
        # not be swallowed by a pending resume's dedup key — which is exactly
        # what a stuck user would hit when they finally reach for it.
        dedup_key=(
            f"generate_vault:{workspace_id}:{cfg.repo_slug}"
            + (":force" if (payload and payload.force) else "")
        ),
        enqueued_by=user.email,
    )
    if job_id is None:
        return {
            "ok": True, "job_id": None, "language": language,
            "engine": doc_engine, "queued": False,
            "detail": "A vault build for this repository is already queued. "
                      "Nothing new was started.",
        }
    return {
        "ok": True, "job_id": job_id, "language": language,
        "engine": doc_engine, "queued": True,
        "detail": "Vault generation queued — this runs one LLM call per module "
                  "and can take several minutes. The repo appears in Projects "
                  "once embeddings land.",
    }


@router.patch("/{slug}/auto-review", response_model=RepoOut)
def toggle_auto_review(
    slug: str,
    req: AutoReviewToggle,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> RepoOut:
    store = get_auto_review_store()
    cfg = store.get_in_workspace(workspace_id, slug)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Repo not registered")
    cfg.enabled = req.enabled
    cfg.mode = _default_mode(cfg.provider, req.enabled, requested_mode=req.mode)
    store.upsert(cfg)
    return RepoOut(
        slug=cfg.repo_slug,
        provider=cfg.provider,
        full_name=cfg.full_name,
        url=cfg.url,
        indexed=_graph_exists(cfg.repo_slug),
        auto_review_enabled=cfg.enabled,
        auto_review_mode=cfg.mode,
        branch=cfg.branch,
    )


@router.patch("/{slug}/branch", response_model=RepoOut)
def set_repo_branch(
    slug: str,
    req: RepoBranchUpdate,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> RepoOut:
    """Change the branch this repo is cloned/indexed from.

    null (or an empty string) resets to the provider's default branch. The
    existing local clone still holds the OLD ref, so the caller must re-index
    — the UI says so; we only log the change here (blowing the clone away from
    a request handler would race a running index job).
    """
    store = get_auto_review_store()
    cfg = store.get_in_workspace(workspace_id, slug)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Repo not registered")

    new_branch = (req.branch or "").strip() or None
    previous = cfg.branch
    cfg.branch = new_branch
    store.upsert(cfg)
    logger.info(
        "repo_branch_changed ws=%s repo=%s from=%s to=%s by=%s",
        workspace_id, cfg.repo_slug, previous or "<default>",
        new_branch or "<default>", user.id,
    )

    return RepoOut(
        slug=cfg.repo_slug,
        provider=cfg.provider,
        full_name=cfg.full_name,
        url=cfg.url,
        indexed=_graph_exists(cfg.repo_slug),
        auto_review_enabled=cfg.enabled,
        auto_review_mode=cfg.mode,
        branch=cfg.branch,
    )


# ─── Browse-from-provider ────────────────────────────────────────────


@router.get("/browse/{provider}", response_model=list[RepoBrowseItem])
def browse_provider(
    provider: str,
    page: int = Query(default=1, ge=1, le=20),
    per_page: int = Query(default=30, ge=1, le=100),
    q: str | None = Query(
        default=None,
        max_length=100,
        description="Filter by repository name. Matched across ALL pages, not "
                    "just the requested one — an owner with dozens of repos "
                    "cannot be expected to page blindly to find one.",
    ),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> list[RepoBrowseItem]:
    """List repos accessible to the workspace's saved provider token."""
    if provider not in ("github", "gitlab", "bitbucket"):
        raise HTTPException(status_code=400, detail="Unknown provider")

    creds = resolve_git_credential(provider, user_id=user.id, workspace_id=workspace_id)
    if creds is None:
        raise HTTPException(
            status_code=400,
            detail=f"No {provider} token saved — connect via /api/connections",
        )

    store = get_auto_review_store()
    existing = {c.repo_slug for c in store.list_for_workspace(workspace_id)}
    query = (q or "").strip() or None

    if provider == "github":
        return _browse_github(creds.secret, page, per_page, existing, query)
    if provider == "gitlab":
        return _browse_gitlab(creds.secret, page, per_page, existing, query,
                              gitlab=_gitlab_of(creds))
    workspace = (creds.metadata or {}).get("bitbucket_workspace")
    email = (creds.metadata or {}).get("atlassian_email")
    if not workspace or not email:
        raise HTTPException(
            status_code=400,
            detail="Bitbucket connection missing workspace/email — re-save token",
        )
    return _browse_bitbucket(
        str(email), creds.secret, str(workspace), page, per_page, existing, query,
    )


#: Confirmed identity groupings, stored as a JSON blob in the credential store
#: under the workspace slot — the same shape the LLM config uses. Deliberately
#: not a table: it is a small per-workspace preference, and the last thing this
#: schema needs is another migration for one.
_ALIAS_TAG = "__developer_aliases__"
_ALIAS_LABEL = "default"


def _is_robot(identity: str) -> bool:
    from src.repos.identity import is_robot
    return is_robot(identity)


def _load_aliases(workspace_id: str) -> dict[str, list[str]]:
    """{label: [identity, …]} as someone confirmed it. Empty on first use."""
    import json as _json

    from src.credentials import get_credential_store
    from src.credentials.store import CredentialStoreError
    from src.llm.keys import workspace_slot

    try:
        row = get_credential_store().load(
            provider=_ALIAS_TAG,
            user_id=workspace_slot(workspace_id),
            account_label=_ALIAS_LABEL,
        )
    except CredentialStoreError:
        return {}
    if row is None:
        return {}
    try:
        data = _json.loads(row.secret)
    except Exception:  # noqa: BLE001
        return {}
    return {
        str(label): [str(m) for m in members if m]
        for label, members in (data or {}).items()
        if label and isinstance(members, list)
    }


def _group_identities(
    identities: list[str], workspace_id: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """(applied, suggested) — confirmed groupings win over guesses.

    `applied` is what the caller should fold rows by; `suggested` is what the
    UI offers as "these look like one person", so a guess is never silently
    acted on.
    """
    from src.repos.identity import apply_aliases, suggest_groups

    aliases = _load_aliases(workspace_id)
    suggested = suggest_groups(identities)
    confirmed = apply_aliases(identities, aliases)
    named = {m for members in aliases.values() for m in members}
    # An identity a person has already ruled on keeps their answer, whatever
    # the heuristic now thinks — including "on its own", which is a decision.
    applied = {
        i: (confirmed[i] if i in named else suggested.get(i, i))
        for i in identities
    }
    return applied, suggested


class DeveloperAliasesIn(BaseModel):
    """{label: [identity, …]}. An identity in no list stands alone."""

    groups: dict[str, list[str]] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")


@router.get("/developer-aliases")
def get_developer_aliases(
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, list[str]]:
    return _load_aliases(workspace_id)


@router.put("/developer-aliases")
def put_developer_aliases(
    payload: DeveloperAliasesIn,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, list[str]]:
    """Record which identities are one person. Replaces the whole mapping.

    Whole-mapping rather than per-group edits: this is a small object edited
    in one screen, and merging partial updates would make "I removed someone
    from this group" indistinguishable from "I did not mention them".
    """
    import json as _json

    from src.credentials import get_credential_store
    from src.llm.keys import workspace_slot

    groups = {
        label.strip(): sorted({m.strip() for m in members if m and m.strip()})
        for label, members in payload.groups.items()
        if label.strip()
    }
    # A group of one is what the mapping already means by default; storing it
    # would only make the blob grow every time someone opens the screen.
    groups = {label: members for label, members in groups.items() if len(members) > 1}

    get_credential_store().save(
        provider=_ALIAS_TAG,
        secret=_json.dumps(groups),
        metadata={"updated_by": user.email},
        user_id=workspace_slot(workspace_id),
        account_label=_ALIAS_LABEL,
    )
    logger.info("developer_aliases_saved ws=%s groups=%d by=%s",
                workspace_id, len(groups), user.email)
    return groups


#: Registered repos read live when they have no ownership snapshot. One
#: provider request each, on a page load, so it is bounded — a workspace with
#: more registered repos than this should build ownership properly.
_LIVE_AUTHOR_REPOS = 12


async def _developers_from_provider(
    slugs: list[str], user: User, workspace_id: str,
) -> list[tuple[str, str, int]]:
    """(identity, slug, commits) read from each repo's commit history.

    A stand-in for ownership that has not been built. Never raises: a repo the
    token cannot read contributes nothing, and the rest of the list still
    answers.
    """
    from concurrent.futures import ThreadPoolExecutor

    store = get_auto_review_store()
    by_slug = {c.repo_slug: c for c in store.list_for_workspace(workspace_id)}
    targets = [by_slug[s] for s in slugs[:_LIVE_AUTHOR_REPOS] if s in by_slug]

    creds_cache: dict[str, Any] = {}
    for cfg in targets:
        if cfg.provider not in creds_cache:
            creds_cache[cfg.provider] = resolve_git_credential(
                cfg.provider, user_id=user.id, workspace_id=workspace_id,
            )

    def one(cfg) -> list[tuple[str, str, int]]:
        creds = creds_cache.get(cfg.provider)
        if creds is None:
            return []
        found = _contributors(cfg.provider, cfg.full_name, creds, creds.metadata or {})
        return [(identity, cfg.repo_slug, n) for identity, _, n in found if identity]

    out: list[tuple[str, str, int]] = []
    if not targets:
        return out
    with ThreadPoolExecutor(max_workers=_DEVELOPER_SCAN_WORKERS) as pool:
        for rows in pool.map(one, targets):
            out.extend(rows)
    logger.info("developers_live_scan ws=%s repos=%d rows=%d",
                workspace_id, len(targets), len(out))
    return out


@router.get("/developers", response_model=list[RepoDeveloperItem])
async def registered_developers(
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> list[RepoDeveloperItem]:
    """People who commit to the repositories registered in this workspace.

    Read from the ownership snapshots the intel builder writes — real git
    authors over the last lookback window, not the account the repository
    happens to sit under. An organisation name is not a developer, and
    "audit everything Petro touches" is the question people actually have.

    Empty until ownership has been built for at least one repository
    (Repositories → Repo intel → Rebuild). That is reported as an empty list
    rather than an error: nothing is broken, the data simply is not there yet.
    """
    from src.db.models import OwnershipSnapshot

    slugs = [c.repo_slug for c in get_auto_review_store().list_for_workspace(workspace_id)]
    if not slugs:
        return []

    # Latest snapshot per repo. Ordering by computed_at and keeping the first
    # sighting is cheaper than a window function and reads the same.
    rows = (await session.scalars(
        select(OwnershipSnapshot)
        .where(OwnershipSnapshot.repo_slug.in_(slugs))
        .order_by(OwnershipSnapshot.repo_slug, OwnershipSnapshot.computed_at.desc())
    )).all()

    latest: dict[str, OwnershipSnapshot] = {}
    for row in rows:
        latest.setdefault(row.repo_slug, row)

    raw: list[tuple[str, str, int]] = []
    for slug, snap in latest.items():
        for owner in (snap.stats or {}).get("top_owners", []):
            identity = str(owner.get("identity") or "").strip()
            if identity:
                raw.append((identity, slug, int(owner.get("commits") or 0)))

    # Ownership is a separate build step (Repo intel → Rebuild), and nobody
    # guesses that: a repo is added, it says "indexed", and the developer
    # filter answers "0 repos". So repos without a snapshot are read straight
    # from the provider's commit history instead — the same source the browse
    # scan uses, and the answer a user expects to already be there.
    missing = [s for s in slugs if s not in latest]
    if missing:
        raw.extend(await _developers_from_provider(missing, user, workspace_id))

    # One person, several git configs. Folding happens here so every reader —
    # the picker, the scope, the count — sees the same person once.
    applied, _ = _group_identities(sorted({i for i, _, _ in raw}), workspace_id)

    commits: dict[str, int] = {}
    repos: dict[str, list[str]] = {}
    members: dict[str, set[str]] = {}
    for identity, slug, n in raw:
        label = applied.get(identity, identity)
        commits[label] = commits.get(label, 0) + n
        if slug not in repos.setdefault(label, []):
            repos[label].append(slug)
        members.setdefault(label, set()).add(identity)

    return [
        RepoDeveloperItem(
            identity=identity,
            aliases=sorted(members.get(identity, set()) - {identity}),
            repos=sorted(repos[identity]),
            repo_count=len(repos[identity]),
            commits=n,
            is_robot=_is_robot(identity),
        )
        # Most repos first, then most commits — someone spread across four
        # services is the one a scope is usually built around.
        for identity, n in sorted(
            commits.items(), key=lambda kv: (-len(repos[kv[0]]), -kv[1], kv[0].lower()),
        )
    ]


#: How deep the owner scan pages. Owners are a short list even when repos are
#: not — five pages of 100 is enough to name every account a normal token can
#: reach, and it bounds a request the user waits on with a spinner.
_OWNER_SCAN_PAGES = 5


@router.get("/browse/{provider}/owners", response_model=list[RepoOwnerItem])
def browse_provider_owners(
    provider: str,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> list[RepoOwnerItem]:
    """Accounts and organisations visible to the workspace's provider token.

    Typing an owner exactly right is the step people get wrong — a Bitbucket
    workspace id is not the display name, and an org is not the user who
    belongs to it. The owners are derived from the same listing the browser
    pages through, so anything offered here is guaranteed to select something.
    """
    if provider not in ("github", "gitlab", "bitbucket"):
        raise HTTPException(status_code=400, detail="Unknown provider")

    creds = resolve_git_credential(provider, user_id=user.id, workspace_id=workspace_id)
    if creds is None:
        raise HTTPException(
            status_code=400,
            detail=f"No {provider} token saved — connect via /api/connections",
        )

    registered = {
        c.full_name.rsplit("/", 1)[0].lower()
        for c in get_auto_review_store().list_for_workspace(workspace_id)
        if "/" in (c.full_name or "")
    }

    counts: dict[str, int] = {}

    def tally(full_name: str) -> None:
        # Everything before the LAST slash: a GitLab project can sit several
        # groups deep, and "group/subgroup" is the owner a prefix filter needs.
        if "/" not in full_name:
            return
        owner = full_name.rsplit("/", 1)[0]
        if owner:
            counts[owner] = counts.get(owner, 0) + 1

    try:
        if provider == "github":
            for page in range(1, _OWNER_SCAN_PAGES + 1):
                batch = _github_repos_page(creds.secret, page, 100)
                for r in batch:
                    tally(str(r.get("full_name", "")))
                if len(batch) < 100:
                    break
        elif provider == "gitlab":
            gl = _gitlab_of(creds)
            for page in range(1, _OWNER_SCAN_PAGES + 1):
                resp = _get(
                    f"{gl.api_base}/projects",
                    headers={"PRIVATE-TOKEN": creds.secret},
                    params={"membership": "true", "per_page": 100, "page": page},
                    timeout=15.0, gitlab=gl,
                )
                resp.raise_for_status()
                batch = list(resp.json())
                for p in batch:
                    tally(str(p.get("path_with_namespace", "")))
                if len(batch) < 100:
                    break
        else:
            meta = creds.metadata or {}
            workspace = str(meta.get("bitbucket_workspace") or "")
            email = str(meta.get("atlassian_email") or "")
            if not workspace or not email:
                raise HTTPException(
                    status_code=400,
                    detail="Bitbucket connection missing workspace/email — re-save token",
                )
            for page in range(1, _OWNER_SCAN_PAGES + 1):
                resp = _get(
                    f"https://api.bitbucket.org/2.0/repositories/{workspace}",
                    auth=(email, creds.secret),
                    params={"pagelen": 100, "page": page},
                    timeout=15.0,
                )
                resp.raise_for_status()
                payload = resp.json()
                values = payload.get("values", [])
                for r in values:
                    tally(str(r.get("full_name", "")))
                if not payload.get("next"):
                    break
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        # An expired token must not read as "this account owns nothing".
        logger.warning("owner_scan_failed provider=%s err=%s", provider, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Could not read the {provider} repository list.",
        ) from None

    return [
        RepoOwnerItem(
            owner=owner,
            repo_count=n,
            has_registered=owner.lower() in registered,
        )
        # Busiest first, then alphabetical — the owner someone means is
        # usually the one they have the most repos under.
        for owner, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    ]


#: Repositories asked for their contributor list in one browse scan.
#: One provider request each — the cost the user waits on — so the number is
#: reported back rather than hidden, and the scan says which repos it covered.
_DEVELOPER_SCAN_REPOS = 25
_DEVELOPER_SCAN_WORKERS = 8


def _contributors(provider: str, full_name: str, creds, meta: dict) -> list[tuple[str, str, int]]:
    """(identity, display_name, commits) for one repo. Never raises.

    A repo that refuses (archived, no access, rate-limited) contributes
    nothing rather than failing the whole scan — a partial list with an honest
    `scanned` count beats an error page.
    """
    try:
        if provider == "github":
            r = _get(
                f"https://api.github.com/repos/{full_name}/contributors",
                headers={"Authorization": f"Bearer {creds.secret}"},
                params={"per_page": 100, "anon": "false"}, timeout=15.0,
            )
            r.raise_for_status()
            return [
                (str(c.get("login") or ""), "", int(c.get("contributions") or 0))
                for c in r.json() if c.get("login")
            ]
        if provider == "gitlab":
            import urllib.parse as _u

            from src.sync.gitlab_instance import instance_for_credential
            gl = instance_for_credential(creds)
            pid = _u.quote(full_name, safe="")
            r = _get(
                f"{gl.api_base}/projects/{pid}/repository/contributors",
                headers={"PRIVATE-TOKEN": creds.secret},
                params={"per_page": 100}, timeout=15.0, gitlab=gl,
            )
            r.raise_for_status()
            return [
                (str(c.get("email") or c.get("name") or ""), str(c.get("name") or ""),
                 int(c.get("commits") or 0))
                for c in r.json() if c.get("email") or c.get("name")
            ]
        # Bitbucket has no contributors endpoint — the commit list is the only
        # source, so this is a sample of recent history rather than a census.
        email = str(meta.get("atlassian_email") or "")
        r = _get(
            f"https://api.bitbucket.org/2.0/repositories/{full_name}/commits",
            auth=(email, creds.secret), params={"pagelen": 100}, timeout=15.0,
        )
        r.raise_for_status()
        seen: dict[str, tuple[str, int]] = {}
        for commit in r.json().get("values", []):
            author = (commit.get("author") or {})
            userinfo = author.get("user") or {}
            raw = str(author.get("raw") or "")
            # "Name <mail@host>" → the mail, which is the stable identity;
            # display_name is what a person recognises.
            identity = raw.split("<")[-1].rstrip(">").strip() if "<" in raw else raw.strip()
            identity = identity or str(userinfo.get("display_name") or "")
            if not identity:
                continue
            name, n = seen.get(identity, (str(userinfo.get("display_name") or ""), 0))
            seen[identity] = (name, n + 1)
        return [(i, n, c) for i, (n, c) in seen.items()]
    except Exception as exc:  # noqa: BLE001
        logger.info("contributor_scan_skipped repo=%s err=%s", full_name, type(exc).__name__)
        return []


@router.get("/browse/{provider}/developers", response_model=RepoDeveloperScan)
def browse_provider_developers(
    provider: str,
    limit: int = Query(default=_DEVELOPER_SCAN_REPOS, ge=1, le=50),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> RepoDeveloperScan:
    """Who commits to the repositories this token can see, before adding any.

    The owner list answers "which account are these under" — usually one
    organisation, which is not a person. This answers "whose work is where",
    so a repository can be found by the developer who writes it.

    Bounded on purpose: contributors cost one request per repository, and a
    provider with sixty of them would otherwise hang the page. The response
    carries `scanned` and `total` so the UI can say which part was read.
    """
    if provider not in ("github", "gitlab", "bitbucket"):
        raise HTTPException(status_code=400, detail="Unknown provider")

    creds = resolve_git_credential(provider, user_id=user.id, workspace_id=workspace_id)
    if creds is None:
        raise HTTPException(
            status_code=400,
            detail=f"No {provider} token saved — connect via /api/connections",
        )
    meta = creds.metadata or {}

    # Same listing the browser pages through, so a developer found here always
    # points at a repository the user can actually add.
    try:
        if provider == "github":
            names = [str(r.get("full_name", ""))
                     for r in _github_repos_page(creds.secret, 1, 100)]
        elif provider == "gitlab":
            gl = _gitlab_of(creds)
            resp = _get(
                f"{gl.api_base}/projects",
                headers={"PRIVATE-TOKEN": creds.secret},
                params={"membership": "true", "per_page": 100}, timeout=15.0,
                gitlab=gl,
            )
            resp.raise_for_status()
            names = [str(p.get("path_with_namespace", "")) for p in resp.json()]
        else:
            workspace = str(meta.get("bitbucket_workspace") or "")
            email = str(meta.get("atlassian_email") or "")
            if not workspace or not email:
                raise HTTPException(
                    status_code=400,
                    detail="Bitbucket connection missing workspace/email — re-save token",
                )
            resp = _get(
                f"https://api.bitbucket.org/2.0/repositories/{workspace}",
                auth=(email, creds.secret),
                params={"pagelen": 100, "sort": "-updated_on"}, timeout=15.0,
            )
            resp.raise_for_status()
            names = [str(r.get("full_name", "")) for r in resp.json().get("values", [])]
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.warning("developer_scan_list_failed provider=%s err=%s", provider, exc)
        raise HTTPException(
            status_code=502, detail=f"Could not read the {provider} repository list.",
        ) from None

    names = [n for n in names if n]
    # Most recently updated first for Bitbucket, listing order elsewhere: the
    # repos someone is working in now are the ones worth scanning.
    targets = names[:limit]

    from concurrent.futures import ThreadPoolExecutor
    found_rows: list[tuple[str, str, str, int]] = []
    with ThreadPoolExecutor(max_workers=_DEVELOPER_SCAN_WORKERS) as pool:
        for full_name, found in zip(
            targets,
            pool.map(lambda fn: _contributors(provider, fn, creds, meta), targets),
            # pool.map yields exactly one result per target, in order.
            strict=True,
        ):
            for identity, name, n in found:
                found_rows.append((identity, name, full_name, n))

    # A provider mixes display names with commit emails in the same list, so
    # the same person arrives two or three times before any of our own data is
    # involved. Fold them the same way the registered-repo list does.
    applied, _ = _group_identities(
        sorted({i for i, _, _, _ in found_rows}), workspace_id,
    )
    commits: dict[str, int] = {}
    display: dict[str, str] = {}
    repos: dict[str, list[str]] = {}
    members: dict[str, set[str]] = {}
    for identity, name, full_name, n in found_rows:
        label = applied.get(identity, identity)
        commits[label] = commits.get(label, 0) + n
        if name and not display.get(label):
            display[label] = name
        if full_name not in repos.setdefault(label, []):
            repos[label].append(full_name)
        members.setdefault(label, set()).add(identity)

    developers = [
        RepoDeveloperItem(
            identity=identity,
            aliases=sorted(members.get(identity, set()) - {identity}),
            display_name=display.get(identity, ""),
            repos=sorted(repos[identity]),
            repo_count=len(repos[identity]),
            commits=n,
            is_robot=_is_robot(identity),
        )
        for identity, n in sorted(
            commits.items(), key=lambda kv: (-len(repos[kv[0]]), -kv[1], kv[0].lower()),
        )
    ]
    logger.info("developer_scan provider=%s scanned=%d/%d found=%d",
                provider, len(targets), len(names), len(developers))
    return RepoDeveloperScan(
        developers=developers, scanned=len(targets), total=len(names),
    )


# ─── Open PRs — list and review by hand (every provider) ─────────────
#
# The list used to be ONE provider page (50 PRs) presented as the whole list,
# and on Bitbucket it always sent Basic auth — so a workspace access token
# (Bearer) could trigger a review it could not list. Now: every open PR,
# any target branch, all pages (src/repos/open_pulls.py), searchable, with
# each PR's last Celmis review beside it; and reviews started from here go
# through the queue, so every one is recorded with its stages.


def _registered_repo(slug: str, workspace_id: str) -> RepoConfig:
    cfg = get_auto_review_store().get_in_workspace(workspace_id, slug)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Repo not registered")
    return cfg


def _repo_credential(cfg: RepoConfig, user: User) -> tuple[str, str, Any]:
    """(secret, atlassian e-mail, GitLab instance or None) for the repo's
    provider, or 400."""
    creds = resolve_git_credential(cfg.provider, user_id=user.id,
                                   workspace_id=cfg.workspace_id)
    if creds is None:
        raise HTTPException(
            status_code=400,
            detail=f"No {cfg.provider} token saved — connect first",
        )
    gitlab = _gitlab_of(creds) if cfg.provider == "gitlab" else None
    return (creds.secret, str((creds.metadata or {}).get("atlassian_email") or ""),
            gitlab)


def _open_listing(cfg: RepoConfig, secret: str, email: str, *,
                  branch: str | None, refresh: bool = False, gitlab: Any = None):
    from src.repos import open_pulls

    label = {"github": "GitHub", "gitlab": "GitLab",
             "bitbucket": "Bitbucket"}.get(cfg.provider, cfg.provider)
    try:
        return open_pulls.cached_open_pulls(
            cfg.provider, cfg.full_name, secret, email,
            target=branch or None, refresh=refresh,
            **({"gitlab": gitlab} if gitlab is not None else {}),
        )
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        logger.warning("open_pulls_failed repo=%s provider=%s status=%s",
                       cfg.repo_slug, cfg.provider, code)
        hint = (" — check the saved token's permissions" if code in (401, 403)
                else "")
        raise HTTPException(
            status_code=502,
            detail=f"{label} answered HTTP {code} to the open pull request "
                   f"listing{hint}",
        ) from None
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("open_pulls_failed repo=%s provider=%s err=%s",
                       cfg.repo_slug, cfg.provider, type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail=f"{label} could not be reached to list open pull requests",
        ) from None


@router.get("/{slug}/pulls", response_model=OpenPullListOut)
def list_open_prs(
    slug: str,
    branch: str | None = Query(
        default=None, max_length=255,
        description="Only PRs targeting this branch; empty = every branch"),
    q: str = Query(default="", max_length=200,
                   description="Title, #number, author or branch name"),
    sort: str = Query(default="newest", pattern="^(newest|recently_updated|oldest)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    refresh: bool = Query(default=False, description="Bypass the 30s cache"),
    user: User = Depends(require_repo_permission("read")),
    workspace_id: str = Depends(current_workspace_id),
) -> OpenPullListOut:
    """Every open PR/MR of a registered repo — any target branch, all pages —
    with search, an optional target-branch filter, and each PR's last review."""
    from src.api.review_runs import get_review_run_store
    from src.repos import open_pulls
    from src.review.dispatch import BULK_LIMIT

    cfg = _registered_repo(slug, workspace_id)
    secret, email, gitlab = _repo_credential(cfg, user)
    listing = _open_listing(cfg, secret, email, branch=branch, refresh=refresh,
                            gitlab=gitlab)
    chosen = open_pulls.select(listing.items, q=q, target=branch or None, sort=sort)
    page = chosen[offset:offset + limit]
    try:
        latest = get_review_run_store().latest_for_prs(
            cfg.workspace_id, cfg.provider, cfg.full_name, [p.number for p in page])
    except Exception as exc:  # noqa: BLE001 — the list is worth more than the badge
        logger.warning("open_pulls_last_review_unreadable repo=%s err=%s",
                       slug, type(exc).__name__)
        latest = {}
    items = []
    for p in page:
        run = latest.get(p.number)
        items.append(OpenPullOut(
            provider=cfg.provider, repo=cfg.full_name, number=p.number,
            title=p.title, author=p.author, url=p.url,
            created_at=p.created_at, updated_at=p.updated_at,
            source_branch=p.source_branch, target_branch=p.target_branch,
            draft=p.draft,
            last_review_status=run.status if run else None,
            last_review_reason=run.status_reason if run else None,
            last_run_id=run.id if run else None,
            last_review_at=run.started_at if run else None,
        ))
    return OpenPullListOut(
        items=items, total=len(chosen), open_total=len(listing.items),
        limit=limit, offset=offset, truncated=listing.truncated,
        target_branches=sorted({p.target_branch for p in listing.items
                                if p.target_branch}),
        bulk_limit=BULK_LIMIT,
    )


def _run_inline(payload: dict) -> None:
    """The queue is unavailable: run the review here, as the worker would."""
    from src.review.dispatch import execute_review

    try:
        execute_review(payload)
    except Exception:  # noqa: BLE001 — recorded on the run already
        logger.exception("manual_review_inline_failed run=%s", payload.get("run_id"))


def _queue_one(cfg: RepoConfig, number: int, *, user: User, post_comments: bool,
               source: str, background: BackgroundTasks) -> QueuedReviewOut:
    from src.review.dispatch import enqueue_review_run

    try:
        res = enqueue_review_run(
            cfg.provider, cfg.full_name, number, user_id=user.id,
            workspace_id=cfg.workspace_id, post_comments=post_comments,
            source=source,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("manual_review_enqueue_failed repo=%s pr=%s",
                         cfg.repo_slug, number)
        return QueuedReviewOut(number=number, status="failed",
                               reason=f"Could not record the review ({type(exc).__name__}).")
    if res.status == "inline":
        background.add_task(_run_inline, res.payload)
    return QueuedReviewOut(number=number, run_id=res.run_id, status=res.status,
                           reason=res.reason)


@router.post("/{slug}/pulls/{number}/review", response_model=QueuedReviewOut)
def review_open_pr(
    slug: str,
    number: int,
    background: BackgroundTasks,
    post_comments: bool = Query(default=True),
    user: User = Depends(require_repo_permission("review")),
    workspace_id: str = Depends(current_workspace_id),
) -> QueuedReviewOut:
    """Queue a review of one open PR. The run row exists before the job, so
    it shows as queued at once and its stages fill in as the worker goes."""
    if number < 1:
        raise HTTPException(status_code=422, detail="PR number must be positive")
    cfg = _registered_repo(slug, workspace_id)
    return _queue_one(cfg, number, user=user, post_comments=post_comments,
                      source="manual", background=background)


@router.post("/{slug}/pulls/review-all", response_model=BulkReviewOut)
def review_all_open_prs(
    slug: str,
    body: BulkReviewIn,
    request: Request,
    background: BackgroundTasks,
    user: User = Depends(require_repo_permission("review")),
    workspace_id: str = Depends(current_workspace_id),
) -> BulkReviewOut:
    """Queue a review of every open PR matching the page's filters (or the
    listed `numbers`), at most `BULK_LIMIT`, after explicit confirmation."""
    from src.repos import open_pulls
    from src.review.dispatch import BULK_LIMIT

    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="Bulk review needs confirmation (confirm: true).",
        )
    cfg = _registered_repo(slug, workspace_id)
    secret, email, gitlab = _repo_credential(cfg, user)
    # Fresh, not cached: this spends model budget on what it reads.
    listing = _open_listing(cfg, secret, email, branch=body.branch, refresh=True,
                            gitlab=gitlab)
    if body.numbers:
        open_numbers = {p.number for p in listing.items}
        targets = sorted({int(n) for n in body.numbers if int(n) in open_numbers})
    else:
        targets = [p.number for p in open_pulls.select(
            listing.items, q=body.q, target=body.branch or None)]
    if len(targets) > BULK_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=(f"{len(targets)} open pull requests match; at most "
                    f"{BULK_LIMIT} can be reviewed in one request — narrow the "
                    f"search or the target-branch filter."),
        )
    items = [
        _queue_one(cfg, n, user=user, post_comments=body.post_comments,
                   source="bulk", background=background)
        for n in targets
    ]
    queued = sum(1 for i in items if i.status in ("queued", "inline"))
    record_action(
        action="review.bulk_queued", actor=user.email, actor_id=user.id,
        workspace_id=workspace_id, target=slug, ip=client_ip(request),
        detail={"requested": len(targets), "queued": queued,
                "branch": body.branch or "", "q": bool(body.q)},
    )
    logger.info("bulk_review_queued repo=%s requested=%d queued=%d by=%s",
                slug, len(targets), queued, user.email)
    return BulkReviewOut(requested=len(targets), queued=queued, items=items)


@router.get("/{slug}/branches", response_model=RepoBranchesOut)
def list_branches(
    slug: str,
    q: str = Query(default="", max_length=200,
                   description="Case-insensitive substring of the branch name"),
    limit: int = Query(default=100, ge=1, le=1000),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> RepoBranchesOut:
    """Branches of a registered repo, for the branch pickers.

    Every page of the provider's listing (up to `BRANCH_CAP`), cached briefly
    per credential — see src/repos/branches.py. It used to read one page of
    100 and present it as the whole list.
    """
    from src.repos.branches import branch_page

    store = get_auto_review_store()
    cfg = store.get_in_workspace(workspace_id, slug)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Repo not registered")
    creds = resolve_git_credential(cfg.provider, user_id=user.id, workspace_id=cfg.workspace_id)
    if creds is None:
        return RepoBranchesOut(repo_slug=slug, branches=[], default_branch=None,
                               error="no_credential")
    email = str((creds.metadata or {}).get("atlassian_email") or "")
    try:
        from src.sync.gitlab_instance import gitlab_kwarg

        page = branch_page(cfg.provider, cfg.full_name, creds.secret, email,
                           q=q, limit=limit, **gitlab_kwarg(cfg.provider, creds))
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("branch_list_failed repo=%s provider=%s err=%s",
                       slug, cfg.provider, type(exc).__name__)
        return RepoBranchesOut(repo_slug=slug, branches=[], default_branch=None,
                               error="provider_error")
    return RepoBranchesOut(
        repo_slug=slug, branches=page.branches, default_branch=page.default_branch,
        total=page.total, truncated=page.truncated, source="provider",
    )


# ─── Review webhook: install / status / remove ───────────────────────
#
# Registering a repository with auto-review on used to stop at the database
# row: nothing told the provider to deliver, so nothing was ever reviewed and
# /pull-requests stayed empty with no reason given. These endpoints (and the
# automatic attempt in `add_repo`) install the webhook with the workspace's
# own git token — see src/review/webhook_install.py for the provider calls.


def _webhook_states(workspace_id: str) -> dict:
    """Last known webhook state per slug. Never raises: the list must render."""
    try:
        from src.review.webhook_install import get_webhook_state_store
        return get_webhook_state_store().for_workspace(workspace_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("webhook_state_unreadable ws=%s err=%s", workspace_id, exc)
        return {}


def _forget_webhook_state(workspace_id: str, slug: str) -> None:
    try:
        from src.review.webhook_install import get_webhook_state_store
        get_webhook_state_store().delete(workspace_id, slug)
    except Exception as exc:  # noqa: BLE001
        logger.warning("webhook_state_delete_failed ws=%s repo=%s err=%s",
                       workspace_id, slug, exc)


def _remember_webhook_state(workspace_id: str, slug: str, st: Any) -> None:
    try:
        from src.review.webhook_install import get_webhook_state_store
        get_webhook_state_store().save(workspace_id, slug, st)
    except Exception as exc:  # noqa: BLE001
        logger.warning("webhook_state_save_failed ws=%s repo=%s err=%s",
                       workspace_id, slug, exc)


def _webhook_out(st: Any) -> RepoWebhookOut | None:
    if st is None:
        return None
    return RepoWebhookOut(**st.as_dict())


def _bind_to_installed_hook(cfg: RepoConfig, st: Any) -> None:
    """Make the auto-review row agree with the hook that now exists.

    The receiver drops a delivery for a repository that is not bound, or bound
    but switched off — so a hook installed for a disabled row would deliver
    into nothing, which is the exact failure this feature exists to end. The
    provider's own spelling of the name is stored when it differs only in
    case, so the row matches what the payload will carry byte for byte (the
    lookup is case-insensitive too; this keeps the two agreeing everywhere
    else the name is compared or shown).
    """
    canonical = st.full_name or cfg.full_name
    if canonical != cfg.full_name:
        if canonical.lower() == cfg.full_name.lower():
            cfg.full_name = canonical
        else:
            # Renamed or transferred at the provider: deliveries will name the
            # new repository and not match this row. Say so instead of
            # rewriting a binding the user did not ask to move.
            st.message = (
                f"The provider calls this repository {canonical!r}, but it is "
                f"registered here as {cfg.full_name!r}; deliveries will not "
                "match until it is re-registered under the new name."
            )
    cfg.enabled = True
    cfg.mode = "webhook"
    get_auto_review_store().upsert(cfg)


def _install_webhook(cfg: RepoConfig, user_id: str) -> Any:
    """Install (or repair), bind, remember. Returns a WebhookStatus."""
    from src.review import webhook_install as wi

    base, problem = wi.public_base_url()
    if base is None:
        st = wi.WebhookStatus(
            provider=cfg.provider, status="skipped",
            events=wi.EVENTS.get(cfg.provider, []),
            reason="no_public_url", message=problem,
        )
    else:
        st = wi.install(cfg, user_id=user_id, base=base)
        if st.status == "installed":
            _bind_to_installed_hook(cfg, st)
    _remember_webhook_state(cfg.workspace_id, cfg.repo_slug, st)
    return st


def _auto_install_on_register(cfg: RepoConfig, user: User, workspace_id: str) -> Any:
    """The automatic attempt at registration. Never raises — a webhook problem
    must not cost the caller the registration they just made."""
    try:
        from src.review import webhook_install as wi

        events = wi.EVENTS.get(cfg.provider, [])
        if not cfg.enabled:
            return wi.WebhookStatus(
                provider=cfg.provider, status="skipped", events=events,
                reason="auto_review_disabled",
                message="Auto-review is off for this repository, so no webhook "
                        "was installed.")
        base, problem = wi.public_base_url()
        if base is None:
            st = wi.WebhookStatus(
                provider=cfg.provider, status="skipped", events=events,
                reason="no_public_url", message=problem)
            _remember_webhook_state(workspace_id, cfg.repo_slug, st)
            return st
        # Installing writes to the provider with the WORKSPACE's token and
        # creates the workspace's webhook secret: an admin's action, the same
        # bar the explicit endpoint holds.
        if not is_workspace_admin(user, workspace_id):
            st = wi.WebhookStatus(
                provider=cfg.provider, status="skipped", events=events,
                reason="not_admin",
                message="Only a workspace owner or admin can install the "
                        "webhook. Ask one to press Install webhook.")
            _remember_webhook_state(workspace_id, cfg.repo_slug, st)
            return st
        return _install_webhook(cfg, user.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("webhook_auto_install_failed ws=%s repo=%s err=%s",
                       workspace_id, cfg.repo_slug, type(exc).__name__)
        return None


def _repo_or_404(workspace_id: str, slug: str) -> RepoConfig:
    cfg = get_auto_review_store().get_in_workspace(workspace_id, slug)
    if cfg is None:
        # 404 for "registered in another workspace" too: whether some other
        # tenant has this slug is not this caller's business.
        raise HTTPException(status_code=404, detail="Repo not registered")
    return cfg


def _require_public_base() -> str:
    from src.review.webhook_install import public_base_url

    base, problem = public_base_url()
    if base is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=problem)
    return base


@router.post("/{slug}/webhook", response_model=RepoWebhookOut)
def install_repo_webhook(
    slug: str,
    request: Request,
    user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> RepoWebhookOut:
    """Install or repair the review webhook on the provider.

    Idempotent: an existing hook with our URL is updated, never duplicated.
    A provider refusal (token without webhook permission, …) is answered as
    `status: "failed"` with `reason`, `message` and `hint` — 200, because the
    request was understood and the answer is about the provider, not the call.
    409 when PUBLIC_BASE_URL is not usable.
    """
    cfg = _repo_or_404(workspace_id, slug)
    _require_public_base()
    st = _install_webhook(cfg, user.id)
    record_action(
        action="repo.webhook_installed" if st.status == "installed"
        else "repo.webhook_install_failed",
        actor=user.email, actor_id=user.id, workspace_id=workspace_id,
        target=slug, ip=client_ip(request),
        detail={"provider": cfg.provider, "status": st.status,
                "reason": st.reason, "action": st.action},
    )
    return RepoWebhookOut(**st.as_dict())


@router.get("/{slug}/webhook", response_model=RepoWebhookOut)
def get_repo_webhook(
    slug: str,
    live: bool = Query(True, description="Ask the provider (false: last known state)"),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> RepoWebhookOut:
    """Is our webhook on the provider, with which events, and — where the
    provider says — how did the last delivery go."""
    from src.review import webhook_install as wi

    cfg = _repo_or_404(workspace_id, slug)
    base, problem = wi.public_base_url()
    known = _webhook_states(workspace_id).get(slug)
    if base is None:
        return RepoWebhookOut(
            provider=cfg.provider, status=known.status if known else "unknown",
            events=wi.EVENTS.get(cfg.provider, []), reason="no_public_url",
            message=problem,
        )
    if not live:
        if known is not None:
            return RepoWebhookOut(**known.as_dict())
        return RepoWebhookOut(provider=cfg.provider, status="unknown",
                              url=wi.webhook_url(base, cfg.provider, workspace_id),
                              events=wi.EVENTS.get(cfg.provider, []))
    st = wi.status(cfg, user_id=user.id, base=base)
    if st.status in ("installed", "not_installed"):
        _remember_webhook_state(workspace_id, slug, st)
    return RepoWebhookOut(**st.as_dict())


@router.delete("/{slug}/webhook", response_model=RepoWebhookOut)
def delete_repo_webhook(
    slug: str,
    request: Request,
    user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> RepoWebhookOut:
    """Remove our webhook from the provider (only ours — matched by URL).

    Auto-review stays as it was; only the delivery mode falls back to what the
    provider supports without a webhook (polling, or manual on Bitbucket).
    """
    from src.review import webhook_install as wi

    cfg = _repo_or_404(workspace_id, slug)
    base = _require_public_base()
    st = wi.uninstall(cfg, user_id=user.id, base=base)
    if st.status == "not_installed" and cfg.mode == "webhook":
        cfg.mode = _default_mode(cfg.provider, cfg.enabled)
        get_auto_review_store().upsert(cfg)
    _remember_webhook_state(workspace_id, slug, st)
    record_action(
        action="repo.webhook_removed", actor=user.email, actor_id=user.id,
        workspace_id=workspace_id, target=slug, ip=client_ip(request),
        detail={"provider": cfg.provider, "status": st.status, "reason": st.reason},
    )
    return RepoWebhookOut(**st.as_dict())


# ─── helpers ─────────────────────────────────────────────────────────


def _get(url: str, *, timeout: float = 15.0, gitlab: Any = None,
         **kwargs: Any) -> httpx.Response:
    """One guarded GET against a git provider's API.

    Replaces the raw ``httpx.get`` verbs this router grew: each of those
    built an ephemeral, allowlist-blind client inside httpx per call. Same
    per-call lifecycle, same defaults — the only change is the egress
    whitelist transport, and every host this router talks to (api.github.com,
    gitlab.com, api.bitbucket.org) is on the shipped public allowlist.

    `gitlab` — the workspace's self-hosted GitLabInstance: its host becomes
    the client's one extra allowed destination, pinned to the address that
    was validated just now. Derived from the workspace's credential, never
    from the request.
    """
    extra: dict[str, Any] = {}
    if gitlab is not None:
        from src.sync.gitlab_instance import UnsafeGitLabURL

        try:
            extra = gitlab.http_kwargs()
        except UnsafeGitLabURL as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
    with build_client(timeout=timeout, **extra) as client:
        return client.get(url, **kwargs)



def _default_mode(provider: str, enabled: bool, *, requested_mode: str | None = None) -> str:
    """Bitbucket auto-review enables manual mode; others default to polling."""
    if not enabled:
        return "manual"
    if provider == "bitbucket":
        return "manual"
    return requested_mode or "polling"


def _bare_name(query: str) -> str:
    """Drop an `owner/` prefix from a search term.

    GitLab's `search` and Bitbucket's `name ~ …` both match the repository
    name alone, so a user who types what they see in the list
    ("acme/web-ui-ai") would otherwise get zero hits. A half-typed
    "acme/" must still search for the owner, not for the empty string.
    """
    trimmed = query.strip().rstrip("/")
    return trimmed.rsplit("/", 1)[-1].strip() or trimmed


# How deep a name search digs into GitHub's listing. 5 × 100 covers every
# realistic account while capping the cost of one keystroke-triggered request.
_GITHUB_SEARCH_PAGES = 5


def _github_repos_page(token: str, page: int, per_page: int) -> list[dict[str, Any]]:
    resp = _get(
        "https://api.github.com/user/repos",
        headers={"Authorization": f"Bearer {token}"},
        params={"per_page": per_page, "page": page, "sort": "updated"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return list(resp.json())


def _browse_github(
    token: str, page: int, per_page: int, existing: set[str],
    query: str | None = None,
) -> list[RepoBrowseItem]:
    data: list[dict[str, Any]]
    if query:
        # GET /user/repos takes no search parameter, and /search/repositories
        # is a different corpus — it answers with strangers' public repos
        # unless every user:/org: the token can reach is spelled out. Scanning
        # the very listing the user would otherwise page through by hand keeps
        # the searched set identical to the browsed set.
        needle = query.lower()
        matches: list[dict[str, Any]] = []
        for scan_page in range(1, _GITHUB_SEARCH_PAGES + 1):
            batch = _github_repos_page(token, scan_page, 100)
            matches.extend(
                r for r in batch
                if needle in str(r.get("full_name", "")).lower()
            )
            if len(batch) < 100:
                break
        start = (page - 1) * per_page
        data = matches[start:start + per_page]
    else:
        data = _github_repos_page(token, page, per_page)
    return [
        RepoBrowseItem(
            full_name=str(r.get("full_name", "")),
            url=str(r.get("html_url", "")),
            description=str(r.get("description") or ""),
            private=bool(r.get("private")),
            default_branch=str(r.get("default_branch") or "main"),
            already_added=_full_name_to_slug("github", str(r.get("full_name", ""))) in existing,
        )
        for r in data
    ]


def _browse_gitlab(
    token: str, page: int, per_page: int, existing: set[str],
    query: str | None = None, *, gitlab: Any = None,
) -> list[RepoBrowseItem]:
    from src.sync.gitlab_instance import DEFAULT_INSTANCE

    gitlab = gitlab or DEFAULT_INSTANCE
    # NB: skip `order_by=last_activity_at` — the GitLab API returns 500 for it
    # on membership=true queries (an existing transient bug, checked May 2026).
    # The default order (id desc) gives acceptable UX.
    params: dict[str, str | int] = {
        "membership": "true", "per_page": per_page, "page": page,
    }
    if query:
        # GitLab narrows the whole membership set server-side, so paging stays
        # meaningful; `search_namespaces` lets the owner prefix count as a hit.
        params["search"] = _bare_name(query)
        params["search_namespaces"] = "true"
    resp = _get(
        f"{gitlab.api_base}/projects",
        headers={"PRIVATE-TOKEN": token},
        params=params,
        timeout=15.0, gitlab=gitlab,
    )
    resp.raise_for_status()
    data: list[dict[str, Any]] = resp.json()
    return [
        RepoBrowseItem(
            full_name=str(p.get("path_with_namespace", "")),
            url=str(p.get("web_url", "")),
            description=str(p.get("description") or ""),
            private=p.get("visibility") != "public",
            default_branch=str(p.get("default_branch") or "main"),
            already_added=_full_name_to_slug(
                "gitlab", str(p.get("path_with_namespace", "")),
            ) in existing,
        )
        for p in data
    ]


def _browse_bitbucket(
    email: str, token: str, workspace: str,
    page: int, per_page: int, existing: set[str],
    query: str | None = None,
) -> list[RepoBrowseItem]:
    params: dict[str, str | int] = {
        "pagelen": per_page, "page": page, "sort": "-updated_on",
    }
    if query:
        # BBQL is a quoted mini-language: an unescaped " or \ from the search
        # box would not just fail to match, it would 400 the whole listing.
        term = _bare_name(query).replace("\\", "").replace('"', "")
        if term:
            params["q"] = f'name ~ "{term}"'
    resp = _get(
        f"https://api.bitbucket.org/2.0/repositories/{workspace}",
        auth=(email, token),
        params=params,
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    out: list[RepoBrowseItem] = []
    for r in data.get("values", []):
        full_name = str(r.get("full_name", ""))
        out.append(RepoBrowseItem(
            full_name=full_name,
            url=str((r.get("links", {}).get("html", {}) or {}).get("href", "")),
            description=str(r.get("description") or ""),
            private=bool(r.get("is_private")),
            default_branch=str((r.get("mainbranch") or {}).get("name") or "main"),
            already_added=_full_name_to_slug("bitbucket", full_name) in existing,
        ))
    return out


def _full_name_to_slug(provider: str, full_name: str) -> str:
    """Convert 'owner/name' to internal slug used by parse_repo_url."""
    if not full_name:
        return ""
    return parse_repo_url(f"{provider}:{full_name}").slug
