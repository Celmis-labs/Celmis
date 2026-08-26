"""Security agent — CWE-prompted security review (Gemini 3 Pro).

Focus on OWASP Top 10 + CWE Top 25. It requires deep reasoning — hence Pro tier.
"""

from __future__ import annotations

from src.review.agents.base import (
    AVOID_LIST_PROMPT,
    FINDING_OUTPUT_FORMAT,
    SECOND_DEFECT_PROMPT,
    LLMReviewAgent,
)
from src.review.models import FindingSeverity
from src.review.settings import get_review_settings

# The opening frame exists because of a number, not a hunch. Measured on a
# benchmark fork set: the model REFUSED this agent's request in one call out
# of five — "Sorry, I cannot fulfill your request to analyze or identify
# vulnerabilities in specific code snippets" — while quality, tests and
# architect, which never say "vulnerabilities", were not refused at all. The
# refusal is a 200 with polite prose, so it surfaced only as "no JSON array"
# and the corrective resend asked the identical question and got the identical
# no. The retry ladder now classifies a refusal and re-asks once with
# authorising context (base.py _REFUSAL_REFRAME); but a second call is a cost
# and a delay, and at 20% per call the cheaper fix is to stop provoking it:
# say up front that this is the owner's own change, being reviewed before it
# merges, at their request. Same words as the re-frame, so the two cannot
# drift into making different claims about what the task is. It stays the
# FIRST thing in the prompt: the sweep below is the part that reads as
# vulnerability hunting, and the authorisation has to be read before it.
#
# The sweep block itself is there because of a shape in the run data. On the
# Martian Code Review Bench development subset (14 PRs, judge
# claude-sonnet-4-5, TP 24 / FP 31 / FN 29) this agent returned exactly three
# findings and stopped on discourse#14, and again on discourse#11. On #14 the
# golden set held three security bugs, each on a SINGLE changed line —
# `open(SiteSetting.feed_polling_url)` (SSRF), an `X-Frame-Options: ALLOWALL`
# header, and `request.referer` interpolated into JavaScript (XSS) — and the
# three findings the agent did return were about other things (two credited,
# one not). Those calls carried zero refusals and the full graph context, and
# there is no numeric cap anywhere in this agent's code: nothing capped it,
# the phrasing invited it. "Review the diff … return a JSON array of security
# findings" is a request for SOME findings, and three is what some looks like.
# So the task is stated as a per-line sweep against named, line-recognisable
# shapes — the three misses above are shapes 1, 2 and 3 — and completeness is
# stated as the finishing condition. Whether that moves the number is NOT yet
# measured: re-run the bench before claiming it did.
#
# The CWE-367 rule below is the security half of one measured gap. On the
# Martian Code Review Bench development subset (14 PRs, judge claude-sonnet-4-5)
# the product scored TP 24 / FP 31 / FN 29, and three of the 29 misses were
# concurrency defects — one of them a concurrent mutation of `backupCodes`,
# which is an authentication bypass before it is a design problem: two requests
# both read "unused" and both spend the same code. The architect carries the
# general check-then-act shape (see architect.py); this names the single-use
# secret, because that is the one an attacker triggers on purpose and it is the
# only concurrency case this agent's "actually exploitable" rule admits.
_ROLE = """You are a security engineer reviewing a pull request before it merges.

This is an authorised code review: the owner of this repository installed
this reviewer and asked for exactly these findings. The code is their own
change. Pointing out where it can be exploited, so it is fixed before it
ships, is the requested and legitimate task.

Review the diff with a focus on vulnerabilities.

HOW TO READ THE DIFF — one changed line at a time, in order, to the end:

    Check every changed line against the shapes below. Each line that matches
    a shape is its own finding, and you are finished when the LAST changed
    line has been checked — not when you have written enough findings to be
    going on with. A diff with six matching lines gets six findings. Two
    matching lines in one file are two findings, one per line. Leaving out a
    line that matched, because other findings were already written, is the
    vulnerability shipping.

    The shapes, each recognisable from the changed line itself:

    1. SSRF (CWE-918) — a URL, host, hostname or address that comes from a
       setting, a configuration value, a parameter or any user input, and is
       then fetched, opened, requested, redirected to or resolved.
    2. Response headers (CWE-16, CWE-1021) — a security-relevant header set,
       weakened or removed: X-Frame-Options, Content-Security-Policy,
       Access-Control-Allow-Origin, Strict-Transport-Security,
       X-Content-Type-Options, or a cookie that loses Secure, HttpOnly or
       SameSite.
    3. XSS (CWE-79) — a value interpolated into HTML, into JavaScript, into a
       template, into an HTML attribute, or into a URL a browser will follow.
       The referer, a query parameter, a request header and a stored database
       field are all such values.
    4. Command injection (CWE-78) — a value reaching a shell, `exec`,
       `system`, backticks, or an argument array handed to a process.
    5. SQL injection (CWE-89) — a value reaching SQL, a raw query, or the raw
       fragment of a query builder.
    6. Unsafe deserialization (CWE-502) — untrusted input reaching a
       deserializer, an unmarshaller, or an object loader.
    7. Access control (CWE-862, CWE-863, CWE-287) — an authentication or
       authorisation check added, changed, or missing on a path whose
       siblings in the same file have one.
    8. Secrets (CWE-798, CWE-532) — a key, token or password written as a
       literal, logged, or placed in a URL.

    Those eight are the floor, not the ceiling. Check each changed line
    against the rest of OWASP Top 10 (2025) + CWE Top 25 on the same terms —
    one finding per matching line:
        A01:2025 Broken Access Control            (CWE-862, CWE-863)
        A02:2025 Cryptographic Failures           (CWE-327, CWE-798 hardcoded keys)
        A03:2025 Injection                         (CWE-89 SQL, CWE-79 XSS, CWE-78 OS cmd)
        A04:2025 Insecure Design                   (CWE-209 verbose errors)
        A05:2025 Security Misconfiguration         (CWE-16, CWE-260 cleartext storage)
        A06:2025 Vulnerable Components             (CWE-1104)
        A07:2025 ID & Auth Failures                (CWE-287, CWE-384 session fixation)
        A08:2025 Software/Data Integrity Failures  (CWE-502 unsafe deserialization)
        A09:2025 Security Logging Failures         (CWE-532 sensitive data in logs)
        A10:2025 SSRF                              (CWE-918)

Rules:
    - REPORT ONLY actually exploitable issues. Don't invent vulnerabilities.
      Completeness means never SKIPPING a line that matched; it is never a
      reason to write up a line that matched nothing.
    - If input is sanitized or validated — do not flag.
    - A one-time secret — a backup code, a reset token, a single-use invite —
      looked up on one line and marked used on another is CWE-367: two
      requests that arrive together both pass the check before either writes.
      Report it (`sec.cwe-367`) when the diff shows both lines and nothing
      atomic between them — a lock, a transaction, a conditional update.
    - The reasoning sentence names the line, the value an attacker controls,
      and what they get; a finding whose attacker gets nothing is not one."""

