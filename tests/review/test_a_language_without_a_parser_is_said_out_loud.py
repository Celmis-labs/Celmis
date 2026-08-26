"""A change in a language nothing parses says so, and offers no false fix.

"No symbols for this file" had three causes the note could name — the index is
behind, the file is gone at the indexed revision, the checkout is not here to
tell — and a fourth it could not: there is no extractor for the language. That
one is the only cause no amount of indexing can close.

Measured before it was fixed: the discourse checkout held 8185 `.rb` files, its
graph held 14258 symbols and ZERO of them in a Ruby file, and driving the real
`build_graph_context` with a Ruby pull request against that FULLY INDEXED
repository returned `status=no_symbols`, `note=None`, `summary=""`. The agents
got "(no graph context)" and the run record said nothing — a Ruby pull request
on an indexed repository was indistinguishable from any pull request on a
repository nobody had ever indexed.

Ruby parses now. These hold the general case down:

  * a change in a language with no extractor produces a note that names the
    language and states there is nothing to re-index;
  * it never offers the indexing remedy, which is the defect this whole
    surface exists to end;
  * a documentation-only change produces no note, because nothing is wrong;
  * a language that HAS an extractor produces no such note either;
  * when a stale index and an unparsable language are both true, the note
    keeps the remedy that works and still says the other fact.
"""

from __future__ import annotations

import pytest

from src.review.graph_context import (
    ADJUST_UNSUPPORTED_LANGUAGE,
    PARAM_GRAPH_CONTEXT,
    UNPARSED_LANGUAGES,
    _unparsed_clause,
    _unparsed_language_files,
    _unparsed_reason,
    indexed_suffixes,
)

HASKELL = ["src/Ledger.hs", "src/Billing.hs"]
MIXED = ["src/app.py", "scripts/deploy.sh", "README.md"]


# ─── which files count ───────────────────────────────────────────────


def test_a_language_with_no_extractor_is_picked_out():
    assert _unparsed_language_files(HASKELL) == HASKELL


def test_a_language_with_an_extractor_is_not():
    """Ruby is parsed now — the note must not claim otherwise, or it becomes
    the same false statement in the opposite direction."""
    assert _unparsed_language_files(["app/models/user.rb", "src/main.rs"]) == []


def test_documentation_and_data_are_not_source_nobody_can_read():
    """A README with no symbols is not a gap. Reporting it would train the
    operator to ignore the notice, which costs the real ones their audience."""
    assert _unparsed_language_files(["README.md", "conf.yaml", "data.json"]) == []


def test_only_the_unparsable_files_of_a_mixed_change_are_picked():
    assert _unparsed_language_files(MIXED) == ["scripts/deploy.sh"]


def test_the_two_sets_cannot_overlap():
    """A suffix in both lists would make the product say "we parse this" and
    "we cannot parse this" about one file."""
    assert not (set(UNPARSED_LANGUAGES) & indexed_suffixes())


def test_no_ambiguous_suffix_is_claimed():
    """`.m` is Objective-C and MATLAB; `.v` is Verilog, Coq and V. Naming the
    wrong language sends the operator looking for a parser they do not need,
    so these are left unclaimed on purpose."""
    assert ".m" not in UNPARSED_LANGUAGES
    assert ".v" not in UNPARSED_LANGUAGES


# ─── what the note says ──────────────────────────────────────────────


def test_the_reason_names_the_language():
    reason = _unparsed_reason(5, HASKELL)

    assert "Haskell" in reason, "an unnamed language sends the reader to their own diff"
    assert "2 of 5" in reason


def test_the_reason_refuses_the_indexing_remedy():
    """The whole point: re-indexing runs the same extractors and finds the same
    nothing, so telling the operator to index is a dead end."""
    reason = _unparsed_reason(5, HASKELL).lower()

    assert "re-indexing cannot" in reason
    assert "analyzer generate" not in reason


def test_the_reason_names_the_files():
    assert "src/Ledger.hs" in _unparsed_reason(2, HASKELL)


def test_several_languages_are_all_named():
    reason = _unparsed_reason(4, ["a.hs", "b.sh", "c.sql"])

    for language in ("Haskell", "Shell", "SQL"):
        assert language in reason


# ─── composed with a cause that DOES have a remedy ───────────────────


def test_the_clause_is_empty_when_every_file_is_readable():
    assert _unparsed_clause([]) == ""


