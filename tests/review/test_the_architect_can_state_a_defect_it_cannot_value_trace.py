"""Some defects have no wrong value to point at, and the architect lost them.

Measured on the Martian Code Review Bench development subset (14 PRs, judge
claude-sonnet-4-5), comparing the run before the `reasoning` field existed with
the run after it became a parse-time requirement:

  * the gate changed the shape it was meant to change — every architect finding
    carrying a reasoning sentence named a numbered line in it, 20 of 20, where
    only 11 of 32 architect comment bodies in the earlier run cited a numbered
    line anywhere;
  * and it cost two goldens. `postMessage` handed the full referer where the
    HTML messaging contract takes an origin, and an `Authenticate` that returns
    ErrDeviceLimitReached where device tagging used to be asynchronous, were
    both written by the architect in the earlier run and by no agent in the
    later one. Neither is "value V is wrong at line N": every value on those
    lines is the one the author intended, and the decision is what is wrong, so
    the example the shared contract gives — "cfg can be nil on line 42;
    dereferenced without a check on line 47" — has no sentence to offer them.

At this operating point (golden count 53, so F2 = 5·TP / (TP + FP + 212)) one
recovered true positive is worth +1.79 F2 and one removed false positive
+0.16: a second admissible sentence form pays for itself at any precision
above 8%.

What is pinned here is BEHAVIOUR. Every prompt assertion reads the string that
actually reached `generate(system_instruction=...)` after the real
override/extras resolution, so no assertion can be satisfied by a comment in a
source file; and the two gate tests run the reply through the real parser.
"""

from __future__ import annotations

import json
import re

import pytest

from src.review.agents.base import AVOID_LIST_PROMPT, FINDING_OUTPUT_FORMAT, AgentContext
from src.review.agents.defect import DefectAgent
from src.review.agents.security import SecurityAgent
from src.review.models import Hunk, PullRequest
from src.review.settings import AgentLLMSettings

#: The grafana golden, written as the second form permits: a changed line, the
#: path that reaches it, and the outcome that path now produces instead of the
#: one it produced before. There is no wrong value anywhere in it — `err` is
#: exactly what the callee returned.
BEHAVIOUR_REPLY = json.dumps([{
    "reasoning": "when an anonymous request arrives after the device limit is reached, "
                 "TagDevice returns ErrDeviceLimitReached on line 1 and Authenticate "
                 "returns it on line 2 instead of the identity, so a user the "
                 "asynchronous tagging used to admit is now rejected",
    "file": "src/foo.py", "line": 2, "severity": "error",
    "title": "device limit now refuses anonymous authentication",
    "body": "b", "rule_id": "arch.fatal-error-path", "confidence": 0.8,
}])

#: The same claim with the sentence left off. The parser decides this, not a
#: model, and it has to keep deciding it — a second admissible form that also
#: admitted findings with no sentence at all would be a removed gate wearing
#: the name of a widened one.
NO_SENTENCE_REPLY = json.dumps([{
    "reasoning": "",
    "file": "src/foo.py", "line": 2, "severity": "error",
    "title": "device limit now refuses anonymous authentication",
    "body": "b", "rule_id": "arch.fatal-error-path", "confidence": 0.8,
}])


def _flat(text: str) -> str:
    """Line wraps are layout, not meaning — compare the words."""
    return " ".join(text.split())


class _Probe:
    """An LLMClient double that records what it was asked, then answers.

    The default answer is `[]` — the "nothing found" reply the output contract
    names — so `review()` runs to completion and the recorded call is the one a
    real review would have made.
    """

    def __init__(self, reply: str = "[]") -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        from src.llm.client import LLMResult

        return LLMResult(
            text=self.reply, input_tokens=10, output_tokens=2, model="double",
            finish_reason="stop", cost_usd=0.0, cost_source="litellm_estimate",
            provider="gemini",
        )


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="d",
        author="alice", base_ref="main", base_sha="a", head_ref="feat",
        head_sha="b", state="open",
        hunks=[Hunk(
            file_path="src/foo.py", old_file_path="src/foo.py",
            old_start=1, old_count=1, new_start=1, new_count=2,
            content="@@ -1 +1,2 @@\n-go(ctx)\n+err := tag(ctx)\n+return nil, err\n",
        )],
    )


