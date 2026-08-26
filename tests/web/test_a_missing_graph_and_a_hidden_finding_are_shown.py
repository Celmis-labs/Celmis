"""The reviews page says when the graph was missing, and how many findings a
run hid — with the rule that hid them.

Two silences, both measured on the benchmark this product was compared on:
161 runs with no code graph and nothing on any screen saying so; a deny-list
and a veto whose drops existed only in two log lines. Both now travel on the
run — the graph as a `graph_context` adjustment, the drops as `hidden` — and
this file pins what the page does with them:

  * `adjustmentRemedy` sends a missing graph to the Repositories page, not to
    the LLM settings: the fix is indexing, and a link to a page with nothing
    to change on it is the dead end the remedy column exists to end;
  * `hiddenTotal` is zero for a run recorded before the field existed (no
    claim over "nobody wrote it down"), sums every cause otherwise, and
    `hiddenHint` names the rules — "dropped 7" with no WHAT is the shape that
    let a filter eat true positives unnoticed;
  * every catalogue has words for the new kind, the new link and the count.

Runs the real functions through the web app's own tsc, like the sibling test,
because both files are full of prose ABOUT these decisions and a grep would
find the names in the comments.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.web.test_a_changed_parameter_is_shown_to_the_operator import (
    ADJUSTMENTS,
    MESSAGES,
    WEB,
)
from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    TSC,
    _lift,
    _strip_comments,
)

REVIEWS_PAGE = WEB / "app" / "(app)" / "reviews" / "page.tsx"
REPOSITORIES_PAGE = WEB / "app" / "(app)" / "repositories" / "page.tsx"

HIDDEN = {"by_rule": {"quality.todo": 2, "tests.no-coverage": 1},
          "duplicates": 1, "near_duplicates": 0, "low_confidence": 2,
          "no_evidence": 3, "veto": 0}

CASES = [
    {"name": "missing_graph", "fn": "adjustmentRemedy",
     "args": [{"parameter": "graph_context", "action": "unavailable"}]},
    {"name": "partial_graph", "fn": "adjustmentRemedy",
     "args": [{"parameter": "graph_context", "action": "partial"}]},
    {"name": "older_run_hid_nothing_we_know_of", "fn": "hiddenTotal", "args": [{"id": "r1"}]},
    {"name": "null_run", "fn": "hiddenTotal", "args": [None]},
    {"name": "recorded_zeros", "fn": "hiddenTotal",
     "args": [{"hidden": {"by_rule": {}, "duplicates": 0, "veto": 0}}]},
    {"name": "every_cause_summed", "fn": "hiddenTotal", "args": [{"hidden": HIDDEN}]},
    {"name": "a_broken_count_is_ignored", "fn": "hiddenTotal",
     "args": [{"hidden": {"by_rule": {"a": -3}, "duplicates": "seven", "veto": 2}}]},
    {"name": "the_hint_names_the_rules", "fn": "hiddenHintWords", "args": [{"hidden": HIDDEN}]},
    {"name": "the_hint_with_no_rules", "fn": "hiddenHintWords",
     "args": [{"hidden": {"duplicates": 2}}]},
]


def _harness(cases: list[dict]) -> str:
    adjustments = _strip_comments(ADJUSTMENTS.read_text(encoding="utf-8"))
    page = _strip_comments(REVIEWS_PAGE.read_text(encoding="utf-8"))
    lifted = "\n\n".join([
        _lift("adjustmentRemedy", adjustments, ADJUSTMENTS),
        _lift("hiddenTotal", page, REVIEWS_PAGE),
        _lift("hiddenHint", page, REVIEWS_PAGE),
    ])
    return (
        "type ParameterAdjustment = any;\n"
        "type ReviewRunOut = any;\n\n"
        + lifted
        + "\n\nconst cases: any[] = " + json.dumps(cases) + ";\n"
        # `t` is replaced by one that returns the key and its parameters, so
        # the assertion can read what the page would have asked the catalogue
        # for — the words themselves are the catalogue's business.
        "const hiddenHintWords = (run: any) => hiddenHint(run, (k: string, p?: any) => JSON.stringify({ k, p }));\n"
        "const fns: Record<string, (...a: any[]) => any> = { adjustmentRemedy, hiddenTotal, hiddenHintWords };\n"
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
    tmp_path: Path = tmp_path_factory.mktemp("hidden")
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
    assert set(decisions) == {c["name"] for c in CASES}, decisions


# ─── the graph's remedy is indexing, and the link lands there ────────


@pytest.mark.parametrize("name", ["missing_graph", "partial_graph"])
def test_a_missing_graph_sends_the_operator_to_the_repositories_page(decisions, name) -> None:
    r = decisions[name]
    assert r["kind"] == "graphMissing"
    assert [link["label"] for link in r["links"]] == ["repositories"]
    href = r["links"][0]["href"]
    assert href == "/repositories"
    assert REPOSITORIES_PAGE.exists(), "the link points at a page that is not there"


# ─── the hidden count ────────────────────────────────────────────────


def test_an_older_run_is_not_a_claim(decisions) -> None:
    assert decisions["older_run_hid_nothing_we_know_of"] == 0
    assert decisions["null_run"] == 0
    assert decisions["recorded_zeros"] == 0


def test_every_cause_is_summed(decisions) -> None:
    assert decisions["every_cause_summed"] == 2 + 1 + 1 + 2 + 3


def test_a_broken_count_is_ignored_not_believed(decisions) -> None:
    assert decisions["a_broken_count_is_ignored"] == 2


def test_the_hint_names_the_rules(decisions) -> None:
    got = json.loads(decisions["the_hint_names_the_rules"])
    assert got["k"] == "reviews.hiddenHint"
    assert got["p"]["rules"] == "quality.todo ×2, tests.no-coverage ×1"
    assert got["p"]["evidence"] == 3
    assert got["p"]["low"] == 2
    assert got["p"]["duplicates"] == 1


def test_the_hint_without_rules_says_so(decisions) -> None:
    got = json.loads(decisions["the_hint_with_no_rules"])
    assert got["p"]["rules"] == "—"
    assert got["p"]["duplicates"] == 2
    assert got["p"]["veto"] == 0


# ─── the words, in every catalogue ───────────────────────────────────

NEW_KEYS = (
    "reviews.adjParam.graph_context", "reviews.adjAction.unavailable",
    "reviews.adjAction.partial", "reviews.adjRemedy.graphMissing",
    "reviews.adjRemedyLink.repositories", "reviews.hiddenCount", "reviews.hiddenHint",
)
EN = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))
LOCALES = sorted(p.stem for p in MESSAGES.glob("*.json"))


@pytest.mark.parametrize("locale", LOCALES)
def test_every_locale_has_words_for_them(locale: str) -> None:
    d = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    missing = [k for k in NEW_KEYS if not d.get(k, "").strip()]
    assert not missing, f"{locale} is missing {missing}"
    # The placeholders the page fills must survive translation, or the count
    # renders as the literal "{count}".
    assert "{count}" in d["reviews.hiddenCount"]
    for p in ("{rules}", "{duplicates}", "{near}", "{low}", "{evidence}", "{veto}"):
        assert p in d["reviews.hiddenHint"], (locale, p)


@pytest.mark.parametrize("locale", [loc for loc in LOCALES if loc != "en"])
def test_no_locale_shows_english_for_them(locale: str) -> None:
    d = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    untranslated = [
        k for k in NEW_KEYS
        if d.get(k) == EN[k] and k not in ("reviews.adjRemedyLink.repositories",)
    ]
    assert not untranslated, f"{locale} still shows English for {untranslated}"
