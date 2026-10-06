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

from src.review import messages
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
#: The author's own text, folded under our block so a later run can merge again.
#: Wrapped in two markers because Bitbucket shows `<details>` as text: there it
#: becomes a bold line (see `markers.bitbucket_flavour`), and the markers are
#: what still bound the fold. A fold written before the markers is read as well.
_ORIGINAL_OPEN = (
    "<!-- celmis:summary:original -->\n"
    "<details>\n<summary>Original description</summary>\n\n"
)
_ORIGINAL_CLOSE = "\n\n</details>\n<!-- celmis:summary:original:end -->"
_ORIGINAL = re.compile(
    r"<!-- celmis:summary:original -->\s*"
    r"(?:<details>\s*<summary>Original description</summary>|\*\*Original description\*\*)"
    r"\s*(.*?)\s*(?:</details>\s*)?<!-- celmis:summary:original:end -->",
    re.DOTALL,
)
_ORIGINAL_LEGACY = re.compile(
    r"<details>\n<summary>Original description</summary>\n\n(.*?)\n\n</details>",
    re.DOTALL,
)
#: A "later push" section of our block starts at this hidden line (the heading
#: after it is in the repository's language, so it cannot be searched for);
#: blocks written before the marker existed are found by the English heading.
UPDATE_MARK = "<!-- celmis:summary:update -->"
_UPDATE_HEADING = "### Update — "

#: Our block's own first line. When a person edits the description in the
#: provider's UI the hidden marker lines are the first thing to go (an editor
#: that does not know them drops them); the heading is what is left to find the
#: block by.
SUMMARY_HEADING = "## 🤖 Celmis summary"
_HEADING_LINE = re.compile(r"^" + re.escape(SUMMARY_HEADING) + r"[ \t]*\r?$", re.MULTILINE)

#: The tightest description limit of the three providers is GitHub's 65,536
#: characters for a body; stay well under it.
DESCRIPTION_MAX_CHARS = 60_000

#: What the markers, the stamp and the author's folded original take besides the
#: overview itself, and the least room the overview is ever squeezed to.
_BLOCK_OVERHEAD = 1_000
_MIN_INSIGHTS = 2_000
#: A provider that hides our markers writes them longer than the HTML form the
#: limit is measured in (zero-width markers by far the longest); the block is
#: kept this far under the limit so `markers.fit` has nothing left to cut.
_HIDDEN_MARGIN = 1_500


_END_LINE = re.compile(r"^" + re.escape(SUMMARY_END) + r"[ \t]*\r?$", re.MULTILINE)


def _find_end(text: str, pos: int) -> tuple[int, int]:
    """(start, end) of our end marker after `pos`, (-1, -1) when there is none.

    The marker is written on a line of its own; text that merely contains the
    same characters — a finding title quoted from the diff — does not end the
    block. A description a provider's editor reflowed, so that no marker is
    still alone on its line, falls back to the plain search.
    """
    found = _END_LINE.search(text, pos)
    if found is not None:
        return found.start(), found.end()
    plain = text.find(SUMMARY_END, pos)
    return (plain, plain + len(SUMMARY_END)) if plain >= 0 else (-1, -1)


def split_description(text: str) -> tuple[str, str | None, str]:
    """(before, our block's inner text or None, after) of a description."""
    text = text or ""
    start = text.find(SUMMARY_START)
    if start < 0:
        return _split_by_heading(text)
    end, after_end = _find_end(text, start + len(SUMMARY_START))
    if end < 0:
        # A start marker with no end — somebody cut the block in half. The
        # rest of the text is treated as ours, so the next write repairs it
        # instead of stacking a second block below the broken one.
        return text[:start], text[start + len(SUMMARY_START):], ""
    return (text[:start], text[start + len(SUMMARY_START):end],
            text[after_end:])


def _split_by_heading(text: str) -> tuple[str, str | None, str]:
    """No start marker: the block is read from our heading instead.

    The markers are gone but the block is not — somebody edited the description
    and lost the hidden lines. Without this the next write would stack a second
    block under the first. The block runs to its end marker when that is still
    there, else to the end of the text.
    """
    found = _HEADING_LINE.search(text)
    if found is None:
        return text, None, ""
    end, after_end = _find_end(text, found.end())
    if end < 0:
        return text[:found.start()], text[found.start():], ""
    return text[:found.start()], text[found.start():end], text[after_end:]


def _original_of(inner: str) -> str:
    """The author's text folded into our block, "" when there is none."""
    found = _ORIGINAL.search(inner) or _ORIGINAL_LEGACY.search(inner)
    return found.group(1) if found else ""


def _block(inner: str) -> str:
    return f"{SUMMARY_START}\n{inner.strip()}\n{SUMMARY_END}"


def _around(before: str, block: str, after: str) -> str:
    """`block` between the author's `before` and `after`, each on lines of its own.

    A marker only counts (and is only hidden) as a whole line: an author who
    typed straight up to our start marker must not glue it to their text.
    """
    if before and not before.endswith("\n"):
        before += "\n\n"
    if after and not after.startswith("\n"):
        after = "\n\n" + after
    return before + block + after


