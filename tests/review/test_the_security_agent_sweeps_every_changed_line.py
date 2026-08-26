"""The security agent enumerates every changed line; it does not pick a few.

Measured on the Martian Code Review Bench development subset (14 PRs, judge
claude-sonnet-4-5, TP 24 / FP 31 / FN 29): on discourse#14 this agent returned
exactly three findings and stopped, and on discourse#11 the same three-and-stop
shape. The #14 golden set held three security bugs on three single changed
lines — `open(SiteSetting.feed_polling_url)` (SSRF), an `X-Frame-Options:
ALLOWALL` header, and `request.referer` interpolated into JavaScript (XSS) —
and none of the three findings the agent returned was any of them. Those calls
carried zero refusals and the full graph context, and no numeric cap exists in
this agent's code, so what was left is the phrasing: "return a JSON array of
security findings" asks for SOME findings.

These tests drive the real prompt builder and the real system-prompt
composition and assert on what the model is actually handed: the sweep reaches
it, no finding budget reaches it, the shapes the bench missed are named, the
authorising frame is still read first, and the evidence contract still stands.
"""

from __future__ import annotations

import re

import pytest

from src.review.agents.base import (
    AVOID_LIST_PROMPT,
    FINDING_OUTPUT_FORMAT,
    AgentContext,
    _compose_effective_system_prompt,
)
from src.review.agents.security import SecurityAgent
from src.review.agents.verifier import prefilter
from src.review.models import FindingSeverity, Hunk, PullRequest
from src.review.settings import get_review_settings

# The three lines discourse#14's golden set was built from, in the shape a
# unified diff carries them. They are here so the prompt under test is built
# over the diff the agent actually got the fewest findings on.
_SSRF_LINE = "+    feed = open(SiteSetting.feed_polling_url)"
_HEADER_LINE = '+    headers["X-Frame-Options"] = "ALLOWALL"'
_XSS_LINE = '+    script = "window.opener.location = \\"#{request.referer}\\";"'


def _flat(text: str) -> str:
    """Line wraps are layout, not meaning — compare the words."""
    return " ".join(text.split())


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="discourse/discourse", number=14,
        title="Add feed polling", description="Polls a configured feed.",
        author="alice", base_ref="main", base_sha="a",
        head_ref="feed", head_sha="b", state="open",
        hunks=[
            Hunk(
                file_path="app/models/feed.rb", old_file_path="app/models/feed.rb",
                old_start=10, old_count=1, new_start=10, new_count=4,
                content=f"@@ -10,1 +10,4 @@\n context\n{_SSRF_LINE}\n"
                        f"{_HEADER_LINE}\n{_XSS_LINE}\n",
            ),
        ],
    )


def _context() -> AgentContext:
    return AgentContext(pull_request=_pr(), graph_summary="feed_polling_url -> open")


def _composed_system() -> str:
    """The system prompt as `review()` composes it — overrides, extras and all."""
    return _compose_effective_system_prompt(
        agent_name="security",
        default_system=SecurityAgent.system_prompt,
        context=_context(),
    )


def _sweep_block() -> str:
    """The per-line shape list alone — not the OWASP catalogue below it.

    The catalogue was already in the prompt, and already named CWE-918,
    CWE-79, CWE-78, CWE-89, CWE-502 and CWE-862, on the run that produced
    three findings and stopped. A test satisfied by the catalogue would have
    passed before this change and would prove nothing about it. What is new is
    each shape stated as something one changed line can be matched against,
    inside the sweep — so that is where these assertions look.
    """
    system = _composed_system()
    start = system.index("HOW TO READ THE DIFF")
    end = system.index("Those eight are the floor")
    return system[start:end]


# ─── the sweep reaches the model ─────────────────────────────────────


def test_the_user_prompt_asks_for_one_finding_per_matching_line():
    """The last thing the model reads before answering is the sweep, not a
    request for "security findings" that three of anything satisfies."""
    built = _flat(SecurityAgent()._build_prompt(_context()))
    assert "Sweep the changed lines in order" in built
    assert "ONE finding per matching line" in built
    assert "Report every line that matched" in built
    # After the diff: an instruction the model reads before the code it
    # applies to is an instruction about nothing yet.
    assert built.index("Sweep the changed lines in order") > built.index(_flat(_SSRF_LINE))


def test_the_diff_the_sweep_applies_to_actually_arrives():
    """A sweep instruction over a diff that never reached the prompt would
    pass every wording assertion above and find nothing at all."""
    built = SecurityAgent()._build_prompt(_context())
    for line in (_SSRF_LINE, _HEADER_LINE, _XSS_LINE):
        assert line in built


def test_the_system_prompt_states_the_finishing_condition():
    """"Finished" is defined by the last changed line, not by a comfortable
    number of findings — the three-and-stop shape is exactly the model
    deciding it had enough."""
    system = _flat(_composed_system())
    assert "Check every changed line against the shapes below" in system
    assert "you are finished when the LAST changed line has been checked" in system
    assert "Two matching lines in one file are two findings" in system


