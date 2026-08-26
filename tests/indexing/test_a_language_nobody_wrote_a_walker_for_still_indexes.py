"""Sixteen more languages index, through the grammars' own tags queries.

THE MEASUREMENT THAT MOTIVATED IT. Celmis had seven hand-written extractors
(Python, the TypeScript family, Go, Java, C#, PHP, C/C++) at 440-640 lines
each. Everything else produced a graph with nothing in it: the discourse
checkout holds 8185 `.rb` files, and its graph held 14258 symbols of which ZERO
were in a Ruby file — every one came from the Ember.js frontend. Ten of the
fifty benchmark pull requests were on that repository, and each got
"(no graph context)", the same thing a repository nobody ever indexed gets.

`tree-sitter-language-pack` ships each grammar's own `tags` query — the
artefact GitHub's code navigation is built on, written by the people who wrote
the grammar. One generic extractor over that gives 16 more languages without
16 more walkers, and without a new dependency.

What this file pins:

  * a real Ruby class parses into its class, its methods and its calls, and
    every call is attributed to the symbol whose body contains it;
  * the same class works for languages with completely different syntax, so
    the generic path is generic and not Ruby-shaped;
  * the registry dispatches the new suffixes here and NOT away from the
    hand-written extractors, which resolve imports this cannot see;
  * `supported_suffixes()` is derived from the registry, so the review side's
    idea of "a language we parse" cannot drift from the indexer's;
  * a file it cannot parse never raises, because one bad file must not fail a
    repository's index.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.indexing.graph.languages.factory import (
    TAGS_LANGUAGES,
    build_default_registry,
    supported_suffixes,
)
from src.indexing.graph.languages.go import GoExtractor
from src.indexing.graph.languages.python import PythonExtractor
from src.indexing.graph.languages.tags import TagsExtractor
from src.indexing.graph.languages.typescript import TypeScriptExtractor

RUBY = b'''
module Billing
  class Invoice < ApplicationRecord
    def total
      line_items.sum(&:amount)
    end

    def overdue?
      due_at < Time.now
    end
  end
end
'''


def _extract(language: str, extensions: tuple[str, ...], name: str, source: bytes):
    return TagsExtractor(language, extensions).extract(Path(name), source=source)


# ─── a real language, really parsed ──────────────────────────────────


def test_ruby_yields_its_class_its_methods_and_its_calls():
    result = _extract("ruby", (".rb",), "app/models/invoice.rb", RUBY)

    assert not result.parse_errors
    kinds = {s.name: s.kind for s in result.symbols}
    assert kinds.get("Invoice") == "class"
    assert kinds.get("Billing") == "module"
    assert kinds.get("total") == "method"
    assert kinds.get("overdue?") == "method"

    called = {e.raw_target for e in result.edges if e.kind == "CALLS"}
    assert "sum" in called, "the body's calls are the blast radius; without them the graph is a name list"


def test_a_call_is_attributed_to_the_method_that_makes_it():
    """A call on line 5 of a class with two methods belongs to the method whose
    body contains it — not to the file, and not to the class. That attribution
    is the whole reason the graph can answer "who reaches this"."""
    result = _extract("ruby", (".rb",), "app/models/invoice.rb", RUBY)

    by_id = {s.id: s for s in result.symbols}
    callers = {
        by_id[e.from_id].name
        for e in result.edges
        if e.kind == "CALLS" and e.from_id in by_id
    }
    assert "total" in callers


def test_every_symbol_is_contained_by_its_file():
    result = _extract("ruby", (".rb",), "app/models/invoice.rb", RUBY)

    file_symbols = [s for s in result.symbols if s.kind == "file_module"]
    assert len(file_symbols) == 1
    file_id = file_symbols[0].id
    contained = {e.from_id for e in result.edges if e.kind == "DEFINED_IN" and e.to_id == file_id}
    assert contained == {s.id for s in result.symbols if s.id != file_id}


def test_the_file_path_is_relative_to_the_repository_root(tmp_path):
    """A symbol's `file` is the key every later lookup joins on; an absolute
    path here makes the graph unjoinable with a PR's changed-file list."""
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    target = root / "app" / "invoice.rb"
    target.write_bytes(RUBY)

    result = TagsExtractor("ruby", (".rb",), repo_root=root).extract(target)

    assert all(s.file == "app/invoice.rb" for s in result.symbols)


# ─── generic, not Ruby-shaped ────────────────────────────────────────


