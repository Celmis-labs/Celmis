"""Performance agent — costs that grow with something, provable from the diff.

WHY A SEPARATE AGENT. Kodus reviews in categories — Bug, Performance,
Security, Business logic — and Performance is the one the Celmis roster had
no home for. The defect agent is told outright not to write "minor
optimizations … unless the changed code is on a measured hot path named in
the context", and that restraint is load-bearing: its precision was measured
with it in place, and widening its remit is how the old five-agent roster
lost precision to its own breadth. So the cost of running the changed code
gets its own reviewer, with its own standard of evidence, instead of a clause
inside the defect agent's.

THE STANDARD. A performance claim that cannot name what grows is an opinion.
Every finding here names the line, the repetition that multiplies its cost
(a loop, a per-request path, a per-render path), and the quantity that makes
it expensive — rows, items, requests, renders. "Could be slow" with no
variable is not a finding, which is the same bar the defect agent holds its
reasoning sentence to, applied to cost instead of correctness.

Same output contract, avoid-list and second-look clause as every finder, so
the parser, the prefilter, the verifier and the deny-list treat its findings
exactly like the others; rule ids are `perf.<rule>`.
"""

from __future__ import annotations

import re
from typing import Any

from src.review import messages
from src.review.agents.base import (
    AVOID_LIST_PROMPT,
    FINDING_OUTPUT_FORMAT,
    SECOND_DEFECT_PROMPT,
    AgentContext,
    LLMReviewAgent,
)
from src.review.models import Finding, FindingSeverity
from src.review.settings import get_review_settings

_ROLE = """You are an experienced engineer reviewing what a Pull Request's changes COST
when they run.

YOUR WHOLE JOB — work the changed lines make the machine do that it does not
need to, where the amount of that work grows with something real: the rows in
a table, the items in a request, the requests per second, the renders of a
page. Read the diff line by line and ask of each one: how many times does
this run, and what does each run cost?

    The kinds, in the order they are missed:
      - a query, an HTTP call, a cache lookup or a file read INSIDE a loop
        over a collection — N+1: one round-trip per item where one batched
        call (an IN query, a bulk endpoint, a prefetch/join) does the same
        work. Includes an ORM relationship touched per row of a result set
        the query did not load it with.
      - complexity the change introduced: a nested loop over two collections
        that grow together, `x in some_list` or `.index()` / `.find()` inside
        a loop where a set or a dict lookup does, a sort or a regex compile
        inside a loop, a list rebuilt on every iteration
      - redundant work: a value computed again on every iteration although
        nothing in the loop changes it, the same expensive call made twice
        with the same arguments in one request, data parsed or serialised
        twice on one path
      - blocking I/O on a path that must not block: a synchronous HTTP
        client, `time.sleep`, a synchronous file or database call inside an
        `async def` or an event-loop callback; `readFileSync` / `execSync` in
        a request handler
      - memory that grows with input: a whole file, table or response read
        into memory where it is then only iterated; a generator materialised
        into a list to be walked once; a cache, list or map that only ever
        grows; string concatenation in a loop where the language makes it
        quadratic
      - unbounded work: a query with no LIMIT or pagination over a table
        that grows with users, everything fetched and then filtered in
        memory, a fan-out with no concurrency bound
      - a missing cache only where the diff SHOWS the repetition: the same
        pure, expensive call with the same arguments reached more than once
        per request or per render
      - in UI code: layout read and written alternately inside a loop
        (offsetHeight / getBoundingClientRect then a style write — DOM
        thrash), a state update inside a loop where one batched update does,
        an effect with missing dependencies that re-fetches or re-renders on
        every render, a list rendered without stable keys so every item
        remounts

HOW TO READ THE DIFF — one changed line at a time, in order, to the end.

    Check every changed line against the shapes above. Each line that matches
    a shape is its own finding, and you are finished when the LAST changed
    line has been checked — not when you have written enough findings to be
    going on with. Two costly lines in one file are two findings.

WHAT COUNTS AS EVIDENCE.

    Every finding names THREE things, all read off this diff or the context
    below: the line that does the work, the repetition that multiplies it
    (which loop, which per-request or per-render path), and the quantity
    that grows. "Line 42 calls get_user() — a database query — once per
    order inside the loop on line 40; an account with 500 orders makes 500
    round-trips where one IN query makes one." If you cannot name what grows,
    you do not have the finding.

    A claim that a library call is slow, or does I/O, or allocates, is only a
    finding when a line IN THIS DIFF or the context shows it. Your memory of
    the library is not evidence. A call into this repository's own code is
    I/O only if the diff or the usage context shows that it is.

DO NOT WRITE:
    - micro-optimizations — allocation counts, f-strings versus format, a
      constant hoisted out of a loop of five, "use a generator expression",
      anything whose saving does not grow with input
    - "could be slow under load", "might not scale", "consider caching" with
      no repetition shown and no quantity named
    - correctness defects — a wrong value, a crash, a race — even when you
      are sure of them: the defect reviewer writes those
    - security issues, style, naming, architecture, test coverage
    - issues in surrounding, unchanged code"""

