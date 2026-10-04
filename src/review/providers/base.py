"""PullRequestProvider abstract interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.review.models import (
    Finding,
    HunkSide,
    PullRequest,
    ReviewBatch,
    ReviewVerdict,
)


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

    #: The id of the lifecycle comment this provider instance posted or
    #: adopted for the current review — "🔄 reviewing…" first, the final
    #: summary later. `post_review` rewrites THIS comment instead of picking
    #: one by age, so the placeholder and the summary are one comment even
    #: when `replace_on_synchronize` is off and nothing else is upserted.
    _status_comment_id: int | None = None

    def upsert_status_comment(
        self, pr: PullRequest, body: str, *, create: bool = True,
        only_if_in_progress: bool = False,
    ) -> int | None:
        """Write `body` into the persistent, marked summary comment.

        The same comment `post_review` later fills with the final summary:
        the one this instance already wrote, else (when re-runs replace) the
        oldest marked top-level comment of ours, else — only when `create` —
        a new one. `create=False` is for the skip and failure paths, which
        must rewrite a placeholder that exists and never start a thread on a
        pull request nobody is reviewing.

        `only_if_in_progress` is the skip path's guard: the comment is
        rewritten only when it is this instance's own placeholder or an
        existing comment of ours that is STILL a placeholder (carries
        `STATUS_IN_PROGRESS_MARK`, e.g. left by a killed run). A finished
        summary of an earlier commit is never overwritten by a skip.

        Returns the comment id, or None when nothing was written. The default
        writes nothing, so a provider (or a test double) that has not
        implemented it keeps today's behaviour. Implementations may raise;
        the orchestrator treats every failure here as non-fatal.
        """
        return None

    def _our_summary_comments(
        self, pr: PullRequest, marker: str,
    ) -> list[tuple[int, str]]:
        """(id, body) of every top-level marked comment of OURS, oldest first.

        Authorship as in `find_marked_comment_ids`. Defaults to nothing.
        """
        return []

    def _write_top_level_comment(
        self, pr: PullRequest, body: str, existing_id: int | None,
    ) -> int | None:
        """Update `existing_id` or post a new top-level comment. Default: nothing."""
        return None

    def _comment_marker(self) -> str:
        """The review marker, read the way this provider's module reads it."""
        from src.review.settings import get_review_settings

        return get_review_settings().comment_marker

    def upsert_feedback_comment(self, pr: PullRequest, reason: str) -> int | None:
        """The brief "not reviewed: <reason>" note, written at most once per PR.

        For a review skipped or blocked before any placeholder existed, when
        the repository's `status_feedback` is on. It carries the review marker
        AND `STATUS_FEEDBACK_MARK`: a later skip finds it by the second and
        rewrites it in place (never a second note), and a later real review
        adopts or cleans it up like any other marked comment of ours. Only a
        note of ours that IS a feedback note is ever rewritten here — a
        finished summary of an earlier commit is not touched.
        """
        marker = self._comment_marker()
        existing = [
            cid for cid, text in self._our_summary_comments(pr, marker)
            if STATUS_FEEDBACK_MARK in (text or "")
        ]
        return self._write_top_level_comment(
            pr, _format_feedback_comment(pr, reason=reason, marker=marker),
            existing[-1] if existing else None,
        )

    def update_description(self, pr: PullRequest, transform) -> dict:
        """Rewrite the pull request's description through `transform`.

        `transform(current) -> str | None` gets the description as the
        provider holds it NOW (re-read, so an edit made while the review ran
        is not overwritten) and returns the new text, or None to leave it.
        Returns {"written": bool, "error": str | None}. Default: unsupported.
        """
        return {"written": False, "error": "not supported by this provider"}

    def _status_target(
        self, pr: PullRequest, marker: str, *, replace: bool, create: bool,
        only_if_in_progress: bool,
    ) -> tuple[bool, int | None]:
        """(write?, id to update or None for a new comment) — shared by all
        three `upsert_status_comment` implementations."""
        if self._status_comment_id is not None:
            return True, self._status_comment_id
        if only_if_in_progress:
            # Listed regardless of `replace_on_synchronize`: a placeholder a
            # killed run left behind is stale in either mode. The newest one
            # wins — with history kept, older finished summaries are records.
            for cid, text in reversed(self._our_summary_comments(pr, marker)):
                if STATUS_IN_PROGRESS_MARK in (text or ""):
                    return True, cid
            return False, None
        existing = (
            self.find_existing_review_comment(pr.repo, pr.number, marker)
            if replace else None
        )
        if existing is None and not create:
            return False, None
        return True, existing


