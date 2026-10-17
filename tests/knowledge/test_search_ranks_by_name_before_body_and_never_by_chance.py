"""Tokens, ranking and snippets — deterministic, and cheap enough to run on every call.

The contract (docs/CODEBASE.md, "Search"): words are lower-cased unicode words
without stop words or bare numbers; a long word also yields a stem so another
case of the same Ukrainian or Russian noun still matches; a hit in the name
outweighs one in the description, which outweighs the path and the body terms;
the order is score, then path, then name, so the same query always gives the
same list; a snippet is the paragraph that matches most, cut to size.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from src.knowledge import search as s


def doc(name, description="", path="", terms=None, tags=()):
    return NS(name=name, description=description, path=path or f"{name}.md",
              terms=s.terms_for(f"{name} {description}") if terms is None else terms,
              tags=list(tags))


def test_words_are_lower_cased_and_drop_stop_words_numbers_and_one_letter_tokens():
    assert s.tokenize("The Quick brown 42 fox, a b and 2024!") == ["quick", "brown", "fox"]
    assert s.tokenize("") == [] and s.tokenize(None) == []


def test_a_long_word_also_yields_a_stem_that_cannot_collide_with_a_word():
    assert s.tokenize("authentication") == ["authentication", "~authe"]
    assert s.tokenize("short") == ["short"]
    assert all(not t.startswith("~") for t in s.tokenize("tilde ~word"))


def test_two_cases_of_one_ukrainian_noun_share_a_stem():
    assert set(s.tokenize("автентифікація")) & set(s.tokenize("автентифікації")) == {"~автен"}


def test_identifiers_keep_their_underscores_and_digits():
    assert s.tokenize("retry_policy v2 api") == ["retry_policy", "~retry", "v2", "api"]


def test_the_most_frequent_terms_are_kept_and_ties_are_stable():
    text = "alpha alpha alpha beta beta gamma delta"
    assert s.terms_for(text, limit=2) == {"alpha": 3, "beta": 2}
    tied = s.terms_for("zeta eta theta", limit=2)
    assert list(tied) == ["eta", "theta"]
    assert s.terms_for(text) == s.terms_for(text)


def test_the_name_outweighs_the_description_which_outweighs_the_path_and_body():
    """One mention each, nothing else: where the word sits decides the order."""
    by_name = doc("invoicing", "", path="docs/a.md", terms={})
    by_description = doc("misc", "covers invoicing", path="docs/b.md", terms={})
    by_path = doc("other", "", path="invoicing/c.md", terms={})
    by_body = doc("body", "", path="docs/d.md", terms={"invoicing": 1})
    order = [h.item.name for h in s.rank([by_body, by_path, by_description, by_name], "invoicing")]
    assert order[:2] == ["invoicing", "misc"]
    assert set(order[2:]) == {"other", "body"}


def test_an_item_that_shares_no_token_does_not_score():
    assert s.rank([doc("alpha", "beta")], "gamma") == []
    assert s.rank([doc("alpha")], "the a of") == []


def test_tags_count_like_the_description():
    tagged = doc("one", "", tags=["billing"], terms={})
    plain = doc("two", "", terms={})
    assert [h.item.name for h in s.rank([plain, tagged], "billing")] == ["one"]


def test_the_order_is_score_then_path_then_name_so_ties_are_stable():
    items = [doc("same", "x", path="b/z.md"), doc("same", "x", path="a/z.md"),
             doc("same", "x", path="a/a.md")]
    first = [h.item.path for h in s.rank(items, "same")]
    assert first == ["a/a.md", "a/z.md", "b/z.md"]
    assert [h.item.path for h in s.rank(list(reversed(items)), "same")] == first


def test_the_limit_applies_and_empty_inputs_are_empty():
    items = [doc(f"w{i}", "shared word") for i in range(10)]
    assert len(s.rank(items, "shared", limit=3)) == 3
    assert s.rank(items, "shared", limit=0) == []
    assert s.rank([], "shared") == [] and s.rank(items, "") == []


def test_a_rare_term_outranks_a_common_one():
    common = [doc(f"c{i}", "deploy deploy") for i in range(8)]
    rare = doc("r", "rollback")
    top = s.rank([*common, rare], "deploy rollback")[0]
    assert top.item.name == "r"


def test_items_missing_optional_fields_still_rank():
    bare = NS(name="alpha", description=None, path=None, terms=None)
    assert [h.item for h in s.rank([bare], "alpha")] == [bare]


# ─── Snippets ────────────────────────────────────────────────────────

TEXT = """---
name: x
description: y
---
# Title

Intro with nothing useful.

## Retries

Back off twice on a 429 reply, then give up and report the failure.

Another paragraph about ledgers.
"""


def test_the_snippet_is_the_paragraph_that_matches_most_with_its_heading():
    got = s.snippet(TEXT, s.tokenize("retries 429 reply"))
    assert got == "## Retries Back off twice on a 429 reply, then give up and report the failure."


def test_front_matter_never_appears_in_a_snippet():
    assert "description" not in s.snippet(TEXT, s.tokenize("zzz"))
    assert s.snippet(TEXT, []).startswith("# Title Intro")


def test_a_long_paragraph_is_cut_around_the_first_match_within_the_size():
    long = ("lorem ipsum " * 60) + "the needle sits here " + ("dolor sit " * 60)
    got = s.snippet(long, s.tokenize("needle"), max_chars=120)
    assert "needle" in got and len(got) <= 120
    assert got.startswith("… ") and got.endswith(" …")


def test_a_short_text_and_no_text():
    assert s.snippet("one line", ["line"]) == "one line"
    assert s.snippet("", ["x"]) == "" and s.snippet(None, ["x"]) == ""
