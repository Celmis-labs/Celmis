"""A parameter Celmis changed behind the operator's back is shown, with a remedy.

THE PRINCIPLE. The runtime self-heals in four places and every one of them is
right in the moment: a ceiling above the model's maximum is clamped, a
reasoning level the provider refuses is dropped and the call retried, a
temperature the model refuses is dropped the same way (claude-sonnet-5 takes
only 1; the architect agent ran ZERO times until that retry existed), and a
fallback model takes over an agent the primary could not serve. A review that
ran without the knob is worth more than no review.

And every one of them was invisible. Each was recorded somewhere different — a
field on the LLM result, a process-wide memory, an audit record and a log line,
a flag on the agent result — and none reached a screen: GET /api/reviews/*
exposed none of it, the reviews page rendered none of it, and on /settings/llm
a reasoning word the provider refused simply VANISHED from the dropdown with no
reason, no date and no remedy. A review quietly got worse from its second run
onward and nobody knew which knob to turn, or that there was one.

WHAT THIS FILE PINS. Two decision functions, one per screen, and the helpers
under them:

  - `adjustmentRemedy` on the reviews page: which sentence a row gets and which
    control it links to. The sentences tell the operator what to DO, and the
    links land on the control (an anchor on /settings/llm) rather than at the
    top of a long page — so the anchors are checked to exist.
  - `providerRefusals` / `refusalFor` / `temperatureFixedToDefault` behind the
    per-agent row both /settings/llm and the repo policy render: a refused word
    is listed and struck rather than omitted, a SAVED value that is now refused
    is found case-insensitively so the row can say so in red, and the older
    bare-word wire shape is folded in so a page ahead of its server still
    strikes the word.

WHY IT RUNS THE REAL FUNCTIONS. Both files are full of prose ABOUT these
decisions, so a grep finds the name in the comment explaining it. The functions
are lifted out of the source, transpiled by the web app's own `tsc` and
executed on node — what is asserted is what the browser will run. The lifting
apparatus is borrowed from the reasoning-save test rather than copied: a second
comment stripper is a second one that can quietly stop stripping.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    CONTROLS,
    TSC,
    _lift,
    _strip_comments,
)

WEB = CONTROLS.parent.parent
ADJUSTMENTS = WEB / "components" / "parameter-adjustments.tsx"
SETTINGS_PAGE = WEB / "app" / "(app)" / "settings" / "llm" / "page.tsx"
MESSAGES = WEB / "lib" / "i18n" / "messages"

#: Everything the reviews page's decision reaches for, in compile order.
FROM_ADJUSTMENTS = ("adjustmentsOf", "adjustmentsCount", "AdjustmentRemedy",
                    "adjustmentRemedy")
#: Everything the per-agent row's refusal rendering reaches for.
FROM_CONTROLS = ("storedReasoning", "providerRefusals", "refusalFor",
                 "temperatureFixedToDefault")


def _harness(cases: list[dict]) -> str:
    adjustments = _strip_comments(ADJUSTMENTS.read_text(encoding="utf-8"))
    controls = _strip_comments(CONTROLS.read_text(encoding="utf-8"))
    lifted = "\n\n".join(
        [_lift(n, adjustments, ADJUSTMENTS) for n in FROM_ADJUSTMENTS]
        + [_lift(n, controls, CONTROLS) for n in FROM_CONTROLS]
    )
    # The shapes live in web/lib/api.ts; aliasing them keeps this file from
    # becoming a second, drifting copy. Types are erased before any of it runs.
    return (
        "type ParameterAdjustment = any;\n"
        "type ReviewRunOut = any;\n"
        "type ProviderRefusal = any;\n"
        "type ModelCapabilities = any;\n"
        "type AgentLLMOverride = any;\n\n"
        + lifted
        + "\n\nconst cases: any[] = "
        + json.dumps(cases)
        + ";\n"
        "const fns: Record<string, (...a: any[]) => any> = {\n"
        "  adjustmentsOf, adjustmentsCount, adjustmentRemedy,\n"
        "  providerRefusals, refusalFor, temperatureFixedToDefault,\n"
        "};\n"
        "const out = cases.map((c) => {\n"
        "  const r = fns[c.fn](...c.args);\n"
        "  return { name: c.name, got: r === undefined ? '<undefined>' : r };\n"
        "});\n"
        "console.log(JSON.stringify(out));\n"
    )


def _run(cases: list[dict], tmp_path: Path) -> dict[str, object]:
    ts = tmp_path / "harness.ts"
    ts.write_text(_harness(cases), encoding="utf-8")
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


# ─── The reviews page: what a run carries, and what to do about it ───

ROWS = [
    {"agent": "architect", "parameter": "max_output_tokens", "requested": 70000,
     "sent": 65535, "action": "clamped", "reason": "model ceiling is 65535"},
    {"agent": "security", "parameter": "reasoning", "requested": "minimal",
     "sent": None, "action": "dropped",
     "reason": "Unsupported value: 'minimal' is not supported with this model."},
]


def _remedy(name: str, parameter: str, action: str) -> dict:
    return {"name": name, "fn": "adjustmentRemedy",
            "args": [{"parameter": parameter, "action": action}]}


CASES = [
    # ── what a run carries ──
    {"name": "older_run_has_no_rows", "fn": "adjustmentsOf", "args": [{"id": "r1"}]},
    {"name": "older_run_counts_zero", "fn": "adjustmentsCount", "args": [{"id": "r1"}]},
    {"name": "recorded_none_counts_zero", "fn": "adjustmentsCount",
     "args": [{"parameter_adjustments": []}]},
    {"name": "rows_alone_are_counted", "fn": "adjustmentsCount",
     "args": [{"parameter_adjustments": ROWS}]},
    {"name": "count_alone_is_believed", "fn": "adjustmentsCount",
     "args": [{"adjustments_count": 3}]},
    {"name": "trimmed_rows_keep_the_true_count", "fn": "adjustmentsCount",
     "args": [{"parameter_adjustments": ROWS[:1], "adjustments_count": 2}]},
    {"name": "a_broken_count_is_zero", "fn": "adjustmentsCount",
     "args": [{"adjustments_count": -4}]},
    {"name": "a_null_run_counts_zero", "fn": "adjustmentsCount", "args": [None]},
    # ── the remedy, per knob ──
    _remedy("clamped_ceiling", "max_output_tokens", "clamped"),
    _remedy("dropped_reasoning", "reasoning", "dropped"),
    _remedy("dropped_temperature", "temperature", "dropped"),
    _remedy("swapped_model", "model", "swapped"),
    _remedy("shouted_and_padded", "  Reasoning ", "DROPPED"),
    _remedy("unknown_knob_clamped", "top_p", "clamped"),
    _remedy("unknown_knob_swapped", "endpoint", "swapped"),
    _remedy("nothing_recognisable", "frobnication", "tuned"),
]


@pytest.fixture(scope="module")
def decisions(tmp_path_factory) -> dict[str, object]:
    if not TSC.exists():
        pytest.skip("web deps not installed")
    return _run(CASES + REFUSAL_CASES, tmp_path_factory.mktemp("adjustments"))


def test_the_harness_actually_ran(decisions) -> None:
    """Guards the guard: a harness that silently produced nothing would make
    every assertion below vacuously true."""
    assert set(decisions) == {c["name"] for c in CASES + REFUSAL_CASES}, decisions


def test_an_older_run_is_not_an_all_clear(decisions) -> None:
    """No rows and no count is "nobody wrote it down". It gets NO badge —
    a badge over it would be a claim — and null rather than [] so a consumer
    cannot read it as "recorded, and nothing was changed"."""
    assert decisions["older_run_has_no_rows"] is None
    assert decisions["older_run_counts_zero"] == 0
    assert decisions["recorded_none_counts_zero"] == 0
    assert decisions["a_null_run_counts_zero"] == 0


def test_either_field_alone_is_enough_for_the_badge(decisions) -> None:
    """A lean history may ship the count without the rows; a detail fetch the
    rows without a count. The badge must not wait for both."""
    assert decisions["rows_alone_are_counted"] == 2
    assert decisions["count_alone_is_believed"] == 3


def test_trimmed_rows_do_not_shrink_the_badge(decisions) -> None:
    """Rows below the count mean the history trimmed them; the badge says the
    true number so the operator opens it and the panel fetches the rest."""
    assert decisions["trimmed_rows_keep_the_true_count"] == 2


def test_a_broken_count_is_not_a_badge(decisions) -> None:
    assert decisions["a_broken_count_is_zero"] == 0


def _links(r: dict) -> list[str]:
    return [link["label"] for link in r["links"]]


def test_a_clamped_ceiling_sends_the_operator_to_the_ceiling(decisions) -> None:
    """"lower the ceiling for this agent, or pick a model with a higher one"
    — both are set on the per-agent row."""
    r = decisions["clamped_ceiling"]
    assert r["kind"] == "clamp"
    assert _links(r) == ["agents"]


def test_a_dropped_reasoning_level_offers_both_fixes(decisions) -> None:
    """"pick another level on the LLM page, or set a fallback model" — two
    remedies, two links. A refused level is fixed either by choosing a level
    the provider takes or by letting a different model answer; sending the
    operator to only one of them hides the other."""
    r = decisions["dropped_reasoning"]
    assert r["kind"] == "reasoningDropped"
    assert _links(r) == ["agents", "fallback"]


def test_a_dropped_temperature_has_nothing_to_link_to(decisions) -> None:
    """"this model accepts only its default temperature; nothing to fix, noted
    for comparability". There is no temperature control on any screen, so a
    link here would be a dead end — the exact thing this surface ends."""
    r = decisions["dropped_temperature"]
    assert r["kind"] == "temperatureDropped"
    assert r["links"] == []


def test_a_swapped_model_points_at_the_fallback(decisions) -> None:
    """"the fallback model ran this agent" — and the fallback is the control
    that decided it, so that is where the link lands."""
    r = decisions["swapped_model"]
    assert r["kind"] == "swap"
    assert _links(r) == ["fallback"]


def test_the_vocabulary_is_read_leniently(decisions) -> None:
    """The wire carries open strings, and a backend that capitalises or pads
    must not turn a known remedy into "check this agent's settings"."""
    assert decisions["shouted_and_padded"]["kind"] == "reasoningDropped"


def test_an_unknown_knob_falls_back_to_the_action(decisions) -> None:
    """A fifth self-heal renders as a row before this file learns its name —
    with the action's general remedy, not with nothing."""
    assert decisions["unknown_knob_clamped"]["kind"] == "clamp"
    assert decisions["unknown_knob_swapped"]["kind"] == "swap"
    r = decisions["nothing_recognisable"]
    assert r["kind"] == "other"
    assert _links(r) == ["agents"], "even the unknown case names a page to look at"