def _with_marker(body: str, marker: str) -> str:
    """`body` carrying `marker` on its first line — added only when missing.

    The marker is what every later run finds the persistent comment by, so a
    lifecycle body without it would be a comment no re-run can ever replace.
    """
    if not marker or marker in body:
        return body
    return f"{marker}\n{body}"


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


#: How a provider renders `Finding.suggested_code` when it may be committed.
SUGGESTION_GITHUB = "github"
SUGGESTION_GITLAB = "gitlab"


def _fence_for(*texts: str) -> str:
    """A backtick fence longer than any run of backticks inside `texts`."""
    import re

    longest = max((len(m) for t in texts for m in re.findall(r"`+", t or "")), default=0)
    return "`" * max(3, longest + 1)


def _suggestion_parts(
    finding: Finding, *, committable: str | None = None,
    original: list[str] | None = None,
) -> list[str]:
    """The fix part of an inline comment.

    `committable` is set by the provider only when the repository switched
    `committable_suggestions` on AND the anchor is exact (see
    `_committable_span`): GitHub then gets a ```suggestion block, GitLab a
    ```suggestion:-0+N block whose N is the number of lines BELOW the anchored
    one that the replacement also covers. Anything else — a hint, code on a
    provider or anchor that cannot take a one-click commit, Bitbucket — is a
    plain block a human reads: a ```diff when the replaced lines are known,
    plain code otherwise. A hint is never rendered as a committable block:
    structural rules ship prose like "=== / !==", and committing that is a
    broken file.
    """
    parts: list[str] = []
    code = finding.suggested_code
    hint = finding.suggestion
    if code is not None:
        start = finding.line
        end = max(start, int(finding.suggested_end_line or start))
        fence = _fence_for(code, *(original or []))
        parts.append("")
        if committable == SUGGESTION_GITHUB:
            parts += [f"{fence}suggestion", code, fence]
        elif committable == SUGGESTION_GITLAB:
            parts += [f"{fence}suggestion:-0+{end - start}", code, fence]
        elif original:
            parts.append("**Suggested change:**")
            parts.append("")
            parts.append(f"{fence}diff")
            parts += [f"-{ln}" for ln in original]
            parts += [f"+{ln}" for ln in code.split("\n")]
            parts.append(fence)
        else:
            parts.append("**Suggested change:**")
            parts.append("")
            parts += [fence, code, fence]
    if hint and hint.strip() != (code or "").strip():
        fence = _fence_for(hint)
        parts.append("")
        parts.append("**Suggestion:**")
        parts.append("")
        parts += [fence, hint, fence]
    return parts


def _new_side_text(pr: PullRequest) -> dict[tuple[str, int], str]:
    """(path, new-file line) → that line's text, for every line a hunk shows.

    Read off the hunk bodies: context and added lines exist in the new file,
    removed lines do not. Used to show what a suggested change replaces.
    """
    out: dict[tuple[str, int], str] = {}
    for hunk in pr.hunks:
        if not hunk.file_path or hunk.is_binary:
            continue
        n = hunk.new_start
        rows = (hunk.content or "").split("\n")
        if rows and rows[-1] == "":
            rows.pop()
        for raw in rows:
            if raw.startswith("@@") or raw.startswith("\\") or raw.startswith("-"):
                continue
            # "" is a blank context line whose leading space was trimmed.
            out[(hunk.file_path, n)] = raw[1:]
            n += 1
    return out


def _original_lines(
    pr_lines: dict[tuple[str, int], str], finding: Finding,
) -> list[str] | None:
    """The new-file lines a suggested replacement covers, or None if unseen."""
    if finding.suggested_code is None:
        return None
    end = max(finding.line, int(finding.suggested_end_line or finding.line))
    found = [pr_lines.get((finding.file_path, n)) for n in range(finding.line, end + 1)]
    if any(t is None for t in found):
        return None
    return [t for t in found if t is not None]