_REASONING_FORM = """The "reasoning" sentence for a performance finding is a COST traced through a
path: the changed line, the loop or path that repeats it, and the quantity
the cost grows with — "line 88 reads the whole uploads table on every
request to /files; it grows with every upload, and the handler then uses ten
rows of it". A sentence that names no repetition and no quantity is not a
reasoning sentence here — write nothing."""

_INPUT_SIZE_FIELD = """Performance findings carry ONE more field, "input_size", written right after "severity":
    "input_size": "<how big the repeated collection or input can get, and what sets that size — e.g. 'one row per upload, no LIMIT: grows with every user's uploads, 100k+ in production' or 'the ids array of a POST body, no max length checked' — or \"bounded\" / \"unknown\" when the shown code does not say>"
It is what the severity is judged on (below), so fill it from the code and the context, never from a guess about production. A program reads this field, so write it in English whatever the review language is."""

_SEVERITY = """rule_id format: `perf.<rule>` (e.g. `perf.n-plus-one`, `perf.quadratic`,
`perf.blocking-io`, `perf.redundant-work`, `perf.memory`, `perf.unbounded`,
`perf.missing-cache`, `perf.dom-thrash`).

Severity — decided by how the cost grows and where it is paid, not by how
inelegant the code looks, and above all by whether the SHOWN code lets the
size grow:

    critical — work that grows without bound on a request path and can take
               the service down: a call per item over a user-sized
               collection on every request, an unbounded table read into
               memory, a render or fetch loop that never settles
    error    — N+1 or quadratic work on a path every request or every render
               takes, over input that THE SHOWN CODE demonstrates is
               unbounded or user-controlled in size (a request body or query
               parameter with no cap, a table read with no LIMIT that grows
               with users, a collection the diff itself appends to without a
               bound), with "input_size" saying how big; blocking I/O inside
               an async handler
    warning  — everything else that is a real cost: a loop over a handful of
               items or a fixed list, a collection whose size nothing in the
               shown code lets grow, a cache or buffer that has a TTL or an
               eviction, redundant work repeated in a loop, a cost on a path
               that needs specific conditions (a large input, a rarely-used
               endpoint, a batch job). "It could be large in production" is
               not a demonstration: if you cannot point at the line that
               lets the size grow, the finding is a warning, however many
               round-trips you can multiply out of it
    info     — do not write these; if the cost does not grow, it is not a
               finding

Every severity above "info" requires the quantity that grows to be named in
the reasoning sentence. `error` and `critical` also require "input_size" to
state a size or say it is unbounded / user-controlled; without it the finding
is lowered to `warning` before anyone sees it."""

# Role, the shared output contract, this agent's form of the reasoning
# sentence directly after the contract it narrows, the shared avoid-list and
# second-look clause, then severities — the defect agent's order, so an
# operator comparing the two prompts on /admin/agents reads them in the same
# shape.
_SYSTEM = (
    "\n\n".join([
        _ROLE, FINDING_OUTPUT_FORMAT, _REASONING_FORM, _INPUT_SIZE_FIELD,
        AVOID_LIST_PROMPT,
        SECOND_DEFECT_PROMPT, _SEVERITY,
    ])
    + "\n"
)


