"""The report on an external 1C file: facts, document, and the model's boxed-in words.

Everything runs on the synthetic `Акме` configuration (`tests.onec.acme`): the
report must say the file is not usable, name each planted problem with its
excerpt from both sides, list the change the file lacks, and keep every number
and excerpt out of a model's hands.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from src.docs.blocks import (
    Code,
    Heading,
    Para,
    Table,
    blocks_from_data,
    blocks_to_data,
    to_markdown_text,
)
from src.onec.checks import check
from src.onec.compare import compare
from src.onec.config import Config, DirSource
from src.onec.external import inspect_bytes
from src.onec.facts import build_facts, numbers_in
from src.onec.review_doc import build_document
from src.onec.review_llm import Choice, build_prompt, parse_choice, parse_prose
from tests.onec import acme


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = acme.write_dump(tmp_path_factory.mktemp("dump"))
    data = acme.report_file()
    ext = inspect_bytes(data, f"{acme.REPORT}.erf")
    cfg = Config(DirSource(root), "acme")
    facts = build_facts(ext, data, cfg, compare(ext, cfg), check(ext, cfg),
                        config_info={"label": "acme", "ref": "main@abcdef12", "age": "14d"},
                        now=datetime(2025, 6, 1))
    return ext, cfg, facts, data


def _text(blocks) -> str:
    return to_markdown_text(blocks)


def test_the_facts_name_the_missing_register_with_both_excerpts(built):
    _ext, _cfg, facts, _ = built
    reg = next(b for b in facts.blockers if b.code == "missing_object"
               and b.name == "ТоварыНаСкладахСтарый")
    assert reg.plural == "AccumulationRegisters" and reg.where == "query"
    assert reg.procedure == "Проверки"
    assert "ТоварыНаСкладахСтарый" in reg.file_snippet.lines[0]
    assert reg.similar == ["ТоварыНаСкладахАкме"]


def test_the_facts_count_what_was_checked(built):
    _ext, _cfg, facts, data = built
    assert facts.level == "bad"
    assert len(facts.sha256) == 64 and facts.size == len(data)
    assert facts.refs_missing.get("AccumulationRegisters") == 1
    assert sum(facts.refs_ok.values()) >= 3
    assert facts.calls_total >= 3 and facts.calls_bad == 3
    assert facts.handlers_total == 2
    obj = next(m for m in facts.modules if m.owner == "object")
    assert obj.file_lines != obj.config_lines
    assert obj.config_last_marker == "ACME-205"
    assert obj.config_last_marker_date == datetime(2025, 5, 12)
    assert [m.id for m in facts.markers_missing] == ["ACME-205"]
    assert any("ТолькоВКонфигурации" in h for h in obj.missing)
    bal = next(b for b in facts.balance if b["module"] == "object module")
    assert bal["items"]["Если"] == [1, 0]  # the planted unclosed block


def test_a_bare_task_number_with_a_date_is_a_marker():
    import re

    from src.onec.compare import DEFAULT_MARKER, all_markers_of

    marks = all_markers_of(
        "//404242+ от 14.03.2025\n// задача 55123 2025.04.10\nх = 12345; // 99999\n"
        "// просто 111222 текст\n", "m.bsl", re.compile(DEFAULT_MARKER))
    assert set(marks) == {"404242", "55123"}
    assert marks["404242"].numeric and marks["404242"].latest == datetime(2025, 3, 14)
    assert marks["55123"].latest == datetime(2025, 4, 10)


def test_the_report_reads_like_a_report_in_ukrainian(built):
    _e, _c, facts, _ = built
    blocks = build_document(facts, lang="uk")
    md = _text(blocks)
    heads = [b.text for b in blocks if isinstance(b, Heading) and b.level == 2]
    assert heads == ["1. З чим порівнювали", "2. Блокери", "3. Версія застаріла",
                     "4. Що перевірено і в порядку", "5. Межі перевірки", "6. Рекомендація"]
    assert "Висновок: файл використовувати не можна." in md
    assert "регістра накопичення «ТоварыНаСкладахСтарый»" in md
    assert "Файл, модуль" not in md.split("Блокер 1")[0]  # excerpts live in the blockers
    assert "ТоварыНаСкладахАкме" in md                    # the close name
    assert "ACME-205" in md and "Трекер ACME" in md
    assert "Показник | Файл | Конфігурація" in md.replace("| --- |", "")
    assert "Рядків у модулі" in md and "04.03.2025" in md
    assert "розпаковано всі" in md
    assert "Не підключати цей файл." in md
    assert "has no" not in md.split("## 5.")[0].replace("has no template", "")  # no raw English


def test_the_report_reads_in_english_too(built):
    _e, _c, facts, _ = built
    md = _text(build_document(facts, lang="en"))
    assert "Conclusion: do not use this file." in md
    assert "## 2. Blockers" in md and "Do not connect this file." in md
    assert "Висновок" not in md


def test_the_index_age_is_words_not_the_mcp_shorthand(built):
    _e, _c, facts, _ = built
    uk = _text(build_document(facts, lang="uk"))
    en = _text(build_document(facts, lang="en"))
    assert "індекс оновлено 14 днів тому" in uk and "оновлено 14 днів тому." in uk
    assert "index updated 14 days ago" in en and "was updated 14 days ago." in en
    assert "14d" not in uk and "14d" not in en


@pytest.mark.parametrize(("age", "uk", "en"), [
    ("now", "щойно", "just now"),
    ("1m", "1 хвилину тому", "1 minute ago"),
    ("3h", "3 години тому", "3 hours ago"),
    ("21d", "21 день тому", "21 days ago"),
    ("5d", "5 днів тому", "5 days ago"),
    ("?", "", ""),
    ("", "", ""),
])
def test_an_age_reads_in_both_languages(age, uk, en):
    from src.onec.review_doc import _T

    assert _T("uk").ago(age) == uk
    assert _T("en").ago(age) == en


def test_a_conclusion_is_coloured_by_its_level(built):
    _e, _c, facts, _ = built
    first = next(b for b in build_document(facts) if isinstance(b, Para) and b.kind == "callout")
    assert first.tone == "bad"


def test_blocks_survive_the_json_round_trip(built):
    _e, _c, facts, _ = built
    blocks = build_document(facts)
    again = blocks_from_data(json.loads(json.dumps(blocks_to_data(blocks))))
    assert again == blocks
    assert any(isinstance(b, Code) for b in again) and any(isinstance(b, Table) for b in again)


def test_without_a_configuration_the_report_says_so(built):
    ext, _cfg, _facts, data = built
    facts = build_facts(ext, data, None, None, check(ext, None))
    md = _text(build_document(facts))
    assert facts.level in ("bad", "unknown")
    assert "Порівняння з конфігурацією не робилося" in md
    assert "Версія застаріла" not in md


def test_a_clean_copy_is_usable(tmp_path):
    from tests.onec.container_writer import external

    data = external(acme.REPORT, object_module=acme.CONFIG_OBJECT_MODULE,
                    forms=[("ФормаОтчета", ["ПриОткрытии", "ПриЗакрытии"], acme.CONFIG_FORM_MODULE)],
                    templates=[("Макет", ["Шапка"], [])])
    ext = inspect_bytes(data, "same.erf")
    cfg = Config(DirSource(acme.write_dump(tmp_path)), "acme")
    facts = build_facts(ext, data, cfg, compare(ext, cfg), check(ext, cfg))
    assert facts.level == "good" and not facts.blockers
    md = _text(build_document(facts))
    assert "Висновок: файл можна використовувати." in md
    assert "збігається з конфігурацією рядок у рядок" in md


# ─── the model ───────────────────────────────────────────────────────

def test_a_choice_is_parsed():
    assert parse_choice("").mode == "none" and parse_choice("none").mode == "none"
    assert parse_choice("default").mode == "default"
    c = parse_choice("openai:gpt-x")
    assert (c.mode, c.provider, c.model) == ("model", "openai", "gpt-x")
    assert parse_choice("nonsense").mode == "none"


def test_the_prompt_carries_the_facts_but_not_the_secrets_of_the_file(built):
    _e, _c, facts, _ = built
    prompt = build_prompt(facts, "uk")
    assert "ТоварыНаСкладахСтарый" in prompt and "Ukrainian" in prompt
    assert facts.sha256 not in prompt


def test_the_models_words_go_into_their_places_and_invented_numbers_do_not(built):
    _e, _c, facts, _ = built
    bid = facts.blockers[0].id
    raw = json.dumps({
        "conclusion": "Файл не можна використовувати. Він старіший за конфігурацію.",
        "blockers": {bid: {"explanation": "Запит читає регістр, якого вже немає.",
                           "effect": "Формування завершиться помилкою 987654."},
                     "b99": {"explanation": "не існує"}},
        "recommendation": "Оновити файл з конфігурації.",
        "notes": {"sources": "Коментар версії збігається.", "version": "У 2031 році все змінилося."},
    }, ensure_ascii=False)
    prose, dropped = parse_prose("```json\n" + raw + "\n```", facts)
    assert dropped == 2                                    # 987654 and 2031 are not in the facts
    assert prose.conclusion == "Він старіший за конфігурацію."   # the verdict is not restated
    assert prose.blockers[bid]["explanation"].startswith("Запит читає")
    assert prose.blockers[bid]["effect"] == ""
    assert "b99" not in prose.blockers
    assert prose.notes == {"sources": "Коментар версії збігається."}
    md = _text(build_document(facts, prose=prose))
    assert "Запит читає регістр, якого вже немає." in md   # the model's sentence
    assert "ТоварыНаСкладахСтарый" in md                   # the excerpt is still the facts'
    assert "987654" not in md and "2031" not in md
    assert "Очікуваний результат у 1С" in md              # the template effect stayed


def test_markup_in_the_models_text_is_neutralised(built):
    _e, _c, facts, _ = built
    prose, _ = parse_prose('{"conclusion": "**жирний** `код` <b>x</b> | таблиця"}', facts)
    assert not any(ch in prose.conclusion for ch in "*`<>|")


def test_an_answer_that_is_not_json_is_refused(built):
    _e, _c, facts, _ = built
    with pytest.raises(ValueError):
        parse_prose("sorry, I cannot", facts)


def test_numbers_of_the_facts_are_what_a_model_may_repeat(built):
    _e, _c, facts, _ = built
    allowed = numbers_in(facts)
    assert facts.size in allowed and 2025 in allowed


def test_a_failing_model_leaves_the_template_and_a_note(built, monkeypatch):
    from src.onec import review_llm

    _e, _c, facts, _ = built

    def boom(*_a, **_k):
        raise RuntimeError("no key for provider google")

    monkeypatch.setattr(review_llm, "profile_for", boom)
    prose, label, why = review_llm.ask_prose(facts, "uk", Choice("default"), "ws", "u")
    assert prose is None and label == "" and "no key" in why
    assert review_llm.ask_prose(facts, "uk", Choice("none"), "ws", "u") == (None, "", "")


def test_a_model_that_answers_is_used_and_its_spend_booked(built, monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace

    from src.llm import completion
    from src.onec import review_llm

    _e, _c, facts, _ = built
    booked = []
    prof = SimpleNamespace(provider="openai", model="gpt-x", api_key="k", litellm_model="openai/gpt-x")
    monkeypatch.setattr(review_llm, "profile_for", lambda *_a, **_k: prof)
    monkeypatch.setattr(completion, "completion_route", lambda p: {})
    monkeypatch.setattr(completion, "record_completion_spend",
                        lambda p, resp, **kw: booked.append(kw["operation"]))
    fake = ModuleType("litellm")
    fake.completion = lambda **kw: SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content='{"conclusion": "Коротко: не можна.", "recommendation": ""}'))])
    monkeypatch.setitem(sys.modules, "litellm", fake)
    prose, label, why = review_llm.ask_prose(facts, "uk", Choice("model", "openai", "gpt-x"), "ws", "u")
    assert why == "" and label == "openai:gpt-x" and prose.conclusion == "Коротко: не можна."
    assert booked == ["onec_check"]


def test_a_translated_1c_name_or_a_foreign_date_keeps_the_template(built):
    _e, _c, facts, _ = built
    bid = facts.blockers[0].id
    raw = json.dumps({
        "conclusion": "Версія файлу датується 2024-06-17.",
        "blockers": {bid: {"explanation": "Запит читає регістр ТовариНаСкладахСтарий.",
                           "effect": "Звіт ТоварыНаСкладахСтарый не сформується."}},
        "recommendation": "Перенести правку станом на 31.12.1999.",
        "notes": {"version": "Регістр замінено в задачі ACME-99999."},
    }, ensure_ascii=False)
    prose, dropped = parse_prose(raw, facts)
    assert dropped == 4                                              # ACME-99999: no such task
    assert prose.conclusion == "" and prose.recommendation == ""
    assert prose.blockers[bid]["explanation"] == ""                  # a name the facts lack
    assert "ТоварыНаСкладахСтарый" in prose.blockers[bid]["effect"]  # copied exactly: kept
    md = _text(build_document(facts, prose=prose))
    assert "ТовариНаСкладахСтарий" not in md and "2024-06-17" not in md


def test_the_prompt_carries_the_template_drafts_and_the_version_date(built):
    _e, _c, facts, _ = built
    prompt = build_prompt(facts, "uk")
    assert "DRAFTS:" in prompt and "never translate" in prompt
    assert '"saved"' in prompt