def _committable_span(
    finding: Finding, anchored_line: int,
    ranges: dict[tuple[str, str], list[tuple[int, int]]],
) -> tuple[int, int] | None:
    """(start, end) when a suggestion can be a one-click commit, else None.

    Three conditions, all about not committing over the wrong lines: the
    finding carries `suggested_code`; the anchor was NOT moved by
    `_snap_to_span` (a moved anchor would apply the replacement to the line
    it was moved to); and the whole range sits on the new side inside ONE
    span the diff carries, which is where GitHub and GitLab accept it.
    """
    if finding.suggested_code is None:
        return None
    if getattr(finding, "side", HunkSide.RIGHT) != HunkSide.RIGHT:
        return None
    start = finding.line
    end = max(start, int(finding.suggested_end_line or start))
    if anchored_line != start:
        return None
    for lo, hi in ranges.get((finding.file_path, "RIGHT"), []):
        if lo <= start and end <= hi:
            return start, end
    return None


def _committable_enabled(batch: ReviewBatch) -> bool:
    actions = getattr(batch, "pr_actions", None)
    return bool(getattr(actions, "committable_suggestions", False))


def _format_finding_body(
    finding: Finding, marker: str = "", *, committable: str | None = None,
    original: list[str] | None = None,
) -> str:
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

    parts.extend(_suggestion_parts(finding, committable=committable, original=original))

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


_THRESHOLD_LABEL = {
    "critical": "critical only",
    "error": "critical + error",
    "warning": "warning and above",
}


def _posting_line(batch: ReviewBatch) -> str:
    """How many of the counted findings became inline comments, and why not
    the rest.

    The severity counts above are ALL findings. Once a repo raises its comment
    threshold, or a review crosses the inline cap, fewer comments arrive than
    the counts promise — and a reader who counts the threads and gets a
    different number concludes the tool lost some. So the gap is said, with
    its cause: "3 shown inline · 7 below the warning threshold (kept in
    Celmis)". Empty when every finding was posted.
    """
    from src.review.settings import get_review_settings

    total = len(batch.findings)
    below = batch.below_threshold_count
    postable = total - below
    cap = batch.inline_cap(int(get_review_settings().max_inline_comments))
    shown = min(postable, max(0, cap))
    over_cap = postable - shown
    if not below and not over_cap:
        return ""
    # "up to": this text is composed before the comments are sent, and a
    # provider can still refuse some of them one by one (GitLab, Bitbucket
    # post per finding). The number is what was SELECTED, said as such.
    parts = [f"up to **{shown}** shown inline"]
    if below:
        label = _THRESHOLD_LABEL.get(
            str(batch.comment_min_severity or "").lower(), "the threshold")
        parts.append(
            f"{below} below the comment threshold ({label}) — recorded in "
            f"Celmis, not posted"
        )
    if over_cap:
        parts.append(f"{over_cap} over the {cap}-comment limit")
    return "_" + " · ".join(parts) + "_"


def _files(n: int) -> str:
    return "file" if n == 1 else "files"


def _format_summary(batch: ReviewBatch, marker: str) -> str:
    """Top-level summary comment markdown — universal for all 3 providers.

    Two shapes. The compact one is what every comment looked like before the
    lifecycle work and stays the default for a hand-built batch. The rich one
    (`batch.rich_summary`, switched on by the orchestrator from the repo
    policy's `summary_enabled`) is Kodus-shaped: a natural-language summary,
    a per-file walkthrough table, findings by severity and by source with the
    top ones listed, and the scope/telemetry folded into <details>.
    """
    if getattr(batch, "rich_summary", False):
        return _format_rich_summary(batch, marker)
    lines: list[str] = []
    lines.append(marker)
    lines.append(_summary_header(batch))
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
        lines.extend(_severity_count_lines(batch))
        lines.append("")
        posting = _posting_line(batch)
        if posting:
            lines.append(posting)
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
    lines.extend(_scope_lines(batch))
    lines.append("")

    # Telemetry
    perf = _performance_line(batch)
    if perf:
        lines.append("### Performance")
        lines.append(perf)
        lines.append("")

    lines.append("---")
    lines.append(f"<sub>Powered by Code Analyzer · {_provenance(batch)}</sub>")
    return "\n".join(lines)


def _summary_header(batch: ReviewBatch) -> str:
    """The summary's first visible line: the repository's own
    `message_finished_header` when it set one, else the built-in heading."""
    from src.review.pr_actions import (
        HEADER_TEMPLATE_MAX_CHARS,
        render_template,
        template_values,
    )

    pr = batch.pull_request
    actions = getattr(batch, "pr_actions", None)
    custom = render_template(
        getattr(actions, "message_finished_header", None),
        template_values(pr, list(batch.agents_run)),
        limit=HEADER_TEMPLATE_MAX_CHARS,
    )
    return custom or f"## 🤖 Code Review for PR #{pr.number}"


