"""Every surface that reads code knows the same languages.

THE DEFECT THIS PINS. "Which languages does Celmis support" had FOUR answers
living in four places, and only one of them was ever updated:

  * the extractor registry — the source of truth, what actually parses;
  * `graph_context.INDEXED_SUFFIXES` — a hand-copied frozenset the review used
    to decide whether a changed file with no symbols was a gap worth reporting;
  * `chunker._LANG_BY_EXT` — twelve suffixes deciding what gets EMBEDDED, so
    Java, C# and C++ produced a full graph and not one vector, and "ask the
    code" could not retrieve a line of them;
  * `Settings.supported_extensions` — what module detection, the Q&A file
    selection and feature-doc generation will even look at.

Adding sixteen languages to the registry moved exactly one of the four. A
repository in any of them would have had a graph, no embeddings, no detected
modules and no documentation — a half-supported language, which is harder to
diagnose than an unsupported one.

Two of the three copies are now derived from the registry. The third cannot be
(it is a `Settings` default, and importing the extractor registry to build
settings would put tree-sitter in the import path of every process that reads a
config), so this file is its guard: it fails the moment somebody adds a
language and forgets the list.
"""

from __future__ import annotations

import pytest

from src.config import Settings
from src.indexing.graph.languages.factory import TAGS_LANGUAGES, supported_suffixes
from src.indexing.vectors.chunker import _GRAMMAR_OVERRIDES, lang_for
from src.review.graph_context import indexed_suffixes

#: Dispatched by filename or content sniff, never by suffix, so the registry
#: cannot name them and `supported_extensions` legitimately holds them anyway.
SNIFFED = {".yml", ".yaml"}


@pytest.fixture(scope="module")
def registry_suffixes() -> frozenset[str]:
    return supported_suffixes()


def test_the_registry_is_not_empty(registry_suffixes):
    """Guards the guard: an empty registry would make every test below pass
    while claiming nothing."""
    assert len(registry_suffixes) > 40


# ─── the review side ─────────────────────────────────────────────────


def test_the_review_knows_every_language_the_indexer_parses(registry_suffixes):
    assert indexed_suffixes() == registry_suffixes


# ─── what gets embedded, and therefore what "ask the code" can find ──


def test_every_parsed_suffix_has_a_grammar_to_chunk_with(registry_suffixes):
    """A suffix the indexer parses but the chunker does not know produces a
    graph with no vectors: findable by `impact`, invisible to a question."""
    missing = sorted(s for s in registry_suffixes if lang_for(f"x{s}") is None)

    assert not missing, f"parsed but never embedded: {missing}"


@pytest.mark.parametrize("suffix,expected", [
    (".rb", "ruby"), (".rs", "rust"), (".kt", "kotlin"), (".swift", "swift"),
    (".ex", "elixir"), (".scala", "scala"), (".lua", "lua"), (".dart", "dart"),
    # The languages that had an extractor for years and were never embedded.
    (".java", "java"), (".cs", "csharp"), (".cpp", "cpp"),
])
def test_the_chunker_names_the_right_grammar(suffix, expected):
    assert lang_for(f"file{suffix}") == expected


def test_the_javascript_family_keeps_its_own_grammars():
    """The one TypeScript extractor walks the whole family because the graph
    does not care about the dialect. A splitter does, and these mappings
    predate the registry — the coarser answer must not overwrite them."""
    assert lang_for("a.ts") == "typescript"
    assert lang_for("a.tsx") == "tsx"
    assert lang_for("a.js") == "javascript"
    assert lang_for("a.jsx") == "javascript"


def test_every_override_is_for_a_suffix_the_registry_actually_serves(registry_suffixes):
    """An override for a suffix nothing parses is dead weight that reads as a
    supported language."""
    stray = sorted(set(_GRAMMAR_OVERRIDES) - registry_suffixes)

    assert not stray, f"overrides for unparsed suffixes: {stray}"


def test_a_grammar_the_pack_cannot_load_is_never_named(registry_suffixes):
    """`CodeSplitter` takes its parsers from `tree_sitter_language_pack`. A
    name the pack does not know means every file of that language falls back to
    line windows — it still gets embedded, but silently worse, and the mapping
    is a bug rather than a fact of life."""
    from tree_sitter_language_pack import get_parser

    unloadable = []
    for suffix in sorted(registry_suffixes):
        language = lang_for(f"x{suffix}")
        try:
            get_parser(language)
        except Exception:  # noqa: BLE001, PERF203
            unloadable.append((suffix, language))

    assert not unloadable, f"named grammars the pack cannot load: {unloadable}"


# ─── what module detection, Q&A and documentation will look at ───────


def test_the_settings_list_covers_every_parsed_language(registry_suffixes):
    """`Settings.supported_extensions` gates `modules.py`, the Q&A retriever's
    file selection (two call sites) and `generation/feature_doc.py`. A language
    missing here is a language those three cannot see at all, however well the
    graph knows it."""
    configured = {e.lower() for e in Settings(gemini_api_key="k").supported_extensions}
    missing = sorted(registry_suffixes - configured)

    assert not missing, (
        "parsed by the indexer but invisible to module detection, Q&A and "
        f"documentation: {missing}"
    )


def test_the_settings_list_claims_nothing_it_cannot_read(registry_suffixes):
    """The other direction: a suffix here that nothing parses makes the Q&A
    retriever open files it can do nothing with."""
    configured = {e.lower() for e in Settings(gemini_api_key="k").supported_extensions}
    unparsed = sorted(configured - registry_suffixes - SNIFFED)

    assert not unparsed, f"claimed but unparsed: {unparsed}"


def test_every_tags_language_reaches_all_three_surfaces():
    """The sixteen added at once, checked end to end rather than by set
    arithmetic — this is the exact failure the file exists for."""
    configured = {e.lower() for e in Settings(gemini_api_key="k").supported_extensions}

    for language, extensions in TAGS_LANGUAGES:
        for suffix in extensions:
            assert suffix in indexed_suffixes(), f"{language}: review cannot see {suffix}"
            assert lang_for(f"x{suffix}"), f"{language}: {suffix} is never embedded"
            assert suffix in configured, f"{language}: {suffix} is invisible to Q&A"
