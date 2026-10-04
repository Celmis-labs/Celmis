"""What a review does to a pull request beyond commenting on it.

Provider-agnostic decisions shared by the three providers and the
orchestrator, so GitHub, GitLab and Bitbucket cannot answer "approve this?"
three different ways:

  * `review_decision` — approve, request changes, or neither, from the batch
    and the repository's `PRActions`;
  * `render_template` — the custom started / finished messages, with a fixed
    set of placeholders and no way to raise;
  * the description summary — our block between two markers inside the pull
    request's description, composed by `compose_description` under the
    `summary_existing_description` × `summary_on_new_commits` modes.

Nothing here talks to a provider or a model; the callers do, and they pass
the results in.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime

from src.review.models import (
    PRActions,
    ReviewBatch,
    ReviewRunStatus,
    ReviewVerdict,
)

APPROVE = "approve"
REQUEST_CHANGES = "request_changes"


# ─── Approve / request changes ──────────────────────────────────────


def review_decision(batch: ReviewBatch) -> str | None:
    """`APPROVE`, `REQUEST_CHANGES` or None (a plain comment).

    Request changes: `request_changes_on_critical` and at least one critical
    finding. A partial review still requests changes — the critical it found
    is real whatever else did not run — but a skipped or failed one never
    does, because a review that did not happen blocks nobody.

    Approve: `approve_when_clean`, a review that COMPLETED (not skipped,
    failed or partial), nothing at or above the comment threshold, and a
    verdict that is not itself a request for changes (blocking compliance,
    three errors under a "critical only" threshold). Never both: a critical
    finding is postable at every threshold, so the two arms cannot meet, and
    the request arm is checked first regardless.
    """
    actions = getattr(batch, "pr_actions", None) or PRActions()
    status = batch.run_status
    if (actions.request_changes_on_critical and batch.critical_count > 0
            and status in (ReviewRunStatus.COMPLETE, ReviewRunStatus.PARTIAL)):
        return REQUEST_CHANGES
    if (actions.approve_when_clean
            and status == ReviewRunStatus.COMPLETE
            and not batch.postable_findings
            and batch.verdict not in (ReviewVerdict.REQUEST_CHANGES, ReviewVerdict.SKIPPED)):
        return APPROVE
    return None


# ─── Message templates ──────────────────────────────────────────────

#: The placeholders a template may use. Anything else in braces stays as typed.
TEMPLATE_PLACEHOLDERS = ("commit", "agents", "files", "pr_number")
STARTED_TEMPLATE_MAX_CHARS = 2000
HEADER_TEMPLATE_MAX_CHARS = 300

_PLACEHOLDER = re.compile(r"\{(" + "|".join(TEMPLATE_PLACEHOLDERS) + r")\}")


def render_template(template: object, values: dict[str, object], *, limit: int) -> str:
    """`template` with the known placeholders filled in; "" when there is none.

    Not `str.format`: a brace the maintainer meant literally, an unknown name
    or a `{0}` would raise there, and attribute/index syntax would reach into
    the values. Here only `{commit}`, `{agents}`, `{files}` and `{pr_number}`
    are replaced, everything else is left exactly as typed, and the result is
    capped at `limit` characters. Never raises.
    """
    if not isinstance(template, str) or not template.strip():
        return ""
    try:
        out = _PLACEHOLDER.sub(
            lambda m: str(values.get(m.group(1), m.group(0))), template,
        )
    except Exception:  # noqa: BLE001 — a template never fails a review
        out = template
    out = out.strip()
    if len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out


def template_values(pr, agents: list[str]) -> dict[str, object]:
    return {
        "commit": (getattr(pr, "head_sha", "") or "")[:7] or "unknown",
        "agents": ", ".join(agents) if agents else "none",
        "files": len(getattr(pr, "changed_files", []) or []),
        "pr_number": getattr(pr, "number", ""),
    }


# ─── The summary inside the PR description ──────────────────────────

SUMMARY_START = "<!-- celmis:summary:start -->"
SUMMARY_END = "<!-- celmis:summary:end -->"
_COMMIT_MARK = re.compile(r"<!-- celmis:summary:commit=([0-9a-fA-F]*) -->")
_ORIGINAL_OPEN = "<details>\n<summary>Original description</summary>\n\n"
_ORIGINAL_CLOSE = "\n\n</details>"
_ORIGINAL = re.compile(
    re.escape(_ORIGINAL_OPEN) + r"(.*?)" + re.escape(_ORIGINAL_CLOSE), re.DOTALL,
)
_UPDATE_HEADING = "### Update — "

#: The tightest description limit of the three providers is GitHub's 65,536
#: characters for a body; stay well under it.
DESCRIPTION_MAX_CHARS = 60_000


def split_description(text: str) -> tuple[str, str | None, str]:
    """(before, our block's inner text or None, after) of a description."""
    text = text or ""
    start = text.find(SUMMARY_START)
    if start < 0:
        return text, None, ""
    end = text.find(SUMMARY_END, start + len(SUMMARY_START))
    if end < 0:
        # A start marker with no end — somebody cut the block in half. The
        # rest of the text is treated as ours, so the next write repairs it
        # instead of stacking a second block below the broken one.
        return text[:start], text[start + len(SUMMARY_START):], ""
    return (text[:start], text[start + len(SUMMARY_START):end],
            text[end + len(SUMMARY_END):])


def _block(inner: str) -> str:
    return f"{SUMMARY_START}\n{inner.strip()}\n{SUMMARY_END}"


def _stamp(commit: str, when: datetime) -> str:
    return (f"<sub>Celmis summary · commit `{(commit or '')[:7] or 'unknown'}` · "
            f"{when.strftime('%Y-%m-%d %H:%M UTC')}</sub>\n"
            f"<!-- celmis:summary:commit={commit or ''} -->")


def _join(*parts: str) -> str:
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def compose_description(
    current: str,
    *,
    insights: str,
    commit: str,
    existing_mode: str,
    new_commits_mode: str,
    complement: Callable[[str, str], str | None] | None = None,
    now: datetime | None = None,
) -> str | None:
    """The new description, or None to leave it alone.

    `insights` is our markdown (overview + walkthrough). `complement(author,
    insights)` is the one LLM call for `existing_mode == "complement"`; when
    it is missing or answers None the block falls back to "append".

    First review (no block of ours in the description yet):
      append     → author's text, then our block
      replace    → our block is the whole description
      complement → our block holds the merged text, the author's original
                   folded underneath it so a later run can merge again
    Our block already there:
      nothing    → leave it (only the first review writes)
      replace    → rewrite the block, the author's text around it untouched
      append     → a dated "Update" section at the end of the block; a re-run
                   of the SAME commit rewrites its own section instead of
                   adding another
    """
    insights = (insights or "").strip()
    if not insights:
        return None
    when = now or datetime.now(UTC)
    before, inner, after = split_description(current)
    stamp = _stamp(commit, when)

    if inner is None:
        if existing_mode == "replace":
            return _block(_join(insights, stamp))
        if existing_mode == "complement":
            merged = _complemented(before + after, insights, complement)
            if merged is not None:
                return _block(_join(merged, stamp))
        return _join(before + after, _block(_join(insights, stamp)))

    if new_commits_mode == "nothing":
        return None

    recorded = _COMMIT_MARK.findall(inner)
    same_commit = bool(recorded) and bool(commit) and recorded[-1] == commit

    if new_commits_mode == "append":
        if same_commit and _UPDATE_HEADING in inner:
            # A re-run of the commit the last section describes: rewrite that
            # section rather than add a copy of it.
            inner = inner[: inner.rfind(_UPDATE_HEADING)]
        elif same_commit:
            return _rewrite(before, after, inner, insights, stamp, existing_mode,
                            complement)
        section = _join(
            f"{_UPDATE_HEADING}{when.strftime('%Y-%m-%d')} "
            f"(commit `{(commit or '')[:7] or 'unknown'}`)",
            insights, stamp,
        )
        new = before + _block(_join(inner, section)) + after
        if len(new) <= DESCRIPTION_MAX_CHARS:
            return new
        # Too long to keep every update: the block starts over.
    return _rewrite(before, after, inner, insights, stamp, existing_mode, complement)


def _rewrite(before, after, inner, insights, stamp, existing_mode, complement) -> str:
    if existing_mode == "complement":
        found = _ORIGINAL.search(inner)
        original = found.group(1) if found else ""
        merged = _complemented(_join(original, before + after), insights, complement)
        if merged is not None:
            return _block(_join(merged, stamp))
    return before + _block(_join(insights, stamp)) + after


def _complemented(author: str, insights: str,
                  complement: Callable[[str, str], str | None] | None) -> str | None:
    author = (author or "").strip()
    if not author:
        # Nothing of the author's to keep: complementing nothing is replacing.
        return insights
    if complement is None:
        return None
    try:
        merged = complement(author, insights)
    except Exception:  # noqa: BLE001 — the description never fails a review
        merged = None
    if not merged or not merged.strip():
        return None
    return _join(merged, _ORIGINAL_OPEN + author + _ORIGINAL_CLOSE)


def description_insights(batch: ReviewBatch) -> str:
    """Our part of the description: the overview and the walkthrough table."""
    from src.review.providers.base import _walkthrough_lines

    overview = (getattr(batch, "pr_overview", "") or "").strip()
    parts = ["## 🤖 Celmis summary"]
    if overview:
        parts.append(overview)
    walk = "\n".join(_walkthrough_lines(batch)).strip()
    if walk:
        parts.append(walk)
    if len(parts) == 1:
        return ""
    return "\n\n".join(parts)


def strip_markers(text: str) -> str:
    """A model's merged text with anything that could forge our block removed."""
    s = re.sub(r"<!--.*?-->", "", str(text or ""), flags=re.DOTALL)
    return s.strip()
