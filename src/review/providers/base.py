"""PullRequestProvider abstract interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.review.models import Finding, PullRequest, ReviewBatch, ReviewVerdict


class PullRequestProviderError(Exception):
    """Provider operation failed (auth/network/api error)."""


class PullRequestProvider(ABC):
    """Provider-agnostic PR ops: fetch + post comments."""

    name: str = ""

    @abstractmethod
    def fetch_pull_request(
        self, repo: str, pr_number: int,
    ) -> PullRequest:
        """Fetch PR metadata + raw diff + parse hunks."""

    @abstractmethod
    def post_review(
        self, batch: ReviewBatch, *, dry_run: bool = False,
    ) -> dict:
        """Submit review (batched comments + summary).

        Returns a dict with the provider-specific response (review_id,
        comment_ids). If dry_run=True — just simulate without an actual
        API call.
        """

    @abstractmethod
    def find_existing_review_comment(
        self, repo: str, pr_number: int, marker: str,
    ) -> int | None:
        """Find existing summary comment with marker — for idempotent updates.

        Returns comment_id if found (for PATCH); None — create a new one.
        """

    def find_marked_comment_ids(
        self, repo: str, pr_number: int, marker: str,
    ) -> list[int]:
        """Every comment of OURS carrying the marker — summary AND inline.

        Authorship is part of the contract, not just the marker: all three
        implementations match author AND marker, because "Quote reply" copies
        the quoted comment's raw markdown — marker included — into a human's
        rebuttal. Defaults to nothing so a provider that has not implemented
        it keeps today's behaviour rather than silently deleting the wrong
        things.
        """
        return []


# ─── Factory ─────────────────────────────────────────────────────


def get_provider_for(
    provider_name: str, *, user_id: str = "default", workspace_id: str = "default",
) -> PullRequestProvider:
    """Return PR provider instance by name.

    Args:
        provider_name: 'github' | 'gitlab' | 'bitbucket'
        user_id: legacy credential owner (default transition tenant only).
        workspace_id: the tenant whose git token to use. The provider resolves
                 strictly this workspace's `ws:{id}` slot (see resolve_git_credential),
                 so a review can never post/clone under another tenant's PAT.

    Raises ValueError if the provider is unknown.
    """
    name = provider_name.lower().strip()
    if name == "github":
        from src.review.providers.github import GitHubPRProvider
        return GitHubPRProvider(user_id=user_id, workspace_id=workspace_id)
    if name == "gitlab":
        from src.review.providers.gitlab import GitLabPRProvider
        return GitLabPRProvider(user_id=user_id, workspace_id=workspace_id)
    if name == "bitbucket":
        from src.review.providers.bitbucket import BitbucketPRProvider
        return BitbucketPRProvider(user_id=user_id, workspace_id=workspace_id)
    raise ValueError(
        f"Unknown PR provider '{provider_name}'. "
        f"Allowed: github | gitlab | bitbucket"
    )


# ─── Severity → emoji helper (cross-provider) ────────────────────


_SEVERITY_EMOJI = {
    "critical": "🔴",
    "error": "🟠",
    "warning": "🟡",
    "info": "💡",
}


def _severity_emoji(severity: str) -> str:
    return _SEVERITY_EMOJI.get(severity.lower(), "•")


def _format_finding_body(finding: Finding, marker: str = "") -> str:
    """Markdown body for an inline comment — universal cross-provider.

    `marker` is the same idempotency token the summary comment carries. Only
    the summary had one, so `replace_on_synchronize` deleted the summary and
    left every inline comment behind — and Bitbucket fires pullrequest:updated
    on every push, so a branch pushed five times collected five full sets of
    inline comments, up to max_inline_comments each.
    """
    parts: list[str] = []
    if getattr(finding, "reasoning", ""):
        # The FIRST line, before the title: the evidence the agent wrote
        # down before it wrote the claim. It went after the body at first
        # (e01531d), and the benchmark image that measured the verifier
        # predated even that — 0 of 77 posted comments carried a Why: line.
        # The judge reads the whole comment and matches on the issue it
        # names, and a human decides in the first line whether to read the
        # second; both get the derivation before the conclusion.
        parts.append(f"*Why:* {finding.reasoning}")
        parts.append("")
    emoji = _severity_emoji(finding.severity.value)
    if finding.title:
        parts.append(f"**{emoji} {finding.title}**")
    else:
        parts.append(f"**{emoji} {finding.severity.value.upper()}**")
    parts.append("")
    parts.append(finding.body or "(no details)")

    if finding.suggestion:
        parts.append("")
        parts.append("```suggestion")
        parts.append(finding.suggestion)
        parts.append("```")

    # The footer always renders: confidence is the agent's own number and
    # belongs where telemetry belongs — visible, last, and never the thing
    # the reader was asked to weigh the finding by.
    meta = []
    if finding.agent:
        meta.append(f"agent: `{finding.agent}`")
    if finding.rule_id:
        meta.append(f"rule: `{finding.rule_id}`")
    meta.append(f"confidence: {finding.confidence:.2f}")
    parts.append("")
    parts.append(f"<sub>{' · '.join(meta)}</sub>")

    if marker:
        # Invisible in rendered markdown on all three providers. It marks the
        # SHAPE of the comment, not its authorship: "Quote reply" copies the raw
        # markdown, marker and all, so a human arguing with a finding ends up
        # holding one too. The cleanup therefore pairs this with the posting
        # account (see `_is_ours` in the github/gitlab providers) — a substring
        # match on its own would delete that human's words.
        parts.append("")
        parts.append(marker)

    return "\n".join(parts)


_VERDICT_EMOJI = {
    ReviewVerdict.APPROVE: "✅",
    ReviewVerdict.COMMENT: "💬",
    ReviewVerdict.REQUEST_CHANGES: "❌",
    ReviewVerdict.SKIPPED: "⏭️",
}

_VERDICT_TEXT = {
    ReviewVerdict.APPROVE: "**APPROVED** — no blocking findings",
    ReviewVerdict.COMMENT: "**COMMENT** — findings to consider",
    ReviewVerdict.REQUEST_CHANGES: "**CHANGES REQUESTED** — blocking findings",
    # SKIPPED used to fall through both .get() defaults and render as
    # "💬 " — a bare speech bubble above "_No issues detected._" on a PR
    # nothing had reviewed. The banner in the summary carries the why; this
    # line only has to stop impersonating a verdict about the code.
    ReviewVerdict.SKIPPED: "**SKIPPED** — nothing was reviewed",
}


def _verdict_line(batch: ReviewBatch) -> str:
    """One verdict line, rendered once for every surface that shows it.

    Both the persistent summary comment and GitHub's immutable review body
    print this line. It is a single function because the two used to be the
    same full text and are now two renderings of one review — if the wordings
    could drift, a PR timeline could call a review APPROVED while the summary
    it points at says otherwise.
    """
    emoji = _VERDICT_EMOJI.get(batch.verdict, "💬")
    return f"{emoji} {_VERDICT_TEXT.get(batch.verdict, '')}"


def _format_review_pointer(batch: ReviewBatch, summary_url: str | None = None) -> str:
    """The body of a SUBMITTED review — a pointer to the summary, never the summary.

    A submitted GitHub review is immutable: the delete endpoint covers PENDING
    reviews only, and a dismissal leaves the body on the page. While the review
    body carried the full summary, every re-run therefore stacked one more full
    copy onto the PR timeline — the one duplication no cleanup can reach. The
    full summary now lives ONLY in the persistent comment, which IS updated in
    place; the review body keeps just the verdict (so the timeline stays
    readable at a glance) and one sentence saying where the rest is.

    Trade-off, accepted by the product owner: GitHub's notification email
    quotes the review body, so the email now carries a pointer instead of the
    content. Self-hosted installs rarely have SMTP configured, so the email
    was mostly never sent anyway.

    `summary_url` is the anchor link to the persistent comment when its id is
    already known — every re-run, because the upsert rewrites the oldest
    comment in place and its id is stable. On the FIRST run the comment does
    not exist until after the review is submitted (a review that fails to post
    must not update the summary first), so the sentence points without a link;
    the comment lands right below the review in the timeline.
    """
    where = (
        f"[the review summary]({summary_url})"
        if summary_url
        else "the review summary comment on this pull request"
    )
    return (
        f"{_verdict_line(batch)}\n\n"
        f"Full findings and scope are in {where} — one persistent comment, "
        f"updated in place on every run."
    )


def _format_summary(batch: ReviewBatch, marker: str) -> str:
    """Top-level summary comment markdown — universal for all 3 providers."""
    pr = batch.pull_request
    lines: list[str] = []
    lines.append(marker)
    lines.append(f"## 🤖 Code Review for PR #{pr.number}")
    lines.append("")

    # The gap notice, before anything else can be read on its own. This
    # function composes the posted comment from findings, scope and telemetry
    # and for one whole wave read neither `summary` nor the banner — so a
    # review in which nothing ran told the run row and the notification
    # "FAILED" while showing the pull-request author "💬 _No issues
    # detected._". It reads `partial_banner` (the property), not `summary`:
    # the early-skip paths write free prose into `summary` that belongs to
    # the run row, and the claude_code engine writes its own error there.
    # The property derives from the same `agents_run`/`agents_failed` state
    # the row persists, so the comment and the row cannot name different gaps.
    banner = batch.partial_banner
    if banner:
        lines.append(banner.strip())
        lines.append("")

    lines.append(_verdict_line(batch))
    lines.append("")

    # Severity summary
    if batch.findings:
        lines.append("### Findings")
        lines.append("")
        if batch.critical_count:
            lines.append(f"- 🔴 **Critical:** {batch.critical_count}")
        if batch.error_count:
            lines.append(f"- 🟠 **Error:** {batch.error_count}")
        if batch.warning_count:
            lines.append(f"- 🟡 **Warning:** {batch.warning_count}")
        if batch.info_count:
            lines.append(f"- 💡 **Info:** {batch.info_count}")
        lines.append("")
    elif batch.agents_run:
        # "_No issues detected._" is a claim that something looked and found
        # the code clean. It used to print unconditionally on zero findings,
        # which put it under the header of reviews in which every agent
        # failed — or none was ever dispatched. Gated on `agents_run` so it
        # is unreachable when nothing ran; the banner above explains those
        # runs instead.
        lines.append("_No issues detected._")
        lines.append("")

    # PR scope
    lines.append("### Scope")
    lines.append(f"- Files changed: **{len(pr.changed_files)}**")
    lines.append(f"- Lines: **+{pr.total_added_lines} / -{pr.total_removed_lines}**")
    if batch.cross_repo_callers:
        lines.append(
            f"- Cross-repo callers: **{batch.cross_repo_callers}** "
            f"(blast radius via materialized edges)"
        )
    if batch.skipped_files:
        lines.append(f"- Skipped: {len(batch.skipped_files)} files (lock/binary/generated)")
    lines.append("")

    # Telemetry
    if batch.elapsed_seconds:
        lines.append("### Performance")
        parts = [
            f"Analysis time: **{batch.elapsed_seconds:.1f}s**",
            # "agents: none" and not "agents: " — this line is reachable for
            # skipped/failed runs now that they post a real comment.
            f"agents: {', '.join(batch.agents_run) or 'none'}",
        ]
        # Only when there are any. The Claude Code engine bills by
        # subscription and never populates these, so every review it produced
        # printed "tokens: 0/0" beside a real $0.21 — a number that reads as a
        # measurement and is an absent field. A missing line is honest; a zero
        # is not.
        if batch.tokens_in or batch.tokens_out:
            parts.append(f"tokens: {batch.tokens_in:,}/{batch.tokens_out:,}")
        lines.append("- " + " · ".join(parts))
        lines.append("")

    lines.append("---")
    lines.append(f"<sub>Powered by Code Analyzer · {_provenance(batch)}</sub>")
    return "\n".join(lines)


def _provenance(batch: ReviewBatch) -> str:
    """What actually produced this review.

    The footer used to be a constant: "context: tree-sitter graph + cross-repo
    edges + Gemini 3 Pro/Flash". Every review carried it, including the ones
    run entirely by the Claude Code engine with `agents_run=["claude_code"]`
    and `cost_source=claude_code_subscription` — so the line under a Claude
    review named Gemini, and the line under a review with no cross-repo group
    claimed cross-repo edges.

    Provenance is the one part of a machine-written comment a reader uses to
    decide how much to trust the rest. A constant there is not a small lie.
    """
    bits: list[str] = ["tree-sitter graph"]
    if getattr(batch, "cross_repo_callers", 0):
        bits.append("cross-repo edges")
    engine = ", ".join(batch.agents_run) if batch.agents_run else ""
    if engine:
        bits.append(engine)
    return "context: " + " + ".join(bits)


# ─── Anchoring an inline comment to a line the diff carries ──────────
#
# Shared by all three providers. It lived in the GitHub provider, where the
# consequence is loudest — GitHub validates a review as ONE object, so a
# single refused anchor 422s the batch and takes every other finding with it.
# GitLab and Bitbucket post per finding, so there a bad anchor loses one
# comment instead of all of them; it was still lost, and lost silently, with
# nothing but a `failed` counter to show for it.
#
# One helper, three providers, so a fix measured on one of them is not a fix
# for one of them.

def _anchorable_ranges(pr: PullRequest) -> dict[tuple[str, str], list[tuple[int, int]]]:
    """(path, side) -> the inclusive line spans GitHub will accept an anchor on.

    A review comment must sit on a line the diff actually carries. The hunk
    header states that span exactly: `@@ -old_start,old_count +new_start,new_count @@`,
    so the new file's postable lines are new_start .. new_start+new_count-1 —
    context lines included, which is why this is a span and not the set of '+'
    lines.

    A count of 0 contributes nothing: a pure-deletion hunk has no new-side line
    to point at, and a pure-addition hunk has no old-side one.

    LEFT spans are registered under both paths a rename gives the file, because
    the finding names whichever one the agent saw.
    """
    ranges: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for hunk in pr.hunks:
        if hunk.new_count > 0 and hunk.file_path:
            ranges.setdefault((hunk.file_path, "RIGHT"), []).append(
                (hunk.new_start, hunk.new_start + hunk.new_count - 1),
            )
        if hunk.old_count > 0:
            span = (hunk.old_start, hunk.old_start + hunk.old_count - 1)
            for path in {hunk.file_path, hunk.old_file_path}:
                if path:
                    ranges.setdefault((path, "LEFT"), []).append(span)
    return ranges


def _snap_to_span(line: int, spans: list[tuple[int, int]]) -> int:
    """The nearest line inside `spans`; `line` itself when it is already in one.

    MEASURED, on the two 14-PR Martian runs (146 findings between them, 69 and
    77): `github:celmis-bench/discourse-graphite#18` is the ONE PR of the 14
    whose findings never reached the pull request — per_pr.json records
    findings 4, posted 0, status "complete". GitHub validates a review as ONE
    object, so the single anchor the API refused (recorded as
    `app/controllers/admin/groups_controller.rb` line 117, on a file of 104
    lines) 422'd the batch and took the other three findings with it.

    It snaps and never drops, and the arithmetic is why. With the golden count
    fixed at 53 the benchmark's F2 is 5*TP / (TP + FP + 212), so one more true
    positive is worth +1.79 F2 and one fewer false positive +0.16 — a factor
    of eleven. A comment is worth posting at any precision above 8%, which no
    snapped anchor is plausibly below. A finding outside the diff is also not
    noise: it is the class architect exists to produce, the untouched caller
    the change breaks, which by design sits outside the changed lines.

    With no span for that file at all — a finding on a file the PR does not
    touch — the line is returned unchanged: there is nowhere to snap TO, and
    the 422 fallback folds it into the summary rather than losing it.

    Ties go to the lower line so the same finding lands in the same place on
    every re-run; a comment that moves between runs is a comment that looks new.
    """
    best_distance: int | None = None
    best_line = line
    for start, end in spans:
        candidate = start if line < start else (end if line > end else line)
        distance = abs(candidate - line)
        if best_distance is None or distance < best_distance or (
            distance == best_distance and candidate < best_line
        ):
            best_distance = distance
            best_line = candidate
    return best_line