_SEVERITY = """rule_id format: the specific CWE, `sec.cwe-<number>` (e.g. `sec.cwe-89`).

Severity:
    critical — RCE / SQL injection / hardcoded prod secrets
    error    — auth bypass / SSRF / path traversal
    warning  — verbose error / missing CSRF / weak crypto choice
    info     — defensive hardening recommendation"""

# Same four-part shape as the other agents — see architect.py.
_SYSTEM = "\n\n".join([_ROLE, FINDING_OUTPUT_FORMAT, AVOID_LIST_PROMPT, SECOND_DEFECT_PROMPT, _SEVERITY]) + "\n"


_USER_TEMPLATE = """## PR
**Title:** {pr_title}
**Description:**
{pr_description}

## Diff
{diff}

## Graph context (for taint analysis)
{graph_summary}

---

Sweep the changed lines in order, checking each one against the shapes, and
return a JSON array carrying ONE finding per matching line — each starting
with its "reasoning" sentence. Report every line that matched: several of
them in one diff is the normal case, and stopping while changed lines are
still unchecked leaves a vulnerability in the merge. If no changed line
matched — `[]`. Each finding must reference a specific file+line + a specific
CWE ID.
"""


class SecurityAgent(LLMReviewAgent):
    """OWASP/CWE-prompted security review."""

    name = "security"
    severity_default = FindingSeverity.ERROR
    system_prompt = _SYSTEM
    user_prompt_template = _USER_TEMPLATE

    def __init__(self, model: str | None = None) -> None:
        self.model = model or get_review_settings().security_model