def _context(client: _Probe, agent: str) -> AgentContext:
    return AgentContext(
        pull_request=_pr(), llm_client=client,
        agent_llm={agent: AgentLLMSettings(model="double", max_output_tokens=1000)},
    )


@pytest.fixture(autouse=True)
def _default_install(monkeypatch):
    """No workspace override, no workspace extras.

    `_compose_effective_system_prompt` asks for both before it composes, and on
    a developer machine with a workspace configured they would replace or
    extend the prompt with text this test did not write. Answering "nothing
    configured" is what a default install returns, and it makes the recorded
    system instruction depend only on the agent module.
    """
    import src.api.routers.agents as agents_router
    import src.api.routers.llm as llm_router

    monkeypatch.setattr(
        agents_router, "get_effective_system_prompt", lambda *a, **k: None,
    )
    monkeypatch.setattr(llm_router, "_load_workspace_config", lambda *a, **k: {})


def _system_instruction(agent) -> str:
    """Run the real agent and return the system prompt the model received."""
    client = _Probe()
    result = agent.review(_context(client, agent.name))
    assert result.error is None, result.error
    assert len(client.calls) == 1, "one call, so one prompt to read"
    return client.calls[0]["system_instruction"]


def _defect_system_instruction() -> str:
    return _system_instruction(DefectAgent(passes=1))


# ─── two forms arrive, and they are two ──────────────────────────────


def test_both_reasoning_forms_reach_the_model():
    """The value trace keeps its place and the behaviour trace joins it.

    Asserted structurally rather than by phrase: the prompt has to offer two
    labelled alternatives, the first about a value and the second about what a
    path produces, and each has to carry a worked example. One form dressed up
    as two would fail on the second half.
    """
    flat = _flat(_defect_system_instruction())
    a = flat.index("(a)")
    b = flat.index("(b)", a)
    form_a, form_b = flat[a:b], flat[b:]

    assert "VALUE" in form_a and "BEHAVIOUR" in form_b
    # Each form shows the sentence it wants, not just names it.
    assert re.search(r'"[^"]*line \d+[^"]*"', form_a), form_a[:200]
    assert re.search(r'"[^"]*line \d+[^"]*"', form_b), form_b[:200]


def test_the_second_form_asks_for_a_path_and_an_outcome_the_first_does_not():
    """What form (b) buys, in the two words form (a) has no room for.

    The grafana and postMessage goldens are both "on this path, Y happens
    instead of Z"; neither has a value to trace. If the second form did not ask
    for the path and the substitution, it would be the first form with a longer
    label.
    """
    flat = _flat(_defect_system_instruction())
    form_b = flat[flat.index("(b)"):]
    head = form_b[:form_b.index("Form (b) exists")]

    assert "the path that reaches it" in head
    assert "instead of" in head


def test_the_second_form_names_why_the_first_cannot_hold_these_defects():
    """The reason has to travel with the licence, or the model reads the two
    forms as a style choice. The first two cases it names are the two goldens
    this product matched before the reasoning field existed and missed after
    it — the grafana error path and the postMessage argument."""
    flat = _flat(_defect_system_instruction()).lower()
    assert "some defects have no wrong value to point at" in flat
    assert "an error path that now rejects a caller it used to admit" in flat
    assert "an api given an argument its contract does not accept" in flat


# ─── and the evidence demand is not what was widened ─────────────────


def test_the_second_form_still_demands_a_line_a_path_and_a_read_outcome():
    """Widening the admissible shape must not widen the admissible evidence.

    The refusal has to be stated of form (b) in form (b)'s own terms —
    speculation is exactly the failure mode a licence to describe behaviour
    invites — and it has to end where the shared contract ends: write nothing.
    """
    flat = _flat(_defect_system_instruction())
    form_b = flat[flat.index("Form (b) is not a licence to speculate"):]

    assert "demands the same three things" in form_b
    assert '"may under load"' in form_b
    assert "you do not have the finding" in form_b
    assert "write nothing" in form_b


def test_a_behaviour_trace_clears_the_evidence_gate():
    """The sentence the new form permits has to arrive as a Finding.

    `_dict_to_finding` drops anything with no `reasoning` or a file outside the
    PR. A form that produced only dropped claims would raise the token bill and
    post nothing.
    """
    agent = DefectAgent(passes=1)  # one call, so one prompt to read — the second pass is exercised in test_the_defect_agent_reads_the_diff_twice.py
    result = agent.review(_context(_Probe(BEHAVIOUR_REPLY), agent.name))

    assert result.dropped_no_evidence == 0
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.file_path == "src/foo.py"
    assert "instead of the identity" in finding.reasoning
    assert finding.confidence == 0.8


