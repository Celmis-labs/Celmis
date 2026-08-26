"""Index one repository into its graph — the one implementation.

The per-repo button on the Repositories page did this inline in the route
handler, which is fine while there is one caller. "Index all" is the second,
and it runs from the queue where there is no request, no `Depends`, and no
place to raise an HTTPException. Copying the body would have produced two
versions of the credential resolution, and the copy is the one that misses the
next fix.

Graphs only. Vault generation is a separate, expensive, explicitly-chosen step
(`POST /{slug}/generate-vault`) and indexing must never trigger it — chat and
search work off the index alone.

Every run of `index_repo_sync` — success or failure — leaves a
`repo_index_state` row behind (see src/repos/index_state.py). Until it did,
this path wrote nothing at all: six successful full indexes in production left
that table empty, so no surface could say when a repo was last indexed or at
which revision, and a repo whose index had died six times looked identical to
one nobody had asked to index.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class IndexError_(Exception):
    """Indexing refused or failed, with a message meant for a person."""


def safe_detail(text: str, *, limit: int = 400) -> str:
    """A failure message fit to leave the server.

    `raise IndexError_(f"Index failed: {exc}")` put whatever the indexer threw
    straight into an HTTP 500 body, and the router returns it as `detail`. Two
    things rode out on it:

      * THE CONTAINER'S LAYOUT. Messages arrived at the browser reading
        `/workspace/repos/github_acme-worker/.git`, which tells a stranger the
        deployment's directory structure and the naming scheme of everything
        under it. The path INSIDE the repository is what a person needs
        ("src/settlement.py"); the absolute prefix is ours.

      * ANYTHING A LIBRARY PUT IN AN EXCEPTION. `CloneError` strips
        credentials, but it is one exception type out of everything the
        indexer can raise, and a `subprocess` error carries the argv it was
        built from — which for a clone contains a token. The log filter
        catches that on the way to a log; this is the same hole pointing at
        the browser.

    Both are stripped here rather than in the router so every consumer of the
    exception gets it — the message is documented as showable verbatim, and
    that is only true if it is true at the raise site.
    """
    from src.security.log_filter import redact_text

    out = redact_text(text or "")
    try:
        from src.config import get_settings
        s = get_settings()
        # Longest first: `workspace_dir` is a prefix of the others, and
        # replacing it first would leave `<workspace>/repos/...` rather than
        # the more informative `<repos>/...`.
        roots = sorted(
            ((str(s.repos_dir), "<repos>"), (str(s.data_dir), "<data>"),
             (str(s.vault_dir), "<vault>"), (str(s.logs_dir), "<logs>"),
             (str(s.workspace_dir), "<workspace>")),
            key=lambda kv: -len(kv[0]),
        )
        for literal, label in roots:
            if literal:
                out = out.replace(literal, label)
    except Exception:  # noqa: BLE001 — sanitising must not raise in a raise path
        pass
    return out[:limit]


# ─── Queueing an index ───────────────────────────────────────────────

#: What a caller did about a repository's graph, reported verbatim so the
#: answer can be shown rather than guessed. The distinction these constants
#: draw is the whole point: 161 Martian-bench review runs went out against
#: "(no graph context)" because 50 forks were registered through
#: POST /api/repos and nothing ever cloned them, and no response, badge or log
#: line said so.
INDEX_QUEUED = "queued"
#: `enqueue` returned None — a pending/running full index for this repo in this
#: workspace already holds the dedup key.
INDEX_ALREADY_QUEUED = "already_queued"
#: A graph file already exists for the slug, so there is nothing to clone.
INDEX_ALREADY_INDEXED = "already_indexed"
#: The caller asked for no index (bulk registration). Registering the 50 bench
#: forks with a clone each cost 57.9 GB, so "register without cloning" has to
#: stay expressible.
INDEX_NOT_REQUESTED = "not_requested"
#: The queue insert raised. The repository is still registered — losing the
#: registration because Postgres hiccuped would be a worse bug than not
#: indexing — but nothing is running, and this says so instead of implying it.
INDEX_QUEUE_UNAVAILABLE = "queue_unavailable"


def index_dedup_key(repo_slug: str, workspace_id: str) -> str:
    """The one dedup key for a full index of this repo in this workspace.

    Shared by every path that can start one (registration, "Index all"). Two
    paths with two keys would let a registration and a button press clone the
    same repository twice in parallel into the same directory.
    """
    return f"index_full:{workspace_id}:{repo_slug}"


def queue_index_if_needed(
    repo_slug: str,
    *,
    workspace_id: str,
    user_id: str,
    enqueued_by: str | None = None,
) -> str:
    """Queue a full graph index for `repo_slug` unless one is unnecessary.

    Returns one of the INDEX_* constants above. Never raises: callers run
    inside a request that has already written the repository row, and a queue
    problem must not turn into a lost registration.
    """
    from src.config import get_settings
    from src.sync.queue import KIND_INDEX_REPO_FULL, enqueue

    try:
        if get_settings().repo_graph_path(repo_slug).exists():
            return INDEX_ALREADY_INDEXED
        job_id = enqueue(
            kind=KIND_INDEX_REPO_FULL,
            payload={
                "repo_slug": repo_slug,
                "workspace_id": workspace_id,
                "user_id": user_id,
            },
            dedup_key=index_dedup_key(repo_slug, workspace_id),
            enqueued_by=enqueued_by,
        )
    except Exception as exc:  # noqa: BLE001 — any queue failure, same answer
        logger.warning("index_not_queued repo=%s ws=%s err=%s: %s",
                       repo_slug, workspace_id, type(exc).__name__, exc)
        return INDEX_QUEUE_UNAVAILABLE
    return INDEX_QUEUED if job_id is not None else INDEX_ALREADY_QUEUED


@dataclass(frozen=True)
class IndexResult:
    repo_slug: str
    symbols: int


def index_repo_sync(
    repo_slug: str,
    *,
    user_id: str,
    workspace_id: str,
) -> IndexResult:
    """Clone + tree-sitter index. Blocking (~5-60s for a small repo).

    Raises IndexError_ with a message the caller can show or log verbatim.
    """
    from src.api.auto_review import get_auto_review_store
    from src.config import get_settings
    from src.credentials import resolve_git_credential
    from src.credentials.git_auth import git_auth_kwargs
    from src.groups.indexer import GroupIndexer
    from src.groups.models import RepoGroup
    from src.repos.index_state import record_index_failure, record_index_success

    store = get_auto_review_store()
    cfg = store.get_in_workspace(workspace_id, repo_slug) or store.get(user_id, repo_slug)
    if cfg is None:
        # Deliberately outside the recording block below: a slug this workspace
        # has not registered owns no index state of ours, and writing one would
        # leave a row that no purge is ever asked to remove.
        raise IndexError_("Repo not registered")

    try:
        settings = get_settings()
        # Resolve the git credential the SAME way every other surface does
        # (workspace slot first). Without this the clone falls back to RepoSync's
        # per-user lookup, misses the ws:{id} token and goes out anonymous — which
        # is exactly how a private repo turns into "index failed, no credentials".
        creds = resolve_git_credential(cfg.provider, user_id=user_id, workspace_id=workspace_id)
        if creds is None:
            # Wrapped like the rest, though this one interpolates only a
            # provider name. The rule is "any interpolated message goes
            # through it" precisely so nobody has to work out which
            # interpolations are safe — that judgement is where the next leak
            # comes from.
            raise IndexError_(safe_detail(
                f"No {cfg.provider} token for this workspace — connect one on the "
                f"Connections page, then index again."
            ))
        kw = git_auth_kwargs(cfg.provider, creds.secret, creds.metadata)

        indexer = GroupIndexer(
            group=RepoGroup(name=f"_solo_{cfg.repo_slug[:50]}", repos=[cfg.url]),
            settings=settings,
            user_id=user_id,
            api_token=kw.get("api_token") or kw.get("password"),
            git_username=kw.get("username"),
            # Configured branch (None → provider default). Without this the clone
            # always lands on the default branch, so a repo whose work happens on
            # `dev` would be indexed from the wrong ref.
            branch=cfg.branch or None,
        )
        try:
            result = indexer.index()
        except Exception as exc:  # noqa: BLE001
            raise IndexError_(safe_detail(f"Index failed: {exc}")) from exc

        # GroupIndexer.index() COLLECTS per-repo errors instead of raising, so a
        # "successful" call can still have indexed nothing. Reporting success there
        # is what made the UI show "Indexed ✓" while the badge stayed on "index
        # now" forever — fail loudly instead.
        if not result.repos_indexed:
            detail = "; ".join(result.failures) or "indexer returned no repositories"
            raise IndexError_(safe_detail(f"Index failed: {detail}"))

        # `indexed` in GET /api/repos is "graph file exists for THIS slug" — assert
        # the same condition here so the response can never disagree with the list.
        if not settings.repo_graph_path(cfg.repo_slug).exists():
            wrote = ", ".join(sorted(r.slug for r in result.repos_indexed)) or "nothing"
            suffix = f" — {'; '.join(result.failures)}" if result.failures else ""
            logger.error("index_slug_mismatch expected=%s wrote=%s failures=%s",
                         cfg.repo_slug, wrote, result.failures)
            raise IndexError_(safe_detail(
                f"Index finished but no graph was written for {cfg.repo_slug!r} "
                f"(indexer wrote: {wrote}){suffix}"
            ))

        # The revision that was indexed comes from the clone RepoSync already
        # resolved (`SyncResult.commit_sha`) rather than a fresh `git rev-parse`:
        # a second call reads the working copy at answer time, which is not
        # necessarily the tree the extractor walked.
        per_repo = next(
            (r for r in result.repos_indexed if r.slug == cfg.repo_slug), None
        )
        if per_repo is None:
            # Reachable only when the indexer wrote a slug other than the one we
            # asked for and a graph file from an earlier run kept the check above
            # happy. Record the run without a revision rather than claim
            # somebody else's.
            logger.warning("index_state_no_matching_repo slug=%s", cfg.repo_slug)
        record_index_success(
            cfg.repo_slug,
            sha=per_repo.sync.commit_sha if per_repo else None,
            files=per_repo.files_processed if per_repo else 0,
            # A rebuild, but only of a slug this run actually indexed.
            # GroupIndexer drops the existing graph file before writing, so
            # `per_repo is not None` really is a from-scratch rebuild. On the
            # branch above it is not: the indexer wrote somebody else's slug
            # and the graph on disk is the one an EARLIER run left, so
            # stamping `last_full_rebuild_at` would date a rebuild that never
            # happened and make the row more optimistic than the run.
            full_rebuild=per_repo is not None,
        )

        try:
            from src.groups.indexer import enqueue_materialize_for_repo
            enqueue_materialize_for_repo(
                cfg.repo_slug, enqueued_by=f"index_full:{cfg.repo_slug}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("materialize_enqueue_failed repo=%s err=%s",
                           cfg.repo_slug, exc)

        return IndexResult(repo_slug=cfg.repo_slug, symbols=result.total_symbols)
    except Exception as exc:
        # A repo whose index keeps dying has to say so. Without this the only
        # trace of cal.diy's six consecutive failures was a dead queue row, and
        # the repositories page showed it exactly like a repo nobody had asked
        # to index. `record_index_failure` swallows its own errors, so it can
        # neither replace nor hide the exception on its way out.
        record_index_failure(cfg.repo_slug, str(exc))
        raise


__all__ = [
    "INDEX_ALREADY_INDEXED",
    "INDEX_ALREADY_QUEUED",
    "INDEX_NOT_REQUESTED",
    "INDEX_QUEUED",
    "INDEX_QUEUE_UNAVAILABLE",
    "IndexError_",
    "safe_detail",
    "IndexResult",
    "index_dedup_key",
    "index_repo_sync",
    "queue_index_if_needed",
]
