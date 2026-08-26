"""Nobody was asked about races, so nobody found any.

Measured on the Martian Code Review Bench development subset (14 PRs, judge
claude-sonnet-4-5): TP 24 / FP 31 / FN 29. All 29 missed golden findings were
then read by hand, and three of them were concurrency defects — an async
`findMembers` whose result is never awaited, a race on a device count, and a
concurrent mutation of `backupCodes`. Nothing in the review package asked for
that shape: "race" and "TOCTOU" appear nowhere under src/review.

What is pinned here is BEHAVIOUR, not a word in a file: each test runs the real
`ArchitectAgent.review()` against an LLMClient double and reads the string that
actually reached `generate(system_instruction=...)` — the composed prompt,
after the override/extras resolution the agent redoes on every call. Four
claims, in the order they can fail:

  * the two concurrency shapes reach the model, in the FIRST (defect) list and
    not in the SECOND (architecture) section, whose findings the same prompt
    caps at "warning";
  * a narrated interleaving is still demanded — the shapes were cut to one line
    each after the follow-up run, but the demand only moved: it is made once,
    for every architect finding, by the reasoning-forms block, and it is what
    keeps a model that starts seeing races everywhere from writing them down.
    The verifier drops a finding that states a risk instead of a consequence,
    so an un-narrated race would cost tokens and post nothing;
  * every instruction that was on the architect's list BEFORE this addition
    still arrives, because a nine-item list is exactly where a tenth item gets
    added by deleting a ninth;
  * a race reported the way the clause asks still clears the evidence gate and
    comes back as a Finding, reasoning and confidence intact.

The one-line form and the second admissible sentence shape that replaced the
paragraphs are pinned next door, in
`test_the_architect_can_state_a_defect_it_cannot_value_trace.py`.
"""

from __future__ import annotations

import json

import pytest

from src.review.agents.base import AVOID_LIST_PROMPT, FINDING_OUTPUT_FORMAT, AgentContext
from src.review.agents.defect import DefectAgent
from src.review.models import Hunk, PullRequest
from src.review.settings import AgentLLMSettings

#: A race written the way the new clause asks for it: both lines named, and
#: the other caller's move between them.
RACE_REPLY = json.dumps([{
    "reasoning": "src/foo.py line 1 reads the count and line 2 writes count+1; "
                 "a second request that reads between them stores the same value",
    "file": "src/foo.py", "line": 1, "severity": "error",
    "title": "device count is read and written on separate lines",
    "body": "b", "rule_id": "defect.check-then-act", "confidence": 0.75,
}])


def _flat(text: str) -> str:
    """Line wraps are layout, not meaning — compare the words."""
    return " ".join(text.split())


