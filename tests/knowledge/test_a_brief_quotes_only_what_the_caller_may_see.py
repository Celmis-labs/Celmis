"""What a task briefing contains and what it never does.

The contract (docs/CODEBASE.md, "Brief"): the briefing is built from rows the
caller's visibility already filtered; sections come in a fixed order and only
when non-empty (skills, checklists, API examples, notes, then plain documents);
every item names its `pack@vN`; repo pointers appear only for
repos the caller may read and an unreadable one leaves no trace; the text is
cut at a line boundary to the budget and says how many items were left out; a
task nothing matches gets one honest line, never an empty string.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from src.knowledge.brief import MIN_BUDGET, NO_MATCH, build_brief
from src.knowledge.search import terms_for

PACK = NS(id="p1", slug="shop-pack")
OTHER = NS(id="p2", slug="ops-pack")
_n = 0


def item(kind, name, description, text="", *, pack=PACK, number=3, skill=None, repos=(), path=None):
    global _n
    _n += 1
    it = NS(id=f"i{_n}", kind=kind, name=name, description=description,
            path=path or f"{kind}/{name}.md", skill=skill, repos=list(repos), tags=[],
            content=text, terms=terms_for(f"{name} {description} {text}"))
    return (pack, number, it)


def everyone(_slug):
    return True


def nobody(_slug):
    return False


def test_sections_appear_in_the_fixed_order():
    rows = [
        item("note", "retry-notes", "How retries work in invoicing", "Invoicing retries twice."),
        item("api", "invoice-create", "Create an invoice call", "POST invoicing create."),
        item("checklist", "invoicing-review", "Review invoicing changes", "Check idempotency."),
        item("skill", "invoicing", "Invoicing skill", "Use the invoicing client.", skill="invoicing"),
    ]
    out = build_brief("change how invoicing retries", rows, everyone, 4000)
    heads = [ln for ln in out.text.splitlines() if ln.startswith("## ")]
    assert heads == ["## Skills to follow", "## Checklists", "## API examples",
                     "## How other services do it"]


def test_a_section_with_nothing_in_it_is_left_out():
    rows = [item("note", "retry-notes", "How retries work", "Retries twice.")]
    out = build_brief("retries", rows, everyone, 4000)
    assert out.text.splitlines()[1] == "## How other services do it"       # line 1 is the framing line
    assert "Skills to follow" not in out.text and "Checklists" not in out.text


def test_each_section_has_its_own_cap_and_the_rest_are_also_relevant():
    # a document (kind "doc", what a knowledge-base page is) has a section of its own
    rows = ([item("skill", f"sk{i}", "payments flow", "payments", skill=f"sk{i}") for i in range(5)]
            + [item("checklist", f"ck{i}", "payments review", "payments") for i in range(4)]
            + [item("api", f"ap{i}", "payments call", "payments") for i in range(4)]
            + [item("note", f"nt{i}", "payments notes", "payments") for i in range(5)]
            + [item("doc", f"dc{i}", "payments doc", "payments") for i in range(8)])
    out = build_brief("payments", rows, everyone, 20000)
    sections: dict[str, int] = {}
    current = ""
    for ln in out.text.splitlines():
        if ln.startswith("## "):
            current = ln[3:]
            sections[current] = 0
        elif ln.startswith("- "):
            sections[current] += 1
    assert sections == {"Skills to follow": 3, "Checklists": 2, "API examples": 2,
                        "How other services do it": 3, "Documents": 2, "Also relevant": 5}
    assert len(out.item_ids) == 17


def test_every_item_names_the_pack_and_version_it_came_from():
    rows = [item("skill", "invoicing", "Invoicing skill", "x", skill="invoicing", number=7),
            item("note", "n", "invoicing note", "x", pack=OTHER, number=2)]
    out = build_brief("invoicing", rows, everyone, 4000)
    assert "[shop-pack@v7]" in out.text and "[ops-pack@v2]" in out.text
    assert out.pack_ids == ["p1", "p2"]


def test_a_skill_says_how_to_open_it():
    rows = [item("skill", "invoicing", "Invoicing skill", "Use it.", skill="invoicing")]
    out = build_brief("invoicing", rows, everyone, 4000)
    assert 'skills name="shop-pack/invoicing"' in out.text


def test_the_files_of_a_skill_are_only_ever_also_relevant():
    rows = [item("file", "ref", "invoicing reference", "invoicing details", skill="invoicing",
                 path="invoicing/ref.md")]
    out = build_brief("invoicing reference", rows, everyone, 4000)
    assert out.text.splitlines()[1] == "## Also relevant"


def test_the_snippet_is_the_paragraph_that_matches_the_task():
    text = "# Title\n\nIrrelevant intro.\n\nTo reconcile ledgers, post the adjustment twice.\n"
    rows = [item("note", "ledger", "ledger notes", text)]
    out = build_brief("reconcile ledgers", rows, everyone, 4000)
    assert "post the adjustment twice" in out.text and "Irrelevant intro" not in out.text


def test_file_text_is_fetched_only_for_the_items_that_are_quoted():
    rows = [item("note", f"n{i}", "ledger notes", "") for i in range(6)]
    asked: list[list[str]] = []

    def load(ids):
        asked.append(list(ids))
        return {i: "Reconcile the ledgers nightly." for i in ids}

    out = build_brief("ledger reconcile", rows, everyone, 4000, load_content=load)
    assert len(asked) == 1 and len(asked[0]) == 3
    assert "Reconcile the ledgers nightly." in out.text


def test_a_failing_content_loader_still_gives_a_brief():
    rows = [item("note", "ledger", "ledger notes", "")]

    def load(_ids):
        raise RuntimeError("db is down")

    out = build_brief("ledger", rows, everyone, 4000, load_content=load)
    assert "ledger" in out.text and out.item_ids


# ─── Repo pointers ───────────────────────────────────────────────────


def test_repo_pointers_are_listed_for_repos_the_caller_may_read():
    rows = [item("note", "retry", "retry notes", "x",
                 repos=["billing", "billing:src/client.py:42", "ledger:app.py"])]
    out = build_brief("retry", rows, everyone, 4000)
    assert "Repos: billing, billing:src/client.py:42, ledger:app.py" in out.text


def test_a_repo_the_caller_may_not_read_leaves_no_trace():
    rows = [item("note", "retry", "retry notes", "x",
                 repos=["billing", "secretrepo:src/a.py:1", "secretrepo"])]
    out = build_brief("retry", rows, lambda slug: slug == "billing", 4000)
    assert "secretrepo" not in out.text and "Repos: billing" in out.text


def test_with_no_readable_repo_the_repos_line_is_gone_but_the_note_stays():
    rows = [item("note", "retry", "retry notes", "x", repos=["billing:a.py"])]
    out = build_brief("retry", rows, nobody, 4000)
    assert "billing" not in out.text and "Repos:" not in out.text and "retry" in out.text


def test_a_repo_check_that_raises_counts_as_no():
    def broken(_slug):
        raise RuntimeError("boom")

    rows = [item("note", "retry", "retry notes", "x", repos=["billing"])]
    out = build_brief("retry", rows, broken, 4000)
    assert "Repos:" not in out.text and "billing" not in out.text


# ─── Budget and the empty case ───────────────────────────────────────


def test_the_text_is_cut_at_a_line_to_the_budget_and_counts_what_was_left():
    rows = [item("note", f"n{i}", "ledger notes", "Reconcile ledgers nightly. " * 6) for i in range(3)]
    full = build_brief("ledger reconcile", rows, everyone, 20000)
    assert "more" not in full.text
    cut = build_brief("ledger reconcile", rows, everyone, 400)
    assert len(cut.text) <= 400
    last = cut.text.splitlines()[-1]
    assert last.startswith("… +") and last.endswith(" more")
    left = int(last.split("+")[1].split()[0])
    assert left + len(cut.item_ids) == len(full.item_ids)
    assert all(ln in full.text for ln in cut.text.splitlines()[:-1])


def test_a_tiny_budget_is_raised_and_still_names_the_best_match():
    rows = [item("skill", "invoicing", "Invoicing skill " + "very long " * 60, "x", skill="invoicing")]
    out = build_brief("invoicing", rows, everyone, 10)
    assert len(out.text) <= MIN_BUDGET and "invoicing" in out.text and out.item_ids


def test_a_task_nothing_matches_gets_one_line_that_points_at_search():
    rows = [item("note", "retry", "retry notes", "x")]
    out = build_brief("kubernetes ingress", rows, everyone, 4000)
    assert out.text == NO_MATCH and "skills query=" in out.text
    assert out.item_ids == [] and out.pack_ids == []


def test_an_empty_task_or_no_packs_at_all_gets_the_same_line():
    assert build_brief("", [item("note", "n", "d", "x")], everyone, 4000).text == NO_MATCH
    assert build_brief("anything at all", [], everyone, 4000).text == NO_MATCH
