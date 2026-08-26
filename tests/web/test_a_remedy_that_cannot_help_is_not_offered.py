"""A graph gap the operator cannot index away is shown without an indexing link.

The reviews page attaches a remedy to every adjustment, and the rule that
column lives by is that the remedy must be reachable: "temperatureDropped" has
no link because there is no temperature control anywhere, and offering one
would be a dead end.

The graph note broke that rule from the other side. Every changed file the
index had no symbols for was reported with "re-run `analyzer generate`" and a
link to the Repositories page — including the files that are missing because
the pull request's base is OLDER than the indexed revision and they were
renamed or deleted before it. One 2023 benchmark PR had 51 of its 172 changed
files in that state; re-indexing could not have added a single one. So the
review side now distinguishes the two and rides them on different actions, and
this file pins what the page does with the new one:

  * `base_too_old` gets its own sentence and NO link, like a dropped
    temperature — the honest remedy is that there is nothing to fix;
  * `partial`, where re-indexing genuinely helps, keeps the Repositories link;
  * the vocabulary is read leniently, because the wire carries open strings;
  * `KNOWN_ACTIONS` has the word, or the row's tag renders the raw
    `base_too_old` instead of the catalogue's;
  * all sixteen catalogues have both new words, translated.

The decision function and the set are lifted out of the component, compiled by
the web app's own tsc and executed on node — what is asserted is what the
browser runs. The component is full of prose ABOUT this decision, so a grep
would find every name in a comment explaining it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.web.test_a_changed_parameter_is_shown_to_the_operator import (
    ADJUSTMENTS,
    MESSAGES,
)
from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    TSC,
    _lift,
    _strip_comments,
)


def _lift_const(name: str, src: str) -> str:
    """One top-level `const NAME = …;`, brackets balanced.

    The remedy is a function and can be lifted by the shared helper; the
    vocabulary is a `Set` literal, and reading it with a regex would let a
    mention of the word in a nearby comment stand in for membership. It is
    compiled and asked instead.
    """
    opener = f"\nconst {name} = "
    start = src.find(opener)
    assert start != -1, f"{name} is not declared at top level in {ADJUSTMENTS.name}"
    start += 1
    depth = 0
    for i in range(start, len(src)):
        ch = src[i]
        if ch in "{([":
            depth += 1
        elif ch in "})]":
            depth -= 1
        elif ch == ";" and depth == 0:
            return src[start:i + 1]
    raise AssertionError(f"{name} has no terminating ';'")  # pragma: no cover


CASES = [
    {"name": "gone_since_the_index", "fn": "adjustmentRemedy",
     "args": [{"parameter": "graph_context", "action": "base_too_old"}]},
    {"name": "gone_since_shouted", "fn": "adjustmentRemedy",
     "args": [{"parameter": "graph_context", "action": " BASE_TOO_OLD "}]},
    {"name": "stale_index", "fn": "adjustmentRemedy",
     "args": [{"parameter": "graph_context", "action": "partial"}]},
    {"name": "no_graph_at_all", "fn": "adjustmentRemedy",
     "args": [{"parameter": "graph_context", "action": "unavailable"}]},
    {"name": "the_new_word_is_known", "fn": "knownAction", "args": ["base_too_old"]},
    {"name": "the_old_word_is_still_known", "fn": "knownAction", "args": ["partial"]},
    {"name": "an_invented_word_is_not", "fn": "knownAction", "args": ["frobnicated"]},
]


def _harness(cases: list[dict]) -> str:
    src = _strip_comments(ADJUSTMENTS.read_text(encoding="utf-8"))
    lifted = "\n\n".join([
        _lift("AdjustmentRemedy", src, ADJUSTMENTS),
        _lift("adjustmentRemedy", src, ADJUSTMENTS),
        _lift_const("KNOWN_ACTIONS", src),
    ])
    return (
        "type ParameterAdjustment = any;\n\n"
        + lifted
        + "\n\nconst knownAction = (a: string) => KNOWN_ACTIONS.has(a);\n"
        "const cases: any[] = " + json.dumps(cases) + ";\n"
        "const fns: Record<string, (...a: any[]) => any> = { adjustmentRemedy, knownAction };\n"
        "const out = cases.map((c) => {\n"
        "  const r = fns[c.fn](...c.args);\n"
        "  return { name: c.name, got: r === undefined ? '<undefined>' : r };\n"
        "});\n"
        "console.log(JSON.stringify(out));\n"
    )


@pytest.fixture(scope="module")
def decisions(tmp_path_factory) -> dict[str, object]:
    if not TSC.exists():
        pytest.skip("web deps not installed")
    tmp_path: Path = tmp_path_factory.mktemp("dead-end")
    ts = tmp_path / "harness.ts"
    ts.write_text(_harness(CASES), encoding="utf-8")
    build = subprocess.run(
        [str(TSC), str(ts), "--target", "es2020", "--module", "commonjs",
         "--outDir", str(tmp_path)],
        capture_output=True, text=True, timeout=300,
    )
    js = tmp_path / "harness.js"
    if not js.exists():  # pragma: no cover - tsc itself failed
        pytest.fail(f"tsc emitted nothing:\n{build.stdout}\n{build.stderr}")
    run = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    return {row["name"]: row["got"] for row in json.loads(run.stdout)}


def test_the_harness_actually_ran(decisions) -> None:
    """Guards the guard: a harness that produced nothing would make every
    assertion below vacuously true."""
    assert set(decisions) == {c["name"] for c in CASES}, decisions


# ─── the remedy, per cause ───────────────────────────────────────────


def test_a_base_older_than_the_index_gets_no_link(decisions) -> None:
    """Nothing on the Repositories page can add a file that does not exist at
    the indexed revision. The row says so instead of sending the operator to
    spend an index run and find the same gap."""
    r = decisions["gone_since_the_index"]
    assert r["kind"] == "graphBaseTooOld"
    assert r["links"] == []


def test_the_word_is_read_leniently(decisions) -> None:
    """The wire carries open strings; a backend that shouts or pads must not
    turn the honest remedy back into the dead end."""
    assert decisions["gone_since_shouted"]["kind"] == "graphBaseTooOld"


def test_a_stale_index_still_points_at_the_repositories_page(decisions) -> None:
    """The other half of the split: when the missing files ARE in the checkout
    the index was built from, re-indexing is exactly the fix, and the link
    must survive this change."""
    for name in ("stale_index", "no_graph_at_all"):
        r = decisions[name]
        assert r["kind"] == "graphMissing", name
        assert [link["label"] for link in r["links"]] == ["repositories"], name
        assert r["links"][0]["href"] == "/repositories", name


def test_the_action_has_a_word_in_the_vocabulary(decisions) -> None:
    """A row whose action the file does not know renders the raw wire string
    in the tag; `base_too_old` next to "graph context" is worse than "base
    too old", and the catalogue only gets asked for words in this set."""
    assert decisions["the_new_word_is_known"] is True
    assert decisions["the_old_word_is_still_known"] is True
    assert decisions["an_invented_word_is_not"] is False


# ─── the words, in every catalogue ───────────────────────────────────

NEW_KEYS = ("reviews.adjAction.base_too_old", "reviews.adjRemedy.graphBaseTooOld")
EN = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
LOCALES = sorted(p.stem for p in MESSAGES.glob("*.json"))


def test_there_are_sixteen_catalogues() -> None:
    assert len(LOCALES) == 16, LOCALES


@pytest.mark.parametrize("locale", LOCALES)
def test_every_locale_has_words_for_the_new_kind(locale: str) -> None:
    d = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    missing = [k for k in NEW_KEYS if not d.get(k, "").strip()]
    assert not missing, f"{locale} is missing {missing}"
    # The kinds that DO link end their sentence with the colon that introduces
    # the link list ("Index the repository:"). This one has no link, so a
    # sentence written as an introduction would trail off into nothing.
    assert not d["reviews.adjRemedy.graphBaseTooOld"].rstrip().endswith(":"), locale


@pytest.mark.parametrize("locale", [loc for loc in LOCALES if loc != "en"])
def test_no_locale_shows_english_for_them(locale: str) -> None:
    d = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    untranslated = [k for k in NEW_KEYS if d.get(k) == EN[k]]
    assert not untranslated, f"{locale} still shows English for {untranslated}"
