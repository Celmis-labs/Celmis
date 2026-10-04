"""Every built-in library rule is one the store would accept and a review can
apply.

A library entry is copied into a workspace by `add_from_library`, which runs
the store's own validator — so an entry that breaks a limit would fail for the
person pressing "Add", not here. These tests move that failure to the commit
that writes the entry.
"""

from __future__ import annotations

from collections import Counter

import pytest

from src.review.policy_rules import glob_matches, split_globs
from src.review.rules_library import (
    LANGUAGES,
    LIBRARY,
    TAGS,
    get_library_rule,
    search_library,
)
from src.review.rules_store import validate_rule

#: The titles the release notes promise, by name.
PROMISED = (
    "Do not ignore exceptions",
    "Always sanitize user inputs",
    "Avoid equality operators in loop termination conditions",
    "Prevent race conditions in shared state operations",
    "Ensure database uniqueness constraints",
    "Avoid using undeclared variables",
)


def test_the_library_is_about_forty_rules_across_the_languages():
    assert 35 <= len(LIBRARY) <= 60
    covered = {lang for rule in LIBRARY for lang in rule.languages}
    assert covered == set(LANGUAGES), set(LANGUAGES) - covered
    assert {"security", "performance"} <= {tag for rule in LIBRARY for tag in rule.tags}


def test_ids_and_titles_are_unique():
    for counter in (Counter(r.id for r in LIBRARY),
                    Counter(r.title.casefold() for r in LIBRARY)):
        assert [k for k, n in counter.items() if n > 1] == []


@pytest.mark.parametrize("rule", LIBRARY, ids=lambda r: r.id)
def test_every_entry_passes_the_store_validator(rule):
    stored = validate_rule({
        "title": rule.title, "instructions": rule.instructions,
        "path_glob": rule.path_glob or None, "severity": rule.severity,
        "agents": [], "examples_good": rule.examples_good or None,
        "examples_bad": rule.examples_bad or None, "status": "active",
    })
    assert stored["title"] == rule.title
    assert set(rule.languages) <= set(LANGUAGES)
    assert rule.tags and set(rule.tags) <= set(TAGS)
    for glob in split_globs(rule.path_glob):
        assert "{" not in glob or "}" in glob, glob


@pytest.mark.parametrize("title", PROMISED)
def test_the_promised_rules_are_there(title):
    assert any(r.title == title for r in LIBRARY), title


def test_a_language_glob_matches_its_own_files():
    assert glob_matches("web/app/page.tsx", get_library_rule("ts.no-any").path_glob)
    assert glob_matches("src/a.py", get_library_rule("python.no-mutable-default-args").path_glob)
    assert not glob_matches("src/a.py", get_library_rule("vue.key-on-v-for").path_glob)


def test_search_filters_by_text_language_and_tag():
    assert {r.id for r in search_library(language="vue")} == {
        r.id for r in LIBRARY if "vue" in r.languages}
    hits = search_library("SANITIZE")
    assert any(r.id == "security.sanitize-user-input" for r in hits)
    assert all("security" in r.tags for r in search_library(tag="security"))
    assert search_library("no such words anywhere") == []
    assert get_library_rule("nope") is None
