"""One word, one token — in the index and in the query alike.

The contract (src/knowledge/search.py, docs/CODEBASE.md "Search"): text is
composed (NFC) before it is split, so Ukrainian typed or pasted in decomposed
form (`и` + a combining breve for `й`, `і` + a diaeresis for `ї`) still finds and
is found by the composed word; an apostrophe INSIDE a word — ASCII `'`, curly `’`
or the modifier letter `ʼ` — is dropped, so the three spellings of one word are
one token; the English possessive `'s` is not part of its word; and a snippet
still lands on the matching word when the text around it is in the other form.
"""

from __future__ import annotations

import unicodedata
from types import SimpleNamespace as NS

import pytest

from src.knowledge import ingest
from src.knowledge import search as s

APOSTROPHES = ("'", "’", "ʼ")


def nfd(text: str) -> str:
    return unicodedata.normalize("NFD", text)


def doc(name, body="", path=""):
    return NS(name=name, description="", path=path or f"{name}.md", terms=s.terms_for(body), tags=[])


@pytest.mark.parametrize("word", ["Київ", "йога", "їжак", "Йосип", "café"])
def test_a_decomposed_word_is_the_same_token_as_the_composed_one(word):
    assert nfd(word) != word
    assert s.tokenize(nfd(word)) == s.tokenize(word) == s.tokenize(word.lower())


def test_a_decomposed_sentence_loses_no_letter():
    assert s.tokenize(nfd("Київ та йога")) == ["київ", "йога"]


def test_the_three_apostrophes_make_one_token():
    forms = [s.tokenize(f"об{mark}єкт") for mark in APOSTROPHES]
    assert forms[0] == forms[1] == forms[2] == ["обєкт"]
    assert [s.tokenize(f"п{mark}ять") for mark in APOSTROPHES] == [["пять"]] * 3


def test_a_long_word_with_an_apostrophe_keeps_the_stem_the_other_spellings_share():
    stems = {tuple(s.tokenize(f"зв{mark}язковий")) for mark in APOSTROPHES}
    assert stems == {("звязковий", "~звязк")}
    assert s.tokenize("обʼєктів") == s.tokenize("об'єктів") == s.tokenize("об’єктів")


def test_an_apostrophe_that_is_not_inside_a_word_only_ends_it():
    assert s.tokenize("'quoted' and ‘words’ users'") == ["quoted", "words", "users"]


def test_the_possessive_is_not_part_of_its_word_in_any_of_its_spellings():
    assert s.tokenize("the user's token") == s.tokenize("the user’s token") == ["user", "token"]
    assert s.tokenize("don't") == ["dont"]


def test_a_query_in_one_form_finds_a_document_in_every_other_form():
    docs = [doc("a", "Опис об'єкта у Київ"), doc("b", "Інше про погоду")]
    for body_mark in APOSTROPHES:
        for query_mark in APOSTROPHES:
            indexed = [doc("a", f"Опис об{body_mark}єкта у Київ"), docs[1]]
            hits = s.rank(indexed, f"об{query_mark}єкта")
            assert [h.item.name for h in hits] == ["a"], (body_mark, query_mark)


def test_a_decomposed_query_finds_a_composed_document_and_the_other_way_round():
    composed = [doc("a", "Місто Київ над Дніпром"), doc("b", "нічого спільного")]
    assert [h.item.name for h in s.rank(composed, nfd("Київ"))] == ["a"]
    decomposed = [doc("a", nfd("Місто Київ над Дніпром")), doc("b", "нічого спільного")]
    assert [h.item.name for h in s.rank(decomposed, "Київ")] == ["a"]


def test_an_uploaded_decomposed_file_is_indexed_under_the_composed_words():
    text = nfd("# Заголовок\n\nЙога та Київ, об'єкт.\n").encode()
    [kept] = ingest.ingest_files([("notes/a.md", text)]).files
    assert {"йога", "київ", "обєкт"} <= set(kept.terms)
    assert not any("̆" in term or "̈" in term for term in kept.terms)


def test_a_snippet_lands_on_the_word_even_when_the_text_is_in_the_other_form():
    filler = "слово " * 80
    body = f"{filler}\n\n{nfd('Тут сказано про Київ і про об' + chr(0x2019) + 'єкт')} {filler}"
    cut = s.snippet(body, s.tokenize("об'єкт"), max_chars=120)
    assert "об’єкт" in cut and len(cut) <= 130