class _Client:
    """An LLMClient double that records what it was asked, then answers.

    The default answer is `[]` — the "nothing found" reply the output contract
    names — so `review()` runs to completion without a parse error and the
    recorded call is the one a real review would have made.
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
            content="@@ -1 +1,2 @@\n-count = read()\n+count = read()\n+write(count + 1)\n",
        )],
    )


def _context(client: _Client, agent: str) -> AgentContext:
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


def _defect_system_instruction() -> str:
    """Run the real agent and return the system prompt the model received."""
    agent = DefectAgent(passes=1)  # one call, so one prompt to read — the second pass is exercised in test_the_defect_agent_reads_the_diff_twice.py
    client = _Client()
    result = agent.review(_context(client, agent.name))
    assert result.error is None, result.error
    assert len(client.calls) == 1, "one call, so one prompt to read"
    return client.calls[0]["system_instruction"]


# ─── the shape nobody was asking for ─────────────────────────────────


def test_the_architect_asks_the_model_about_check_then_act():
    """The device-count race and the `backupCodes` mutation are both this
    shape: read, decide, write, with a second caller in between."""
    system = _flat(_defect_system_instruction())
    assert "check-then-act on state another request can reach between the two" in system
    assert "a count checked then incremented" in system
    assert "a set read then replaced" in system
    assert "a code marked used after it was looked up" in system


def test_the_race_clause_demands_the_interleaving_be_named():
    """The precision half, made once instead of per-clause.

    A model told to look for races finds them in every diff; a model that has
    to name the line, the path and what happens instead has to have an actual
    interleaving. The clause used to restate that demand in its own words and
    no longer does — it is the contract every architect finding answers, so a
    race is admissible on exactly the terms everything else is. It is also
    what survives the verifier, which drops a finding stating a risk rather
    than a consequence of running the code.
    """
    system = _flat(_defect_system_instruction())
    assert "the path that reaches it" in system
    assert "what that path now produces instead of" in system
    assert "If you cannot say which line, on which path, produces which wrong outcome" in system
    assert "you do not have the finding" in system


def test_a_callee_this_pr_made_async_is_a_named_shape():
    """The missed `findMembers` was not a stray `await`: the callee became
    async in the PR and the call site was left as it was. The bullet already
    here described one call site and would not have matched that."""
    system = _flat(_defect_system_instruction())
    assert "an async call whose result is never awaited and is then used" in system
    assert "including a callee this PR made async under a caller it left as it was" in system


def test_the_concurrency_shapes_land_among_the_defects_not_the_design():
    """Placement, once asserted as position INSIDE one prompt, now asserted as
    an agent boundary. The architect prompt held a FIRST (defects) list and a
    SECOND (architecture) section, and this test pinned the clauses to the
    first; the restructure turned that ordering into two agents, so the same
    property is now: the concurrency clauses live in the DEFECT prompt, and
    the architecture remit — which reports at "warning" and only with a
    graph-named caller — is not in that prompt at all to swallow them."""
    system = _flat(_defect_system_instruction())
    for clause in (
        "check-then-act on state another request can reach",
        "including a callee this PR made async",
    ):
        assert clause in system, clause
    assert "SECOND — architecture" not in system
    assert "cross-repo impact" not in system.lower()


# ─── nothing was displaced to make room ──────────────────────────────


PRE_EXISTING_DEFECT_SHAPES = [
    "copy-paste that survived review",
    "a branch that can never be taken because an earlier condition already covers it",
    "an off-by-one in a slice, index or range boundary",
    "a literal where a variable belongs",
    "a call that assumes one platform: shell flags, path separators",
    "an argument in the wrong position, a wrong unit, a wrong sign",
    "a library used with syntax that is not valid for it",
    "state mutated inside a loop that also decides the loop's exit",
]

#: Instructions that lived in the architect's prompt and now live in the
#: defect agent's — the severity grammar and the runs-based bar came over
#: with the defect list they graded.
PRE_EXISTING_DEFECT_INSTRUCTIONS = [
    "critical — data loss, a bypassed check, a crash on a request path",
    'If a finding cannot be stated as "when X runs, Y happens instead of Z"',
]

#: And the ones that moved to the CONTRACT agent, quoted in its wording.
#: The old phrasings ("deterministic grep-based factual signal", "only when
#: the graph shows a caller that will now fail") were rewritten when the
#: remit moved; what is pinned is that each obligation still exists SOMEWHERE
#: — the drift mandate, the named-caller bar, the caller's-line rule — not
#: the sentence that once carried it. A test keyed on the sentence is how
#: three tests died in acefcae.
MOVED_TO_CONTRACT = [
    "deterministic grep, not a model's guess",
    "you MUST write a finding",
    "a caller the graph can name",
    "goes on the CALLER's line",
    "quote both sides",
]


@pytest.mark.parametrize(
    "instruction", PRE_EXISTING_DEFECT_SHAPES + PRE_EXISTING_DEFECT_INSTRUCTIONS,
)
def test_the_defect_agent_still_asks_for_everything_the_list_held(instruction):
    assert instruction in _flat(_defect_system_instruction())


@pytest.mark.parametrize("obligation", MOVED_TO_CONTRACT)
def test_the_architecture_remit_moved_whole_not_away(obligation):
    """The split must not quietly drop the other half. Every cross-file
    obligation the architect carried — the drift mandate above all, the one
    finding that is deterministic and mandatory — has its home in the
    contract prompt."""
    from src.review.agents.contract import ContractAgent

    assert obligation in _flat(ContractAgent.system_prompt), obligation


def test_the_evidence_contract_still_frames_the_answer_exactly_once():
    """The addition is a bullet inside the role, so the shared blocks the
    composition splices in — the output contract and the avoid-list — must be
    untouched, whatever agent-local text sits between them. Two copies of the
    contract would be two orderings of the same fields."""
    system = _defect_system_instruction()
    assert system.count(FINDING_OUTPUT_FORMAT.strip()) == 1
    assert system.count(AVOID_LIST_PROMPT) == 1
    flat = _flat(system)
    assert flat.index('"reasoning"') < flat.index('"file"') < flat.index('"confidence"')


# ─── and the finding it asks for is one the pipeline accepts ─────────


def test_a_narrated_race_clears_the_evidence_gate():
    """The clause asks for a longer reasoning sentence than the rest of the
    list does — two line numbers and a second caller. That has to arrive as a
    Finding, not as a claim the parser refuses: `_dict_to_finding` drops
    anything with no `reasoning` or a file outside the PR, and a lever that
    produced only dropped claims would raise the token bill and nothing else.
    """
    agent = DefectAgent(passes=1)  # one call, so one prompt to read — the second pass is exercised in test_the_defect_agent_reads_the_diff_twice.py
    result = agent.review(_context(_Client(RACE_REPLY), agent.name))

    assert result.dropped_no_evidence == 0
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.file_path == "src/foo.py"
    assert "a second request that reads between them" in finding.reasoning
    assert finding.confidence == 0.75