OTHER_LANGUAGES = [
    ("rust", (".rs",), "src/lib.rs",
     b"pub struct Ledger { pub id: u32 }\n"
     b"impl Ledger {\n    pub fn total(&self) -> u32 { self.id }\n}\n",
     "total"),
    ("kotlin", (".kt",), "Main.kt",
     b"class Ledger {\n    fun total(): Int { return 1 }\n}\n", "total"),
    ("lua", (".lua",), "init.lua",
     b"local function total(a, b)\n  return a + b\nend\n", "total"),
    ("elixir", (".ex",), "lib/ledger.ex",
     b"defmodule Ledger do\n  def total(a), do: a\nend\n", "Ledger"),
]


@pytest.mark.parametrize("language,exts,name,source,expected", OTHER_LANGUAGES)
def test_other_languages_yield_their_definitions(language, exts, name, source, expected):
    result = _extract(language, exts, name, source)

    assert not result.parse_errors
    names = {s.name for s in result.symbols}
    assert expected in names, f"{language}: got {sorted(names)}"


# ─── the registry keeps the hand-written extractors ──────────────────


@pytest.mark.parametrize("filename,expected", [
    ("app/models/user.rb", TagsExtractor),
    ("src/main.rs", TagsExtractor),
    ("Main.kt", TagsExtractor),
    ("view.swift", TagsExtractor),
    # The seven that have a real walker keep it: it resolves imports and module
    # paths a tags query cannot see, so losing it would be a downgrade.
    ("src/app.py", PythonExtractor),
    ("src/app.ts", TypeScriptExtractor),
    ("main.go", GoExtractor),
])
def test_the_registry_dispatches_to_the_right_extractor(filename, expected):
    match = build_default_registry().match(Path(filename))

    assert isinstance(match, expected), f"{filename} went to {type(match).__name__}"


def test_no_tags_language_shadows_a_hand_written_one():
    """The tags table must not claim a suffix a real walker owns — the walker
    would still win on priority, but the overlap would be a trap for whoever
    next changes a priority."""
    hand_written = set()
    for extractor in build_default_registry().extractors():
        if not isinstance(extractor, TagsExtractor):
            hand_written.update(e.lower() for e in getattr(extractor, "extensions", ()) or ())

    for language, extensions in TAGS_LANGUAGES:
        clash = {e.lower() for e in extensions} & hand_written
        assert not clash, f"{language} claims {clash}, already owned by a hand-written extractor"


# ─── the review side asks the indexer, and gets the truth ────────────


def test_supported_suffixes_is_derived_from_the_registry():
    suffixes = supported_suffixes()

    # The languages that were always there.
    assert {".py", ".ts", ".go", ".java"} <= suffixes
    # And the ones the tags table added — the point of the whole change.
    assert {".rb", ".rs", ".kt", ".swift", ".ex", ".scala"} <= suffixes


def test_every_declared_tags_language_reaches_the_supported_set():
    """A language in the table but not in the set means the registration is not
    happening — the exact drift a hand-copied list used to hide."""
    declared = {e.lower() for _lang, exts in TAGS_LANGUAGES for e in exts}

    assert declared <= supported_suffixes()


def test_the_review_side_reads_the_same_answer():
    """`graph_context.indexed_suffixes()` is what decides whether a changed file
    with no symbols is a gap worth reporting. It used to be a hand-copied
    frozenset that knew nothing of any language added afterwards."""
    from src.review.graph_context import indexed_suffixes

    assert indexed_suffixes() == supported_suffixes()
    assert ".rb" in indexed_suffixes()


# ─── nothing it meets can fail an index ──────────────────────────────


def test_a_file_that_is_not_the_language_it_claims_does_not_raise():
    result = _extract("ruby", (".rb",), "broken.rb", b"\x00\xff not ruby at all {{{")

    assert isinstance(result.symbols, list)


def test_an_empty_file_yields_only_itself():
    result = _extract("ruby", (".rb",), "empty.rb", b"")

    assert [s.kind for s in result.symbols] == ["file_module"]
    assert result.edges == []


def test_an_unreadable_file_is_reported_not_raised(tmp_path):
    result = TagsExtractor("ruby", (".rb",)).extract(tmp_path / "gone.rb")

    assert result.symbols == []
    assert any("read_failed" in e for e in result.parse_errors)


def test_a_language_the_pack_does_not_know_is_reported_not_raised():
    result = _extract("nosuchlanguage", (".zzz",), "x.zzz", b"anything")

    assert result.symbols == []
    assert result.parse_errors