def _severity_count_lines(batch: ReviewBatch) -> list[str]:
    lines: list[str] = []
    if batch.critical_count:
        lines.append(f"- 🔴 **Critical:** {batch.critical_count}")
    if batch.error_count:
        lines.append(f"- 🟠 **Error:** {batch.error_count}")
    if batch.warning_count:
        lines.append(f"- 🟡 **Warning:** {batch.warning_count}")
    if batch.info_count:
        lines.append(f"- 💡 **Info:** {batch.info_count}")
    return lines


def _scope_lines(batch: ReviewBatch) -> list[str]:
    pr = batch.pull_request
    lines = [
        f"- Files changed: **{len(pr.changed_files)}**",
        f"- Lines: **+{pr.total_added_lines} / -{pr.total_removed_lines}**",
    ]
    if batch.cross_repo_callers:
        lines.append(
            f"- Cross-repo callers: **{batch.cross_repo_callers}** "
            f"(blast radius via materialized edges)"
        )
    if batch.skipped_files:
        # Two causes with two owners: the install's skip lists and size limit,
        # and this repository's own ignore globs (tagged by the orchestrator).
        by_glob = sum(1 for p in batch.skipped_files
                      if str(p).endswith(" (ignore glob)"))
        other = len(batch.skipped_files) - by_glob
        if other:
            lines.append(
                f"- Skipped: {other} {_files(other)} (lock/binary/generated/too large)")
        if by_glob:
            lines.append(
                f"- Ignored by this repository's ignore globs: {by_glob} {_files(by_glob)}")
    return lines


def _performance_line(batch: ReviewBatch) -> str:
    if not batch.elapsed_seconds:
        return ""
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
    return "- " + " · ".join(parts)


# ─── The Kodus-style summary ─────────────────────────────────────────

#: Rows in the walkthrough table before the rest collapse into "+N more".
WALKTHROUGH_MAX_FILES = 30
#: Findings listed by title under the counts; the inline comments carry the rest.
TOP_FINDINGS_MAX = 10

_SEVERITY_LABEL = {
    "critical": "Critical", "error": "Error", "warning": "Warning", "info": "Info",
}


def _md_cell(text: str, limit: int = 200) -> str:
    """One table cell / one line: whitespace collapsed, pipes escaped, capped."""
    flat = " ".join(str(text or "").split())
    if len(flat) > limit:
        flat = flat[: limit - 1].rstrip() + "…"
    return flat.replace("|", "\\|")


def _file_stats(pr: PullRequest) -> dict[str, tuple[int, int]]:
    stats: dict[str, tuple[int, int]] = {}
    for h in pr.hunks:
        added, removed = stats.get(h.file_path, (0, 0))
        stats[h.file_path] = (added + h.added_lines, removed + h.removed_lines)
    return stats


def _blob_link(pr: PullRequest, path: str, line: int) -> str | None:
    """A link to `path` at `line` in the head commit, when the URL shape is known.

    Built from the pull request's own web URL, so it never points at a host the
    review did not come from: GitHub `/blob/<sha>/<path>#L<n>`, GitLab
    `/-/blob/<sha>/<path>#L<n>`, Bitbucket `/src/<sha>/<path>#lines-<n>`.
    None when the URL or the head sha is missing — the caller prints plain
    text, which is the "where the provider supports it" half of the rule.
    """
    url = (pr.url or "").strip()
    sha = (pr.head_sha or "").strip()
    if not url or not sha or not path or line <= 0:
        return None
    from urllib.parse import quote

    safe_path = quote(path, safe="/")
    if pr.provider == "github" and "/pull/" in url:
        base = url.split("/pull/", 1)[0]
        return f"{base}/blob/{sha}/{safe_path}#L{line}"
    if pr.provider == "gitlab" and "/-/merge_requests/" in url:
        base = url.split("/-/merge_requests/", 1)[0]
        return f"{base}/-/blob/{sha}/{safe_path}#L{line}"
    if pr.provider == "bitbucket" and "/pull-requests/" in url:
        base = url.split("/pull-requests/", 1)[0]
        return f"{base}/src/{sha}/{safe_path}#lines-{line}"
    return None


