"""A question about 1C logic gets the whole algorithm, not the one function the words hit.

THE CONTRACT. For a 1C repository `MultiRepoRetriever._graph_text_matches` does
three things beyond the word match: a matched metadata object (found through a
synonym in another language) or module is expanded into its methods at a decayed
score; the best methods' callees are walked (callers, for an impact question)
within a character budget; and the question's own stems outweigh the terms a
model added. The intent comes from the SAME model call as the terms, and a reply
that is an old plain array or garbage still works. Nothing a hidden file holds is
scored, expanded into or walked to, and a call the resolver could only guess at
(several methods of that name) is not followed.

Fixtures are synthetic: an `acme` configuration with Russian-named objects.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.access.resolver import RepoAccessDecision, _RuleView
from src.config import Settings
from src.indexing.graph.pipeline import index_repo_graph
from src.qa import onec_context, query_terms
from src.qa.multi_repo_retriever import MultiRepoRetriever

SLUG = "acme-erp"
NS = ('xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core"')

# A document whose NAME says nothing about the question: only its uk synonym does.
_DOCUMENT = f"""<?xml version="1.0" encoding="UTF-8"?>
<MetaDataObject {NS}><Document uuid="1"><Properties><Name>ЗаданиеАкме</Name>
<Synonym><v8:item><v8:lang>uk</v8:lang><v8:content>Порізка аркушів</v8:content></v8:item></Synonym>
</Properties><ChildObjects/></Document></MetaDataObject>"""
_OTHER_DOCUMENT = _DOCUMENT.replace("ЗаданиеАкме", "ПриходАкме").replace(
    "Порізка аркушів", "Надходження товару")

_OBJECT_MODULE = """\
Процедура ПровестиДокумент() Экспорт
    ПодготовитьДанные();
КонецПроцедуры

Процедура ПодготовитьДанные()
    Размер = АкмеУтилиты.ОкруглитьРазмер(1);
    Вес = СекретАкме.Взвесить(Размер);
    Дубль.Метод();
КонецПроцедуры

Функция ЗаполнитьТаблицу() Экспорт
    Возврат 0;
КонецФункции
"""
_MANAGER_MODULE = """\
Функция ПолучитьШаблон() Экспорт
    Возврат Неопределено;
КонецФункции
"""
_UTILS = """\
Функция ОкруглитьРазмер(Значение) Экспорт
    Возврат ПроверитьГраницы(Значение);
КонецФункции

Функция ПроверитьГраницы(Значение)
    Возврат ДоЦелого(Значение);
КонецФункции

Функция ДоЦелого(Значение)
    Возврат Значение;
КонецФункции
"""
_SECRET = """\
Функция Взвесить(Значение) Экспорт
    Возврат Значение;
КонецФункции
"""
_DUPLICATE = """\
Функция Метод() Экспорт
    Возврат 1;
КонецФункции
"""
_RECEIPT = """\
Процедура ПровестиПриход() Экспорт
    Возврат;
