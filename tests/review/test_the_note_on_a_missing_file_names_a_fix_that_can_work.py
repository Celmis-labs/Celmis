"""A changed file the index has no symbols for gets the remedy that can help — or none.

THE DEFECT. `build_graph_context` attached one sentence to every pre-existing
changed file the graph held no symbol for: "the index predates them — re-run
`analyzer generate`". On the benchmark this product was measured against it was
false in the case that actually happens. The clone sits at the default branch's
HEAD; a pull request opened against a 2023 base touches files that were renamed
or deleted since. One such PR had 51 of its 172 changed files missing that way.
The index does not predate those files, it POSTdates them, and no number of
`analyzer generate` runs would ever have added one of them. The product was
sending the operator to a dead end — the same class of defect the adjustment
surface exists to end: "a parameter changed behind the operator's back must be
shown with a remedy attached", and a remedy that cannot work is worse than
none, because the operator spends the re-index, sees the same gap, and stops
believing the next note.

THE DISCRIMINATOR is the checkout the graph was built from,
`settings.repo_path(slug)`. In it and symbol-less → the index is stale (or the
extractor could not parse the file) and re-indexing IS the fix. Not in it →
gone at the indexed revision, and there is nothing to re-index. No checkout at
all → neither cause may be claimed.

What is pinned here, per case: which bucket each file lands in, which sentence
the note carries, which `action` it rides on (the reviews page keys the REMEDY
off it — `partial` links to the Repositories page, `base_too_old` offers no
link at all), and that the model's own summary makes the same distinction
rather than telling it the index "may be stale" about a file that no longer
exists at the indexed revision.

The graph store double and the PR builders are imported from the sibling file
rather than copied: a second double is a second one that can quietly stop
matching the queries the product sends.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.llm.capabilities import adjustment_as_dict
from src.review.graph_context import (
    ADJUST_BASE_TOO_OLD,
    ADJUST_PARTIAL,
    HOW_TO_INDEX,
    PARAM_GRAPH_CONTEXT,
    GraphContext,
)
from src.review.graph_context import (
    build_graph_context as build,
)
from src.review.models import PullRequest, ReviewBatch
from tests.review.test_the_graph_context_covers_every_changed_file import (
    FakeGraph,
    _hunk,
    _pr,
    _sym,
)

SLUG = "github_acme-api"

#: The decisive half-sentences. Each says a CAUSE, and no other clause of the
#: note repeats it, so "this cause was claimed" is exactly "this text is in
#: the reason" — including for the hedge, whose whole point is that it names
#: both possibilities and asserts neither.
SAYS_STALE = "still in the checkout the index was built from"
SAYS_GONE = "not in that checkout at all"
SAYS_UNKNOWN = "is not known here"
#: What the old note told everyone to do. `HOW_TO_INDEX` opens with it.
REINDEX = "analyzer generate"


@pytest.fixture
def workspace(tmp_path: Path):
    """A graph, and — unlike the sibling fixture — a CLONE the note can consult.

    `indexed(graph)` registers the graph file; `working_tree(*paths)` puts
    files in the checkout `settings.repo_path` points at; `clone(...)` makes
    the checkout exist while holding nothing. Nothing creates the checkout
    implicitly, so "there is no clone on this machine" is a state a test can
    ask for by not asking for one.
    """
    stores: dict[Path, object] = {}
    settings = SimpleNamespace(
        workspace_dir=tmp_path,
        repo_graph_path=lambda slug: tmp_path / "data" / slug / "graph.fdblite",
        repo_path=lambda slug: tmp_path / "repos" / slug,
    )

    def indexed(graph: FakeGraph, slug: str = SLUG) -> Path:
        p = settings.repo_graph_path(slug)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
        stores[p] = graph
        return p

    def clone(slug: str = SLUG) -> Path:
        root = settings.repo_path(slug)
        root.mkdir(parents=True, exist_ok=True)
        return root

    def working_tree(*rel: str, content: bytes = b"def f():\n    return 1\n") -> None:
        for r in rel:
            f = clone() / r
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(content)

    def opener(path: Path):
        store = stores.get(Path(path))
        if store is None:
            raise AssertionError(f"opened a graph nobody registered: {path}")
        return store

    def run(pr: PullRequest) -> GraphContext:
        return build(pr, settings=settings, open_store=opener)

    return SimpleNamespace(
        settings=settings, indexed=indexed, clone=clone,
        working_tree=working_tree, run=run,
    )


KNOWN = _sym("src/keep.py::keep", file="src/keep.py")


# ─── gone at the indexed revision: no remedy, and none invented ──────


def test_a_file_gone_at_the_indexed_revision_is_not_sent_for_a_re_index(workspace):
    """The benchmark's case. The file is not in the checkout the graph was
    built from, so the PR's base is older than the index: re-indexing cannot
    add it, and the note must not say it can."""
    workspace.indexed(FakeGraph(symbols=[KNOWN]))
    workspace.working_tree("src/keep.py")  # the clone is here; the other file is not

    ctx = workspace.run(_pr("src/keep.py", "src/renamed_away.py"))

    assert ctx.files_not_indexed == ["src/renamed_away.py"]
    assert ctx.files_missing_gone == ["src/renamed_away.py"]
    assert ctx.files_missing_stale == [] and ctx.files_missing_undetermined == []

    note = ctx.note
    assert note is not None and note.action == ADJUST_BASE_TOO_OLD
    assert REINDEX not in note.reason, "re-indexing cannot bring the file back"
    assert "index-all" not in note.reason
    assert SAYS_STALE not in note.reason and SAYS_UNKNOWN not in note.reason
    assert SAYS_GONE in note.reason
    assert "src/renamed_away.py" in note.reason
    assert "1 of 2 changed files" in note.reason
    assert "base is older than the indexed revision" in note.reason
    assert "there is nothing to fix" in note.reason, (
        "when there is no fix, say so rather than name one that is not"
    )


def test_the_summary_does_not_tell_the_model_the_index_may_be_stale_either(workspace):
    """The same false claim one layer down: the line the architect reads."""
    workspace.indexed(FakeGraph(symbols=[KNOWN]))
    workspace.working_tree("src/keep.py")

    ctx = workspace.run(_pr("src/keep.py", "src/renamed_away.py"))

    assert "stale" not in ctx.summary
    assert "no longer exist" in ctx.summary and "src/renamed_away.py" in ctx.summary
    assert "do not exist at the indexed revision" in ctx.brief
    assert "not in the index (stale)" not in ctx.brief


# ─── still in the checkout: the index really is behind ───────────────


def test_a_file_still_in_the_checkout_keeps_the_re_index_remedy(workspace):
    workspace.indexed(FakeGraph(symbols=[KNOWN]))
    workspace.working_tree("src/keep.py", "src/forgotten.py")

    ctx = workspace.run(_pr("src/keep.py", "src/forgotten.py"))

    assert ctx.files_missing_stale == ["src/forgotten.py"]
    assert ctx.files_missing_gone == [] and ctx.files_missing_undetermined == []

    note = ctx.note
    assert note is not None and note.action == ADJUST_PARTIAL
    assert SAYS_STALE in note.reason and SAYS_GONE not in note.reason
    assert HOW_TO_INDEX in note.reason, "here the operator CAN fix it"
    assert "1 of 2 changed files" in note.reason
    assert "src/forgotten.py" in note.reason
    assert "stale" in ctx.summary and "src/forgotten.py" in ctx.summary


def test_a_comments_only_file_is_still_reported_as_a_gap(workspace):
    """A known false positive, pinned so it cannot be fixed silently: this
    file has no symbols because it has no code, but telling the two apart
    needs the extractor's own parse, which is not run here. It is reported
    as stale — a wrong bucket, not a wrong remedy: re-indexing it is
    harmless, and the operator sees the file named."""
    workspace.indexed(FakeGraph(symbols=[KNOWN]))
    workspace.working_tree("src/keep.py")
    workspace.working_tree("src/notes.py", content=b"# nothing but a comment\n")

    ctx = workspace.run(_pr("src/keep.py", "src/notes.py"))

    assert ctx.files_missing_stale == ["src/notes.py"]


def test_a_dangling_symlink_is_an_entry_that_is_there(workspace):
    """`exists()` follows the link and would call the entry deleted when only
    its target is. A symlink in the checkout is a file at the indexed
    revision, so the cause is the index, not the PR's base."""
    workspace.indexed(FakeGraph(symbols=[KNOWN]))
    workspace.working_tree("src/keep.py")
    os.symlink("nowhere.py", workspace.clone() / "src" / "link.py")

    ctx = workspace.run(_pr("src/keep.py", "src/link.py"))

    assert ctx.files_missing_gone == []
    assert ctx.files_missing_stale == ["src/link.py"]