def test_every_remedy_link_lands_on_a_control_that_exists(decisions) -> None:
    """The links are anchors on /settings/llm so they land on the control, not
    at the top of a long page. An anchor renamed on the page with the link left
    behind would scroll to nothing — silently, which is how this whole class of
    bug ships. So the ids are read off the page the links point at."""
    page = SETTINGS_PAGE.read_text(encoding="utf-8")
    ids = set(re.findall(r'\bid="([^"]+)"', page))
    seen: set[str] = set()
    for name in ("clamped_ceiling", "dropped_reasoning", "swapped_model",
                 "nothing_recognisable"):
        for link in decisions[name]["links"]:
            path, _, fragment = link["href"].partition("#")
            assert path == "/settings/llm", link
            assert fragment in ids, (
                f"{link['href']} points at an id /settings/llm does not render; "
                f"the page has {sorted(ids)}"
            )
            seen.add(fragment)
    assert seen == {"review-agents", "review-fallback"}, seen


def test_every_remedy_has_words_in_every_language() -> None:
    """The kind is a key suffix on the reviews page. A kind without a sentence
    renders as "reviews.adjRemedy.<kind>" — in all sixteen languages."""
    kinds = {"clamp", "reasoningDropped", "temperatureDropped", "swap", "graphMissing", "other"}
    for path in sorted(MESSAGES.glob("*.json")):
        d = json.loads(path.read_text(encoding="utf-8"))
        missing = {k for k in kinds if not d.get(f"reviews.adjRemedy.{k}", "").strip()}
        assert not missing, f"{path.name}: no sentence for {sorted(missing)}"
        for label in ("agents", "fallback", "repositories"):
            assert d.get(f"reviews.adjRemedyLink.{label}", "").strip(), (path.name, label)