def test_a_finding_with_no_sentence_is_still_refused():
    """The gate the second form rides on. Two admissible sentences, still no
    admissible silence: the parser drops a finding with no reasoning, and that
    is what makes "the sentence you cannot finish is the finding you do not
    have" true rather than advisory."""
    agent = DefectAgent(passes=1)  # one call, so one prompt to read — the second pass is exercised in test_the_defect_agent_reads_the_diff_twice.py
    result = agent.review(_context(_Probe(NO_SENTENCE_REPLY), agent.name))

    assert result.findings == []
    assert result.dropped_no_evidence == 1


# ─── paid for by the architect, not by security ──────────────────────


def test_the_forms_are_defect_local_and_do_not_reach_security():
    """The reason this block is not in FINDING_OUTPUT_FORMAT.

    That constant feeds every agent, and on the same 14 PRs all 13 security
    findings named a numbered line in their reasoning sentence. Widening the
    shared contract would spend that shape to buy the defect agent's recall,
    so the security prompt must not carry the second form.
    """
    security = _flat(_system_instruction(SecurityAgent()))
    defect = _flat(_defect_system_instruction())

    assert "A BEHAVIOUR traced through a path" in defect
    assert "A BEHAVIOUR traced through a path" not in security
    assert "Form (b) is not a licence to speculate" not in security
    # The shared half is shared: both agents still get the same contract.
    assert _flat(FINDING_OUTPUT_FORMAT) in security
    assert _flat(FINDING_OUTPUT_FORMAT) in defect


def test_the_shared_constants_arrive_verbatim_and_once():
    """Nothing here edits base.py. Both shared blocks must appear exactly once
    and unmodified, and the field order the parser reads must survive — two
    copies of the contract would be two orderings of the same fields."""
    system = _defect_system_instruction()

    assert system.count(FINDING_OUTPUT_FORMAT.strip()) == 1
    assert system.count(AVOID_LIST_PROMPT) == 1

    flat = _flat(system)
    assert flat.index('"reasoning"') < flat.index('"file"') < flat.index('"confidence"')


def test_the_forms_block_sits_between_the_contract_it_widens_and_the_avoid_list():
    """Position is meaning here. Read apart from the contract that requires a
    reasoning sentence, two forms have nothing to be forms OF."""
    flat = _flat(_defect_system_instruction())

    contract = flat.index(_flat(FINDING_OUTPUT_FORMAT))
    forms = flat.index("Two sentence forms satisfy")
    avoid = flat.index(_flat(AVOID_LIST_PROMPT))
    assert contract < forms < avoid


# ─── the concurrency shapes are shorter, not gone ────────────────────


def test_both_concurrency_shapes_are_still_named_among_the_defects():
    """Two of the four concurrency goldens on this subset are still open, and
    at +1.79 F2 each they are worth keeping a shape for. They sit in the
    defect agent's list; the contract agent — whose remit caps at "warning"
    with a graph-named caller, which a lost write is not — must not be where
    they landed."""
    flat = _flat(_defect_system_instruction())
    for shape in (
        "check-then-act on state another request can reach between the two",
        "including a callee this PR made async under a caller it left as it was",
    ):
        assert shape in flat, shape

    from src.review.agents.contract import ContractAgent
    contract = _flat(ContractAgent.system_prompt)
    assert "check-then-act" not in contract


def test_the_check_then_act_shape_still_names_what_to_look_for():
    """The clause is one line now, but the examples are what made it findable:
    a bare "check-then-act" is a topic heading, and the goldens are a device
    count and a single-use code."""
    flat = _flat(_defect_system_instruction())
    assert "a count checked then incremented" in flat
    assert "a set read then replaced" in flat
    assert "a code marked used after it was looked up" in flat


