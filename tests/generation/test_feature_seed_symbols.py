"""Feature notes were generated without any source code at all.

Every one of the 12 feature notes for a production repository carried

    Примітка: Оскільки ви надали лише метадані (Context), а сам вихідний код
    відсутній, цей PRD сформовано на основі архітектурного поділу модулів.
    Детальні кроки, бізнес-правила та посилання на рядки коду позначені як
    «не визначено»…

and not a single `file.py:line` reference, while the same repository's graph
held 383 fields, 199 functions and 116 methods the whole time. The model was
behaving correctly: it was handed metadata and told not to invent.

The chain, each link verified against production before the fix:

    ModuleDiscovery            -> Module(symbols=[])   hardcoded since v3.0
    module_prd.NoteMetadata    -> symbols=[]           so the key is omitted
    auto_detect: note.symbols  -> set()
    FeatureSpec.seed_symbols   -> []
    GraphRetriever.expand([])  -> returns before touching the graph
    read_locations([])         -> empty bundle
    the model                  -> "the source code is absent"

Module notes escaped it because the module generator has a disk fallback when
the graph yields nothing; the feature generator had none. Both ends are fixed:
`enrich_with_graph` reconnects modules to the graph, and the feature generator
gained the same fallback so a missing expansion can never again pass silently.

Measured after the fix on the same repository: 0 symbols -> 790, and
expand() on one module went from 0 roots to 27 roots / 500 located symbols.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from src.indexing.modules import Module, enrich_with_graph

SRC = Path(__file__).resolve().parents[2] / "src"


class _FakeStore:
    def __init__(self, by_prefix):
        self.by_prefix = by_prefix
        self.closed = False
        self.asked: list[str] = []

    def symbols_in_path(self, prefix, limit=200):
        self.asked.append(prefix)
        return self.by_prefix.get(prefix.rstrip("/"), [])[:limit]

    def close(self):
        self.closed = True


def _sym(name, file, line=1):
    from types import SimpleNamespace
    return SimpleNamespace(name=name, kind="function", file=file,
                           start_line=line, end_line=line + 5)


def test_enrichment_fills_symbols_from_the_graph(monkeypatch, tmp_path):
    modules = [Module(name="air", path="air", files=["air/views.py"]),
               Module(name="core", path="core", files=["core/models.py"])]
    store = _FakeStore({
        "air": [_sym("AirConfig", "air/apps.py"), _sym("index", "air/views.py")],
        "core": [_sym("dashboard", "core/views.py")],
    })

    graph = tmp_path / "graph.fdblite"
    graph.write_bytes(b"x")
    monkeypatch.setattr("src.indexing.graph.graph_store.make_graph_store",
                        lambda _p: store)

    class _S:
        def repo_graph_path(self, _slug):
            return graph

    added = enrich_with_graph(modules, tmp_path / "repo", _S())
    assert added == 3
    assert [s.name for s in modules[0].symbols] == ["AirConfig", "index"]
    assert modules[0].symbols[0].file == "air/apps.py"
    assert modules[0].symbols[0].line == 1
    assert store.closed, "the graph handle is leaked"


def test_a_repo_with_no_graph_degrades_instead_of_raising(tmp_path):
    """Generation must still run for a repository that was never indexed —
    it just goes back to producing what it produced before."""
    modules = [Module(name="air", path="air")]

    class _S:
        def repo_graph_path(self, _slug):
            return tmp_path / "absent.fdblite"

    assert enrich_with_graph(modules, tmp_path / "repo", _S()) == 0
    assert modules[0].symbols == []


def test_the_query_excludes_file_pseudo_symbols():
    """One `file_module` node exists per file; including them would crowd out
    the functions and classes a reader actually wants."""
    source = (SRC / "indexing" / "graph" / "graph_store.py").read_text()
    idx = source.find("def symbols_in_path(")
    body = source[idx:idx + 1200]
    assert "s.kind <> 'file_module'" in body
    assert "STARTS WITH $prefix" in body
    assert "LIMIT $limit" in body, "an unbounded query on a large repo"


def test_generation_enriches_before_writing_any_note():
    """The symbol list travels into the note's frontmatter and from there into
    feature detection, so enriching after the notes are written would fix
    nothing."""
    source = (SRC / "generation" / "orchestrator.py").read_text()
    enrich = source.find("enrich_with_graph(")
    assert enrich > 0, "discovery no longer enriches"
    discover = source.find("self.discovery.discover(")
    assert discover < enrich, "enrichment must follow discovery"
    # Call sites, not imports — the imports all sit at the top of the file.
    for later in ("self.module_gen.generate(", "detect_features("):
        pos = source.find(later)
        if pos > 0:
            assert enrich < pos, f"{later} runs before enrichment"


def test_the_feature_generator_falls_back_to_module_files():
    """The module generator has always had this; the feature generator had
    none, which is the whole reason module notes carried code and feature
    notes did not."""
    from src.generation import feature_doc

    body = inspect.getsource(feature_doc.FeatureDocGenerator.generate)
    assert "if not code_bundle.files_included():" in body
    assert "_module_file_locations(" in body


def test_the_fallback_is_bounded_and_says_when_it_has_nothing():
    from src.generation.feature_doc import MAX_FALLBACK_FILES

    assert 0 < MAX_FALLBACK_FILES <= 40
    body = inspect.getsource(
        __import__("src.generation.feature_doc", fromlist=["x"])
        .FeatureDocGenerator.generate)
    assert "feature_no_code" in body, (
        "a feature that still has no code must say so — that silence is what "
        "let twelve metadata-only notes ship"
    )


def test_module_names_are_accepted_in_both_shapes(tmp_path):
    """Detection passes bare names; notes record them as `modules/<name>`."""
    from src.generation.feature_doc import _module_file_locations

    (tmp_path / "air").mkdir()
    (tmp_path / "air" / "views.py").write_text("x = 1\n")

    for shape in ("air", "modules/air"):
        found = _module_file_locations(tmp_path, [shape])
        assert found, f"{shape!r} resolved to nothing"
        assert found[0][0] == "air/views.py"
        assert found[0][1] == 1