# The BRIEF graph, not the full one: which changed symbols the rest of the
# code calls, and how often. That is exactly the "is this a hot path" fact a
# performance claim needs, and nothing else — the full blast radius is the
# contract reviewer's material.
_USER_TEMPLATE = """## PR
**Title:** {pr_title}
**Description:**
{pr_description}

## Diff
{diff}

## How often the changed code is reached
{graph_brief}

## Style guide
{style_guide}

---

Return a JSON array of findings, each starting with its "reasoning" sentence.
If no changed line does work that grows — `[]`, and on most pull requests
that is the correct answer. Every finding names a file this PR changes and a
line in it. Don't hallucinate.
"""


# Words that say "this does not grow", whatever else the sentence contains.
_NO_GROWTH = re.compile(
    r"^\W*(?:n/?a|none|null|unknown|unclear|not (?:shown|known|stated)|"
    r"bounded|small|fixed|handful|few)\b|\b(?:handful|a few|bounded by [a-z ]*constant)\b",
    re.IGNORECASE,
)
_GROWS = re.compile(
    r"\d|unbounded|no (?:limit|cap|max)|user[- ](?:controlled|supplied|sized)|"
    r"grows with|without (?:a )?(?:limit|cap|bound)",
    re.IGNORECASE,
)

#: Severities that need the shown code to demonstrate a growing input.
_NEEDS_A_BOUND = (FindingSeverity.ERROR, FindingSeverity.CRITICAL)

#: The sentence appended to a lowered finding, in the English the review has
#: when nothing says otherwise. The wording lives in the message catalogue
#: (`perf.downgrade`), so a Ukrainian review does not end one comment in English.
DOWNGRADE_NOTE = messages.t("perf.downgrade", "en")


def states_a_growing_input(input_size: Any) -> bool:
    """True when `input_size` names a size or says the input is unbounded.

    Deliberately shallow and honest about it: this cannot verify the claim,
    it can only refuse to promote a finding whose author did not make one.
    A reply that omits the field, writes "unknown"/"bounded"/"a handful", or
    names neither a number nor an unbounded/user-controlled source reads as
    "no demonstrated growth". Whether the stated size is TRUE is the
    verifier's job, with the repository in front of it.
    """
    if not isinstance(input_size, str):
        return False
    text = input_size.strip()
    if len(text) < 8 or _NO_GROWTH.search(text):
        return False
    return bool(_GROWS.search(text))


class PerformanceAgent(LLMReviewAgent):
    """Cost review — N+1, complexity, blocking I/O, memory, DOM thrash."""

    name = "performance"
    severity_default = FindingSeverity.WARNING
    system_prompt = _SYSTEM
    user_prompt_template = _USER_TEMPLATE

    #: The language of the note a lowered finding gets. Set from the context
    #: before each parse, because the shared parse hands `_dict_to_finding`
    #: nothing but the reply's own item.
    _language: str = "en"

    def __init__(self, model: str | None = None) -> None:
        self.model = model or get_review_settings().performance_model

    def _parse_findings(self, llm_text: str, context: AgentContext) -> list[Finding]:
        self._language = messages.resolve_language(
            getattr(context, "review_language", None), context.workspace_id)
        return super()._parse_findings(llm_text, context)

    def _dict_to_finding(
        self, data: dict[str, Any], changed_files: frozenset[str] | None = None,
    ) -> Finding | None:
        """The shared parse, then the severity gate this agent owns.

        In the scored runs every performance finding posted at `error` (an
        N+1 over a handful of items, `in_array` in a short loop, an "unbounded"
        array that had a TTL) was minor or speculative: the prompt asked for
        the growing quantity to be NAMED, and a model can always name one.
        So `error` and `critical` now also need `input_size` to state a size
        or an unbounded / user-controlled source (`states_a_growing_input`);
        otherwise the finding is kept, at `warning`, with a note saying why.
        The finding is not dropped — a real cost is still worth a comment —
        only kept from the severity that gets posted. A finding the pipeline
        already proved (`evidence_kind == "proven"`) is never lowered.
        """
        finding = super()._dict_to_finding(data, changed_files)
        if finding is None or finding.severity not in _NEEDS_A_BOUND:
            return finding
        if finding.evidence_kind == "proven" or states_a_growing_input(data.get("input_size")):
            return finding
        finding.severity = FindingSeverity.WARNING
        note = messages.t("perf.downgrade", self._language)
        finding.body = f"{finding.body}\n\n{note}" if finding.body else note
        return finding
