"""Incremental repo indexer.

Contract:

    run_index(repo_slug, force_full=False, since_sha=None)

Flow:

    1. Load `RepoIndexState` for `repo_slug`. If missing OR force_full,
       trigger full rebuild (existing GroupIndexer path). Persist new
       state (last_indexed_sha = HEAD).
    2. Otherwise `git diff --name-status <last_sha>..HEAD`. If empty →
       fast-return (nothing changed). If HEAD == last_sha → same.
    3. Walk the diff:
        - 'D' / 'M' → graph.delete_by_file(path)
        - 'A' / 'M' → extractor.extract(path) + graph.add_symbols/edges
        - 'R' (rename) → treat as D + A
        - vault MD refresh: `delete_by_note_path` for affected notes.
          Full regeneration of an affected feature/module is deferred to
          Phase 4 (reverse-index) — current fallback just drops stale
          Qdrant points so QA doesn't return removed content.
    4. Enqueue `cross_repo_materialize` for every group the repo belongs
       to — cheap re-materialize keeps cross-repo edges in sync.
    5. Persist `RepoIndexState.last_indexed_sha = new_sha` last (atomic
       success signal — half-done updates leave state on the old sha).
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
from pathlib import Path

from src.repos.index_state import (
    read_index_state,
    record_index_failure,
    record_index_success,
    record_index_unchanged,
)

logger = logging.getLogger(__name__)


def run_index(
    repo_slug: str,
    *,
    force_full: bool = False,
    since_sha: str | None = None,
) -> dict:
    """Blocking. Returns diagnostic dict.

    Callers: sync worker via handle_index_repo, or CLI/HTTP for manual
    testing.
    """
    from src.config import get_settings
    settings = get_settings()
    repo_path = settings.repo_path(repo_slug)
    if not repo_path.exists() or not (repo_path / ".git").exists():
        return {"status": "skipped", "reason": "clone missing", "repo": repo_slug}

    # Bring the CHECKOUT to the remote, not just the remote-tracking ref.
    #
    # This was `git fetch --all` alone, under a comment saying it pulled. It
    # does not: fetch advances `origin/<branch>` and leaves HEAD, the index
    # and the working tree where they were. So `_git_head` returned the OLD
    # commit, `prior_sha == head_sha` held, and the pass recorded "unchanged"
    # for a repository whose remote had moved — with the pushed files not even
    # on disk to parse.
    #
    # It survived because nothing called this function: every enqueuer used
    # `index_repo_full`. The freshness check made the path reachable, and a
    # re-index that indexes nothing is worse than none — the row stamps
    # `last_indexed_at = now` while `last_indexed_sha` stays old, so one
    # column reads "indexed just now" and the next reads "behind", for ever.
    if not _advance_to_remote(repo_path):
        logger.warning("advance_failed repo=%s — indexing the checkout as it is",
                       repo_slug)
    head_sha = _git_head(repo_path)
    if not head_sha:
        return {"status": "skipped", "reason": "cannot resolve HEAD"}

    state = read_index_state(repo_slug)
    prior_sha = since_sha or (state.last_indexed_sha if state else None)

    if not force_full and prior_sha and prior_sha == head_sha:
        record_index_unchanged(repo_slug, sha=head_sha)
        return {"status": "noop", "repo": repo_slug, "sha": head_sha}

    try:
        if force_full or not prior_sha:
            result = _run_full(repo_slug, repo_path)
        else:
            result = _run_incremental(repo_slug, repo_path, prior_sha, head_sha)
    except Exception as exc:
        # Same row, same meaning as the full path in src/repos/indexing.py: the
        # attempt is recorded and `last_indexed_sha` keeps pointing at the last
        # revision that actually got into the graph. A pass that dies here used
        # to leave nothing but a dead queue row.
        record_index_failure(repo_slug, str(exc))
        raise

    # Atomic success signal — state written LAST.
    record_index_success(
        repo_slug,
        sha=head_sha,
        files=int(result.get("files_touched") or 0),
        full_rebuild=result.get("mode") == "full",
    )

    # Cross-repo edges tend to touch multiple repos — enqueue a
    # rematerialize for every group this repo belongs to. Enqueue-with-
    # dedup means back-to-back changes coalesce into one materialize.
    try:
        from src.groups.indexer import enqueue_materialize_for_repo
        enqueue_materialize_for_repo(repo_slug, enqueued_by=f"index:{repo_slug}")
    except Exception as exc:  # noqa: BLE001
        # WARNING, not debug. This block swallowed a real regression for a
        # week: groups_containing() raised for every tenant-scoped group and
        # the only trace was a debug line nobody reads. Indexing still
        # succeeded, so the failure had to be loud somewhere or it was
        # invisible everywhere.
        logger.warning("materialize_enqueue_failed repo=%s err=%s", repo_slug, exc)

    return {"status": "ok", "repo": repo_slug, "sha": head_sha, **result}


# ─── Full rebuild path (delegate to existing GroupIndexer) ──────────


def _run_full(repo_slug: str, repo_path: Path) -> dict:
    logger.info("incremental_full_rebuild repo=%s", repo_slug)
    try:
        from src.indexing.graph.pipeline import index_repo_graph
        # src_subdir=None → index the whole repo (with ignore patterns);
        # we don't assume the acme "src" layout here.
        res = index_repo_graph(
            repo_path=repo_path, repo_slug=repo_slug, src_subdir=None,
        )
        return {
            "mode": "full",
            "files_touched": res.files_processed,
            "symbols_added": res.symbols,
            "edges_added": res.edges,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("full_rebuild_failed repo=%s err=%s", repo_slug, exc)
        raise


# ─── Incremental path ─────────────────────────────────────────────


def _run_incremental(
    repo_slug: str, repo_path: Path, prior_sha: str, head_sha: str,
) -> dict:
    changed = _git_diff(repo_path, prior_sha, head_sha)
    if changed is None:
        # Diff itself failed — do NOT advance state (caller checks this).
        raise RuntimeError(f"git diff failed for {repo_slug} {prior_sha[:8]}..{head_sha[:8]}")
    if not changed:
        logger.info("incremental_empty_diff repo=%s %s..%s",
                    repo_slug, prior_sha[:8], head_sha[:8])
        return {"mode": "incremental", "files_touched": 0}

    # Renames arrive as ('R', old_path, new_path) — treat as delete(old)
    # + add(new) so the graph drops the old symbols and re-extracts the
    # new file.
    added_or_modified: list[str] = []
    deleted: list[str] = []
    for entry in changed:
        st = entry[0]
        if st == "D":
            deleted.append(entry[1])
        elif st.startswith("R") and len(entry) == 3:
            deleted.append(entry[1])
            added_or_modified.append(entry[2])
        elif st in ("A", "M") or st.startswith("R") or st.startswith("C"):
            added_or_modified.append(entry[-1])

    logger.info(
        "incremental_diff repo=%s prior=%s head=%s add/mod=%d del=%d",
        repo_slug, prior_sha[:8], head_sha[:8],
        len(added_or_modified), len(deleted),
    )

    # ── Graph — per-file re-extraction via the language registry.
    from src.config import get_settings
    from src.indexing.graph.configs import build_repo_context
    from src.indexing.graph.graph_store import make_graph_store
    from src.indexing.graph.languages.factory import build_default_registry
    from src.indexing.graph.resolver import HeuristicResolver

    settings = get_settings()
    graph_path = settings.repo_graph_path(repo_slug)
    ctx = build_repo_context(repo_path)
    enabled = (
        set(settings.enabled_language_adapters)
        if settings.enabled_language_adapters else None
    )
    registry = build_default_registry(ctx=ctx, repo_root=repo_path, enabled=enabled)

    store = make_graph_store(str(graph_path))
    total_syms = total_edges = 0
    try:
        # Delete stale symbols for every touched file (modified files get
        # their old symbols dropped before re-extraction; deleted files
        # just get dropped).
        store.delete_by_files(deleted + added_or_modified)

        for path in added_or_modified:
            fpath = repo_path / path
            if not fpath.exists() or not fpath.is_file():
                continue
            extractor = registry.match(fpath)
            if extractor is None:
                continue
            try:
                res = extractor.extract(fpath)
            except Exception as exc:  # noqa: BLE001
                logger.warning("extract_failed path=%s err=%s", path, exc)
                continue
            # Resolve edges against this file's own symbols (best-effort;
            # cross-file edges refresh on the next full rebuild).
            resolver = HeuristicResolver(ctx, res.symbols)
            resolved = resolver.resolve_edges(res.edges)
            total_syms += store.add_symbols_batch(res.symbols)
            total_edges += store.add_edges_batch(resolved)
        store.commit()
    finally:
        store.close()

    # ── Vault MD reverse-index — resolve which notes reference the
    # changed source files, drop their stale Qdrant points, and enqueue
    # regeneration. Rebuild the reverse-index cache so the next diff run
    # sees the current mapping.
    try:
        from src.vault.reverse_index import affected_notes, invalidate_cache
        invalidate_cache(repo_slug)  # cache is stale after any code change
        notes_to_refresh = affected_notes(
            repo_slug, added_or_modified + deleted,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("reverse_index_failed err=%s", exc)
        notes_to_refresh = []

    try:
        from src.api.auto_review import workspace_for_repo_slug
        from src.retrieval.tier1_vault import VaultRetriever
        rt = VaultRetriever(
            settings, workspace_id=workspace_for_repo_slug(repo_slug),
        )
        for note_path in notes_to_refresh:
            # One unreachable note must not stop the rest of the delta.
            with contextlib.suppress(Exception):
                rt.delete_by_note_path(note_path, repo=repo_slug)
    except Exception as exc:  # noqa: BLE001
        logger.debug("qdrant_delta_failed err=%s", exc)

    # Enqueue TARGETED vault regeneration (Stage 21) — only the affected
    # notes get their LLM content rebuilt, instead of re-embedding the
    # entire repo. Zero-cost if the notes list is empty.
    if notes_to_refresh:
        try:
            from src.sync.queue import KIND_REGENERATE_NOTES, enqueue
            # NO dedup_key: each incremental run deletes a specific set of
            # Qdrant points *before* enqueuing their regeneration. Deduping
            # on repo would drop a later batch whose points were already
            # deleted, leaving those notes permanently missing from the
            # index. Distinct jobs keep the delete↔regen pairing intact.
            enqueue(
                kind=KIND_REGENERATE_NOTES,
                payload={"repo_slug": repo_slug, "note_paths": notes_to_refresh},
                enqueued_by=f"incremental:{repo_slug}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("regen_enqueue_failed err=%s", exc)

    return {
        "mode": "incremental",
        "files_touched": len(added_or_modified) + len(deleted),
        "symbols_added": total_syms,
        "edges_added": total_edges,
    }


# ─── git helpers ─────────────────────────────────────────────────


def _advance_to_remote(repo_path: Path) -> bool:
    """Fetch, then move the checkout onto the branch's remote head.

    Returns False when it could not — a detached HEAD, a directory that is not
    a repository, a branch with no remote counterpart. False is reported
    rather than swallowed: the caller must not treat "I could not update" as
    "there was nothing to update", which is the exact confusion this function
    was written to end.

    `reset --hard`, not a merge, for the reason RepoSync does the same on the
    full path: nobody's work lives in the indexer's clone, and a merge
    conflict on a machine with no human at it is a repository that quietly
    stops updating.
    """
    try:
        if not (repo_path / ".git").exists():
            return False
        subprocess.run(["git", "-C", str(repo_path), "fetch", "--all", "--quiet"],
                       check=False, timeout=120)
        branch = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        if not branch or branch == "HEAD":
            # Detached: somebody pinned this deliberately, and forcing it onto
            # a branch head would silently discard that decision.
            return False
        remote_ref = f"origin/{branch}"
        exists = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--verify", "--quiet", remote_ref],
            capture_output=True, text=True, timeout=10,
        )
        if exists.returncode != 0:
            return False
        subprocess.run(["git", "-C", str(repo_path), "reset", "--hard", remote_ref],
                       capture_output=True, text=True, timeout=60, check=True)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("advance_to_remote_failed path=%s err=%s", repo_path, exc)
        return False


def _git_head(repo_path: Path) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return r.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _git_diff(
    repo_path: Path, prior_sha: str, head_sha: str,
) -> list[tuple] | None:
    """Returns a list of tuples, or None if the diff command FAILED.

    Distinguishing "no changes" ([]) from "diff failed" (None) matters:
    on failure the caller must NOT advance last_indexed_sha, else the
    real changes are silently skipped forever.

    Tuple shapes:
        ("A"|"M"|"D", path)
        ("R###", old_path, new_path)   — rename (via -M)
        ("C###", old_path, new_path)   — copy
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "diff", "--name-status", "-M",
             f"{prior_sha}..{head_sha}"],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("git_diff_failed repo=%s err=%s", repo_path, exc)
        return None
    out: list[tuple] = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if (status.startswith("R") or status.startswith("C")) and len(parts) >= 3:
            out.append((status, parts[1], parts[2]))  # (status, old, new)
        else:
            out.append((status, parts[-1]))
    return out


# ─── State persistence ───────────────────────────────────────────
#
# This module used to be the ONLY reader and writer of `repo_index_state`, and
# it owned its own session + column semantics. The full-index path
# (src/repos/indexing.py) wrote nothing, which is why the table was empty in
# production after six successful full indexes. Both now go through
# src/repos/index_state.py so a row means the same thing whichever pass wrote
# it: `last_indexed_sha` is the clone HEAD that reached the graph, and
# `last_error` is the attempt after it, if that one died.


__all__ = ["run_index"]
