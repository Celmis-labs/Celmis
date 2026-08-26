"""Phase 8 — entry point for graph indexing of a repository.

`index_repo_graph(repo_path, repo_slug)`:
    1. build RepoContext (tsconfig paths + package.json deps + ...)
    2. build a LanguageRegistry with all enabled extractors
    3. walk repo files (with ignore patterns)
    4. for each file — registry.match() → extractor.extract()
    5. heuristic resolver
    6. write to FalkorDBLiteStore (idempotent — MERGE-based)

Stage 2.A.3 (May 2026) — refactored:
    - hardcoded TS+Vue dispatch removed, replaced with LanguageRegistry
    - file discovery is generic — the extension list is no longer in the pipeline
    - settings.enabled_language_adapters controls the enabled languages
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from src.config import Settings, get_settings
from src.indexing.graph.configs import build_repo_context
from src.indexing.graph.graph_store import make_graph_store
from src.indexing.graph.languages.factory import build_default_registry, walk_repo_files
from src.indexing.graph.resolver import HeuristicResolver

logger = logging.getLogger(__name__)


@dataclass
class GraphIndexResult:
    repo_slug: str
    files_processed: int
    files_skipped: int  # when the registry found no extractor (third batch of languages)
    parse_failures: int
    symbols: int
    edges: int
    elapsed_seconds: float
    extractors_used: dict[str, int]  # per-extractor file counts — for diagnostics


def index_repo_graph(
    repo_path: Path,
    repo_slug: str,
    settings: Settings | None = None,
    src_subdir: str | None = None,
) -> GraphIndexResult:
    """Full pipeline: extract + resolve + persist into FalkorDBLite.

    Args:
        repo_path: absolute path to the cloned repo.
        repo_slug: name for the resolved settings.repo_graph_path.
        src_subdir: restrict the walk to this subdirectory. **None means the
            whole repository**, and that is now the default.

            It used to default to "src", a layout assumption borrowed from one
            customer's repository, and it cost a real graph. Vault generation
            calls this without the argument, so a repo whose code is not all
            under `src/` had its graph silently rebuilt from `src/` alone.
            Measured: a Go+Rust repository lost every Go symbol — the Rust
            lives in `src/`, the Go in `cmd/` and `internal/` — while
            `/api/repos` went on reporting `indexed: true` with no error. Both
            other callers already passed None explicitly, one of them with a
            comment saying it would not assume that layout; only the caller
            that relied on the default was wrong, which is what a bad default
            looks like.
    """
    settings = settings or get_settings()
    repo_path = Path(repo_path).resolve()
    db_path = settings.repo_graph_path(repo_slug)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Build BESIDE the live graph, then swap. The old code unlinked first, so
    # a rebuild that produced less than the graph it replaced — a wrong root, a
    # grammar that failed to load, a clone that arrived shallow — destroyed
    # good data and reported success. Vault generation calls this and
    # downgrades any exception to "continuing", so the loss was silent twice
    # over.
    build_path = db_path.with_suffix(db_path.suffix + ".building")
    previous = db_path.with_suffix(db_path.suffix + ".previous")
    for stale in (build_path, previous):
        if stale.exists():
            stale.unlink()

    t0 = time.time()

    ctx = build_repo_context(repo_path)
    logger.info(
        "graph_index_repo_context aliases=%d deps=%d env_keys=%d",
        len(ctx.path_aliases), len(ctx.external_deps), len(ctx.env_keys),
    )

    # Build language registry, applying settings filter
    enabled = (
        set(settings.enabled_language_adapters)
        if settings.enabled_language_adapters else None
    )
    registry = build_default_registry(ctx=ctx, repo_root=repo_path, enabled=enabled)

    # File discovery — generic walker, registry decides per-file
    if src_subdir is not None and (repo_path / src_subdir).is_dir():
        src_root = repo_path / src_subdir
    else:
        src_root = repo_path

    all_files = walk_repo_files(src_root)

    all_symbols, all_edges = [], []
    parse_failures = 0
    files_skipped = 0
    extractors_used: dict[str, int] = {}

    for f in all_files:
        extractor = registry.match(f)
        if extractor is None:
            files_skipped += 1
            continue

        # Track per-extractor counts (via the language attr if present)
        ext_name = getattr(extractor, "language", "") or extractor.__class__.__name__
        extractors_used[ext_name] = extractors_used.get(ext_name, 0) + 1

        try:
            res = extractor.extract(f)
            all_symbols.extend(res.symbols)
            all_edges.extend(res.edges)
            if res.parse_errors:
                parse_failures += 1
        except Exception as exc:  # noqa: BLE001
            parse_failures += 1
            logger.warning("extract_failed file=%s err=%s", f, exc)

    files_processed = len(all_files) - files_skipped

    resolver = HeuristicResolver(ctx, all_symbols)
    resolved = resolver.resolve_edges(all_edges)

    store = make_graph_store(build_path)
    try:
        store.add_symbols_batch(all_symbols)
        n_edges = store.add_edges_batch(resolved)
        store.commit()
    finally:
        store.close()

    # Refuse to replace a graph that holds symbols with one that holds none.
    #
    # Not a general "fewer symbols" rule: a genuine deletion legitimately
    # shrinks a graph, and refusing that would leave a stale graph forever.
    # Zero is different — it is what a wrong root, a missing grammar or an
    # empty clone produces, and it is never what a real repository means.
    if not all_symbols and db_path.exists() and _graph_has_symbols(db_path):
        build_path.unlink(missing_ok=True)
        logger.error(
            "graph_index_refused_empty repo=%s files=%d root=%s — the existing "
            "graph was KEPT. A rebuild that finds nothing is a broken rebuild, "
            "not an empty repository.",
            repo_slug, len(all_files), src_root,
        )
        raise EmptyGraphRebuild(
            f"rebuilding the graph for {repo_slug!r} from {src_root} produced 0 "
            f"symbols while the existing graph is not empty; kept the existing "
            f"graph. Check the walk root and the language grammars."
        )

    if db_path.exists():
        db_path.replace(previous)
    build_path.replace(db_path)
    previous.unlink(missing_ok=True)

    elapsed = time.time() - t0
    result = GraphIndexResult(
        repo_slug=repo_slug,
        files_processed=files_processed,
        files_skipped=files_skipped,
        parse_failures=parse_failures,
        symbols=len(all_symbols),
        edges=n_edges,
        elapsed_seconds=elapsed,
        extractors_used=extractors_used,
    )
    logger.info(
        "graph_index_complete repo=%s files=%d skipped=%d symbols=%d edges=%d "
        "failures=%d extractors=%s %.1fs",
        repo_slug, result.files_processed, result.files_skipped,
        result.symbols, result.edges, result.parse_failures,
        extractors_used, elapsed,
    )
    return result


class EmptyGraphRebuild(RuntimeError):
    """A rebuild produced no symbols while a non-empty graph already existed.

    Raised rather than logged because the caller's choice matters: vault
    generation catches it and carries on with the graph it already had, and a
    deliberate re-index surfaces it to whoever asked.
    """


def _graph_has_symbols(db_path: Path) -> bool:
    """Whether the graph on disk holds anything. False on any read failure —
    an unreadable graph is not worth protecting."""
    try:
        store = make_graph_store(db_path)
        try:
            # A direct query, because the convenience readers all take a
            # filter and none of them means "anything at all":
            # `symbols_in_path("")` returns [] rather than everything, and
            # `find_by_name_like("%")` matches nothing. The first version used
            # the former and would have reported EVERY populated graph as
            # empty — turning this guard off entirely, silently, which is the
            # failure it exists to prevent. A test caught it.
            return bool(store.query("MATCH (s:Symbol) RETURN s LIMIT 1"))
        finally:
            store.close()
    except Exception:  # noqa: BLE001
        return False