КонецПроцедуры
"""

FILES = {
    "Documents/ЗаданиеАкме.xml": _DOCUMENT,
    "Documents/ЗаданиеАкме/Ext/ObjectModule.bsl": _OBJECT_MODULE,
    "Documents/ЗаданиеАкме/Ext/ManagerModule.bsl": _MANAGER_MODULE,
    "Documents/ПриходАкме.xml": _OTHER_DOCUMENT,
    "Documents/ПриходАкме/Ext/ObjectModule.bsl": _RECEIPT,
    "CommonModules/АкмеУтилиты/Ext/Module.bsl": _UTILS,
    "CommonModules/СекретАкме/Ext/Module.bsl": _SECRET,
    "Библиотека/Дубль.bsl": _DUPLICATE,
    "Копия/Дубль.bsl": _DUPLICATE,
}
SECRET_FILE = "CommonModules/СекретАкме/Ext/Module.bsl"
DUPLICATES = {"Библиотека/Дубль.bsl", "Копия/Дубль.bsl"}


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    repo = tmp_path / "repos" / SLUG
    for rel, text in FILES.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    cfg = Settings(workspace_dir=tmp_path)
    index_repo_graph(cfg.repo_path(SLUG), SLUG, cfg)
    return cfg


class _Reply:
    def __init__(self, text: str) -> None:
        self.text = text


def _no_model(monkeypatch) -> None:
    def boom(user_id, workspace_id):
        raise RuntimeError("no key")
    monkeypatch.setattr(MultiRepoRetriever, "_terms_generator", staticmethod(boom))


def _model_says(monkeypatch, text: str) -> list[dict]:
    calls: list[dict] = []

    def generate(**kw):
        calls.append(kw)
        return _Reply(text)

    monkeypatch.setattr(MultiRepoRetriever, "_terms_generator",
                        staticmethod(lambda u, w: generate))
    return calls


def _ask(settings: Settings, question: str, access=None) -> list:
    matches, _ = MultiRepoRetriever(settings=settings, vault_ret=None)._graph_text_matches(
        question, [SLUG], access or {}, "u", "w")
    return matches


def _names(matches) -> set[str]:
    return {h.name for _, h in matches}


def _deny(*globs: str) -> dict:
    dec = RepoAccessDecision(
        repo_slug=SLUG, visibility="code", open_default=False,
        rules=(_RuleView("code", (), globs, ()),), deny_globs=globs)
    return {SLUG: dec}


# ─── expanding an object into its methods ────────────────────────────────


def test_a_document_found_by_its_synonym_brings_its_object_and_manager_methods(
        settings, monkeypatch):
    _no_model(monkeypatch)
    names = _names(_ask(settings, "поясни логіку порізки"))
    assert {"Documents.ЗаданиеАкме.ObjectModule.ПровестиДокумент",
            "Documents.ЗаданиеАкме.ObjectModule.ПодготовитьДанные",
            "Documents.ЗаданиеАкме.ManagerModule.ПолучитьШаблон"} <= names
    assert not any("ПриходАкме" in n for n in names)


def test_an_expanded_method_scores_below_the_object_that_led_to_it(settings, monkeypatch):
    _no_model(monkeypatch)
    matches = _ask(settings, "поясни логіку порізки")
    obj = next(h for _, h in matches if h.kind == "vendor.onec.document")
    methods = [h for _, h in matches if h.name.startswith("Documents.ЗаданиеАкме.")]
    assert methods and all(h.score < obj.score for h in methods)
    assert all(h.score <= obj.score * onec_context.EXPAND_DECAY for h in methods)


def test_expansion_takes_at_most_the_cap_per_object_and_exported_methods_first():
    rows = [{"id": f"m{i}", "name": f"Catalogs.Акме.ObjectModule.Метод{i}", "kind": "method",
             "file": "Catalogs/Акме/Ext/ObjectModule.bsl", "start_line": i * 10,
             "end_line": i * 10 + 3, "signature": "Процедура Метод(Х)" + (" Экспорт" if i == 7 else "")}
            for i in range(10)]
    container = query_terms.SymbolHit("o", "Catalogs.Акме", "vendor.onec.catalog",
                                      "Catalogs/Акме.xml", 1, 1, 10.0)
    got = onec_context.expand_container(container, rows, [])
    assert len(got) == onec_context.EXPAND_PER_OBJECT
    assert got[0].id == "m7"


# ─── term sources ────────────────────────────────────────────────────────


def test_the_questions_own_stem_outranks_a_term_only_the_model_added():
    rows = [{"id": f"x{i}", "name": f"Модуль.Метод{i}", "kind": "method", "file": "a.bsl"}
            for i in range(7)]
    rows.append({"id": "q1", "name": "Модуль.Раскрой", "kind": "method", "file": "a.bsl"})
    rows.append({"id": "q2", "name": "Модуль.РаскройЛиста", "kind": "method", "file": "a.bsl"})
    rows.append({"id": "m1", "name": "Модуль.Нарезка", "kind": "method", "file": "a.bsl"})
    q, m = query_terms.stem("раскрой"), query_terms.stem("нарезка")
    # the rarer model term wins on idf alone — that is what the weights correct
    _, plain = query_terms.rank_symbols(rows, [q, m])
    assert plain[0].id == "m1"
    weights = query_terms.term_weights([q], [m])
    assert weights == {q: query_terms.QUESTION_TERM_WEIGHT, m: query_terms.MODEL_TERM_WEIGHT}
    _, weighted = query_terms.rank_symbols(rows, [q, m], weights=weights)
    assert weighted[0].id in ("q1", "q2")


def test_a_term_both_sides_have_counts_as_the_questions():
    assert query_terms.term_weights(["a" * 4], ["a" * 4, "b" * 4]) == {
        "a" * 4: query_terms.QUESTION_TERM_WEIGHT, "b" * 4: query_terms.MODEL_TERM_WEIGHT}


def test_a_symbol_matching_several_terms_gets_a_coverage_bonus():
    rows = [{"id": f"x{i}", "name": f"М.Метод{i}", "kind": "method", "file": "a.bsl"}
            for i in range(8)]
    rows.append({"id": "both", "name": "М.Оба", "kind": "method", "file": "a.bsl",
                 "docstring": "лист и раскрой"})
    terms = [query_terms.stem("лист"), query_terms.stem("раскрой")]
    _, with_bonus = query_terms.rank_symbols(rows, terms)
    _, without = query_terms.rank_symbols(rows, terms, coverage_bonus=0.0)
    assert with_bonus[0].score > without[0].score


# ─── walking the calls ───────────────────────────────────────────────────


def test_the_walk_follows_a_call_chain_two_hops_down(settings, monkeypatch):
    _no_model(monkeypatch)
    names = _names(_ask(settings, "поясни логіку порізки"))
    assert "АкмеУтилиты.ОкруглитьРазмер" in names           # one hop from ПодготовитьДанные
    assert "АкмеУтилиты.ПроверитьГраницы" in names          # two
    assert "АкмеУтилиты.ДоЦелого" not in names              # three: only a trace goes there


def test_a_trace_question_goes_one_hop_deeper_in_the_same_model_call(settings, monkeypatch):
    calls = _model_says(monkeypatch, '{"terms": ["порізка"], "intent": "trace"}')
    names = _names(_ask(settings, "поясни логіку порізки крок за кроком"))
    assert "АкмеУтилиты.ДоЦелого" in names
    assert len(calls) == 1


def test_the_walk_stops_at_the_character_budget(settings, monkeypatch):
    _no_model(monkeypatch)
    one_body = onec_context.CHARS_PER_LINE * 3   # ОкруглитьРазмер: three lines
    monkeypatch.setattr(onec_context, "WALK_MAX_CHARS", one_body + 1)
    names = _names(_ask(settings, "поясни логіку порізки"))
    assert "АкмеУтилиты.ОкруглитьРазмер" in names
    assert "АкмеУтилиты.ПроверитьГраницы" not in names


def test_a_call_the_resolver_could_only_guess_is_not_walked(settings, monkeypatch):
    _no_model(monkeypatch)
    matches = _ask(settings, "поясни логіку порізки")
    assert "Дубль.Метод" not in _names(matches)
    assert not DUPLICATES & {h.file for _, h in matches}


def test_a_callee_in_a_hidden_file_is_never_walked_to(settings, monkeypatch):
    _no_model(monkeypatch)
    assert SECRET_FILE in {h.file for _, h in _ask(settings, "поясни логіку порізки")}
    matches = _ask(settings, "поясни логіку порізки", _deny("CommonModules/СекретАкме/**"))
    assert SECRET_FILE not in {h.file for _, h in matches}
    assert "АкмеУтилиты.ОкруглитьРазмер" in _names(matches)   # the rest of the chain stays


def test_a_hidden_object_module_is_never_expanded_into(settings, monkeypatch):
    _no_model(monkeypatch)
    matches = _ask(settings, "поясни логіку порізки", _deny("Documents/ЗаданиеАкме/Ext/ManagerModule.bsl"))
    names = _names(matches)
    assert "Documents.ЗаданиеАкме.ObjectModule.ПровестиДокумент" in names
    assert not any("ManagerModule" in n for n in names)


def test_an_impact_question_walks_the_callers_not_the_callees(settings, monkeypatch):
    _model_says(monkeypatch, '{"terms": ["округлить", "размер"], "intent": "impact"}')
    names = _names(_ask(settings, "що зламається, якщо змінити ОкруглитьРазмер"))
    assert "Documents.ЗаданиеАкме.ObjectModule.ПодготовитьДанные" in names    # caller, hop 1
    assert "Documents.ЗаданиеАкме.ObjectModule.ПровестиДокумент" in names     # caller, hop 2
    assert "АкмеУтилиты.ДоЦелого" not in names                                # a callee


def test_a_walk_without_a_model_guesses_impact_from_the_wording(settings, monkeypatch):
    _no_model(monkeypatch)
    names = _names(_ask(settings, "хто викликає ОкруглитьРазмер"))
    assert "Documents.ЗаданиеАкме.ObjectModule.ПодготовитьДанные" in names


# ─── intent ──────────────────────────────────────────────────────────────


def test_an_object_reply_gives_terms_and_intent():
    plan = query_terms.parse_plan('```json\n{"terms": ["раскрой", "cut"], "intent": "trace"}\n```')
    assert plan.terms == [query_terms.stem("раскрой"), query_terms.stem("cut")]
    assert plan.intent == "trace"


def test_the_old_plain_array_reply_still_gives_terms_and_the_default_intent():
    plan = query_terms.parse_plan('["раскрой", "cut"]')
    assert plan.terms == [query_terms.stem("раскрой"), query_terms.stem("cut")]
    assert plan.intent == "explain"


def test_a_garbage_reply_gives_no_terms_and_the_default_intent():
    for text in ("sorry, no", "{broken", '{"terms": 5, "intent": 7}', '{"terms": ["раскрой"], "intent": "dance"}'):
        plan = query_terms.parse_plan(text)
        assert plan.intent == "explain"
    assert query_terms.parse_plan("sorry, no").terms == []
    assert query_terms.parse_plan('{"terms": ["раскрой"], "intent": "dance"}').terms == [
        query_terms.stem("раскрой")]


def test_a_failed_model_call_leaves_the_intent_to_the_wording():
    def boom(**kw):
        raise OSError

    plan = query_terms.plan_query("q", [], boom)
    assert (plan.terms, plan.intent) == ([], None)
    assert query_terms.guess_intent("who calls Foo?") == "impact"
    assert query_terms.guess_intent("поясни логіку порізки") == "explain"


def test_the_prompt_asks_for_the_intent_in_the_same_call(settings, monkeypatch):
    calls = _model_says(monkeypatch, '{"terms": ["порізка"], "intent": "explain"}')
    _ask(settings, "поясни логіку порізки")
    assert len(calls) == 1 and '"intent"' in calls[0]["prompt"]
