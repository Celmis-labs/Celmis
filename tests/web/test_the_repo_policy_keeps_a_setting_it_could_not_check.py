"""The layer that WINS must not delete a setting nobody touched either.

/admin/review-policies/<repo> now edits the same three per-agent knobs as
/settings/llm — model, output ceiling, reasoning — and it OUTRANKS that page:
a repo policy beats the workspace `agents` entry, which beats the review
profile, which beats ReviewSettings. Its `agent_llm_overrides` map is PUT
whole and REPLACES the stored one, because absent is the only spelling of
"stop overriding" this chain has. So every field the form declines to emit is
a field the save DELETES.

That is exactly how the workspace screen shipped broken: `reasoning` was sent
only once the capabilities lookup had ANSWERED, and the answer is null in three
states that say nothing about the model — the lookup in flight, the lookup
errored (`retry: false`, so one blip is final for as long as the page is open),
and no model to ask about. Pressing Save in any of them wiped the override
silently. Repeating it here would be worse: this layer is the one whose value
actually reaches the provider.

TWO THINGS THIS FILE REFUSES TO DO.

It does not grep. The page is full of prose ABOUT `reasoningToSave` and about
the vendor-prefix helper that was deleted, so a test looking for a name finds
it in the comment explaining why the name is gone. The real function is lifted
out of the page, transpiled by the web app's own `tsc` and executed on node —
what is asserted is what the browser will run.

And it does not describe the server from memory. The keys the form emits are
checked against the router's own `_agent_llm_fields()` and its `_MODEL_FIELD`
ban, and the emitted map is fed through the router's real
`_agent_llm_overrides_from_payload` and the real `ReviewPolicyIn`. A payload
this test blesses is a payload the API accepts.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

# The TypeScript-lifting harness, borrowed rather than copied: it is the same
# apparatus reading the same two files, and a second copy of a comment stripper
# is a second copy that can quietly stop stripping.
from tests.web.test_a_configured_reasoning_setting_survives_the_save import (
    CONTROLS,
    POLICY,
    TSC,
    _lift,
    _strip_comments,
)

#: Everything `policyAgentLLMOverrides` reaches for, in an order that compiles
#: as one file. All but the last live in the shared controls module.
FROM_CONTROLS = ("AgentDraft", "reasoningValue", "takesReasoning",
                 "storedReasoning", "reasoningToSave", "agentEntryToSave")
FROM_POLICY = ("policyAgentLLMOverrides",)

AGENTS = ["defect", "contract", "security", "verifier"]

API = CONTROLS.parent.parent / "lib" / "api.ts"


def _ts_const(name: str) -> int:
    """A numeric `export const` read out of web/lib/api.ts.

    Read rather than repeated: these two numbers already exist twice on
    purpose (the server bound, and the form's copy of it so a value is refused
    in front of the person typing it), and a third copy in a test would be the
    one nobody updates.
    """
    m = re.search(rf"export const {name} = ([\d_]+);", API.read_text(encoding="utf-8"))
    assert m, f"{name} is not an `export const` in {API.name}"
    return int(m.group(1).replace("_", ""))


def _harness(cases: list[dict]) -> str:
    controls = _strip_comments(CONTROLS.read_text(encoding="utf-8"))
    policy = _strip_comments(POLICY.read_text(encoding="utf-8"))
    lifted = "\n\n".join(
        [_lift(n, controls, CONTROLS) for n in FROM_CONTROLS]
        + [_lift(n, policy, POLICY) for n in FROM_POLICY]
    )
    # The shapes live in web/lib/api.ts; aliasing them keeps this file from
    # becoming a second, drifting copy of those definitions. Types are erased
    # before any of it runs.
    return (
        "type ModelCapabilities = any;\n"
        "type AgentSettings = any;\n"
        "type AgentLLMOverride = any;\n\n"
        + lifted
        + "\n\nconst cases: any[] = "
        + json.dumps(cases)
        + ";\n"
        "const out = cases.map((c) => ({\n"
        "  name: c.name,\n"
        "  got: policyAgentLLMOverrides(c.agents, c.drafts, c.stored, c.caps),\n"
        "}));\n"
        "console.log(JSON.stringify(out));\n"
    )


def _run(cases: list[dict], tmp_path: Path) -> dict[str, dict]:
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
    run = subprocess.run(
        ["node", str(js)], capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 0, run.stderr
    return {row["name"]: row["got"] for row in json.loads(run.stdout)}


def _drafts(**overrides: dict) -> dict:
    """Five blank rows, with the named ones filled in. Blank means inherit."""
    out = {a: {"model": "", "maxOut": "", "reasoning": "", "temperature": ""}
           for a in AGENTS}
    for agent, patch in overrides.items():
        out[agent] = {**out[agent], **patch}
    return out


def _caps(kind: str | None, values: list[str] | None = None) -> dict:
    return {
        "model": "m", "known": kind is not None, "max_output_tokens": 65536,
        "supports_reasoning": kind is not None, "reasoning_kind": kind,
        "reasoning_values": values, "supports_function_calling": True,
        "source": "litellm",
    }


EFFORT = _caps("effort", ["low", "medium", "high"])
NO_REASONING = _caps(None)
#: Every row unanswered — the state the page opens in, and the state a single
#: failed lookup stays in for as long as the tab is open.
UNANSWERED = {a: None for a in AGENTS}
STORED_HIGH = {"contract": {"reasoning": "high"}}

CASES = [
    # ── the defect this file exists for ──
    {"name": "unanswered_keeps_reasoning", "agents": AGENTS,
     "drafts": _drafts(), "stored": STORED_HIGH, "caps": UNANSWERED},
    {"name": "unanswered_keeps_budget", "agents": AGENTS, "drafts": _drafts(),
     "stored": {"contract": {"reasoning": 4096}}, "caps": UNANSWERED},
    {"name": "unanswered_keeps_ceiling_beside_it", "agents": AGENTS,
     "drafts": _drafts(contract={"maxOut": "32768"}),
     "stored": {"contract": {"max_output_tokens": 32768, "reasoning": "high"}},
     "caps": UNANSWERED},
    # ── the model belongs to the column, never to the blob ──
    {"name": "model_only_row_emits_no_entry", "agents": AGENTS,
     "drafts": _drafts(contract={"model": "gpt-4o"}), "stored": {},
     "caps": {**UNANSWERED, "contract": EFFORT}},
    {"name": "model_beside_reasoning_emits_reasoning_alone", "agents": AGENTS,
     "drafts": _drafts(contract={"model": "gpt-4o", "reasoning": "high"}),
     "stored": {}, "caps": {**UNANSWERED, "contract": EFFORT}},
    # ── the states where the form is in charge ──
    {"name": "typed_ceiling_is_a_number", "agents": AGENTS,
     "drafts": _drafts(contract={"maxOut": "32768"}), "stored": {},
     "caps": {**UNANSWERED, "contract": EFFORT}},
    {"name": "operator_cleared_the_reasoning", "agents": AGENTS,
     "drafts": _drafts(), "stored": STORED_HIGH,
     "caps": {**UNANSWERED, "contract": EFFORT}},
    {"name": "blank_form_emits_nothing", "agents": AGENTS, "drafts": _drafts(),
     "stored": {}, "caps": {a: EFFORT for a in AGENTS}},
    # ── the one deliberate deletion ──
    {"name": "answered_no_reasoning_drops_it", "agents": AGENTS,
     "drafts": _drafts(), "stored": STORED_HIGH,
     "caps": {**UNANSWERED, "contract": NO_REASONING}},
]


@pytest.fixture(scope="module")
def saved(tmp_path_factory) -> dict[str, dict]:
    if not TSC.exists():
        pytest.skip("web deps not installed")
    return _run(CASES, tmp_path_factory.mktemp("policy-agents"))


def test_the_harness_actually_ran(saved) -> None:
    """Guards the guard: a harness that silently produced nothing would make
    every assertion below vacuously true."""
    assert set(saved) == {c["name"] for c in CASES}, saved


def test_an_unanswered_lookup_keeps_the_stored_reasoning(saved) -> None:
    """The whole point. The page opens with every lookup in flight; a save in
    that window must not be a deletion."""
    assert saved["unanswered_keeps_reasoning"] == {"contract": {"reasoning": "high"}}, (
        "the map omitted `reasoning`, and it REPLACES the stored one — so this "
        "press deletes an override the operator never touched and cannot see, "
        "on the layer that outranks every other"
    )


def test_a_stored_budget_survives_as_a_number(saved) -> None:
    """4096, not "4096": the resolver reads an int and the provider is sent a
    thinking budget, so a string here would be a different setting."""
    assert saved["unanswered_keeps_budget"] == {"contract": {"reasoning": 4096}}


def test_the_ceiling_and_the_reasoning_survive_together(saved) -> None:
    entry = saved["unanswered_keeps_ceiling_beside_it"]["contract"]
    assert entry == {"max_output_tokens": 32768, "reasoning": "high"}


@pytest.mark.parametrize(
    "case", ["model_only_row_emits_no_entry",
             "model_beside_reasoning_emits_reasoning_alone"],
)
def test_the_model_never_enters_the_override_blob(saved, case) -> None:
    """One field, one home. The model of this layer is the `<agent>_model`
    column; the router 422s an entry that carries one, and it is right to —
    the resolver reads the column first, so a copy in the blob would be the
    one that looks authoritative and never applies."""
    for agent, entry in saved[case].items():
        assert "model" not in entry, f"{agent}: {entry}"


def test_a_model_only_override_creates_no_entry(saved) -> None:
    """An empty entry is not "set to nothing", it is "no override"."""
    assert saved["model_only_row_emits_no_entry"] == {}


def test_a_typed_ceiling_is_sent_as_a_number(saved) -> None:
    assert saved["typed_ceiling_is_a_number"] == {"contract": {"max_output_tokens": 32768}}


def test_clearing_an_editable_row_clears_the_override(saved) -> None:
    """The one omission that is a feature: an empty box on a model that CAN
    take a value is how the operator says "stop overriding this"."""
    assert saved["operator_cleared_the_reasoning"] == {}


def test_a_blank_form_overrides_nothing(saved) -> None:
    assert saved["blank_form_emits_nothing"] == {}


def test_an_answered_no_still_drops_the_value(saved) -> None:
    """The narrow destructive branch, kept narrow: LiteLLM names no parameter
    for this model, the server 422s a stored one, and the row says out loud
    that saving removes it. Widening this back to "caps is falsy" is the bug."""
    assert saved["answered_no_reasoning_drops_it"] == {}


# ─── The ceiling the form refuses before the server has to ───────────
#
# The Save button on the policy page is disabled while any row's ceiling is out
# of range or above what the model accepts, because that is a 422 the operator
# would otherwise meet after the press, on whichever tab they happened to be
# on. `agentMaxOutError` is the whole of that decision, and it is now shared
# with /settings/llm — so a change here changes both screens at once.


def _ceiling_harness(cases: list[dict]) -> str:
    controls = _strip_comments(CONTROLS.read_text(encoding="utf-8"))
    lifted = "\n\n".join(
        _lift(n, controls, CONTROLS)
        for n in ("agentCeiling", "agentMaxOutError")
    )
    return (
        "type ModelCapabilities = any;\n"
        f"const AGENT_TOKENS_MIN = {_ts_const('AGENT_TOKENS_MIN')};\n"
        f"const AGENT_TOKENS_MAX = {_ts_const('AGENT_TOKENS_MAX')};\n\n"
        + lifted
        + "\n\nconst cases: any[] = "
        + json.dumps(cases)
        + ";\n"
        "const out = cases.map((c) => ({\n"
        "  name: c.name, got: agentMaxOutError(c.raw, c.caps) ?? 'ok',\n"
        "}));\n"
        "console.log(JSON.stringify(out));\n"
    )


CEILING_CASES = [
    {"name": "empty_is_inherit", "raw": "", "caps": _caps("effort", ["low"])},
    {"name": "inside_the_model_ceiling", "raw": "32768", "caps": _caps("effort", ["low"])},
    {"name": "above_the_model_ceiling", "raw": "70000", "caps": _caps("effort", ["low"])},
    {"name": "above_the_model_ceiling_unanswered", "raw": "70000", "caps": None},
    {"name": "zero", "raw": "0", "caps": _caps("effort", ["low"])},
    {"name": "not_a_number", "raw": "abc", "caps": _caps("effort", ["low"])},
    {"name": "above_the_server_bound", "raw": "300000", "caps": None},
]


@pytest.fixture(scope="module")
def ceiling(tmp_path_factory) -> dict[str, str]:
    if not TSC.exists():
        pytest.skip("web deps not installed")
    tmp_path = tmp_path_factory.mktemp("ceiling")
    ts = tmp_path / "ceiling.ts"
    ts.write_text(_ceiling_harness(CEILING_CASES), encoding="utf-8")
    build = subprocess.run(
        [str(TSC), str(ts), "--target", "es2020", "--module", "commonjs",
         "--outDir", str(tmp_path)],
        capture_output=True, text=True, timeout=300,
    )
    js = tmp_path / "ceiling.js"
    if not js.exists():  # pragma: no cover - tsc itself failed
        pytest.fail(f"tsc emitted nothing:\n{build.stdout}\n{build.stderr}")
    run = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    return {row["name"]: row["got"] for row in json.loads(run.stdout)}


def test_the_ceiling_harness_actually_ran(ceiling) -> None:
    assert set(ceiling) == {c["name"] for c in CEILING_CASES}, ceiling


def test_a_ceiling_the_model_cannot_take_is_refused(ceiling) -> None:
    assert ceiling["above_the_model_ceiling"] == "over"
    assert ceiling["inside_the_model_ceiling"] == "ok"
    assert ceiling["empty_is_inherit"] == "ok"


def test_an_unanswered_lookup_does_not_block_the_save(ceiling) -> None:
    """Nothing is known about the model, so nothing can be said about the
    number — and an operator locked out of Save because one lookup blipped is
    worse off than one whose value simply went to the server."""
    assert ceiling["above_the_model_ceiling_unanswered"] == "ok"


@pytest.mark.parametrize("case", ["zero", "not_a_number", "above_the_server_bound"])
def test_a_value_outside_the_server_bounds_is_refused(ceiling, case) -> None:
    assert ceiling[case] == "range"


def test_the_form_bounds_are_the_server_bounds() -> None:
    """The two numbers live in two languages on purpose; this is the thread
    between them. If the server widens its range and the form does not, every
    value in the new band is refused by a screen that no longer needs to."""
    from src.api.routers.llm import AGENT_TOKENS_MAX, AGENT_TOKENS_MIN

    assert _ts_const("AGENT_TOKENS_MIN") == AGENT_TOKENS_MIN
    assert _ts_const("AGENT_TOKENS_MAX") == AGENT_TOKENS_MAX


# ─── What the form emits is what the API accepts ─────────────────────


def test_every_emitted_field_is_one_the_router_allows(saved) -> None:
    """Read off the router, not off memory: when that surface grows a fourth
    knob, this test grows with it instead of in the bug report after."""
    from src.api.routers.review_policies import _MODEL_FIELD, _agent_llm_fields

    allowed = set(_agent_llm_fields())
    assert _MODEL_FIELD not in allowed, "the router stopped banning the model"
    for name, emitted in saved.items():
        for agent, entry in emitted.items():
            unknown = set(entry) - allowed
            assert not unknown, f"{name}/{agent} emits {unknown}, allowed {allowed}"


def test_the_preserved_value_survives_the_server_too(saved) -> None:
    """End to end on the thing that broke: the form's map, through the router's
    own shaping, still holds the reasoning nobody touched."""
    from src.api.routers.review_policies import _agent_llm_overrides_from_payload

    emitted = saved["unanswered_keeps_reasoning"]
    stored = _agent_llm_overrides_from_payload(emitted, {"contract": {"reasoning": "high"}})
    assert stored == {"contract": {"reasoning": "high"}}


def test_an_omitted_map_is_what_keeps_a_blank_form_harmless() -> None:
    """The page sends `agent_llm_overrides: undefined` until the policy has
    loaded, because until then every draft is blank — and a blank map is the
    server's spelling of "clear every override", not "I have nothing to say"."""
    from src.api.routers.review_policies import _agent_llm_overrides_from_payload

    held = {"contract": {"reasoning": "high"}}
    assert _agent_llm_overrides_from_payload(None, held) == held
    assert _agent_llm_overrides_from_payload({}, held) == {}


def test_the_payload_the_form_builds_validates(saved) -> None:
    """`ReviewPolicyIn` forbids extra keys and types this one narrowly, so a
    shape this test blesses is a shape the PUT does not 422."""
    from src.api.schemas import ReviewPolicyIn

    body = ReviewPolicyIn(
        enabled=True,
        architect_model="gpt-4o",
        agent_llm_overrides=saved["unanswered_keeps_ceiling_beside_it"],
    )
    assert body.agent_llm_overrides == {
        "contract": {"max_output_tokens": 32768, "reasoning": "high"},
    }