def test_the_two_concurrency_clauses_no_longer_dominate_the_defect_list():
    """Measured, and the reason for the cut. In the long form these two of ten
    items took eleven of the defect list's twenty-four lines, and across the 14
    PRs they matched neither of the two goldens they name verbatim while the
    concurrency findings the architect did write were both judged false
    positives. Held here as a share of the list the model reads, so the
    paragraphs cannot creep back a sentence at a time.
    """
    system = _defect_system_instruction()
    start = system.index("The kinds that hide well")
    # The list ends where the per-line sweep begins. That heading replaced
    # a one-line "Sweep EVERY changed line…" when the sweep was widened into
    # the security agent's proven form; the bullets it bounds are unchanged.
    end = system.index("HOW TO READ THE DIFF")
    bullets = [
        line for line in system[start:end].splitlines()
        if line.strip()
    ][1:]

    joined = "\n".join(bullets)
    # Twelve now, not ten: the merge brought quality's swallowed-exception /
    # leak / mutable-default shape and tests' untested-branch shape into the
    # one list. The dominance bound below is what this test defends.
    assert joined.count("      - ") == 12, "twelve shapes after the merge"

    def _clause(marker: str) -> str:
        i = joined.index(marker)
        j = joined.find("\n      - ", i)
        return joined[i:] if j == -1 else joined[i:j]

    concurrency = (
        _clause("      - check-then-act")
        + _clause("      - an async call whose result is never awaited")
    )
    assert len(concurrency) < 0.30 * len(joined), (
        f"{len(concurrency)} of {len(joined)} chars is back to dominating the list"
    )


# ─── nothing was displaced to make room ──────────────────────────────


#: What stayed in the defect list through the restructure. The architecture
#: half of the old table moved to the contract agent and is pinned — against
#: the obligations, not the sentences — in test_nobody_was_looking_for_a_race
#: (MOVED_TO_CONTRACT); the repo-rules mandate moved to the shared composer
#: in base.py, which appends custom_rules to EVERY agent.
PRE_EXISTING_INSTRUCTIONS = [
    "copy-paste that survived review",
    "a branch that can never be taken because an earlier condition already covers it",
    "an off-by-one in a slice, index or range boundary",
    "a literal where a variable belongs",
    "a call that assumes one platform: shell flags, path separators",
    "an argument in the wrong position, a wrong unit, a wrong sign",
    "a library used with syntax that is not valid for it",
    "state mutated inside a loop that also decides the loop's exit",
    "critical — data loss, a bypassed check, a crash on a request path",
    'If a finding cannot be stated as "when X runs, Y happens instead of Z"',
]


@pytest.mark.parametrize("instruction", PRE_EXISTING_INSTRUCTIONS)
def test_the_defect_agent_still_asks_for_everything_it_asked_for_before(instruction):
    assert instruction in _flat(_defect_system_instruction())


def test_the_restraint_clamp_survives_the_widening():
    """The last sentence of the severity block keeps an enumeration honest,
    and a prompt just told it may describe behaviour is exactly the prompt
    that starts enumerating. It stays, and it stays last.

    ITS NUMBERS DO NOT. The clamp read "a review of thirty observations and
    three defects is read as noise and closed", and a 50-PR benchmark then
    measured what the restraint framing cost: volume fell from 4.08 findings
    per PR to 2.08 — exactly halved — while precision per claim ROSE, so the
    loss was not judgement but length. F2 fell 44.2 → 34.5. A contrast
    between thirty and three, in a prompt, is read as a target however it is
    meant.

    So what is pinned is the clamp's JOB — an observation that is not a
    defect is withheld — plus the sentence that now says explicitly that this
    is a bar on quality and not a ceiling on count. Keyed on both halves,
    because dropping either one recreates a failure we have already measured:
    without the first the model enumerates, without the second it stops early.
    """
    flat = _flat(_defect_system_instruction())

    assert "An observation that is not a defect costs the defects beside it" in flat
    assert "NOT a limit on how many findings a review may carry" in flat
    assert "however many of them the diff turns out to contain" in flat

    clamp = "An observation that is not a defect"
    assert flat.index("Two sentence forms satisfy") < flat.index(clamp)


def test_nothing_in_the_prompt_reads_as_a_finding_quota():
    """The specific regression this file now guards. Each of these shipped at
    some point and each measurably suppressed volume."""
    flat = _flat(_defect_system_instruction())
    for banned in ("and not one more", "padding is visible",
                   "thirty observations", "right 100% of the time"):
        assert banned not in flat, f"the prompt still says {banned!r}"
