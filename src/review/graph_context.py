"""Graph context for a pull request — the changed symbols, and who depends on them.

Moved here from `ReviewOrchestrator._build_graph_context` and
`_query_cross_repo_callers`, so the lookup can be driven against a fake store
and handed to every engine, not composed inside the orchestrator where no test
ever reached it. What the move changed, and the bug behind each change:

  * `pr.changed_files[:5]` — a seven-file PR had two files the graph was never
    asked about, and nothing said so. Every changed file is queried now, in
    batches of `FILES_PER_QUERY`, under one cap on the total (`MAX_SYMBOLS`);
    when the cap cuts, the files it left out are named in the summary.
  * `LIMIT 50` symbols, twenty rendered, no callers at all — the "blast radius"
    the architect received was a list of the changed symbols' names. The
    callers are the radius. Per changed symbol the context now carries who in
    this repository reaches it (the count exact, the sample capped at
    `MAX_CALLERS_PER_SYMBOL`) and which other repositories of the group
    reference its file.
  * the repository-wide cross-repo edge count was reported as the PR's blast
    radius — the same number for every PR in a repository, and written to the
    run row as if it were this PR's. `cross_repo_callers_count` now counts the
    edges whose target is one of the changed files; the repository-wide
    breakdown stays in the summary as what it is.
  * "(repo not indexed — `analyzer generate` first for graph context)" was
    handed to the model as if it were context, logged at DEBUG and recorded
    nowhere. On the benchmark this was measured against, the run store held
    161 reviews, every one with cross_repo_callers=0, there was no graph file
    for any benchmark slug, and not one log line said so: the whole comparison
    ran without the product's main differentiator and nobody could tell from
    the outside. The absence is now a `ParameterAdjustment` on the run — the
    road the dropped temperature already travels to the PR banner and the run
    row — so a review with a blast radius and one without no longer look the
    same.
  * every symbol of a touched file was "changed". Against a real index
    (sentry, 14 MB) seven files held 1,000+ symbols, `ORDER BY file LIMIT 200`
    filled the budget from the two alphabetically-first files, and the other
    five — present in the graph — came back as "not indexed". Now a symbol is
    changed when its lines overlap a hunk of the PR (old side, the revision
    the index was built from), the per-file COUNT is a separate aggregate
    query no cap can cut, and "not indexed" is derived from that count alone.
  * a Bitbucket repository is indexed under `{owner}-{name}` (see
    `ParsedRepo.slug`) while `PullRequest.repo_slug` asks for
    `bitbucket_{owner}-{name}`; the graph was never found for one. Both spellings
    are tried, in that order.
  * "the index predates them — re-run `analyzer generate`" was attached to
    every pre-existing changed file the index held no symbol for. It was
    false in the case that actually occurs: the clone sits at the default
    branch's HEAD, and on one benchmark PR opened against a 2023 base, 51 of
    its 172 changed files were missing because they had been renamed or
    deleted SINCE. The index postdates them; re-running `analyzer generate`
    would never have added one of them, however many times the operator ran
    it. The clone decides which it is now — a missing file still present in
    `settings.repo_path(slug)` is a stale index and re-indexing is the
    remedy; one that is absent from it is gone at the indexed revision, and
    the note says there is nothing to re-index rather than inventing a
    remedy. With no clone on the machine neither cause is claimed.

Nothing here raises: a review must not be lost over its context. Every failure
mode returns a `GraphContext` whose `note` says what was missing and why.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from src.llm.capabilities import ParameterAdjustment
from src.review.models import PullRequest

logger = logging.getLogger(__name__)

__all__ = [
    "ADJUST_BASE_TOO_OLD",
    "ADJUST_PARTIAL",
    "ADJUST_UNAVAILABLE",
    "ADJUST_UNSUPPORTED_LANGUAGE",
    "CALLER_EDGE_KINDS",
    "FILES_PER_QUERY",
    "MAX_CALLERS_PER_SYMBOL",
    "MAX_SYMBOLS",
    "PARAM_GRAPH_CONTEXT",
    "UNPARSED_LANGUAGES",
    "Caller",
    "ChangedSymbol",
    "CrossRepoCaller",
    "GraphContext",
    "build_graph_context",
    "graph_files",
    "graph_slug_candidates",
    "indexed_suffixes",
]

# ─── Caps ─────────────────────────────────────────────────────────────

#: Changed files per Cypher query. A PR with 300 files makes ten queries, not
#: one with a 300-element IN list and not 300 round trips.
FILES_PER_QUERY = 32
#: Symbol rows read per batch of files — the whole file's symbols, before
#: the overlap filter, because the filter runs here and not in Cypher. A
#: batch that exceeds it has files only partly scanned; they are named.
MAX_SYMBOL_ROWS = 4000
#: Changed symbols kept per review. Beyond this the summary is no longer
#: read anyway; the files left out are named so the gap is visible.
MAX_SYMBOLS = 200
#: Callers listed per symbol. The COUNT is exact regardless — "41 callers,
#: eight shown" is the fact; the sample is for the model to name one.
MAX_CALLERS_PER_SYMBOL = 8
#: Symbols rendered with their callers in the full summary; the rest are
#: compacted to one line per file.
SYMBOLS_IN_SUMMARY = 60
#: Symbols the brief rendering names as most depended-on.
BRIEF_TOP_SYMBOLS = 8
#: Rows per cross-repo query — a group graph can hold many edges into one file.
MAX_CROSS_REPO_ROWS = 500

#: Incoming edges that mean "this code is reached from there". DEFINED_IN is
#: the containment edge (a method lives in its class) and would make every
#: member of a changed class look like a caller of it. Each name must be in
#: `graph_store.ALLOWED_EDGE_KINDS`, because it is embedded in Cypher as a
#: literal; tests pin that.
CALLER_EDGE_KINDS = ("CALLS", "IMPORTS", "EXTENDS", "IMPLEMENTS", "REFERENCES", "TEMPLATE_REF")

#: A pre-existing file whose suffix the indexer dispatches on, and which the
#: graph holds no symbol for, is a CANDIDATE gap and nothing more: the suffix
#: says only that an extractor would have been used. Which of the three causes
#: it is — index behind, file gone before the indexed revision, or unknown —
#: is `_classify_missing`'s job, and reading the suffix as "evidence the index
#: predates the file" is the false claim the note used to make. A README or a
#: YAML file with no symbol is not even a candidate; kinds dispatched by
#: filename or content sniff (Dockerfile, compose, k8s) are out for the same
#: reason. A file in a language with NO extractor is a fourth thing again —
#: see `UNPARSED_LANGUAGES`.
#:
#: The fallback below is used only when the indexer package cannot be imported. A
#: review must not fail because the indexer moved, and a stale answer here is
#: worse than none only if it is silent — `indexed_suffixes()` logs the fall
#: back. Kept deliberately small: the languages that existed before the
#: registry became the source of truth.
_FALLBACK_INDEXED_SUFFIXES = frozenset({
    ".py", ".pyi",
    ".ts", ".tsx", ".js", ".jsx", ".cjs", ".mjs", ".cts", ".mts", ".vue",
    ".java", ".go", ".cs", ".csx", ".php", ".phtml",
    ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h", ".c",
    ".tf",
})


@lru_cache(maxsize=1)
def indexed_suffixes() -> frozenset[str]:
    """Suffixes the indexer has an extractor for, asked of the indexer itself.

    This used to be a hand-copied frozenset, which is a copy of a list and so
    a list that goes stale: the day sixteen languages were added through the
    generic tags extractor, the review would still have called every changed
    `.rb` file "not a language we parse". Derived from the registry instead,
    so the two answers cannot disagree.
    """
    try:
        from src.indexing.graph.languages.factory import supported_suffixes

        found = supported_suffixes()
        if found:
            return found
    except Exception as exc:  # noqa: BLE001
        logger.warning("indexed_suffixes_fallback err=%s", exc)
    return _FALLBACK_INDEXED_SUFFIXES


#: Source languages this indexer has NO extractor for, by suffix.
#:
#: Needed because "no symbols for this file" has one more cause than the three
#: `_classify_missing` knows, and it is the only one no amount of indexing can
#: fix. Measured: the discourse checkout holds 8185 `.rb` files and its graph
#: held zero Ruby symbols, and the review said NOTHING — a Ruby pull request on
#: a fully indexed repository was indistinguishable from one on a repository
#: nobody had ever indexed. Ruby is parsed now; these are the ones that are not.
#:
#: Only suffixes that unambiguously mean one language. `.m` (Objective-C and
#: MATLAB) and `.v` (Verilog, Coq and V) are left out: naming the wrong
#: language is worse than saying nothing, because the operator would go looking
#: for a parser they do not need.
UNPARSED_LANGUAGES: dict[str, str] = {
    ".hs": "Haskell", ".lhs": "Haskell",
    ".erl": "Erlang", ".hrl": "Erlang",
    ".pl": "Perl", ".pm": "Perl",
    ".zig": "Zig",
    ".nim": "Nim",
    ".cr": "Crystal",
    ".jl": "Julia",
    ".groovy": "Groovy", ".gradle": "Gradle",
    ".clj": "Clojure", ".cljs": "ClojureScript", ".cljc": "Clojure",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell",
    ".sql": "SQL",
    ".ps1": "PowerShell",
    ".tcl": "Tcl",
    ".purs": "PureScript",
    ".hx": "Haxe",
    ".pas": "Pascal",
    ".vb": "Visual Basic",
}

# ─── The note on the run ─────────────────────────────────────────────

#: `ParameterAdjustment.parameter` for the graph stage. Not a model parameter,
#: but the same list: it is the one road that reaches the run row, the API and
#: the PR banner without a new column, and `adjustments_notice` renders kinds
#: it does not know rather than dropping them.
PARAM_GRAPH_CONTEXT = "graph_context"
#: No graph reached the review at all.
ADJUST_UNAVAILABLE = "unavailable"
#: A graph was read, but some changed files it should have known were absent.
ADJUST_PARTIAL = "partial"
#: The same gap, with no remedy: every missing file is absent from the checkout
#: the graph was built from, so the PR's base is older than the indexed
#: revision and no amount of re-indexing can cover them. Its own action rather
#: than a differently-worded `partial`, because the reviews page keys the
#: REMEDY off it: "partial" links to the Repositories page to index, and
#: sending the operator there for this case is the dead end being removed.
ADJUST_BASE_TOO_OLD = "base_too_old"
#: Some changed files are source in a language this indexer cannot parse at
#: all. Its own action because its remedy is the ONLY one that is "none":
#: re-indexing runs the same extractors and finds the same nothing.
ADJUST_UNSUPPORTED_LANGUAGE = "unsupported_language"
#: What was asked for, in the words the banner prints.
GRAPH_REQUESTED = "blast radius from the code graph"

HOW_TO_INDEX = (
    "run `analyzer generate` or index it from the Repositories page "
    "(POST /api/repos/index-all)"
)

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_NO_SYMBOLS = "no_symbols"
STATUS_NO_FILES = "no_changed_files"
STATUS_NOT_INDEXED = "not_indexed"
STATUS_OPEN_FAILED = "open_failed"
STATUS_QUERY_FAILED = "query_failed"


# ─── Result shape ────────────────────────────────────────────────────


@dataclass
class Caller:
    """One symbol in this repository that reaches a changed symbol."""

    name: str
    kind: str
    file: str
    start_line: int | None
    edge: str


@dataclass
class CrossRepoCaller:
    """A symbol in another repository of the group that references a changed file.

    The group graph is materialized at FILE granularity (`to_id` is the target
    file's module node — see `groups/cross_repo.py`), so this is attached to
    every changed symbol of that file rather than resolved per symbol.
    """

    repo: str
    name: str
    edge: str
    edges: int


@dataclass
class ChangedSymbol:
    id: str
    name: str
    kind: str
    file: str
    start_line: int | None
    callers: list[Caller] = field(default_factory=list)
    callers_total: int = 0
    cross_repo_callers: list[CrossRepoCaller] = field(default_factory=list)

    @property
    def cross_repo_edges(self) -> int:
        return sum(c.edges for c in self.cross_repo_callers)


@dataclass
class GraphContext:
    """What the graph said about this PR, and what it could not say.

    `summary` is the full markdown for the agents that reason about impact
    (architect, security, the Claude engine); `brief` is three lines for the
    agents that would drown in it (quality, tests) and still need to know
    which changed symbol the rest of the code depends on. Both are "" when
    there is nothing to say — the prompt template then prints its own
    "(no graph context)", and `note` says why.
    """

    status: str
    symbols: list[ChangedSymbol] = field(default_factory=list)
    files_queried: list[str] = field(default_factory=list)
    #: Symbols the index holds per changed file — every symbol, not only the
    #: changed ones. From its own aggregate query, so a cap cannot shrink it.
    symbols_in_files: dict[str, int] = field(default_factory=dict)
    #: Changed files a cap left partly or wholly unread — named in the summary.
    files_beyond_cap: list[str] = field(default_factory=list)
    #: Pre-existing files of an indexed language with no symbol in the graph —
    #: the three buckets below concatenated, in `files_queried` order.
    files_not_indexed: list[str] = field(default_factory=list)
    #: Changed files written in a language this indexer has no extractor for.
    #: Kept apart from `files_not_indexed` because re-indexing closes that one
    #: and can never close this one — there is nothing to run.
    files_unparsed_language: list[str] = field(default_factory=list)
    #: Of those, the ones still in the checkout the graph was built from: the
    #: index is stale for them (or the extractor could not parse them), and
    #: re-indexing is the remedy.
    files_missing_stale: list[str] = field(default_factory=list)
    #: Of those, the ones absent from that checkout: renamed or deleted before
    #: the indexed revision, so the PR's base is older than the index and
    #: re-indexing cannot reach them.
    files_missing_gone: list[str] = field(default_factory=list)
    #: Of those, the ones the checkout could not answer for — it is not on this
    #: machine, or the path could not be resolved inside it. Neither cause is
    #: claimed for these.
    files_missing_undetermined: list[str] = field(default_factory=list)
    #: Edges from other repositories into ANY file of this one, by source repo.
    cross_repo_edges_by_repo: dict[str, int] = field(default_factory=dict)
    #: Who in other repositories references each CHANGED file — kept per file,
    #: not only on the symbols, because a file the group graph references and
    #: the repo graph holds no symbol for (a deleted file, for one) is still
    #: somebody's dependency.
    cross_repo_by_file: dict[str, list[CrossRepoCaller]] = field(default_factory=dict)
    #: Edges from other repositories into the CHANGED files — the PR's radius.
    cross_repo_callers_count: int = 0
    truncated: bool = False
    note: ParameterAdjustment | None = None
    summary: str = ""
    brief: str = ""

    @property
    def has_graph(self) -> bool:
        return self.status in (STATUS_OK, STATUS_PARTIAL, STATUS_NO_SYMBOLS)


# ─── Entry point ─────────────────────────────────────────────────────


def build_graph_context(
    pr: PullRequest,
    *,
    settings: Any | None = None,
    open_store: Callable[[Path], Any] | None = None,
    workspace_id: str | None = None,
) -> GraphContext:
    """Look up the changed symbols and their callers for `pr`. Never raises.

    `settings` needs `repo_graph_path(slug)` and `workspace_dir`; `open_store`
    takes a path and returns something with `query(cypher, params)` and
    `close()`. Both default to the product's own — the parameters exist so a
    test can hand in a fake graph instead of a FalkorDB file.
    """
    files = graph_files(pr)
    try:
        settings = settings or _default_settings()
        open_store = open_store or _default_opener()
        found = _find_graph(pr, settings)
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph_context_unavailable pr=%d err=%s", pr.number, exc)
        return _unavailable(
            files, status=STATUS_OPEN_FAILED,
            reason=f"graph settings could not be loaded ({_short(exc)})",
        )

    if found is None:
        logger.warning(
            "graph_context_missing pr=%d repo=%s slugs_tried=%s reason=not_indexed",
            pr.number, pr.repo, ",".join(graph_slug_candidates(pr)),
        )
        return _unavailable(
            files, status=STATUS_NOT_INDEXED,
            reason=f"repository not indexed; {HOW_TO_INDEX}",
        )
    slug, db_path = found

    if not files:
        return GraphContext(status=STATUS_NO_FILES)

    try:
        store = open_store(db_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph_open_failed pr=%d db=%s err=%s", pr.number, db_path, exc)
        return _unavailable(
            files, status=STATUS_OPEN_FAILED,
            reason=f"graph at {db_path} could not be opened ({_short(exc)}); "
                   "re-run `analyzer generate`",
        )

    try:
        symbols, files_queried, files_beyond_cap, counts = _changed_symbols(store, pr, files)
        _attach_callers(store, symbols)
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph_query_failed pr=%d db=%s err=%s", pr.number, db_path, exc)
        return _unavailable(
            files, status=STATUS_QUERY_FAILED,
            reason=f"graph query failed ({_short(exc)}); re-run `analyzer generate`",
        )
    finally:
        with contextlib.suppress(Exception):
            store.close()

    by_repo, per_file = _cross_repo(
        slug, files, settings, open_store, workspace_id=workspace_id
    )
    for sym in symbols:
        sym.cross_repo_callers = list(per_file.get(sym.file, ()))
    cross_count = sum(c.edges for callers in per_file.values() for c in callers)

    unparsed = _unparsed_language_files(files)
    missing = _files_missing_from_index(pr, files_queried, counts)
    stale, gone, undetermined = _classify_missing(missing, _clone_root(slug, settings))
    kept = {*stale, *gone, *undetermined}
    files_not_indexed = [f for f in missing if f in kept]
    if files_not_indexed:
        status = STATUS_PARTIAL
    elif symbols or any(counts.values()):
        status = STATUS_OK
    else:
        status = STATUS_NO_SYMBOLS

    ctx = GraphContext(
        status=status,
        symbols=symbols,
        files_queried=files_queried,
        symbols_in_files=counts,
        files_beyond_cap=files_beyond_cap,
        files_not_indexed=files_not_indexed,
        files_missing_stale=stale,
        files_missing_gone=gone,
        files_missing_undetermined=undetermined,
        cross_repo_edges_by_repo=by_repo,
        cross_repo_by_file=per_file,
        cross_repo_callers_count=cross_count,
        truncated=bool(files_beyond_cap),
        files_unparsed_language=unparsed,
    )
    if files_not_indexed:
        # `partial` keeps the indexing remedy, which is true as long as ONE
        # missing file is still in the checkout, or the checkout could not be
        # consulted (indexing restores it and settles the question). Only when
        # every missing file is provably gone is there nothing to index.
        action = ADJUST_PARTIAL if (stale or undetermined) else ADJUST_BASE_TOO_OLD
        ctx.note = ParameterAdjustment(
            agent=None, parameter=PARAM_GRAPH_CONTEXT,
            requested=GRAPH_REQUESTED,
            sent=f"{len(files) - len(files_not_indexed)} of {len(files)} changed files",
            action=action,
            reason=_missing_reason(len(files), stale, gone, undetermined)
            + _unparsed_clause(unparsed),
        )
        logger.info(
            "graph_context_partial pr=%d missing=%d/%d stale=%d gone=%d undetermined=%d "
            "files=%s",
            pr.number, len(files_not_indexed), len(files),
            len(stale), len(gone), len(undetermined), _names(files_not_indexed),
        )
    elif unparsed:
        # The fourth cause of "no symbols", and the only one no amount of
        # indexing can fix: there is no extractor for the language. It gets its
        # own action because every other action's remedy is "index it", and
        # offering that here would send the operator to run a job that will
        # find exactly the same nothing.
        ctx.note = ParameterAdjustment(
            agent=None, parameter=PARAM_GRAPH_CONTEXT,
            requested=GRAPH_REQUESTED,
            sent=f"{len(files) - len(unparsed)} of {len(files)} changed files",
            action=ADJUST_UNSUPPORTED_LANGUAGE,
            reason=_unparsed_reason(len(files), unparsed),
        )
        logger.info(
            "graph_context_unparsed_language pr=%d files=%d/%d langs=%s",
            pr.number, len(unparsed), len(files),
            ",".join(sorted(_languages_of(unparsed))),
        )
    if symbols or cross_count or by_repo or any(counts.values()):
        ctx.summary = _render_summary(ctx, total_files=len(files))
        ctx.brief = _render_brief(ctx, total_files=len(files))
    logger.info(
        "graph_context pr=%d status=%s files=%d symbols=%d callers=%d cross_repo=%d",
        pr.number, status, len(files), len(symbols),
        sum(s.callers_total for s in symbols), cross_count,
    )
    return ctx


def graph_files(pr: PullRequest) -> list[str]:
    """The paths to ask the graph about, in the PR's order.

    The graph was built from the base revision, so a renamed file is there
    under its OLD path; asking for the new one would find nothing and call
    the rename "not indexed". Every other file is asked for by the path the
    diff names.
    """
    seen: set[str] = set()
    out: list[str] = []
    for h in pr.hunks:
        path = _graph_path_of(h)
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _graph_path_of(h: Any) -> str:
    if getattr(h, "is_renamed", False) and getattr(h, "old_file_path", ""):
        return str(h.old_file_path)
    return str(h.file_path)


def graph_slug_candidates(pr: PullRequest) -> list[str]:
    """Slugs under which this repository's graph may have been written.

    `PullRequest.repo_slug` is `{provider}_{owner}-{name}`; the indexer keys
    the graph by `ParsedRepo.slug`, which drops the prefix for Bitbucket (the
    clones that predate the prefix live under `{owner}-{name}`). GitHub and
    GitLab spell the two the same, so only Bitbucket gets a second try.
    """
    slugs = [pr.repo_slug]
    if pr.local_slug not in slugs:
        slugs.append(pr.local_slug)
    return slugs


# ─── Lookups ─────────────────────────────────────────────────────────


def _find_graph(pr: PullRequest, settings: Any) -> tuple[str, Path] | None:
    for slug in graph_slug_candidates(pr):
        db_path = Path(settings.repo_graph_path(slug))
        if db_path.exists():
            return slug, db_path
    return None


def _changed_symbols(
    store: Any, pr: PullRequest, files: list[str],
) -> tuple[list[ChangedSymbol], list[str], list[str], dict[str, int]]:
    """The symbols of the changed files whose lines the PR touches.

    Two queries per batch of files. The COUNT per file is its own aggregate
    so that "this file has no symbols in the index" is a fact about the index
    and never about a LIMIT: the detail rows are ordered by file and capped,
    and a file with 800 constants ahead in the alphabet would otherwise eat
    the rows of every file behind it, which then read as absent. A file whose
    rows the cap did cut is reported in `files_beyond_cap`, not as missing.

    A symbol is changed when its line span overlaps a hunk's OLD-side range —
    the revision the graph was built from. A deleted file's symbols all count.
    Returns (symbols, files queried, files beyond a cap, symbols per file).
    """
    ranges = _old_line_ranges(pr)
    symbols: list[ChangedSymbol] = []
    files_queried: list[str] = []
    files_beyond_cap: list[str] = []
    counts: dict[str, int] = {}
    for batch in _chunks(files, FILES_PER_QUERY):
        files_queried.extend(batch)
        for row in store.query(
            "MATCH (s:Symbol) WHERE s.file IN $files AND s.kind <> 'file_module' "
            "RETURN s.file AS file, count(s) AS n",
            params={"files": list(batch)},
        ):
            counts[str(row.get("file") or "")] = int(row.get("n") or 0)
        rows = store.query(
            "MATCH (s:Symbol) WHERE s.file IN $files AND s.kind <> 'file_module' "
            "RETURN s.id AS id, s.name AS name, s.kind AS kind, s.file AS file, "
            "       s.start_line AS start_line, s.end_line AS end_line "
            "ORDER BY s.file, s.start_line "
            "LIMIT $cap",
            params={"files": list(batch), "cap": MAX_SYMBOL_ROWS},
        )
        rows_seen: dict[str, int] = {}
        for row in rows:
            sid = str(row.get("id") or "")
            file = str(row.get("file") or "")
            if not sid:
                continue
            rows_seen[file] = rows_seen.get(file, 0) + 1
            start = _int_or_none(row.get("start_line"))
            end = _int_or_none(row.get("end_line"))
            if not _overlaps(start, end, ranges.get(file)):
                continue
            symbols.append(ChangedSymbol(
                id=sid,
                name=str(row.get("name") or sid),
                kind=str(row.get("kind") or "symbol"),
                file=file,
                start_line=start,
            ))
        for file in batch:
            if counts.get(file, 0) > rows_seen.get(file, 0):
                files_beyond_cap.append(file)
    if len(symbols) > MAX_SYMBOLS:
        cut_files = {sym.file for sym in symbols[MAX_SYMBOLS:]}
        symbols = symbols[:MAX_SYMBOLS]
        files_beyond_cap.extend(f for f in files if f in cut_files and f not in files_beyond_cap)
    return symbols, files_queried, files_beyond_cap, counts


#: "Every line" — a deleted file, whose symbols all go with it.
_WHOLE_FILE: tuple[tuple[int, int], ...] = ((1, 1 << 31),)


def _old_line_ranges(pr: PullRequest) -> dict[str, tuple[tuple[int, int], ...]]:
    """Per graph path, the old-side line ranges the PR touches."""
    out: dict[str, list[tuple[int, int]]] = {}
    whole: set[str] = set()
    for h in pr.hunks:
        path = _graph_path_of(h)
        if getattr(h, "is_new_file", False):
            continue
        if getattr(h, "is_deleted_file", False):
            whole.add(path)
            continue
        lo = max(int(h.old_start or 1), 1)
        hi = lo + max(int(h.old_count or 0), 1) - 1
        out.setdefault(path, []).append((lo, hi))
    return {
        **{path: tuple(ranges) for path, ranges in out.items()},
        **dict.fromkeys(whole, _WHOLE_FILE),
    }


def _overlaps(
    start: int | None, end: int | None, ranges: tuple[tuple[int, int], ...] | None,
) -> bool:
    """Does a symbol spanning start..end touch any of the ranges?

    A symbol with no line at all is kept — the extractor did not say where it
    is, and dropping it would hide a change rather than a line number.
    """
    if not ranges:
        return False
    if start is None:
        return True
    last = end if end is not None and end >= start else start
    return any(lo <= last and start <= hi for lo, hi in ranges)


def _attach_callers(store: Any, symbols: list[ChangedSymbol]) -> None:
    """Exact caller counts for every symbol, a capped sample for those with any.

    Two shapes of query on purpose: one aggregate over a batch of ids gives
    the counts (cheap, bounded by the number of symbols), then one small
    indexed lookup per symbol that has callers gives the sample. A single
    sampled query over the batch would let one hot symbol eat the whole
    LIMIT and leave the others with "0 shown" next to a count of dozens.
    """
    if not symbols:
        return
    pattern = "|".join(CALLER_EDGE_KINDS)
    by_id = {s.id: s for s in symbols}
    for batch in _chunks(list(by_id), 100):
        rows = store.query(
            f"MATCH (c:Symbol)-[r:{pattern}]->(s:Symbol) WHERE s.id IN $ids "
            "RETURN s.id AS target, count(r) AS callers",
            params={"ids": list(batch)},
        )
        for row in rows:
            sym = by_id.get(str(row.get("target") or ""))
            if sym is not None:
                sym.callers_total = int(row.get("callers") or 0)
    for sym in symbols:
        if sym.callers_total <= 0:
            continue
        rows = store.query(
            f"MATCH (c:Symbol)-[r:{pattern}]->(s:Symbol) WHERE s.id = $id "
            "RETURN c.name AS name, c.kind AS kind, c.file AS file, "
            "       c.start_line AS start_line, type(r) AS edge "
            "ORDER BY c.file, c.start_line "
            "LIMIT $cap",
            params={"id": sym.id, "cap": MAX_CALLERS_PER_SYMBOL},
        )
        sym.callers = [
            Caller(
                name=str(row.get("name") or "?"),
                kind=str(row.get("kind") or "symbol"),
                file=str(row.get("file") or ""),
                start_line=_int_or_none(row.get("start_line")),
                edge=str(row.get("edge") or "REFERENCES"),
            )
            for row in rows
        ]


def _cross_repo(
    slug: str, files: list[str], settings: Any, open_store: Callable[[Path], Any],
    workspace_id: str | None = None,
) -> tuple[dict[str, int], dict[str, list[CrossRepoCaller]]]:
    """Edges from the group's other repositories into this one.

    Returns (edges into ANY file of this repo by source repo, callers of each
    CHANGED file). A group file that cannot be opened or queried is skipped
    with a log line, as before: the per-repo graph already answered, and
    losing the review over the group's index would be the wrong trade.
    """
    by_repo: dict[str, int] = {}
    per_file: dict[str, list[CrossRepoCaller]] = {}
    # ASK THE MANAGER WHERE THE GRAPHS ARE. This globbed `groups/*.fdblite`,
    # which was wrong in both directions at once: it read EVERY tenant's
    # cross-repo edges into every review — the leak the group scoping exists to
    # close — and, once a scoped group's graph moved into its tenant directory
    # beside the YAML, the flat glob stopped finding it at all, so the feature
    # went quiet for exactly the installs that had it.
    try:
        from src.groups.manager import GroupManager

        mgr = GroupManager(settings=settings)
        group_files = sorted(
            path for path in (mgr.graph_path(g) for _p, g in mgr.iter_groups(workspace_id))
            if path.exists()
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("cross_repo_groups_unreadable err=%s", exc)
        return by_repo, per_file
    for fdblite in group_files:
        try:
            store = open_store(fdblite)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cross_repo_open_failed group=%s err=%s", fdblite.name, exc)
            continue
        try:
            totals = store.query(
                "MATCH (a:Symbol)-[r]->(b:Symbol) WHERE b.module = $slug "
                "RETURN a.module AS source_repo, count(r) AS edges",
                params={"slug": slug},
            )
            for row in totals:
                src = str(row.get("source_repo") or "?")
                by_repo[src] = by_repo.get(src, 0) + int(row.get("edges") or 0)
            for batch in _chunks(files, FILES_PER_QUERY):
                rows = store.query(
                    "MATCH (a:Symbol)-[r]->(b:Symbol) "
                    "WHERE b.module = $slug AND b.file IN $files "
                    "RETURN b.file AS file, a.module AS source_repo, a.name AS name, "
                    "       type(r) AS edge, count(r) AS edges "
                    "LIMIT $cap",
                    params={"slug": slug, "files": list(batch), "cap": MAX_CROSS_REPO_ROWS},
                )
                for row in rows:
                    per_file.setdefault(str(row.get("file") or ""), []).append(CrossRepoCaller(
                        repo=str(row.get("source_repo") or "?"),
                        name=str(row.get("name") or "?"),
                        edge=str(row.get("edge") or "REFERENCES_REPO"),
                        edges=int(row.get("edges") or 0),
                    ))
        except Exception as exc:  # noqa: BLE001
            logger.warning("cross_repo_query_failed group=%s err=%s", fdblite.name, exc)
        finally:
            with contextlib.suppress(Exception):
                store.close()
    return by_repo, per_file


def _files_missing_from_index(
    pr: PullRequest, files_queried: Iterable[str], counts: dict[str, int],
) -> list[str]:
    """Changed files the graph should have known and did not — the candidates.

    "Should have known" is two tests: the file existed before this PR (a file
    the PR adds cannot be in an index built from the base branch, and its
    absence is not a gap), and its suffix is one the indexer parses by
    extension. A deleted file passes both — its callers are exactly the
    radius of the deletion — and so does a rename, under its old path.
    "Did not" is the index's own count for the file, never a capped list.

    Candidates, not gaps: `_classify_missing` then asks the checkout which of
    them the index could still be made to hold.
    """
    new_files = {_graph_path_of(h) for h in pr.hunks if getattr(h, "is_new_file", False)}
    pre_existing = {_graph_path_of(h) for h in pr.hunks if not getattr(h, "is_new_file", False)}
    return [
        f for f in files_queried
        if counts.get(f, 0) == 0
        and f in pre_existing and f not in new_files
        and Path(f).suffix.lower() in indexed_suffixes()
    ]


# ─── Which of the gaps re-indexing can actually close ────────────────


def _clone_root(slug: str, settings: Any) -> Path | None:
    """The checkout the graph was built from, or None when it is not here.

    None is "cannot tell", never "gone": the review may be running on a
    machine that holds the graph and not the clone, and a purge removes the
    checkout while leaving the graph behind.
    """
    repo_path = getattr(settings, "repo_path", None)
    if repo_path is None:
        return None
    try:
        root = Path(repo_path(slug))
        return root if root.is_dir() else None
    except Exception:  # noqa: BLE001 - a settings double, a bad slug, a dead mount
        return None


#: Bytes read to decide a file is blank. An empty `__init__.py` is 0 and a
#: newline-only one is 1; nothing that needs more than this to look empty is
#: worth a read here.
BLANK_FILE_BYTES = 4096


def _classify_missing(
    candidates: Iterable[str], root: Path | None,
) -> tuple[list[str], list[str], list[str]]:
    """Split the missing files into (stale, gone, undetermined).

    The discriminator is the clone the graph was built from. A file still in
    it has no symbols because the index is behind (or the extractor did not
    parse it), and re-indexing is the remedy. A file NOT in it does not exist
    at the indexed revision at all — the PR's base is older than the index —
    and re-indexing will never add it. Without a clone neither can be said.

    A file that is in the clone and holds nothing (an empty `__init__.py`) is
    dropped from all three: it has no symbols because it has no code, which
    is not a gap in the index. A comments-only module is NOT caught by this —
    that needs the extractor's own parse — and is still reported as stale.
    """
    if root is None:
        return [], [], list(candidates)
    stale: list[str] = []
    gone: list[str] = []
    undetermined: list[str] = []
    for f in candidates:
        path = _inside(root, f)
        state = "unknown" if path is None else _checkout_state(path)
        if state == "present":
            stale.append(f)
        elif state == "absent":
            gone.append(f)
        elif state == "unknown":
            undetermined.append(f)
        # "blank" falls through: nothing to index, so nothing missing.
    return stale, gone, undetermined


def _inside(root: Path, rel: str) -> Path | None:
    """`rel` resolved under `root`, or None when it does not belong under it.

    Diff paths are repository-relative and POSIX-spelled. One that is absolute
    or climbs out with `..` is not a path in this checkout, and statting it
    would answer about some other file entirely — so it is "cannot tell".
    """
    parts = PurePosixPath(rel).parts
    if not parts or PurePosixPath(rel).is_absolute() or ".." in parts:
        return None
    return root.joinpath(*parts)


def _checkout_state(path: Path) -> str:
    """"present" | "absent" | "blank" | "unknown" for one path in the clone."""
    try:
        if path.is_symlink():
            # A dangling symlink is still an entry at the indexed revision —
            # one killed a whole pull on 2026-08-22 (commit ab2a864) — and
            # `exists()` follows the link, so it must be asked first or the
            # entry reads as deleted when only its target is.
            return "present"
        stat = path.stat()
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    if not stat.st_size:
        return "blank"
    try:
        if stat.st_size <= BLANK_FILE_BYTES and not path.read_bytes().strip():
            return "blank"
    except OSError:
        return "unknown"
    return "present"


def _names(files: list[str], limit: int = 5) -> str:
    """As many file names as a sentence can carry, and a count for the rest."""
    rest = len(files) - limit
    return ", ".join(files[:limit]) + (f" and {rest} more" if rest > 0 else "")


def _them(n: int) -> str:
    """"1 of them is" / "3 of them are" — the note is read by a person."""
    return f"{n} of them is" if n == 1 else f"{n} of them are"


def _missing_reason(
    total: int, stale: list[str], gone: list[str], undetermined: list[str],
) -> str:
    """The note's sentence: one clause per cause, each with its own remedy.

    Never one clause for all of them. Telling an operator to re-index files
    that no longer exist at the indexed revision is a dead end, and a note
    that sends them down it is worse than no note: they spend the re-index,
    see the same gap, and stop believing the next one.
    """
    missing = len(stale) + len(gone) + len(undetermined)
    parts = [f"{missing} of {total} changed files have no symbols in the index"]
    if stale:
        parts.append(
            f"{_them(len(stale))} still in the checkout the index was built from "
            f"({_names(stale)}) — the index is stale there, or the extractor could "
            f"not parse it; {HOW_TO_INDEX}"
        )
    if gone:
        parts.append(
            f"{_them(len(gone))} not in that checkout at all ({_names(gone)}) — this "
            "PR's base is older than the indexed revision, so those files were "
            "renamed or deleted before it and no re-index can bring them back; "
            "there is nothing to fix"
        )
    if undetermined:
        parts.append(
            f"the checkout could not be consulted for {len(undetermined)} of them "
            f"({_names(undetermined)}), so whether the index is stale or this PR's "
            "base predates the indexed revision is not known here; indexing the "
            "repository restores the checkout and settles it"
        )
    return "; ".join(parts)


# ─── Rendering ───────────────────────────────────────────────────────


def _render_summary(ctx: GraphContext, *, total_files: int) -> str:
    symbols = ctx.symbols
    reached = [s for s in symbols if s.callers_total or s.cross_repo_callers]
    total_callers = sum(s.callers_total for s in symbols)
    in_files = sum(ctx.symbols_in_files.values())
    lines = [
        f"Blast radius from the code graph — {total_files} changed files, "
        f"{len(symbols)} symbols on the changed lines (of {in_files} in those files), "
        f"{len(reached)} of them reached from elsewhere ({total_callers} callers in "
        f"this repository, {ctx.cross_repo_callers_count} cross-repo references).",
    ]
    if ctx.files_beyond_cap:
        lines.append(
            f"Capped ({MAX_SYMBOLS} changed symbols, {MAX_SYMBOL_ROWS} rows per batch) — "
            "these changed files were NOT fully looked up: "
            + ", ".join(ctx.files_beyond_cap)
        )
    # One line per cause, not one line for the gap: "index may be stale" over
    # a file that no longer exists at the indexed revision is the same false
    # claim the note used to make, made to the model instead of the operator.
    if ctx.files_missing_stale:
        lines.append(
            "In the checkout the index was built from, but with no symbols in the "
            "index — stale, or not parsed by the extractor: "
            + ", ".join(ctx.files_missing_stale)
        )
    if ctx.files_missing_gone:
        lines.append(
            "Not in the index and not in the checkout it was built from — this PR's "
            "base is older than the indexed revision, so these files no longer exist "
            "there and the graph cannot cover them: "
            + ", ".join(ctx.files_missing_gone)
        )
    if ctx.files_missing_undetermined:
        lines.append(
            "Not in the index although they existed before this PR; the checkout was "
            "not available to say whether the index is stale or this PR's base "
            "predates it: " + ", ".join(ctx.files_missing_undetermined)
        )

    # Symbols somebody reaches come first, most-reached first, up to the
    # rendering cap; the unreached remainder is compacted per file so the
    # model still sees what else changed without a line per symbol.
    ranked = sorted(
        reached, key=lambda s: (-(s.callers_total + s.cross_repo_edges), s.file, s.start_line or 0),
    )
    listed = ranked[:SYMBOLS_IN_SUMMARY]
    listed_ids = {s.id for s in listed}
    by_file: dict[str, list[ChangedSymbol]] = {}
    for s in listed:
        by_file.setdefault(s.file, []).append(s)
    for file in ctx.files_queried:
        group = by_file.get(file)
        if not group:
            continue
        lines.append("")
        lines.append(f"### {file}")
        for s in sorted(group, key=lambda s: s.start_line or 0):
            lines.append(_symbol_line(s))
    rest = [s for s in symbols if s.id not in listed_ids]
    untouched = [
        f for f in ctx.files_queried
        if ctx.symbols_in_files.get(f, 0) and not any(s.file == f for s in symbols)
    ]
    if rest or untouched:
        lines.append("")
    for f in untouched:
        lines.append(
            f"- {f}: {ctx.symbols_in_files[f]} symbols in the index, none on the changed lines"
        )
    if rest:
        rest_by_file: dict[str, list[str]] = {}
        for s in rest:
            rest_by_file.setdefault(s.file, []).append(s.name)
        for file, names in rest_by_file.items():
            shown = ", ".join(names[:12]) + (f", +{len(names) - 12} more" if len(names) > 12 else "")
            tag = "not listed above" if any(s.callers_total for s in rest if s.file == file) \
                else "no callers in the index"
            lines.append(f"- {file}: {len(names)} more symbols ({tag}): {shown}")

    if ctx.cross_repo_callers_count:
        lines.append("")
        lines.append("**Cross-repo references to the changed files:**")
        for file, callers in ctx.cross_repo_by_file.items():
            repos: dict[str, int] = {}
            for c in callers:
                repos[c.repo] = repos.get(c.repo, 0) + c.edges
            lines.append(
                f"- {file}: " + ", ".join(f"{repo} ({n} edges)" for repo, n in repos.items())
            )
    if ctx.cross_repo_edges_by_repo:
        lines.append("")
        lines.append(
            "**Other repositories referencing this one (all files):** "
            + ", ".join(f"{r}: {n} edges" for r, n in ctx.cross_repo_edges_by_repo.items())
        )
    return "\n".join(lines)


def _symbol_line(s: ChangedSymbol) -> str:
    where = f"L{s.start_line}" if s.start_line is not None else "?"
    head = f"- `{s.kind}` **{s.name}** ({where})"
    if s.callers_total:
        sample = ", ".join(f"`{c.file}:{c.name}` ({c.edge})" for c in s.callers)
        more = s.callers_total - len(s.callers)
        tail = f" +{more} more" if more > 0 else ""
        head += f" — {s.callers_total} callers: {sample}{tail}"
    else:
        head += " — no callers in this repository"
    if s.cross_repo_callers:
        repos = ", ".join(f"{c.repo} ({c.edges} {c.edge})" for c in s.cross_repo_callers[:5])
        head += f"; cross-repo: {repos}"
    return head


def _render_brief(ctx: GraphContext, *, total_files: int) -> str:
    """Three lines for the agents the full summary would drown."""
    symbols = ctx.symbols
    reached = [s for s in symbols if s.callers_total or s.cross_repo_callers]
    total_callers = sum(s.callers_total for s in symbols)
    lines = [
        f"Blast radius (code graph): {len(symbols)} changed symbols across "
        f"{total_files} files; {len(reached)} reached from elsewhere in this repository "
        f"({total_callers} callers); {ctx.cross_repo_callers_count} cross-repo references."
    ]
    top = sorted(
        reached, key=lambda s: (-(s.callers_total + s.cross_repo_edges), s.file, s.start_line or 0),
    )[:BRIEF_TOP_SYMBOLS]
    if top:
        lines.append(
            "Most depended-on: " + ", ".join(
                f"{s.name} ({s.file}, {s.callers_total} callers"
                + (f", {s.cross_repo_edges} cross-repo" if s.cross_repo_edges else "")
                + ")"
                for s in top
            )
        )
    gaps: list[str] = []
    if ctx.files_beyond_cap:
        gaps.append(f"{len(ctx.files_beyond_cap)} changed files not fully looked up (cap)")
    if ctx.files_missing_stale:
        gaps.append("not in the index (stale): " + _names(ctx.files_missing_stale))
    if ctx.files_missing_gone:
        gaps.append(
            f"{len(ctx.files_missing_gone)} changed files do not exist at the indexed "
            "revision (this PR's base is older)"
        )
    if ctx.files_missing_undetermined:
        gaps.append(
            "not in the index, cause unknown without the checkout: "
            + _names(ctx.files_missing_undetermined)
        )
    if gaps:
        lines.append("Gaps: " + "; ".join(gaps) + ".")
    return "\n".join(lines)


# ─── Helpers ─────────────────────────────────────────────────────────


def _unavailable(files: list[str], *, status: str, reason: str) -> GraphContext:
    """The context of a review that got no graph: empty, and a note that says why."""
    return GraphContext(
        status=status,
        files_queried=[],
        note=ParameterAdjustment(
            agent=None, parameter=PARAM_GRAPH_CONTEXT, requested=GRAPH_REQUESTED,
            sent=None, action=ADJUST_UNAVAILABLE, reason=reason,
        ),
    )


def _default_settings() -> Any:
    from src.config import get_settings
    return get_settings()


def _default_opener() -> Callable[[Path], Any]:
    from src.indexing.graph.graph_store import make_graph_store
    return make_graph_store


def _chunks(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _short(exc: BaseException, limit: int = 160) -> str:
    text = f"{type(exc).__name__}: {exc}".strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ─── Languages this indexer cannot read at all ───────────────────────


def _unparsed_language_files(files: Iterable[str]) -> list[str]:
    """Changed files written in a language with no extractor, in PR order.

    Deliberately not conditioned on the symbol count: a file in a language
    nothing parses has no symbols by construction, and asking the graph first
    would make the answer depend on whether the repository was indexed at all.
    It is also deliberately not conditioned on the file being pre-existing —
    unlike a stale-index gap, this is just as true of a file the PR adds.
    """
    return [f for f in files if PurePosixPath(f).suffix.lower() in UNPARSED_LANGUAGES]


def _languages_of(files: Iterable[str]) -> set[str]:
    return {
        UNPARSED_LANGUAGES[suffix]
        for f in files
        if (suffix := PurePosixPath(f).suffix.lower()) in UNPARSED_LANGUAGES
    }


def _unparsed_reason(total_files: int, unparsed: list[str]) -> str:
    """Why there is no blast radius for these files, and that there is no fix.

    Names the language, because "unsupported" without a name sends the operator
    looking through their own diff to work out which files were meant. Says
    plainly that indexing does not help: the honest remedy for this cause is
    none at all, and offering one anyway is the defect this whole surface
    exists to end.
    """
    languages = ", ".join(sorted(_languages_of(unparsed)))
    shown = _names(unparsed)
    return (
        f"{len(unparsed)} of {total_files} changed files are written in a language "
        f"Celmis has no parser for ({languages}): {shown}. Those files were "
        f"reviewed from the diff alone, with no callers and no blast radius. "
        f"Re-indexing cannot change this — the repository may be perfectly "
        f"indexed and still hold nothing for them."
    )


def _unparsed_clause(unparsed: list[str]) -> str:
    """The same fact, appended to another cause's sentence.

    A pull request can have both a stale index and files in a language nothing
    parses. The action picks the remedy — indexing, which helps the first — so
    the second has to travel in the words, or it is lost.
    """
    if not unparsed:
        return ""
    languages = ", ".join(sorted(_languages_of(unparsed)))
    return (
        f" Separately, {len(unparsed)} changed file(s) are in a language Celmis "
        f"has no parser for ({languages}); indexing will not add those."
    )