# ─── no budget reaches the model ─────────────────────────────────────

#: Phrasings that put a number, or a superlative, on how many findings to
#: write. The sibling quality agent ends its template with "Aim for the 3-7
#: most-impactful issues (no spam)"; security must carry nothing of that
#: shape, in either polarity — a prompt that says "do not report only the most
#: important ones" has still put the idea of a shortlist in front of the model.
_BUDGET_SHAPES = (
    r"most[-\s]impactful",
    r"\bmost (?:important|interesting|serious|significant|critical)\b",
    r"\b\d+\s*[-–]\s*\d+\s+most\b",
    r"\baim for\b[^.]{0,20}\d",
    r"\bno spam\b",
    r"\b(?:at most|no more than|up to|a maximum of)\s+\d+\s+[\w-]*\s*"
    r"(?:findings|issues|comments|problems)\b",
    r"\b(?:top|first|best)\s+\d+\s+(?:findings|issues|comments|problems)\b",
    r"\b(?:pick|choose|select|report)\b[^.]{0,40}\b(?:most|best|top)\b",
    r"\blimit yourself\b",
    r"\bonly the\b[^.]{0,30}\bmost\b",
)

#: One sentence carrying the shape, so the patterns above are demonstrably
#: able to see one. Written out rather than read from QualityAgent: quality's
#: own wording is another change's business, and a detector that goes blind
#: the day somebody else edits that file is not a detector.
_A_BUDGET_SENTENCE = "Aim for the 3-7 most-impactful issues (no spam). If OK — `[]`."


def test_the_budget_detector_is_not_blind():
    """The patterns must fire on a sentence that IS a budget, or the test
    below passes because they match nothing anywhere."""
    fired = [p for p in _BUDGET_SHAPES if re.search(p, _A_BUDGET_SENTENCE, re.I)]
    assert fired, "no pattern recognises a budget sentence"


@pytest.mark.parametrize("shape", _BUDGET_SHAPES)
def test_no_finding_budget_reaches_the_model(shape):
    system = _composed_system()
    user = SecurityAgent()._build_prompt(_context())
    for where, text in (("system", system), ("user", user)):
        hit = re.search(shape, text, re.I)
        assert hit is None, f"{where} prompt budgets the findings: {hit.group(0)!r}"


# ─── the shapes the bench missed are named ───────────────────────────

#: What the model has to be able to match a single changed line against. The
#: first three are discourse#14's three golden bugs in order; the rest are the
#: injection/deserialization/authorisation shapes the same analysis named.
_SHAPES_A_LINE_CAN_MATCH = (
    "CWE-918",          # 1. a configured URL that is fetched — the SSRF miss
    "X-Frame-Options",  # 2. a security header set/weakened — the header miss
    "CWE-79",           # 3. a value interpolated into HTML/JS — the XSS miss
    "CWE-78",           # 4. a value reaching a shell or a command array
    "CWE-89",           # 5. a value reaching SQL or a query builder
    "CWE-502",          # 6. deserialization of untrusted input
    "CWE-862",          # 7. an authorisation check added/changed/absent
)


@pytest.mark.parametrize("shape", _SHAPES_A_LINE_CAN_MATCH)
def test_every_shape_the_sweep_checks_for_is_named_to_the_model(shape):
    assert shape in _flat(_sweep_block())


def test_the_header_shape_names_headers_a_diff_line_can_carry():
    """"A security-relevant header" is a category; the model matches a line
    against names. The one the bench missed is X-Frame-Options."""
    system = _flat(_sweep_block())
    for header in ("X-Frame-Options", "Content-Security-Policy",
                   "Access-Control-Allow-Origin", "Strict-Transport-Security"):
        assert header in system


def test_the_xss_shape_names_the_values_the_missed_line_used():
    """discourse#14 interpolated `request.referer`; a shape that says only
    "user input" does not obviously cover a referer or a stored field."""
    system = _flat(_sweep_block())
    assert "referer" in system
    assert "stored database" in system


# ─── the refusal frame and the evidence contract survive ─────────────


def test_the_authorisation_is_read_before_the_sweep():
    """The sweep is the part that reads as vulnerability hunting, and this
    agent was refused in one call out of five before the frame was added. The
    frame has to come first in the composed prompt, not merely exist in it."""
    system = _composed_system()
    assert system.index("authorised code review") < system.index("HOW TO READ THE DIFF")
    assert system.index("legitimate task") < system.index("HOW TO READ THE DIFF")


#: Wordings that turn a review request into a request for attack material.
#: None of them was ever here; the sweep is where one would be tempted in.
_ATTACK_MATERIAL = (
    "write an exploit", "exploit code", "proof of concept", "proof-of-concept",
    "payload that", "how to attack", "attack this", "weaponi",
)


@pytest.mark.parametrize("phrase", _ATTACK_MATERIAL)
def test_the_prompt_never_asks_for_attack_material(phrase):
    both = _flat(_composed_system() + " " + SecurityAgent()._build_prompt(_context()))
    assert phrase not in both.lower()


