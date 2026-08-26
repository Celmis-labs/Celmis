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
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auto_review import RepoConfig, get_auto_review_store
from src.api.deps import (
    client_ip,
    current_workspace_id,
    get_current_user,
    require_repo_permission,
)
from src.api.schemas import (
    AutoReviewToggle,
    PullRequestSummary,
    RepoAddRequest,
    RepoBranchUpdate,
    RepoBrowseItem,
    RepoDeveloperItem,
    RepoDeveloperScan,
    RepoOut,
    RepoOwnerItem,
)
from src.config import get_settings
from src.credentials import resolve_git_credential
from src.db.session import get_async_session
from src.http import build_client
from src.security.audit import record_action
from src.sync.git_providers import parse_repo_url
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/repos", tags=["repos"])


# ─── List + add + remove ─────────────────────────────────────────────


@router.get("", response_model=list[RepoOut])
def list_repos(
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> list[RepoOut]:
    """Repos registered in the ACTIVE workspace — every member sees the
    same list regardless of who registered each repo."""
    from src.repos.index_state import read_index_states

    settings = get_settings()
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

    out: list[RepoOut] = []
    # Primary source: auto_review_config (user-registered repos)
    for slug, cfg in cfg_by_slug.items():
        graph_path = settings.repo_graph_path(slug)
        st = states.get(slug)
        out.append(RepoOut(
            slug=slug,
            provider=cfg.provider,
            full_name=cfg.full_name,
            url=cfg.url,
            indexed=graph_path.exists(),
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


@router.post("", response_model=RepoOut, status_code=status.HTTP_201_CREATED)
def add_repo(
    request: Request,
    req: RepoAddRequest, user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> RepoOut:
    parsed = parse_repo_url(
        _qualify_with_connected_provider(req.url, workspace_id, user.id)
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
        url=req.url,
        workspace_id=workspace_id,
        branch=(req.branch or "").strip() or None,
        enabled=req.auto_review,
        # Bitbucket cannot poll — force manual mode if user toggled auto for BB
        mode=_default_mode(parsed.provider.value, req.auto_review),
    )
    store.upsert(cfg)
    settings = get_settings()

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
    return RepoOut(
        slug=parsed.slug,
        provider=parsed.provider.value,
        full_name=cfg.full_name,
        url=cfg.url,
        indexed=settings.repo_graph_path(parsed.slug).exists(),
        auto_review_enabled=cfg.enabled,
        auto_review_mode=cfg.mode,
        branch=cfg.branch,
        index_queued=index_status == INDEX_QUEUED,
        index_status=index_status,
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

    store = get_auto_review_store()
    cfg = store.get_in_workspace(workspace_id, slug) or store.get(user.id, slug)
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
    cfg = store.get_in_workspace(workspace_id, slug) or store.get(user.id, slug)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Repo not registered")

    from src.llm.keys import has_key
    if not (
        any(
            has_key(prov, workspace_id=ws)
            for prov in ("google", "openai", "anthropic")
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
    cfg = store.get_in_workspace(workspace_id, slug) or store.get(user.id, slug)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Repo not registered")
    cfg.enabled = req.enabled
    cfg.mode = _default_mode(cfg.provider, req.enabled, requested_mode=req.mode)
    store.upsert(cfg)
    settings = get_settings()
    return RepoOut(
        slug=cfg.repo_slug,
        provider=cfg.provider,
        full_name=cfg.full_name,
        url=cfg.url,
        indexed=settings.repo_graph_path(cfg.repo_slug).exists(),
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
    cfg = store.get_in_workspace(workspace_id, slug) or store.get(user.id, slug)
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

    settings = get_settings()
    return RepoOut(
        slug=cfg.repo_slug,
        provider=cfg.provider,
        full_name=cfg.full_name,
        url=cfg.url,
        indexed=settings.repo_graph_path(cfg.repo_slug).exists(),
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
        return _browse_gitlab(creds.secret, page, per_page, existing, query)
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
            for page in range(1, _OWNER_SCAN_PAGES + 1):
                resp = _get(
                    "https://gitlab.com/api/v4/projects",
                    headers={"PRIVATE-TOKEN": creds.secret},
                    params={"membership": "true", "per_page": 100, "page": page},
                    timeout=15.0,
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
            pid = _u.quote(full_name, safe="")
            r = _get(
                f"https://gitlab.com/api/v4/projects/{pid}/repository/contributors",
                headers={"PRIVATE-TOKEN": creds.secret},
                params={"per_page": 100}, timeout=15.0,
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
            resp = _get(
                "https://gitlab.com/api/v4/projects",
                headers={"PRIVATE-TOKEN": creds.secret},
                params={"membership": "true", "per_page": 100}, timeout=15.0,
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


# ─── Bitbucket — list open PRs (manual mode) ─────────────────────────


@router.get("/{slug}/pulls", response_model=list[PullRequestSummary])
def list_open_prs(
    slug: str,
    branch: str | None = Query(default=None, description="Filter by target branch"),
    sort: str = Query(default="newest", pattern="^(newest|recently_updated|oldest)$"),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> list[PullRequestSummary]:
    """Return open PRs/MRs for a registered repo with optional branch + sort."""
    store = get_auto_review_store()
    cfg = store.get_in_workspace(workspace_id, slug) or store.get(user.id, slug)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Repo not registered")
    creds = resolve_git_credential(cfg.provider, user_id=user.id, workspace_id=cfg.workspace_id)
    if creds is None:
        raise HTTPException(
            status_code=400,
            detail=f"No {cfg.provider} token saved — connect first",
        )

    if cfg.provider == "github":
        return _list_pulls_github(cfg.full_name, creds.secret, branch=branch, sort=sort)
    if cfg.provider == "gitlab":
        return _list_pulls_gitlab(cfg.full_name, creds.secret, branch=branch, sort=sort)
    if cfg.provider == "bitbucket":
        email = (creds.metadata or {}).get("atlassian_email") or ""
        return _list_pulls_bitbucket(
            cfg.full_name, str(email), creds.secret, branch=branch, sort=sort,
        )
    return []


@router.get("/{slug}/branches", response_model=list[str])
def list_branches(
    slug: str,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> list[str]:
    """List target branches for a repo (for branch filter dropdown)."""
    store = get_auto_review_store()
    cfg = store.get_in_workspace(workspace_id, slug) or store.get(user.id, slug)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Repo not registered")
    creds = resolve_git_credential(cfg.provider, user_id=user.id, workspace_id=cfg.workspace_id)
    if creds is None:
        return []
    try:
        if cfg.provider == "github":
            r = _get(
                f"https://api.github.com/repos/{cfg.full_name}/branches",
                headers={"Authorization": f"Bearer {creds.secret}"},
                params={"per_page": 100}, timeout=15.0,
            )
            r.raise_for_status()
            return [str(b.get("name", "")) for b in r.json() if b.get("name")]
        if cfg.provider == "gitlab":
            import urllib.parse as _u
            pid = _u.quote(cfg.full_name, safe="")
            r = _get(
                f"https://gitlab.com/api/v4/projects/{pid}/repository/branches",
                headers={"PRIVATE-TOKEN": creds.secret},
                params={"per_page": 100}, timeout=15.0,
            )
            r.raise_for_status()
            return [str(b.get("name", "")) for b in r.json() if b.get("name")]
        if cfg.provider == "bitbucket":
            email = (creds.metadata or {}).get("atlassian_email") or ""
            r = _get(
                f"https://api.bitbucket.org/2.0/repositories/{cfg.full_name}/refs/branches",
                auth=(str(email), creds.secret),
                params={"pagelen": 100}, timeout=15.0,
            )
            r.raise_for_status()
            return [
                str(b.get("name", "")) for b in r.json().get("values", []) if b.get("name")
            ]
    except httpx.HTTPError:
        return []
    return []


# ─── helpers ─────────────────────────────────────────────────────────


def _get(url: str, *, timeout: float = 15.0, **kwargs: Any) -> httpx.Response:
    """One guarded GET against a git provider's API.

    Replaces the raw ``httpx.get`` verbs this router grew: each of those
    built an ephemeral, allowlist-blind client inside httpx per call. Same
    per-call lifecycle, same defaults — the only change is the egress
    whitelist transport, and every host this router talks to (api.github.com,
    gitlab.com, api.bitbucket.org) is on the shipped public allowlist.
    """
    with build_client(timeout=timeout) as client:
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
    query: str | None = None,
) -> list[RepoBrowseItem]:
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
        "https://gitlab.com/api/v4/projects",
        headers={"PRIVATE-TOKEN": token},
        params=params,
        timeout=15.0,
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


_GITHUB_SORT = {
    "newest": ("created", "desc"),
    "oldest": ("created", "asc"),
    "recently_updated": ("updated", "desc"),
}
_GITLAB_SORT = {
    "newest": ("created_at", "desc"),
    "oldest": ("created_at", "asc"),
    "recently_updated": ("updated_at", "desc"),
}
_BITBUCKET_SORT = {
    "newest": "-created_on",
    "oldest": "created_on",
    "recently_updated": "-updated_on",
}


def _list_pulls_github(
    full_name: str, token: str, *,
    branch: str | None = None, sort: str = "newest",
) -> list[PullRequestSummary]:
    sort_field, direction = _GITHUB_SORT.get(sort, _GITHUB_SORT["newest"])
    params: dict[str, str | int] = {
        "state": "open", "per_page": 50,
        "sort": sort_field, "direction": direction,
    }
    if branch:
        params["base"] = branch
    resp = _get(
        f"https://api.github.com/repos/{full_name}/pulls",
        headers={"Authorization": f"Bearer {token}"},
        params=params, timeout=15.0,
    )
    resp.raise_for_status()
    return [
        PullRequestSummary(
            provider="github",
            repo=full_name,
            number=int(p.get("number", 0)),
            title=str(p.get("title") or ""),
            author=str((p.get("user") or {}).get("login") or ""),
            state=str(p.get("state") or "open"),
            url=str(p.get("html_url") or ""),
            created_at=p.get("created_at"),
            updated_at=p.get("updated_at"),
        )
        for p in resp.json()
    ]


def _list_pulls_gitlab(
    full_name: str, token: str, *,
    branch: str | None = None, sort: str = "newest",
) -> list[PullRequestSummary]:
    import urllib.parse as _u
    project_id = _u.quote(full_name, safe="")
    order_by, direction = _GITLAB_SORT.get(sort, _GITLAB_SORT["newest"])
    params: dict[str, str | int] = {
        "state": "opened", "per_page": 50,
        "order_by": order_by, "sort": direction,
    }
    if branch:
        params["target_branch"] = branch
    resp = _get(
        f"https://gitlab.com/api/v4/projects/{project_id}/merge_requests",
        headers={"PRIVATE-TOKEN": token},
        params=params, timeout=15.0,
    )
    resp.raise_for_status()
    return [
        PullRequestSummary(
            provider="gitlab",
            repo=full_name,
            number=int(m.get("iid", 0)),
            title=str(m.get("title") or ""),
            author=str((m.get("author") or {}).get("username") or ""),
            state=str(m.get("state") or "opened"),
            url=str(m.get("web_url") or ""),
            created_at=m.get("created_at"),
            updated_at=m.get("updated_at"),
        )
        for m in resp.json()
    ]


def _list_pulls_bitbucket(
    full_name: str, email: str, token: str, *,
    branch: str | None = None, sort: str = "newest",
) -> list[PullRequestSummary]:
    sort_param = _BITBUCKET_SORT.get(sort, _BITBUCKET_SORT["newest"])
    params: dict[str, str | int] = {
        "state": "OPEN", "pagelen": 50, "sort": sort_param,
    }
    if branch:
        params["q"] = f'destination.branch.name="{branch}"'
    resp = _get(
        f"https://api.bitbucket.org/2.0/repositories/{full_name}/pullrequests",
        auth=(email, token),
        params=params, timeout=15.0,
    )
    resp.raise_for_status()
    out: list[PullRequestSummary] = []
    for p in resp.json().get("values", []):
        out.append(PullRequestSummary(
            provider="bitbucket",
            repo=full_name,
            number=int(p.get("id", 0)),
            title=str(p.get("title") or ""),
            author=str((p.get("author") or {}).get("nickname") or ""),
            state=str(p.get("state") or "OPEN"),
            url=str((p.get("links", {}).get("html", {}) or {}).get("href", "")),
            created_at=p.get("created_on"),
            updated_at=p.get("updated_on"),
        ))
    return out


def _full_name_to_slug(provider: str, full_name: str) -> str:
    """Convert 'owner/name' to internal slug used by parse_repo_url."""
    if not full_name:
        return ""
    return parse_repo_url(f"{provider}:{full_name}").slug