def _finding_location(pr: PullRequest, finding: Finding) -> str:
    label = f"`{finding.file_path}:{finding.line}`"
    # A LEFT-side finding sits on a line that no longer exists in the head
    # commit, so a link to the head blob would land on the wrong code.
    if getattr(finding, "side", HunkSide.RIGHT) != HunkSide.RIGHT:
        return label
    link = _blob_link(pr, finding.file_path, finding.line)
    return f"[{label}]({link})" if link else label


def _category_of(finding: Finding) -> str:
    if finding.agent:
        return finding.agent
    rule = finding.rule_id or ""
    return rule.split(".", 1)[0] if rule else "other"


def _walkthrough_lines(batch: ReviewBatch) -> list[str]:
    pr = batch.pull_request
    walkthrough = getattr(batch, "walkthrough", None) or {}
    if not walkthrough:
        return []
    stats = _file_stats(pr)
    files = pr.changed_files
    shown = files[:WALKTHROUGH_MAX_FILES]
    lines = [
        "### Changes walkthrough",
        "",
        "| File | +/- | Change summary |",
        "|---|---|---|",
    ]
    for path in shown:
        added, removed = stats.get(path, (0, 0))
        lines.append(
            f"| `{_md_cell(path, 120)}` | +{added} / -{removed} | "
            f"{_md_cell(walkthrough.get(path) or '—')} |"
        )
    more = len(files) - len(shown)
    if more > 0:
        lines.append(f"| _+{more} more {_files(more)}_ | | |")
    lines.append("")
    return lines


def _rich_findings_lines(batch: ReviewBatch) -> list[str]:
    pr = batch.pull_request
    lines = ["### Findings", ""]
    if not batch.findings:
        if batch.agents_run:
            lines.append("_No issues detected._")
            lines.append("")
            return lines
        # Same rule as the compact form: "no issues" is a claim that
        # something looked. Nothing did, and the banner above says why.
        return []

    sev = [
        f"{_severity_emoji(level)} {_SEVERITY_LABEL[level]}: **{count}**"
        for level, count in (
            ("critical", batch.critical_count), ("error", batch.error_count),
            ("warning", batch.warning_count), ("info", batch.info_count),
        )
        if count
    ]
    lines.append("**By severity:** " + " · ".join(sev))
    by_cat: dict[str, int] = {}
    for f in batch.findings:
        cat = _category_of(f)
        by_cat[cat] = by_cat.get(cat, 0) + 1
    lines.append("")
    lines.append("**By source:** " + " · ".join(
        f"{cat}: **{n}**"
        for cat, n in sorted(by_cat.items(), key=lambda kv: (-kv[1], kv[0]))
    ))
    lines.append("")

    postable = batch.postable_findings
    top = postable[:TOP_FINDINGS_MAX]
    if top:
        lines.append("**Top findings:**")
        lines.append("")
        for i, f in enumerate(top, 1):
            title = _md_cell(f.title or f.severity.value.upper(), 160)
            lines.append(
                f"{i}. {_severity_emoji(f.severity.value)} **{title}** — "
                f"{_finding_location(pr, f)}"
            )
        rest = len(postable) - len(top)
        if rest > 0:
            lines.append("")
            lines.append(f"_…and {rest} more in the inline comments._")
        lines.append("")
    posting = _posting_line(batch)
    if posting:
        lines.append(posting)
        lines.append("")
    return lines


def _failure_headline(batch: ReviewBatch) -> str:
    """'❌ Review failed: <reason>' for a run in which no stage completed.

    The reason comes from `agent_errors`, which holds curated sentences only
    (see the orchestrator: never a provider's raw message), so nothing secret
    can reach the pull request through it.
    """
    reasons = sorted({r for a, r in batch.agent_errors.items()
                      if a in batch.agents_failed and r})
    if reasons:
        reason = "; ".join(reasons)
    elif batch.agents_failed:
        reason = f"no review stage completed ({', '.join(batch.agents_failed)})"
    else:
        reason = "no review stage completed"
    return f"### ❌ Review failed: {_md_cell(reason, 300)}"