# ─── The per-agent row: what the provider has refused, said with a date ──

REFUSED_MINIMAL = {"parameter": "reasoning", "value": "minimal",
                   "reason": "Unsupported value: 'minimal' is not supported.",
                   "seen_at": "2026-08-20T10:00:00Z"}
REFUSED_TEMP = {"parameter": "temperature", "value": 0.1,
                "reason": "`temperature` may only be set to 1 for this model.",
                "seen_at": "2026-08-22T02:00:00Z"}
#: The contract shape: both refusals on one model.
NEW_WIRE = {"model": "m", "known": True, "max_output_tokens": 65536,
            "supports_reasoning": True, "reasoning_kind": "effort",
            "reasoning_values": ["low", "medium", "high"],
            "provider_refusals": [REFUSED_MINIMAL, REFUSED_TEMP],
            "supports_function_calling": True, "source": "litellm"}
#: A server one release behind: the bare word list, no sentence, no date.
OLD_WIRE = {**NEW_WIRE, "provider_refusals": None,
            "reasoning_values_provider_refused": ["minimal", "xhigh"]}
#: Both lists, and they disagree by one word.
BOTH_WIRES = {**NEW_WIRE, "reasoning_values_provider_refused": ["minimal", "xhigh"]}
#: Nothing refused, and nothing claimed.
CLEAN = {**NEW_WIRE, "provider_refusals": [], "reasoning_values_provider_refused": None}