# ─── a mix: both causes stated, neither drowned ──────────────────────


def test_a_mix_of_both_states_both_and_keeps_the_remedy_that_works(workspace):
    workspace.indexed(FakeGraph(symbols=[KNOWN]))
    workspace.working_tree("src/keep.py", "src/behind.py")

    ctx = workspace.run(_pr("src/keep.py", "src/behind.py", "src/deleted_long_ago.py"))

    assert ctx.files_missing_stale == ["src/behind.py"]
    assert ctx.files_missing_gone == ["src/deleted_long_ago.py"]
    assert ctx.files_not_indexed == ["src/behind.py", "src/deleted_long_ago.py"]

    note = ctx.note
    # `partial`, not `base_too_old`: one of the two IS fixable by indexing,
    # and the row's link must still take the operator there.
    assert note is not None and note.action == ADJUST_PARTIAL
    reason = note.reason
    assert "2 of 3 changed files" in reason
    assert SAYS_STALE in reason and SAYS_GONE in reason
    assert "src/behind.py" in reason and "src/deleted_long_ago.py" in reason
    assert HOW_TO_INDEX in reason
    assert "there is nothing to fix" in reason
    # Each cause names only its own file: a reader must be able to tell which
    # half the re-index is for.
    stale_clause, _, gone_clause = reason.partition(SAYS_GONE)
    assert "src/behind.py" in stale_clause and "src/deleted_long_ago.py" not in stale_clause
    assert "src/deleted_long_ago.py" in gone_clause and "src/behind.py" not in gone_clause
    # And the model gets both lines, not one merged claim.
    assert "stale" in ctx.summary and "no longer exist" in ctx.summary


