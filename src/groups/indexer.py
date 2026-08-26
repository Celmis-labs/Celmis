"""GroupIndexer — orchestrator that joins Phases 1-6 into a live workflow.

Pipeline:
    1. **Clone all repos** in the group through RepoSync (Phase 1, multi-host)
    2. **Per-repo extraction** through LanguageRegistry (Phase 2.B + Phase 4)
    3. **Per-repo persistence** in `~/code-analysis/data/{slug}/graph.fdblite`
    4. **Cross-repo materialization** through CrossRepoMaterializer (Phase 6)
    5. **Cross-repo persistence** in `~/code-analysis/groups/{group_name}.fdblite`

Storage layout (hybrid per-repo + cross-repo, as discussed architecturally):
    ~/code-analysis/
        repos/{slug}/                — clones
        data/{slug}/graph.fdblite    — per-repo graph (existing pipeline.py)
        groups/{name}.yaml           — group definitions
        groups/{name}.fdblite        — cross-repo edge index (new)

Stage 7 (post-Phase 6, May 2026) — implemented.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from src.config import Settings, get_settings
from src.groups.cross_repo import CrossRepoEdge, CrossRepoMaterializer
from src.groups.models import RepoGroup
from src.indexing.graph.configs import build_repo_context
from src.indexing.graph.extractor import EdgeInfo, ExtractionResult, SymbolInfo
from src.indexing.graph.graph_store import make_graph_store
from src.indexing.graph.languages.factory import build_default_registry, walk_repo_files
from src.indexing.graph.resolver import HeuristicResolver
from src.sync.clone import CloneError, RepoSync, SyncResult

logger = logging.getLogger(__name__)


@dataclass
class _RepoIndexResult:
    """Per-repo subset of GroupIndexResult — for diagnostics."""

    slug: str
    sync: SyncResult
    files_processed: int
    files_skipped: int
    parse_failures: int
    symbols: int
    edges_resolved: int
    elapsed_seconds: float


@dataclass
class GroupIndexResult:
    """Result of the full group index pipeline."""

    group_name: str
    repos_indexed: list[_RepoIndexResult] = field(default_factory=list)
    cross_repo_edges: int = 0
    failures: list[str] = field(default_factory=list)  # human-readable error messages
    elapsed_seconds: float = 0.0

    @property
    def total_symbols(self) -> int:
        return sum(r.symbols for r in self.repos_indexed)

    @property
    def total_edges(self) -> int:
        return sum(r.edges_resolved for r in self.repos_indexed) + self.cross_repo_edges


class GroupIndexer:
    """Coordinates clone + index + materialize for all repos in a RepoGroup.

    Idempotent — re-running on the same group rebuilds the graphs from scratch
    (drops the existing fdblite files before re-writing). This is consistent
    with the existing pipeline.py behavior.
    """

    def __init__(
        self,
        group: RepoGroup,
        settings: Settings | None = None,
        *,
        user_id: str = "default",
        api_token: str | None = None,
        git_username: str | None = None,
        branch: str | None = None,
    ) -> None:
        self.group = group
        self.settings = settings or get_settings()
        self.sync = RepoSync(self.settings)
        self.user_id = user_id
        # Explicit branch for every repo in the group. None keeps the historic
        # behaviour (clone the provider's default branch).
        self.branch = branch
        # Explicit credentials win over RepoSync's own lookup: that one only
        # knows per-user/legacy slots, so a workspace-scoped token (ws:{id})
        # would be invisible to it and the clone would silently go anonymous.
        self.api_token = api_token
        self.git_username = git_username

    def index(
        self,
        progress_callback: Callable[[str], None] | None = None,
        *,
        enable_scip: bool = False,
        scip_languages: tuple[str, ...] = ("python", "typescript", "go"),
    ) -> GroupIndexResult:
        """Run the full pipeline. Does not raise — collects failures.

        Args:
            progress_callback: (msg) → None for UI updates
            enable_scip: if True, run the SCIP indexer per repo + enrich the
                        extractions before persisting. Auto-detects available
                        SCIP binaries — gracefully skips if the binary is
                        missing for a language.
            scip_languages: list of languages to try SCIP enrichment for.
                            Default — Python/TS/Go (the most common backends).
        """
        t0 = time.time()
        result = GroupIndexResult(group_name=self.group.name)
        materializer = CrossRepoMaterializer(self.group)

        # Lazy-load the SCIP runner if enabled
        scip_runner = None
        scip_enricher = None
        if enable_scip:
            from src.indexing.scip import ScipEnricher, ScipExternalRunner
            scip_runner = ScipExternalRunner()
            scip_enricher = ScipEnricher()

        for repo_id in self.group.repos:
            try:
                per_repo, extractions = self._index_one_repo(
                    repo_id, progress_callback,
                    scip_runner=scip_runner,
                    scip_enricher=scip_enricher,
                    scip_languages=scip_languages,
                )
            except Exception as exc:  # noqa: BLE001
                msg = f"repo={repo_id} error={type(exc).__name__}: {exc}"
                # exc_info, not just the message: the message says WHAT file
                # was missing, the traceback says WHO asked for it. A broken
                # symlink in cal.com read as "Index failed" for six attempts
                # while the faulting frame was in clone.py's chmod walk — the
                # one place nobody looked, because the text named the indexer.
                logger.error("group_repo_failed %s", msg, exc_info=exc)
                result.failures.append(msg)
                continue

            result.repos_indexed.append(per_repo)
            materializer.add_repo(per_repo.slug, extractions)

        # Cross-repo materialization
        if progress_callback:
            progress_callback(f"materializing cross-repo edges across {len(result.repos_indexed)} repos")
        cross_edges = materializer.materialize()
        result.cross_repo_edges = self._persist_cross_repo_edges(cross_edges)

        result.elapsed_seconds = time.time() - t0
        logger.info(
            "group_index_complete group=%s repos=%d failures=%d "
            "symbols=%d edges=%d cross_repo=%d %.1fs",
            self.group.name, len(result.repos_indexed), len(result.failures),
            result.total_symbols, result.total_edges, result.cross_repo_edges,
            result.elapsed_seconds,
        )
        return result

    # ─── per-repo ───────────────────────────────────────────────

    def _index_one_repo(
        self,
        repo_id: str,
        progress_callback: Callable[[str], None] | None,
        *,
        scip_runner=None,
        scip_enricher=None,
        scip_languages: tuple[str, ...] = (),
    ) -> tuple[_RepoIndexResult, dict[str, ExtractionResult]]:
        """Clone + extract + (optional SCIP enrich) + persist one repo.

        Returns (result, extractions). If scip_runner+scip_enricher are passed,
        we try SCIP enrichment for each language from the scip_languages list —
        gracefully skipping when the binary is missing.
        """
        t0 = time.time()
        if progress_callback:
            progress_callback(f"sync: {repo_id}")

        try:
            sync_result = self.sync.clone_or_update(
                repo_id,
                # None → default branch (universal for multi-host);
                # an explicit branch comes from the repo configuration.
                branch=self.branch,
                user_id=self.user_id,
                api_token=self.api_token,
                username=self.git_username,
                password=self.api_token if self.git_username else None,
                progress_callback=progress_callback,
            )
        except CloneError:
            raise

        slug = sync_result.repo_slug

        if progress_callback:
            progress_callback(f"index: {slug}")

        # Build registry — apply settings filter
        enabled = (
            set(self.settings.enabled_language_adapters)
            if self.settings.enabled_language_adapters else None
        )
        ctx = build_repo_context(sync_result.path)
        registry = build_default_registry(
            ctx=ctx, repo_root=sync_result.path, enabled=enabled,
        )

        # Walk files
        all_files = walk_repo_files(sync_result.path)
        extractions: dict[str, ExtractionResult] = {}
        all_symbols: list[SymbolInfo] = []
        all_edges: list[EdgeInfo] = []

        files_skipped = 0
        parse_failures = 0

        for f in all_files:
            extractor = registry.match(f)
            if extractor is None:
                files_skipped += 1
                continue

            try:
                res = extractor.extract(f)
            except Exception as exc:  # noqa: BLE001
                parse_failures += 1
                logger.warning("extract_failed file=%s err=%s", f, exc)
                continue

            if res.parse_errors:
                parse_failures += 1

            # Relative path from the repo root — for the cross-repo materializer key
            try:
                rel = str(f.relative_to(sync_result.path))
            except ValueError:
                rel = str(f)
            extractions[rel] = res

            all_symbols.extend(res.symbols)
            all_edges.extend(res.edges)

        # ─── Optional SCIP enrichment (Stage 15.2) ───────────────
        # Per-repo type-aware enrichment before heuristic resolution. SCIP
        # resolves cross-file CALLS edges precisely; the resolver is heuristic.
        # Order: SCIP first (high-confidence), then heuristic as a fallback.
        scip_stats_total = 0
        if scip_runner is not None and scip_enricher is not None:
            for lang in scip_languages:
                if not scip_runner.is_indexer_available(lang):
                    continue
                if progress_callback:
                    progress_callback(f"scip-{lang}: {slug}")
                try:
                    scip_result = scip_runner.try_run(
                        lang, sync_result.path,
                        output_dir=self.settings.repo_data_path(slug),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "scip_run_skipped slug=%s lang=%s err=%s",
                        slug, lang, exc,
                    )
                    continue
                if scip_result is None or scip_result.index is None:
                    continue
                stats = scip_enricher.enrich(extractions, scip_result.index)
                scip_stats_total += stats.references_resolved
                logger.info(
                    "scip_enriched slug=%s lang=%s defs=%d resolved=%d",
                    slug, lang, stats.definitions_matched, stats.references_resolved,
                )

        # Resolve edges (heuristic — converts unresolved → resolved)
        resolver = HeuristicResolver(ctx, all_symbols)
        resolved = resolver.resolve_edges(all_edges)

        # Persist into the per-repo graph
        db_path = self.settings.repo_graph_path(slug)
        if db_path.exists():
            db_path.unlink()
        db_path.parent.mkdir(parents=True, exist_ok=True)

        store = make_graph_store(db_path)
        try:
            store.add_symbols_batch(all_symbols)
            n_edges = store.add_edges_batch(resolved)
            store.commit()
        finally:
            store.close()

        files_processed = len(all_files) - files_skipped

        per_repo = _RepoIndexResult(
            slug=slug,
            sync=sync_result,
            files_processed=files_processed,
            files_skipped=files_skipped,
            parse_failures=parse_failures,
            symbols=len(all_symbols),
            edges_resolved=n_edges,
            elapsed_seconds=time.time() - t0,
        )
        return per_repo, extractions

    # ─── cross-repo persistence ─────────────────────────────────

    def _persist_cross_repo_edges(
        self, edges: list[CrossRepoEdge],
    ) -> int:
        """Writes the cross-repo edges into the group's own FalkorDBLite file.

        Each edge → 2 nodes (from / to) + 1 edge. The nodes are synthetic and
        have an id formatted as `{repo_slug}::{local_id}` for unique identity
        across repos.

        Returns the number of materialized edges.
        """
        if not edges:
            return 0

        db_path = self._cross_repo_graph_path()
        if db_path.exists():
            db_path.unlink()
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # Build synthetic SymbolInfo for the cross-repo nodes
        # (repo prefix to avoid a collision)
        seen_node_ids: set[str] = set()
        synth_symbols: list[SymbolInfo] = []
        synth_edges: list[EdgeInfo] = []

        for e in edges:
            from_full = f"{e.from_repo}::{e.from_id}"
            to_full = f"{e.to_repo}::{e.to_id}"

            for full_id, repo, local_id in (
                (from_full, e.from_repo, e.from_id),
                (to_full, e.to_repo, e.to_id),
            ):
                if full_id in seen_node_ids:
                    continue
                seen_node_ids.add(full_id)
                # Synthetic node — kind 'cross_repo_node' (not in the core
                # enum, vendor.cross_repo.node for consistency).
                synth_symbols.append(SymbolInfo(
                    id=full_id,
                    name=local_id,
                    kind="vendor.cross_repo.node",
                    file=local_id.split("::", 1)[0] if "::" in local_id else "",
                    start_line=1,
                    end_line=None,
                    language="cross_repo",
                    module=repo,
                ))

            synth_edges.append(EdgeInfo(
                from_id=from_full,
                to_id=to_full,
                kind=e.kind,  # whitelisted in graph_store: REFERENCES_REPO, BUILD_CONTEXT
                confidence=e.confidence,
                raw_target=None,
            ))

        store = make_graph_store(db_path)
        try:
            store.add_symbols_batch(synth_symbols)
            n_persisted = store.add_edges_batch(synth_edges)
            store.commit()
        finally:
            store.close()

        logger.info(
            "cross_repo_persisted group=%s nodes=%d edges=%d path=%s",
            self.group.name, len(synth_symbols), n_persisted, db_path,
        )
        return n_persisted

    def _cross_repo_graph_path(self) -> Path:
        """Path to the cross-repo graph for this group.

        Beside the group's own YAML, so the graph moves with the definition.
        Built from the bare name, it put two tenants' edges in one file.
        """
        from src.groups.manager import GroupManager

        return GroupManager(settings=self.settings).graph_path(self.group)


# Module-level convenience
def index_group(
    group: RepoGroup,
    settings: Settings | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> GroupIndexResult:
    return GroupIndexer(group, settings).index(progress_callback)


# ─── Lightweight rematerialize (Stage 21) ───────────────────────────
#
# The cross-repo materializer only consumes docker-compose / Dockerfile
# extractions (see CrossRepoMaterializer._resolve_image_references and
# _resolve_build_context). So a rematerialize does NOT need a full repo
# re-index — walking just the docker-ish files per repo is enough.
#
# A content digest of those files is cached next to the group graph;
# when it matches, the whole materialize step is skipped. This is what
# makes per-commit incremental indexing cheap: repos change daily, their
# compose topology changes maybe monthly.

# Recursive globs so nested compose/Dockerfiles (services/*/Dockerfile,
# deploy/compose.yaml, …) are picked up — the full indexer walks every
# file, so the lightweight path must not miss them or it produces fewer
# edges than a full index.
_DOCKER_GLOBS = (
    "**/docker-compose*.yml", "**/docker-compose*.yaml",
    "**/compose*.yml", "**/compose*.yaml",
    "**/Dockerfile", "**/Dockerfile.*", "**/*.Dockerfile",
)
_DOCKER_SKIP_DIRS = {"node_modules", "vendor", ".git", "dist", "build", ".venv"}
_DOCKER_MAX_FILES = 200


def _docker_files(repo_path: Path) -> list[Path]:
    seen: set[Path] = set()
    for pattern in _DOCKER_GLOBS:
        for f in repo_path.glob(pattern):
            if not f.is_file():
                continue
            if any(part in _DOCKER_SKIP_DIRS for part in f.relative_to(repo_path).parts):
                continue
            seen.add(f)
            if len(seen) >= _DOCKER_MAX_FILES:
                return sorted(seen)
    return sorted(seen)


def enqueue_materialize_for_repo(
    repo_slug: str, *, enqueued_by: str
) -> list[str]:
    """Schedule a cross-repo rematerialize for every group holding this repo.

    BOTH index paths must call this. Cross-repo edges used to be built only by
    the incremental (webhook) path in src/sync/incremental.py — the full path
    (the per-repo Index button, `index-all`, and the KIND_INDEX_REPO_FULL job)
    ran the whole pipeline and scheduled nothing. An installation that added
    its repositories through the UI and never received a push therefore had no
    cross-repo edges at all, and nothing anywhere said so: a review simply
    reported no cross-repo callers, which reads exactly like a truthful
    "nothing calls this".

    Returns the ids actually enqueued — a dedup hit yields no id, which is the
    point: indexing every repo of one group coalesces into one materialize.
    """
    from src.groups import get_group_manager
    from src.sync.queue import KIND_CROSS_REPO_MATERIALIZE, enqueue

    enqueued: list[str] = []
    for g in get_group_manager().groups_containing(repo_slug):
        # THE TENANT TRAVELS WITH THE NAME. A group name stopped identifying a
        # group when two workspaces could hold the same one: the handler
        # loading by name alone would open somebody else's group or, for a
        # tenant-scoped one, none at all — and the dedup key would have
        # collapsed two tenants' materialisations into a single job that ran
        # for whichever of them enqueued first.
        ws = g.workspace_id or "default"
        job_id = enqueue(
            kind=KIND_CROSS_REPO_MATERIALIZE,
            payload={"group_name": g.name, "workspace_id": ws},
            dedup_key=f"materialize:{ws}:{g.name}",
            enqueued_by=enqueued_by,
        )
        if job_id:
            enqueued.append(job_id)
    return enqueued


def rematerialize_group(
    group: RepoGroup,
    settings: Settings | None = None,
) -> int:
    """Rebuild the group's cross-repo edges from local clones only.

    No cloning, no full extraction — just the docker-related files each
    resolver actually reads. Returns the number of persisted edges, or
    -1 when skipped because the digest is unchanged.
    """
    import hashlib

    settings = settings or get_settings()
    indexer = GroupIndexer(group, settings)

    # Collect docker-file extractions per repo that has a local clone.
    materializer = CrossRepoMaterializer(group)
    digest = hashlib.sha256()
    repos_seen = 0
    enabled = (
        set(settings.enabled_language_adapters)
        if settings.enabled_language_adapters else None
    )
    for parsed in group.parsed_repos():
        slug = parsed.slug if hasattr(parsed, "slug") else str(parsed)
        repo_path = settings.repo_path(slug)
        if not repo_path.exists():
            continue
        repos_seen += 1
        ctx = build_repo_context(repo_path)
        registry = build_default_registry(ctx=ctx, repo_root=repo_path, enabled=enabled)
        extractions: dict[str, ExtractionResult] = {}
        for f in _docker_files(repo_path):
            extractor = registry.match(f)
            if extractor is None:
                continue
            try:
                res = extractor.extract(f)
            except Exception as exc:  # noqa: BLE001
                logger.debug("remat_extract_failed file=%s err=%s", f, exc)
                continue
            rel = str(f.relative_to(repo_path))
            extractions[rel] = res
            try:
                digest.update(rel.encode())
                digest.update(f.read_bytes())
            except OSError:
                pass
        if extractions:
            materializer.add_repo(slug, extractions)

    if repos_seen == 0:
        logger.info("rematerialize_skipped group=%s reason=no_local_clones", group.name)
        return 0

    # Fold group membership into the digest so adding/removing a repo
    # invalidates even when no single repo's docker files changed —
    # cross-repo edges depend on which repos are present, not just their
    # file contents.
    digest.update(("|".join(sorted(group.repos))).encode())

    digest_path = indexer._cross_repo_graph_path().with_suffix(".digest")
    new_digest = digest.hexdigest()
    if digest_path.exists():
        try:
            if digest_path.read_text().strip() == new_digest:
                logger.info("rematerialize_skipped group=%s reason=digest_unchanged",
                            group.name)
                return -1
        except OSError:
            pass

    edges = materializer.materialize()
    graph_path = indexer._cross_repo_graph_path()
    if edges:
        n = indexer._persist_cross_repo_edges(edges)
    else:
        # No edges this round — remove any stale graph so consumers don't
        # read edges that no longer exist. (_persist early-returns on
        # empty without clearing the old file.)
        n = 0
        try:
            if graph_path.exists():
                graph_path.unlink()
        except OSError:
            pass

    try:
        digest_path.parent.mkdir(parents=True, exist_ok=True)
        digest_path.write_text(new_digest)
    except OSError as exc:
        logger.warning("remat_digest_write_failed err=%s", exc)
    logger.info("rematerialized group=%s edges=%d repos=%d",
                group.name, n, repos_seen)
    return n