def _stamp(commit: str, when: datetime, language: str | None = None, *,
           same_commit: bool = False) -> str:
    """The line under our block: which commit it describes, and when.

    The clock time is left out when the block already described this commit:
    the text of a re-run is then the same as before, so a provider that
    reports a description change as an event is not woken by every re-run.
    """
    stamped = when.strftime("%Y-%m-%d" if same_commit else "%Y-%m-%d %H:%M UTC")
    text = messages.t("description.stamp", language,
                      sha=(commit or "")[:7] or "unknown", when=stamped)
    return f"_{text}_\n<!-- celmis:summary:commit={commit or ''} -->"


def _last_update_start(inner: str) -> int:
    """Where the last "update" section of our block starts, -1 when none."""
    at = inner.rfind(UPDATE_MARK)
    return at if at >= 0 else inner.rfind(_UPDATE_HEADING)


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
    limit: int | None = None,
    language: str | None = None,
) -> str | None:
    """The new description, or None to leave it alone.

    `insights` is our markdown (overview, findings, walkthrough). `language`
    is the repository's review language, for the stamp and the update heading.
    `complement(author,
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
    limit = limit or DESCRIPTION_MAX_CHARS
    if limit > 4 * _HIDDEN_MARGIN:
        limit -= _HIDDEN_MARGIN
    when = now or datetime.now(UTC)
    before, inner, after = split_description(current)
    # Our part gives way, never the author's text and never a marker: whatever
    # the provider's description limit leaves after the author's words is the
    # room the overview has.
    room = max(limit - len(before) - len(after) - _BLOCK_OVERHEAD, _MIN_INSIGHTS)
    if len(insights) > room:
        insights = insights[:room].rstrip() + "\n\n…"
    recorded = _COMMIT_MARK.findall(inner) if inner is not None else []
    same_commit = bool(recorded) and bool(commit) and recorded[-1] == commit
    stamp = _stamp(commit, when, language, same_commit=same_commit)

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

    if new_commits_mode == "append":
        start = _last_update_start(inner)
        if same_commit and start >= 0:
            # A re-run of the commit the last section describes: rewrite that
            # section rather than add a copy of it.
            inner = inner[:start]
        elif same_commit:
            return _rewrite(before, after, inner, insights, stamp, existing_mode,
                            complement)
        section = _join(
            UPDATE_MARK,
            messages.t("description.update", language,
                       when=when.strftime("%Y-%m-%d"),
                       sha=(commit or "")[:7] or "unknown"),
            insights, stamp,
        )
        new = _around(before, _block(_join(inner, section)), after)
        if len(new) <= limit:
            return new
        # Too long to keep every update: the block starts over.
    return _rewrite(before, after, inner, insights, stamp, existing_mode, complement)


def _rewrite(before, after, inner, insights, stamp, existing_mode, complement) -> str:
    if existing_mode == "complement":
        original = _original_of(inner)
        merged = _complemented(_join(original, before + after), insights, complement)
        if merged is not None:
            return _block(_join(merged, stamp))
    return _around(before, _block(_join(insights, stamp)), after)


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
    """Our part of the description: the overview, what was found, the
    walkthrough table and the blocks other stages added.

    The findings sit between the overview and the walkthrough: when the block
    has to be cut to fit the provider's limit the cut comes off the end, and
    the walkthrough is the part that can best be spared.
    """
    from src.review.providers.base import _section_lines, _walkthrough_lines

    language = getattr(batch, "review_language", None)
    overview = (getattr(batch, "pr_overview", "") or "").strip()
    parts = [SUMMARY_HEADING]
    if overview:
        parts.append(overview)
    found = _findings_block(batch, language)
    if found:
        parts.append(found)
    walk = "\n".join(_walkthrough_lines(batch, language)).strip()
    if walk:
        parts.append(walk)
    extra = "\n".join(_section_lines(batch, "description")).strip()
    if extra:
        parts.append(extra)
    if len(parts) == 1:
        return ""
    return "\n\n".join(parts)


def _findings_block(batch: ReviewBatch, language: str | None) -> str:
    """"### Found": how many, of what kind, the first few, and how many are
    comments in the code. "" for a review in which nothing ran — "no issues"
    claims that something looked."""
    from src.review.providers.base import (
        _category_items,
        _severity_breakdown,
        _top_findings_lines,
    )
    from src.review.settings import get_review_settings

    if not batch.findings and not batch.agents_run:
        return ""
    lines = [messages.t("description.found", language), ""]
    if not batch.findings:
        lines.append(messages.t("description.clean", language))
        return "\n".join(lines)
    lines.append(messages.t(
        "description.counts", language, n=len(batch.findings),
        breakdown=_severity_breakdown(batch, language)))
    lines.append("")
    lines.append(messages.t(
        "completed.by_category", language, items=_category_items(batch, language)))
    lines.append("")
    top = _top_findings_lines(batch, language)
    if top:
        lines.extend(top)
        lines.append("")
    inline = len(batch.inline_findings(
        batch.inline_cap(int(get_review_settings().max_inline_comments))))
    if inline:
        lines.append(messages.t("description.inline", language, n=inline))
    return "\n".join(lines).rstrip()


def strip_markers(text: str) -> str:
    """A model's merged text with anything that could forge our block removed."""
    s = re.sub(r"<!--.*?-->", "", str(text or ""), flags=re.DOTALL)
    return s.strip()