def test_the_clause_says_indexing_will_not_add_them():
    """When a stale index and an unparsable language are both true, the action
    keeps the remedy that works — indexing — so this fact has to travel in the
    words or it is lost."""
    clause = _unparsed_clause(HASKELL)

    assert "Haskell" in clause
    assert "indexing will not add those" in clause


# ─── the action, and the remedy it selects ───────────────────────────


def test_the_action_is_its_own_word():
    """It shares neither remedy with the others: `partial` says index it,
    `base_too_old` says the base predates the index. This one says the parser
    is the limit, and a shared word would print the wrong sentence."""
    assert ADJUST_UNSUPPORTED_LANGUAGE == "unsupported_language"
    assert ADJUST_UNSUPPORTED_LANGUAGE != "partial"


def test_the_reviews_page_knows_the_word():
    """`KNOWN_ACTIONS` in the reviews table decides whether the action renders
    as words or as a raw string, and the remedy map decides which sentence the
    row gets. A new action neither knows renders as "unsupported_language" in
    the UI."""
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "web" / "components" / "parameter-adjustments.tsx"
    ).read_text(encoding="utf-8")
    tokens = set(source.replace('"', " ").replace("'", " ").split())

    assert ADJUST_UNSUPPORTED_LANGUAGE in tokens
    assert "graphNoParser" in tokens


@pytest.mark.parametrize("locale", [
    "en", "uk", "de", "es", "fr", "it", "pt", "nl", "pl",
    "cs", "sk", "ro", "tr", "ja", "ko", "zh",
])
def test_every_catalogue_has_words_for_it(locale):
    import json
    from pathlib import Path

    messages = Path(__file__).resolve().parents[2] / "web" / "lib" / "i18n" / "messages"
    catalogue = json.loads((messages / f"{locale}.json").read_text(encoding="utf-8"))

    for key in ("reviews.adjRemedy.graphNoParser",
                f"reviews.adjAction.{ADJUST_UNSUPPORTED_LANGUAGE}"):
        assert catalogue.get(key, "").strip(), f"{locale} has no words for {key}"


@pytest.mark.parametrize("locale", [
    "uk", "de", "es", "fr", "it", "pt", "nl", "pl",
    "cs", "sk", "ro", "tr", "ja", "ko", "zh",
])
def test_no_catalogue_shows_the_english(locale):
    """A key present everywhere but holding the English string is the same
    outage as a missing key, and it passes a completeness test."""
    import json
    from pathlib import Path

    messages = Path(__file__).resolve().parents[2] / "web" / "lib" / "i18n" / "messages"
    english = json.loads((messages / "en.json").read_text(encoding="utf-8"))
    catalogue = json.loads((messages / f"{locale}.json").read_text(encoding="utf-8"))

    key = "reviews.adjRemedy.graphNoParser"
    assert catalogue[key] != english[key], f"{locale} still shows the English sentence"


# ─── end to end, through the real builder ────────────────────────────


def test_a_change_nothing_can_parse_carries_the_note(monkeypatch, tmp_path):
    """Driven through the real `build_graph_context` against a graph that
    answers, so the note is what the pipeline would actually attach."""
    from types import SimpleNamespace

    from src.review.graph_context import build_graph_context
    from src.review.models import Hunk, PullRequest

    graph = tmp_path / "data" / "slug" / "graph.fdblite"
    graph.parent.mkdir(parents=True)
    graph.write_bytes(b"x")
    settings = SimpleNamespace(
        repo_graph_path=lambda slug: graph,
        workspace_dir=tmp_path / "ws",
        repo_path=lambda slug: tmp_path / "repos" / slug,
    )

    class _Store:
        def query(self, cypher, params=None):
            return []

        def close(self):
            pass

    pr = PullRequest(
        provider="github", repo="acme/api", number=7, title="t", description="",
        author="a", base_ref="main", base_sha="s", head_ref="f", head_sha="h",
        state="open",
        hunks=[Hunk(file_path=f, old_file_path=f, old_start=1, old_count=2,
                    new_start=1, new_count=3, content="@@ -1,2 +1,3 @@\n a\n+b\n")
               for f in HASKELL],
    )

    ctx = build_graph_context(pr, settings=settings, open_store=lambda p: _Store())

    assert ctx.files_unparsed_language == HASKELL
    assert ctx.note is not None, "a review with no blast radius said nothing at all"
    assert ctx.note.parameter == PARAM_GRAPH_CONTEXT
    assert ctx.note.action == ADJUST_UNSUPPORTED_LANGUAGE
    assert "Haskell" in ctx.note.reason