# ─── no clone: hedge, do not guess ───────────────────────────────────


def test_without_a_clone_neither_cause_is_claimed(workspace):
    """The graph is here and the checkout is not — purged, or a machine that
    only ever had the graph. Silence is not acceptable and a false cause is
    worse, so the note says which two things it could not tell apart."""
    workspace.indexed(FakeGraph(symbols=[KNOWN]))  # no clone created at all

    ctx = workspace.run(_pr("src/keep.py", "src/mystery.py"))

    assert ctx.files_missing_undetermined == ["src/mystery.py"]
    assert ctx.files_missing_stale == [] and ctx.files_missing_gone == []

    note = ctx.note
    assert note is not None
    assert SAYS_UNKNOWN in note.reason
    assert SAYS_STALE not in note.reason and SAYS_GONE not in note.reason
    assert "src/mystery.py" in note.reason
    assert REINDEX not in note.reason, "no confident remedy over an unknown cause"
    # `partial` keeps the Repositories link, and the sentence says what that
    # link is FOR here: indexing brings the checkout back and settles it.
    assert note.action == ADJUST_PARTIAL
    assert "settles it" in note.reason
    # The model's own line hedges too, and names the file.
    assert "the checkout was not available" in ctx.summary
    assert "src/mystery.py" in ctx.summary
    assert "cause unknown without the checkout" in ctx.brief


