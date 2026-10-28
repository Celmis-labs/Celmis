"""A question in one language finds code named in another, and only code the asker may see.

THE CONTRACT. A consultant-style question has no identifier in it ("explain the
cutting logic"), and the code may be written in a language the question is not.
`MultiRepoRetriever._graph_text_matches` matches the question's word stems (and,
when a model is reachable, ONE call that restates them in the code's language)
against the names/docstrings of the graph's symbols, and hands back method
bodies with their lines. Without a model it still works on the question's own
stems, with the ru/uk spelling difference levelled. A file the access decision
hides is never scored, never an excerpt.

Fixtures are synthetic: an `acme` configuration with a Russian-named module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.access.resolver import RepoAccessDecision, _RuleView
from src.config import Settings
from src.indexing.graph.pipeline import index_repo_graph
from src.indexing.vectors.chunker import CodeChunker
from src.qa import query_terms
from src.qa.multi_repo_retriever import MultiRepoRetriever

SLUG = "acme-erp"
CUT = "Documents/РаскройАкме/Ext/ObjectModule.bsl"
OTHER = "Documents/ПриходАкме/Ext/ObjectModule.bsl"
HIDDEN = "Documents/РаскройСекрет/Ext/ObjectModule.bsl"

_MODULE = """\
// Раскрой листа на детали
Процедура ВыполнитьРаскрой(Лист) Экспорт
    Результат = Лист;
КонецПроцедуры

Функция ПосчитатьОтходы(Лист) Экспорт
    Возврат 0;
КонецФункции
"""
_OTHER = """\
Процедура ПровестиПриход(Док) Экспорт
    Возврат;
КонецПроцедуры
"""


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    repo = tmp_path / "repos" / SLUG
    for rel, text in {CUT: _MODULE, OTHER: _OTHER, HIDDEN: _MODULE}.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    cfg = Settings(workspace_dir=tmp_path)
    index_repo_graph(cfg.repo_path(SLUG), SLUG, cfg)
    return cfg


def _retriever(settings: Settings) -> MultiRepoRetriever:
    return MultiRepoRetriever(settings=settings, vault_ret=None)


def _no_model(monkeypatch) -> None:
    def boom(user_id, workspace_id):
        raise RuntimeError("no key")
    monkeypatch.setattr(MultiRepoRetriever, "_terms_generator", staticmethod(boom))


class _Reply:
    def __init__(self, text: str) -> None:
        self.text = text


def test_ru_uk_spellings_and_case_endings_share_a_stem():
    assert query_terms.stem("порізка") == query_terms.stem("порезка")
    assert query_terms.stem("раскроя") == query_terms.stem("раскрой")
    assert query_terms.stem("cutting") == "cutting"


def test_a_reply_is_read_as_words_and_a_bad_one_as_nothing():
    assert query_terms.parse_terms('```json\n["раскрой", "cut sheet", 5]\n```') == [
        query_terms.stem("раскрой"), query_terms.stem("cut"), query_terms.stem("sheet")]
    assert query_terms.parse_terms("sorry, no") == []
    assert query_terms.llm_terms("q", [], lambda **kw: (_ for _ in ()).throw(OSError())) == []


def test_without_a_model_the_questions_own_stems_find_the_module(settings, monkeypatch):
    _no_model(monkeypatch)
    matches, terms = _retriever(settings)._graph_text_matches(
        "поясни логіку розкрою у модулі раскроя", [SLUG], {}, "u", "w")
    assert terms
    assert matches and matches[0][1].file == CUT
    assert matches[0][1].start_line >= 1


def test_an_english_question_reaches_it_through_the_models_terms(settings, monkeypatch):
    seen = {}

    def generate(**kw):
        seen.update(kw)
        return _Reply('["раскрой", "порезка"]')

    monkeypatch.setattr(MultiRepoRetriever, "_terms_generator",
                        staticmethod(lambda u, w: generate))
    matches, _ = _retriever(settings)._graph_text_matches(
        "explain the cutting logic", [SLUG], {}, "u", "w")
    assert {h.file for _, h in matches} >= {CUT}
    assert "explain the cutting logic" in seen["prompt"]


def test_a_repository_without_1c_code_costs_no_model_call(tmp_path, monkeypatch):
    slug = "acme-api"
    src = tmp_path / "repos" / slug / "app.py"
    src.parent.mkdir(parents=True)
    src.write_text('def cut_sheet(sheet):\n    """Раскрой листа."""\n    return sheet\n',
                   encoding="utf-8")
    cfg = Settings(workspace_dir=tmp_path)
    index_repo_graph(cfg.repo_path(slug), slug, cfg)
    calls = []
    monkeypatch.setattr(MultiRepoRetriever, "_terms_generator",
                        staticmethod(lambda u, w: calls.append(1)))
    matches, terms = _retriever(cfg)._graph_text_matches(
        "поясни раскрой листа", [slug], {}, "u", "w")
    assert (matches, terms, calls) == ([], [], [])


def test_a_hidden_file_is_neither_scored_nor_returned(settings, monkeypatch):
    _no_model(monkeypatch)
    deny = ("Documents/РаскройСекрет/**",)
    dec = RepoAccessDecision(
        repo_slug=SLUG, visibility="code", open_default=False,
        rules=(_RuleView("code", (), deny, ()),), deny_globs=deny)
    matches, _ = _retriever(settings)._graph_text_matches(
        "раскрой листа", [SLUG], {SLUG: dec}, "u", "w")
    files = {h.file for _, h in matches}
    assert CUT in files and HIDDEN not in files


def test_the_excerpt_is_the_method_body_with_its_real_line_numbers(settings):
    r = _retriever(settings)
    files, bundle, _ = r._read_code_for_hits(
        [], excerpts=[(f"{SLUG}/{CUT}", 6, 8)])
    assert files == [f"{SLUG}/{CUT}"]
    assert f"### {SLUG}/{CUT}:6-8" in bundle
    assert "  6  Функция ПосчитатьОтходы" in bundle
    assert "ВыполнитьРаскрой" not in bundle


def test_a_bsl_module_is_chunked_per_method(settings):
    chunks = CodeChunker(settings).chunk_file(settings.repo_path(SLUG) / CUT, rel_path=CUT)
    named = {c.symbol: c for c in chunks if c.symbol}
    assert len(named) == 2
    method = next(c for n, c in named.items() if n.endswith("ПосчитатьОтходы"))
    assert (method.start_line, method.end_line) == (6, 8)