REFUSAL_CASES = [
    {"name": "unanswered_has_no_refusals", "fn": "providerRefusals",
     "args": [None, "reasoning"]},
    {"name": "new_wire_filters_by_parameter", "fn": "providerRefusals",
     "args": [NEW_WIRE, "reasoning"]},
    {"name": "old_wire_is_folded_in", "fn": "providerRefusals",
     "args": [OLD_WIRE, "reasoning"]},
    {"name": "both_wires_are_unioned", "fn": "providerRefusals",
     "args": [BOTH_WIRES, "reasoning"]},
    {"name": "clean_model_has_none", "fn": "providerRefusals",
     "args": [CLEAN, "reasoning"]},
    # the saved value, looked up
    {"name": "saved_word_is_found", "fn": "refusalFor",
     "args": ["minimal", NEW_WIRE, "reasoning"]},
    {"name": "saved_word_is_found_however_typed", "fn": "refusalFor",
     "args": ["  MINIMAL ", NEW_WIRE, "reasoning"]},
    {"name": "saved_word_is_found_on_the_old_wire", "fn": "refusalFor",
     "args": ["xhigh", OLD_WIRE, "reasoning"]},
    {"name": "an_accepted_word_is_not_refused", "fn": "refusalFor",
     "args": ["high", NEW_WIRE, "reasoning"]},
    {"name": "nothing_saved_is_not_refused", "fn": "refusalFor",
     "args": [None, NEW_WIRE, "reasoning"]},
    {"name": "blank_saved_is_not_refused", "fn": "refusalFor",
     "args": ["   ", NEW_WIRE, "reasoning"]},
    {"name": "a_budget_never_matches_a_word", "fn": "refusalFor",
     "args": [4096, NEW_WIRE, "reasoning"]},
    {"name": "the_temperature_is_not_a_reasoning_refusal", "fn": "refusalFor",
     "args": [0.1, NEW_WIRE, "reasoning"]},
    {"name": "the_temperature_is_found_under_its_own_name", "fn": "refusalFor",
     "args": [0.1, NEW_WIRE, "temperature"]},
    # the model card's one-line temperature note
    {"name": "temperature_fixed_is_the_provider_entry", "fn": "temperatureFixedToDefault",
     "args": [NEW_WIRE]},
    {"name": "temperature_not_fixed_when_nothing_learned", "fn": "temperatureFixedToDefault",
     "args": [OLD_WIRE]},
    {"name": "temperature_not_fixed_unanswered", "fn": "temperatureFixedToDefault",
     "args": [None]},
]


