"""Pressing Save must not delete a reasoning override nobody touched.

THE BUG. /settings/llm builds the per-agent `agents` block from the form and
PUTs it WHOLE — the server replaces the stored map rather than patching it,
because absent is the only spelling of "stop overriding" the inheritance chain
has. So every field the form declines to send is a field the save DELETES.

`reasoning` was sent only when `takesReasoning(caps)` said the model accepts
one, and `caps` is null in three states that say nothing about the model:
the capabilities lookup in flight, the lookup errored (the query sets
`retry: false`, so one blip is final for as long as the page is open), and the
row having no model to ask about. Pressing Save in any of them wiped the
reasoning override for every affected agent — silently, with the Save button
happily enabled, because its disabled condition only ever covered the
max-output-tokens range check.

That is `gemini_thinking_budget` all over again: a value an operator
configured, still visible in the database, quietly gone after an unrelated
click. It is the exact failure this whole surface was built to end.

WHY THIS TEST RUNS THE REAL FUNCTION. The obvious guard here is a grep — and a
grep is what would have shipped the bug twice, because the file is full of
prose ABOUT `takesReasoning` and about the helper that used to weld a vendor
prefix on. A test that greps a name finds it in the comment explaining why the
name is gone. So the decision function is lifted out of the source verbatim,
transpiled by the web app's own `tsc`, and executed on node: what is asserted
is what the browser will run.

WHERE THE FUNCTION LIVES NOW. It began on /settings/llm and moved into
web/components/agent-llm-controls.tsx when the layer that WINS over that one —
a repository's review policy — grew the same three controls. Both screens now
PUT a map that replaces rather than patches, so both can delete a setting
nobody touched, and one function decides for both. The policy layer's own use
of it is pinned in test_the_repo_policy_keeps_a_setting_it_could_not_check.py.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
PAGE = WEB / "app" / "(app)" / "settings" / "llm" / "page.tsx"
#: Where the decision functions live, and the one file both screens import.
CONTROLS = WEB / "components" / "agent-llm-controls.tsx"
#: The layer that outranks /settings/llm and renders the same row.
POLICY = WEB / "app" / "(app)" / "admin" / "review-policies" / "[slug]" / "page.tsx"
TSC = WEB / "node_modules" / ".bin" / "tsc"

#: Everything `reasoningToSave` reaches for. Lifted whole, in this order, so
#: the harness compiles as one file.
LIFTED = ("AgentDraft", "reasoningValue", "takesReasoning",
          "storedReasoning", "reasoningToSave")


def _source(path: Path = PAGE) -> str:
    return path.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """The file minus its prose, quotes and template literals intact.

    Hand-rolled rather than regexed: a `//` inside a string and a `*/` inside a
    comment both break the one-liner, and the whole point of this helper is
    that a name surviving in a COMMENT must not count as the name surviving in
    the code.
    """
    out: list[str] = []
    i, n = 0, len(src)
    quote: str | None = None
    while i < n:
        ch = src[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j == -1 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j == -1 else j + 2
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _lift(name: str, src: str, where: Path = PAGE) -> str:
    """One top-level `function`/`type` declaration, braces balanced.

    Every declaration this test needs sits at column 0, so the opening line is
    unambiguous and the closing `}` is the first one back at column 0. The
    `export ` prefix is dropped rather than matched around: the harness
    compiles the lifted declarations as one script, and an `export` in it makes
    node's commonjs loader disagree with tsc's about what the file is.

    A type ends at the first `;` OUTSIDE its braces. Not the first `;` at all —
    `type AgentDraft = { model: string; … }` is three fields, and cutting at
    the first one leaves tsc a syntax error it reports while still emitting the
    JS this test then runs, which is how a broken lift passes quietly.
    """
    for opener in (f"\nexport function {name}(", f"\nfunction {name}(",
                   f"\nexport type {name} =", f"\ntype {name} ="):
        start = src.find(opener)
        if start != -1:
            break
    else:  # pragma: no cover - the assertion below reports it
        raise AssertionError(f"{name} is not declared at top level in {where.name}")
    start += 1
    if src.startswith("export ", start):
        start += len("export ")
    if src.startswith("type ", start):
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
    end = src.index("\n}", start) + 2
    return src[start:end]


def _harness(cases: list[dict]) -> str:
    src = _strip_comments(_source(CONTROLS))
    lifted = "\n\n".join(_lift(name, src, CONTROLS) for name in LIFTED)
    # The shapes come from web/lib/api.ts; aliasing them to `any` keeps this
    # file from becoming a second, drifting copy of those definitions — the
    # assertions below are about runtime behaviour, and types are erased
    # before any of it runs.
    return (
        "type ModelCapabilities = any;\n"
        "type AgentSettings = any;\n"
        "type AgentLLMOverride = any;\n\n"
        + lifted
        + "\n\nconst cases: any[] = "
        + json.dumps(cases)
        + ";\n"
        "const out = cases.map((c) => {\n"
        "  const r = reasoningToSave(c.draft, c.settings, c.caps);\n"
        "  return { name: c.name, got: r === undefined ? '<omitted>' : r };\n"
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
    run = subprocess.run(
        ["node", str(js)], capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 0, run.stderr
    return {row["name"]: row["got"] for row in json.loads(run.stdout)}


def _draft(reasoning: str = "") -> dict:
    return {"model": "", "maxOut": "", "reasoning": reasoning}


def _caps(kind: str | None, values: list[str] | None = None) -> dict:
    return {
        "model": "m", "known": kind is not None, "max_output_tokens": 16384,
        "supports_reasoning": kind is not None, "reasoning_kind": kind,
        "reasoning_values": values, "supports_function_calling": True,
        "source": "litellm",
    }


HIGH = {"reasoning": "high", "effective_model": "openai/gpt-5",
        "effective_max_output_tokens": 16384, "effective_reasoning": "high"}

CASES = [
    # ── the defect, in its three disguises ──
    {"name": "lookup_in_flight", "draft": _draft("high"),
     "settings": HIGH, "caps": None},
    {"name": "lookup_errored", "draft": _draft("high"),
     "settings": HIGH, "caps": None},
    {"name": "no_model_to_ask_about", "draft": _draft(""),
     "settings": HIGH, "caps": None},
    # A budget survives as the number it was stored as, not as "4096".
    {"name": "unresolved_budget", "draft": _draft(""),
     "settings": {**HIGH, "reasoning": 4096}, "caps": None},
    # Nothing stored: there is nothing to preserve and nothing to invent.
    {"name": "unresolved_nothing_stored", "draft": _draft(""),
     "settings": {"effective_model": "x", "effective_max_output_tokens": 1},
     "caps": None},
    {"name": "unresolved_no_entry_at_all", "draft": _draft(""),
     "settings": None, "caps": None},
    # ── the states where the form is in charge ──
    {"name": "editable_typed_word", "draft": _draft("high"),
     "settings": HIGH, "caps": _caps("effort", ["low", "high"])},
    {"name": "editable_cleared_by_operator", "draft": _draft(""),
     "settings": HIGH, "caps": _caps("effort", ["low", "high"])},
    {"name": "editable_budget_is_a_number", "draft": _draft("4096"),
     "settings": HIGH, "caps": _caps("budget")},
    # ── the one deliberate deletion ──
    {"name": "answered_model_cannot_reason", "draft": _draft("high"),
     "settings": HIGH, "caps": _caps(None)},
    {"name": "answered_effort_with_no_vocabulary", "draft": _draft("high"),
     "settings": HIGH, "caps": _caps("effort", [])},
]


@pytest.fixture(scope="module")
def decisions(tmp_path_factory) -> dict[str, object]:
    if not TSC.exists():
        pytest.skip("web deps not installed")
    return _run(CASES, tmp_path_factory.mktemp("reasoning"))


def test_the_harness_actually_ran(decisions) -> None:
    """Guards the guard: a harness that silently produced nothing would make
    every assertion below vacuously true."""
    assert set(decisions) == {c["name"] for c in CASES}, decisions


@pytest.mark.parametrize(
    "case", ["lookup_in_flight", "lookup_errored", "no_model_to_ask_about"],
)
def test_an_unanswered_lookup_keeps_the_stored_value(decisions, case) -> None:
    assert decisions[case] == "high", (
        f"{case}: the save omitted `reasoning`, and the agents block REPLACES "
        f"the stored map — so this press deletes an override the operator "
        f"never touched and cannot even see. Got {decisions[case]!r}."
    )


def test_a_stored_budget_survives_as_a_number(decisions) -> None:
    assert decisions["unresolved_budget"] == 4096


@pytest.mark.parametrize(
    "case", ["unresolved_nothing_stored", "unresolved_no_entry_at_all"],
)
def test_nothing_is_invented_when_nothing_was_stored(decisions, case) -> None:
    assert decisions[case] == "<omitted>"


def test_an_editable_row_sends_what_the_operator_typed(decisions) -> None:
    assert decisions["editable_typed_word"] == "high"


def test_clearing_an_editable_row_clears_the_override(decisions) -> None:
    """The one omission that is a feature: an empty box on a model that CAN
    take a value is how the operator says "stop overriding this"."""
    assert decisions["editable_cleared_by_operator"] == "<omitted>"


def test_a_budget_is_sent_as_a_number(decisions) -> None:
    assert decisions["editable_budget_is_a_number"] == 4096


@pytest.mark.parametrize(
    "case", ["answered_model_cannot_reason", "answered_effort_with_no_vocabulary"],
)
def test_an_answered_no_still_drops_the_value(decisions, case) -> None:
    """The narrow destructive branch, kept narrow on purpose: LiteLLM names no
    parameter for this model, the server 422s a stored one, and the row says
    out loud that saving removes it. Widening this back to "caps is falsy" is
    the bug."""
    assert decisions[case] == "<omitted>"


# ─── The vendor prefix the page must NOT re-derive ────────────────────


@pytest.mark.parametrize("path", [CONTROLS, PAGE, POLICY], ids=lambda p: p.parent.name)
def test_no_screen_welds_a_vendor_prefix_onto_a_model_id(path: Path) -> None:
    """DEFECT 1's web half, and why this assertion is written twice.

    `withVendorPrefix(model, reviewProvider)` glued the REVIEW profile's vendor
    onto whatever bare id a row carried: an operator on a Gemini review profile
    who pointed one agent at "gpt-4o" had "gemini/gpt-4o" looked up, which
    LiteLLM has no entry for. The row then said "unrecognised model", hid the
    ceiling and disabled reasoning — for a model the RUNTIME routes to OpenAI
    with the OpenAI key and that LiteLLM knows perfectly well.

    The prose explaining that deletion still names the helper, so the first
    assertion proves the comment stripper works before the second one leans on
    it. Without that pair, this test passes on a file that never changed.

    All three files, because the temptation is per-screen: the repo policy page
    also chooses a model, also asks what it supports, and would have to invent
    the same wrong prefix to do it in TypeScript.
    """
    raw = _source(path)
    if path == CONTROLS:
        assert "withVendorPrefix" in raw, (
            "the note explaining why there is no vendor-prefix helper is gone — "
            "without it the check below cannot tell a working stripper from a "
            "broken one"
        )
    code = _strip_comments(raw)
    for banned in ("withVendorPrefix", "LITELLM_PREFIX"):
        assert banned not in code, (
            f"{banned} is live code again in {path.name}: a screen is "
            f"re-deriving a LiteLLM model string in TypeScript instead of "
            f"handing the id to GET /model-capabilities, which is how the "
            f"screen and the runtime disagreed about a model in the first place"
        )


def test_the_capabilities_endpoint_resolves_a_bare_id_by_itself() -> None:
    """What makes deleting the web twin safe rather than merely tidier.

    The page now sends "gpt-4o" untouched. That is only correct because
    LiteLLM resolves a bare id itself — so this pins the fact the deletion
    rests on, next to the deletion.
    """
    from src.llm.capabilities import model_capabilities

    bare = model_capabilities("gpt-4o")
    assert bare.known, "a bare OpenAI id must resolve without the page's help"
    assert bare.max_output_tokens == 16384
    assert bare.max_output_tokens == model_capabilities("openai/gpt-4o").max_output_tokens

    welded = model_capabilities("gemini/gpt-4o")
    assert not welded.known, (
        "the shape the deleted helper produced is still unknown to LiteLLM — "
        "if this ever starts resolving, the story in this file needs rewriting"
    )


def test_the_row_asks_about_the_model_string_it_was_given() -> None:
    """The positive half: the query is keyed on the raw override or the
    server-resolved `effective_model`, with nothing in between."""
    code = _strip_comments(_source())
    start = code.index("const effectiveModels = REVIEW_AGENTS.map(")
    body = code[start:code.index(");", start)]
    assert "draft[agent].model.trim()" in body, body
    assert "effective_model" in body, body
    # Nothing else may be CALLED on the way out. `.trim()` is the one allowed
    # call (whitespace is not part of a model id); any other pair of parens in
    # the arrow body is a transformation, which is the bug coming back.
    calls = body.split("=>", 1)[1].replace(".trim()", "")
    assert "(" not in calls, (
        "something is transforming the model id between the form and the "
        "capabilities call:\n" + textwrap.indent(body, "    ")
    )
