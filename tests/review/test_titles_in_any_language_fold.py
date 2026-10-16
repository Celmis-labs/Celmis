"""A review written in a language other than English folds its duplicates too.

The title tokenizer was `[a-z0-9]+`. A review in Ukrainian can come back with one
defect copied into several files as several findings, all of them with the
byte-identical title "Помилка логіки значення за замовчуванням для категорії", and
every one was posted: Cyrillic tokenized to nothing, `title_jaccard` scored
0.0, and neither the same-file nor the cross-file pass folded a thing.
"""
from __future__ import annotations

from src.review.agents.verifier import prefilter, title_jaccard
from src.review.models import Finding, FindingSeverity


def _f(file: str, line: int, title: str) -> Finding:
    return Finding(
        file_path=file, line=line, rule_id="defect.logic-error", title=title,
        body="", severity=FindingSeverity.ERROR, agent="defect",
        confidence=0.9, evidence_kind="inferred",
    )


def test_cyrillic_titles_have_tokens():
    t = "Помилка логіки значення за замовчуванням для категорії"
    assert title_jaccard(t, t) == 1.0
    assert title_jaccard(t, "Помилка логіки значення за замовчуванням UUID") >= 0.6


def test_latin_with_diacritics_and_identifiers_still_tokenize():
    assert title_jaccard("Fehlende Prüfung in `get_user`", "Fehlende Prüfung in `get_user`") == 1.0
    assert title_jaccard("snake_case name", "snake case name") == 1.0


def test_one_defect_in_five_files_in_ukrainian_is_one_comment():
    title = "Помилка логіки значення за замовчуванням для категорії"
    findings = [_f(f"src/models/Post{i}.php", 60 + i, title) for i in range(5)]
    kept = prefilter(findings).kept
    assert len(kept) == 1
    for i in range(5):
        assert f"src/models/Post{i}.php" in kept[0].body or kept[0].file_path == f"src/models/Post{i}.php"


def test_different_defects_in_ukrainian_stay_apart():
    a = _f("a.php", 10, "Неіснуюча константа статусу чернетки")
    b = _f("b.php", 20, "Синхронні HTTP-запити в циклі")
    assert len(prefilter([a, b]).kept) == 2