def test_an_unanswered_lookup_claims_no_refusals(decisions) -> None:
    """Null caps is "did not get to ask" — no refusals, and not a clean bill
    either; nothing in the row should render over it."""
    assert decisions["unanswered_has_no_refusals"] == []
    assert decisions["temperature_not_fixed_unanswered"] is None


def test_refusals_are_read_per_parameter(decisions) -> None:
    """The reasoning control must not list the temperature refusal as a word,
    and the temperature note must not quote a reasoning sentence."""
    assert decisions["new_wire_filters_by_parameter"] == [REFUSED_MINIMAL]
    assert decisions["temperature_fixed_is_the_provider_entry"] == REFUSED_TEMP


def test_the_older_wire_still_strikes_the_word(decisions) -> None:
    """A page ahead of its server sees the bare word list. The words are
    folded into the contract shape — reason empty, date null — so the dropdown
    strikes them and the row can still say "refused" rather than lose the
    option between two page loads. The WHOLE bug was the silent loss."""
    got = decisions["old_wire_is_folded_in"]
    assert [r["value"] for r in got] == ["minimal", "xhigh"]
    assert all(r["parameter"] == "reasoning" for r in got)
    assert all(r["seen_at"] is None and r["reason"] == "" for r in got)


def test_both_wires_are_unioned_not_chosen_between(decisions) -> None:
    """When both lists arrive, a word either one names is a word the provider
    refused. The richer entry wins for a word both name; the word only the
    older list names is still struck."""
    got = decisions["both_wires_are_unioned"]
    assert [r["value"] for r in got] == ["minimal", "xhigh"]
    assert got[0] == REFUSED_MINIMAL, "the entry with the sentence and date wins"
    assert got[1]["seen_at"] is None


def test_a_clean_model_has_nothing_to_say(decisions) -> None:
    assert decisions["clean_model_has_none"] == []
    assert decisions["temperature_not_fixed_when_nothing_learned"] is None


def test_the_saved_value_is_matched_however_it_was_typed(decisions) -> None:
    """The red line on the row hangs off this. The operator's saved word and
    the word the provider refused are the same word however either was typed;
    a case-sensitive match would leave "Minimal" running nowhere in silence —
    the exact failure the line exists to end."""
    assert decisions["saved_word_is_found"] == REFUSED_MINIMAL
    assert decisions["saved_word_is_found_however_typed"] == REFUSED_MINIMAL
    assert decisions["saved_word_is_found_on_the_old_wire"]["value"] == "xhigh"


def test_an_accepted_or_absent_value_raises_no_alarm(decisions) -> None:
    assert decisions["an_accepted_word_is_not_refused"] is None
    assert decisions["nothing_saved_is_not_refused"] is None
    assert decisions["blank_saved_is_not_refused"] is None


def test_a_budget_is_never_mistaken_for_a_refused_word(decisions) -> None:
    """A thinking budget on a budget model is a number; no reasoning refusal
    names a number, and the row must not go red over 4096."""
    assert decisions["a_budget_never_matches_a_word"] is None


def test_the_temperature_refusal_stays_under_its_own_parameter(decisions) -> None:
    assert decisions["the_temperature_is_not_a_reasoning_refusal"] is None
    assert decisions["the_temperature_is_found_under_its_own_name"] == REFUSED_TEMP


# ─── The option the dropdown no longer lets you choose is still shown ──


def test_the_select_can_list_a_value_it_will_not_let_you_choose() -> None:
    """The row hands the dropdown the refused words as `disabled` options. The
    shared Select has to honour that flag — render the row, refuse the click —
    or the words are back to vanishing. Read off the component the row uses,
    not from memory: the flag is a field on its option type and a prop on the
    item it renders."""
    select = _strip_comments(
        (WEB / "components" / "ui" / "select.tsx").read_text(encoding="utf-8"))
    option_type = _lift("SelectOption", select, WEB / "components" / "ui" / "select.tsx")
    assert re.search(r"\bdisabled\?:\s*boolean", option_type), option_type
    assert re.search(r"<DropdownMenuItem[^>]*\bdisabled=\{o\.disabled\}", select, re.S), (
        "the item is rendered without its disabled flag — a struck word would "
        "still be choosable"
    )
