"""The graph context covers every changed file, resolves callers, and says when it cannot.

What the orchestrator's `_build_graph_context` did before it moved to
`src/review/graph_context.py`, and what these tests pin instead:

  * `changed_files[:5]` — a seven-file PR had two files never looked up, and
    no line anywhere said which. Now every file is queried, in batches, and
    the files a cap leaves out are named.
  * no callers at all — the "blast radius" was the changed symbols' names.
    Now each symbol carries who reaches it in this repository (exact count,
    capped sample) and which other repositories reference its file.
  * "(repo not indexed — ...)" handed to the model as context, logged at
    DEBUG, recorded nowhere. The benchmark this was measured on ran 161
    reviews with no graph for any of them and nothing to show for it. Now the
    absence is a `ParameterAdjustment` on the run — the road the dropped
    temperature takes to the PR banner and the run row — and the summary is
    empty, so the prompt prints its own "(no graph context)".
  * a cross-repo count that was the repository's total, the same for every PR.
    Now it counts the edges into the CHANGED files.
  * every symbol of a touched file was a "changed symbol", and `ORDER BY file
    LIMIT 200` against a real index let one 800-constant file ahead in the
    alphabet eat the rows of five files behind it, which then read as "not
    indexed". Now a symbol is changed when it overlaps the PR's old-side
    lines, and "not indexed" comes from a per-file count no cap can cut.

The graph is a fake that dispatches on the parameters a query binds — the
file list, the id list, the single id, the slug — never on the query text, so
the Cypher can change shape without these tests noticing, and a query that
binds nothing these tests know is an error, not a silent empty result.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.llm.capabilities import adjustment_as_dict
from src.review import graph_context as gc
from src.review.graph_context import (
    ADJUST_PARTIAL,
    ADJUST_UNAVAILABLE,
    CALLER_EDGE_KINDS,
    MAX_CALLERS_PER_SYMBOL,
    PARAM_GRAPH_CONTEXT,
    GraphContext,
    build_graph_context,
)
from src.review.models import Hunk, PullRequest, ReviewBatch

# ─── doubles ─────────────────────────────────────────────────────────


def _hunk(
    path: str, *, new: bool = False, deleted: bool = False,
    old_start: int = 1, old_count: int = 200, old_path: str | None = None,
) -> Hunk:
    """A hunk over old lines old_start..old_start+old_count-1 — wide by default,
    so a test about something else does not have to think about lines."""
    return Hunk(
        file_path=path, old_file_path=old_path or path,
        old_start=0 if new else old_start, old_count=0 if new else old_count,
        new_start=1, new_count=0 if deleted else 2,
        content="@@ -1 +1,2 @@\n line\n+added\n",
        is_new_file=new, is_deleted_file=deleted, is_renamed=old_path is not None,
    )


def _pr(*paths: str, provider: str = "github", repo: str = "acme/api", hunks=()) -> PullRequest:
    return PullRequest(
        provider=provider, repo=repo, number=7,
        title="t", description="d", author="alice",
        base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
        state="open",
        hunks=list(hunks) or [_hunk(p) for p in paths],
    )


def _sym(sid: str, *, file: str, kind: str = "function", line: int | None = 1,
         end: int | None = None) -> dict:
    return {"id": sid, "name": sid.rsplit("::", 1)[-1], "kind": kind, "file": file,
            "start_line": line, "end_line": end if end is not None or line is None else line + 4}


class FakeGraph:
    """A graph store double.

    `symbols` are the per-repo Symbol rows; `edges` are (from_id, to_id, kind)
    between them; `cross` are the group graph's rows as
    (source_repo, source_name, target_module, target_file, kind). One double
    serves as both kinds of store — which queries it answers depends on what
    the caller binds.
    """

    def __init__(self, symbols=(), edges=(), cross=(), fail: BaseException | None = None):
        self.symbols = list(symbols)
        self.edges = list(edges)
        self.cross = list(cross)
        self.fail = fail
        self.calls: list[dict] = []
        self.closed = False

    def query(self, cypher: str, params: dict | None = None) -> list[dict]:
        params = dict(params or {})
        self.calls.append(params)
        if self.fail is not None:
            raise self.fail
        if "slug" in params and "files" in params:
            counts: dict[tuple, int] = {}
            for src_repo, name, module, file, kind in self.cross:
                if module == params["slug"] and file in params["files"]:
                    counts[(file, src_repo, name, kind)] = counts.get((file, src_repo, name, kind), 0) + 1
            return [
                {"file": f, "source_repo": r, "name": n, "edge": k, "edges": c}
                for (f, r, n, k), c in counts.items()
            ][: params["cap"]]
        if "slug" in params:
            totals: dict[str, int] = {}
            for src_repo, _name, module, _file, _kind in self.cross:
                if module == params["slug"]:
                    totals[src_repo] = totals.get(src_repo, 0) + 1
            return [{"source_repo": r, "edges": n} for r, n in totals.items()]
        if "files" in params:
            rows = [dict(s) for s in self.symbols
                    if s["file"] in params["files"] and s["kind"] != "file_module"]
            if "cap" not in params:  # the per-file aggregate
                counts: dict[str, int] = {}
                for r in rows:
                    counts[r["file"]] = counts.get(r["file"], 0) + 1
                return [{"file": f, "n": n} for f, n in counts.items()]
            rows.sort(key=lambda s: (s["file"], s["start_line"] or 0))
            return rows[: params["cap"]]
        if "ids" in params:
            counts = {}
            for _f, t, kind in self.edges:
                if t in params["ids"] and kind in CALLER_EDGE_KINDS:
                    counts[t] = counts.get(t, 0) + 1
            return [{"target": t, "callers": n} for t, n in counts.items()]
        if "id" in params:
            by_id = {s["id"]: s for s in self.symbols}
            rows = []
            for f, t, kind in self.edges:
                if t == params["id"] and kind in CALLER_EDGE_KINDS:
                    c = by_id.get(f, {"name": f, "kind": "?", "file": "?", "start_line": None})
                    rows.append({"name": c["name"], "kind": c["kind"], "file": c["file"],
                                 "start_line": c["start_line"], "edge": kind})
            return rows[: params["cap"]]
        raise AssertionError(f"the fake does not know this query: {cypher!r} {params!r}")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def workspace(tmp_path: Path):
    """A settings double plus a way to register graphs under it.

    `indexed(slug, graph)` creates the graph file the product checks for and
    maps it to a FakeGraph; `group(name, graph)` does the same for a group's
    cross-repo file. `opener` is what `build_graph_context` is handed.
    """
    stores: dict[Path, FakeGraph] = {}
    settings = SimpleNamespace(
        workspace_dir=tmp_path,
        repo_graph_path=lambda slug: tmp_path / "data" / slug / "graph.fdblite",
    )

    def indexed(slug: str, graph: FakeGraph) -> Path:
        p = settings.repo_graph_path(slug)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        stores[p] = graph
        return p

    def group(name: str, graph: FakeGraph) -> Path:
        """A group's cross-repo file — WITH the group it belongs to.

        This used to drop a bare `.fdblite` into `groups/`, which the reader
        found by globbing the directory. That glob read every tenant's edges
        into every review, so the reader now asks the group manager where its
        own tenant's graphs are — and a graph file with no group behind it is
        not something production can produce.
        """
        from src.config import Settings
        from src.groups.manager import GroupManager

        mgr = GroupManager(settings=Settings(workspace_dir=tmp_path))
        g = mgr.create(name)
        mgr.save(g)
        p = mgr.graph_path(g)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        stores[p] = graph
        return p

    def opener(path: Path):
        store = stores.get(Path(path))
        if store is None:
            raise AssertionError(f"opened a graph nobody registered: {path}")
        if isinstance(store, BaseException):
            raise store
        return store

    return SimpleNamespace(settings=settings, indexed=indexed, group=group,
                           opener=opener, stores=stores)


def _build(pr: PullRequest, ws) -> GraphContext:
    return build_graph_context(pr, settings=ws.settings, open_store=ws.opener)


# ─── every changed file is looked up ────────────────────────────────


SEVEN = [f"src/m{i}.py" for i in range(7)]


def test_every_changed_file_is_queried_in_batches(workspace, monkeypatch):
    """Seven files, batches of three: three queries, every file in exactly one.

    The old code asked about the first five; with it restored, m5 and m6
    never reach a query and their symbols are missing below.
    """
    monkeypatch.setattr(gc, "FILES_PER_QUERY", 3)
    graph = FakeGraph(symbols=[_sym(f"{f}::fn", file=f) for f in SEVEN])
    workspace.indexed("github_acme-api", graph)

    ctx = _build(_pr(*SEVEN), workspace)

    counts = [c["files"] for c in graph.calls if "files" in c and "slug" not in c and "cap" not in c]
    details = [c["files"] for c in graph.calls if "files" in c and "slug" not in c and "cap" in c]
    assert len(counts) == 3 and len(details) == 3, "one count and one detail query per batch"
    assert all(len(batch) <= 3 for batch in counts + details)
    assert sorted(f for batch in details for f in batch) == sorted(SEVEN)
    assert sorted(f for batch in counts for f in batch) == sorted(SEVEN)
    assert ctx.files_queried == SEVEN
    assert {s.file for s in ctx.symbols} == set(SEVEN)
    assert ctx.status == "ok" and ctx.note is None
    assert graph.closed


def test_the_symbol_cap_names_the_files_it_left_out(workspace, monkeypatch):
    """The cap cuts whole files, and the summary says which ones it never asked about."""
    monkeypatch.setattr(gc, "FILES_PER_QUERY", 2)
    monkeypatch.setattr(gc, "MAX_SYMBOLS", 4)
    files = ["a.py", "b.py", "c.py", "d.py"]
    graph = FakeGraph(symbols=[
        _sym(f"{f}::{n}", file=f, line=i) for f in files for i, n in enumerate(("x", "y"), 1)
    ])
    workspace.indexed("github_acme-api", graph)

    ctx = _build(_pr(*files), workspace)

    assert len(ctx.symbols) == 4
    assert ctx.truncated
    assert ctx.files_beyond_cap == ["c.py", "d.py"]
    assert "c.py, d.py" in ctx.summary and "NOT fully looked up" in ctx.summary
    assert "not fully looked up" in ctx.brief
    # The files beyond the cap are not "missing from the index" — they were
    # never asked about, which is a different sentence.
    assert ctx.files_not_indexed == []


def test_only_symbols_on_the_changed_lines_are_changed_symbols(workspace):
    """Four symbols in the file, a hunk over old lines 12..21: the two it
    overlaps are the change; the other two are counted, not listed."""
    graph = FakeGraph(symbols=[
        _sym("src/a.py::one", file="src/a.py", line=1, end=5),
        _sym("src/a.py::two", file="src/a.py", line=10, end=15),
        _sym("src/a.py::three", file="src/a.py", line=20, end=25),
        _sym("src/a.py::four", file="src/a.py", line=30, end=35),
    ])
    workspace.indexed("github_acme-api", graph)

    ctx = _build(_pr(hunks=[_hunk("src/a.py", old_start=12, old_count=10)]), workspace)

    assert [s.name for s in ctx.symbols] == ["two", "three"]
    assert ctx.symbols_in_files == {"src/a.py": 4}
    assert "2 symbols on the changed lines (of 4 in those files)" in ctx.summary
    assert ctx.status == "ok" and ctx.note is None


def test_a_symbol_without_a_line_is_kept_rather_than_hidden(workspace):
    graph = FakeGraph(symbols=[_sym("src/a.py::ghost", file="src/a.py", line=None)])
    workspace.indexed("github_acme-api", graph)

    ctx = _build(_pr(hunks=[_hunk("src/a.py", old_start=50, old_count=1)]), workspace)

    assert [s.name for s in ctx.symbols] == ["ghost"]


def test_a_huge_file_does_not_turn_its_neighbours_into_gaps(workspace, monkeypatch):
    """The defect the real index exposed: rows ordered by file and capped, so
    a.py's five symbols fill a cap of three and b.py gets no row at all. b.py
    is in the index — its count says so — and must not be called missing."""
    monkeypatch.setattr(gc, "MAX_SYMBOL_ROWS", 3)
    graph = FakeGraph(
        symbols=[_sym(f"a.py::s{i}", file="a.py", line=i) for i in range(1, 6)]
        + [_sym("b.py::only", file="b.py", line=1)],
    )
    workspace.indexed("github_acme-api", graph)

    ctx = _build(_pr("a.py", "b.py"), workspace)

    assert ctx.files_not_indexed == []
    assert ctx.note is None
    assert ctx.symbols_in_files == {"a.py": 5, "b.py": 1}
    assert ctx.files_beyond_cap == ["a.py", "b.py"]
    assert "NOT fully looked up: a.py, b.py" in ctx.summary


def test_a_renamed_file_is_looked_up_under_its_old_path(workspace):
    """The index was built from the base branch, where the file still had its
    old name. Asking for the new one found nothing and called it missing."""
    graph = FakeGraph(symbols=[_sym("src/old_name.py::f", file="src/old_name.py")])
    workspace.indexed("github_acme-api", graph)

    ctx = _build(
        _pr(hunks=[_hunk("src/new_name.py", old_path="src/old_name.py")]), workspace,
    )

    assert [s.name for s in ctx.symbols] == ["f"]
    assert ctx.files_queried == ["src/old_name.py"]
    assert ctx.files_not_indexed == [] and ctx.note is None


# ─── callers resolve per symbol ─────────────────────────────────────


def test_callers_resolve_per_symbol(workspace):
    graph = FakeGraph(
        symbols=[
            _sym("src/auth.py::refresh", file="src/auth.py", line=10),
            _sym("src/auth.py::Session", file="src/auth.py", kind="class", line=1),
            _sym("src/api.py::login", file="src/api.py", line=5),
            _sym("src/api.py::logout", file="src/api.py", line=20),
            _sym("src/auth.py::__module__", file="src/auth.py", kind="file_module"),
        ],
        edges=[
            ("src/api.py::login", "src/auth.py::refresh", "CALLS"),
            ("src/api.py::logout", "src/auth.py::refresh", "CALLS"),
            ("src/auth.py::refresh", "src/auth.py::Session", "DEFINED_IN"),
        ],
    )
    workspace.indexed("github_acme-api", graph)

    ctx = _build(_pr("src/auth.py"), workspace)

    by_name = {s.name: s for s in ctx.symbols}
    assert set(by_name) == {"refresh", "Session"}, "the file_module pseudo-symbol is not a change"
    refresh = by_name["refresh"]
    assert refresh.callers_total == 2
    assert sorted((c.file, c.name, c.edge) for c in refresh.callers) == [
        ("src/api.py", "login", "CALLS"), ("src/api.py", "logout", "CALLS"),
    ]
    assert by_name["Session"].callers_total == 0, "containment is not a call"
    assert "**refresh**" in ctx.summary and "src/api.py:login" in ctx.summary
    assert "2 callers" in ctx.summary


def test_the_caller_count_is_exact_when_the_sample_is_capped(workspace):
    callers = [f"src/c{i}.py::use{i}" for i in range(MAX_CALLERS_PER_SYMBOL + 4)]
    graph = FakeGraph(
        symbols=[_sym("src/hot.py::hot", file="src/hot.py")]
        + [_sym(c, file=c.split("::")[0]) for c in callers],
        edges=[(c, "src/hot.py::hot", "CALLS") for c in callers],
    )
    workspace.indexed("github_acme-api", graph)

    ctx = _build(_pr("src/hot.py"), workspace)

    hot = ctx.symbols[0]
    assert hot.callers_total == MAX_CALLERS_PER_SYMBOL + 4
    assert len(hot.callers) == MAX_CALLERS_PER_SYMBOL
    assert f"{MAX_CALLERS_PER_SYMBOL + 4} callers" in ctx.summary and "+4 more" in ctx.summary


# ─── when the graph is missing, the run says so ─────────────────────


def test_a_repo_that_is_not_indexed_yields_the_note_and_an_empty_summary(workspace):
    """No graph file: no open attempted, nothing raised, and a note that names the fix."""
    ctx = _build(_pr("src/auth.py"), workspace)

    assert ctx.status == "not_indexed"
    assert ctx.summary == "" and ctx.brief == ""
    assert ctx.symbols == [] and ctx.cross_repo_callers_count == 0
    assert ctx.note is not None
    assert ctx.note.parameter == PARAM_GRAPH_CONTEXT
    assert ctx.note.action == ADJUST_UNAVAILABLE
    assert "not indexed" in ctx.note.reason
    assert "analyzer generate" in ctx.note.reason
    assert workspace.stores == {}, "nothing was opened for a graph that does not exist"


def test_a_graph_that_cannot_be_opened_is_a_note_not_an_exception(workspace):
    p = workspace.indexed("github_acme-api", FakeGraph())
    workspace.stores[p] = RuntimeError("falkordb: incompatible RDB version")

    ctx = _build(_pr("src/auth.py"), workspace)

    assert ctx.status == "open_failed"
    assert ctx.note is not None and ctx.note.action == ADJUST_UNAVAILABLE
    assert "could not be opened" in ctx.note.reason
    assert "incompatible RDB version" in ctx.note.reason
    assert ctx.summary == ""


def test_a_failing_query_is_a_note_and_the_store_is_still_closed(workspace):
    graph = FakeGraph(fail=RuntimeError("syntax error near IN"))
    workspace.indexed("github_acme-api", graph)

    ctx = _build(_pr("src/auth.py"), workspace)

    assert ctx.status == "query_failed"
    assert ctx.note is not None and ctx.note.action == ADJUST_UNAVAILABLE
    assert "syntax error near IN" in ctx.note.reason
    assert ctx.summary == ""
    assert graph.closed


def test_files_the_index_never_saw_are_named_but_new_and_non_code_files_are_not(workspace):
    """A pre-existing .py with no symbol is a gap. A file this PR adds, a README and a
    YAML are not: the index could not and should not have them."""
    graph = FakeGraph(symbols=[_sym("src/known.py::k", file="src/known.py")])
    workspace.indexed("github_acme-api", graph)
    pr = _pr(hunks=[
        _hunk("src/known.py"),
        _hunk("src/forgotten.py"),
        _hunk("src/brand_new.py", new=True),
        _hunk("README.md"),
        _hunk("deploy/values.yaml"),
    ])

    ctx = _build(pr, workspace)

    assert ctx.status == "partial"
    assert ctx.files_not_indexed == ["src/forgotten.py"]
    assert ctx.note is not None
    assert ctx.note.action == ADJUST_PARTIAL
    assert "src/forgotten.py" in ctx.note.reason
    assert "brand_new" not in ctx.note.reason and "README" not in ctx.note.reason
    assert "1 of 5 changed files" in ctx.note.reason
    # The graph still answered for the file it knew — a partial radius is
    # handed over, not withheld.
    assert [s.name for s in ctx.symbols] == ["k"]
    assert "src/forgotten.py" in ctx.summary


def test_a_deleted_file_still_counts_as_one_the_index_should_know(workspace):
    """Deleting a file is the change whose callers matter most; its absence is a gap."""
    graph = FakeGraph(symbols=[])
    workspace.indexed("github_acme-api", graph)

    ctx = _build(_pr(hunks=[_hunk("src/legacy_auth.py", deleted=True)]), workspace)

    assert ctx.files_not_indexed == ["src/legacy_auth.py"]
    assert ctx.note is not None and ctx.note.action == ADJUST_PARTIAL


def test_a_docs_only_pr_on_an_indexed_repo_is_not_a_gap(workspace):
    workspace.indexed("github_acme-api", FakeGraph())

    ctx = _build(_pr("README.md", "docs/guide.md"), workspace)

    assert ctx.status == "no_symbols"
    assert ctx.note is None
    assert ctx.summary == "" and ctx.brief == ""


def test_a_pr_with_no_changed_files_has_no_note(workspace):
    workspace.indexed("github_acme-api", FakeGraph())

    ctx = _build(_pr(hunks=[]), workspace)

    assert ctx.status == "no_changed_files"
    assert ctx.note is None and ctx.summary == ""


# ─── cross-repo callers ─────────────────────────────────────────────


def test_cross_repo_callers_are_counted_against_the_changed_files(workspace):
    """Two edges from `billing` into the changed file, five into a file this PR
    does not touch: the PR's radius is 2, the repository's total is 7, and the
    summary shows both as what they are."""
    repo = FakeGraph(symbols=[
        _sym("src/auth/session.py::refresh", file="src/auth/session.py", line=3),
        _sym("src/auth/session.py::revoke", file="src/auth/session.py", line=30),
    ])
    workspace.indexed("github_acme-api", repo)
    group = FakeGraph(cross=[
        ("billing", "pay.py::charge", "github_acme-api", "src/auth/session.py", "REFERENCES_REPO"),
        ("billing", "pay.py::refund", "github_acme-api", "src/auth/session.py", "REFERENCES_REPO"),
    ] + [
        ("billing", f"x{i}", "github_acme-api", "src/other.py", "REFERENCES_REPO") for i in range(5)
    ] + [
        ("billing", "y", "somebody_else", "src/auth/session.py", "REFERENCES_REPO"),
    ])
    workspace.group("platform", group)

    ctx = _build(_pr("src/auth/session.py"), workspace)

    assert ctx.cross_repo_callers_count == 2
    assert ctx.cross_repo_edges_by_repo == {"billing": 7}
    for s in ctx.symbols:
        assert {c.repo for c in s.cross_repo_callers} == {"billing"}
        assert s.cross_repo_edges == 2
    assert "Cross-repo references to the changed files" in ctx.summary
    assert "billing (2 edges)" in ctx.summary
    assert "billing: 7 edges" in ctx.summary
    assert "2 cross-repo references" in ctx.brief
    assert group.closed


def test_a_broken_group_graph_does_not_lose_the_repo_graph(workspace):
    repo = FakeGraph(symbols=[_sym("src/a.py::f", file="src/a.py")])
    workspace.indexed("github_acme-api", repo)
    p = workspace.group("platform", FakeGraph())
    workspace.stores[p] = RuntimeError("group index is being rebuilt")

    ctx = _build(_pr("src/a.py"), workspace)

    assert ctx.status == "ok"
    assert [s.name for s in ctx.symbols] == ["f"]
    assert ctx.cross_repo_callers_count == 0
    assert ctx.note is None


# ─── the two renderings ─────────────────────────────────────────────


def test_the_brief_is_shorter_and_names_the_most_depended_on(workspace):
    files = [f"src/f{i}.py" for i in range(6)]
    symbols = [_sym(f"{f}::s{j}", file=f, line=j) for f in files for j in range(1, 6)]
    callers = [_sym(f"src/c{i}.py::c{i}", file=f"src/c{i}.py") for i in range(9)]
    edges = [(c["id"], "src/f2.py::s3", "CALLS") for c in callers]  # the hot one
    edges += [(callers[0]["id"], "src/f4.py::s1", "IMPORTS")]
    workspace.indexed("github_acme-api", FakeGraph(symbols=symbols + callers, edges=edges))

    ctx = _build(_pr(*files), workspace)

    assert len(ctx.brief) < len(ctx.summary) / 3
    assert ctx.brief.count("\n") <= 2
    assert ctx.brief.startswith("Blast radius (code graph): 30 changed symbols across 6 files")
    assert "Most depended-on: s3 (src/f2.py, 9 callers), s1 (src/f4.py, 1 callers)" in ctx.brief
    # The full rendering lists the reached symbols with their callers and
    # compacts the rest per file rather than dropping them.
    assert "### src/f2.py" in ctx.summary and "9 callers" in ctx.summary
    assert "src/f0.py: 5 more symbols (no callers in the index)" in ctx.summary


# ─── the note takes the adjustment road ─────────────────────────────


def test_the_note_reaches_the_banner_and_the_run_record_shape(workspace):
    """Appended to `parameter_adjustments`, the note is printed by the same
    banner and serialized by the same `as_dict` as a dropped temperature —
    without a new column, a new schema field or a new renderer."""
    ctx = _build(_pr("src/auth.py"), workspace)
    batch = ReviewBatch(pull_request=_pr("src/auth.py"))
    batch.parameter_adjustments.append(ctx.note)

    notice = batch.adjustments_notice
    assert "graph" in notice.lower()
    assert "not indexed" in notice and "analyzer generate" in notice

    wire = adjustment_as_dict(ctx.note)
    assert wire["parameter"] == PARAM_GRAPH_CONTEXT
    assert wire["action"] == ADJUST_UNAVAILABLE
    assert wire["agent"] is None


# ─── the graph is found under the slug it was written with ──────────


def test_a_bitbucket_graph_is_found_under_the_legacy_slug(workspace):
    """`ParsedRepo.slug` drops the provider prefix for Bitbucket; the review's
    `repo_slug` keeps it. The graph used to be invisible to every Bitbucket PR."""
    graph = FakeGraph(symbols=[_sym("src/a.py::f", file="src/a.py")])
    workspace.indexed("acme-api", graph)

    ctx = _build(_pr("src/a.py", provider="bitbucket"), workspace)

    assert ctx.status == "ok"
    assert [s.name for s in ctx.symbols] == ["f"]


def test_a_github_graph_is_not_looked_for_under_another_providers_name(workspace):
    """The fallback is Bitbucket's alone: a GitHub PR must not read a graph
    written for a same-named repository on another provider."""
    workspace.indexed("acme-api", FakeGraph(symbols=[_sym("src/a.py::f", file="src/a.py")]))

    ctx = _build(_pr("src/a.py", provider="github"), workspace)

    assert ctx.status == "not_indexed"


# ─── the Cypher literal is whitelisted ──────────────────────────────


def test_caller_edge_kinds_are_whitelisted_and_exclude_containment():
    """The kinds are embedded in Cypher as a literal; every one must pass the
    store's injection whitelist. DEFINED_IN is containment, not reach."""
    from src.indexing.graph.graph_store import ALLOWED_EDGE_KINDS

    assert set(CALLER_EDGE_KINDS) <= ALLOWED_EDGE_KINDS
    assert "DEFINED_IN" not in CALLER_EDGE_KINDS