def test_a_path_that_climbs_out_of_the_checkout_is_not_answered_for(workspace):
    """A diff path with `..` names a file outside the clone; statting it would
    answer about some other file entirely. It is "cannot tell", not "stale" —
    even when something does happen to sit there."""
    workspace.indexed(FakeGraph(symbols=[KNOWN]))
    workspace.working_tree("src/keep.py")
    outside = workspace.clone().parent / "evil.py"
    outside.write_bytes(b"def f():\n    return 1\n")

    ctx = workspace.run(_pr("src/keep.py", "../evil.py"))

    assert ctx.files_missing_undetermined == ["../evil.py"]
    assert ctx.files_missing_stale == []


# ─── nothing missing: nothing said ───────────────────────────────────


def test_a_complete_graph_still_produces_no_note(workspace):
    workspace.indexed(FakeGraph(symbols=[KNOWN, _sym("src/other.py::o", file="src/other.py")]))
    workspace.working_tree("src/keep.py", "src/other.py")

    ctx = workspace.run(_pr("src/keep.py", "src/other.py"))

    assert ctx.status == "ok"
    assert ctx.note is None
    assert ctx.files_not_indexed == []
    assert "stale" not in ctx.summary and "no longer exist" not in ctx.summary
    assert "Gaps:" not in ctx.brief


def test_a_pre_existing_file_with_nothing_in_it_is_not_a_gap(workspace):
    """An empty `__init__.py` has no symbols because it has no code. Calling
    it a hole in the index sends the operator to re-index a repository that
    was indexed correctly — and every package in the diff would do it."""
    workspace.indexed(FakeGraph(symbols=[_sym("src/pkg/mod.py::f", file="src/pkg/mod.py")]))
    workspace.working_tree("src/pkg/mod.py")
    workspace.working_tree("src/pkg/__init__.py", content=b"")
    workspace.working_tree("src/pkg/spaces.py", content=b"\n\n   \n")

    ctx = workspace.run(_pr("src/pkg/mod.py", "src/pkg/__init__.py", "src/pkg/spaces.py"))

    assert ctx.files_not_indexed == []
    assert ctx.note is None
    assert ctx.status == "ok"


def test_a_deleted_file_missing_from_the_checkout_names_the_base_not_the_index(workspace):
    """The PR deletes it and the checkout — already past the merge — no longer
    has it. That is still a blast-radius gap worth saying, and still not one
    an operator can index away."""
    workspace.indexed(FakeGraph(symbols=[]))
    workspace.clone()

    ctx = workspace.run(_pr(hunks=[_hunk("src/legacy_auth.py", deleted=True)]))

    assert ctx.files_missing_gone == ["src/legacy_auth.py"]
    assert ctx.note is not None and ctx.note.action == ADJUST_BASE_TOO_OLD
    assert REINDEX not in ctx.note.reason


# ─── the note still rides the same road ──────────────────────────────


def test_the_new_action_reaches_the_banner_and_the_wire(workspace):
    """`base_too_old` is a new word on an open vocabulary: the batch's banner
    prints it through the same `graph_context` branch, and the wire dict the
    run row stores carries it unchanged. What must NOT survive the trip is the
    advice to re-index."""
    workspace.indexed(FakeGraph(symbols=[KNOWN]))
    workspace.working_tree("src/keep.py")
    pr = _pr("src/keep.py", "src/renamed_away.py")

    ctx = workspace.run(pr)
    batch = ReviewBatch(pull_request=pr)
    batch.parameter_adjustments.append(ctx.note)

    notice = batch.adjustments_notice
    assert "graph context partial (1 of 2 changed files)" in notice
    assert "there is nothing to fix" in notice
    assert REINDEX not in notice
    # `partial_banner` is what the posted PR comment prepends (see
    # `_format_summary`), and it is built from the same list.
    assert "there is nothing to fix" in batch.partial_banner
    assert REINDEX not in batch.partial_banner

    wire = adjustment_as_dict(ctx.note)
    assert wire["parameter"] == PARAM_GRAPH_CONTEXT
    assert wire["action"] == ADJUST_BASE_TOO_OLD
    assert wire["agent"] is None
    assert wire["sent"] == "1 of 2 changed files"