def test_the_evidence_contract_still_stands_after_the_sweep():
    """Every finding opens with a reasoning sentence, on a file the PR
    touches, and carries a confidence — the shared shape, once, plus the
    user prompt's own reminder of the order."""
    system = _composed_system()
    assert system.count(FINDING_OUTPUT_FORMAT.strip()) == 1
    assert system.count(AVOID_LIST_PROMPT) == 1
    built = _flat(SecurityAgent()._build_prompt(_context()))
    assert 'each starting with its "reasoning" sentence' in built


def test_the_sweep_does_not_smuggle_back_an_avoided_category():
    """The avoid-list is downstream of nothing: a prompt that asks for a
    category the deny-list hides spends tokens on a comment nobody sees."""
    suppressed = get_review_settings().suppressed_rules
    system = _composed_system()
    head, _, _ = system.partition(AVOID_LIST_PROMPT)
    _, _, tail = system.rpartition(AVOID_LIST_PROMPT)
    for rule_id in suppressed:
        assert rule_id not in head + tail


# ─── what the sweep produces survives the way to the PR ──────────────


def _reply(*items: str) -> str:
    return "[" + ",".join(items) + "]"


def test_three_hits_on_one_file_stay_three_findings():
    """The whole point of the sweep: three single-line bugs in one file are
    three comments. Parsed by the real agent, then through the deterministic
    prefilter the orchestrator always runs — where a same-file cluster is the
    thing that could quietly put them back down to one."""
    agent = SecurityAgent()
    text = _reply(
        '{"reasoning": "line 11 opens SiteSetting.feed_polling_url, which an '
        'admin-settable value controls, so the server fetches an attacker-chosen '
        'host", "file": "app/models/feed.rb", "line": 11, "severity": "error", '
        '"title": "SSRF via feed_polling_url", "body": "b", '
        '"rule_id": "sec.cwe-918", "confidence": 0.8}',
        '{"reasoning": "line 12 sets X-Frame-Options to ALLOWALL, so any site '
        'may frame this page and clickjack a logged-in user", '
        '"file": "app/models/feed.rb", "line": 12, "severity": "warning", '
        '"title": "X-Frame-Options weakened to ALLOWALL", "body": "b", '
        '"rule_id": "sec.cwe-1021", "confidence": 0.8}',
        '{"reasoning": "line 13 interpolates request.referer into a JavaScript '
        'string, so a crafted referer executes in the page", '
        '"file": "app/models/feed.rb", "line": 13, "severity": "critical", '
        '"title": "Reflected XSS from the referer header", "body": "b", '
        '"rule_id": "sec.cwe-79", "confidence": 0.9}',
    )
    findings = agent._parse_findings(text, _context())
    assert len(findings) == 3
    assert {f.line for f in findings} == {11, 12, 13}

    kept = prefilter(findings, suppressed_rules=get_review_settings().suppressed_rules)
    assert len(kept.kept) == 3, kept.dropped_by_rule
    assert kept.dropped_dedup == 0
    assert kept.dropped_near_duplicate == 0
    # Severity sort decides what survives findings[:max_inline_comments].
    assert kept.kept[0].severity is FindingSeverity.CRITICAL


def test_a_sweep_hit_that_omits_its_severity_is_not_demoted_below_the_cap():
    """The providers post findings[:max_inline_comments] after the severity
    sort, so the fallback severity decides whether a sweep hit with no
    "severity" key survives a long review. For this agent that fallback is
    `error`, not the base class's `warning`."""
    agent = SecurityAgent()
    text = _reply(
        '{"reasoning": "line 11 opens a configured URL, so the server fetches '
        'an attacker-chosen host", "file": "app/models/feed.rb", "line": 11, '
        '"title": "SSRF", "body": "b", "rule_id": "sec.cwe-918", '
        '"confidence": 0.8}',
    )
    findings = agent._parse_findings(text, _context())
    assert [f.severity for f in findings] == [FindingSeverity.ERROR]


def test_the_single_use_secret_rule_still_reaches_the_model():
    """The CWE-367 rule is the security half of the concurrency gap: a backup
    code looked up on one line and marked used on another is an auth bypass,
    and it was one of the three concurrency defects in this run's 29 misses.

    It carried no test of its own, and this file is the one that rewrites
    `_ROLE`: an earlier whole-file rewrite of security.py already deleted
    this rule once and nothing went red. Asserted on the composed prompt, so
    it fails whether the rule is deleted or merely stops being sent.
    """
    system = _flat(_composed_system())
    assert "sec.cwe-367" in system
    assert "looked up on one line and marked used on another" in system
    # In Rules, where the restraint lives — not floating above the diff, and
    # after the authorising frame like everything else this agent asks for.
    assert system.index("Rules:") < system.index("sec.cwe-367")
    assert system.index("authorised code review") < system.index("sec.cwe-367")
