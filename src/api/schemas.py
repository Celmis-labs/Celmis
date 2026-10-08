"""Pydantic request/response schemas for Celmis REST API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

# ─── Auth ─────────────────────────────────────────────────────────────


class LoginRequest(BaseModel):
    # Deliberately `str`, not EmailStr: login is a lookup key, and EmailStr
    # rejects reserved domains like the master account's admin@celmis.local.
    # Address validity is enforced where addresses are CREATED (signup).
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    name: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def _check_password(self):
        # Policy lives in one place so the API and the UI meter agree.
        from src.users.password_policy import validate_password
        try:
            validate_password(self.password, email=str(self.email))
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return self


class ForgotPasswordRequest(BaseModel):
    """Always answered with 200 — never reveals whether the email exists."""

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=16, max_length=256)
    password: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _check_password(self):
        from src.users.password_policy import validate_password
        try:
            validate_password(self.password)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return self


class GoogleCallbackRequest(BaseModel):
    """ID token from Google sign-in (frontend-driven flow)."""

    id_token: str


class OidcCallbackRequest(BaseModel):
    """ID token from a generic OIDC / Keycloak sign-in (frontend-driven flow)."""

    id_token: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    is_admin: bool
    #: The env master account (src/users/roles.py `is_superadmin`) — the only
    #: one who may grant owner, grant anything in a workspace they do not own,
    #: or create a shared workspace.
    is_superadmin: bool = False
    auth_method: str
    has_password: bool
    has_google: bool
    has_oidc: bool = False
    created_at: str
    last_login_at: str | None


# ─── Connections (provider tokens) ────────────────────────────────────


class ConnectionStatus(BaseModel):
    provider: str  # 'github' | 'gitlab' | 'bitbucket'
    connected: bool
    account_label: str = "default"
    metadata: dict[str, object] = Field(default_factory=dict)
    updated_at: str | None = None
    last_used_at: str | None = None


class ConnectionUpsert(BaseModel):
    provider: str = Field(pattern="^(github|gitlab|bitbucket|jira)$")
    #: Four characters at least — except for a Jira save that reuses the
    #: Bitbucket token, where the server supplies it (see `reuse_bitbucket`).
    token: str = Field(default="", max_length=512)
    # Bitbucket needs email (Atlassian API token uses email:token Basic auth)
    email: str | None = None
    workspace: str | None = None  # Bitbucket only
    account_label: str = "default"
    # GitLab only: the instance root (https://gitlab.example.com, or
    # https://host/gitlab under a sub-path). Empty = https://gitlab.com.
    # Validated (https, public address or operator allowlist) before the token
    # is sent anywhere — see src/sync/gitlab_instance.py.
    base_url: str | None = Field(default=None, max_length=2048)
    # Jira only: copy the stored Bitbucket email + token (an unscoped Atlassian
    # API token works for both) instead of typing them again. The server reads
    # the token itself — it never travels to the browser — and verifies it
    # against the Jira site before saving anything.
    reuse_bitbucket: bool = False

    @model_validator(mode="after")
    def _token_present(self) -> ConnectionUpsert:
        if not (self.reuse_bitbucket and self.provider == "jira") and len(self.token) < 4:
            raise ValueError("token: at least 4 characters")
        return self


class ConnectionVerifyResult(BaseModel):
    ok: bool
    provider: str
    username: str | None = None
    error: str | None = None
    # GitLab: the normalised instance the token was verified against.
    base_url: str | None = None
    # Token scopes/permissions when the provider exposes them (GitHub classic
    # PATs report them in a header). Empty when unknown (fine-grained PATs,
    # GitLab, Bitbucket) — the caller shows "could not read scopes".
    scopes: list[str] = Field(default_factory=list)


# ─── Repositories ─────────────────────────────────────────────────────


#: What a call did about a repository's graph. The values are the INDEX_*
#: constants in src/repos/indexing.py — spelled out here so the OpenAPI schema
#: (and the TypeScript generated from a person reading it) lists them.
RepoIndexStatus = Literal[
    "queued", "already_queued", "already_indexed",
    "not_requested", "queue_unavailable",
]


class RepoWebhookOut(BaseModel):
    """A repository's review webhook, as far as we know. Never carries a secret.

    `status`: installed | not_installed | failed | skipped | unknown.
    `reason` is a short code when not installed (permission, auth, not_found,
    no_public_url, no_credentials, not_admin, rejected, network, …), `message`
    says what happened and `hint` what the token needs.
    """

    provider: str
    status: str
    url: str | None = None
    events: list[str] = Field(default_factory=list)
    hook_id: str | None = None
    action: str | None = None
    reason: str | None = None
    message: str | None = None
    hint: str | None = None
    last_delivery: dict[str, Any] | None = None
    full_name: str | None = None
    updated_at: str | None = None
    #: Events the receiver acts on that this hook does not subscribe to (a hook
    #: installed before comment commands existed); `outdated` is "there are
    #: some" — pressing Install webhook repairs it in place.
    missing_events: list[str] = Field(default_factory=list)
    outdated: bool = False


class RepairedRepoOut(BaseModel):
    """One repository `POST /api/repos/webhooks/repair-outdated` touched."""

    repo_slug: str
    #: installed (repaired) | failed | skipped … — the installer's own word.
    status: str
    reason: str | None = None
    message: str | None = None


class RepairOutdatedOut(BaseModel):
    repos: list[RepairedRepoOut] = Field(default_factory=list)


class RepoOut(BaseModel):
    slug: str  # internal slug e.g. github_owner-name
    provider: str
    #: Name given at upload for an ``upload`` repository (None for git repos).
    display_name: str | None = None
    full_name: str  # owner/repo
    url: str
    indexed: bool
    #: How many symbols the graph holds, or None when nobody counted.
    #:
    #: It was `int = 0`, and the list endpoint filled it with a literal zero
    #: under the comment "cheap; populate via graph stats endpoint if needed" —
    #: an endpoint that does not exist. So a repository holding 31 symbols was
    #: indistinguishable in the repo list from one holding none, and the number
    #: read as a measurement.
    #:
    #: None is not the same as 0 and the UI must not render it as one: 0 means
    #: an indexed repository with nothing in it, None means the question was
    #: not asked.
    symbol_count: int | None = None
    auto_review_enabled: bool = False
    auto_review_mode: str = "polling"  # 'polling' | 'webhook' | 'manual'
    branch: str | None = None  # None → provider default branch
    #: Target-branch patterns automatic review runs for (`!` excludes; [] =
    #: every branch), resolved repo policy > workspace defaults > install by
    #: the orchestrator's own resolver. LIST only; None = not resolved.
    target_branches: list[str] | None = None
    #: Which layer `target_branches` came from: repo | workspace | install.
    target_branches_source: Literal["repo", "workspace", "install"] | None = None
    #: True only when THIS call put a new full-index job in the queue.
    index_queued: bool = False
    #: Why `index_queued` is what it is. None on responses that started
    #: nothing and are not claiming to — the list, the branch and auto-review
    #: toggles. Silence here used to be the only answer available, and it is
    #: how 161 benchmark reviews ran on repositories that had no graph.
    index_status: RepoIndexStatus | None = None
    #: What the last index of this repo actually did, from `repo_index_state`
    #: (src/repos/index_state.py). `indexed` above is a file-exists check and
    #: cannot tell an hour-old graph from a March one, cannot name the
    #: revision it was built from, and reads a repo whose indexing has failed
    #: six times exactly like a repo nobody has asked to index — the state
    #: that let 161 benchmark reviews run with no graph and no surface able to
    #: say so. All None means "nothing recorded", which is also what a
    #: database this process cannot reach looks like: the list must render
    #: either way, so the fields degrade to null rather than to a 500.
    #: Only the LIST fills them in; the responses that started something
    #: (register, index, the toggles) leave them null under the same rule
    #: `index_status` follows — a call answers for what it did.
    last_indexed_sha: str | None = None
    last_indexed_at: datetime | None = None
    last_full_rebuild_at: datetime | None = None
    #: The newest attempt AFTER the last success, if that one died. Set with a
    #: non-null `last_indexed_sha` it reads "the graph is from X and the
    #: attempt after it failed"; set with a null one it reads "this repo has
    #: never indexed successfully", which is the badge state `indexed=False`
    #: alone cannot distinguish from "nobody asked yet".
    last_index_error: str | None = None
    last_index_error_at: datetime | None = None
    #: WHEN THE REMOTE WAS LAST ASKED, and what it said — a different question
    #: from when the index was built. "Indexed three days ago" means either
    #: nobody has looked since or we looked this morning and the branch has
    #: not moved, and those are the two answers a person wants told apart.
    last_checked_at: datetime | None = None
    last_remote_sha: str | None = None
    #: Why the last check failed, if it did. A check that cannot reach the
    #: remote must not render as "no new changes": a wrong answer carrying a
    #: fresh timestamp is worse than no answer.
    last_check_error: str | None = None
    #: True / False / **null**, and null is an answer. It means we cannot say —
    #: never checked, the check failed, or nothing recorded to compare
    #: against. Rendering null as "up to date" is the same mistake as
    #: reporting zero vulnerabilities for an ecosystem nobody scanned.
    up_to_date: bool | None = None
    #: The review webhook's last known state (no live provider call on the
    #: list). On POST /api/repos: the outcome of the automatic install.
    webhook: RepoWebhookOut | None = None


class RepoAddRequest(BaseModel):
    """Add by URL — provider auto-detected."""

    url: str = Field(min_length=4, max_length=512)
    auto_review: bool = False
    # Empty/omitted → clone whatever the provider calls the default branch.
    branch: str | None = Field(default=None, max_length=255)
    #: Queue the graph index as part of registering. Default True because that
    #: is what one person adding one repository means: a registered repo that
    #: nothing clones gets reviewed on the diff alone. False is for bulk
    #: registration — the 50 Martian-bench forks cost 57.9 GB of clone, and a
    #: script that wants the rows without the disk has to be able to say so.
    index: bool = True


class RepoBranchUpdate(BaseModel):
    """PATCH /api/repos/{slug}/branch — null/empty resets to default branch."""

    branch: str | None = Field(default=None, max_length=255)


class RepoBrowseItem(BaseModel):
    """Item in the browse-from-provider list."""

    full_name: str
    url: str
    description: str = ""
    private: bool = False
    default_branch: str = "main"
    already_added: bool = False


class RepoOwnerItem(BaseModel):
    """One account/organisation the connected token can see repos under.

    `repo_count` is what the scan found, not what the provider holds: the
    listing is capped, so a very large account reports the first N. It orders
    the list and tells the user which owner is the busy one — it is not a
    figure to quote anywhere.
    """

    owner: str
    repo_count: int = 0
    #: True when at least one repo of this owner is already registered here.
    has_registered: bool = False


class RepoDeveloperItem(BaseModel):
    """One person who commits to repositories the caller can reach.

    `identity` is whatever the source calls them — a git author email for
    registered repos, a provider login when browsing. The two are not the same
    namespace and are never merged: guessing that `a.dev@corp.com` and `adev`
    are one person is the kind of wrong that quietly drops a repository from a
    scope.
    """

    identity: str
    #: Other identities folded into this one — the same person's second git
    #: config, their machine-local address. Shown so a grouping is visible
    #: rather than silently applied.
    aliases: list[str] = []
    #: Human name when the source has one distinct from `identity`.
    display_name: str = ""
    repos: list[str] = []
    repo_count: int = 0
    #: Commits seen in the scanned window. Ordering only — not a total.
    commits: int = 0
    #: A machine, not a colleague — `root@some-server`, a CI account. Real
    #: commits, so they are reported rather than dropped, but they belong
    #: behind a toggle instead of between two people.
    is_robot: bool = False


class RepoDeveloperScan(BaseModel):
    """Provider-side developer scan, with what it actually covered.

    Contributors cost one provider request per repository, so the scan is
    bounded. Saying how many of how many were read is the difference between
    a short list and a wrong one.
    """

    developers: list[RepoDeveloperItem] = []
    scanned: int = 0
    total: int = 0


class AutoReviewToggle(BaseModel):
    enabled: bool
    mode: str = Field(default="polling", pattern="^(polling|webhook|manual)$")


# ─── Pull Requests (Bitbucket manual mode + listings) ─────────────────


class PullRequestSummary(BaseModel):
    provider: str
    repo: str  # owner/name
    number: int
    title: str
    author: str
    state: str
    url: str
    created_at: str | None = None
    updated_at: str | None = None


# ─── Reviews ──────────────────────────────────────────────────────────


class ReviewTriggerRequest(BaseModel):
    pr_ref: str = Field(min_length=4, max_length=512)
    post_comments: bool = True
    #: Review even if the base branch is outside the target patterns
    #: (`ReviewRequest.force`). A manual request already skips the draft,
    #: title and cadence gates.
    force: bool = False


class ParameterAdjustmentOut(BaseModel):
    """One parameter Celmis changed between what was asked and what was sent.

    Mirrors `ParameterAdjustment.as_dict()` in src/llm/capabilities.py —
    what the run row stores. `parameter` and `action` are OPEN vocabularies,
    deliberately plain strings: today they carry max_output_tokens |
    reasoning | temperature | model with clamped | dropped | swapped, plus
    the graph stage's graph_context with unavailable | partial |
    base_too_old, and a value this list has not heard of must reach the page
    as itself rather than be rejected here (the reviews table renders an
    unknown word raw for exactly that reason). `reason` is the provider's own
    sentence when there is one and the rule otherwise ("model ceiling is
    65535"); `model` names the model the parameter was fitted to, so the page
    can say "refused by gemini-3.7-flash" and not just "refused".

    Every field has a default on purpose: the rows are JSON written by
    whichever version of the pipeline was deployed at the time, and a history
    request must not 500 because one of them grew or lost a key.
    """

    agent: str | None = None
    parameter: str = ""
    requested: Any = None
    sent: Any = None
    action: str = ""
    reason: str = ""
    model: str | None = None


class HiddenReportOut(BaseModel):
    """What a run hid before posting, by cause.

    `by_rule` is the deny-list's count per rule id (`ReviewSettings.
    suppressed_rules`, or the repo policy's own list); the rest are the
    prefilter's merges, the confidence floor, the claims the parser refused
    for want of evidence, and the LLM veto's drops. Every field defaults,
    like ParameterAdjustmentOut and for the same reason.
    """

    by_rule: dict[str, int] = Field(default_factory=dict)
    duplicates: int = 0
    near_duplicates: int = 0
    low_confidence: int = 0
    no_evidence: int = 0
    coverage_claim: int = 0
    veto: int = 0
    #: Hidden because the team dismissed the same finding before.
    learned: int = 0
    #: Shadow mode: how many the learned filter would have hidden, and which.
    learned_would_hide: int = 0
    learned_items: list[dict[str, Any]] = Field(default_factory=list)


class ReviewRunOut(BaseModel):
    id: str
    pr_ref: str
    verdict: str
    findings_count: int
    critical: int = 0
    error: int = 0
    warning: int = 0
    info: int = 0
    cross_repo_callers: int = 0
    #: Deterministic cross-repo drift hits. Separate from findings_count,
    #: which counts the model's findings only — a run can have none of those
    #: and still have caught a constant left behind in a sibling repository.
    drift_hits: int = 0
    posted: bool = False
    started_at: str
    elapsed_seconds: float | None = None
    summary: str = ""
    # Stage 11 — cost tracking (BYOK)
    cost_usd: float | None = None
    cost_source: str | None = None      # 'openrouter_actual' | 'manual_price' | 'proxy_price' | 'litellm_estimate' | 'unknown' | 'mixed'
    tokens_input: int = 0
    tokens_output: int = 0
    #: Lifecycle state of the run — 'queued' | 'running' | 'complete' |
    #: 'partial' | 'failed' | 'skipped' (ReviewRunStatus in src/review/models.py).
    #: 'partial' is the Kodus PARTIAL_ERROR case: the comments were posted and
    #: a stage is missing from them. 'skipped' means nothing was ever
    #: dispatched — an early skip or a policy disabling every agent.
    status: str = "queued"
    #: Which agents answered, and which failed to.
    #:
    #: null, not [], for runs recorded before these were persisted — the
    #: difference between "nothing failed" and "nobody wrote it down" is the
    #: whole reason the fields exist, so a consumer must not read the absence
    #: as an all-clear.
    agents_run: list[str] | None = None
    agents_failed: list[str] | None = None
    #: Switched off by policy (or the verifier with its LLM veto disabled) —
    #: the third state that keeps "absent from agents_run" readable. Skipped
    #: is a decision, failed is an accident; None is a row written before the
    #: column existed.
    agents_skipped: list[str] | None = None
    #: Comment-cleanup outcome from the provider's post step —
    #: {deleted, failed, kept_threaded, complete}. `complete: False` means
    #: duplicates from an earlier run may still be on the PR, and the UI
    #: says so instead of letting a half-done cleanup look like a finished
    #: one. None when the run never posted or predates the column — absence
    #: of a report, not a clean one. A plain dict on purpose: three
    #: providers build it, and history must not 500 over a grown key.
    cleanup: dict | None = None
    #: What Celmis changed behind the operator's back during this run — a
    #: ceiling clamped to the model max, a reasoning word or a temperature the
    #: provider refused, a fallback model called — with what was asked, what
    #: was sent and why. Shipped on GET /api/reviews/{id} only: /history rows
    #: carry `adjustments_count` instead, so the list view can badge a run
    #: without shipping the list. null there means "not shipped"; on the
    #: detail view null means "not recorded" (a row written before the
    #: column), which a consumer must not read as "nothing was adjusted" —
    #: the same rule as `agents_failed`.
    parameter_adjustments: list[ParameterAdjustmentOut] | None = None
    #: How many adjustments the run carries, on every row. 0 for a run that
    #: sent exactly what was asked AND for a row that predates the record;
    #: the detail view's null tells those apart.
    adjustments_count: int = 0
    #: What the run hid and why. null means "not recorded" (a row written
    #: before the column), which a consumer must not read as "nothing was
    #: hidden" — the same rule as `parameter_adjustments`.
    hidden: HiddenReportOut | None = None
    #: When the run ended; null while it is queued or running.
    finished_at: str | None = None
    #: One sentence: why the run ended the way it did — "Skipped — Branch
    #: mismatch: target branch 'master' does not match configured patterns
    #: ['main']". On every row, list and detail alike.
    status_reason: str | None = None
    #: The PR the run reviewed, when known.
    pr_provider: str | None = None
    pr_repo: str | None = None
    pr_number: int | None = None
    #: What the run read: "full" (the whole pull request) or "incremental"
    #: (only the commits since `scope_base_sha`); null when not recorded.
    scope: str | None = None
    scope_base_sha: str | None = None
    #: The ordered stages (src/review/stages.py). Shipped on the detail view
    #: and on a pull request's run list; null on /history rows ("not
    #: shipped") and on runs recorded before stages existed ("not recorded").
    stages: list[ReviewStageOut] | None = None


class ReviewStageOut(BaseModel):
    """One stage of a review run — Kodus-style timeline row."""

    #: Stable id: received | queued | retry | fetch_pr | settings |
    #: ignore_globs | gate_enabled | gate_target_branch | gate_draft | gate_title |
    #: gate_cadence | scope | context
    #: | gate_size | gate_hunks | summary | agent:<name> | verifier |
    #: breaking_change | compliance | publish | record | finished. Open
    #: vocabulary — the page renders an unknown key by its `name`.
    key: str
    #: English label, the fallback when the page has no translation for `key`.
    name: str
    #: success | skipped | failed | running
    status: str
    started_at: str | None = None
    duration_ms: int | None = None
    #: A sentence, written from templates — never a provider's error text.
    reason: str = ""
    #: Scalars for chips: model, tokens_in, tokens_out, findings, …
    meta: dict | None = None


ReviewRunOut.model_rebuild()


class QueuedReviewOut(BaseModel):
    """One PR's answer to a manual or bulk review request."""

    number: int
    run_id: str | None = None
    #: queued | inline | duplicate | failed
    status: str
    reason: str = ""


class BulkReviewIn(BaseModel):
    """POST /api/repos/{slug}/pulls/review-all.

    Which PRs: `numbers` when given, otherwise every open PR matching `q` and
    `branch` — the list the page is showing. `confirm` must be true: a bulk
    review spends model budget on up to `BULK_LIMIT` PRs at once and is never
    started by a stray request.
    """

    numbers: list[int] | None = Field(default=None, max_length=200)
    q: str = Field(default="", max_length=200)
    branch: str | None = Field(default=None, max_length=255)
    post_comments: bool = True
    confirm: bool = False


class BulkReviewOut(BaseModel):
    #: PRs the request tried to review — targeted ones only.
    requested: int
    queued: int
    items: list[QueuedReviewOut]
    #: PRs left out before queuing because their base branch is not in the
    #: repo's target branches (status "skipped", reason "base branch not
    #: targeted"). Not counted in `requested`, so they do not eat BULK_LIMIT.
    skipped: list[QueuedReviewOut] = Field(default_factory=list)


class OpenPullOut(BaseModel):
    """An open PR/MR of a registered repository, any target branch."""

    provider: str
    repo: str
    number: int
    title: str
    author: str
    state: str = "open"
    url: str
    created_at: str | None = None
    updated_at: str | None = None
    source_branch: str | None = None
    target_branch: str | None = None
    draft: bool = False
    #: The newest Celmis run of this PR, if any.
    last_review_status: str | None = None
    last_review_reason: str | None = None
    last_run_id: str | None = None
    last_review_at: str | None = None
    #: False when the repo's effective target branches leave this PR's base
    #: branch out — a review would be skipped by the orchestrator's gate.
    targeted: bool = True


class OpenPullListOut(BaseModel):
    items: list[OpenPullOut]
    #: Matching PRs before `limit`/`offset`.
    total: int
    #: Open PRs read from the provider before filtering.
    open_total: int
    limit: int
    offset: int
    #: True when the provider listing was cut at the read cap.
    truncated: bool = False
    #: Distinct target branches of the open PRs, for the filter.
    target_branches: list[str] = Field(default_factory=list)
    #: The most PRs one "Review all open PRs" request may queue.
    bulk_limit: int = 25
    #: The repo's effective target-branch patterns (repo > workspace default);
    #: empty = every branch is targeted.
    effective_target_branches: list[str] = Field(default_factory=list)
    #: Matching PRs (before limit/offset) whose base branch is targeted — what
    #: "Review all" would actually queue.
    targeted_total: int = 0


# ═══════════════════════════════════════════════════════════════════
# Phase 2 — Projects + Chats + Q&A
# ═══════════════════════════════════════════════════════════════════


class ProjectRepoIn(BaseModel):
    """Adding a repo to a project — POST body."""

    repo_slug: str = Field(min_length=1, max_length=200)
    role: str | None = Field(default=None, max_length=64)
    include_globs: list[str] | None = Field(default=None, max_length=50)
    exclude_globs: list[str] | None = Field(default=None, max_length=50)


class ProjectRepoPatch(BaseModel):
    """Narrow what a project looks at in one of its repos — PATCH body.

    Only the lists that are sent change. Empty ``include_globs`` = every file;
    ``exclude_globs`` always wins.
    """

    include_globs: list[str] | None = Field(default=None, max_length=50)
    exclude_globs: list[str] | None = Field(default=None, max_length=50)


class ProjectRepoOut(BaseModel):
    repo_slug: str
    role: str | None
    added_at: datetime
    include_globs: list[str] = Field(default_factory=list)
    exclude_globs: list[str] = Field(default_factory=list)
    model_config = ConfigDict(from_attributes=True)


class ProjectIn(BaseModel):
    """Create / update project."""

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    repos: list[ProjectRepoIn] = Field(default_factory=list, max_length=20)


class ProjectOut(BaseModel):
    id: str
    name: str
    description: str | None
    owner_user_id: str | None
    created_at: datetime
    updated_at: datetime
    repos: list[ProjectRepoOut] = Field(default_factory=list)
    chats_count: int = 0
    model_config = ConfigDict(from_attributes=True)


class AvailableRepo(BaseModel):
    """Repo with vault readiness for Q&A — from a Qdrant scan."""

    repo_slug: str
    vault_points: int  # how many points are in Qdrant
    is_ready: bool  # True if vault_points > 0
    in_projects: list[str] = Field(default_factory=list)  # ids of projects that contain it


# ─── Chats ────────────────────────────────────────────────────────────


class ChatIn(BaseModel):
    """Create chat — either bound to a project, or to one repo."""

    project_id: str | None = None
    repo_slug: str | None = None  # for backward-compat single-repo
    name: str | None = Field(default=None, max_length=300)

    model_config = ConfigDict(extra="forbid")


class MessageMeta(BaseModel):
    """Meta for an assistant message — telemetry / context."""

    type: str | None = None  # technical | functional | overview | …
    route: str | None = None  # A | B | C
    tokens_in: int = 0
    tokens_out: int = 0
    vault_hits: list[dict[str, Any]] = Field(default_factory=list)
    files_read: list[str] = Field(default_factory=list)
    files_read_count: int = 0
    elapsed_s: float | None = None
    error: bool = False


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    timestamp: datetime
    meta: dict[str, Any] | None = None
    model_config = ConfigDict(from_attributes=True)


class ChatOut(BaseModel):
    id: str
    project_id: str | None
    repo_slug: str | None
    name: str | None
    owner_user_id: str | None
    created_at: datetime
    updated_at: datetime
    messages_count: int = 0
    messages: list[MessageOut] = Field(default_factory=list)
    model_config = ConfigDict(from_attributes=True)


class AskRequest(BaseModel):
    """POST to /api/chats/{id}/messages — a request for a new answer."""

    content: str = Field(min_length=1, max_length=10_000)
    stream: bool = True  # SSE or a single-block response
    include_code: bool = True  # show source code in the answer (toggle)

    model_config = ConfigDict(extra="forbid")


# ─── Review policies (Stage 10) ──────────────────────────────────────


class FolderRule(BaseModel):
    """Glob pattern → extra prompt fragment applied when files match.

    The three optional fields make it a structured custom rule. A row stored
    before they existed is `{pattern, prompt}` and still reads — and renders —
    exactly as it always did: no title, no severity hint, every agent.
    """

    pattern: str = Field(min_length=1, max_length=200, description="Glob like 'src/api/**/*.py'")
    prompt: str = Field(min_length=1, max_length=4000)
    #: Short name, printed as the rule's heading in the prompt.
    title: str | None = Field(default=None, max_length=200)
    #: The severity an agent should report a violation at.
    severity_hint: Literal["info", "warning", "error", "critical"] | None = None
    #: The agents the rule is for (a subset of the LLM finders); [] = all.
    #: Checked against the roster by the router.
    agents: list[str] = Field(default_factory=list, max_length=10)

    model_config = ConfigDict(extra="forbid")


class ReviewPolicyIn(BaseModel):
    """Body for PUT /api/review-policies/{slug} — full upsert."""

    enabled: bool = True
    prompt_template: str = Field(default="", max_length=20_000)
    # Three states, told apart by `model_fields_set`: the key ABSENT keeps
    # what is stored (a new row inherits); null inherits the workspace review
    # defaults; a list — [] ("every branch") included — is this repo's own.
    target_branches: list[str] | None = Field(default=None, max_length=50)
    folder_rules: list[FolderRule] = Field(default_factory=list, max_length=20)
    department: str | None = Field(default=None, max_length=128)
    # Per-agent model overrides (Stage 11). NULL = workspace default.
    # THE model for this layer — `agent_llm_overrides` below carries no
    # `model` key, and the router refuses one that tries.
    architect_model: str | None = Field(default=None, max_length=200)
    security_model: str | None = Field(default=None, max_length=200)
    quality_model: str | None = Field(default=None, max_length=200)
    tests_model: str | None = Field(default=None, max_length=200)
    verifier_model: str | None = Field(default=None, max_length=200)
    # The other two per-agent knobs, in the shape the workspace `agents` blob
    # already uses: {"architect": {"max_output_tokens": 32768,
    # "reasoning": "high"}}. Every field optional, absent meaning "inherit".
    #
    # Three-state on purpose, exactly like `LLMConfigIn.agents`:
    #   omitted / null  → leave the stored map alone (a client that predates
    #                     this field cannot wipe what a newer one saved)
    #   {}              → clear every override
    #   {"agent": null} → clear that one agent
    # The map is otherwise sent WHOLE and replaces the stored one, because
    # absent already means "inherit" at every layer: omitting a field is the
    # only way a form can say "stop overriding that", and a per-key merge
    # would read that as "leave it alone" and keep a value the operator
    # watched disappear from the screen.
    agent_llm_overrides: dict[str, dict | None] | None = None
    # Per-repo per-agent system prompt REPLACEMENTS (the advanced mode).
    # Empty string / missing key means "inherit the workspace replacement →
    # agent default".
    agent_prompt_overrides: dict[str, str] = Field(default_factory=dict)
    # Per-repo per-agent team guidelines — ADDED to the agent's prompt in a
    # delimited block (src/review/prompt_guidelines.py), at most
    # GUIDELINES_MAX_CHARS each: longer is a 422, never a silent cut. ABSENT
    # keeps what is stored (a client that predates the field cannot wipe
    # it); a map replaces it whole — a missing agent or an empty value
    # inherits the workspace's guidelines for that agent.
    agent_prompt_guidelines: dict[str, str] | None = None
    # Agents whose guidelines here are ADDED to the workspace's instead of
    # replacing them. ABSENT keeps; a list replaces ([] = replace for every
    # agent, the default). Unknown names are dropped by the router.
    agent_guidelines_extend: list[str] | None = Field(default=None, max_length=20)
    # Per-repo MCP evidence sources (Stage 13).
    mcp_sources: list[dict] = Field(default_factory=list)
    # Agents that must not run for this repo (no LLM call, no findings).
    # Unknown names are dropped by the router. Three states like
    # `target_branches`: absent keeps, null inherits the workspace review
    # defaults, a list — [] ("every agent runs") included — is this repo's own.
    disabled_agents: list[str] | None = Field(default=None, max_length=20)
    # Rule ids the review prefilter hides for this repo. Three states on the
    # way in, told apart by `model_fields_set`: the key ABSENT keeps what is
    # stored (so a client that cannot render this control — the policy page
    # today — does not wipe it on every save, the same courtesy
    # `agent_llm_overrides` extends); an explicit `null` goes back to the code
    # default; a list — `[]` included — replaces the default outright.
    suppressed_rules: list[str] | None = Field(default=None, max_length=200)
    # Whether the LLM false-positive veto runs for this repo. Three states,
    # told apart by `model_fields_set` exactly as `suppressed_rules` is: the
    # key ABSENT keeps what is stored, an explicit `null` goes back to
    # inheriting the install default, and true/false is this repository's own
    # decision. The default is off — see `ReviewSettings.verifier_enabled`.
    verifier_enabled: bool | None = None
    # Paths this repo's review never reads (gitignore-ish globs, see
    # src/review/ignore_globs.py). Absent keeps what is stored; null inherits
    # the workspace review defaults; [] is "nothing extra" for this repo.
    ignore_globs: list[str] | None = Field(default=None, max_length=200)
    # Lowest severity posted as an inline comment: critical | error | warning
    # | info. Absent keeps what is stored; null inherits (= post everything).
    comment_min_severity: str | None = None
    # Review output (Kodus-style). Every one: key ABSENT keeps what is stored,
    # so a client that does not render the control cannot reset it.
    #   summary_enabled / started_comment_enabled — null inherits the
    #                     workspace review default (then on).
    #   summary_instructions — null or "" inherits.
    #   review_language — a code from src.llm.prompts.language; null or ""
    #                     inherits the workspace language.
    #   max_inline_comments — 1..100; null inherits REVIEW_MAX_INLINE_COMMENTS.
    summary_enabled: bool | None = None
    summary_instructions: str | None = Field(default=None, max_length=4000)
    started_comment_enabled: bool | None = None
    review_language: str | None = Field(default=None, max_length=16)
    max_inline_comments: int | None = Field(default=None, ge=1, le=100)
    # The 2.3.0 finders' models — columns, like the five above, but with the
    # three-state courtesy the newer fields extend: ABSENT keeps what is
    # stored (the policy page predates them and must not wipe them on save),
    # null clears, a string pins.
    performance_model: str | None = Field(default=None, max_length=200)
    business_logic_model: str | None = Field(default=None, max_length=200)
    # ── 2.3.0 review settings — the same fields, limits and validation as
    # `WorkspaceReviewDefaultsIn` (src.review.review_defaults says what each
    # means). ABSENT keeps, null inherits, a value is this repository's own.
    # Enums and the agent names are checked by the router, for a 422 that
    # names the choices; blank text is stored as null (inherit).
    enabled_agents: list[str] | None = Field(default=None, max_length=20)
    run_on_drafts: bool | None = None
    approve_when_clean: bool | None = None
    request_changes_on_critical: bool | None = None
    status_feedback: bool | None = None
    committable_suggestions: bool | None = None
    apply_filters_to_rules: bool | None = None
    summary_target: str | None = Field(default=None, max_length=32)
    summary_on_new_commits: str | None = Field(default=None, max_length=32)
    summary_existing_description: str | None = Field(default=None, max_length=32)
    base_instruction: str | None = Field(default=None, max_length=2000)
    message_started: str | None = Field(default=None, max_length=2000)
    message_finished_header: str | None = Field(default=None, max_length=2000)
    completed_comment: str | None = Field(default=None, max_length=32)
    commands_guide_enabled: bool | None = None
    review_cadence: str | None = Field(default=None, max_length=32)
    review_scope: str | None = Field(default=None, max_length=32)
    auto_pause_pushes: int | None = Field(default=None, ge=2, le=20)
    auto_pause_window_minutes: int | None = Field(default=None, ge=1, le=240)
    ignored_title_keywords: list[str] | None = Field(default=None, max_length=50)
    commands_enabled: bool | None = None
    chat_enabled: bool | None = None
    command_permission: str | None = Field(default=None, max_length=32)
    # Learning (src.review.memories): memories on/off, whether a machine's
    # proposal waits for a person, and whose "remember" is active at once.
    memories_enabled: bool | None = None
    knowledge_approval: bool | None = None
    memory_trusted_commenters: list[str] | None = Field(default=None, max_length=100)
    # Issues backlog: resolve a merged PR's open issue once the target branch
    # no longer has it. Booleans inherit on null; the cap is 0..50 model calls.
    issues_auto_resolve: bool | None = None
    issues_resolve_llm_verify: bool | None = None
    issues_resolve_max_llm: int | None = Field(default=None, ge=0, le=50)
    issues_announce_resolved: bool | None = None
    # Jira task context (src.review.review_defaults says what each means).
    # The router checks the vocabulary, the project keys and the field id.
    task_context_enabled: bool | None = None
    task_project_keys: list[str] | None = Field(default=None, max_length=50)
    task_acceptance_field: str | None = Field(default=None, max_length=64)
    task_include_comments: int | None = Field(default=None, ge=0, le=10)
    business_logic_auto: str | None = Field(default=None, max_length=32)
    # Feedback learning: off | shadow | on, and whose verdicts teach nothing.
    learning_suppression: str | None = Field(default=None, max_length=32)
    learning_excluded_reviewers: list[str] | None = Field(default=None, max_length=100)
    requirements_check_mode: str | None = Field(default=None, max_length=32)
    task_urls_enabled: bool | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _guidelines_fit(self) -> ReviewPolicyIn:
        from src.review.prompt_guidelines import GUIDELINES_MAX_CHARS

        for agent, text in (self.agent_prompt_guidelines or {}).items():
            if isinstance(text, str) and len(text.strip()) > GUIDELINES_MAX_CHARS:
                raise ValueError(
                    f"agent_prompt_guidelines.{agent}: at most "
                    f"{GUIDELINES_MAX_CHARS} characters ({len(text.strip())} given)")
        return self


class ReviewPolicyOut(BaseModel):
    """Full policy detail."""

    repo_slug: str
    enabled: bool
    prompt_template: str
    # What THIS policy says (None = inherit the workspace review defaults).
    target_branches: list[str] | None = None
    target_branches_effective: list[str] = Field(default_factory=list)
    folder_rules: list[FolderRule]
    department: str | None
    created_at: datetime
    updated_at: datetime
    updated_by: str | None
    architect_model: str | None = None
    security_model: str | None = None
    quality_model: str | None = None
    tests_model: str | None = None
    verifier_model: str | None = None
    # Per-repo per-agent system prompts. Declared here because the router has
    # always passed it and `model_config` does not forbid extras: when this
    # line was dropped while `agent_llm_overrides` was being added, pydantic
    # silently swallowed the keyword, the detail page loaded every prompt box
    # empty, and the first save PUT those empty boxes back over the stored
    # prompts. A field the router sends must be declared, or the drop is
    # invisible until the data is gone.
    agent_prompt_overrides: dict[str, str] = Field(default_factory=dict)
    # This repository's team guidelines per agent (ADDED to the prompt), and
    # the agents whose guidelines extend the workspace's.
    agent_prompt_guidelines: dict[str, str] = Field(default_factory=dict)
    agent_guidelines_extend: list[str] = Field(default_factory=list)
    # What THIS policy overrides — {} for a policy that overrides nothing.
    agent_llm_overrides: dict[str, dict] = Field(default_factory=dict)
    # What each agent would actually run with if a review started now, after
    # the whole chain (this policy → workspace `agents` entry → review profile
    # → ReviewSettings) has had its say: {"architect": {"model": "gemini/…",
    # "max_output_tokens": 16384, "reasoning": null}, …}.
    #
    # Here because the screen with the most authority was showing the least:
    # an operator setting a model on /admin/review-policies could not see that
    # a ceiling and a reasoning level existed at all, let alone which ones were
    # in force. `model` is the LiteLLM string, so the UI can hand it straight
    # to GET /api/llm/model-capabilities and render the same limits the save
    # will be validated against. Empty when the workspace config cannot be
    # read — the form still has to render.
    agents_effective: dict[str, dict] = Field(default_factory=dict)
    mcp_sources: list[dict] = Field(default_factory=list)
    # What THIS policy says (None = inherit the workspace review defaults) and
    # which agents a review starting now would actually skip.
    disabled_agents: list[str] | None = None
    disabled_agents_effective: list[str] = Field(default_factory=list)
    # What THIS policy says: None when it inherits the code default.
    suppressed_rules: list[str] | None = None
    # What the prefilter will actually hide if a review started now — the
    # policy's list, or the code default it inherits. Shown beside the
    # override for the same reason `agents_effective` is: the layer that wins
    # has to be able to show what it is winning over.
    suppressed_rules_effective: list[str] = Field(default_factory=list)
    # What THIS policy says about the veto: None when it inherits.
    verifier_enabled: bool | None = None
    # Whether a review starting now would actually run it — the policy's
    # answer, or the install default it inherits. Beside the override for the
    # reason `suppressed_rules_effective` is: the layer that wins has to show
    # what it is winning over.
    verifier_enabled_effective: bool = False
    # What "inherit" resolves to (the workspace review default, else
    # REVIEW_VERIFIER_ENABLED), so a reset control can say what it resets to.
    verifier_enabled_default: bool = False
    # What THIS policy says (None = inherit) and what a review would apply.
    ignore_globs: list[str] | None = None
    ignore_globs_effective: list[str] = Field(default_factory=list)
    comment_min_severity: str | None = None
    comment_min_severity_effective: str = "info"
    # Review output. None = inherit the workspace default (then on).
    summary_enabled: bool | None = None
    summary_enabled_effective: bool = True
    summary_instructions: str | None = None
    summary_instructions_effective: str | None = None
    started_comment_enabled: bool | None = None
    started_comment_enabled_effective: bool = True
    # What THIS policy says (None = inherit) and what a review would use.
    review_language: str | None = None
    review_language_effective: str = "en"
    max_inline_comments: int | None = None
    max_inline_comments_effective: int = 20
    performance_model: str | None = None
    business_logic_model: str | None = None
    # ── 2.3.0: what THIS policy says (None = inherit) and what a review
    # starting now would apply. `sources` / `inherited` /
    # `inherited_sources` carry every one of them too.
    enabled_agents: list[str] | None = None
    enabled_agents_effective: list[str] = Field(default_factory=list)
    run_on_drafts: bool | None = None
    run_on_drafts_effective: bool = False
    approve_when_clean: bool | None = None
    approve_when_clean_effective: bool = False
    request_changes_on_critical: bool | None = None
    request_changes_on_critical_effective: bool = False
    status_feedback: bool | None = None
    status_feedback_effective: bool = True
    committable_suggestions: bool | None = None
    committable_suggestions_effective: bool = False
    apply_filters_to_rules: bool | None = None
    apply_filters_to_rules_effective: bool = True
    summary_target: str | None = None
    summary_target_effective: str = "comment"
    summary_on_new_commits: str | None = None
    summary_on_new_commits_effective: str = "replace"
    summary_existing_description: str | None = None
    summary_existing_description_effective: str = "append"
    base_instruction: str | None = None
    base_instruction_effective: str | None = None
    message_started: str | None = None
    message_started_effective: str | None = None
    message_finished_header: str | None = None
    message_finished_header_effective: str | None = None
    completed_comment: str | None = None
    completed_comment_effective: str = "completed"
    commands_guide_enabled: bool | None = None
    commands_guide_enabled_effective: bool = True
    review_cadence: str | None = None
    review_cadence_effective: str = "automatic"
    review_scope: str | None = None
    review_scope_effective: str = "incremental"
    auto_pause_pushes: int | None = None
    auto_pause_pushes_effective: int = 3
    auto_pause_window_minutes: int | None = None
    auto_pause_window_minutes_effective: int = 15
    ignored_title_keywords: list[str] | None = None
    ignored_title_keywords_effective: list[str] = Field(default_factory=list)
    commands_enabled: bool | None = None
    commands_enabled_effective: bool = True
    chat_enabled: bool | None = None
    chat_enabled_effective: bool = True
    command_permission: str | None = None
    command_permission_effective: str = "repo_access"
    memories_enabled: bool | None = None
    memories_enabled_effective: bool = True
    knowledge_approval: bool | None = None
    knowledge_approval_effective: bool = True
    memory_trusted_commenters: list[str] | None = None
    memory_trusted_commenters_effective: list[str] = Field(default_factory=list)
    issues_auto_resolve: bool | None = None
    issues_auto_resolve_effective: bool = True
    issues_resolve_llm_verify: bool | None = None
    issues_resolve_llm_verify_effective: bool = True
    issues_resolve_max_llm: int | None = None
    issues_resolve_max_llm_effective: int = 8
    issues_announce_resolved: bool | None = None
    issues_announce_resolved_effective: bool = True
    task_context_enabled: bool | None = None
    task_context_enabled_effective: bool = True
    task_project_keys: list[str] | None = None
    task_project_keys_effective: list[str] = Field(default_factory=list)
    task_acceptance_field: str | None = None
    task_acceptance_field_effective: str | None = None
    task_include_comments: int | None = None
    task_include_comments_effective: int = 0
    business_logic_auto: str | None = None
    business_logic_auto_effective: str = "off"
    learning_suppression: str | None = None
    learning_suppression_effective: str = "shadow"
    learning_excluded_reviewers: list[str] | None = None
    learning_excluded_reviewers_effective: list[str] = Field(default_factory=list)
    requirements_check_mode: str | None = None
    requirements_check_mode_effective: str = "checklist"
    task_urls_enabled: bool | None = None
    task_urls_enabled_effective: bool = False
    # agent → takes part in a review starting now (both lists and the
    # built-in participation map folded together), and that map itself.
    agent_participation_effective: dict[str, bool] = Field(default_factory=dict)
    agent_participation_defaults: dict[str, bool] = Field(default_factory=dict)
    # The closed vocabularies (field → choices, built-in first) and the
    # placeholders a message template may use — so a page renders its
    # controls from what the server accepts.
    setting_choices: dict[str, list[str]] = Field(default_factory=dict)
    message_placeholders: list[str] = Field(default_factory=list)
    # The agents a per-repo system prompt may be set for (the LLM finders
    # plus the verifier) and the agents a custom rule may target (the
    # finders), in roster order — so the page renders a box per agent the
    # server accepts instead of a list of its own that goes stale.
    overridable_agents: list[str] = Field(default_factory=list)
    rule_target_agents: list[str] = Field(default_factory=list)
    # The output-language codes `review_language` accepts.
    review_languages: list[str] = Field(default_factory=list)
    # Per inheritable field: which layer the effective value comes from —
    # "repo" (this policy), "workspace" (the workspace review defaults,
    # /admin/review-defaults) or "install" (env / built-in).
    sources: dict[str, str] = Field(default_factory=dict)
    # Per inheritable field: what it would be if this policy said nothing —
    # the value a "reset to inherited" control resets to.
    inherited: dict[str, Any] = Field(default_factory=dict)
    # Per inheritable field: where that inherited value comes from —
    # "workspace" or "install".
    inherited_sources: dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)


class WorkspaceReviewDefaultsIn(BaseModel):
    """PUT /api/review-defaults — the active workspace's review defaults.

    A PATCH in effect: a key ABSENT keeps what is stored, null goes back to
    the install default, a value sets the workspace default. `agents` (per
    agent model / ceiling / reasoning / temperature) and `review_language`
    are written to the workspace LLM config — their one workspace home — with
    the same validation /api/llm/config applies.
    """

    disabled_agents: list[str] | None = Field(default=None, max_length=20)
    verifier_enabled: bool | None = None
    comment_min_severity: str | None = None
    max_inline_comments: int | None = Field(default=None, ge=1, le=100)
    summary_enabled: bool | None = None
    summary_instructions: str | None = Field(default=None, max_length=4000)
    started_comment_enabled: bool | None = None
    ignore_globs: list[str] | None = Field(default=None, max_length=200)
    target_branches: list[str] | None = Field(default=None, max_length=50)
    suppressed_rules: list[str] | None = Field(default=None, max_length=200)
    review_language: str | None = Field(default=None, max_length=16)
    # Sent WHOLE like the /settings/llm `agents` block: {} clears every
    # override, {"agent": null} clears one, absent keeps the stored map.
    agents: dict[str, dict | None] | None = None
    # ── 2.3.0 review settings (src.review.review_defaults says what each
    # means and its built-in). Enums and agent names are checked by the
    # router; blank text is stored as null (= the built-in).
    enabled_agents: list[str] | None = Field(default=None, max_length=20)
    run_on_drafts: bool | None = None
    approve_when_clean: bool | None = None
    request_changes_on_critical: bool | None = None
    status_feedback: bool | None = None
    committable_suggestions: bool | None = None
    apply_filters_to_rules: bool | None = None
    summary_target: str | None = Field(default=None, max_length=32)
    summary_on_new_commits: str | None = Field(default=None, max_length=32)
    summary_existing_description: str | None = Field(default=None, max_length=32)
    base_instruction: str | None = Field(default=None, max_length=2000)
    message_started: str | None = Field(default=None, max_length=2000)
    message_finished_header: str | None = Field(default=None, max_length=2000)
    completed_comment: str | None = Field(default=None, max_length=32)
    commands_guide_enabled: bool | None = None
    review_cadence: str | None = Field(default=None, max_length=32)
    review_scope: str | None = Field(default=None, max_length=32)
    auto_pause_pushes: int | None = Field(default=None, ge=2, le=20)
    auto_pause_window_minutes: int | None = Field(default=None, ge=1, le=240)
    ignored_title_keywords: list[str] | None = Field(default=None, max_length=50)
    commands_enabled: bool | None = None
    chat_enabled: bool | None = None
    command_permission: str | None = Field(default=None, max_length=32)
    # Learning (src.review.memories): memories on/off, whether a machine's
    # proposal waits for a person, and whose "remember" is active at once.
    memories_enabled: bool | None = None
    knowledge_approval: bool | None = None
    memory_trusted_commenters: list[str] | None = Field(default=None, max_length=100)
    # Issues backlog: resolve a merged PR's open issue once the target branch
    # no longer has it. Booleans inherit on null; the cap is 0..50 model calls.
    issues_auto_resolve: bool | None = None
    issues_resolve_llm_verify: bool | None = None
    issues_resolve_max_llm: int | None = Field(default=None, ge=0, le=50)
    issues_announce_resolved: bool | None = None
    # Jira task context (src.review.review_defaults says what each means).
    # The router checks the vocabulary, the project keys and the field id.
    task_context_enabled: bool | None = None
    task_project_keys: list[str] | None = Field(default=None, max_length=50)
    task_acceptance_field: str | None = Field(default=None, max_length=64)
    task_include_comments: int | None = Field(default=None, ge=0, le=10)
    business_logic_auto: str | None = Field(default=None, max_length=32)
    # Feedback learning: off | shadow | on, and whose verdicts teach nothing.
    learning_suppression: str | None = Field(default=None, max_length=32)
    learning_excluded_reviewers: list[str] | None = Field(default=None, max_length=100)
    requirements_check_mode: str | None = Field(default=None, max_length=32)
    task_urls_enabled: bool | None = None

    model_config = ConfigDict(extra="forbid")


class WorkspaceReviewDefaultsOut(BaseModel):
    """GET /api/review-defaults — what this workspace says (None = inherit
    the install default), what is in force, and where each value comes from."""

    workspace_id: str
    disabled_agents: list[str] | None = None
    verifier_enabled: bool | None = None
    comment_min_severity: str | None = None
    max_inline_comments: int | None = None
    summary_enabled: bool | None = None
    summary_instructions: str | None = None
    started_comment_enabled: bool | None = None
    ignore_globs: list[str] | None = None
    target_branches: list[str] | None = None
    suppressed_rules: list[str] | None = None
    review_language: str | None = None
    # 2.3.0 — what this workspace says; None = the built-in (`install`).
    enabled_agents: list[str] | None = None
    run_on_drafts: bool | None = None
    approve_when_clean: bool | None = None
    request_changes_on_critical: bool | None = None
    status_feedback: bool | None = None
    committable_suggestions: bool | None = None
    apply_filters_to_rules: bool | None = None
    summary_target: str | None = None
    summary_on_new_commits: str | None = None
    summary_existing_description: str | None = None
    base_instruction: str | None = None
    message_started: str | None = None
    message_finished_header: str | None = None
    completed_comment: str | None = None
    commands_guide_enabled: bool | None = None
    review_cadence: str | None = None
    review_scope: str | None = None
    auto_pause_pushes: int | None = None
    auto_pause_window_minutes: int | None = None
    ignored_title_keywords: list[str] | None = None
    commands_enabled: bool | None = None
    chat_enabled: bool | None = None
    command_permission: str | None = None
    memories_enabled: bool | None = None
    knowledge_approval: bool | None = None
    memory_trusted_commenters: list[str] | None = None
    issues_auto_resolve: bool | None = None
    issues_resolve_llm_verify: bool | None = None
    issues_resolve_max_llm: int | None = None
    issues_announce_resolved: bool | None = None
    task_context_enabled: bool | None = None
    task_project_keys: list[str] | None = None
    task_acceptance_field: str | None = None
    task_include_comments: int | None = None
    business_logic_auto: str | None = None
    learning_suppression: str | None = None
    learning_excluded_reviewers: list[str] | None = None
    requirements_check_mode: str | None = None
    task_urls_enabled: bool | None = None
    # agent → takes part for a repository that overrides nothing, and the
    # built-in participation map (False = opt-in, named in enabled_agents).
    agent_participation_effective: dict[str, bool] = Field(default_factory=dict)
    agent_participation_defaults: dict[str, bool] = Field(default_factory=dict)
    setting_choices: dict[str, list[str]] = Field(default_factory=dict)
    message_placeholders: list[str] = Field(default_factory=list)
    # The workspace `agents` blob from the LLM config (model included at this
    # layer) and what each agent runs with when a repo overrides nothing.
    agents: dict[str, dict] = Field(default_factory=dict)
    agents_effective: dict[str, dict] = Field(default_factory=dict)
    # Install defaults: what null resolves to, per field.
    install: dict[str, Any] = Field(default_factory=dict)
    # Effective value per field for a repository that overrides nothing.
    effective: dict[str, Any] = Field(default_factory=dict)
    sources: dict[str, str] = Field(default_factory=dict)
    # Roster / vocabulary the page renders its controls from.
    toggleable_agents: list[str] = Field(default_factory=list)
    llm_agents: list[str] = Field(default_factory=list)
    review_languages: list[str] = Field(default_factory=list)
    comment_severity_levels: list[str] = Field(default_factory=list)
    # How many repo policies of this workspace override each field — a
    # default changed here does nothing for those repositories.
    repo_overrides: dict[str, int] = Field(default_factory=dict)
    can_edit: bool = False
    updated_by: str | None = None
    updated_at: datetime | None = None


class AgentPromptOverrideRepo(BaseModel):
    """One repository that overrides an agent's system prompt."""

    repo_slug: str
    updated_at: datetime | None = None


class AgentOverridesSummary(BaseModel):
    """GET /api/review-policies/overrides-summary — who overrides what, in
    the caller's active workspace and among the repos the caller may read."""

    #: agent → the repositories overriding its system prompt.
    prompt_overrides: dict[str, list[AgentPromptOverrideRepo]] = Field(default_factory=dict)
    #: agent → the repositories with team guidelines of their own for it.
    guideline_overrides: dict[str, list[AgentPromptOverrideRepo]] = Field(default_factory=dict)


class ReviewPolicyListItem(BaseModel):
    """Compact row for admin listing — includes derived branch summary."""

    repo_slug: str
    department: str | None
    enabled: bool
    # Effective — the repo's own list, or the workspace default it inherits.
    target_branches: list[str]
    has_custom_prompt: bool  # True if prompt_template != ''
    folder_rules_count: int
    disabled_agents: list[str] = Field(default_factory=list)
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReviewSettingsWorkspaceSummary(BaseModel):
    """The workspace layer of GET /api/review-settings/overview."""

    workspace_id: str
    #: How many settings the workspace defaults set (non-null), and which.
    set_count: int = 0
    set_fields: list[str] = Field(default_factory=list)
    can_edit: bool = False
    updated_by: str | None = None
    updated_at: datetime | None = None


class ReviewSettingsRepoSummary(BaseModel):
    """One repository of GET /api/review-settings/overview — Kodus'
    per-repository list: what it overrides, and how its last review went."""

    repo_slug: str
    full_name: str
    provider: str
    #: False when the repository has no policy row (it overrides nothing).
    has_policy: bool = False
    #: The policy's on/off switch (True without a row).
    review_enabled: bool = True
    #: How many workspace-defaultable settings this repo overrides — the
    #: "Overridden N" badge — and which ones.
    overridden_count: int = 0
    overridden_fields: list[str] = Field(default_factory=list)
    #: complete | partial | skipped | failed, of the most recently reviewed
    #: PR of this repository; None when none was reviewed (or unreadable).
    last_review_status: str | None = None
    last_review_at: datetime | None = None


class ReviewSettingsOverview(BaseModel):
    """GET /api/review-settings/overview — the active workspace's defaults and
    every repository the caller may read, in one cheap call."""

    workspace: ReviewSettingsWorkspaceSummary
    repositories: list[ReviewSettingsRepoSummary] = Field(default_factory=list)


class RepoBranchesOut(BaseModel):
    """Branches of a repository for a picker — from the provider when the
    workspace has a token for it, else from the local clone.

    `branches` is at most `limit` names matching `q`; `total` counts every
    match, so `total > len(branches)` means "refine the search". `truncated`
    says the provider listing itself was cut at the server's cap, so a branch
    may exist that no search here can find.
    """

    repo_slug: str
    branches: list[str]
    default_branch: str | None
    total: int = 0
    truncated: bool = False
    #: "provider", "clone" or "none" (nothing could be read).
    source: str = "none"
    #: Why the list is empty when it is: "no_credential", "provider_error".
    error: str | None = None