def _format_rich_summary(batch: ReviewBatch, marker: str) -> str:
    from src.review.models import ReviewRunStatus

    lines: list[str] = [marker, _summary_header(batch), ""]

    if batch.run_status == ReviewRunStatus.FAILED:
        lines.append(_failure_headline(batch))
        lines.append("")
    banner = batch.partial_banner
    if banner:
        lines.append(banner.strip())
        lines.append("")
    lines.append(_verdict_line(batch))
    lines.append("")

    if getattr(batch, "summary_in_description", False):
        # The overview and the walkthrough are in the pull request's
        # description; this comment keeps the verdict and the findings.
        lines.append("_The change summary is in the pull request description._")
        lines.append("")
    else:
        overview = (getattr(batch, "pr_overview", "") or "").strip()
        if overview:
            lines.append("### Summary")
            lines.append("")
            lines.append(overview)
            lines.append("")
        lines.extend(_walkthrough_lines(batch))
    lines.extend(_rich_findings_lines(batch))

    details = ["**Scope**", "", *_scope_lines(batch)]
    perf = _performance_line(batch)
    if perf:
        details += ["", "**Performance**", "", perf]
    lines.append("<details>")
    lines.append("<summary>Scope &amp; performance</summary>")
    lines.append("")
    lines.extend(details)
    lines.append("")
    lines.append("</details>")
    lines.append("")

    lines.append("---")
    lines.append(f"<sub>Powered by Code Analyzer · {_provenance(batch)}</sub>")
    return "\n".join(lines)


# ─── Lifecycle comments: started / skipped / failed ──────────────────

#: Present only in the placeholder, so a later reader (or a test) can tell
#: "still running" from a finished summary without parsing prose.
STATUS_IN_PROGRESS_MARK = "<!-- celmis:review-status:in-progress -->"


def _format_started_comment(
    pr: PullRequest, *, agents: list[str], started_at: str, marker: str = "",
    template: str | None = None,
) -> str:
    """The "🔄 reviewing…" placeholder posted as soon as a review begins.

    `template` is the repository's `message_started`; when it renders to
    anything it replaces the built-in text. The in-progress mark stays either
    way — it is how a later skip or crash recognises the placeholder.
    """
    from src.review.pr_actions import (
        STARTED_TEMPLATE_MAX_CHARS,
        render_template,
        template_values,
    )

    lines = [marker] if marker else []
    custom = render_template(
        template, template_values(pr, agents), limit=STARTED_TEMPLATE_MAX_CHARS,
    )
    if custom:
        return "\n".join([*lines, STATUS_IN_PROGRESS_MARK, custom])
    sha = (pr.head_sha or "")[:7] or "unknown"
    files = len(pr.changed_files)
    roster = ", ".join(f"`{a}`" for a in agents) if agents else "_none_"
    lines += [
        STATUS_IN_PROGRESS_MARK,
        "## 🔄 Celmis is reviewing this PR…",
        "",
        f"- Commit: `{sha}`",
        f"- Agents: {roster}",
        f"- Files to review: **{files}**",
        f"- Started: {started_at}",
        "",
        "_The results will replace this comment when the review finishes._",
    ]
    return "\n".join(lines)


def _format_status_comment(
    pr: PullRequest, *, outcome: str, reason: str, marker: str = "",
) -> str:
    """A terminal state that is not a review: `outcome` is 'skipped' or 'failed'."""
    sha = (pr.head_sha or "")[:7] or "unknown"
    if outcome == "skipped":
        head = f"### ⏭️ Skipped: {_md_cell(reason, 400)}"
        tail = "_Nothing was reviewed for this commit._"
    else:
        head = f"### ❌ Review failed: {_md_cell(reason, 300)}"
        tail = ("_No review was delivered for this commit. Re-run it once the "
                "cause is fixed._")
    lines = [marker] if marker else []
    lines += [
        f"## 🤖 Code Review for PR #{pr.number}",
        "",
        head,
        "",
        f"- Commit: `{sha}`",
        "",
        tail,
    ]
    return "\n".join(lines)


#: Carried (beside the review marker) by the short note a skipped or blocked
#: review leaves when `status_feedback` is on — the key a re-run finds the
#: note by, so a second skip rewrites it instead of adding another.
STATUS_FEEDBACK_MARK = "<!-- celmis:review-status:feedback -->"


def _format_feedback_comment(pr: PullRequest, *, reason: str, marker: str = "") -> str:
    """The brief note for a review that never started: why, and for which commit."""
    sha = (pr.head_sha or "")[:7] or "unknown"
    lines = [marker] if marker else []
    lines += [
        STATUS_FEEDBACK_MARK,
        f"⏭️ **Celmis did not review this pull request** (commit `{sha}`): "
        f"{_md_cell(reason, 400)}.",
    ]
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
