"""A card's front matter: what Celmis reads, what it keeps, what it refuses."""

from __future__ import annotations

import pytest

from src.kb.cards import MAX_LIST, CardProblem, normalize_status, parse_card, source_sha


def test_the_keys_celmis_reads_and_the_teams_own_keys_are_both_kept():
    c = parse_card("sales/reserve.md", (
        "---\ntype: process\nid: proc-reserve\ndomain: sales\nstatus: Verified\nauthor: Олена\n"
        "lang: UK\ntags: [резерв, продажі]\nobjects_1c: [Документ.Заказ, РегистрНакопления.Резерв]\n"
        "related: [proc-ship]\nreplaces: [modules/sales]\nsource: erp@3f2c1a9\n"
        "owner: ops\nmigration: {target: frappe, wave: 2}\n---\n# Резервування\n\nКроки.\n"))
    assert (c.type, c.id, c.status, c.domain, c.author, c.lang) == (
        "process", "proc-reserve", "verified", "sales", "Олена", "uk")
    assert c.title == "Резервування"
    assert c.tags == ["резерв", "продажі"]
    assert c.objects == ["Документ.Заказ", "РегистрНакопления.Резерв"]
    assert c.related == ["proc-ship"] and c.replaces == ["modules/sales"]
    assert c.source_sha == "3f2c1a9"
    assert c.fm["owner"] == "ops" and c.fm["migration"] == {"target": "frappe", "wave": 2}
    assert c.body.startswith("# Резервування") and not c.warnings


def test_a_file_without_front_matter_is_a_draft_note_named_after_its_file():
    c = parse_card("notes/Як працює склад.md", "Текст без заголовка.\n")
    assert (c.type, c.status, c.fm) == ("note", "draft", {})
    assert c.id == "як-працює-склад"
    assert c.title == "Як працює склад"


@pytest.mark.parametrize("text, reason", [
    ("---\na: &x [1]\nb: *x\n---\n", "anchor"),
    ("---\na: 1\n", "never closes"),
    ("---\n- just\n- a list\n---\n", "mapping"),
    ("---\na: [unclosed\n---\n", "not valid YAML"),
    ("---\n" + "k: " + "x" * 17_000 + "\n---\n", "longer than"),
])
def test_unsafe_or_broken_front_matter_is_refused_with_a_reason(text, reason):
    with pytest.raises(CardProblem, match=reason):
        parse_card("bad.md", text)


def test_a_generator_type_an_odd_status_and_a_source_without_a_commit_are_warned_about():
    c = parse_card("x.md", "---\ntype: module\nstatus: someday\nsource: written by hand\n---\n")
    assert c.type == "kb-module"
    assert c.status == "someday"
    assert c.source_sha == ""
    assert len(c.warnings) == 3


def test_status_spellings_and_source_forms():
    assert normalize_status("Needs_Review") == "needs-review"
    assert normalize_status("на пересмотр") == "needs-review"
    assert normalize_status("чернетка") == "draft"
    assert source_sha("celmis@ABCDEF1") == "abcdef1"
    assert source_sha("0123456789abcdef0123456789abcdef01234567") == "0123456789abcdef0123456789abcdef01234567"
    assert source_sha("erp@main") == ""


def test_every_list_is_capped():
    many = ", ".join(f"Документ.Д{i}" for i in range(MAX_LIST + 50))
    c = parse_card("x.md", f"---\nobjects_1c: [{many}]\n---\n")
    assert len(c.objects) == MAX_LIST
