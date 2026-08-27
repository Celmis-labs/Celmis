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


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    is_admin: bool
    auth_method: str
    has_password: bool
    has_google: bool
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
    provider: str = Field(pattern="^(github|gitlab|bitbucket)$")
    token: str = Field(min_length=4, max_length=512)
    # Bitbucket needs email (Atlassian API token uses email:token Basic auth)
    email: str | None = None
    workspace: str | None = None  # Bitbucket only
    account_label: str = "default"


class ConnectionVerifyResult(BaseModel):
    ok: bool
    provider: str
    username: str | None = None
    error: str | None = None
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


class RepoOut(BaseModel):
    slug: str  # internal slug e.g. github_owner-name
    provider: str
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
    cost_source: str | None = None      # 'openrouter_actual' | 'litellm_estimate' | 'unknown' | 'mixed'
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


# ═══════════════════════════════════════════════════════════════════
# Phase 2 — Projects + Chats + Q&A
# ═══════════════════════════════════════════════════════════════════


class ProjectRepoIn(BaseModel):
    """Adding a repo to a project — POST body."""

    repo_slug: str = Field(min_length=1, max_length=200)
    role: str | None = Field(default=None, max_length=64)


class ProjectRepoOut(BaseModel):
    repo_slug: str
    role: str | None
    added_at: datetime
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
    """Glob pattern → extra prompt fragment applied when files match."""

    pattern: str = Field(min_length=1, max_length=200, description="Glob like 'src/api/**/*.py'")
    prompt: str = Field(min_length=1, max_length=4000)

    model_config = ConfigDict(extra="forbid")


class ReviewPolicyIn(BaseModel):
    """Body for PUT /api/review-policies/{slug} — full upsert."""

    enabled: bool = True
    prompt_template: str = Field(default="", max_length=20_000)
    target_branches: list[str] = Field(default_factory=list, max_length=50)
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
    # Per-repo per-agent system prompt overrides. Empty string / missing key
    # means "inherit /admin/agents global override → agent default".
    agent_prompt_overrides: dict[str, str] = Field(default_factory=dict)
    # Per-repo MCP evidence sources (Stage 13).
    mcp_sources: list[dict] = Field(default_factory=list)
    # Agents that must not run for this repo (no LLM call, no findings).
    # Unknown names are dropped by the router.
    disabled_agents: list[str] = Field(default_factory=list, max_length=20)
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

    model_config = ConfigDict(extra="forbid")


class ReviewPolicyOut(BaseModel):
    """Full policy detail."""

    repo_slug: str
    enabled: bool
    prompt_template: str
    target_branches: list[str]
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
    disabled_agents: list[str] = Field(default_factory=list)
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

    model_config = ConfigDict(from_attributes=True)


class ReviewPolicyListItem(BaseModel):
    """Compact row for admin listing — includes derived branch summary."""

    repo_slug: str
    department: str | None
    enabled: bool
    target_branches: list[str]
    has_custom_prompt: bool  # True if prompt_template != ''
    folder_rules_count: int
    disabled_agents: list[str] = Field(default_factory=list)
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RepoBranchesOut(BaseModel):
    """Available branches in the cloned repo — used by the UI to populate checkboxes."""

    repo_slug: str
    branches: list[str]
    default_branch: str | None
