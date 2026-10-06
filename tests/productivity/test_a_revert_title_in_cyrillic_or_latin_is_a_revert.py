"""What kind of PR is it: a revert, a hotfix, a bugfix or a feature — and which PR a revert undoes."""

from __future__ import annotations

import pytest

from src.productivity import classify
from src.productivity.settings import BUILTIN
from tests.productivity.support import FakeProvider, at, enable, make_engine, pr, prs_in, sync


@pytest.mark.parametrize("title", [
    'Revert "PROJ-456: Fix totals"',
    "revert PROJ-456",
    "REVERT: totals",
    "Rollback of the invoice change",
    "Відкат PROJ-456",
    "відкат змін у рахунках",
])
def test_a_revert_title_is_a_revert_in_either_alphabet(title: str) -> None:
    assert classify.classify_kind(title, "feature/x", BUILTIN) == "revert"


def test_the_revert_button_branch_is_a_revert_whatever_the_title() -> None:
    assert classify.classify_kind("Totals again", "revert-3f9a1c", BUILTIN) == "revert"


def test_a_hotfix_branch_or_title_is_a_hotfix() -> None:
    assert classify.classify_kind("Totals", "hotfix/totals", BUILTIN) == "hotfix"
    assert classify.classify_kind("Hotfix: totals", "feature/x", BUILTIN) == "hotfix"


@pytest.mark.parametrize("title", ["Fix the total", "bug in export", "Виправлення звіту", "баг у формі"])
def test_a_title_that_says_fix_is_a_bugfix(title: str) -> None:
    assert classify.classify_kind(title, "feature/x", BUILTIN) == "bugfix"


def test_anything_else_is_a_feature_and_a_word_inside_a_word_is_not_a_fix() -> None:
    assert classify.classify_kind("Add prefix column", "feature/x", BUILTIN) == "feature"
    assert classify.classify_kind("Add invoice export", "feature/x", BUILTIN) == "feature"


def test_a_pattern_that_does_not_compile_is_skipped_not_fatal() -> None:
    from dataclasses import replace

    broken = replace(BUILTIN, bugfix_patterns=("([", r"\bfix\b"))
    assert classify.classify_kind("fix it", "x", broken) == "bugfix"


def test_a_ticket_key_is_found_in_the_title_then_the_branch() -> None:
    assert classify.ticket_key("PROJ-123: Export", "feature/x") == "PROJ-123"
    assert classify.ticket_key("Export", "feature/PROJ-456-export") == "PROJ-456"
    assert classify.ticket_key("Export", "feature/x", "no key") is None


def test_a_revert_names_its_pr_by_number_when_it_can() -> None:
    assert classify.revert_reference("Revert totals", "This reverts pull request #412", 500) == (412, None)
    assert classify.revert_reference("Revert totals", "see /pull-requests/77/", 500) == (77, None)


def test_a_revert_never_names_itself() -> None:
    assert classify.revert_reference("Revert totals (#500)", "", 500) == (None, "totals (#500)")


def test_a_revert_without_a_number_keeps_the_quoted_title_as_a_hint() -> None:
    number, hint = classify.revert_reference('Revert "PROJ-456: Fix totals"', "", 500)
    assert number is None and hint == "PROJ-456: Fix totals"


def test_the_hint_finds_the_older_pr_with_that_title_else_that_ticket() -> None:
    candidates = [
        {"number": 410, "title": "PROJ-456: Fix totals", "ticket_key": "PROJ-456"},
        {"number": 420, "title": "PROJ-456: Fix totals", "ticket_key": "PROJ-456"},
        {"number": 450, "title": "PROJ-9999: Other", "ticket_key": "PROJ-9999"},
        {"number": 600, "title": "PROJ-456: Fix totals", "ticket_key": "PROJ-456"},
    ]
    # The newest older PR with that title; #600 is later than the revert and cannot be its target.
    assert classify.resolve_hint("PROJ-456: Fix totals", 500, candidates) == 420
    assert classify.resolve_hint("PROJ-456: reworded by hand", 500, candidates) == 420
    assert classify.resolve_hint("Nothing like it", 500, candidates) is None


def test_a_synced_revert_ends_up_linked_to_the_pr_it_undid() -> None:
    engine = make_engine()
    enable(engine)
    prs = [
        pr(410, title="PROJ-456: Fix totals", merged=at(-10), target="main"),
        pr(420, title='Revert "PROJ-456: Fix totals"', source="revert-3f9a", merged=at(-9), target="main"),
    ]
    sync(engine, FakeProvider(prs))
    rows = prs_in(engine)
    assert rows[420].kind == "revert"
    assert rows[420].reverts_pr_number == 410
    assert rows[410].kind == "bugfix" and rows[410].ticket_key == "PROJ-456"


def test_githubs_own_revert_text_names_the_pr_when_the_repository_is_this_one() -> None:
    body = "Reverts acme/shop#123"
    assert classify.revert_reference('Revert "Add totals"', body, 200, repo="acme/shop") == (123, None)
    assert classify.revert_reference('Revert "Add totals"', body, 200, repo="ACME/Shop") == (123, None)
    # another repository's number is not a PR of this one
    assert classify.revert_reference('Revert "Add totals"', body, 200, repo="acme/other") == (None, "Add totals")
    assert classify.revert_reference('Revert "Add totals"', body, 200) == (None, "Add totals")


def test_a_number_after_the_revert_word_beats_an_issue_mentioned_before_it() -> None:
    assert classify.revert_reference("Revert totals", "Fixes issue #7, reverts #9", 200) == (9, None)
    assert classify.revert_reference("Revert totals", "see #7 for context", 200) == (7, None)
